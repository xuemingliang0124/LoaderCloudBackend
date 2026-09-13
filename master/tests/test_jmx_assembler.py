"""JMX 脚本组装单测：覆盖正常场景、基准场景标志、线程组匹配、循环策略收口、
吞吐量定时器 TPS×60=TPM、异常等。

验证策略：组装后用 jmx_scanner.scan_jmx 回读线程组参数，实现 round-trip 校验；
定时器参数直接解析 XML 断言（scanner 不扫描定时器）；
同时直接断言 TestPlan.serialize_threadgroups 的文本存在性。
"""

import xml.etree.ElementTree as ET

import pytest

from app.models.enums import ScenarioType
from app.models.scenario_script_tg import ScenarioScriptTG
from app.services import jmx_assembler, orchestrator
from app.services.exceptions import BusinessError
from app.services.jmx_scanner import scan_jmx

# ---------- 测试用 JMX 片段 ----------

_JMX_STANDARD = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
      <stringProp name="TestPlan.comments"></stringProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="用户登录" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">10</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">50</stringProp>
        <stringProp name="ThreadGroup.ramp_time">30</stringProp>
        <boolProp name="ThreadGroup.scheduler">false</boolProp>
        <stringProp name="ThreadGroup.duration"></stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 两个线程组：用于验证匹配键 (name, testclass) 与不匹配保持原值
_JMX_TWO_TGS = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="TG1" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
        <boolProp name="ThreadGroup.scheduler">false</boolProp>
        <stringProp name="ThreadGroup.duration">0</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="TG2" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">7</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">7</stringProp>
        <stringProp name="ThreadGroup.ramp_time">7</stringProp>
        <boolProp name="ThreadGroup.scheduler">false</boolProp>
        <stringProp name="ThreadGroup.duration">0</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# scheduler=true + duration 的 JMX，用于验证关闭调度器时 duration 置 0
_JMX_SCHEDULER = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="定时组" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">-1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">99</stringProp>
        <stringProp name="ThreadGroup.ramp_time">99</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 两个线程组：一个脚本中禁用，一个 enabled 属性缺省（按 true）
_JMX_TG_ENABLED_STATES = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="脚本禁用组" enabled="false">
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
        <stringProp name="ThreadGroup.ramp_time">0</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="缺省启用组">
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
        <stringProp name="ThreadGroup.ramp_time">0</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 线程组内已存在常量吞吐量定时器（JMeter 5.x TestBean 结构，初始禁用）
_JMX_WITH_TIMER = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="定时器组" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
        <stringProp name="ThreadGroup.ramp_time">0</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree>
        <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="Constant Throughput Timer" enabled="false">
          <intProp name="calcMode">0</intProp>
          <doubleProp>
            <name>throughput</name>
            <value>6.0</value>
            <savedValue>0.0</savedValue>
          </doubleProp>
        </ConstantThroughputTimer>
        <hashTree/>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 定时器挂在启用的事务控制器子树下（初始启用、值 6.0）
_JMX_NESTED_TIMER = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="嵌套组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="事务" enabled="true"/>
        <hashTree>
          <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="Constant Throughput Timer" enabled="true">
            <intProp name="calcMode">0</intProp>
            <doubleProp>
              <name>throughput</name>
              <value>6.0</value>
              <savedValue>0.0</savedValue>
            </doubleProp>
          </ConstantThroughputTimer>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 定时器挂在启用的取样器子树下
_JMX_SAMPLER_TIMER = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="取样器组" enabled="true">
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree>
        <HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="下单" enabled="true"/>
        <hashTree>
          <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="Constant Throughput Timer" enabled="true">
            <intProp name="calcMode">0</intProp>
            <doubleProp>
              <name>throughput</name>
              <value>6.0</value>
              <savedValue>0.0</savedValue>
            </doubleProp>
          </ConstantThroughputTimer>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 定时器挂在【禁用】的事务控制器子树下（自身启用但不执行）
_JMX_TIMER_DISABLED_ANCESTOR = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="禁用祖先组" enabled="true">
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="禁用事务" enabled="false"/>
        <hashTree>
          <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="Constant Throughput Timer" enabled="true">
            <intProp name="calcMode">0</intProp>
            <doubleProp>
              <name>throughput</name>
              <value>6.0</value>
              <savedValue>0.0</savedValue>
            </doubleProp>
          </ConstantThroughputTimer>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 组直接子级 + 控制器内各一个启用定时器（6000 / 1200 TPM）
