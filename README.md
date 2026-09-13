# JMeter 分布式性能测试平台

FastAPI Master + Python Agent 架构的 JMeter 性能测试管理平台：脚本与数据文件管理、
JMX 静态扫描/替换、多脚本场景（脚本级选机 + 线程组级加压参数、四类场景模型）、
分布式执行（按标签选机 + 按压力机 CPU 核数拆分线程）、定时场景、实时指标
（WebSocket 推送 + Elasticsearch）、全局插件池、产物归档（MinIO）。

## 目录结构

```
master/    控制面：FastAPI + SQLAlchemy 2.0 async + APScheduler + ES/MinIO
  app/
    api/v1/      REST 端点（auth/agents/projects/scripts/plugins/scenarios/runs/schedules/metrics）
    services/    业务逻辑：orchestrator 编排 / jmx_scanner / jmx_assembler /
                 jmx_checker / plugin_sync / scheduler / es_client / storage
    models/      SQLAlchemy ORM，一表一文件；schemas/ Pydantic 契约
    ws/          WebSocket 双通道：/ws/agent（Agent）+ /ws/runs/{run_no}（前端）
  alembic/       数据库迁移（一变更一文件）
  tests/         pytest + httpx
agent/     压力机端：websockets + httpx + psutil + JMeter 子进程 + JTL 增量解析
deploy/    docker-compose：平台侧（MySQL/ES/Kibana/MinIO/Master）/ 压力机侧 / 生产离线版
docs/      tech-selection.md 技术选型方案
.trae/skills/ptp-dev/  开发规范（写代码前必读）
```

## 快速启动（平台侧）

```bash
docker compose -f deploy/docker-compose.yml up -d --build
# Master Swagger: http://localhost:8000/docs
# 默认账号 admin / admin123
# MinIO 控制台: http://localhost:9001   ES: http://localhost:9200
# Kibana:      http://localhost:5601
```

## 启动 Agent（每台压力机）

```bash
cd agent && pip install -r requirements.txt && cp .env.example .env
# .env 中配置 MASTER_WS_URL / TAGS / JMETER_BIN
# AGENT_ID 留空时，启动按宿主机 IP 向 Master 注册并领取固定 ID（重启不变）
python -m pt_agent.main
```

容器方式（压力机上需已装 Docker，Master 与压力机不同机时改 MASTER_WS_URL）：

```bash
MASTER_WS_URL=ws://<master-host>:8000/ws/agent AGENT_ID=agent-01 TAGS=机房A \
  docker compose -f deploy/docker-compose.agent.yml up -d --build
```

生产压力机推荐使用预构建镜像的离线部署版（含 host 网络、ulimit/sysctl 调优说明）：
`deploy/docker-compose.agent-prod.yml`。

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
- **容器部署自动迁移**：master 镜像入口 `entrypoint.sh` 在 uvicorn 启动前先执行
  `alembic upgrade head`，迁移失败则容器退出（compose 已用 MySQL healthcheck 保证
  启动顺序）。应用 lifespan 的 `create_all` 仅作 dev 兜底（只补建新表、不给旧表加列），
  schema 演进一律以 alembic 为准；新增迁移后重建/重启 master 即可，无需手工进容器。
  多副本部署时需确保同一时刻仅一个实例执行迁移（当前 compose 单副本）。

## 关键端点

所有业务接口走 `/api/v1` 前缀、JWT 鉴权（`agents/register` 与 WS 除外），
统一响应包裹 `{"code": 0, "message": "ok", "data": ...}`。

