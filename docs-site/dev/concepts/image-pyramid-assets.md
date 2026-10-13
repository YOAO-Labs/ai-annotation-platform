---
title: 超大图金字塔派生资产
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-07-31
---

# 超大图金字塔派生资产

图片金字塔把超大源图转换为可按视口加载的 overview 和 tile。它是服务端派生资产：源图片和标注坐标仍是
权威数据，金字塔可重建，不能反向覆盖 source。

图片与审核工作台会把 ready pyramid 解析为视口图片源，只加载 overview、当前 LOD 可见 tile 和有界
overscan。没有 ready pyramid 的小图继续使用原 single-image 路径。

## 可复现开发夹具

浏览器基准、文档截图和服务端生成验证共用
`apps/web/scripts/image-bench/fixtures.json` 的 `realLargeImages` 清单。三种输入分别覆盖高熵 RGB、
接近硬上限的超宽 JPEG，以及 optional 门附近的超高 RGBA 竖图；清单固定尺寸、字节数、SHA-256、
来源页、署名和使用政策。

下载器只把原图写到 gitignored 的 `test-results/image-seeds/`。API 脚本会在写数据库和对象存储前再次
核对完整性，幂等创建 `P-LARGE-IMG` / `DS-LARGE-IMG` 和逐图 Task，并可显式入队、等待 generation
进入终态：

```bash
pnpm --filter @anno/web image:seeds
cd apps/api
PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --enqueue-pyramids --wait-seconds 1800
```

该入口只服务 development/staging，production 会拒绝执行。固定资源带 seed ownership 标记；如果同一
display ID 已被其它数据占用，脚本不会自动接管或覆盖。

## 所有权与代次

`image_pyramid_assets` 表示稳定 owner：

- DatasetItem 来源以 DatasetItem 为 owner，多个 Task 共用一份资产；
- 未关联 DatasetItem 的 direct Task 以 Task 为 owner；
- owner 与 `profile_version` 唯一，数据库约束保证两种 owner 恰有一个非空。

`image_pyramid_generations` 表示一次不可变生成：

```text
pending → building → ready
                    └→ failed
```

同一 asset 同时最多有一个 pending/building generation。Worker 在所有对象上传和校验成功后，才在事务中
更新 `active_generation`。同源重建时旧 active 可继续提供服务；源被替换后旧 active 立即失效，避免旧像素
与新坐标叠加。

对象布局是服务端实现细节：

```text
image-pyramids/{asset_id}/g{generation}/
  manifest.json
  overview.webp
  tiles/{level}/{x}/{y}.webp
```

## 源身份与规范化

快速 source fence 由对象 ETag、字节数和可用的 version ID 组成。Worker 同时在流式下载时计算 SHA-256，
作为 manifest 的内容 fingerprint。生成前和发布前都会再次验证快速身份，覆盖生成期间源对象被替换的竞态。

规范化 profile 的合同为：

- 应用 EXIF autorotate，输出宽高是旋转后的逻辑尺寸；
- 有 ICC profile 时转换为 sRGB，否则把非 sRGB interpretation 转为 sRGB；
- 灰度转成 sRGB，alpha 保留；
- 多页图片拒绝，不静默选择任意页；
- 数据库已有逻辑尺寸与生成结果不同时稳定失败，不改变已有标注坐标。

生产生成器是 pyvips/libvips。Pillow 只用于小图缩略图、header probe 和测试 oracle，不是超大图生成兜底。

## Manifest 合同

manifest schema 是 `aap-image-pyramid/v1`：

- level 按 full-resolution-first 排列，`scaleFactor` 为二次幂；
- core tile 为 512×512，存储 tile 允许 1px overlap；
- edge 宽高、columns 和 rows 都由整数 ceil 规则显式计算；
- tile 使用 WebP；
- manifest 包含 generation、规范化版本和内容摘要；
- manifest 不包含 bucket、存储 key、host 或签名 URL。

客户端不得依赖对象前缀，也不得从 tile 坐标自行拼存储 key。

## 客户端视口调度