_JMX_TWO_TIMERS = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true"/>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="双定时器组" enabled="true">
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">600</stringProp>
      </ThreadGroup>
      <hashTree>
        <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="组级定时器" enabled="true">
          <intProp name="calcMode">0</intProp>
          <doubleProp>
            <name>throughput</name>
            <value>6000.0</value>
            <savedValue>0.0</savedValue>
          </doubleProp>
        </ConstantThroughputTimer>
        <hashTree/>
        <TransactionController guiclass="TransactionControllerGui" testclass="TransactionController" testname="事务" enabled="true"/>
        <hashTree>
          <ConstantThroughputTimer guiclass="TestBeanGUI" testclass="ConstantThroughputTimer" testname="控制器内定时器" enabled="true">
            <intProp name="calcMode">0</intProp>
            <doubleProp>
              <name>throughput</name>
              <value>1200.0</value>
              <savedValue>0.0</savedValue>
            </doubleProp>
          </ConstantThroughputTimer>
          <hashTree/>
        </hashTree>
      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
""".encode("utf-8")

# 非法 XML
_JMX_BAD = b"<?xml version='1.0'?><jmeterTestPlan><hashTree><broken></hashTree>"

# 无 TestPlan 的畸形 JMX
_JMX_NO_PLAN = b"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="x"/>
  </hashTree>
</jmeterTestPlan>
"""


def _tg(name="用户登录", testclass="ThreadGroup", **kw) -> ScenarioScriptTG:
    """构造线程组设置对象（不写库，仅承载参数）。"""
    return ScenarioScriptTG(
        thread_group_name=name,
        testclass=testclass,
        enabled=kw.get("enabled", True),
        num_threads=kw.get("num_threads", 100),
        ramp_time=kw.get("ramp_time", 20),
        tps=kw.get("tps", 0),
        scheduler=kw.get("scheduler", True),
        duration=kw.get("duration", 900),
    )


def _tg_timers(assembled: bytes, tg_name: str) -> list[dict]:
    """回读指定线程组 hashTree 直接子级定时器列表（enabled/throughput/calcMode）。"""
    root = ET.fromstring(assembled)
    plan_tree = root.find("hashTree").find("hashTree")
    children = list(plan_tree)
    timers = []
    for i, child in enumerate(children):
        if child.tag == "ThreadGroup" and child.get("testname") == tg_name:
            subtree = children[i + 1]
            for node in list(subtree):
                if node.tag != "ConstantThroughputTimer":
                    continue
                value = ""
                for dp in node.findall("doubleProp"):
                    name_el = dp.find("name")
                    if name_el is not None and (name_el.text or "") == "throughput":
                        value = (dp.find("value").text or "").strip()
                timers.append({"enabled": node.get("enabled"), "throughput": value})
    return timers


def _tg_subtree(assembled: bytes, tg_name: str) -> ET.Element:
    """取指定线程组紧邻的 hashTree 子树（承载任意深度的控制器/取样器/定时器）。"""
    root = ET.fromstring(assembled)
    plan_tree = root.find("hashTree").find("hashTree")
    children = list(plan_tree)
    for i, child in enumerate(children):
        if child.tag == "ThreadGroup" and child.get("testname") == tg_name:
            return children[i + 1]
    raise AssertionError(f"未找到线程组 {tg_name}")


def _all_timer_info(assembled: bytes, tg_name: str) -> list[dict]:
    """列出组子树内【任意深度】全部定时器（含禁用/禁用祖先下），按文档顺序。"""
    subtree = _tg_subtree(assembled, tg_name)
    info = []
    for node in subtree.iter("ConstantThroughputTimer"):
        value = ""
        for dp in node.findall("doubleProp"):
            name_el = dp.find("name")
            if name_el is not None and (name_el.text or "") == "throughput":
                value = (dp.find("value").text or "").strip()
        info.append(
            {
                "testname": node.get("testname"),
                "enabled": node.get("enabled"),
                "throughput": value,
            }
        )
    return info


# ---------- 普通场景：按 DB 设置改写 ----------


def test_normal_scenario_uses_db_settings():
    assembled = jmx_assembler.assemble_jmx(_JMX_STANDARD, [_tg()], ScenarioType.MIXED)
    tg = scan_jmx(assembled).thread_groups[0]
    assert tg.num_threads == 100
    assert tg.ramp_time == 20
    # 非基准场景循环策略收口为无限循环（时长控制结束）
    assert tg.loops == -1
    assert tg.scheduler is True
    assert tg.duration == 900
    # 非基准场景不开启独立运行线程组
    assert "TestPlan.serialize_threadgroups" not in assembled.decode()


