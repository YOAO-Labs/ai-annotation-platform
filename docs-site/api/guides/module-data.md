---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 同库事务数据接口

这些接口供可信后端进程使用，不是终端用户 REST API。调用方仍负责身份认证和用例准入；
初始操作接受时还会在数据库内校验当前项目管理权。持有函数执行权限不等于获得任意用户的项目管理权限。

实现与冻结合同位于 `apps/api/alembic/sql/0190*` 至 `0205*`。接口通过 PostgreSQL
协议调用，使用调用方同一连接的外层事务，不接收 Python Session、ORM 或任意 SQL。
安装、独立角色及本地验收命令见仓库 `DEV.md`；数据所有权见
[ADR-0092](/dev/adr/0092-transactional-module-data-capabilities)。

HTTP 数据集路由对 API key 按操作检查 `datasets:read` / `datasets:write` scope；现有 JWT
授权行为保持不变。SQL 能力执行权不代替 HTTP 身份、scope 或当前项目管理权校验。

## 操作与职责

| 操作                                      | 输入                                         | 返回与约束                                             |
| ----------------------------------------- | -------------------------------------------- | ------------------------------------------------------ |
| `aap_workspace_v1.lock_project_reference` | project UUID                                 | 当前 UUID 或 null，持有引用锁；不判断当前用户权限      |
| `aap_assets_v1.lock_dataset_reference`    | dataset UUID                                 | 当前 UUID 或 null，持有父行锁                          |
| `aap_assets_v1.lock_current_association`  | dataset、project UUID                        | 当前关联 UUID 或 null，锁定父行、项目引用和关联        |
| `aap_assets_v1.lock_append_sources`       | dataset UUID、item UUID 数组                 | 当前关联与素材的最小 JSON 快照，供 Labeling owner 使用 |
| `aap_labeling_v1.append_dataset_items`    | dataset UUID、item UUID 数组、operation UUID | 创建尚不存在的 Task，同事务更新计数和保存冻结结果      |

专用追加调用角色只需最后一个操作的执行权限；模块函数 owner 通过必要的引用接口
组合其实现。不得授予调用方其他模块的私有表读取权来代替合同。

## 追加请求与结果

同一操作的 dataset 与排序去重后的素材 ID 集合固定后不能更换；重排或重复同一素材
ID 不改变规范请求。调用方在首次发送前生成 operation UUID，响应丢失时使用原 ID
重放。每次更换 ID 代表新操作；已有 Task 会被跳过。

```sql
BEGIN;
SELECT aap_labeling_v1.append_dataset_items(
    '11111111-1111-1111-1111-111111111111'::uuid,
    ARRAY['22222222-2222-2222-2222-222222222222'::uuid],
    '33333333-3333-3333-3333-333333333333'::uuid
);
COMMIT;
```

函数返回 `aap-task-append/v1` JSON，完整 schema 是
`apps/api/alembic/sql/0191_task_append.schema.json`，所有对象拒绝未知字段：

| 字段                         | 含义                                                     |
| ---------------------------- | -------------------------------------------------------- |
| `schema`                     | 固定 `aap-task-append/v1`                                |
| `operation_id`、`dataset_id` | 本操作身份，调用方必须核对                               |
| `created`                    | 本操作实际新建任务数；同素材关联多个项目时可能大于素材数 |
| `project_totals`             | 被增量更新的项目 UUID 与任务总数快照                     |
| `associations`               | 首次执行时持有的关联 UUID 与项目 UUID                    |

结果是冻结回执。重放可以在后续解绑或计数变化后返回原结果，不能用历史总数覆盖当前
投影。空数组、缺失 dataset、非本 dataset 的素材或没有当前关联时不建题，返回零；
已完成操作的重放仍返回其原始结果。原上传/扫描入口每次调用使用新操作 ID，因此重复
同步追加继续返回零，不改变公开 REST 响应。

## 事务、错误与恢复

来源素材插入、任务、计数和回执可以在同一外层事务共同提交或回滚。函数不提交，
调用返回也不代表外层提交已经成功。当前验收使用 `READ COMMITTED`；行锁等待至
调用方事务结束。多 dataset 操作的调用方必须保持一致锁序。

| SQLSTATE / detail code           | 含义与处理                                             |
| -------------------------------- | ------------------------------------------------------ |
| `22023` / `invalid_request`      | 身份、数组参数或数组元素为 null；修正输入              |
| `22023` / `operation_conflict`   | 同操作 ID 对应不同规范请求；不得覆盖原请求             |
| `55000` / `operation_incomplete` | 已有回执缺少完成结果；拒绝继续写入并排查数据           |
| PostgreSQL 锁、连接或序列化错误  | 沿用数据库错误；确认外层回滚后，按既有策略重试整个事务 |

