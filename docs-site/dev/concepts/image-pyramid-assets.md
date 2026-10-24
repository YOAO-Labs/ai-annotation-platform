---
title: 超大图金字塔派生资产
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-24
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
- direct／primary 关联解析到 DatasetItem 时共享该 Item；独立 TaskOriginal 以 Task 为 owner；
- owner 与 `profile_version` 唯一，数据库约束保证两种 owner 恰有一个非空。

`image_pyramid_generations` 表示一次不可变生成：

```text
pending → building → ready
                    └→ failed
```

同一 asset 同时最多有一个 pending/building generation。Worker 在所有对象上传和校验成功后，才在事务中
更新 `active_generation`。同源重建时旧 active 可继续提供服务；源被替换后旧 active 立即失效，避免旧像素
与新坐标叠加。

`image_pyramid_outputs` 独立保存每次租约的物理责任，不随 Task、Item 或 Asset 级联删除。分配时保存已登记位置、端点身份、桶和独立 UUID 前缀；用途绑定只选择新输出。公开 manifest 不暴露这些字段。输出从 building 进入 ready 或 retiring，删除成功后进入 deleted；已发布内容清单不可改写。

每次租约使用不同的输出目录，旧任务失败不会删除新租约的结果。对象布局是服务端实现细节：

```text
image-pyramids/objects/{output_id}/
  manifest.json
  overview.webp
  tiles/{level}/{x}/{y}.webp
```

## 源身份与规范化

原件由 canonical resolver 解析已登记 DatasetAsset／TaskOriginal 的 SHA-256、长度、保存位置与准确关联，不按当前用途桶猜读。准备只冻结元数据；Worker 用当前观测设置条件请求，完整核对原件 SHA 与长度，再交给 libvips。manifest fingerprint 必须等于登记 SHA，下载后自行计算一个摘要不构成原件登记证据。

HTTP 委派绑定发起 Task、实际凭据指纹与现行 annotations:read／Task 可见性，后台持续检查当前身份、API Key、关系和 Gate。内部上传、导入与维护使用显式系统委派，缺失凭据不隐式升级为系统。读取已归档数据集中的可见 Task 不要求数据集管理权限；目录与原地引用仍不进入此派生链。

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

调度器分两阶段工作：

```text
逻辑坐标批签（最多 128 项）
  → 有界 fetch / Blob
     → createImageBitmap
        └─ 失败时 HTMLImageElement + ObjectURL
```

| 调度维度                       | 取值与规则                                                                            | 设计目的                                       |
| :----------------------------- | :------------------------------------------------------------------------------------ | :--------------------------------------------- |
| **LOD 选择与滞回区间**         | `viewport scale × scaleFactor × devicePixelRatio`，在 `0.75..1.25` 区间内保持当前层级 | 避免连续缩放视口时跨阈值频繁抖动               |
| **解码缓存预算（低/标/高档）** | `32 MiB` / `64 MiB` / `128 MiB`（按 `decodedWidth × decodedHeight × 4` 计费）         | 可见 tile 固定（pin），非可见 tile 按 LRU 淘汰 |
| **请求并发与批签上限**         | 并发 `2` / `4` / `6`，单次 `asset-urls` 批签最多 `128` 项                             | 控制连接池占用并减少签名往返                   |
| **降级覆盖与失败重试**         | 目标 tile 到达前保持 overview 或已缓存粗层级；拉取失败重签一次，5 秒冷却后自动重试    | 杜绝缩放平移时出现空白棋盘格                   |
| **Konva 生命周期对账**         | 淘汰时先移出可绘制列表再同步执行 `ImageBitmap.close()` / `revokeObjectURL`            | 防止异步重绘访问已释放位图导致画布冻结         |

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
generation、level/grid 坐标和保存清单中的完整 SHA／长度，再统一签发到期时间。读取、签名与最终响应前复验权限、原件关联、active generation、独立输出 owner 与维护门禁。

对象缺失或 source fence 不匹配时，不返回 URL，并把 active generation 标记为不一致或陈旧。retry 对
pending/building 是幂等的，对失败请求有冷却和频率限制。

Minimap、评论画布和相邻任务预取只消费 thumbnail/overview；审核页复用同一个图片源与调度器。ML Backend、
导出和用户显式下载原文件仍使用 original，不把有损 WebP tile 当权威媒体。

## 资源与生命周期

专用 Celery Worker 订阅 `image-pyramid` 队列，初始并发为 1。生成流程按以下顺序执行：

```text
固定原件与显式委派，取得 DB lease 和独立输出 owner
  → durable Permit 下完整核验原件临时文件
  → libvips 规范化并生成
  → 校验完整网格、edge 尺寸和摘要
  → 上传 tile 与 overview
  → 最后上传 manifest，逐文件完整读回 SHA／长度
  → 原生工作、文件和临时目录真实收尾
  → 短事务复验授权／原件／有效 lease／owner／Gate 并发布
```

像素、单边尺寸、source/derived/temp bytes、tile 数和 wall time 保留现有硬限制；源读取另有两槽 admission。取消、超时和软时限等到真实 I/O、原生生成与注册句柄关闭，未知收尾保留 durable StorageOperation。统一 Asset→Generation→Output 锁序，metadata 锁不跨物理读取。

每日 reconciliation 复验过期租约、移除非 active 旧代的引用，再将无引用且无在用操作的已知输出退役，提交后按保存前缀删除；删除失败保留责任重试。业务删除不执行当前缓存桶前缀清理，独立 output 留待 GC。不裸扫默认桶，不按时间删除其他任务临时目录，`image-pyramids/` 不使用固定期限 lifecycle。旧无归属代不自动签名、采纳或删除；原件已登记后可以显式重试新代，旧未知文件留待单独核验。该消费者迁移不开放整个 media-cache 用途切换。

相关运维步骤见[图片金字塔运行手册](/ops/runbooks/image-pyramid)。
客户端背景选择见 [ADR 0063](../adr/archive/0063-konva-viewport-image-tiles)，联合资源决策见 [ADR 0064](../adr/archive/0064-task-scoped-raster-resource-coordination)。
