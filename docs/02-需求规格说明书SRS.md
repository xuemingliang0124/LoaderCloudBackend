# 基于 RAG 的性能测试智能助手与报告生成系统
# 需求规格说明书（SRS）

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
| v1.0 | 2026-09-20 | 项目组 | 初稿，覆盖 P3 LLM 编排接入全部功能需求 |
| v1.1 | 2026-09-20 | 项目组 | 按 LangChain 使用规范重构文档加载/切块/向量库/检索/提示词/工具/LLM 调用设计 |

---

## 目录

1. 引言、项目范围与约束
2. 总体流程与关键能力映射
3. 功能性需求（FR-01 ~ FR-10）
4. 非功能需求（NFR）
5. 数据与文件规范
6. 接口规范与错误码
7. 需求/设计/用例追踪矩阵
8. 验收与评测
9. 文档规范性与版控
10. 风险与里程碑

---

## 1. 引言、项目范围与约束

### 1.1 目的

本文档为"基于 RAG 的性能测试智能助手与报告生成系统"的需求规格说明书，旨在明确系统在已有性能测试平台（LoaderCloudBackendV2）基础上接入大语言模型（LLM）后的业务范围、功能需求、数据规范、接口契约与验收口径，作为概要设计、编码实现、测试与验收的统一依据。

### 1.2 项目范围

**项目名称**：基于 RAG 的性能测试智能助手与报告生成系统

**端到端目标**：在已有的性能测试平台（覆盖项目/脚本/场景/运行/监控全链路）之上，引入大模型能力，完成"文档资产 → 解析切块 → 向量索引 → 检索召回 → 提示词 → LLM 调用 → Function Calling 工具编排 → 压测操作/归因分析/报告生成 → 指标校验 → REST API/WebSocket 服务 → 轻量评测"的端到端流程。

**能力覆盖**（逐项落地）：

1. **文档资产解析与元数据统一**：方案文档/SLA 文档/架构文档/环境清单/交易清单（docx/xlsx/pdf）解析，统一元数据。
2. **清洗与去噪**：页眉页脚、断词、标点统一，保证检索质量。
3. **切块与重叠**：chunk_size=500、overlap=50，保留 section/chunk_id。
4. **向量索引构建**：Qdrant 向量库（本地单容器），可持久化与复用。
5. **检索器**：Top-k、阈值、去重，可选 BM25 混检。
6. **提示词模板与工具定义**：含引用插槽、Function Calling 工具定义。
7. **LLM/MockLLM 调用与结构化解析**：失败回退、引用兜底。
8. **指标校验与一致性检查**：LLM 回答中的压测指标（TPS/P95/错误率）与 ES 真实运行数据比对，±5% 视为一致。
9. **统一输出格式**：answer、citations、used_metrics、confidence、notes。
10. **入口形态**：REST API（对话、报告生成）+ WebSocket（流式响应）。

### 1.3 术语与缩略语

| 术语 | 含义 |
| --- | --- |
| RAG | Retrieval-Augmented Generation，检索增强生成 |
| LLM | Large Language Model，大语言模型 |
| LangChain | 大模型应用开发框架，提供加载/切块/向量库/检索/链/工具等抽象 |
| LCEL | LangChain Expression Language，LangChain 表达式语言，用于声明式组合 Chain |
| FR | Functional Requirement，功能性需求 |
| NFR | Non-Functional Requirement，非功能需求 |
| Qdrant | 向量数据库（Rust 实现，本项目选用，通过 `langchain-qdrant` 接入） |
| Document Loader | LangChain 文档加载器抽象（如 `Docx2txtLoader`/`PyPDFLoader`） |
| TextSplitter | LangChain 文本分割器（如 `RecursiveCharacterTextSplitter`） |
| Embeddings | LangChain 向量化抽象（如 `OpenAIEmbeddings`/`FakeEmbeddings`） |
| VectorStore | LangChain 向量存储抽象（本项目用 `Qdrant` 实现） |
| Retriever | LangChain 检索器接口，由 `VectorStore.as_retriever()` 生成 |
| ChatPromptTemplate | LangChain 聊天提示词模板，支持消息列表与变量插值 |
| Tool | LangChain 工具抽象（`@tool` 装饰器或 `StructuredTool`），供 Function Calling 调用 |
| ChatModel | LangChain 聊天模型抽象（如 `ChatOpenAI`/`FakeListChatModel`） |
| Runnable | LCEL 基础可运行单元（`RunnableSequence`/`RunnableLambda`/`RunnablePassthrough`） |
| Function Calling | 大模型函数调用能力，用于工具编排 |
| TPS | Transactions Per Second，每秒交易数 |
| P95 | 95 分位响应时间 |
| SLA | Service Level Agreement，服务等级协议 |
| SUT | System Under Test，被测系统 |
| JTL | JMeter Test Log，JMeter 结果文件 |
| WS | WebSocket |

### 1.4 参考资料

- 题目要求文档
- 已有性能测试平台技术选型文档：`docs/tech-selection.md`
- 迭代总纲：`docs/roadmap-todo.md`（P2 文档资产管道、P3 LLM 编排接入）
- LangChain 官方文档（架构参考）

### 1.5 约束条件

