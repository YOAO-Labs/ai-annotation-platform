---
title: 视频后端帧服务
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-24
---

# 视频后端帧服务

视频帧服务把视频帧作为后端一等资源暴露，服务于长视频 chunk 拉取、单帧 thumbnail / AI 推理复用，以及 manifest v2。原有 Task 和 facade 路径继续提供服务，派生交付按输出身份核验。

## 资源模型

- 物理视频仍是 `DatasetItem(file_type="video")`。
- `VideoDerivativeOutput` 独立保存每次 metadata、frame、chunk 尝试的来源、委派、固定存储位置、租约、结果和 SHA／大小清单；不随业务行级联删除。
- `DatasetItem.video_output_id` 指向活动 metadata generation，`video_building_output_id` 保留在建尝试；完整时间表随该 generation 冻结，`VideoFrameIndex` 是同次发布的查询索引。
- `VideoChunk` 保存 chunk 元数据和 MinIO key。
- `VideoFrameCache` 保存单帧 WebP/JPEG 缓存元数据和 MinIO key。
- `VideoSegment` 保存视频内可分配 frame range、assignee 和短 TTL lock。
- `VideoTrackerJob` 保存交互式视频 tracker 的 job 状态、frame range、输入 prompt、取消请求和待审 `staged_result`；接受前不修改 annotation。
- `/api/v1/tasks/{task_id}/video/...` 是现有前端兼容入口。
- `/api/v1/videos/{dataset_item_id}/...` 是长期 facade；服务端必须找到当前用户可见的 video task，否则返回 404。

## Manifest v2

```http
GET /api/v1/tasks/{task_id}/video/manifest-v2
GET /api/v1/videos/{dataset_item_id}/manifest
```

响应包含：

- `video_url`：为空；整段原件使用独立的认证准备与范围读取协议。
- `metadata_generation_id`：当前冻结元数据／时序的输出 UUID。
- `chunks_manifest_url`：chunk 列表入口。
- `frame_timetable_url`：帧时间表入口。
- `frame_service_base`：单帧接口前缀。
- `chunk_size_frames`：当前后端 chunk 粒度。
- `segments`：视频协作段列表；旧前端可忽略。

## Chunk

```http
GET /api/v1/tasks/{task_id}/video/chunks?from_frame=0&to_frame=120
GET /api/v1/videos/{dataset_item_id}/chunks?from_frame=0&to_frame=120
GET /api/v1/tasks/{task_id}/video/chunks/{chunk_id}
GET /api/v1/videos/{dataset_item_id}/chunks/{chunk_id}
```

首次请求缺失 chunk 时，API 分配独立 output、设置逻辑行 pending，提交短事务后投递 `generate_video_derivatives`。单 chunk 未 ready 时返回 HTTP 202 和 `Retry-After`；ready 后返回 signed URL。

### 相邻块预热

`video_chunk_warmup_lookahead` 控制主请求范围之后的预热块数，0 关闭额外预热。`_ensure_chunk_rows` 返回行及本事务实际插入的 chunk ID；预热在提交后派发本次新建行和可重试的失败行，跳过已有的 ready/pending 行。

新行虽然也是 pending，但不能被误判为此前已入队。并发 INSERT 的失败方通过 SAVEPOINT 回读现有行，不把其他事务创建的 pending 行标为本次新建，避免多个预热请求把同一次创建重复投递。主请求仍保留原有按需派发策略；此规则不提供数据库与 broker 的原子提交，也不自动恢复此前遗留的孤立 pending 行。

### Chunk 状态机

<ExcalidrawDiagram
  src="/diagrams/dev/reference/video-chunk-lifecycle.svg"
  alt="视频 chunk 从 pending 进入 ready 或 failed，failed 可通过 retry 重新投递，ready 记录 smart_copy 或 transcode 生成模式"
  caption="Chunk 生成、失败重试与就绪后的生成模式"
/>

| 状态      | 触发 / 含义                                                                         |
| --------- | ----------------------------------------------------------------------------------- |
| `pending` | 行刚创建或 retry 后，Celery 任务在排队 / 执行中。API 返回 `202` + `Retry-After: 3s` |
| `ready`   | worker 完成 chunk 生成（含 ffprobe 写 `diagnostics.samples`），返回 signed URL      |
| `failed`  | ffmpeg 失败 / 超时；行携带 `error` 字段；客户端可触发 retry                         |

