---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-23
---

# Video Tracker Jobs

Video Tracker Job API 用于视频工作台里的异步 AI 追踪。它与批量预标 `Prediction` API 分开：推理结果先暂存为 job candidate，客户端可整批或按目标与帧窗口决定，接受后才写入 annotation。

Python SDK 的 `video_tracker` 提供创建、任务恢复、preview 与候选决策，`video_review` 提供 Mask 内容／关键帧、质量与轨迹操作；CLI 对应 `aap video-tracker` 和 `aap video-review`。创建没有幂等键，结果不确定时先查询 task active/reviewable jobs。计算完成不代表候选已接纳；决策须保留原 job_revision、expected_source_versions 和 selector/digest，409 后重新读取并明确决定。完整流程见 [视频、模板与存储运维](../../dev/sdk/video-and-templates)。

相关素材 `GET /videos/{dataset_item_id}/frames/{frame_index}`／`chunks/{chunk_id}` 及 task-scoped 同类路径返回 JSON 描述，未就绪时为 HTTP 202，包含 status、retry_after 和 Retry-After 头；ready 后通过短期签名 URL 读取媒体字节。查询可能创建缓存记录或排队生成，宜限定帧范围并按提示间隔观察。

## 创建追踪任务

任务级入口统一承载单源延展、多源批量延展和无源发现：

```http
POST /api/v1/tasks/{task_id}/video:track
Authorization: Bearer <token>
Content-Type: application/json
```

通过源字段选择模式：

| 模式         | 请求字段                         | 类别与落库语义                                                          |
| ------------ | -------------------------------- | ----------------------------------------------------------------------- |
| 单源延展     | 只传 `source_annotation_id`      | 继承源轨迹的类别与工具单位；主实例接受后回填该源轨迹                    |
| 多源批量延展 | 只传非空 `source_annotation_ids` | 每个源自动成为独立种子，并在接受后各自回填对应源轨迹                    |
| 无源发现     | 两个源字段都不传                 | 必须传 `target_class_name` 与 `target_tool_unit_id`；接受后全部新建轨迹 |

客户端不要同时传单数和复数源字段；当前服务会在复数列表非空时进入多源分支。单源最小请求：

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "model_key": "sam2_video",
  "direction": "forward",
  "output_geometry": "mask",
  "source_annotation_id": "11111111-1111-4111-8111-111111111111"
}
```

多源批量延展只需给出源轨迹列表；服务会读取每条源轨迹在 `from_frame` 的几何并分配稳定的 `obj_id`：

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "model_key": "sam3_video_interactive",
  "direction": "forward",
  "output_geometry": "mask",
  "source_annotation_ids": [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222"
  ]
}
```

单源延展还有一个路径参数形式的快捷入口，适合已有客户端继续使用：

```http
POST /api/v1/tasks/{task_id}/video/tracks/{annotation_id}:propagate
```

单源请求也可以在 `prompt.seeds` 中补充多目标、多帧提示；与源轨迹匹配的主实例会回填源，其余实例新建轨迹：

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "model_key": "sam3_video_interactive",
  "direction": "forward",
  "prompt": {
    "seeds": [
      {
        "obj_id": 1,
        "prompts": [
          { "frame_index": 0, "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 } },
          {
            "frame_index": 24,
            "points": [
              [0.5, 0.5, 1],
              [0.7, 0.5, 0]
            ]
          }
        ]
      },
      {
        "obj_id": 2,
        "prompts": [{ "frame_index": 0, "points": [[0.8, 0.4, 1]] }]
      }
    ]
  }
}
```

## 文本与视觉示例

文本自动发现使用 `model_key="sam3_video"`，并在请求顶层传必填 `text`。API 客户端还可用可选的 `exemplars` 收紧或排除相似实例：`bbox` 是归一化的 `[x1, y1, x2, y2]`，`label: true` 表示正示例，`label: false` 表示负示例。

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "model_key": "sam3_video",
  "direction": "forward",
  "text": "the red car",
  "target_class_name": "car",
  "target_tool_unit_id": "bbox",
  "exemplars": [
    { "bbox": [0.1, 0.2, 0.32, 0.58], "label": true },
    { "bbox": [0.62, 0.18, 0.81, 0.55], "label": false }
  ]
}
```

