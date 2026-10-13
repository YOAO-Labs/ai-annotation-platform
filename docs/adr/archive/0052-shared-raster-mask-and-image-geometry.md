# 0052 — 共享栅格 Mask 与图片 geometry 合同（raster_mask + image mask 静态 API + 转换无损报告）

- **Status:** Accepted（v0.23.5 冻结 v0.23.6 实施所需的全部边界；gzip 传输 / polygon 转换无损报告 / 编辑会话状态机语义为本次新增冻结项）
- **Date:** 2026-07-21
- **Deciders:** core team
- **Supersedes:** —（在 [ADR-0048](./0048-video-raster-mask-content-addressed-rle.md) 的视频 `coco_rle_ref` 内容寻址之上，统一图片与视频两侧的栅格 mask 真值；ADR-0022 图片「mask→polygon」过渡决策在本 ADR 后正式标记为仅图片遗留路径，不再扩散到视频或新 geometry）

## Context

当前平台存在两条割裂的 mask 真值：

```text
图片：MaskBuffer 临时像素态 → 最大外环 → polygon        (ADR-0022 过渡决策, 丢 hole / 多连通 / 像素边界)
视频：MaskBuffer 像素态 → COCO RLE 对象 → coco_rle_ref    (ADR-0048, 内容寻址 + 强校验 + GC)
```

视频侧已具备内容寻址、SHA-256 校验、鉴权读取、24h 宽限 GC、tracker accept、alpha picking 与无损导出。图片侧仍在执行 ADR-0022 的过渡决策：`maskToPolygon.ts` 默认 `pickLargest=true`，只取最大外环，丢弃 hole、小岛与原始像素边界；`ImageStageShapes.tsx:KonvaPolygon` 也只画单个 `<Line closed>`，`transforms.ts:geometryToShape` 虽然透传了 `holes` / `multiPolygon` 但渲染层注释明确写「暂不参与渲染」。

v0.23.5 是栅格 mask 统一 Epic 的 Phase 1（可靠性与安全地基）。v0.23.6 才真正落 `raster_mask` annotation 类型与图片静态内容 API。本 ADR 必须在 v0.23.6 编码之前**冻结所有共享边界**，让后续版本不再临场补数据合同：

- v0.23.5 暴露的真实问题：编辑会话被迟到 GET 覆盖、保存失败丢稿、`is_locked` 绕过、视频 Delete 误删整轨、mask 内容上传无 quota、对象存储 I/O 阻塞 async loop、tracker accept 在共享源 annotation 上无版本冲突检测。
- v0.23.5 新增的 gzip 传输需要一个冻结的编码契约，否则 v0.23.6 实施时前后端会各自发明。
- polygon ↔ Mask 转换在图片侧历史上是「静默有损」，必须在冻结层面禁止。

候选方案：

| 选项                                                                               | 主要卖点                                                                                                       | 主要劣势                                                                                           |
| ---------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **A. 共享 `raster_mask` + 泛化静态 GET + 显式无损/有损转换 + gzip 契约（本 ADR）** | 图片 / 视频复用同一 `coco_rle_ref` 与 validator；转换损失显式报告；gzip 在传输层冻结；v0.23.6 编码零开放阻断项 | 需要在 v0.23.5 先做安全 / 可靠性止血与契约冻结，不直接产出 `raster_mask`                           |
| B. 继续维持图片 mask→polygon 现状                                                  | 改动最小                                                                                                       | hole / 多连通继续丢失；v0.23.6 仍要面对同一批竞态与安全洞；合同推迟只会更贵                        |
| C. v0.23.5 直接实施 `raster_mask` schema                                           | 一次到位                                                                                                       | 违反 Epic 退出门：未先解决丢稿 / 锁绕过 / 并发 accept / 无界上传就引入新持久真值，回滚与审计不可控 |

## Decision

### D1. 类型名与职责边界

- 图片单对象 mask 的持久真值类型名为 **`raster_mask`**（v0.23.6 落地）。其 JSONB 形态复用 ADR-0048 的 `coco_rle_ref`，**不**为图片发明新引用结构。
- 视频继续使用 **`video_track_mask`**（`keyframes[].mask` 为 `coco_rle_ref`），**不**新增单帧 `video_mask` 类型。帧间语义（nearest hold / outside / occluded）由视频侧独有字段承载，不污染 `raster_mask`。
- 空 mask 合法且可往返：`counts = [pixels]`（全 0）或等价表达；空 mask 视为不可见，**不**静默退化为空 polygon。
- 4096 上限沿用 ADR-0048：width / height ≤ 4096，pixels ≤ 16,777,216，runs ≤ 1,000,000，canonical JSON ≤ 4 MiB。tile / sparse 编辑的触发条件不在本 ADR 冻结，留给 v0.23.10 在 5K / 8K 基准出来后决策（见 Open Questions）。

