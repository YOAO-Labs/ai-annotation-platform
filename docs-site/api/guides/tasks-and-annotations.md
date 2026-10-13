---
audience: [dev]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-09-12
---

# 任务与标注

## 任务模型

`tasks` 表的每行代表一条待标数据，可以是一张图片、一个视频任务，或其它项目类型的数据。它属于一个 batch，batch 属于 project。任务状态以 `pending / in_progress / completed / review` 为主，批次状态负责更高层的生产推进。

```
pending → in_progress → completed → review
                 ↑          ↘ reviewer reject
                 └────────────── pending
```

## 拉取下一个任务

```http
GET /api/v1/tasks/next?project_id=<project_uuid>&batch_id=<batch_uuid>
```

调度器优先复用当前账号的任务，再选择可领取任务。客户端进入工作台后通过显式锁接口取得或续期编辑锁；返回任务本身不保证残留锁仍有效，详见[任务锁](../../dev/concepts/task-locking)。

## 保存标注与提交任务

```http
POST /api/v1/tasks/:id/annotations
Idempotency-Key: 61fffd41-c706-48c7-818e-0c40cdbeb6c9

{
  "annotation_type": "bbox",
  "tool_unit_id": "bbox",
  "class_name": "dog",
  "geometry": { "type": "bbox", "x": 0.12, "y": 0.34, "w": 0.56, "h": 0.28 },
  "attributes": { "color": "brown" }
}
```

该接口保存单条标注，返回 `201` 和标注对象。可选 `Idempotency-Key` 长度为 1–128 字符：同一任务、账号、键和请求内容的重试返回首次结果；复用键发送不同内容返回 `409 idempotency_conflict`。新操作使用新键；不带键时保留每次创建新对象的行为。服务端将标注、去重记录和审计一起提交，避免响应丢失后的重试重复创建。

每次写入和重试都会重新检查账号、任务可见性、当前归属及可编辑状态。员工停用后重新启用，也不能用旧离线操作修改已经交接的任务。

全部标注保存并同步后，调用 `POST /api/v1/tasks/:id/submit` 将任务送审并释放当前锁；保存标注本身不会提交审核。

## 选中框二次推理

`POST /api/v1/tasks/:id/annotations/:annotation_id/secondary-inference` 在选中标注的 ROI 上运行模型，按 `write_target` 写回属性或创建子标注。

仅 `super_admin`、`project_admin`、`annotator` 可调用，且仍需通过项目与任务可见性、当前归属和可编辑状态检查；API Key 还需 `annotations:write` scope。审核员调用返回 `403`，不会执行推理或写入结果。审核员通过普通标注接口进行人工修正的权限保持原有规则。

## 任务讨论与问题线程

```http
GET /api/v1/tasks/:id/discussion/page?scope=all&limit=50
GET /api/v1/tasks/:id/discussion/annotation-counts
GET /api/v1/feedbacks?project_id=…&task_id=…&kind=issue&root_only=true&include_counts=true&status=open
GET /api/v1/feedbacks/:root_id/thread?limit=50
```

讨论分页接口默认读取当前任务的全部评论，`scope=task` 只读任务留言，`scope=annotation` 必须同时传入真实标注的 `annotation_id`。其他范围不接受 `annotation_id`。每页默认 50 条，最多 200 条；返回 `items`、`next_cursor` 和当前范围的准确 `total`。

每条记录保留 `source`、原始 `data` 与服务端计算的 `actions`，客户端据此展示来源并调用原写入接口：

| 来源                 | 读取内容                                                      | 写入与附件归属                                               |
| -------------------- | ------------------------------------------------------------- | ------------------------------------------------------------ |
| `annotation_comment` | 原标注评论的正文、提及、附件、画布批注和帧锚点                | 继续调用标注评论接口；附件下载使用记录本身的 `annotation_id` |
| `feedback`           | `kind=comment`、`anchor_type=task` 且没有父回复的原生任务留言 | 创建调用 `POST /feedbacks`，修改和删除调用 `/feedbacks/:id`  |

汇总不读取评论镜像或统一反馈视图，也不混入问题回复、BUG 和退回记录。结果按 `(created_at, source, id)` 降序排列；游标绑定任务、阅读范围和标注，不能用于其他查询。游标格式错误、超过 2048 字符或与查询不匹配时返回 `400`。旧标注评论接口继续保留。