> 实现见 `app/services/video_derivatives.py`、`video_frame_service.py` 和 `app/workers/media_derivatives.py`。逻辑状态与不可变物理输出责任分开保存。`video_frame_service` 的 `_ensure_chunk_rows` / `_chunk_out` 输出 chunk 状态时按 `{"pending", "ready", "failed"}` 白名单处理，越界值视为 `pending`。

media worker 会在源 codec 为 H.264 / H.265 且 chunk 起始帧 keyframe 对齐时优先尝试 ffmpeg stream copy；不满足条件或 smart-copy 失败时，自动 fallback 到既有 H.264 baseline fragmented MP4 重编码。API 行为保持兼容，只额外返回诊断字段：

```json
{
  "generation_mode": "smart_copy",
  "diagnostics": {
    "source_codec": "h264",
    "output_codec": "h264",
    "keyframe_aligned": true,
    "start_byte_offset": 100,
    "end_byte_offset": 9000,
    "smart_copy_eligible": true,
    "fallback_reason": null
  }
}
```

前端可用这些字段判断 WebCodecs / Worker 解码是否继续使用 chunk，或降级到整段视频 / frame service。

### Sample manifest（WebCodecs demux）

chunk 生成时，media worker 会用 `ffprobe -show_packets` 扫一遍生成出的 chunk mp4，把每个 packet 的字节偏移 / 大小 / 时间戳 / 关键帧标记写进 `diagnostics["samples"]`（同时写 `codec_string` / `width` / `height`）。已知探测失败时可缺少 samples，普通播放仍可用；进程构造或关闭不确定时拒绝发布并保留责任。存在的样本必须精确覆盖冻结帧范围，每个 offset／size 都落在实际 chunk 内；smart-copy 帧数不符先尝试转码，仍不符则拒绝发布。

```http
GET /api/v1/videos/{dataset_item_id}/chunks/{chunk_id}/samples
```

返回 sample manifest；旧 chunk（`diagnostics` 无 `samples`）返回 404 `samples_not_available`。响应示例：

```json
{
  "dataset_item_id": "…",
  "chunk_id": 0,
  "codec_string": "avc1.4d001e",
  "width": 1920,
  "height": 1080,
  "samples": [
    {
      "frame_index": 0,
      "pts_ms": 0,
      "duration_ms": 33,
      "is_keyframe": true,
      "size_bytes": 45123,
      "offset_in_chunk": 1024
    }
  ]
}
```

- **解码顺序与 GOP 计划**：`samples` 数组按解码顺序排列（满足浏览器 `VideoDecoder` 喂入要求），`frame_index` 按 PTS 展示顺序（presentation rank）加 chunk `start_frame` 推算。前端默认尝试 WebCodecs 精确解码（可通过 `?webcodecs=0`、`localStorage["video.experimental.webcodecs"]=0` 或工作台设置「性能与实验」关闭）。启用时，`VideoKonvaStage` 经 `useVideoPreciseFrame` 定位 chunk → 拉取 samples → 切出「GOP 起点关键帧 → 目标帧」的 GOP 计划 → 由有状态 GOP decoder session 构造 `EncodedVideoChunk[]` 交由 `useVideoChunkDecoder` 解码。
- **硬件加速偏好与无损回退**：解码器优先请求硬件加速，若浏览器拒绝该偏好但支持对应 codec 则自动去掉偏好重试；同 GOP 逐帧前进只提交增量包，后退或跨 GOP 确定性重建，后台预取若需 reset 则直接跳过以免阻塞当前可见帧。任何 codec 不支持、chunk 未就绪、缺失 `samples` / `description` 或 URL 过期均安全回退至原生 `<video>` / 单帧位图。
- **客户端执行边界**：`VideoDecoder` 与 Canvas / WebGPU 均运行在客户端浏览器并消费客户端机器的 CPU/GPU；Linux 服务端仅提供 API、解复用元数据与分片字节流。Apple Silicon Chrome 严格资格验收通过 CDP Media 域区分 `WebCodecs::VideoDecoder` 与隐藏原生 `<video>`，并核验 `VideoToolboxVideoDecoder` 及平台硬解标记。
- **AVCC/HVCC `description` 注入**：manifest 同时返回 base64 编码的 `description`（后端直接读取 chunk MP4 的 `avcC`/`hvcC` box 提取 `AVC/HEVCDecoderConfigurationRecord` 含 SPS/PPS，并派生准确的 `codec_string`），确保 AVCC 长度前缀样本能被浏览器 `VideoDecoder` 正确解析。