1. **不可联网微调**：禁止对大模型进行微调（Fine-tuning），仅使用 Prompt Engineering 与 Function Calling。
2. **仅 CPU 可运行**：考试演示环境支持仅 CPU 运行；允许使用 `FakeListChatModel`/`FakeEmbeddings` 等 Mock 组件完成端到端流程验证；生产环境可调用外部 LLM API（智谱/阿里，通过 `ChatOpenAI` 兼容接口），但不依赖网络完成核心流程。
3. **基于 LangChain 实现**：文档加载、文本切块、向量存储、检索、提示词模板、工具定义、LLM 调用与 Chain 编排均使用 LangChain 组件（`langchain` + `langchain-community` + `langchain-openai` + `langchain-qdrant`）。向量库通过 `VectorStore` 抽象层接入，业务层不直接依赖具体 SDK。
4. **离线可降级**：未配置 Embedding/LLM API Key 时，结构化抽取与检索流程照常运行；向量入库改用 `FakeEmbeddings`、LLM 调用改用 `FakeListChatModel` 返回兜底响应（不返回错误）。

### 1.6 评测维度

本系统在验收章节（第 8 章）落地以下评测维度：

1. **可运行性**：样例数据端到端跑通（资产上传 → 解析 → 检索 → LLM 调用 → 输出）。
2. **检索相关性**：Top-k 召回含正确证据的比例 ≥ 设定阈值。
3. **回答完整性**：答案含关键要素且结构化输出字段完整。
4. **引用准确性**：引用格式 `[{asset_type}:{asset_id}:chunk_{index}]` 或等价标识，命中真实来源。
5. **指标校验正确性**：对数值型压测指标（TPS/P95/错误率）执行 ±5% 容差校验并据此调整 confidence/notes。
6. **鲁棒性**：异常输入（空库/缺列/解析失败/LLM 超时）有明确定义与日志。

---

## 2. 总体流程与关键能力映射

### 2.1 用户画像与使用场景

**用户画像**：

- **性能测试工程师**：日常编写压测脚本、执行压测、查看结果。痛点是反复手工配置场景参数、阅读大量历史报告定位瓶颈。
- **测试负责人**：关注整体测试进度与报告质量。痛点是报告生成耗时、指标口径不一致。

**核心使用场景**：

1. **对话式场景操作**：用户输入"把生产环境登录交易压到 500 TPS"，系统通过工具链自动查询环境/交易、创建场景、发起压测。
2. **监控归因分析**：压测中出现异常，用户询问"这次压测为什么 P95 飙升"，系统召回历史同类故障文档（RAG）并调用 metrics 工具给出归因。
3. **测试报告自动生成**：压测结束后，系统自动拉取 run summary + metrics 聚合 + JTL 关键统计，生成 Markdown 报告并入库供后续召回。

### 2.2 端到端流程图

```
┌─────────────────────────────────────────────────────────────────┐
│                        数据流 (Data Flow)                         │
├─────────────────────────────────────────────────────────────────┤
│  文档资产(docx/xlsx/pdf)                                         │
│        │                                                         │
│        ▼                                                         │
│  [FR-01 解析] ──► [FR-02 清洗] ──► [FR-03 切块(500/50)]          │
│        │                         │                               │
│        ▼                         ▼                               │
│  结构化抽取(envs/txns)    [FR-04 向量化+Qdrant索引]              │
│                                  │                               │
│                                  ▼                               │
│  用户问题 ──► [FR-05 检索Top-k] ──► [FR-06 提示词+工具组装]      │
│                                  │                               │
│                                  ▼                               │
│                          [FR-07 LLM/MockLLM 调用]                │
│                                  │                               │
│                                  ▼                               │
│                    Function Calling 工具编排                      │
│                    (query_envs / query_txns /                    │
│                     create_scenario / get_run_summary /          │
│                     query_metrics / search_knowledge)            │
│                                  │                               │
│                                  ▼                               │
│                    [FR-08 指标校验 ±5%]                          │
│                                  │                               │
│                                  ▼                               │
│                    [FR-09 统一JSON输出]                          │
│                                  │                               │
│                                  ▼                               │
│                    [FR-10 REST API / WebSocket]                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                      证据流 (Evidence Flow)                       │
├─────────────────────────────────────────────────────────────────┤
│  citations: [{asset_type}:{asset_id}:chunk_{index}]              │
│  used_metrics: [tps, p95_ms, error_rate]                         │
│  confidence: 启发式 + 指标校验后调整                              │
│  notes: 指标不一致 / 降级提示 / 工具调用失败                     │
└─────────────────────────────────────────────────────────────────┘
```

### 2.3 能力 → 章节映射表

| 能力 | 落点章节 | LangChain 组件 |
| --- | --- | --- |
| 文档资产解析 | FR-01 / 第 5 章数据规范 | `Docx2txtLoader` / `PyPDFLoader` / 自定义 `ExcelInventoryLoader` |
| 清洗与去噪 | FR-02 | `RunnableLambda`（对 `Document.page_content` 变换） |
| 切块与重叠 | FR-03 | `RecursiveCharacterTextSplitter` |
| 向量索引构建 | FR-04 | `OpenAIEmbeddings` / `FakeEmbeddings` + `langchain_qdrant.Qdrant` |
| 检索器 | FR-05 | `VectorStore.as_retriever()` / `EnsembleRetriever`（BM25 混检） |
| 提示词模板与工具定义 | FR-06 | `ChatPromptTemplate` + `@tool` / `StructuredTool` / `create_retriever_tool` |
| LLM/MockLLM 调用与解析 | FR-07 | `ChatOpenAI` / `FakeListChatModel` + `create_tool_calling_executor` + `JsonOutputParser` |
| 指标校验与一致性检查 | FR-08 | `RunnableLambda`（自定义校验逻辑） |
| 统一输出格式 | FR-09 | `PydanticOutputParser`（`AnswerOutput` 模型） |
| 入口形态（API/WS） | FR-10 / 第 6 章接口规范 | FastAPI 路由 + `agent.astream`（WS 流式） |
| 配置中心 | NFR-05 | `BaseSettings` 注入 LLM/Embeddings 构造参数 |
| 日志与可观测 | NFR-03 | `RunnableLambda` 打点 + 已有 Prometheus 埋点 |

