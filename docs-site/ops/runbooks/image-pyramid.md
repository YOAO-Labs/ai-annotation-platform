---
title: 图片金字塔运行手册
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-10-24
---

# 图片金字塔运行手册

本手册用于部署、回填和诊断超大图金字塔。自动生成默认关闭；API、数据库和专用 Worker 可以先上线，再按
容量证据启用。

## 部署检查

运行环境必须包含 libvips、pyvips 和可用的 sRGB ICC profile。先检查实际挂载：挂载 Python 源码变更只需刷新对应 Worker；依赖、Dockerfile 或镜像内源码变化才需要重建。需要重建时：

```bash
docker compose build celery-worker-image-pyramid
docker compose up -d celery-worker-image-pyramid celery-worker celery-beat
```

先执行数据库迁移，再确认专用 Worker 只消费 `image-pyramid` 队列且并发为 1：

```bash
docker exec <api-container> alembic upgrade head
docker compose ps celery-worker-image-pyramid
docker compose logs --tail=100 celery-worker-image-pyramid
```

容器内快速检查：

```bash
docker compose exec celery-worker-image-pyramid \
  python -c "import pyvips; print(pyvips.version(0), pyvips.version(1), pyvips.version(2))"
docker compose exec celery-worker-image-pyramid \
  test -r /usr/share/color/icc/sRGB.icc
```

## 关键配置

| 配置                                   |        默认值 | 作用                          |
| -------------------------------------- | ------------: | ----------------------------- |
| `IMAGE_PYRAMID_AUTO_GENERATE`          |       `false` | 上传/缩略图流程是否自动入队   |
| `IMAGE_PYRAMID_OPTIONAL_PIXELS`        |    `16777216` | 允许生成的最低像素数          |
| `IMAGE_PYRAMID_REQUIRED_PIXELS`        |    `50000000` | 客户端必须走 tile 的像素门    |
| `IMAGE_PYRAMID_MAX_PIXELS`             |   `300000000` | 最大解码像素                  |
| `IMAGE_PYRAMID_MAX_DIMENSION`          |       `32768` | 任一边最大尺寸                |
| `IMAGE_PYRAMID_MAX_TILES`              |       `20000` | 单代次最大 tile 数            |
| `IMAGE_PYRAMID_MAX_SOURCE_BYTES`       |  `8589934592` | 最大压缩源字节                |
| `IMAGE_PYRAMID_MAX_DERIVED_BYTES`      |  `8589934592` | 最大持久派生字节              |
| `IMAGE_PYRAMID_MAX_TEMP_BYTES`         | `12884901888` | source 与生成目录临时空间上限 |
| `IMAGE_PYRAMID_JOB_TIMEOUT_SECONDS`    |        `1800` | Worker soft timeout           |
| `IMAGE_PYRAMID_LEASE_SECONDS`          |        `2100` | generation lease              |
| `IMAGE_PYRAMID_URL_EXPIRY_SECONDS`     |         `900` | overview/tile URL 有效期      |
| `IMAGE_PYRAMID_RETRY_COOLDOWN_SECONDS` |         `300` | 失败后重试冷却                |
| `IMAGE_PYRAMID_VIPS_CONCURRENCY`       |           `4` | 单 job libvips 并发           |
| `VITE_EXPERIMENTAL_LARGE_IMAGE_TILES`  |        `true` | 前端是否选择 ready pyramid    |

`profile_version` 或 `normalization_version` 变更会产生新资产身份/输出合同，不能只改一台 Worker。所有 API、
media Worker 和 image-pyramid Worker 必须使用一致值。

`VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` 是 Vite build-time 开关，修改后必须重建 Web 镜像。它与
`IMAGE_PYRAMID_AUTO_GENERATE` 独立：前者只控制浏览器选择，后者只控制新资产是否自动入队。

## 安全启用与回填

先保持自动生成关闭，用 dry-run 估算 DatasetItem 数量：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py \
  --owner-kind dataset_item --dry-run --limit 100
```

按 cursor 小批量入队：

```bash
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py \
  --owner-kind dataset_item --cursor <uuid> --limit 100
```

独立 Task 必须先有已登记 TaskOriginal，缺少原件归属的历史 Task 不按当前 annotations 桶猜读。CLI 显式选择，Worker 完整核验原件后应用硬上限：

```bash
PYTHONPATH=. uv run python scripts/backfill_image_pyramids.py \
  --owner-kind task --dry-run --limit 100
```

浏览器 E2E、性能基准和文档截图可按需下载固定的现实大图 seed；原图写入 gitignored 的
`test-results/image-seeds/`，下载后必须通过固定字节数与 SHA-256：

```bash
pnpm --filter @anno/web image:seeds -- --list
pnpm --filter @anno/web image:seeds
pnpm --filter @anno/web image:seeds -- --verify-only
```

清单包含高熵 RGB PNG、接近硬上限的超宽 JPEG 和 optional 边界的超高 RGBA 竖图。每项记录 NASA
官方来源页、原始下载地址、署名和媒体使用政策；自动化输出不得暗示 NASA 或合作机构背书。

当前开发栈可把已校验原图导入固定项目/数据集，并显式等待专用 Worker 生成完成：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --enqueue-pyramids --wait-seconds 1800
```

命令幂等创建 `P-LARGE-IMG` / `DS-LARGE-IMG` 与逐图 Task。它不会开启全局
`IMAGE_PYRAMID_AUTO_GENERATE`，只为本次选中的 eligible 夹具入队；重复运行复用同源 active
generation。可用重复的 `--id <fixture-id>` 缩小范围。production 环境会拒绝执行。