### D2. 共享 `coco_rle_ref` schema（图片与视频共用）

`raster_mask` / `video_track_mask.keyframes[].mask` 的引用对象统一为：

```json
{
  "encoding": "coco_rle_ref",
  "size": [1080, 1920],
  "object_key": "raster-masks/sha256/ab/cd/<sha256>.json",
  "sha256": "<64 lowercase hex>",
  "runs": 4096,
  "bytes": 16384
}
```

字段与 `build_raster_mask_reference`（`apps/api/app/services/raster_mask_storage.py:build_rle_reference`）完全一致；读取路径必须复核 `object_key == rle_object_key(sha256)`、`sha256`、`bytes`、`runs`、`size`，任一不符即拒绝（`load_coco_rle` 已实现）。

引用的 `encoding` 永远描述引用协议（`coco_rle_ref`），不描述正文或存储压缩。gzip 对象额外携带 `storage_encoding: "gzip"` 且使用 `.json.gz`；未压缩旧引用可省略该字段。reader 继续接受历史 `encoding: "coco_rle_gzip"` + `.json.gz`，writer 不再生成该混合形态。

### D3. 图片静态内容 GET API（静态规范路径 + 旧路径兼容）

图片单态 Mask 使用 `GET /annotations/{annotation_id}/mask-content` 作为规范接口；现有视频接口 `GET /annotations/{annotation_id}/mask-content/{frame_index}` 保持不变。为兼容按本 ADR 早期草案接入的客户端，带 `frame_index` 的路径也接受 `raster_mask` 并忽略该参数：

- 无 `frame_index` 的静态路径只接受 `geometry.type == "raster_mask"`；
- 带 `frame_index` 的路径接受 `video_track_mask`，并兼容 `raster_mask` 单态读取；
- 响应头带 `ETag: "<sha256>"` 与 `Cache-Control: private, max-age=300`；`If-None-Match` 命中时不读取对象正文，返回 `304 Not Modified`；
- 鉴权统一为 `annotations:read` scope + task-context 可见性，必须执行批次状态与标注员分派校验；仅项目可见不足以授权读取任务内容；
- annotation `is_locked` 不影响读取，但 read feature flag 必须开启，且返回正文前必须经 D4 的 task-context validator 校验引用完整性与媒体尺寸。

### D4. task-context validator（图片 / 视频共用）

`validate_mask_geometry_for_task`（`apps/api/app/services/raster_mask_storage.py`）泛化为同时校验 `raster_mask` 与 `video_track_mask`：

- 必须存在 `task.dataset_item_id` 且与 mask `size` 匹配；
- 每个 `coco_rle_ref` 必须能 `load_coco_rle` 通过（可选 `verify=True` 触发实际对象读取）；
- 写路径（annotation POST/PATCH、tracker accept）必须调用此 validator；
- **不允许**绕过 validator 直接写 `coco_rle_ref` 到 JSONB。
- codec 层允许全背景 RLE 往返；创建或更新可见 `raster_mask` annotation 时，必须读取对象并拒绝前景像素数为零的内容。

### D5. polygon ↔ Mask 显式转换（禁止静默有损）

- 任何 `polygon → mask` 或 `mask → polygon` 转换必须返回**无损 / 有损标记 + 损失报告**（`{ lossy: bool, reason?, droppedHoles?, droppedComponents? }`）。
- **禁止**静默 `pickLargest`：`maskToPolygon`（`apps/web/src/pages/Workbench/stage/shared/geometry/maskToPolygon.ts`）在 `multipleComponents || hasHoles` 时返回 `lossy: true` + 诊断字段，由调用方决定弹确认或禁用入口。
- v0.23.5 的图片 mask refine 入口若不能无损保存，必须**禁用并说明原因**（提示「含孔 / 多连通区域，请等待原生 Mask 工作台」），**不**静默取最大环落库。
- source / parent lineage：接受 prediction / tracker 候选时，`raster_mask` / `video_track_mask` 必须保留 `source`（prediction / tracker / manual）、`parent_annotation_id`、`backend_id`、`model_id`、`prompt`、`source_version`（候选产生时的源 annotation version），便于审计与刷新后不复活原候选。

### D6. gzip 传输契约（v0.23.5 新增）

为降低大 mask 的传输体积，新增可选 gzip 传输层（与未压缩 JSON 向后兼容）：

