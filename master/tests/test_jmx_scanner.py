"""JMX 线程组扫描单测：覆盖标准线程组、变量作用域解析、__P 默认值、禁用等场景。"""

import pytest

from app.services.exceptions import BusinessError
from app.services.jmx_scanner import (
    JmxScanResult,
    ThreadGroupInfo,
    compare_thread_groups,
    scan_jmx,
)

# ---------- 测试用 JMX 片段 ----------

# 标准线程组：loops=10、scheduler=false，无任何变量
_JMX_STANDARD = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
      <stringProp name="TestPlan.comments"></stringProp>
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="用户登录" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
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
"""

# 全局变量：num_threads/loops 引用全局变量，ramp_time 引用 __P 默认值
_JMX_GLOBAL_VARS = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <Arguments guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments">
          <elementProp name="threads" elementType="Argument">
            <stringProp name="Argument.name">threads</stringProp>
            <stringProp name="Argument.value">50</stringProp>
            <stringProp name="Argument.metadata">=</stringProp>
          </elementProp>
          <elementProp name="loop_count" elementType="Argument">
            <stringProp name="Argument.name">loop_count</stringProp>
            <stringProp name="Argument.value">5</stringProp>
          </elementProp>
          <elementProp name="host" elementType="Argument">
            <stringProp name="Argument.name">host</stringProp>
            <stringProp name="Argument.value">http://demo.local</stringProp>
          </elementProp>
        </collectionProp>
      </Arguments>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="全局变量组" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">${loop_count}</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">${threads}</stringProp>
        <stringProp name="ThreadGroup.ramp_time">${__P(ramp_up,15)}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 线程组内变量覆盖全局：全局 threads=10，TG1 自带 threads=99，TG2 无定义
_JMX_TG_OVERRIDE = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <Arguments guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments">
          <elementProp name="threads" elementType="Argument">
            <stringProp name="Argument.name">threads</stringProp>
            <stringProp name="Argument.value">10</stringProp>
          </elementProp>
        </collectionProp>
      </Arguments>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="覆盖组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">${threads}</stringProp>
      </ThreadGroup>
      <hashTree>
        <Arguments guiclass="ArgumentsPanel" testclass="Arguments" testname="TG变量" enabled="true">
          <collectionProp name="Arguments.arguments">
            <elementProp name="threads" elementType="Argument">
              <stringProp name="Argument.name">threads</stringProp>
              <stringProp name="Argument.value">99</stringProp>
            </elementProp>
          </collectionProp>
        </Arguments>
        <hashTree></hashTree>
      </hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="继承组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">${threads}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 未定义变量引用：num_threads 引用 ${username}（无定义）
_JMX_UNDEFINED_VAR = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="未定义组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">${username}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 链式变量与嵌套 __P：base=5，total=${base}，num=${__P(t,${base})}
_JMX_CHAIN = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <Arguments guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments">
          <elementProp name="base" elementType="Argument">
            <stringProp name="Argument.name">base</stringProp>
            <stringProp name="Argument.value">5</stringProp>
          </elementProp>
          <elementProp name="total" elementType="Argument">
            <stringProp name="Argument.name">total</stringProp>
            <stringProp name="Argument.value">${base}</stringProp>
          </elementProp>
        </collectionProp>
      </Arguments>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="链式组" enabled="true">
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          <stringProp name="LoopController.loops">${total}</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">${__P(t,${base})}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# __P 无默认值：${__P(t)} 运行时注入，无法静态解析
_JMX_P_NO_DEFAULT = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="无默认组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">${__P(t)}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 禁用 Arguments：不参与变量解析
_JMX_DISABLED_VARS = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <Arguments guiclass="ArgumentsPanel" testclass="Arguments" testname="禁用变量" enabled="false">
        <collectionProp name="Arguments.arguments">
          <elementProp name="threads" elementType="Argument">
            <stringProp name="Argument.name">threads</stringProp>
            <stringProp name="Argument.value">777</stringProp>
          </elementProp>
        </collectionProp>
      </Arguments>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="引用组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">${threads}</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 持续加压线程组：continue_forever=true、scheduler=true、duration=300
_JMX_DURATION = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="持续加压" enabled="true">
        <stringProp name="ThreadGroup.on_sample_error">continue</stringProp>
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">true</boolProp>
          <stringProp name="LoopController.loops">1</stringProp>
        </elementProp>
        <stringProp name="ThreadGroup.num_threads">100</stringProp>
        <stringProp name="ThreadGroup.ramp_time">60</stringProp>
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        <stringProp name="ThreadGroup.duration">300</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 禁用线程组 + 启用线程组
_JMX_DISABLED = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="禁用线程组" enabled="false">
        <stringProp name="ThreadGroup.num_threads">999</stringProp>
        <stringProp name="ThreadGroup.ramp_time">1</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="启用线程组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
        <stringProp name="ThreadGroup.ramp_time">5</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 多线程组类型：ThreadGroup + SetUpThreadGroup + TearDownThreadGroup
_JMX_MULTI_TYPE = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <SetUpThreadGroup guiclass="ThreadGroupGui" testclass="SetUpThreadGroup" testname="setUp" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">0</stringProp>
      </SetUpThreadGroup>
      <hashTree></hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="主线程组" enabled="true">
        <stringProp name="ThreadGroup.num_threads">20</stringProp>
        <stringProp name="ThreadGroup.ramp_time">10</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
      <TearDownThreadGroup guiclass="ThreadGroupGui" testclass="TearDownThreadGroup" testname="tearDown" enabled="true">
        <stringProp name="ThreadGroup.num_threads">1</stringProp>
        <stringProp name="ThreadGroup.ramp_time">0</stringProp>
      </TearDownThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 缺省参数：无 main_controller、属性值为空
_JMX_DEFAULTS = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="缺省线程组" enabled="true">
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 空 JMX（只有 TestPlan，无线程组）
_JMX_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="true">
    </TestPlan>
    <hashTree></hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 禁用 TestPlan：整个计划下的内容都应被跳过
_JMX_DISABLED_PLAN = """<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Test Plan" enabled="false">
    </TestPlan>
    <hashTree>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="不应出现" enabled="true">
        <stringProp name="ThreadGroup.num_threads">10</stringProp>
      </ThreadGroup>
      <hashTree></hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""

