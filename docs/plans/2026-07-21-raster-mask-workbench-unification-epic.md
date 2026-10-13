# 图像 / 视频栅格 Mask 能力统一 Epic

> Status: In progress（Phase 1–6 completed，Phase 7 plan finalized）· 2026-07-22
>
> 起始版本：v0.23.5
>
> 当前基线：Mask QC、审阅、批量修复和图像 / 视频格式闭环已完成。部署级 `RASTER_MASK_CREATE_ENABLED` 默认开启并保留为紧急总闸；项目级 `raster_mask_native_editing_enabled` 已正式默认开启并允许项目 opt-out。
>
> 相关决策：[ADR-0022 Mask 编辑器 v1](../adr/archive/0022-mask-editor-tool-architecture.md) ·
> [ADR-0048 视频栅格 Mask 内容寻址 RLE](../adr/archive/0048-video-raster-mask-content-addressed-rle.md) ·
> [ADR-0053 原生 Mask AI 候选生命周期与视频纠错](../adr/archive/0053-native-mask-ai-candidate-lifecycle-and-video-correction.md) ·
> [v0.22.0 真·栅格 Mask Track](archive/2026-07-12-v0.22.0-raster-mask-track-davis.md)

## 1. Epic 结论

当前平台存在两条割裂的 Mask 真值：

```text
图片：MaskBuffer 临时像素态 → 最大外环 → polygon
视频：MaskBuffer 像素态 → COCO RLE 对象 → coco_rle_ref → video_track_mask
```

视频侧已经具备内容寻址、强校验、鉴权读取、GC、AI tracker、alpha picking 和无损导出；图片侧仍执行
ADR-0022 的过渡决策，会丢失 hole、多连通区域和原始像素边界。两侧还存在编辑会话竞态、锁绕过、缓存内存、
标准格式覆盖与 Mask 专属质检缺口。

本 Epic 的终局不是继续在两条链路分别堆功能，而是建立一个共享栅格内容层，再让图片、视频按各自时间语义复用：

```text
                          ┌─ raster_mask（图片单对象）
共享 COCO RLE 内容对象 ──┤
                          └─ video_track_mask.keyframes[].mask（视频轨迹）

共享能力：codec / validator / storage / GC / cache / editor / AI prompt / import-export / QC
媒体差异：图片单态；视频 keyframe + nearest hold + outside / occluded
```

## 2. 版本路线

| 版本                                                                               | 主题                     | 必须交付的结果                                                                       | 依赖     |
| ---------------------------------------------------------------------------------- | ------------------------ | ------------------------------------------------------------------------------------ | -------- |
| [v0.23.5](archive/2026-07-21-v0.23.5-mask-reliability-security-foundation.md)      | 可靠性与安全地基         | 防丢稿、异步隔离、锁与 Delete 语义、安全解压、accept 并发、polygon hole / multi 止血 | v0.23.4  |
| [v0.23.6](archive/2026-07-21-v0.23.6-shared-rle-image-mask-schema.md)              | 共享 RLE 与图片 geometry | ADR-0052、`raster_mask`、静态内容 API、AAP / COCO 后端闭环                           | v0.23.5  |
| [v0.23.7](archive/2026-07-21-v0.23.7-image-mask-workbench-native-editing.md)       | 图片原生 Mask 工作台     | 对象级安全渲染、alpha picking、项目 opt-in、创建 / 重载 / 再编辑、单对象双向显式转换 | v0.23.6  |
| [v0.23.8](archive/2026-07-21-v0.23.8-mask-ai-interaction-video-correction.md)      | AI 原生 Mask 与视频修正  | 单帧 SAM 原生 RLE、mask-as-prompt、scribble、局部视频纠错再传播                      | v0.23.7  |
| [v0.23.9](archive/2026-07-21-v0.23.9-mask-advanced-editing-instance-operations.md) | 高级编辑与实例操作       | 套索增减、连通域、填洞、形态学、非重叠绘制、批量 / 视频 / 派生几何转换               | v0.23.8  |
| [v0.23.10](archive/2026-07-21-v0.23.10-mask-performance-large-canvas.md)           | 性能与大画布             | 字节预算缓存、AABB 裁剪、Worker、tile editor、5K / 8K 可用性                         | v0.23.9  |
| [v0.23.11](archive/2026-07-21-v0.23.11-mask-quality-review-format-ecosystem.md)    | 质检、审阅与格式生态     | Mask QC、跨帧稳定性、局部接受、Label Studio / PNG / MOTS 等格式                      | v0.23.10 |
| [待排期](2026-07-21-semantic-panoptic-mask-workflows.md)                           | 语义 / 全景分割          | class-map、instance + semantic 合成、冲突策略、16-bit 输出与专用工作流               | v0.23.11 |

