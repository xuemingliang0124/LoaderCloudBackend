"""Function Calling 工具集（FR-06，SRS B.8）。

9 个工具对应平台已有 REST 接口的封装：
- 8 个静态工具（@tool 异步定义）：query_projects / query_environments /
  query_transactions / get_scenario / create_scenario / get_run_summary /
  get_realtime_summary / query_metrics
- 1 个 RAG 检索工具 search_knowledge：`create_retriever_tool` 包装 Retriever 生成
  （retriever 由 orchestrator.build_retriever 按 project_id 预绑定过滤，
  SRS FR-05 多租户隔离；无需手写工具逻辑，SRS FR-06 验收口径）

后端接线：静态工具通过 `register_backend(name, fn)` 注册异步实现；真实后端
位于 app.services.llm.backends，由 lifespan 启动期 register_tool_backends
一次性注册（DB 走 SessionLocal 短会话、运行指标走 ES）。未接线时返回
{"error": ...} 占位而非抛异常，后端执行失败也包装为 error dict——保证
agent 编排链路鲁棒（工具失败可被 LLM 感知并降级回答，SRS NFR-02）。

args_schema 由 @tool 从类型注解自动生成（Pydantic 模型 → OpenAI Function
Calling 规范的 JSON Schema，TC-PRM-002 验收点）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain_core.tools import BaseTool, create_retriever_tool, tool
from loguru import logger

# 工具名 → 异步后端函数（Stage 4 注册；签名与各工具参数一致，返回 dict）
_backends: dict[str, Callable[..., Awaitable[dict]]] = {}


def register_backend(name: str, fn: Callable[..., Awaitable[dict]]) -> None:
    """注册工具后端实现（Stage 4 接线：api 层包装 DB 会话查询后注册）。"""
    _backends[name] = fn


def reset_backends() -> None:
    """测试辅助：清空后端注册表。"""
    _backends.clear()


async def _dispatch(tool_name: str, **kwargs) -> dict:
    """统一分发：未接线返回 error 占位；后端异常包装为 error dict（不抛）。"""
    backend = _backends.get(tool_name)
    if backend is None:
        logger.warning(f"工具 {tool_name} 后端未接线（Stage 4 注册），返回 error 占位")
        return {"error": f"工具 {tool_name} 后端未接线，无法执行"}
    try:
        return await backend(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"工具 {tool_name} 执行失败: {exc}")
        return {"error": f"工具 {tool_name} 执行失败: {exc}"}


@tool
async def query_projects(name: str = "") -> dict:
    """查询测试项目列表（项目ID/名称/描述等），可按项目名称模糊过滤；name 为空时返回全部项目。"""
    return await _dispatch("query_projects", name=name)


@tool
async def query_environments(project_id: int, name: str = "") -> dict:
    """查询项目环境清单（环境编码/名称/基础URL/变量等），可按名称模糊过滤。"""
    return await _dispatch("query_environments", project_id=project_id, name=name)


@tool
async def query_transactions(project_id: int, code: str = "") -> dict:
    """查询项目交易清单（交易编码/名称/SLA指标/默认脚本等），可按编码过滤。"""
    return await _dispatch("query_transactions", project_id=project_id, code=code)


@tool
async def get_scenario(scenario_id: int) -> dict:
    """获取压测场景详情（脚本/线程组/运行时间/绑定环境等）。"""
    return await _dispatch("get_scenario", scenario_id=scenario_id)


@tool
async def create_scenario(
    project_id: int,
    name: str,
    env_id: int,
    txn_id: int,
    tps: float = 0.0,
    duration_seconds: int = 0,
) -> dict:
    """创建压测场景（对应 POST /projects/{pid}/scenarios）：绑定环境与交易，指定目标 TPS 与运行时长。"""
    return await _dispatch(
        "create_scenario",
        project_id=project_id,
        name=name,
        env_id=env_id,
        txn_id=txn_id,
        tps=tps,
        duration_seconds=duration_seconds,
    )


@tool
async def get_run_summary(run_no: str) -> dict:
    """获取压测运行汇总（运行状态/TPS/P95/错误率/Agent 数/成功失败采样数）。"""
    return await _dispatch("get_run_summary", run_no=run_no)


@tool
async def get_realtime_summary(run_no: str) -> dict:
    """获取压测实时汇总（进行中运行的当前聚合指标）。"""
    return await _dispatch("get_realtime_summary", run_no=run_no)


@tool
async def query_metrics(run_no: str, agg: str = "avg") -> dict:
    """查询压测指标（tps/p95_ms/error_rate 等），agg 取值 avg/max/min/p95。"""
    return await _dispatch("query_metrics", run_no=run_no, agg=agg)


def build_search_knowledge_tool(retriever) -> BaseTool:
    """RAG 知识召回工具：`create_retriever_tool` 包装检索器生成（SRS FR-06）。

    retriever 已由 orchestrator.build_retriever 按 project_id 预绑定过滤与
    top_k，工具入参仅需 query。
    """
    return create_retriever_tool(
        retriever,
        name="search_knowledge",
        description=(
            "RAG 检索项目知识库（方案/SLA/架构文档、环境与交易清单切片），"
            "输入为检索查询文本，返回最相关的知识片段及引用标识。"
        ),
    )


def build_tools(retriever=None) -> list[BaseTool]:
    """组装工具集：8 个静态工具 + 可选 search_knowledge（传入 retriever 时）。"""
    tools: list[BaseTool] = [
        query_projects,
        query_environments,
        query_transactions,
        get_scenario,
        create_scenario,
        get_run_summary,
        get_realtime_summary,
        query_metrics,
    ]
    if retriever is not None:
        tools.append(build_search_knowledge_tool(retriever))
    return tools