工作台的文本追踪面板当前只收集 `text`；视觉示例适用于直接调用此 API 的客户端。`frame_index` 与范围字段始终使用绝对源帧。

创建端点要求 task 对当前用户可见且可编辑，并且普通标注员持有覆盖整个范围的有效 video segment lock；请求不能跨 segment。单源 / 多源模式下，每条源 annotation 都必须属于该 task，折线轨迹暂不支持；无源模式下，目标类别必须存在于项目已启用的 `target_tool_unit_id` 绑定中。

响应是 `VideoTrackerJobOut`，其中 `event_channel` 可用于订阅 WebSocket：

```text
WS /ws/video-tracker-jobs/{job_id}?token=<jwt>
```

## 保存 Mask 纠错帧并定向重传播

视频 Mask 纠错分为两个显式步骤。先用 annotation 当前版本保存人工关键帧：

```http
PUT /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}
If-Match: W/"<annotation-version>"
Authorization: Bearer <token>
Content-Type: application/json
```

请求正文只接受已通过 task 级上传接口验证的 `coco_rle_ref`：

```json
{
  "mask": {
    "encoding": "coco_rle_ref",
    "size": [1080, 1920],
    "object_key": "raster-masks/sha256/ab/cd/<sha256>.json",
    "sha256": "<sha256>",
    "runs": 4096,
    "bytes": 16384
  },
  "source": "manual",
  "occluded": false
}
```

普通交互可省略 `source`，默认写成 `manual`；撤销 / 重做回放原关键帧时可传回原始 `manual | prediction` 值。服务端按 task → task edit lock → segment → RLE / upload → annotation 的顺序锁定，复核 assignment、segment lease、版本、媒体尺寸和内容引用，从 `outside` 中移除该帧，并返回新的 `ETag`。保存成功后，即使后续传播创建或入队失败，人工关键帧也保留。

关键帧删除和 `outside` 状态使用同路径的 `PATCH`，同样必须携带 annotation 当前版本：

```http
PATCH /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/mask-keyframes/{frame_index}
If-Match: W/"<annotation-version>"
```

```json
{ "operation": "delete_keyframe" }
```

`operation` 可为 `delete_keyframe | mark_outside | restore_held`。删除要求当前帧存在精确关键帧且轨迹仍有其它关键帧；`mark_outside` 只增加 manual 区间，`restore_held` 只移除覆盖当前帧的 manual 区间，不修改 prediction 区间。响应返回新 `ETag`、`X-Resolved-Keyframe-Frame` 与 `X-Restored-Held`；稳定冲突原因包括 `source_version_conflict`、`task_lock_conflict`、`annotation_locked`、`keyframe_missing`、`manual_outside_missing` 和 `frame_already_outside`。

再用返回的新 annotation version、RLE 摘要以及精确 backend/model 创建纠错 job：

```http
POST /api/v1/tasks/{task_id}/video/tracks/{annotation_id}/correction-jobs
```

```json
{
  "correction_frame": 50,
  "from_frame": 40,
  "to_frame": 50,
  "direction": "backward",
  "segment_id": "<segment-uuid>",
  "source_annotation_version": 8,
  "corrected_mask_digest": "<sha256>",
  "backend_id": "<backend-uuid>",
  "model_id": "sam3-pvs",
  "model_key": "sam3_video_interactive",
  "allow_bbox_fallback": false
}
```

创建时冻结 backend registry、service pool、model、方向窗口、源版本、segment lease 和 corrected digest。同一 task + track 同时只允许一个 `queued / running / pending_review / partially_reviewed` correction job。平台与 backend 的窗口上限取较小值；双向传播按纠错帧拆成两个独立单窗，纠错帧本身不会成为预测候选。

模型声明 `correction_frame + mask_prompt` 时使用原生 RLE seed。只支持 bbox 的模型必须由用户显式设置 `allow_bbox_fallback=true`；job lineage 会记录 `fallback_reason="mask_prompt_unsupported"`，文本模型还必须提供 `text`。纠错 job 只能通过 `POST .../{job_id}/decisions` 做局部接受或拒绝；对它调用整批 `accept / discard` 固定返回 `correction_requires_local_decision`。

