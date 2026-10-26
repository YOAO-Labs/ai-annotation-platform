---
title: 环境变量参考
audience: [dev, ops]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-10-27
---

# 环境变量参考

> **自动生成说明**：本页由 `docs-site/scripts/generate-env-vars.mjs` 从 `.env.example` 生成。
> 修改环境变量说明请编辑 `.env.example` 中的注释，再运行 `pnpm docs:gen-env-vars`。

## 数据库 (PostgreSQL)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@localhost:5432/annotation` | 异步数据库连接串，格式：postgresql+asyncpg://用户名:密码@主机:端口/数据库名 本地开发可直接使用下方默认值；生产环境请替换为真实凭据 注：驱动必须是 postgresql+asyncpg；托管库走 SSL 时用 ?ssl=require（asyncpg 不认 sslmode=） pnpm dev:worktree 只使用本机实例，并向子进程覆盖为专属库；不要为工作树改共享 .env。 |
| `MIGRATION_DATABASE_URL` | `—` | 可选的 Alembic / 专用维护 worker 的 schema-owner 连接；运行角色没有 DDL 权限时必须配置。 未设置或留空时迁移仍回退 DATABASE_URL，保持单角色部署兼容。 dev:worktree 会同时覆盖运行/迁移目标库名；迁移账号需具备本机 CREATEDB 权限。 |
| `DATABASE_URL_DOCKER` | `postgresql+asyncpg://user:pass@postgres:5432/annotation` | 仅供开发态 Compose 内的 Celery worker 使用；容器访问 PostgreSQL 应使用 postgres 服务名。 GPU collector 启用前请改成独立、非 owner、非超级用户且无 membership/fence DELETE 的应用角色。 生产叠加文件仍统一读取 DATABASE_URL，不使用此项。 |
| `MIGRATION_DATABASE_URL_DOCKER` | `—` | 开发态 Compose 的 maintenance worker 使用此连接执行数据库维护，迁移由宿主机显式执行；容器地址通常为 postgres:5432。 |
| `ALEMBIC_AUTO_UPGRADE` | `true` | API 镜像 entrypoint 是否自动执行 Alembic；标准生产 API 保持 true。 开发及生产 Worker 均强制为 false；开发迁移使用 pnpm dev:api 或显式运行 Alembic。 |

## 仅供 docker-compose.yml 里的 postgres 容器初始化用（后端不读这三项）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `POSTGRES_USER` | `user` | 若沿用 compose 自带的 postgres 容器，生产请在此设强凭据；分离运行/迁移角色时应与 MIGRATION_DATABASE_URL 的 owner 凭据对应，而 DATABASE_URL 可使用普通应用角色。 用托管 RDS/Cloud SQL 时这三项可忽略。 |
| `POSTGRES_PASSWORD` | `pass` | — |
| `POSTGRES_DB` | `annotation` | — |

## 缓存 / 消息队列 (Redis)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接地址，用于会话缓存、速率限制等 dev:worktree 自动创建独立 Redis 并覆盖缓存、Celery broker/read/write/result 目标；无需手填端口。 格式：redis://[:密码@]主机:端口/数据库编号 |

## --- 设备感知预标队列路由 ---

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PREANNOTATE_GPU_QUEUE` | `ml` | 按模型自报的 resource_profile.device 把预标任务路由到不同 Celery 队列: 整条 pipeline 全部 device=cpu → CPU 队列 (高并发); 任一 GPU/未自报阶段 → GPU 队列 (低并发护显存)。 队列名默认复用现有 "ml" (GPU) + 新增 "ml.cpu" (CPU)。改名须同步 docker-compose 各 worker 的 -Q。 |
| `PREANNOTATE_CPU_QUEUE` | `ml.cpu` | — |
| `CELERY_GPU_CONCURRENCY` | `2` | GPU/CPU 预标 worker 并发 (docker-compose celery-worker-gpu / celery-worker-cpu 读取)。 GPU 低并发避免多预标并发打爆显存; CPU 可较高并发。 |
| `CELERY_CPU_CONCURRENCY` | `4` | — |

## 可选 Jobs 私有 Celery owner；URL 留空时 API 使用现有内嵌 broker。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `JOBS_QUEUE_URL` | `—` | 私有 HTTP 服务没有宿主机端口，仅供同一 Compose 网络内的 API 调用。 |
| `JOBS_QUEUE_TOKEN` | `—` | — |
| `JOBS_QUEUE_TOKEN_FILE` | `—` | — |
| `JOBS_QUEUE_BROKER_URL` | `—` | 独立 Jobs owner 只读取 broker 和服务 bearer，不接收 API、数据库或 Identity 凭据。 |
| `JOBS_QUEUE_BROKER_URL_FILE` | `—` | — |
| `JOBS_QUEUE_HOST` | `127.0.0.1` | — |
| `JOBS_QUEUE_PORT` | `8190` | — |
| `JOBS_DISPATCH_RELAY_ENABLED` | `false` | 独立持久派发 relay 必须显式开启；仅持有 claim/finish/status 三项函数权限及队列 bearer。 不注入 broker、API 迁移凭据或私表读写权限；与 jobs-queue 分进程运行。 |
| `JOBS_DISPATCH_RELAY_DATABASE_URL` | `—` | — |
| `JOBS_DISPATCH_RELAY_DATABASE_URL_FILE` | `—` | — |
| `JOBS_DISPATCH_RELAY_BATCH_SIZE` | `100` | — |
| `JOBS_DISPATCH_RELAY_LEASE_SECONDS` | `30` | — |
| `JOBS_DISPATCH_RELAY_POLL_SECONDS` | `1` | — |
| `AAP_JOBS_QUEUE_RUNTIME_IMAGE` | `—` | 本机独立 Compose 组合：使用已验收镜像和已核实的基础设施网络，不创建第二份 Redis／PostgreSQL。 |
| `AAP_JOBS_DEVELOPMENT_NETWORK` | `—` | — |

## 对象存储 (MinIO / S3 兼容)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINIO_ENDPOINT` | `localhost:9000` | MinIO 服务地址（不含协议前缀），Docker 默认 localhost:9000 |
| `MINIO_PUBLIC_URL` | `/minio` | 浏览器可达的 MinIO 根地址。DEV 用同源 /minio，由 Vite :3000 代理到 localhost:9000，远程浏览器无需额外打通 9000 端口。生产应改为完整外网 URL。 容器中的 ML Backend 访问地址仍由 ML_BACKEND_STORAGE_HOST 独立控制。 |
| `MINIO_ACCESS_KEY` | `minioadmin` | MinIO 访问密钥（相当于 AWS Access Key ID） 沿用 compose 自带 minio 时，这两项同时作为 minio 容器的 root 凭据（docker-compose.yml 绑定）， 后端/worker 与 minio 容器自动共用同一份；用托管 S3/OSS 时填对方的 AK/SK 即可。 |
| `MINIO_SECRET_KEY` | `minioadmin` | MinIO 密钥（相当于 AWS Secret Access Key）；生产环境务必更换 |
| `MINIO_BUCKET` | `annotations` | 存放标注文件（图片、音频等）的桶名称 |
| `MINIO_DATASETS_BUCKET` | `datasets` | 数据集源文件桶（图片/视频/文本原始文件） |
| `MINIO_BUG_REPORTS_BUCKET` | `bug-reports` | Bug 反馈截图桶（180 天 lifecycle） |
| `MINIO_MEDIA_CACHE_BUCKET` | `media-cache` | 派生媒体缓存桶（thumbnails / 视频帧 / chunks / playback，30 天 lifecycle，可重生） |
| `MINIO_AUDIT_ARCHIVE_BUCKET` | `audit-archive` | 审计冷分区归档桶（永久保留，合规相关，建议开启 versioning + object lock） |
| `MINIO_IMPORT_BUCKET` | `import` | 导入预标注产物桶（7 天 lifecycle，短生命周期） |
| `MINIO_EXPORT_BUCKET` | `export` | 导出标注产物桶（7 天 lifecycle，短生命周期） |
| `MINIO_AVATARS_BUCKET` | `avatars` | 用户头像桶（永久保留，不可重生，纳入备份；不挂 lifecycle） |
| `MINIO_TRAINING_BUCKET` | `training` | Immutable training datasets and model artifacts; no automatic expiration. |
| `MINIO_INPUTS_BUCKET` | `accepted-inputs` | 已受理的不可变输入对象使用独立启用版本控制的桶，不配置自动过期。 |

## 项目受控训练，默认关闭。API 与独立 executor 使用同一随机凭据（至少 32 字符）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TRAINING_ENABLED` | `false` | — |
| `TRAINING_EXECUTOR_TOKEN` | `—` | — |
| `TRAINING_NODE_ID` | `local` | — |
| `TRAINING_GPU_UUID` | `—` | — |
| `TRAINING_CATALOG_JSON` | `{}` | JSON: {"ultralytics":{"version":"PINNED_VERSION","image_digest":"sha256:IMAGE_DIGEST", "base_models":{"yolo11n":{"filename":"yolo11n.pt","sha256":"WEIGHTS_SHA256"}}}} Qualified adapters: ultralytics 8.4.177 / libreyolo 1.6.0; entries share the same shape. Ultralytics additionally qualifies yolo11n-seg simple-polygon instance training; configure its verified weight entry explicitly. Revision-2 detection image upgrades require a qualified source-image -> complete schema-identity allowlist in compatible_config_images. LibreYOLO9 t/s/m/c native detection weights can return through the qualified CPU inference adapter. |
| `TRAINING_HEARTBEAT_TIMEOUT_SECONDS` | `60` | — |
| `TRAINING_IMAGE` | `—` | 仅供 docker-compose.training.yml：先准备已发布镜像，Compose 不构建镜像。 TRAINING_IMAGE 为仓库名（可带标签），不得包含 @digest；凭据文件不进入镜像。 TRAINING_IMAGE_DIGEST 必须为 registry manifest digest（RepoDigests），不能用镜像 .Id。 Compose 用同一 repository@sha256 引用选择镜像并生成 TRAINING_IMAGE_REFERENCE。 |
| `TRAINING_IMAGE_DIGEST` | `—` | — |
| `TRAINING_API_URL` | `http://host.docker.internal:8000/api/v1/internal/training` | — |
| `TRAINING_EXECUTOR_TOKEN_PATH` | `—` | — |
| `TRAINING_MODELS_PATH` | `./training/models` | — |