### 2.4 设计约束与取舍

- **技术栈选型**：基于 LangChain 框架实现 RAG 全链路，核心依赖为 `langchain`、`langchain-community`、`langchain-openai`、`langchain-qdrant`；通过 LCEL（`RunnableSequence`）组合加载→切块→向量化→检索→提示词→LLM→解析的 Chain。
- **文档加载**：按格式分发 LangChain Loader——docx 用 `Docx2txtLoader`、pdf 用 `PyPDFLoader`、xlsx 用 `UnstructuredExcelLoader`（或自定义 Loader 复用 openpyxl 做结构化抽取）；输出统一为 LangChain `Document` 对象（`page_content` + `metadata`）。
- **向量库选型**：选用 Qdrant（Rust 实现、单容器部署、async SDK 原生），不使用 ES dense_vector（避免与 pt-metrics/pt-summary 争 JVM heap）。通过 `langchain_qdrant.Qdrant` 实现 `VectorStore` 抽象，未来可切换 FAISS/Chroma（仅改实例化一行）。
- **检索策略**：以向量索引为主（`VectorStore.as_retriever(search_kwargs={"k": 5})`），可选 BM25 混检（`EnsembleRetriever` 合并 BM25Retriever + 向量 Retriever，`ContextualCompressionRetriever` 重排），合并去重后返回 Top-k。
- **LLM 编排**：以 Function Calling 为主、RAG 为辅——用 `create_tool_calling_executor`（或 `AgentExecutor` + `StructuredTool`）编排工具直接操作结构化数据（环境/交易/场景/运行），RAG 仅用于历史文档与故障知识召回（通过 `create_retriever_tool` 包装为工具）。
- **降级策略**：Embedding 未配置时用 `FakeEmbeddings` 入库；LLM 调用失败时用 `FakeListChatModel` 返回兜底 JSON（引用取检索 Top-1、notes 标注降级原因），保证端到端链路不中断。

---

## 3. 功能性需求（FR）

> 每条 FR 包含：描述、输入/输出、前置条件、异常处理、验收口径（可检测）。

### FR-01 文档资产解析与元数据统一（4 分）

**描述**：使用 LangChain Document Loader 按格式加载 docx/xlsx/pdf 文档，输出统一的 LangChain `Document` 对象（`page_content` + `metadata`）；xlsx 格式的环境清单/交易清单需额外做结构化抽取（写入 environments/transactions 表）。

**LangChain 组件**：

- 加载器分发（按扩展名）：
  - `.docx` → `langchain_community.document_loaders.Docx2txtLoader`
  - `.pdf` → `langchain_community.document_loaders.PyPDFLoader`
  - `.xlsx` → 自定义 `ExcelInventoryLoader`（封装 `openpyxl`，同时产出文本 `Document` 与结构化数据行）
  - `.doc`/`.xls`/`.pptx` → 不支持，直接抛 `UnsupportedFormatError` → 资产 status=FAILED

**输入/输出**：

- 输入：用户上传的文档文件（docx/xlsx/pdf），资产类型（plan_doc/sla_doc/architecture_doc/env_inventory/txn_inventory）
- 输出：
  - `list[Document]`：每个 `Document.page_content` 为文本，`metadata` 含 `asset_id, project_id, asset_type, filename, source, page`
  - 元数据落库：`asset_id, project_id, asset_type, filename, file_key, hash_sha256, file_size, content_type`
  - 结构化数据（仅 env_inventory/txn_inventory）：environments/transactions 表新增行

**前置条件**：

- 项目已创建，用户具备 editor+ 权限
- 文件扩展名与资产类型匹配（docx/doc/pdf 对应文档类；xlsx/xls 对应清单类）

**异常处理**：

- 扩展名与资产类型不匹配 → 错误码 3060
- `.doc`/`.xls`/`.pptx` 旧格式 → 解析失败，status=FAILED，提示"请另存为 .docx/.xlsx"
- xlsx 表头未匹配到映射列 → 记录 `parse_meta.unmatched_columns`，结构化抽取跳过，文本切片照常
- Loader 抛异常（文件损坏）→ 捕获后 status=FAILED，记录异常到 parse_meta

**验收口径**：

- 能从 docx/pdf 生成 `Document` 列表，`metadata` 含 asset_id/asset_type；
- 上传环境交付清单.xlsx 后，environments 表自动新增行（含 hosts/variables 宽松解析）；
- 同项目同 hash 二次上传返回原 asset_id（reused=true），不重复存 MinIO。

---

### FR-02 清洗与去噪（2 分）

**描述**：对 LangChain `Document` 的 `page_content` 进行清洗，去除页眉页脚、修复断词、统一标点与空白，保证后续检索质量。实现为 `RunnableLambda` 对 `Document` 列表做就地变换（保留 metadata）。

**LangChain 组件**：`RunnableLambda`（`langchain_core.runnables`），输入输出均为 `list[Document]`。

**输入/输出**：

- 输入：`list[Document]`（FR-01 输出，page_content 含页眉页脚、断词、不规则标点）
- 输出：`list[Document]`（page_content 已清洗，metadata 透传）

**前置条件**：FR-01 已完成解析。

**异常处理**：page_content 为空 → 跳过该 Document，记录日志。

**验收口径**：

- "Page x of y" 类页眉页脚被移除；
- `-\n` 断词被修复（如 "perform-\nance" → "performance"）；
- 换行符在非段落边界处转为空格；
- 提供清洗前后对比样例。

---

### FR-03 切块与重叠（3 分）

**描述**：使用 LangChain `RecursiveCharacterTextSplitter` 将清洗后的 `Document` 切分为固定大小的 chunk，默认 `chunk_size=500`、`chunk_overlap=50`，保留 section 与 chunk_id（写入 metadata）。