图片任务的原生留言可在 `POST /feedbacks` 携带可空的 `canvas_drawing`，沿用标注评论的 `CanvasDrawing` 结构（归一化坐标和非空笔触）。只允许 `kind=comment`、`anchor_type=task`、无父记录且属于可访问图片任务的目标；视频、点云、问题和回复不接受绘图。正文、已有兼容附件或非空绘图至少一项有内容，因此允许仅绘图的任务留言。读取反馈和讨论分页都会返回该字段；已有绘图的留言可将正文修改为空。此字段不扩展任务附件上传能力。

所有媒体类型的原生任务留言均支持 `mentions`，沿用标注评论的结构：`userId`、`displayName`、`offset`、`length`。历史记录和省略字段时使用空列表，反馈读取与讨论分页保留提及。非空提及只接受在 `kind=comment`、`anchor_type=task` 且没有父记录的原生任务留言中；问题、回复和其他反馈目标会明确拒绝。创建时校验项目成员身份，通知前再检查接收人的账号状态和任务可见性。

编辑原生任务留言时，可在 `PATCH /feedbacks/:id` 同时提交正文和更新后的 `mentions`。只修改正文且没有提供提及列表时，会清除旧位置信息；正文完全不变时保留原提及。显式提交空列表会清除提及。提及修改仍检查成员和编辑权限，编辑不会再次发送提及通知。

`discussion/annotation-counts` 返回 `{ "counts": { "annotation-uuid": 3 } }`，一次统计当前任务可用标注的全部有效原评论，包含已解决评论。按标注 ID 分组，只返回正数，不受分页影响，不计反馈镜像；已删除评论或不可用标注不计入。接口沿用任务讨论可见性检查，数量不代表每用户未读数。

标注附件下载接口 `GET /annotations/:annotation_id/comment-attachments/download?key=...` 默认仍返回短期签名地址的 302 跳转。浏览器使用 Bearer 登录时，应先带认证头请求 `as_json=true`，取得 `{ download_url }` 后不携带 Bearer 头下载对象；直接打开 API 链接不会自动携带登录凭据。两种返回方式都重新校验原标注的任务权限和附件键前缀，签名有效期为五分钟。

反馈列表的 `root_only` 和 `include_counts` 默认为 `false`，旧调用方式不变。开启 `include_counts` 后，`total` 为当前筛选的准确数量，`status_counts` 返回 `open`、`resolved`、`wont_fix` 各自数量，只忽略 `status` 筛选。未解决数量应读取 `status_counts.open`，不能用当前已加载卡片数替代。

线程接口返回根记录 `root`、回复 `items`、`next_cursor` 和回复总数 `total`。回复按时间和 ID 降序分页，历史多层回复会展开为同一会话；已删除的中间回复不展示，其仍有效的后代可以读取。根记录删除、祖先链循环或跨项目、任务、标注锚点时，相关子树不可访问。标注评论镜像不能用作问题线程。

新回复通过 `POST /feedbacks/:root_id/replies` 提交，工作台回复仅提供纯文本，不能为空白。已有回复 API 的附件字段保持兼容；工作台任务留言和问题回复不提供附件上传下载，问题回复不支持提及或富文本编辑。

图片和视频的像素问题可以同时携带 `annotation_id` 与 `anchor_position`。对象必须属于同一项目和任务；取消对象关联时保留像素坐标，视频同时移除关联对象的轨迹和版本信息。旧问题没有对象关联时仍然有效。`severity=blocker` 是普通问题的严重度，不改变任务提交和审核规则。

列表、线程和写接口都会检查项目与任务权限。`actions` 包含 `edit`、`change_status`、`delete`、`reply`，仅作客户端能力提示，服务端仍独立校验每次操作。作者和管理员可编辑、删除；非作者审核员仅可修改问题状态，混入正文、标题或严重度的请求整体返回 `403`。删除采用软删除，不会把子回复转成任务留言。

### 讨论通知

创建问题回复、修改根问题状态和创建含有效提及的任务留言或原标注评论，会使用现有通知接口：`GET /notifications`、`POST /notifications/:id/read` 和 `/notification-preferences`。通知与业务数据同事务提交，成功后再尽力推送 WebSocket；Redis 故障不撤销已保存的回复或评论。

| 事件                           | 目标                                        | 载荷中的额外身份           |
| ------------------------------ | ------------------------------------------- | -------------------------- |
| `feedback.reply_created`       | `target_type=feedback`，根问题 ID           | `reply_id`                 |
| `feedback.status_changed`      | `target_type=feedback`，根问题 ID           | `from_status`、`to_status` |
| `feedback.comment_mentioned`   | `target_type=feedback`，原生任务留言 ID     | 任务与项目身份             |
| `annotation.comment_mentioned` | `target_type=annotation_comment`，原评论 ID | `annotation_id`            |