观察连续任务的 RSS、临时盘、失败率和队列积压稳定后，才把
`IMAGE_PYRAMID_AUTO_GENERATE=true` 注入 API/media Worker 与专用 Worker，并重建或重启对应服务。

## 观测与告警

重点指标：

- `image_pyramid_generations_total{outcome,error_code}`：成功与稳定故障码；
- `image_pyramid_phase_duration_seconds{phase,outcome}`：下载、生成、校验和上传耗时；
- `image_pyramid_source_bytes`、`image_pyramid_decoded_pixels`、`image_pyramid_tile_count`；
- `image_pyramid_derived_bytes`、`image_pyramid_temp_bytes`、`image_pyramid_asset_bytes`；
- `image_pyramid_api_requests_total{operation,outcome}`；
- `image_pyramid_gc_total{outcome}`。

建议告警：

- `failed` 比例持续升高；
- `lease_expired`、`timeout` 或 `resource_limit` 突增；
- `object_missing`/`inconsistent_ready` 非零；
- 队列持续增长且 Worker 无 ready 结果；
- Worker RSS 接近 `--max-memory-per-child` 或临时卷接近容量上限。

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
history 不变，可缩小可见区域或先保存再重试。

## 故障码处理

| 错误码                                  | 含义                                   | 处理                                             |
| --------------------------------------- | -------------------------------------- | ------------------------------------------------ |
| `source_missing` / `source_unavailable` | 源对象不存在或无法读取                 | 先修复 source/MinIO，不要盲目重试                |
| `source_changed`                        | 生成期间或读取时源身份变化             | 确认替换完成后重试新 generation                  |
| `unsupported_dimensions`                | 尺寸或像素超过硬门                     | 保留原路径或调整产品限制，不能只放大内存         |
| `resource_limit`                        | source、tile、derived 或 temp 预算超限 | 检查输入和容量证据后再调整单项上限               |
| `decode_failed`                         | 损坏或 loader 不支持                   | 校验源文件格式                                   |
| `multi_page_unsupported`                | 多页输入                               | 先显式抽取目标页再入库                           |
| `dimension_mismatch`                    | DB 逻辑尺寸与规范化输出不一致          | 不自动迁移已有标注；人工核查 EXIF/历史数据       |
| `color_profile_unavailable`             | sRGB profile 缺失                      | 修复镜像或配置路径                               |
| `upload_verification_failed`            | 上传对象集合/大小不一致                | 检查 MinIO、网络与凭据                           |
| `object_missing` / `inconsistent_ready` | ready 资产不完整                       | API 会停止签发；检查 lifecycle 和 reconciliation |
| `lease_expired` / `lease_lost`          | Worker 超时、重启或竞争                | 检查 Worker 健康，等待 reconciliation 后重试     |

## GC 与回滚

Celery beat 每日触发 reconciliation，按 Asset→Generation→Output 锁序复验过期 lease、移除非 active 旧代引用，再退役无引用且无在用操作的独立 output。物理清理在 Gate 准入下只删除保存桶中的该 UUID 前缀，核对为空后保留 deleted 记录；失败留待重试。业务删除只解除引用，不在请求事务里猜当前缓存桶清理。

准备阶段不读取对象，输出分配只使用已登记 media-cache 绑定。现有 owner 不跟随当前用途桶变化；所有 tile、overview、manifest 写入后完整 GET 回读 SHA／长度。发布还需有效 lease、准确原件和委派、输出 owner 与维护修订一致，原生处理、Body、文件和临时目录先真实收尾。取消、超时或 Worker 丢失不能仅靠租约到期解除未知 StorageOperation；未确认的操作会阻止对应 owner 退休，需要先定位实际进程和句柄，不能直接删账本。

旧无 output owner 的代不自动采纳、签名或物理删除；先完成原件登记，再显式重试生成新代，旧未知文件保留待单独核验。reconciliation 不裸扫默认缓存桶，也不按目录年龄删除其他任务临时文件。0232 迁移新增独立责任表和输入／授权回执；存在责任记录时不做破坏性降级。
不要给 `image-pyramids/` 单独配置固定天数 lifecycle，否则会留下 ready 数据库行指向缺失对象。

紧急回滚时：

1. 设 `IMAGE_PYRAMID_AUTO_GENERATE=false` 并重启 API/media Worker；
2. 如需停止客户端选择，设 `VITE_EXPERIMENTAL_LARGE_IMAGE_TILES=false` 并重建 Web 镜像；
3. 停止专用 Worker，保留数据库表和 ready 对象；
4. required 大图会显示当前产品限制与安全预览，不会自动退回无界整图解码；
5. 不通过批量删除 ready 对象回滚。

保留状态后可以修复 Worker 并显式 retry，无需重新上传 source。

常见新增拒绝包括 `no_readable_original`／`content_unverified`（先登记并核验原件）、`pyramid_authority_unrecorded`（旧队列缺准确委派，重新发起）、`image_pyramid_storage_unpinned`（未知旧输出，显式重建）、`image_pyramid_storage_integrity_mismatch`／`image_pyramid_storage_invalid_receipt`（核对对象与内容清单）以及 `storage_operation_unsettled`（物理工作尚未确认结束）。维护或撤权拒绝不代表文件丢失，也不应通过更换当前用途桶绕过。
