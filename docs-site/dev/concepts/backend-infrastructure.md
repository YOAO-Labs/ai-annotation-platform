---
audience: [dev]
type: explanation
since: v0.1.0
status: stable
last_reviewed: 2026-07-29
---

# 后端基础设施（容器）

本页对照 [`docker-compose.yml`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docker-compose.yml) 说明每个容器**做什么**、**存什么**、以及**怎么启停**。

## 一图看全

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/backend-infrastructure.svg"
  alt="开发环境宿主应用、默认 Compose 基础设施、五类 Celery worker、可选 ML 后端与监控服务拓扑"
  caption="开发环境基础设施与可选 profile"
/>

> **api / web 不在 compose 里**：dev 直接 host 跑（`uvicorn` / `vite`），compose 只管「不便本机跑的」基础设施。production 形态的 api 容器化在 [部署拓扑](./deployment-topology) 与 [部署指南](/ops/deploy/docker-compose)。

## 启动 / 关停速查

```bash
# 默认服务（postgres / redis / minio / mailpit / 五类 worker / beat）
docker compose up -d

# GPU 推理（需 NVIDIA Container Toolkit；backend 在叠加文件 docker-compose.ml.yml）
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend

# 可观测性（dev 多吃 ~200MB，按需）
docker compose --profile monitoring up -d prometheus grafana alertmanager

# 全部停（保留 volume）
docker compose down

# 全部停 + 抹数据（重置 dev 环境）
docker compose down -v
```

## 各容器职责

### postgres · 业务主库

- **镜像**：`postgres:16-alpine`
- **端口**：宿主 `5432`
- **凭证**（dev only）：`user / pass`，库名 `annotation`
- **存什么**：所有业务数据 — `users / projects / batches / tasks / annotations / predictions / audit_log / bug_reports / api_keys / failed_predictions / ml_backends ...`，外加 alembic 迁移记录 `alembic_version`。详见 [后端分层](./backend-layers)。
- **持久化**：volume `pgdata` → `/var/lib/postgresql/data`
- **重启 vs rebuild**：业务代码改动只重启 api/worker；schema 改动跑 `alembic upgrade head`，不需要重启容器
- **常用命令**

  ```bash
  # 进 psql
  docker exec -it ai-annotation-platform-postgres-1 psql -U user -d annotation
  # 备份
  docker exec ai-annotation-platform-postgres-1 pg_dump -U user annotation > dump.sql
  # 看慢查询 / 表大小
  docker exec ai-annotation-platform-postgres-1 psql -U user -d annotation \
    -c "select relname, pg_size_pretty(pg_total_relation_size(c.oid)) from pg_class c \
        join pg_namespace n on n.oid=c.relnamespace where n.nspname='public' order by pg_total_relation_size(c.oid) desc limit 10;"
  ```

### redis · 多用途内存数据库

- **镜像**：`redis:7-alpine`
- **端口**：宿主 `6379`
- **存什么（**全在内存 + 不开 RDB**）**：

  | 用途                            | key 形态                                                                                 | 写入方                 |
  | ------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------- |
  | Celery broker（任务队列）       | `default / ml / ml.cpu / media / gpu / gpu.control / export / unacked / kombu.binding.*` | api 入队 / worker 消费 |
  | Celery result backend           | `celery-task-meta-<task_id>`                                                             | worker 写结果          |
  | WebSocket pub/sub（多副本广播） | `notify:<user_id>`、`predict:<project_id>`                                               | api / worker → 前端 ws |
  | 限流 / 失败计数 / 进度缓存      | `ratelimit:*`、`login_fail:<ip>`、`progress:*`                                           | middleware / api       |

- **持久化**：**无 volume**。容器删 / 重启即清空 — 包括未消费的 Celery 任务、ws session、限流计数。dev 上是合意的；生产部署需挂 AOF volume。
- **常用命令**

  ```bash
  # 看队列堆积
  docker exec ai-annotation-platform-redis-1 redis-cli llen default
  docker exec ai-annotation-platform-redis-1 redis-cli llen ml
  docker exec ai-annotation-platform-redis-1 redis-cli llen media
  docker exec ai-annotation-platform-redis-1 redis-cli llen gpu
  # 清空（仅限 dev！）
  docker exec ai-annotation-platform-redis-1 redis-cli flushall
  ```

