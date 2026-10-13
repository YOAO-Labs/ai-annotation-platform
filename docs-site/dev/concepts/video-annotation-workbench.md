---
audience: [dev]
type: explanation
since: v0.9.16
status: stable
last_reviewed: 2026-08-13
---

# 视频标注工作台

视频工作台当前支持视频元数据与 manifest、帧时间表、单帧预览缓存、逐帧播放与 J/K/L jog、单帧 bbox / polygon / polyline / rotated bbox、bbox / polygon / polyline / raster mask compact 轨迹、outside / occluded 语义、矩形轨迹组合与转换、交互式单帧 AI、原生 mask tracker 候选审阅、时间轴关键帧与 held 区间、bitmap cache、minimap、评论锚点和工作台诊断快照。

`video_track_*` 一条 annotation 保存一个对象轨迹和 compact keyframes，前端按需显示关键帧与插值结果；单帧 `video_*` geometry 仍是一等标注。矩形框轨迹拥有完整关键帧编辑、组合、转换与 AI 追踪；polygon / polyline 轨迹当前提供渲染、顶点变形与基础管理。视频 stage 复用 Workbench 外壳、任务锁、离线队列、评论、审核与导出入口。

<!-- history: the original version-by-version video workbench notes are merged into this current capability overview. -->

## 数据入口

视频文件通过 dataset 导入进入系统：

1. `DatasetItem.file_type = "video"`。
2. 上传、ZIP 导入、bucket scan 完成后投递 `app.workers.media.generate_video_metadata`。
3. Celery media worker 下载原视频到临时目录，调用 `ffprobe` 解析元数据，再用 `ffmpeg` 抽首帧 poster。
4. 元数据写入 `dataset_items.metadata["video"]`，poster 路径写入 `dataset_items.thumbnail_path`，使任务列表复用现有缩略图链路。

`metadata["video"]` 当前字段：

| 字段                                              | 含义                                                     |
| ------------------------------------------------- | -------------------------------------------------------- |
| `duration_ms`                                     | 视频时长，毫秒                                           |
| `fps`                                             | 帧率，优先取 `avg_frame_rate`                            |
| `frame_count`                                     | 帧数，优先取 `nb_frames`，缺失时用 `duration * fps` 估算 |
| `width` / `height`                                | 视频原始尺寸                                             |
| `codec`                                           | 视频编码名                                               |
| `playback_path` / `playback_codec`                | 非浏览器兼容编码转码后的 H.264 MP4 对象路径与编码        |
| `poster_frame_path`                               | poster 对象存储路径                                      |
| `probe_error` / `poster_error` / `playback_error` | 解析、抽帧或播放转码失败原因                             |
| `frame_timetable_frame_count`                     | 已生成帧时间表的帧数                                     |
| `frame_timetable_error`                           | 帧时间表生成失败原因；失败时前端按 fps 估算降级          |

这些失败字段会出现在 `/storage` 的「视频资产失败」面板中。管理员点击重试后，probe / poster / frame timetable 统一投递 `generate_video_metadata`；chunk / frame cache 失败则投递对应的 `ensure_video_chunks` / `extract_video_frames`。

## Manifest API

`GET /tasks/{task_id}/video/manifest` 返回播放所需信息：

```json
{
  "task_id": "...",
  "video_url": "https://...",
  "poster_url": "https://...",
  "metadata": {
    "duration_ms": 1000,
    "fps": 25,
    "frame_count": 25,
    "width": 640,
    "height": 360,
    "codec": "mpeg4",
    "playback_path": "playback/..."
  },
  "expires_in": 3600
}
```

非视频任务会返回 `400`。如果 `playback_path` 存在，manifest 的 `video_url` 会优先指向转码后的 H.264 MP4；否则使用原始视频对象。`GET /tasks/{id}` 也透出 `video_metadata`，用于列表和工作台决定是否进入视频 stage。

## Frame Timetable API

```http
GET /api/v1/tasks/{task_id}/video/frame-timetable?from=0&to=120
```

响应示例：

```json
{
  "task_id": "...",
  "fps": 29.97,
  "frame_count": 1800,
  "source": "ffprobe",
  "frames": [
    {
      "frame_index": 0,
      "pts_ms": 0,
      "is_keyframe": true,
      "pict_type": "I",
      "byte_offset": 48
    }
  ]
}
```

