---
title: Python SDK 参考
audience: [dev]
type: reference
since: v0.15.2
status: beta
last_reviewed: 2026-06-11
---

# Python SDK 参考

包名 `ai-annotation-sdk`,导入名 `ai_annotation`。同步 Client,底层 httpx + pydantic v2。

> **兼容承诺范围**:公开 API 仅限本页所列(`Client` + 资源命名空间方法 + `ai_annotation` 顶层导出的模型与异常)。内部传输层 `ai_annotation._http` 不在兼容承诺内,外部代码不要直接依赖。

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
- 幂等重试:**仅 GET** 请求在 429 / 502 / 503 / 504 时做最多 3 次指数退避重试;POST / PATCH / DELETE 不重试。
- 预签名 URL(上传 PUT、绝对地址下载)走无 auth 的裸 client。

## 资源命名空间

### client.projects

| 方法     | 签名                                                                                       | 返回            |
| -------- | ------------------------------------------------------------------------------------------ | --------------- |
| `list`   | `list(status: str \| None = None, search: str \| None = None)`                             | `list[Project]` |
| `create` | `create(name: str, type_key: str \| None = None, data_type: str \| None = None, **kwargs)` | `Project`       |
| `get`    | `get(project_id)`                                                                          | `Project`       |
| `update` | `update(project_id, **fields)`                                                             | `Project`       |
| `delete` | `delete(project_id)`                                                                       | `None`          |
| `stats`  | `stats()`                                                                                  | `ProjectStats`  |
| `access` | `access(project_id)`                                                                       | `ProjectAccess` |

`create` 说明:后端 `type_label` 必填,未通过 `kwargs` 显式给出时 SDK 按 `type_key` → `data_type` → `name` 顺序兜底填充。

`stats()` 返回可见项目聚合(`total_data` / `completed` / `ai_rate` / `pending_review`)+ 最近 12 周时间序列(`*_series`),任意已认证用户可达。

`access()` 返回当前账号在该项目上的解析后权限(`ProjectAccess`):`platform_role`(平台角色)、`project_role`(项目角色,管理者可为 `None`)、`access_kind`(`super_admin` / `owner` / `member`)、`membership_id` / `membership_version`、`is_manager` 与 `capabilities`。`Project.my_project_role` 是列表 / 详情里当前账号的项目角色。授权来自项目成员关系而非账号全局角色,详见[可见性与权限](../concepts/visibility-and-permissions)。

### client.datasets

| 方法             | 签名                                                                                                                  | 返回                   |
| ---------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `list`           | `list(search: str \| None = None, data_type: str \| None = None, limit: int = 50, offset: int = 0)`                   | `Page[Dataset]`        |
| `create`         | `create(name: str, data_type: str = "image", **kwargs)`                                                               | `Dataset`              |
| `get`            | `get(dataset_id)`                                                                                                     | `Dataset`              |
| `update`         | `update(dataset_id, **fields)`                                                                                        | `Dataset`              |
| `delete`         | `delete(dataset_id)`                                                                                                  | `None`                 |
| `list_items`     | `list_items(dataset_id, limit: int = 50, offset: int = 0)`                                                            | `Page[DatasetItem]`    |
| `delete_item`    | `delete_item(dataset_id, item_id)`                                                                                    | `None`                 |
| `list_projects`  | `list_projects(dataset_id)`                                                                                           | `list[Project]`        |
| `upload_files`   | `upload_files(dataset_id, paths: Sequence[str \| Path], on_progress: Callable[[int, int, str], None] \| None = None)` | `list[UploadedItem]`   |
| `upload_zip`     | `upload_zip(dataset_id, zip_path: str \| Path)`                                                                       | `ZipUploadResult`      |
| `link_project`   | `link_project(dataset_id, project_id)`                                                                                | `LinkResult`           |
| `preview_unlink` | `preview_unlink(dataset_id, project_id)`                                                                              | `DatasetUnlinkPreview` |
| `unlink_project` | `unlink_project(dataset_id, project_id)`                                                                              | `DatasetUnlinkResult`  |

- `upload_files`:逐文件三步流(upload-init → PUT 预签名 URL → upload-complete)。`on_progress(done, total, file_name)` 在每个文件完成后回调。
- `upload_zip`:multipart 上传单个 ZIP 包,后端解压入库(≤200MB / ≤5000 文件)。
- `link_project`:返回的 `LinkResult.status == "linking"` 时建任务走异步,用 `async_job_id` 配合 `client.jobs.wait` 等待。
- `update`: `**fields` 保留“未提供”与“显式传 `None`”的区别，因此可以用 `axis_convention=None` 清除坐标系约定。
- `preview_unlink` 返回将删除的 task / annotation / batch 计数；`unlink_project` 返回实际删除计数。

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

| 方法       | 签名                                                                                        | 返回          |
| ---------- | ------------------------------------------------------------------------------------------- | ------------- |
| `create`   | `create(project_id, targets: list[str], include_attributes: bool \| None = None, **kwargs)` | `str`(job_id) |
| `wait`     | 同 `jobs.wait`(纯转发)                                                                      | `Job`         |
| `download` | `download(job_or_id: Job \| IdLike, dest_path: str \| Path)`                                | `Path`        |

- `create`:发起异步导出(HTTP 202),返回 job_id;参数走 query string(与后端端点一致)。导出格式清单见[导出格式参考](/user-guide/reference/export-formats)。
- `download`:从 `job.result["download_url"]` 流式下载导出包;传 job_id 时会先 GET 一次 job。job 无 `result.download_url`(如尚未完成)时抛 `AAPError`。

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

| 方法     | 签名                                                      | 返回            |
| -------- | --------------------------------------------------------- | --------------- |
| `list`   | `list()`                                                  | `list[ApiKey]`  |
| `create` | `create(name: str, scopes: Sequence[str] \| None = None)` | `ApiKeyCreated` |
| `revoke` | `revoke(key_id)`                                          | `None`          |

`ApiKeyCreated.plaintext` 是**一次性**的完整 key,创建响应之后无法再次查看,请立即妥善保存。

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

所有模型 `extra="allow"`:只声明 SDK 用户关心的稳定字段,**容忍服务端新增字段**——未声明字段不会导致校验失败,且仍可通过属性访问(如 `project.total_tasks`,服务端附加字段,可能缺失,建议 `getattr(p, "total_tasks", None)` 取用)。