- object key 后缀：未压缩 `.json`，gzip `.json.gz`；`coco_rle_ref.object_key` 必须与实际存储后缀一致。
- canonical bytes：未压缩为 compact JSON；gzip 为 `gzip` 压缩后的 canonical JSON 字节流，SHA-256 仍对**未压缩的 canonical JSON** 计算（`bytes` 字段记录未压缩长度，便于校验）。
- bounded decompress（`apps/api/app/utils/raster_mask_gzip.py`）：限制输出分配，并强制 `eof`、空 `unused_data`、空 `unconsumed_tail`；截断、拼接 member 和尾随数据一律拒绝。超任一上限立即 `raise ValueError` 并拒绝写库 / 拒绝响应。
  - `MAX_COMPRESSED_BYTES = 8 MiB`
  - `MAX_UNCOMPRESSED_BYTES = MAX_RLE_OBJECT_BYTES = 4 MiB`
  - `MAX_EXPANSION_RATIO = 20`
- 编码分层：请求 JSON 正文始终是 `{encoding:"coco_rle",size,counts}`；HTTP 传输压缩只由 `Content-Encoding: gzip` 表示，对象存储偏好只由 `storage_encoding: "identity" | "gzip"` 表示。服务端在 JSON 解析前有界解压 HTTP gzip。历史 body `encoding:"coco_rle_gzip"` 会被归一化为 `coco_rle` + gzip 存储偏好。
- writer/readability：若 canonical JSON 的 gzip 展开比超过 20，writer 自动回退 `.json`，不得生成 reader 会拒绝的 `.json.gz` 对象。
- 匿名上传归属：`raster_mask_uploads` 记录 `(task_id, object_key)`，task 级事务 advisory lock 串行化预留；每 task 最多 256 个未被 annotation 事务认领的对象。GC 删除对象时同步删除归属记录。
- 前端 `maskRle.ts` 在满足相同边界且浏览器支持 `CompressionStream` 时发送 HTTP gzip / gzip 存储偏好，压缩不可用或失败时回退普通 JSON。下载侧不暴露对象存储编码：服务端按引用完成校验与解压，统一返回 `coco_rle` JSON。

### D7. 编辑会话状态机语义（图片 / 视频共用）

冻结统一状态机，约束 v0.23.6 及以后所有 mask 编辑路径：

- 状态：`idle → loading → ready → dirty → saving → error`。
- 会话键：`sessionId = hash(taskId, frameIndex, selectionKey, annotationVersion)` + 单调递增 `generation`。
- 过期请求隔离：`loading` 期间发起的新会话自增 `generation`；旧 GET / 404 / mutation 回包若 `generation` 不匹配当前会话，**不得**回写 Buffer。
- 离开 dirty session（切 task / frame / tool / route）必须经过 guard：保存 / 丢弃 / 继续编辑。
- 保存语义：单飞 Promise（同 session 内重复 Enter / 双击只产生一次 mutation）；**服务端成功前不清 Buffer**；失败保留 history / label / refine lineage 并暴露 retry。
- pointer / 快捷键 / 工具栏 / 右键 / commit 使用同一 `canEditMask` 门：`!taskReadOnly && !annotation.is_locked && !trackLocked && !segmentLocked && editorState ∈ {ready, dirty}`。
- Enter 真实提交条件：`editorState ∈ {ready, dirty}` 且 `dirty === true`；无变化不物化 held keyframe。
- Delete：图片删 annotation 对象；视频删当前关键帧；**整轨删除仅走 `Ctrl/⌘+Delete` 或显式确认动作**。
- Escape 只取消当前 mask session，**不**冒泡到 annotation history undo。

### D8. JSONB 加法扩展部署顺序与回滚

`raster_mask` 是 annotation JSONB `geometry.type` 的加法扩展。部署顺序冻结为：

1. **后端 reader**：先发能读 / 校验 / GC `raster_mask` 的后端（validator + storage + 静态 GET），read flag 默认开启。
2. **前端安全 reader**：再发认识 `raster_mask` 的前端；原生像素 renderer 上线前，它只能作为明确的只读占位，不得落入 bbox 移动、缩放或复制降级路径。
3. **创建开关**：最后用独立 create feature flag 打开创建路径；本阶段默认关闭，待原生图片 Mask 工作台就绪后再灰度开启。

回滚约束：旧后端镜像不认识 `raster_mask`，一旦生产数据中存在 `raster_mask` annotation，**回滚必须采用 forward-fix**（在新版本中处理），**不**直接切回旧镜像——旧镜像会把 `raster_mask` 当未知 type 静默丢弃或报错。GC 与静态 GET 必须在所有 reader 升级后才打开 create flag。

feature flag 语义：read flag 与 create flag 独立；read flag 默认 on（允许新后端读旧数据），create flag 默认 off。

## Consequences

正向：

