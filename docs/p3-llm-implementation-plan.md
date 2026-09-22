# P3 LLM 编排接入实施方案

> 基于 SRS v1.1 + HLD v1.0 + roadmap-todo.md P3 阶段，覆盖 FR-01~FR-10 + NFR-01~05 全量重构。

## 一、重构范围盘点（FR → 现状 → 动作）

| FR | SRS 强约束 | 仓库现状 | 动作 |
|---|---|---|---|
| FR-01 Loader | `Docx2txtLoader`/`PyPDFLoader`/自定义`ExcelInventoryLoader`（继承`BaseLoader`） | `asset_parser.py` `parse_docx/parse_xlsx/parse_pdf` 裸函数 | **重构**：包成 LangChain `BaseLoader` 子类 |
| FR-02 Cleaner | `RunnableLambda` 对 `list[Document]` 变换 | 无 | **新增** |
| FR-03 Chunker | `RecursiveCharacterTextSplitter` | `asset_parser.chunk_text` 自写段落贪心聚合 | **重构**：替换为 `RecursiveCharacterTextSplitter` |
| FR-04 Indexer | `langchain_qdrant.Qdrant` + `OpenAIEmbeddings`/`FakeEmbeddings` | `vector_store.py` 自写 Protocol；`embedding_client.py` 裸 httpx | **重构**：叠加 LangChain `Embeddings` + `Qdrant` VectorStore |
| FR-05 Retriever | `as_retriever` + `EnsembleRetriever` | 无 | **新增** |
| FR-06 Promptor+Tools | `ChatPromptTemplate` + `@tool` + `create_retriever_tool` | 无 | **新增** |
| FR-07 LLM Engine+Parser | `ChatOpenAI`/`FakeListChatModel` + `create_tool_calling_executor` + `PydanticOutputParser` | 无 | **新增** |
| FR-08 Guardrail | ±5% 容差校验 | 无 | **新增** |
| FR-09 统一输出 | `AnswerOutput` + `PydanticOutputParser` | 无 | **新增**（随 FR-07） |
| FR-10 入口 | `POST /chat` + `WS /ws/chat` + `POST /reports/generate` + `GET /assets/knowledge-search` | 无 chat/ws；knowledge-search D2 计划未实现 | **新增** |
| NFR-01 离线降级 | `FakeListChatModel` + `FakeEmbeddings` 入库 | embedding 未配置时**跳过**入库（indexed=false） | **重构**：改为 `FakeEmbeddings` 入库（SRS 1.5.4 明确要求"向量入库改用 FakeEmbeddings"） |

## 二、重构核心策略（适配器模式 + 双抽象共存）

### 策略 1：结构化抽取逻辑全部保留（与 LangChain 无关）

`asset_parser.py` 中的下列纯业务逻辑**不动**：
- `ENV_INVENTORY_COLUMN_MAP` / `TXN_INVENTORY_COLUMN_MAP` 列映射常量
- `extract_inventory_rows` / `match_column` / `_normalize_header` 表头识别
- `_split_list_value` / `_parse_dict_value` / `_parse_number` 宽松取值
- `_dedupe_rows` 三层去重
- `_build_environment_row` / `_build_transaction_row` 行字典→ORM
- `_extract_structured` 结构化抽取主流程
- 状态机 CAS（PENDING→PARSING→READY/FAILED）

**只重构"文本路径"**：`dispatch_parse` → `chunk_text` → `_index_chunks` 这条链。

### 策略 2：VectorStore 双抽象共存

| 抽象 | 用途 | 实现 |
|---|---|---|
| 现有 `VectorStore` Protocol | 业务层接口（可切 Milvus，D2 约定） | 保留 `vector_store.py` `QdrantVectorStore` |
| LangChain `VectorStore` | FR-04/05 LangChain 链路用 | 新增 `get_langchain_vector_store()` 工厂，返回 `langchain_qdrant.Qdrant` |