接口不自动重试，也不在锁超时后跳过素材。连接或响应中断时不能仅凭超时推断提交失败；
使用原操作 ID 和原请求核对结果。外部对象存储不参与这个 SQL 事务。

## 初始关联与分块建题

新操作使用固定 `schema`、布尔 `ok` 和成功 `data` 或失败 `error.code`。
失败不包含 Python 异常或任意 SQL；调用方必须回滚所属用例事务，再按稳定错误码处理。
完整请求和响应字段见 `0192_creation_authority.schema.json`、
`0193_task_creation_jobs.schema.json`、`0194_task_creation.schema.json`。

| 所有者    | 操作                                            | 输入与行为                                                                                       |
| --------- | ----------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Identity  | `aap_identity_v1.lock_active_actor`             | actor UUID；当前活跃身份事实，仅供 Workspace owner 组合                                          |
| Workspace | `aap_workspace_v1.lock_project_manager`         | actor/project UUID；当前管理权和媒体/scene 事实，锁住身份与项目配置                              |
| Assets    | `aap_assets_v1.associate_project`               | actor/dataset/project UUID；授权后创建或返回关联 UUID、现有标记、素材数和项目类型                |
| Assets    | `aap_assets_v1.lock_task_creation_items`        | dataset UUID、可空 item UUID 数组；最小素材事实，仅供 Labeling owner 使用                        |
| Jobs      | `aap_jobs_v1.create_task_creation_job`          | operation/actor/dataset/project/association UUID；创建版本化后台作业，job ID 等于 operation ID   |
| Jobs      | `aap_jobs_v1.find_task_creation_job`            | actor/dataset/project/association UUID；查找同关联代次的活跃作业，用于重复派发恢复               |
| Jobs      | `aap_jobs_v1.claim_task_creation_job`           | job UUID、非空 execution ID；领取 pending/running 作业，不复活终态                               |
| Jobs      | `aap_jobs_v1.finish_task_creation_job`          | job/operation UUID；从 Labeling 公开摘要核实完成和计数后记终态                                   |
| Jobs      | `aap_jobs_v1.fail_task_creation_job`            | job/operation UUID、稳定错误码；保留已提交计数和已有终态                                         |
| Jobs      | `aap_jobs_v1.cancel_task_creation_job`          | 当前 actor UUID、job UUID；当前管理者显式停止后续块                                              |
| Labeling  | `aap_labeling_v1.accept_dataset_task_creation`  | operation/actor/dataset/project/association/job UUID、mode、chunk size；当前授权并保存不可变许可 |
| Labeling  | `aap_labeling_v1.prepare_dataset_task_creation` | operation UUID、可空点云 frame proposals；冻结待建来源和 total，不建 Task                        |
| Labeling  | `aap_labeling_v1.read_dataset_task_creation`    | operation UUID；返回持久摘要和 next_chunk，不返回私有来源计划                                    |
| Labeling  | `aap_labeling_v1.advance_dataset_task_creation` | operation UUID、chunk index；按持久游标原子建题，返回冻结块回执                                  |

Jobs 的引用锁与进度操作只授予 Labeling owner，内部 helper 不授予调用角色。
调用角色只有选定公开操作执行权，不能直接访问用户、项目、素材、Task、作业或计划表。

接受顺序为：关联 → 可选后台作业 → 接受操作，同一外层事务提交后才派发。
`mode` 是 `generic` 或 `pointcloud`；chunk size 为 1–5000。点云 proposals 只含
`primary_item_id` 和 `{item_id, sensor_name}` 相机列表，素材路径从 Assets 合同取得。
普通点云标定保留原前置提交；nuScenes 导入仍由外层单事务提交。

同步关联使用关联 UUID 作为操作 ID，因此标定前置提交后的失败可在再次关联时
找到并恢复原操作。后台作业使用自己的操作 UUID；取消或失败作业不会被此同步路径复活。

执行器领取作业后用 operation ID 读取、准备并推进 `next_chunk`。每块 Task、点云链接、
实际项目计数、游标、Jobs 进度和回执共同提交。提交或响应不明时按原 operation/chunk
重放，不能跳到自己推测的下一块；新读取返回的是持久游标。来源追加不会改写已准备的
total，后续追加使用独立追加操作。冻结块回执的项目总数不能覆盖当前投影。

已接受操作在发起者停用或失去管理权后继续。每块仍检查原 association UUID 和作业
状态；解绑、重建关联或显式取消阻止后续块，已提交块保留。新操作始终重新授权。
这是 [ADR-0093](/dev/adr/0093-durable-task-creation-authority) 固定的许可语义。

