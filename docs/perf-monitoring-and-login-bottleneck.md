# 性能监控栈搭建与登录接口瓶颈定位经验

> 日期：2026-09-14
> 背景：登录接口基准测试 646ms，5tps 单交易负载 P95 达 970ms（早期 JMeter 口径为 868ms）。搭建全链路监控（应用/容器/主机/MySQL/ES/MinIO）定位瓶颈，最终确认瓶颈为登录密码校验的 bcrypt CPU 开销，并决定**不做优化**（登录不适合持续 TPS 压测）。

## 1. 监控栈架构

采用与业务 compose 叠加（overlay）的方式部署，不改动现有编排：

```
docker compose -f deploy/docker-compose.yml \
               -f deploy/docker-compose.monitoring.yml up -d
```

| 组件 | 镜像 | 端口 | 作用 |
|---|---|---|---|
| Prometheus | prom/prometheus:v2.55.0 | 9090 | 指标采集，保留 15 天 |
| Grafana | grafana/grafana:11.2.0 | 3000 | 可视化（admin/admin） |
| node-exporter | v1.8.2 | 9100 | 宿主机 CPU/内存/磁盘/网络 |
| cAdvisor | v0.49.1 | 8080 | 容器级 CPU/内存/网络/IO |
| mysqld-exporter | v0.15.1 | 9104 | MySQL 连接数/QPS/慢查询/InnoDB |
| elasticsearch-exporter | v1.7.0 | 9114 | ES JVM 堆/索引/shard/线程池 |
| MinIO | 业务镜像自带 | 9000 | `/minio/v2/metrics/cluster` 免鉴权暴露 |

Master 应用层埋点（自写轻量实现，未引入 prometheus-fastapi-instrumentator）：

- `master/app/metrics.py`：HTTP Histogram 中间件 + 自定义 Collector（DB 连接池、APScheduler 任务数、Agent 在线数）
- 指标：`http_request_duration_seconds_bucket{handler,method,status}`（handler 用**路由模板**如 `/auth/login`，避免高基数）、`ptp_es_write_duration_seconds_bucket{index}`、`ptp_db_pool{metric}`、`ptp_scheduler_active_jobs`、`ptp_agent_online`
- ES 写入耗时在 `es_client.py` 的 write_metrics/write_summary 路径内 observe
- `/metrics` 在中间件中显式排除，避免自污染

Grafana 面板（均仅需 Prometheus 一个数据源，ID 已通过 grafana.com API 逐个核实）：

| ID | 面板 |
|---|---|
| 11074 | Node Exporter Full |
| 14282 | Cadvisor exporter |
| 7362 | MySQL Overview（Percona） |
| 4358 | Elasticsearch detailed（2018 老面板，部分指标名需按下文修正） |

## 2. 部署踩坑记录（按时间顺序）

### 2.1 elasticsearch-exporter：无效启动参数

- 现象：容器反复重启，`unknown long flag '--es.cluster_settings'`
- 原因：v1.7.0 无此 flag
- 修复：只保留 `--es.uri`、`--es.all`、`--es.indices`、`--es.shards`

### 2.2 mysqld-exporter v0.15.x：DATA_SOURCE_NAME 已废弃

- 现象：`failed to validate config section=client err="no user specified in section or parent"`，反复重启
- 原因：v0.15.0 起废弃 `DATA_SOURCE_NAME` 环境变量（被当作 .my.cnf 路径解析）
- 修复：改用命令行参数
  ```yaml
  command:
    - --mysqld.address=mysql:3306
    - --mysqld.username=ptp:ptp123456
  ```

### 2.3 MySQL 8.4：SHOW SLAVE STATUS 语法被移除

- 现象：exporter 起来后刷 `Error 1064 ... near 'SLAVE STATUS'`
- 原因：MySQL 8.4 彻底移除旧语法（改用 SHOW REPLICA STATUS），v0.15.1 exporter 未适配；单节点本就不需要复制监控
- 修复：追加 `--no-collect.slave_status`（如遇 slave_hosts/binlog 同类报错同模式禁用）

### 2.4 MinIO metrics 端点 403