MinIO key：

```text
video-derivatives/objects/{output_id}/chunk.mp4
```

### Chunk warmup

`list_chunks` / `get_chunk` 命中某些 chunk 后，会向后 look-ahead 预解码相邻 chunk（逐帧导航多为向前推进），减少后续等待。受 `VIDEO_CHUNK_WARMUP_LOOKAHEAD` 控制（默认 1，设 0 关闭）。warmup 保守降级：只对**还没 ready 且没在 pending 进行中**的相邻 chunk 提交独立 owner 后投递，复用已在建的同一尝试、不阻塞主请求。纯选择逻辑见 `video_frame_service.warmup_chunk_ids`。

## 单帧缓存

```http
GET /api/v1/tasks/{task_id}/video/frames/{frame_index}?format=webp&w=512
POST /api/v1/tasks/{task_id}/video/frames:prefetch
POST /api/v1/tasks/{task_id}/video/frames:retry
GET /api/v1/videos/{dataset_item_id}/frames/{frame_index}?format=jpeg&w=320
POST /api/v1/videos/{dataset_item_id}/frames:prefetch
POST /api/v1/videos/{dataset_item_id}/frames:retry
```

缓存命中先沿保存位置完整验证 metadata.json 与图片，再返回 `status="ready"`、签名 URL、output_id、sha256、byte_size 和 metadata_generation_id；未命中创建独立 output，提交后投递并返回 HTTP 202。抽帧只用已登记原件和该 generation 的 PTS；缺少确切条目时保留 fps 估算。

帧缓存与转码分块使用毫秒时间戳舍入区间的下界定位，并保留微秒精度传给 FFmpeg，避免舍入后的起点晚于目标帧真实 PTS，造成错取下一帧或末帧抽取失败。

`frames:retry` 默认只重投 `status="failed"` 的缓存行；传入 `frame_indices` 时只处理这些帧，未传时最多处理当前 `width + format` 下 500 条失败行。`force=true` 会重置指定帧的 storage key / byte size 并重新投递，适合源视频修复后刷新坏缓存。

更新抽帧逻辑后，先刷新实际消费任务的 worker，再按宽度、格式分组重试。已生成但内容受影响的缓存需要显式列出源帧号并设置 `force=true` 重建，每批最多 500 帧；空帧号列表仍只选失败项，不会强制重建全部缓存。重建完成后刷新浏览器中的帧缓存。

MinIO key：

```text
video-derivatives/objects/{output_id}/frame.{format}
```

poster 属于 metadata output 的 `poster.webp`，不冒充某个 frame-cache output。缩略图只从当前 metadata owner 核验和签发；旧无归属路径不签名。

内部 AI 读取必须提供显式原件委派；`get_frame_array()` 完整核验当前 output 的字节再解码，不按 Item／帧号复用无身份的内存数组。逐帧预标注同样使用已登记来源与 saved owner。

## 失败资产与重试

管理侧通过存储 API 汇总视频资产失败状态：

```http
GET /api/v1/storage/video-assets/failures
POST /api/v1/storage/video-assets/retry
```

失败列表覆盖五类资产：

| asset_type        | 来源                                                       | 重试任务                  |
| ----------------- | ---------------------------------------------------------- | ------------------------- |
| `probe`           | `dataset_items.metadata["video"]["probe_error"]`           | `generate_video_metadata` |
| `poster`          | `dataset_items.metadata["video"]["poster_error"]`          | `generate_video_metadata` |
| `frame_timetable` | `dataset_items.metadata["video"]["frame_timetable_error"]` | `generate_video_metadata` |
| `chunk`           | `video_chunks.status = "failed"`                           | `ensure_video_chunks`     |
| `frame`           | `video_frame_cache.status = "failed"`                      | `extract_video_frames`    |

`probe` / `poster` / `frame_timetable` 共用 metadata 任务，因此重试任一项都会重新跑视频 metadata 生成链路。`chunk` / `frame` 重试由 `video_frame_service` 重置对应行的状态、错误，再由存储 API 在提交后投递 media 队列；派发失败不会回滚已提交的 `pending` 状态，也没有隐式补偿。