`account_busy`、`project_busy`、`membership_busy` 表示当前授权繁忙；回滚后重试。
`association_changed`、`job_cancelled`、`job_not_active` 禁止继续旧操作；
`cursor_conflict` 应重新读取，`source_changed` 应排查来源，`operation_conflict` 不得覆盖请求。
Celery 对连接、锁和序列化暂时失败持续重试，不将它们转为成功建题结果丢失；正式失败
保留累计结果。未版本化旧作业以 `legacy_job_scope` 拒绝，不能从当前负责人补造原许可。
切换前先排空旧作业；需要重建时由当前管理者重新接受操作，不通过解绑删除已有任务来恢复。

## 项目删除与跨 owner 强事务

`DELETE /projects/{project_id}` 成功仍返回 HTTP 204。Workspace 的
`aap_workspace_v1.delete_project(operation_id, actor_id, project_id, audit_context)`
是该命令的事务协调者：它在调用方同一 PostgreSQL 连接和外层事务中组合各 owner 的
检查、清理、审计与回执，不在函数内部提交。Python 适配器只传 UUID 和 JSON，校验冻结
响应并提交原请求事务；它不把 ORM 对象或 SQL Session 传给其他 owner。

新命令先锁定当前活跃身份，再按稳定操作 ID 和项目串行化，随后锁定当前项目管理权并以
NOWAIT 锁定项目行。Learning owner 按既定优先级检查主动学习引用和保留的训练、模型、评估、
策略及数据版本；有保留引用时拒绝删除。通过检查后，Workspace 依次调用 Diagnostics、
Assets、Inference、Insights、Export、Jobs、Learning 和 Labeling 的私有数据所有者能力，
最后清除成员与模板来源引用、删除项目，并由 Audit owner 写入 `project.delete` 记录。
Audit owner 通过 Identity 的私有类型化 `lock_audit_actor` helper 获取当前锁定身份事实；只有 Audit owner
可以执行该 helper。
审计快照、删除计数和操作回执与这些删除处于同一事务；owner 清理函数不授予工作流调用角色。

函数返回 `aap-project-delete/v1`。成功结果包含 operation、actor 与 project UUID，删除的 Task/annotation
计数及 audit ID；失败返回稳定 `error.code`，由现有 Python/HTTP 边界恢复原状态与错误细节。Owner-specific
cleanup 函数只授予各自 `NOLOGIN` owner 和协调方需要的最小权限；受限 `LOGIN` 调用者没有其他 owner 的私有
表权限或清理 helper 执行权。

数据库异常或事务最终提交失败会使所有 owner 的变更、审计行和回执共同回滚。锁冲突返回稳定
`project_busy` 失败结果；其他未预期数据库错误继续中止事务。该命令不删除源对象存储字节或导出文件，
不发布或撤销 broker 消息。Jobs owner 在 SQL 中取消活动建题作业并解除 live Project 外键；这不是
Celery 撤销或对象存储清理。

回执不依赖 Project 外键，因而项目删除后仍保留。相同操作 ID 只有在 actor 仍是原活跃账号且
actor/project/operation 身份完全一致时才重放原结果；同一 ID 换 actor 或 project 返回
`operation_conflict`。仅拥有函数执行权限不代表有管理权：首次执行仍按当前账号和项目管理权
锁重新授权。需要处理不确定提交的原生调用方必须在首次发送前生成并保存稳定 operation ID。
现有 REST 路由仍按原行为处理缺失项目，HTTP 重试不会自动暴露原生回执重放接口。

迁移 0200 在 `async_jobs` 保存由数据库维护、不可重绑且无 Project 外键的
`retained_project_id`。项目删除后，作业以及关联通知仍使用原项目范围进行授权；项目 A 的作业不能变成
全局作业或转绑项目 B，现存全局作业也不能通过更新进入项目 A。合法版本化 `create_tasks` 作业的实时/保留范围
必须与冻结 payload 相符；有效 v1 作业若丢失必需范围，worker 只能持久化带 `job_scope_mismatch` 的终态失败，不能从 payload 重建范围。
旧未版本化作业与范围不一致均失败关闭，不做 payload 授权回填；取消后的迟到 worker 不会复活作业。

## Workspace 数据集解绑与清理回执

