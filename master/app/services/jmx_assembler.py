"""JMX 脚本组装：按场景设置改写原始 JMX，生成执行用脚本（原始脚本不动）。

职责：
- 按 thread_group_settings 改写对应线程组的 num_threads / ramp_time /
  loops / scheduler / duration。
- 单交易基准场景在 TestPlan 下开启"独立运行每个线程组"
  （TestPlan.serialize_threadgroups=true）。

注意：单交易基准的线程数固定 5、循环 100、关闭调度器等参数约束由调用方
（orchestrator）在多 Agent 线程拆分前统一处理，组装器只负责把传入的设置
（已是最终值）写入 XML，避免拆分后的 per-Agent 线程数被覆盖。

匹配键：线程组名称 (testname) + 类型 (testclass)，与 jmx_scanner.compare_thread_groups
保持一致。不匹配的线程组保持原始 JMX 值不变。

产物：组装后的 JMX 字节，由调用方上传到 MinIO runs/{run_no}/ 下供 Agent 下载执行。
"""

import xml.etree.ElementTree as ET

from app.models.enums import ScenarioType
from app.models.scenario_script_tg import ScenarioScriptTG
from app.services.exceptions import BusinessError

# 与 jmx_scanner 同源的属性名常量（JMX 结构定义，两处保持一致）
_PROP_NUM_THREADS = "ThreadGroup.num_threads"
_PROP_RAMP_TIME = "ThreadGroup.ramp_time"
_PROP_DURATION = "ThreadGroup.duration"
_PROP_SCHEDULER = "ThreadGroup.scheduler"
_PROP_CONTINUE_FOREVER = "LoopController.continue_forever"
_PROP_LOOPS = "LoopController.loops"
_PROP_MAIN_CONTROLLER = "ThreadGroup.main_controller"
_PROP_SERIALIZE_TG = "TestPlan.serialize_threadgroups"


def _set_string_prop(node: ET.Element, name: str, value: str) -> None:
    """设置 node 直接子节点中指定 name 的 stringProp 值；不存在则新建。"""
    for prop in node.iter("stringProp"):
        if prop.get("name", "") == name:
            prop.text = value
            return
    ET.SubElement(node, "stringProp", {"name": name}).text = value


def _set_bool_prop(node: ET.Element, name: str, value: bool) -> None:
    """设置 node 直接子节点中指定 name 的 boolProp 值；不存在则新建。"""
    for prop in node.iter("boolProp"):
        if prop.get("name", "") == name:
            prop.text = "true" if value else "false"
            return
    ET.SubElement(node, "boolProp", {"name": name}).text = "true" if value else "false"


def _set_loops(thread_group: ET.Element, loops: int) -> None:
    """设置线程组循环次数；loops=-1 表示无限循环（continue_forever=true）。"""
    main_ctrl = None
    for child in thread_group:
        if (
            child.tag == "elementProp"
            and child.get("name", "") == _PROP_MAIN_CONTROLLER
        ):
            main_ctrl = child
            break
    if main_ctrl is None:
        return
    if loops == -1:
        _set_bool_prop(main_ctrl, _PROP_CONTINUE_FOREVER, True)
    else:
        _set_bool_prop(main_ctrl, _PROP_CONTINUE_FOREVER, False)
        _set_string_prop(main_ctrl, _PROP_LOOPS, str(loops))


def _apply_thread_group(elem: ET.Element, setting: ScenarioScriptTG) -> None:
    """按设置改写单个线程组节点的加压参数。"""
    _set_string_prop(elem, _PROP_NUM_THREADS, str(setting.num_threads))
    _set_string_prop(elem, _PROP_RAMP_TIME, str(setting.ramp_time))
    _set_bool_prop(elem, _PROP_SCHEDULER, setting.scheduler)
    if setting.scheduler:
        _set_string_prop(elem, _PROP_DURATION, str(setting.duration))
    else:
        _set_string_prop(elem, _PROP_DURATION, "0")
    _set_loops(elem, setting.loops)


def assemble_jmx(
    original_bytes: bytes,
    thread_group_settings: list[ScenarioScriptTG],
    scenario_type: ScenarioType,
) -> bytes:
    """组装执行用 JMX。

    Args:
        original_bytes: 原始脚本 JMX 字节（只读，不修改原始对象）。
        thread_group_settings: 场景保存的线程组设置列表。
        scenario_type: 场景类型，单交易基准时套用固定参数。

    Returns:
        组装后的 JMX 字节。

    Raises:
        BusinessError(code=3005): JMX 解析失败。
    """
    try:
        root = ET.fromstring(original_bytes)
    except ET.ParseError as exc:
        raise BusinessError(f"JMX 解析失败: {exc}", code=3005) from exc

    # 定位 TestPlan 及其承载配置/线程组的子 hashTree
    top_tree = root.find("hashTree")
    if top_tree is None:
        raise BusinessError("JMX 结构异常：缺少顶层 hashTree", code=3005)
    top_children = list(top_tree)
    plan_elem = next((c for c in top_children if c.tag != "hashTree"), None)
    if plan_elem is None or plan_elem.tag != "TestPlan":
        raise BusinessError("JMX 结构异常：未找到 TestPlan", code=3005)

    # 单交易基准：TestPlan 开启独立运行每个线程组
    if scenario_type == ScenarioType.SINGLE_BASELINE:
        _set_bool_prop(plan_elem, _PROP_SERIALIZE_TG, True)

    # TestPlan 子 hashTree 承载线程组
    plan_tree = top_tree
    idx = top_children.index(plan_elem)
    for sibling in top_children[idx + 1 :]:
        if sibling.tag == "hashTree":
            plan_tree = sibling
            break

    settings_map = {
        (s.thread_group_name, s.testclass): s for s in thread_group_settings
    }

    # 按 (testname, testclass) 匹配线程组并改写（设置已是最终值，调用方已处理基准/拆分）
    for child in list(plan_tree):
        if child.tag in ("ThreadGroup", "SetUpThreadGroup", "TearDownThreadGroup"):
            key = (child.get("testname", ""), child.get("testclass", child.tag))
            setting = settings_map.get(key)
            if setting is None:
                continue
            _apply_thread_group(child, setting)

    # 声明编码并序列化
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
