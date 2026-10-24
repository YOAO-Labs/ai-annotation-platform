---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 审计与通知

这页讲两个横切机制：

- `audit_logs`：事后追溯和运营可见性
- `notifications`：在线提醒和待办收件箱

它们经常在同一条业务链路里一起出现，但语义不同：

- audit 面向“记录发生过什么”
- notification 面向“提醒谁该处理什么”

## 模块定位

业务语义审计与通知由 route / service 显式调用；除此之外，`AuditMiddleware` 会在大多数 HTTP 写请求响应后旁路记录一条 `http.*` 元数据审计。自动层不含业务 action、target 或 detail，不能替代显式审计。

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/audit-notification-flow.svg"
  alt="业务审计、自动 HTTP 元数据审计、通知偏好、持久化收件箱与 Redis 在线推送的分支流程"
  caption="审计与通知的写入、持久化和在线分发边界"
/>

通知创建不是事务型 outbox。默认调用仍先 `flush` 通知行、再尽力发布 Redis，由调用方稍后提交；调用方最终回滚时，在线端可能已经收到消息。讨论事件使用显式延迟发布：业务与通知同事务提交成功后才推送，Redis 失败不改变已提交结果，也没有后台重试或恰好一次推送保证。

## 代码入口

| 位置                                                | 作用                                              |
| --------------------------------------------------- | ------------------------------------------------- |
| `apps/api/app/services/audit.py`                    | `AuditAction`、`AuditService.log()`、`log_many()` |
| `apps/api/app/middleware/audit.py`                  | HTTP 写请求响应后的 `http.*` 元数据审计           |
| `apps/api/app/workers/audit.py`                     | 异步持久化 HTTP 审计；投递失败时中间件同步回退    |
| `apps/api/app/db/models/audit_log.py`               | `AuditLog` 数据模型                               |
| `apps/api/app/api/v1/audit_logs.py`                 | audit 查询与导出                                  |
| `apps/api/app/services/notification.py`             | 通知写表与 Redis PubSub                           |
| `apps/api/app/services/discussion_notifications.py` | 讨论事件收件人、原始目标身份与延迟发布记录        |
| `apps/api/app/services/async_job_notify.py`         | `async_jobs` 终态 → 通用 `job.*` 通知 helper      |
| `apps/api/app/services/async_job_terminal.py`       | 各作业 kind 的领域终态账本回填（signal / 硬取消） |
| `apps/api/app/workers/signals.py`                   | Celery crash / revoke 兜底信号，驱动终态回填      |
| `apps/api/app/db/models/notification.py`            | `Notification` 模型                               |
| `apps/api/app/api/v1/notifications.py`              | 通知列表、已读、偏好设置                          |
| `apps/api/app/api/v1/ws.py`                         | `/ws/notifications` 在线推送                      |

## Audit：记录发生了什么

### 核心模型

`AuditLog` 当前核心字段：

| 字段                                      | 含义                                |
| ----------------------------------------- | ----------------------------------- |
| `actor_id` / `actor_email` / `actor_role` | 谁触发了动作                        |
| `action`                                  | 业务动作名，如 `task.submit`        |
| `target_type` / `target_id`               | 作用对象                            |
| `method` / `path` / `status_code`         | HTTP 元数据                         |
| `ip`                                      | 客户端 IP                           |
| `detail_json`                             | 补充上下文                          |
| `request_id`                              | 同一 HTTP 请求下的多条 audit 关联键 |
| `created_at`                              | 时间戳                              |

### `AuditAction`

`apps/api/app/services/audit.py` 里的 `AuditAction` 当前覆盖了几类高频动作：

- 认证与用户：`auth.*`、`user.*`
- 项目 / 数据集：`project.*`、`dataset.*`
- 批次：`batch.*`
- 标注：`annotation.*`
- 任务审核：`task.submit / withdraw / approve / reject / reopen / skip`
- AI 预标、原生 Mask 与视频追踪：`preannotate.bulk_clear`、`mask_ai.candidate_accept`、`annotation.mask_mutation`、`annotation.convert`、`video_mask.keyframe_correct / keyframe_operate`、`video_tracker_job.create / cancel / accept / discard / decision`、`video_correction_job.create / cancel`
- 在线学习与训练体系：`training_dataset.*`、`training_job.*`、`training_study.*`、`model_release.*`、`active_learning.*`（详见 [在线学习与训练体系](./training-datasets)）
- 反馈：`feedback.created / status_changed / deleted`、`feedback.reconcile_drift`（双写对账漂移）
- 存储连接器：`storage_connection.create / update / delete / test`、`connector.allowlist_update`（见 [存储连接器](/dev/concepts/storage-connections)）。`update` 的 `detail` 含 `secret_rotated` 标记是否轮换密钥，且**绝不写入明文密钥**