## 超大图 Image Pyramid

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_PYRAMID_AUTO_GENERATE` | `false` | 默认关闭自动金字塔受理；尺寸和缩略图仍自动处理。容量验收后再显式开启金字塔。 |
| `IMAGE_PYRAMID_PROFILE_VERSION` | `pyramid-v1` | — |
| `IMAGE_PYRAMID_NORMALIZATION_VERSION` | `exif-autorotate-srgb-v1` | — |
| `IMAGE_PYRAMID_OPTIONAL_PIXELS` | `16777216` | 16MP 起可选生成，50MP 起客户端必须使用金字塔；缺失时保留安全预览。 |
| `IMAGE_PYRAMID_REQUIRED_PIXELS` | `50000000` | — |
| `IMAGE_PYRAMID_MAX_PIXELS` | `300000000` | — |
| `IMAGE_PYRAMID_MAX_DIMENSION` | `32768` | — |
| `IMAGE_PYRAMID_MAX_TILES` | `20000` | — |
| `IMAGE_PYRAMID_MAX_SOURCE_BYTES` | `8589934592` | — |
| `IMAGE_PYRAMID_MAX_DERIVED_BYTES` | `8589934592` | — |
| `IMAGE_PYRAMID_MAX_TEMP_BYTES` | `12884901888` | — |
| `IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS` | `1800` | — |
| `IMAGE_PYRAMID_LEASE_SECONDS` | `2100` | 必须严格大于 IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS + 60（worker hard time limit）。 |
| `IMAGE_PYRAMID_URL_EXPIRY_SECONDS` | `900` | — |
| `IMAGE_PYRAMID_ASSET_URL_BATCH_MAX` | `128` | — |
| `IMAGE_PYRAMID_RETRY_COOLDOWN_SECONDS` | `300` | — |
| `IMAGE_PYRAMID_GC_GRACE_HOURS` | `24` | — |
| `IMAGE_PYRAMID_VIPS_CONCURRENCY` | `4` | 独立 worker 内 libvips 的像素流水线线程数；不要跟宿主 100+ 核数线性放大 RSS。 |
| `IMAGE_PYRAMID_SRGB_PROFILE` | `/usr/share/color/icc/sRGB.icc` | 容器由 icc-profiles-free 提供；宿主开发若路径不同可覆盖。 |

## 可选：把 MinIO 数据目录 bind 到宿主机路径，用于测试不同磁盘的对象存储性能。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MINIO_DATA_DIR` | `/mnt/fast-disk/ai-annotation-platform/minio` | 留空时继续使用 Docker 托管的 miniodata 命名卷。 |
| `ML_BACKEND_STORAGE_HOST` | `172.17.0.1:9000` | ML backend 在 docker compose 网内、平台 api 在 host 进程时, SAM 容器无法 hit host 的 localhost:9000; 设为 docker bridge 网关地址即可。 Linux: 172.17.0.1:9000; macOS/Win Docker Desktop: host.docker.internal:9000; 生产 (api/sam/minio 同 K8s 网) 留空。 |

## ML Backend 注册表单 URL 默认值预填 hint (avoid 手敲).

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ML_BACKEND_DEFAULT_URL` | `http://172.17.0.1:8001` | 留空则用前端硬编码默认 http://172.17.0.1:8001; 生产 K8s 同 namespace 时可设为 service DNS, 让运维注册时直接 ready. |

## 模型市场「容器直连观测」面板要观测的后端容器 URL 列表 (逗号分隔 / JSON list)。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ML_BACKEND_OBSERVE_URLS` | `—` | 与项目注册解耦: 没有任何项目注册 backend 时, 运维也能在模型市场直连这些容器看 健康度 / 变体目录 / 试启动。留空则回退到 ML_BACKEND_DEFAULT_URL (若其非空)。 例: ML_BACKEND_OBSERVE_URLS=http://172.17.0.1:8001,http://172.17.0.1:8002,http://172.17.0.1:8003 (8001=grounded-sam2, 8002=sam3, 8003=yolo) |

## ADR-0049 · 跨 backend GPU 显存仲裁。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GPU_ARBITER_MODE` | `off` | 全局 desired mode 是逐卡 desired mode 的上限，默认 off。 observe 在真实派发点记录非权威 would-* 决策，不拒绝、排队或驱逐请求。 enforce 仍需 Redis 账本和 lifecycle gate 握手，未就绪时 effective 保持 off。 |
| `GPU_ARBITER_ROLLOUT_ENABLED` | `false` | P6 持久 rollout 总门。保持 false 时 worker 不执行 enforce repair，effective enforce 永远不会打开；启用后 health repair 只在 Backend gate 与 Redis ready 证明成立后 提交逐资源 enforcing，GPU 派发对过渡态或数据库不可用在 Backend HTTP 前阻断。 |
| `GPU_ARBITER_COLLECTOR_DATABASE_URL_FILE` | `—` | Host file containing the dedicated collector function-only DSN. Mount only into the Inference owner, never API/Celery or model backends. The combined owner runtime requires this profile even on a CPU-only installation. |
| `GPU_ARBITER_RESOURCES_JSON` | `{}` | resource key 必须等于 node_id/physical_device_token；优先使用 GPU/MIG UUID。 只有部署明确固定索引映射时才使用 index:N。allocatable_mb 已扣除系统与安全余量。 例: {"gpu-node-a/GPU-xxx":{"node_id":"gpu-node-a","physical_device_token":"GPU-xxx","allocatable_mb":22000,"mode":"observe"}} |
| `GPU_ARBITER_ADMISSION_TIMEOUT_SECONDS` | `30` | 仅供 enforce authority 的 backend/card slow-path FIFO，独立于推理、build 与 drain timeout。 ticket 使用固定期限且不因轮询续期；驱逐会在期限内预留终态清理窗口；取值 1..3600 秒，off/observe 不入队。 backend/card 超时分别返回 gpu_backend_concurrency_saturated/gpu_capacity_unavailable，均为 503 + Retry-After。 |
| `GPU_ARBITER_RESIDENCY_COOLDOWN_SECONDS` | `30` | 新 residency 的最短保护窗口；只接受 1..3600 秒。未到期 victim 会在 exact card ticket 上按 admission deadline 与固定 ticket TTL 有界等待且不续期；预算耗尽返回 503 + Retry-After， 超时或取消会精确清理票据。 |
| `GPU_LIFECYCLE_SIGNING_KEYS_FILE` | `—` | Platform signer private seeds stay in a root-readable file, never in an env value. Strict JSON: {"kid":"unpadded-base64url-raw-32-byte-Ed25519-private-seed"}. The Inference owner reads it lazily for enforce authority/promotion. Never mount it into the API, ordinary workers or GPU backends. |
| `GPU_LIFECYCLE_ACTIVE_SIGNING_KID` | `—` | Select one exact kid from the private key file after backend public-key rollout. |

## ADR-0050 · ML Backend service-pool routing ledger

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ML_BACKEND_ROUTER_MODE` | `off` | Deployment-level single switch (off | observe | enforce). off/observe keep legacy single-instance dispatch; observe additionally computes would-select in a shadow namespace and records diagnostics without gating; enforce uses router-selected instances and fails closed on Redis/topology uncertainty. API/worker/beat read the same value; deploy must rebuild/restart as a unit, do not mix modes across processes. |
| `ML_BACKEND_ROUTER_HEALTH_MAX_AGE_SECONDS` | `90` | Health snapshot freshness gate: members whose last_checked_at is older than this are not eligible candidates (their capability may be stale). |
| `ML_BACKEND_ROUTER_LEASE_TTL_SECONDS` | `120` | Route lease TTL must exceed the normal transport timeout; long tasks heartbeat. |
| `ML_BACKEND_ROUTER_HEARTBEAT_INTERVAL_SECONDS` | `15` | — |
| `ML_BACKEND_ROUTER_PASSIVE_FAILURE_THRESHOLD` | `3` | Passive circuit: consecutive transport failures before ejection + open duration. |
| `ML_BACKEND_ROUTER_EJECT_SECONDS` | `30` | — |

## yolo-backend (ultralytics 多任务 det/seg/pose/obb)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `YOLO_DEVICE` | `cuda:0` | profile gpu-yolo · 与 grounded-sam2 / sam3 三 backend 独立启停 |
| `YOLO_MODEL_POOL_CAP` | `2` | — |
| `YOLO_MANAGED_MODELS_TOKEN` | `—` | Controlled model installation/prediction credential; at least 32 random characters. Configure the same token on the platform's YOLO inference registry. Empty disables this path. |
| `YOLO_BUILD_TIMEOUT` | `30` | — |
| `YOLO_IDLE_UNLOAD_SECONDS` | `600` | — |
| `YOLO_IDLE_CHECK_INTERVAL` | `60` | — |
| `STRICT_OFFLINE` | `1: checkpoints/ 缺权重直接返 400, 不去 GH release 下载.` | — |
| `YOLO_STRICT_OFFLINE` | `0` | — |
| `YOLO_MANAGED_LIFECYCLE_VERIFIED` | `0` | 仅在当前部署完成 YOLO 多模型池真实 GPU load → full unload → baseline 验收后设为 1。 只接受字面量 0 或 1；其他值拒绝启动。未验证时 /setup 不发布 managed_lifecycle，且拒绝切入 enforce gate。 |
| `YOLO_LOG_LEVEL` | `INFO` | — |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON` | `—` | Managed GPU lifecycle Ed25519 public-key ring (kid -> unpadded base64url key). Empty keeps the backend in legacy-compatible mode; a non-empty invalid value fails startup. |

## ONNXTools 固定句柄池与受管生命周期

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ONNXTOOLS_DET_MODEL` | `rtdetr-2024080100.onnx` | 检测模型文件名。 |
| `ONNXTOOLS_VA_MODEL` | `va_260612.onnx` | ONNXTools 车辆属性分类模型文件名。 |
| `ONNXTOOLS_CONF_THRES` | `0.5` | ONNXTools 检测置信度阈值。 |
| `ONNXTOOLS_BUILD_TIMEOUT` | `30` | 调用方等待冷启动的秒数；超时后真实 builder 仍受跟踪。 |
| `ONNXTOOLS_IDLE_UNLOAD_SECONDS` | `600` | 全池空闲卸载阈值（秒）；非正数关闭。 |
| `ONNXTOOLS_IDLE_CHECK_INTERVAL` | `60` | 空闲检查周期（秒）。 |
| `ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED` | `0` | 仅在当前部署完成真实 GPU 四 session warmup → full unload → baseline 验证后设为 1；只接受字面量 0 或 1。 |
| `ONNXTOOLS_LOG_LEVEL` | `INFO` | ONNXTools 日志级别。 |

## RapidOCR 动态 composite 引擎池与受管生命周期

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RAPIDOCR_DEVICE` | `gpu` | 构造设备：gpu 优先 CUDAExecutionProvider，cpu 只构造 CPU session。 |
| `RAPIDOCR_POOL_CAP` | `3` | composite 引擎数上限；每个引擎固定持有 det/cls/rec 三个 ORT session。 |
| `RAPIDOCR_BUILD_TIMEOUT` | `30` | 调用方等待冷启动的秒数；超时后真实 builder 仍受跟踪。 |
| `RAPIDOCR_IDLE_UNLOAD_SECONDS` | `600` | 整池空闲卸载阈值（秒）；非正数关闭。 |
| `RAPIDOCR_IDLE_CHECK_INTERVAL` | `60` | 空闲检查周期（秒）。 |
| `RAPIDOCR_MANAGED_LIFECYCLE_VERIFIED` | `0` | 仅在当前镜像、权重与物理 GPU 的满池实卡验收通过后设为 1；只接受字面量 0 或 1。 |
| `RAPIDOCR_LOG_LEVEL` | `INFO` | RapidOCR 日志级别。 |

## Prometheus http_sd 服务发现端点 /api/v1/internal/metrics-targets 的可选 bearer token。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `METRICS_SD_TOKEN` | `—` | 默认空 = 免鉴权 (内网端点, 靠 nginx /internal 网段隔离, 与 /metrics 一致); 设为非空时该端点校验请求头 Authorization: Bearer TOKEN, 不匹配返回 401。 |