| 端点 | 说明 |
|---|---|
| `GET /api/v1/health` | 健康检查 |
| `POST /api/v1/auth/login` | 登录取 token |
| `POST /api/v1/users` | 新建用户（仅全局 admin，1010）：用户名重复 1011；角色收中文（管理员/普通用户） |
| `GET /api/v1/users` | 用户列表（仅 admin）：用户名模糊、角色过滤、分页 |
| `GET /api/v1/users/{username}` | 用户详情（仅 admin）：不存在 1012 |
| `PUT /api/v1/users/{username}` | 改角色/密码（仅 admin，两者至少传一项）：不可降级自己 1014、末位 admin 1015 |
| `DELETE /api/v1/users/{username}` | 删除用户（仅 admin）：不可删除自己 1014、末位 admin 1015；同事务级联清理 project_member |
| `POST /api/v1/agents/register` | Agent 启动按宿主机 IP 注册/领取固定 agent_id，返回 expected_plugins 清单 |
| `GET /api/v1/agents` | 压力机列表（分页、关键字、在线状态过滤） |
| `WS /ws/agent` | Agent 控制通道（注册/心跳/任务/指令/指标） |
| `POST /api/v1/projects` | 新建项目（名称唯一，重名拒绝 3020；创建者同事务自动成为 owner） |
| `GET /api/v1/projects` | 项目列表（分页、名称模糊查询；响应含 my_role=当前用户项目内角色，admin 恒为项目管理员） |
| `PUT /api/v1/projects/{project_id}` | 更新项目名称/描述（owner+，admin 直通）：至少传一项否则 422；重名 3020；响应含 my_role |
| `GET /api/v1/projects/{project_id}/delete-precheck` | 删除预检（viewer+）：返回 scripts/scenarios/running_runs/schedule_jobs |
| `DELETE /api/v1/projects/{project_id}` | 删除项目（owner+）：严格模式存在脚本/场景拒绝 3023；`?force=true` 级联清理场景/执行记录/定时任务/脚本/成员及 MinIO 产物；存在未结束执行任务时 3014（force 也不例外） |
| `POST /api/v1/projects/{project_id}/members` | 项目成员授权（owner+，admin 直通）：目标用户不存在 3035、重复授权 3032 |
| `GET /api/v1/projects/{project_id}/members` | 成员列表（viewer+，分页、用户名模糊查询） |
| `PUT /api/v1/projects/{project_id}/members/{username}` | 变更成员角色（owner+）：成员不存在 3033，创建者不可降级/末位 owner 不可降级 3034 |
| `DELETE /api/v1/projects/{project_id}/members/{username}` | 移除成员（owner+）：创建者不可移除/末位 owner 不可移除 3034，成员不存在 3033 |
| `POST /api/v1/projects/{project_id}/scripts` | 在项目下上传 JMX（multipart），可带数据文件（csv/txt/dat/tsv）与 params 占位符定义；项目不存在 3021 |
| `GET /api/v1/projects/{project_id}/scripts` | 项目内脚本列表（分页、名称模糊查询） |
| `GET /api/v1/projects/{project_id}/scripts/{id}/thread-groups` | 静态扫描 JMX，返回启用线程组及加压参数（前端配置场景用）；跨项目访问 3022 |
| `PUT /api/v1/projects/{project_id}/scripts/{id}/jmx` | 替换 JMX：新旧启用线程组（名称+类型）必须一致，否则拒绝（3009） |
| `DELETE /api/v1/projects/{project_id}/scripts/{id}` | 删除脚本：跨项目 3022；被场景引用时拒绝（3010），随后清理 MinIO 对象 |
| `POST/GET /api/v1/plugins` | 全局插件池：jar 上传（sha256 内容去重）/列表 |
| `GET/PATCH /api/v1/plugins/{id}` | 详情 / 启用禁用、改描述（在线 Agent 联动 install/remove 推送） |
| `DELETE /api/v1/plugins/{id}` | 删除：推 remove → 清 agent_plugin → 删库 → 删 MinIO 对象 |
| `POST /api/v1/plugins/{id}/sync` | 手动触发对在线 Agent 推送同步 |
| `POST /api/v1/projects/{project_id}/scenarios` | 在项目下创建场景：多脚本组合 + 脚本级选机（agent_tags/agent_count）+ 线程组级参数；脚本须属于该项目（3022），项目不存在 3021 |
| `GET /api/v1/projects/{project_id}/scenarios` | 项目内场景列表（分页、名称模糊查询） |
| `PUT /api/v1/projects/{project_id}/scenarios/{id}` | 修改场景：基础信息 + 脚本关联事务内全量替换；跨项目 3022，有未结束任务时拒绝（3014） |
| `GET /api/v1/projects/{project_id}/scenarios/{id}/delete-precheck` | 删除预检：返回 running_runs / history_runs / schedule_jobs |
| `DELETE /api/v1/projects/{project_id}/scenarios/{id}?force=` | 跨项目 3022；严格模式默认拒绝（3014 运行中 / 3015 定时引用 / 3016 历史记录）；force=true 级联删除并清理 MinIO 产物 |
| `POST /api/v1/projects/{project_id}/runs` | 在项目下触发执行（场景须属于该项目 3013/3022；自动选机或指定 agent_ids） |
| `GET /api/v1/projects/{project_id}/runs` | 项目内执行记录列表（分页，经场景归属过滤） |
| `GET /api/v1/projects/{project_id}/runs/{run_no}` | 执行记录详情（返回同列表单条；记录须属于该项目 2003/3022） |
| `POST /api/v1/projects/{project_id}/runs/{run_no}/stop` | 停止执行（记录须属于该项目 2003/3022；先置 STOPPING，收齐 Agent 回报或看门狗超时才置 STOPPED） |
| `WS /ws/runs/{run_no}?token=` | 前端实时通道：指标批次/状态 fan-out，替代轮询 ES；仅项目成员（viewer+）可订阅，无权/记录不存在一律 1008 拒绝 |
| `GET /api/v1/metrics/timeseries` | ES 按 label 聚合的时间序列曲线，可选 `sample_type=request\|transaction` 过滤；仅项目成员（viewer+）可查（3030），记录不存在 2003 |
| `POST/GET /api/v1/projects/{project_id}/schedules` | 项目内定时场景（标准 5 段 crontab；场景须属于该项目 3013/3022），支持名称/启用状态过滤 |
| `POST /api/v1/projects/{project_id}/schedules/{id}/toggle` | 定时任务启停（任务须属于该项目 4002/3022；联动 APScheduler 注册/注销） |

