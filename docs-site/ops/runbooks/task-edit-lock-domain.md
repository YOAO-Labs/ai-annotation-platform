---
title: Runbook：任务编辑锁域切换
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-11-18
---

# Runbook：任务编辑锁域切换 {#runbook-任务编辑锁域切换}

本手册执行一次前向切换：把所有活跃写者从历史
`aap:task-edit-lock:<UUID>` advisory 域迁移到
`task-edit-lock:<UUID>`。切换由 `bo0466` 迁移和同版本 API／worker／模块产物共同完成；
它不改变请求／回执身份、任务锁表结构、租约、接管规则或调用方事务归属。

::: danger 只能整体切换
不要只升级数据库、只替换某一个消费者，或让新旧域同时运行。旧域与新域没有互相排斥
关系；并存会破坏任务编辑与标注写入的互斥。迁移会拒绝未排空的旧写者，但仍必须在
维护窗口内停止准入并排空连接后再执行。
:::

## 影响范围 {#影响范围}

以下入口共同使用该域，必须在同一次切换中更新：

- Labeling `yoao_labeling_v1.task_edit_lock_command(jsonb)`：API 编辑锁、审批、
  视频预测采纳、Mask 编辑与最终写入围栏。
- API active-learning 候选编辑围栏：通过同一 `TaskLockService` 取当前域。
- 视频选集 `yoao_labeling_v1.lock_video_input_manifest_member(...)`。
- Identity 离职处理 `yoao_labeling_v1.lock_identity_offboarding_work(...)`。
- Workspace 成员变更 `yoao_labeling_v1.lock_membership_work(...)`。

这些函数签名与授权保持不变。迁移不新增函数、不新增角色、不扩大 EXECUTE；只替换
`yoao_labeling_v1` 中这四个函数的正文。

## 前置条件 {#前置条件}

- 已安排维护窗口，可停止新任务编辑、送审／审核、预测采纳、视频选集、成员变更与离职
  处理，并等待在途事务结束。
- 已保留经验证可恢复的迁移前备份或快照；记录当前 Alembic revision。
- 迁移连接使用数据库 owner／超级用户凭据；四个入口当前仍由 `labeling_code` 等模块
  owner 持有，替换正文需要相应权限。
- API、Celery worker、Labeling 运行进程、视频／Assets 进程、Identity 与 Workspace
  调用方都能在同一窗口部署匹配版本。
- 已确认没有其他维护脚本或临时会话持有这些入口的 EXECUTE；迁移的排空守卫会把它们
  当作未排空写者拒绝。

## 1. 切换前只读核对 {#1-切换前只读核对}

在目标库以只读方式确认四个函数仍使用旧域，且没有已经切换到新域的产物：

```sql
SELECT p.oid::regprocedure,
       (pg_catalog.length(p.prosrc)
         - pg_catalog.length(pg_catalog.replace(p.prosrc, '''aap:task-edit-lock:''', '')))
         / pg_catalog.length('''aap:task-edit-lock:''') AS legacy_occurrences,
       (pg_catalog.length(p.prosrc)
         - pg_catalog.length(pg_catalog.replace(p.prosrc, '''task-edit-lock:''', '')))
         / pg_catalog.length('''task-edit-lock:''') AS current_occurrences
FROM pg_catalog.pg_proc AS p
WHERE p.oid IN (
  'yoao_labeling_v1.task_edit_lock_command(jsonb)'::regprocedure,
  'yoao_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)'::regprocedure,
  'yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])'::regprocedure,
  'yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'::regprocedure
)
ORDER BY p.oid::regprocedure::text;
```

预期 `task_edit_lock_command` 有 7 个旧域引用，其余三个函数各 1 个；所有 `current_occurrences` 均为 0。若不符合，
先停止并核对部署来源，不要手工修补正文。

检查其他连接是否仍可进入锁域：

```sql
SELECT a.pid, a.usename, a.datname, a.state, a.backend_type, a.query_start
FROM pg_catalog.pg_stat_activity AS a
WHERE a.pid <> pg_catalog.pg_backend_pid()
  AND a.datid = (SELECT oid FROM pg_catalog.pg_database
                 WHERE datname = pg_catalog.current_database())
  AND a.usesysid IS NOT NULL
  AND (
    pg_catalog.has_function_privilege(a.usesysid,
      'yoao_labeling_v1.task_edit_lock_command(jsonb)', 'EXECUTE')
    OR pg_catalog.has_function_privilege(a.usesysid,
      'yoao_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)', 'EXECUTE')
    OR pg_catalog.has_function_privilege(a.usesysid,
      'yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])', 'EXECUTE')
    OR pg_catalog.has_function_privilege(a.usesysid,
      'yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)', 'EXECUTE')
  )
ORDER BY a.pid;
```