载荷共同包含 `project_id`、`task_id`、`source`、`actor_name`，不复制正文。收件人按当前访问权限、账号状态和偏好过滤，并排除操作人、去重；无实际状态变化不发通知。收到载荷不代表继续拥有目标权限，客户端打开时仍需通过任务、根线程或原标注评论接口重新校验。通知已读不代表整个线程已读，也不新增任务留言或问题回复的提及、附件能力。

#### 通知偏好

`GET /api/v1/notification-preferences` 返回账号全部已知类型的偏好：

```json
{
  "items": [{ "type": "task.rejected", "in_app": true, "email": false, "toast": true }]
}
```

- `in_app`：是否接收该类型通知。关闭后该类型新事件不写入、不推送、不计数；无记录默认 `true`。
- `toast`：可见页面是否弹出瞬时提醒，是**有效值**——显式存储的选择优先，未存储时按类型的默认重要程度计算。调用方直接读取即可，不需要自己推导默认。
- `email`：保留字段，当前固定为 `false`，不消费。

`PUT /api/v1/notification-preferences` 更新单个类型，请求体至少提供 `in_app`、`toast` 之一：

```json
{ "type": "task.rejected", "toast": false }
```

- 只合并请求中出现的键（数据库按 JSONB `channels || patch` 原子合并）：未提供的 `in_app`/`toast`/`email` 以及并发标签页写入的其它键都保留，不会被缺省值覆盖。
- `type` 必须是已知类型，否则 `400`；`in_app` 与 `toast` 同时缺省返回 `400`；显式传 `null` 校验失败。
- 写入成功返回 `{"ok": true}` 并发布 `notifications.sync reason=preferences`，其它会话据此刷新偏好；该事件不产生通知，也不改未读数。

## 视频问题反馈

`POST /api/v1/feedbacks` 复用像素锚点合同保存视频问题，`anchor_position.frame` 为从 0 开始的源视频帧号，x/y 为画面内 0–1 相对坐标：

```json
{
  "kind": "issue",
  "anchor_type": "pixel",
  "project_id": "00000000-0000-0000-0000-000000000001",
  "task_id": "00000000-0000-0000-0000-000000000002",
  "annotation_id": "00000000-0000-0000-0000-000000000003",
  "anchor_position": {
    "x": 0.5,
    "y": 0.25,
    "frame": 130,
    "video_context": {
      "schema_version": 1,
      "track_id": "trk_car_left",
      "annotation_version": 7,
      "frame_range": { "from_frame": 120, "to_frame": 160 },
      "viewport": { "center_x": 0.5, "center_y": 0.4, "zoom": 2 },
      "timeline_window": { "from": 110.5, "to": 170.5 }
    }
  },
  "body": "这一段目标区域需要复核"
}
```

通过 `GET /api/v1/feedbacks?project_id=…&task_id=…&kind=issue` 读回同一锚点。不绑定画面位置的问题使用 `anchor_type: "task"` 和 `anchor_position: null`；图片像素锚点继续省略 frame。工作台在实际源帧就绪后冻结视频创建锚点，提交时不重新读取播放头。

`video_context` 为可选扩展，保存在原有 JSONB 中。只有视频任务的 pixel 锚点可以写入，并须同时携带整数源帧 `frame`。版本为整数 `1`；其余字段均可省略，但提供范围、视口或时间窗时必须完整提供该子对象。

| 字段                 | 合同                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------- |
| `track_id`           | 可选普通字符串，保留对象身份线索，不要求 UUID                                          |
| `annotation_version` | 大于等于 1 的整数，要求顶层 `annotation_id`；记录捕获版本，不要求等于提交时最新版本    |
| `frame_range`        | 从 0 开始的源帧整数闭区间，包含 `frame`，且不超过视频最后一帧                          |
| `viewport`           | 视频归一化坐标中的视口中心，可位于画面外；`zoom` 为 `scale / fitScale`，必须为正有限数 |
| `timeline_window`    | 源帧窗口，允许分数帧；`from ≤ to`，且在视频范围内                                      |

新上下文拒绝未知字段、布尔或字符串形式的整数、非有限数以及不完整子对象。服务端还检查任务属于该项目、标注属于该任务及项目、媒体边界和访问权限。无任务权限时返回 404；字段或关系非法返回 422；媒体元数据尚未就绪返回 503。省略 `task_id` 的项目反馈列表仍按任务可见性过滤，并返回 `next_cursor`。

历史上下文按原值读取，未知 `schema_version` 不按当前版本解释；旧 `frame/x/y` 仍可用于定位。原对象变化后，已有反馈与继承同一锚点的回复仍可读取。Python SDK 的 `client.feedbacks.create()` 和 `client.feedbacks.list()` 提供对应类型化入口，创建字段与接口保持一致。