## 视频帧服务

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MEDIA_PROBE_URL` | `—` | 独立素材探测服务地址；空值使用当前进程内实现。仅显式配置后转发，无远程失败回退。 |
| `MEDIA_PROBE_TOKEN` | `—` | 独立服务的专用 bearer token（至少 32 字符），只在启用远程探测时必填。 |
| `MEDIA_PROBE_TOKEN_FILE` | `—` | 独立媒体执行进程可用 secret 文件替代 MEDIA_PROBE_TOKEN；二者不要同时传入。 主 API 网关仍使用上面的实际 token 值；Compose 文件路径需由自有 override 挂载。 |
| `MEDIA_PROBE_TIMEOUT_SECONDS` | `90` | HTTP 各 I/O 阶段超时秒数，包含上传／等待／读取；ffprobe 自身仍有独立的 30 秒上限。 |
| `MEDIA_PROBE_MAX_FILE_BYTES` | `1073741824` | 独立探测服务单次文件输入上限（字节），仅由 apps/media-probe 读取。 |
| `MEDIA_PROBE_HOST_PORT` | `8092` | docker-compose.modules.yml 的宿主回环端口，仅供显式本地独立部署。 |
| `ASSETS_CATALOG_URL` | `—` | 独立 Assets 目录服务：留空时目录、关联和空间素材操作使用同一原生 SQL 合同；显式选远程后不自动回退。 |
| `ASSETS_CATALOG_TIMEOUT_SECONDS` | `5` | 网关 HTTP 各 I/O 阶段超时；范围 (0,60] 秒。 |
| `ASSETS_CATALOG_TOKEN` | `—` | API 网关与目录服务共享专用 Bearer secret，至少 32 个非空白 ASCII 字符。 |
| `ASSETS_CATALOG_TOKEN_FILE` | `—` | 独立目录服务的 secret 文件入口；主 API 网关使用上面的实际 token 值。 |
| `ASSETS_CATALOG_DATABASE_URL` | `—` | 独立目录服务专用 execute-only LOGIN，不使用 API DATABASE_URL。 |
| `ASSETS_CATALOG_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_CATALOG_S3_ACCESS_KEY` | `—` | 专用只读 S3 身份；不得沿用 API/MinIO 管理员凭据。 |
| `ASSETS_CATALOG_S3_SECRET_KEY` | `—` | — |
| `ASSETS_CATALOG_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_CATALOG_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_CATALOG_S3_PUBLIC_ENDPOINT` | `—` | 固定公开签名 origin，必须能由浏览器到达；不从请求接收 endpoint。 |
| `ASSETS_CATALOG_S3_ENDPOINT` | `—` | 服务连接 S3 的固定 origin；留空沿用公开 origin，支持容器内外地址不同。 |
| `ASSETS_CATALOG_S3_REGION` | `us-east-1` | — |
| `ASSETS_CATALOG_DATASETS_BUCKET` | `—` | — |
| `ASSETS_CATALOG_MEDIA_CACHE_BUCKET` | `—` | — |
| `ASSETS_CATALOG_HOST` | `0.0.0.0` | 独立服务进程监听；Compose 只在内部网络开放端口。 |
| `ASSETS_CATALOG_PORT` | `8766` | — |
| `ASSETS_CATALOG_MAX_REQUEST_BYTES` | `1048576` | 独立服务与网关 HTTP JSON 请求上限（1 MiB）；内部同事务计划不套用 HTTP 体积限制。 |
| `ASSETS_CATALOG_MAX_RESPONSE_BYTES` | `16777216` | 独立服务原生回执/HTTP 响应上限（16 MiB）；网关本地与远程单页响应同为 16 MiB。 |
| `ASSETS_CATALOG_POOL_MIN_SIZE` | `1` | 专用数据库连接池及获取连接/SQL 的秒级等待上限。 |
| `ASSETS_CATALOG_POOL_MAX_SIZE` | `8` | — |
| `ASSETS_CATALOG_POOL_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_CATALOG_SQL_TIMEOUT_SECONDS` | `5` | — |
| `ASSET_CLEANUP_DATABASE_URL` | `—` | 独立 Assets 清理进程；不继承 API 的 DATABASE_URL、MINIO_* 或 Celery 配置。 宿主进程可直接设置 ASSET_CLEANUP_DATABASE_URL / S3_ACCESS_KEY / S3_SECRET_KEY； 每项与对应 *_FILE 二选一。Compose 的 *_FILE 是宿主 secret 文件路径。 |
| `ASSET_CLEANUP_S3_ACCESS_KEY` | `—` | — |
| `ASSET_CLEANUP_S3_SECRET_KEY` | `—` | — |
| `ASSET_CLEANUP_DATABASE_URL_FILE` | `—` | — |
| `ASSET_CLEANUP_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSET_CLEANUP_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSET_CLEANUP_BUCKET` | `—` | — |
| `ASSET_CLEANUP_INPUTS_BUCKET` | `accepted-inputs` | 独立进程须与 MINIO_INPUTS_BUCKET 完全一致；Compose overlay 从 MINIO_INPUTS_BUCKET 取值。 |
| `ASSET_CLEANUP_S3_ENDPOINT` | `—` | — |
| `ASSET_CLEANUP_S3_REGION` | `us-east-1` | — |
| `ASSET_CLEANUP_POLL_SECONDS` | `5` | — |
| `ASSET_CLEANUP_PAGE_SIZE` | `100` | — |
| `ASSET_CLEANUP_HEALTH_HOST` | `127.0.0.1` | 健康接口仅绑定进程内 IPv4 回环；独立镜像使用此默认值。 |
| `ASSET_CLEANUP_HEALTH_PORT` | `8765` | — |
| `VIDEO_CHUNK_SIZE_FRAMES` | `60` | 每个 chunk 包含的帧数。30fps 下默认 60 帧约等于 2 秒。 |
| `VIDEO_CHUNK_WARMUP_LOOKAHEAD` | `1` | chunk warmup look-ahead：请求命中 chunk N 时顺带预解码 N+1..N+K。默认 1 仅预热紧邻的下一个 chunk，设 0 关闭。 |
| `VIDEO_FRAME_CACHE_TTL_DAYS` | `14` | 单帧 WebP/JPEG 缓存对象未访问多少天后由 Celery beat 清理。 |
| `VIDEO_CHUNK_CACHE_TTL_DAYS` | `30` | 视频 chunk 缓存对象未访问多少天后由 Celery beat 清理。 |
| `VIDEO_FRAME_MEMORY_CACHE_ITEMS` | `64` | AI worker 通过内部 frame_service.get_frame_array 读取单帧时的进程内 LRU 上限。 |
| `VIDEO_SEGMENT_SIZE_FRAMES` | `18000` | 每个协作 segment 包含的帧数。30fps 下默认 18000 帧约等于 10 分钟。 |
| `VIDEO_SEGMENT_LOCK_TTL_SECONDS` | `300` | segment claim/heartbeat 锁 TTL，单位秒。 |
| `VIDEO_TRACKER_WINDOW_SIZE_FRAMES` | `300` | AI tracker 调 ML Backend 时单次请求最多覆盖的帧数；长区间会由 worker 自动分窗，降低 GPU OOM 风险。 |
| `VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES` | `16` | SAM3 视频 tracker（sam3_video 文本 multiplex / sam3_video_interactive PVS）专用的更小分窗；视频前向显存随窗口近似线性增长（文本 multiplex 实测 ~0.85GB/帧、16 帧 ~18.9GB、41 帧即 OOM@24GB）。仅对 SAM3 系生效；不动 sam2 的窗口。24GB 卡若与图像 sam3.pt 常驻并存建议再降到 ~12。 |
| `VIDEO_TRACKER_LOW_CONFIDENCE_OUTSIDE_THRESHOLD` | `0.15` | AI tracker 返回 confidence 低于该阈值时，后端按 outside prediction range 写回而不是生成 keyframe。 |
| `TRACKER_SOFT_TIME_LIMIT_SECONDS` | `1800` | detect-then-track 批量预标注 tracker 阶段的 soft 超时（秒）。tracker 整段跑帧耗时远超逐帧检测，与 YOLO_TRACKER_MAX_FRAMES 帧上限双保险，防单个追踪 job 长时间占住 worker；仅对含 tracker 阶段的 job 施加。 |
| `FRAME_PREANNOTATE_MAX_FRAMES` | `900` | 单帧分支批量逐帧预标注（execution_unit=frame）：每个视频 task 逐帧的帧数上限（对齐 YOLO_TRACKER_MAX_FRAMES），超限截断，防长视频 × 全帧 × 多框砸库。 |
| `FRAME_PREANNOTATE_CHUNK_SIZE` | `30` | 逐帧预标注每个 Celery 段子任务处理的帧数（fan-out 粒度：太小则子任务过多、太大则并行差）。 |
| `FRAME_PREANNOTATE_MAX_BOXES_PER_FRAME` | `100` | 逐帧预标注单帧落库框数上限（防单帧检出爆量）。 |
| `FRAME_PREANNOTATE_SEGMENT_SOFT_TIME_LIMIT_SECONDS` | `900` | 逐帧预标注段子任务的 soft 超时（秒，段内逐帧跑图像 predict）。 |

## 图片原生栅格 Mask

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RASTER_MASK_READ_ENABLED` | `true` | 图片原生 raster_mask reader 默认开启，保证新后端可读取已有 geometry。 |
| `RASTER_MASK_CREATE_ENABLED` | `true` | 持久化创建与项目原生编辑均默认开启；本项保留为部署紧急总闸，项目仍可单独关闭。 |

## 前端 Raster Mask 实验计算

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_EXPERIMENTAL_RASTER_MASK_WEBGPU` | `true` | Vite build-time 开关。默认 true，但只在大 ROI morphology 请求时惰性探测浏览器 adapter。 true 只允许符合 ROI、设备档位和字节预算的 square dilate 候选 WebGPU；其余操作及任何 adapter/device 故障都在客户端 Raster Mask Worker 内精确回退 CPU。使用的是访问页面的 用户浏览器 GPU，不是 Linux API、Celery 或部署机器 GPU。设为 false 可紧急回滚；修改后必须重建前端镜像。 |

## 前端超大图 Tile

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` | `true` | Vite build-time 开关。默认 true：有 ready pyramid 的图片只请求 overview 与当前视口切片， required 大图在生成中/失败时不会自动下载整张原图。背景继续由浏览器解码 + Konva Canvas2D 绘制，不依赖 WebGPU。设为 false 可紧急停止选择切片路径；修改后必须重建前端镜像。 |