**关键约束**：两边共享同一 collection（`pt_knowledge`）+ 同一 point_id 算法（`crc32(f"{asset_id}:{chunk_index}")`），保证 D3 入库的向量 L1 检索能直接命中。LangChain 链路用 `Qdrant.add_documents(ids=...)` 显式传 point_id。

### 策略 3：Embedding 双层封装

| 层 | 职责 | 实现 |
|---|---|---|
| LangChain `Embeddings` | FR-04 链路用（`OpenAIEmbeddings`/`FakeEmbeddings`） | 新增 `get_embeddings()` 工厂 |
| 现有 `EmbeddingClient` | D3 批量/重试封装 | **重构**：内部委托给 LangChain `Embeddings`，保留批量切片+指数退避 |

**NFR-01 降级重构**：未配置 `EMBEDDING_API_KEY` 时，从"跳过入库（indexed=false）"改为"`FakeEmbeddings` 入库（indexed=true, degraded=true）"，对齐 SRS 1.5.4。

### 策略 4：Loader 适配器（不重写解析器底层）

新增 `services/langchain_loader.py`，把现有 `parse_docx/parse_xlsx/parse_pdf` 包装成 LangChain `BaseLoader` 子类：

- `DocxAssetLoader(BaseLoader)`：包装 `parse_docx`
- `PdfAssetLoader(BaseLoader)`：包装 `parse_pdf`，每页一个 Document（page 入 metadata）
- `ExcelInventoryLoader(BaseLoader)`：包装 `parse_xlsx`，产出文本 Document + 触发结构化抽取钩子

**理由**：底层 python-docx/openpyxl/pdfplumber 已经稳定且通过测试，重写无收益；SRS 1.5.3 要求的是"LangChain Loader 抽象"，包装即满足。

### 策略 5：LCEL 链式组装

FR-01~04 用 LCEL `RunnableSequence` 串起来：

```python
pipeline = (
    RunnableLambda(load_documents)      # FR-01 Loader
    | RunnableLambda(clean_documents)   # FR-02 Cleaner
    | RunnableLambda(split_documents)   # FR-03 Chunker
    | RunnableLambda(index_documents)   # FR-04 Indexer
)
```

结构化抽取作为 Loader 的副作用钩子（ExcelInventoryLoader 在 `lazy_load` 时触发 `_extract_structured`）。

## 三、目标目录结构

```
master/app/services/
├── asset_parser.py          # 重构：保留结构化抽取，文本路径委托 langchain_pipeline
├── embedding_client.py      # 重构：委托 LangChain Embeddings + Fake 降级
├── vector_store.py          # 保留 Protocol + 新增 get_langchain_vector_store()
├── langchain_loader.py      # 新增：3 个 BaseLoader 子类（FR-01）
├── langchain_pipeline.py    # 新增：Cleaner + Chunker + LCEL 链（FR-02/03/04）
├── es_client.py             # 补 get_run_metrics 标准化方法（FR-08 用）
├── llm/                     # 新增整个目录
│   ├── __init__.py
│   ├── client.py            # get_llm + AnswerOutput + PydanticOutputParser + fallback（FR-07/09）
│   ├── tools.py             # 8 个 @tool + create_retriever_tool（FR-06）
│   ├── orchestrator.py      # build_retriever + build_prompt + run_qa_chain（FR-05/06/07）
│   ├── guardrail.py         # validate_metrics ±5%（FR-08）
│   └── report_generator.py  # L4 报告生成
master/app/api/v1/
├── chat.py                  # 新增：POST /chat + /chat/stream + /reports/generate
├── assets.py                # 修改：补 GET /knowledge-search
master/app/ws/
└── chat.py                  # 新增：WS /ws/chat
master/app/schemas/__init__.py  # 加 ChatRequestIn/AnswerOut/KnowledgeSearchOut
master/app/core/config.py       # 加 LLM_* + CHUNK/TOP_K/THRESHOLD/TOLERANCE
master/requirements.txt         # 加 langchain 全家桶 + rank-bm25
master/app/main.py              # 挂载 /ws/chat
```