但它不是强制枚举。代码里也存在直接传字符串的情况，例如：

- `ai.preannotate.triggered`
- `project.cleanup_orphans`

所以现在更准确的理解是：

- 常用动作建议进入 `AuditAction`
- 业务上仍允许显式字符串

### `AuditService.log()`

最常见调用方式是：

1. 在 route 层完成状态写入
2. 调 `AuditService.log(...)`
3. 同事务 `commit`

`log()` 会自动补：

- `actor_*`
- `method`
- `path`
- `ip`
- `request_id`

调用方只需要专注于：

- `action`
- `target_type`
- `target_id`
- `detail`

### `AuditService.log_many()`

这个接口主要给“一次请求产生多条细粒度审计”的场景用。

当前最典型的例子是：

- annotation attributes 修改时，为每个 key 单独写一条 `annotation.attribute_change`

它的意义是：

- 降低前端 diff 回放的复杂度
- 便于后续按 `detail_json.field_key` 做 JSONB 查询

### `detail_json` 应该放什么

经验上，`detail_json` 适合放：

- 状态迁移前后值
- 影响数量
- 原因 / feedback
- 业务上对排障有价值的上下文

当前常见模式：

- task 提交：`project_id`、`assignee_id`
- batch 逆向迁移：`before`、`after`、`reason`
- batch reset：`from_status`、`affected_tasks`、`cascade`
- export：格式、行数、筛选条件、request id

不要把大块重复对象整份塞进去。`detail_json` 更适合“补事实”，不是“存快照”。

针对 Mask、转换与视频追踪等包含大体积几何/RLE 载荷的动作，`detail_json` 严格遵循以下脱敏与有界记录规则：

| 审计动作                                            | 允许记录的 `detail_json` 摘要字段                                                                                    | 严禁写入审计或普通日志的字段                       |
| --------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `mask_ai.candidate_accept`                          | candidate / content digest、Prediction、源/结果版本、帧号、实际 backend / pool / model、有界 prompt 计数摘要         | RLE counts、原图、scribble 点集、receipt、logits   |
| `annotation.mask_mutation`（指向 operation ledger） | task、operation、updated / created / deleted ID、类型化面积 / 拓扑摘要（与 operation、lineage、annotation 同事务）   | 完整 geometry、RLE counts、未知客户端字段          |
| `annotation.convert`（指向 `convert_annotations`）  | task、copy / replace、目标类型、范围、来源 / 结果 / 有损数量                                                         | 完整 geometry、RLE 正文（逐对象关系由账本保留）    |
| `video_mask.keyframe_correct` / `keyframe_operate`  | operation、帧号、segment、源/结果版本、前后 digest、删除后解析到的 held 来源帧                                       | RLE counts、完整 geometry                          |
| `video_tracker_job.decision`（与标注变更同事务）    | 目标标识、帧窗口、决定、候选数量、前后 revision、显式覆盖人工帧时的 before / after geometry digest                   | staged geometry、Mask RLE、原始媒体                |
| `video_correction_job.create` / `cancel`            | 源 annotation / track、纠错帧、窗口、方向、源版本、corrected digest、backend / pool / model、lease、seed mode / 原因 | RLE counts、scribble 坐标、logits、receipt、原文本 |

### 审计归档的独立存储归属

`audit_archive_outputs` 永久保留每个归档候选的独立所有权，记录作业 ID、规范月份、源分区及父表的历史 OID、源行数与字节回执、压缩对象回执和固定 `storage_ref`。作业删除或源分区删除不会级联删除这些事实；同一月份最多只有一个 `ready` 记录。`pending` 候选可能对应尚未完成核验的远端对象，不能据此重放、覆盖或清理对象。

对象键使用 `{year}/{month}/{archive_id}.jsonl.gz`，月份补足两位。只有分配新候选时读取当前 `audit-archive` 绑定；既有归档始终使用自身保存的桶与端点身份。恢复映射仅允许表所有者在专用事务标记下修改桶和存储身份，历史 OID、月份、对象键和内容回执保持不变。

