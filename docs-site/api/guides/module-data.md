---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-18
---

# 同库事务数据接口

这些接口供可信后端进程使用，不是终端用户 REST API。调用方仍负责现有身份、当前
权限和用例准入；持有函数执行权限不等于获得任意用户的项目管理权限。

实现与冻结合同位于 `apps/api/alembic/sql/0190*`、`0191*`。接口通过 PostgreSQL
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

首次关联的分块任务、排队作业代次与完整模块服务仍未迁移到这条接口；
总体进度见[模块部署台账](/dev/concepts/module-deployment)。
