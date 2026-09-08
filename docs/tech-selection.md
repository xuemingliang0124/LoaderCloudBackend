# JMeter 分布式性能测试平台 — 技术选型方案

> 版本：v2.0（Master-Agent 分布式架构） 日期：2026-09-05

## 1. 需求范围

- **JMeter 脚本管理**：JMX 上传、版本、参数（占位符）定义
- **测试场景管理**：脚本 + 参数覆盖 + 压力机分组/线程拆分
- **场景执行记录管理**：执行历史、实时曲线、汇总报告、产物归档
- **定时场景管理**：cron 定时触发、启停控制
- **分布式压测**：多压力机 Agent 化管理（注册/心跳/状态/调度/执行）
- **结果存储**：指标存 Elasticsearch，支持聚合查询；原始产物存 MinIO

## 2. 总体架构

```
                    Vue3 前端 (Element Plus + ECharts)
                              │ REST
                              ▼
        ┌─────────────── FastAPI Master（控制面）───────────────┐
        │  脚本/场景/执行记录/定时任务 管理                        │
        │  Agent 注册中心：心跳超时判定、分组、状态机               │
        │  编排器：选压力机 → 下发任务 → 汇聚结果 → 归档            │
        │  ES Writer：实时指标 bulk 写入 + 聚合查询 API            │
        └───────┬──────────────────────────────┬───────────────┘
                │ WebSocket 控制通道            │ HTTP
                ▼                              ▼
        ┌── Agent 压力机 A ──┐          ┌── Agent 压力机 B ──┐
        │ psutil 状态采集     │          │ （同左）            │
        │ JMeter 子进程执行   │          │                    │
        │ 5s 粒度指标上报     │          │                    │
        └─────┬──────────────┘          └─────┬──────────────┘
              │ 下载 JMX/CSV     上传 JTL/HTML 报告
              ▼                                ▼
        MinIO（脚本/参数文件/JTL/报告归档）  Elasticsearch 8.x（指标存储与聚合查询）
```

核心原则：**执行从平台进程剥离到 Agent，Master 只做编排和数据面**；MySQL 只存元数据，指标全部进 ES。

## 3. 技术选型清单

| 层次 | 选型 | 版本基线 | 理由 |
|---|---|---|---|
| Master 框架 | FastAPI + Pydantic v2 + uvicorn | FastAPI ≥0.115 | 自带 OpenAPI 文档；类型化代码对 AI 生成友好 |
| ORM/迁移 | SQLAlchemy 2.0 (async) + Alembic | 2.0 | 主流标配，async 契合事件循环 |
| 数据库 | MySQL 8.0 | 8.0 | 元数据存储（脚本/场景/记录/Agent/定时），团队熟悉度高 |
| 任务调度 | APScheduler (AsyncIOScheduler + SQLAlchemyJobStore) | 3.10+ | v2 中定时触发仅是"下发任务"毫秒级动作，无需 Celery 的分布式执行能力；少一个 Redis 依赖；misfire/coalesce 内建 |
| Agent | Python 3.12 异步：websockets + httpx + psutil | 3.12 | 与 Master 同栈，一套语言维护；打包为 Docker 镜像部署 |
| 通信 | WebSocket 控制通道（心跳/任务/指令/指标）+ HTTP（文件下载/产物上传） | — | Agent 主动外连 Master，压力机无需开端口，可穿透 NAT |
| 指标存储 | Elasticsearch 8.x + elasticsearch-py (Async) | 8.x | date_histogram/terms 聚合查询曲线；ILM 管理生命周期；单节点起步 |
| 对象存储 | MinIO (miniopy-async) | 最新稳定版 | JMX/CSV/JTL/HTML 报告统一归档，S3 协议可换云存储 |
| 鉴权 | JWT (python-jose) + bcrypt 直连（passlib 已停维护，与 bcrypt 4.1+ 不兼容，不使用） | — | MVP 简单；RBAC（Casbin）留扩展点 |
| 前端 | Vue 3 + Vite + TS + Element Plus + Pinia + ECharts | Vue 3.4+ | 国内生态全，覆盖管理后台与图表场景 |
| 测试 | pytest + pytest-asyncio + httpx (AsyncClient) | — | API 契约测试先行 |
| 部署 | Docker Compose（mysql / minio / elasticsearch / master；agent 独立 compose） | — | 一键起全套；后期量大迁 K8s |
| 日志 | loguru | — | 简洁、文件轮转开箱即用 |
| 代码质量 | ruff（lint + format）+ mypy（可选渐进） | — | 统一风格，AI 生成代码的一致性抓手 |