读取先执行 HEAD 和条件 GET，核验完整压缩字节的 SHA-256 与大小，再分块核验单个 gzip 成员中的全部 JSONL 内容、源行顺序、UTC 月份、源行数、源字节摘要及唯一末尾元数据。追加 gzip 成员和尾部零填充均被拒绝，压缩输入与解压输出分别受分块预算约束。分页只限制返回内存中的条目，不缩减核验范围；元数据仍作为最后一项返回。创建对象使用条件 PUT，完整读回核验成功后才允许发布，失败保留候选和对象，不执行补偿删除。

## Notification：提醒谁该处理什么

### 核心模型

`Notification` 当前关键字段：

| 字段                        | 含义                         |
| --------------------------- | ---------------------------- |
| `user_id`                   | 收件人                       |
| `type`                      | 通知类型，如 `task.rejected` |
| `target_type` / `target_id` | 指向哪个对象                 |
| `payload`                   | UI 展示辅助数据              |
| `read_at`                   | 已读时间                     |
| `created_at`                | 创建时间                     |

通知是“按用户持久化”的，不是纯瞬时弹窗。

### `NotificationService.notify()`

一次通知会做两件事：

1. 写 `notifications` 表
2. 向 Redis PubSub 频道 `notify:{user_id}` 发布消息

这样系统就同时有：

- 离线可恢复的持久化记录
- 在线即时推送

### `notify_many()`

业务上更常见的是 fan-out：

- reviewer 退回 task，通知 assignee
- annotator reopen 任务，通知原 reviewer
- reviewer 驳回 batch，通知 batch annotator

`notify_many()` 会自动去重 `user_ids`，避免重复通知同一收件人。

### 讨论事件的提交与导航

- **收件人计算与越权过滤**：回复创建、问题根记录状态变化以及任务留言/原标注评论创建时的已验证提及，统一调用 `discussion_notifications.py`。收件人按当前项目、任务、批次归属与账号状态重新检查；状态事件通过根线程递归查询收集全部有效后代作者（穿过已删除的中间回复，不受列表分页限制），排除操作人和重复收件人后再应用通知偏好。
- **同事务提交后延迟发布（`defer_publish=True`）**：讨论路由调用 `notify` / `notify_many` 时显式传入 `defer_publish=True`，只收集本请求实际插入的通知行；业务 `commit` 成功后调用 `publish_committed(rows)`，失败则不推送。
- **通知目标与载荷边界**：
  - 问题事件：以根问题 ID 为 `target_id`，来源为 `feedback`。
  - `feedback.comment_mentioned`：以原生任务留言 ID 为目标，来源为 `feedback`，导航到任务评论而非问题线程。
  - `annotation.comment_mentioned`：以原标注评论 ID 为目标，来源为 `annotation_comment`，不引用反馈镜像。
  - 载荷仅保留项目、任务、操作人名称及必要的回复/标注/状态字段，不复制评论正文。
- **工作台深链消费**：通知入口和工作台均重新读取目标。工作台以路由导航身份消费一次激活请求（与面板折叠/重挂载解耦）；旧回复按根游标继续读取，已退休任务或账号的结果不能激活当前面板；读取其他视频分段的评论不申请编辑租约。

### 偏好静音

`NotificationService` 在写通知前会先查：

- `notification_preferences`

当前逻辑：

- 无偏好记录时，默认 `in_app = true`
- 如果某个 type 被静音，则直接跳过
  不写表，也不发 PubSub

这点很重要：**静音不是只屏蔽 websocket 推送，而是整个 in-app 通知链都不写。**

### 已知通知类型

`apps/api/app/services/notification_preferences.py` 中的 `KNOWN_NOTIFICATION_TYPES` 是「**用户可在设置页配置**」的全部类型，`DEFAULT_TOAST_TYPES` 是无显式选择时默认弹出提示的重要类型；设置页 API 从这里读取，前端不维护第二份名单。新增类型时同步该文件与前端设置页标签：

- `bug_report.commented`
- `bug_report.reopened`
- `bug_report.status_changed`
- `batch.rejected`
- `batch.review_reopened`
- `batch.admin_locked`
- `batch.admin_unlocked`
- `batch.unarchived`
- `task.approved`
- `task.rejected`
- `task.reopened`
- `failed_prediction.retry.started`
- `failed_prediction.retry.succeeded`
- `failed_prediction.retry.failed`
- `export.ready`
- `export.failed`
- `job.completed`
- `job.failed`
- `job.cancelled`
- `user.deactivation_requested`
- `user.deactivation_completed`
- `feedback.reply_created`
- `feedback.status_changed`
- `feedback.comment_mentioned`
- `annotation.comment_mentioned`

