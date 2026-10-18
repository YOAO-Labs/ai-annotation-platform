---
title: 图片金字塔运行手册
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-10-19
---

# 图片金字塔运行手册

图片控制与视频媒体共用 `apps/assets-media`；图片工作由独立轮询 Worker 执行，派生对象由既有 Assets 清理进程删除。自动生成默认关闭。仓库包含可选部署配置；此文不代表已部署到主环境/生产环境，也不构成生产容量验收。

## 运行边界与授权

| 身份                                        | 数据库能力                                       | 对象存储范围                                           |
| ------------------------------------------- | ------------------------------------------------ | ------------------------------------------------------ |
| API/共享控制端 `asset_media_control_client` | 共享控制 32 项（视频 18、图片 14），仅函数执行权 | HEAD/GET 注册源图和缓存、签发读取 URL；不写源、不清理  |
| 图片 Worker `asset_image_worker_client`     | 图片执行 6 项函数                                | 只读 datasets/历史 Task 源图；只在派生前缀读写；不删源 |
| 清理端 `asset_cleanup_client`               | 4 项清理函数（原 outbox 与图片垃圾领取/确认）    | 对缓存桶派生前缀列举、删除；不读写源对象               |

这些 LOGIN 不直接访问应用表或其他 owner schema，也不继承迁移/owner 角色。Task 可见性由控制端与 Labeling owner 能力确认。已受理作业捕获来源身份与策略，发起者后来被禁用不会撤销执行权；来源变化和明确取消仍会阻止迟到发布。

## 部署与权限

先将数据库迁移到包含图片能力的当前 head（迁移 `0220`），按既有 module-data 流程安装 owner 辅助授权，再应用共享媒体授权 `infra/docker/assets-media-grants.sql` 和图片 Worker 授权 `infra/docker/assets-image-grants.sql`。创建独立 NOLOGIN Assets owner、control/worker LOGIN；迁移身份单独使用。cleanup 的四项能力沿用 module-data 授权流程。

`docker-compose.assets-media.yml` 不属于默认 Compose。按部署环境准备独立 DSN、token 与 S3 secret 文件，确认容器 UID 10005 可读；API 通过 `ASSETS_MEDIA_URL` 和对应 token 接入控制端。独立 Worker 使用 `ASSETS_IMAGES_DATABASE_URL_FILE`、`ASSETS_IMAGES_S3_ACCESS_KEY_FILE`、`ASSETS_IMAGES_S3_SECRET_KEY_FILE`。控制、Worker、清理身份不可共用高权限凭据。

S3 权限应限定为：

- 控制端对已登记源图和缓存对象 HEAD/GET，并签发短期读取 URL；不写缓存或删除对象。
- 图片 Worker 对源桶只读；对 `image-assets/*`、`image-pyramids/*` 派生前缀读写和校验，并在 `image-pyramids/*` 受限列举以做协调；不写/删源对象。
- 清理身份只对缓存桶中由数据库签发的图片派生 prefix 做 List/Delete；不授予 Get/Put、源桶访问或桶管理。

共享控制端与图片 Worker 的 endpoint、region、桶配置要一致；API 通过共享控制端 URL/token 接入。公共 S3 endpoint 必须对浏览器可达，不要改写签名 URL。历史 Task 图像桶通过 `ASSETS_IMAGES_LEGACY_BUCKET` 指定，开发环境可回退至既有 `MINIO_BUCKET` / `MINIO_BUCKET_ANNOTATIONS`。

## 启动与健康检查

图片执行使用 `apps/assets-media/Dockerfile` 的 `image-worker` target，不消费 Celery 队列，也不依赖 Redis/beat。`ASSETS_IMAGES_POLL_SECONDS` 默认 5 秒；Worker 启动时以及此后约每日运行一次有界协调。使用默认栈与 overlay 一起启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml build assets-media-control assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml up -d assets-media-control assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml ps
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml logs --tail=100 assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml exec assets-images-worker python -m aap_assets_images_runtime.worker --healthcheck
docker compose -f docker-compose.yml -f docker-compose.assets-cleanup.yml up -d --build assets-cleanup
```

API 必须能经私有网络访问 `http://assets-media-control:8768`。overlay 只在容器网络 expose 8768，不发布宿主端口。图片 Worker 的可选 Compose 限制为 4 CPU、16 GiB 内存和 12 GiB 临时盘；这是本地配置，不代表生产容量。控制端 `GET /health/live` 表示进程可响应，`GET /health/ready` 检查 SQL 能力；Worker `--healthcheck` 检查配置、数据库连接与六项精确权限。它们都不验证实际 S3 IAM、图片解码或发布。小批量验收应至少走通源 HEAD/GET、Job、manifest/asset URL、对象身份校验和 cleanup 精确删除；使用专用环境和素材，不据此声称生产验收。