Task 层先把图片解析成 `single`、`pyramid`、`pyramid-pending` 或 `pyramid-failed`。required 大图在
building/failed 或客户端 gate 关闭时只使用 overview、thumbnail 或 blurhash，不自动请求 original；
optional 图片可以继续使用有界的 single-image 路径。

ready pyramid 的 level 由 `viewport scale × scaleFactor × devicePixelRatio` 决定，当前 level 在
`0.75..1.25` 的采样区间内保持，避免连续缩放时跨阈值抖动。tile node 仍占 full-resolution world rect；
1px 存储 overlap 只用于 crop，不改变标注、Mask、Issue 或 Minimap 坐标。

调度器分两阶段工作：

```text
逻辑坐标批签（最多 128 项）
  → 有界 fetch / Blob
     → createImageBitmap
        └─ 失败时 HTMLImageElement + ObjectURL
```

低、标准、高设备档位的 decoded tile budget 分别为 32/64/128 MiB，并发为 2/4/6。成本按
`decodedWidth × decodedHeight × 4` 计算；可见 tile pin，非可见 tile 按 LRU 淘汰。
`ImageBitmap.close()`、ObjectURL revoke、请求 abort、reservation 和 stale commit 都进入 resource
snapshot。overview 与已缓存的粗层级在目标 tile 到达前保持，单 tile 失败不会制造空白棋盘。
短期 tile URL 过期或首次拉取失败时只重新批签一次；仍失败的可见区域由 overview/粗层级覆盖，
并在五秒冷却结束后自动重试，无需用户平移或缩放视口。

背景路径不读取 `navigator.gpu`。Raster Mask 的 WebGPU/CPU 计算与图片 tile 保持独立 owner 和释放语义，
但共同进入当前 Task 的[栅格资源协调账本](./raster-resource-coordination)。前台 Mask 运算会暂停背景预取，
pressure 先释放非可见 detail，不能淘汰 dirty Mask 或撤销历史。

## API 边界

Task 列表和详情只附带 `image_pyramid` 轻量摘要，不附完整 levels 或 URL。

```text
GET  /api/v1/tasks/{task_id}/image-pyramid
POST /api/v1/tasks/{task_id}/image-pyramid/asset-urls
POST /api/v1/tasks/{task_id}/image-pyramid/retry
```

GET 返回状态 envelope；ready 时返回 manifest 和短期 overview URL，并支持 `ETag`/`If-None-Match`。
`asset-urls` 最多接受 128 个 overview/tile 逻辑请求，去重后按首次出现顺序返回。服务端验证 Task 可见性、
generation、level/grid 坐标和对象存在性，再统一签发到期时间。

对象缺失或 source fence 不匹配时，不返回 URL，并把 active generation 标记为不一致或陈旧。retry 对
pending/building 是幂等的，对失败请求有冷却和频率限制。

Minimap、评论画布和相邻任务预取只消费 thumbnail/overview；审核页复用同一个图片源与调度器。ML Backend、
导出和用户显式下载原文件仍使用 original，不把有损 WebP tile 当权威媒体。

## 资源与生命周期

专用 Celery Worker 订阅 `image-pyramid` 队列，初始并发为 1。生成流程按以下顺序执行：

```text
取得 DB lease
  → 流式下载到有界临时文件
  → libvips 规范化并生成
  → 校验完整网格、edge 尺寸和摘要
  → 上传 tile 与 overview
  → 最后上传 manifest
  → 事务发布 active generation
```

像素、单边尺寸、source/derived/temp bytes、tile 数和 wall time都有硬限制。每日 reconciliation 回收过期
lease、非 active 旧代次和孤儿前缀。source 或 owner 删除时同步清理对应派生对象；`image-pyramids/`
不使用普通媒体缓存的固定期限 lifecycle。

相关运维步骤见[图片金字塔运行手册](/ops/runbooks/image-pyramid)。
客户端背景选择见 [ADR-0063](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/adr/archive/0063-konva-viewport-image-tiles.md)，联合资源决策见 [ADR-0064](https://github.com/yyq19990828/ai-annotation-platform/blob/main/docs/adr/archive/0064-task-scoped-raster-resource-coordination.md)。