**特殊类型（不在 `KNOWN_NOTIFICATION_TYPES`，因此用户不能静音）：**

- `feedback.reconcile_drift` — 双写对账发现不一致，仅发给 superadmin（`apps/api/app/workers/feedback_reconcile.py`）

新增可配置通知类型时要同步更新后端 `KNOWN_NOTIFICATION_TYPES` 与前端设置页标签；
不可配置的运维类通知则刻意不进 `KNOWN`，避免被错误静音。

### 偏好存储与弹出门控

偏好按 `(user_id, type)` 存在 `notification_preferences.channels` JSONB：

- `in_app`：接收开关；关闭时整个通知链不写不推。
- `toast`：弹出提示选择；键缺省表示「未选择」，GET 返回按类型默认值
  （`DEFAULT_TOAST_TYPES`）计算的有效值。关闭接收只禁用弹出、不重置该选择，
  重新开启接收时恢复。
- `email`：保留键，当前不消费。

PUT `/notification-preferences` 只合并请求出现的键（JSONB `channels || patch`），
未提供的键与并发标签页写的其它键互不覆盖；提交成功后发布
`notifications.sync reason=preferences`（不产生通知、不改未读数），其它会话据此
刷新偏好查询。

### 读/删除同步的提交顺序

`mark_read` / `mark_all_read` / `delete_for_user` / `clear_read` 只在事务内改行，
`notifications.sync`（`reason=read` / `reason=deleted`）由 API handler 在
`db.commit()` 成功且确有行变化后发布。收到事件的会话读到的必然是已提交状态；
发布失败只记日志，不影响已提交的请求。历史创建路径（`notify` 默认立即 publish）
仍可能在事务提交前发出，客户端继续把 push 视为「可以刷新了」并用 REST 读真值。

### async_jobs 终态通知

用户发起且有 `user_id` 的后台任务在进入终态后会发通用通知：

| 状态        | 通知 type       |
| ----------- | --------------- |
| `completed` | `job.completed` |
| `failed`    | `job.failed`    |
| `cancelled` | `job.cancelled` |

helper 位于 `apps/api/app/services/async_job_notify.py`，由 worker / API 在各自 `mark_*`
之后显式调用；不要把通知职责塞进 `services/async_job.py` 的状态方法。调用方保留事务所有权：
helper 在 `mark_*` 之后、调用方 `commit()` 之前调用；`/ws/notifications` 的投递闸门会在
自己的会话里短暂重试读取尚不可见的行，因此消息在提交落地后送达而不是被丢弃。

各作业 kind 的**领域终态账本**（`MaskQCRun` / `PointCloudQualityRun` /
`MaskRepairBatch` / `MaskFormatImport` / 在线训练与评估作业）的正规更新仍在各自 worker 或内部回调路由内。兜底路径——worker
进程 crash、Celery revoke、未被 task body 捕获的异常——由 `workers/signals.py` 走统一骨架
（连接 → 按 `celery_task_id` 查 `async_jobs` → 落终态 → 回填领域账本 → 通知 → 提交 → 释放），
领域差异由 `services/async_job_terminal.py` 的封闭分派承担：mask 格式导入取消保留
`partial`、回滚失败保留 `rollback_failed`、修复取消按已完成分片决定 `partial` / `cancelled`。
终态作业不会被迟到的信号覆盖，未知 `celery_task_id` 与无领域账本的 kind 静默跳过。

当前白名单包括：

- `batch_predict` / `prediction_retry` / `predictions_import`
- `video_tracker` / `video_correction`
- `dataset_import` / `audit_archive`
- `mask_qc` / `mask_repair` / `mask_repair_rollback` / `mask_format_import`
- 在线学习作业（`training_dataset_build` / `training_job` / `training_evaluation` / `active_learning_refresh`，详见 [在线学习与训练体系](./training-datasets)）

`failed_prediction.retry.succeeded` / `failed_prediction.retry.failed` 保留为历史通知偏好类型；当前失败预测重试的终态由 `prediction_retry` 的 `job.completed` / `job.failed` 承载。

`export` 不进通用 `job.*`，因为导出 worker 已经发 `export.ready` / `export.failed`
并携带 `download_url`。

## 在线推送与离线读取

通知中心有两条消费路径：