- 现象：Prometheus targets 中 minio 为 down，`HTTP 403`
- 修复：业务 compose 的 minio 服务加环境变量 `MINIO_PROMETHEUS_AUTH_TYPE: public`（内网监控信任）

### 2.5 cAdvisor：Docker Desktop(WSL2) 双重兼容问题（最耗时）

**坑 A：私有 cgroup namespace**

- 现象：target UP，但只导出 `/`、`/docker`、`/docker/buildkit` 等系统 cgroup，业务容器全部缺失，`name` 标签为空
- 原因：Docker Desktop 默认每容器私有 cgroup namespace，cAdvisor 虽能看到 `/sys/fs/cgroup/docker/<hash>` 目录，但读不到其他容器 `cgroup.procs` 里的进程，误判为空 cgroup
- 修复：compose 加 `cgroup: host`

**坑 B：containerd image store 不兼容**

- 现象：加了 host cgroup 后容器被枚举到，但每个都报
  `Failed to create existing container: /docker/<id>: failed to identify the read-write layer ID`
- 诊断：`docker info` 显示 `Driver=overlayfs`（正常应为 overlay2）、`io.containerd.snapshotter.v1`，即 Docker Desktop 开启了 "Use containerd for pulling and storing images"。cAdvisor v0.49 的 Docker factory 依赖 layerdb 识别读写层，overlayfs 快照布局下找不到
- 尝试过的**失败方案**（留档，勿重复）：`--containerd=/var/run/containerd/containerd.sock --containerd-namespace=moby`。该 socket 是 Docker Desktop 系统级 containerd，dockerd 业务容器不在其中；且 dockerd 容器 cgroup 路径无 namespace 前缀，containerd factory 仍会跳过
- 最终修复：Docker Desktop → Settings → General → **取消勾选 "Use containerd for pulling and storing images"** → Apply & restart。然后 `docker info` 确认 Driver 恢复 `overlay2`，重新 `up -d --build`（镜像需重新 pull/build，**数据卷保留，MySQL/ES/MinIO 数据不丢**）
- 验证：`curl http://localhost:8080/api/v1.3/subcontainers` 应能列出全部业务容器；Prometheus 中 `container_cpu_usage_seconds_total{name!=""}` 序列带 `ptp-*` 容器名

**备用方案**（若未来必须保留 containerd image store）：弃用 cAdvisor，写一个走 Docker API `/containers/{id}/stats` 的轻量 Python exporter，不依赖 layerdb。

### 2.6 Grafana 面板 ID 张冠李戴

- 教训：凭记忆给的 19305 实际是 **mosdns v5**（DNS 服务面板，要求 Loki 数据源），不是 cAdvisor 面板
- 正确做法：给他人/未来引用面板 ID 前，用官方 API 核实
  `https://grafana.com/api/dashboards/<ID>`，检查返回的 `name`/`slug`
- 正确 cAdvisor 面板 ID 是 **14282**

### 2.7 ES exporter 指标命名差异

- v1.7.0（prometheus-community 版）所有指标带 `elasticsearch_` 前缀；网上及 4358 老面板常见的 `es_jvm_*` 是 justwatch 旧版命名，直接用会 No data
- 实例：
  - `elasticsearch_jvm_memory_used_bytes{area="heap"}` / `elasticsearch_jvm_memory_max_bytes{area="heap"}`
  - `elasticsearch_thread_pool_queue_size{name="write"}`
  - `elasticsearch_indices_search_query_time_seconds`
- 另注意：`handler` 标签名与 mysqld-exporter 部分指标重名（MySQL 会导出 mrr_init/read_rnd_next 等 handler 值），查 master HTTP 指标时加 `job="master"` 限定

## 3. 登录接口瓶颈定位（核心经验）

### 3.1 五面板证据链

5tps 登录压测期间同步观察：

