# 项目作用域改造总结

> 完成日期：2026-09-13 ｜ 涉及模块：项目、脚本、场景、执行、定时任务

## 1. 背景与目标

改造前，脚本/场景/执行/定时任务均为全局扁平资源：无归属边界，任意登录用户可跨"业务线"引用彼此的资源（如场景可引用任意项目的脚本），删除与权限粒度无法按业务线隔离。

本次改造引入**项目管理模块**，并将全部核心资源收敛到项目作用域下，形成归属链路：

```
test_project（项目）
 ├── jmeter_script      脚本（物理外键 project_id）
 ├── test_scenario      场景（物理外键 project_id，且引用脚本须同项目）
 │    ├── scenario_run          执行记录（经 scenario_id 派生项目归属）
 │    └── schedule_job          定时任务（经 scenario_id 派生项目归属）
```

## 2. 接口契约（新旧对照）

### 项目管理（新增）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/projects` | 新建项目，名称唯一（重名 3020） |
| GET | `/api/v1/projects` | 项目列表（name 模糊 + 分页） |

### 脚本管理

| 旧路径 | 新路径 |
|---|---|
| `POST /api/v1/scripts` | `POST /api/v1/projects/{project_id}/scripts` |
| `GET /api/v1/scripts` | `GET /api/v1/projects/{project_id}/scripts` |
| `PUT /api/v1/scripts/{id}/jmx` | `PUT /api/v1/projects/{project_id}/scripts/{id}/jmx` |
| `DELETE /api/v1/scripts/{id}` | `DELETE /api/v1/projects/{project_id}/scripts/{id}` |
| `GET /api/v1/scripts/{id}/thread-groups` | `GET /api/v1/projects/{project_id}/scripts/{id}/thread-groups` |

### 场景管理

| 旧路径 | 新路径 |
|---|---|
| `POST /api/v1/scenarios` | `POST /api/v1/projects/{project_id}/scenarios` |
| `GET /api/v1/scenarios` | `GET /api/v1/projects/{project_id}/scenarios` |
| `PUT /api/v1/scenarios/{id}` | `PUT /api/v1/projects/{project_id}/scenarios/{id}` |
| `GET /api/v1/scenarios/{id}/delete-precheck` | `GET /api/v1/projects/{project_id}/scenarios/{id}/delete-precheck` |
| `DELETE /api/v1/scenarios/{id}?force=` | `DELETE /api/v1/projects/{project_id}/scenarios/{id}?force=` |

### 执行与定时任务

| 旧路径 | 新路径 |
|---|---|
| `POST /api/v1/runs` | `POST /api/v1/projects/{project_id}/runs` |
| `GET /api/v1/runs` | `GET /api/v1/projects/{project_id}/runs` |
| `POST /api/v1/runs/{run_no}/stop` | `POST /api/v1/projects/{project_id}/runs/{run_no}/stop` |
| `POST /api/v1/schedules` | `POST /api/v1/projects/{project_id}/schedules` |
| `GET /api/v1/schedules` | `GET /api/v1/projects/{project_id}/schedules` |
| `POST /api/v1/schedules/{id}/toggle` | `POST /api/v1/projects/{project_id}/schedules/{id}/toggle` |

> 所有旧扁平路径已**整体下线**（404，非重定向），避免前端静默使用旧契约。`WS /ws/runs/{run_no}?token=` 保持扁平路径：run_no 全局唯一且经 token 鉴权，前端实时通道无需作用域。

## 3. 校验语义与错误码

新增错误码（项目段从 3020 起，避开脚本/场景已占用的 3001–3016）：

| 码 | 含义 | 触发点 |
|---|---|---|
| 3020 | 项目名称已存在 | POST /projects |
| 3021 | 项目不存在 | 所有嵌套路由（路径中的 project_id） |
| 3022 | 资源不属于指定项目（跨项目访问） | 脚本/场景/执行记录/定时任务的归属校验 |

既有错误码语义不变：脚本 3001–3010、场景 3010–3016、执行 2001/2003/2006、定时 4001/4002。

**新增跨资源校验**：

- 场景创建/更新：引用的所有脚本必须**存在**（3012）且**与场景同项目**（3022）
- 执行触发/定时创建：引用场景必须**存在**（3013，统一用场景模块语义；orchestrator 内部 2001 保留为兜底）且**同项目**（3022）
- 执行停止：run_no 必须存在（2003）且其场景归属该项目（3022）
- 定时启停：任务必须存在（4002）且其场景归属该项目（3022）

**校验顺序固定**：`401 未登录 → 422 参数非法 → 3021/3022 作用域 → 业务错误`。

## 4. 数据模型与迁移

