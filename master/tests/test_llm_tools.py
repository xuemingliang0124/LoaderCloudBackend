"""P3 Stage 2 tools 测试（FR-06，TC-PRM-002 工具定义校验）。

验收口径（SRS FR-06）：
- 工具 args_schema JSON Schema 符合 OpenAI Function Calling 规范
- search_knowledge 由 create_retriever_tool 包装 Retriever 生成，入参仅 query
- 后端未接线/执行失败时返回 error dict 而非抛异常（NFR-02 鲁棒性）
"""

import pytest
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from app.services.llm.tools import (
    build_search_knowledge_tool,
    build_tools,
    create_scenario,
    query_environments,
    query_projects,
    register_backend,
    reset_backends,
)

_STATIC_TOOL_NAMES = {
    "query_projects",
    "query_environments",
    "query_transactions",
    "get_scenario",
    "create_scenario",
    "get_run_summary",
    "get_realtime_summary",
    "query_metrics",
}


class FakeRetriever(BaseRetriever):
    docs: list[Document] = []

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        return list(self.docs)


# ---- 工具集组装 ----


def test_build_tools_static_eight() -> None:
    """不传 retriever 时返回 8 个静态工具，名字与 SRS FR-06 工具表一致。"""
    tools = build_tools()
    assert {t.name for t in tools} == _STATIC_TOOL_NAMES


def test_build_tools_with_retriever_nine() -> None:
    """传入 retriever 时追加 search_knowledge，共 9 个工具。"""
    tools = build_tools(FakeRetriever(docs=[]))
    assert len(tools) == 9
    assert {t.name for t in tools} == _STATIC_TOOL_NAMES | {"search_knowledge"}


def test_tools_have_non_empty_description() -> None:
    """每个工具必须有 description（Function Calling 依赖 description 选择工具）。"""
    for t in build_tools(FakeRetriever(docs=[])):
        assert t.description.strip()


# ---- TC-PRM-002：args_schema JSON Schema 符合 OpenAI Function Calling 规范 ----


def test_args_schema_openai_compliant() -> None:
    """每个工具 schema：object 类型 + properties 全带 type + required ⊆ properties。"""
    for t in build_tools(FakeRetriever(docs=[])):
        schema = t.args_schema.model_json_schema()
        assert schema.get("type") == "object"
        props = schema.get("properties", {})
        assert props, f"工具 {t.name} 缺 properties"
        for key, prop in props.items():
            assert "type" in prop, f"工具 {t.name} 参数 {key} 缺 type"
        required = schema.get("required", [])
        assert set(required) <= set(props), f"工具 {t.name} required 超出 properties"


def test_query_projects_schema() -> None:
    """query_projects：无必填参数，name 可选 string（空名返回全部项目）。"""
    schema = query_projects.args_schema.model_json_schema()
    assert schema.get("required", []) == []
    assert schema["properties"]["name"]["type"] == "string"


def test_query_environments_schema() -> None:
    """query_environments：project_id 必填 integer，name 可选 string。"""
    schema = query_environments.args_schema.model_json_schema()
    assert schema["required"] == ["project_id"]
    assert schema["properties"]["project_id"]["type"] == "integer"
    assert schema["properties"]["name"]["type"] == "string"


def test_create_scenario_schema() -> None:
    """create_scenario：project_id/name/env_id/txn_id 必填，tps/duration_seconds 可选。"""
    schema = create_scenario.args_schema.model_json_schema()
    assert set(schema["required"]) == {"project_id", "name", "env_id", "txn_id"}
    assert schema["properties"]["tps"]["type"] == "number"
    assert schema["properties"]["duration_seconds"]["type"] == "integer"


# ---- 后端分发：未接线占位 / 注册接线 / 异常包装 ----


async def test_tool_without_backend_returns_error_dict() -> None:
    """后端未接线返回 error 占位（不抛异常，agent 链路不中断）。"""
    result = await query_environments.ainvoke({"project_id": 1})
    assert "error" in result
    assert "未接线" in result["error"]


async def test_register_backend_roundtrip() -> None:
    """注册后端后工具透传参数并返回后端结果；结束清理注册表。"""

    async def fake_backend(**kwargs) -> dict:
        return {"items": [kwargs]}

    register_backend("query_environments", fake_backend)
    try:
        result = await query_environments.ainvoke({"project_id": 7, "name": "prod"})
        assert result == {"items": [{"project_id": 7, "name": "prod"}]}
    finally:
        reset_backends()


async def test_backend_exception_wrapped_as_error_dict() -> None:
    """后端抛异常包装为 error dict（不外抛，NFR-02）。"""

    async def boom(**kwargs) -> dict:
        raise RuntimeError("db down")

    register_backend("query_environments", boom)
    try:
        result = await query_environments.ainvoke({"project_id": 1})
        assert "error" in result
        assert "执行失败" in result["error"]
        assert "db down" in result["error"]
    finally:
        reset_backends()


# ---- search_knowledge（create_retriever_tool 包装，SRS FR-06 验收口径）----


def test_search_knowledge_tool_wraps_retriever() -> None:
    """search_knowledge：名字/入参来自 create_retriever_tool，调用透传检索结果。"""
    docs = [
        Document(
            page_content="登录交易目标 500TPS",
            metadata={"asset_id": 3, "asset_type": "sla_doc", "chunk_index": 1},
        )
    ]
    knowledge_tool = build_search_knowledge_tool(FakeRetriever(docs=docs))
    assert knowledge_tool.name == "search_knowledge"
    assert "query" in knowledge_tool.args
    # create_retriever_tool 默认 response_format="content"：返回拼接后的文本
    out = knowledge_tool.invoke({"query": "登录"})
    assert isinstance(out, str)
    assert "登录交易目标 500TPS" in out


def test_search_knowledge_tool_description_mentions_rag() -> None:
    """description 说明知识库范围（供 LLM 判断何时调用）。"""
    knowledge_tool = build_search_knowledge_tool(FakeRetriever(docs=[]))
    assert "知识库" in knowledge_tool.description


@pytest.fixture(autouse=True)
def _clean_backends() -> None:
    """每个用例前后清空后端注册表（模块级 dict 隔离）。"""
    reset_backends()
    yield
    reset_backends()