顺延规则：任何版本未满足退出门，后续版本保持 blocked。不能在后续 UI 中临时补写前置数据合同，也不能为了赶版本绕过
鉴权、乐观锁、对象校验或无损 round-trip。

## 3. 跨版本不变量

### 3.1 数据真值

- 原生 Mask 的持久真值是不可变、内容寻址的 COCO RLE 对象；annotation JSONB 只保存强校验引用。
- `MaskBuffer` 是浏览器编辑态，不是可移植格式；polygon 是显式派生格式，不再是 Mask 的隐式唯一落库格式。
- 图片采用单对象 `raster_mask`；视频继续采用 `video_track_mask`，不新增单帧 `video_mask`。
- 任何格式或 geometry 转换都必须标明无损 / 有损；不允许静默取最大外环、丢 hole、丢小岛或降级 bbox。
- RLE row-major / column-major 转换必须经过共享 fixture；禁止直接遍历 backing array 假装兼容。

### 3.2 编辑会话

- 图片、视频共用 `idle → loading → ready → dirty → saving → error` 状态语义。
- 每个会话有 `sessionId + generation`；过期请求不能回写当前 task / frame / annotation。
- 服务端成功前不清 Buffer；失败后保留稿件、history、label 与 AI lineage。
- pointer、快捷键、工具栏、右键和 commit 使用同一 `canEditMask` 门。
- 锁定、只读、任务状态、分配与 segment lock 同时在进入和提交边界检查。
- 每个 pointer stroke 是一个本地历史单元；annotation mutation 是另一个历史层，二者不能串线。

### 3.3 安全与资源

- 压缩输入、解压输出、图像上传、解码像素、RLE runs、canonical bytes、geometry bytes 和 staged bytes 均有前置上限。
- 内容对象读取必须复核 object key、SHA-256、canonical bytes、runs 和 size。
- 同步对象存储 I/O 不阻塞 async event loop。
- 缓存使用可解释的字节预算，淘汰时释放 `ImageBitmap`；单对象损坏不清空其它对象。
- 任何扩大 4096 上限的工作必须先有 5K / 8K 基准和 tile / sparse 决策，不能只改常量。

### 3.4 AI 与审计

- Backend 只有真实支持时才声明 `mask`、`scribble`、`mask-as-prompt` 等能力。
- 候选预览与已接受 annotation 使用同一解码 / renderer，避免接受前后形状漂移。
- prediction / tracker accept 保留 parent、backend、模型、prompt 与源 version；不能只做浏览器本地 dismiss。
- 接受结果前重新检查任务状态、锁和源版本；并发冲突返回 409，不使用 last-writer-wins。

### 3.5 文档与测试

- 每个版本在同一变更中更新 CHANGELOG、用户手册、开发概念、API / 格式文档和相关 ADR。
- 用户文档描述当前态，不在正文留下可见版本号；版本来源放 frontmatter 或 HTML comment。
- Python 测试优先使用 `apps/api/.venv`，并关闭 bytecode / pytest cache；测试完清理临时导出包、trace 和非版本化缓存。
- 每个版本都要有真实浏览器 E2E；纯函数单测不能替代加载、切帧、失败恢复和持久化闭环。

## 4. Epic 终局能力矩阵