当存量视频还没有时间表时，接口返回 `source: "estimated"` 和空 `frames`；前端使用 `fps` 与 `frame_count` 继续估算，不阻断打开工作台。`from` / `to` 都是可选且包含边界。

## Frame Preview API

视频工作台前端消费 task 级单帧缓存接口：

```http
GET /api/v1/tasks/{task_id}/video/frames/{frame_index}?format=webp&w=320
POST /api/v1/tasks/{task_id}/video/frames:prefetch
```

`VideoKonvaStage` 只把它用于时间轴 hover preview 和轻量预取，不替代 `<video>` 的主播放源。响应状态处理：

- `ready` 且有 `url`：时间轴 preview popover 显示 signed URL 图片。
- `pending`：显示轻量 loading，并在短延迟后重试一次；不弹 toast。
- `failed` 或网络错误：保留 frame/time 文案，当前 hover 帧不阻断 seek/playback。
- `400` / `404`：认为当前 task 不支持 frame service，本次打开期间停用 hover preview，只保留原 frame tooltip。

前端会对以下帧调用 `frames:prefetch` 作为 hint：当前选中 `video_track_bbox` 的 keyframes、当前 task 的 bookmark frames，以及 loop region 的起止帧。预取只影响后端单帧缓存，不写 annotation，也不会改变播放 / seek 语义。

## Observability

视频工作台提供两层前端诊断：

- `window.__videoFrameClockDiagnostics`：按 task 保存 `useFrameClock` 诊断，包含 seek 次数、stale 回调、long task 计数、最近 frame-ready source 和最近 seek 样本。
- `window.__videoWorkbenchDiagnostics`：按 task 保存工作台快照，包含当前 frame、fps、timeline mode、J/K/L 播放速率、当前对象密度、loop/bookmark 状态，以及 frame preview cache hit/miss。

BugReportDrawer 会在视频工作台提交反馈时自动读取当前 active task 快照：

- 描述末尾追加 `Video Workbench Diagnostics` JSON 块，方便管理员在 `/bugs` 直接查看。
- `recent_console_errors` 插入 `[video-workbench-diagnostics]` 结构化 payload，方便后续导出或聚类。
- 如果快照里的 `taskId` 是 UUID，会同步写入 bug report 的 `task_id` 字段。

本地性能回归入口：

```bash
pnpm --filter @anno/web video:bench -- --dry-run
pnpm --filter @anno/web video:bench
```

详细流程见 [视频工作台性能回归](/dev/how-to/video-workbench-performance-regression)。

## Annotation Schema

视频工作台 UI 当前消费这些视频 geometry：

| 类型                                           | 时间语义                  | 当前管理能力                                                                                     |
| ---------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------ |
| `video_bbox`                                   | 单帧矩形框                | 选择、移动、缩放、改类、删除，可聚合成轨迹                                                       |
| `video_polygon` / `video_polyline`             | 单帧点集                  | 绘制、顶点变形、改类、删除                                                                       |
| `video_rotated_bbox`                           | 单帧旋转框                | 绘制、真实区域选择、移动、局部轴缩放、旋转、改类、删除                                           |
| `video_keypoint`                               | 单帧关键点                | 按模板绘制、遮挡 / 跳过、节点移动、可见性切换、改类、删除                                        |
| `video_mask`                                   | 单帧内容寻址 RLE          | alpha 渲染 / picking、brush / erase、帧级选择、改类、删除                                        |
| `video_track_bbox`                             | bbox compact 轨迹         | 完整关键帧编辑、outside / occluded、组合、转换、AI 追踪                                          |
| `video_track_polygon` / `video_track_polyline` | 点集 compact 轨迹         | 渲染 / 插值、指标、首帧定位、显隐、锁定、改类、整条删除                                          |
| `video_track_mask`                             | 内容寻址 RLE compact 轨迹 | alpha 渲染 / picking、brush / erase、held 关键帧、outside / occluded、AI 追踪、DAVIS / COCO 导出 |

前端通过 `videoTool` 与工具单位的 `video_modes` 决定写单帧 geometry 还是 compact track keyframe。rotated_bbox 与 keypoint 当前只提供单帧工具；关键点创建依赖类别级 `keypoint_schema`。

`video_track_bbox` 示例：

```json
{
  "type": "video_track_bbox",
  "track_id": "trk_...",
  "semantic_label": "car_3",
  "outside": [{ "from": 24, "to": 48, "source": "manual" }],
  "keyframes": [
    {
      "frame_index": 0,
      "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
      "source": "manual",
      "occluded": false
    }
  ]
}
```

