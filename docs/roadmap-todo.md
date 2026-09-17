# 项目迭代 Todo 清单

> 整体路径：**方向 2（业务迭代）为主线 → 方向 3（文档资产+LLM）作为后接增值 → 方向 1a（HA）作为并行支线 → 方向 1b（微服务）挂起待驱动**
>
> 关键路径：P1.A1 → P1.A2 → P2.D1 → P2.D3 → P3.L1 → P3.L2

## 时间线总览

| 阶段 | 时间 | 是否阻塞主线 |
|------|------|--------------|
| P0 基础设施 | 0.5 周（并行） | 否 |
| P1 结构化资产 | 2-3 周 | 是 |
| P2 文档管道 | 3-4 周 | 是 |
| P3 LLM 接入 | 3-4 周 | 是 |
| P4 SUT 监控 | 2 周 | 否（可插队） |
| P5 微服务 | 挂起 | 触发条件驱动 |

---

## P0 阶段｜基础设施准备（并行支线，0.5 周）

不阻塞业务迭代，可与 P1 并行推进。

### I-1 Master 进程级 HA
- **涉及文件**：`deploy/docker-compose.yml`、`master/app/main.py`、新增 `deploy/nginx/master.conf`
- **动作**：uvicorn `--workers 4`；nginx upstream 反代
- **验收**：`docker compose up -d` 后 4 worker 都被 nginx 调度，单 worker OOM 不影响服务
- **依赖**：无
- **风险**：WS 长连接需要 nginx `ip_hash` 或独立 WS 服务（ptp-dev 已约定 WS 在 `/ws/agent`，nginx 配 sticky）
- **工作量**：0.5 天

### I-2 DB/ES/MinIO HA（基础设施，不动 Master 代码）
- **涉及文件**：新增 `deploy/docker-compose.ha.yml`（compose overlay 模式，参照 `deploy/docker-compose.monitoring.yml` 风格）
- **动作**：
  - MySQL 8 InnoDB Cluster（MGR 3 节点）或主从 + Orchestrator/ProxySQL
  - ES 集群 3 节点 + `index.number_of_replicas: 1`
  - MinIO Distributed Mode（≥4 节点）
- **验收**：单节点宕机业务接口仍可用；`ELASTICSEARCH_URL` 改为多节点列表，`AsyncElasticsearch` 已支持
- **依赖**：无
- **工作量**：3-5 天（运维侧）

### I-3 更新技术栈文档
- **涉及文件**：`docs/tech-selection.md`（确认存在；若不存在则补）
- **动作**：登记新增组件（nginx、ProxySQL、ES 集群、MinIO Distributed、未来 LLM/embedding 调用层）
- **验收**：ptp-dev 第 1 节技术栈与文档一致
- **工作量**：0.5 天

---

## P1 阶段｜结构化资产管理（主线，2-3 周）

按契约先行：schema → model → migration → service → API → test。

### A1 环境清单（Environment）模块 ✅ 已完成（2026-09-16）
- **新增文件**：
  - `master/app/models/environment.py`（ORM）
  - `master/app/api/v1/environments.py`（路由）
  - `master/alembic/versions/20260916a1_environment.py`
  - `master/tests/test_environments.py`（24 用例）
- **修改文件**：
  - `master/app/models/__init__.py`（导出）
  - `master/app/schemas/__init__.py`（EnvironmentIn/UpdateIn/Out 集中定义）
  - `master/app/api/v1/__init__.py`（注册路由）
  - `master/app/api/v1/projects.py`（删除预检加 environments 计数；force 级联补环境清理，解除 RESTRICT FK）
  - `master/tests/test_projects.py`（force 响应断言补 removed_environments）
- **域模型字段**：`id, project_id, name, env_code, base_url, hosts(JSON list), db_connections(JSON list), middleware_info(JSON list), variables(JSON dict), description`
- **关键约束**：
  - 项目内 `(project_id, env_code)` 唯一（重复错误码 3040；不存在 3041；跨项目 3042；A3 后引用阻断 3043）
  - 删除预检 + force 模式（A3 场景引用统计当前恒 0，契约前向兼容）
  - viewer+ 可查、editor+ 增改、owner+ 可删
  - 表名 `test_environment`；FK 默认 RESTRICT（与 jmeter_script 口径一致），项目 force 删除在应用层 bulk delete 环境