def test_scheduler_false_sets_duration_zero():
    setting = _tg(name="定时组", scheduler=False, duration=900)
    assembled = jmx_assembler.assemble_jmx(
        _JMX_SCHEDULER, [setting], ScenarioType.SINGLE_LOAD
    )
    tg = scan_jmx(assembled).thread_groups[0]
    assert tg.scheduler is False
    assert tg.duration == 0
    # 非基准场景即便保存值 scheduler=False，循环仍统一为无限
    assert tg.loops == -1


# ---------- 线程组启用状态写入 ----------


def test_enabled_flag_written_to_tg_nodes():
    """场景开关写入节点 enabled 属性：禁用保持 false，缺省属性显式写 true。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TG_ENABLED_STATES,
        [_tg(name="脚本禁用组", enabled=False), _tg(name="缺省启用组")],
        ScenarioType.MIXED,
    )
    states = {g.name: g.enabled for g in scan_jmx(assembled).thread_groups}
    assert states == {"脚本禁用组": False, "缺省启用组": True}
    # 禁用组的加压参数仍被写入（保留配置，重新启用即生效）
    disabled_tg = next(
        g for g in scan_jmx(assembled).thread_groups if g.name == "脚本禁用组"
    )
    assert disabled_tg.num_threads == 100


def test_disabled_in_script_can_be_enabled_by_scenario():
    """脚本中禁用的组可被场景设置启用（组装结果 enabled=true）。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TG_ENABLED_STATES,
        [_tg(name="脚本禁用组", enabled=True), _tg(name="缺省启用组")],
        ScenarioType.MIXED,
    )
    states = {g.name: g.enabled for g in scan_jmx(assembled).thread_groups}
    assert states["脚本禁用组"] is True


# ---------- 基准场景：固定循环 100 + serialize 标志 ----------


def test_baseline_forces_loops_100():
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg()], ScenarioType.SINGLE_BASELINE
    )
    tg = scan_jmx(assembled).thread_groups[0]
    assert tg.loops == 100


def test_baseline_adds_serialize_threadgroups_true():
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg()], ScenarioType.SINGLE_BASELINE
    )
    text = assembled.decode()
    assert 'name="TestPlan.serialize_threadgroups"' in text
    assert ">true</boolProp>" in text.split("serialize_threadgroups", 1)[1][:80]


def test_baseline_sets_serialize_even_if_already_false():
    jmx = _JMX_STANDARD.replace(
        b'<TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">\n      <stringProp name="TestPlan.comments"></stringProp>',
        b'<TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">\n      <stringProp name="TestPlan.comments"></stringProp>\n      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>',
    )
    assembled = jmx_assembler.assemble_jmx(jmx, [_tg()], ScenarioType.SINGLE_BASELINE)
    root = ET.fromstring(assembled)
    plan = root.find(".//TestPlan")
    serialize = next(
        p
        for p in plan.iter("boolProp")
        if p.get("name") == "TestPlan.serialize_threadgroups"
    )
    assert serialize.text == "true"


# ---------- 吞吐量定时器：TPS ×60 = TPM ----------


def test_existing_timer_gets_tpm_and_enabled():
    """组内已有定时器：throughput=tps×60（100TPS→6000TPM），并启用。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_WITH_TIMER, [_tg(name="定时器组", tps=100)], ScenarioType.MIXED
    )
    timers = _tg_timers(assembled, "定时器组")
    assert len(timers) == 1
    assert timers[0]["enabled"] == "true"
    assert timers[0]["throughput"] == "6000.0"


def test_timer_tps_zero_disables_existing_timer():
    """tps=0 表示不限速：禁用组内既有定时器，不新建。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_WITH_TIMER, [_tg(name="定时器组", tps=0)], ScenarioType.MIXED
    )
    timers = _tg_timers(assembled, "定时器组")
    assert len(timers) == 1
    assert timers[0]["enabled"] == "false"
    assert timers[0]["throughput"] == "6.0"  # 原值保留


def test_timer_auto_created_when_absent():
    """组内无定时器且 tps>0：组树顶部补建 calcMode=0 的定时器，值为 TPM。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg(tps=50)], ScenarioType.MIXED
    )
    timers = _tg_timers(assembled, "用户登录")
    assert len(timers) == 1
    assert timers[0]["enabled"] == "true"
    assert timers[0]["throughput"] == "3000.0"
    root = ET.fromstring(assembled)
    timer = next(root.iter("ConstantThroughputTimer"))
    calc = next(p for p in timer if p.tag == "intProp" and p.get("name") == "calcMode")
    assert calc.text == "0"  # 仅作用于当前线程组


def test_no_timer_created_when_tps_zero():
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg(tps=0)], ScenarioType.MIXED
    )
    assert _tg_timers(assembled, "用户登录") == []


def test_fractional_tps_share_writes_tpm():
    """多机均摊的小数份额（0.6 TPS）→ 36 TPM，定时器 throughput 为 double。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_WITH_TIMER,
        [_tg(name="定时器组", tps=0.6)],
        ScenarioType.MIXED,
    )
    timers = _tg_timers(assembled, "定时器组")
    assert timers[0]["enabled"] == "true"
    assert timers[0]["throughput"] == "36.0"


