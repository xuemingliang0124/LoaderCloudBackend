# 基于 RAG 的性能测试智能助手与报告生成系统
# 概要设计说明书（HLD）

| 项目名 | 基于 RAG 的性能测试智能助手与报告生成系统 |
| --- | --- |
| 版本 | v1.0 |
| 日期 | 2026-09-20 |
| 作者 | 项目组 |
| 保密级别 | 内部 |

---

## 版本修订记录

| 版本 | 日期 | 修订人 | 变更说明 |
| --- | --- | --- | --- |
| v1.0 | 2026-09-20 | 项目组 | 初稿，覆盖 P3 LLM 编排接入全部模块设计 |

---

## 目录

- A. 架构对齐与约束落地
- B. 模块分解与接口设计
- C. 数据/元数据与文件规范
- D. 两条关键时序流
- E. 关键设计决策与权衡
- F. 异常处理、降级与鲁棒性
- G. 运行形态与配置/部署视图
- H. 观测性：日志/指标/证据定位
- I. 合规与安全
- J. 需求/用例追踪与一致性
- K. 图文质量与可读性
- 附录：术语与缩略语

---

## A. 架构对齐与约束落地

### A.1 架构总览图

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    图 1  系统架构总览（离线运行边界）                        │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │  Loader      │───▶│  Cleaner     │───▶│  Chunker     │               │
│  │ (文档加载器)  │    │ (RunnableL-  │    │ (Recursive-  │               │
│  │ Docx2txt/    │    │  ambda清洗)  │    │  Character-  │               │
│  │ PyPDF/       │    │              │    │  TextSpli-   │               │
│  │ ExcelLoader  │    │              │    │  tter)       │               │
│  └──────────────┘    └──────────────┘    └──────┬───────┘               │
│                                                 │                       │
│                                                 ▼                       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │  Retriever   │◀───│  Indexer     │◀───│ Embeddings   │               │
│  │ (as_retriever│    │ (Qdrant      │    │ (OpenAI/     │               │
│  │  +Ensemble-  │    │  VectorStore)│    │  Fake)       │               │
│  │  Retriever)  │    └──────────────┘    └──────────────┘               │
│  └──────┬───────┘                                                         │
│         │                                                                 │
│         ▼                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │  Promptor    │───▶│  LLM Engine  │───▶│  Parser &    │               │
│  │ (ChatPrompt- │    │ (ChatOpenAI/ │    │  Synthesizer │               │
│  │  Template +  │    │  FakeList-   │    │ (JsonOutput- │               │
│  │  Tools)      │    │  ChatModel + │    │  Parser)     │               │
│  │              │    │  Agent Exec) │    │              │               │
│  └──────────────┘    └──────────────┘    └──────┬───────┘               │
│                                                 │                       │
│                                                 ▼                       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │  Service     │◀───│  Metric      │◀───│  Answer      │               │
│  │ (REST API +  │    │  Guardrail   │    │  Payload     │               │
│  │  WebSocket)  │    │ (validate_   │    │ (统一输出)    │               │
│  │              │    │  metrics)    │    │              │               │
│  └──────────────┘    └──────────────┘    └──────────────┘               │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │  🔒 离线运行边界：仅 CPU / 不可联网 / 不可微调 / 可 MockLLM       │   │
│  │     - Embedding 未配置 → FakeEmbeddings                          │   │
│  │     - LLM API 未配置 → FakeListChatModel                         │   │
│  └──────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────────┘
```

### A.2 架构约束声明

🔒 **离线运行、仅 CPU、不可联网、不可微调**：

- **仅 CPU**：考试演示环境使用 `FakeListChatModel` + `FakeEmbeddings` 完成端到端流程，无需 GPU。
- **不可联网**：生产环境 LLM/Embedding 通过 `ChatOpenAI`/`OpenAIEmbeddings` 的 `base_url` 对接智谱/阿里 API（可选）；未配置时自动降级为 Mock 组件，不依赖网络。
- **不可微调**：仅使用 Prompt Engineering + Function Calling，不对模型做任何微调。
- **可 MockLLM**：`FakeListChatModel` 预设响应用于离线测试与演示。

### A.3 能力 → 章节映射

| 能力 | SRS 章节 | HLD 模块 | 评测维度对齐 |
| --- | --- | --- | --- |
| 文档资产解析 | FR-01 | Loader (B.1) | 可运行性 |
| 清洗与去噪 | FR-02 | Cleaner (B.2) | 检索相关性 |
| 切块与重叠 | FR-03 | Chunker (B.3) | 检索相关性 |
| 向量索引构建 | FR-04 | Indexer + Embeddings (B.4) | 可运行性 |
| 检索器 | FR-05 | Retriever (B.5) | 检索相关性 |
| 提示词模板与工具 | FR-06 | Promptor + Tools (B.6) | 回答完整性 |
| LLM 调用与解析 | FR-07 | LLM Engine + Parser (B.7) | 回答完整性 |
| 指标校验 | FR-08 | Metric Guardrail (B.8) | 指标校验正确性 |
| 统一输出格式 | FR-09 | Synthesizer (B.7) | 引用准确性 |
| 入口形态 | FR-10 | Service (B.9) | 可运行性 |

---

## B. 模块分解与接口设计

### B.1 模块职责表

| 模块 | 源码路径 | 关键接口 | 入参 | 出参 | 关键异常 | 配置键 |
| --- | --- | --- | --- | --- | --- | --- |
| Loader | `services/asset_parser.py` | `dispatch_parse(file_path, asset_type)` | 文件路径、资产类型 | `list[Document]` | `UnsupportedFormatError` | `ASSET_PARSE_TIMEOUT` |
| Cleaner | `services/asset_parser.py` | `_clean_text(doc: Document)` | `Document` | `Document` | 空文本跳过 | — |
| Chunker | `services/asset_parser.py` | `RecursiveCharacterTextSplitter.split_documents(docs)` | `list[Document]` | `list[Document]` | 超长自动切分 | `CHUNK_SIZE`, `CHUNK_OVERLAP` |
| Indexer | `services/vector_store.py` | `VectorStore.upsert_chunks(chunks, ids)` | chunks, point_ids | None | `VectorStoreError` | `QDRANT_URL`, `QDRANT_COLLECTION` |
| Embeddings | `services/embedding_client.py` | `EmbeddingClient.embed(texts)` | `list[str]` | `list[list[float]]` | `EmbeddingError` | `EMBEDDING_PROVIDER`, `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` |
| Retriever | `services/llm/orchestrator.py` | `retriever.invoke(query)` | query | `list[Document]` | 空结果返回 [] | `TOP_K`, `SIMILARITY_THRESHOLD`, `USE_BM25` |
| Promptor | `services/llm/orchestrator.py` | `prompt.invoke({"input": q, "agent_scratchpad": []})` | query, context | `ChatPromptValue` | context 超长裁剪 | `MAX_CONTEXT_CHARS` |
| Tools | `services/llm/tools.py` | `@tool` 装饰的 8 个工具 | 见 B.6 | 工具结果 JSON | 工具执行异常 | — |
| LLM Engine | `services/llm/client.py` | `agent.invoke({"input": q, "chat_history": h})` | query, history | `AgentResult` | 超时/网络异常 | `LLM_PROVIDER`, `LLM_MODEL`, `LLM_TIMEOUT` |
| Parser | `services/llm/client.py` | `JsonOutputParser.parse(text)` | LLM 原始输出 | dict | `OutputParserException` | — |
| Guardrail | `services/llm/orchestrator.py` | `validate_metrics(run_no, payload)` | run_no, payload | 修正后 payload | run_no 不存在跳过 | `METRIC_TOLERANCE` |
| Service | `api/v1/chat.py`, `ws/chat.py` | `POST /api/v1/chat`, `WS /ws/chat` | 请求 JSON | JSON 响应 | 422/403/503 | — |

### B.2 统一数据模型

**Document 元数据（LangChain `Document.metadata`）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| asset_id | int | 资产 ID |
| project_id | int | 项目 ID（过滤用） |
| asset_type | str | plan_doc/sla_doc/architecture_doc/env_inventory/txn_inventory |
| chunk_index | int | 块序号 |
| source | str | 来源文件路径（LangChain 标准字段） |
| page | int | 页码（pdf 加载器产出） |
| source_type | str | asset/report/domain_doc |
| source_ref | str | file_key/run_no |

**Answer Payload（统一输出）**：

```json
{
  "answer": "已为您创建登录交易压测场景，目标 TPS 500",
  "citations": ["plan_doc:12:chunk_3", "env:5"],
  "used_metrics": ["tps"],
  "confidence": 0.85,
  "notes": ""
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| answer | string | 自然语言回答 |
| citations | array[string] | 引用来源，格式 `[{type}:{id}:chunk_{index}]` |
| used_metrics | array[string]? | 涉及的压测指标（tps/p95_ms/error_rate） |
| confidence | float | 置信度 0-1，启发式 + Guardrail 调整 |
| notes | string | 备注（指标不一致/降级原因） |

### B.3 Loader 模块设计

**组件**：LangChain Document Loader 分发

```python
# services/asset_parser.py
def dispatch_parse(file_path: str, asset_type: str) -> list[Document]:
    ext = Path(file_path).suffix.lower()
    if ext == ".docx":
        loader = Docx2txtLoader(file_path)
    elif ext == ".pdf":
        loader = PyPDFLoader(file_path)
    elif ext == ".xlsx":
        return ExcelInventoryLoader(file_path, asset_type).load()  # 自定义
    else:
        raise UnsupportedFormatError(f"Unsupported format: {ext}")
    docs = loader.load()
    for doc in docs:
        doc.metadata.update({"asset_type": asset_type, ...})
    return docs
```

**自定义 ExcelInventoryLoader**：继承 `BaseLoader`，封装 openpyxl，同时产出文本 `Document`（供切块）与结构化数据行（写入 environments/transactions 表）。

### B.4 Cleaner 模块设计

**组件**：`RunnableLambda`

```python
cleaner = RunnableLambda(_clean_documents)

def _clean_documents(docs: list[Document]) -> list[Document]:
    for doc in docs:
        text = doc.page_content
        text = re.sub(r"Page\s+\d+\s+of\s+\d+", "", text)  # 去页眉页脚
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)        # 断词修复
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)        # 换行转空格
        doc.page_content = text
    return docs
```

### B.5 Chunker 模块设计

**组件**：`RecursiveCharacterTextSplitter`

```python
splitter = RecursiveCharacterTextSplitter(
    chunk_size=settings.chunk_size,        # 默认 500
    chunk_overlap=settings.chunk_overlap,  # 默认 50
    separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
)
chunks = splitter.split_documents(cleaned_docs)
# 为每个 chunk 写入 chunk_index 到 metadata
```

### B.6 Indexer + Embeddings 模块设计

**Embeddings**：`OpenAIEmbeddings`（生产）/ `FakeEmbeddings`（降级）

```python
# services/embedding_client.py
def get_embeddings() -> Embeddings:
    if settings.embedding_api_key:
        return OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
    return FakeEmbeddings(size=settings.embedding_dims)
```

**Indexer（Qdrant VectorStore）**：

```python
# services/vector_store.py
vector_store = Qdrant(
    client=qdrant_client,
    collection_name=settings.qdrant_collection,
    embeddings=get_embeddings(),
)
point_ids = [crc32(f"{asset_id}:{i}") for i in range(len(chunks))]
vector_store.add_documents(chunks, ids=point_ids)
```

### B.7 Retriever 模块设计

**组件**：`VectorStore.as_retriever()` + 可选 `EnsembleRetriever`（BM25 混检）

```python
# 基础向量检索
retriever = vector_store.as_retriever(
    search_type="similarity_score_threshold",
    search_kwargs={
        "k": settings.top_k,
        "score_threshold": settings.similarity_threshold,
        "filter": {"project_id": project_id},
    },
)

# BM25 混检（可选）
if settings.use_bm25:
    bm25_retriever = BM25Retriever.from_documents(all_chunks)
    bm25_retriever.k = settings.top_k
    retriever = EnsembleRetriever(
        retrievers=[bm25_retriever, vector_retriever],
        weights=[0.5, 0.5],
    )
```

**多租户隔离**：`search_kwargs["filter"]` 强制 `project_id`，不返回其他项目数据。

### B.8 Promptor + Tools 模块设计

**Promptor**：`ChatPromptTemplate`

```python
prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),  # 含约束、引用格式 [{type}:{id}:chunk_{index}]
    MessagesPlaceholder(variable_name="chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),
])
```

**Tools**（8 个，通过 `@tool` 或 `StructuredTool.from_function` 定义）：

| 工具名 | 入参 | 功能 | 对应已有接口 |
| --- | --- | --- | --- |
| `query_environments` | project_id, name? | 查询环境清单 | GET /projects/{pid}/environments |
| `query_transactions` | project_id, code? | 查询交易清单 | GET /projects/{pid}/transactions |
| `get_scenario` | scenario_id | 获取场景详情 | GET /scenarios/{id} |
| `create_scenario` | project_id, name, env_id, txn_id, tps... | 创建压测场景 | POST /projects/{pid}/scenarios |
| `get_run_summary` | run_no | 获取运行汇总 | GET /runs/{run_no}/summary |
| `get_realtime_summary` | run_no | 获取实时汇总 | GET /runs/{run_no}/realtime |
| `query_metrics` | run_no, agg | 查询压测指标(TPS/P95/错误率) | 复用 es_client + metrics.py |
| `search_knowledge` | project_id, query, top_k | RAG 知识召回 | `create_retriever_tool(retriever, ...)` |

### B.9 LLM Engine + Parser 模块设计

**LLM Engine**：`ChatOpenAI` / `FakeListChatModel` + `create_tool_calling_executor`

```python
# services/llm/client.py
def get_llm() -> BaseChatModel:
    if settings.llm_api_key:
        return ChatOpenAI(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            temperature=0.1,
            timeout=settings.llm_timeout,
        )
    return FakeListChatModel(responses=[FALLBACK_RESPONSE])

agent = create_tool_calling_executor(llm, tools, prompt=prompt)
result = agent.invoke({"input": user_message, "chat_history": history})
```

**Parser & Synthesizer**：`JsonOutputParser` / `PydanticOutputParser`

```python
class AnswerOutput(BaseModel):
    answer: str
    citations: list[str] = []
    used_metrics: list[str] | None = None
    confidence: float = 0.5
    notes: str = ""

parser = PydanticOutputParser(pydantic_object=AnswerOutput)
try:
    output = parser.parse(result["messages"][-1].content)
except OutputParserException:
    output = _fallback_payload(retrieved_docs)  # 兜底
```

### B.10 Metric Guardrail 模块设计

**功能**：从 LLM 回答中正则抽取 TPS/P95/错误率，与 ES 中 run_no 对应的实际指标比对，±5% 容差。

```python
# services/llm/orchestrator.py
def validate_metrics(run_no: int, payload: AnswerOutput) -> AnswerOutput:
    actual = es_client.get_run_metrics(run_no)  # {tps, p95_ms, error_rate}
    extracted = _extract_metrics_from_text(payload.answer)
    for field in ["tps", "p95_ms", "error_rate"]:
        if field in extracted and field in actual:
            diff = abs(extracted[field] - actual[field]) / actual[field]
            if diff > settings.metric_tolerance:  # 默认 0.05
                payload.notes = f"Mismatch with actual metrics: {field}"
                payload.confidence = min(payload.confidence, 0.7)
                break
    else:
        payload.notes = "Metrics verified"
    return payload
```

### B.11 Service 模块设计

**REST API**（`api/v1/chat.py`）：

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| POST | `/api/v1/chat` | 同步对话，返回 Answer Payload |
| POST | `/api/v1/chat/stream` | SSE 流式响应 |
| POST | `/api/v1/reports/generate` | 为 run_no 生成 LLM 报告 |
| GET | `/api/v1/assets/knowledge-search` | RAG 检索 |

**WebSocket**（`ws/chat.py`）：`WS /ws/chat`，推送 `token` / `tool_call` / `done` 消息。

---

## C. 数据/元数据与文件规范

### C.1 目录结构

```
master/app/
├── services/
│   ├── asset_parser.py        # Loader + Cleaner + Chunker (FR-01~03)
│   ├── embedding_client.py    # Embeddings (FR-04)
│   ├── vector_store.py        # Indexer (FR-04)
│   ├── es_client.py           # ES 指标查询（Guardrail 数据源）
│   ├── llm/
│   │   ├── client.py          # LLM Engine + Parser (FR-07)
│   │   ├── tools.py           # Tools (FR-06)
│   │   └── orchestrator.py    # Retriever + Promptor + Guardrail + Chain 编排
│   └── report_generator.py    # L4 报告生成
├── api/v1/
│   ├── assets.py              # 资产上传 + knowledge-search
│   └── chat.py                # 对话 REST API (FR-10)
├── ws/
│   └── chat.py                # 对话 WebSocket (FR-10)
├── models/
│   └── asset.py               # test_asset 表
└── core/
    └── config.py              # 配置中心
data/
├── assets/                    # MinIO: ptp/assets/{asset_id}/{filename}
└── reports/                   # MinIO: ptp/reports/{run_no}/llm-report.md
logs/
├── llm_calls/                 # LLM 调用日志
└── parse_pipeline/            # 解析管道日志
```

### C.2 核心数据字典

**test_asset 表**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| id | int | 主键 |
| project_id | int | 项目 ID（FK） |
| name | string | 资产名称 |
| asset_type | enum | plan_doc/sla_doc/architecture_doc/env_inventory/txn_inventory |
| status | enum | pending/parsing/ready/failed |
| filename | string | 原始文件名 |
| file_key | string | MinIO 存储 key |
| hash_sha256 | string | 内容哈希（项目内唯一） |
| file_size | int | 文件大小（字节） |
| content_type | string | MIME 类型 |
| parse_meta | JSON | 解析元数据（unmatched_columns/warnings/extracted_count） |
| created_by | int | 创建人 |

**Qdrant collection（pt_knowledge）payload（对应 Document.metadata）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| asset_id | int | 资产 ID |
| project_id | int | 项目 ID（过滤用） |
| asset_type | string | 资产类型 |
| chunk_index | int | 块序号 |
| source | string | 来源文件路径 |
| page | int | 页码 |
| source_type | string | asset/report/domain_doc |
| source_ref | string | file_key/run_no |

### C.3 引用标识规范

统一格式：`[{type}:{id}:chunk_{index}]`

示例：
- 文档资产：`[plan_doc:12:chunk_3]`
- 运行汇总：`[run_summary:1001]`
- 指标：`[metrics:1001:p95]`
- 环境：`[env:5]`

### C.4 评测数据格式（dev_set.jsonl）

```json
{
  "query": "把生产环境登录交易压到 500 TPS",
  "target": "应调用 create_scenario 工具，参数含 env_code=prod, txn_code=login, tps=500",
  "expected_tools": ["query_environments", "query_transactions", "create_scenario"],
  "notes": "验证工具链编排正确性"
}
```

---

## D. 两条关键时序流

### D.1 QA 链时序图（对话式场景操作）

```
图 2  QA 链时序图（含失败回退与引用兜底）

用户          Service      Orchestrator    Retriever     Promptor      LLM Engine    Parser        Guardrail
 │              │              │              │              │              │              │              │
 │──message────▶│              │              │              │              │              │              │
 │              │──invoke──────▶│              │              │              │              │              │
 │              │              │──retrieve────▶│              │              │              │              │
 │              │              │◀─docs(Top-k)─│              │              │              │              │
 │              │              │──build_prompt───────────────▶│              │              │              │
 │              │              │◀─ChatPromptValue─────────────│              │              │              │
 │              │              │──agent.invoke───────────────────────────────▶│              │              │
 │              │              │              │              │              │──tool_call?──│              │
 │              │              │              │              │              │◀─tool_result─│              │
 │              │              │              │              │              │──content─────▶│              │
 │              │              │              │              │              │              │──parse───────▶│
 │              │              │              │              │              │              │◀─AnswerOutput│
 │              │              │──validate_metrics(run_no, payload)─────────────────────────────────────▶│
 │              │              │◀─修正后 payload─────────────────────────────────────────────────────────│
 │              │◀─JSON────────│              │              │              │              │              │
 │◀─response────│              │              │              │              │              │              │
 │              │              │              │              │              │              │              │
 │  [失败回退路径]                                                                                        │
 │              │              │              │              │              │──timeout/异常─│              │
 │              │              │              │              │              │              │──fallback────▶│
 │              │              │              │              │              │              │  引用=Top-1  │
 │              │              │              │              │              │              │  notes=降级   │
```

**关键步骤说明**：

1. **Retriever 检索**：`retriever.invoke(query)` 返回 Top-k `Document`，含 score 与 metadata
2. **Promptor 构建**：`prompt.invoke({"input": query, "agent_scratchpad": []})` 组装消息
3. **LLM 调用**：`agent.invoke(...)` 执行 Function Calling 多轮编排
4. **Parser 解析**：`JsonOutputParser.parse(content)` 转为 `AnswerOutput`
5. **失败回退**：超时/解析失败 → `_fallback_payload(docs)`，citations 取检索 Top-1
6. **Guardrail 校验**：`validate_metrics(run_no, payload)` 比对 ES 实际指标

### D.2 评测/守护链时序图（指标校验）

```
图 3  守护链时序图（±5% 容差校验）

Answer Payload          Guardrail              ES Client              判定逻辑
      │                     │                      │                      │
      │──payload────────────▶│                      │                      │
      │                     │──get_run_metrics─────▶│                      │
      │                     │◀─{tps,p95,error_rate}─│                      │
      │                     │                      │                      │
      │                     │──regex extract──────────────────────────────▶│
      │                     │   从 answer 抽取数值                          │
      │                     │                      │                      │
      │                     │──for each field:─────▶                      │
      │                     │   diff = |ext - act| / act                   │
      │                     │                      │                      │
      │                     │──if diff > 0.05: ────▶                      │
      │                     │   notes = "Mismatch: {field}"                │
      │                     │   confidence = min(conf, 0.7)                │
      │                     │                      │                      │
      │                     │──else: ──────────────▶                      │
      │                     │   notes = "Metrics verified"                 │
      │                     │                      │                      │
      │◀─修正后 payload─────│                      │                      │
      │                     │                      │                      │
```

**容差阈值**：`METRIC_TOLERANCE=0.05`（可配置），支持字段：`tps`、`p95_ms`、`error_rate`。

---

## E. 关键设计决策与权衡

### E.1 索引方案取舍

**决策**：选用 Qdrant（通过 `langchain_qdrant.Qdrant`）作为向量库，以向量检索为主，可选 BM25 混检。

**理由**：
- ES `dense_vector` 大规模召回性能差，且与 `pt-summary`/`pt-metrics` 争 JVM heap
- Qdrant 单容器部署、async SDK 原生，契合异步架构
- 通过 `VectorStore` 抽象层接入，未来切换 FAISS/Chroma 仅改实例化一行

**局限**：Qdrant 不支持复杂聚合查询，仅用于向量检索；结构化查询仍走 MySQL/ES。

### E.2 Prompt 设计

**决策**：使用 `ChatPromptTemplate` 含 `MessagesPlaceholder("agent_scratchpad")`，context 拼接限制 1800 字符。

**策略**：
- 系统提示词含：角色约束、引用格式 `[{type}:{id}:chunk_{index}]`、工具说明
- context 拼接：`"\n\n".join([f"[{m['asset_type']}:{m['asset_id']}:chunk_{m['chunk_index']}]\n{d.page_content}" for d in docs])`
- 超长裁剪：超过 1800 字符时按 chunk 顺序截断并记录日志

### E.3 输出结构化与置信度策略

**决策**：`PydanticOutputParser` 解析为 `AnswerOutput` 模型；confidence = 启发式初值（0.8）+ Guardrail 调整。

**置信度来源**：
- 初值 0.8（有检索结果且非空）
- 检索 Top-1 score < 0.5 → 降至 0.6
- Guardrail 指标不一致 → 降至 ≤ 0.7
- 工具调用全部成功 → 维持；有失败 → 降至 0.5

### E.4 MockLLM 的必要性与扩展位

**决策**：未配置 LLM API Key 时使用 `FakeListChatModel(responses=[FALLBACK_RESPONSE])`。

**扩展位**：`get_llm()` 工厂方法按配置分支，新增 provider 仅需加一个 `elif` 分支，业务层无感知。

---

## F. 异常处理、降级与鲁棒性

### F.1 错误表

| 异常 | 检测点 | 处理 | 日志关键字 | 影响范围 |
| --- | --- | --- | --- | --- |
| JSON 解析失败 | `JsonOutputParser.parse` | normalize 后重试；仍失败则 fallback（citations=Top-1） | `LLM_PARSE_FAILED` | 单轮对话降级 |
| 无引用/错误引用 | Synthesizer | 自动填充检索 Top-1 引用 | `CITATION_FALLBACK` | 引用准确性 |
| 指标超容差 | Guardrail | notes 标注 mismatch，confidence ≤ 0.7 | `METRIC_MISMATCH` | 置信度下调 |
| 单位不明 | Guardrail | notes 标注 "unit unclear"，confidence 降至 0.6 | `UNIT_UNCLEAR` | 置信度下调 |
| 空检索结果 | Retriever | 返回空列表，Promptor 提示"无相关知识" | `EMPTY_RETRIEVAL` | 回答降级 |
| 索引损坏 | Qdrant client | 触发重建（删 collection + 重新 upsert），日志报警 | `VECTOR_STORE_REBUILD` | 全量重建 |
| xlsx 缺列 | ExcelInventoryLoader | 跳过该列，记录 unmatched_columns | `COLUMN_UNMATCHED` | 结构化抽取不完整 |
| LLM 超时 | `ChatOpenAI(timeout=30)` | 重试 1 次后 fallback | `LLM_TIMEOUT` | 单轮对话降级 |
| Embedding 失败 | `EmbeddingClient.embed` | 指数退避重试 3 次，耗尽抛 EmbeddingError | `EMBEDDING_FAILED` | 资产 FAILED |
| 工具执行异常 | Tool wrapper | 捕获异常返回错误描述，不中断 Agent | `TOOL_EXEC_ERROR` | 单工具失败 |

### F.2 降级策略总览

```
未配置 EMBEDDING_API_KEY → FakeEmbeddings 入库（维度=embedding_dims）
未配置 LLM_API_KEY       → FakeListChatModel 返回预设响应
LLM 超时/解析失败        → fallback payload（citations=检索 Top-1, notes=降级原因）
指标校验无基准数据       → 跳过校验，notes="无法获取基准指标"
```

---

## G. 运行形态与配置/部署视图

### G.1 入口形态

- **REST API**：`POST /api/v1/chat`（同步）、`POST /api/v1/chat/stream`（SSE）
- **WebSocket**：`WS /ws/chat`（流式 token + 工具调用进度）
- **报告生成**：`POST /api/v1/reports/generate`

### G.2 配置中心

**.env 关键参数**：

| 配置键 | 默认值 | 说明 | 生效范围 |
| --- | --- | --- | --- |
| `LLM_PROVIDER` | zhipu | LLM 服务商 | LLM Engine |
| `LLM_API_KEY` | 空 | LLM API Key（空则用 FakeListChatModel） | LLM Engine |
| `LLM_BASE_URL` | provider 默认 | LLM API 地址 | LLM Engine |
| `LLM_MODEL` | glm-4 | 模型名 | LLM Engine |
| `LLM_TEMPERATURE` | 0.1 | 采样温度 | LLM Engine |
| `LLM_TIMEOUT` | 30 | 超时秒数 | LLM Engine |
| `EMBEDDING_PROVIDER` | zhipu | Embedding 服务商 | Embeddings |
| `EMBEDDING_API_KEY` | 空 | Embedding API Key（空则用 FakeEmbeddings） | Embeddings |
| `EMBEDDING_BASE_URL` | provider 默认 | Embedding API 地址 | Embeddings |
| `EMBEDDING_MODEL` | embedding-3 | 模型名 | Embeddings |
| `EMBEDDING_DIMS` | 1024 | 向量维度 | Embeddings/Indexer |
| `VECTOR_PROVIDER` | qdrant | 向量库类型 | Indexer |
| `QDRANT_URL` | http://qdrant:6333 | Qdrant 地址 | Indexer |
| `QDRANT_COLLECTION` | pt_knowledge | collection 名 | Indexer |
| `CHUNK_SIZE` | 500 | 切块大小 | Chunker |
| `CHUNK_OVERLAP` | 50 | 切块重叠 | Chunker |
| `TOP_K` | 5 | 检索返回数 | Retriever |
| `SIMILARITY_THRESHOLD` | 0.5 | 相似度阈值 | Retriever |
| `USE_BM25` | false | 是否启用 BM25 混检 | Retriever |
| `MAX_CONTEXT_CHARS` | 1800 | context 最大字符 | Promptor |
| `METRIC_TOLERANCE` | 0.05 | 指标容差 | Guardrail |

### G.3 部署视图

```
┌─────────────────────────────────────────────┐
│              Docker Compose 拓扑             │
├─────────────────────────────────────────────┤
│  ┌──────────┐   ┌──────────┐   ┌──────────┐ │
│  │  Master  │──▶│  Qdrant  │   │  MinIO   │ │
│  │ (uvicorn │   │ :6333    │   │ :9000    │ │
│  │  :8000)  │   └──────────┘   └──────────┘ │
│  │          │──▶│  MySQL   │   ┌──────────┐ │
│  │          │   │ :3306    │   │   ES     │ │
│  └──────────┘   └──────────┘   │ :9200    │ │
│                                └──────────┘ │
│  🔒 离线边界：Master 不主动联网              │
│     LLM/Embedding API 可选，未配置用 Mock    │
└─────────────────────────────────────────────┘
```

**启动命令**：`docker compose up -d`

**依赖**：Python 3.11、langchain>=0.2、langchain-openai、langchain-qdrant、python-docx、openpyxl、pypdf、qdrant-client。

---

## H. 观测性：日志/指标/证据定位

### H.1 关键日志点

| 日志点 | 级别 | 关键字 | 说明 |
| --- | --- | --- | --- |
| 资产上传 | INFO | `ASSET_UPLOAD asset_id={id} hash={sha}` | 记录 hash 与 reused |
| 解析开始 | INFO | `PARSE_START asset_id={id} status=pending→parsing` | 状态机流转 |
| 解析完成 | INFO | `PARSE_DONE asset_id={id} chunks={n} extracted={m}` | chunk 数与抽取行数 |
| 索引复用/重建 | INFO | `VECTOR_STORE_LOAD` / `VECTOR_STORE_BUILD` | collection 是否复用 |
| 检索结果 | INFO | `RETRIEVE query={q} top_k={n} scores={...}` | 返回条数与分数 |
| Prompt 长度 | INFO | `PROMPT_LEN chars={n}` | context 字符数 |
| LLM 调用 | INFO | `LLM_INVOKE model={m} tokens_in={n} tokens_out={m} latency={t}ms` | token 与耗时 |
| 解析失败回退 | ERROR | `LLM_PARSE_FAILED fallback=true` | 触发 fallback |
| 守护校验 | INFO | `GUARDRAIL field={f} diff={d}% within_tolerance={bool}` | 校验结果 |
| 工具调用 | INFO | `TOOL_CALL name={n} args={...} result_len={m}` | 工具链 |

### H.2 证据定位

- 日志路径：`logs/llm_calls/YYYYMMDD.log`、`logs/parse_pipeline/YYYYMMDD.log`
- 命名规则：`{module}_{YYYYMMDD}.log`
- 与测试用例证据对接：每条日志含 `asset_id`/`run_no`/`trace_id`，可按 ID 检索

### H.3 Prometheus 指标

复用已有 `master/app/metrics.py` 埋点，新增：
- `ptp_llm_call_duration_seconds{model}`：LLM 调用耗时
- `ptp_llm_tool_calls_total{tool_name}`：工具调用次数
- `ptp_retrieve_duration_seconds`：检索耗时
- `ptp_guardrail_mismatch_total`：指标校验不一致次数

---

## I. 合规与安全

### I.1 离线运行与禁止联网微调

- **离线运行**：未配置 LLM/Embedding API Key 时自动降级为 `FakeListChatModel` + `FakeEmbeddings`，不发起任何网络请求。
- **禁止联网**：生产环境 LLM/Embedding 通过 `base_url` 对接智谱/阿里 API（显式配置才生效），无配置即离线。
- **禁止微调**：仅使用 Prompt Engineering + Function Calling，代码中无微调相关调用。

### I.2 第三方依赖清单与 License

| 依赖 | License | 用途 |
| --- | --- | --- |
| langchain | MIT | RAG 框架 |
| langchain-openai | MIT | OpenAI 兼容 LLM/Embeddings |
| langchain-qdrant | MIT | Qdrant VectorStore |
| langchain-community | MIT | Loader/Fake 组件 |
| langchain-text-splitters | MIT | TextSplitter |
| qdrant-client | Apache-2.0 | Qdrant SDK |
| python-docx | MIT | docx 解析 |
| openpyxl | MIT | xlsx 解析 |
| pypdf | BSD-3-Clause | pdf 解析 |

### I.3 数据使用范围

- 文档资产存储于 MinIO（项目隔离），向量索引按 `project_id` 过滤
- LLM 调用仅传递检索到的 chunk 内容与工具结果，不透传无关数据
- 不存储用户原始对话（仅存工具调用日志用于审计）

---

## J. 需求/用例追踪与一致性

| 能力 | SRS-FR/NFR | HLD 模块 | 测试用例 |
| --- | --- | --- | --- |
| 文档资产解析 | FR-01 | Loader (B.3) | TC-ASY-001/002/003 |
| 清洗与去噪 | FR-02 | Cleaner (B.4) | TC-CLN-001 |
| 切块与重叠 | FR-03 | Chunker (B.5) | TC-CHK-001 |
| 向量索引构建 | FR-04 | Indexer+Embeddings (B.6) | TC-IDX-001/002 |
| 检索器 | FR-05 | Retriever (B.7) | TC-RET-001/002 |
| 提示词模板与工具 | FR-06 | Promptor+Tools (B.8) | TC-PRM-001/002 |
| LLM 调用与解析 | FR-07 | LLM Engine+Parser (B.9) | TC-LLM-001/002 |
| 指标校验 | FR-08 | Guardrail (B.10) | TC-VLD-001/002 |
| 统一输出格式 | FR-09 | Synthesizer (B.9) | TC-OUT-001 |
| 入口形态 | FR-10 | Service (B.11) | TC-API-001/002 |
| 离线可运行 | NFR-01 | Mock 组件 (B.6/B.9) | TC-NFR-003 |
| 鲁棒性 | NFR-02 | 异常表 (F.1) | TC-NFR-001/002 |
| 性能 | NFR-04 | 超时配置 (G.2) | TC-NFR-004 |
| 可维护性 | NFR-05 | 配置中心 (G.2) | TC-NFR-005 |

---

## K. 图文质量与可读性

### K.1 图表清单

| 编号 | 名称 | 位置 |
| --- | --- | --- |
| 图 1 | 系统架构总览（离线运行边界） | A.1 |
| 图 2 | QA 链时序图（含失败回退） | D.1 |
| 图 3 | 守护链时序图（±5% 容差） | D.2 |

### K.2 术语与缩略语

| 术语 | 含义 |
| --- | --- |
| LangChain | 大模型应用开发框架 |
| LCEL | LangChain Expression Language |
| Document | LangChain 文档对象（page_content + metadata） |
| Loader | 文档加载器抽象 |
| TextSplitter | 文本分割器 |
| Embeddings | 向量化抽象 |
| VectorStore | 向量存储抽象 |
| Retriever | 检索器接口 |
| ChatPromptTemplate | 聊天提示词模板 |
| Tool | 工具抽象（@tool / StructuredTool） |
| ChatModel | 聊天模型抽象 |
| Runnable | LCEL 可运行单元 |
| Qdrant | 向量数据库 |
| RAG | 检索增强生成 |
| TPS | 每秒交易数 |
| P95 | 95 分位响应时间 |
| Guardrail | 指标一致性校验模块 |
| FakeListChatModel | LangChain 模拟聊天模型 |
| FakeEmbeddings | LangChain 模拟向量化 |
| SUT | 被测系统 |

---

## 交付清单（HLD 专用）

1. **组件图 + 两张时序图**：本文档图 1/图 2/图 3（文本化，可导出为 draw.io）
2. **模块接口表**：B.1 模块职责表 + B.3~B.11 各模块接口签名
3. **配置与环境表**：G.2 配置中心参数表
4. **日志点清单**：H.1 关键日志点表
5. **追踪矩阵**：J 节需求/用例追踪表
