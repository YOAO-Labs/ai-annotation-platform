---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 后端基础设施（容器）

本页对照 `docker-compose.yml`、`docker-compose.ml.yml` 与 `docker-compose.training.yml`，说明各基础设施、后台队列与计算容器**做什么**、**存什么**以及**如何启停**。

## 一图看全

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/backend-infrastructure.svg"
  alt="开发环境宿主应用、默认 Compose 基础设施、Celery worker、可选 ML 后端与监控服务拓扑"
  caption="开发环境基础设施与可选 profile"
/>

> **API 与 Web 开发态不在默认 Compose 中**：日常开发直接在宿主机运行 `uvicorn` 与 `vite`，Compose 仅托管基础设施与后台 Worker。生产容器化形态见[部署拓扑](./deployment-topology)与[部署指南](/ops/deploy/docker-compose)。

## 启动 / 关停速查

```bash
# 默认服务（postgres / redis / minio / mailpit / 7 类 celery-worker / celery-beat）
docker compose up -d

# GPU 推理后端（需 NVIDIA Container Toolkit，定义于 docker-compose.ml.yml）
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-yolo up -d yolo-backend

# 独立训练执行器（定义于 docker-compose.training.yml）
docker compose -f docker-compose.training.yml --profile training up -d training-executor

# 可观测性（按需启动 Prometheus / Grafana / Alertmanager）
docker compose --profile monitoring up -d prometheus grafana alertmanager

# 停止容器（保留持久化数据卷）
docker compose down

# 停止容器并清空所有数据卷（仅限重置本地开发环境！）
docker compose down -v
```

## 各容器职责

### postgres · 业务主库

- **镜像**：`postgres:16-alpine`（宿主端口 `5432`，开发默认凭证 `user / pass`，库名 `annotation`）
- **存储内容**：所有核心业务与在线学习真值数据（`users`、`projects`、`batches`、`tasks`、`annotations`、`predictions`、`ml_backend_registry`、`training_dataset_versions`、`training_runs`、`trained_model_versions`、`audit_logs`、`bug_reports` 等）及 `alembic_version`。
- **持久化**：命名卷 `pgdata` → `/var/lib/postgresql/data`。
- **常用命令**：

  ```bash
  # 进入 psql 交互终端
  docker exec -it ai-annotation-platform-postgres-1 psql -U user -d annotation
  # 导出数据库备份
  docker exec ai-annotation-platform-postgres-1 pg_dump -U user annotation > dump.sql
  ```

### redis · 多用途内存数据库

- **镜像**：`redis:7-alpine`（宿主端口 `6379`）
- **存储内容（纯内存，开发态不开持久化）**：

  | 用途                          | Key 形态                                                                                                                         | 读写方                        |
  | :---------------------------- | :------------------------------------------------------------------------------------------------------------------------------- | :---------------------------- |
  | **Celery Broker（任务队列）** | `default` / `ml` / `ml.cpu` / `media` / `gpu` / `gpu.control` / `export` / `image-pyramid` / `maintenance` / `cleanup` / `audit` | API 入队 / Worker 消费        |
  | **Celery Result Backend**     | `celery-task-meta-<task_id>`                                                                                                     | Worker 写入执行结果           |
  | **WebSocket Pub/Sub**         | `notify:<user_id>`、`predict:<project_id>`、`video-tracker-job:<job_id>`、`ml-backend-stats:global`                              | API / Worker → 前端 WebSocket |
  | **路由租约、GPU 仲裁与限流**  | `ml-router:v1:*`、`gpu-arbiter:*`、`ratelimit:*`、`login_fail:<ip>`                                                              | API / Worker 协调             |

- **常用命令**：

  ```bash
  # 查看指定队列积压长度
  docker exec ai-annotation-platform-redis-1 redis-cli llen default
  docker exec ai-annotation-platform-redis-1 redis-cli llen ml
  ```

### minio · S3 兼容对象存储

- **镜像**：`icr.io/fusion-open/minio/minio`（S3 API 端口 `9000`，Web 控制台 `http://localhost:9001`，默认账号 `minioadmin / minioadmin`）
- **存储内容**：按 9 个专用存储桶隔离存放各类对象（`annotations`、`datasets`、`media-cache`、`import`、`export`、`bug-reports`、`audit-archive`、`avatars`、`training`）。详见[存储桶布局](../reference/storage-buckets)。
- **持久化**：命名卷 `miniodata` → `/data`。

### mailpit · 本地开发邮件沙箱

- **端口**：SMTP `1025` / Web 收件箱 `http://localhost:8025`
- **职责**：拦截开发环境的密码重置、成员邀请与告警邮件，不在开发态向公网真实发信。