# 非法 XML
_JMX_INVALID = "<jmeterTestPlan><hashTree>"


# ---------- 基础线程组用例 ----------


def test_standard_thread_group() -> None:
    """标准线程组：无变量，字段原值提取。"""
    result = scan_jmx(_JMX_STANDARD.encode())
    assert len(result.thread_groups) == 1
    g = result.thread_groups[0]
    assert g.name == "用户登录"
    assert g.testclass == "ThreadGroup"
    assert g.num_threads == 50
    assert g.ramp_time == 30
    assert g.loops == 10
    assert g.scheduler is False
    assert g.duration == 0


def test_duration_and_infinite_loops() -> None:
    """持续加压：scheduler=true、duration=300、无限循环返回 -1。"""
    groups = scan_jmx(_JMX_DURATION.encode()).thread_groups
    assert len(groups) == 1
    g = groups[0]
    assert g.name == "持续加压"
    assert g.num_threads == 100
    assert g.ramp_time == 60
    assert g.loops == -1
    assert g.scheduler is True
    assert g.duration == 300


def test_disabled_thread_group_skipped() -> None:
    """禁用线程组被跳过，只返回启用的。"""
    groups = scan_jmx(_JMX_DISABLED.encode()).thread_groups
    assert len(groups) == 1
    assert groups[0].name == "启用线程组"
    assert groups[0].num_threads == 10


def test_multi_type_thread_groups() -> None:
    """SetUpThreadGroup / TearDownThreadGroup 均被识别。"""
    groups = scan_jmx(_JMX_MULTI_TYPE.encode()).thread_groups
    assert len(groups) == 3
    assert groups[0].testclass == "SetUpThreadGroup"
    assert groups[1].testclass == "ThreadGroup"
    assert groups[2].testclass == "TearDownThreadGroup"
    assert groups[1].num_threads == 20


def test_default_values_when_missing_props() -> None:
    """属性缺失时返回合理默认值。"""
    groups = scan_jmx(_JMX_DEFAULTS.encode()).thread_groups
    assert len(groups) == 1
    g = groups[0]
    assert g.name == "缺省线程组"
    assert g.num_threads == 0
    assert g.ramp_time == 0
    assert g.loops == 1  # 缺省 main_controller 时默认 1 次
    assert g.scheduler is False
    assert g.duration == 0


def test_empty_plan_no_thread_groups() -> None:
    """无线程组的 JMX 返回空线程组列表。"""
    result = scan_jmx(_JMX_EMPTY.encode())
    assert result.thread_groups == []


def test_disabled_plan_skips_all() -> None:
    """禁用 TestPlan 下所有内容被跳过。"""
    result = scan_jmx(_JMX_DISABLED_PLAN.encode())
    assert result.thread_groups == []


def test_invalid_xml_raises_business_error() -> None:
    """非法 XML 抛出 BusinessError。"""
    with pytest.raises(BusinessError) as exc_info:
        scan_jmx(_JMX_INVALID.encode())
    assert exc_info.value.code == 3005


def test_return_type() -> None:
    """返回 JmxScanResult，线程组元素为 ThreadGroupInfo 实例。"""
    result = scan_jmx(_JMX_STANDARD.encode())
    assert isinstance(result, JmxScanResult)
    assert all(isinstance(g, ThreadGroupInfo) for g in result.thread_groups)


