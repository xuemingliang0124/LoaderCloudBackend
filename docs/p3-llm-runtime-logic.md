# P3 大模型模块运行逻辑说明

> 对应代码：`master/app/services/llm/`、`master/app/services/embedding_client.py`、
> `master/app/services/vector_store.py`、`master/app/api/v1/chat.py`、`master/app/main.py`
>
> 本文回答三个问题：① 模块由什么组成；② 项目启动时初始化到什么程度；
> ③ 用户发起对话后内部如何执行（含 Function Calling 工具决策机制）。

---

## 1. 模块组成与职责

```
app/services/
├── llm/
│   ├── client.py           # LLM 工厂 get_llm()（单例）+ AnswerOutput 统一输出模型 + parse_answer 解析/降级
│   ├── orchestrator.py     # RAG 编排：检索 → 提示词 → LLM 调用 → 解析（含流式）
│   ├── guardrail.py        # FR-08 指标校验（run_no 存在时，对答案做 ±5% 容差核对）
│   ├── tools.py            # Function Calling 工具集（build_tools）+ 后端注册表
│   └── report_generator.py # LLM 报告生成（POST /reports/generate）
├── embedding_client.py     # Embedding 工厂 get_embeddings()（单例）+ 兼容层 EmbeddingClient
├── vector_store.py         # Qdrant 双抽象：QdrantVectorStore（入库）+ LangChain Qdrant（检索）
└── api/v1/chat.py          # HTTP 入口：POST /chat（同步）、POST /chat/stream（SSE 流式）
```

**核心设计思想：两层降级 + 双路径编排**

1. **配置级降级**：`get_llm()` / `get_embeddings()` 都是"配置齐用真模型，配置缺失用 Fake 降级"，
   保证端到端链路永远可跑（SRS 1.5.4）。
   - LLM：`LLM_API_KEY` + `LLM_MODEL` + `LLM_BASE_URL` 齐全 → `ChatOpenAI`；否则
     `FakeListChatModel`（返回预设 `FALLBACK_RESPONSE` JSON）。
   - Embedding：`EMBEDDING_API_KEY` + `EMBEDDING_MODEL` 齐全 → `OpenAIEmbeddings`
     （`check_embedding_ctx_length=False`，兼容 DashScope）；否则 `FakeEmbeddings`（入库标记
     `indexed=true, degraded=true`）。
2. **路径级降级**：真实 LLM 且 `use_tools=true` → langgraph ReAct Agent 路径；
   Mock 或关闭工具 → 纯 RAG 链路径（`prompt | llm | StrOutputParser`）。
   任何一步失败都兜底到 `_fallback_payload`（answer=检索 Top-1 原文，citations=Top-1 引用），
   **不向用户抛异常**。

---

## 2. 项目启动时的初始化步骤

启动入口 `app/main.py` 的 `lifespan`，按以下顺序执行（`_retry` 对外部依赖最多重试 15 次、
间隔 3 秒，防止 ES/MinIO/Qdrant 冷启动未就绪导致容器崩溃退出）：

| 步骤 | 调用 | 说明 |
|---|---|---|
| 1 | `setup_logging` | 日志初始化 |
| 2 | `_retry("MySQL", _init_db)` | 建表（dev 兜底 `create_all`，生产走 alembic） |
| 3 | `user_service.ensure_default_user` | 确保默认用户存在 |
| 4 | `_retry("Elasticsearch", es_client.ensure_indices)` | ES 索引幂等创建 |
| 5 | `_retry("Qdrant", vector_store.get_vector_store().ensure_collection)` | **向量库初始化**（见下） |
| 6 | `_retry("MinIO", storage.ensure_bucket)` | Bucket 幂等创建 |
| 7 | `start_scheduler` | APScheduler 启动（资产解析任务在此投递） |
| 8 | `orchestrator.recover_active_runs` | 恢复重启前未收尾的执行现场 |
| 9 | `_offline_check_loop` | 后台任务：每 10 秒扫描心跳超时 Agent 置 OFFLINE |

**Qdrant 初始化细节**（`vector_store.py`）：
- `get_vector_store()` 是单例工厂，按 `VECTOR_PROVIDER` 创建 `QdrantVectorStore`（当前仅支持 qdrant）。
- `ensure_collection` 幂等：collection 已存在则跳过（保护存量数据）；不存在则用**命名向量**
  `{"embedding": VectorParams(size, COSINE)}` 创建。用匿名向量会导致检索
  `query_points(using="embedding")` 报 400。

**关键洞察：LLM 与 Embedding 在启动时并不初始化。**
`get_llm()` / `get_embeddings()` 均为**懒加载单例**（模块级全局变量初始为 `None`，第一次被调用时
才检查配置并创建实例）。因此启动日志里看不到"LLM 已配置"，该日志出现在第一次对话/入库时。

