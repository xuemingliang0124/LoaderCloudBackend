# JMeter 分布式性能测试平台 — 技术选型方案

> 版本：v2.1（Master-Agent 分布式 + LLM/RAG 智能层） 日期：2026-09-22

## 1. 需求范围

- **JMeter 脚本管理**：JMX 上传、版本、参数（占位符）定义、数据文件（csv/txt/dat/tsv）
- **测试场景管理**：脚本 + 参数覆盖 + 压力机分组/线程拆分、四类场景模型、环境绑定
- **场景执行记录管理**：执行历史、实时曲线（WebSocket 推送）、汇总报告、产物归档
- **定时场景管理**：cron 定时触发、启停控制
- **分布式压测**：多压力机 Agent 化管理（注册/心跳/状态/调度/执行）
- **结果存储**：指标存 Elasticsearch，支持聚合查询；原始产物存 MinIO
- **结构化资产管理**（P1 新增）：环境清单、交易清单、文档资产（方案/SLA/架构）上传与解析
- **LLM 智能层**（P3 新增）：RAG 检索增强问答、Function Calling 工具编排、测试报告自动生成

## 2. 总体架构

```
                    Vue3 前端 (Element Plus + ECharts)
                              │ REST / WS
                              ▼
        ┌─────────────── FastAPI Master（控制面 + 智能面）───────────────┐
        │  脚本/场景/执行记录/定时任务 管理                                │
        │  环境/交易/文档资产 管理 + 解析管道（docx/xlsx/pdf → 结构化 + 向量）│
        │  Agent 注册中心：心跳超时判定、分组、状态机                       │
        │  编排器：选压力机 → 下发任务 → 汇聚结果 → 归档                    │
        │  ES Writer：实时指标 bulk 写入 + 聚合查询 API                    │
        │  LLM 编排层：Retriever + Prompt + Tool + ChatModel + Guardrail  │
        └──┬──────────┬───────────────┬───────────────┬───────────────┘
           │ WS 控制   │ HTTP 文件      │ HTTP 指标     │ HTTP 向量/LLM
           ▼           ▼               ▼               ▼
   ┌── Agent 压力机 ──┐  MinIO     Elasticsearch 8.x   Qdrant (向量库)
   │ psutil 状态采集    │  (脚本/参数/  (pt-metrics-*    (pt_knowledge
   │ JMeter 子进程执行  │   JTL/报告    pt-summary)      语义检索)
   │ 5s 粒度指标上报    │   归档)
   └──────────────────┘
```

核心原则：
- **执行从平台进程剥离到 Agent**，Master 只做编排和数据面；MySQL 只存元数据，指标全部进 ES，语义向量进 Qdrant。
- **LLM/RAG 全链路基于 LangChain 抽象**（Loader/Splitter/Embeddings/VectorStore/Retriever/Prompt/Tool/ChatModel/OutputParser），业务层禁止 import 具体 SDK。
- **降级优先（NFR-01）**：未配置 Embedding/LLM API Key 时，分别用 `FakeEmbeddings` 入库、`FakeListChatModel` 返回兜底 JSON，保证端到端链路不中断。

## 3. 技术选型清单