**LangChain 组件**：`langchain_text_splitters.RecursiveCharacterTextSplitter`

```python
splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
)
chunks = splitter.split_documents(documents)
```

**输入/输出**：

- 输入：`list[Document]`（FR-02 输出）、`chunk_size`（默认 500）、`chunk_overlap`（默认 50）
- 输出：`list[Document]`，每个 chunk 的 `metadata` 含 `chunk_index, section, asset_id, asset_type, project_id`

**前置条件**：FR-02 已完成清洗。

**异常处理**：单个 Document 长度 < chunk_size → 生成单个 chunk。

**验收口径**：

- 统计 chunk 数量与总字符数；
- 每个 chunk 的 `metadata` 保留 `chunk_index` 与 `section`（若可识别）；
- 相邻 chunk 重叠 50 字符。

---

### FR-04 向量索引构建（Qdrant）（3 分）

**描述**：使用 LangChain `Embeddings` 抽象将 chunk 向量化，通过 `langchain_qdrant.Qdrant` 写入 Qdrant 向量库；支持首次构建与复用加载。

**LangChain 组件**：

- Embeddings：`langchain_openai.OpenAIEmbeddings`（生产，OpenAI 兼容协议，对接智谱/阿里）/ `langchain_community.embeddings.FakeEmbeddings`（离线降级）
- VectorStore：`langchain_qdrant.Qdrant`（实现 `VectorStore` 抽象，`add_documents` 入库、`from_documents` 建库）
- 入库调用：`vector_store.add_documents(chunks, ids=point_ids)`，其中 `point_id = crc32(f"{asset_id}:{chunk_index}")` 确定性生成

```python
embeddings = OpenAIEmbeddings(
    model=settings.embedding_model,
    api_key=settings.embedding_api_key,
    base_url=settings.embedding_base_url,
) if settings.embedding_api_key else FakeEmbeddings(size=settings.embedding_dims)

vector_store = Qdrant(
    client=qdrant_client,
    collection_name=settings.qdrant_collection,
    embeddings=embeddings,
)
vector_store.add_documents(chunks, ids=point_ids)
```

**输入/输出**：

- 输入：`list[Document]`（FR-03 输出的 chunks）、Embedding 配置（provider/model/dims）
- 输出：Qdrant collection 中持久化的向量点（payload 含 asset_id/project_id/asset_type/chunk_index 等 metadata）

**前置条件**：

- Qdrant 服务可用（本地单容器）
- Embedding 组件已实例化（未配置 API Key 时自动降级为 `FakeEmbeddings`）

**异常处理**：

- Qdrant 不可达 → 重试 3 次后抛异常，资产 status=FAILED
- Embedding 调用失败 → 指数退避重试 3 次，耗尽抛 `EmbeddingError`，资产 status=FAILED
- FakeEmbeddings 降级时不抛异常，正常入库（维度由 `embedding_dims` 配置）

**验收口径**：

- 首次构建：collection 创建成功，向量点可通过 `as_retriever` 检索；
- 复用加载：已有 collection 直接加载（`Qdrant(client=..., collection_name=...)`），不重复构建；
- `point_id` 确定性生成，重解析幂等覆盖；
- 降级场景：未配置 API Key 时使用 `FakeEmbeddings` 仍可完成入库与检索。

---

### FR-05 检索器（Top-k、阈值、去重、可混检）（4 分）

**描述**：通过 LangChain `Retriever` 接口从 Qdrant 召回 Top-k 最相关 chunk；支持相似度阈值过滤与去重；可选 BM25 混检。

**LangChain 组件**：

- 基础检索：`vector_store.as_retriever(search_type="similarity_score_threshold", search_kwargs={"k": 5, "score_threshold": 0.5, "filter": {"project_id": project_id}})`
- BM25 混检：`langchain.retrievers.EnsembleRetriever`（合并 `BM25Retriever.from_documents` + 向量 Retriever，权重 `[0.5, 0.5]`），可选 `ContextualCompressionRetriever` 重排
- 多租户隔离：通过 `search_kwargs["filter"]` 强制 `project_id` 过滤

```python
retriever = vector_store.as_retriever(
    search_type="similarity_score_threshold",
    search_kwargs={"k": 5, "score_threshold": 0.5, "filter": {"project_id": project_id}},
)
docs = retriever.invoke(query)
```

**输入/输出**：

- 输入：`project_id, query, top_k（默认 5）, threshold（默认 0.5）, use_bm25`
- 输出：`list[Document]`，每个 chunk 的 `metadata` 含 `text, asset_id, asset_type, chunk_index, score`

**前置条件**：FR-04 已构建索引。

**异常处理**：

- 索引为空 → `retriever.invoke` 返回空列表，不报错；
- 查询为空 → 返回空列表。

**验收口径**：

- Top-k 返回条数 ≤ 指定值；
- 低于 `score_threshold` 的结果被过滤；
- 开启 BM25 混检时，`EnsembleRetriever` 合并去重后 ≤ Top-k；
- 多租户隔离：`search_kwargs["filter"]` 强制 project_id，不返回其他项目数据。

---

### FR-06 提示词模板与工具定义（4 分）

**描述**：使用 LangChain `ChatPromptTemplate` 定义系统提示词模板（含引用插槽、工具说明），使用 `@tool` 装饰器或 `StructuredTool.from_function` 定义 Function Calling 工具集；工具对应已有 REST 接口的封装。

**LangChain 组件**：

