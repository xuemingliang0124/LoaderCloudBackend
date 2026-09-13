"""JMX 脚本组装：按场景设置改写原始 JMX，生成执行用脚本（原始脚本不动）。

职责：
- 按 thread_group_settings 改写对应线程组的启用状态（enabled 属性）与
  num_threads / ramp_time / scheduler / duration。
- 循环次数不再由场景传入，按场景类型统一收口：
  * 非基准场景：无限循环（continue_forever=true），运行时长由调度器
    duration 控制，避免有限循环先于时长结束；
  * 单交易基准：固定循环 100 次、关闭调度器。
- 按 tps 设置线程组内常量吞吐量定时器（ConstantThroughputTimer）：
  多机执行时 tps 是该 Agent 的均摊份额（orchestrator 按 CPU 权重拆分，
  可为小数，各机份额之和等于场景配置的集群目标 TPS）；单机执行即为总量。
  JMeter 定时器吞吐单位为样本/分钟（TPM），写入值 = tps × 60。
  定时器在子树内按文档顺序 DFS 查找（可挂在组直接子级、事务/逻辑控制器
  或取样器下）：
  * tps>0：复用第一个祖先链启用的定时器（自身被禁用也算，写入时启用），
    避免重复新建；仅当所有定时器都处在禁用控制器/取样器子树（或子树内
    根本没有定时器）时，才在线程组 hashTree 顶部补建 calcMode=0 定时器；
  * tps=0：递归禁用所有【生效】定时器（自身及祖先链均启用），已禁用或
    处于禁用祖先下的不动，不补建。
  "生效"口径与 jmx_scanner 扫描一致（扫描只返回生效集合）。
- 单交易基准场景在 TestPlan 下开启"独立运行每个线程组"
  （TestPlan.serialize_threadgroups=true）。

注意：单交易基准的线程数固定 5、关闭调度器等参数约束由调用方
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

# 常量吞吐量定时器（ConstantThroughputTimer）相关
_TIMER_TAG = "ConstantThroughputTimer"
_TIMER_TESTNAME = "Constant Throughput Timer"
_PROP_CALC_MODE = "calcMode"
_PROP_THROUGHPUT = "throughput"

# 单交易基准场景固定循环次数（固定线程数 5 在 orchestrator 处理）
_BASELINE_LOOPS = 100


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


def _build_throughput_timer(tpm: float) -> ET.Element:
    """构造常量吞吐量定时器节点（calcMode=0：仅当前线程组，单位样本/分钟）。"""
    timer = ET.Element(
        _TIMER_TAG,
        {
            "guiclass": "TestBeanGUI",
            "testclass": _TIMER_TAG,
            "testname": _TIMER_TESTNAME,
            "enabled": "true",
        },
    )
    ET.SubElement(timer, "intProp", {"name": _PROP_CALC_MODE}).text = "0"
    double_prop = ET.SubElement(timer, "doubleProp")
    ET.SubElement(double_prop, "name").text = _PROP_THROUGHPUT
    ET.SubElement(double_prop, "value").text = f"{float(tpm):.1f}"
    ET.SubElement(double_prop, "savedValue").text = "0.0"
    return timer


def _set_timer_throughput(timer: ET.Element, tpm: float) -> None:
    """启用定时器并把 throughput（TPM）写入 doubleProp；结构缺失时补齐。"""
    timer.set("enabled", "true")
    throughput_prop = None
    for double_prop in timer.findall("doubleProp"):
        name_elem = double_prop.find("name")
        if name_elem is not None and (name_elem.text or "").strip() == _PROP_THROUGHPUT:
            throughput_prop = double_prop
            break
    if throughput_prop is None:
        throughput_prop = ET.SubElement(timer, "doubleProp")
        ET.SubElement(throughput_prop, "name").text = _PROP_THROUGHPUT
        ET.SubElement(throughput_prop, "savedValue").text = "0.0"
    value_elem = throughput_prop.find("value")
    if value_elem is None:
        value_elem = ET.SubElement(throughput_prop, "value")
    value_elem.text = f"{float(tpm):.1f}"


def _resolve_tg_subtree(plan_tree: ET.Element, thread_group: ET.Element) -> ET.Element:
    """取线程组紧邻的 hashTree 子树（承载组内定时器/取样器）；缺失则补建。"""
    children = list(plan_tree)
    idx = children.index(thread_group)
    if idx + 1 < len(children) and children[idx + 1].tag == "hashTree":
        return children[idx + 1]
    subtree = ET.Element("hashTree")
    plan_tree.insert(idx + 1, subtree)
    return subtree


def _paired_hash_tree(children: list[ET.Element], idx: int) -> ET.Element | None:
    """取 children[idx] 紧邻的下一个兄弟 hashTree（JMeter 节点配对结构，不补建）。"""
    if idx + 1 < len(children) and children[idx + 1].tag == "hashTree":
        return children[idx + 1]
    return None


def _node_enabled(node: ET.Element) -> bool:
    """JMeter 节点 enabled 缺省为 true。"""
    return node.get("enabled", "true").lower() != "false"


def _collect_timers(
    tree: ET.Element, include_self_disabled: bool, out: list[ET.Element]
) -> None:
    """按文档顺序 DFS 收集线程组子树内的常量吞吐量定时器。

    控制器/取样器祖先链必须全部启用才下钻（禁用节点整棵子树不执行，
    与 JMeter 语义一致）；定时器节点自身：
    - include_self_disabled=False：仅收集自身启用的（"生效"集合，
      与 jmx_scanner._collect_effective_timers 同口径，tps=0 禁用用）；
    - include_self_disabled=True：祖先启用即收集，含自身禁用的
      （tps>0 复用时写入即启用，避免在组内重复新建定时器）。
    定时器节点的配对 hashTree 下不再递归（其下不会嵌套有效定时器）。
    """
    children = list(tree)
    for idx, child in enumerate(children):
        if child.tag == "hashTree":
            continue
        if child.tag == _TIMER_TAG:
            if _node_enabled(child) or include_self_disabled:
                out.append(child)
            continue
        if not _node_enabled(child):
            continue  # 禁用的控制器/取样器：整棵子树剪枝
        sub = _paired_hash_tree(children, idx)
        if sub is not None:
            _collect_timers(sub, include_self_disabled, out)


def _apply_throughput_timer(
    plan_tree: ET.Element, thread_group: ET.Element, tps: float
) -> None:
    """按 tps（多机时为本机均摊份额，可为小数）配置线程组吞吐量定时器。

    搜索范围为整个线程组子树（组直接子级 + 事务/逻辑控制器 + 取样器下）：
    tps>0：第一个祖先链启用的定时器置 throughput=tps×60 并启用（复用脚本
           作者放置位置及其作用域），其余定时器不动；无任何祖先启用的
           定时器时才在组树顶部补建（作用整个线程组）；
    tps=0：递归禁用所有生效定时器（自身及祖先链均启用，不删除并保留原值），
           已禁用或处于禁用祖先下的不动，不补建。
    """
    tg_tree = _resolve_tg_subtree(plan_tree, thread_group)
    if tps > 0:
        reusable: list[ET.Element] = []
        _collect_timers(tg_tree, include_self_disabled=True, out=reusable)
        tpm = tps * 60
        if reusable:
            _set_timer_throughput(reusable[0], tpm)
        else:
            # JMX 中每个节点与其 hashTree 成对出现，插到组树顶部即可作用全组
            tg_tree.insert(0, _build_throughput_timer(tpm))
            tg_tree.insert(1, ET.Element("hashTree"))
    else:
        effective: list[ET.Element] = []
        _collect_timers(tg_tree, include_self_disabled=False, out=effective)
        for timer in effective:
            timer.set("enabled", "false")


def _apply_thread_group(
    plan_tree: ET.Element,
    elem: ET.Element,
    setting: ScenarioScriptTG,
    scenario_type: ScenarioType,
) -> None:
    """按设置改写单个线程组节点的启用状态、加压参数与组内吞吐量定时器。"""
    # 启用开关写入节点属性：false 时整组不执行（其余参数仍照常写入，
    # 保留配置以便重新启用时生效）
    elem.set("enabled", "true" if setting.enabled else "false")
    _set_string_prop(elem, _PROP_NUM_THREADS, str(setting.num_threads))
    _set_string_prop(elem, _PROP_RAMP_TIME, str(setting.ramp_time))
    _set_bool_prop(elem, _PROP_SCHEDULER, setting.scheduler)
    if setting.scheduler:
        _set_string_prop(elem, _PROP_DURATION, str(setting.duration))
    else:
        _set_string_prop(elem, _PROP_DURATION, "0")
    # 循环策略按场景类型收口：基准固定 100 次，其余无限循环（时长收口）
    if scenario_type == ScenarioType.SINGLE_BASELINE:
        _set_loops(elem, _BASELINE_LOOPS)
    else:
        _set_loops(elem, -1)
    _apply_throughput_timer(plan_tree, elem, setting.tps)


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
            _apply_thread_group(plan_tree, child, setting, scenario_type)

    # 声明编码并序列化
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
