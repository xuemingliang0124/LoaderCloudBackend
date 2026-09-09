"""JMX 脚本检查：解析 CSV Data Set Config 引用的数据文件，与上传清单比对。

逐层判断节点 enabled：测试计划 / 线程组 / 事务控制器 / 取样器 / CSVDataSet
本身，任一祖先禁用或自身禁用则跳过该子树。

支持 JMeter 标准 CSVDataSet（filename）与第三方 RandomCSVDataSet（fileName）。
动态引用（含 ${...}）无法静态解析，跳过校验。
"""

import os
import xml.etree.ElementTree as ET

from app.services.exceptions import BusinessError

# CSV Data Set Config 的 filename 属性名（标准与第三方插件大小写不同）
_CSV_FILENAME_PROPS = ("filename", "fileName")

# CSV Data Set Config 节点标签
# - CSVDataSet：JMeter 内置 CSV Data Set Config
# - com.blazemeter.jmeter.RandomCSVDataSetConfig：blazemeter 第三方 Random CSV Data Set Config
_CSV_DATASET_TAGS = frozenset(
    {
        "CSVDataSet",
        "com.blazemeter.jmeter.RandomCSVDataSetConfig",
    }
)


def _is_enabled(node: ET.Element) -> bool:
    """判断节点是否启用：enabled 属性缺省为 true（JMeter 未显式标注即启用）。"""
    return node.get("enabled", "true").lower() != "false"


def _is_csv_dataset(node: ET.Element) -> bool:
    """是否为 CSV 数据配置节点。"""
    return node.tag in _CSV_DATASET_TAGS


def _extract_filename(node: ET.Element) -> str | None:
    """从 CSVDataSet 节点的 stringProp 中提取 filename/fileName 值。

    返回 None 表示空值或变量引用（运行时才能确定，不校验）。
    """
    for prop in node.iter("stringProp"):
        if prop.get("name", "") not in _CSV_FILENAME_PROPS:
            continue
        value = (prop.text or "").strip()
        if not value or "${" in value:
            return None
        return value
    return None


def _find_child_hash_tree(elem: ET.Element, parent: ET.Element) -> ET.Element | None:
    """找到 elem 的下一个兄弟 <hashTree>（JMeter 用它承载 elem 的子节点）。"""
    children = list(parent)
    idx = children.index(elem)
    for sibling in children[idx + 1 :]:
        if sibling.tag == "hashTree":
            return sibling
    return None


def _process_element(
    elem: ET.Element,
    parent: ET.Element,
    refs: list[str],
    seen: set[str],
) -> None:
    """处理单个 JMX 元素：禁用则跳过；启用则收集 CSV 引用并递归其子 hashTree。"""
    if not _is_enabled(elem):
        return

    if _is_csv_dataset(elem):
        filename = _extract_filename(elem)
        if filename and filename not in seen:
            seen.add(filename)
            refs.append(filename)
        return  # CSVDataSet 无实际子节点，无需下钻

    child_tree = _find_child_hash_tree(elem, parent)
    if child_tree is not None:
        _walk_hash_tree(child_tree, refs, seen)


def _walk_hash_tree(hash_tree: ET.Element, refs: list[str], seen: set[str]) -> None:
    """遍历一个 <hashTree>：其直接子元素按"元素 + 后续兄弟 hashTree"成对组织。"""
    for child in list(hash_tree):
        if child.tag == "hashTree":
            # hashTree 之间互为兄弟，已在处理前一个元素时顺带遍历，跳过
            continue
        _process_element(child, hash_tree, refs, seen)


def extract_csv_references(jmx_bytes: bytes) -> list[str]:
    """解析 JMX，提取所有**启用链路**下的 CSV 数据文件引用（去重，保留原始写法）。

    逐层校验 enabled：测试计划 → 线程组 → 控制器 → 取样器 → CSVDataSet，
    任一环节禁用则该子树内的 CSV 引用全部跳过。
    """
    try:
        root = ET.fromstring(jmx_bytes)
    except ET.ParseError as exc:
        raise BusinessError(f"JMX 解析失败: {exc}", code=3005) from exc

    refs: list[str] = []
    seen: set[str] = set()
    # 根 <jmeterTestPlan> 下第一个 <hashTree> 承载 TestPlan
    top_tree = root.find("hashTree")
    if top_tree is not None:
        _walk_hash_tree(top_tree, refs, seen)
    return refs


def check_data_files_complete(
    jmx_bytes: bytes, uploaded_filenames: set[str]
) -> list[str]:
    """比对 JMX 引用与上传文件清单，返回缺失的引用列表。

    比对口径：取引用的 basename 与上传文件名集合比较（Agent 下载时 save_as
    为纯文件名落工作目录根，JMX 内带目录的引用需用户自行保证结构一致）。
    """
    missing: list[str] = []
    for ref in extract_csv_references(jmx_bytes):
        if os.path.basename(ref) not in uploaded_filenames:
            missing.append(ref)
    return missing