- 提示词：`langchain_core.prompts.ChatPromptTemplate`（含 `SystemMessage`、`MessagesPlaceholder(variable_name="chat_history")`、`HumanMessage`）
- 工具：`langchain_core.tools.@tool` 装饰器或 `StructuredTool.from_function`
- RAG 检索工具：`langchain.tools.retriever.create_retriever_tool(retriever, name="search_knowledge", description=...)`

**提示词模板要素**：

- 系统约束：仅基于提供的 context 与工具结果回答，禁止编造；引用格式 `[{asset_type}:{asset_id}:chunk_{index}]`
- 问题改写：将用户口语化问题改写为明确指令
- 证据拼接：将检索到的 chunk 拼接为 context（≤ 1800 字符）
- 引用占位符：`{citations}`

```python
prompt = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_PROMPT),  # 含约束、引用格式说明
    MessagesPlaceholder(variable_name="chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder(variable_name="agent_scratchpad"),  # Function Calling 必需
])
```

**工具集**（每个工具对应一个 REST 接口封装，通过 `@tool` 定义）：

| 工具名 | 功能 | 对应接口 |
| --- | --- | --- |
| `query_environments(project_id, name?)` | 查询项目环境清单 | GET /projects/{pid}/environments |
| `query_transactions(project_id, code?)` | 查询项目交易清单 | GET /projects/{pid}/transactions |
| `get_scenario(scenario_id)` | 获取场景详情 | GET /scenarios/{id} |
| `create_scenario(...)` | 创建压测场景 | POST /projects/{pid}/scenarios |
| `get_run_summary(run_no)` | 获取运行汇总 | GET /runs/{run_no}/summary |
| `get_realtime_summary(run_no)` | 获取实时汇总 | GET /runs/{run_no}/realtime |
| `query_metrics(run_no, agg)` | 查询压测指标 | 复用 metrics 埋点 |
| `search_knowledge(project_id, query, top_k)` | RAG 知识召回（由 `create_retriever_tool` 包装） | GET /assets/knowledge-search |

**输入/输出**：

- 输入：用户问题、检索到的 context、工具列表
- 输出：`ChatPromptValue`（组装好的消息列表）+ `list[Tool]`

**异常处理**：context 超长 → 截断至 1800 字符并记录日志。

**验收口径**：

- 模板含系统约束、问题改写、证据拼接、引用占位符；
- 工具的 `args_schema` JSON Schema 符合 OpenAI Function Calling 规范；
- context 长度 ≤ 1800 字符；
- `search_knowledge` 由 `create_retriever_tool` 包装 Retriever 生成，无需手写工具逻辑。

---

### FR-07 LLM/MockLLM 调用与结构化解析（4 分）

**描述**：使用 LangChain `ChatModel` 抽象调用 LLM（或 Mock），通过 `create_tool_calling_executor`（或 `AgentExecutor` + LCEL）支持 Function Calling 多轮编排；输出经 `JsonOutputParser`/`PydanticOutputParser` 解析，失败时回退。

**LangChain 组件**：

- ChatModel：`langchain_openai.ChatOpenAI`（生产，OpenAI 兼容协议，对接智谱/阿里）/ `langchain_community.chat_models.FakeListChatModel`（离线降级）
- Agent：`langgraph.prebuilt.create_react_agent` 或 `langchain.agents.create_tool_calling_executor`（LangChain 0.2+ 推荐）
- 输出解析：`langchain_core.output_parsers.JsonOutputParser` 或 `PydanticOutputParser`（定义 `AnswerOutput` Pydantic 模型含 answer/citations/used_metrics/confidence/notes）
- 降级回退：`RunnableLambda` 包装兜底逻辑（引用取检索 Top-1，notes 标注降级原因）

```python
llm = ChatOpenAI(
    model=settings.llm_model,
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
    temperature=0.1,
    timeout=30,
) if settings.llm_api_key else FakeListChatModel(responses=[FALLBACK_RESPONSE])

agent = create_tool_calling_executor(llm, tools, prompt=prompt)
result = agent.invoke({"input": user_message, "chat_history": history})
# result 含 messages，最后一条 AIMessage 含 tool_calls 或 content
```

**输入/输出**：

- 输入：`ChatPromptValue`（FR-06 输出）、工具列表、用户消息
- 输出：`AgentResult`（含 `messages`、`tool_calls`、最终 `content`）→ 经输出解析器转为结构化 JSON

**前置条件**：FR-06 已组装 prompt 与工具。

**异常处理**：

- LLM 调用超时（30s）→ `ChatOpenAI(timeout=30)` 抛异常 → 捕获后回退到兜底 JSON（引用取检索 Top-1，notes 标注超时）；
- JSON 解析失败 → `JsonOutputParser` 的 `parse_with_prompt` 抛 `OutputParserException` → 尝试 normalize（去除 markdown 代码块标记、修复引号）；仍失败则回退；
- 引用为空 → 兜底填充检索 Top-1 的引用；
- `FakeListChatModel` 降级时直接返回预设响应，不抛异常。

**验收口径**：

- 正常调用返回可解析 JSON（经 `JsonOutputParser`）；
- 人为破坏返回时走回退逻辑，citations 非空；
- `FakeListChatModel` 可回放固定响应用于离线测试；
- 工具调用链可追踪（`result.messages` 中含 `ToolMessage`）。

---

### FR-08 指标校验与一致性检查（6 分，权重最高）

**描述**：对 LLM 回答中出现的压测指标数值，与 ES 中存储的真实运行数据比对；差值 ≤ ±5% 视为一致，否则在 `notes` 写入提示并降低 `confidence`。

**支持字段**：`tps, p95_ms, error_rate`（对应 SLA 指标与运行实际指标）

**输入/输出**：

- 输入：`(run_no, payload: dict) -> payload`
- 输出：带校验结果的 JSON（含 `answer, citations, used_metrics, confidence, notes`）

