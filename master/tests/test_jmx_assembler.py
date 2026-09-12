"""JMX 脚本组装单测：覆盖正常场景、基准场景标志、线程组匹配、无限循环、异常等。

验证策略：组装后用 jmx_scanner.scan_jmx 回读参数，实现 round-trip 校验；
同时直接断言 TestPlan.serialize_threadgroups 的文本存在性。
"""

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
        num_threads=kw.get("num_threads", 100),
        ramp_time=kw.get("ramp_time", 20),
        loops=kw.get("loops", 5),
        scheduler=kw.get("scheduler", True),
        duration=kw.get("duration", 900),
    )


# ---------- 普通场景：按 DB 设置改写 ----------

def test_normal_scenario_uses_db_settings():
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg()], ScenarioType.MIXED
    )
    tg = scan_jmx(assembled).thread_groups[0]
    assert tg.num_threads == 100
    assert tg.ramp_time == 20
    assert tg.loops == 5
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


def test_infinite_loops_sets_continue_forever():
    setting = _tg(loops=-1, scheduler=False)
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [setting], ScenarioType.MIXED
    )
    tg = scan_jmx(assembled).thread_groups[0]
    assert tg.loops == -1


# ---------- 基准场景：serialize 标志 ----------

def test_baseline_adds_serialize_threadgroups_true():
    assembled = jmx_assembler.assemble_jmx(
        _JMX_STANDARD, [_tg()], ScenarioType.SINGLE_BASELINE
    )
    text = assembled.decode()
    assert 'name="TestPlan.serialize_threadgroups"' in text
    assert ">true</boolProp>" in text.split("serialize_threadgroups", 1)[1][:80]


def test_baseline_sets_serialize_even_if_already_false():
    jmx = _JMX_STANDARD.replace(
        b"<TestPlan guiclass=\"TestPlanGui\" testclass=\"TestPlan\" testname=\"Test Plan\" enabled=\"true\">\n      <stringProp name=\"TestPlan.comments\"></stringProp>",
        b"<TestPlan guiclass=\"TestPlanGui\" testclass=\"TestPlan\" testname=\"Test Plan\" enabled=\"true\">\n      <stringProp name=\"TestPlan.comments\"></stringProp>\n      <boolProp name=\"TestPlan.serialize_threadgroups\">false</boolProp>",
    )
    assembled = jmx_assembler.assemble_jmx(jmx, [_tg()], ScenarioType.SINGLE_BASELINE)
    # 重新扫描确认 serialize 为 true
    import xml.etree.ElementTree as ET

    root = ET.fromstring(assembled)
    plan = root.find(".//TestPlan")
    serialize = next(
        p for p in plan.iter("boolProp") if p.get("name") == "TestPlan.serialize_threadgroups"
    )
    assert serialize.text == "true"


# ---------- 线程组匹配 ----------

def test_match_by_name_and_testclass():
    settings = [
        _tg(name="TG1", testclass="ThreadGroup", num_threads=11),
    ]
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TWO_TGS, settings, ScenarioType.MIXED
    )
    groups = {g.name: g for g in scan_jmx(assembled).thread_groups}
    assert groups["TG1"].num_threads == 11
    # TG2 不在设置中，保持原始值
    assert groups["TG2"].num_threads == 7
    assert groups["TG2"].loops == 7


def test_testclass_mismatch_keeps_original():
    # 设置的 testclass 是 SetUpThreadGroup，但 JMX 中是 ThreadGroup，不匹配
    setting = _tg(name="TG1", testclass="SetUpThreadGroup", num_threads=999)
    assembled = jmx_assembler.assemble_jmx(
        _JMX_TWO_TGS, [setting], ScenarioType.MIXED
    )
    groups = {g.name: g for g in scan_jmx(assembled).thread_groups}
    assert groups["TG1"].num_threads == 1  # 未被改写


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


# ---------- orchestrator 基准场景设置覆盖 ----------

def test_effective_settings_baseline_override():
    db_setting = _tg(
        num_threads=100, ramp_time=20, loops=5, scheduler=True, duration=900
    )
    eff = orchestrator._effective_thread_group_settings(
        [db_setting], ScenarioType.SINGLE_BASELINE
    )
    tg = eff[0]
    assert tg.num_threads == 5
    assert tg.loops == 100
    assert tg.scheduler is False
    assert tg.duration == 0
    # ramp_time 沿用保存值
    assert tg.ramp_time == 20
    # 名称与类型不变
    assert tg.thread_group_name == db_setting.thread_group_name
    assert tg.testclass == db_setting.testclass


@pytest.mark.parametrize("stype", [ScenarioType.SINGLE_LOAD, ScenarioType.MIXED, ScenarioType.STABILITY])
def test_effective_settings_non_baseline_unchanged(stype):
    db_setting = _tg(num_threads=100, loops=5, scheduler=True, duration=900)
    eff = orchestrator._effective_thread_group_settings([db_setting], stype)
    tg = eff[0]
    assert tg.num_threads == 100
    assert tg.loops == 5
    assert tg.scheduler is True
    assert tg.duration == 900


def test_effective_settings_returns_new_list():
    db_setting = _tg()
    original_list = [db_setting]
    eff = orchestrator._effective_thread_group_settings(
        original_list, ScenarioType.SINGLE_BASELINE
    )
    # 返回的是新对象，不修改原列表中的对象
    assert eff is not original_list
    assert db_setting.num_threads == 100