约定：

- `annotation_type` 写 `video_track_bbox`。
- `track_id` 在单条 annotation 内稳定，用于 UI 展示和审核定位（uuid，只读不变）。
- `semantic_label` 是用户可编辑的语义标签（如 `car_3`），仅作跨任务 Re-ID 心智，不参与主键、不强制唯一。
- 类别继续使用 annotation 顶层 `class_name`，本期不引入稳定 `class_id`。
- `keyframes[]` 是持久化数据；插值结果由前端按相邻关键帧计算，不写库。前端用缓存索引和二分查找解析当前帧。
- `outside[]` 是一等消失段，使用闭区间 `{ from, to }` 表示目标在该段帧内不存在；相邻或重叠区间会在读写 helper 中归一化。
- `source` 当前支持 `manual` / `prediction` / `interpolated`；前端不会把计算得到的 interpolated frame 展开保存。
- 轨迹"消失"语义完全由 `outside` 区间表达（对齐 CVAT 两态 outside/occluded）。历史 `absent=true` 关键帧已被迁移为单帧 outside。
- `outside` 对渲染和导出优先级最高：落在 outside 的帧不显示对象、不导出 bbox，也不会参与 track → `video_bbox` 转换。
- `occluded=true` 表示目标存在但被遮挡，前端用虚线状态显示，不阻断插值。
- `track_number` 是显示/导出用的确定性派生整数：按「首关键帧帧号升序、并列按 `track_id` 字典序」派生 `1..N`，**不持久化**（util `derive_track_number` / 前端 `deriveTrackNumber`）。

`video_bbox` geometry：

```json
{
  "type": "video_bbox",
  "frame_index": 12,
  "x": 0.1,
  "y": 0.2,
  "w": 0.3,
  "h": 0.4
}
```

约定：

- `frame_index` 从 0 开始，是唯一时间轴定位字段。
- `x/y/w/h` 与图片 bbox 一样使用归一化坐标。
- `annotation_type` 写 `video_bbox`。
- `video_bbox` 可由视频矩形框工具直接创建，也可由 track 转换 API 生成。
- `video_rotated_bbox` 保存 `frame_index/cx/cy/w/h/angle`；画布在像素空间做旋转命中与局部轴缩放。
- `video_keypoint` 保存 `frame_index/points[]`，每个节点为 `{x,y,v}`，骨骼节点名和连线仍来自 keypoint 工具单位的类别模板。

点集轨迹与 bbox 轨迹使用相同的 `track_id / outside / keyframes[]` 外壳，但关键帧保存 `points` 而非 `bbox`；polygon 闭合，polyline 不闭合。点集插值要求相邻关键帧顶点可对应，当前工作台先开放渲染与管理层，关键帧表和完整逐帧编辑仍只属于 bbox 轨迹。

单帧 Mask 保存 `{type:"video_mask", frame_index, mask}`，只在精确帧显示且不持有
`track_id`。Mask 轨迹关键帧保存 `{frame_index, mask, source, occluded?, attributes?}`。
两者的 `mask` 都是 `coco_rle_ref`，包含 `[height,width]`、对象键、SHA-256、runs 和
canonical bytes；像素内容不重复嵌进 annotation JSONB。轨迹当前帧解析采用最近关键帧保持、
距离相同时选更早帧，`outside` 优先。前端只解码可见帧，缓存键包含 annotation version、
resolved frame 与内容哈希，淘汰、版本变化或切 task 时关闭 `ImageBitmap`。选择与右键使用
row-major alpha 命中，不用外接框冒充像素命中。

内容端点：

```http
POST /api/v1/tasks/{task_id}/mask-content
GET /api/v1/annotations/{annotation_id}/mask-content
GET /api/v1/annotations/{annotation_id}/mask-content/{frame_index}
GET /api/v1/video-tracker-jobs/{job_id}/mask-content/{sha256}
```

写入端点校验 RLE、视频尺寸和任务可编辑性后返回不可变引用；annotation 读取端点按当前帧解析并返回带 ETag 的 RLE；job 端点只暴露该 job staged result 实际引用的对象。

## Track 转独立框 API

```http
POST /api/v1/tasks/{task_id}/annotations/{annotation_id}/video/convert-to-bboxes
```

请求体：

