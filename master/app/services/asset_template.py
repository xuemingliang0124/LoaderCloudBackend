"""资产导入模板生成：按列映射规则动态生成 xlsx / json / md 导入模板。

设计动机：模板表头必须与 asset_parser 的列映射规则（ENV/TXN_INVENTORY_COLUMN_MAP）
严格匹配，否则上传后列命中失败进 unmatched_columns。由后端按规则动态生成，
保证「模板表头 ↔ 解析规则」单一真相源，避免前端静态模板随代码演进漂移。

格式矩阵：
- 清单类（env_inventory / txn_inventory）：xlsx / json / md
- 文档类（plan_doc / sla_doc / architecture_doc）：仅 md（docx/pdf 无法生成且无必要）

关键约定：
- 全部生成函数为同步 CPU 实现，xlsx 生成经 generate_template 的 asyncio.to_thread
  调用（ptp-dev 3.1：禁止事件循环内阻塞调用）
- 清单模板表头逐一核对可命中列映射（match_column 归一化后精确匹配）
- JSON 模板严格使用 {"items": [...]} 形态，与 parse_json_inventory 对齐
"""

import asyncio
import io
import json
from dataclasses import dataclass

from openpyxl import Workbook

# 资产类型 → 中文名（模板文件名用）
ASSET_TYPE_LABELS: dict[str, str] = {
    "plan_doc": "测试方案",
    "env_inventory": "环境清单",
    "txn_inventory": "交易清单",
    "sla_doc": "SLA指标",
    "architecture_doc": "架构说明",
}


@dataclass(frozen=True)
class _TemplateField:
    """清单模板字段：规范表头 + 填写说明 + 示例值。

    header 必须能命中 asset_parser 对应列映射（match_column 归一化后查表）。
    """

    header: str
    hint: str
    example: str


_ENV_FIELDS: tuple[_TemplateField, ...] = (
    _TemplateField("环境名称", "环境显示名称", "生产环境"),
    _TemplateField("环境编码", "项目内唯一编码，必填，缺失该行跳过", "PROD"),
    _TemplateField("基础URL", "被测服务基础地址", "https://prod.example.com"),
    _TemplateField("主机", "多个用逗号/换行分隔，或 JSON 数组", "10.0.0.1,10.0.0.2"),
    _TemplateField(
        "数据库连接", "多个用逗号/换行分隔", "mysql://user:pwd@host:3306/db"
    ),
    _TemplateField("中间件", "多个用逗号/换行分隔", "nginx,kafka"),
    _TemplateField("变量", "k=v 或 k:v 键值对，或 JSON 对象", "timeout=30"),
    _TemplateField("描述", "备注说明", "2026Q3 生产环境"),
)

_TXN_FIELDS: tuple[_TemplateField, ...] = (
    _TemplateField("交易名称", "交易显示名称", "用户登录"),
    _TemplateField("交易码", "项目内唯一编码，必填，缺失该行跳过", "LOGIN"),
    _TemplateField("目标TPS", "数值", "100"),
    _TemplateField("P95(ms)", "数值，毫秒", "500"),
    _TemplateField("错误率", "如 0.1% 或 0.001", "0.1%"),
    _TemplateField("默认脚本", "项目内 JMeter 脚本名称，未命中记警告置空", "login.jmx"),
    _TemplateField("描述", "备注说明", "核心登录交易"),
)

_INVENTORY_FIELDS: dict[str, tuple[_TemplateField, ...]] = {
    "env_inventory": _ENV_FIELDS,
    "txn_inventory": _TXN_FIELDS,
}

# 文档类 md 模板骨架：章节标题 + 占位说明（全部内容将入向量库供 RAG 检索）
_MD_DOC_SKELETONS: dict[str, str] = {
    "plan_doc": """# 性能测试方案

> 本文档为测试方案导入模板：章节骨架可按需增删，全部文字内容将进入
> 平台知识库（切块向量化），供智能问答与测试报告生成检索引用。

## 1. 测试目标

（描述本次压测要验证的目标与通过标准）

## 2. 测试范围

（列出纳入压测的交易、场景与排除项）

## 3. 环境说明

（被测环境拓扑、数据准备、监控接入方式）

## 4. 场景设计

（基准/负载/混合/稳定性各场景的加压模型与目标指标）

## 5. 退出标准

（明确 TPS / P95 / 错误率的通过阈值）
""",
    "sla_doc": """# SLA 指标文档

> 本文档用于沉淀各交易的 SLA 指标约定，全部内容将进入平台知识库。

## SLA 指标约定

| 交易码 | 目标TPS | P95(ms) | 错误率 |
| --- | --- | --- | --- |
| TRADE_001 | 100 | 500 | 0.1% |

## 指标说明

（描述各指标口径与测量方式）
""",
    "architecture_doc": """# 架构说明文档

> 本文档描述被测系统架构，全部内容将进入平台知识库。

## 1. 系统架构

（整体架构、组件划分与部署拓扑）

## 2. 核心链路

（关键业务链路与调用关系）

## 3. 依赖组件

（数据库、缓存、消息队列等依赖及版本）
""",
}