# ---------- 变量解析用例 ----------


def test_global_variables_resolved() -> None:
    """全局变量对所有线程组生效；__P 解析为默认值。"""
    groups = scan_jmx(_JMX_GLOBAL_VARS.encode()).thread_groups
    assert len(groups) == 1
    g = groups[0]
    assert g.num_threads == 50  # ${threads} → 全局变量 50
    assert g.ramp_time == 15  # ${__P(ramp_up,15)} → 默认值 15
    assert g.loops == 5  # ${loop_count} → 全局变量 5


def test_tg_vars_override_global() -> None:
    """线程组内同名变量覆盖全局，仅对本线程组生效。"""
    groups = scan_jmx(_JMX_TG_OVERRIDE.encode()).thread_groups
    assert len(groups) == 2
    # TG1：线程组内 threads=99 覆盖全局 10
    assert groups[0].name == "覆盖组"
    assert groups[0].num_threads == 99
    # TG2：无线程组内定义，继承全局 threads=10
    assert groups[1].name == "继承组"
    assert groups[1].num_threads == 10


def test_undefined_var_falls_back_to_zero() -> None:
    """未定义变量无法静态解析：字段取默认值 0。"""
    groups = scan_jmx(_JMX_UNDEFINED_VAR.encode()).thread_groups
    assert len(groups) == 1
    assert groups[0].num_threads == 0


def test_chained_and_nested_p_resolution() -> None:
    """链式变量（total→base）与嵌套 __P（${__P(t,${base})}）均解析到底。"""
    groups = scan_jmx(_JMX_CHAIN.encode()).thread_groups
    assert len(groups) == 1
    g = groups[0]
    assert g.num_threads == 5  # __P 默认值里嵌套变量
    assert g.loops == 5  # ${total} → ${base} → 5


def test_p_without_default_falls_back_to_zero() -> None:
    """${__P(t)} 无默认值（执行时经 -J 注入），字段取默认值 0。"""
    groups = scan_jmx(_JMX_P_NO_DEFAULT.encode()).thread_groups
    assert len(groups) == 1
    assert groups[0].num_threads == 0


def test_disabled_arguments_skipped() -> None:
    """禁用的 Arguments 不参与变量解析，引用回退为 0。"""
    groups = scan_jmx(_JMX_DISABLED_VARS.encode()).thread_groups
    assert len(groups) == 1
    assert groups[0].num_threads == 0  # ${threads} 的定义被禁用，无法解析


# ---------- 线程组一致性比对用例 ----------


def _tg(name: str, testclass: str = "ThreadGroup") -> ThreadGroupInfo:
    return ThreadGroupInfo(
        name=name,
        testclass=testclass,
        num_threads=1,
        ramp_time=0,
        loops=1,
        scheduler=False,
        duration=0,
    )


def test_compare_identical_groups() -> None:
    """名称/类型相同（参数值不同也算一致，参数由场景覆盖）。"""
    old = [_tg("A"), _tg("B")]
    new = [
        ThreadGroupInfo("A", "ThreadGroup", 99, 10, 5, True, 300),
        _tg("B"),
    ]
    diff = compare_thread_groups(old, new)
    assert diff.is_consistent is True
    assert diff.added == []
    assert diff.removed == []
    assert diff.type_changed == []


def test_compare_detects_added_and_removed() -> None:
    """新文件多出/缺失线程组被识别。"""
    diff = compare_thread_groups([_tg("A"), _tg("B")], [_tg("B"), _tg("C")])
    assert diff.is_consistent is False
    assert diff.added == ["C"]
    assert diff.removed == ["A"]
    assert "新增线程组" in diff.describe()
    assert "缺失线程组" in diff.describe()


def test_compare_detects_type_change() -> None:
    """同名但线程组类型变化（ThreadGroup→SetUpThreadGroup）判为不一致。"""
    diff = compare_thread_groups(
        [_tg("A", "ThreadGroup")], [_tg("A", "SetUpThreadGroup")]
    )
    assert diff.is_consistent is False
    assert diff.type_changed == [
        {"name": "A", "old_type": "ThreadGroup", "new_type": "SetUpThreadGroup"}
    ]
    assert "类型由" in diff.describe()


def test_compare_empty_groups() -> None:
    """两份无线程组的文件视为一致。"""
    diff = compare_thread_groups([], [])
    assert diff.is_consistent is True


def test_compare_via_scanned_jmx() -> None:
    """端到端：扫描两份线程组名称不同的 JMX，比对应判为不一致。"""
    old = scan_jmx(_JMX_STANDARD.encode()).thread_groups  # 用户登录
    new = scan_jmx(_JMX_DURATION.encode()).thread_groups  # 持续加压
    diff = compare_thread_groups(old, new)
    assert diff.is_consistent is False
    assert diff.removed == ["用户登录"]
    assert diff.added == ["持续加压"]
