---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# 同库事务数据接口

这些接口供可信后端进程使用，不是终端用户 REST API。调用方仍负责身份认证和用例准入；
初始操作接受时还会在数据库内校验当前项目管理权。持有函数执行权限不等于获得任意用户的项目管理权限。

实现与冻结合同位于 `apps/api/alembic/sql/0190*` 至 `0219*`。接口通过 PostgreSQL
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

## 同事务的项目访问锁定事实

跨 owner 写入可在自身的同一 PostgreSQL 连接与读写事务中，调用
`aap_workspace_v1.project_access_step(actor_id, project_id, phase)`。它按固定 phase 返回
账号、完整项目行或完整成员行事实；返回信封绑定协议、actor、project 与 phase。
调用者在每一步后解码，再应用 Workspace 纯权限规则。它不返回授权布尔值，也不自行提交。

phase 只有 `lock_actor_share`、`lock_project_key_share`、`lock_member_share`。
依次保留账号 SHARE → 项目 KEY SHARE → 必要时成员 SHARE，并将锁保留到业务提交或回滚。
项目计数来自同一语句中的 Labeling 类型化能力。普通计数更新可以继续，所有权变更的排他锁
会冲突；不能改用更强的旧 manager SHARE 锁，也不能将远程权限响应当作跨进程的锁凭证。

争用保留 SQLSTATE `55P03`、`40001`、`40P01`，并提供固定的 account／project／membership busy
HINT；客户端按 phase 恢复原有冲突原因，不向外暴露数据库异常正文。应用当前迁移及
`module-data-grants.sql` 后，只有 Inference owner 增加这项固定执行权，没有新增 LOGIN 或原表授权。
完整合同与限制见 [ADR-0145](/dev/adr/0145-workspace-project-access-facts)。

## 项目集合锁与 Labeling 计数持久化