### audit-summary · 隔离实验

- **构建**：`apps/audit-summary/Dockerfile`，非 root scratch 镜像运行 Go 静态二进制。
- **启用**：仅独立 `docker-compose.experiments.yml` 的 `audit-summary-experiment` profile；默认／生产 Compose 不包含它。
- **端口**：实验内部 8091，仅发布到宿主 `127.0.0.1:8091`。
- **权限与状态**：无数据库、Redis、对象存储凭据、业务卷或任务账本。
- **健康检查**：二进制 `-healthcheck`；停止实验不影响本地 Python 月报。
- **默认运行**：工作树、普通应用镜像和普通 CI 不包含或启动实验，仅显式命令或手动 workflow 验证。

### assets-source · 可选来源与连接管理服务

- **构建**：`apps/assets-source/Dockerfile`；独立 Python runtime，也承载现有连接管理流程。
- **启动**：单独项目 `docker-compose.assets-source.yml`，默认和生产主 Compose 都不拉起；内部监听 `8767`。
- **职责**：单文件上传/发布、Dataset/Item 删除、同步 ZIP/旧前缀扫描，以及连接列表、读取、创建、更新、删除和连通性检查。
- **数据库与对象存储**：使用 26 项 Assets Source LOGIN 能力和专用 S3 身份；连接导入进程另用九项 LOGIN，Catalog 使用独立 29 项 LOGIN。视频与图片共用的 media control LOGIN 执行 32 项能力；视频 worker 为 10 项，图片 worker 使用独立 6 项 LOGIN。没有对业务表或其他 owner schema 的直接授权。
- **API 选择**：`ASSETS_SOURCE_URL` 留空时走本地 Source 工作流；设置远程 URL 后，Source 错误不会回退到本地实现。Source 服务不连接 Redis 或 Celery。
- **连接器凭据**：Source Fernet key 可留空启动，但创建密文或解密凭据时需要它；它应与现有连接器 key 相同。主机策略采用 Settings 数据库覆盖、Source JSON 环境回退的顺序。

### 7 类 Celery Worker 与 Beat · 后台任务消费者

- **构建**：`infra/docker/Dockerfile.api`（与 API 共用基础镜像，仅启动命令与队列参数不同）。
- **代码挂载**：开发态将 `./apps/api:/app` 挂载进所有 Worker 与 Beat（匿名卷 `/app/.venv` 屏蔽宿主机虚拟环境）。修改 Python 业务代码后**必须重启对应队列的 Worker 容器**（Celery 不支持代码热重载，详见 [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart)）。

#### Worker 容器与队列订阅矩阵

Celery 任务按设备属性、I/O 密集度与数据库权限拆分为 **7 类专职 Worker 容器** 和 **1 个 Beat 调度器**：

| 容器服务名                        | 订阅队列（`-Q`）              | 核心任务与职责                                                                                                                                                                                                                                                                                                                                                                                                                    |
| :-------------------------------- | :---------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`celery-worker`**               | `default,media,cleanup,audit` | • **`default`**：ML 健康探活、PerfHud 采样、在线学习策略巡检（`check_policies`）、超参搜索推进（`check_studies`）、评估与主动学习维护（`maintain_evaluations` / `maintain_rounds`）<br>• **`media`**：缩略图生成、视频抽帧、连接器数据集导入<br>• **`cleanup`**：软删除附件清理、DuckDB 同步、过期增强预览清理（`purge_expired_previews`）、历史训练观测清理（`purge_observations`）<br>• **`audit`**：审计日志与任务事件批量落库 |
| **`celery-worker-gpu`**           | `ml,gpu`                      | GPU 批量预标注（`batch_predict`）、失败预测重试（`retry_failed_prediction`）、视频 AI 追踪（`run_video_tracker_job`）                                                                                                                                                                                                                                                                                                             |
| **`celery-worker-cpu`**           | `ml.cpu`                      | 纯 CPU 模型批量预标注、冻结逐样本评估（`run_sample_evaluation`）、主动学习候选推理与选样（`run_round`）                                                                                                                                                                                                                                                                                                                           |
| **`celery-worker-gpu-control`**   | `gpu.control`                 | 单并发 GPU 显存仲裁控制面：显存修复、墓碑回收与 collector 账本对账                                                                                                                                                                                                                                                                                                                                                                |
| **`celery-worker-export`**        | `export`                      | 项目/批次异步导出打包（`run_export`）、在线学习冻结数据集构建与物化（`run_training_dataset`）                                                                                                                                                                                                                                                                                                                                     |
| **`celery-worker-image-pyramid`** | `image-pyramid`               | 超大图金字塔 overview 与多层级 WebP tile 构建                                                                                                                                                                                                                                                                                                                                                                                     |
| **`celery-worker-maintenance`**   | `maintenance`                 | 单并发所有者权限维护：物化视图刷新（`refresh_*_mv`）、月度表分区创建与旧审计分区归档                                                                                                                                                                                                                                                                                                                                              |

