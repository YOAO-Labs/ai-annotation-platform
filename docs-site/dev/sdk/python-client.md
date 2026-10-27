---
title: Python SDK 参考
audience: [dev]
type: reference
status: beta
last_reviewed: 2026-10-24
---

# Python SDK 参考

包名 `ai-annotation-sdk`,导入名 `ai_annotation`。同步 Client,底层 httpx + pydantic v2。

项目分页／维护、批次分派／切分／送审、数据集回填、绩效、搜索、指引图片、失败预测与标注导入见 [项目、批次与数据运维](./project-workflows)。已有资源继续沿用 `projects`、`batches`、`datasets` 等入口，另有 `project_performance`、`search`、`guide_assets` 和 `failed_predictions`。

账户、身份、用户管理和运维入口包括 `auth`、`account`、`avatars`、`api_keys`、`users`、`groups`、`invitations`、`health`、`ml_capabilities`、`system_settings`、`audit_logs`、`storage_connections`、`admin_operations`、`ml_diagnostics` 与 `project_ml`。这些命名空间的方法、公开类型与权限／恢复规则见 [账户、管理与运维](./account-and-operations)，精确参数和 schema 可用 `describe_operation("namespace.method")` 离线读取。

> **兼容承诺范围**:公开 API 仅限本页所列(`Client` + 资源命名空间方法 + 顶层导出的模型、异常与能力发现函数)。内部传输层 `ai_annotation._http` 不在兼容承诺内,外部代码不要直接依赖。

`comments`、`annotation_history`、`bug_reports`、`mask_formats`、`mask_qc`、`point_cloud_quality` 的类型请求、分页、签名附件、回执／摘要及失败恢复见 [讨论、缺陷报告与质量处理](./annotation-quality)。

任务级读取、媒体、租约、点云与几何修改见 [任务工作流](./task-workflows)；六条 WebSocket 的离线发现、typed 事件、鉴权与有界 NDJSON 监听见 [公开事件流](./streams)。REST 已覆盖全部 558 项采用接口，十项执行器内部协议保持排除。

## 版本与 AAP target

SDK、CLI 与 TUI 共用一个独立于 AAP 的 SemVer。运行时同时暴露 SDK 版本和该 release 完成测试与
OpenAPI 对账的 AAP target：

```python
from ai_annotation import __aap_target_version__, __version__

print(__version__)
print(__aap_target_version__)
```

AAP target 是验证基线，不表示对所有更早或更晚平台版本的兼容范围。安装元数据版本与
`__version__` 来自同一个静态来源。

## Client 构造

```python
from ai_annotation import Client

client = Client(
    base_url="http://localhost:8000",   # 平台地址(不含 /api/v1 前缀)
    api_key="ak_...",                    # ak_ API key 或 JWT, SDK 不区分
    timeout=30.0,                        # httpx 超时(秒)
)
```

`base_url` / `api_key` 缺省时按以下优先级解析(任一项独立回落):

1. 显式构造参数
2. 环境变量 `AAP_BASE_URL` / `AAP_API_KEY`
3. 配置文件 `~/.config/ai-annotation/config.toml`(由 `aap login` 写入)

三处都拿不到 `base_url` 时构造抛 `AAPError`。`api_key` 允许为空(此时请求不带 Authorization 头,通常会得到 401)。

`Client` 支持上下文管理器,也可手动 `client.close()`:

```python
with Client() as client:
    ...
```

所有接受 ID 的参数类型为 `IdLike = str | UUID`,传字符串或 `uuid.UUID` 均可。

### 传输层行为

- 认证:`Authorization: Bearer <api_key>`。
- 幂等重试:普通 API 的 **GET** 读取在 429 / 502 / 503 / 504 时指数退避，最多请求 3 次；POST / PATCH / DELETE 与流式下载不自动重试。
- 预签名 URL(上传 PUT、绝对地址下载)走无 auth 的裸 client。

## 资源命名空间

离线发现可用 `list_capabilities(tag=None, status=None, limit=20, offset=0)` 与 `describe_operation(name)`，它们不创建 Client，也不需要凭据。描述接受规范 SDK 方法名和可解析的 CLI 点分路径，给出关联 API、参数、请求模型及操作约束。多步骤 CLI 不能被误当成单个 SDK 方法；完整用法见 [Agent 接入与操作](./agents)。

### client.projects

| 方法     | 签名                                                                                                                              | 返回            |
| -------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `list`   | `list(status: str \| None = None, search: str \| None = None)`                                                                    | `list[Project]` |
| `create` | `create(name, type_key=None, data_type=None, *, template_id=None, source_project_id=None, copy_annotation_guide=False, **kwargs)` | `Project`       |
| `get`    | `get(project_id)`                                                                                                                 | `Project`       |
| `update` | `update(project_id, **fields)`                                                                                                    | `Project`       |
| `delete` | `delete(project_id)`                                                                                                              | `None`          |
| `stats`  | `stats()`                                                                                                                         | `ProjectStats`  |
| `access` | `access(project_id)`                                                                                                              | `ProjectAccess` |

`create` 额外提供 keyword-only `template_id`、`source_project_id` 和 `copy_annotation_guide`。模板与源项目互斥；未明确给出的类型与 type_label 由服务器继承，明确覆盖优先。普通创建仍按 `type_key` → `data_type` → `name` 填 type_label，保持既有行为。模板不支持 copy_annotation_guide；配置继承由服务器处理，SDK 不复制合并逻辑。