宿主直接运行入口：

```bash
uv run --project apps/assets-media --extra runtime --extra server python -m aap_assets_media_runtime
uv run --project apps/assets-media --extra runtime python -m aap_assets_images_runtime.worker
```

API 配置 `ASSETS_MEDIA_URL` 后使用远程共享控制；远程配置/请求失败不回退本地。地址留空时，当前开发兼容路径仍可本地装配控制和轻量 Celery 唤醒；要验证独立边界，确认 API 走远程控制且作业由 `assets-images-worker` 执行。

## 策略与 profile

| 设置                                                                |                    默认值 | 说明                             |
| ------------------------------------------------------------------- | ------------------------: | -------------------------------- |
| `IMAGE_PYRAMID_AUTO_GENERATE`                                       |                   `false` | 新图自动受理开关                 |
| `IMAGE_PYRAMID_PROFILE_VERSION`                                     |              `pyramid-v1` | 读取的金字塔 profile/lineage     |
| `IMAGE_PYRAMID_NORMALIZATION_VERSION`                               | `exif-autorotate-srgb-v1` | EXIF 与 sRGB 归一化规则          |
| `IMAGE_PYRAMID_OPTIONAL_PIXELS` / `IMAGE_PYRAMID_REQUIRED_PIXELS`   |   16,777,216 / 50,000,000 | 可选/必须使用金字塔的像素门槛    |
| `IMAGE_PYRAMID_MAX_PIXELS` / `IMAGE_PYRAMID_MAX_DIMENSION`          |      300,000,000 / 32,768 | 解码像素和单边硬上限             |
| `IMAGE_PYRAMID_MAX_TILES`                                           |                    20,000 | 单代 tile 上限                   |
| `IMAGE_PYRAMID_MAX_SOURCE_BYTES`                                    |                     8 GiB | 最大压缩源大小                   |
| `IMAGE_PYRAMID_MAX_DERIVED_BYTES` / `IMAGE_PYRAMID_MAX_TEMP_BYTES`  |                8 / 12 GiB | 派生字节与临时空间上限           |
| `IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS` / `IMAGE_PYRAMID_LEASE_SECONDS` |          1,800 / 2,100 秒 | 租约必须大于超时并留出硬终止余量 |
| `IMAGE_PYRAMID_RETRY_COOLDOWN_SECONDS`                              |                    300 秒 | 失败后的重试冷却                 |
| `IMAGE_PYRAMID_GC_GRACE_HOURS`                                      |                   24 小时 | 代次失活后的默认保留期           |
| `IMAGE_PYRAMID_URL_EXPIRY_SECONDS`                                  |                    900 秒 | 派生读取 URL 有效期              |
| `IMAGE_PYRAMID_VIPS_CONCURRENCY`                                    |                         4 | 单 Worker libvips 并发           |
| `ASSETS_IMAGES_SRGB_PROFILE`                                        |             镜像 ICC 文件 | Worker 可覆盖的绝对路径          |

作业受理时固定策略。API 与共享控制的 profile 必须一致；切换 profile/normalization version 前规划新输出线与回填范围，不要只改一台服务。`VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` 是 Web 构建时开关，改动后需重建 Web；它不控制自动受理。

## 有界回填、状态与重试