| 层次 | 选型 | 版本基线 | 理由 |
|---|---|---|---|
| Master 框架 | FastAPI + Pydantic v2 + uvicorn | FastAPI ≥0.115 | 自带 OpenAPI 文档；类型化代码对 AI 生成友好 |
| ORM/迁移 | SQLAlchemy 2.0 (async) + Alembic | 2.0 | 主流标配，async 契合事件循环 |
| 数据库 | MySQL 8.0 | 8.0 | 元数据存储（脚本/场景/记录/Agent/定时/环境/交易/资产），团队熟悉度高 |
| 任务调度 | APScheduler (AsyncIOScheduler + SQLAlchemyJobStore) | 3.10+ | 定时触发仅是"下发任务"毫秒级动作，无需 Celery；misfire/coalesce 内建 |
| Agent | Python 3.12 异步：websockets + httpx + psutil | 3.12 | 与 Master 同栈，一套语言维护；打包为 Docker 镜像部署 |
| 通信 | WebSocket 控制通道（心跳/任务/指令/指标/对话）+ HTTP（文件下载/产物上传/REST） | — | Agent 主动外连 Master，压力机无需开端口，可穿透 NAT |
| 指标存储 | Elasticsearch 8.x + elasticsearch-py (Async) | 8.x | date_histogram/terms 聚合查询曲线；ILM 管理生命周期；单节点起步 |
| 向量存储 | Qdrant + qdrant-client (Async) + langchain-qdrant | 1.11+ | 承担 pt-knowledge 语义检索；Rust 单容器 + async SDK 原生；ES dense_vector 大规模召回性能差且与指标争 JVM heap，故剥离 |
| 对象存储 | MinIO (miniopy-async) | 最新稳定版 | JMX/CSV/JTL/HTML 报告/文档资产统一归档，S3 协议可换云存储 |
| 文档解析 | python-docx + openpyxl + pdfplumber + pypdf | — | docx→python-docx / xlsx→openpyxl / pdf→pdfplumber；旧格式 .doc/.xls/.pptx 不支持，提示另存 |
| RAG 框架 | LangChain 1.x（core/community/openai/qdrant/text-splitters/classic） | 1.x | SRS 强约束：Loader/Splitter/Embeddings/VectorStore/Retriever/Prompt/Tool/ChatModel/OutputParser 全链路抽象；锁 1.x 避免接口漂移 |
| BM25 混检 | rank-bm25 + langchain-classic EnsembleRetriever | 0.2+ | USE_BM25=true 时启用 BM25Retriever + 向量 Retriever 混检，提升关键词命中召回；默认关闭 |
| Embedding | OpenAI 兼容 /embeddings（智谱 embedding-3 / 阿里 text-embedding-v3，1024 维） | — | OpenAIEmbeddings 自带重试；未配置 → FakeEmbeddings 降级入库 |
| LLM | OpenAI 兼容 /chat/completions（智谱 GLM / 阿里 Qwen） | — | Function Calling 工具编排；未配置 → FakeListChatModel 兜底 JSON |
| 鉴权 | JWT (python-jose) + bcrypt 直连（passlib 已停维护，与 bcrypt 4.1+ 不兼容，不使用） | — | MVP 简单；RBAC（Casbin）留扩展点 |
| 前端 | Vue 3 + Vite + TS + Element Plus + Pinia + ECharts | Vue 3.4+ | 国内生态全，覆盖管理后台与图表场景 |
| 可观测 | prometheus-client（HTTP/ES/LLM/DB 池/Agent 指标） + /metrics 端点 | 0.20+ | 轻量无侵入；中间件拦截 HTTP 路径模板；自定义 Collector 回调运行时状态 |
| 测试 | pytest + pytest-asyncio + httpx (AsyncClient) + aiosqlite | — | API 契约测试先行；510+ 用例 |
| 部署 | Docker Compose（mysql / minio / elasticsearch / kibana / qdrant / master；agent 独立 compose） | — | 一键起全套；后期量大迁 K8s |
| 日志 | loguru | — | 简洁、文件轮转开箱即用 |
| 代码质量 | ruff（lint + format） | — | 统一风格，AI 生成代码的一致性抓手 |

**为何不用 Celery**：执行负载已由 Agent 承担，服务端定时任务只是触发一个轻量 async 编排函数，引入 Celery+Redis 属于过度设计。若 P3 增加重量级离线后处理（如大规模 JTL 分析），可局部引入。

**为何向量库选 Qdrant 而非 ES dense_vector**：ES `dense_vector` 大规模召回性能差且与 `pt-metrics`/`pt-summary` 争 JVM heap；Qdrant 为 Rust 实现、单容器部署、async SDK 原生，契合异步约束。通过 `VectorStore` Protocol 抽象，未来切 Milvus 仅需新增实现类，业务层 0 改动。

## 4. 通信协议（Master ↔ Agent）

统一信封：`{"type": "<消息类型>", "data": {...}, "ts": <unix秒>}`

### 4.1 Agent → Master

| type | data | 说明 |
|---|---|---|
| `register` | agent_id, ip, hostname, tags, jmeter_version | 连接建立后首条 |
| `heartbeat` | cpu, mem, net_in, net_out, status, current_run_id | 每 10s；Master 连续 3 次未收到判 OFFLINE |
| `task_ack` | run_id, accepted, message | 任务确认/拒绝 |
| `status` | run_id, phase(downloading/running/uploading/finished/failed/stopped), message | 生命周期上报 |
| `metrics` | run_id, interval_tps, avg_rt, p95_rt, err_rate, threads, by_label[{label,sample_type,...}] | 每 5s 一批，Master bulk 写 ES；sample_type=request\|transaction（事务行按 JMeter 官方标记行级判定，全局口径只计 request） |
| `result` | run_id, summary{...,by_label[{label,sample_type,...}]}, artifacts[] | 最终汇总 + MinIO 产物 key |