存储管理页的错误栏支持鼠标点击或键盘激活，复制接口返回的完整错误原文；显示省略不会截断复制内容，复制成功或失败都会提示。

chunk 重试会清空旧的 `generation_mode` / `diagnostics`，下一次 worker 会重新判断 smart-copy eligibility。frame 重试保留 `frame_index`、`width`、`format` 及已有 `storage_key` / `byte_size`。

## Timetable 重建

旧视频或 probe 异常视频可重建 B1 帧时间表：

```bash
cd apps/api
uv run python -m app.cli.video.rebuild_timetable --dataset-item-id <uuid>
uv run python -m app.cli.video.rebuild_timetable --dataset-id <uuid> --keep-going
uv run python -m app.cli.video.rebuild_timetable --all --limit 100
```

命令为已登记原件显式创建新的 metadata output，使用同一套完整读取、原生生成、回读与发布合同；成功后原子替换元数据和时序索引。始终处理原视频，不使用 playback 转码副本。未知原件明确拒绝；失败不提前替换活动时序。`--dry-run` 只读取已有 owner，不下载、不投递。

单项失败保留其旧索引并记录错误；使用 `--keep-going` 时，CLI 从已保存的 UUID 和刷新后的行继续后续项，不复用回滚后过期的 ORM 属性。成功项仍逐项提交，最终只要有失败就返回非零退出码；继续处理不等于重试失败项。

### Sparse timetable（长视频）

超长视频不必给每帧都存一行 `VideoFrameIndex`。`--sparse-stride <N>`（默认 1 = 全帧）让重建只持久化**锚点子集**：`select_sparse_anchor_rows` 取「stride 网格上的帧 ∪ 所有关键帧」（保留关键帧使 chunk smart-copy 的 keyframe 对齐判定不退化）。锚点仍写进现有 `video_frame_indices`。

独立 output 保留完整探测时间表，HTTP timetable 和派生规格从该冻结结果读取；数据库索引只持久化所选锚点，包含首末帧及关键帧。切换 stride 会产生新 generation，不改写旧结果。

### 帧采样网格 helper

项目级 `Project.video_sampling`（`{mode: none|fps|step, target_fps?, frame_step?}`）只约束**标注导航/打点网格**，不改 `VideoFrameIndex`、不生成新资产（决策 D1）；标注 geometry 的 `frame_index` 永远是源视频帧号（决策 D2）。后端 `video_context` 持有共享上下文和与前端语义一致的纯函数：`derive_step(source_fps, sampling)` 派生步长、`derive_sampled_frames(frame_count, step)` 给出绝对网格（锚定 0：`[0, step, 2*step, …]`）。帧服务只保留上下文构建与缓存编排，导出/协作/追踪直接消费采样所有者，不通过旧导出或兼容别名。导出按采样网格重编号；MOT / KITTI / `yolo-frames-det` 都只输出网格帧，其中 `yolo-frames-det` 会把 `video_bbox` 与摊平后的 `video_track_bbox` 写成逐帧检测 label。逐帧导航语义见标注员手册「帧采样与软网格导航」。

## Segment 协同

```http
GET /api/v1/tasks/{task_id}/video/segments
GET /api/v1/videos/{dataset_item_id}/segments
POST /api/v1/tasks/{task_id}/video/segments/{segment_id}:claim
POST /api/v1/tasks/{task_id}/video/segments/{segment_id}:heartbeat
POST /api/v1/tasks/{task_id}/video/segments/{segment_id}:release
```

首次访问 manifest 或 segments 列表时，后端按 `VIDEO_SEGMENT_SIZE_FRAMES` 懒生成 `VideoSegment`。短视频默认单段；segment 是协作单位，chunk 仍是物理缓存单位，两者不要求对齐。

`claim` 会把未分配 segment 分配给当前用户并设置 `locked_by / lock_expires_at`。标注员只能 claim 未分配或分配给自己的 segment；锁未过期时其他非管理员用户 claim 返回 409。`heartbeat` 续约锁；`release` 释放锁但保留 assignee，方便用户稍后继续该段。

## Tracker Job