```json
{
  "operation": "copy",
  "scope": "track",
  "frame_mode": "all_frames"
}
```

字段：

| 字段          | 取值                       | 说明                                                              |
| ------------- | -------------------------- | ----------------------------------------------------------------- |
| `operation`   | `copy` / `split`           | `copy` 保留原 track；`split` 会移除源 keyframe 或删除整条源 track |
| `scope`       | `frame` / `track`          | 转换当前帧或整条轨迹                                              |
| `frame_index` | number                     | `scope=frame` 时必填                                              |
| `frame_mode`  | `keyframes` / `all_frames` | `scope=track` 时决定只转关键帧还是展开插值帧                      |

响应返回源 annotation 的新状态、创建出的 `video_bbox[]`、是否删除源 track，以及被移除的 frame indexes。`copy` 不会改动源轨迹，`removed_frame_indexes` 为空；`split` 才会移除源关键帧或删除整条源轨迹，并返回被移除的帧号。`all_frames` 使用与 Video Tracks JSON 导出相同的后端插值 helper：outside 范围不输出 bbox，也不会跨消失段转换。为避免长视频一次性写爆 annotation 表，单次请求最多生成 5000 个 `video_bbox`。

### Track Composition

```http
POST /api/v1/tasks/{task_id}/annotations/video/track-compositions
```

请求体按 `operation` 分三类：

```json
{
  "operation": "merge_tracks",
  "annotation_ids": ["track-a", "track-b"],
  "frame_index": 120
}
```

字段：

| 字段             | 取值                                                                | 说明                                                                                                               |
| ---------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `operation`      | `aggregate_bboxes` / `split_track` / `merge_tracks` / `join_tracks` | 聚合单帧框、拆分轨迹、合并轨迹、跳连轨迹                                                                           |
| `annotation_ids` | UUID[]                                                              | 聚合时传 `video_bbox[]`；拆分时传 1 条 `video_track_bbox`；合并/跳连时传 2 条 `video_track_bbox`                   |
| `frame_index`    | number                                                              | `split_track` 必填，表示在当前可见帧之后切出后段                                                                   |
| `gap_mode`       | `interpolate` / `outside`                                           | `join_tracks` 用：`interpolate` 不写 gap、靠线性插值过渡；`outside` 把 gap 区标 outside 后合并。默认 `interpolate` |
| `delete_sources` | boolean                                                             | `aggregate_bboxes` 默认为 true，成功后删除源 `video_bbox`                                                          |

约束：

- `aggregate_bboxes` 要求同任务、同类、每帧最多一个 `video_bbox`。
- `split_track` 要求切点是可见帧，源 annotation 保留前段，新 annotation 保存后段。
- `merge_tracks` 只接受两条同类且可见帧区间不重叠的 track；中间 gap 会写入 `outside` 段。
- `join_tracks` 同样要求两条同类、可见帧区间不重叠的 track；与 merge 共用合并落库 helper，区别仅在 gap 处理（见 `gap_mode`）。
- 响应返回 `updated_annotations[]`、`created_annotations[]` 和 `deleted_annotation_ids[]`，前端用这些结果更新 annotation cache 并组成 undo/redo batch。

## 插值与质量检查

前端只在相邻有效关键帧之间做 bbox 线性插值：

- `x/y/w/h` 按 `frame_index` 距离线性计算。
- 如果两个关键帧之间落入 `outside` 段，不显示跨段插值。
- 手工 / 预测关键帧优先于插值结果。
- 编辑时 bbox 会 clamp 到 `[0, 1]` 归一化范围。

当前质检提示在前端完成，不阻止保存：

- 同一 track 关键帧间隔过大。
- 当前帧 bbox 极小。
- 当前帧同类别 bbox 高度重叠。

## 视频 AI 追踪候选

视频传播不直接把 worker 结果混入 annotation cache。`useVideoTrackerJobs` 维护运行 job 和 `staged_result` preview：`job_completed` 或带部分结果的 `job_cancelled` 到达后拉取 preview，`useWorkbenchShellModel` 把当前帧 bbox candidate 映射到 `VideoSamCandidateOverlay`，`VideoTrackerReviewBar` 提供整批接受 / 丢弃。

accept 成功后才 invalidate annotation query；discard 只清前端候选和后端 staged result。该 job 级候选与 `usePredictions` 管理的单 shape Prediction 候选是两条独立状态。完整状态机、能力路由与多目标落库见[视频 AI 追踪架构](./video-ai-tracking)。