## 认证 / 安全

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SECRET_KEY` | `change-this-to-a-random-string-in-production` | JWT 签名密钥；生产环境必须替换为高强度随机字符串（≥32 字符） |

## 存储连接器（外部 S3 / SFTP 服务端拉取）凭据 Fernet key，与 SECRET_KEY 隔离。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CONNECTOR_ENCRYPTION_KEY` | `—` | 本地 API 模式留空时，创建或解密凭据的连接器操作不可用；远程 Source 模式由 Source 服务配置同一 key。 生成: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" |
| `CONNECTOR_HOST_ALLOWLIST` | `—` | 存储连接器主机白名单部署默认值（CIDR/IP/域名，CSV 或 JSON 数组）。 超管通过 /storage-connections/allowlist 写入 DB 后会覆盖该默认值。 本地连接宿主机 SFTP 示例: CONNECTOR_HOST_ALLOWLIST=172.17.0.1/32,172.26.1.17/32 |
| `CONNECTOR_DEPLOYMENT_SFTP_HOST` | `—` | 可选：超级管理员「添加部署主机」快捷项预填的 SFTP hostname/IP；留空不展示。 只配置地址，不包含凭据、路径或白名单规则。目标仍须显式加入 CONNECTOR_HOST_ALLOWLIST。 |
| `DATASET_IMPORT_MAX_FILES` | `50000` | 连接器导入单个 job 最多扫描 / 导入的文件数；超限直接失败，避免误扫全桶。 |
| `DATASET_IMPORT_MAX_TOTAL_BYTES` | `214748364800` | 连接器导入单个 job 允许的总字节数；默认 200GiB。 |
| `MASK_FORMAT_TEMP_QUOTA_BYTES` | `8589934592` | Mask 格式导入 / 导出单 job 临时空间上限；写入时按实际字节持续复核。 |
| `MASK_FORMAT_MAX_ENTRY_BYTES` | `536870912` | 安全 archive 单 entry 展开字节、文件数与压缩比上限。 |
| `MASK_FORMAT_MAX_ARCHIVE_FILES` | `200000` | — |
| `MASK_FORMAT_MAX_COMPRESSION_RATIO` | `100` | — |
| `TASK_CREATE_SYNC_THRESHOLD` | `2000` | dataset link 建 task 同步阈值：dataset item 数 ≤ 阈值走同步快路径， > 阈值改入 Celery 异步建 task（带进度），避免大 dataset link 在 HTTP 单事务里超时 + 长事务锁。 |

## 是否允许开放注册

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ALLOW_OPEN_REGISTRATION` | `false` | true  — 任何人可自行注册，默认获得 viewer 角色 false — 仅管理员可创建账号 |
| `MAX_INVITATIONS_PER_DAY` | `30` | 每位管理员滚动 24 小时最多创建的邀请数（后台系统设置可覆盖；0 不表示无限）。 |
| `OFFLINE_THRESHOLD_MINUTES` | `5` | 在线状态扫描把无心跳用户标记为 offline 的时间窗口（分钟；后台系统设置可覆盖）。 |

## 开放注册是否强制邮箱验证

| 变量 | 默认值 | 说明 |
|---|---|---|
| `REQUIRE_EMAIL_VERIFICATION` | `—` | 留空  — 按环境派生：production 默认开、development/staging 默认关 true  — 注册后须点邮件链接验证才能登录（验证前不可登录；邀请注册恒视为已验证） false — 注册即可登录（与历史行为一致） |

## Cloudflare Turnstile CAPTCHA

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TURNSTILE_ENABLED` | `false` | dev/CI 留默认 false；production 放量前在控制台申请 site key + secret key 后启用： https://dash.cloudflare.com/?to=/:account/turnstile 启用后注册（/auth/register-open）与忘记密码（/auth/forgot-password）必须携带 captcha_token， 后端向 challenges.cloudflare.com/turnstile/v0/siteverify 校验失败返 400 captcha_failed。 |
| `TURNSTILE_SITE_KEY` | `# 前端 widget 的 sitekey（同时设置 VITE_TURNSTILE_SITE_KEY）` | — |
| `TURNSTILE_SECRET_KEY` | `# 后端 siteverify 用的 secret，绝不可暴露给前端` | — |

## 审计日志冷数据保留月数

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_RETENTION_MONTHS` | `12` | Celery beat 每月 2 日扫描超期分区，归档为 jsonl.gz 上传 MinIO `audit-archive/{YYYY}/{MM}.jsonl.gz`， 然后 DROP 该分区。默认 12 个月。 |

## 错误监控 (Sentry)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SENTRY_DSN` | `—` | 后端 Sentry DSN；留空则禁用后端错误上报 |
| `SENTRY_ENVIRONMENT` | `development` | Sentry 环境标签，用于区分 development / staging / production |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.1` | 性能追踪采样率，0.0 ~ 1.0（0.1 = 采样 10% 的请求） |
| `VITE_SENTRY_DSN` | `—` | 前端 Sentry DSN（Vite 构建时注入）；留空则禁用前端错误上报 |

## 前端 Turnstile sitekey；留空时注册页不渲染 widget。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VITE_TURNSTILE_SITE_KEY` | `—` | — |

## SMTP 邮件

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SMTP_HOST` | `localhost` | 留空则 GET /settings/system 显示「未配置」，发送测试邮件按钮报错。 dev 推荐用 docker-compose 内置的 mailpit 收件箱（不外发真实邮件）： docker compose up -d mailpit  → Web UI http://localhost:8025 API 跑 host 时连 localhost:1025；API 也进 Docker 时把 SMTP_HOST 改成 mailpit。 |
| `SMTP_PORT` | `1025` | — |
| `SMTP_FROM` | `noreply@local.test` | — |
| `SMTP_USER` | `—` | — |
| `SMTP_PASSWORD` | `—` | — |

## 跨域 (CORS)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `["https://app.example.com","https://admin.example.com"]` | 允许的前端来源列表，支持两种格式： JSON 数组：["https://app.example.com","https://admin.example.com"] 逗号分隔：https://app.example.com,https://admin.example.com production 环境必填；dev/staging 留空则默认放行 localhost 常用端口 |
| `CORS_ALLOW_ORIGINS` | `https://app.example.com,https://admin.example.com` | — |

## 来源正则匹配（仅 dev/staging 生效，production 自动忽略以防误放本机正则上线）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CORS_ALLOW_ORIGIN_REGEX` | `http://localhost:\d+` | — |

## Production request-header allowlist; keep Idempotency-Key for repeatable unlink requests.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CORS_ALLOW_HEADERS` | `["Authorization","Content-Type","X-Request-ID","Idempotency-Key"]` | Allowed cross-origin clients can read the echoed Idempotency-Key response header. |

