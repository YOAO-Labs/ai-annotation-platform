# 语义 / 全景分割 Mask 工作流

> Status: research-draft; deferred Epic Phase 8. Reassess project demand and current contracts through the backlog promotion gate before implementation.
>
> 父 Epic：[图像 / 视频栅格 Mask 能力统一](../2026-07-21-raster-mask-workbench-unification-epic.md)
>
> 硬前置：[v0.23.11 Mask 质检、审阅与格式生态](../archive/2026-07-21-v0.23.11-mask-quality-review-format-ecosystem.md)
>
> Scheduling: unscheduled. Use the [promotion gate](README.md#promotion-gate) to reassess project demand, instance-mask contracts and ADR-0055.
>
> 这是新的数据语义，不把 instance Mask 字段复用成 class-map；进入实现前必须新增 ADR-0055。

## 1. 目标

- 在成熟 instance Mask 基础上新增语义分割和全景分割工作流。
- 明确类别像素、实例像素、void / ignore、重叠和覆盖优先级。
- 提供图片与视频帧序列的 class-map / id-map 编辑、AI 生成、质检和标准格式交换。
- instance annotation 保持独立，可以显式合成为 panoptic map，但不能被隐式改写。
- 支持 8-bit / 16-bit indexed map 和超过 255 类 / 实例的数据集。

## 2. P0 · ADR-0055 与数据模型

ADR 至少比较：

1. 每类 / 每实例 annotation 集合实时合成；
2. 单个 class-map / id-map 内容对象；
3. tiled map + manifest；
4. 图片和视频帧序列是否共享 envelope。

草案推荐把语义 / 全景视为独立 dataset-level segmentation map，不加入 `RasterMaskGeometry` union：

```json
{
  "type": "panoptic_map",
  "size": [1080, 1920],
  "encoding": "png16_ref",
  "object_key": "segmentation-maps/sha256/...",
  "sha256": "...",
  "segments_info": [{ "id": 1001, "class_id": 1, "instance_id": 1, "iscrowd": false }],
  "void_id": 0
}
```

冻结事项：

- `semantic_map` 每像素只有 class id；`panoptic_map` 每像素是 segment id，并由 `segments_info` 映射 class / instance。
- instance `raster_mask` / `video_track_mask` 继续允许重叠；map 模式每像素只有一个最终 id。
- void / ignore 与 background 分离；导入导出不能把 ignore 悄悄变 background。
- 8-bit / 16-bit / RGB panoptic id 编码、端序、palette 和最大 id 有共享 fixture。
- 视频不沿用 Mask Track nearest hold 冒充逐帧 class-map；保存哪些帧、如何补帧由独立合同决定。
- 内容对象、tile、版本、锁、history 和 GC 沿用共享基础，但引用扫描独立。

## 3. 项目与标注模式

### P1 · 项目 schema

- 项目显式选择 instance、semantic、panoptic 或允许组合；默认保持 instance。
- 类别声明 `thing / stuff`、导出 id、颜色、void / ignore 行为和允许重叠策略。
- schema 变更有 dry-run：受影响像素、未知 class、冲突 segment 和格式兼容。
- tool binding 将 instance Mask 与 map painting 分开，不能只启用 region 就暴露全部模式。

### P2 · 图片工作台

- 类别笔刷、橡皮到 void、套索增减、flood fill、组件和形态学复用 v0.23.9 operation core。
- 当前类别 painting 自动替换目标像素的旧 class；冲突策略始终可见。
- panoptic 模式支持 thing 实例选择 / 新建与 stuff 类直接绘制。
- 图层显示 class palette、segment boundary、void、只看当前类 / 实例和冲突区域。
- history 记录 tile patch + class / segment metadata，支持跨 tile undo。

### P3 · 视频工作台

- 支持标注项目采样帧的 semantic / panoptic map，时间轴显示已标 / AI / 未标 / void。
- AI propagate 可生成帧 map，但未运行推理的帧不能被 UI 标成已标。
- instance track 可显式合成 panoptic thing segments；stuff 由 frame map 提供。
- 邻帧复制、局部纠错和窗口重传播复用 v0.23.8，但输出 map candidate。
- 长视频存储、frame sampling 与导出前全帧要求必须在任务创建时明确。

## 4. AI 工作流

### P4 · 全场分割

- backend capability 区分 instance mask、semantic map、panoptic map。
- 支持文本 / 类别列表、点 / 框 / scribble 和已有 map correction。
- AI candidate 带 class / segment metadata、置信度和 unknown / void 区域。
- 多模型结果合并必须通过冲突策略和审阅，不按响应顺序静默覆盖。
- 大图 / 长视频用 tile / window job，保持 v0.23.10 资源预算。

### P5 · Instance → Panoptic 合成

- 以项目 thing / stuff schema 为前提，dry-run 检查重叠、未覆盖、重复 segment id 和越界。
- overlap winner 可选 z-order、置信度、人工优先或逐问题决策；策略进入 manifest / audit。
- locked / reviewed instance 不被合成任务修改；合成只生成 map candidate。
- 接受后保留来源 annotation ids / versions，便于追溯和重新生成。

## 5. 质检与审阅

- semantic：未知类、void 占比、类面积异常、边界破碎、孤岛和相邻禁配。
- panoptic：segment id 孤儿、segments_info 缺失、thing 无实例、stuff 被拆碎、重叠决策未完成。
- 视频：类别 flicker、segment id 切换、thing identity 与 instance track 不一致。
- reviewer 使用 class / segment confusion overlay、边界差异和来源追溯。
- blocking rule 由项目显式配置；默认提示。

## 6. 格式

| 格式                        | Semantic    | Panoptic    | 关键合同                              |
| --------------------------- | ----------- | ----------- | ------------------------------------- |
| AAP                         | 新增        | 新增        | 内容对象 + metadata 无损              |
| COCO Panoptic               | —           | 导入 / 导出 | RGB id PNG + `segments_info`          |
| Cityscapes                  | 导入 / 导出 | 导入 / 导出 | labelIds / instanceIds / ignore       |
| Pascal VOC Segmentation     | 导入 / 导出 | —           | indexed PNG + palette                 |
| CamVid                      | 导入 / 导出 | —           | class index map                       |
| PNG 8 / 16-bit + manifest   | 导入 / 导出 | 导入 / 导出 | id range、端序、palette               |
| Label Studio semantic brush | 导入 / 导出 | 条件支持    | label mapping 与 overlap flatten 报告 |

每个格式必须有 consumer、palette / id golden fixture 和无损 / 有损报告；超过格式 id 上限时失败，不取模。

## 7. 非目标

- 不改变 instance Mask 的允许重叠与轨迹语义。
- 不把每个 semantic class 强制拆成大量 annotation rows。
- 不自动推断 thing / stuff；由项目 schema 明确配置。
- 不承诺所有视频帧自动有 map；采样与推理覆盖必须可见。
- 不用 8-bit PNG 承载超过其上限的 class / segment id。
- 不在无 ADR / benchmark 情况下引入新的专用图像数据库。

## 8. 验收

1. 图片 semantic class-map 创建、刷新、tile 编辑、AAP / PNG round-trip 像素一致。
2. 图片 panoptic thing + stuff 合成，重叠策略和 segments_info 一致。
3. instance Mask 保持不变，panoptic map 可按来源版本重新生成。
4. 视频采样帧 map 与未标帧区分明确，AI propagate 不伪造人工状态。
5. 16-bit id、void / ignore、超过 255 类 / 实例 fixture 无截断。
6. COCO Panoptic、Cityscapes、VOC、CamVid 使用真实 consumer round-trip。
7. semantic / panoptic QC 能定位 class、segment、帧和冲突像素。
8. 8K tile、长视频 window job 遵守既有内存、临时空间和取消合同。

## 9. 文档与 Epic 完成门

- 新增 semantic / panoptic 用户教程、项目 schema、AI、QC、格式与运维文档。
- ADR-0055 Accepted；更新 annotation module、API schema boundary 与共享内容层 ADR 关系。
- CHANGELOG 按 Added / Changed / Fixed 记录；发布时同步全部版本源。
- Epic 完成前回填总纲 Outcome：全部子版本 release、性能、安全、格式 consumer 与真实浏览器证据。
