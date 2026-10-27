---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-25
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

一般通知创建不是事务型 outbox。普通默认调用仍先 `flush` 通知行、再尽力发布 Redis，由调用方稍后提交；调用方最终回滚时，在线端可能已经收到消息。讨论事件使用显式延迟发布：业务与通知同事务提交成功后才推送，Redis 失败不改变已提交结果，也没有后台重试或恰好一次推送保证。自助注销的两种通知是窄范围例外：Notifications owner 同事务写持久投递 outbox，由独立 publisher 领取和重试；这不改变其他通知语义。

## 代码入口

| 位置                                                                              | 作用                                                         |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `apps/api/app/services/audit.py`                                                  | `AuditAction` 与兼容 `AuditService.log()`／`log_many()` 门面 |
| `apps/audit/src/aap_audit/append_contract.py`、`append.schema.json`               | `aap-audit-append/v1` 追加命令与回执合同                     |
| `apps/audit/src/aap_audit/postgres.py`、`sql/0315_audit_append.sql`               | 同事务 PostgreSQL Audit owner 适配器与追加函数               |
| `apps/audit/src/aap_audit/query_contract.py`、`query.schema.json`                 | `aap-audit-query/v1` 全局查询命令与响应合同                  |
| `apps/audit/src/aap_audit/query_postgres.py`、`sql/0317_audit_query.sql`          | 借用调用方事务的固定 Audit 查询函数                          |
| `apps/audit/src/aap_audit/read_relation_postgres.py`                              | 公开事件关系及原生类型；领域范围由消费者组合                 |
| `apps/api/app/middleware/audit.py`                                                | HTTP 写请求响应后的 `http.*` 元数据审计                      |
| `apps/api/app/workers/audit.py`                                                   | 异步持久化 HTTP 审计；投递失败时中间件同步回退               |
| `apps/api/app/db/models/audit_log.py`                                             | `AuditLog` 数据模型                                          |
| `apps/api/app/api/v1/audit_logs.py`                                               | audit 查询与导出                                             |
| `apps/api/app/services/audit_summary.py`                                          | 正常月报的本地 Python 归并                                   |
| `apps/audit-summary`、`apps/api/experiments`                                      | 显式隔离实验，不参与正常月报执行                             |
| `apps/api/app/services/notification.py`                                           | 通知 owner 调用、公开事实读取与 Redis PubSub                 |
| `apps/api/app/services/notification_read_relations.py`                            | 公开通知关系与原 Session identity 兼容绑定                   |
| `apps/notifications/src/aap_notifications/preferences.policy.json`                | 跨语言共享的可配置类型与默认 toast 策略                      |
| `apps/notifications/src/aap_notifications/preferences_contract.py`                | 版本化 GET／PATCH／mute JSON 合同与响应验证                  |
| `apps/notifications/src/aap_notifications/sql/0311_notifications_preferences.sql` | 同事务读取、静音与 JSONB merge owner 函数                    |
| `apps/notifications/src/aap_notifications/append.py`                              | 持久化 append manager 与请求/返回行关联检查                  |
| `apps/notifications/src/aap_notifications/append_contract.py`                     | 版本化通知 append JSON 合同与返回值关联验证                  |
| `apps/notifications/src/aap_notifications/append_postgres.py`                     | 借用 API 请求 Session 的 append PostgreSQL 适配器            |
| `apps/notifications/src/aap_notifications/sql/0314_notifications_append.sql`      | 返回服务端行快照的通知持久化 append 函数                     |
| `apps/notifications/src/aap_notifications/preferences_postgres.py`                | 借用 API 请求 Session 的 PostgreSQL 适配器                   |
| `apps/api/app/services/discussion_notifications.py`                               | 讨论事件收件人、原始目标身份与延迟发布记录                   |
| `apps/api/app/services/async_job_notify.py`                                       | `async_jobs` 终态 → 通用 `job.*` 通知 helper                 |
| `apps/api/app/services/async_job_terminal.py`                                     | 不含 ORM/传输的不可变 `JobTerminal` 事实                     |
| `apps/api/app/application/job_terminal.py`                                        | 领域终态绑定与信号兜底工作流，同事务协调状态/通知            |
| `apps/api/app/application/job_cancellation.py`                                    | 取消准入、领域命令与执行停止请求组合，提交由入口持有         |
| `apps/notifications/src/aap_notifications/notification_delivery_contract.py`      | publisher 的领取、确认与策略协议                             |
| `apps/notifications/src/aap_notifications/notification_delivery_postgres.py`      | publisher 的 PostgreSQL 执行器                               |
| `apps/notifications/src/aap_notifications/delivery_worker.py`                     | `NotificationPublisher` 执行器与独立 CLI                     |
| `apps/notifications/src/aap_notifications/delivery_config.py`                     | `NotificationPublisherSettings` 环境配置                     |
| `apps/notifications/Dockerfile`                                                   | 锁定依赖、非 editable 安装的独立 publisher 工件定义          |
| `apps/identity/src/aap_identity_runtime/notification_worker.py`                   | 既有 Identity publisher 命令的兼容入口                       |
| `apps/api/app/services/job_execution_control.py`                                  | 不依赖 broker 的协作取消执行端口                             |
| 各领域服务的 `reconcile_job_terminal`                                             | 只回填所属账本，保留各自终态语义                             |
| `apps/api/app/workers/signals.py`                                                 | Celery 信号适配、连接与调用方提交/释放                       |
| `apps/api/app/db/models/notification.py`                                          | `Notification` 模型                                          |
| `apps/api/app/api/v1/notifications.py`                                            | 通知列表/已读及偏好的 HTTP 鉴权、校验、提交与提交后同步      |
| `apps/api/app/api/v1/ws.py`                                                       | `/ws/notifications` 在线推送                                 |