## Grounded-SAM-2 ML Backend

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SAM_VARIANT` | `tiny` | 仅当启用叠加文件 docker-compose.ml.yml 的 --profile gpu 拉起 grounded-sam2-backend 时生效. 模型变体 (按精度/显存递增): tiny | small | base_plus | large; 默认 tiny (4060 友好). |
| `DINO_VARIANT` | `T` | GroundingDINO 变体: T (Swin-T, 默认) | B (Swin-B, 更准但显存翻倍). |
| `BOX_THRESHOLD` | `0.35` | DINO 检测阈值; 业务图召回不足可下调到 0.25, 误检多则上调到 0.45. |
| `TEXT_THRESHOLD` | `0.25` | DINO 文本-标签匹配阈值; 短语 prompt 通常 0.25 即可. |
| `GSAM2_LOG_LEVEL` | `INFO` | Backend 日志级别 (DEBUG / INFO / WARNING). |
| `IDLE_UNLOAD_SECONDS` | `600` | B-28+ · 空闲多少秒后自动卸载模型释放显存 (默认 600s); <=0 关闭定时卸载, 仍可手动 /unload. |
| `IDLE_CHECK_INTERVAL` | `60` | B-28+ · idle 检查器轮询间隔 (默认 60s). |
| `MODEL_POOL_CAP` | `1` | ModelPool 同容器内并存的变体数上限 (LRU 驱逐). 显存预算: 4060(8G) 用 1, 3090(24G) 1-2, A100 2-4. 默认 1 = 维持单变体常驻行为, 切变体走"驱逐旧+冷启新". |
| `MODEL_POOL_BUILD_TIMEOUT` | `30` | pool 满 + 并发 miss 时排队等待显存的超时 (秒), 超时返回 503 "显存繁忙". |
| `PREFETCH_SAM_VARIANTS` | `tiny,small,base_plus,large` | entrypoint 启动时额外预拉的变体 checkpoint (主变体之外). pool 能服务多变体, 但只有这里声明 (+ 主变体) 的 checkpoint 会落盘; 运行期请求未预拉的变体会 503. 逗号分隔. 默认全量, 让 pool 任意切换不踩缺失; 磁盘紧张时裁剪 (大致 tiny~150M/small~180M/base_plus~320M/large~900M, SwinB~940M). |
| `PREFETCH_DINO_VARIANTS` | `T,B` | — |
| `VIDEO_MODEL_POOL_CAP` | `1` | sam2_video tracker 独立显存池 (与图片池预算分离, 互不驱逐). 同容器内并存的 video 变体上限 (LRU); 默认 1. |
| `VIDEO_MODEL_POOL_BUILD_TIMEOUT` | `60` | video 池满 + 并发 miss 排队等显存的超时 (秒), 超时 503; video build 比图片慢, 默认 60. |
| `VIDEO_TRACKER_MAX_WINDOW_FRAMES` | `300` | 单次 init_state 一次性加载的最大帧数 (安全上限, 防超长窗口灌爆显存); 超此值的窗口拒绝. |
| `VIDEO_TRACKER_MAX_CONTEXT_FRAMES` | `300` | 同一 SAM2 predictor session 可覆盖的最大源帧数。大于 window 才启用跨窗 context；提高前需按目标 GPU 验收峰值。 |
| `VIDEO_IDLE_UNLOAD_SECONDS` | `600` | video 池独立 idle 卸载 (与图片池 IDLE_UNLOAD_SECONDS 各自计时); <=0 关闭. |
| `GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED` | `0` | 仅在 Grounded-SAM2 完成实卡 image/video/双池/full-unload 验收后设为 1。 只接受字面量 0 或 1；其他值拒绝启动。未验证时 /setup 不发布 managed_lifecycle，且拒绝切入 enforce gate。 |

## SAM 3 ML Backend

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HF_TOKEN` | `hf_xxxxxxxxxxxx` | ⚠️ HF_TOKEN 必填: facebook/sam3 (图像 PCS + inst) 与 facebook/sam3.1 (视频权重, 预留) 均为 gated repo, 必须先分别在 HuggingFace 接受 license (https://huggingface.co/facebook/sam3 与 https://huggingface.co/facebook/sam3.1), 再创建 read-only token (https://huggingface.co/settings/tokens) 填到这里. |
| `SAM3_IMAGE_HF_REPO_ID` | `facebook/sam3` | 图像模型 (实际 /predict 加载) 仓库与权重文件; 默认 sam3.pt (3.0, 官方 image+inst 路径). |
| `SAM3_IMAGE_CHECKPOINT_FILE` | `sam3.pt` | — |
| `SAM3_DOWNLOAD_VIDEO` | `1` | 视频 multiplex 仓库与权重文件; 容器启动时默认一并拉取 (~3.2GB, 需 facebook/sam3.1 license). 设为 0 可关闭 (仅用图像交互, 不需要 sam3.1 license). |
| `SAM3_HF_REPO_ID` | `facebook/sam3.1` | — |
| `SAM3_CHECKPOINT_FILE` | `sam3.1_multiplex.pt` | — |
| `SAM3_EMBEDDING_CACHE_SIZE` | `32` | Embedding cache LRU 容量; A100 充裕可调到 64, 4060 别部 sam3. |
| `SAM3_SCORE_THRESHOLD` | `0.5` | SAM 3 PCS text / exemplar 路径 score 过滤阈值; 召回不足下调到 0.3, 误检多调到 0.6. |
| `SAM3_LOG_LEVEL` | `INFO` | Backend 日志级别 (DEBUG / INFO / WARNING). |
| `SAM3_IDLE_UNLOAD_SECONDS` | `600` | 空闲 N 秒后自动卸载模型释放显存 (sam3 开 inst FP16 ~5.8GB, 与 grounded-sam2 并存强烈建议保留); <=0 关闭定时卸载, 仍可通过 POST /unload 手动卸载. 下次 /predict 自动懒重载 (冷启动 ~8-12s). |
| `SAM3_IDLE_CHECK_INTERVAL` | `60` | idle 检查器轮询间隔 (默认 60s). |
| `SAM3_MODEL_POOL_BUILD_TIMEOUT` | `120` | 三个模型池等待冷构建的超时 (秒); 超时后真实 builder 仍由 backend 跟踪至结束。 |
| `SAM3_PVS_MAX_WINDOW_FRAMES` | `16` | SAM3 interactive PVS 单窗与同一 session context 的帧数上限。context 大于 window 才启用跨窗续追。 |
| `SAM3_PVS_MAX_CONTEXT_FRAMES` | `16` | — |
| `SAM3_MANAGED_LIFECYCLE_VERIFIED` | `0` | 仅在当前部署完成 image/multiplex/PVS 真实推理与全池显存回落验收后设为 1。 只接受字面量 0 或 1；其他值拒绝启动。未验证时 /setup 不发布 managed_lifecycle，且拒绝切入 enforce gate。 |

## ML Backend GPU 分卡 (多卡机器可选)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `GSAM2_GPU_DEVICE_ID` | `0` | 各 GPU profile backend 固定绑定的物理显卡号 (docker-compose.ml.yml 的 deploy.reservations.devices.device_ids，并由 Compose 复用为容器内物理卡身份)。 NVIDIA runtime 可能把 PID 1 的 NVIDIA_VISIBLE_DEVICES 重写为 void；后端会 报告 Compose 从同一 *_GPU_DEVICE_ID 派生的 token，不用逻辑 cuda:0 猜测物理卡。 默认 GSAM2/YOLO/ONNXTOOLS/RAPIDOCR 占卡 0、SAM3 占卡 1, 双卡机器可错开显存。 单卡机器必须把 SAM3_GPU_DEVICE_ID 覆盖成 0, 否则容器找不到卡 1 起不来。 每个 backend 只允许一个物理 GPU/MIG；0,1、多 UUID 或已暴露 GPU 的 all 会在启动时拒绝。 |
| `SAM3_GPU_DEVICE_ID` | `1` | — |
| `YOLO_GPU_DEVICE_ID` | `0` | — |
| `ONNXTOOLS_GPU_DEVICE_ID` | `0` | — |
| `RAPIDOCR_GPU_DEVICE_ID` | `0` | — |

## DuckDB 离线分析视图

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DUCKDB_PATH` | `./data/duckdb/analytics.duckdb` | Celery worker 每日 02:30 UTC 增量同步 task_events + audit_logs 到这个 DuckDB 文件; FastAPI /admin/analytics 端点以 read_only 模式读取它出固定面板. Docker 部署: worker 容器把 host ./data/duckdb 挂到 /var/lib/duckdb; 本地 API 进程跑 host 时直接读 host 文件 (单 writer 多 reader). PostgreSQL *_test / *_e2e 库未显式配置时，自动使用当前 checkout 下 .artifacts/analytics/数据库标识哈希.duckdb；测试 seed 与 API 必须 使用同一 checkout 和数据库。显式路径优先，但截图 seed 拒绝共享开发默认文件。 |

## 部署环境

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ENVIRONMENT` | `development` | 当前运行环境，影响 CORS 策略、日志级别、调试开关等 可选值：development | staging | production |

## 独立局域网生产栈专用：只放入 .env.production，使用 docker-compose.lan-prod.yml。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LAN_BIND_IP` | `192.168.1.20` | 固定局域网网卡地址；3030 和 8080 都由 Caddy 提供 HTTPS。 |
| `AAP_SHARED_NETWORK` | `ai-annotation-platform_default` | 现有 PostgreSQL / MinIO / 模型服务所在 Docker 网络。 |
| `AAP_IMAGE_TAG` | `local-production` | 应用镜像的源代码标识；每次部署保留旧标识用于回退。 |
| `AAP_PRODUCTION_STATE_DIR` | `/srv/aap-production` | 权限 0700 的本机目录；migration-database-url 文件只提供给一次性迁移服务。 |

## Playwright 专用 seed/login/cleanup 路由总闸，默认关闭。开启后仍只允许连接

| 变量 | 默认值 | 说明 |
|---|---|---|
| `E2E_SEED_ENABLED` | `false` | 名称以 _e2e 或 _test 结尾的数据库；这些端点会创建和删除固定测试数据， 绝不要在开发库、staging 或 production 数据库上开启。 |

## Docker 卷存储位置 (可选)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_ROOT` | `/srv/annotation-data` | 默认 docker compose 用 Docker 托管的命名卷 (落在 /var/lib/docker/volumes)。 想把所有命名卷 (pgdata/minio/模型权重/监控) 集中到宿主机统一目录便于 备份/迁移/指定磁盘, 叠加 docker-compose.hostvols.yml 并设 DATA_ROOT: export DATA_ROOT=/srv/annotation-data   # 绝对路径, 子目录须先 mkdir docker compose -f docker-compose.yml -f docker-compose.hostvols.yml up -d 想固化成默认 (免每次敲 -f), 取消下面两行注释: |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.hostvols.yml` | — |

## Video capture uses process-local settings, not persistent application defaults.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MARKETING_HEADLESS_GPU` | `—` | 无头 NVIDIA 母版录制可选：指定 nvidia-smi 报告的 PCI bus ID（例如 00000000:73:00.0）；默认选第一张 GPU。仅在采集进程中设置。 |
| `MARKETING_FFMPEG` | `—` | 无头录制可选：指定支持 NVENC、libx264、x11grab 的 ffmpeg；默认验证 PATH，再尝试 /usr/bin。 |
| `MARKETING_FFPROBE` | `—` | 无头录制可选：指定 ffprobe；默认使用所选 ffmpeg 同目录中的 ffprobe。 |

## 工作树运行时内部标识

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AAP_WORKTREE_MODE` | `—` | 由 pnpm dev:worktree 向子进程注入 dev/test/e2e；不要手工配置。 pytest / Playwright 据此拒绝在 dev 模式运行破坏性测试；测试连接、队列和存储也由入口覆盖。 |

## Identity 认证与账号恢复（可选独立进程，不进入默认 Compose）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IDENTITY_URL` | `—` | 空 URL 使用同一个内嵌验证器；显式远程失败不回退。原角色/项目/scope检查继续由业务入口执行。 |
| `IDENTITY_TOKEN` | `—` | — |
| `IDENTITY_TIMEOUT_SECONDS` | `20` | — |
| `IDENTITY_AUTH_DATABASE_URL` | `—` | 可选函数专用准入 LOGIN；两项二选一，使用 postgresql://，留空沿用当前应用连接。 |
| `IDENTITY_AUTH_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_KEY_DATABASE_URL` | `—` | 内嵌 API key 命令可用独立的两项函数 LOGIN；每对 DSN / 文件最多一项。 |
| `IDENTITY_KEY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_SESSION_DATABASE_URL` | `—` | 内嵌核心会话命令可用独立三函数 LOGIN；登录计数复用 REDIS_URL。 |
| `IDENTITY_SESSION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_ACCOUNT_DATABASE_URL` | `—` | 自助账户命令的三函数 LOGIN，与单函数在线扫描 LOGIN 分开授权。 |
| `IDENTITY_ACCOUNT_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_PRESENCE_DATABASE_URL` | `—` | — |
| `IDENTITY_PRESENCE_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_GROUP_DATABASE_URL` | `—` | 内嵌 API 分组目录／指派可选 GROUP 两函数 LOGIN；留空使用当前 caller transaction。 |
| `IDENTITY_GROUP_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_DIRECTORY_DATABASE_URL` | `—` | User directory reads use an optional two-function Identity LOGIN; leave empty for the caller transaction. |
| `IDENTITY_DIRECTORY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_ADMIN_PASSWORD_DATABASE_URL` | `—` | Administrator password reset uses its own optional two-function Identity LOGIN. |
| `IDENTITY_ADMIN_PASSWORD_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_PLATFORM_ROLE_DATABASE_URL` | `—` | Platform-role previews and changes use a separate optional two-function LOGIN. |
| `IDENTITY_PLATFORM_ROLE_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_SUSPENSION_DATABASE_URL` | `—` | Administrator suspension and reactivation use an optional two-function LOGIN. |
| `IDENTITY_SUSPENSION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_DELETION_DATABASE_URL` | `—` | Administrator soft deletion uses an optional two-function LOGIN. |
| `IDENTITY_DELETION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_OFFBOARDING_DATABASE_URL` | `—` | Offboarding preview and commit use an optional three-function Identity LOGIN. |
| `IDENTITY_OFFBOARDING_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_BRIEFS_DATABASE_URL` | `—` | Optional function-only profile reads; empty uses the API caller transaction. |
| `IDENTITY_BRIEFS_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_INVITATION_DATABASE_URL` | `—` | Complete Identity invitation lifecycle uses an optional three-function owner; mail-v2 is a separate optional four-function sender. |
| `IDENTITY_INVITATION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_MAIL_V2_DATABASE_URL` | `—` | — |
| `IDENTITY_MAIL_V2_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_INVITATION_MAIL_TIMEOUT_SECONDS` | `60` | — |
| `IDENTITY_INVITATION_FRONTEND_BASE_URL` | `http://localhost:5173` | — |
| `IDENTITY_DEACTIVATION_DATABASE_URL` | `—` | 自助注销用户命令与到期 operator 命令使用不同能力；每对 DSN／文件最多设置一项。 |
| `IDENTITY_DEACTIVATION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL` | `—` | — |
| `IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL_FILE` | `—` | — |
| `NOTIFICATION_PUBLISH_DATABASE_URL` | `—` | API/Celery durable-notification relay 的可选 Notifications DSN；Redis 复用现有 Celery task 设置。 |
| `NOTIFICATION_PUBLISH_DATABASE_URL_FILE` | `—` | — |
| `NOTIFICATION_PUBLISH_REDIS_URL` | `—` | 独立 notification publisher 的 Redis DSN；与可选 Notifications 数据库 DSN 成对配置。 |
| `NOTIFICATION_PUBLISH_REDIS_URL_FILE` | `—` | — |
| `NOTIFICATION_PUBLISH_API_TOKEN` | `—` | 同一独立 publisher 的可选即时发布 HTTP；没有 API token 时保持原 durable-only 循环。 |
| `NOTIFICATION_PUBLISH_API_TOKEN_FILE` | `—` | — |
| `NOTIFICATION_PUBLISH_HOST` | `127.0.0.1` | — |
| `NOTIFICATION_PUBLISH_PORT` | `8181` | — |
| `NOTIFICATION_PUBLICATION_URL` | `—` | API 的可选独立即时发布绑定；空 URL 使用内嵌 transport，显式地址故障不回退。 |
| `NOTIFICATION_PUBLICATION_TOKEN` | `—` | — |
| `NOTIFICATION_PUBLICATION_TOKEN_FILE` | `—` | — |
| `NOTIFICATION_SOCKET_URL` | `—` | 独立 Notifications WebSocket 的可选 API 绑定；留空时使用同一 owner 的内嵌装配。 配置私网 ws://notifications-socket:8180 后，故障不会回退本地数据库／Redis。 |
| `NOTIFICATION_SOCKET_DATABASE_URL` | `—` | 独立 socket 的七函数只读 DSN、subscribe-only Redis 和 Identity admission-only bearer。 直接启动可用值或文件（二选一）；Compose 只使用文件且不发布宿主机端口。 |
| `NOTIFICATION_SOCKET_DATABASE_URL_FILE` | `—` | — |
| `NOTIFICATION_SOCKET_REDIS_URL` | `—` | — |
| `NOTIFICATION_SOCKET_REDIS_URL_FILE` | `—` | — |
| `NOTIFICATION_SOCKET_IDENTITY_URL` | `http://identity-admission:8170` | — |
| `NOTIFICATION_SOCKET_IDENTITY_TOKEN` | `—` | — |
| `NOTIFICATION_SOCKET_IDENTITY_TOKEN_FILE` | `—` | — |
| `NOTIFICATION_SOCKET_HOST` | `127.0.0.1` | — |
| `NOTIFICATION_SOCKET_PORT` | `8180` | — |
| `IDENTITY_RECOVERY_DATABASE_URL` | `—` | API 内嵌恢复与 mail kick 的四组配置；每一对直接值／文件最多设置一项。 未配置 recovery DSN 时沿用调用方事务；配置 recovery DSN 后，MAIL 和 MAIL_SETTINGS DSN 必须同时指定。 两个 mail DSN 都留空时，内嵌 mail kick 复用应用连接；显式 DSN 只成对使用。 |
| `IDENTITY_RECOVERY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RECOVERY_MAIL_KEY` | `—` | — |
| `IDENTITY_RECOVERY_MAIL_KEY_FILE` | `—` | — |
| `IDENTITY_MAIL_DATABASE_URL` | `—` | — |
| `IDENTITY_MAIL_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_MAIL_SETTINGS_DATABASE_URL` | `—` | — |
| `IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_DATABASE_URL_FILE` | `—` | 独立 Compose 的运行时 secret 文件选择器；左侧是宿主机路径，容器内会映射为 IDENTITY_*_FILE。 各数据库身份只授权对应函数。Fernet key 与 producer 使用同一秘密，并独立于 JWT secret。 Redis 身份只需 login_failed:* 的 GET/INCRBY/EXPIRE/DEL；签名 secret 与所有现存签发端相同。 |
| `IDENTITY_RUNTIME_KEY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_SESSION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_ACCOUNT_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_PRESENCE_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_DIRECTORY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_ADMIN_PASSWORD_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_PLATFORM_ROLE_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_SUSPENSION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_DELETION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_OFFBOARDING_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_INVITATION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_MAIL_V2_DATABASE_URL_FILE` | `—` | Required when starting the optional `mail-v2` Compose profile. |
| `IDENTITY_RUNTIME_DEACTIVATION_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_DEACTIVATION_OPERATOR_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_RECOVERY_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE` | `—` | — |
| `IDENTITY_RUNTIME_MAIL_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_LOGIN_REDIS_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_TOKEN_FILE` | `—` | — |
| `IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE` | `—` | — |
| `IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_ADMISSION_TOKEN` | `—` | 独立 Identity 的可选 admission-only bearer 与单函数状态 DSN；API 无需持有它们。 |
| `IDENTITY_ADMISSION_TOKEN_FILE` | `—` | — |
| `IDENTITY_ACTOR_STATUS_DATABASE_URL` | `—` | — |
| `IDENTITY_ACTOR_STATUS_DATABASE_URL_FILE` | `—` | — |
| `IDENTITY_RUNTIME_SIGNING_SECRET_FILE` | `—` | — |
| `IDENTITY_RUNTIME_EXPIRE_MINUTES` | `1440` | — |
| `IDENTITY_RUNTIME_EMAIL_VERIFICATION_REQUIRED` | `false` | — |
| `IDENTITY_RUNTIME_LOGIN_CAPTCHA_THRESHOLD` | `5` | — |
| `IDENTITY_RUNTIME_LOGIN_FAILED_WINDOW_SECONDS` | `3600` | — |
| `IDENTITY_RUNTIME_ALLOW_OPEN_REGISTRATION` | `false` | — |
| `IDENTITY_RUNTIME_FRONTEND_BASE_URL` | `http://localhost:3000` | — |
| `IDENTITY_MAIL_SMTP_HOST` | `—` | Standalone mail worker fallbacks; Settings DB values take precedence. Blank password means no SMTP authentication. These IDENTITY_MAIL_SMTP_* values are distinct from legacy API SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM. |
| `IDENTITY_MAIL_SMTP_PORT` | `0` | — |
| `IDENTITY_MAIL_SMTP_USER` | `—` | — |
| `IDENTITY_MAIL_SMTP_PASSWORD` | `—` | — |
| `IDENTITY_MAIL_SMTP_FROM` | `—` | — |

## 独立 Assets 来源服务（可选，不进入默认 Compose）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_SOURCE_URL` | `—` | 网关：URL 留空时用本地 Source 工作流；设置 URL 后所有操作都走远程，失败不会回退。 |
| `ASSETS_SOURCE_TOKEN` | `—` | — |
| `ASSETS_SOURCE_TIMEOUT_SECONDS` | `300` | — |
| `ASSETS_SOURCE_DATABASE_URL` | `—` | 独立进程：显式专用 DSN/token/S3 凭据，不能使用日常 API 宽凭据。 Source LOGIN 精确执行 23 项 Assets 能力（14 项来源/入库 + 9 项连接管理）；Catalog 29 项、导入 LOGIN 7 项各自独立。 不授予业务关系、序列或其他 owner schema 的直接访问权限。 |
| `ASSETS_SOURCE_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_SOURCE_TOKEN_FILE` | `—` | — |
| `ASSETS_SOURCE_S3_ACCESS_KEY` | `—` | — |
| `ASSETS_SOURCE_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_SOURCE_S3_SECRET_KEY` | `—` | — |
| `ASSETS_SOURCE_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` | `—` | 可选 Fernet key；应与 CONNECTOR_ENCRYPTION_KEY 相同，以读取既有密文。留空仍可启动；创建/加密或需要解密密文的操作会失败。 |
| `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY_FILE` | `—` | — |
| `ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST` | `[]` | Settings 数据库覆盖不存在时使用的部署回退；必须为 JSON 字符串数组，空数组不允许连接器目标出网。 |
| `ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE` | `—` | 可选只读 known_hosts 文件；容器内路径必须可读，未知 SFTP 主机仍被拒绝。 |
| `ASSETS_SOURCE_S3_ENDPOINT` | `—` | I/O origin 供服务访问；public origin 供浏览器签名访问，签名后不替换 Host。 |
| `ASSETS_SOURCE_S3_PUBLIC_ENDPOINT` | `—` | — |
| `ASSETS_SOURCE_S3_REGION` | `us-east-1` | — |
| `ASSETS_SOURCE_DATASETS_BUCKET` | `—` | — |
| `ASSETS_SOURCE_IMPORT_BUCKET` | `—` | — |
| `ASSETS_SOURCE_MEDIA_CACHE_BUCKET` | `—` | — |
| `ASSETS_SOURCE_HOST` | `0.0.0.0` | — |
| `ASSETS_SOURCE_PORT` | `8767` | — |
| `ASSETS_SOURCE_MAX_REQUEST_BYTES` | `1048576` | — |
| `ASSETS_SOURCE_MAX_RESPONSE_BYTES` | `16777216` | — |
| `ASSETS_SOURCE_POOL_MIN_SIZE` | `1` | — |
| `ASSETS_SOURCE_POOL_MAX_SIZE` | `8` | — |
| `ASSETS_SOURCE_POOL_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_SOURCE_SQL_TIMEOUT_SECONDS` | `5` | — |
| `ASSETS_SOURCE_S3_CONNECT_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_SOURCE_S3_READ_TIMEOUT_SECONDS` | `10` | — |

## Optional guide-byte capability in the same Assets Source binary.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_SOURCE_MODE` | `source` | source (default), guide-only (no PostgreSQL/source credentials), source-and-guide. |
| `ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT` | `4` | — |
| `ASSETS_SOURCE_GUIDE_S3_ENDPOINT` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_PUBLIC_URL` | `/minio` | — |
| `ASSETS_SOURCE_GUIDE_S3_REGION` | `us-east-1` | — |
| `ASSETS_SOURCE_GUIDE_S3_BUCKET` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_SECRET_KEY` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_SOURCE_GUIDE_S3_CONNECT_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_SOURCE_GUIDE_S3_READ_TIMEOUT_SECONDS` | `5` | — |

## 独立连接器导入进程（可选，不进入默认 Compose）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_IMPORT_DATABASE_URL` | `—` | 使用专用七函数 LOGIN 和只写来源前缀的对象身份，无 API/Celery/Redis 配置。 |
| `ASSETS_IMPORT_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_IMPORT_S3_ACCESS_KEY` | `—` | — |
| `ASSETS_IMPORT_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_IMPORT_S3_SECRET_KEY` | `—` | — |
| `ASSETS_IMPORT_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_IMPORT_S3_ENDPOINT` | `—` | — |
| `ASSETS_IMPORT_S3_REGION` | `us-east-1` | — |
| `ASSETS_IMPORT_DATASETS_BUCKET` | `—` | — |
| `CONNECTOR_ENCRYPTION_KEY_FILE` | `—` | 本进程可从文件读取与 API 相同的 Fernet key；API 仍使用 CONNECTOR_ENCRYPTION_KEY。 |
| `ASSETS_IMPORT_SFTP_KNOWN_HOSTS` | `—` | 宿主执行时为本地文件；Compose 将该文件挂载到固定只读 secret 路径。 留空时 Compose 使用空示例，SFTP 未知指纹仍然拒绝。 |
| `ASSETS_IMPORT_LEASE_SECONDS` | `120` | — |
| `ASSETS_IMPORT_HEARTBEAT_SECONDS` | `20` | — |
| `ASSETS_IMPORT_POLL_SECONDS` | `2` | — |
| `ASSETS_IMPORT_POOL_MIN_SIZE` | `1` | — |
| `ASSETS_IMPORT_POOL_MAX_SIZE` | `4` | — |
| `ASSETS_IMPORT_POOL_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_IMPORT_SQL_TIMEOUT_SECONDS` | `10` | — |
| `ASSETS_IMPORT_S3_CONNECT_TIMEOUT_SECONDS` | `10` | — |
| `ASSETS_IMPORT_S3_READ_TIMEOUT_SECONDS` | `120` | — |
| `ASSETS_IMPORT_SFTP_TIMEOUT_SECONDS` | `10` | — |

## 独立 Mask 字节服务：API 持有内容 token，清单查询使用单独的清理 token。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_MASK_CONTENT_URL` | `—` | 全部 URL 留空时本地装配同一实现；显式远程配置失败时不回退本地 S3。 cleanup 通过持久删除日志接收 Labeling 已建立围栏的操作。 |
| `ASSETS_MASK_CONTENT_TOKEN` | `—` | — |
| `ASSETS_MASK_CLEANUP_URL` | `—` | — |
| `ASSETS_MASK_CLEANUP_TOKEN` | `—` | — |
| `ASSETS_MASK_CONTENT_TIMEOUT_SECONDS` | `30` | — |
| `LABELING_MASK_GC_URL` | `—` | Mask 回收协调服务；URL/token 全空时本地装配同一持久协调器。 显式远程配置失败时不回退；扫描页数和删除数分别受限。 |
| `LABELING_MASK_GC_TOKEN` | `—` | — |
| `LABELING_MASK_GC_TIMEOUT_SECONDS` | `300` | — |
| `MASK_GC_DATABASE_URL` | `—` | 独立回收进程：只持有六项 GC 函数登录与 cleanup HTTP token。 |
| `MASK_GC_DATABASE_URL_FILE` | `—` | — |
| `MASK_GC_TOKEN` | `—` | — |
| `MASK_GC_TOKEN_FILE` | `—` | — |
| `MASK_GC_CONTENT_URL` | `—` | — |
| `MASK_GC_CONTENT_URL_FILE` | `—` | — |
| `MASK_GC_CONTENT_TOKEN` | `—` | — |
| `MASK_GC_CONTENT_TOKEN_FILE` | `—` | — |
| `MASK_GC_HOST` | `0.0.0.0` | — |
| `MASK_GC_PORT` | `8771` | — |
| `MASK_GC_POLL_SECONDS` | `5` | — |
| `MASK_GC_POLL_ENABLED` | `true` | false 保留手动 HTTP 与健康检查，关闭后台回收轮询。 |
| `PREANNOTATION_RESULTS_DATABASE_URL` | `—` | Optional function-only LOGIN for batch result checkpoints; final Labeling writes keep the normal application transaction. Empty uses DATABASE_URL. |
| `PREANNOTATION_RESULTS_DATABASE_URL_FILE` | `—` | — |
| `MASK_CONTENT_S3_ENDPOINT_URL` | `—` | 运行工件无数据库凭据；两个进程只共享固定桶/前缀，各用自己的 S3 身份。 |
| `MASK_CONTENT_BUCKET` | `—` | — |
| `MASK_CONTENT_REGION` | `us-east-1` | — |
| `MASK_CONTENT_ROLE` | `content` | — |
| `MASK_CONTENT_HOST` | `0.0.0.0` | — |
| `MASK_CONTENT_PORT` | `8770` | — |
| `MASK_CONTENT_TOKEN` | `—` | 宿主运行时 inline 和 _FILE 二选一；以下 *_FILE 也是可选 Compose 的宿主 secret 来源。 |
| `MASK_CONTENT_TOKEN_FILE` | `—` | — |
| `MASK_CONTENT_S3_ACCESS_KEY` | `—` | — |
| `MASK_CONTENT_S3_ACCESS_KEY_FILE` | `—` | — |
| `MASK_CONTENT_S3_SECRET_KEY` | `—` | — |
| `MASK_CONTENT_S3_SECRET_KEY_FILE` | `—` | — |
| `MASK_CLEANUP_TOKEN_FILE` | `—` | Compose 中清理进程独享这三份 secret，不挂入普通内容进程。 |
| `MASK_CLEANUP_S3_ACCESS_KEY_FILE` | `—` | — |
| `MASK_CLEANUP_S3_SECRET_KEY_FILE` | `—` | — |

## 独立图片/视频媒体控制与执行器：仅显式选择，默认本地组合，不进入默认 Compose。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_MEDIA_URL` | `—` | — |
| `ASSETS_MEDIA_TOKEN` | `—` | — |
| `ASSETS_MEDIA_TIMEOUT_SECONDS` | `30` | — |
| `ASSETS_MEDIA_CONTROL_DATABASE_URL` | `—` | 两个独立函数 LOGIN；与 migration/Assets owner 分离。直接值和 _FILE 二选一。 |
| `ASSETS_MEDIA_CONTROL_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_MEDIA_WORKER_DATABASE_URL` | `—` | — |
| `ASSETS_MEDIA_WORKER_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_MEDIA_TOKEN_FILE` | `—` | — |
| `ASSETS_MEDIA_S3_ACCESS_KEY` | `—` | 独立进程内使用的固定桶凭据。控制只需源/缓存 HEAD 和签名读取；执行器可写/清理派生缓存。 |
| `ASSETS_MEDIA_S3_SECRET_KEY` | `—` | — |
| `ASSETS_MEDIA_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_MEDIA_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_MEDIA_S3_ENDPOINT` | `—` | — |
| `ASSETS_MEDIA_S3_PUBLIC_ENDPOINT` | `—` | — |
| `ASSETS_MEDIA_S3_REGION` | `us-east-1` | — |
| `ASSETS_MEDIA_DATASETS_BUCKET` | `—` | — |
| `ASSETS_MEDIA_CACHE_BUCKET` | `—` | — |
| `ASSETS_MEDIA_INPUTS_BUCKET` | `—` | 独立 Assets Media 控制端和图片 Worker 的输入桶；Compose 默认继承 MINIO_INPUTS_BUCKET。 |
| `ASSETS_MEDIA_CONTROL_S3_ACCESS_KEY_FILE` | `—` | 仅供可选 Compose 的宿主 secret 路径；容器分别映射为上述进程内凭据。 |
| `ASSETS_MEDIA_CONTROL_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_MEDIA_WORKER_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_MEDIA_WORKER_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_MEDIA_HOST` | `0.0.0.0` | — |
| `ASSETS_MEDIA_PORT` | `8768` | — |
| `ASSETS_MEDIA_POOL_MIN_SIZE` | `1` | — |
| `ASSETS_MEDIA_POOL_MAX_SIZE` | `8` | — |
| `ASSETS_MEDIA_POOL_TIMEOUT_SECONDS` | `2` | — |
| `ASSETS_MEDIA_SQL_TIMEOUT_SECONDS` | `5` | — |
| `ASSETS_MEDIA_POLL_SECONDS` | `5` | — |
| `ASSETS_MEDIA_LEASE_SECONDS` | `1200` | — |
| `ASSETS_MEDIA_FRAME_TTL_DAYS` | `14` | — |
| `ASSETS_MEDIA_CHUNK_TTL_DAYS` | `30` | — |
| `ASSETS_MEDIA_S3_CONNECT_TIMEOUT_SECONDS` | `10` | 控制进程默认 2/10 秒；执行进程默认 10/120 秒。留给所选进程显式覆盖。 |
| `ASSETS_MEDIA_S3_READ_TIMEOUT_SECONDS` | `120` | — |
| `ASSETS_MEDIA_S3_MAX_ATTEMPTS` | `2` | — |

## 独立图片执行器：单独的六函数 LOGIN，与视频执行器分离。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `ASSETS_IMAGES_DATABASE_URL` | `—` | DSN 与 _FILE 二选一；可选 Compose 将以下宿主凭据路径映射到进程内 ASSETS_MEDIA_S3_*_FILE。 |
| `ASSETS_IMAGES_DATABASE_URL_FILE` | `—` | — |
| `ASSETS_IMAGES_S3_ACCESS_KEY_FILE` | `—` | — |
| `ASSETS_IMAGES_S3_SECRET_KEY_FILE` | `—` | — |
| `ASSETS_IMAGES_POLL_SECONDS` | `5` | — |
| `ASSETS_IMAGES_LEGACY_BUCKET` | `—` | 留空依次使用 MINIO_BUCKET / MINIO_BUCKET_ANNOTATIONS；独立进程必须显式选定历史 Task 桶。 |
| `ASSETS_IMAGES_SRGB_PROFILE` | `—` | 留空使用 IMAGE_PYRAMID_SRGB_PROFILE，再使用镜像内的 ICC 文件。 |

## Workspace 成员与项目负责人转移命令（可选独立运行）。API 可选择私有 runtime URL、

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKSPACE_MEMBERSHIP_URL` | `—` | 专用事务 DSN 或默认 caller Session；直接值与 _FILE 二选一，显式失败不回退。 |
| `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN` | `—` | — |
| `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN_FILE` | `—` | — |
| `WORKSPACE_MEMBERSHIP_DATABASE_URL` | `—` | — |
| `WORKSPACE_MEMBERSHIP_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS` | `30` | — |
| `WORKSPACE_OWNER_TRANSFER_URL` | `—` | 转移命令复用上面的 control token 和 timeout；默认借用 caller transaction 且不提交。 |
| `WORKSPACE_OWNER_TRANSFER_DATABASE_URL` | `—` | — |
| `WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_CONFIGURATION_URL` | `—` | Configuration writes: empty borrows the caller transaction without committing. See ADR-0136 for the current qualification status. Select URL or DSN, never both. Remote failures do not select another writer. |
| `WORKSPACE_CONFIGURATION_DATABASE_URL` | `—` | — |
| `WORKSPACE_CONFIGURATION_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_CONFIGURATION_TIMEOUT_SECONDS` | `30` | — |
| `WORKSPACE_INFERENCE_PROBE_URL` | `—` | Standalone Workspace calls the credential-owning Inference probe for backend modalities. These are independent-runtime settings; API probe selectors are below. |
| `WORKSPACE_INFERENCE_PROBE_TOKEN` | `—` | — |
| `WORKSPACE_INFERENCE_PROBE_TOKEN_FILE` | `—` | — |
| `WORKSPACE_INFERENCE_PROBE_TIMEOUT_SECONDS` | `30` | — |
| `WORKSPACE_GUIDE_OBJECTS_URL` | `—` | Guide byte capability consumed by the API binding and the Workspace owner. In the independent owner, use the existing Assets Source Guide endpoint. |
| `WORKSPACE_GUIDE_OBJECTS_TOKEN` | `—` | — |
| `WORKSPACE_GUIDE_OBJECTS_TOKEN_FILE` | `—` | — |
| `WORKSPACE_GUIDE_OBJECTS_TIMEOUT_SECONDS` | `15` | — |
| `WORKSPACE_TEMPLATE_QUERY_URL` | `—` | Template reads: empty borrows the caller transaction. Select URL or DSN. Explicit remote failures do not fall back to another reader. |
| `WORKSPACE_TEMPLATE_QUERY_DATABASE_URL` | `—` | — |
| `WORKSPACE_TEMPLATE_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_TEMPLATE_QUERY_TIMEOUT_SECONDS` | `30` | — |

## Project query provider: unset uses the caller transaction; select URL or DSN/file.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `WORKSPACE_PROJECT_QUERY_URL` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_DATABASE_URL` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_TIMEOUT_SECONDS` | `30` | — |
| `WORKSPACE_DIRECTORY_QUERY_URL` | `—` | Directory reads share the runtime Identity profile dependency below. |
| `WORKSPACE_DIRECTORY_QUERY_DATABASE_URL` | `—` | — |
| `WORKSPACE_DIRECTORY_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_DIRECTORY_QUERY_TIMEOUT_SECONDS` | `30` | — |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_URL` | `—` | Standalone Workspace profile dependency; evaluated only for nonempty display IDs. HTTP takes precedence over the Briefs DSN; these credentials never enter query wire. |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_TOKEN` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_TOKEN_FILE` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_PROJECT_QUERY_IDENTITY_TIMEOUT_SECONDS` | `2` | — |
| `WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE` | `—` | 独立 Compose runtime 的宿主 secret-file selectors，容器内映射为上面的数据库与控制 token 配置。 |
| `WORKSPACE_RUNTIME_OWNER_TRANSFER_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_CONFIGURATION_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_TEMPLATE_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_PROJECT_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_DIRECTORY_QUERY_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` | `—` | — |
| `WORKSPACE_RUNTIME_INFERENCE_PROBE_TOKEN_FILE` | `—` | — |

## Labeling class usage: API first checks current Workspace management authority.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LABELING_CLASS_QUERY_URL` | `—` | Empty selectors borrow the caller transaction. Select DSN or HTTP, never both. |
| `LABELING_CLASS_QUERY_DATABASE_URL` | `—` | — |
| `LABELING_CLASS_QUERY_DATABASE_URL_FILE` | `—` | — |
| `LABELING_CLASS_QUERY_CONTROL_TOKEN` | `—` | — |
| `LABELING_CLASS_QUERY_CONTROL_TOKEN_FILE` | `—` | — |
| `LABELING_CLASS_QUERY_TIMEOUT_SECONDS` | `30` | — |
| `LABELING_CLASS_QUERY_HOST` | `0.0.0.0` | Standalone runtime; it requires only its Labeling DSN and private bearer. |
| `LABELING_CLASS_QUERY_PORT` | `8178` | — |
| `LABELING_CLASS_QUERY_MAX_CONCURRENCY` | `8` | — |
| `LABELING_RUNTIME_CLASS_QUERY_DATABASE_URL_FILE` | `—` | Host paths mapped to the standalone container's read-only secret files. |
| `LABELING_RUNTIME_CLASS_QUERY_CONTROL_TOKEN_FILE` | `—` | — |

## Video finalization: empty selectors borrow the current worker transaction.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LABELING_VIDEO_WRITER_URL` | `—` | Select a function-only DSN or authenticated HTTP owner; never both. |
| `LABELING_VIDEO_WRITER_DATABASE_URL` | `—` | — |
| `LABELING_VIDEO_WRITER_DATABASE_URL_FILE` | `—` | — |
| `LABELING_VIDEO_WRITER_CONTROL_TOKEN` | `—` | — |
| `LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE` | `—` | — |
| `LABELING_VIDEO_WRITER_TIMEOUT_SECONDS` | `90` | — |
| `LABELING_VIDEO_WRITER_HOST` | `127.0.0.1` | Standalone owner uses the same DSN/control-token names and existing MaskContent URL/token settings when a candidate contains Mask references. |
| `LABELING_VIDEO_WRITER_PORT` | `8180` | — |
| `LABELING_VIDEO_WRITER_MAX_CONCURRENCY` | `8` | — |
| `LABELING_RUNTIME_VIDEO_WRITER_DATABASE_URL_FILE` | `—` | — |
| `LABELING_RUNTIME_VIDEO_WRITER_CONTROL_TOKEN_FILE` | `—` | — |
| `LABELING_RUNTIME_VIDEO_WRITER_MASK_CONTENT_TOKEN_FILE` | `—` | — |

## API/worker Inference HTTP clients. Stored backend secrets stay in the owner.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `INFERENCE_PIPELINES_URL` | `—` | Saved pipeline operations may still borrow the caller's transaction when URL is empty. |
| `INFERENCE_PIPELINES_TOKEN` | `—` | — |
| `INFERENCE_PIPELINES_TOKEN_FILE` | `—` | — |
| `INFERENCE_PIPELINES_TIMEOUT_SECONDS` | `30` | — |
| `INFERENCE_BACKEND_PROBE_URL` | `—` | — |
| `INFERENCE_BACKEND_PROBE_TOKEN` | `—` | — |
| `INFERENCE_BACKEND_PROBE_TOKEN_FILE` | `—` | — |
| `INFERENCE_BACKEND_PROBE_TIMEOUT_SECONDS` | `30` | — |
| `INFERENCE_HEALTH_OBSERVATION_URL` | `—` | — |
| `INFERENCE_HEALTH_OBSERVATION_TOKEN` | `—` | — |
| `INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE` | `—` | — |
| `INFERENCE_HEALTH_OBSERVATION_TIMEOUT_SECONDS` | `90` | — |
| `INFERENCE_REGISTRY_INGRESS_URL` | `—` | — |
| `INFERENCE_REGISTRY_INGRESS_TOKEN` | `—` | — |
| `INFERENCE_REGISTRY_INGRESS_TOKEN_FILE` | `—` | — |
| `INFERENCE_REGISTRY_INGRESS_TIMEOUT_SECONDS` | `90` | — |
| `INFERENCE_ROUTING_URL` | `—` | — |
| `INFERENCE_ROUTING_TOKEN` | `—` | — |
| `INFERENCE_ROUTING_TOKEN_FILE` | `—` | — |
| `INFERENCE_ROUTING_TIMEOUT_SECONDS` | `30` | — |
| `INFERENCE_MODEL_EXECUTION_URL` | `—` | — |
| `INFERENCE_MODEL_EXECUTION_TOKEN` | `—` | — |
| `INFERENCE_MODEL_EXECUTION_TOKEN_FILE` | `—` | — |
| `INFERENCE_BACKEND_CONTROL_URL` | `—` | — |
| `INFERENCE_BACKEND_CONTROL_TOKEN` | `—` | — |
| `INFERENCE_BACKEND_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_GPU_URL` | `—` | — |
| `INFERENCE_GPU_TOKEN` | `—` | — |
| `INFERENCE_GPU_TOKEN_FILE` | `—` | — |

## Owner process only: python -m aap_inference. Each DSN has its own closed profile.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `INFERENCE_HOST` | `127.0.0.1` | Select a nonempty direct value OR its _FILE. Never pass these DSNs to API/Celery. |
| `INFERENCE_PORT` | `8768` | — |
| `INFERENCE_MAX_CONCURRENCY` | `8` | — |
| `INFERENCE_REQUIRED_CAPABILITIES` | `pipelines,backend_probe,health_observation,registry_ingress,routing,execution,backend_control,gpu` | — |
| `INFERENCE_ROUTING_HEALTH_MAX_AGE_SECONDS` | `90` | — |
| `INFERENCE_ROUTING_LEASE_TTL_SECONDS` | `120` | — |
| `INFERENCE_CONTROL_TOKEN` | `—` | — |
| `INFERENCE_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_BACKEND_PROBE_CONTROL_TOKEN` | `—` | — |
| `INFERENCE_BACKEND_PROBE_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_PIPELINES_DATABASE_URL` | `—` | — |
| `INFERENCE_PIPELINES_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_PROBE_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_PROBE_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL` | `—` | — |
| `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_HEALTH_DATA_DATABASE_URL` | `—` | — |
| `INFERENCE_HEALTH_DATA_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_REGISTRY_INGRESS_DATABASE_URL` | `—` | — |
| `INFERENCE_REGISTRY_INGRESS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_ROUTING_DATABASE_URL` | `—` | — |
| `INFERENCE_ROUTING_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_GPU_DATABASE_URL` | `—` | — |
| `INFERENCE_GPU_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_GPU_REDIS_URL` | `—` | — |
| `INFERENCE_GPU_REDIS_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_STATS_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_STATS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_SETUP_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_SETUP_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_CATALOG_FALLBACK_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_CATALOG_FALLBACK_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_STATS_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_STATS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_BACKEND_READ_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_BACKEND_READ_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_LIFECYCLE_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_LIFECYCLE_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_EXECUTION_DATABASE_URL` | `—` | — |
| `INFERENCE_BACKEND_ENDPOINT_EXECUTION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_GPU_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL` | `—` | — |
| `INFERENCE_GPU_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_GPU_ENDPOINT_GPU_CONTROL_DATABASE_URL` | `—` | — |
| `INFERENCE_GPU_ENDPOINT_GPU_CONTROL_DATABASE_URL_FILE` | `—` | — |

## Host files mounted only into docker-compose.inference.yml's owner container.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `INFERENCE_RUNTIME_PIPELINES_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_HEALTH_OBSERVATION_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_HEALTH_DATA_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_REGISTRY_INGRESS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_REGISTRY_INGRESS_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_ROUTING_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_ROUTING_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_MODEL_EXECUTION_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_CONTROL_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_TOKEN_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_STATS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_REDIS_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_SIGNING_KEYS_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_COLLECTOR_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_SETUP_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_CATALOG_FALLBACK_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_STATS_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_BACKEND_READ_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_LIFECYCLE_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_BACKEND_ENDPOINT_EXECUTION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_ENDPOINT_HEALTH_OBSERVATION_DATABASE_URL_FILE` | `—` | — |
| `INFERENCE_RUNTIME_GPU_ENDPOINT_GPU_CONTROL_DATABASE_URL_FILE` | `—` | — |

## Model process route controller; use the Registry's configured bearer.

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AAP_ROUTE_CONTROL_TOKEN` | `—` | — |
| `AAP_ROUTE_CONTROL_TOKEN_FILE` | `—` | — |
| `AAP_ROUTE_REGISTRY_ID` | `—` | — |

## Audit 独立维护与归档读取（可选，不改变默认运行拓扑）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `AUDIT_MAINTENANCE_DATABASE_URL` | `—` | 一次性维护 CLI 只使用单函数 LOGIN；值／文件恰好选择一项。 |
| `AUDIT_MAINTENANCE_DATABASE_URL_FILE` | `—` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL` | `—` | 只有 archive-partitions 需要以下独立 Audit 归档桶凭据；不创建桶、不持有删除权限。 |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_REGION` | `us-east-1` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_BUCKET` | `audit-archive` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID` | `—` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_ACCESS_KEY_ID_FILE` | `—` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY` | `—` | — |
| `AUDIT_MAINTENANCE_ARCHIVE_S3_SECRET_ACCESS_KEY_FILE` | `—` | — |
| `AUDIT_ARCHIVE_READ_S3_ENDPOINT_URL` | `—` | API 管理员归档列表／读取可单独选择只读凭据；空 endpoint 保留原内嵌存储绑定。 显式选择后配置或访问失败不会回退通用存储身份，API 原有鉴权继续生效。 |
| `AUDIT_ARCHIVE_READ_S3_REGION` | `us-east-1` | — |
| `AUDIT_ARCHIVE_READ_S3_BUCKET` | `audit-archive` | — |
| `AUDIT_ARCHIVE_READ_S3_ACCESS_KEY_ID` | `—` | — |
| `AUDIT_ARCHIVE_READ_S3_ACCESS_KEY_ID_FILE` | `—` | — |
| `AUDIT_ARCHIVE_READ_S3_SECRET_ACCESS_KEY` | `—` | — |
| `AUDIT_ARCHIVE_READ_S3_SECRET_ACCESS_KEY_FILE` | `—` | — |
