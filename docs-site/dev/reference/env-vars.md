---
title: 环境变量参考
audience: [dev, ops]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-09-15
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

## 超大图 Image Pyramid

| 变量 | 默认值 | 说明 |
|---|---|---|
| `IMAGE_PYRAMID_AUTO_GENERATE` | `false` | 默认只发布 schema/API，不在上传时自动生成；容量验收后再显式开启。 |
| `IMAGE_PYRAMID_PROFILE_VERSION` | `pyramid-v1` | — |
| `IMAGE_PYRAMID_NORMALIZATION_VERSION` | `exif-autorotate-srgb-v1` | — |
| `IMAGE_PYRAMID_OPTIONAL_PIXELS` | `16777216` | 16MP 起可选生成，50MP 起客户端必须使用（客户端切换在后续版本）。 |
| `IMAGE_PYRAMID_REQUIRED_PIXELS` | `50000000` | — |
| `IMAGE_PYRAMID_MAX_PIXELS` | `300000000` | — |
| `IMAGE_PYRAMID_MAX_DIMENSION` | `32768` | — |
| `IMAGE_PYRAMID_MAX_TILES` | `20000` | — |
| `IMAGE_PYRAMID_MAX_SOURCE_BYTES` | `8589934592` | — |
| `IMAGE_PYRAMID_MAX_DERIVED_BYTES` | `8589934592` | — |
| `IMAGE_PYRAMID_MAX_TEMP_BYTES` | `12884901888` | — |
| `IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS` | `1800` | — |
| `IMAGE_PYRAMID_LEASE_SECONDS` | `2100` | — |
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
| `GPU_ARBITER_COLLECTOR_DATABASE_URL_FILE` | `—` | 宿主机文件路径；文件内仅一行独立 collector 的 postgresql+asyncpg URL。 该文件只挂载到 gpu.control 单并发控制 worker。普通应用角色必须无 membership/fence DELETE。 off/observe 或尚未启用 rollout 时留空；生产 enforce 前必须完成角色隔离并设置 0400 权限。 |
| `GPU_ARBITER_RESOURCES_JSON` | `{}` | resource key 必须等于 node_id/physical_device_token；优先使用 GPU/MIG UUID。 只有部署明确固定索引映射时才使用 index:N。allocatable_mb 已扣除系统与安全余量。 例: {"gpu-node-a/GPU-xxx":{"node_id":"gpu-node-a","physical_device_token":"GPU-xxx","allocatable_mb":22000,"mode":"observe"}} |
| `GPU_ARBITER_ADMISSION_TIMEOUT_SECONDS` | `30` | 仅供 enforce authority 的 backend/card slow-path FIFO，独立于推理、build 与 drain timeout。 ticket 使用固定期限且不因轮询续期；驱逐会在期限内预留终态清理窗口；取值 1..3600 秒，off/observe 不入队。 backend/card 超时分别返回 gpu_backend_concurrency_saturated/gpu_capacity_unavailable，均为 503 + Retry-After。 |
| `GPU_ARBITER_RESIDENCY_COOLDOWN_SECONDS` | `30` | 新 residency 的最短保护窗口；只接受 1..3600 秒。未到期 victim 会在 exact card ticket 上按 admission deadline 与固定 ticket TTL 有界等待且不续期；预算耗尽返回 503 + Retry-After， 超时或取消会精确清理票据。 |
| `GPU_LIFECYCLE_SIGNING_KEYS_FILE` | `—` | Platform signer private seeds stay in a root-readable file, never in an env value. Strict JSON: {"kid":"unpadded-base64url-raw-32-byte-Ed25519-private-seed"}. Leave both values empty while arbitration is off/observe. The file is read lazily only by enforce authority/promotion, and must not be mounted into GPU backends. |
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

## 存储连接器（外部 S3 / SFTP 服务端拉取）凭据 Fernet 加密 key。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CONNECTOR_ENCRYPTION_KEY` | `—` | 与 SECRET_KEY 隔离；留空则连接器加解密一律拒绝（API 返回 503）。 生成: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" |
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
| `DUCKDB_PATH` | `./data/duckdb/analytics.duckdb` | Celery worker 每日 02:30 UTC 增量同步 task_events + audit_logs 到这个 DuckDB 文件; FastAPI /admin/analytics 端点以 read_only 模式读取它出固定面板. Docker 部署: worker 容器把 host ./data/duckdb 挂到 /var/lib/duckdb; 本地 API 进程跑 host 时直接读 host 文件 (单 writer 多 reader). |

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
| `AAP_WORKTREE_MODE` | `—` | 工作树运行时内部标识 由 pnpm dev:worktree 向子进程注入 dev/test/e2e；不要手工配置。 pytest / Playwright 据此拒绝在 dev 模式运行破坏性测试；测试连接、队列和存储也由入口覆盖。 |