**为何不用 Celery（v1→v2 决策记录）**：Celery 的价值在分布式任务执行（队列/worker 池/重试/ack）。v2 架构下执行负载已由 Agent 承担，服务端定时任务只是触发一个轻量 async 编排函数，引入 Celery+Redis 属于过度设计。若 P3 增加重量级离线后处理（如大规模 JTL 分析），可局部引入，协议不受影响。

## 4. 通信协议（Master ↔ Agent）

统一信封：`{"type": "<消息类型>", "data": {...}, "ts": <unix秒>}`

### 4.1 Agent → Master

| type | data | 说明 |
|---|---|---|
| `register` | agent_id, ip, hostname, tags, jmeter_version | 连接建立后首条 |
| `heartbeat` | cpu, mem, net_in, net_out, status, current_run_id | 每 10s；Master 连续 3 次未收到判 OFFLINE |
| `task_ack` | run_id, accepted, message | 任务确认/拒绝 |
| `status` | run_id, phase(downloading/running/uploading/finished/failed/stopped), message | 生命周期上报 |
| `metrics` | run_id, interval_tps, avg_rt, p95_rt, err_rate, threads, by_label[] | 每 5s 一批，Master bulk 写 ES |
| `result` | run_id, summary, artifacts[] | 最终汇总 + MinIO 产物 key |

### 4.2 Master → Agent

| type | data | 说明 |
|---|---|---|
| `task` | run_id, files[], jmeter_args(-J 覆盖), start_at(对齐起压时间戳) | 下发任务 |
| `stop` | run_id | 停止执行，Agent 杀进程树 |
| `ping` / `pong` | — | 保活 |

幂等约定：Agent 以 run_id 去重；WS 断线指数退避重连，重连后重新 register + 上报当前状态。

## 5. ES 索引设计

| 索引 | 内容 | 用途 |
|---|---|---|
| `pt-metrics-yyyy.MM.dd` | 5s 粒度时序文档（run_id、agent_id、label 维度） | 实时曲线：date_histogram + terms(label) |
| `pt-summary` | 执行汇总：run_no（keyword）、总请求/错误/P50/P90/P95/P99/TPS，多 Agent 按 label 合并去重 | 执行详情页、报告导出 |
| `pt-agent-logs-yyyy.MM.dd` | Agent/JMeter 关键日志（可选） | 失败排查 |

原始 JTL 存 MinIO 不进 ES；ILM：30 天热 → 90 天删除。

## 6. 数据模型（MySQL）

```
user            用户、密码哈希、角色
agent_node      agent_id、ip、hostname、tags、jmeter_version、status(online/busy/offline)、
                cpu/mem、current_run_id、last_heartbeat
jmeter_script   脚本名、版本、minio key、占位符参数定义(JSON)、描述
test_scenario   场景名、script_id、参数覆盖(JSON)、agent 分组/标签、线程数/时长、描述
scenario_run    run_no、scenario_id、status(pending/running/finished/partial/failed/stopped)、
                trigger(manual/scheduled)、agent 快照、起止时间、summary 索引引用
schedule_job    名称、scenario_id、cron、enabled、next_run_time、last_run_id
```

## 7. 关键执行流程

```
创建执行 → 选 N 台 IDLE Agent（按分组）→ WS 下发 task（文件清单 + -J 参数 + start_at）
→ Agent 下载脚本 → 上报 running → 同步等 start_at 齐发
→ 运行中每 5s 上报 metrics → Master bulk 写 ES → 前端轮询 ES 聚合渲染
→ 结束：上传 JTL + HTML 报告到 MinIO，上报 result
→ Master 全部收齐后合并汇总写 pt-summary，置 FINISHED（任一失败置 PARTIAL）
停止：Master → WS stop → Agent 杀进程树 → 上报 stopped
```

JMX 参数化：脚本内使用 `${__P(threads,10)}` 占位符，场景存 key-value 覆盖，运行时拼 `-Jthreads=200`，避免 XML 改写；复杂编辑场景预留 lxml。

## 8. 分期路线

- **P1 (MVP)**：Agent 上线（注册/心跳/状态页）、手动下发单/多机执行、实时指标入 ES + 曲线、执行记录 + HTML 报告归档
- **P2**：定时场景、同步起压、强制停止、Agent 分组调度与线程拆分、异常恢复
- **P3**：Agent 自动升级、Grafana 直连 ES、RBAC、钉钉/企微通知、压力机资源水位调度

## 9. 部署形态

- 平台侧 docker-compose：mysql、minio、elasticsearch（单节点、开发期关安全认证）、master
- 压力机侧独立 compose / PyInstaller 单文件：agent 容器内置 OpenJDK + JMeter 5.6.x
- 环境变量统一 `.env` + pydantic-settings 管理（12-factor）