### 1. REST 读取

- `GET /notifications`
- `GET /notifications/unread-count`
- `POST /notifications/{id}/read`
- `POST /notifications/mark-all-read`
- `DELETE /notifications/{id}`
- `POST /notifications/clear-read`

这是持久化收件箱。

### 2. WebSocket 在线推送

- `WS /ws/notifications?token=...`

WS 握手时会校验 JWT，然后订阅：

- `notify:{user_id}`

如果 WS 断线，前端还能靠 REST 列表补齐遗漏消息。

## 审计和通知的配合方式

它们经常一起出现在同一个动作里，但承担不同职责。

以 `task.review/reject` 为例：

1. route 改 `task.status`
2. `AuditService.log(action=task.reject, ...)`
3. `NotificationService.notify_many(type="task.rejected", ...)`
4. `commit`

其中：

- audit 给管理员和历史记录看
- notification 给 assignee 立刻看

二者不是谁替代谁。

## 当前项目里的典型模式

### 只记 audit，不发 notification

适合：

- 纯管理动作
- 批量后台操作
- 不需要有人立即响应的设置项变更

例如：

- 用户角色变更
- 系统设置更新
- 导出请求阶段（worker 终态另发 `export.ready` / `export.failed`）

### audit + notification 一起发

适合：

- 有明确下游处理人的工作流动作

例如：

- `task.approved`
- `task.rejected`
- `task.reopened`
- `batch.rejected`
- `batch.review_reopened`

### 只发 notification 的情况很少

在这个仓库里，重要业务动作通常都应该至少有 audit。
如果你发现某个关键工作流只有通知没有审计，通常值得补。

## 反馈统一表(ADR-0027)

反馈入口历史上散在 `bug_reports`（产品 BUG）、`annotation_comments`（标注评论）、`tasks.reject_reason`（审核驳回）和 pixel-anchored issue。当前用 `annotation_feedbacks` 表统一锚点（`anchor_type` ∈ project/task/annotation/pixel）+ `kind` ∈ issue/comment/reject/bug，并通过旧源镜像与对账任务逐步收口为单一写入入口。详见 [ADR-0027](/dev/adr/archive/0027-annotation-feedback-unified-table)（规范由 docs/adr 维护）。

迁移按三段式推进，每段独立可回退：

| 阶段            | 状态    | 行为                                                                                                                                                                                                                                                                                             |
| --------------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 新表与 API      | ✅ 已落 | 新表 + 新 API（`GET/POST/PATCH/DELETE /feedbacks`），旧三表读写不动；前端 IssueLayer 直接读新表                                                                                                                                                                                                  |
| 双写与统一 view | ✅ 已落 | `v_annotation_feedback_unified` UNION ALL view（带 `source_table` 列对账）；`FeedbackService.mirror_bug_report` / `mirror_annotation_comment` / `mirror_task_reject` 3 个 helper 接入旧三处写路径，**同事务**双写，失败一起回滚；前端只读暂不切到 view，避免老 bug/reject 出现在 DiscussionPanel |
| 切单源          | 计划中  | 验证双写一致性 → 旧表存量数据一次性 backfill 到 `annotation_feedbacks` → 删旧写路径，旧表保留只读一个版本作回退                                                                                                                                                                                  |

**audit 落点对齐**：新表的写操作走 `FEEDBACK_CREATED` / `FEEDBACK_STATUS_CHANGED` / `FEEDBACK_DELETED` 三个 AuditAction，不再为 4 个 source 各维护 detail helper。旧 `bug_report.*` 通知类型保留。

**双写一致性监控**：view 的 `source_table` 列可用于对账查询（`SELECT source_table, COUNT(*) FROM v_annotation_feedback_unified GROUP BY source_table` 对比 `annotation_feedbacks` 行数）；日志关键字 `[ADR-0027 double-write]` 用于排障。`bug_reports.project_id IS NULL` 行（登录页等无项目归属 bug）不 mirror，留切单源时单独处理。

**自动对账**：上述手工对账已自动化为每日 03:00 UTC 的 celery beat 任务 `reconcile_annotation_feedback`。它逐源比对旧表「应 mirror 行数」与统一表「实际镜像行数」，发现缺失时写 `FEEDBACK_RECONCILE_DRIFT` 审计并通知全部 superadmin（`feedback.reconcile_drift`）。drift 长期为 0 是切单源的前置条件。机制详见 [反馈收敛与双写对账](./feedback-convergence)。