## 视频任务

视频轨迹标注的 `geometry.keyframes[].source` 只代表该关键帧记录的来源。读取矩形框、多边形、折线和 Mask 轨迹时，旧数据未保存的 `source` 字段保持缺省，客户端应显示「来源未知」，不能用整条标注的 `source` 替代。新建关键帧未指定来源时仍按既有写入规则保存为 `manual`；此读取规则不修改已存几何或补写旧数据。

视频任务会在 `GET /api/v1/tasks/:id` 的 `TaskOut.video_metadata` 里透出标准化视频元数据：

```json
{
  "video_metadata": {
    "duration_ms": 1000,
    "fps": 25,
    "frame_count": 25,
    "width": 640,
    "height": 360,
    "codec": "mpeg4",
    "playback_path": "playback/...",
    "playback_codec": "h264",
    "poster_frame_path": "thumbnails/...",
    "frame_timetable_frame_count": 25
  }
}
```

工作台播放视频前会再请求 manifest：

```http
GET /api/v1/tasks/:id/video/manifest
```

如果原视频编码不是浏览器稳定支持的 H.264，media worker 会生成 `playback/*.mp4`；manifest 的 `video_url` 优先返回该播放版本。

返回 presigned 播放地址、poster 地址和同一份标准化 metadata。非视频任务会返回 `400`。

工作台还会读取帧时间表，用真实 `pts_ms` 替代单纯 `frame / fps`：

```http
GET /api/v1/tasks/:id/video/frame-timetable?from=0&to=120
```

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

存量视频没有时间表时会返回 `source: "estimated"` 和空 `frames`，前端继续按 `fps` 估算。

逐帧 `video_bbox` 表示单个 frame 上的独立框：

```json
{
  "annotation_type": "video_bbox",
  "geometry": {
    "type": "video_bbox",
    "frame_index": 12,
    "x": 0.1,
    "y": 0.2,
    "w": 0.3,
    "h": 0.4
  }
}
```

新建视频轨迹标注默认使用 compact `video_track_bbox`，一条 annotation 表达一个对象轨迹：

```json
{
  "annotation_type": "video_track_bbox",
  "class_name": "person",
  "geometry": {
    "type": "video_track_bbox",
    "track_id": "trk_...",
    "keyframes": [
      {
        "frame_index": 0,
        "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
        "source": "manual",
        "occluded": false
      }
    ]
  }
}
```

`video_track_bbox.keyframes[]` 只保存关键帧；插值帧由前端按需计算，不会展开写入 `annotations` 表。旧 `video_bbox` 数据仍可读取和显示。

逐像素视频轨迹使用 `video_track_mask`。关键帧内只保存内容寻址引用；帧间按最近关键帧保持，不做像素插值：

```json
{
  "annotation_type": "video_track_mask",
  "tool_unit_id": "region",
  "class_name": "person",
  "geometry": {
    "type": "video_track_mask",
    "track_id": "trk_...",
    "keyframes": [
      {
        "frame_index": 0,
        "source": "manual",
        "mask": {
          "encoding": "coco_rle_ref",
          "size": [1080, 1920],
          "object_key": "raster-masks/sha256/...json",
          "sha256": "...",
          "runs": 312,
          "bytes": 3727
        }
      }
    ]
  }
}
```

客户端先 `POST /api/v1/tasks/{task_id}/mask-content` 上传 `{encoding:"coco_rle",size,counts}` 获得引用，再创建或更新 annotation。需要对象存储压缩时增加 `storage_encoding:"gzip"`；需要压缩 HTTP 请求时仍发送相同 JSON，并设置 `Content-Encoding:gzip`。返回引用的 `encoding` 始终是 `coco_rle_ref`，gzip 引用额外带 `storage_encoding:"gzip"` 和 `.json.gz` key。

视频单帧 Mask 使用同一种引用，但不持有 `track_id` 或 `keyframes[]`：

```json
{
  "annotation_type": "video_mask",
  "tool_unit_id": "region",
  "class_name": "person",
  "geometry": {
    "type": "video_mask",
    "frame_index": 12,
    "mask": {
      "encoding": "coco_rle_ref",
      "size": [1080, 1920],
      "object_key": "raster-masks/sha256/...json",
      "sha256": "...",
      "runs": 312,
      "bytes": 3727
    }
  }
}
```

图片的原生像素 Mask 使用相同引用，但 geometry 是单态的 `raster_mask`：

