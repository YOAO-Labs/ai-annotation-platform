---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-24
---

# Predictions / Async Jobs

候选预测和后台任务分离：

| 表            | 用途                                                         | 端点前缀                 |
| ------------- | ------------------------------------------------------------ | ------------------------ |
| `predictions` | 当前可采纳的候选框（按 task）                                | `/tasks/:id/predictions` |
| `async_jobs`  | 批量预标、失败重试、导出、视频追踪、连接器导入等后台任务历史 | `/async-jobs`            |

<!-- history: batch prediction history used to live in prediction_jobs; current SoT is async_jobs(kind=batch_predict). -->

## 触发预标

```http
POST /api/v1/projects/:id/preannotate
{
  "batch_id": "uuid",
  "prompt": "person . car . bicycle",
  "output_mode": "both",        // box / mask / both
  "ml_backend_id": "uuid",
  "params": { "box_threshold": 0.35 }
}
```

返回 `200` + `job_id`。任务进 Celery，状态机见 [预标注流水线](../../dev/concepts/prediction-pipeline)。

## 托管图片的固定输入

图片项目的单阶段和多阶段请求均在排队前固定执行配置、过滤后的 Task 清单及原件来源。每个请求最多实际选择 1000 题，私有来源证明最多 1MiB；显式 `task_ids` 仍沿用接口的 200 项上限。`skip_predicted` 先过滤，过滤后为空不会扩大为整个项目。相同幂等键重发 pending 作业复用首次范围、配置和来源。

托管来源包括独立 TaskOriginal、直接 DatasetItem 和准确 primary 关联。Worker 沿登记的保存位置读取并完整核验 SHA／长度，单图最多 64MiB、3200万像素；整图、框提示和所有 ROI 使用同一份核验快照。每次尝试生成独立临时副本路径，上传后完整回读再投递，避免仅重新签名同一 URL 时复用旧 embedding。托管请求保留现有入口权限，不额外增加引用图片所需的 `tasks:read`。

保存前重核原发起凭据、项目／任务范围、配置和原件绑定；通过后才在同一事务中覆盖旧候选并写入整题结果。`extra.original_input` 是服务端生成的来源记录，客户端和模型不能通过同名字段授予写入资格。私有请求授权不出现在 AsyncJob 响应中。

单条失败和整 Job 失败重试均重新核验本次发起者，但沿用原失败的配置和图片来源，允许按当前合法 Task 版本重新授权。缺少可信原件或配置证明的历史图片失败，以及缺证明的旧 pending 作业，返回 `409 prediction_original_requires_new_request`，需要新建预标请求；不会在重试时按当前桶补造原输入。托管同步交互、predict-test、secondary、mask repair 和整段视频追踪不属于此后台合同。

## 托管视频的固定输入

视频项目的整段执行（`execution_unit=video` 或原有整段默认值）在排队前固定 Task 范围、来源、执行配置与模型声明，保留 bulk 管理权限和 workbench 当前题编辑资格。支持独立 TaskOriginal、直接 DatasetItem 和准确 primary 关联；单次最多实际选择 1000 题，私有来源证明最多 1MiB，显式 `task_ids` 保持 200 项上限。

Worker 完整核验登记原件的 SHA／长度，并对同一文件实际探测尺寸、fps、时长与帧数。单视频最多 5GiB、一百万帧，探测最多五分钟；每次尝试只有一个独立临时视频，上传后完整回读，模型 GET 固定非 null VersionId。导入桶须启用版本控制。聚合框轨迹、逐帧框／多边形与受支持的原生 Mask 输出均按真实视频、实际请求窗口和累计预算核验，再统一保存；不能把旧 Task 尺寸当成真实视频尺寸。

每次实际模型派发前及最终保存时重新核对原凭据、任务关系、维护状态、原件和实际模型配置。单条失败与整 Job 重试采用本次合法凭据和新 attempt，同时保留首次失败的原件、模型和 pipeline；原 Job 已清理也不能改读今天的同名对象。缺少可信证据的历史整段失败／待执行作业返回 `409 prediction_video_requires_new_request`，需发起新请求。

普通 Prediction 的单条和整组采纳按当前调用者重新核验准确候选、当前原件及掩码内容，不要求生成时的 JWT／Key 仍有效。来源变化、候选被替换、内容不符或当前权限失效时拒绝写入；驳回不要求原视频在线。`extra.original_video_input` 仅为安全来源摘要，完整来源和候选证明由服务端保存，不接受客户端或模型自行补写。

视频轨迹 Mask 候选的 `GET /tasks/{task_id}/predictions/{prediction_id}/mask-content/{shape_index}/{frame_index}` 和已采纳标注的 `GET /annotations/{annotation_id}/mask-content/{frame_index}` 使用当前 `annotations:read` 权限，支持同样三类原件。服务端重新核验原件、真实尺寸、准确帧的 RLE 及其存储归属，内容读取与关闭先于最终权限复核；不要求采纳写权限或生成凭据仍有效。已采纳标注保留服务端来源血缘，父候选清理后仍可核验读取；客户端填写 `source`、父候选 ID 或属性不能补造该资格。

