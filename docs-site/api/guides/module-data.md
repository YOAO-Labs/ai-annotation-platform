---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 同库事务数据接口

这些接口供可信后端进程使用，不是终端用户 REST API。调用方仍负责身份认证和用例准入；
初始操作接受时还会在数据库内校验当前项目管理权。持有函数执行权限不等于获得任意用户的项目管理权限。

实现与冻结合同位于 `apps/api/alembic/sql/0190*` 至 `0195*`。接口通过 PostgreSQL
协议调用，使用调用方同一连接的外层事务，不接收 Python Session、ORM 或任意 SQL。
安装、独立角色及本地验收命令见仓库 `DEV.md`；数据所有权见
[ADR-0092](/dev/adr/0092-transactional-module-data-capabilities)。

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

完整模块的其余操作、运行时与凭据仍在迁移，总体进度见[模块部署台账](/dev/concepts/module-deployment)。