```json
{
  "annotation_type": "raster_mask",
  "tool_unit_id": "region",
  "class_name": "person",
  "geometry": {
    "type": "raster_mask",
    "mask": {
      "encoding": "coco_rle_ref",
      "size": [1080, 1920],
      "object_key": "raster-masks/sha256/...json",
      "sha256": "...",
      "runs": 312,
      "bytes": 3727
    }
  }
}
```

每个 task 最多保留 256 个尚未被 annotation POST/PATCH 认领的匿名上传；重复上传相同内容不重复占额度。达到上限返回 `422 mask_quota_exceeded`。图片和视频单帧 Mask 使用 `GET /api/v1/annotations/{annotation_id}/mask-content`；视频 Mask 轨迹的当前解析帧使用带 `{frame_index}` 的路径，兼容客户端也可以用带帧路径读取单态 Mask。对象或帧路径会随编辑及恢复指向新内容，因此返回 `Cache-Control: private, no-cache`，每次按当前摘要重新校验；`If-None-Match` 命中返回 304，摘要变化返回完整 RLE。对象损坏或尺寸失配返回 409，存储暂时不可用返回可重试 503。部署可以分别控制读取和创建；创建关闭时仍允许安全读取存量 geometry。

## Mask 实例原子操作

切割、拆分、复制、合并和严格非重叠会在一个任务级事务内提交：

```http
POST /api/v1/tasks/:id/annotations/mask-mutations:commit
```

请求的核心字段是：

- `idempotency_key`：同一预览重试必须复用同一个 key；同 key 异参返回 `idempotency_conflict`。
- `operation`：`slice_mask`、`split_components`、`copy_component`、`copy_keyframe`、`join_masks` 或 `overlap`。`copy_keyframe` 只用于视频，并要求 `source_frame_index`。
- `scope`：固定 image / video、当前帧与 segment、同类 / 全部对象过滤、overlap policy 和是否要求严格非重叠。
- `scope_fingerprint` 与 `expected_versions`：必须来自同一个预览快照；版本项按 annotation UUID 排序并覆盖范围内全部对象。
- `mutations`：只允许有判别字段的 `update | create | delete`；geometry 只能是 `raster_mask` 或 `video_track_mask`，新内容引用必须先由当前任务上传保留。
- `report`：只接收受控的面积、拓扑、连通性和受影响对象摘要；不得携带 RLE counts 或任意客户端 JSON。服务端会从实际 RLE 重算并覆盖可审计指标，不信任客户端面积或变化像素。

服务端在同一事务内复核任务可见性与可编辑状态、对象与分段锁、范围成员、版本、类别、帧和内容引用，然后一次写入
annotation 变更、内容关联、操作账本、lineage、任务统计和聚合审计。响应只返回操作 ID、对象 ID / 版本、删除 ID、lineage、摘要和审计 ID，不回传完整 geometry 或 RLE。

服务端还会对像素代数做权威校验：component copy 必须等于指定 4/8 连通性下的一个完整源连通域，keyframe copy 必须与来源帧当时解析的完整 RLE 逐像素相同，split 的每个结果必须是完整连通域且不重叠地覆盖全部来源，join 必须等于全部来源并集，overlap 必须精确等于「原对象 − 主 Mask」。视频 keyframe copy 即使来源在目标帧不可见，也会把来源加入范围指纹与版本锁；新轨只含目标帧的 manual keyframe。视频 join 只允许在当前帧创建副本并保留源轨迹；不允许用单帧请求删除整条来源轨迹。视频 update 只重验当前帧新引用，其他关键帧必须与已锁定的源 geometry 逐项相同，不会在提交时重读整条历史轨迹的对象。

请求体上限为 12 MiB，范围候选对象、版本项、mutation 和引用的 RLE 对象各不得超过 1000，单次验证的 RLE runs 总数不得超过 200 万，派生 RLE 不得超过 100 万 runs，累计代数与连通域扫描不得超过 500 万步，严格非重叠经过 bbox 剪枝后最多比较 10 万对。范围查询在 SQL 层使用 `limit + 1` 预检；超限请求会在无界加载或更大规模的像素计算之前被拒绝。

缺少范围版本返回 `428 expected_versions_missing`。范围成员变化、版本漂移、任务 / 对象 / 分段锁冲突分别返回结构化 409 reason；操作合同、类别、geometry、引用或空结果无效返回结构化 422 reason。任一失败都不会留下部分 annotation 或账本记录。

### Mask 直线切割

