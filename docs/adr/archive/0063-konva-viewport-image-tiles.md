# 0063 — 超大图客户端采用视口 LOD Tile 与解码字节 LRU

- **Status:** Accepted
- **Date:** 2026-07-31
- **Deciders:** core team
- **Supersedes:** N/A

## Context

ADR-0062 已定义不可变图片金字塔、manifest 与批量鉴权交付，但图片工作台仍用一个
`HTMLImageElement` 下载、解码和绘制完整源图。50MP 以上图片即使压缩文件不大，浏览器中的 RGBA、
Canvas 纹理和重绘也会占用数百 MiB；Minimap、邻题预取和评论背景还可能在主画布之外再次请求原图。

工作台的标注、Raster Mask、Issue pin 和 Minimap 已统一使用 full-resolution 坐标，因此客户端需要只替换
背景采样方式，不能改变几何真值或引入另一套画布坐标。

## Decision

### D1. 上层解析一个图片源 union

Task 的 `image_pyramid` summary 决定是否读取 manifest。上层 hook 把 API 状态解析成 `single`、
`pyramid`、`pyramid-pending` 或 `pyramid-failed`，`ImageStage` 不自行猜测 required、generation 或
原图回退策略。

required 大图的自动路径不请求 original。building/failed 时只显示 overview、thumbnail 或 blurhash；
客户端 gate 关闭也不会把 required 大图静默退回无界整图解码。optional 图片和没有 pyramid 的小图保留
原 single-image 路径。

### D2. 背景 LOD 不改变 full-resolution world

level 只决定背景采样率。每个 tile 的 Konva node 仍占 manifest 计算出的 full-resolution 整数半开区间；
存储 overlap 通过 image crop 去除，不扩大 world rect，也不通过相邻浮点累加定位。

LOD 使用 `viewport scale × scaleFactor × devicePixelRatio`，选择最粗但不会明显上采样的 level。
当前 level 在 `0.75..1.25` 区间内保持，避免缩放阈值附近反复请求。

### D3. overview/ancestor 保底，目标 tile 渐进覆盖

overview 先于 tile 显示。调度器保留当前可见的旧层级 cache 作为 ancestor，目标 LOD tile 到达后在其上
覆盖；单 tile 签发、网络或解码失败时不撤掉 overview。快速平移、任务切换和 generation 变化均通过
AbortController 与 immutable source identity fence 阻止迟到提交。

### D4. 解码资源按字节而非对象数管理

tile 优先走 `fetch → Blob → createImageBitmap`，失败时同一 Blob 降级为
`HTMLImageElement + ObjectURL`。两条路径共用签发、队列、并发、decoded-byte reservation 和 LRU。

低/标准/高设备档位分别使用 32/64/128 MiB retained budget 与 2/4/6 并发。解码成本固定按
`decodedWidth × decodedHeight × 4` 计费；可见 tile pin，非可见 tile 按 LRU 回收。
eviction/dispose 必须对应执行 `ImageBitmap.close()` 或清空 image 并 revoke ObjectURL。
相同逻辑 tile 集合不会因连续 pointer move 重复排队；短期 URL/首次拉取失败只重新批签一次。

背景层用一个 Konva `Shape` 顺序批量绘制 overview 与当前可见 tile，而不是为每个 tile 创建声明式
Konva node。这样保留整数 crop/world 映射，同时避免连续平移时的节点协调开销。

### D5. 背景 tile 与 WebGPU 独立

背景继续使用浏览器图片解码和 Konva Canvas2D。`navigator.gpu`、Raster Mask WebGPU gate、adapter
初始化或 device lost 均不参与图片 source/LOD 路由。调度器只暴露 resource snapshot 与
`pausePrefetch`，供后续 task-scoped 资源协调使用，不在本决策中合并 Mask 真值或预算。

## Consequences

正向：

- required 超大图首屏不再下载或解码完整 source，只请求 overview、当前 LOD 可见 tile 和有界 overscan。
- vector、Mask、Issue 和 Minimap 坐标无需迁移，tile 失败也不会破坏标注真值。
- 邻题、Minimap、评论和审核页共用同一 source 解释，不再从隐藏路径绕回原图。
- bitmap、ObjectURL、请求、reservation 和 cache 都有可检查的 dispose/诊断计数。
- 无 WebGPU、无独立 GPU 或 `createImageBitmap` 失败的浏览器仍有完整路径。

负向：

- 客户端需要维护签名 URL 刷新、请求取消、decode fallback 与 LRU 状态。
- overview 是故障时的有损背景，目标 tile 未就绪时清晰度会渐进变化。
- 浏览器没有可靠总显存 API，预算只能表示应用自身保守计账，不能宣称是物理 GPU memory。
- 本决策只管理背景资源；与 Raster Mask 多套缓存的共同峰值由后续协调层处理。

## Alternatives Considered

**把 Konva 整体改成 WebGL/WebGPU renderer**：会扩大坐标、命中、标注层和跨浏览器迁移面，且无 GPU
客户端仍需另一套实现，拒绝。

**只依赖浏览器对单张大图的内部分块**：浏览器仍需下载完整 source，内存、取消和跨消费者重复请求不可控，
拒绝。

**用压缩 Blob 字节或 tile 条目数作为 LRU 成本**：与解码后驻留量相关性不足，高熵/低熵输入会得到错误
准入，拒绝。

**评论弹窗与 Minimap 各自启动完整 tile scheduler**：增加重复 cache 和网络，且它们只需要 overview，
拒绝。

## Notes

- 服务端资产决策见 [ADR-0062](0062-immutable-image-pyramid-assets.md)。
- 实施计划见
  [`docs/plans/archive/2026-07-31-v0.23.23-large-image-konva-viewport-tiles.md`../../plans/archive/2026-07-31-v0.23.23-large-image-konva-viewport-tiles.md)。
- 客户端合同实现位于 `apps/web/src/pages/Workbench/stage/imagePyramid.ts` 与
  `imageTileScheduler.ts`。