**前置条件**：

- run_no 对应的运行已结束并有 ES 指标数据；
- LLM 回答中含可解析的数值。

**异常处理**：

- run_no 不存在 → 跳过校验，notes 标注"无法获取基准指标"；
- 数值无法解析 → 该字段标记为 "N/A" 并写入日志；
- 指标超出 ±5% 容差 → notes 写入 "Mismatch with actual metrics: {field}"，confidence 降至 ≤ 0.7。

**函数级约定**：

```python
def validate_metrics(run_no: int, payload: dict) -> dict:
    """
    从 payload.answer 中正则抽取 tps/p95_ms/error_rate 数值，
    与 ES 中 run_no 对应的实际指标比对，±5% 内视为一致。
    不一致时写入 notes 并降低 confidence。
    """
```

**验收口径**：

- 给定 run_no 的问答样例，若 LLM 回答 TPS 与实际差异 10%，notes 标注 "Mismatch with actual metrics: tps"，confidence ≤ 0.7；
- ±5% 容差内的指标标记为一致，notes 为空或标注 "Metrics verified"；
- 输出 JSON 示例：

```json
{
  "answer": "本次压测 TPS 为 480，P95 为 120ms",
  "citations": ["run_summary:1001", "metrics:1001:p95"],
  "used_metrics": ["tps", "p95_ms"],
  "notes": "Mismatch with actual metrics: tps",
  "confidence": 0.68
}
```

---

### FR-09 统一输出格式（4 分）

**描述**：所有 LLM 相关接口返回统一 JSON 格式，字段含义明确。

**输出字段定义**：

| 字段 | 类型 | 含义 | 示例 |
| --- | --- | --- | --- |
| answer | string | 自然语言回答 | "已为您创建登录交易压测场景" |
| citations | array[string] | 引用来源列表 | ["plan_doc:12:chunk_3", "env:5"] |
| used_metrics | array[string]? | 涉及的指标字段（可选） | ["tps", "p95_ms"] |
| confidence | float | 置信度（0-1） | 0.85 |
| notes | string | 备注（指标不一致/降级原因等） | "Metrics verified" |

**异常处理**：字段缺失 → 系统补齐默认值（citations=[]、confidence=0.5、notes=""）。

**验收口径**：

- 返回 JSON 必须含 5 个字段（used_metrics 可选）；
- citations 格式统一为 `[{type}:{id}:chunk_{index}]`；
- confidence 来源可解释（启发式 + 指标校验后调整）。

---

### FR-10 入口形态：REST API 与 WebSocket（2 分）

**描述**：提供 REST API 用于对话问答与报告生成，WebSocket 用于流式响应。

**REST API**：

- `POST /api/v1/chat`：提交对话问题，返回 JSON（同步）
- `POST /api/v1/chat/stream`：提交对话问题，返回 SSE 流式响应
- `POST /api/v1/reports/generate`：为指定 run_no 生成 LLM 报告
- `GET /api/v1/assets/knowledge-search`：RAG 检索

**WebSocket**：

- `WS /ws/chat`：前端流式接收 LLM 输出与工具调用进度

**输入/输出（对话示例）**：

- 请求：
```json
{
  "project_id": 1,
  "message": "把生产环境登录交易压到 500 TPS",
  "use_tools": true
}
```

- 响应（FR-09 统一格式）

**异常处理**：

- 必填字段缺失 → 422 校验错误；
- project_id 不存在或无权限 → 403；
- LLM 服务不可用 → 503 并返回降级 JSON。

**验收口径**：

- REST API 成功返回结构化 JSON；
- WebSocket 可接收流式 token；
- 错误码语义明确。

---

## 4. 非功能需求（NFR）

### NFR-01 可运行性（3 分）

- **离线/仅 CPU**：考试演示环境支持仅 CPU 运行，使用 LangChain `FakeListChatModel` + `FakeEmbeddings` 完成端到端流程；
- **可降级**：未配置 LLM/Embedding API Key 时，自动切换到 Mock 组件（不返回错误），结构化抽取与检索照常运行；
- **容器化部署**：通过 docker-compose 一键启动（Qdrant + Master），LangChain 依赖随 requirements.txt 安装。

### NFR-02 鲁棒性（2 分）

- 空目录/空索引 → 友好提示，不崩溃；
- CSV/xlsx 缺列 → 跳过该列并记录 warnings；
- 解析失败 → status=FAILED，支持 retry-parse；
- LLM 超时/解析失败 → 回退兜底 JSON；
- 非法问题（空串/超长） → 参数校验拒绝。

### NFR-03 日志与可观测（2 分）

- 日志等级：INFO/ERROR/EXCEPTION；
- 关键打点：资产上传/解析状态流转、检索耗时与 Top-k 分数、LLM 调用耗时与 token 数、工具调用链、指标校验结果；
- 复用已有 Prometheus 埋点（`master/app/metrics.py`）。

### NFR-04 性能（3 分）

- 样例数据端到端（上传 → 解析 → 检索 → 回答）≤ 30 秒（MockLLM）；
- 单轮 LLM 调用超时 30 秒；
- 检索响应 ≤ 2 秒（Qdrant 单容器，万级 chunk）。

### NFR-05 可维护性与配置（3 分）