# ---------- 吞吐量定时器：子树递归查找（控制器/取样器下） ----------


def test_nested_controller_timer_reused_not_created():
    """定时器在启用的事务控制器下：原地改写为 6000 TPM，组直接子级不新增。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_NESTED_TIMER, [_tg(name="嵌套组", tps=100)], ScenarioType.MIXED
    )
    # 组直接子级没有定时器（没有补建）
    assert _tg_timers(assembled, "嵌套组") == []
    all_timers = _all_timer_info(assembled, "嵌套组")
    assert len(all_timers) == 1  # 原定时器被复用，未新增第二个
    assert all_timers[0]["enabled"] == "true"
    assert all_timers[0]["throughput"] == "6000.0"
    # round-trip：扫描器从同一子树读回 100 TPS
    assert scan_jmx(assembled).thread_groups[0].tps == 100.0


def test_sampler_timer_reused():
    """定时器在启用的取样器下：原地改写为 3000 TPM（50 TPS），不新增。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_SAMPLER_TIMER, [_tg(name="取样器组", tps=50)], ScenarioType.MIXED
    )
    assert _tg_timers(assembled, "取样器组") == []
    all_timers = _all_timer_info(assembled, "取样器组")
    assert len(all_timers) == 1
    assert all_timers[0]["enabled"] == "true"
    assert all_timers[0]["throughput"] == "3000.0"
    assert scan_jmx(assembled).thread_groups[0].tps == 50.0


def test_timer_under_disabled_controller_triggers_create():
    """定时器仅存在于禁用控制器子树：视为无可用定时器，组顶部补建新的。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TIMER_DISABLED_ANCESTOR,
        [_tg(name="禁用祖先组", tps=100)],
        ScenarioType.MIXED,
    )
    all_timers = _all_timer_info(assembled, "禁用祖先组")
    assert len(all_timers) == 2  # 禁用祖先下旧定时器不动 + 组顶部新建
    root_timers = _tg_timers(assembled, "禁用祖先组")
    assert len(root_timers) == 1
    assert root_timers[0]["enabled"] == "true"
    assert root_timers[0]["throughput"] == "6000.0"
    # 旧的仍挂在禁用事务下、保持 enabled=true（但其不执行，扫描不计）
    assert scan_jmx(assembled).thread_groups[0].tps == 100.0


def test_first_timer_document_order_root_preferred():
    """组级与控制器内各有定时器：只改写文档顺序第一个（组级），控制器内不动。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TWO_TIMERS, [_tg(name="双定时器组", tps=100)], ScenarioType.MIXED
    )
    all_timers = _all_timer_info(assembled, "双定时器组")
    assert len(all_timers) == 2
    assert all_timers[0]["testname"] == "组级定时器"
    assert all_timers[0]["throughput"] == "6000.0"
    assert all_timers[1]["testname"] == "控制器内定时器"
    assert all_timers[1]["throughput"] == "1200.0"  # 原值保留，不被改写


def test_tps_zero_disables_nested_timer():
    """tps=0 不限速：递归禁用控制器下的生效定时器，值保留，不补建。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_NESTED_TIMER, [_tg(name="嵌套组", tps=0)], ScenarioType.MIXED
    )
    all_timers = _all_timer_info(assembled, "嵌套组")
    assert len(all_timers) == 1
    assert all_timers[0]["enabled"] == "false"
    assert all_timers[0]["throughput"] == "6.0"  # 原值保留
    assert _tg_timers(assembled, "嵌套组") == []  # 未补建
    assert scan_jmx(assembled).thread_groups[0].tps == 0.0


def test_tps_zero_keeps_timer_under_disabled_ancestor_untouched():
    """禁用控制器下的定时器本就不生效：tps=0 时不动它，也不补建。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TIMER_DISABLED_ANCESTOR,
        [_tg(name="禁用祖先组", tps=0)],
        ScenarioType.MIXED,
    )
    all_timers = _all_timer_info(assembled, "禁用祖先组")
    assert len(all_timers) == 1
    assert all_timers[0]["enabled"] == "true"  # 未被改动
    assert _tg_timers(assembled, "禁用祖先组") == []