### 4.2 Master → Agent

| type | data | 说明 |
|---|---|---|
| `task` | run_id, files[], jmeter_args(-J 覆盖), start_at(对齐起压时间戳) | 下发任务 |
| `stop` | run_id | 停止执行，Agent 杀进程树 |
| `ping` / `pong` | — | 保活 |

幂等约定：Agent 以 run_id 去重；WS 断线指数退避重连，重连后重新 register + 上报当前状态。

## 5. 存储设计

### 5.1 Elasticsearch 索引

| 索引 | 内容 | 用途 |
|---|---|---|
| `pt-metrics-yyyy.MM.dd` | 5s 粒度时序文档（run_id、agent_id、label、sample_type 维度） | 实时曲线：date_histogram + terms(label, sample_type) |
| `pt-summary` | 执行汇总：run_no（keyword）、总请求/错误/P50/P90/P95/P99/TPS，多 Agent 按 label 合并去重 | 执行详情页、报告导出 |
| `pt-agent-logs-yyyy.MM.dd` | Agent/JMeter 关键日志（可选） | 失败排查 |

原始 JTL 存 MinIO 不进 ES；ILM：30 天热 → 90 天删除。**ES 不再承担 dense_vector 职责**，语义向量全部进 Qdrant。

### 5.2 Qdrant Collection（pt_knowledge）

| 字段 | 类型 | 说明 |
|---|---|---|
| vector `embedding` | float[1024] | COSINE 距离，HNSW 索引 |
| payload `asset_id` | int | 来源资产 ID |
| payload `project_id` | int | 项目 ID（强制过滤，多租户隔离） |
| payload `asset_type` | str | plan_doc/sla_doc/architecture_doc/env_inventory/txn_inventory |
| payload `chunk_index` | int | 切片序号 |
| payload `text_chunk` | str | 文本内容（content_payload_key） |
| payload `source_type` | str | asset/report（报告生成后回写） |
| payload `source_ref` | str | MinIO file_key |
| payload `created_at` | datetime | 入库时间 |

point_id = `crc32(f"{asset_id}:{chunk_index}")` 确定性 int64，重解析幂等覆盖。

### 5.3 MinIO 对象

bucket `ptp`，key 规范：
- `scripts/{script_id}/{version}/...` — JMX 与数据文件
- `plugins/{plugin_id}/...` — 全局插件 jar
- `runs/{run_no}/...` — JTL/HTML 报告等执行产物
- `assets/{asset_id}/{filename}` — 文档资产原文
- `reports/{run_no}/llm-report.md` — LLM 生成的测试报告

## 6. 数据模型（MySQL）

```
user                用户、密码哈希、全局角色（admin/user）
project             项目（名称唯一）
project_member      项目成员（owner/editor/viewer），项目级双层权限
agent_node          agent_id、ip、hostname、tags、jmeter_version、status(online/busy/offline)、
                    cpu/mem、current_run_id、last_heartbeat
jmeter_plugin       全局插件池（sha256 去重）
agent_plugin        Agent 实际安装的插件关联
jmeter_script       脚本名、版本、minio key、占位符参数定义(JSON)、描述、数据文件
test_environment    环境清单：name、env_code(项目内唯一)、base_url、hosts(JSON)、
                    db_connections(JSON)、middleware_info(JSON)、variables(JSON)、description
test_transaction    交易清单：name、txn_code(项目内唯一)、default_script_id(弱关联 SET NULL)、
                    sla_tps(Float)、sla_p95_ms(Int)、sla_error_rate(Float)、description
test_scenario       场景名、script_id、environment_id(弱关联 SET NULL)、参数覆盖(JSON)、
                    agent 分组/标签、线程数/时长、场景类型(四枚举)、描述
scenario_script     场景-脚本关联（顺序、agent_tags OR 选机、agent_count）
scenario_script_tg  场景-脚本-线程组级参数（enabled/num_threads/ramp_time/tps）
scenario_run        run_no、scenario_id、status(pending/running/stopping/finished/partial/failed/stopped)、
                    trigger(manual/scheduled)、agent 快照、起止时间、summary 索引引用
run_agent_result    执行结果分片持久化（Master 重启恢复汇聚现场）
schedule_job        名称、scenario_id、cron、enabled、next_run_time、last_run_id
test_asset          文档资产：name、asset_type、status(pending/parsing/ready/failed)、
                    filename、file_key、hash_sha256(项目内唯一)、file_size、content_type、
                    parse_meta(JSON)、created_by
```