`operation: "slice_mask"` 仅用于图片 Raster Mask，必须包含恰好两个归一化有限端点的 `cut_path: [[x1,y1],[x2,y2]]`，端点不同；其它 operation 不接受 `cut_path`。mutations 必须为同一来源的一次 update 和一次 create，不得删除对象。仍须提交完整 scope 指纹、范围版本、预览结果引用及受控 report。

服务端按像素中心 `((x+0.5)/W,(y+0.5)/H)` 对有向直线计算叉积，`>= 0` 属于左侧，其他属于右侧，前后端均不使用 epsilon。按 RLE 前景列区间和二分边界独立重算两块，逐像素验证提交的结果；不重叠、并集等于来源且两个结果非空。像素较多的区域必须为 update，数量相等时左侧必须为 update。来源可以有孔洞与多连通分量，`split_components` 仍仅允许拆出完整原连通分量。

切割来源必须已保存、未锁定、没有活动子对象，保留原有身份、属性和父对象。新对象复制类别、工具单元、业务属性、属性来源、z_order 和原父对象，使用操作者与 manual 来源，不继承预测身份、置信度或外部轨迹身份。

响应额外提供 `slice_restore`，其字段与下文 Polygon 切割响应相同。使用其中 `slice_operation_id` 和 `result_versions` 调用共享 `slices/{operation_id}:restore`；两块对象在一次 undo / redo 中保留 ID、递增版本，失败保持全部原状。恢复账本只记录前后版本号、内容摘要和活动状态，RLE 正文和内容引用来自当前对象或 `MaskAnnotationRevision`。每次切割 / 恢复事务把相关 revision 的保留期非缩短地保护到原切割后 30 天，无限保留项仍为无限。GC 无需新增扫描来源；缺失、过期或损坏的恢复引用返回 `409 snapshot_unavailable`，整次恢复拒绝。原操作超过 30 天返回 `410 restore_expired`。

## Polygon 原子切割与恢复

`POST /api/v1/tasks/{task_id}/annotations/polygon-slices:commit` 只接受以下字段：

```json
{
  "annotation_id": "11111111-1111-4111-8111-111111111111",
  "expected_version": 3,
  "idempotency_key": "slice_commit_20260908_001",
  "cut_path": [
    [0.7, 0.03],
    [0.7, 0.97]
  ]
}
```

切线为 2–256 个有限归一化坐标点，不能自交；来源必须是图片内已保存、未锁定、无活动子对象的简单单外环 Polygon。切线只允许穿越两次，允许边界端点；相切、沿边重叠、多次进出、孔洞和 multi_polygon 拒绝。服务端独立重算两块非空区域，验证它们分割且覆盖来源，面积误差阈值为 `max(1e-10, source_area * 1e-8)`。切线交点与自交候选边对各设固定百万步检查预算。

较大区域保留来源 ID，等面积按质心 x / y 排序。新对象复制类别、工具单元、业务 attributes、attributes_meta、z_order 及原 parent_annotation_id，使用 manual 来源与当前操作者，confidence、parent_prediction_id 为空。来源身份与属性保留，追溯通过 lineage 保存。

响应包含本次 `operation_id`、原切割 `slice_operation_id`、`source_annotation_id`、`created_annotation_id`、两个对象的完整 `result_versions`、`active_annotation_ids`、当前 `target`、`restore_expires_at`、`idempotent_replay` 与 `no_op`。读取完整结果沿用任务 annotations GET。

`POST /api/v1/tasks/{task_id}/annotations/slices/{operation_id}:restore` 的 operation_id 必须指向原切割：

```json
{
  "target": "before",
  "expected_versions": {
    "11111111-1111-4111-8111-111111111111": 4,
    "22222222-2222-4222-8222-222222222222": 1
  },
  "idempotency_key": "slice_restore_20260908_001"
}
```

`target` 为 `before` 或 `after`。必须携带原切割或上次成功恢复返回的完整版本集，含 inactive 对象；服务端还会核对该版本集等于账本记录。不得刷新任意最新版本以覆盖后续修改。几何与活动状态只从服务端账本快照或 Mask 版本引用恢复，不接受客户端回滚 geometry。ID 保持、版本递增，恢复响应继续返回两个 ID 的全部版本。恢复到当前同一侧时 `no_op=true`，对象版本不变。

两个接口均要求当前任务可见、可编辑且任务锁允许写入，以及 `annotations:write` scope。相同 actor / task / key 同参回放首次响应，异参返回 `409 idempotency_conflict`。同一次超时重试复用原 key；下一次 undo / redo 必须使用新 key。对象版本变化、锁定或活动子对象分别返回 `409 version_mismatch / annotation_locked / active_children`，非法切线返回 `422 invalid_cut`。原切割提交后 30 天返回 `410 restore_expired`，撤销与重做不续期。