结果应为空。若有连接，先停止对应服务并等待事务结束；不要只 `pg_terminate_backend`
正在提交业务事务的会话。

## 2. 停止准入并排空 {#2-停止准入并排空}

1. 停止 API 写入入口、Celery worker 的任务编辑／审批／预测采纳消费者，以及视频、
   Identity 离职和 Workspace 成员变更调用方。
2. 等待在途事务提交或回滚；确认上一步查询为空。
3. 停止会直接以旧域取锁的旧 API 进程。旧进程即使后续调用新函数，也可能先在旧域取锁，
   因此不能与新版本并行。

## 3. 应用迁移与部署 {#3-应用迁移与部署}

1. 应用 `bo0466`（其前一个 revision 为 `bo0465`）。迁移会：
   - 核对四个入口的当前正文、owner 与固定 `search_path`；
   - 拒绝仍有旧域／新域写者连接的数据库；
   - 拒绝仍被持有的旧域或新域 advisory key；
   - 以新域重写四个正文并复核，任何偏差直接失败回滚。
2. 在同一窗口部署匹配的 API、worker、Labeling 包及视频／Identity／Workspace 调用方。
3. 重新执行既有 owner／runtime／专用 Labeling 授权脚本。四个函数签名未变，通常不需要
   新增授权；不要为本次切换扩大任何角色权限。
4. 只有上述步骤全部成功后才重开准入。

## 4. 切换后核对 {#4-切换后核对}

复核正文只在新域取锁：

```sql
SELECT p.oid::regprocedure,
       p.prosrc LIKE '%''task-edit-lock:''%' AS uses_current_domain,
       p.prosrc ~ 'pg_(try_)?advisory_xact_lock\(\s*hashtextextended\(\s*''aap:task-edit-lock:'
         AS acquires_legacy
FROM pg_catalog.pg_proc AS p
WHERE p.oid IN (
  'yoao_labeling_v1.task_edit_lock_command(jsonb)'::regprocedure,
  'yoao_labeling_v1.lock_video_input_manifest_member(uuid,uuid,uuid,jsonb,jsonb)'::regprocedure,
  'yoao_labeling_v1.lock_identity_offboarding_work(uuid,uuid[])'::regprocedure,
  'yoao_labeling_v1.lock_membership_work(uuid,uuid,text,boolean)'::regprocedure
)
ORDER BY p.oid::regprocedure::text;
```

`uses_current_domain` 应为 `true`，`acquires_legacy` 应为 `false`。旧域字符串仍可能出现在
失败关闭检测分支中，这是预期行为；它不是获取旧锁。

运行时再核对一个任务：

- 两个独立会话中，一个持有 `task-edit-lock:<UUID>`，另一个调用
  `task_edit_lock_command('try_final_scope')` 应得到 `available=false`。
- 同一新域锁存在时，`lock_membership_work` 应返回 `resource_busy`，
  `lock_identity_offboarding_work` 应返回 `false`。
- 若人为持有旧域 `aap:task-edit-lock:<UUID>`，新函数应分别以 SQLSTATE `55006`
  （API 映射为 503）、`resource_busy` 或 `false` 失败关闭，而不是取一把新锁继续执行。
- 正常编辑释放后，工作台可以重新获取；旧域锁释放后不应再出现该任务的新旧并存。

## 5. 回滚边界 {#5-回滚边界}

`bo0466` 的 downgrade 始终抛错，不提供自动回退，也不对数据库降级作资格声明。

- **重开新写入之前**：可恢复迁移前快照并回退到旧版本产物；恢复后旧域与旧数据一致。
- **重开新写入之后**：新域下已有编辑锁／租约与业务写入，旧域无法无损表达；只能向前修复
  并保持新版本，或把旧快照恢复视为一次显式业务数据回滚（会丢失快照后的写入），需业务
  确认。不要同时运行新旧域来“平滑”回退。

## 相关 {#相关}

- [Labeling 运行协议](/api/guides/labeling-runtime#任务编辑锁与生命周期)
- [升级指南](/ops/upgrade-guide)
- [模块部署](/dev/concepts/module-deployment)
