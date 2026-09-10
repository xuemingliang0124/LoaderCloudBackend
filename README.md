# JMeter 分布式性能测试平台

FastAPI Master + Python Agent 架构的 JMeter 性能测试管理平台：脚本管理、场景管理、
分布式执行（压力机 Agent 化）、定时场景、实时指标（Elasticsearch）、产物归档（MinIO）。

## 目录结构

```
master/    控制面：FastAPI + SQLAlchemy + APScheduler + ES/MinIO
agent/     压力机端：websockets + httpx + psutil + JMeter 子进程管理
deploy/    docker-compose（平台侧 / 压力机侧）
docs/      tech-selection.md 技术选型方案
.trae/skills/ptp-dev/  开发规范（写代码前必读）
```

## 快速启动（平台侧）

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# Master: http://localhost:8000/docs  Swagger: /docs
# 默认账号 admin / admin123
# MinIO 控制台: http://localhost:9001  ES: http://localhost:9200
```

## 启动 Agent（每台压力机）

```bash
cd agent && pip install -r requirements.txt && cp .env.example .env
# .env 中配置 MASTER_WS_URL / AGENT_ID / TAGS / JMETER_BIN
python -m pt_agent.main
```

容器方式（压力机上需已装 Docker，Master 与压力机不同机时改 MASTER_WS_URL）：

```bash
MASTER_WS_URL=ws://<master-host>:8000/ws/agent AGENT_ID=agent-01 TAGS=机房A \
  docker compose -f deploy/docker-compose.agent.yml up -d --build
```

## 本地开发（Master）

```bash
cd master
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

- 质量检查：`ruff check .` / `ruff format .`
- 测试：`pytest`
- 迁移：`alembic revision --autogenerate -m "xxx"` && `alembic upgrade head`

## 关键端点

| 端点 | 说明 |
|---|---|
| `POST /api/v1/auth/login` | 登录取 token |
| `GET /api/v1/agents` | 压力机列表（状态/心跳） |
| `POST /api/v1/scripts` | 上传 JMX（multipart） |
| `POST/GET/PATCH/DELETE /api/v1/plugins` | JMeter 插件 jar 上传/列表/启用禁用/删除 |
| `POST /api/v1/plugins/{id}/sync` | 手动触发对在线 Agent 推送同步 |
| `POST/GET /api/v1/scenarios` | 场景创建/列表 |
| `POST /api/v1/runs` | 触发执行（自动选机或指定 agent_ids） |
| `POST /api/v1/runs/{run_no}/stop` | 停止执行 |
| `GET /api/v1/metrics/timeseries` | ES 聚合曲线数据 |
| `POST/GET /api/v1/schedules` | 定时场景（5 段 crontab） |
| `WS /ws/agent` | Agent 控制通道 |

## 待办事项

### P1 — 链路贯通

- [x] Agent 文件下载（MinIO presigned）与产物上传
- [x] Agent 镜像内置 JMeter 5.6.3 + 第三方插件（按需在 `agent/Dockerfile` 增补 jar）
- [x] JTL 增量解析产出实时指标 — `agent/pt_agent/jtl_parser.py` `parse_increment` + `executor.py` `_metrics_loop`
- [x] Master 结果分片按 label 合并去重 — `master/app/services/orchestrator.py` `_merge_summaries` + `on_agent_result`
- [x] JMeter 真实汇总解析（samples/errors/p95/max_tps/by_label）— `agent/pt_agent/jtl_parser.py` + `executor.py` `_execute`

### P2 — 生产化

- [x] stop_run 以 Agent 回报 stopped 为准再置终态：先发 stop 置 STOPPING，收齐 Agent result（或看门狗 `STOP_WAIT_TIMEOUT` 超时兜底）才置 STOPPED — `orchestrator.py` `stop_run`/`_maybe_finalize`/`_stop_watchdog`
- [x] 结果汇聚持久化到 `run_agent_result` 表（替代内存态 `_pending_results`），Master 重启经 `recover_active_runs` 恢复汇聚现场
- [x] 脚本级插件依赖声明：脚本上传插件 jar，Master 下发时比对 Agent 已装清单，缺失的预签 URL 随任务下发到 plugin_dir 并经 `-Jsearch_paths` 注入，免改镜像
- [x] 插件池化与压力机维度依赖管理：jar 上传到全局 `jmeter_plugin` 表（sha256 去重），`agent_plugin` 关联表审计实际安装；Agent 启动按 `expected_plugins` diff 对齐 + 在线推送 `MSG_PLUGIN_SYNC/REMOVE` + 心跳对账纠偏，任务期 remove 标记 pending 等结束后清理 — `api/v1/plugins.py` `services/plugin_sync.py` `agent/pt_agent/plugin_sync.py`
- [x] Agent 分组调度（标签 OR 匹配）与 `total_threads` 按压力机 CPU 核数最大余数法拆分 — `orchestrator.py` `split_threads`
- [x] WebSocket 实时曲线替代前端轮询 ES：前端订阅 `/ws/runs/{run_no}?token=`，metrics/状态经 `FrontendHub` fan-out

### P3 — 平台能力

- [ ] 压力机自动扩缩与资源水位调度
- [ ] Grafana 直连 ES 看板 / 报告对比与基线
- [ ] RBAC（Casbin）、钉钉/企微告警通知
- [ ] Agent 自动升级机制

协议约定与开发规范见 [.trae/skills/ptp-dev/SKILL.md](.trae/skills/ptp-dev/SKILL.md)，
架构与选型详见 [docs/tech-selection.md](docs/tech-selection.md)。
