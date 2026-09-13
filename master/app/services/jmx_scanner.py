"""JMX 脚本扫描：提取线程组参数，并基于用户定义变量解析变量引用。

解析规则：
- 线程组支持 ThreadGroup / SetUpThreadGroup / TearDownThreadGroup：
  禁用的线程组仍会返回（enabled=False，参数为脚本静态配置，供前端展示
  开关），执行时整组不运行；TestPlan 禁用则整体不返回；
- 线程组内部仍逐层判断 enabled，禁用的控制器/取样器节点及其子树整体跳过；
- 变量作用域：TestPlan 下 hashTree 直接子级的 Arguments 为全局变量，对所有
  线程组生效；线程组子树内的 Arguments 仅对当前线程组生效，同名时覆盖全局；
- 属性引用 ${var} 时按上述作用域解析；${__P(key, default)} 解析为 default
  （平台约定：实际值执行时经 -J 参数注入）；
- TPS 在线程组子树内按文档顺序深度优先查找第一个【生效】的常量吞吐量定时器
  （ConstantThroughputTimer）：定时器可挂在组直接子级、事务/逻辑控制器或
  取样器下；"生效"指定时器自身 enabled 且其控制器/取样器祖先链均 enabled
  （JMeter 禁用节点整棵子树不执行）。定时器 throughput 单位为样本/分钟
  （TPM），扫描结果按 tps = throughput / 60 换算返回；无生效定时器或值
  无法解析时 tps=0（不限速）。
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

from app.services.exceptions import BusinessError

# 线程组节点标签（标准 ThreadGroup + Setup/Teardown 变体，属性结构相同）
_THREAD_GROUP_TAGS = frozenset(
    {
        "ThreadGroup",
        "SetUpThreadGroup",
        "TearDownThreadGroup",
    }
)

# ThreadGroup 直接 stringProp 属性名
_PROP_NUM_THREADS = "ThreadGroup.num_threads"
_PROP_RAMP_TIME = "ThreadGroup.ramp_time"
_PROP_DURATION = "ThreadGroup.duration"

# ThreadGroup 直接 boolProp 属性名
_PROP_SCHEDULER = "ThreadGroup.scheduler"

# LoopController 属性名（嵌套在 ThreadGroup.main_controller elementProp 内）
_PROP_CONTINUE_FOREVER = "LoopController.continue_forever"
_PROP_LOOPS = "LoopController.loops"

# main_controller elementProp 的 name
_PROP_MAIN_CONTROLLER = "ThreadGroup.main_controller"

# 常量吞吐量定时器（ConstantThroughputTimer）相关
_TIMER_TAG = "ConstantThroughputTimer"
_PROP_THROUGHPUT = "throughput"

# 最内层占位符：${...} 内不含 $、{、}（嵌套时先解析最内层，多轮迭代收敛）
_PLACEHOLDER_RE = re.compile(r"\$\{([^{}]*)\}")
_INT_RE = re.compile(r"-?\d+")
_FLOAT_RE = re.compile(r"-?\d+(?:\.\d+)?")
_FUNC_P_RE = re.compile(r"__P\((.*)\)", re.DOTALL)
# 变量引用解析的迭代上限（防循环引用死循环）
_MAX_RESOLVE_DEPTH = 10


@dataclass
class ThreadGroupInfo:
    """线程组参数信息（变量引用已按作用域解析，无法解析的字段取默认值 0）。"""

    name: str
    testclass: str
    num_threads: int
    ramp_time: int
    loops: int  # -1 表示无限循环（continue_forever=true）
    scheduler: bool
    duration: int  # 0 表示未设置或 scheduler=false
    # 组内启用的常量吞吐量定时器换算的目标 TPS（throughput TPM / 60），
    # 允许小数；无定时器/禁用/无法解析为 0.0（不限速）
    tps: float = 0.0
    # 线程组节点自身启用状态（缺省 true）；禁用组仍返回，执行时整组不运行
    enabled: bool = True


@dataclass
class JmxScanResult:
    """JMX 扫描结果。"""

    thread_groups: list[ThreadGroupInfo]


@dataclass
class ThreadGroupDiff:
    """两份 JMX 的启用线程组差异（按名称+类型比对，不含线程数等参数值）。

    场景设置按线程组名称落库，因此替换 JMX 时线程组标识必须保持一致；
    线程数/rampUp/循环/持续时间等参数允许不同（执行时由场景设置覆盖）。
    """

    added: list[str]  # 新文件相对旧文件多出的线程组名
    removed: list[str]  # 新文件相对旧文件缺失的线程组名
    # 同名但 testclass 不同：[{"name", "old_type", "new_type"}]
    type_changed: list[dict[str, str]]

    @property
    def is_consistent(self) -> bool:
        return not (self.added or self.removed or self.type_changed)

    def describe(self) -> str:
        parts: list[str] = []
        if self.removed:
            parts.append(f"缺失线程组: {', '.join(self.removed)}")
        if self.added:
            parts.append(f"新增线程组: {', '.join(self.added)}")
        for c in self.type_changed:
            parts.append(
                f"线程组 {c['name']} 类型由 {c['old_type']} 变为 {c['new_type']}"
            )
        return "；".join(parts)


def compare_thread_groups(
    old_groups: list[ThreadGroupInfo], new_groups: list[ThreadGroupInfo]
) -> ThreadGroupDiff:
    """比对两份扫描结果的【启用】线程组标识（名称 → testclass）是否一致。

    禁用线程组不参与比对（场景设置只覆盖启用组，脚本作者在新版中停用/新增
    一个禁用组不影响既有场景配置的有效性）。
    """
    old_map = {g.name: g.testclass for g in old_groups if g.enabled}
    new_map = {g.name: g.testclass for g in new_groups if g.enabled}
    removed = [name for name in old_map if name not in new_map]
    added = [name for name in new_map if name not in old_map]
    type_changed = [
        {"name": name, "old_type": old_map[name], "new_type": new_map[name]}
        for name in old_map
        if name in new_map and old_map[name] != new_map[name]
    ]
    return ThreadGroupDiff(added=added, removed=removed, type_changed=type_changed)


def _is_enabled(node: ET.Element) -> bool:
    """判断节点是否启用：enabled 属性缺省为 true（JMeter 未显式标注即启用）。"""
    return node.get("enabled", "true").lower() != "false"


def _get_string_prop(node: ET.Element, name: str) -> str | None:
    """从 node 子孙节点中查找指定 name 的 stringProp 值。"""
    for prop in node.iter("stringProp"):
        if prop.get("name", "") == name:
            return (prop.text or "").strip()
    return None


def _get_bool_prop(node: ET.Element, name: str) -> bool:
    """从 node 子孙节点中查找指定 name 的 boolProp 值。"""
    for prop in node.iter("boolProp"):
        if prop.get("name", "") == name:
            return (prop.text or "").strip().lower() == "true"
    return False


def _resolve_placeholder(inner: str, lookup: Callable[[str], str | None]) -> str | None:
    """解析单个占位符内容，返回替换值；无法解析返回 None（保留原样）。

    - __P(key, default)：返回 default（平台约定实际值执行时经 -J 注入）；
      无 default 的 ${__P(key)} 运行时才确定，返回 None；
    - 普通变量：按作用域查找定义值（定义值可能仍含引用，由外层迭代继续解析）。
    """
    p_match = _FUNC_P_RE.fullmatch(inner)
    if p_match is not None:
        parts = p_match.group(1).split(",", 1)
        if len(parts) < 2:
            return None
        default = parts[1].strip()
        # 去除 default 两端成对引号
        if len(default) >= 2 and default[0] == default[-1] and default[0] in "'\"":
            default = default[1:-1]
        return default
    return lookup(inner)


def _sub_placeholders(expr: str, lookup: Callable[[str], str | None]) -> str:
    """替换一轮全部最内层占位符；无法解析的保持原样。"""

    def _repl(m: re.Match[str]) -> str:
        val = _resolve_placeholder(m.group(1).strip(), lookup)
        return m.group(0) if val is None else val

    return _PLACEHOLDER_RE.sub(_repl, expr)


def _resolve_expression(expr: str, lookup: Callable[[str], str | None]) -> str:
    """迭代解析表达式中的变量引用（支持链式/嵌套引用），直至收敛或达迭代上限。"""
    for _ in range(_MAX_RESOLVE_DEPTH):
        resolved = _sub_placeholders(expr, lookup)
        if resolved == expr:
            break
        expr = resolved
    return expr


def _resolve_int(
    raw: str | None, lookup: Callable[[str], str | None], default: int
) -> tuple[int, str | None]:
    """解析整型属性：返回 (值, 未解析时的原始表达式)。

    raw 为空表示属性未设置，返回 default 且不算未解析。
    解析结果非整数（含未解析变量）时返回 default 并携带 raw 供上层记录。
    """
    if not raw:
        return default, None
    resolved = _resolve_expression(raw, lookup).strip()
    if _INT_RE.fullmatch(resolved):
        return int(resolved), None
    return default, raw


def _resolve_float(
    raw: str | None, lookup: Callable[[str], str | None], default: float
) -> float:
    """解析浮点属性（如定时器 throughput）；未设置或无法解析返回 default。"""
    if not raw:
        return default
    resolved = _resolve_expression(raw, lookup).strip()
    if _FLOAT_RE.fullmatch(resolved):
        return float(resolved)
    return default


def _extract_arguments(elem: ET.Element) -> dict[str, str]:
    """提取 Arguments（用户定义变量）节点内的 name-value 对，按文档顺序覆盖。"""
    result: dict[str, str] = {}
    for ep in elem.iter("elementProp"):
        name = ""
        value = ""
        for sp in ep.iter("stringProp"):
            prop_name = sp.get("name", "")
            if prop_name == "Argument.name":
                name = (sp.text or "").strip()
            elif prop_name == "Argument.value":
                value = (sp.text or "").strip()
        if name:
            result[name] = value
    return result


def _next_hash_tree(children: list[ET.Element], idx: int) -> ET.Element | None:
    """找到 children[idx] 的下一个兄弟 <hashTree>（JMeter 用它承载子节点）。"""
    for sibling in children[idx + 1 :]:
        if sibling.tag == "hashTree":
            return sibling
    return None


def _collect_variables(tree: ET.Element) -> dict[str, str]:
    """递归收集子树内的用户变量（跳过禁用节点及其子树）。"""
    result: dict[str, str] = {}
    children = list(tree)
    for idx, child in enumerate(children):
        if child.tag == "hashTree":
            continue
        if not _is_enabled(child):
            continue
        if child.tag == "Arguments":
            result.update(_extract_arguments(child))
        child_tree = _next_hash_tree(children, idx)
        if child_tree is not None:
            result.update(_collect_variables(child_tree))
    return result


def _extract_loops(elem: ET.Element, lookup: Callable[[str], str | None]) -> int:
    """从 ThreadGroup 提取循环次数。

    continue_forever=true 返回 -1（无限循环）；未找到 main_controller 默认 1；
    无法解析的引用取默认值 1。
    """
    for child in elem:  # 仅查直接子节点
        if child.tag != "elementProp":
            continue
        if child.get("name", "") != _PROP_MAIN_CONTROLLER:
            continue
        if _get_bool_prop(child, _PROP_CONTINUE_FOREVER):
            return -1
        value, _ = _resolve_int(_get_string_prop(child, _PROP_LOOPS), lookup, 1)
        return value
    return 1


def _timer_throughput_raw(timer: ET.Element) -> str | None:
    """读取常量吞吐量定时器的 throughput 原始表达式（TPM）。

    兼容 JMeter 5.x TestBean 结构（doubleProp 内 name/value 子元素）与
    旧版直接写在 doubleProp 文本上的形式。
    """
    for double_prop in timer.findall("doubleProp"):
        name_elem = double_prop.find("name")
        if name_elem is not None and (name_elem.text or "").strip() == _PROP_THROUGHPUT:
            value_elem = double_prop.find("value")
            if value_elem is not None:
                return (value_elem.text or "").strip()
        if double_prop.get("name", "") == _PROP_THROUGHPUT:
            return (double_prop.text or "").strip()
    return None


def _collect_effective_timers(tree: ET.Element) -> list[ET.Element]:
    """按文档顺序 DFS 收集子树内【生效】的常量吞吐量定时器。

    生效 = 定时器自身 enabled 且其控制器/取样器祖先链均 enabled：
    遇到禁用节点时整棵子树不下钻（与 JMeter 执行语义一致）。
    遍历沿每个启用元素紧邻的配对 hashTree 递归，hashTree 本身不是节点。
    与 jmx_assembler 的同名收集逻辑必须保持同口径（扫描值=执行写入目标）。
    """
    found: list[ET.Element] = []
    children = list(tree)
    for idx, child in enumerate(children):
        if child.tag == "hashTree" or not _is_enabled(child):
            continue
        if child.tag == _TIMER_TAG:
            found.append(child)
        sub = _next_hash_tree(children, idx)
        if sub is not None:
            found.extend(_collect_effective_timers(sub))
    return found


def _extract_tps(
    tg_tree: ET.Element | None, lookup: Callable[[str], str | None]
) -> float:
    """提取线程组子树内第一个生效定时器并换算为 TPS（throughput TPM / 60）。

    搜索范围覆盖组直接子级、事务/逻辑控制器、取样器下的定时器（DFS 文档
    顺序）；无生效定时器返回 0.0。第一个生效定时器的值为 0 或无法解析时
    同样返回 0.0，不再顺延后续定时器（以第一个表达脚本作者限速意图）。
    """
    if tg_tree is None:
        return 0.0
    timers = _collect_effective_timers(tg_tree)
    if not timers:
        return 0.0
    tpm = _resolve_float(_timer_throughput_raw(timers[0]), lookup, 0.0)
    return round(tpm / 60, 2) if tpm > 0 else 0.0


def _extract_thread_group(
    elem: ET.Element,
    lookup: Callable[[str], str | None],
    tg_tree: ET.Element | None = None,
) -> ThreadGroupInfo:
    """从 ThreadGroup 节点提取全部参数（变量引用已解析，无法解析取默认值）。"""
    num_threads, _ = _resolve_int(_get_string_prop(elem, _PROP_NUM_THREADS), lookup, 0)
    ramp_time, _ = _resolve_int(_get_string_prop(elem, _PROP_RAMP_TIME), lookup, 0)
    duration, _ = _resolve_int(_get_string_prop(elem, _PROP_DURATION), lookup, 0)
    return ThreadGroupInfo(
        name=elem.get("testname", ""),
        testclass=elem.get("testclass", elem.tag),
        num_threads=num_threads,
        ramp_time=ramp_time,
        loops=_extract_loops(elem, lookup),
        scheduler=_get_bool_prop(elem, _PROP_SCHEDULER),
        duration=duration,
        tps=_extract_tps(tg_tree, lookup),
        enabled=_is_enabled(elem),
    )


def _process_thread_group(
    elem: ET.Element, tg_tree: ET.Element | None, global_vars: dict[str, str]
) -> ThreadGroupInfo:
    """处理单个线程组：先收集其子树内变量，再按作用域解析参数。"""
    tg_vars: dict[str, str] = _collect_variables(tg_tree) if tg_tree is not None else {}
    # 作用域：全局变量打底，线程组内同名变量覆盖
    scope = dict(global_vars)
    scope.update(tg_vars)
    return _extract_thread_group(elem, scope.get, tg_tree)


def scan_jmx(jmx_bytes: bytes) -> JmxScanResult:
    """解析 JMX，返回线程组参数（变量已按作用域解析）。

    TestPlan 禁用则不返回任何线程组；禁用的线程组仍返回（enabled=False），
    其内部禁用的控制器/取样器子树在变量与定时器收集时剪枝。全局变量先于
    线程组收集（两遍遍历，与元素在计划中的先后顺序无关）。
    """
    try:
        root = ET.fromstring(jmx_bytes)
    except ET.ParseError as exc:
        raise BusinessError(f"JMX 解析失败: {exc}", code=3005) from exc

    result = JmxScanResult(thread_groups=[])
    # 根 <jmeterTestPlan> 下第一个 <hashTree> 承载 TestPlan
    top_tree = root.find("hashTree")
    if top_tree is None:
        return result

    children = list(top_tree)
    plan_elem = next((c for c in children if c.tag != "hashTree"), None)
    if plan_elem is not None and not _is_enabled(plan_elem):
        return result
    # TestPlan 的子 hashTree 承载全局配置与线程组；缺失时回退顶层（容错）
    plan_tree = top_tree
    if plan_elem is not None:
        idx = children.index(plan_elem)
        elem_tree = _next_hash_tree(children, idx)
        if elem_tree is not None:
            plan_tree = elem_tree

    plan_children = list(plan_tree)
    # 第一遍：收集全局变量（对所有线程组生效）
    global_vars: dict[str, str] = {}
    for child in plan_children:
        if child.tag == "hashTree" or not _is_enabled(child):
            continue
        if child.tag == "Arguments":
            global_vars.update(_extract_arguments(child))
    # 第二遍：处理线程组（携带全局变量作用域）。禁用线程组同样返回
    # （enabled=False，参数为脚本静态配置），执行时整组不运行；组内部
    # 禁用的控制器/取样器子树仍由变量/定时器收集逻辑剪枝。
    for idx, child in enumerate(plan_children):
        if child.tag == "hashTree":
            continue
        if child.tag in _THREAD_GROUP_TAGS:
            result.thread_groups.append(
                _process_thread_group(
                    child, _next_hash_tree(plan_children, idx), global_vars
                )
            )
    return result