| 能力                | 图片                                        | 视频                                          | 终局要求                   |
| ------------------- | ------------------------------------------- | --------------------------------------------- | -------------------------- |
| 原生 instance Mask  | `raster_mask`                               | `video_track_mask`                            | 无损、可重编辑             |
| 手工编辑            | brush / erase / lasso / region / morphology | 同左 + keyframe                               | 操作可撤销                 |
| AI 交互             | 点、框、scribble、mask prompt               | 同左 + propagate / correct                    | 原生 RLE                   |
| 帧间语义            | 不适用                                      | nearest hold、outside、occluded、AI propagate | 不伪造线性插值             |
| 转换                | polygon / multi ↔ Mask                      | polygon track / frame ↔ Mask keyframe         | 显式损失报告               |
| 质检                | hole、小岛、重叠、边界                      | 同左 + flicker、drift、temporal IoU           | 可定位、可批量处理         |
| instance 导入导出   | AAP、COCO、PNG、Label Studio                | AAP、COCO、DAVIS、MOTS                        | round-trip 合同            |
| semantic / panoptic | class-map / id-map                          | frame sequence                                | 独立于 instance annotation |
| 大图                | tile / sparse editor                        | tile decode / frame budget                    | 5K / 8K 不崩溃             |

## 5. 跨版本验收场景

Epic 完成前必须保留以下跨切片场景，并在相关版本逐步变绿：

1. 图片 hole + 三个分离组件创建、刷新、再编辑、AAP 导出导入后逐像素一致。
2. polygon 显式转 Mask，再 undo 回 polygon；有损 Mask→polygon 转换展示完整损失报告。
3. 保存失败后继续编辑并重试，最终只产生一个 annotation mutation。
4. 快速切换 50 个视频帧，迟到内容不闪回；一个损坏 RLE 不影响其它实例。
5. 锁定 Mask 经按钮、`M`、pointer、Enter、Delete、API accept 均不能修改。
6. 单帧 SAM 原生 Mask → 人工补边 → Tracker 延展 → 局部帧修正 → 后续重传播。
7. 1080p 20 个实例连续播放 300 帧，缓存稳定；4K 编辑 20 strokes 不持续增长。
8. 8K 图片在 tile 模式中局部编辑，未访问 tile 不物化全图 Buffer。
9. Mask QC 能发现小岛、hole、重叠和视频 flicker，并从问题跳到对象 / 帧。
10. instance Mask 与 semantic / panoptic class-map 使用不同 schema、导出和冲突语义，不互相伪装。

## 6. Epic 非目标

- 不更换 Konva，不引入第二套工作台状态框架。
- 不把 AI logits 作为 annotation 持久真值。
- 不用自动 polygon 化替代原生 Mask 持久化。
- 不在没有 benchmark 时承诺无限分辨率、无限实例或无限历史。
- 不一次性迁移所有历史 polygon；只提供显式转换与可选后台迁移工具设计。
- 不把 instance segmentation、semantic segmentation 和 panoptic segmentation 混成一个 geometry。
- 不顺手重构无关的 Annotation、Tracker、导出或项目配置模块。

## 7. 发布与回滚总策略

- 新 geometry 按“后端 reader → 前端 renderer → 创建开关”顺序部署，使用独立 read / create feature flag。
- JSONB union 是加法扩展，但旧后端不认识新类型；一旦创建新类型，应用回滚采用 forward-fix，不直接切回旧镜像。
- 内容对象格式保持兼容，关闭图片创建不能影响既有视频 Mask 或 GC。
- 每个版本只在自身退出门满足后切换默认行为；保留上一个稳定路径至少一个顺延版本。
- semantic / panoptic schema 在延期计划中单独决策，不修改 `raster_mask` / `video_track_mask` 的 instance 语义。

## 8. Outcome 回填模板

每个子计划回填自身 Outcome；Epic 完成后汇总：

- 最终 ADR、geometry、API、AAP 与格式合同；
- 各版本 release commit、运行态版本和 feature flag 状态；
- 安全、并发、逐像素 round-trip、E2E 和性能证据；
- 1080p / 4K / 8K 峰值内存、decode / render / stroke p95；
- 支持与明确不支持的格式 / AI capability；
- 已删除的兼容路径、仍保留的迁移工具和后续债务。