### 讨论读取与草稿归属

- **只读聚合路由（`GET /tasks/{task_id}/discussion/page`）**：
  - 标注评论以 `annotation_comments` 为准；任务留言（支持成员提及，图片可附绘图）以原生 `annotation_feedbacks` 任务评论根记录为准。
  - 聚合结果携带 `source` 和原记录，客户端以 `(source, id)` 标识条目并路由修改、删除及附件下载；Issue 回复、BUG 和退回镜像不进入评论列表。
- **画布绘图与评论气泡**：
  - 图片任务绘图复用 `CanvasDrawing`，存入原生任务留言的可空 `annotation_feedbacks.canvas_drawing`。
  - 画布气泡通过 `discussion/annotation-counts` 对可用标注的有效原评论做任务级分组计数（关闭 `workbench.common.showAnnotationComments` 偏好时停用）。
  - 视频评论气泡按持久化 `annotation.id` UUID 聚合评论，单帧标注只显示在其源帧，轨迹按当前可见几何定位。
- **提及（`Mention`）与像素问题（Issue）**：
  - 任务留言提及存入 `annotation_feedbacks.mentions`；问题回复不接收提及。
  - 图片和视频像素问题共用严重度与状态视觉规则，位置和关联对象在创建时冻结；普通问题的 `blocker` 严重度不参与 Mask 质检的审核阻断计算。
- **服务端游标与写入复核**：
  - 聚合查询在数据库内合并排序键后分批读取；线程读取穿过已删除的中间回复，但不可见或已删除根记录下的线程不可操作。
  - 接口返回的 `actions` 仅用于 UI 展示，每次写入重新校验项目、任务及线程权限；审核员的状态权限不允许在同一 PATCH 中夹带他人正文、标题或严重度修改。
- **前端草稿隔离（`DiscussionDraftProvider`）**：
  - 位于路由展示层之上，按认证会话、用户、项目、任务、目标类型及目标 ID 隔离草稿；文字与结构化草稿只保存在当前登录会话内存中。
  - 题图上的活动画布笔触另用带命名空间的 5 分钟 `sessionStorage` 恢复记录，任务切换和路由卸载前先同步保存原 owner 再释放画布，注销时清除原用户记录。

## 常见修改落点

| 你想改什么       | 先看哪里                                                     |
| ---------------- | ------------------------------------------------------------ |
| 新增业务动作审计 | `services/audit.py` + 对应 route                             |
| 让某动作可通知   | `services/notification.py` + 对应 route                      |
| 通知偏好开关     | `api/v1/notifications.py` + `notification_preferences`       |
| 后台任务终态通知 | `services/async_job_notify.py` + 对应 worker/API 终态点      |
| 作业终态领域账本 | `services/async_job_terminal.py` + `workers/signals.py` 兜底 |
| 审计查询 / 导出  | `api/v1/audit_logs.py`                                       |
| 批次审计聚合视图 | `api/v1/batches.py:list_batch_audit_logs`                    |

## 常见误解

### 误解 1：通知就是审计

不是。

- 通知面向收件人体验
- 审计面向追溯和运营

### 误解 2：有 Redis PubSub 就不用写通知表

不对。当前实现明确是“写表 + 推送”双轨。

### 误解 3：新增 route 只要补一条 `AuditAction` 就够了

不够。你还要决定：

- `target_type / target_id` 怎么定义
- `detail_json` 记录哪些关键上下文
- 是否需要 fan-out 给具体用户

## 相关文档

- [审核模块](./review-module)
- [反馈收敛与双写对账](./feedback-convergence)
- [批次生命周期（端到端）](./batch-lifecycle-end-to-end)
- [可见性与权限](./visibility-and-permissions)
- [WebSocket 协议](/dev/reference/ws-protocol)

存储位置注册、更新和节点探测记录 `storage_location.create / update / probe` 审计；只记录种类、修订、启停与脱敏结果码，不记录绝对根、凭据或内部目录指纹。

目录来源登记/更新和扫描启动/恢复记录 `dataset_source.create / update` 与 `dataset_scan.start / resume`。`dataset_scan` 的终态复用 job 通知，摘要只带ID、计数和完整性，不含目录路径、根身份或原始异常；扫描取消及失联由同一领域 owner 保留不完整状态。

目录条目首次成功内容核验记录 `dataset_content.verify`，目标仅为 run/路径摘要，不记录根指纹、绝对路径或原图字节。重复同内容核验不改写历史回执。