稳定冲突 / 校验原因包括：

- `source_version_conflict`、`corrected_mask_digest_mismatch`；
- `correction_job_active`、`segment_lease_changed`；
- `mask_prompt_unsupported`、`correction_prompt_unsupported`、`text_required_for_bbox_fallback`；
- `tracker_window_capability_missing`、`correction_window_exceeds_native_limit`；
- `correction_requires_local_decision`。

## 查询 job 与候选

```http
GET /api/v1/video-tracker-jobs/{job_id}
GET /api/v1/video-tracker-jobs/{job_id}/preview
```

两个端点都按 job 所属 task 做可见性检查。preview 响应：

```json
{
  "job_id": "...",
  "status": "pending_review",
  "annotation_id": "...",
  "grid_step": 1,
  "output_geometry": "bbox",
  "job_revision": 1,
  "expected_source_versions": { "11111111-1111-4111-8111-111111111111": 3 },
  "candidate_total": 1,
  "candidate_pending": 1,
  "candidate_accepted": 0,
  "candidate_rejected": 0,
  "results": [
    {
      "frame_index": 1,
      "geometry": { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
      "confidence": 0.91,
      "outside": false,
      "instance_id": "1",
      "primary": true,
      "candidate_key": "1:1:...",
      "geometry_digest": "...",
      "source_annotation_id": "11111111-1111-4111-8111-111111111111",
      "target_annotation_id": "11111111-1111-4111-8111-111111111111",
      "manual_protected": false
    }
  ]
}
```

preview 只表示暂存候选，不代表 annotation 已经改变。没有 staged result 时 `results` 为空数组。

Mask preview 的 geometry 为 `{type:"mask", mask:coco_rle_ref, bbox?}`。内容通过下列鉴权端点读取，客户端应以 job id + instance id + frame + SHA-256 作为 staged cache identity：

```http
GET /api/v1/video-tracker-jobs/{job_id}/mask-content/{sha256}
```

## 接受、丢弃与取消

```http
POST /api/v1/video-tracker-jobs/{job_id}/accept
POST /api/v1/video-tracker-jobs/{job_id}/discard
POST /api/v1/video-tracker-jobs/{job_id}/decisions
DELETE /api/v1/video-tracker-jobs/{job_id}
```

这些写操作要求当前用户是 job 创建者，或对项目拥有特权角色：

- `accept`：只对带结果的 `pending_review` / `cancelled` job 应用候选。服务端在同一事务内重新锁定 task、segment 和按 UUID 排序的全部源 annotation，复核 task 状态、assignment、segment lease、源版本与 annotation lock；任一源被软删或变化都 fail closed 返回 409。单源模式把主实例回填源轨迹，额外实例新建；多源模式按 `instance_id` 把各实例回填各自源轨迹；无源模式全部新建。接受后立即清空 staged result（引用已由 annotation 保活），响应的 `touched_annotation_ids` 列出本次回填和新建的轨迹。
- `discard`：只允许带暂存结果的 `pending_review` / `cancelled` job，清空 staged result，annotation 保持不变；已丢弃时幂等返回，其它状态返回 `409`。
- `decisions`：显式选择 `instance_ids` 与闭区间 `from_frame / to_frame`，决定 `accept` 或 `reject`。请求还要回传 preview 的 `job_revision` 与 `expected_source_versions`；接受人工关键帧需在收到 `manual_keyframe_protected` 后由用户确认，再用 `override_manual=true` 重试。服务端只处理选区，未选目标、窗口外结果和未决 Mask 引用保持不变；相同决定可安全重试，响应以 `review_replayed=true` 标识回放。
- `DELETE`：请求停止 queued / running job。已计算的部分结果可能保留为 candidate，之后仍可接受或丢弃。取消状态与后台执行退役是两个阶段：API 先提交取消和一次性审计，再把同一 canonical job ID 投递给现有 worker 完成 proof-bound scope 退役。HTTP 200 的 `VideoTrackerJobOut` 只表示 job 已取消，不表示物理推理已停止或退役完成。

取消提交后若队列投递无法确认，API 返回 `503`，job 可能已经是 `cancelled`；可以对同一 ID 重试 `DELETE`。重复请求会重投同一 job，不会将状态改回 `queued` 或重复写取消审计。错误形状：