- **验收**：master 全量 262 测试通过、ruff 全过
- **工作量**：3-4 天

### A2 交易清单（Transaction）模块 ✅ 已完成（2026-09-16）
- **新增文件**：
  - `master/app/models/transaction.py`（ORM）
  - `master/app/api/v1/transactions.py`（路由）
  - `master/alembic/versions/20260916b1_transaction.py`
  - `master/tests/test_transactions.py`（30 用例）
- **修改文件**：
  - `master/app/models/__init__.py`（导出 Transaction）
  - `master/app/schemas/__init__.py`（TransactionIn/UpdateIn/Out 集中定义）
  - `master/app/api/v1/__init__.py`（注册路由）
  - `master/app/api/v1/projects.py`（删除预检加 transactions 计数；3023 严格阻断含交易；force 级联补交易清理，解除 RESTRICT FK）
  - `master/tests/test_projects.py`（force 响应断言补 removed_transactions；级联清理校验加 test_transaction 表）
- **域模型字段**：`id, project_id, name, txn_code, default_script_id(弱关联 FK ondelete=SET NULL), sla_tps(Float), sla_p95_ms(Int), sla_error_rate(Float), description`
- **关键约束**：
  - 项目内 `(project_id, txn_code)` 唯一（重复 3050；不存在 3051；跨项目 3052；A3/A4 引用阻断 3053；默认脚本不存在/跨项目 3054）
  - 删除预检 + force 模式（A3/A4 场景/方案引用统计当前恒 0，契约前向兼容）
  - viewer+ 可查、editor+ 增改、owner+ 可删
  - 表名 `test_transaction`；project_id FK RESTRICT（项目 force 删除在应用层 bulk delete 交易）；default_script_id 弱关联 ondelete=SET NULL（脚本删除由 DB 自动置空，不阻断）
  - SLA 指标用 Float 而非 Numeric：监控阈值非货币，跨 SQLite(REAL)/MySQL(FLOAT) 行为一致
- **验收**：master 全量 292 测试通过、ruff 全过
- **工作量**：3-4 天

### A3 Scenario 绑定 Environment ✅ 已完成（2026-09-16）
- **修改文件**：
  - `master/app/models/scenario.py`（加 `environment_id` 外键，nullable 兼容存量）
  - `master/app/schemas/__init__.py`（ScenarioIn/UpdateIn/Out 加 `environment_id`）
  - `master/app/api/v1/scenarios.py`（create/update 校验环境归属 3041/3042 + 持久化 + 响应含 environment_id）
  - `master/app/api/v1/environments.py`（删除预检激活场景引用统计 + 严格 3043 + force 解绑引用场景）
  - `master/app/services/orchestrator.py`（执行期 `_build_jmeter_args` 合并 environment.variables 与 scenario.param_overrides）
- **新增文件**：
  - `master/alembic/versions/20260916c1_scenario_environment.py`
  - `master/tests/test_scenario_environment.py`（19 用例）
- **关键约束**：
  - `environment_id` 弱关联 FK→test_environment.id，ondelete=SET NULL（nullable 兼容存量）
  - 场景 create/update 校验环境归属：不存在 3041 / 跨项目 3042（与 environments.py 两码对齐）
  - 环境删除预检返回 scenario 引用数；严格模式 >0 抛 3043，force 模式批量 UPDATE scenario.environment_id=NULL 解绑再删
  - orchestrator -J 参数优先级：scenario.param_overrides > environment.variables；未绑定环境退化为纯场景级覆盖（向后兼容）
- **验收**：master 全量 311 测试通过、ruff 全过