```python
def get_llm():
    global _llm
    if _llm is not None:          # 单例命中，直接返回
        return _llm
    settings = get_settings()
    if settings.llm_api_key and settings.llm_model and settings.llm_base_url_resolved:
        _llm = ChatOpenAI(...)    # 真实模型
    else:
        _llm = FakeListChatModel(responses=[FALLBACK_RESPONSE])  # Mock 降级
    return _llm
```

---

## 3. 用户发起对话时的执行步骤

### 3.1 同步接口 POST /chat

```
① 门禁层（chat.py）
   ensure_project_access(project_id, user, "viewer")   ← 多租户隔离，无权限直接拒绝
   run_no 非空 → _ensure_run_for_chat                  ← 执行可见性校验，run 不存在 → 4004

② 编排层 run_qa_chain（orchestrator.py）
   └─ _run_qa_chain_impl
      │
      ├─ ②-a 检索 _retrieve_context
      │     build_retriever(project_id, top_k, threshold)
      │       ├ get_langchain_vector_store()              ← LangChain Qdrant 单例（懒加载）
      │       ├ as_retriever(search_type="similarity_score_threshold",
      │       │             filter=project_id 强制过滤)    ← 相似度阈值 + 租户隔离
      │       └ use_bm25 → scroll 拉项目语料 → EnsembleRetriever 混检 → _TopKRetriever 截断
      │     retriever.ainvoke(question)                   ← 内部触发 Embedding.aembed_query
      │     build_context(docs)                           ← 每块前置 [资产类型:ID:chunk_N]，超长截断
      │
      ├─ ②-b 选模型 llm = get_llm()                       ← 懒加载单例
      │
      ├─ ②-c 双路径分支 use_tools and _supports_tool_calling(llm)
      │     ├ 真实 LLM + use_tools=true：
      │     │    _run_agent_path → langgraph create_react_agent
      │     │    （LLM 可多轮调用工具：检索/查库等，工具链可追踪）
      │     └ Mock 降级 / use_tools=false：
      │          chain = build_prompt() | llm | StrOutputParser()
      │          单次调用：SYSTEM_PROMPT(context, citations) + 问题 + 历史 → JSON 文本
      │
      └─ ②-d 解析 parse_answer（client.py）
            normalize_json 剥离垃圾文本 → PydanticOutputParser → AnswerOutput
            解析失败 → _fallback_payload（answer=检索Top-1原文, citations=Top-1）
            citations 为空 → 兜底填充检索 Top-1

③ 校验层（run_no 非空时）
   validate_answer（guardrail）                           ← FR-08：答案指标 vs 实际执行指标
                                                            ±5% 容差，超差压 confidence、
                                                            notes 写 mismatch

④ 响应层
   ok(AnswerOut)                                          ← FR-09 五字段：answer/citations/
                                                            used_metrics/confidence/notes
```

**基础设施级兜底**：若检索/LLM 层异常直接外抛（如 Qdrant 宕机），`/chat` 捕获后返回
**503 + 降级 JSON**（错误码 4000）。

### 3.2 流式接口 POST /chat/stream

门禁在**返回流之前**用独立 DB 会话完成（避免流式期间长期持有请求级连接），随后进入
`astream_qa_events`，SSE 事件协议：

| 事件 | 时机 |
|---|---|
| `{"type": "token", "content": "..."}` | LLM 增量文本。纯 RAG 路径逐 chunk 推送；agent 路径无逐 token 流，整段回答作为单个 token 事件 |
| `{"type": "tool_call", "tool", "args"}` | 工具调用进度（仅真实 LLM agent 路径；当前流式钩子预留，Mock 路径不产出） |
| `{"type": "error", "message": "..."}` | 检索失败/LLM 调用失败（可恢复，后面还会接 done） |
| `{"type": "done", "final": {...}}` | 终态，必达，携带完整 AnswerOutput 五字段（run_no 非空时已完成 FR-08 校验） |

关键差异：流式接口**握手永不失败**——检索挂了发 error 事件后仍以兜底 AnswerOutput 收尾，
SSE 连接正常关闭；同步接口则是 503。

### 3.3 一次真实对话的完整时序（当前环境：阿里百炼已配置）

1. 前端 POST `/api/v1/chat`，body 含 `project_id=2, message="..."`。
2. 权限校验通过（项目成员且 viewer+）。
3. `build_retriever`：问题文本 → DashScope `text-embedding-v4` 向量化
   （`check_embedding_ctx_length=False` 直传字符串）→ Qdrant `query_points(using="embedding")`
   + `project_id=2` 过滤 → 取相似度 ≥ 阈值的 top_k 块。
4. `get_llm()` 返回 `ChatOpenAI(model=<LLM_MODEL>, base_url=<DashScope compatible-mode>)`
   （首次调用时创建，之后单例复用）。
5. `use_tools=true` 且 ChatOpenAI 支持 bind_tools → 走 langgraph ReAct Agent。
6. 回答 JSON → `parse_answer` 得到 `AnswerOutput` → HTTP 200 返回
   answer（带 `[txn_inventory:5:chunk_0]` 式引用）、citations、confidence 等。
