---
name: "ptp-dev"
description: "Defines architecture, tech stack, directory layout, coding and API conventions for the JMeter distributed performance testing platform (FastAPI Master + Python Agent). Invoke before writing, refactoring or reviewing any code in this repo."
---

# JMeter 分布式压测平台 — 开发规范

本 skill 是本仓库的唯一开发规范来源。**在此仓库写任何代码前必须先阅读本文档**，保证架构与代码风格一致。

## 1. 项目定位与技术栈（不可随意更换）

- **Master（控制面）**：`master/` — FastAPI + SQLAlchemy 2.0 async + MySQL 8 + APScheduler + elasticsearch-py(async) + miniopy-async
- **Agent（压力机端）**：`agent/` — Python 3.12 异步：websockets + httpx + psutil
- **存储**：MySQL 只存元数据；指标全部进 ES（时序 `pt-metrics-*`、汇总 `pt-summary-{run_id}`）；文件产物全进 MinIO
- **通信**：Agent 主动连 Master 的 WebSocket 控制通道（心跳/任务/指令/指标），HTTP 用于文件下载与产物上传
- **调度**：APScheduler（AsyncIOScheduler + SQLAlchemyJobStore），定时任务只做"触发下发"，不承载执行
- 前端（Vue3）另行建仓；后端只提供 REST + WS

改动以上任一技术栈必须先更新 `docs/tech-selection.md` 并与需求方确认。

## 2. 目录结构规范

```
master/
  app/
    main.py            # 入口：lifespan（DB/调度器/ES 初始化）、路由挂载、WS 端点
    core/              # config.py(pydantic-settings) / log.py(loguru) / security.py(JWT)
    db/                # session.py（async engine/session）
    models/            # SQLAlchemy ORM，一表一文件，models/__init__.py 汇总导出
    schemas/           # Pydantic 请求/响应模型，一资源一文件
    api/               # 路由层：api/v1/ 一资源一文件；deps.py 公共依赖
    services/          # 业务逻辑：orchestrator(编排) / agent_registry(注册中心)
                       #   es_client / storage(MinIO) / scheduler(APScheduler)
    ws/                # agent 连接管理器与消息分发
  alembic/             # 迁移，versions/ 一变更一文件
  tests/               # pytest + httpx
agent/
  pt_agent/
    main.py            # 入口
    config.py          # pydantic-settings
    protocol.py        # 与 master/app/ws/protocol.py 保持字段一致（信封 {type,data,ts}）
    state.py           # Agent 状态机
    collector.py       # psutil 采集
    runner.py          # JMeter 子进程管理（启动/停止/进程树清理）
    executor.py        # 任务生命周期：下载→执行→上传→上报
    reporter.py        # WS 客户端（重连）+ 心跳 + HTTP 上传
deploy/                # docker-compose 平台侧/压力机侧
docs/                  # tech-selection.md 等
```

规则：
- **分层调用方向**：`api → services → models/db`，禁止 api 直接操作 db session 做业务，禁止 services 反向 import api
- WS 消息与协议字段改动必须同步 `master/app/ws/protocol.py` 与 `agent/pt_agent/protocol.py`

## 3. 代码规范

### 3.1 Python 风格

- Python 3.12；全部使用类型注解（函数签名、模型字段必须注解）
- 格式化与 lint：`ruff format` + `ruff check`（line-length 100，规则集 `E,F,I,UP,B`）
- 命名：模块/包 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE`，私有 `_leading_underscore`
- I/O 一律 `async/await`（SQLAlchemy async、httpx.AsyncClient、AsyncElasticsearch）；**禁止在事件循环内做阻塞调用**——子进程管理、重 CPU 用 `asyncio.to_thread` 或线程
- 注释/Docstring 用中文，只在逻辑不自明处写；禁止提交 `# TODO` 之外的调试残渣（print、注释掉的代码块）
- 日志统一 loguru：`from app.core.log import logger`（agent 用 `pt_agent` 内等价封装），关键路径必须带 `run_id`/`agent_id` 上下文

### 3.2 异常与统一响应

- REST 统一响应包裹：`{"code": 0, "message": "ok", "data": ...}`；业务错误码非 0，HTTP 层仍用语义化状态码
- 服务层抛业务异常（`services/exceptions.py` 定义），由全局 exception_handler 统一转响应；API 层不写裸 try/except 吞错
- 枚举一律 Python `enum.Enum` + SQLAlchemy `Enum`：AgentStatus(ONLINE/BUSY/OFFLINE)、RunStatus(PENDING/RUNNING/FINISHED/PARTIAL/FAILED/STOPPED)、AgentPhase 等，禁止魔法字符串散落

### 3.3 配置规范

- 一切配置走环境变量 + `pydantic-settings`（`core/config.py` / agent `config.py`），`.env.example` 同步更新；**禁止硬编码地址/密钥**
- 新增配置项必须给默认值 + 注释

### 3.4 数据库规范

- 表名 `snake_case` 复数；所有表含 `id`(自增主键)、`created_at`、`updated_at`（models/base.py 公共 Mixin）
- 迁移只能通过 Alembic 生成，禁止 `create_all` 用于生产（骨架启动期的 dev 兜底除外，需注释标明）
- 指标类数据**不入 MySQL**（走 ES）

### 3.5 API 设计规范

- 路由前缀 `/api/v1`，资源名复数（`/api/v1/scenarios`）；WS 端点 `/ws/agent`
- 分页统一 `?page=1&page_size=20`，响应含 `total`
- 请求/响应必须走 Pydantic schema，禁止直接透传 ORM 对象
- 契约测试先行：每新增资源先在 `tests/` 写接口用例

## 4. 领域约定（跨模块一致性，改动需评审）

1. **JMX 参数化**：脚本内占位符 `${__P(key, default)}`，场景存 key-value 覆盖，执行时拼 `-Jkey=value`，不直接改 XML
2. **心跳判定**：Agent 每 10s 心跳，连续 3 次未收到 → OFFLINE；执行中 Agent 失联 → 对应 run 置异常
3. **幂等**：一切任务以 `run_id` 去重；下发重复任务 Agent 回 `task_ack(accepted=false)`
4. **指标批次**：5s 聚合一批，字段见 tech-selection 第 4.1 节，Master bulk 写 `pt-metrics-yyyy.MM.dd`
5. **结果汇聚**：Master 收齐全部 Agent 的 `result` 才合并写 `pt-summary` 并置 FINISHED；任一失败置 PARTIAL
6. **产物路径**：MinIO bucket `ptp`，key 规范 `scripts/{script_id}/{version}/...`、`runs/{run_no}/{agent_id}/...`

## 5. Git 与提交规范

- 分支：`main`（可发布）/ `feat/*` / `fix/*` / `chore/*`
- Commit：Conventional Commits（`feat: 新增场景批量启停`），一次提交一个意图
- 禁止提交：`.env`、日志、JTL、报告、`__pycache__`、`node_modules`

## 6. 开发工作流（vibe coding 约定）

1. 动手前先读 `docs/tech-selection.md`（Why）+ 本 skill（How）
2. 新功能先补 Pydantic schema / 协议字段，再写 service，最后接 API/WS——契约先行，AI 生成质量最高
3. 每完成一个可运行单元立即 `ruff check` + 跑 `pytest`，通过再继续
4. 涉及 Master↔Agent 协议的功能，两端同分支一次提交，避免协议漂移