### A4 测试方案（TestPlan）模块（弱关联 Scenario）
- **新增文件**：`master/app/models/test_plan.py`、`test_plan_scenario.py`、对应 schema/api/migration/tests
- **域模型**：
  - `test_plans(id, project_id, name, pass_criteria(JSON), report_template, schedule_id?)`
  - `test_plan_scenarios(id, plan_id, scenario_id, seq, weight)` 弱关联表
- **关键约束**：不要强外键级联删除（保持 Scenario 可独立执行）
- **验收**：方案可挂多个场景、可定义通过判据、可一键批量起场景
- **工作量**：3-4 天

---

## P2 阶段｜文档资产管道（主线，3-4 周）

解决"用户上传一次，系统自动理解可用"的核心痛点。

### D1 资产元数据表 + 上传接口 ✅ 已完成（2026-09-16）
- **新增文件**：
  - `master/app/models/asset.py`（ORM）
  - `master/app/api/v1/assets.py`（路由）
  - `master/alembic/versions/20260916d1_asset.py`
  - `master/tests/test_assets.py`（24 用例）
- **修改文件**：
  - `master/app/models/enums.py`（加 `AssetType`/`AssetStatus` 枚举）
  - `master/app/models/__init__.py`（导出 Asset/AssetType/AssetStatus）
  - `master/app/schemas/__init__.py`（AssetIn/UpdateIn/Out 集中定义）
  - `master/app/api/v1/__init__.py`（注册 assets 路由）
  - `master/app/api/v1/projects.py`（删除预检加 assets 计数；3023 严格阻断含文档资产；force 级联补资产 bulk delete + MinIO 清理）
  - `master/tests/test_projects.py`（force 响应断言补 removed_assets；级联清理校验加 test_asset 表）
- **域模型字段**：`id, project_id, name, asset_type, status, filename, file_key, hash_sha256, file_size, content_type, description, parse_meta(JSON), created_by`
- **关键约束**：
  - `(project_id, hash_sha256)` 项目内唯一：同内容重复上传返回原 asset_id（reused=true），不重复存 MinIO
  - 资产类型 ↔ 扩展名映射：plan_doc/sla_doc/architecture_doc→docx/doc/pdf(+pptx)、env_inventory/txn_inventory→xlsx/xls，不匹配 3060
  - 上传即入库 status=PENDING（D3 异步解析管道推进 PARSING→READY/FAILED）
  - 存储 key 规范：MinIO `ptp` bucket `assets/{asset_id}/{原文件名}`
  - viewer+ 可查、editor+ 上传/更新/删除（文件类资产口径同 scripts，非 owner 才能删）
  - 表名 `test_asset`；FK→test_project 默认 RESTRICT（同 environment/transaction），项目 force 删除在应用层 bulk delete 资产
  - 错误码段 3060 起：3060 扩展名与资产类型不匹配 / 3061 资产不存在 / 3062 跨项目
- **验收**：上传 .docx/.xlsx 落地 MinIO + assets 表 PENDING；同 hash 二次上传返回原 asset_id
- **工作量**：2 天

### D2 Qdrant 向量库 + 检索 API（抽象层先行，便于后续切 Milvus）
- **选型理由**：ES `dense_vector` 大规模召回性能差且与 `pt-metrics`/`pt-summary` 争 JVM heap；专用向量库（Qdrant Rust 实现、单容器部署、async SDK 原生）更契合 ptp-dev 3.1 异步约束；同时本项目作练手用，采用主流向量库技术栈
- **新增文件**：
  - `master/app/services/vector_store.py`（VectorStore Protocol + QdrantVectorStore 实现 + `get_vector_store()` 工厂）
- **修改文件**：
  - `master/app/core/config.py` 加 `vector_provider`/`qdrant_url`/`qdrant_api_key`/`qdrant_collection`/`embedding_dims`
  - `deploy/docker-compose.yml` 加 `qdrant` 服务（单容器，无 HA 需求）
  - `deploy/docker-compose.monitoring.yml` 加 Qdrant exporter（可选）