逐帧图片执行、专用交互 Tracker／纠错／修复子任务以及引用视频沿各自既有边界处理；本合同不扩张它们的准入或改变模型 `file_path` 协议。同步视频预测测试见[预测测试说明](./ml-backend.md#托管视频的预测测试)。

## 原地引用图片

同一预标端点支持普通图片项目的 `bulk` 与 `workbench` 后台执行，沿用各自管理权限或当前题编辑资格。API Key 还需 `tasks:read`，执行时继续复核原 Key 的身份、scope、有效期及撤销状态。

当选择范围包含引用时，服务端固定实际任务 UUID 列表（包括跳过已预标后为空的情况）；实际执行引用的作业还固定 Task 版本、Item/Asset/SHA、大小、尺寸、来源修订及模型快照。相同幂等键重发 pending 作业复用首次范围和证据，不自动扩展到新增任务。

实际从来源读取时，worker 节点必须具有该位置的读取锚点；统一 reader 的完整原图缓存边界见[严格内容读取](verified-content.md#完整原图缓存的接入状态)。完整核验的原图通过作业独占临时输入交给现有 HTTP 后端，根阶段、geometry 和 ROI 共用一次捕获的内容；最终写入前复查当前权限和绑定。候选元信息 `extra.reference_input` 仅保存内容与执行溯源，私有授权不在 AsyncJob 响应中返回，也不接受客户端提供写入凭证。

后台预标这里只支持图片执行单位。已发布且部署就绪的受控实例分割整图源阶段支持引用图片的原生 Mask，部署的读取/创建开关、项目原生编辑及 region 工具必须可用。最终写入使用独立逐结果资格：整组结果在首次对象写入前核验原图尺寸、精确 RLE 对象集合、模型/运行时、项目类别和阶段来源，两个重试入口复用相同边界。普通引用凭证、外部导入或客户端元信息不能授予此资格。frame 与下游原生掩码仍不支持；同步交互及 predict-test 使用[独立资格合同](./ml-backend#原地引用图片的同步预测)。来源不可读或资格变化时不会替换旧候选。

两个失败重试端点共用准备与设备队列解析；引用重试在 API 预建独立 `prediction_retry`，固定当前 actor/Key 与 Task 版本，同时保留失败项的原模型快照及 Item/Asset/SHA/大小/尺寸。重复投递复用该 attempt；成功时新候选、旧 partial 替代、失败项删除和作业完成同事务提交，失败则保留旧候选并释放本 attempt 占用。历史引用失败缺少原模型或内容绑定时返回 `409`，需重新预标。

采纳指定 `shape_index` 时，已驳回对象返回 `409`；不指定下标时只采纳未驳回的 shape，全部已驳回则返回 `409`。保留原数组下标，不因过滤重新编号。已被重试结果替代的引用后台原生候选不能继续采纳。

`POST /tasks/{task_id}/predictions/{prediction_id}/accept` 始终核对候选所属任务与 URL 一致，不一致返回 `404`。引用候选采纳要求 `tasks:read` 和 `annotations:write`，按当前可编辑权限严格核验原图，并与候选内容溯源匹配；不要求当前 Task 版本等于历史推理版本，因此支持预标后正常分派/激活。后台原生 Mask 额外核验服务端保存的候选、模型/运行时与阶段记录，最终读取 RLE 并复查当前编辑开关。历史作业清理或模型离线不撤销已生成结果的证明；缺失证明的旧原生候选需重新预标。内容证明、候选或权限变化时拒绝写入；忙冲突返回可重试 `409`。内部凭证不作为请求参数。

引用图片与托管图片的 `mask_repair` SAM 候选经[批量修复独立资格](./tasks-and-annotations.md#mask-质检批量修复)采纳：必须匹配已提交单元、生成时结果/元信息摘要、源标注及当前原图，其凭证不能代替后台分割资格。重复或已驳回候选返回 `409`。`POST /tasks/{task_id}/predictions/{prediction_id}/reject` 按当前任务编辑和 Key 权限写入驳回状态；来源离线或 native mask 开关关闭不阻止驳回。

## 图片原生候选内容

```http
GET /api/v1/tasks/{task_id}/predictions/{prediction_id}/mask-content/{shape_index}
```

返回 `CocoRleContent`（`encoding: "coco_rle"`、`size`、`counts`），支持普通 JSON 与 gzip 存储引用。下标使用查询预测响应的原始 `shape_index`，不按过滤、排序或分页后的数组位置重新编号；每个 shape 还返回自身 `tool_unit_id`，混合阶段不能只使用 Prediction 的根工具单位。

要求当前 Task 可见、API Key 具备 `annotations:read`，且 Prediction 同时属于该 Task 和项目。不存在、已驳回、已被替代或交互采纳审计候选返回 `404`，非图片原生 Mask shape 返回 `422`。全局读取开关关闭返回 `404 raster_mask_read_disabled`；AI、创建与项目原生编辑开关不限制存量内容读取。

响应含 `ETag` 和 `Cache-Control: private, no-cache`；支持弱、多值及通配 `If-None-Match`。每次返回 `304` 前仍复核权限、状态、读取开关、Task/Item 图片类型与尺寸。内容损坏或尺寸失配返回结构化 `409`，存储不可用返回可重试 `503 mask_storage_unavailable`。

普通已采纳候选仍可读取生成时的原始像素，之后修改 Annotation 不改变该内容。读取原地引用图片已保存的 RLE 不访问原图，来源离线时仍可读；生成和采纳继续采用各自的严格原图资格。客户端应保留 `(predictionId, shapeIndex)` 身份，切题、过滤或审阅后清理对应资源。

## 查询 jobs

```http
GET /api/v1/async-jobs?kind=batch_predict&kind=prediction_retry&status=running&search=
GET /api/v1/async-jobs/{job_id}
POST /api/v1/async-jobs/{job_id}/cancel
POST /api/v1/async-jobs/{job_id}/retry-failed
```

`kind` 支持重复 query 参数过滤多种任务。批量预标取消是协作式取消：pending job 会直接置 cancelled；running job 先提交 `payload.cancel_requested=true`，再尽力 revoke Celery task。图片 worker 在结果提交前与取消共锁，取消先提交则丢弃该题在途结果，已提交题目保留；具体统计与视频逐帧边界见[异步任务取消](./async-jobs)。图片 `predict_mode="overwrite"` 在推理成功和最终权限检查后逐题替换旧结果，任一结果写入异常整题回滚，不留下半题候选或清空旧内容。

## 查询当前快照

```http
GET /api/v1/admin/preannotate-queue?project_id=&status=
```

只看当前 `pre_annotated` 批次（与 jobs 历史区分）。批量清理预标队列仍走 `/admin/preannotate-queue/bulk-clear`。

支持 `limit`（接口默认 50，1–200；界面默认 20）和 `offset`（默认 0）；按更新时间、批次 ID 倒序稳定分页。`project_id` 与 `status` 在分页前应用，不传时保留原范围。响应继续为批次数组；客户端可请求下一页判断是否结束，不能把单页长度当作全部记录数。

## 查询任务预测

```http
GET /api/v1/tasks/:task_id/predictions?model_version=&min_confidence=&limit=&offset=
```

返回 `PredictionOut[]`。每条 prediction 的 `source` 表示候选来源：

| source            | 含义                           |
| ----------------- | ------------------------------ |
| `ml_backend`      | 平台内 ML backend 生成的预标   |
| `external_import` | 通过预测导入端点写入的外部预测 |

Workbench 会用该字段显示来源标识，并支持按来源隐藏 / 恢复候选框。

## 按来源清理预测

```http
POST /api/v1/projects/:project_id/predictions/purge
{
  "source_scope": "external_import",
  "task_ids": null,
  "dry_run": true
}
```

权限：项目 owner 或 super_admin。`source_scope` 可为：

| source_scope      | 含义                                                     |
| ----------------- | -------------------------------------------------------- |
| `external_import` | 只清外部导入预测，适合重导前预览或手动回滚导入           |
| `ml_backend`      | 只清平台 ML Backend 生成的预标，清理后需重新运行模型恢复 |
| `all`             | 清理当前项目范围内全部 prediction                        |

`task_ids=null` 表示项目级清理；传 UUID 数组时仅清这些 task。建议先传 `dry_run=true` 读取计数，再用同一参数正式执行：

```json
{
  "source_scope": "external_import",
  "task_ids": null,
  "dry_run": true,
  "counts": {
    "ml_backend": 0,
    "external_import": 12,
    "unknown": 0,
    "total": 12
  }
}
```

正式清理会先删 `prediction_metas`，再删 `predictions`，并写 `predictions.purge` 审计。当前 `annotations.parent_prediction_id` 没有外键级联，已采纳的人工标注不会被删除。

## 重置批次

```http
POST /api/v1/admin/batches/:id/reset
```

清掉该批次所有 `predictions` / `failed_predictions` / 对应 `batch_predict` async job 索引，并释放相关锁。重置后可重新跑预标。

## 接受 / 驳回

详见 [任务与标注](./tasks-and-annotations#采纳预测)。

## WebSocket 进度

| 通道                       | 何时订阅                      |
| -------------------------- | ----------------------------- |
| `project:{id}:preannotate` | 工作台 / `/ai-pre` 该项目页   |
| `global:prediction-jobs`   | 任何 admin（Topbar 进度徽章） |

消息体：

```json
{ "job_id": "...", "type": "progress", "i": 3, "n": 10, "ts": ... }
{ "job_id": "...", "type": "error", "message": "..." }
{ "job_id": "...", "type": "done", "succeeded": 9, "failed": 1 }
```

## Schema 边界

- DB 写入：LabelStudio 标准（保持导出兼容）
- API 读出：经 `to_internal_shape` 转内部 schema
- 前端只接受内部 schema

详见 [API Schema 边界](../../dev/concepts/api-schema-boundary) 与 [Schema 适配器陷阱](../../dev/troubleshooting/schema-adapter-pitfalls)。