- 配置中心：所有参数走 `master/app/core/config.py` + `.env` + 默认值；
- 关键配置项：
  - `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `LLM_TEMPERATURE` / `LLM_TIMEOUT`
  - `EMBEDDING_PROVIDER` / `EMBEDDING_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_MODEL` / `EMBEDDING_DIMS`
  - `VECTOR_PROVIDER` / `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION`
  - `CHUNK_SIZE` / `CHUNK_OVERLAP` / `TOP_K` / `SIMILARITY_THRESHOLD`
- LangChain 依赖（requirements.txt）：
  - `langchain>=0.2` / `langchain-core` / `langchain-community` / `langchain-openai` / `langchain-qdrant` / `langchain-text-splitters`
  - `python-docx` / `openpyxl` / `pypdf`（供 Docx2txtLoader / ExcelLoader / PyPDFLoader 使用）
- 禁止硬编码 API Key；`ChatOpenAI` 与 `OpenAIEmbeddings` 的 `api_key`/`base_url` 均从 config 注入。

---

## 5. 数据与文件规范

### 5.1 目录结构

```
data/
├── assets/                 # 原始文档（MinIO: ptp/assets/{asset_id}/{filename}）
├── vector_store/           # Qdrant 持久化（qdrant_data 卷）
└── reports/                # LLM 生成的报告（MinIO: ptp/reports/{run_no}/llm-report.md）
logs/
├── llm_calls/              # LLM 调用日志
└── parse_pipeline/         # 解析管道日志
```

### 5.2 核心数据字典

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

**Qdrant collection（pt_knowledge）payload（对应 LangChain Document.metadata）**：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| asset_id | int | 资产 ID |
| project_id | int | 项目 ID（过滤用，对应 `search_kwargs["filter"]`） |
| asset_type | string | 资产类型 |
| chunk_index | int | 块序号 |
| source | string | 来源文件路径（LangChain Document 标准字段） |
| page | int | 页码（pdf 加载器产出） |
| source_type | string | 来源类型（asset/report/domain_doc） |
| source_ref | string | 来源引用（file_key/run_no） |
| created_at | datetime | 创建时间 |

### 5.3 引用标识规范

统一格式：`[{type}:{id}:chunk_{index}]`

示例：

- 文档资产：`[plan_doc:12:chunk_3]`
- 运行汇总：`[run_summary:1001]`
- 指标：`[metrics:1001:p95]`
- 环境：`[env:5]`

### 5.4 评测数据格式（dev_set.jsonl）

每行一个 JSON 对象：

```json
{
  "query": "把生产环境登录交易压到 500 TPS",
  "target": "应调用 create_scenario 工具，参数含 env_code=prod, txn_code=login, tps=500",
  "expected_tools": ["query_environments", "query_transactions", "create_scenario"],
  "notes": "验证工具链编排正确性"
}
```

---

## 6. 接口规范与错误码

### 6.1 REST API

**POST /api/v1/chat**

请求：
```json
{
  "project_id": 1,
  "message": "string",
  "use_tools": true,
  "top_k": 5
}
```

响应（FR-09 统一格式）：
```json
{
  "answer": "string",
  "citations": ["plan_doc:12:chunk_3"],
  "used_metrics": ["tps"],
  "confidence": 0.85,
  "notes": ""
}
```

**POST /api/v1/reports/generate**

请求：
```json
{
  "run_no": "RUN-20260920-001"
}
```

响应：
```json
{
  "run_no": "RUN-20260920-001",
  "report_key": "reports/RUN-20260920-001/llm-report.md",
  "confidence": 0.9,
  "notes": ""
}
```

### 6.2 WebSocket

**WS /ws/chat**

- 客户端发送：`{"project_id": 1, "message": "..."}`
- 服务端推送（流式）：`{"type": "token", "content": "..."}`
- 工具调用：`{"type": "tool_call", "tool": "create_scenario", "args": {...}}`
- 结束：`{"type": "done", "final": {<FR-09 格式>}}`

### 6.3 错误码

| 错误码 | 含义 |
| --- | --- |
| 3060 | 扩展名与资产类型不匹配 |
| 3061 | 资产不存在 |
| 3062 | 跨项目访问 |
| 3063 | 资产状态不允许此操作（如非 pending/failed 不可 retry-parse） |
| 4000 | LLM 调用失败 |
| 4001 | LLM 响应 JSON 解析失败（已回退） |
| 4002 | Embedding 调用失败 |
| 4003 | 向量库不可用 |
| 4004 | 指标校验失败（run_no 不存在） |

---

## 7. 需求/设计/用例追踪矩阵

| FR 编号 | 用例 ID | 用例描述 | 验收口径（可验证点） | 证据/日志路径 |
| --- | --- | --- | --- | --- |
| FR-01 | TC-ASY-001 | 解析 docx 成文本+元数据 | 返回带 asset_id/asset_type 的文本 | logs/parse_pipeline/ |
| FR-01 | TC-ASY-002 | xlsx 环境清单结构化抽取 | environments 表新增行含 hosts/variables | logs/parse_pipeline/ |
| FR-01 | TC-ASY-003 | 同 hash 二次上传复用 | 返回 reused=true，不重复存 MinIO | logs/parse_pipeline/ |
| FR-02 | TC-CLN-001 | 去页眉脚与断词修复 | "Page x of y" 消失、`-\n` 消除 | 对比前后文本 |
| FR-03 | TC-CHK-001 | 切块 500/50 | 生成 chunk 含 chunk_index/section | 统计数量 |
| FR-04 | TC-IDX-001 | 首次建库 | collection 创建，向量可检索 | Qdrant dashboard |
| FR-04 | TC-IDX-002 | 复用向量库 | 已有 collection 直接加载 | 日志含 "load" |
| FR-05 | TC-RET-001 | Top-5 语义检索 | 返回 ≤5 段，含 score | 结果列表 |
| FR-05 | TC-RET-002 | 混检去重 | BM25+向量合并去重 ≤5 | 去重检查 |
| FR-06 | TC-PRM-001 | 模板与上下文拼接 | context ≤1800 字符，含引用占位符 | 消息截取 |
| FR-06 | TC-PRM-002 | 工具定义校验 | JSON Schema 符合 OpenAI 规范 | schema 校验 |
| FR-07 | TC-LLM-001 | 正常调用解析 | 返回可解析 JSON | JSON |
| FR-07 | TC-LLM-002 | 解析降级 | 破坏返回走回退，citations 非空 | JSON |
| FR-08 | TC-VLD-001 | 指标校验 ±5% | 差异 >5% 降 confidence 并写 notes | JSON |
| FR-08 | TC-VLD-002 | 容差内一致 | 差异 ≤5% 标注 Metrics verified | JSON |
| FR-09 | TC-OUT-001 | 输出字段完整 | 含 answer/citations/confidence/notes | JSON |
| FR-10 | TC-API-001 | REST 对话返回 JSON | 控制台输出合法 JSON | 终端截图 |
| FR-10 | TC-API-002 | WS 流式响应 | 收到 token 流 | 终端截图 |
| NFR-02 | TC-NFR-001 | 空库鲁棒性 | 空索引返回空列表不报错 | 日志 |
| NFR-02 | TC-NFR-002 | LLM 超时降级 | 超时返回兜底 JSON | 日志 |

---

## 8. 验收与评测

### 8.1 评测维度落地为可测 KPI

| 评测维度 | KPI | 阈值 |
| --- | --- | --- |
| 可运行性 | 样例数据端到端跑通 | 100% 成功 |
| 检索相关性 | Top-k 含正确证据比例 | ≥ 80% |
| 回答完整性 | 结构化输出字段完整率 | 100% |
| 引用准确性 | 引用格式合规率 | 100%（`[{type}:{id}:chunk_{index}]`） |
| 指标校验正确性 | ±5% 容差内判定一致准确率 | ≥ 95% |
| 鲁棒性 | 异常输入优雅失败率 | 100% |

### 8.2 评分映射

- 引用准确率 ≥ 90%：满分；80-90%：8/10；< 80%：5/10
- 指标校验准确率 ≥ 95%：满分；90-95%：8/10；< 90%：5/10
- 端到端成功率 100%：满分；否则按成功比例给分

### 8.3 dev_set.jsonl 使用方式

- 用作自动化评测脚本的输入；
- 每条样例验证：工具链是否正确调用、回答是否含关键要素、引用是否非空；
- 评测脚本输出：通过率、平均 confidence、失败样例明细。

---

## 9. 文档规范性与版控

- 采用统一模板，含封面、修订记录、目录、图表编号；
- 术语与缩略语与本平台一致（见 1.3 节）；
- 合规声明：禁止微调大模型、禁止硬编码 API Key、考试环境仅 CPU / `FakeListChatModel` + `FakeEmbeddings` 可运行；
- 版控：正文首页列出版本修订记录（见文首）。

---

## 10. 风险与里程碑

### 10.1 风险

| 风险 | 应对 |
| --- | --- |
| LLM API 不可用 | `FakeListChatModel` 兜底，返回预设响应 |
| Embedding 未配置 | `FakeEmbeddings` 替代，结构化抽取与向量入库照常 |
| LangChain 版本接口变更 | 锁定 `langchain>=0.2` 主版本，关键调用加封装层 |
| xlsx 表头不规范 | unmatched_columns 记录，D5 remap 接口修正 |
| 指标校验无基准数据 | 跳过校验，notes 标注 |

### 10.2 里程碑

| 里程碑 | 内容 |
| --- | --- |
| M1 | FR-01~FR-05（资产解析→检索）完成 |
| M2 | FR-06~FR-07（LLM 调用+工具编排）完成 |
| M3 | FR-08~FR-10（指标校验+输出+接口）完成 |
| M4 | 全量测试通过，提交验收 |

---

## 附录 A：FR-08 指标校验范式

```json
{
  "answer": "本次压测 TPS 为 480，P95 为 120ms",
  "citations": ["run_summary:1001", "metrics:1001:p95"],
  "used_metrics": ["tps", "p95_ms"],
  "notes": "Mismatch with actual metrics: tps",
  "confidence": 0.68
}
```

## 附录 B：交付检查清单（SRS 自检）

- [x] 约束（CPU/离线/不可微调）已声明
- [x] 评测维度已在第 8 章落地
- [x] FR-01 ~ FR-10 完整且可验收
- [x] 输出字段与 ±5% 规则写清楚并有例证
- [x] 能力→章节映射表 + 追踪矩阵齐全
- [x] REST API/WS 接口规范 + 错误码完整
- [x] 日志/异常/NFR 到位
- [x] 全部 RAG 组件基于 LangChain 抽象实现（Loader/Splitter/Embeddings/VectorStore/Retriever/PromptTemplate/Tool/ChatModel/OutputParser）

## 附录 C：LangChain 组件依赖与版本

| 组件 | 包 | 用途 |
| --- | --- | --- |
| `langchain` | langchain>=0.2 | 核心框架、Agent、Retriever |
| `langchain-core` | langchain-core | Runnable、PromptTemplate、Tool、OutputParser 抽象 |
| `langchain-community` | langchain-community | Docx2txtLoader、PyPDFLoader、FakeEmbeddings、FakeListChatModel、BM25Retriever |
| `langchain-openai` | langchain-openai | ChatOpenAI、OpenAIEmbeddings（OpenAI 兼容协议） |
| `langchain-qdrant` | langchain-qdrant | Qdrant VectorStore 实现 |
| `langchain-text-splitters` | langchain-text-splitters | RecursiveCharacterTextSplitter |
| `python-docx` | python-docx | Docx2txtLoader 依赖 |
| `openpyxl` | openpyxl | Excel 结构化抽取 |
| `pypdf` | pypdf | PyPDFLoader 依赖 |