## 四、分阶段实施步骤（5 阶段，约 11-15 天）

### Stage 0｜依赖 + 配置 + 空包（0.5-1 天）

**修改**：
- `requirements.txt`：追加 `langchain>=0.2,<0.3` / `langchain-core` / `langchain-community` / `langchain-openai` / `langchain-qdrant` / `langchain-text-splitters` / `pypdf` / `rank-bm25`
- `config.py`：新增 `llm_provider/llm_api_key/llm_base_url/llm_model/llm_temperature/llm_timeout` + `chunk_size(500)/chunk_overlap(50)/top_k(5)/similarity_threshold(0.5)/use_bm25(False)/max_context_chars(1800)/metric_tolerance(0.05)`
- `.env.example` 同步
- 新增 `services/llm/__init__.py`（空包）

**验收**：`pip install -r requirements.txt` 成功；`ruff check` 全过；现有 368 测试不受影响。

### Stage 1｜重构 FR-01/02/03/04 走 LangChain 抽象（3-4 天）

**新增文件**：
- `services/langchain_loader.py`：3 个 `BaseLoader` 子类
- `services/langchain_pipeline.py`：`build_cleaner` / `build_chunker` / `build_index_pipeline` / `index_chunks`

**重构文件**：
- `embedding_client.py`：新增 `get_embeddings()` 工厂；`EmbeddingClient.embed` 委托 LangChain `Embeddings`；降级改为 FakeEmbeddings 入库
- `vector_store.py`：新增 `get_langchain_vector_store()`，共享 collection 与 point_id 算法
- `asset_parser.py`：`dispatch_parse` 改返回 `list[Document]`；删除 `chunk_text` 改用 `RecursiveCharacterTextSplitter`；`_index_chunks` 改调 `langchain_pipeline.index_chunks`；保留 `_extract_structured` 全部子函数

**测试调整**：chunk 数精确断言改区间；新增 FakeEmbeddings 降级入库用例

**验收**：TC-ASY-001/002/003 + TC-CLN-001 + TC-CHK-001 + TC-IDX-001/002 全过；D3 原 30 用例全过

**风险**：`RecursiveCharacterTextSplitter` 中文分隔符行为；`langchain_qdrant.Qdrant` payload 字段名对齐（`content_payload_key="text_chunk"`）

### Stage 2｜FR-05/06/07/09 检索+提示词+工具+LLM+输出（3-4 天）

**新增文件**：
- `services/llm/tools.py`：8 个 `@tool`
- `services/llm/client.py`：`get_llm` + `AnswerOutput` + `PydanticOutputParser` + `_fallback_payload`
- `services/llm/orchestrator.py`：`build_retriever` + `build_prompt` + `run_qa_chain`

**测试**：`test_llm_tools.py` + `test_llm_orchestrator.py`

**验收**：TC-PRM-001/002 + TC-LLM-001/002 + TC-OUT-001 全过

### Stage 3｜FR-08 指标校验 Guardrail（1-2 天，权重最高 6 分）

**新增文件**：`services/llm/guardrail.py`

**修改文件**：`es_client.py` 补 `get_run_metrics`；`orchestrator.run_qa_chain` 接入校验

**测试**：`test_llm_guardrail.py`（TC-VLD-001/002）

**验收**：SRS 附录 A JSON 示例可复现；±5% 容差判定准确率 ≥95%

### Stage 4｜FR-10 REST + WS 入口 + knowledge-search（2-3 天）

**新增文件**：`api/v1/chat.py` + `ws/chat.py`

**修改文件**：`api/v1/assets.py` 补 knowledge-search；`api/v1/__init__.py` 注册路由；`main.py` 挂载 WS；`schemas/__init__.py` 加 ChatRequestIn/AnswerOut/KnowledgeSearchOut；`config.py` 加错误码 4000-4004

**测试**：`test_chat_api.py` + `test_chat_ws.py`