::: warning 易踩坑：`task_default_queue`
Celery 内置默认队列名为 `celery`，不在任何 Worker 的 `-Q` 列表中。`celery_app.conf` 显式将 `task_default_queue` 设为 `"default"`，确保未显式声明路由的任务落入主 Worker 消费的 `default` 队列。排查任务不执行时，可先用 `redis-cli llen <queue>` 检查对应队列是否积压。
:::

### ML Backend 推理服务与训练执行器（可选叠加）

- **5 个第一方 ML Backend（`docker-compose.ml.yml`）**：
  - `grounded-sam2-backend`（`:8001`，profile `gpu`）
  - `sam3-backend`（`:8002`，profile `gpu-sam3`）
  - `yolo-backend`（`:8003`，profile `gpu-yolo`，同时支持 `/managed-models` 受控自训权重挂载与 `/managed-evaluation/*` 冻结评估）
  - `onnxtools-backend`（`:8004`，profile `gpu-onnxtools`）
  - `rapidocr-backend`（`:8005`，profile `gpu-rapidocr`）
- **独立训练执行器（`docker-compose.training.yml`）**：
  - `training-executor`（profile `training`，无外露端口）：轮询 `/api/v1/internal/training/*`，通过宿主状态卷上的 OS `flock` 协调同一资源的执行器 执行 Ultralytics YOLO / LibreYOLO 训练。

### prometheus / grafana / alertmanager · 监控栈（`profile: monitoring`）

- **`prometheus`（`:9090`）**：抓取宿主机/容器 `api:8000/metrics`，并通过 `/api/v1/internal/metrics-targets` 从 `ml_backend_registry` 动态发现活跃 ML Backend 实例采集指标（保留 14 天）。
- **`grafana`（`:3001`）**：预置 `infra/grafana/provisioning/` 数据源与 `anno-overview.json` 看板（默认账号 `admin / admin`）。
- **`alertmanager`（`:9093`）**：接收 Prometheus 告警并按 `infra/alertmanager/alertmanager.yml` 路由至 Mailpit 或生产 SMTP。

## Volume 一览

| Volume                                                   | 关联容器                | 存储内容                               | 开发态删除（`down -v`）的影响                |
| :------------------------------------------------------- | :---------------------- | :------------------------------------- | :------------------------------------------- |
| `pgdata`                                                 | `postgres`              | PostgreSQL 业务数据库                  | **业务数据全清**，需重新执行迁移与 `seed.py` |
| `miniodata`                                              | `minio`                 | 9 个对象存储桶全部文件                 | 所有图片、导出包、训练数据集与模型权重丢失   |
| `gsam2_checkpoints` / `gsam2_hf_cache`                   | `grounded-sam2-backend` | SAM 2.1 / GroundingDINO 权重与 HF 缓存 | 下次启动需重新下载约 900 MB                  |
| `sam3_checkpoints` / `sam3_hf_cache`                     | `sam3-backend`          | SAM 3 权重与 HF 缓存                   | 下次启动重新下载                             |
| `yolo_checkpoints`                                       | `yolo-backend`          | YOLO 预置权重与受控自训模型缓存        | 预置权重按需重拉，已挂载收据需重新触发部署   |
| `prometheus_data` / `grafana_data` / `alertmanager_data` | 监控组件                | 历史时序指标、自定义面板与静默状态     | 历史监控曲线清空，预置看板自动重建           |

## 健康检查与启动顺序

`postgres`、`redis`、`minio`、7 类 Celery Worker 及各 ML Backend 均配置了 `healthcheck`。各 Worker 通过 `depends_on.condition: service_healthy` 等待底层存储就绪后再拉起。刚执行 `docker compose up -d` 后，若立即在宿主机启动 `uvicorn` 报数据库连接拒绝，等待 5–10 秒待 `postgres` 通过健康检查即可。

## 相关文档

- [本地开发](../tutorials/local-dev) · 宿主机 API / Web 与容器基础设施联调指南
- [部署拓扑](./deployment-topology) · 单机、计算分离、局域网生产与训练节点拓扑
- [运行环境形态](./runtime-environments) · `development` / `staging` / `production` 行为差异
- [ML Backend 协议](../reference/ml-backend-protocol) · 推理后端与受控模型热加载协议
- [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart) · 代码与依赖变更后的刷新策略