- **抽象层设计**（迁移 Milvus 仅需新增一个实现类，业务层 0 改动）：
  ```python
  class VectorStore(Protocol):
      async def ensure_collection(self) -> None: ...
      async def upsert_chunks(self, points: list[ChunkDoc]) -> None: ...
      async def search(self, project_id: int, query_vec: list[float],
                       top_k: int, filters: dict | None = None) -> list[ChunkHit]: ...
  ```
- **迁移友好约定**（落地时必须遵守，降低未来切 Milvus 成本）：
  - chunk 主键用 **int64 自增**（Qdrant/Milvus 均原生支持；勿用 UUID，Milvus 主键推荐 int64）
  - filter 用 **Python dict 表达**，由各 Store 实现翻译为各自语法（Qdrant `FieldCondition` / Milvus `expr` 字符串）
  - 业务层（asset_parser/embedding_client/assets.py）**只依赖 VectorStore 协议**，禁止 import Qdrant SDK
  - 配置驱动切换：`VECTOR_PROVIDER=qdrant|milvus`，工厂方法分支
- **collection schema**（Qdrant payload 模型）：
  - vector：`embedding`（dims=1024，Distance.COSINE）
  - payload：`asset_id`(int)/`project_id`(int)/`asset_type`(str)/`chunk_index`(int)/`text_chunk`(str)/`source_type`(str)/`source_ref`(str)/`created_at`(datetime)