### PVS 种子采集与传播对话框

`state/useTrackerSeedCollection.ts` 持有「落点选目标」工作流：点/框种子列表（每颗带目标序号 obj、落点帧 frame）、当前目标与锚点帧、采集会话（临时借用 smart-point/smart-box 工具，退出恢复原工具），以及传播对话框的生命周期（无源检测 / 单源延展 / ≥2 条多选批量）。提交前由纯模块 `state/trackerSeedPrompts.ts` 把种子按 obj → frame 分组成多帧 prompts（xyxy 框归一化为后端 {x,y,w,h}，两级均数值排序保证 payload 确定，从而幂等键不依赖收集顺序）。

归属边界：面板显隐命令归工作区布局域、章节圈选解除与刷选范围归时间轴章节域、临时工具准入归 `useVideoToolCommands`——本模块只调用注入的窄命令。画布手势通过 `collectPoint` / `collectBox` 归集，不直接改写种子列表。对话框提交成功后挂上 jobId 就地转「追踪中…」，仅当该 job 出候选、失败或被清理时关闭，运行中不因重渲染丢失。

## Video Tracks JSON 导出

`video-track` 项目可通过现有导出入口拿到专用 JSON：

```http
GET /api/v1/projects/{project_id}/export?format=coco&video_frame_mode=keyframes
GET /api/v1/projects/{project_id}/batches/{batch_id}/export?format=coco&video_frame_mode=all_frames
```

虽然复用了 `format=coco` 查询参数，响应不是 COCO，而是：

```json
{
  "export_type": "video_tracks",
  "exported_at": "2026-05-11T00:00:00",
  "frame_mode": "keyframes",
  "project": { "id": "...", "display_id": "P-1", "type_key": "video-track" },
  "categories": [{ "id": 0, "name": "car" }],
  "tasks": [{ "id": "...", "display_id": "T-1", "video_metadata": { "fps": 25 } }],
  "tracks": [
    {
      "annotation_id": "...",
      "task_id": "...",
      "track_id": "trk_car",
      "class_name": "car",
      "outside": [{ "from": 24, "to": 48, "source": "manual" }],
      "keyframes": [
        {
          "frame_index": 0,
          "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
          "source": "manual",
          "occluded": false
        }
      ]
    }
  ],
  "keyframes": [],
  "video_bbox": [],
  "video_metadata": {}
}
```

导出模式：

- `keyframes`：只输出持久化关键帧。
- `all_frames`：每条 track 增加 `frames[]`，后端按相邻有效关键帧线性插值 `x/y/w/h`。

插值规则与前端显示保持一致：outside 段优先；精确关键帧其次；`occluded=true` 表示目标存在但遮挡，不阻断插值。`video_frame_mode=all_frames` 不输出 outside 范围内的 bbox，也不会把 track → `video_bbox` 转换到 outside 帧上。

`include_attributes=false` 会移除 `project.attribute_schema` 以及 track / legacy `video_bbox` 上的 `attributes`。图片侧 `yolo-det` / `yolo-obb` / `yolo-seg` / `voc` 对视频项目返回 400；视频检测训练集要使用 `targets=yolo-frames-det`，它按采样网格抽帧并把 `video_bbox` 与摊平后的 `video_track_bbox` 写成逐帧 YOLO label。

### 部分范围导出

项目和批次导出接受可选的 task-scoped `scope`：按连续 Segment 起止或源帧闭区间选择。API 在入队前把 selection 规范化为确定的 `task_id / dataset_item_id / from_frame / to_frame`，并把同一结果写入 preflight、job payload、worker options 和 manifest；因此 segment 配置随后变化也不会改变已排队任务的范围。

共享的 geometry 裁剪只保留范围内单帧 shape；track 会裁剪 outside、保留范围内原始关键帧，并用现有 resolver 在可见区间边界补齐自包含状态。帧格式从原视频全局采样网格过滤范围，再按各格式现有 base 密集编号，避免从 `from_frame` 重新锚定导致采样相位漂移。

## 前端 Stage 边界

`WorkbenchShell` 只计算 `stageKind`。`WorkbenchStageHost` 根据 `stageKind` 分派到 `ImageWorkbench` / `VideoWorkbench` / `ThreeDWorkbench.placeholder`；视频任务由 `VideoWorkbench` 包装 `VideoKonvaStage`。