# 格式矩阵：asset_type → 支持的模板格式
TEMPLATE_FORMATS: dict[str, tuple[str, ...]] = {
    "env_inventory": ("xlsx", "json", "md"),
    "txn_inventory": ("xlsx", "json", "md"),
    "plan_doc": ("md",),
    "sla_doc": ("md",),
    "architecture_doc": ("md",),
}

# 格式 → Content-Type / 扩展名
_TEMPLATE_MEDIA_TYPES: dict[str, str] = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "md": "text/markdown",
}

_INVENTORY_NOTE = "「{required}」为必填项，缺失的行将被跳过；表头名称需与本模板一致（列映射自动匹配）。"


def template_supported_formats(asset_type: str) -> tuple[str, ...]:
    """资产类型支持的模板格式（API 层校验用）。"""
    return TEMPLATE_FORMATS.get(asset_type, ())


def template_filename(asset_type: str, fmt: str) -> str:
    """模板下载文件名，如 环境清单导入模板.xlsx。"""
    return f"{ASSET_TYPE_LABELS[asset_type]}导入模板.{fmt}"


def template_media_type(fmt: str) -> str:
    return _TEMPLATE_MEDIA_TYPES[fmt]


# ---------- xlsx 模板（openpyxl：清单 sheet + 填写说明 sheet） ----------


def build_xlsx_template(asset_type: str) -> bytes:
    fields = _INVENTORY_FIELDS[asset_type]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "清单"
    sheet.append([f.header for f in fields])
    sheet.append([f.example for f in fields])

    hint_sheet = workbook.create_sheet("填写说明")
    hint_sheet.append(["字段", "说明"])
    for f in fields:
        hint_sheet.append([f.header, f.hint])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------- json 模板（严格 {"items": [...]} 形态，与 parse_json_inventory 对齐） ----------


def build_json_template(asset_type: str) -> bytes:
    """JSON 模板：严格 {"items": [...]} 形态，下载后可直接上传。

    JSON 无注释语法且解析端要求 keys 恰为 {"items"}，说明信息不内嵌，
    由前端下载入口提示填写规则（必填字段：见 _required_headers）。
    """
    fields = _INVENTORY_FIELDS[asset_type]
    example: dict[str, object] = {}
    for f in fields:
        if f.header == "主机":
            example[f.header] = f.example.split(",")
        else:
            example[f.header] = f.example
    return (
        json.dumps({"items": [example]}, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")


# ---------- md 模板（说明文字 + 管道表格，或文档类骨架） ----------


def _required_headers(fields: tuple[_TemplateField, ...]) -> list[str]:
    return [f.header for f in fields if "必填" in f.hint]


def build_md_template(asset_type: str) -> bytes:
    skeleton = _MD_DOC_SKELETONS.get(asset_type)
    if skeleton is not None:
        return skeleton.encode("utf-8")

    fields = _INVENTORY_FIELDS[asset_type]
    required = _required_headers(fields)
    lines = [
        f"# {ASSET_TYPE_LABELS[asset_type]}导入模板",
        "",
        "> 表格外的说明文字将进入平台知识库检索；表格行按列映射抽取为结构化数据。",
        _INVENTORY_NOTE.format(required="、".join(required)) if required else "",
        "",
        "| " + " | ".join(f.header for f in fields) + " |",
        "| " + " | ".join("---" for _ in fields) + " |",
        "| " + " | ".join(f.example for f in fields) + " |",
        "",
    ]
    text = "\n".join(line for line in lines if line != "")
    return (text + "\n").encode("utf-8")


# ---------- 统一入口 ----------


async def generate_template(asset_type: str, fmt: str) -> tuple[bytes, str, str]:
    """生成模板：返回 (内容 bytes, content_type, 文件名)。

    xlsx 生成含压缩与磁盘 IO，用 asyncio.to_thread 卸载；json/md 为纯
    字符串拼接，直接同步执行。fmt 合法性由 API 层按 TEMPLATE_FORMATS 校验。
    """
    if fmt == "xlsx":
        data = await asyncio.to_thread(build_xlsx_template, asset_type)
    elif fmt == "json":
        data = build_json_template(asset_type)
    else:
        data = build_md_template(asset_type)
    return data, template_media_type(fmt), template_filename(asset_type, fmt)