| 迁移 | 内容 |
|---|---|
| `20260913a1` | 新建 `test_project`（name 唯一约束 `uq_test_project_name`、description、created_by） |
| `20260913b1` | `jmeter_script` 新增 `project_id BIGINT NOT NULL` + 外键 `fk_jmeter_script_project`（RESTRICT）+ 索引；历史脚本回填「默认项目」 |
| `20260913c1` | `test_scenario` 新增 `project_id BIGINT NOT NULL` + 外键 `fk_test_scenario_project`（RESTRICT）+ 索引；历史场景回填「默认项目」 |

迁移要点：

- **幂等可重跑**：DDL 经 information_schema 存在性检查；「默认项目」用 `NOT EXISTS` 守卫插入（created_by=system），b1/c1 共享同一默认项目
- **历史数据零丢失**：project_id 先以可空列加入 → 回填 → 置 NOT NULL，避免旧数据阻塞 ALTER
- `scenario_run`/`schedule_job` **不加冗余列**（见 §5 决策 2）

## 5. 关键设计决策

**1）嵌套路由而非扁平路径 + 必填参数**
project_id 位于路径中，天然必填、URL 自描述、与 REST 层级语义一致；跨项目访问由归属校验兜底（3022）。

**2）执行/定时的项目归属经 join 派生，不加列**
执行记录与定时任务通过 `scenario_id → test_scenario.project_id` 派生归属。场景的项目归属不可变（无迁移接口），派生关系稳定；避免了双写一致性与额外回填迁移。列表查询为一次 `JOIN ... WHERE scenario.project_id = ?`。

**3）`ensure_project_exists` 显式调用，而非 Depends 依赖**
实现过程中发现 FastAPI 行为陷阱：**依赖解析先于 query/body 参数校验执行**。若做成依赖，参数非法（422）的请求也会先触发数据库查询，且错误优先级变为 3021 > 422。最终落地为 [deps.py](/master/app/api/deps.py) 中的公共函数，各 handler **首行显式调用**，稳定保持 `401 → 422 → 3021/3022 → 业务错误` 顺序。此约定适用于后续所有新增的项目作用域接口。

**4）项目重名 3020 优先于建表**
项目名称唯一由 DB 约束 `uq_test_project_name` 兜底，接口层先查后插。

## 6. 不受影响范围

- **Agent 执行链路**：Agent 下载脚本/数据文件走任务载荷中的 MinIO 预签名 URL，不经过管理接口
- **orchestrator**：按 scenario_id 加载场景及其脚本关联，project_id 仅作归属字段，无逻辑变更
- **场景删除级联**：原 run_agent_result → scenario_run → schedule_job → test_scenario 顺序不变，仅新增归属前置校验
- **插件管理**：全局插件池（/api/v1/plugins）本就不属于项目作用域

## 7. 变更文件清单

| 文件 | 变更 |
|---|---|
| `app/models/project.py` | 新增 Project ORM |
| `app/models/script.py` / `app/models/scenario.py` | 新增 project_id 外键字段 |
| `app/schemas/__init__.py` | 新增 ProjectIn/ProjectOut；ScriptOut/ScenarioOut 增加 project_id |
| `app/api/deps.py` | 新增 ensure_project_exists 公共校验 |
| `app/api/v1/projects.py` | 新建（新建 + 列表） |
| `app/api/v1/scripts.py` / `scenarios.py` / `runs.py` / `schedules.py` | 全部端点嵌套化 + 归属校验 |
| `alembic/versions/20260913a1_project.py` / `20260913b1_*.py` / `20260913c1_*.py` | 三段幂等迁移 |
| `tests/test_projects.py` / `test_scripts.py` / `test_scenarios.py` / `test_runs.py` / `test_schedules.py` | 5 个契约测试文件，401/404/422 门禁 |
| `README.md` | 接口表与功能清单同步 |

## 8. 验证结果

- `ruff check`（line-length 100）+ `ruff format`：全部通过
- `pytest`：**95 passed**（含新增 32 个项目作用域契约用例）
- `alembic heads`：单一 head `20260913c1`（a1 → b1 → c1 线性链）
- OpenAPI：18 个端点全部注册，旧扁平路径 0 残留
- 本地无 MySQL 实例，迁移未实际执行过库（幂等设计支持安全重跑）

## 9. 升级指引

```bash
# 上线时按序执行（幂等，可安全重跑）
alembic upgrade head
```

执行后所有历史脚本与场景自动归入「默认项目」（system 创建）。如需按真实业务线重新归类，直接 UPDATE 对应行的 project_id，或删除后重新导入。

**前端配合项**：脚本/场景/执行/定时全部接口改用嵌套路由；列表页需先选定项目；创建/触发时的资源选择器需按项目过滤（后端已强制同项目校验）。

## 10. 后续建议

- 项目删除接口（需先预检项目内脚本/场景/执行/定时占用，复用场景删除的 precheck + force 模式）
- RunOut/ScheduleOut 是否补充 project_id 字段，按前端需要决定
- 若未来出现"项目内成员/角色"需求，可基于 project_id 做行级权限，模型已就绪