批次决策的有序 NOWAIT 项目集合锁由 Workspace 执行；Project 上的五项 Labeling
计数投影在原调用方 UoW flush 内通过窄补丁写入。两项能力共享原连接和事务，
不引入远程提交点。未知补丁字段及空计数补丁拒绝；丢失行和畸形回复不会留下部分
计数写入。完整字段、运行授权与验证边界见
[Workspace 运行指南](./workspace-runtime#排序项目集合锁与计数投影)及
[ADR-0172](/dev/adr/0172-project-set-locks-and-labeling-counters)。

生命周期与审核的增减使用三个独立的 `aap_labeling_v1` 函数：

| 函数                                      | 返回                                                    |
| ----------------------------------------- | ------------------------------------------------------- |
| `decrement_project_review_count(uuid)`    | 饱和减一后的 review 整数；缺失为 null                   |
| `decrement_project_completed_count(uuid)` | 饱和减一后的 completed 整数；缺失为 null                |
| `record_project_review_approval(uuid)`    | completed 加一、review 饱和减一后的两个整数；缺失为零行 |

每项只执行一次原子 UPDATE，并写事务时间戳；保留正常行锁等待与原生 int32 溢出。
空 ID 仍尝试匹配零行的写入，因此 READ ONLY 仍返回 25006。调用方保留权限、
autoflush、缓存及 Task／Audit 的共同提交或回滚。函数不接收任意补丁、不增加连接或
自行提交。受限 API 与 Node 证据见
[ADR-0176](/dev/adr/0176-labeling-lifecycle-counter-deltas)。

## actor 项目成员事实

`aap_workspace_v1.actor_project_memberships(uuid)` 是 STABLE、STRICT 的只读关系，
返回项目 UUID 与原始职责文本；NULL 输入返回空集。既有未知角色也作为事实保留，
当前账号、职责和 viewer 配对校验继续由调用方在同一条 SQL 中完成。它没有锁、
日期解码或提交点。既有 `project_search_scope` 继续承载候选项目和元数据。

调用方使用相同连接组合范围、分页和计数；独立调用查询时也保留原 autoflush。
专用 owner、应用及 worktree grant 清单同步更新，PUBLIC 无新函数执行权。合同与
限定验收见 [Workspace 运行指南](./workspace-runtime#项目范围看板与列表读取)和
[ADR-0173](/dev/adr/0173-workspace-actor-scope-relations)。

## 非锁定成员事实关系

Workspace 拥有 `aap_workspace_v1.project_member_facts` 安全屏障视图，公开固定九列
成员事实；类型、列顺序及行键见 `aap-workspace-read-relations/v1`。它保留原生 UUID、
文本、整数和带时区日期，不做 JSON 全行解码或成员资格判断。客户端只申请 SELECT，
不获得视图写入或锁行权限；PUBLIC 无权限。

关系可在原 SQL 内联接、聚合及参与 Task UPDATE 的相关 NOT EXISTS。调用方继续
持有授权、账号校验和事务，保留各操作的不同规则。普通账号可显式应用
[Workspace 凭据边界](./workspace-runtime#普通消费者的-sql-凭据边界)；完整模块装配仍待验收。具体范围见
[运行指南](./workspace-runtime#非锁定成员集合与名册)和
[ADR-0174](/dev/adr/0174-workspace-nonlocking-member-facts)。

## Project 原生事实与计数依赖

Workspace 的 `project_facts` 公开 38 个原生字段，固定列顺序及精确类型由共享关系
描述声明；box/text 阈值保留实际的 PostgreSQL real 类型。完整读取以主键组合既有
Labeling `project_catalog_counters`，计数所有权和原 43 列行布局保持。两方都只授权
客户端 SELECT，PUBLIC 不获权限，不新增连接或提交点。

该关系支持原 SQL 内的联接、外联接、分组、相关谓词和分页，不预取全量行再在应用
过滤。原授权和各调用的 autoflush／缓存语义继续由调用方持有。接口与资格边界见
[Workspace 运行指南](./workspace-runtime#项目集合投影与元数据)。

## 同事务的主后端绑定清理

`aap_workspace_v1.clear_primary_backend_bindings(pool_id)` 只清空指向给定服务池的
项目主绑定，并按事务时间更新项目时间戳，返回影响行数。空 UUID 参数返回 SQLSTATE
`22023`；没有匹配项返回0。它使用调用方的同一连接与事务，不自行授权、提交或回滚，
不移除独立的项目服务池启用关系。

注册实例删除工作流在原有权威、保留和路由静默检查之后调用它，再记录审计并统一提交。
函数由 Workspace 持有，Inference owner 只有固定执行权，没有 Project 原表权限。
当前 API 协调器也获得这一个固定函数的显式执行权；部署时先迁移再应用对应 grants。
没有新增 LOGIN、HTTP endpoint 或 DSN。详见
[绑定清理边界](/dev/adr/0147-inference-pool-policy-and-workspace-bindings)。

## 注册实例删除的跨域读取

`aap_learning_v1.registry_evaluation_reference_exists(registry_id)` 检查是否存在引用该实例
部署的样本评估，不按评估或部署状态过滤。
`aap_jobs_v1.count_running_registry_predictions(registry_id)` 返回 payload 精确指向该实例、
kind 为 `batch_predict`、status 为 `running` 的作业数。两者分别返回布尔值和 bigint，
空 UUID 参数返回 SQLSTATE `22023`。

删除工作流在原事务中按 Learning → Jobs 的顺序调用，再检查 GPU 和路由状态；
这些只读函数不锁定其他业务行、不自行提交，也不能代替 GPU 或路由租约证明。
函数由各领域持有，Inference 和当前 API 协调器只有显式固定执行权。
部署先应用当前迁移，再应用 owner 和实际 API 角色的 grants。详见
[传输与删除依赖边界](/dev/adr/0148-inference-backend-transport-and-deletion-dependencies)。

## Workspace 后端快照探测

`aap_inference_v1.workspace_configuration_backend(registry_id)` 保留原读取时点，
只向 Workspace 返回 `{id, probe_subject}`；空 backend 仍为 null。Subject 使用数据库内
私钥加密原后端快照，绑定数据库、registry 和仍在进行的原事务。Inference 的专用
`backend_probe_policy()`／`open_backend_probe_subject(text,uuid)` 能力打开快照，
不重新读取当前 registry；Workspace 角色没有开封或私钥表权限。

这让独立 Inference 进程可以探测调用方尚未提交的后端，以及凭据轮换前已选定的配置，
无需额外业务提交，也不在外部 HTTP 前锁 registry 行。事务提交或回滚后，重复开封失败；
已经受理的外部请求不会被追溯取消。备份保留私钥；新克隆库须生成自己的新 subject，
原数据库 subject 不能跨库使用。权限脚本和错误相位见
[Inference 运行配置](/api/guides/inference-runtime#workspace-的后端能力探测)。

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

## 视频退役与删除围栏

项目删除和项目内数据集解绑遇到未退役视频执行时，都返回 HTTP 409：

```json
{
  "detail": {
    "code": "video_execution_not_retired",
    "message": "存在尚未退役的视频执行，请先取消并完成退役后重试"
  }
}
```

先取消需要停止的作业，并等待现有 worker 完成物理退役后重试。业务状态显示完成、
失败或取消时，退役仍可能未完成；判据为当前 run 匹配的 Jobs journal 退役证明。
证明成立后，lease 尚未到期也可删除。拒绝保留原 Task、Job、输入、Mask 运行根及
检查点，不创建删除回执或清理 outbox。锁竞争继续返回 project_busy，回滚后重试。

Labeling 按原删除足迹锁批次、Task、Annotation，包含跨 Task 的 Annotation 外键与
来源 sidecar；Jobs 再按 UUID 批量锁 Job、execution、journal。新准入不能越过这些
外键和执行围栏，保留的输入 scope／日志也能找回已有孤儿。

从未启动且没有执行账本的 Job 可删除，Jobs 在原事务中保存私有未启动证明，避免
其保留输入快照误挡后续删除；该证明不能替代任何已有 execution 的退役证明。
已启动但无账本、或已成孤儿且无证明的旧记录继续拒绝，需要独立排空／修复入口。
降级遇到已提交的未启动证明时返回 55000 并保留对象，防止销毁证据。

运行权限保持内部 Workspace → Labeling → Jobs；应用及两个独立命令账号没有 helper
或证明私表权限。现有命令、回执和公开成功响应保持。设计与限定本地证据见
[ADR-0178](/dev/adr/0178-video-retirement-deletion-fences)。

## Workspace 数据集解绑与清理回执

`DELETE /datasets/{dataset_id}/link/{project_id}` 成功仍返回 HTTP 200 和原有五个字段：
`deleted_tasks`、`deleted_annotations`、`deleted_batches`、`deleted_batch_ids`、`soft`。
可选的 UUID `Idempotency-Key` 请求头固定一次操作，成功响应回传同一值。自动重试或丢失响应后
重试必须复用原 key；SDK 允许显式传入，Web 确认窗口在本次重试中保留它。
Workspace 的 `aap_workspace_v1.preview_dataset_unlink(actor_id, dataset_id, project_id)`
提供不删除业务数据、但会锁定当前权限的数量预览；`aap_workspace_v1.unlink_dataset(operation_id, actor_id,
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

解绑事务内不执行 S3 或 broker 操作。独立 `apps/assets-cleanup` 进程轮询已提交的 outbox，
按精确 asset UUID/prefix/bucket 删除和确认；API 不再派发清理 Celery 消息。源对象字节、item-owned
pyramid 和共享 pyramid 保留。专用 cleanup LOGIN 只能执行新的 bucket/keyset pending 操作和完成确认，
workflow LOGIN 不能领取或确认 outbox，也不能读取其私有表。

`active_learning_reference`、`task_feedback_retained` 和
`annotation_reference_retained` 表示证据必须先保留或经明确的领域流程处理；`source_in_use` 表示数据库
仍有其他受限来源引用。锁/序列化争用返回 `project_busy`，完整回滚后重试整个命令；同 operation ID
对应不同 Dataset、Project、actor 或 bucket 时返回 `operation_conflict`，不能覆盖原回执。

便携合同和 `DatasetUnlinkManager` 位于 Workspace 包；API 的兼容导入和原调用路径
保持。`DatasetUnlinkPostgresData(connection)` 使用 `workspace_dataset_unlink_client`
调用同一两项 Native 函数，只借用已开启事务，不自行提交、回滚或关闭连接。预览也会
获取权限锁，因此不能使用 READ ONLY 事务。该账号没有私表／列、序列、DDL 或 TEMP
权限；授权脚本为 `infra/docker/workspace-dataset-unlink-grants.sql`。

原始 JSON 拒绝重复键／非有限数，Python UUID／tuple 值继续支持。Node 等客户端处理
大于安全整数范围的 audit ID 时，应保留原始 JSON 或使用精确整数解码；回执字段仍是
整数。受限调用、失败提交、冻结重放和实际 API 证据见
[ADR-0177](/dev/adr/0177-workspace-caller-transaction-dataset-unlink)。

数据集解绑的调用边界和上述退役门禁已实现；旧作业无证明的排空／修复仍开放，
完整 Workspace 和各参与 owner 的工件／凭据继续按台账验收。
公开来源删除、上传与独立清理见下文；完整模块状态见[模块部署台账](/dev/concepts/module-deployment)。

## Assets 来源删除

`delete_dataset(operation_id, actor_id, dataset_id, cleanup_bucket, audit_context)` 和
`delete_dataset_item(operation_id, actor_id, dataset_id, item_id, cleanup_bucket, audit_context)`
返回 `aap-dataset-source-delete/v1`。完整字段、计数和错误见
`0209_dataset_source_deletion.schema.json`。成功回执记录删除的数据集、条目、场景、关联数，
取消的原关联建题作业、废弃的未完成上传、排队的派生清理及审计 ID；不返回任意存储路径。

新命令要求当前活跃素材库管理员（super_admin/project_admin），沿用全局素材库准入，不改成项目
负责人或 created_by 校验。新鲜身份、Dataset 排他锁及有序 Scene/Item/association/intent 锁先于检查和
级联删除。Learning 保留 member/candidate 来源；Labeling 保护 Task 主来源、所有素材链接、sensor
标注、轨迹、quality 和 video 执行记录。完整 Dataset 删除只解除 TaskBatch 的数据集归属，不删除批次。
Item 删除遇当前关联仍接受/运行中的建题返回 `creation_in_progress`；Dataset 删除则显式取消匹配原
association 的建题。活动导入或不能证明无关的旧导入 scope 返回 `source_busy`，不改造旧 payload。
导入接受与 JSON-only 质检记录分别取得 Dataset/Scene 来源锁，不能绕过删除检查提交迟到引用。

`source_in_use`、`active_learning_reference` 或 `creation_in_progress` 返回 HTTP 409；锁争用为
`source_busy`，换请求复用操作 ID 为 `operation_conflict`。所有 owner 写入、审计和 Assets 回执与调用方
提交/回滚一致。原始 source bytes 始终保留；只有 item-owned 金字塔进入精确 outbox。
REST 两个 DELETE 保持 204 空响应，缺失/父子不匹配为 404。UUID `Idempotency-Key` 可选且成功回传；
同次重试使用原 key。原生回执允许原 actor 仍活跃时重放，REST 的当前管理员检查继续生效。

## Assets 暂存上传

`reserve_dataset_upload(item, actor, dataset, file_name, content_type)` 保留未来 Item UUID，返回
`aap-dataset-upload/v1` intent facts，不创建 DatasetItem、不增加 file_count、不参与建题。
原生调用者自己保存 UUID，可对相同待完成规范请求重放；REST upload-init 每次保留一个新 UUID。
文件名必须是最多 500 字符、897 个 UTF-8 字节的安全相对路径（完整对象 key 不超过 1024 字节），拒绝绝对路径、空段、`.`/`..`、反斜杠和控制字符。

PUT 只签发到 import 桶 `dataset-uploads/{item}/source`。`lock_dataset_upload(actor,dataset,item)`
在当前活跃原发起者和父 Dataset/intent 围栏内，为每次 pending 尝试生成独立 publication UUID，目标为
`dataset-sources/{dataset}/{item}/{publication}/{file_name}`。这条锁在调用方最终提交前保持。
已完成 intent 返回冻结结果且不旋转路径。父子不匹配仍按缺失处理，不补造旧记录的上传归属。

调用方 HEAD staging，读取尺寸时使用 `IfMatch`，并通过 `CopySourceIfMatch` 将同一 ETag 条件拷贝到
冻结目标。已知内容重复可通过 `find_dataset_upload_duplicate` 提前避免拷贝，但最终判定仍在
`complete_dataset_upload(actor,dataset,item,publication,size,hash,width,height)` 内完成。该公开 SQL
命令原子组合私有接受、Labeling append 和私有完成，外层提交前不会暴露半登记的 Item。
已完成重放返回原 `{item_id,linked_tasks}`，不会再次拷贝或追加。私有 accept/finish 不授权给 workflow。

缺失 staging 通过 `discard_dataset_upload(...,'missing')` 废弃 intent；重复仅保存 duplicate_of，
不删除已有 Item/对象。旧无 intent 占位记录保留非破坏性完成兼容，缺失或重复不删除旧来源。
临时 staging 按 import 生命周期过期；未完成 intent 与不确定提交留下的原始拷贝目前保留，
需要计入容量，后续回收必须另有持久 publication 证据和迟到 I/O 围栏。

## 独立派生清理

`pending_storage_deletions_v2(bucket,after_created_at,after_operation,after_asset,limit)` 仅返回指定桶的
已到期 outbox，limit 为 1–1000，游标三个值必须全部为空或全部存在。结果按
`(created_at,operation_id,asset_id)` 排序并返回下一页游标所需的 created_at。失败记录之后的页仍可继续，
轮询下一轮从头重试。`complete_storage_deletion(operation,asset)` 成功后可幂等确认；提前确认返回 false。

删除生产者捕获 building generation 的最大 lease 到 `not_before`；新 pending 和完成确认都要求该时刻
已到。数据库读取事务在 S3 操作前结束，确认另用短事务。对象删除失败、响应丢失或确认失败不会移除
未完成责任。进程显式配置 cleanup DSN 和 S3 专用凭据，只允许一个桶中 `image-pyramids/*` 的
ListBucket/DeleteObject，不继承 API、Redis 或 Celery 配置。配置、独立镜像和本地 Compose 入口见
仓库 `DEV.md`、`apps/assets-cleanup/README.md` 与 [ADR-0096](/dev/adr/0096-assets-source-deletion-and-cleanup)。

## 目录与关联读取

目录五项读写命令及两个关联查询由独立 Assets 工件提供；公开网关保留 JWT/API key 的认证与各路由既有 scope，原生操作再检查当前身份。配置、幂等写入、响应预算和故障切换见[独立素材目录](/api/guides/assets-catalog)。

`list_dataset_projects(actor,dataset)` 和 `list_project_datasets(actor,project)` 返回冻结 `aap-assets-associations/v1`，完整结构见 `0212_asset_association_reads.schema.json`。两者均为 STABLE 读取，按最外层 SELECT 的同一个 MVCC 快照组合身份、关联、配置与计数，不调用写入围栏或持有行锁。快照开始后的撤权和解绑不改变已开始读取的结果，下一条 READ COMMITTED 语句读取新状态。

Identity 只提供 actor UUID、平台角色和活跃状态；Workspace 提供原始项目配置及当前可见性；Labeling 提供持久项目计数和按直接来源分组的任务数；Assets 持有目录、条目与关联。Owner 间只授予版本化投影/函数权限，不授予其他 owner 私有表。Catalog LOGIN 执行目录、关联与空间素材的 29 个公开 Assets 操作，不能读取这些投影。

## 旧上传条目的完成与回执

已有 DatasetItem 没有上传 intent 时，不能从当前账号补造历史上传者。Assets 的 `lock_legacy_dataset_upload(operation,actor,dataset,item)` 在当前活跃身份、Dataset 与 Item 围栏内返回最小来源快照；`complete_legacy_dataset_upload(operation,actor,dataset,item,expected_path,size,hash,width,height)` 校验同一来源后更新元数据、调用 Labeling 追加任务，并保存 Assets 自己的完成回执。

调用方在同一个外层事务中持有锁，期间只读取既有来源的 HEAD/有限图像头，不复制或删除旧字节。文件缺失或内容重复不清除旧行、对象或人工证据。成功时元数据、任务、计数、Labeling 回执和 Assets 回执共同提交；最终提交失败则共同回滚。Labeling 的可复用追加 ID 不能单独证明 Assets 已完成上传。

`dataset_legacy_upload_operations` 没有 live Dataset/Item/账号外键，也不自动过期。重放仍检查当前活跃身份、父子存在关系和当前来源路径/类型；不同规范请求复用操作 ID 返回 `operation_conflict`，来源替换返回 `source_changed`，不会按历史回执改回当前来源。状态为 pending/completed/duplicate，完整字段及错误见 `0213_assets_source_runtime.schema.json`。在 0215 阶段，独立来源进程的专用 LOGIN 精确执行 14 项能力，即五项现代上传、两项删除、两项旧上传和五项 ZIP/扫描能力，不读取业务关系。

## 同步 ZIP 与对象桶扫描

0215 将同步 ZIP 和旧前缀扫描交给既有 Assets Source HTTP 工作流，公开 REST 仍由 API 认证、校验 scope 和角色，并保留原同步响应。五项数据能力及固定签名为：

| 能力                            | 签名                                |
| ------------------------------- | ----------------------------------- |
| `begin_dataset_ingest`          | `(uuid, uuid, uuid, jsonb)`         |
| `prepare_dataset_ingest_batch`  | `(uuid, uuid, uuid, uuid, jsonb)`   |
| `complete_dataset_ingest_batch` | `(uuid, uuid, uuid, uuid, jsonb)`   |
| `read_dataset_ingest_layout`    | `(uuid, uuid, uuid, uuid, integer)` |
| `finish_dataset_ingest`         | `(uuid, uuid, uuid, jsonb)`         |

前 3 个 UUID 依次绑定操作、当前 actor 与 Dataset；prepare/complete 还绑定稳定的 batch UUID。ZIP 请求摘要绑定原始归档 SHA-256，扫描绑定协议固定的扫描命令指纹。原生操作重新检查当前活跃身份；ZIP 只要求公开 API 已认证账号和 `datasets:write`，扫描另要求既有 `super_admin` / `project_admin` 管理权限。所有 preparing 阶段调用使用同一个调用方事务 ID；若 preparing 状态意外提交，后续调用会失败关闭。

`dataset_source_ingest_operations`、`dataset_source_ingest_entries` 与 `dataset_source_ingest_deliveries` 均归 Assets 所有，不保存 live Dataset/Item 外键。分块最多 200 项，Item、Dataset 计数、Labeling append 回执、ZIP Scene/frame 变更、最终收据和媒体派发意图都在一个外层事务内提交。完成收据保留用于原 actor、Dataset、操作类型和请求摘要的精确重放；对象存储写入不能随 SQL 回滚，ZIP 新对象因此使用不可变 `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}`，任何失败路径均不删除旧来源或新复制字节。只有 ZIP 做 Scene/frame 推断与时序断言，扫描只枚举 Dataset 名称的精确 legacy 前缀。

ZIP 的 `read_dataset_ingest_layout` 以 UUID keyset 分页返回至多 500 个 detached Item，并带有权威 snapshot digest/count；`finish_dataset_ingest` 会在写 Scene 与 frame assignment 前重验快照、计划归属和完整结算状态。扫描不调用这两项布局/Scene 能力。媒体 delivery claim 在派发前保存租约，ack 需校验同一活动 lease token 与当前 delivery target；此 outbox 提供可重试的 at-least-once 派发，不承诺 exactly-once。

完整 native JSON 合同见 `apps/api/alembic/sql/0215_dataset_source_ingest.schema.json`；纯 Python HTTP/wire 合同见 `apps/assets-source/src/aap_assets_source/ingest_contract.schema.json`。迁移按 `0215_dataset_source_ingest.sql` 后 `0215_dataset_source_ingest_delivery.sql` 的顺序安装。这五项入库能力属于 Source LOGIN 当前的 26 项能力；`claim_dataset_source_ingest_delivery` 和 `complete_dataset_source_ingest_delivery` 仅供 API maintenance relay 领取/确认派发，不授予 Source LOGIN。图像来源验证与作业受理的三个能力由 Source LOGIN 使用，详见下文。交付收据表示 broker 接受，不表示媒体已执行。

## 存储连接器管理

0217 为 Assets Source 增加九项连接器原生能力。Source LOGIN 当前还包含 `resolve_internal_image_source`、`schedule_image_media` 与 `read_legacy_dataset_upload` 三项图像来源/作业受理能力，共执行 26 项公开能力。独立连接器导入执行器使用九项 LOGIN 能力，其中额外两项用于与图片导入原子组合；它不能接受新的导入授权。两个角色都不能直接读取或写入业务表。

| 原生能力                          | SQL 参数签名                                                       |
| --------------------------------- | ------------------------------------------------------------------ |
| `list_storage_connections`        | `(uuid, timestamptz, uuid, integer)`                               |
| `read_storage_connection`         | `(uuid, uuid)`                                                     |
| `read_storage_connection_policy`  | `(uuid, jsonb)`                                                    |
| `capture_storage_connection`      | `(uuid, uuid, text)`                                               |
| `read_storage_connection_receipt` | `(uuid, uuid, text, uuid, jsonb)`                                  |
| `create_storage_connection`       | `(uuid, uuid, jsonb, jsonb, text, text, jsonb, jsonb)`             |
| `update_storage_connection`       | `(uuid, uuid, uuid, jsonb, jsonb, text, text, text, jsonb, jsonb)` |
| `delete_storage_connection`       | `(uuid, uuid, uuid, jsonb)`                                        |
| `record_storage_connection_test`  | `(uuid, uuid, uuid, text, text, jsonb, jsonb, jsonb)`              |

六个公开 REST 操作通过内部 `POST /v1/connections/{list,read,create,update,delete,test}` 调用 Source 工作流。API 网关保留 JWT/API key、公开角色和响应；本地模式复用同一工作流与调用方 SQL 连接，远程模式由 Source 持有事务，远程故障不回退到本地。列表在内部按有界页面读取并校验完整快照，公开响应仍是完整列表。

Source 在 `public.storage_connection_operations` 中保存不可变幂等回执，并与连接器变化和审计共同提交。create、update、delete、test 接受 `operation_id`，通过 API 的可选 UUID `Idempotency-Key` 传入；重放要求相同 actor、操作、资源和用户意图。create 令 `connection_id=operation_id`。历史兼容 `project_id` 不参与新记录归属，owner 连接器使用 `created_by`，其创建者即使降为 employee/viewer 仍可管理，global 连接器仅超级管理员可管理。Source 通过公开身份能力检查当前角色，不依赖调用方传入角色布尔值。

## 可信导入器数据集与素材登记

0218 为现有本地图片与 nuScenes 导入器发布独立的 `aap-assets-operator/v1` SQL 合同。它与 Source 的 26 项、Catalog 的 29 项和连接器导入执行器的 9 项 LOGIN 能力分开；专用 `asset_operator_client` LOGIN 被授予下面六项 Assets 函数。持有该 operator 凭据即获得此路径的受信任执行权限，owner UUID 只保留既有归属标记。图片导入沿用已有 owner 选择且没有新增 manager 门槛；nuScenes 保留既有 Catalog 资格检查和先锁 actor、再锁 Dataset 的顺序。函数不接收调用者传来的角色布尔值。

| 原生能力                           | SQL 参数签名               | 用途                                                                |
| ---------------------------------- | -------------------------- | ------------------------------------------------------------------- |
| `create_operator_image_dataset`    | `(uuid, uuid, text, text)` | 使用稳定操作身份创建图片 Dataset 并回传当前 Dataset 事实            |
| `ensure_operator_nuscenes_dataset` | `(uuid, uuid, jsonb)`      | 保留已有 Dataset 的名称、类型与创建者，返回 nuScenes 来源和升级事实 |
| `append_operator_items`            | `(uuid, uuid, jsonb)`      | 按请求顺序追加最多 500 个新 Item，并原子更新 file_count             |
| `patch_operator_items`             | `(uuid, uuid, jsonb)`      | 比较不透明来源快照，合并请求的 nuScenes metadata、尺寸、大小或哈希  |
| `recount_operator_items`           | `(uuid, uuid)`             | 从 Assets 当前 Item 集重算 Dataset 的 file_count                    |
| `read_operator_items`              | `(uuid, uuid, integer)`    | 按 Item UUID 游标返回有序 Item 页和稳定快照摘要                     |

每个写能力都接收操作 UUID；重放必须匹配原操作和请求。`dataset_operator_operations` 存储不可变回执且没有 live Dataset/Item 外键。批量追加/更新最多 500 项，请求 JSON 最多 8 MiB。读取每页最多 500 项，Item 记录 JSON 最多 15 MiB，完整响应最多 16 MiB；调用方跨页核对稳定摘要、总数、顺序、游标和完成标记，因此完整读取不受单页上限截断。source snapshot 是 opaque CAS 值，由 Assets 持有并在 patch 时重验；更新保留 Scene/frame、标定和无关 metadata，只改请求字段，尺寸只在目标相机尺寸不完整时成对回填。

本地导入器通过 `AssetsOperatorGateway` 将这些函数绑定到自己的 SQL Session；nuScenes 路径与既有 Labeling、Workspace 和空间写入共用外层事务，函数不自行提交。它们不是 Source HTTP 工作流，没有 `ASSETS_SOURCE_URL` 远程选择或错误回退。对象存储上传、nuScenes 文件写入及必要的既有对象覆盖不属于 SQL 事务，回滚保留原始字节。专用 operator LOGIN 不读业务表，也不继承其他角色。完整边界与部署顺序见仓库 `DEV.md` 和 [ADR-0104](/dev/adr/0104-operator-source-registration)；SQL schema 在 `apps/api/alembic/sql/0218_assets_operator.schema.json`。

## 持久连接器导入

Assets 的 `prepare_dataset_import`、`accept_dataset_import` 与 `cancel_dataset_import` 服务公开适配器；独立执行器只获得 claim、heartbeat、connection、manifest、next、complete-entry、finish 七项能力，不能接受新授权或读取其他 owner 关系。协议为 `aap-dataset-import/v1`，完整定义见 `0214_dataset_import.schema.json` 和随包的 `import_contract.schema.json`。

`dataset_import_operations` 保存已接受执行权、加密连接配置与凭据快照、预算、租约和聚合进度；`dataset_import_entries` 保存不可变清单、稳定条目 UUID 与逐文件回执。它们和 `dataset_import_deliveries` 没有 live 业务外键，也不自动过期。通用清理保留带该受理协议的 Jobs 回执，避免清理后原 UUID 无法重放。最多 16 MiB 的清单在复制前校验；源路径与文件名分别受目录安全、数据库字符及对象 key UTF-8 预算约束。

接受时仍校验当前活跃管理员与连接器使用权，prepare 持有连接行锁直到调用方完成主机/路径校验和接受提交。同一 UUID 重放绑定原用户意图并沿用首次预算。作业接受后使用已保存的连接器配置和加密凭据快照；删除连接器不会删除该快照或撤销已接受的执行权。执行不重新授予原受理者权限；显式取消、作业终态、Dataset 与 lease/token 在 SQL 中逐文件围栏。每次复制使用新的 publication 路径，原始及不确定拷贝保留。

Item、文件数、Labeling append、游标/计数、Jobs 进度和媒体派发意图共同提交或回滚。Jobs 终态经 Notifications 能力持久化通知；Settings 的当前白名单通过其版本化能力读取。两个新 NOLOGIN owner 不读取 Identity 私表，Identity 存在事实由既有公开能力提供。

后续派发使用 `aap-dataset-import-delivery/v1`：领取在实际传输前提交，失败保留租约供重试，成功再确认；原始 SQL/schema 包含 wall-clock 期限检查。该回执只表示传输被接受，不证明既有媒体计算完成。迁移拒绝活动旧导入，不能为其推导受理证据；先排空或取消再升级。操作和运行限制见 [ADR-0100](/dev/adr/0100-durable-connector-import)。

## 空间素材状态与同事务组合

场景、帧组织、位姿、标定与 nuScenes 标定 provenance 使用二十项公开原生能力；完整合同见[空间素材协议](./assets-spatial)。Assets 新增不可变 `dataset_spatial_operations`，不保存 live-resource 外键。Catalog LOGIN 共 29 项 execute-only 能力，Source LOGIN 共 26 项。

Assets 只通过 Labeling 当前相机/共享与受理范围、Workspace 当前项目访问和 Audit 事件能力组合跨 owner 状态，不读取其他 owner 私有表。标定更新与审计、回执、历史修订和元数据共同提交；需要同事务的建题导入继续在调用方同一 SQL 连接上组合公开能力。读取标定文件前不增加空间状态行锁，最终应用再检查来源快照和已受理作业/关联代次。普通远程故障不触发本地回退。

## 受理视频输入与帧计划

`aap-assets-video-input-execution/v1` 定义 24 项原生能力，完整参数与值模型固定在
`apps/api/alembic/sql/0225_video_inputs.schema.json`。它冻结完整任务清单、原始视频对象身份、采样策略和所选帧时间；成员全部准备完成后才封存结果摘要。

| 调用方        | 能力数 | 范围                                                |
| ------------- | -----: | --------------------------------------------------- |
| 工作流        |      1 | 原始 Job 同事务清单捕获                             |
| Media control |      9 | 清单读取、准备、封存，原始输入/时间读取和按需帧请求 |
| 视频 worker   |      9 | 输入和帧的租约、心跳、时间页、精确对象发布与失败    |
| Labeling      |      2 | 同事务输入/计划和精确帧写入围栏                     |
| 系统维护      |      1 | 有界保留扫描                                        |
| Cleanup       |      2 | 视频尝试前缀领取和确认                              |

请求最多 64 KiB、响应最多 2 MiB，清单与时间页面最多 100 项。原始视频和派生帧都绑定真实对象版本与 SHA-256，不用当前 URL 替换缺失版本。重复准备返回原回执；输入完成不批量创建所有帧记录。锁争用回滚部分变更并返回可重试错误。

Labeling 的源检查必须和当前 Task/批次/编辑锁、实际执行作业权威一起使用。历史原始 Job 一致性不代替当前重试 Job；合法历史读取和后续采纳可以继续使用原输入。强类型视频证据及 Annotation 引用合同固定在 `0225_video_input_evidence.schema.json`。同项目的受信任标注复制保留原始来源 Task，直接附加新证据仍需匹配目标 Task。仍被有效候选、可重试失败或标注引用的输入不会回收；无引用且原作业终止后先保留 30 天，回收只领取原始/帧尝试的精确前缀。

原生账本、租约、证据、十项 HTTP 控制、独立视频 Worker 和 Cleanup 已完成隔离的本地数据库及真实 S3 联调；业务预测消费者尚未接通。完整切片状态见 [ADR-0109](/dev/adr/0109-retained-video-inputs-and-frame-plans)。

## 普通批量结果检查点

`aap-preannotation-results/v1` 用单个 JSONB 请求和封闭成功／错误信封提供十七项能力。冻结模式见 `apps/api/alembic/sql/0231_preannotation_results.schema.json`，可独立安装的客户端、编解码器和 CLI 位于 `apps/preannotation-results`。它使用标准 PostgreSQL 传输，无需交换 Python Session 或 ORM 对象。

| 所有者   | 执行角色可调用                                                                                                       | 仅业务写入方可调用                       |
| -------- | -------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| Jobs     | claim、heartbeat、release、stage/read members、start execution/member、mark overwrite、skip member、finish execution | owner 内部的成员状态与完成证明组合       |
| Labeling | begin result、put part、seal、read result                                                                            | lock write、finish write、finish failure |

Scope 固定 Job、actor、project 和完整排队参数的 SHA-256；租约额外绑定 generation 与 token。成员顺序不可改变，重放保留原始 Labeling 作用域。原始／准备请求按各自 SHA-256 和顺序摘要链保存；每块解码后最多 512 KiB，原生请求／响应最多 2 MiB。整题没有新增结果总字节或数量上限，仍遵循单个 RLE 与 PostgreSQL 值类型的既有上限。

最后一块完整提交后，即使引用根尚不能封存，也不得重置原始数据。重启先恢复完整数据；最终业务写入完成后保留精确 ID／时间／工具单位回执，释放大字段、分块和临时根。写入、Meta、回执、成员 outcome、计费和游标共同提交；丢提交确认通过回执解决。`storage_unavailable`、`execution_busy`、`lease_lost` 和内容围栏等待续写；作用域或实际 Prediction／Meta 证明不一致拒绝完成，不能据此推断原始结果可以丢弃。

成员结算与 `progress_pct` 同事务更新。作业摘要的 `failed_prediction_ids` 最多保留前 100 项，并用 `failed_prediction_ids_truncated` 标记截断；`batch_result_scope` 指向完整成员账本。按作业重试从该账本分页读取所有失败 ID，保持既有完整重试行为。prepared 数据及读回元数据必须包含非空工具单位；原始数据不携带工具单位。输出模式只允许作为重试请求上下文中的有界字符串保留。

专用 `preannotation_result_client` 没有私表、DDL、TEMP 或角色成员权限，只能调用十二项 Jobs 与五项 Labeling 能力，其中三项属于重试读取与派发。最终写入仍由 Labeling 调用方事务完成。该合同不迁移整个 Inference／Jobs 服务，失败项重试通过下述独立准入合同复用结果账本；部署和验收范围见 [ADR-0113](/dev/adr/0113-durable-preannotation-results)。

## 失败预测重试

`aap-prediction-retries/v1` 同样使用单 JSONB 请求与封闭信封；冻结模式为 `0233_prediction_retries.schema.json`，结果作用域扩展为 `0233_preannotation_result_scope.schema.json`。独立包、CLI 与原始／准备结果沿用 `apps/preannotation-results`，无需应用或队列依赖。

| 所有者   | 操作                                | 调用方                                                              |
| -------- | ----------------------------------- | ------------------------------------------------------------------- |
| Labeling | `admit_prediction_retry`            | 业务写入方；同事务固定失败项与原始作用域并创建 Job/outbox           |
| Labeling | `read_prediction_retry`             | executor；原失败项和 Job 历史删除后仍可读取终态                     |
| Labeling | `finish_prediction_retry_write`     | 业务写入方；证明实际 Prediction／Meta 并原子结算                    |
| Labeling | `finish_prediction_retry_failure`   | 业务写入方；永久失败只计一次，`abandon=true` 释放失效执行且不扣次数 |
| Jobs     | `claim_prediction_retry_deliveries` | executor；领取待发布、确认不确定或已过期的同一 Job                  |
| Jobs     | `finish_prediction_retry_delivery`  | executor；代次与 token 匹配后记录发布结果                           |

准入固定原失败 ID、actor、项目、Task、backend、参数、失败次数基线和前置写入作用域。结果成员与原失败项使用不同的稳定身份，避免碰撞原批量的成员历史。Job UUID 即 broker task ID，发布异常不能证明消息未到达。保留输入准备期间可查询 pending Job，但没有开始通知、模型调用或失败计数。

模型只能返回恰好一个原 Task 的结果。完整 raw 先于来源复核保存，之后的写入等待和进程接管复用同一输出与 Prediction 身份。成功结算同时删除确切原失败行和 Meta，记录精确回执、计数与 Job 终态；永久模型／Mask 错误只计一次。来源、actor、dismiss 或 Job 失效使用放弃结算，清理对应占用及临时根。所有终结均须当前租约，重复或陈旧投递不能删除失败行、递增次数或结束后来的执行。

`PREANNOTATION_RESULTS_DATABASE_URL` 或其 secret 文件同时控制读取、检查点和立即／定时 outbox 的函数专用连接。`aap-prediction-retries` CLI 只开放读取与两项派发能力；准入及业务结算不对 executor 登录开放。运行中视频跟踪另用 `register_tracker_running_mask_root` 在写入事务登记精确 Job/Task 的临时 Mask 根；GC 登录无此能力。持久窗口账本在 writer 完成前延续这些根，随后与待审或标注根原子交接。

## 视频执行窗口账本

`video_execution_client` 仅可调用 Jobs 的 `video_execution_step(text,text)` 与
`video_execution_checkpoint_step(text,text)`，没有私表、序列、DDL 或 TEMP 权限。
core policy 仍列出自身函数，checkpoint policy 列出这组完整的两函数 LOGIN 配置；升级时
须同步授权源与适配器。冻结描述位于 `0282_video_execution_checkpoints.schema.json`，
便携计划、逐窗口 payload/manifest/part 和恢复判断位于 `apps/preannotation-results`。

新 RPC 使用 `aap-video-checkpoint-data/v1` 成功／拒绝信封，每次非 policy 调用都携带
job/run/worker/generation/lease-token。动作依次覆盖 input facts、freeze/read plan、分页
resume、begin/capture、put part/seal、read part、verify replay、close span、recovery ready、
finish 与 retire。请求和响应最多 2 MiB；窗口 payload 最多 64 MiB，每块最多 512 KiB，
最多 128 块；元数据每页最多 100 窗。过时代次不能封存结果或完成 writer。

canonical window 不可覆盖，checkpoint ID 绑定计划与 ordinal，后继种子绑定原 manifest。
重放只登记新观察摘要并核对规范化结果／continuation／discovery seed，不替换旧字节。
成功要求所有窗口及必要的 session 关闭事实完整；取消允许普通追踪的部分候选，后续审阅
不改写原 writer outcome。finish 与业务终态和最终引用根处于同一事务；输入与素材的
执行 pin 只在实际 scope 退役后释放，租约或时间间隔不构成完成证明。
任务延迟确认，并在 worker 丢失时重投；续租结果不明会停止旧执行的写入，仍活跃的作业
保留重派发机会。业务提交已成功但确认丢失时，以重读的主作业终态继续退役清理。

Inference 的私有 status 经过配置的嵌入、专用 DSN 或 HTTP owner；HTTP 端点为
`POST /v1/routes/video-execution-status`，沿用路由服务鉴权并校验精确证明回显。它不属于
公开应用 OpenAPI。完整模块与提供方资格仍分开验收，见 [ADR-0158](/dev/adr/0158-video-window-journals-and-replay)。

## 视频 worker 最终写入 owner

Labeling 的 `video_writer_step(text,text)` 将一次视频 worker 的 completed、
cancelled 或 failed 终结写入绑定到当前执行证明，并与 Job 状态、候选结果、
Mask 引用根和不可变 receipt 在同一事务内提交。公开 REST API 不变。API 可以
默认借用其 Session，也可以使用专用 PostgreSQL DSN 或显式 HTTP owner；独立路径
使用 READ COMMITTED，函数本身不提交。URL 与 DSN 互斥，显式配置失败不会回退
到另一提供方。

私有 HTTP 使用 `aap-labeling-video-writer/v1`，通过 bearer 控制凭据保护：

| 方法 | 路径                                | 用途                                                                              |
| ---- | ----------------------------------- | --------------------------------------------------------------------------------- |
| POST | `/v1/labeling/video-writer/execute` | 校验并结算稳定的终结命令                                                          |
| POST | `/v1/labeling/video-writer/status`  | 按 operation／执行证明／摘要查询回执；仅 execution proof 查询 job/run publication |
| GET  | `/ready`                            | 检查运行策略与精确 LOGIN 权限面                                                   |

命令绑定 UUID operation、job/run/worker/generation/lease、结果 outcome、可选
已接受输入断言和最多 2000 字符错误说明。operation UUID 由 run 稳定派生；
不确定提交只重试原命令或查询原回执。Native read_publication action 和 status
的 publication lookup 使用封闭
`{schema, execution}`，只按当前 proof 定位 job/run 中已存在的不可变终态回执；
原 operation status 仍要求完整的 operation ID、execution proof 和 request digest。
HTTP 请求上限 65 MiB，封存后的 staged JSON 上限 64 MiB；较大的内部 Native JSON
上限用于转义后的传输，不改变结果预算。
成功回执同时记录 operation、job/run、请求摘要、结果状态和 staged 摘要。

Labeling 登录角色只允许执行 `aap_labeling_v1.video_writer_step(text,text)`。
内部 wrapper `aap_jobs_v1.video_writer_authority(text,jsonb)` 仅授权给 Labeling
owner，并将操作限制为 inspect、read window、固定 guard 和固定 finish；视频
LOGIN 没有 Jobs 或 Labeling 表权限，也没有任意 Jobs RPC。安装先应用当前迁移和
`infra/docker/module-data-grants.sql`，再应用
`infra/docker/labeling-video-writer-grants.sql`。

完成命令不信任 worker 再传的一份候选全量列表。owner 从 Jobs 账本按冻结计划
逐窗读取 manifest 与原始 UTF-8 payload 文本，验证原始字节摘要、计划身份、
前驱、窗口与 attempt 后组合 track 阶段结果；discovery 阶段只用于恢复依赖，不形成
候选。非空调用方列表只能作为断言，必须与重建结果完全相同。成功必须具备全部
冻结窗口和必要的 session close 事实。

普通追踪取消将封存的 track 前缀作为候选。可选未封存尾部只允许来自当前代次
已开始／捕获的下一 planned track window，且每帧都须位于窗口边界内；普通取消
必须先有数据库取消标记。空列表表示只保留封存前缀。新代次取消清理先按当前 proof
查询 job/run publication，命中就原样返回 receipt、不再暂存旧 payload；未命中才从
窗口账本重建封存前缀且不带本地帧行。纠错取消绕过 candidate stage 授权并清空纠错
候选。失败不需要加载窗口或重新授权
已损坏的来源计划；若作业已取消，失败清理不覆盖其既有取消候选。各种终结仍
受当前 tracker actor、原片来源、Task、segment 和状态检查约束。

Mask 对象键会在 Labeling 事务内锁定，再通过 Assets Mask Content owner 验证
对象字节；只在业务写入原子应用后释放临时引用根。bbox-only 作业不需要 Mask
服务凭据。临时存储错误会保留同一操作供重试，不触发模型重算。

此能力仍是 Labeling 的一个 worker-finalization 切片。Human accept/decide/discard
与 start/progress 写仍由 API ORM 持有；普通取消崩溃恢复接入、三路径真实消费者、
Mask 独立验证、全量备份恢复、受限镜像与容量尚未资格验证。参见
[Labeling 运行协议](/api/guides/labeling-runtime)和
[ADR-0159](/dev/adr/0159-independent-labeling-video-writer)。

## 视频执行权的离线恢复

`video_restore_operator_client` 只执行 Inference 的
`aap_inference_routing_v1.video_restore_step(text,text)`，没有原表、DDL 或 TEMP 权限。
专用授权脚本为 `infra/docker/video-restore-grants.sql`。固定命令与结果分别使用
`aap-video-restore/v1`、`aap-video-restore-data/v1`，冻结 schema 位于
`0284_video_restore.schema.json`。该操作不属于普通 API 或 routing listener。

Inference 以仅授予其 owner 的 `aap_jobs_v1.video_restore_authority(text,jsonb)`
协调 Jobs 权威轮换与原路由命令重新包裹；operator 不获得 Jobs 表或内部函数权限。
准备态关闭两类普通权威，事务屏障先排空旧 Native 写入；相关入口只接受
READ COMMITTED。完成事务保存目标 realm 的不可变 scope 退休证明，并使旧执行证明失效，
保留逻辑 run、窗口、历史 intent 和发布回执。新执行代次仍由普通 claim 产生。
八张恢复表由 Native 持有，ORM autogeneration 不得删除。

外部源进程／数据库／对象存储凭据围栏仍由操作人员建立；命令中的围栏摘要只引用记录。
实际 CPU 后台及独立 CLI 的本地验收已覆盖中断续跑，未验证对象存储备份恢复。
操作顺序见 [Inference 运行协议](/api/guides/inference-runtime#离线视频恢复-operator)，
完整恢复语义见 [ADR-0160](/dev/adr/0160-coordinated-video-restore)。

## Identity 已接受 key 的写入前围栏

`aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])` 由 Identity 持有，返回
boolean。Learning owner 有固定执行权；过渡 API 连接沿用原运行授权清单，不新增 LOGIN。
函数不向 AUTH、key-management 或建题客户端开放，也不返回 key/hash/private data。
调用方保留账号／项目资格与事务，key SHARE 等待后再核对当前撤销、scope 和真实过期时间。
SQLSTATE 与失败事务状态不被吞掉。冻结描述为 `0285_identity_accepted_api_key.schema.json`。
完整合同、消费者范围和定向证据见 [ADR-0161](/dev/adr/0161-identity-current-authority-consumers)。

## Audit 日聚合与 Jobs 通知范围事实

Audit 发布 `aap-audit-daily-projection/v1`：`aap_audit_v1.audit_daily_facts`
保留 `day`、`action`、`target_type`、`actor_role`、`status_family`、`event_count`
六个原生字段。DATE 是日历日期，BIGINT 的 JSON 表达是无损十进制字符串。
固定无参 `aap_audit_v1.refresh_audit_bi_mv()` 由 Audit owner 并发刷新原 MV；
调用方控制提交／回滚。owner 需要数据库 TEMP，读取和刷新客户端不需要该权限；
普通读取账号不能读取私有 MV 或执行刷新。维护连接的进一步拆分仍按模块台账推进。

Jobs 发布 `aap-jobs-notification-scope-facts/v1`：
`aap_jobs_v1.notification_scope_facts` 只包含作业 UUID、开放 `kind` 和可空
`scope_project_id`，保留当前项目优先、历史保留项目回退的语义。
通知消费者通过该关系读取事实，API 继续组合当前成员和导出权限；
缺失或不允许的作业类型仍拒绝投递。批量读取保留 autoflush、no_autoflush
和调用方事务，普通 API 其他 Jobs 路径的私表权限尚未撤销。

这两项是可信后端的同库合同，公开 HTTP 不变，关系本身不提供用户授权。
冻结 schema 和原生客户端见仓库 `apps/audit`、`apps/jobs`；
授权及限定验收范围见[模块台账](/dev/concepts/module-deployment#support-daily-and-jobs-facts)。

## Jobs 测试项目清理

`aap-jobs-fixture-cleanup/v1` 提供固定的
`aap_jobs_v1.delete_fixture_project_jobs(uuid[])`，返回原生非负 `bigint`。
只按当前 `project_id = ANY(project_ids)` 删除全部 kind／status 的作业；保留仅有
`retained_project_id` 的历史及其他项目，空集合返回 0，重复 ID 不重复计数。
SQL 和语言无关 schema 随 `apps/jobs` 分发。

函数由 Jobs owner 执行，仅在实际库名以 `_test` 或 `_e2e` 结尾时向普通运行账号
授权，函数内部也再次校验库名。它在调用方同一事务内执行 Workspace 既有项目清理
守卫，保留 NOWAIT 锁、视频退役要求、原 DELETE 触发器及约束；不自行提交或回滚。
NULL／含 NULL 的范围及异常守卫回复为 `22023`，项目忙为 `55P03`，视频未退役为
`23514`。E2E 清理仍先验证开关、测试库和命名空间，保留原删除顺序及保存点；项目忙
与未退役仍映射为 HTTP 409，Jobs 能力不可用或回包无效返回 503 并停止清理。

LAN／worktree 普通角色在宽授权之后撤销私表 INSERT／UPDATE／DELETE，清除列级
INSERT／UPDATE 并拒绝继承旁路；生产批次清理及终态保留清理继续使用既有 owner 函数。
普通生命周期命令和 payload／result 投影保持；显式兼容列／时间戳改写继续按实际
invoker 权限拒绝。SELECT 及完整 Jobs 凭据退出仍按[模块台账](/dev/concepts/module-deployment#剩余工作台账)推进。
截图等直接构造、修改和删除多领域夹具的脚本仍使用自有测试库的 schema-owner
连接，不把该项目范围清理能力扩大成任意作业删除。

## Jobs 筛选夹具初始化

`aap-jobs-filter-fixtures/v1` 的固定
`aap_jobs_v1.set_filter_fixture_job_outcomes(uuid,uuid)` 接收 completed／failed 两条
夹具作业 ID，返回原生 `bigint` 2。它只执行原筛选夹具的两组字段投影，保留开始／完成
时间、失败行 result、其他字段及调用方事务；更新顺序、UPDATE OF 触发字段和默认
更新时间与原 ORM 相同。调用方同步原缓存字段并让 updated_at 过期，原 invitation
flush、最后完整 flush、审计及提交顺序保持。

函数仅向实际测试库普通账号授权，内部再次验证 `_test`／`_e2e` 库名。空或相同 ID
为 `22023`，缺失行是 `P0002`，后者回滚整个函数语句；非测试库为 `42501`。
普通运行账号无私表表级／列级 UPDATE，生产更新继续通过既有生命周期能力完成。
该函数限定筛选夹具，截图等多领域 owner 工具继续使用原自有测试库连接。

## Jobs 动作幂等查找

`aap-jobs-action-idempotency/v1` 提供两项固定能力：

- `aap_jobs_v1.lock_action_idempotency(text,uuid,uuid,text)` 使用原 `aap:data-manager` 前缀及 `hashtextextended(...,0)`，取得等待型事务 advisory 锁，不触发 ORM autoflush。
- `aap_jobs_v1.find_action_idempotent_jobs(text,uuid,uuid,text)` 按 kind、当前 project_id、user_id 和原 JSONB 文本键查找，返回固定十五列作业行，按 created_at／id 倒序并执行等待型 FOR UPDATE。没有 LIMIT；全部匹配行保持锁定，原标量调用方取第一条。

旧 SQLAlchemy 空值判断、缓存 identity、查询 autoflush 和调用方提交／回滚保持；没有
新增状态筛选或业务授权。原调用方继续规范化请求键、校验当前权限并比较请求摘要，
不一致仍返回原 409。SQL 仅选择闭合的谓词模板，参数通过绑定传递。

普通运行账号无需私表 SELECT／UPDATE 即可调用；严格 lifecycle/list/domain 角色
维持原能力集合。项目预标与所选任务导出共用这项绑定，避免撤销 UPDATE 后原锁读失败。
全局私表 SELECT 仍因其他调用方保留，完整 Jobs 资格见[模块台账](/dev/concepts/module-deployment#jobs-action-idempotency)。

## Jobs 列表查询

`aap-jobs-async-job-list-query/v1` 将现有 `GET /async-jobs` 的查询策略放入 Jobs owner，公开两个固定只读函数：

- `aap_jobs_v1.async_job_list_page(uuid,text,text[],text[],uuid,text,integer,integer)` 返回十五列原生作业行，按 `created_at DESC, id DESC` 排序。
- `aap_jobs_v1.async_job_list_total(uuid,text,text[],text[],uuid,text)` 返回 PostgreSQL `bigint` 总数；Node 默认以十进制字符串读取。

参数依次为 actor UUID、平台角色快照、状态、开放 kind、可空项目 UUID、可空搜索模式，分页函数另接收 limit／offset。调用方先完成 Identity 认证与当前活动状态读取，再分别执行 page 和 total；两个语句保留各自快照，不合并查询或提交事务。成员资格在每条语句内通过 Identity 当前事实与 Workspace 能力验证，预查角色仍控制原有 super-admin／项目管理员分支。当前／保留项目范围、reviewer 导出权限、六项 JSONB 文本搜索、autoflush 与 ORM identity 行为保持。

`jobs_list_query_client` 是仅持两项函数权限的独立 LOGIN，没有 Jobs 私表、内部 helper、Identity 表／函数、DDL、TEMP 或角色成员权限。普通 API 的其余 Jobs 操作仍需原有私表权限，本操作不提前撤销。迁移和普通 owner 授权后，使用 `infra/docker/jobs-list-query-grants.sql` 安装严格客户端权限；工作区与 LAN 的普通 API 授权也包含这两个公开函数。

schema 与 SQL 随 `apps/jobs` 分发，独立 Node 消费者位于 `apps/jobs/conformance/node/`。实现、消费者及限定验收状态分别见[模块台账](/dev/concepts/module-deployment#jobs-list-query-owner)，这不代表完整 Jobs 已独立运行。

## Notifications 即时发布

现有独立 `aap-notifications-publisher` 可同时提供私网 `POST /v1/notifications/publish`，
协议标识为 `aap-notifications-immediate-publication/v1`。这不是公开用户 API；使用独立
service bearer，在读取请求体前鉴权。冻结 JSON Schema 位于
`apps/notifications/src/aap_notifications/publication.schema.json`。

请求恰好包含以下四项：

| 字段            | 合同                                                                              |
| --------------- | --------------------------------------------------------------------------------- |
| `schema`        | 固定协议标识                                                                      |
| `user_id`       | 收件人 UUID；owner 据此生成 `notify:{UUID}`，不接受任意频道                       |
| `message`       | 原六字段通知事件，或开放字段的 `notifications.sync` 对象；不改变原生 JSON payload |
| `authorization` | sync 提示为 null；业务事件为封闭授权快照对象                                      |

授权对象包含 `restricted`、`project_id`、`export_required`、`member`、`reviewer`、
`owner`、`super_admin`。除可空 UUID `project_id` 外均为严格 boolean。受限通知没有
项目时拒绝；普通项目通知要求 member／owner／super，导出要求 reviewer／owner／super。
快照由受信业务服务在原事务中读取，不能接受终端用户自行声明的权限，也不传 SQL、
Session 或数据库凭据。业务 `type` 保持开放；是否为同步提示由 null 授权区分。

成功响应恰好为 `schema` 和严格 boolean `published`。策略拒绝返回 200／false；
Redis 接受后才返回 true，零订阅者也视为成功。未认证返回 401，非法请求返回 400，
依赖失败返回 503；响应不包含传入 payload、凭据或依赖地址。调用方不自动重试、
不跟随重定向、不使用环境代理，显式远程错误不回退本地 transport。

API 保留通知的 identity-map／过期字段、autoflush、Jobs 范围与四项顺序授权读取，
owner 使用该次事实，不另开事务读取尚未提交的行。普通提交后发布及同步提示仍为
best-effort，终态发布保留提交前路径；按 ID 发布失败仍传播给既有投递租约。
既有 durable outbox 的 lease／ack／重试单独保留，普通发布不转为耐久队列。

API 配置 `NOTIFICATION_PUBLICATION_URL` 和 `NOTIFICATION_PUBLICATION_TOKEN(_FILE)`；
publisher 使用 `NOTIFICATION_PUBLISH_API_TOKEN(_FILE)`、`NOTIFICATION_PUBLISH_HOST`、
`NOTIFICATION_PUBLISH_PORT`。直接 token 与文件二选一，空 URL 保留内嵌装配；
未给 publisher API token 时仍只运行原 durable 循环，`--once` 行为保持。
独立 Compose 文件 `docker-compose.notifications-publisher.yml` 不发布宿主端口。
`/health/live` 检查进程，`/health/ready` 检查 Redis、原投递策略及 publisher 循环。
本地运行与完整模块资格按[唯一台账](../../dev/concepts/module-deployment#notification-publication-owner)分别记录。
