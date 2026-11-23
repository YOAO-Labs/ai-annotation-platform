---
title: 图片金字塔运行手册
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-11-16
---

# 图片金字塔运行手册

默认由 API 内嵌图片控制、`celery-worker-image-pyramid` 执行；只在需要独立内存或临时盘限制时切换到 `assets-images-worker`。派生对象由既有 Assets 清理进程处理。自动生成默认关闭。Native 输出保存、Source 签名与 Cleanup 回收见[动态保存目标与 Native 清理](#动态保存目标与-native-清理)；本配方不是生产容量或现网切换结论。

## 运行边界与授权

| 身份                                                 | 数据库能力                                                                      | 对象存储范围                                                                                                               |
| ---------------------------------------------------- | ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| 独立共享控制端 `yoao_asset_media_control_client`     | 当前共享控制能力集（视频、图片、输入、清单与跟踪），仅函数执行权                | HEAD/GET 注册源图和缓存、签发读取 URL；Native 读取只执行 Source 签发的有限请求；不写源、不清理                             |
| 图片 Worker `yoao_asset_image_worker_client`         | 图片执行 16 项、输入 6 项函数                                                   | 只读已登记原件（TaskOriginal／DatasetItem 源）；只在派生前缀读写；Native 写入/校验只执行 Source 签发的请求；不写源、不删源 |
| 清理端 `yoao_asset_cleanup_client`                   | 11 项清理函数（五条 lane：outbox、图片垃圾、Native 保存输出回收、两类输入墓碑） | 对缓存桶派生前缀列举、删除；Native lane 只执行 Source 签发的保存成员/前缀请求，不持有 Native 对象凭据；不读写源对象        |
| Source `yoao_asset_source_client`（Native 签名子集） | 3 项 Native 捕获能力                                                            | 只为精确保存成员/前缀签发有限 S3 请求；内建目标使用独立 Native 身份；不执行 PUT/GET/DELETE/列举 I/O                        |

这些 LOGIN 不直接访问应用表或其他 owner schema，也不继承迁移/owner 角色。Task 可见性由控制端与 Labeling owner 能力确认。已受理作业捕获来源身份与策略，发起者后来被禁用不会撤销执行权；来源变化和明确取消仍会阻止迟到发布。

## 部署与权限

先将数据库迁移到当前 head，按既有 module-data 流程安装 owner 辅助授权，再应用共享媒体授权 `infra/docker/assets-media-grants.sql` 和图片 Worker 授权 `infra/docker/assets-image-grants.sql`。Native 私有表归属及保存输出的必要列权限由 `infra/docker/module-data-grants.sql` 安装给 NOLOGIN Assets owner；control/worker/cleanup LOGIN 仍只有有限函数能力，迁移身份单独使用。cleanup 的 11 项能力（五条 lane）沿用 module-data 授权流程；Native 保存输出 lane 不增加直接对象权限。

`docker-compose.assets-media.yml` 不属于默认 Compose。分离执行选 `ASSETS_IMAGE_EXECUTOR=split` 与 `assets-image-executor` profile，配置 `ASSETS_IMAGES_DATABASE_URL_FILE`、`ASSETS_IMAGES_S3_ACCESS_KEY_FILE`、`ASSETS_IMAGES_S3_SECRET_KEY_FILE`（UID 10005 可读）。`ASSETS_MEDIA_URL/TOKEN` 只选择独立控制；需要时另启 `assets-media-control` profile。显式远程配置失败不回退，三个进程不共用数据库或高权限 S3 身份。

默认 Celery 图片执行使用固定函数 LOGIN 和 `ASSETS_CELERY_IMAGE_DATABASE_URL(_FILE)`，包括普通图片领取、写入和已受理输入准备。启用 Native 另给实际执行者 writer token，不给 API。切到 split 时移除该 Celery 绑定，改用 split worker 的 `ASSETS_IMAGES_DATABASE_URL_FILE`。见[媒体执行角色](../deploy/docker-compose#media-executor-provisioning)。

S3 权限应限定为：

- 控制端对已登记源图和缓存对象 HEAD/GET，并签发短期读取 URL；不写缓存或删除对象。
- 图片 Worker 对源桶只读；对 `image-assets/*`、`image-pyramids/*` 派生前缀读写和校验，并在 `image-pyramids/*` 受限列举以做协调；不写/删源对象。
- 清理身份只对缓存桶中由数据库签发的图片派生 prefix 做 List/Delete；不授予 Get/Put、源桶访问或桶管理。Native lane 不获得 Native 对象读取/删除权限，只执行 Source 签发的精确保存成员与空前缀请求。
- Native 内建身份（在 Source 内）只对缓存桶 `image-pyramids/objects/*` 与 `_aap_managed/media-cache/image-pyramids/objects/*` 保存输出执行精确 GetObject/GetObjectVersion、PutObject、DeleteObject/DeleteObjectVersion、受限 ListBucket/ListBucketVersions 和桶级 ListBucketMultipartUploads；不授予桶管理或其他对象权限，并显式拒绝 AbortMultipartUpload 与 ListMultipartUploadParts，防止兼容后端把 PutObject 扩展为额外权限。模板见 `infra/docker/native-source-minio-policy.json`，其中的桶名须替换为实际 media-cache 桶；Native 用途的专用连接身份也须保留这两项拒绝。普通清理身份还须明确拒绝这两类 Native 输出的删除，避免既有 `image-pyramids/*` 授权覆盖它们。
- 已受理输入另用 `ASSETS_MEDIA_INPUTS_BUCKET` 指向独立 versioning 桶（默认 `accepted-inputs`）；控制端与 Worker 仅读 versioning 状态，Worker 只在该桶写入不可变输入版本，输入清理身份只列举输入版本/未完成 multipart 并删除指定版本。清理权限包括 `ListBucketVersions`、`ListBucketMultipartUploads`、`AbortMultipartUpload`、`DeleteObjectVersion`，以及仅在请求包含非空且非 `null` 的 `s3:versionid` 时允许的 `DeleteObject`；清理端逐个删除 `(key, versionId)`，不发无版本 ID 的删除或 S3 批量删除。不得授予任何身份配置 versioning、创建桶或管理整个桶的权限。

输入桶需通过 `MINIO_INPUTS_BUCKET` 与 `ASSETS_MEDIA_INPUTS_BUCKET` 配置，并且必须启用 versioning；不要给它或 `media-cache` 添加桶级生命周期。Compose overlay 默认让独立控制与图片 Worker 继承 API bucket 名称。清理 worker 依照输入保留账本逐版本回收，不能按 age 或生命周期直接删除对象版本。

实际控制端与执行器使用同一数据库，核对 endpoint、region、桶与保存身份配置。公共 S3 endpoint 必须对浏览器可达，不改写签名 URL。历史 Task 图像先登记 TaskOriginal；缺少原件归属时不按当前 annotations 桶猜读。

### Native 签名与保存输出配置

Native 金字塔以 Source 为唯一保存目标凭据 owner 和有限签名方。内建目标使用独立 Native S3 身份；已注册连接目标只由 Source 使用其既有加密凭据、连接密钥与主机策略。Images 控制/执行器、API 本地实现和 Cleanup 的 Native 路径只配置按角色限定的 URL 与 token，不取得保存目标的原始凭据；普通原件读取仍沿用各自受限身份。实际目标坐标来自 owner 保存合同，不能由调用方任意选择。

Source 的五项设置必须同时配置，否则拒绝启动；三项 token 互不相同，并区别于 `ASSETS_SOURCE_TOKEN`、`ASSETS_SOURCE_IMPORT_VERIFY_TOKEN`；Native access key 必须区别于普通 Source 身份。全部留空时功能关闭，Native 路由返回 `source_unavailable`，调用方不会回退其他签名方或当前缓存桶。

| 调用方                    | 设置                                                                                                  | 运行用户         |
| ------------------------- | ----------------------------------------------------------------------------------------------------- | ---------------- |
| 所选图片执行器            | `ASSETS_IMAGES_NATIVE_STORAGE_URL`、`ASSETS_IMAGES_NATIVE_STORAGE_TOKEN` 或 `_TOKEN_FILE`             | 所选 worker 用户 |
| 共享控制端 / API 本地控制 | `ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL`、`ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN` 或 `_TOKEN_FILE`   | 10005 / API 进程 |
| Assets Cleanup            | `ASSET_CLEANUP_NATIVE_STORAGE_URL`、`ASSET_CLEANUP_NATIVE_STORAGE_TOKEN` 或 `_TOKEN_FILE`（两项必填） | 10001            |

各 token 值与 Source 对应角色相同，但用各自运行用户可读的不同文件。`docker-compose.assets-native.yml` 为 Source 提供五类 secret，为 `assets-media-control` 提供 reader，为图片执行器提供 writer；`docker-compose.assets-cleanup.yml` 自带清理 token。内嵌 API 控制须另设 reader 的 URL 与 `_TOKEN_FILE` 宿主路径；不把执行或清理 token 挂给 API。默认文件名：

```text
.secrets/assets-source-native-pyramid-writer-token
.secrets/assets-source-native-pyramid-reader-token
.secrets/assets-source-native-pyramid-cleanup-token
.secrets/assets-source-native-s3-access-key
.secrets/assets-source-native-s3-secret-key
.secrets/assets-cleanup-native-storage-token
.secrets/assets-images-native-storage-token
.secrets/assets-media-native-image-storage-token
```

Cleanup 要求 Native URL/token，即使 `IMAGE_PYRAMID_AUTO_GENERATE=false`；使用它前需先启用 Source signer 和合格的保存目标。URL 从实际调用进程验证可达，宿主 API 不能直接使用 Compose 服务名。控制模式不改变所选执行器的 writer。部分配置明确失败，不降级为当前缓存桶或普通凭据。公开 URL 有效期依据冻结策略 `IMAGE_PYRAMID_URL_EXPIRY_SECONDS` 和 SigV4 604800 秒上限，超限明确失败。

## 启动与健康检查

运行环境必须包含 libvips、pyvips 和可用的 sRGB ICC profile。先检查实际挂载：挂载 Python 源码变更只需刷新对应 Worker；依赖、Dockerfile 或镜像内源码变化才需要重建。

默认执行设 `ASSETS_IMAGE_EXECUTOR=celery`，使用已迁移、授权和启动的默认栈；不需要媒体 overlay。用 `docker compose ps celery-worker-image-pyramid` 确认对应 worker，主 worker 不是该队列的执行者。

需要 split 时暂停新受理，排空旧执行器与实际对象 I/O 后，把 `ASSETS_IMAGE_EXECUTOR=split` 同步给 API／worker；再启独立 `assets-images-worker`。它轮询数据库而不依赖 Redis／Beat，默认间隔 `ASSETS_IMAGES_POLL_SECONDS=5`。以下为同项目 Native 保存输出示例；确保 [Native 授权](#native-签名与保存输出配置)已配置：

```bash
images_stack() {
  docker compose -f docker-compose.yml -f docker-compose.assets-source.yml \
    -f docker-compose.assets-media.yml -f docker-compose.assets-cleanup.yml \
    -f docker-compose.assets-native.yml --profile assets-image-executor "$@"
}
images_stack up -d --build --wait assets-source assets-images-worker assets-cleanup
images_stack ps
images_stack exec assets-images-worker python -m yoao_assets_images_runtime.worker --healthcheck
images_stack exec assets-cleanup python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/ready', timeout=2)"
```

不使用 Native 时只给基础文件叠加 `docker-compose.assets-media.yml` 启动图片执行器；要使用独立控制，另选 `--profile assets-media-control`、启动 `assets-media-control` 并设置控制 URL/token。Native 组合保留完整 `-f` 文件链。Source 在同一网络使用 `http://assets-source:8767`；独立控制只 expose 8768，不发布宿主端口。容器预算 4 CPU／16 GiB 内存／12 GiB 临时盘是启动配置，不是容量证明。

实际 `--healthcheck` 只检查配置、连接和受限函数权限。不要用健康检查代替真实源 HEAD／GET、Job 推进、manifest／asset URL、读回核验及精确清理测试。验证 split 时，还要确认未开启相同工作流的 Celery 领取。

独立控制与 split 图片执行器的宿主直启入口（分别注入自己的环境）：

```bash
uv run --project apps/assets-media --extra runtime --extra server python -m yoao_assets_media_runtime
ASSETS_IMAGE_EXECUTOR=split uv run --project apps/assets-media --extra runtime python -m yoao_assets_images_runtime.worker
```

## 动态保存目标与 Native 清理

媒体缓存用途绑定只决定**新** Native 输出写到哪里。每个输出在受理时把 `endpoint_url`、`bucket`、`key_prefix`、`prefix` 和 `storage_identity` 保存到不可变目标；之后写入、读取、校验与回收都使用该保存目标，而不是当前绑定。

- 把绑定从 A 切到 B 后，新输出写入 B；A 上的旧输出仍从 A 读取和回收。不需要为 Images 控制端、图片 Worker 或 Cleanup 改 endpoint、bucket 或 secret，也不要把 B 的坐标传给它们去读取旧输出。
- 修改 A 的注册位置不是重定向：保存的 endpoint、bucket、prefix 与身份必须和注册位置或连接一致；不一致、连接被修改或位置缺失都明确拒绝，不回退当前桶。
- 已禁用的位置仍可按既有保存目标策略读取/回收；新的写入仍要经过启用、位置和维护门检查。
- Source 只签发精确保存成员和空前缀请求；普通 API、Images 与 Cleanup 不解析原始连接凭据，也不重建当前桶。外部注册连接仍由 Source 使用其既有加密凭据与主机策略。

Cleanup 现在有五条独立 lane 和 11 项精确 owner 函数：既有 outbox、图片垃圾和两类输入墓碑保持不变，新增 Native 保存输出回收；该 lane 只由独立 Cleanup 进程执行，API 与 Celery 不提供回退。Native lane 的严格语义：

- 只领取已到期的保存输出，按不可变成员页处理；先由 Source 为该页签发精确的 conditional HEAD/GET/DELETE，再执行完整 GET 校验 size、SHA-256、ETag 与记录版本，随后精确删除该对象/版本，并再次条件 HEAD 证明不存在。
- 只有具备完整保存回执的 `written` 或 `verified` 成员可退休；已写入但未验证的成员必须先完成自身生产 I/O 的关闭并移除操作许可，未知 I/O 保留 durable barrier。`submitted`/`unknown` 成员永不领取，也不能由缺失回执推断成功。
- 原始版本字面量 `null` 与缺失版本保持不同；不得用猜测版本替代保存版本。任何外来 key、版本、delete marker 或 multipart upload 都不会被删除。
- 成员页清空后，对保存前缀做有界对象和版本列举；发现条目或截断页便拒绝完成。Multipart 使用桶级分页并同时携带 key/upload 游标，完整核对保存前缀内的上传；发现属于该前缀的上传或无法完整证明分页结束时拒绝完成。外来上传既不删除，也不单独阻止本输出退休，不能把一次前缀过滤请求当作所有 provider 的充分证明。
- Native 清理要求存储后端遵守分页上限、完整游标与重启后的完整枚举合同。当前仓库固定的 MinIO 构建不满足此资格：实测 `MaxUploads=100` 返回 121 项且 `IsTruncated=false`，其内存缓存列表在重启后也不能证明磁盘上传不存在。桶级分页不能补足这两项缺陷；该构建不得被声明为 Native 清理合格后端。详见[存储迁移库存的 multipart 限制](../deploy/storage-migration-inventory.md)。候选后端还必须单独通过条件读写删除、版本与受限身份验证，不能仅凭替换验收后端推断生产支持。
- 页面准备未知、确认丢失、取消排水失败或未知 I/O 都保留责任；租约到期、进程丢失或超时本身不证明物理关闭。
- 公开 URL 按冻结的保留期等待后再退休；已签发的短期 URL 不会因切换当前绑定而延长或重定向。

## 策略与 profile

| 设置                                                                |                    默认值 | 说明                                       |
| ------------------------------------------------------------------- | ------------------------: | ------------------------------------------ |
| `IMAGE_PYRAMID_AUTO_GENERATE`                                       |                   `false` | 新图自动金字塔受理开关；尺寸和缩略图仍受理 |
| `IMAGE_PYRAMID_PROFILE_VERSION`                                     |              `pyramid-v1` | 读取的金字塔 profile/lineage               |
| `IMAGE_PYRAMID_NORMALIZATION_VERSION`                               | `exif-autorotate-srgb-v1` | EXIF 与 sRGB 归一化规则                    |
| `IMAGE_PYRAMID_OPTIONAL_PIXELS` / `IMAGE_PYRAMID_REQUIRED_PIXELS`   |   16,777,216 / 50,000,000 | 可选/必须使用金字塔的像素门槛              |
| `IMAGE_PYRAMID_MAX_PIXELS` / `IMAGE_PYRAMID_MAX_DIMENSION`          |      300,000,000 / 32,768 | 解码像素和单边硬上限                       |
| `IMAGE_PYRAMID_MAX_TILES`                                           |                    20,000 | 单代 tile 上限                             |
| `IMAGE_PYRAMID_MAX_SOURCE_BYTES`                                    |                     8 GiB | 最大压缩源大小                             |
| `IMAGE_PYRAMID_MAX_DERIVED_BYTES` / `IMAGE_PYRAMID_MAX_TEMP_BYTES`  |                8 / 12 GiB | 派生字节与临时空间上限                     |
| `IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS` / `IMAGE_PYRAMID_LEASE_SECONDS` |          1,800 / 2,100 秒 | 租约必须大于超时并留出硬终止余量           |
| `IMAGE_PYRAMID_RETRY_COOLDOWN_SECONDS`                              |                    300 秒 | 失败后的重试冷却                           |
| `IMAGE_PYRAMID_GC_GRACE_HOURS`                                      |                   24 小时 | 代次失活后的默认保留期                     |
| `IMAGE_PYRAMID_URL_EXPIRY_SECONDS`                                  |                    900 秒 | 派生读取 URL 有效期                        |
| `IMAGE_PYRAMID_VIPS_CONCURRENCY`                                    |                         4 | 单 Worker libvips 并发                     |
| `ASSETS_IMAGES_SRGB_PROFILE`                                        |             镜像 ICC 文件 | Worker 可覆盖的绝对路径                    |

作业受理时固定策略。API 与共享控制的 profile 必须一致；切换 profile/normalization version 前规划新输出线与回填范围，不要只改一台服务。`VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` 是 Web 构建时开关，改动后需重建 Web；它不控制自动受理。

## 有界回填、状态与重试

脚本先做有界 source 扫描，再受理持久 backfill Job；S3 观察和子作业入队由 Worker 完成。先 dry-run，再用小批次。`dataset_item` 可用 dataset id；`task` 仅指没有 DatasetItem 关联的历史 Task，不能带 dataset id。独立 Task 必须先有已登记 TaskOriginal，缺少原件归属的历史 Task 不按当前 annotations 桶猜读；CLI 显式选择，Worker 完整核验原件后应用硬上限。跨游标分页时固定带时区 cutoff：

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

原生 `read_image_usage` 返回 `source_bytes`、`thumbnail_bytes`、`pyramid_bytes`、`pending`、`running`、`failed` 和 `garbage_pending`。如果显式启用了私有共享控制端，可调用：

```bash
curl --fail --silent --show-error -H 'Authorization: Bearer '"$ASSETS_MEDIA_TOKEN" -H 'Content-Type: application/json' -d '{}' "$ASSETS_MEDIA_URL/v1/images/commands/read_image_usage"
```

`source_bytes` 是已登记 DatasetItem 源大小合计，不含直接历史 Task 源图；`thumbnail_bytes` 是 ready thumbnail Job 记录的派生字节；`pyramid_bytes` 是 active ready 金字塔代次保留字节；pending、running 和 failed 计数跨所有图片作业，`garbage_pending` 为未完成清理责任数。这些是 SQL owner 读数，不等于 S3 账单或全桶盘点。

Worker 日志使用稳定错误码，不应记录来源凭据、签名 URL 或原始文件。遇错检查 profile、登录角色、endpoint/bucket、ICC 路径和容器 RSS/临时盘。`upload_verification_failed` 或 `inconsistent_ready` 应核对缓存对象身份与可达性，不要手工伪造 ready 状态。

浏览器 BUG 反馈会附带 `Large Image Tile Diagnostics`，其中包括 current level、visible/desired tile、
queue/fetch/decode 状态、cache hit/eviction、requested/decoded/reserved/retained/budget bytes、
ImageBitmap/HTML image/ObjectURL 存量、abort、stale commit、签发批次数、URL 刷新和目标细节
coverage。该快照不包含签名 URL、storage key 或文件名。排查长会话时应确认停止交互后
retained/live/request 进入平台档位内 plateau，离开任务后 reserved/retained/live 全部归零。

同一份 BUG 反馈还会附带 `Workbench Raster Resource Diagnostics`。它是浏览器页签内、当前 Task 的逻辑
账本，不是服务端或物理 GPU 指标。按以下顺序排查：

1. `invariantOk=true`，且 `chargedBytes == committedBytes + reservedBytes <= hardBudgetBytes`；
2. `owners` 与 `categories` 的 committed/reserved 合计分别等于全局值；
3. `pressureReason=foreground-operation` 时背景 prefetch 应暂停，operation 结束后按当前 generation 恢复；
4. hidden 达阈值后 `hiddenSheds` 增加、reconstructible bytes 下降，dirty edit/history 仍留在 P0；
5. BFCache 往返后 generation 增加，旧 promise 只增加 stale/abort 计数，coverage 能恢复；
6. 真正离开任务后 coordinator、tile、Mask render/edit/history、Worker/GPU owner 全部归零。

低、标准、高档位的全局 hard budget 分别为 192/384/768 MiB，hidden freeze 为 10/15/30 秒。
档位来自浏览器 `navigator.deviceMemory`，字段缺失时使用标准档；production 没有环境变量可以绕过 hard
invariant。若持续出现 denial，先核对 owner 是否漏 release、是否把同一 buffer 重复计费，以及视口/Mask
ROI 是否过大，不要通过无限调高预算掩盖泄漏。

资源 pressure 与 tile 故障的口径不同：前者通常保持 overview/coverage，只短暂显示清晰度恢复；后者会在
tile diagnostics 增加 error、签名刷新或 decode failure。Mask operation 被总预算拒绝时，用户现有编辑和
history 不变，可缩小可见区域或先保存再重试。浏览器诊断是页签内逻辑账本，不是 Worker 队列、服务器字节或 GPU 物理指标。

## 保留、清理与回滚

ready 代次的 URL 签发使用非阻塞来源/垃圾栅栏检查；同一代次每 10 分钟最多更新一次访问时间，并延长该代次保留截止。退休代次按其捕获的 `gc_grace_hours`（默认 24 小时）等待回收。清理已持有租约或已完成删除后，不再签发新 URL；旧签名链接仍有短期有效期。

图片 Worker 只登记派生前缀，不直接删对象。既有 `assets-cleanup` 领取事务提交后才做 S3 删除并确认；失败会保留责任。Cleanup 现在运行五条独立 lane（outbox、图片垃圾、Native 保存输出回收、两类输入墓碑）和 11 项精确 owner 函数；Native lane 只执行 Source 签发的精确保存成员/空前缀请求，不持有 Native 对象凭据，也不把当前绑定当作旧输出的位置。整 asset 与 attempt prefix tombstone 保留并每 5 分钟重扫，以清除迟到写入。`garbage_pending` 持续非零时检查 cleanup 健康、数据库租约、Native signer 连通性和精确 S3 Delete/List 权限。禁止对活跃 `image-pyramids/*` 配置整桶/固定 TTL 生命周期，禁止清理身份访问源桶或获得 Native 对象读/删权限。

reconciliation 每日运行，按 Asset→Generation→Output 锁序复验过期 lease、移除非 active 旧代引用，再退役无引用且无在用操作的独立 output。物理清理在 Gate 准入下只删除保存桶中的该 UUID 前缀，核对为空后保留 deleted 记录；失败留待重试。业务删除只解除引用，不在请求事务里猜当前缓存桶清理。

准备阶段不读取对象，输出分配只使用已登记的 media-cache 绑定。现有 owner 不跟随当前用途桶变化；用途绑定从 A 切到 B 只影响新输出，旧输出仍按保存目标读取、校验和回收。所有 tile、overview、manifest 写入后完整 GET 回读 SHA／长度。发布还需有效 lease、准确原件和委派、输出 owner 与维护修订一致，原生处理、Body、文件和临时目录先真实收尾。取消、超时或 Worker 丢失不能仅靠租约到期解除未知 StorageOperation；未确认的操作会阻止对应 owner 退休，需要先定位实际进程和句柄，不能直接删账本。

旧无 output owner 的代不自动采纳、签名或物理删除；先完成原件登记，再显式重试生成新代，旧未知文件保留待单独核验。reconciliation 不裸扫默认缓存桶，也不按目录年龄删除其他任务临时文件。0232 迁移新增独立责任表和输入／授权回执；存在责任记录时不做破坏性降级。

紧急暂停：关闭 `IMAGE_PYRAMID_AUTO_GENERATE` 并停止显式重试／回填；若需停全部新工作，还应暂停上传／导入生产者（尺寸和缩略图仍会受理）。按当前选档暂停 `celery-worker-image-pyramid` 或 `assets-images-worker`，保留控制、清理与待确认责任。要暂停浏览器切片选择，设 `VITE_EXPERIMENTAL_LARGE_IMAGE_TILES=false` 后重建 Web。保留 Job、回执、ready 对象及账本，不删表或批量删对象。停用 Native 前先保留 signer、存储身份和旧保存目标访问，直到 Cleanup 实际排空；无法确认在途 I/O 结束时隔离相应清理 token，不删保存目标账本。

常见新增拒绝包括 `no_readable_original`／`content_unverified`（先登记并核验原件）、`pyramid_authority_unrecorded`（旧队列缺准确委派，重新发起）、`image_pyramid_storage_unpinned`（未知旧输出，显式重建）、`image_pyramid_storage_integrity_mismatch`／`image_pyramid_storage_invalid_receipt`（核对对象与内容清单）以及 `storage_operation_unsettled`（物理工作尚未确认结束）。维护或撤权拒绝不代表文件丢失，也不应通过更换当前用途桶绕过。