```http
POST /api/v1/tasks/{task_id}/video/tracks/{annotation_id}:propagate
PUT /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}
PATCH /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}
POST /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs
GET /api/v1/video-tracker-jobs?project_id=&status=&model_key=&cursor=&limit=
GET /api/v1/video-tracker-jobs/{job_id}
DELETE /api/v1/video-tracker-jobs/{job_id}
GET /api/v1/video-tracker-jobs/{job_id}/preview
POST /api/v1/video-tracker-jobs/{job_id}/accept
POST /api/v1/video-tracker-jobs/{job_id}/discard
POST /api/v1/video-tracker-jobs/{job_id}/decisions
```

创建 job 后由显式注入的 `VideoTrackerJobDispatcher` 派发，HTTP 依赖或 worker 入口装配 `CeleryVideoTrackerJobDispatcher`。具体适配器仍向 `gpu` 投递 `app.workers.video_tracker.run_video_tracker_job`，只传一个字符串 UUID 并保留返回的执行 ID；领域不消费 Celery 对象或注册名。当前支持四类 `model_key`：

| model_key                | 用途                                                                                  |
| ------------------------ | ------------------------------------------------------------------------------------- |
| `mock_bbox`              | 无 GPU contract adapter，复用输入 bbox 逐帧输出，供 CI / 前端对接使用。               |
| `sam2_video`             | 种子驱动的 SAM2 视频追踪；可消费源轨迹框或 `prompt.seeds` 点 / 框、多目标、多帧提示。 |
| `sam3_video`             | 文本驱动的多目标自动发现；每窗检测后由平台在窗口边界做 IoU 身份关联。                 |
| `sam3_video_interactive` | 种子驱动的 SAM3 PVS 追踪；点 / 框提示通过视频 memory 跨帧传播。                       |

真实 tracker 不再固定调用 `project.ml_backend_id`。`MLBackendService.get_tracker_backend()` 会在项目所有已启用 backend 中按 `health_meta.capabilities.supported_trackers` 选择：项目主后端支持该 tracker 时优先，否则选择其它 connected 的匹配 backend。没有能力匹配时返回不支持；`mock_bbox` 不需要 backend。

创建请求：

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "model_key": "sam2_video",
  "direction": "forward",
  "segment_id": "optional-segment-uuid",
  "prompt": {
    "seeds": [
      {
        "obj_id": 1,
        "prompts": [
          { "frame_index": 0, "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 } },
          {
            "frame_index": 24,
            "points": [
              [0.45, 0.5, 1],
              [0.7, 0.5, 0]
            ]
          }
        ]
      }
    ]
  },
  "sam_variant": "small"
}
```

`prompt.seeds[]` 按目标组织：`obj_id=1` 是主目标，额外 obj 在接受后各创建一条新轨迹；`prompts[]` 按绝对源帧保存提示，点的第三位 `label` 为 `1` 正点 / `0` 负点。单帧提示也可使用顶层 `bbox` / `points` 简写。`sam3_video` 使用顶层 `text`（必填）和可选 `exemplars`，不消费交互种子。完整 backend wire contract 见 [ML Backend Protocol](./ml-backend-protocol#22-interactive-predict单图或短交互)。

后端校验：

- task 必须是当前用户可见的视频 task。
- `annotation_id` 必须属于该 task 且未删除。
- `from_frame/to_frame` 必须在视频帧范围内，且不能反向。
- 非管理员用户必须先持有覆盖该 frame range 的有效 segment lock；跨 segment 请求会被拒绝。
- polyline 轨迹不允许发起传播；当前 runner 只处理 bbox / polygon 轨迹输出。

响应中的 `event_channel` 形如 `video-tracker-job:{job_id}`。前端可订阅：

```http
WS /ws/video-tracker-jobs/{job_id}?token=<access-token>
```

事件类型：

- `job_started`
- `frame_result`：`{ frame_index, geometry, confidence, outside, source }`
- `job_progress`：`{ current, total }`
- `job_completed`
- `job_failed`
- `job_cancelled`
- `job_partially_reviewed`
- `job_accepted`
- `job_discarded`

<ExcalidrawDiagram
  src="/diagrams/shared/video/video-tracker-human-loop.svg"
  alt="视频 AI 追踪从单轨、多轨、无源发现或人工纠错入口，经能力路由和固定实例执行分窗推理，把候选暂存后通过预览、局部决定、版本复核与整批兼容入口进入已接受或已丢弃状态"
  caption="视频追踪的运行事件、持久化候选与人工决策闭环"
/>

WebSocket 只承载运行期提示：`frame_result` 中的实例 id 仍可能是窗口局部值，最终 job 状态、候选内容、revision 与源版本必须从 HTTP job / preview 接口读取。正常完成会暂存候选并进入 `pending_review`；tracking 取消时可保留已收集的部分候选，correction 取消会清空候选，失败则没有可审候选。

局部 `/decisions` 以 revision、源 annotation version 和稳定 candidate key 做并发复核，决定后仍有候选时进入 `partially_reviewed` 并发布同名事件。`accept / discard` 是普通 tracking job 的整批兼容入口，不替代局部决定契约。

DB 状态机（`VideoTrackerJob.status`，独立于 WS 事件命名）：

```text
queued -> running -> pending_review -> partially_reviewed -> accepted | discarded
                  -> failed
                  -> cancelled --(tracking 有部分 staged_result)--> partially_reviewed | accepted | discarded