### minio · S3 兼容对象存储

- **镜像**：`icr.io/fusion-open/minio/minio`（[IBM 镜像公告](https://www.ibm.com/support/pages/node/7289585)；Compose 与 CI 固定相同 digest，避免原 Quay 镜像停止分发造成启动失败）
- **端口**：宿主 `9000`（S3 API）/ `9001`（Web 控制台 → http://localhost:9001 ， `minioadmin / minioadmin`）
- **存什么**：所有大文件 — 标注任务的图片帧、缩略图（blurhash 占位 + 真图）、批量导出包（COCO/YOLO/JSON zip）、评论附件、SAM 推理 fixture、用户上传 dataset；按 bucket 分（`annotations`、`datasets`、`media-cache`、`import`、`export` 等）
- **持久化**：volume `miniodata` → `/data`
- **常用命令**

  ```bash
  # 装 mc（host 上）：brew install minio/stable/mc
  mc alias set local http://localhost:9000 minioadmin minioadmin
  mc ls local/
  mc du local/local       # 看 bucket 占用
  ```

### celery-worker · 后台任务消费者

- **构建**：`infra/docker/Dockerfile.api`（与 api 同镜像，仅启动命令不同）
- **端口**：无（不对外暴露）
- **代码挂载**：`docker-compose.yml` 把 `./apps/api:/app` 整目录 bind mount 进所有 worker 和 beat（匿名卷 `/app/.venv` 屏蔽 host venv；依赖装在 `--system` site-packages 不在 `/app` 下故不被盖掉）。**改业务码 / 新增 alembic 迁移只需重启实际消费该任务队列的 worker，不必 rebuild**（Celery 没有 `--reload`，故仍须 restart，详见 [docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart)）。

#### 队列与「订阅」模型

Celery 里任务先被**投递（publish）到某个命名队列**，worker 只**消费它显式订阅（`-Q`）的队列**。投到没人订阅的队列的任务会一直躺着、永不执行。

- **worker 分组订阅**（按设备与权限隔离并发）：主 worker `-Q default,media,cleanup,audit`（`--concurrency=4`）；维护 worker `-Q maintenance`（单并发，执行分区维护与统计刷新）；GPU 控制 worker `-Q gpu.control`（单并发、唯一持有 collector 数据库凭据）；GPU worker `-Q ml,gpu`（`CELERY_GPU_CONCURRENCY=2`，低并发护显存）；CPU worker `-Q ml.cpu`（`CELERY_CPU_CONCURRENCY=4`）；导出 worker `-Q export`。预标任务按模型 `resource_profile.device` 路由：整条 pipeline 全 CPU → `ml.cpu`，任一 GPU/未自报 → `ml`（保守，零退化）。
- **任务 → 队列 的映射**：`apps/api/app/workers/celery_app.py` 的 `task_routes` 显式指定；**未显式路由的任务落到 `task_default_queue`**。

  | Queue         | 谁路由进来                                                                             | 典型任务                                                                                                                  | 前端可见？                                |
  | ------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
  | `default`     | `task_default_queue` 兜底 + ml_health                                                  | `publish_ml_backend_stats`(PerfHud) / `check_ml_backends_health` / `worker-heartbeat` / `mark_inactive_offline`(在线状态) | 部分（PerfHud / 在线状态点）              |
  | `ml`          | `batch_predict`（device=gpu/未自报）/ `retry_failed_prediction`                        | 跑 GPU ML backend / 重试失败预测                                                                                          | ✅ 工作台 AI 候选框                       |
  | `ml.cpu`      | `batch_predict`（整条 pipeline 全 device=cpu）                                         | 跑 CPU 模型预标（高并发）                                                                                                 | ✅ 工作台 AI 候选框                       |
  | `media`       | `generate_thumbnail` / `extract_video_frames` / `run_dataset_import` 等                | 缩略图、视频帧、媒体 backfill、连接器数据集导入                                                                           | ✅ 列表 / 工作台的图与帧 / 数据集导入进度 |
  | `gpu`         | `run_video_tracker_job`                                                                | 视频目标追踪                                                                                                              | ✅ 视频追踪结果                           |
  | `cleanup`     | `purge_soft_deleted_attachments` / `sync_to_duckdb` 等                                 | 普通数据清理、DuckDB 同步                                                                                                 | 间接（效率看板数据）                      |
  | `maintenance` | 两个 `refresh_*_mv`、两个 `ensure_future_*_partitions`、`archive_old_audit_partitions` | 所有者权限的统计刷新、月分区创建与归档                                                                                    | 间接（统计与分区维护）                    |
  | `audit`       | `persist_audit_entry` / `persist_task_events_batch`                                    | 审计日志 / task event 批量入库                                                                                            | ✅ admin 审计页                           |
  | `gpu.control` | GPU 显存仲裁控制面任务                                                                 | 显存 repair、tombstone GC 与 collector 账本维护                                                                           | 间接（GPU 资源治理）                      |
  | `export`      | `app.workers.export.run_export`                                                        | 独立构建并上传导出产物                                                                                                    | ✅ 异步导出状态与下载                     |

  ::: warning 易踩坑：`task_default_queue`
  Celery 内置默认队列名是 `celery`，**不在 worker 的 `-Q` 列表里**。若某 beat / 异步任务忘了在 `task_routes` 里路由、且 `task_default_queue` 仍是 `celery`，它就投进无人消费的 `celery` 队列里**静默堆积、永不执行**。修复：`celery_app.conf` 设 `task_default_queue="default"`，让兜底任务落到 worker 实际订阅的 `default` 队列。排查时 `redis-cli llen celery` 看死队列是否堆积。
  :::

- **常用命令**

  ```bash
  # 看实时日志
  docker logs -f ai-annotation-platform-celery-worker-1
  # 改 task 签名后必须重启实际消费的 worker；视频 tracker / GPU 预标用 GPU worker。
  docker restart ai-annotation-platform-celery-worker-gpu-1
  # 验证容器加载的是最新代码
  docker exec ai-annotation-platform-celery-worker-gpu-1 \
    python -c "import inspect, app.workers.tasks as t; print(inspect.signature(t.batch_predict))"
  ```

### grounded-sam2-backend · GPU 推理服务

- **构建**：`apps/grounded-sam2-backend/Dockerfile`（context `./apps`，便于 `COPY` 兄弟目录 `_shared/mask_utils`）
- **端口**：宿主 `8001`
- **profile**：`gpu`（默认不拉起）
- **存什么**：

  | Volume              | 路径                      | 内容                                                      |
  | ------------------- | ------------------------- | --------------------------------------------------------- |
  | `gsam2_checkpoints` | `/app/checkpoints`        | SAM 2.1 / GroundingDINO 模型权重（首启自动下载约 900 MB） |
  | `gsam2_hf_cache`    | `/app/.cache/huggingface` | HuggingFace 下载缓存                                      |

- **环境变量**：`SAM_VARIANT`（默认 `tiny`）/ `DINO_VARIANT`（默认 `T`）/ `BOX_THRESHOLD` / `TEXT_THRESHOLD`
- **协议**：`POST /predict` + `GET /health`，详见 [ML Backend 协议](../reference/ml-backend-protocol)
- **GPU 要求**：`deploy.resources.reservations.devices` 占用 1 张 NVIDIA GPU；本地无 GPU 笔记本启动会报 `could not select device driver`，跳过即可

### prometheus · 指标采集

- **镜像**：`prom/prometheus:v2.55.0`
- **端口**：宿主 `9090`
- **profile**：`monitoring`
- **存什么**：时序指标 TSDB，retention 14 天
- **持久化**：volume `prometheus_data` → `/prometheus`
- **scrape 目标**（`infra/prometheus/prometheus.yml`）：
  - `host.docker.internal:8000/metrics` ← FastAPI（host 上跑）
  - `/api/v1/internal/metrics-targets` ← 从 `ml_backends` 注册表动态发现 ML backend，再抓取各自 `/metrics`
  - `localhost:9090` ← 自身
- **Linux 注意**：`host.docker.internal` 需要 compose 的 `extra_hosts: host-gateway` 才能解析（已配）；不通时把 target 改成宿主机 LAN IP

### grafana · 仪表盘

- **镜像**：`grafana/grafana-oss:11.4.0`
- **端口**：宿主 `3001`（避开前端 dev 3000） → http://localhost:3001 ， `admin / admin`
- **profile**：`monitoring`
- **存什么**：用户 / 看板配置 / 数据源等元信息
- **持久化**：volume `grafana_data` → `/var/lib/grafana`
- **数据源 / 看板预置**：`infra/grafana/provisioning/`（datasource）+ `infra/grafana/dashboards/anno-overview.json`（5 panel：HTTP / ML / Celery）只读 mount 进容器
- **依赖**：`depends_on: prometheus`

### alertmanager · 告警路由

- **镜像**：`prom/alertmanager` <!-- image tag: v0.27.0 -->
- **端口**：宿主 `9093`
- **profile**：`monitoring`
- **作用**：接收 Prometheus firing 告警，分组和去重后按 `infra/alertmanager/alertmanager.yml` 路由到 Mailpit 或生产 SMTP
- **持久化**：volume `alertmanager_data` → `/alertmanager`

## Volume 一览

| Volume                               | 容器                  | 作用                          | dev 抹掉影响                                 |
| ------------------------------------ | --------------------- | ----------------------------- | -------------------------------------------- |
| `pgdata`                             | postgres              | 业务数据库                    | **业务数据全丢**，需重新种用户 / 项目 / 任务 |
| `miniodata`                          | minio                 | 对象存储                      | 所有图片 / 导出 / 附件丢失                   |
| `gsam2_checkpoints`                  | grounded-sam2-backend | 模型权重                      | 下次启动重新下载 ~900MB                      |
| `gsam2_hf_cache`                     | grounded-sam2-backend | HF 缓存                       | 重新拉缓存                                   |
| `sam3_checkpoints` / `sam3_hf_cache` | sam3-backend          | SAM 3 图像与视频权重、HF 缓存 | 下次启动重新下载 / 拉取                      |
| `yolo_checkpoints`                   | yolo-backend          | YOLO 权重缓存                 | 下次按需下载                                 |
| `prometheus_data`                    | prometheus            | 历史指标                      | 历史曲线丢失                                 |
| `grafana_data`                       | grafana               | 看板配置                      | 自定义面板丢失（provisioned 看板会重建）     |
| `alertmanager_data`                  | alertmanager          | 告警分组与静默状态            | 告警运行状态与静默规则丢失                   |

> redis **没有** volume，不在表内。

## 健康检查与依赖关系

`postgres / redis / minio`、各类 worker 和 GPU backend 都配了 `healthcheck`；五类 worker 在 `depends_on.condition: service_healthy` 上等待各自所需的底层服务，`gpu-control` 只依赖 Redis 与 Postgres，其余业务 worker 还依赖 MinIO。`celery-beat` 不是 consumer，没有 inspect healthcheck；`grafana` 只 `depends_on: prometheus`（无健康门控）。

dev 上常见 race：刚 `docker compose up -d` 立刻起 api，会因为 postgres 还在 healthcheck 中而连不上 — 等 5–15 秒再起 host 上的 `uvicorn` 即可。

## 相关文档

- [本地开发](../tutorials/local-dev) · 怎么把这些容器跑起来 + host 上的 api/web
- [部署指南](/ops/deploy/docker-compose) · production 形态（api / web 容器化）
- [部署拓扑](./deployment-topology) · 单机 / 分离 / 多 GPU 三种形态
- [可观测性 / 监控](/ops/observability/) · grafana 看板与 alerting 细节
- [ML Backend 协议](../reference/ml-backend-protocol) · grounded-sam2-backend 的 HTTP 协议
- [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart) · 改完代码什么时候要 rebuild