每次提交或恢复都在单一事务内写入对象、统计、操作账本、lineage 与审计；任一失败不产生部分结果。Python SDK 将两个工作台内部接口明确列为 excluded，尚未提供对应客户端方法。实现与回滚约束见 [ADR-0074](/dev/adr/archive/0074-atomic-annotation-slice-restore)。

## 标注转换计划

图片与视频的几何转换使用「预检计划 → 原子执行」两步接口，客户端不得用多次 annotation PATCH 模拟批量 replace：

```http
POST /api/v1/tasks/:id/annotation-conversions:dry-run
POST /api/v1/tasks/:id/annotation-conversions:execute
```

dry-run 接收最多 100 个同来源类型的 `annotation_ids`、目标 `mask | polygon | bbox`、`copy | replace`、范围 `image | current_frame | keyframes`，以及当前帧和是否允许物化 held 帧。响应返回十分钟有效的不可预测 `plan_token`、逐对象报告与汇总；服务端计划冻结来源版本、geometry 内容摘要、目标 geometry manifest 和内容摘要，token 本身只以 SHA-256 摘要落库。预览不写对象存储、不创建上传 reservation；取消或重复预览不会占用 Mask 配额。
单次请求的原始尺寸 × 待转换对象 / 可见关键帧数不得超过 32 Mi 像素，超限返回 `422 conversion_budget_exceeded`，避免批量请求长时间占用 API 进程。

支持的转换为：图片 polygon / multi-polygon → `raster_mask`，图片 `raster_mask` → polygon / multi-polygon 或紧致 bbox；视频单帧 polygon、polygon track 当前帧或全部可见关键帧 → `video_track_mask`；视频 Mask track 当前帧 → `video_bbox`。Mask → bbox 始终是紧致轴对齐框，不生成 rotated bbox。当前帧 replace 只抑制来源轨迹的当前帧；keyframes replace 才原位替换整条来源轨迹。

execute 只接收 `plan_token`、任务内 actor 维度的幂等 key，以及 replace / lossy 的显式确认。服务端按 task → task edit lock → video segment → Mask 内容引用 → upload reservation → annotation UUID 的顺序加锁，重新验证计划归属、TTL、来源快照、类别和写入闸，并重算转换结果与冻结报告。只有两者一致后才预留配额，取得 annotation 行锁并再次验证快照，然后写入新 Mask 对象；最后在同一个事务内写 annotation、内容关联、`convert_annotations` operation、`converted` lineage、任务统计与聚合审计，并返回完整的更新 / 新建对象。相同 key 与相同请求回放首次响应，即使短期计划已清理；同 key 异参、计划已消费、版本漂移、报告漂移或锁冲突返回结构化 409，任一失败都不会部分转换。

## 原生 AI Mask 候选采纳

交互式推理返回的原生 Mask 是短生命周期候选，不应由客户端拆成“上传内容 + 创建标注”两次写操作。客户端应把候选和平台签发的 receipt 提交到任务级原子采纳接口：

```http
POST /api/v1/tasks/:id/ai-mask-candidates/accept
```

请求包含 `idempotency_key`、候选 RLE 与 `candidate_id`、`prompt_revision`、receipt、类别、目标、prompt 计数摘要、实际路由和推理摘要。图片新建使用 `target.mode=create`；视频还要给出当前 `frame_index`；精修使用 `target.mode=refine`，同时提供 `source_annotation_id`、`source_version` 和同值 `If-Match`。

服务端重新检查任务状态、assignment、任务与标注锁、项目 Mask 写闸、媒体尺寸和类别，并原子写入 Prediction、lineage、接受 decision、Annotation 与审计。响应返回完整 Prediction、Annotation、源/结果版本和内容摘要，ETag 对应结果版本。相同 task、幂等键和请求会回放第一次的完整响应；同 key 不同请求、过期 decision 或版本漂移返回 409。客户端只有收到成功响应后才能清理候选。

## 视频轨迹转独立框

视频轨迹可以转换为一个或多个独立 `video_bbox`：

```http
POST /api/v1/tasks/:id/annotations/:annotation_id/video/convert-to-bboxes
{
  "operation": "copy",
  "scope": "track",
  "frame_mode": "all_frames"
}
```

参数：

| 字段          | 取值                       | 说明                                                |
| ------------- | -------------------------- | --------------------------------------------------- |
| `operation`   | `copy` / `split`           | `copy` 保留源轨迹；`split` 移除源关键帧或整条源轨迹 |
| `scope`       | `frame` / `track`          | 转换当前帧或整条轨迹                                |
| `frame_index` | number                     | `scope=frame` 时必填                                |
| `frame_mode`  | `keyframes` / `all_frames` | `scope=track` 时生效                                |