```json
{
  "detail": {
    "reason": "tracker_retirement_redrive_unavailable",
    "job_id": "11111111-1111-4111-8111-111111111111",
    "retryable": true
  }
}
```

项目删除和数据集解绑会继续拒绝尚无耐久退役证明的执行。已启动但没有 Jobs 执行账本的旧 job，以及 scope-only 孤儿，不能通过重复取消或 lease 到期补出证明；它们须保持阻塞，直到运维人员能验证旧 worker/backend 已围栏并完成 owner 认可的退役。

一般 tracking job 已计算的部分结果可在取消后继续审阅；correction job 取消时会立即丢弃 staged candidate、释放同轨活跃租约，但已保存的人工纠错关键帧不回滚。无人处理的 staged candidate 保留 24 小时，之后清理任务会清空引用：待审 / 部分审阅 job 转为 `discarded`，已取消 job 保持 `cancelled`。

局部决定请求示例：

```json
{
  "instance_ids": ["1"],
  "from_frame": 10,
  "to_frame": 20,
  "decision": "accept",
  "job_revision": 1,
  "expected_source_versions": {
    "11111111-1111-4111-8111-111111111111": 3
  },
  "override_manual": false
}
```

不要把 `DELETE` 当成“回滚已接受结果”；接受后需要通过 annotation API 做后续修改。

## 工作台刷新恢复

```http
GET /api/v1/tasks/{task_id}/video/tracker-jobs/reviewable
GET /api/v1/tasks/{task_id}/video/tracker-jobs/active
```

`reviewable` 返回当前任务中仍可审阅的 `pending_review`、`partially_reviewed`，以及带有 staged result 的 `cancelled` job，按创建时间倒序排列。客户端拿到 job 后继续调用 preview 端点加载逐帧候选；不要从 annotation 推导未接受结果。

`active` 返回当前任务下仍在运行的 `queued` / `running` job，供刷新后重连 WebSocket——这样运行中的追踪任务不会因整页刷新从界面消失，完成时仍能进入候选审阅。二者共享同一套 task 可见性与归属规则：先执行 task 可见性校验，普通用户只恢复自己创建的 job，项目 owner / 超级管理员可恢复该任务下全部 job。

## 状态语义

| status               | 含义                                                    |
| -------------------- | ------------------------------------------------------- |
| `queued`             | 已入队，worker 尚未开始                                 |
| `running`            | 正在分窗推理                                            |
| `pending_review`     | 推理完成，候选已暂存，annotation 未改                   |
| `partially_reviewed` | 已决定部分候选，仍有 staged result 待审                 |
| `cancelled`          | 已取消；可能仍有部分 staged result                      |
| `accepted`           | 候选已写入 annotation                                   |
| `discarded`          | 候选已丢弃                                              |
| `failed`             | 推理失败，无可接受结果                                  |
| `completed`          | 历史兼容状态；当前 runner 正常完成使用 `pending_review` |

## 管理员列表

```http
GET /api/v1/video-tracker-jobs?project_id=<uuid>&status=pending_review&model_key=sam2_video&limit=20&cursor=...
```

该列表仅项目管理员 / 超级管理员可调用，按 `created_at DESC, id DESC` 做 cursor 分页。超级管理员可读全局；项目管理员的 items 与 counts 始终限制在自己拥有的项目内，显式传入他人项目 id 只会得到空交集。响应包含：

- `items[]`：job、项目、模型、范围和时间字段。
- `next_cursor`：下一页游标。
- `counts`：在当前 `project_id / model_key` 过滤下按 status 聚合；counts 刻意忽略列表的 `status / cursor`，用于状态筛选器总数。

## 相关文档

- 架构与数据边界：[视频 AI 追踪架构](../../dev/concepts/video-ai-tracking)
- Worker / backend contract：[视频后端帧服务](../../dev/reference/video-frame-service#tracker-job)
- WebSocket：[WS 协议](../../dev/reference/ws-protocol#6-wsvideo-tracker-jobsjob_id)
- 运行时排障：[视频帧服务 Runbook](../../ops/runbooks/video-frame-service)
- OpenAPI 路由索引：[自动生成路由](./_routes.generated)