`stageKind` 的视频入口仍由 `task.file_type === "video"` 或项目类型 `video-track` 决定。3D 入口只显示占位，不复用视频内部 geometry。

`VideoKonvaStage` 暴露 `VideoStageControls` ref，由 `useWorkbenchHotkeys` 在 `videoMode` 下统一分发快捷键。视频模式快捷键：

- `Space` 播放 / 暂停；按住并拖拽画布时平移视图
- `J` / `K` / `L` 反向播放或减速 / 暂停 / 正向播放或加速
- `V` / `B` / `T` 切换视频选择 / 矩形框 / 轨迹工具
- `P` 进入视频多边形绘制；智能点 / 智能框 / Exemplar / Magic Box 使用视频侧各自直达键
- `Ctrl+B` 选中 bbox 轨迹时打开 AI 追踪检查器
- `←` / `→` 上一帧 / 下一帧；采样开启时按网格跳
- `Shift + ←/→` 采样开启时源帧 ±1 微调
- `,` / `.` 选中 `video_track_bbox` 时跳上 / 下可见关键帧
- `Home` / `End` 选中 `video_track_bbox` 时跳首 / 末可见关键帧
- `Ctrl+M` 当前帧添加 / 移除书签
- `Ctrl+[` / `Ctrl+]` 跳转历史后退 / 前进
- `Alt+L` 清除本地 loop region
- `Delete` / `Backspace` 选中 bbox 轨迹时删除当前帧关键帧；选中单帧视频几何时删除该标注
- `Ctrl+Delete` / `Ctrl+Backspace` 删除整条选中轨迹
- `Tab` / `Shift+Tab` 循环轨迹
- `Esc` 取消选择
- `1-9`、`0` 有选中视频对象时改其 `class_name`；无选中时切 active class（按项目类别顺序取前十个）

视频任务使用自己的 polygon / polyline 与交互式 AI 工具入口：智能点、智能框、Exemplar 在当前帧生成 `video_polygon`，Magic Box 生成 `video_bbox`；切帧会清理帧绑定的瞬态候选。图片专用 canvas 工具不会直接挂进视频 Stage。视频 AI 追踪配置面板与 job 审阅条由 `WorkbenchShell` 经 `stageOverlay` 渲染；配置面板以中间 stage 为局部定位容器，使拖动、缩放和边界夹取始终限定在画布内。左侧队列、顶部提交/审核、右侧属性面板、评论、任务锁和离线队列继续复用同一个 Workbench 外壳。

视频创建、追加关键帧、重命名、改类、track 转 bbox 等动作由 `useVideoAnnotationActions` 维护。跨 Stage 的 class picker / 改类 / SAM 接受 / 批量改类弹窗由 `WorkbenchOverlays` 渲染，不再挂在 `ImageStage.overlay` 上。

### 视频渲染层

视频画布与图片工作台**同栈**，统一用 Konva（`react-konva`）的多 Layer 结构（架构决策见 ADR-0041）。坐标走「归一化存储 + 像素空间渲染 + Konva transform」，scale 抵消 / fit-to-canvas / 滚轮缩放与图片复用同一组 viewport 纯函数（`stage/shared/viewport/`）：

| 层          | 文件                                                     | 职责                                                                                              |
| ----------- | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| media       | `VideoKonvaMediaLayer.tsx`                               | `Konva.Image` 以隐藏 `<video>` 为 source；播放态逐帧重绘视频层，暂停态贴 `ImageBitmap` LRU 缓存帧 |
| track       | `VideoKonvaTracksLayer.tsx` / `VideoKonvaTrackShape.tsx` | committed bbox / 点集几何、track 轨迹预览线、关键帧圆点                                           |
| overlay     | `VideoKonvaOverlayLayer.tsx`                             | 标签（Konva `Label`/`Tag`/`Text`）与 pending draft 草稿                                           |
| issue       | `VideoKonvaIssueLayer.tsx`                               | pixel-anchored issue 图钉（按当前帧显隐，可点击跳到讨论面板）                                     |
| interaction | `VideoKonvaInteractionLayer.tsx`                         | bbox 的 8 向 resize、点集顶点编辑，以及画框 / 移动 / 缩放 live 预览                               |