7. 任一步失败（如百炼限流）：`notes` 写降级原因，`answer` 变为检索 Top-1 原文摘录——
   模块的设计承诺是"永不抛异常"。

---

## 4. Function Calling：大模型如何决定使用工具

### 4.1 决策机制

决策**不在代码里，而在模型内部**。代码只做三件事：

1. **把工具清单交给模型**。`create_react_agent(llm, build_tools(retriever), prompt=system_text)`
   将每个工具的 `name` / `description` / `args_schema`（由 `@tool` 装饰器从类型注解自动生成
   JSON Schema）序列化进 LLM 请求的 `tools` 字段。
2. **模型自主推理决策**。每轮生成时模型面临选择：直接回答，或输出 `tool_calls`
   （"我要调 query_transactions，参数是 project_id=2"）。依据是**语义匹配**——
   用户问题与哪个工具的 description 最相关。工具 description 的质量直接决定触发率。
3. **ReAct 循环**。langgraph 驱动多轮循环：

```
模型思考 → 输出 tool_calls → 代码执行工具 → 结果作为 ToolMessage 回传模型
   ↑                                                    │
   └──────────── 模型继续思考（再调工具 or 最终回答）←────┘
```

没有任何硬编码的调用顺序——"先查项目再查交易"这类规划是模型看到工具参数缺口后自行决定的。

### 4.2 当前工具集（tools.py，8 个）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `query_environments` | 查询项目环境清单 | project_id, name |
| `query_transactions` | 查询项目交易清单 | project_id, code |
| `get_scenario` | 场景详情 | scenario_id |
| `create_scenario` | 创建压测场景 | project_id, name, env_id, txn_id, tps, duration_seconds |
| `get_run_summary` | 运行汇总 | run_no |
| `get_realtime_summary` | 实时汇总 | run_no |
| `query_metrics` | 指标查询 | run_no, agg |
| `search_knowledge` | RAG 知识检索 | query（retriever 已按 project_id 预绑定过滤） |

### 4.3 示例："查询默认项目的交易清单" 实际发生什么

- **project_id 不需要 AI 查**：前端发起对话时 body 已携带 `project_id`（UI 层"默认项目"
  已解析为具体 ID）。LLM 从头到尾只看到问题文本，看不到 project_id。
- **系统里没有"查项目 ID"的工具**（无 query_projects）；`search_knowledge` 的 retriever
  在构建时就按请求的 project_id 预绑定了强制过滤，工具入参只有 query——**越权不可能**。
- **静态工具当前未接线**：`register_backend` 只有定义、无调用点，LLM 调用
  `query_transactions` 只会得到 `{"error": "工具 ... 后端未接线，无法执行"}` 占位。
  模型感知失败后降级：改用 `search_knowledge` 或直接基于前置 RAG context 组织答案。
- 真实时序：

```
① 检索前置：retriever.ainvoke("查询默认项目的交易清单")
   → Qdrant 按 project_id 过滤 → 命中交易清单切片
② system prompt 里已有交易清单内容（context）+ 引用标识
③ LLM 看到 query_transactions 工具 → 决定调用（project_id 未知 → 猜）
④ 工具返回 {"error": "后端未接线"} → 模型感知失败
⑤ 模型降级：改用 search_knowledge 或直接基于 ① 的 context 组织答案
⑥ 最终回答基于知识库切片，带 [txn_inventory:5:chunk_N] 引用
```

结果正确（RAG 上下文前置 + 检索自带项目隔离），但 ③ 的参数猜测是碰运气。

### 4.4 已知设计缺口与推荐修法

| 缺口 | 说明 | 风险 |
|---|---|---|
| LLM 不知道当前 project_id，却要填 project_id 参数 | SYSTEM_PROMPT 未告知"当前项目 ID"；模型只能从问题文本猜（"默认项目"可能被猜成 1） | 无效工具调用、答非所问 |
| 静态工具的 project_id 由 LLM 任意传入 | `_dispatch` 直通后端，不校验 LLM 传值是否等于会话项目 | 提示注入可跨租户读数据 |

**推荐修法（两者结合）**：

1. **服务端强制覆盖（治本，与 search_knowledge 策略对齐）**：接线时不直接注册查询函数，
   而是注册闭包把当前请求的 project_id 硬编码进去、忽略 LLM 入参；工具 schema 中甚至可
   移除 project_id 参数。
2. **SYSTEM_PROMPT 注入当前项目 ID（治标）**："当前会话项目 ID 为 {project_id}，
   所有工具调用必须使用此值"，减少模型猜参数的无效调用。

另外需先完成 Stage 4 的 `register_backend` 接线（api 层包装 DB 会话查询后注册），
否则工具编排路径（use_tools=true 时）只是空转一轮。

---

## 5. 相关文档

- 实施方案：`docs/p3-llm-implementation-plan.md`
- 需求规格：`docs/02-需求规格说明书SRS.md`（FR-05~FR-10、1.5.3/1.5.4 降级策略、附录 A）
- 概要设计：`docs/03-概要设计说明书HLD.md`