```

`completed` 仍保留在 schema 中兼容历史 job，但当前 runner 正常完成后进入 `pending_review`。`DELETE` 对 queued / running job 写 `cancel_requested_at`；tracking 取消前已收集的结果会暂存为部分候选，correction 取消则清空候选并释放租约。`accepted / discarded / failed` 属于终态；有暂存结果的 `cancelled` 仍可审阅。

`video_tracker_jobs.staged_result` 保存 `{results, grid_step, output_geometry}`，其中每条 result 可带 `frame_index / geometry / confidence / outside / instance_id / primary`。`GET .../preview` 只返回当前用户可见 task 的候选；accept / discard 还要求 job 创建者或项目特权角色。局部 decision 允许 job 创建者、已认领任务的审核员或项目特权角色执行，并在行锁后再次复核身份。所有决定都写审计动作。

`POST .../decisions` 接受两种互斥 selector：普通 selector 使用 `instance_ids + from_frame + to_frame`；
QC 区域 selector 使用 `qc_issue_id + candidate_digest`。后者要求当前 issue 是同 task、同 annotation、单帧且
具有可读取的 region Mask。accept 计算 `current XOR candidate` 后只把 region 内差异写入 annotation；reject
只从 staged candidate 扣除 region 内差异。剩余差异继续留在 staged result，并在内容变化后获得新的
candidate digest。`job_revision`、候选摘要或源版本过期均返回结构化 409。

`job_kind` 区分普通 `tracking` 与人工 Mask `correction`。后者同时保存 `track_id_snapshot` 和 `correction_frame`，prompt 冻结源 version / digest、segment lease、方向窗口和精确 backend / pool / model。同一 task + track 的活跃 correction 由 partial unique index 串行化；取消或终态释放租约。correction 不允许整批 accept / discard，只允许带 revision、源版本与显式窗口的局部 decision。

关键帧保存使用独立的 Mask keyframe PUT，删除 / manual outside / held 恢复使用同路径 PATCH，两者都强制 `If-Match`。帧操作按 task → task edit lock → segment → annotation 锁序列化，对 keyframe 与 outside 做局部几何更新，不改写其它帧的引用。纠错保存先于 job 创建，失败重试不得重复写关键帧。原生 correction 每方向只能执行一个 backend window；双向在人工帧处分成两窗，seed frame 不进入 staged candidate。仅 bbox seed 可用时必须由用户确认，并把 fallback 原因写入 lineage。

staged candidate 的保留期为 24 小时。每日内容 GC 先清理过期 staged result：待审 / 部分审阅转为 discarded，cancelled 保持 cancelled；随后对象引用扫描才允许删除已超过宽限期且不再被 annotation、prediction、有效 decision 或 staged job 引用的 RLE。

接受时先在同一事务内复核 task、assignment、segment lease 和按 UUID 排序锁定后的全部源 annotation，再把主实例回填到源、把额外 `instance_id` 创建为同类轨迹；人工关键帧不会被 prediction 覆盖。接受成功与丢弃都会清空 `staged_result`，后者不修改 annotation。该数据边界与批量预标的 `Prediction` 不同，见[视频 AI 追踪架构](../concepts/video-ai-tracking)。

SAM video adapter 会调用能力匹配的 ML Backend `/predict`：

```json
{
  "task": {
    "id": "<task_id>",
    "file_path": "<signed-video-url>",
    "dataset_item_id": "<dataset_item_id>",
    "file_name": "clip.mp4",
    "file_type": "video"
  },
  "context": {
    "type": "video_tracker",
    "model_key": "sam2_video",
    "job_id": "<job_id>",
    "task_id": "<task_id>",
    "project_id": "<project_id>",
    "dataset_item_id": "<dataset_item_id>",
    "annotation_id": "<annotation_id>",
    "from_frame": 0,
    "to_frame": 299,
    "direction": "forward",
    "prompt": {},
    "source_geometry": {},
    "seeds": []
  }
}
```

Backend 响应沿用交互式 `/predict` 响应，其中 `result` 是逐帧数组：

```json
{
  "result": [
    {
      "frame_index": 1,
      "geometry": { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.4, "h": 0.5 },
      "confidence": 0.91,
      "outside": false,
      "instance_id": "1",
      "primary": true
    }
  ]
}
```

长区间会分窗多次调用 backend；SAM3 系使用 `VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES`，其它 tracker 使用 `VIDEO_TRACKER_WINDOW_SIZE_FRAMES`。种子驱动 tracker 在后续窗按每个实例的上一窗末帧几何续种；文本 multiplex tracker 在窗边界按 IoU 把局部 id 映射为全局 `instance_id`。整体 job 仍只发布同一个事件流。`confidence` 低于 `VIDEO_TRACKER_LOW_CONFIDENCE_OUTSIDE_THRESHOLD` 的结果标为 outside；结果先进入 staged candidate，接受时才写入轨迹。

## 配置与指标

| 配置                                             | 默认值 | 用途                                                                   |
| ------------------------------------------------ | -----: | ---------------------------------------------------------------------- |
| `VIDEO_CHUNK_SIZE_FRAMES`                        |     60 | chunk 帧数                                                             |
| `VIDEO_CHUNK_WARMUP_LOOKAHEAD`                   |      1 | chunk warmup look-ahead，命中 chunk N 时顺带预解码 N+1..N+K；设 0 关闭 |
| `VIDEO_FRAME_CACHE_TTL_DAYS`                     |     14 | 单帧缓存 TTL                                                           |
| `VIDEO_CHUNK_CACHE_TTL_DAYS`                     |     30 | chunk 缓存 TTL                                                         |
| `VIDEO_FRAME_MEMORY_CACHE_ITEMS`                 |     64 | 进程内 frame array LRU 上限                                            |
| `VIDEO_SEGMENT_SIZE_FRAMES`                      |  18000 | 协作 segment 帧数                                                      |
| `VIDEO_SEGMENT_LOCK_TTL_SECONDS`                 |    300 | segment lock 心跳 TTL                                                  |
| `VIDEO_TRACKER_WINDOW_SIZE_FRAMES`               |    300 | tracker 调 ML Backend 的单次 frame window 上限                         |
| `VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES`          |     16 | SAM3 文本 / PVS tracker 的单次 frame window 上限                       |
| `VIDEO_TRACKER_LOW_CONFIDENCE_OUTSIDE_THRESHOLD` |   0.15 | 低置信度 tracker 结果写 outside 的阈值                                 |

Celery route：

- `app.workers.video_tracker.run_video_tracker_job` -> `gpu` queue

Prometheus 指标：

- `video_chunk_requests_total{status}`
- `video_chunk_generation_seconds{outcome}`
- `video_frame_cache_total{result,format}`
- `video_frame_extraction_seconds{outcome,format}`
- `video_frame_asset_bytes{asset_type}`
- `mask_ai_correction_jobs{status}`
- `mask_ai_correction_oldest_age_seconds{status}`
- `mask_ai_staged_mask_references{job_kind}`
- `mask_ai_accept_decisions{state}`
- `mask_ai_backend_inference_total{service,model_role,operation,fallback_reason,candidate_count,outcome}`

## 运维注意

修改 `apps/api/app/workers/media.py` 或 `apps/api/app/workers/video_tracker.py` 后必须重启 Celery worker；修改依赖或 Dockerfile 后需要 rebuild API/Celery 镜像。开发环境 worker 需要订阅 `gpu` 队列。