脚本先做有界 source 扫描，再受理持久 backfill Job；S3 观察和子作业入队由 Worker 完成。先 dry-run，再用小批次。`dataset_item` 可用 dataset id；`task` 仅指没有 DatasetItem 关联的历史 Task，不能带 dataset id。跨游标分页时固定带时区 cutoff：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --owner-kind dataset_item --dataset-id <dataset-uuid> --dry-run --limit 100
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --owner-kind dataset_item --dataset-id <dataset-uuid> --limit 100 --accepted-before 2026-10-19T00:00:00Z
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --owner-kind dataset_item --dataset-id <dataset-uuid> --cursor <next-cursor-uuid> --limit 100 --accepted-before 2026-10-19T00:00:00Z
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py --owner-kind task --dry-run --limit 100
```

每个页面使用新的 `--operation-id`；原生 `asset_image_operations` 保存不可变受理回执，重复相同操作意图会返回首次结果。保存输出的 `accepted_before`、`operation_id`、`job_id` 和 `next_cursor`。脚本每次都会重新扫描候选项；不要只用相同 CLI 参数盲目重跑已受理页面，回执只重放同一冻结 owner 选择。已受理范围不因操作者角色变化而重新解释；来源删除、代次变化或明确取消会阻止该源迟到发布。

Task 接口为 `GET /api/v1/tasks/{task_id}/image-pyramid`（manifest/状态）、`POST /api/v1/tasks/{task_id}/image-pyramid/asset-urls`（overview/manifest/tile URL）和 `POST /api/v1/tasks/{task_id}/image-pyramid/retry`。重试返回 202；提供 UUID `Idempotency-Key`，丢失响应时用同 key 重放。先读 manifest 的 `status`、`retryable`、`retry_after_ms` 和 `error_code`。`already_ready` 不需重试；`retry_cooldown` 按服务等待时间处理；`source_not_ready` 检查上传/登记完成状态；`source_changed`/`source_missing` 检查源注册和对象身份后再发起新作业。尺寸/资源/解码/色彩错误先检查输入与策略。`lease_lost`、`operation_busy`、`storage_unavailable` 可等待 Worker 恢复和租约重领，避免快速创建新的操作 ID。

## 观测与排障

当前 API 侧 Prometheus 指标只有与图片有关的 `image_pyramid_api_requests_total{operation,outcome}`（manifest/asset URL/retry 结果）和 `image_pyramid_asset_url_items_total{kind}`（签发 URL 条目）。独立 Worker 不再发布旧版的生成总数、阶段耗时、解码像素、tile、临时盘、派生字节或 GC 指标，不要配置基于这些旧名称的告警。

原生 `read_image_usage` 返回 `source_bytes`、`thumbnail_bytes`、`pyramid_bytes`、`pending`、`running`、`failed` 和 `garbage_pending`。通过受限共享控制端查看：

```bash
curl --fail --silent --show-error -H 'Authorization: Bearer '"$ASSETS_MEDIA_TOKEN" -H 'Content-Type: application/json' -d '{}' "$ASSETS_MEDIA_URL/v1/images/commands/read_image_usage"
```

`source_bytes` 是已登记 DatasetItem 源大小合计，不含直接历史 Task 源图；`thumbnail_bytes` 是 ready thumbnail Job 记录的派生字节；`pyramid_bytes` 是 active ready 金字塔代次保留字节；pending、running 和 failed 计数跨所有图片作业，`garbage_pending` 为未完成清理责任数。这些是 SQL owner 读数，不等于 S3 账单或全桶盘点。

Worker 日志使用稳定错误码，不应记录来源凭据、签名 URL 或原始文件。遇错检查 profile、登录角色、endpoint/bucket、ICC 路径和容器 RSS/临时盘。`upload_verification_failed` 或 `inconsistent_ready` 应核对缓存对象身份与可达性，不要手工伪造 ready 状态。浏览器 Large Image Tile Diagnostics 用于排查客户端请求/解码/覆盖范围；Workbench Raster Resource Diagnostics 是浏览器内存账本，不是 Worker 队列、服务器字节或 GPU 物理指标。

## 保留、清理与回滚

ready 代次的 URL 签发使用非阻塞来源/垃圾栅栏检查；同一代次每 10 分钟最多更新一次访问时间，并延长该代次保留截止。退休代次按其捕获的 `gc_grace_hours`（默认 24 小时）等待回收。清理已持有租约或已完成删除后，不再签发新 URL；旧签名链接仍有短期有效期。

图片 Worker 只登记派生前缀，不直接删对象。既有 `assets-cleanup` 领取事务提交后才做 S3 删除并确认；失败会保留责任。整 asset 与 attempt prefix tombstone 保留并每 5 分钟重扫，以清除迟到写入。`garbage_pending` 持续非零时检查 cleanup 健康、数据库租约和精确 S3 Delete/List 权限。禁止对活跃 `image-pyramids/*` 配整桶/固定 TTL 生命周期，禁止清理身份访问源桶。

紧急回退：关闭 `IMAGE_PYRAMID_AUTO_GENERATE` 并停止显式重试/回填；需要暂停浏览器选择时，将 `VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` 设为 `false` 并重建 Web；停止 `assets-images-worker`，保留共享控制与清理职责。保留 Job、操作回执、ready 对象及未确认垃圾，等待在途租约排空/到期。不要直接删除图片表或批量删对象；降级迁移会拒绝丢弃仍有作业、回执或清理责任的状态。