## 7. 关键执行流程

### 7.1 压测执行

```
创建执行 → 选 N 台 IDLE Agent（按分组）→ WS 下发 task（文件清单 + -J 参数 + start_at）
→ Agent 下载脚本 → 上报 running → 同步等 start_at 齐发
→ 运行中每 5s 上报 metrics → Master bulk 写 ES → 前端 WS 订阅实时曲线
→ 结束：上传 JTL + HTML 报告到 MinIO，上报 result
→ Master 全部收齐后合并汇总写 pt-summary，置 FINISHED（任一失败置 PARTIAL）
停止：Master → WS stop → Agent 杀进程树 → 上报 stopped（看门狗 STOP_WAIT_TIMEOUT 兜底）
```

JMX 参数化：脚本内使用 `${__P(threads,10)}` 占位符，场景存 key-value 覆盖，运行时拼 `-Jthreads=200`；环境 `variables` 与场景 `param_overrides` 合并注入（场景优先级 > 环境）。

### 7.2 文档资产解析管道（D1+D3）

```
上传 docx/xlsx/pdf → 落 test_asset(status=PENDING) + MinIO
→ APScheduler 一次性延迟任务调度 asset_parser
→ PARSING（CAS 幂等防重入）
→ 双路输出：
   (a) 文本切片：LangChain Cleaner → RecursiveCharacterTextSplitter(500/50)
       → Embedding（已配置 OpenAIEmbeddings / 未配置 FakeEmbeddings）
       → Qdrant upsert（point_id 确定性）
   (b) 表格行：列映射（ENV/TXN_INVENTORY_COLUMN_MAP 归一化表头匹配）
       → environments / transactions 表（项目内去重、缺编码跳过、警告记 parse_meta）
→ READY / FAILED（失败支持 retry-parse 重试）
```

### 7.3 LLM 问答编排（P3）

```
用户提问（REST /chat、/chat/stream SSE、WS /ws/chat）
→ 项目 viewer+ 门禁 + run_no 可见性校验
→ build_retriever（Qdrant as_retriever，project_id 过滤；USE_BM25 时 EnsembleRetriever）
→ create_retriever_tool(search_knowledge) + 7 个静态 Function Calling 工具
   （query_environments/query_transactions/get_scenario/create_scenario/
    get_run_summary/get_realtime_summary/query_metrics）
→ LangChain agent 编排 → ChatModel（已配置 / 未配置 FakeListChatModel 兜底）
→ PydanticOutputParser（AnswerOutput：answer/citations/sources/notes/confidence）
→ Guardrail 指标校验（|llm - actual| / actual > 0.05 视为不一致）
→ 返回五字段 + Prometheus 埋点（llm_call_duration/llm_calls_total/llm_retrieval_score/llm_guardrail_result）
```

### 7.4 测试报告自动生成（L4）

```
POST /reports/generate → 拉 run summary + ES metrics 聚合 + MinIO JTL/HTML
→ LLM 生成 Markdown 报告（降级时用模板生成，confidence=0.5）
→ 写回 MinIO reports/{run_no}/llm-report.md
→ 切 chunk + embedding → Qdrant（source_type=report，供下次相似报告召回）
```

## 8. 分期路线

- **P1 (MVP)**：Agent 上线、手动下发单/多机执行、实时指标入 ES + 曲线、执行记录 + HTML 报告归档、环境清单、交易清单、场景绑定环境
- **P2**：定时场景、同步起压、强制停止、Agent 分组调度与线程拆分、异常恢复、文档资产上传与解析管道、Qdrant 向量库、Embedding 调用层
- **P3**：LLM 调用层 + 工具注册框架、对话式场景操作（REST + WS）、RAG 检索增强、指标校验 Guardrail、测试报告自动生成、Prometheus LLM 指标、评测集与 eval 脚本
- **P4**（待启动）：SUT 监控接入（Environment 挂 exporter、Agent 采集、Grafana 面板）
- **P5**（挂起）：微服务拆分（指标域 / Agent WS 网关优先）

## 9. 部署形态

- 平台侧 docker-compose：mysql、minio、elasticsearch、kibana、qdrant、master
- 压力机侧独立 compose / 预构建镜像：agent 容器内置 OpenJDK + JMeter 5.6.x
- 环境变量统一 `.env` + pydantic-settings 管理（12-factor）
- 容器部署自动迁移：master 入口 `entrypoint.sh` 在 uvicorn 启动前执行 `alembic upgrade head`