显式业务事件通过 `apps/audit` 的版本化追加函数写入，并保留调用方的
PostgreSQL 事务；API 适配器在调用返回前恢复 `AuditLog` ORM identity。HTTP
元数据仍在响应后 best-effort 写入，并保留 actor 外键失败后的匿名重试。超级管理员
的全局列表和导出也通过 Audit 的固定查询函数读取；它保留现有筛选、游标、偏移、
当前邮箱回退与 50,000 行导出上限。项目管理员范围由 API 通过公开 Audit 事件关系组合
Workspace 与 Labeling 事实；标注历史、批次日志、管理看板及评审证据也读取同一关系。
月报、DuckDB、绩效读取公开事件／日聚合关系；分区维护和冷归档由 Audit owner 的固定维护能力及独立运行包持有，API／Celery 仅保留调用方事务适配。配置与权限见 [Audit 运行时](/api/guides/audit-runtime)。

## Audit：记录发生了什么

### 本地来源故障观测

`services/storage_fault_events.py` 将本地数据集来源的读取、扫描及预览生成故障写入现有 `AuditLog`：action 为 `storage.fault`，target 为已解析的 `storage_location`，明细只接受固定分类、阶段和白名单 UUID。调用点必须先完成当前用户／Key 授权及来源定位，不能从请求参数猜位置；客户端 SHA 错误和普通权限拒绝不会被记成物理原件变化。

filesystem 只在打开配置根的实际失败处标记根不可用；S3／SFTP 在真实服务认证拒绝处保留内部分类，不改变已有公开错误码。读取的完整摘要不符与预览生成失败分别记录。现有 Run／Preview／AsyncJob 仍拥有业务状态，观测事件不代替成功发布或失败提交证明。

旁路持久化使用 `storage_access.engine(db)`，由当前 API／Worker 的正确 Engine 新建事务，不提交或回滚调用者业务。数据库锁等待限制为250ms、语句为1s，观测尝试另有2s超时；失败只输出固定安全日志，不输出异常原文，也不改原读取异常。调用者回滚后观测仍可存在；它记录已发生的尝试，不保证关联业务最终提交。已删除或尚未提交的账号不强造用户外键，已解析的账号 UUID 仍可保留在明细。

