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

### A3 Scenario 绑定 Environment
- **修改文件**：
  - `master/app/models/scenario.py`（加 `environment_id` 外键，nullable 兼容存量）
  - `master/app/schemas/`（scenario 相关 schema 加 `environment_id`）
  - `master/app/services/orchestrator.py`（执行期把 environment 的 hosts/variables 注入 JMX `-J` 参数）
- **迁移**：新增列即可，向后兼容
- **验收**：场景绑定环境后执行，JMeter 日志能看到 `-Jbase_url=...` 等参数注入
- **工作量**：1-2 天

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

### D1 资产元数据表 + 上传接口（契约先行）
- **新增文件**：
  - `master/app/models/asset.py`（ORM，含 `asset_type`/`status` 枚举）
  - `master/app/schemas/asset.py`
  - `master/app/api/v1/assets.py`（POST 上传 / GET 列表 / GET 详情 / DELETE）
  - `master/alembic/versions/xxx_add_assets.py`
  - `master/tests/test_assets.py`
- **修改文件**：`master/app/models/enums.py` 加 `AssetType`(plan_doc/env_inventory/txn_inventory/sla_doc/architecture_doc)、`AssetStatus`(PENDING/PARSING/READY/FAILED)
- **存储 key 规范**：MinIO `ptp` bucket `assets/{asset_id}/{原文件名}`（参照 ptp-dev 4.6 产物路径约定）
- **去重**：`hash_sha256` 索引，同项目内重复上传直接复用
- **验收**：上传 .docx/.xlsx 落地 MinIO + assets 表 PENDING；同 hash 二次上传返回原 asset_id
- **工作量**：2 天

### D2 ES `pt-knowledge` 索引 + 向量检索 API
- **修改文件**：`master/app/services/es_client.py` 加 `_KNOWLEDGE_MAPPING` + `ensure_knowledge_index()` + `index_knowledge_chunks()` + `search_knowledge()`
- **mapping 草案**：
  ```
  asset_id(keyword), project_id(keyword), asset_type(keyword),
  chunk_index(int), text_chunk(text), source_type(keyword),
  source_ref(keyword), embedding(dense_vector, dims=1024,
  index=true, similarity=cosine), created_at(date)
  ```
- **新增 API**：`GET /api/v1/assets/knowledge-search?project_id=&q=&top_k=`（kNN 召回）
- **验收**：索引创建 + kNN 查询返回 chunks 列表
- **依赖**：D1 完成
- **工作量**：1.5 天

### D3 文档解析管道（核心，避免人工录入）
- **新增文件**：`master/app/services/asset_parser.py`
- **依赖库**（先确认 pyproject.toml 中无则新增）：`python-docx`、`openpyxl`、`pdfplumber`
- **关键设计**：
  - 全部解析逻辑用 `asyncio.to_thread` 包裹（ptp-dev 3.1 强约束）
  - 按 MIME/扩展名分发：docx→python-docx、xlsx→openpyxl、pdf→pdfplumber
  - 双路输出：
    - (a) 文本切片（300-500 字/段，带 overlap 50 字）→ embedding → ES `pt-knowledge`
    - (b) 表格行 → 按列映射规则 → MySQL environments/transactions 表
- **列映射规则配置**（写在 `asset_parser.py` 顶部常量）：
  ```python
  ENV_INVENTORY_COLUMN_MAP = {"环境名称": "name", "基础URL": "base_url", ...}
  TXN_INVENTORY_COLUMN_MAP = {"交易码": "txn_code", "交易名称": "name", ...}
  ```
- **集成 APScheduler**：上传后投递解析任务，避免阻塞上传响应
- **状态机**：PENDING → PARSING → READY / FAILED；FAILED 可前端一键重试
- **验收**：上传环境交付清单.xlsx，几秒后 `environments` 表自动新增对应行；上传性能测试方案.docx，`pt-knowledge` 出现 chunks
- **依赖**：D1 + D2 + A1 + A2（结构化抽取依赖环境/交易表已存在）
- **工作量**：4-5 天

### D4 Embedding 调用层（异步、批量、可重试）
- **新增文件**：`master/app/services/embedding_client.py`
- **修改文件**：`master/app/core/config.py` 加 `EMBEDDING_PROVIDER`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL`/`EMBEDDING_DIMS`
- **关键约束**：
  - 必须用 `httpx.AsyncClient`（ptp-dev 3.1），禁止同步阻塞
  - 批量化：单请求最多 64 段
  - 失败指数退避，3 次失败标记 asset FAILED
- **Provider 起步建议**：智谱 `embedding-3` 或阿里 `text-embedding-v3`（1024 维，国内网络可达）
- **本地化备选**（中长期）：bge-large-zh-v1.5 ONNX，部署在 Agent 侧或独立 micro service
- **验收**：64 段文本调用一次成功，写入 ES 后 kNN 查询能召回
- **依赖**：D2
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
- **RAG 归因**：异常时从 `pt-knowledge` 召回 `source_type=domain_doc`（如 `docs/perf-monitoring-and-login-bottleneck.md`）做相似故障对照
- **依赖**：L1 + D2 + 现有 metrics 埋点
- **工作量**：3 天

### L4 测试报告自动生成
- **新增文件**：`master/app/services/report_generator.py`
- **流程**：
  1. Function Calling 拉 run summary + metrics 聚合 + MinIO JTL/HTML 抽取关键统计
  2. LLM 生成 Markdown 报告
  3. 写回 MinIO `reports/{run_no}/llm-report.md`
  4. 切 chunk + embedding → `pt-knowledge`（source_type=report，供下次相似报告召回）
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