响应包含 `source_annotation`、`created_annotations[]`、`deleted_source` 与 `removed_frame_indexes`。`copy` 不会移除源帧，所以 `removed_frame_indexes` 为空；`split` 才会返回被移除的帧号。

视频标注支持 track composition：

```http
POST /api/v1/tasks/:id/annotations/video/track-compositions
```

```json
{
  "operation": "aggregate_bboxes",
  "annotation_ids": ["bbox-a", "bbox-b"],
  "delete_sources": true
}
```

| `operation`        | 说明                                                                                                      |
| ------------------ | --------------------------------------------------------------------------------------------------------- |
| `aggregate_bboxes` | 将同任务、同类、无重复帧的 `video_bbox[]` 聚合为一条 `video_track_bbox`                                   |
| `split_track`      | 在 `frame_index` 可见帧之后，把一条 track 拆成前后两条                                                    |
| `merge_tracks`     | 合并两条同类、可见帧区间不重叠的 track，并自动补中间 `outside` gap                                        |
| `join_tracks`      | 跳连两条同类、可见帧区间不重叠的 track；`gap_mode=interpolate` 靠插值过渡 / `outside` 把 gap 标消失后合并 |

响应包含 `updated_annotations[]`、`created_annotations[]` 和 `deleted_annotation_ids[]`，客户端应按这三组结果更新 annotation 列表。

## 3D 轨迹拆分与合并

点云 Scene 中的 `box_3d` 轨迹采用“候选 → 预览 → 原子执行”流程修正身份。操作只更新
`track_id` 和 annotation 版本，不改变 cuboid geometry、类别或属性。

```http
GET  /api/v1/tasks/:id/track-operations/candidates?track_id=trk_primary&limit=20
POST /api/v1/tasks/:id/track-operations/preview
POST /api/v1/tasks/:id/track-operations
```

拆分请求以 URL 中任务对应的当前帧为边界；当前帧及之前保留原轨迹，之后的成员获得新轨迹 ID：

```json
{
  "operation": "split",
  "primary_track_id": "trk_primary",
  "split_after_frame": 12
}
```

合并请求把 `primary_track_id` 固定为 survivor，只接受同类别且活跃帧完全不重叠的第二条轨迹：

```json
{
  "operation": "merge",
  "primary_track_id": "trk_survivor",
  "secondary_track_id": "trk_candidate"
}
```

预览响应包含两条轨迹摘要、受影响成员数、实际改写成员数和 `snapshot_token`。执行时提交相同操作字段并附上该
令牌；服务端锁定成员后重算快照，成员身份、版本或集合已变化时返回 `409 track_snapshot_stale`，不会留下部分写入。
成员 task 必须全部可见、可编辑且未锁定；类别漂移、同帧重复、跨 Scene 成员或合并帧冲突同样会拒绝整次操作。
单次最多处理 5000 个活跃成员，超限返回 `422 track_member_limit_exceeded`。

## 候选预测（AI 紫框）

```http
GET /api/v1/tasks/:id/predictions
```

返回**经过 `to_internal_shape` adapter 处理**的内部 schema（不是 LabelStudio 原 raw）。详见 [Schema 适配器](../../dev/troubleshooting/schema-adapter-pitfalls)。

## 采纳预测

```http
POST /api/v1/tasks/:id/annotations/accept
{ "prediction_id": 42, "shape_index": 0 }
```

后端会：

1. 把 shape 写入 `annotations`（source=ai-accepted）
2. 反查类别配置把 alias 映射回原类别名
3. 写审计 `annotation.prediction_accepted`

## 驳回预测

```http
POST /api/v1/tasks/:id/predictions/reject
{ "prediction_id": 42, "shape_index": 0 }
```

驳回后该 shape 不再出现在工作台候选里（按 prediction+shape_index 双键过滤）。

## 历史与版本

```http
GET /api/v1/tasks/:id/history          # annotation_history 全部 revision
GET /api/v1/tasks/:id/comments         # 标注评论
```

## 任务锁

| 端点                     | 作用     |
| ------------------------ | -------- |
| `POST /tasks/:id/lock`   | 显式续锁 |
| `DELETE /tasks/:id/lock` | 主动释放 |

锁过期后由后台清理任务自动归还。详见 [ADR 0005](../../dev/adr/archive/0005-task-lock-and-review-matrix)。

## 相关

- [审核](./predictions)
- [WebSocket 协作](../../dev/reference/ws-protocol)