`DELETE /datasets/{dataset_id}/link/{project_id}` 成功仍返回 HTTP 200 和原有五个字段：
`deleted_tasks`、`deleted_annotations`、`deleted_batches`、`deleted_batch_ids`、`soft`。
可选的 UUID `Idempotency-Key` 请求头固定一次操作，成功响应回传同一值。自动重试或丢失响应后
重试必须复用原 key；SDK 允许显式传入，Web 确认窗口在本次重试中保留它。
Workspace 的 `aap_workspace_v1.preview_dataset_unlink(actor_id, dataset_id, project_id)`
提供只读删除数量预览；`aap_workspace_v1.unlink_dataset(operation_id, actor_id,
dataset_id, project_id, cleanup_bucket, audit_context)` 是有写入效果的事务协调命令。
调用方保留同一 PostgreSQL 连接和外层事务，预览与执行都复用 Labeling 的同一素材来源选择规则。

来源选择优先使用 `Task.dataset_item_id`；仅当旧点云 Task 没有直接来源时，才回退到
`TaskDatasetItemLink(role='primary_lidar')`。相机链接和真正无来源的 Task 不会被当成待解绑来源。
新命令锁定活跃身份并串行化 operation ID，然后按 Dataset → 当前项目管理权与排他 Project 行锁
`FOR UPDATE NOWAIT` → 原关联 generation → Labeling 批次再 Task 的顺序加锁。锁后会检查来源与
批次快照是否仍匹配；锁争用、来源漂移、主动学习引用或有保留意义的反馈/标注 lineage 都会在删除前
失败并回滚。Learning guard 同时检查所有轮次、所有项目的 pool member/candidate Task 引用；Labeling
guard 不允许静默删除人工反馈或外部标注 lineage。

通过检查后，Diagnostics 仅解除匹配 Task 的 bug report 引用，保留报告、评论和 Project 引用；Assets
为 Task 所有的 pyramid 记录对象 UUID、精确 prefix 和受信 bucket 到持久 cleanup outbox，并清理自有
SQL metadata；Jobs 只取消与 Dataset、Project 和原关联 UUID 完全一致的版本化待处理/运行中建题作业；
Labeling 删除被解绑 Task 的评论、draft、prediction、prediction metadata、annotation、lock 和 Task，
保留 feedback，并在同一事务内重算 Project 与 batch 投影；Assets 最后只移除锁定的关联 UUID 代次。
Audit 写入 `dataset.unlink`，Workspace 保存带 operation、actor、Dataset、Project、association、
删除计数、audit ID 和 cleanup bucket 的不可变回执。owner 的清理、作业取消、投影、审计及回执共同
提交或共同回滚。

operation 回执的规范请求包含 operation ID、actor ID、Dataset ID、Project ID 与受信 cleanup bucket；
审计传输上下文不参与请求身份。重放要求原 actor 仍活跃且规范请求一致，不要求重新取得管理权；同 ID
换请求返回 `operation_conflict`。REST 入口仍要求当前项目管理权，原生回执的重放许可不绕过该入口检查。Bucket 由可信服务配置传入，不是终端用户字段。

解绑事务内不执行 S3 或 broker 操作。提交后，API 通过媒体作业端口派发清理任务，cleanup worker 和周期重试处理未完成的 outbox
条目。它仅按记录的 asset UUID、prefix 和 bucket 做幂等删除及确认；配置 bucket 与记录不一致时拒绝
执行。源对象字节、item-owned pyramid 和共享 pyramid 不会被此操作删除。独立 cleanup LOGIN 才能读取
待处理并确认完成，workflow LOGIN 不能扫描 outbox 或读取其私有表。当前受限角色使用隔离
专用队列验收；默认 Compose 仍是宽权限 worker 的共享 `cleanup` 队列，部署受限 worker 前须先拆队列。

`active_learning_reference`、`task_feedback_retained` 和
`annotation_reference_retained` 表示证据必须先保留或经明确的领域流程处理；`source_in_use` 表示数据库
仍有其他受限来源引用。锁/序列化争用返回 `project_busy`，完整回滚后重试整个命令；同 operation ID
对应不同 Dataset、Project、actor 或 bucket 时返回 `operation_conflict`，不能覆盖原回执。

数据集解绑是 Workspace 的同库事务切片，不代表 Workspace 或各参与 owner 已能独立部署。Dataset 与
DatasetItem 的公开删除命令仍未迁移；0201 Learning guard 和后续 owner 引用能力目前是这些删除边界的
私有基础，不是完整删除操作。身份/成员生命周期、其他模块操作以及完整运行工件和凭据仍待完成；总体
状态见[模块部署台账](/dev/concepts/module-deployment)、[ADR-0094](/dev/adr/0094-transactional-project-deletion)
和 [ADR-0095](/dev/adr/0095-transactional-dataset-unlink)。