HTTP 事件沿现有上下文保留不超过36字符的安全 request_id，不改写其格式；不安全或过长值省略，不丢弃其余故障事实。后台扫描在原 task／fence 检查通过后关联真实 Run／Job，旧投递不重复记录；没有原 HTTP request_id 时不补造。现有审计 API 的 action、target 和 JSONB 明细过滤即可追溯，没有新增监控标签、事件表、队列或通知渠道。当前覆盖边界见[存储故障关联](/user-guide/superadmin/audit-logs#存储故障关联)。

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

历史查询和导出优先显示已存的 `actor_email`；旧记录为空且关联账号仍存在时，才将当前 `User.email` 投影到响应 DTO。该回退不写入 ORM `AuditLog`：导出随后会 flush 并提交 `audit.export`，不得因此更新受 append-only trigger 保护的历史行。

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

`AuditService.log()` 与 `log_many()` 保留面向 API 的调用签名，并通过
`aap_audit_v1.append_events(jsonb)` 提交调用方快照。它们不会提交事务：追加函数、
相关业务写入、ORM identity 恢复与调用方显式 flush 都留在同一事务中。追加合同允许
开放的 action 字符串，并将 `detail_json` 中的 Python `None` 表示为 JSON null，
与现有 JSONB 映射一致。

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

### 审计查询与权限边界

`aap_audit_v1.audit_events` 是 Audit owner 持有的 security-barrier 视图，保留 14 个原生字段及 INET 类型。物理行键为 `(id, created_at)`，全局查询合同允许同 ID、不同时间的合法记录；兼容 ORM 仍保持原有仅按 ID 的身份映射。公开关系只表达事实，受限服务 LOGIN 可以 SELECT，但不能据此绕过 API 的用户范围条件。领域消费者借用原事务并保留 autoflush；Learning 暂缓消费者仍保留父表 SELECT；普通运行写入、序列和分区所有权由 Audit owner 持有，尚未因此宣称全模块运行验收完成。

HTTP 查询端点 `GET /api/v1/audit` 面向运营与排障，严格执行基于角色的管理范围隔离：

- **超级管理员**：具备全平台全局可见性，可检索任意用户、实体与系统级别的动作，并独占按月聚合、冷归档（`POST /audit/archive`）与全量 CSV 导出功能。
- **项目管理员**：系统强制注入 `_managed_audit_scope`，在分页与 `total` 统计之前将查询范围限定在其作为 owner 负责的项目内。涵盖项目实体自身、关联任务、批次、标注、反馈、成员邀请以及携带 project_id 的上下文事件；无可靠项目归属的系统级事件不可见。
- **已删除实体的追溯**：批次硬删除时在审计 detail 显式写入 `project_id`；即使批次行已从 `task_batches` 移除，项目管理员依然能通过上下文 `project_id` 或历史路径前缀追溯删除事实与影响任务数。

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

一次未静音通知会先检查偏好，再在调用方事务中写入持久化行；按调用方选择时机发布 Redis 消息：

1. 通过 `preference_command(jsonb)` 检查 `in_app` 静音
2. 调用 `append_notification(jsonb)` 写 `notifications`，把返回行合并到调用方 Session，再执行原有的完整 flush
3. 按 `defer_publish` 约定发布到 Redis PubSub 频道 `notify:{user_id}`

`append_notification` 是持久化能力，不重复执行业务静音策略，也不提交事务。API 保留既有偏好预检与事务协调，收到带有数据库 `now()` 时间戳的行快照后，把它合并进原 Session identity map，再按原有时序返回与发布。跨语言调用方同样先调用偏好 owner 的静音操作，再调用 append；两个调用继续借用同一 PostgreSQL 事务。

### 收件箱与持久行读取

收件箱的列表、总数和未读数分别经 Notifications owner 的 `inbox_page`、`inbox_total_count` 和 `inbox_unread_count` 查询。API 先读取 Identity 状态，再传认证 actor UUID 与该次角色快照；owner 通过 Identity 公开事实、Workspace 成员／项目查询能力和 Jobs 通知范围事实执行原有筛选。三条查询继续分开执行，保留原事务快照顺序与 Session 的 autoflush、identity／dirty／expired 行为。JSONB payload 保持原生形状，列表继续使用 PostgreSQL `->>` 的项目范围语义。

单条读取、终态预查、批量投递与 WebSocket 的持久行检查通过七项固定 Notifications 读取能力，API 不再持有 `notification_facts` 的表／列读取权限。八个原生字段和 Session 缓存／过期 identity 保持；终态预查仍在原保存点中运行，最终去重继续由原子追加负责。

Notifications 的纯投递策略先按需读取 Jobs 实际范围，再分别查询有效成员、reviewer、活跃管理账号所有权和活跃超级管理员权限。四项授权保持独立语句快照；API 的 Session 适配保留缓存与读取次序，Notifications owner 持有发布策略、逐条错误处理和 WebSocket 生命周期。批量快照保留字典键存在性及原 UUID 解析语义；它与收件箱 `->>` 的范围规则分别保留。当前资格及剩余独立运行边界见[投递读取台账](./module-deployment#notification-delivery-read-owner)。

### 即时发布与独立运行

普通发布编排由 `aap_notifications.publication.NotificationPublicationManager` 持有，API 继续控制业务事务和调用时点。空 `NOTIFICATION_PUBLICATION_URL` 使用内嵌 Redis transport；配置私网地址后，经 `aap-notifications-immediate-publication/v1` 调用现有独立 publisher 的可选 HTTP 接口，故障不回退本地 Redis。

API 在原借用 Session 中读取通知、Jobs 范围和四组授权事实，将事件及该次受信快照交给 publisher；publisher 不重新查询未提交的业务行，并再次执行同一授权策略。服务 bearer 只授予这项发布能力，不能作为用户 JWT 或 Identity 控制凭据。消息频道由收件人 UUID 生成，调用方不能指定任意 Redis channel。

`notify()`／`notify_many()` 默认延后发布，调用方提交后逐条 best-effort 发布；终态通知及显式 `defer_publish=False` 保留提交前时序。`publish_by_id()` 的传输错误仍传播，使既有导入投递租约不被确认；偏好、已读和删除的同步提示仍在对应提交后 best-effort 发布。普通发布不新增 outbox 或自动重试，durable publisher 继续按原租约确认和重试。WebSocket 继续在短暂缺行时读取四次、等待三次 150ms，回滚后不可见的通知不会发送给客户端。配置见[环境变量参考](../reference/env-vars)，协议见[模块数据接口](../../api/guides/module-data#notifications-即时发布)。

### 已读状态命令

`POST /api/v1/notifications/{id}/read` 与 `POST /api/v1/notifications/mark-all-read` 经
`aap_notifications.read_state` 调用 `aap_notifications_v1.read_state_command(jsonb)`。
API 仍从当前认证主体取得 `user_id`，owner 只更新该用户的未读行，并使用 API
传入的单次 UTC `read_at` 时间戳。单条操作返回原有 boolean 结果；批量操作返回原有
更新数量。Owner 回执继续验证更新 ID；API 在返回后复用当前锁定 SQLAlchemy 的批量 evaluate 缓存规则，保留脏值、过期字段、谓词次序和保存点回滚登记。旧 ORM 语句仅在本地编译用于缓存同步，不执行第二次 UPDATE／SELECT；事务仍由调用方提交。升级 SQLAlchemy 前须重验这层兼容绑定。

绑定尊重借用 `AsyncSession` 的 `autoflush`／`no_autoflush`：普通调用先执行完整原有 flush，
显式禁止 autoflush 时不提前提交待处理对象。HTTP handler 仍在数据库提交后发布
`notifications.sync reason=read`；单条缺失、他人记录或已读通知仍返回 404，批量无更新时
仍提交请求但不发布同步。列表可见性与 WebSocket 投递授权继续由原 Workspace／Jobs
范围校验负责。

### 删除通知命令

`DELETE /api/v1/notifications/{notification_id}` 与 `POST /api/v1/notifications/clear-read`
经 `aap_notifications.delete` 调用 `aap_notifications_v1.delete_command(jsonb)`。API
始终从当前认证主体取得 `user_id`；0318 安装该 capability。Owner 单条删除按 ID 和用户同时限定，清空只删除该用户
`read_at` 非空的行，并返回原有 boolean／count。SQLAlchemy binding 借用同一 `AsyncSession`、
遵循 `autoflush`／`no_autoflush`，不提交、不返回全量删除 ID。API 在 owner 命令后按原
ORM DELETE evaluator 同步身份缓存，因此已有、脏、过期对象及回滚语义保持一致。

单条不存在或属于其他用户仍由 HTTP handler 返回 404；清空为零时仍提交但不发布。业务事务
提交后才发布 `notifications.sync reason=deleted`。严格权限安装后，普通 API LOGIN 只能调用
固定删除函数，不能直接对通知表执行 DELETE；部署时先更新 API／worker 工件并排空旧进程。

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

`apps/notifications/src/aap_notifications/preferences.policy.json` 是「**用户可在设置页配置**」的类型与默认 toast 合同。API 使用此名单做公开请求校验，0311 Notifications owner 在 PostgreSQL 内保留同一有序策略，并自行计算新行默认值；PATCH 不接受调用方传入默认值。Python contract 测试逐项比较 JSON 与冻结 SQL，Node peer 则直接调用同一 owner 函数。新增类型时更新该策略、后续线性 Alembic owner SQL 与前端设置页标签：

`infra/docker/notification-preferences-grants.sql` 撤销指定 API LOGIN 与 `PUBLIC` 对偏好私表的直接权限；`notification-append-grants.sql` 安装偏好、追加、已读和删除四项固定能力。`notifications-read-grants.sql` 改授公开通知关系 SELECT；普通工作树和 LAN 授权源也关闭三张 Notifications 私表及列权限，publisher 仍使用专用固定函数。先部署匹配 API／worker 工件并排空旧私表消费者，再安装严格授权；回退也须同时匹配代码、关系和授权。

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

注销事件虽然进入现有可配置类型集合，但创建时的 in-app 偏好检查由 Notifications SQL owner 按 Python truthiness 执行，缺少 `in_app` 默认启用；静音时不会新建 Notification 或 outbox 行。通知 payload 不包含用户填写的注销原因。

### 自助注销的 durable delivery

`user.deactivation_requested` 与 `user.deactivation_completed` 由 Identity 生命周期事务通过 `aap_notifications_v1.create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])` 交给 Notifications owner。该 capability 只创建两种固定业务事件及其 `notification_delivery_outbox` 行，并按 operation、recipient、type 去重；通知删除通过外键级联抑制尚未领取的投递。不会建立另一个通知队列或把 payload 拼到 API 响应中。

独立 `notification_publish_client` LOGIN 仅有 `identity_delivery_policy()`、`claim_identity_deliveries(jsonb)` 和 `finish_identity_delivery(jsonb)` 三项能力，不能查询或更新 Notification 表，也不持 Identity/AUTH/mail 凭据。领取 transaction 提交后才向 Redis `notify:{user_id}` 发消息；只有 Redis 接受以后才能确认 lease。锁等待后的时间由数据库检查，旧 worker lease 不能确认新领取。Redis 未确认或 publisher 崩溃时允许下一次 claim 重试；消息使用同一 Notification id，投递是 at-least-once 而非 exactly-once。

独立命令 `aap-notifications-publisher` 或 `python -m aap_notifications.delivery_worker` 由 Notifications 包持有，使用原 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` 和 `NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`，没有 HTTP 监听。`apps/notifications/Dockerfile` 定义独立、锁定、非 editable 的运行工件；Identity 原导入路径和 `python -m aap_identity_runtime.notification_worker` 保留兼容转发，现有 Identity Compose 仍使用该入口。主 API/Celery 的每 10 秒 relay 直接复用 Notifications executor；owner 内部故障在 API 外层仍翻译成既有 Identity 错误。两种执行路径共享 lease 和同一 durable outbox，不改变至少一次投递、取消或确认顺序。详见 [Identity 私有协议](/api/guides/identity-runtime#注销通知的-durable-publisher) 和 [ADR-0121](/dev/adr/0121-identity-self-deactivation-and-notification-delivery)。

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

`mark_read` / `mark_all_read` 经 read-state owner、`delete_for_user` / `clear_read` 经原 SQLAlchemy
命令只在调用方事务内改行；`notifications.sync`（`reason=read` / `reason=deleted`）由 API handler 在
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

各作业 kind 的**领域终态账本**（`MaskQCRun` / `PointCloudQualityRun` / `MaskRepairBatch` / `MaskFormatImport` / 在线训练与评估作业）的正规更新仍在各自 worker 或内部回调路由内。兜底路径——worker 进程 crash、Celery revoke、未被 task body 捕获的异常——由 `workers/signals.py` 查作业后调用 `application/job_terminal.py`，走统一骨架（连接 → 按 `celery_task_id` 查 `async_jobs` → 落终态 → 回填领域账本 → 通知 → 提交 → 释放）；连接、提交和释放仍由信号适配器控制，HTTP 硬取消复用同一领域回填绑定。

`services/async_job_terminal.py` 只定义不可变 `JobTerminal` 事实，各领域服务的 `reconcile_job_terminal` 负责自己的行锁和状态：mask 格式导入取消保留 `partial`，回滚失败保留 `rollback_failed`，修复取消按已完成分片决定 `partial` / `cancelled`。通用状态、领域账本和持久通知沿用调用方同一事务，通知发布时点没有改为提交后。评估和主动学习由各自中断处理器维护取消意图、失联状态与 attempt fencing，不经过通用终态强制转换。迟到失败不覆盖已结束作业；未知 `celery_task_id` 静默跳过，没有领域账本的 kind 仍可更新通用行，通知继续遵循自身白名单。

HTTP 取消入口委托 `application/job_cancellation.py`，领域账本写入由各自 `request_job_cancel` 持有。修复和格式导入的 pending 作业立即落取消及通知，running 作业只写取消请求，实际终态由执行者确认；评估继续保持非终态取消意图。非强制撤销通过 `JobExecutionControl` 装配 Celery 适配器，发布异常仍按原策略记录并继续相应的状态处理。通知白名单、去重、持久化和发布顺序均未改变。

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

| 你想改什么       | 先看哪里                                                                                                       |
| ---------------- | -------------------------------------------------------------------------------------------------------------- |
| 新增业务动作审计 | `services/audit.py` + 对应 route                                                                               |
| 让某动作可通知   | `services/notification.py` + 对应 route                                                                        |
| 通知偏好开关     | `api/v1/notifications.py` + `aap_notifications.preferences` + `aap_notifications_v1.preference_command(jsonb)` |
| 标记通知已读     | `services/notification.py` + `aap_notifications.read_state` + `aap_notifications_v1.read_state_command(jsonb)` |
| 后台任务终态通知 | `services/async_job_notify.py` + 对应 worker/API 终态点                                                        |
| 作业终态领域账本 | 各领域 `reconcile_job_terminal` + `application/job_terminal.py` 绑定                                           |
| 审计查询 / 导出  | `api/v1/audit_logs.py`                                                                                         |
| 批次审计聚合视图 | `api/v1/batches.py:list_batch_audit_logs`                                                                      |

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