## 领域约定

- **用户管理**：`/api/v1/users` 全套 CRUD 仅全局 admin 可访问（`ensure_global_admin`
  纯 token 判定 1010）；全局角色 `GlobalRole`（管理员/普通用户，DB 存 admin/user，
  历史 viewer 按非管理员兼容渲染）。错误码：1010 非管理员 / 1011 用户名重复 /
  1012 用户不存在 / 1014 不可降级或删除自己 / 1015 至少保留一个管理员；非法角色、
  密码 < 6 位、更新体为空均 422。删除用户在同事务级联清理 project_member，
  项目因此失去唯一 owner 时由 admin 事后补授权。
- **项目级权限**：双层角色——`sys_user.role` 全局（admin 超管仅校验项目存在）+
  `project_member.role` 项目内 owner/editor/viewer（中文：项目管理员/编辑者/观察者；
  JWT 携带全局 role，成员关系不进 token 保证吊销即时生效）。所有 `/projects/{project_id}/...`
  接口先过 `ensure_project_access`：3021 项目不存在 → 3030 非成员 → 3031 角色不足
  （读接口 viewer+，写接口 editor+，成员管理 owner+）；项目列表非 admin 仅返回已授权
  项目；run 指标与 WS 订阅按 run_no → 场景 → 项目 校验（viewer+）。成员管理错误码：
  3032 已是成员 / 3033 成员不存在 / 3034 创建者或末位 owner 保护 / 3035 目标用户不存在；
  非法角色由 Pydantic 枚举校验直接 422。项目更新/删除要求 owner+；删除遵循
  「预检 + force 级联」：3023 项目下存在脚本/场景（严格模式），运行中任务 3014
  （force 也不例外），force 按结果分片 → 执行记录 → 定时任务（含 APScheduler 注销）
  → 场景（级联脚本关联）→ 脚本 → 项目+成员 顺序清理，MinIO 产物提交后 best-effort。
- **JMX 参数化**：脚本内占位符 `${__P(key, default)}`，场景存 `param_overrides`
  key-value 覆盖，执行时拼 `-Jkey=value`，不直接改原始 XML。
- **场景类型**：单交易基准 / 单交易负载 / 混合场景 / 稳定性，四选一；
  执行时由 `jmx_assembler` 按场景落库的线程组设置组装运行用 JMX（原始脚本不动）。
- **多脚本选机**：每个脚本可配 agent_tags（标签 OR 匹配）与 agent_count，
  `total_threads` 按压力机 CPU 核数以最大余数法拆分。
- **心跳判定**：Agent 每 10s 心跳，连续 3 次未收到 → OFFLINE；执行中失联 → run 置异常。
- **指标与汇聚**：Agent 5s 一批写 `pt-metrics-*`；Master 收齐全部 Agent result
  合并写 `pt-summary-{run_no}` 并置 FINISHED，任一失败置 PARTIAL。
- **事务/请求区分**：JMeter 事务行按官方标记（responseMessage 含
  "Number of samples in transaction"）行级识别为 `sample_type=transaction`，
  与请求（request）分桶上报/聚合；全局 TPS/请求数/p95 只计请求行，
  避免事务父子样本双计（`/metrics/timeseries` 支持 `sample_type` 过滤）。
- **产物路径**：MinIO bucket `ptp`，key 规范
  `scripts/{script_id}/{version}/...`、`plugins/{plugin_id}/...`、`runs/{run_no}/...`。

## 迭代进展

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

### P2+ — 脚本与场景管理闭环