`VideoKonvaStage` 负责 Stage 容器、视口 transform、播放，以及各 chrome 浮层（时间轴 `VideoPlaybackOverlay`、minimap、QC 警告、关键帧快跳）。命中由 `videoKonvaCoordinates.ts` 把 client 坐标映射到像素空间，再用 `videoStagePicking.ts` 选择顶层框；画框/移动/缩放/选中由 `videoKonvaInteraction.ts` 分流。当前帧应显示哪些框 / 轨迹预览 / ghost / 标签由纯函数 `videoFrameViews.ts` 派生。视觉规格（线宽 / 填充 / 字号 / 标签）经 `annotationVisual.ts` 与图片栈共用同一组纯函数。

画布上下文菜单使用通用 `ContextMenu` + `useCanvasContextMenu` 原语：Stage 负责把命中对象转换成 `DropdownItem[]`，菜单组件只处理 fixed 坐标定位、视口翻转和关闭行为。视频侧 `buildVideoContextMenuItems` 分三层：bbox / bbox track 保留完整动作，单帧 polygon / polyline / rotated bbox 提供改类与删除，polygon / polyline track 提供显隐、锁定、改类和整条删除。图片侧通过 Konva `getIntersection()` 在容器层统一命中 shape，再把 annotation action 映射成 `DropdownItem[]`。

视频工作台的 viewport 与图片工作台复用同一套 `useViewportTransform` 行为：`F` 适应视口、`0` 回到 1:1、Ctrl/Meta+滚轮以光标为锚点缩放、右键拖拽或 `Space`+拖拽平移。缩放和平移只影响显示层，保存到 annotation 的 bbox / keyframe 仍是 `[0,1]` 归一化视频坐标。

R5.2 的 bitmap cache 只优化前端体感，不替代 `<video>` 播放源。`useVideoBitmapCache` 在浏览器支持 `createImageBitmap(video)` 时按 `taskId + frameIndex` 保存 LRU；Konva 媒体层（`VideoKonvaMediaLayer`）按唯一显示真值 `displayBitmap` 绘制 —— 播放态用 `<video>` 实时帧，暂停态优先 WebCodecs 精确帧位图（`useVideoPreciseFrame`），其次原生 `<video>` 抓取位图。同一份 `displayBitmap` 也供当前帧 JPEG（`captureCurrentFrameJpeg`）使用，避免画面 / 标注 / AI 串帧。

精确帧 pipeline 默认按客户端能力尝试：`useVideoPreciseFrame` 经 manifest v2 → chunk 轮询 → packet samples → chunk 字节 → GOP plan → 有状态 GOP decoder session 解码目标帧；同 GOP 逐帧只提交增量，后退 / 跨 GOP / 切任务确定性重建 decoder。B 帧 lookahead 提前输出的未来帧会立即转成 bitmap 并移交现有 LRU，避免关闭后又从关键帧重解，同时不在操作结束后持有 `VideoFrame`。已解码位图的 LRU、活动帧、待显示帧与退休帧共同受同一个总字节预算约束，chunk 字节另按轻量 / 标准 / 激进三档淘汰。用户显式关闭、codec 不支持、chunk pending / failed、缺 samples / description、signed URL 过期或字节越界都安全回退到 `<video>` / 位图，不阻断标注。`window.__videoWorkbenchDiagnostics` 暴露当前 state / source / fallback reason / 资源预算与计数（更新上限 5 Hz，状态与 fallback 转换立即写入），BUG 报告自动附带经裁剪、不含签名 URL / 字节 / 描述的快照。浏览器资格矩阵从 Konva media canvas 验证确定性帧标记，并在有头 Chrome / GPU runner 采集 1080p/4K 延迟、long task、真实媒体时间与资源账本。Apple Silicon Chrome 的 strict 门从实际 WebCodecs player 读取 VideoToolbox/platform decoder 证据，不用静态 GPU profile 或隐藏原生 `<video>` 冒充；尚未完成的 Edge / Safari 矩阵继续作为后验验证债务保留。

`videoStageMode.ts` 提供轻量 busy guard：`idle` 允许 seek / draw / drag / resize；`draw` / `drag` / `resize` 期间 frame setup 会被拦截并暂停播放，避免播放 tick 覆盖编辑中的几何。

`VideoKonvaStage` 底部固定控制条使用 `VideoPlaybackOverlay`：