- **新增 API**：`GET /api/v1/assets/knowledge-search?project_id=&q=&top_k=`（Qdrant filter + search 召回）
- **ES 职责不变**：`pt-summary`/`pt-metrics` 仍由 [es_client.py](file:///d:/PycharmProjects/LoaderCloudBackendV2/master/app/services/es_client.py) 承担，不再扩展向量职责
- **验收**：collection 创建 + 检索返回 chunks 列表；`VECTOR_PROVIDER=qdrant` 可正常切换
- **依赖**：D1 完成
- **工作量**：2 天（含抽象层设计）

### D3 文档解析管道 ✅ 已完成（2026-09-16）
- **新增文件**：
  - `master/app/services/asset_parser.py`（解析分发/切片/列映射/结构化抽取/状态机）
  - `master/app/services/embedding_client.py`（D4 一并落地，见下）
  - `master/tests/test_asset_parser.py`（18 单测）
  - `master/tests/test_asset_parse_pipeline.py`（12 集成测）
- **修改文件**：
  - `master/requirements.txt`（python-docx/openpyxl/pdfplumber）
  - `master/app/core/config.py`（EMBEDDING_PROVIDER/BASE_URL/API_KEY/MODEL/BATCH_SIZE/TIMEOUT/MAX_RETRIES）
  - `master/app/services/scheduler.py`（新增通用 `enqueue_date_job` 一次性延迟任务入口）
  - `master/app/api/v1/assets.py`（上传后 `schedule_asset_parse` 投递 + POST retry-parse 端点）
- **关键设计**：
  - 全部解析 `asyncio.to_thread` 包裹；docx→python-docx / xlsx→openpyxl / pdf→pdfplumber；.doc/.xls/.pptx 旧格式 → FAILED 并提示另存
  - 双路输出：(a) 文本切片（500 字上限、overlap 50，段落贪心聚合+超长硬切）→ embedding → Qdrant（经 VectorStore 协议）；(b) 表格行 → 列映射（ENV/TXN_INVENTORY_COLUMN_MAP 顶部常量，归一化表头匹配）→ environments/transactions 表
  - 宽松取值：列表字段（JSON 数组/逗号分号顿号换行切分）、variables（JSON dict/k=v 键值对）、数值（容忍 %/千分位/单位后缀 120ms）
  - 表头识别 = 首个命中 ≥2 映射列的行；未匹配表头记 `parse_meta.unmatched_columns`（供 D5 remap）
  - 去重三层：项目内已有 env_code/txn_code 跳过、文件内重复跳过、缺编码行跳过，全部记 warnings（parse_meta，上限 50 条）
  - 交易默认脚本按名称关联项目内 jmeter_script，未命中记警告置 NULL
  - 状态机 PENDING→PARSING→READY/FAILED 用 Core update() CAS（`status != parsing` 才置 parsing）幂等防重入，规避异步会话身份映射陷阱
  - 未配置 Embedding 降级：结构化抽取照常，仅跳过向量入库（indexed=false）
  - point_id = crc32(f"{asset_id}:{chunk_index}") 确定性 int64，重解析幂等覆盖（切片变少时旧向量残留，VectorStore 协议待扩展 delete_by_filter）
  - 向量 payload：source_type="asset"、source_ref=file_key、asset_type=类型值
- **验收**：上传环境交付清单.xlsx 解析后 environments 表自动新增行（含 hosts/variables 宽松解析）；上传 .docx 方案 chunks 入向量库（mock embedding+store）；失败/旧格式/重试全链路 30 用例覆盖
- **依赖**：D1 + D2 + A1 + A2 ✅
- **工作量**：4-5 天

### D4 Embedding 调用层 ✅ 已完成（2026-09-16，随 D3 一并落地）
- **新增文件**：`master/app/services/embedding_client.py`
- **关键约束**：
  - httpx.AsyncClient（ptp-dev 3.1）；单请求最多 64 段（EMBEDDING_BATCH_SIZE）；指数退避重试 3 次（EMBEDDING_MAX_RETRIES），耗尽抛 EmbeddingError → 资产 FAILED
  - OpenAI 兼容 /embeddings 协议：EMBEDDING_BASE_URL 显式配置 > provider 默认值（zhipu→open.bigmodel.cn/api/paas/v4、dashscope→compatible-mode/v1）
  - 返回按 index 排序保证顺序一致；段数不符直接报错
- **验收**：mock 客户端验证批量切片一次调用成功、写入向量库可召回路径；真实 provider 联调待配置 API key（智谱 embedding-3 / 阿里 text-embedding-v3，1024 维）
- **依赖**：D2 ✅
- **工作量**：2 天

### D5 列映射修正接口（兜底，避免硬编码失败）
- **新增 API**：
  - `GET /api/v1/assets/{id}/parse-warnings`（返回未匹配列名）
  - `POST /api/v1/assets/{id}/remap`（用户提交列名→字段映射，触发重新结构化抽取）
- **设计**：用户在前端一键修正，仍无需手工录入数据
- **验收**：上传表格列名不规范时，前端能修正后一键生效
- **依赖**：D3
- **工作量**：1 天

---

## P3 阶段｜LLM 编排接入（主线，3-4 周）

### L1 LLM 调用层 + 工具注册框架
- **新增文件**：
  - `master/app/services/llm/client.py`（LLM 调用，httpx async）
  - `master/app/services/llm/tools.py`（Function 工具定义）
  - `master/app/services/llm/orchestrator.py`（多轮编排）
- **修改文件**：`master/app/core/config.py` 加 `LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`
- **工具集**（每个工具对应一个 REST 接口的封装）：
  - `query_environments(project_id, name?)`
  - `query_transactions(project_id, code?)`
  - `get_scenario(scenario_id)` / `create_scenario(...)`
  - `get_run_summary(run_no)` / `get_realtime_summary(run_no)`
  - `search_knowledge(project_id, query, top_k)`（RAG 召回）
- **关键约束**：禁止同步阻塞；超时 30s；失败回退到提示词
- **工作量**：3-4 天

### L2 对话式场景操作
- **新增文件**：`master/app/api/v1/chat.py`、`master/app/ws/chat.py`（前端流式响应）
- **场景示例**：用户"把生产环境登录交易压到 500tps" → 工具链 query_environments → query_transactions → create_scenario → create_run
- **依赖**：L1 + A1 + A2 + A3
- **工作量**：3 天

### L3 监控采集分析归因
- **修改文件**：扩展 L1 工具集，加：
  - `query_metrics(run_no, agg)` → 复用 `master/app/metrics.py` 已埋点
  - `get_slow_queries(window)`
  - `get_realtime_summary` 已存在
- **RAG 归因**：异常时从 Qdrant 召回 `source_type=domain_doc`（如 `docs/perf-monitoring-and-login-bottleneck.md`）做相似故障对照
- **依赖**：L1 + D2 + 现有 metrics 埋点
- **工作量**：3 天

### L4 测试报告自动生成
- **新增文件**：`master/app/services/report_generator.py`
- **流程**：
  1. Function Calling 拉 run summary + metrics 聚合 + MinIO JTL/HTML 抽取关键统计
  2. LLM 生成 Markdown 报告
  3. 写回 MinIO `reports/{run_no}/llm-report.md`
  4. 切 chunk + embedding → Qdrant（source_type=report，供下次相似报告召回）
- **依赖**：L1 + D2 + D4
- **工作量**：3-4 天

---

## P4 阶段｜被测服务监控接入（2 周）

### M1 Environment 挂载 SUT exporter 配置
- **修改文件**：`master/app/models/environment.py` 加 `sut_exporters(JSON)` 字段
- **设计**：环境清单中配置 SUT 的 Prometheus endpoint 列表
- **依赖**：A1
- **工作量**：1 天

### M2 Agent 端 SUT 指标采集
- **修改文件**：
  - `agent/pt_agent/collector.py`：扩展，除 psutil 自身资源，按环境配置拉 SUT exporter
  - `agent/pt_agent/executor.py`：执行期下发 SUT 配置
- **协议同步**：`master/app/ws/protocol.py` + `agent/pt_agent/protocol.py` 同分支同提交（ptp-dev 4 + 工作流约定）
- **验收**：执行压测时 ES 出现 SUT 维度的 metric 文档
- **依赖**：M1
- **工作量**：4-5 天

### M3 Grafana 面板扩展
- **动作**：复用 `deploy/docker-compose.monitoring.yml` 监控栈，新增 SUT 维度面板
- **工作量**：2 天

---

## P5 阶段｜微服务拆分（挂起，待容量驱动）

**触发条件**：
- LLM 编排层（`master/app/services/llm/`）单独成为容量热点
- Agent WS 长连接服务（`master/app/ws/`）连接数突破单 worker 上限
- ES 写入（`master/app/services/es_client.py`）成为指标链路瓶颈
- 向量检索（`master/app/services/vector_store.py`）成为 RAG 链路瓶颈

**优先拆分顺序**：
1. 指标域（ES Client + 独立部署）—— 容量压力最先显现
2. Agent 接入域（WS 网关独立）—— 长连接资源占用
3. 其它保持单体（Scenario/Script/Asset 等 CRUD 不拆）

**前置工作**：拆分前先把 bounded context 在 P1-P3 中通过完整业务模块自然显露，避免过早抽象

---

## 跨阶段横切关注点

### X1 工程约定沉淀
- 每完成一个模块，把踩坑点写入 `c:\Users\Administrator\.trae-cn\memory\projects\-d-PycharmProjects-LoaderCloudBackendV2--p2-24f9077ff59d9bf8ab3e\project_memory.md`
- 涉及协议字段改动，同步 `master/app/ws/protocol.py` 与 `agent/pt_agent/protocol.py` 两端
- 每个新错误码段统一管理（建议在 `enums.py` 同文件加 `ErrorCode` 枚举）

### X2 测试覆盖率
- 每个新模块契约测试先行（ptp-dev 6.3）
- 异步会话陷阱三连经验（memory 已记录）必须复测：
  - `AsyncSession.delete` 是协程，未 await 静默不删
  - `expire_on_commit=False` 下重读实体会命中身份映射旧对象，关系集合需 clear()/append()
  - 异步会话懒加载抛 MissingGreenlet，遍历关系需显式 selectinload

### X3 配置规范
- 所有新配置项走 `master/app/core/config.py` + `.env.example`
- 禁止硬编码 LLM/Embedding API key（ptp-dev 3.3）
- 向量库相关配置统一走 `VECTOR_PROVIDER`/`QDRANT_URL`/`EMBEDDING_DIMS` 等环境变量；业务层禁止 import 具体 SDK，只依赖 `VectorStore` 协议（D2 抽象层约定）