| 指标 | 表现 | 结论 |
|---|---|---|
| 核心容器 CPU | **ptp-master-1 全程钉死 1.0 核**；mysql/es ≈ 0.02 | master 单核饱和，MySQL/ES 空闲 |
| MySQL 慢查询速率 | 全程 0 | 排除数据库 |
| ES JVM 堆使用率 | 0.36→0.65→GC 回落到 0.12 的锯齿 | 健康，排除 ES 堆/GC |
| ES 写入 P95 | 恒定 2.4s，与负载起落无关 | 是压测自身 bulk 写 pt-metrics 的背景流量，**不在登录链路上**，勿误判 |
| 登录 P95 | 0.97s 且随时间缓升 | 到达率 ≈ 服务容量时的排队特征 |

**关键方法**：判断一个慢指标是不是瓶颈，要看它是否与负载/RT **同步起落**。ES 写入 P95 恒定不变即为无关背景噪音。

### 3.2 定量验证：bcrypt

在 master 容器内实测（`docker exec ptp-master-1 python -`）：

```
bcrypt cost=12
单次 checkpw = 191ms 纯 CPU
单核理论容量 = 5.2 tps
```

5tps 负载下 CPU 利用率 ρ = 5 × 0.191 = **95.5%**。M/M/1 排队模型在 ρ=0.955 时平均等待约 4 秒；实测 P95 970ms = 191ms 真实计算 + ~780ms CPU 队列等待，完全吻合。

### 3.3 根因（代码确认）

两个因素叠加：

1. **bcrypt cost 12 故意慢**（191ms/次纯计算），见 `master/app/core/security.py`
2. **同步调用阻塞 async 事件循环**：`user_service.authenticate`（async def）内直接调用同步的 `verify_password()`，191ms 内整个事件循环冻结，其余请求全部排队；且 Dockerfile 的 uvicorn 未配 `--workers`，单进程只能用 1 核（宿主机 28 核中 27 核闲置）

### 3.4 决策：不优化

- 登录是「每用户每 Token 一次」的低频操作，持续 5tps 登录等价于每秒 5 次暴力破解尝试，不代表真实业务形态
- 登录后的业务接口只做 JWT HMAC 校验（微秒级），性能特征完全不同
- 基准 646ms 同理：191ms 是 bcrypt 下限，其余为冷启动/排队
- **压测脚本约定**：登录只做一次（或每线程每 token 周期一次），取 token 后用业务请求打持续负载，不要用登录接口跑 TPS

留档备选方案（未来若确有高并发登录需求再做，未实施）：

1. `await anyio.to_thread.run_sync(verify_password, password, hash)` 丢线程池——bcrypt 的 C 调用释放 GIL，可真并行利用多核且不冻结事件循环
2. uvicorn 加 `--workers 4`
3. bcrypt cost 12→10（191ms→约48ms，但降低抗破解强度，OWASP 下限，最后再考虑）

## 4. 可复用的排查方法论

1. **先建全链路监控再压测**：应用 RT + 各组件 CPU/慢查询/JVM 同屏，5 分钟定位 vs 盲猜几小时
2. **逐层排除**：target UP 不代表有数据（cAdvisor 坑）；指标存在不代表标签可用（name 为空、前缀变化）
3. **相关性判瓶颈**：指标曲线必须与负载同步陡升才算嫌疑
4. **用实测数字说话**：容器内直接 benchmark 可疑函数（bcrypt 191ms），再用排队论（ρ、M/M/1）解释 RT 构成
5. **版本兼容三连**：exporter 大版本变更（废弃 env/flag）、数据库大版本移除旧语法、桌面版 Docker 存储架构切换——三类问题均表现为容器"看似运行但数据不对"，查容器日志的 E/W 级别行最快
6. **外部 ID/配置引用前核实**：Grafana 面板 ID 用官方 API 校验名称

## 5. 相关文件

- `deploy/docker-compose.monitoring.yml` — 监控栈（含全部兼容性注释）
- `deploy/prometheus/prometheus.yml` — 抓取配置
- `deploy/docker-compose.yml` — minio 加了 `MINIO_PROMETHEUS_AUTH_TYPE: public`
- `master/app/metrics.py`、`master/app/main.py`、`master/app/services/es_client.py` — 应用埋点
- `master/tests/test_metrics.py` — 指标契约测试