- 悬浮在视频画布底部，不再占用 stage 布局高度。
- hover 时显示，离开后延迟淡出；绘制或拖动 bbox 时隐藏，避免误触 scrubber。
- 保留播放 / 暂停、逐帧按钮、range scrubber、关键帧 tick、当前帧号、时间和当前帧框数。
- 底部标记的数据源是 timeline markers：keyframe 仍显示为细线，prediction 使用不同颜色，outside 段显示为灰色区间。
- 选中 `video_track_bbox` 时显示该轨迹的单轨 timeline：keyframe 圆点跟随轨迹色、悬浮在进度条上方，连线加粗并加同色外发光，outside 灰段、interpolated 虚线段和 prediction 标记照旧；未选中轨迹时显示全局 keyframe 密度条，按各轨迹关键帧占比自底向上堆叠成彩色渐变（legacy bbox 用 accent 兜底），等宽分桶避免首帧偏窄。
- playhead 显示为 3px 竖线（hover/active 加宽到 5px），不再遮挡相邻关键帧/刻度；overlay 改两行布局让进度条独占一行，不随帧数位数/loop 标签变短；loop 区间渲染为贯穿轨道高度的半透明填充块 + inset 高亮边界。
- `,` / `.` 复用同一套可见关键帧计算，跳过 outside 帧；`Home` / `End` 跳首 / 末出现帧。采样开启时 `Shift+←/→` 仅做源帧 ±1 微调，不参与关键帧跳转。
- `Shift+drag` 时间轴可创建本地 loop region；播放越过范围末帧后 seek 回起始帧，逐帧和手动 seek 不被限制。
- loop region、书签和跳转历史只存前端会话状态，按 task 写入 `sessionStorage`，不改变 annotation schema 或后端 API。
- 书签以小三角 marker 显示，`Ctrl+M` 在当前帧加 / 删；显式 seek、bookmark 跳转和关键帧跳转写入最近 50 条跳转历史，播放 tick 不写历史。
- hover 时间轴会请求单帧预览图；ready 时显示缩略图，pending/error 时降级显示 frame/time。选中轨迹关键帧、书签帧和 loop region 边界会被预取。
- `useFrameClock.seekToAsync` 是 `VideoKonvaStage.seekFrameAsync` 的底层原语；时间轴 scrub、逐帧、关键帧、书签和跳转历史都通过它跳帧。J/K/L jog 播放支持 `0.25x / 0.5x / 1x / 2x / 4x`，overlay 会显示当前速度，反向播放通过帧步进实现。
- review 模式的 `raw / final / diff` 同步作用于视频工作台：`raw` 显示 prediction / interpolated 来源，`final` 显示 manual / legacy，`diff` 叠加。评论协议增加可选 `anchor`，视频评论可记录当前 `frameIndex`、`trackId` 和来源，评论 chip 可点击跳回对应帧。
- 工作台右下角复用 Minimap，放大后显示当前视口、当前帧位置和 ImageBitmap 已缓存帧范围；`window.__videoWorkbenchDiagnostics` 也包含 bitmap cache 与 viewport/minimap 状态。

## History / Offline

图片工作台的 `useAnnotationHistory` 仍处理 annotation 级 create / update / delete。视频侧使用 `videoKeyframe` command：

- 单个 `frame_index` 的关键帧新增、移动、`occluded` 切换只撤销该关键帧；标记"消失"改为写 `outside` 区间（独立撤销）。
- 创建 / 删除整条 track、重命名类别仍按 annotation 级命令处理。
- apply 时读取当前最新 `video_track_bbox` geometry，只替换目标帧 keyframe，保留其它关键帧。

视频写操作仍走原 annotation API。网络断开或 5xx 时：

- create 进入现有 offline queue 的 `create` op。
- keyframe update / rename 进入现有 offline queue 的 `update` op。
- 恢复连接后由 `useWorkbenchOfflineQueue` 顺序重放。
- 409 版本冲突不进入离线队列，继续打开通用 `ConflictModal`；keyframe diff UI 留后续增强。

`VideoKonvaStage` 内部维护轨迹列表 UI 状态：

- 显隐和锁定只影响当前工作台会话，不持久化。
- 重命名轨迹会更新 annotation 顶层 `class_name`。
- 选中轨迹但当前帧无可显示 bbox 时，stage 会用最近未落入 outside 的关键帧渲染虚线参考框；拖动参考框或点击「复制到当前帧」会通过同一 `upsertKeyframe` 路径创建当前帧关键帧，并清理当前帧 outside 覆盖。
- 当前轨迹面板展示 `track_id` + `frame_index`，审核退回时可复制到原因文本中定位问题。