**验收**：TC-API-001/002 全过；SRS 第 6 章接口规范全部落地

### Stage 5｜L4 报告生成 + 评测 + 鲁棒性 + 埋点（2-3 天）

**新增文件**：
- `services/llm/report_generator.py`
- `data/dev_set.jsonl`（≥10 条评测样例）
- `scripts/eval_llm.py`
- `test_chat_robustness.py`

**修改文件**：`api/v1/chat.py` 实现 reports/generate；`metrics.py` 新增 4 个 LLM 指标

**验收**：SRS 第 8 章 KPI 全部达标；master 全量测试通过（基线 368 + 新增 ~50）

## 五、报告生成 Prompt 模板草案（Stage 5 评审用）

```
你是性能测试报告生成助手。基于以下压测数据生成 Markdown 格式的测试报告。

## 输入数据
- 运行编号：{run_no}
- 执行汇总（ES pt-summary）：{summary_json}
- 实时聚合（ES pt-metrics）：{realtime_json}
- JTL 关键统计（采样）：{jtl_stats}
- 场景配置：{scenario_config}

## 输出要求
生成一份结构化 Markdown 报告，包含以下章节：
1. 执行概览（运行编号、场景、时间、Agent 数、成功/失败数）
2. 性能指标（TPS、P95、错误率，与 SLA 对比）
3. 资源使用（Agent CPU/内存峰值）
4. 结论与建议（是否达标、瓶颈分析、优化建议）

## 约束
- 仅基于提供的数据，禁止编造数值
- 数值保留 2 位小数
- 如数据缺失，标注"数据不可用"而非留空
- 引用格式：[run_summary:{run_no}] / [metrics:{run_no}:{field}]
```

## 六、关键风险与对策

| 风险 | 对策 |
|---|---|
| `RecursiveCharacterTextSplitter` 中文分隔符行为与原算法差异 → chunk 数变化 | Stage 1 完成后立即跑 D3 30 用例，断言改区间；必要时调 separators 顺序 |
| `langchain_qdrant.Qdrant` 与现有 `QdrantVectorStore` payload 字段名不一致 | Stage 1 第一件事：在 `get_langchain_vector_store()` 内显式指定 `content_payload_key="text_chunk"`，验证两边互检 |
| `FakeListChatModel` 不触发 `tool_calls` → Function Calling 链路在 Mock 下走不通 | fallback 路径必须独立于 tool_calls；Stage 2 测试覆盖 Mock 路径只走 fallback |
| LangChain 0.2 接口漂移 | 锁 `langchain>=0.2,<0.3`；关键调用在 `client.py`/`orchestrator.py` 集中封装 |
| WS `/ws/chat` 与现有 `/ws/agent` ConnectionManager 耦合 | 独立 `ws/chat.py` ConnectionManager |
| D3 重构破坏 `point_id_for` 确定性 | 两边都用 `crc32(f"{asset_id}:{chunk_index}")`，Stage 1 验证互相可检索 |
| Embedding 降级从"跳过"改"Fake 入库"导致 D3 测试断言变化 | Stage 1 同步改 `test_asset_parser.py` 中 `indexed=false` 断言为 `indexed=true, degraded=true` |

## 七、里程碑对齐

| 里程碑 | 对应 SRS M 节 | 完成阶段 | 验收口径 |
|---|---|---|---|
| **M1** 资产解析→检索 | M1（FR-01~05） | Stage 0 + Stage 1 | Loader/Splitter/Embeddings/VectorStore/Retriever 全 LangChain 抽象 |
| **M2** LLM 调用+工具编排 | M2（FR-06~07） | Stage 2 | `FakeListChatModel` 端到端跑通 |
| **M3** 指标校验+输出+接口 | M3（FR-08~10） | Stage 3 + Stage 4 | Guardrail ±5% + 4 REST + 1 WS 全通 |
| **M4** 全量测试通过 | M4 | Stage 5 | dev_set 评测通过率达标 |