def test_baseline_carries_tps_to_timer():
    """基准场景 tps 同样透传到定时器（基准只固定线程数/循环/调度器）。"""
    assembled = jmx_assembler.assemble_jmx(
        _JMX_WITH_TIMER,
        [_tg(name="定时器组", tps=20)],
        ScenarioType.SINGLE_BASELINE,
    )
    timers = _tg_timers(assembled, "定时器组")
    assert timers[0]["enabled"] == "true"
    assert timers[0]["throughput"] == "1200.0"
    assert scan_jmx(assembled).thread_groups[0].loops == 100


# ---------- 线程组匹配 ----------


def test_match_by_name_and_testclass():
    settings = [
        _tg(name="TG1", testclass="ThreadGroup", num_threads=11),
    ]
    assembled = jmx_assembler.assemble_jmx(_JMX_TWO_TGS, settings, ScenarioType.MIXED)
    groups = {g.name: g for g in scan_jmx(assembled).thread_groups}
    assert groups["TG1"].num_threads == 11
    # TG2 不在设置中，保持原始值（含循环次数与定时器配置）
    assert groups["TG2"].num_threads == 7
    assert groups["TG2"].loops == 7
    assert _tg_timers(assembled, "TG2") == []


def test_testclass_mismatch_keeps_original():
    # 设置的 testclass 是 SetUpThreadGroup，但 JMX 中是 ThreadGroup，不匹配
    setting = _tg(name="TG1", testclass="SetUpThreadGroup", num_threads=999)
    assembled = jmx_assembler.assemble_jmx(_JMX_TWO_TGS, [setting], ScenarioType.MIXED)
    groups = {g.name: g for g in scan_jmx(assembled).thread_groups}
    assert groups["TG1"].num_threads == 1  # 未被改写
    assert groups["TG1"].loops == 1


# ---------- 异常分支 ----------


def test_invalid_xml_raises_business_error():
    with pytest.raises(BusinessError) as exc:
        jmx_assembler.assemble_jmx(_JMX_BAD, [_tg()], ScenarioType.MIXED)
    assert exc.value.code == 3005


def test_missing_testplan_raises_business_error():
    with pytest.raises(BusinessError) as exc:
        jmx_assembler.assemble_jmx(_JMX_NO_PLAN, [_tg()], ScenarioType.MIXED)
    assert exc.value.code == 3005


# ---------- 原始输入不被修改 ----------


def test_original_bytes_unchanged():
    original = bytearray(_JMX_STANDARD)
    snapshot = bytes(original)
    jmx_assembler.assemble_jmx(_JMX_STANDARD, [_tg()], ScenarioType.MIXED)
    assert bytes(original) == snapshot


# ---------- orchestrator 场景设置执行期覆盖 ----------


def test_effective_settings_baseline_override():
    db_setting = _tg(
        num_threads=100, ramp_time=20, tps=80, scheduler=True, duration=900
    )
    eff = orchestrator._effective_thread_group_settings(
        [db_setting], ScenarioType.SINGLE_BASELINE, 600
    )
    tg = eff[0]
    # 固定参数不受场景级运行时间（600）影响
    assert tg.num_threads == 5
    assert tg.scheduler is False
    assert tg.duration == 0
    # ramp_time / tps 沿用保存值（循环策略由 assembler 按类型收口）
    assert tg.ramp_time == 20
    assert tg.tps == 80
    # 名称与类型不变
    assert tg.thread_group_name == db_setting.thread_group_name
    assert tg.testclass == db_setting.testclass


@pytest.mark.parametrize(
    "stype", [ScenarioType.SINGLE_LOAD, ScenarioType.MIXED, ScenarioType.STABILITY]
)
def test_effective_settings_non_baseline_forces_scheduler_duration(stype):
    # 保存值 scheduler=False / duration=900：调度器强制 True，时长用场景级覆盖
    db_setting = _tg(num_threads=100, tps=80, scheduler=False, duration=900)
    eff = orchestrator._effective_thread_group_settings([db_setting], stype, 600)
    tg = eff[0]
    assert tg.scheduler is True
    assert tg.duration == 600
    assert tg.num_threads == 100
    assert tg.tps == 80


def test_effective_settings_returns_new_list():
    db_setting = _tg()
    original_list = [db_setting]
    eff = orchestrator._effective_thread_group_settings(
        original_list, ScenarioType.SINGLE_BASELINE, 0
    )
    # 返回的是新对象，不修改原列表中的对象
    assert eff is not original_list
    assert db_setting.num_threads == 100