- v0.23.6 编码前，`raster_mask` 类型名、`coco_rle_ref` schema、静态 GET URL 形态、validator、转换无损报告、gzip 编码、会话状态机、JSONB 部署顺序全部冻结，无开放阻断项。
- 图片 hole / 多连通区域不再静默丢失：v0.23.5 的 `KonvaPolygon` even-odd 渲染 + `maskToPolygon` 无损报告为 v0.23.6 的 `raster_mask` 渲染铺路。
- 编辑会话状态机统一后，迟到 GET 覆盖、保存失败丢稿、重复 Enter 并发上传等竞态在契约层面被禁止。
- gzip 传输使大 mask（4K / 多实例）的传输体积可控，bounded decompress 关闭 zip bomb 向量。

负向：

- v0.23.5 不创建 `raster_mask` annotation，图片 mask 默认提交仍转 polygon（除非有损），用户在含 hole / 多连通场景下会看到「禁用并说明原因」提示——这是有意为止血而付出的体验代价，v0.23.7 原生 Mask 工作台消除。
- gzip 编码协商增加前后端复杂度；旧客户端与旧后端的组合必须仍按未压缩 JSON 工作，测试矩阵变大。
- `source_version` 字段要求 tracker accept 路径在发起 job 时记录源 annotation version，增加一处状态依赖（D5 + WS-D4）。
- 整轨 Delete 改为 `Ctrl/⌘+Delete` 或确认动作，是一次用户可见的交互变更，需在快捷键文档与 CHANGELOG 同步。

## Alternatives Considered（详）

**方案 B（维持图片 mask→polygon 现状）**：hole / 多连通继续丢失，`maskToPolygon` 继续静默 `pickLargest`。不可接受：v0.23.6 仍要面对同一批竞态与安全洞，且转换损失永远不可审计；Epic 退出门要求「polygon ↔ Mask 显式转换」，静默有损直接违反。

**方案 C（v0.23.5 直接实施 `raster_mask` schema）**：跳过可靠性与安全止血，直接引入新持久真值。不可接受：Epic §2 顺延规则明确「任何版本未满足退出门，后续版本保持 blocked」；在丢稿 / 锁绕过 / 并发 accept / 无界上传未解决前引入 `raster_mask`，回滚时无法区分「新类型数据」与「旧类型被新代码污染」。

## Notes

- 实现代码位置（v0.23.5 已落 / v0.23.6 待落）：
  - v0.23.5：`apps/api/app/utils/raster_mask_gzip.py`（新）、`apps/api/app/services/raster_mask_storage.py`（gzip + async to_thread）、`apps/api/app/services/video_tracking/runner.py:accept_tracker_job`（版本冲突 → 409）、`apps/web/src/pages/Workbench/state/useMaskEditorSession.ts`（新）、`apps/web/src/pages/Workbench/state/canEditMask.ts`（新）、`apps/web/src/pages/Workbench/stage/ImageStageShapes.tsx:KonvaPolygon`（even-odd holes / multi_polygon）、`apps/web/src/pages/Workbench/stage/shared/geometry/maskToPolygon.ts`（无损报告）、`apps/web/src/pages/Workbench/stage/shared/geometry/maskRle.ts`（HTTP gzip 上传与安全回退）。
  - v0.23.6：`raster_mask` JSONB union（加法扩展，无 schema 迁移）、`validate_mask_geometry_for_task` 泛化、静态与兼容 GET、独立 read / create flags。
- 相关 ADR：[ADR-0022](./0022-mask-editor-tool-architecture.md)（图片 mask 工具 v1，过渡决策）、[ADR-0048](./0048-video-raster-mask-content-addressed-rle.md)（视频 `coco_rle_ref` 内容寻址，本 ADR 复用）、[ADR-0045](./0045-track-id-as-annotation-column.md)（track_id 跨帧身份）。
- 相关计划：[v0.23.5 可靠性与安全地基../../plans/archive/2026-07-21-v0.23.5-mask-reliability-security-foundation.md)、[v0.23.6 共享 RLE 与图片 geometry../../plans/archive/2026-07-21-v0.23.6-shared-rle-image-mask-schema.md)、[Epic 总纲../../plans/2026-07-21-raster-mask-workbench-unification-epic.md)。
- Open Questions（本 ADR 不冻结，留后续版本）：
  - tile / sparse 编辑触发条件：5K / 8K 基准出来前不改 4096 常量（v0.23.10）。
  - semantic / panoptic class-map schema：在独立计划中决策，不修改 `raster_mask` 的 instance 语义。
  - gzip 默认开启阈值（canonical > 多少字节才走 gzip）：v0.23.5 实现时选定并写入配置，v0.23.6 视真实流量调整。