正文可通过 `update(project_id, annotation_guide=...)` 保存。显式提交 `guide_assets` 时，还须提交最近读取的 `guide_assets_revision`，仅能原样删减或重排当前清单；不能增加图片key或修改元数据。缺失或过时修订返回409，调用方应重新读取并核对，不能自动重试覆盖。正文单独保存不需要图片修订，详见[指南图片API合同](../../api/guides/projects.md#标注指引图片)。指南图片的认证上传、内容读取、删除及旧传输入口在覆盖清单中归为Web专用，SDK不提供图片传输方法。

`stats()` 返回可见项目聚合(`total_data` / `completed` / `ai_rate` / `pending_review`)+ 最近 12 周时间序列(`*_series`),任意已认证用户可达。

`access()` 返回当前账号在该项目上的解析后权限(`ProjectAccess`):`platform_role`(平台角色)、`project_role`(项目角色,管理者可为 `None`)、`access_kind`(`super_admin` / `owner` / `member`)、`membership_id` / `membership_version`、`is_manager` 与 `capabilities`。`Project.my_project_role` 是列表 / 详情里当前账号的项目角色。授权来自项目成员关系而非账号全局角色,详见[可见性与权限](../concepts/visibility-and-permissions)。

### client.project_templates、videos、task_videos、video_tracker、video_review、storage

这些命名空间提供项目模板 CRUD／复制、素材与任务视频媒体、章节、显式分段租约、追踪创建／恢复／候选决策、Mask 内容／关键帧、质量审阅、轨迹编辑和存储诊断／资产重试。复杂写入接收公开的 typed request models；通过 `describe_operation()` 查看完整具名参数和请求 schema。

视频 frame/chunk 返回 JSON，202 尚未就绪；GET 可能触发缓存生成。追踪和质量 run 的状态独立于通用 AsyncJob，接纳保留原 revision/digest/source versions。Mask 写入使用 If-Match，轨迹 split/merge 可能停用源标注。具体 ID、下载、权限与恢复边界见 [视频、模板与存储运维](./video-and-templates)。

### client.datasets

另有 `client.files.upload_init/upload_content/upload_complete/file_url/backfill_thumbnails` 与 `client.scenes.list/get/trajectory/update`。前者处理项目任务文件，后者处理数据集场景；它们与数据集素材上传身份不同。读取场景要求 API key 的 datasets:read，写入遵循管理员策略。完整请求和恢复边界见 [文件上传与数据集场景](./files-and-scenes)。

| 方法                | 签名                                                                                                                       | 返回                      |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| `list`              | `list(search: str \| None = None, data_type: str \| None = None, limit: int = 50, offset: int = 0, state: str = "active")` | `Page[Dataset]`           |
| `create`            | `create(name: str, data_type: str = "image", **kwargs)`                                                                    | `Dataset`                 |
| `get`               | `get(dataset_id)`                                                                                                          | `Dataset`                 |
| `update`            | `update(dataset_id, **fields)`                                                                                             | `Dataset`                 |
| `delete`            | `delete(dataset_id)`                                                                                                       | `None`                    |
| `list_items`        | `list_items(dataset_id, limit: int = 50, offset: int = 0)`                                                                 | `Page[DatasetItem]`       |
| `delete_item`       | `delete_item(dataset_id, item_id)`                                                                                         | `None`                    |
| `list_projects`     | `list_projects(dataset_id)`                                                                                                | `list[Project]`           |
| `upload_files`      | `upload_files(dataset_id, paths: Sequence[str \| Path], on_progress: Callable[[int, int, str], None] \| None = None)`      | `list[UploadedItem]`      |
| `upload_zip`        | `upload_zip(dataset_id, zip_path: str \| Path)`                                                                            | `ZipUploadResult`         |
| `link_project`      | `link_project(dataset_id, project_id)`                                                                                     | `LinkResult`              |
| `preview_unlink`    | `preview_unlink(dataset_id, project_id)`                                                                                   | `DatasetUnlinkPreview`    |
| `unlink_project`    | `unlink_project(dataset_id, project_id, *, idempotency_key: UUID \| str \| None = None)`                                   | `DatasetUnlinkResult`     |
| `preview_lifecycle` | `preview_lifecycle(dataset_id, operation, *, expected_revision: int)`                                                      | `DatasetLifecyclePreview` |
| `set_lifecycle`     | `set_lifecycle(dataset_id, operation, *, expected_revision: int, impact_token: str)`                                       | `DatasetLifecycleResult`  |

- `upload_files`:逐文件三步流(upload-init → PUT 预签名 URL → upload-complete)。`on_progress(done, total, file_name)` 在每个文件完成后回调。
- `upload_zip`:multipart 上传单个 ZIP 包,后端解压入库(≤200MB / ≤5000 文件)。
- `link_project`:返回的 `LinkResult.status == "linking"` 时建任务走异步,用 `async_job_id` 配合 `client.jobs.wait` 等待。
- `update`: `**fields` 保留“未提供”与“显式传 `None`”的区别，因此可以用 `axis_convention=None` 清除坐标系约定。
- `preview_unlink` 返回将删除的 task / annotation / batch 计数；`unlink_project` 返回实际删除计数。
- `list` 默认只返回活动数据集；`state` 可为 `active`、`archived` 或 `all`。`Dataset` 包含 `archived_at`、`archived_by` 与 `lifecycle_revision`。
- 归档与恢复的 `operation` 分别为 `archive` 与 `restore`。先用当前 `lifecycle_revision` 取得影响预览，核对计数与有效期，再显式把预览返回的修订和 `impact_token` 交给 `set_lifecycle`。凭证有效期为五分钟；过期或发生 409 冲突时须重新核对，SDK 不自动刷新凭证或重试变更。冻结版本计数仅覆盖已记录的数据集成员关系。
- 归档保留现有任务、标注和冻结内容，停止数据集管理写入与新训练冻结；恢复只解除归档，不重放已撤销权限、来源委托或旧后台任务。已归档数据集须先恢复，才能调用原有删除操作。
- `unlink_project` 默认每次调用生成 UUID `Idempotency-Key`。如果响应丢失后要在另一次调用中安全重放，请保存调用方生成的 UUID 并通过 `idempotency_key` 原样复用。

### client.tasks

| 方法               | 签名                                                                                                         | 返回               |
| ------------------ | ------------------------------------------------------------------------------------------------------------ | ------------------ |
| `list`             | `list(project_id, status: str \| None = None, limit: int = 50, offset: int = 0, cursor: str \| None = None)` | `TaskPage`         |
| `get`              | `get(task_id)`                                                                                               | `Task`             |
| `next`             | `next(project_id, batch_id=None)`                                                                            | `Task \| None`     |
| `submit`           | `submit(task_id)`                                                                                            | `TaskActionResult` |
| `skip`             | `skip(task_id, reason, note=None)`                                                                           | `TaskActionResult` |
| `withdraw`         | `withdraw(task_id)`                                                                                          | `TaskActionResult` |
| `reopen`           | `reopen(task_id)`                                                                                            | `TaskActionResult` |
| `accept_rejection` | `accept_rejection(task_id)`                                                                                  | `TaskActionResult` |
| `claim_review`     | `claim_review(task_id)`                                                                                      | `ReviewClaim`      |
| `approve_review`   | `approve_review(task_id, **fields)`                                                                          | `TaskActionResult` |
| `reject_review`    | `reject_review(task_id, reason_type, reason)`                                                                | `TaskActionResult` |

- `list`:cursor 翻页时响应 `total` 为 `None`(复用首页值),下一页游标在 `TaskPage.next_cursor`。
- `next`:领取下一个可标注 task;**无可领任务时返回 `None`**(不抛异常)。

### client.annotations

| 方法          | 签名                                                                                              | 返回                         |
| ------------- | ------------------------------------------------------------------------------------------------- | ---------------------------- |
| `list`        | `list(task_id)`                                                                                   | `list[Annotation]`           |
| `create`      | `create(task_id, annotation_type: str, geometry: dict, class_name: str \| None = None, **kwargs)` | `Annotation`                 |
| `update`      | `update(task_id, annotation_id, **fields)`                                                        | `Annotation`                 |
| `delete`      | `delete(task_id, annotation_id)`                                                                  | `None`                       |
| `bulk_update` | `bulk_update(annotation_ids, **patch)`                                                            | `AnnotationBulkUpdateResult` |

`bulk_update` 只接受 `class_name` / `attributes` / `z_order` / `is_locked` / `is_hidden`；不包装 geometry 批量修改。

### client.feedbacks

| 方法     | 调用                                                                                   | 返回                 |
| -------- | -------------------------------------------------------------------------------------- | -------------------- |
| `create` | `create(project_id, body, *, kind="issue", anchor_type="task", task_id=None, ...)`     | `AnnotationFeedback` |
| `list`   | `list(project_id, *, cursor=None, limit=50, root_only=None, include_counts=None, ...)` | `FeedbackPage`       |
| `thread` | `thread(root_feedback_id, *, cursor=None, limit=50)`                                   | `FeedbackThreadPage` |
| `reply`  | `reply(feedback_id, body: AnnotationFeedbackReply)`                                    | `AnnotationFeedback` |
| `patch`  | `patch(feedback_id, body: AnnotationFeedbackPatch)`                                    | `AnnotationFeedback` |
| `delete` | `delete(feedback_id)`                                                                  | `None`               |

完整具名参数可通过 `describe_operation("feedbacks.create")` 等离线描述读取。线程返回单独的 `root`、活跃后代 `items`、根绑定游标与后代总数；游标原样用于同一根线程。回复仅提供正文和附件，没有幂等键，不自动重试。补丁保留省略与显式 null，当前服务端把 null 当作不修改；`mentions=[]` 才清空提及。软删除成功返回空 204；根删除与回复删除对线程有不同影响。权限、提及和锚点新鲜度仍由服务端核对，详见 [反馈工作流](./workflows#阅读完整反馈线程并明确处理)。

### client.data_manager 与 client.task_views

| 资源           | 方法                                                | 主要返回                                                              |
| -------------- | --------------------------------------------------- | --------------------------------------------------------------------- |
| `data_manager` | `schema`, `summary`, `matches`                      | schema／完整任务范围聚合／单任务命中页                                |
| `data_manager` | `query_objects`, `object_detail`, `object_location` | 对象页／`{item}`／直接 location                                       |
| `data_manager` | `query_tracks`, `track_detail`                      | 逻辑轨迹页／`{track, members}`                                        |
| `data_manager` | `assignment_preview`, `assignment_apply`            | `DataManagerTaskAssignmentResponse`，含逐项结果与 preview_version     |
| `data_manager` | `export_tasks`                                      | `DataManagerTaskExportSubmission`，含 job_id 与实际状态               |
| `task_views`   | `list`, `create`, `get`, `update`, `delete`, `copy` | 内置／保存视图的 `{items}` 或单个 `ProjectTaskView`；delete 返回 None |
| `task_views`   | `query`, `query_view`                               | `ProjectTaskQueryResponse`，含 items/total/limit/offset               |

所有方法先接收 `project_id`，需要时再接收任务／对象／视图 ID 和请求对象。`schema(project_id, *, entity_scope="tasks")` 获取动态目录；复杂读取使用 `DataManagerSummaryRequest`、`DataManagerMatchesRequest`、`DataManagerEntityQueryRequest` 和 `ProjectTaskQueryRequest`。字段与操作符仍由当前项目 schema 和服务器校验，SDK 不实现过滤器执行器。

视图创建／更新／复制分别使用 `ProjectTaskViewCreate`、`ProjectTaskViewUpdate`、`ProjectTaskViewCopyRequest`；省略或 null 表示不更新，空对象／数组清空配置。内置项 id 为 None，保存视图才使用 UUID 路由。`query_view(..., limit=50, offset=0)` 只查询保存的 task 粒度视图。

分配使用 `DataManagerTaskAssignmentRequest` 和带 preview_version 的 `DataManagerTaskAssignmentApplyRequest`；owner 字段省略保持 override，显式 null 清除 override 并继承批次人员。导出使用 `DataManagerTaskExportRequest`，可通过 `export_tasks(..., idempotency_key=...)` 显式带请求头；不扩大选择、不自动重试或等待。完整参数可离线 `describe_operation` 读取，分页、粒度及边界见 [Data Explorer 与保存视图](./data-explorer)。

### client.notifications

| 方法                | 调用                                                          | 返回内容                                       |
| ------------------- | ------------------------------------------------------------- | ---------------------------------------------- |
| `list`              | `list(unread_only=False, limit=30, offset=0)`                 | `NotificationPage`：`items`、`total`、`unread` |
| `unread_count`      | `unread_count()`                                              | 当前账号未读数                                 |
| `mark_read`         | `mark_read(notification_id)`                                  | `ok` 确认                                      |
| `mark_all_read`     | `mark_all_read()`                                             | `updated` 数量                                 |
| `delete`            | `delete(notification_id)`                                     | `ok` 确认                                      |
| `clear_read`        | `clear_read()`                                                | `deleted` 数量                                 |
| `preferences`       | `preferences()`                                               | `NotificationPreferences`                      |
| `update_preference` | `update_preference(preference: NotificationPreferenceUpdate)` | 成功确认                                       |

列表范围为当前账号，limit 为 1–100。偏好仅更新明确提供的 `in_app`／`toast`，禁止显式 null 或空频道补丁；未知类型由服务器拒绝。标记通知已读不代表完成其目标任务，通知中的目标仍需通过对应资源接口读取并核对权限。

### client.pipelines

| 方法          | 调用                                                                         | 返回                       |
| ------------- | ---------------------------------------------------------------------------- | -------------------------- |
| `list`        | `list(*, scope=None, project_id=None, organization_id=None)`                 | `list[ProjectPipeline]`    |
| `get`         | `get(pipeline_id)`                                                           | `ProjectPipeline`          |
| `create`      | `create(body: ProjectPipelineCreate)`                                        | `ProjectPipeline`          |
| `update`      | `update(pipeline_id, body: ProjectPipelineUpdate)`                           | `ProjectPipeline`          |
| `delete`      | `delete(pipeline_id)`                                                        | `None`                     |
| `apply`       | `apply(project_id, body: ProjectPipelineApplyRequest)`                       | 复制后的 `ProjectPipeline` |
| `preannotate` | `preannotate(project_id, body: PreannotateRequest, *, idempotency_key=None)` | `PreannotationSubmission`  |

库列表当前没有服务端分页。`apply` 是复制／可选切换默认，不执行推理。`preannotate` 返回实际 Job acknowledgement，`total_tasks` 可能未知；通过 `jobs.get/wait` 独立观察完成状态。幂等键是明确的 `Idempotency-Key` 请求头，不由 SDK 自动生成；结果不确定时保留原键和完整请求。结构、backend 启用、执行能力和任务范围分别由现有服务端边界校验，不提供伪造的 validate/dry-run。详见 [编排与预标注工作流](./workflows#保存编排与复制到目标项目)。

### client.predictions

| 方法          | 签名                                                                                                                                                                                         | 返回           |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| `import_file` | `import_file(project_id, file_path, format: str = "aap_json", yolo_variant: str \| None = None, model_version: str \| None = None, dry_run: bool = False, overwrite_existing: bool = False)` | `ImportResult` |

导入外部预测结果,`format` 支持 `aap_json` / `coco` / `yolo`(yolo 时可配 `yolo_variant`)。文件格式细节见[预测导入与导出](/user-guide/datasets/prediction-import-export)。

> **注意**:后端 `overwrite_existing` 缺省为 `True`,SDK 显式发送且缺省 `False`(更保守)。要覆盖已有预测必须显式传 `overwrite_existing=True`。

### client.jobs

| 方法           | 签名                                                                                                                                              | 返回             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `list`         | `list(status: str \| Sequence[str] \| None = None, kind: str \| Sequence[str] \| None = None, project_id=None, limit: int = 50, offset: int = 0)` | `JobPage`        |
| `get`          | `get(job_id)`                                                                                                                                     | `Job`            |
| `cancel`       | `cancel(job_id)`                                                                                                                                  | `None`           |
| `retry_failed` | `retry_failed(job_id)`                                                                                                                            | `JobRetryResult` |
| `wait`         | `wait(job_id, timeout: float = 600.0, poll_interval: float = 2.0, on_progress: Callable[[Job], None] \| None = None)`                             | `Job`            |

`cancel` 请求**软取消**:协作式——后端写取消标记,worker 在下一条任务边界落 `cancelled` 终态,返回不代表已终止。仅可取消的 kind 且 `status ∈ {pending, running}` 时有效,否则抛 `APIStatusError`(400/409)。

**`wait` 轮询语义**:每 `poll_interval` 秒 GET 一次 job,每轮先调用 `on_progress(job)`(若提供),然后判定:

- `status == "completed"` → 返回该 `Job`;
- `status in {"failed", "cancelled"}` → 抛 `JobFailedError`(异常的 `.job` 属性带完整 Job);
- 超过 `timeout` 秒仍未到终态 → 抛 `JobTimeoutError`。

### client.exports

| 方法           | 签名                                                                                                                   | 返回          |
| -------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------- |
| `create`       | `create(project_id, targets: list[str], include_attributes=None, *, body: ExportRequestBody \| None = None, **kwargs)` | `str`(job_id) |
| `wait`         | 同 `jobs.wait`(纯转发)                                                                                                 | `Job`         |
| `download`     | `download(job_or_id: Job \| IdLike, dest_path: str \| Path)`                                                           | `Path`        |
| `download_voc` | `download_voc(project_id, dest_path, include_attributes=True)`                                                         | `Path`        |

- `create`:发起异步导出(HTTP 202),返回 job_id；格式、属性与坐标等选项走 query，视频 `scope` 与点云 `lidar` 选项通过 `ExportRequestBody` 的 JSON body 发送。将它们误放入 query kwargs 会在发送前拒绝。导出格式清单见[导出格式参考](/user-guide/reference/export-formats)。
- `download`:从 `job.result["download_url"]` 流式下载导出包;传 job_id 时会先 GET 一次 job。job 无 `result.download_url`(如尚未完成)时抛 `AAPError`。
- `download_voc`：服务器同步返回 ZIP，使用认证 POST 流式落盘并原子替换目标，不自动重试。异步 `create` 不能用于 VOC，混选 VOC 与其他目标同样拒绝。`client.batches.export(..., body=...)` 与 `client.batches.download_voc(project_id, batch_id, dest_path, include_attributes=True)` 提供对应批次入口。

导出请求模型包括 `VideoExportFrameSelection`、`VideoExportSegmentSelection`、`VideoExportScopeRequest`、`LidarExportOptions` 和 `ExportRequestBody`。源帧范围为闭区间；片段和任务归属、相机资格与导出能力由服务端核对。完整示例见 [范围导出与 VOC](./cookbook#_9-范围导出与同步-voc)。

### client.ml_backends

项目作用域的 registry backend / service pool 启用与健康监控。

| 方法                   | 签名                                                | 返回                       |
| ---------------------- | --------------------------------------------------- | -------------------------- |
| `list`                 | `list(project_id)`                                  | `list[MLBackend]`          |
| `get`                  | `get(project_id, backend_id)`                       | `MLBackend`                |
| `list_available`       | `list_available(project_id)`                        | `list[ProjectMLBackend]`   |
| `set_enablement`       | `set_enablement(project_id, backend_id, enabled)`   | `ProjectMLBackend`         |
| `check_health`         | `check_health(project_id, backend_id)`              | `MLBackendHealth`          |
| `list_available_pools` | `list_available_pools(project_id)`                  | `list[ProjectServicePool]` |
| `set_pool_enablement`  | `set_pool_enablement(project_id, pool_id, enabled)` | `ProjectServicePool`       |

`MLBackend.state` 为 `connected` / `error`;`health_meta`(`HealthMeta`)含 `gpu_info` / `host` / `cache` / `model_version`,由后端 `/health` 缓存,`last_checked_at` 反映最近探测时间。

### client.ml_registry

全局物理 backend registry，所有写操作和 unload 均需 super-admin。

| 方法           | 签名                            | 返回                    |
| -------------- | ------------------------------- | ----------------------- |
| `list`         | `list()`                        | `list[MLBackend]`       |
| `create`       | `create(**fields)`              | `MLBackend`             |
| `update`       | `update(registry_id, **fields)` | `MLBackend`             |
| `delete`       | `delete(registry_id)`           | `None`                  |
| `check_health` | `check_health(registry_id)`     | `MLBackendHealth`       |
| `unload`       | `unload(registry_id)`           | `MLBackendUnloadResult` |

`delete` / `unload` 不会在 SDK 内轮询、强制或降级服务端的静默证明；409 / 503 原样映射为 SDK 异常。

### client.service_pools

逻辑服务池、物理成员路由状态与观测快照，不接受项目 backend ID 替代 pool / registry ID。

| 方法                             | 签名                                                                                     | 返回                                |
| -------------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------- |
| `list` / `get`                   | `list()` / `get(pool_id)`                                                                | `list[ServicePool]` / `ServicePool` |
| `create`                         | `create(name, legacy_instance_id=None)`                                                  | `ServicePool`                       |
| `update` / `delete`              | `update(pool_id, **fields)` / `delete(pool_id)`                                          | `ServicePool` / `None`              |
| `add_member` / `remove_member`   | `add_member(pool_id, registry_id, weight=1)` / `remove_member(pool_id, registry_id)`     | `ServicePool`                       |
| `drain_member` / `resume_member` | `drain_member(pool_id, registry_id)` / `resume_member(pool_id, registry_id)`             | `ServicePool`                       |
| `preview_capability_drift`       | `preview_capability_drift(pool_id, registry_id)`                                         | `CapabilityDrift`                   |
| `accept_capability_drift`        | `accept_capability_drift(pool_id, registry_id, expected_fingerprint, enable_pool=False)` | `ServicePool`                       |
| `topology`                       | `topology()`                                                                             | `ServicePoolTopology`               |
| `runtime_snapshot`               | `runtime_snapshot()`                                                                     | `ServicePoolRuntimeSnapshot`        |

接受能力漂移必须传入预览返回的 candidate fingerprint；SDK 不自动接受最新指纹。

### client.batches

查询与管理某项目的批次。

| 方法            | 签名                                                           | 返回                    |
| --------------- | -------------------------------------------------------------- | ----------------------- |
| `list`          | `list(project_id, status: str \| None = None)`                 | `list[Batch]`           |
| `get`           | `get(project_id, batch_id)`                                    | `Batch`                 |
| `create`        | `create(project_id, name, **fields)`                           | `Batch`                 |
| `update`        | `update(project_id, batch_id, **fields)`                       | `Batch`                 |
| `delete`        | `delete(project_id, batch_id, force=False)`                    | `None`                  |
| `transition`    | `transition(project_id, batch_id, target_status, reason=None)` | `Batch`                 |
| `reject`        | `reject(project_id, batch_id, feedback)`                       | `Batch`                 |
| `reset`         | `reset(project_id, batch_id, reason)`                          | `Batch`                 |
| `distribute`    | `distribute(project_id, **options)`                            | `BatchDistributeResult` |
| `bulk_activate` | `bulk_activate(project_id, batch_ids)`                         | `BulkBatchActionResult` |
| `bulk_approve`  | `bulk_approve(project_id, batch_ids)`                          | `BulkBatchActionResult` |
| `bulk_reject`   | `bulk_reject(project_id, batch_ids, feedback)`                 | `BulkBatchActionResult` |
| `bulk_reassign` | `bulk_reassign(project_id, batch_ids, **assignment)`           | `BulkBatchActionResult` |
| `export`        | `export(project_id, batch_id, targets=None, **options)`        | `str` (job_id)          |

`Batch.progress_pct` 为 0–100 浮点;`annotator` / `reviewer` 为 `UserBrief \| None`(责任人摘要)。端点对项目可见者开放。

批量方法不自动拆分超过服务端上限的 ID，也不重试部分失败；返回值保留 `succeeded` / `skipped` / `failed`。`bulk_reassign` 仅接受 `annotator_id` / `reviewer_id`，显式传 `None` 表示清除分配。

### client.members

| 方法                  | 签名                                                                                                                                                        | 返回                       |
| --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `list`                | `list(project_id)`                                                                                                                                          | `list[Member]`             |
| `add`                 | `add(project_id, user_id, role)`                                                                                                                            | `Member`                   |
| `preview_role_change` | `preview_role_change(project_id, member_id, project_role, *, replacement_annotator_id=None, replacement_reviewer_id=None)`                                  | `ProjectMemberRolePreview` |
| `change_role`         | `change_role(project_id, member_id, project_role, *, expected_version, preview_token, reason, replacement_annotator_id=None, replacement_reviewer_id=None)` | `Member`                   |
| `remove`              | `remove(project_id, member_id)`                                                                                                                             | `None`                     |

`Member` 含 `user_name` / `user_email` / `role`(项目角色 `annotator` / `reviewer` / `viewer`)/ `platform_role`(平台角色)/ `version`(CAS 版本)/ `assigned_at` / `updated_at`。端点对项目可见者开放。

`add` 的 `role` 是项目角色;平台 `viewer` 只能获得 `viewer` 成员身份。角色变更分两步:先 `preview_role_change`(只读)拿到 `current_version`、阻塞项、`resource_snapshot` 与一次性 `preview_token`,再 `change_role` 带 `expected_version` + `preview_token` + `reason`(有未完成工作时给显式接替人)。版本 / 快照失效或资源占用抛 `ConflictError`(409),需要重新预检。

### client.me()

`client.me() -> Me`:返回当前认证主体(`GET /auth/me`)。`Me.role` 是**平台角色**(`super_admin` / `project_admin` / `employee` / `viewer`);项目职责用 `client.projects.access()` 或 `client.members.list()`。

### client.dashboard

看板 / 绩效只读查询。多数端点有角色门控,无权限抛 `PermissionDeniedError`(403)。

| 方法             | 签名                                                              | 返回               | 角色                                   |
| ---------------- | ----------------------------------------------------------------- | ------------------ | -------------------------------------- |
| `admin`          | `admin()`                                                         | `DashboardStats`   | super_admin                            |
| `reviewer`       | `reviewer()`                                                      | `DashboardStats`   | super_admin / project_admin / employee |
| `annotator`      | `annotator()`                                                     | `DashboardStats`   | super_admin / project_admin / employee |
| `people`         | `people(role=None, project=None, period=None, sort=None, q=None)` | `list[PersonStat]` | super_admin / project_admin            |
| `me_performance` | `me_performance(period=None)`                                     | `MyPerformance`    | 任意已认证(self)                       |

`people()`:全员绩效卡片;**project_admin 须传 `project`** 指定其管理范围,super_admin 可全局或任意项目。`admin/reviewer/annotator` 字段随角色而异,经 `DashboardStats`(`extra="allow"`)透传。`reviewer()` / `annotator()` 接受 `employee` 账号,并按其在各项目的对应成员角色过滤工作负载;没有该角色的项目返回空工作集,不再依赖账号全局角色。

### client.api_keys

| 方法     | 签名                                                                            | 返回            |
| -------- | ------------------------------------------------------------------------------- | --------------- |
| `list`   | `list()`                                                                        | `list[ApiKey]`  |
| `create` | `create(name: str, scopes: Sequence[str] \| None = None, expires_in_days=None)` | `ApiKeyCreated` |
| `update` | `update(key_id, *, name=None, scopes=None, expires_in_days=None)`               | `ApiKey`        |
| `rotate` | `rotate(key_id)`                                                                | `ApiKeyCreated` |
| `revoke` | `revoke(key_id)`                                                                | `None`          |

`ApiKeyCreated.plaintext` 是**一次性**的完整 key,创建响应之后无法再次查看,请立即妥善保存。

`update(expires_in_days=None)` 保持不改有效期的既有语义；改回永不过期仍使用 REST PATCH 的显式 `null`。轮换后旧明文失效，不应把完整 key 写入日志。

## 异常层级

全部从 `ai_annotation` 顶层导入:

```text
AAPError                      # 所有 SDK 异常的基类
├── APIStatusError            # HTTP 4xx/5xx 统一异常 (.status_code / .detail)
│   ├── AuthenticationError   # 401
│   ├── PermissionDeniedError # 403
│   ├── NotFoundError         # 404
│   ├── ConflictError         # 409 (如上传内容重复)
│   └── ValidationError       # 422 请求体/参数校验失败
├── JobFailedError            # async job 以 failed/cancelled 终态结束 (.job)
└── JobTimeoutError           # jobs.wait 超时仍未到终态
```

其他状态码(如 500)抛 `APIStatusError` 本身。`.detail` 取自后端错误体 `{"detail": str | dict}`,解析失败时回落原始响应文本。

HTTP 异常在响应包含相关上下文时还可读取 `.request_id` 与 `.retry_after` 信息；这些信息不代表写操作可以自动重试。Python 调用方仍负责保存幂等请求和区分服务器冲突、验证失败及网络结果不确定。

```python
from ai_annotation import Client, NotFoundError, JobFailedError

with Client() as client:
    try:
        project = client.projects.get("...")
    except NotFoundError as e:
        print(e.status_code, e.detail)
```

## 响应模型与前向兼容

顶层导出的 pydantic 模型包括资源模型(`Project` / `Dataset` / `Task` / `Annotation` / `Batch` 等)、工作流结果(`TaskActionResult` / `BulkBatchActionResult` / `JobRetryResult` 等)与模型服务运维模型(`MLBackend` / `ServicePool` / `CapabilityDrift` / topology / runtime snapshot)。

响应模型 `extra="allow"`:只声明 SDK 用户关心的稳定字段,**容忍服务端新增字段**——未声明字段不会导致校验失败,且仍可通过属性访问(如 `project.total_tasks`,服务端附加字段,可能缺失,建议 `getattr(p, "total_tasks", None)` 取用)。新的写请求模型使用 `extra="forbid"`，未知字段和非法数值会在发送前被拒绝。

## 现有列表的筛选与反馈计数

现有列表保留原参数行为，并支持以下可选筛选。UUID 可用字符串或 `UUID`；创建时间使用后端接受的日期时间值。省略参数时不改变原查询。

| 方法               | 可选筛选                                                                           |
| ------------------ | ---------------------------------------------------------------------------------- |
| `projects.list`    | `type_key`, `data_type`, `member_id`, `project_role`, `created_from`, `created_to` |
| `datasets.list`    | `has_scenes`                                                                       |
| `tasks.list`       | `assignee_id`, `batch_id`, `unbatched`, `reject_reason_type`, `class_name`         |
| `annotations.list` | `video_segment_id`                                                                 |
| `jobs.list`        | `search`                                                                           |
| `feedbacks.list`   | `root_only`, `include_counts`                                                      |

`FeedbackPage` 保留 `items` 和 `next_cursor`。传 `include_counts=True` 后，`total` 和 `status_counts` 返回同一权限范围的计数；未请求时为 `None`，不能将它理解为零。`root_only=True` 仅返回根反馈。布尔筛选支持明确传入 `False`。

## 在线学习命名空间

项目级方法的前几个位置参数依次为 `project_id`、路径中的资源 ID、写请求对象或下载目标路径；查询参数使用具名参数。全局 `training` 方法不需要项目路径。完整参数及边界可通过 Python 签名或对应的 `aap learning <group> <command> --help` 查询。

| 命名空间                    | 方法                                                                                                                                                                                                     |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `client.training`           | `catalog`, `projects`, `runs`                                                                                                                                                                            |
| `client.task_datasets`      | `list`, `unrecognized_versions`                                                                                                                                                                          |
| `client.training_datasets`  | `preflight`, `create`, `list`, `get`, `retry`, `download`, `delete`, `insights`, `classes`, `compare`, `samples`, `sample`, `download_media`                                                             |
| `client.training_runs`      | `catalog`, `parameter_schema`, `initialization_source`, `preflight`, `create`, `list`, `get`, `events`, `evaluation`, `telemetry`, `logs`, `download_artifact`, `recovery`, `cancel`, `resume`, `delete` |
| `client.training_recipes`   | `list`, `create`, `get`, `revisions`, `create_revision`, `resolve`, `clone_candidate`, `archive`                                                                                                         |
| `client.training_previews`  | `capabilities`, `create`, `get`, `download_image`, `cancel`, `delete`                                                                                                                                    |
| `client.trained_models`     | `create`, `list`, `get`, `download`, `deploy`, `lineage`, `decisions`, `decide`                                                                                                                          |
| `client.model_releases`     | `list`, `create`                                                                                                                                                                                         |
| `client.learning_policies`  | `get`, `save`, `check`, `disable`, `delete`, `triggers`                                                                                                                                                  |
| `client.sample_evaluations` | `qualification`, `create`, `list`, `get`, `compare`, `report`, `samples`, `source`, `download_image`, `cancel`, `retry`, `delete`                                                                        |
| `client.training_studies`   | `preview`, `create`, `list`, `get`, `cancel`                                                                                                                                                             |
| `client.active_learning`    | `pool`, `preview`, `create`, `list`, `get`, `report`, `candidates`, `download_image`, `decisions`, `decide`, `feedback`, `create_feedback`, `cancel`, `retry`                                            |

### 请求对象与冻结身份

请求模型从 `ai_annotation` 顶层导入。训练数据使用 `TrainingDatasetSelection` / `TrainingDatasetCreate`；实验使用 `TrainingRunCreate`；配方使用 `TrainingRecipeWrite` / `TrainingConfigurationResolve`；增强预览使用 `TrainingPreviewCreate`。模型回流、部署、发布和候选决策分别使用 `TrainedModelCreateRequest`、`ModelDeploymentCreateRequest`、`ModelReleaseCreateRequest`、`ModelDecisionCreateRequest`；策略和评估分别使用 `LearningPolicySaveRequest`、`SampleEvaluationCreateRequest`。

参数搜索使用 `TrainingStudyCreate` 和 `StudyBudgets`，主动学习使用 `ActiveLearningSelection`、`ActiveLearningRoundCreate`、`ActiveLearningDecisionCreate`、`ActiveLearningFeedbackCreate`。数字预算、候选任务和人工决策均为明确输入，不会由客户端补算或自动采纳。

训练公开任务为 `detect` / `segment`，当前仅支持合格图片任务。任务流中更广的任务枚举不代表均可训练。原始上传数据集、任务流、冻结版本、版本展示序号是不同身份；训练请求的 `dataset_id` 必须是冻结版本 UUID。冻结配置、清单、模型来源和摘要原样保留。

创建前显式调用 `preflight` / `preview`，将返回摘要放入创建请求。`request_id` 由调用方生成并持久保存，网络结果不确定时重放相同 ID 和相同请求体；不能用新 ID 代替一次重试。实验创建需要 `preflight_digest`，Study 创建需要 `preview_digest`，即使预检共用的请求模型允许省略它们。Study 的幂等 ID 是 `base.request_id`。

配置选用 `configuration` 时不再同时发送旧 `parameters`。请求仅序列化显式设置字段，保留明确的 `null`；动态 `values` 由服务端参数 schema 校验，SDK 不复制整个训练器参数词表。

### 分页、状态与发布保护

训练页常为 `items` / `total`，部分主动学习页只有 `items`。任务流另有待关联和无法识别版本计数；运行详情是 `{run, attempts, registration_eligibility}`，模型详情是 `{model, deployments}`。响应字段容忍服务端扩展，不能假定每页都有 `limit` / `offset`。

日志和遥测使用序列游标；`logs(after=..., before=...)` 不能同时传两个方向。模型决策用 `before` 游标。冻结版本比较中的 `source_version_id` 指明样本实际属于哪个版本，被移除项可能来自基线版本。每次列表只获取请求的一页。

创建返回 `202` 表示已受理。实验、预览、评估、参数搜索和主动学习有各自的状态，`cancelling` 不等于已停止，`lost` 不等于已释放资源；读取资源详情核对当前 attempt 和恢复资格，不能套用 `jobs.wait()` 的 `completed` 规则。取消／恢复、预览创建和参数搜索均不会由读取调用自动提交。

策略保存／停用需要 `expected_revision`，配方修订／归档需要 `expected_head_revision`，模型发布需要 `expected_generation`。候选模型决策和主动学习人工决策是幂等追加记录，不会自动改变当前发布。发布回退通过 `model_releases.create()` 的 `rollback_from_id` 表达。删除受下游引用保护，409 时不会级联清除依赖。

### 权限、错误与下载

项目级接口要求项目管理权限及 `annotations:read`、`datasets:read`；写接口还要求 `datasets:write`。全局 `training` 入口另要求 `project_admin` / `super_admin`。客户端不绕过外键归属、预检新鲜度、原生资格、运行环境或配额检查；服务端错误保持原始状态码与结构化 `detail`。

下载方法返回本地 `Path`。训练包、权重和产物先获取短期预签名描述，再使用无平台认证头的下载请求；冻结样本／预览图片通过带认证的项目接口流式读取。存在大小与 SHA-256 元数据时在落盘前校验，成功后原子替换目标；失败保留原目标并清理临时文件。过期链接需要重新显式调用下载方法，不自动重试写操作或刷新身份。

核心安装仍只依赖 httpx 和 Pydantic，执行器内部 claim、heartbeat、upload、finish、recover 和 observations 协议不在公开 SDK 中。完整流程见 [Cookbook](./cookbook#_6-冻结数据并提交训练)。