- [x] 脚本数据文件（csv/txt/dat/tsv）随 JMX 上传：扩展名/重名校验 + 按 JMX 内引用做缺失校验（3003/3004/3006）
- [x] JMX 线程组静态扫描：逐层 enabled 链路过滤、用户变量按"线程组内 > 全局"作用域解析 — `services/jmx_scanner.py`，`GET /projects/{project_id}/scripts/{id}/thread-groups`
- [x] JMX 文件替换：新旧启用线程组按"名称 + 类型"比对一致才允许覆盖（3009），线程数/rampUp/循环等差异由场景设置在执行期覆盖 — `PUT /projects/{project_id}/scripts/{id}/jmx`
- [x] 脚本删除：场景引用预检（3010）+ MinIO JMX 与数据文件物理清理（失败仅告警）
- [x] 多脚本场景：`scenario_script`（脚本顺序、agent_tags OR 选机、agent_count）+ `scenario_script_tg`（线程组级 num_threads/ramp_time/loops/scheduler/duration），场景-脚本关联全量替换
- [x] 场景类型四枚举（单交易基准/单交易负载/混合场景/稳定性）+ 场景级 duration（Pydantic 与 ORM Enum 双层约束）
- [x] 场景修改接口 `PUT /projects/{project_id}/scenarios/{id}`：存在 PENDING/RUNNING/STOPPING 任务时拒绝（3014），脚本关联在事务内删旧重建
- [x] 场景删除预检 + `force=true` 级联删除：run_agent_result → scenario_run → 定时任务（APScheduler remove_job）→ scenario，事务提交后 best-effort 清理 MinIO `runs/{run_no}/`（3014/3015/3016）
- [x] 执行期 JMX 组装：按场景设置改写线程组参数，单交易基准开启 TestPlan `serialize_threadgroups` — `services/jmx_assembler.py`
- [x] Agent 按宿主机 IP 注册固定 agent_id（UDP connect 探测网卡，Master 不可达指数退避重试），显式 `AGENT_ID` 配置优先 — `agent/pt_agent/identity.py` + `POST /agents/register`
- [x] 插件策略收敛为纯全局池：取消脚本-插件绑定检查，上传 sha256 去重，删除联动 MinIO 物理删除；启动期一次性迁移历史脚本级插件
- [x] 列表接口统一分页 + 关键字模糊查询（agents/scripts/scenarios/schedules）
- [x] 项目作用域：项目 CRUD（POST/GET /api/v1/projects）；脚本与场景全部接口收敛为 `/projects/{project_id}/...` 嵌套路由（3021 项目不存在 / 3022 跨项目访问），场景引用脚本须同项目，历史数据迁移回填「默认项目」
- [x] 项目级权限管理：`project_member` 表（迁移 20260913d1 幂等建表并回填 owner）+ JWT 携带全局 role（旧 token 强制重登）+ `ensure_project_access` 收口全部项目接口（3030 非成员 / 3031 角色不足，admin 直通）；建项目自动 owner、项目列表按成员过滤、`/metrics/timeseries` 与 `/ws/runs/{run_no}` 按 run→场景→项目 校验成员可见性
- [x] 项目成员管理 API：授权/列表/改角色/移除（owner+，admin 直通），角色项目管理员/编辑者/观察者（DB 存英文、API 出中文）；3032 重复授权 / 3033 成员不存在 / 3034 创建者与末位 owner 保护 / 3035 目标用户不存在
- [x] 用户管理 CRUD：`/api/v1/users` 创建/列表/详情/改角色改密/删除（仅全局 admin），GlobalRole 管理员/普通用户；1010 非管理员 / 1011 重名 / 1012 不存在 / 1014 自保护 / 1015 末位 admin 保护，删用户同事务级联清理 project_member
- [x] 项目更新/删除：PUT /projects/{id}（owner+，重名 3020）；删除预检 + 严格/force 级联模式（3023 资产阻断、3014 运行中阻断），force 按分片→执行→定时任务→场景→脚本→项目顺序同事务清理，MinIO best-effort
- [x] 生产压力机离线部署 compose：预构建镜像分发、host 网络、nofile/端口范围调优 — `deploy/docker-compose.agent-prod.yml`
- [x] 平台侧 compose 增加 Kibana（http://localhost:5601）

### P3 — 平台能力

- [ ] 压力机自动扩缩与资源水位调度
- [ ] Grafana 直连 ES 看板 / 报告对比与基线
- [ ] RBAC（Casbin）、钉钉/企微告警通知
- [ ] Agent 自动升级机制
- [ ] 场景级联删除后 ES `pt-summary-*`/`pt-metrics-*` 数据的生命周期清理

协议约定与开发规范见 [.trae/skills/ptp-dev/SKILL.md](.trae/skills/ptp-dev/SKILL.md)，
架构与选型详见 [docs/tech-selection.md](docs/tech-selection.md)。
