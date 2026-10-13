# 仓库优化 P8：迁移验证（§7.2）真实回退、前向数据断言与备份恢复

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（§7.2 特别复核）
> 工作树/分支：`/home/hehao/桌面/ai-annotation-platform-worktree-agent-opt-p6-ml`（分支 `worktree-agent-opt-p8-migrations`，根 `3891c550e`）
> 输入：`scripts/alembic_reversible_floor.py`、`apps/api/alembic/versions/0173_*.py`/`0174_*.py`、
> `apps/api/tests/test_project_role_migration.py`、`apps/api/tests/test_migration_0173_project_role_preparation.py`
> 证据图例：**[V]** 本工作树实测；**[GAP]** 未执行/留待 P8 caller 集成

## 0. 结论

1. **旧 round-trip 的问题确认 [V]**：`ci.yml` 的 `upgrade head` → `alembic stamp <floor>` →
   `downgrade base` → `upgrade head` 中，`stamp` 只改版本记号，不执行被跳过段落的真实
   schema/data 回退；0174 的不可逆段从未被真实验证（P0 记录 run `35455695341` 也直接暴露了
   `0174 not reversible`）。
2. **新的 focused runner [V]**：`scripts/validate_migrations.py` 在**自有一次性库**上分别执行
   `fresh`（174 步完整 upgrade）、`reversible`（0173 → 真实 `downgrade base` 173 步 → head，
   全程无 stamp）、`forward`（播种历史行后前向转换断言 + 不可逆 downgrade 拒绝）、
   `restore`（转换前 `pg_dump` 快照恢复到独立库）四个阶段，全部通过。
3. **策略 fail-closed [V]**：`scripts/alembic_reversible_floor.py` 重写为可测试的 `classify_chain`
   纯函数：多 head、merge、孤儿分支、未知父版本、环、多个不可逆迁移、基线处不可逆一律抛
   `UnsupportedChain` 并以非零退出，不再给出会漏检的 floor；默认 stdout（floor 单行）契约保留，
   新增 `--json` 输出完整策略。
4. **清理可证 [V]**：成功与注入失败（`--fail-after forward`）两种路径都只删除本次创建并带归属
   标记的 `<库名>__mv_*`；运行后 `pg_database` 中无任何 `__mv_` 残留，基础库 schema 未被改动。
5. **CI caller 已交 P8 owner [GAP]**：`ci.yml` 由 P8 负责接线，替换 stamp 步骤；本地与 CI 命令见
   §3。`docs-site/dev/how-to/add-migration.md` 已同步为无 stamp 的真实校验说明。

## 1. 范围与边界

- 只改：`scripts/alembic_reversible_floor.py`、新增 `scripts/validate_migrations.py`、
  新增 `scripts/test_alembic_migration_policy.py`、`docs-site/dev/how-to/add-migration.md`、本文件。
- 不改：`.github/workflows/**`（P8 owner 接线）、已发布 alembic 迁移、P5/P6/P7/TSV/共享台账。
- 只在**自有一次性库**上操作：worktree 模式要求 `AAP_WORKTREE_MODE ∈ {test,e2e}` 且
  `.worktree/<mode>/resources.json` 的 database/owner 与连接串一致；非 worktree 模式要求显式
  `AAP_MIGRATION_VALIDATION_OWNER` 且库名以 `_test`/`_e2e` 结尾。拒绝非本机 host、
  `prod`/`annotation`（开发库）、query 参数、非 postgresql 驱动。
- 迁移入口仍是 `alembic upgrade head`；本工作不重写历史、不改锁文件/生成类型/协议。

## 2. 实现

### 2.1 修订图策略（`alembic_reversible_floor.py`）

`classify_chain(revisions, head)` 从唯一 head 沿 `down_revision` 走到 base，返回
`ChainPolicy{head, reversible_floor, irreversible, reversible_segment}`：

- 0 个不可逆 → floor = head（等价 `downgrade base`）；
- 1 个不可逆 → floor = 该迁移的 `down_revision`；`downgrade <floor>` 的路径是
  `(floor, head]`，其中不含不可逆迁移的 `down_revision` 边界**以上**部分，因此调用方必须
  `upgrade <floor>` 起步再 `downgrade base`，**不能** `stamp`；
- > 1 个不可逆、基线处不可逆、merge/多 head/孤儿/环/未知父版本 → `UnsupportedChain`。

### 2.2 验证 runner（`scripts/validate_migrations.py`）

从 `MIGRATION_DATABASE_URL`（或 `DATABASE_URL`）解析本机 anchor，按前缀派生四个库：

| 阶段       | 数据库                  | 真实动作                                           | 关键断言                                                                                                                                                                                                                       |
| ---------- | ----------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| fresh      | `<base>__mv_fresh`      | `upgrade head`                                     | `alembic_version=head`；0173 哨兵列存在；`users.role` default 含 `employee`                                                                                                                                                    |
| reversible | `<base>__mv_reversible` | `upgrade 0173` → `downgrade base` → `upgrade head` | 起点 default 含 `annotator`；`downgrade base` 实际执行 173 步（非 stamp）；终点回 head                                                                                                                                         |
| forward    | `<base>__mv_forward`    | 播种历史行 → `pg_dump` 快照 → `upgrade head`       | 角色转换 employee、inactive 不复活、成员关系不伪造且不被改写、pending invitation 得到 project_role、已接受邀请不被改写、head default employee；`downgrade 0173` 抛 “not reversible” 且版本记号不变                             |
| restore    | `<base>__mv_restore`    | `pg_restore` 转换前快照                            | 恢复库停在 0173；0173 哨兵列存在；`users.role` default 仍含 `annotator`；历史角色 `annotator/project_admin/reviewer` 原样；成员关系 `annotator` 未被改写；pending invitation 的 `project_role` 仍为 NULL（证明确为转换前快照） |

- 每个库创建时写 `COMMENT ON DATABASE ... IS <owner>`；删除前校验 owner 与无连接，绝不强断未知客户端。
- `pg_dump`/`pg_restore` 优先用宿主机客户端，否则用发布 5432 端口的本地 Postgres 容器内置客户端
  （本机无宿主客户端，实测走容器路径）。
- `finally` 删除全部本次创建库与 dump 文件；`--fail-after {fresh,reversible,forward}` 为失败清理自证开关。

### 2.3 策略单测（`scripts/test_alembic_migration_policy.py`）

33 例（纯逻辑、无数据库；含 5 例历史行缺失/重复校验）：单/零/多个不可逆、基线不可逆、**不可逆之上存在可逆后缀**、merge、
孤儿、未知父版本、环、单/多/零 head；以及 anchor 守卫的拒绝/接受场景（缺 owner、开发库、非本机、
prod、非 postgres 驱动）和 `create_action`/`drop_action` 归属判定（不存在→创建、自有残留→重建、
无注释/异主→拒绝；删除时自建未打标的半成品→删除、非本次创建的无注释库/异主库→拒绝）。

### 2.4 评审加固（f299 之后）

- **修订图边界**：`classify_chain` 显式拒绝「不可逆迁移之上还有可逆版本」的形状（`head→不可逆版本`
  本身是可验证段，但 runner 尚未实现后缀验证），不静默只验证前缀；单测覆盖。
- **分配即登记**：`create_owned_database` 在 `CREATE DATABASE` 成功后**立即**把库名写入
  `state.created`，再执行 COMMENT 与校验；COMMENT/校验失败时 `finally` 仍会清理这个半成品库。
  `--fail-after create` 注入在 COMMENT 之前失败，实测创建 1 库后 `cleanup_errors=[]`、无残留。
- **存在性 vs 无注释**：`database_state` 返回 `(exists, owner, sessions)`；`create_action`/`drop_action`
  纯函数区分「不存在 / 自有 / 无注释 / 异主」。异主或无注释的既有库一律拒绝收养，也绝不被删除
  （实测：预置 `__mv_fresh` 归属 `foreign-owner`，运行失败且该库原样保留）。
- **尽力清理**：`cleanup` 对每个库/文件分别捕获 `Exception` 并聚合错误，单个 SQLAlchemy/连接异常
  不再中断其余清理；有任何清理错误或残留都以非零退出并在 `summary` 中报告。
- **真实 anchor 校验**：`verify_anchor` 在任何派生写库之前，校验基础库真实存在、owner 注释与
  manifest 一致、连接端点与 manifest 的 postgres host/port 一致，并确认连接解析到该库本身；
  只凭库名不再足够。
- **dump 预登记**：`state.dumps` 在 `pg_dump` 写入前登记路径，写入中断也会清理半成品文件。
- **restore 断言扩充**：在 0173 哨兵列、`users.role` default `annotator`、三名历史用户角色之外，
  增加成员关系 `annotator` 未被改写、accepted 历史邀请 role/project_role 未被改写、pending 邀请
  `project_role` 仍为 NULL（证明是转换前快照）。

## 3. CI caller（交给 P8 owner）

```yaml
# ci.yml，pytest job（Postgres service 就绪后）
- name: Migration validation (fresh / reversible / forward / restore)
  working-directory: apps/api
  env:
    AAP_MIGRATION_VALIDATION_OWNER: ci:${{ github.run_id }}
    MIGRATION_DATABASE_URL: ${{ env.DATABASE_URL }}
  run: uv run python ../../scripts/validate_migrations.py
```

要求：`DATABASE_URL` 指向本机一次性库且角色可 `CREATE DATABASE`（CI service `user` 为 superuser）；
宿主机有 `pg_dump`/`pg_restore` 或 Postgres 在可被 `docker ps` 发现的容器中。替换旧步骤：删除
`alembic stamp` 两行，保留 `alembic upgrade head`。本地：`pnpm dev:worktree -- init --mode test` 后
`pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python ../../scripts/validate_migrations.py'`。

## 4. 实测证据

| 检查           | 命令                                                                                                                              | 结果                                                                                                                                                                  |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 策略单测       | `apps/api/.venv/bin/python scripts/test_alembic_migration_policy.py`                                                              | 33 passed（单/多/零 head、单/多/基线不可逆、不可逆之上可逆后缀拒绝、merge/孤儿/环/未知父版本、anchor 拒绝、create/drop 归属判定、历史行缺失/重复校验）[V]             |
| floor 输出     | `apps/api/.venv/bin/python scripts/alembic_reversible_floor.py`                                                                   | `0173`（`--json`：head 0174、irreversible `["0174"]`）[V]                                                                                                             |
| 完整验证       | `pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python ../../scripts/validate_migrations.py --json'` | `result=passed`；fresh 174 upgrades；reversible 0173→base 173 downgrades→0174；forward/restore 通过；`cleanup_errors=[]`、`leftover_owned_databases=[]`；退出码 0 [V] |
| 分配失败清理   | 同上加 `--fail-after create`（在 COMMENT 之前注入失败）                                                                           | `result=failed`、created 1 库、`cleanup_errors=[]`、`leftover=[]`、退出码 1（半成品库被删除）[V]                                                                      |
| 整体失败清理   | 同上加 `--fail-after forward`                                                                                                     | `result=failed`、created 3 库、`cleanup_errors=[]`、`leftover=[]`、退出码 1 [V]                                                                                       |
| 异主冲突不删除 | 预置 `__mv_fresh` 归属 `foreign-owner` 后运行                                                                                     | `result=failed`（拒绝收养）、`created=[]`、`cleanup_errors=[]`，该库运行后仍存在且 owner 不变；随后人工清理 [V]                                                       |
| 残留检查       | `psql -U user -d postgres -tAc "SELECT datname ... LIKE '%__mv_%'"`                                                               | 无 `__mv_` 库 [V]                                                                                                                                                     |
| pg 工具/服务器 | `docker exec … psql -tAc "show server_version"`、`pg_dump --version`、`pg_restore --version`                                      | 服务器 16.14 与容器内置 `pg_dump`/`pg_restore` 16.14 同源同版本；本机无宿主客户端，实测走容器后端 [V]                                                                 |
| 运行库身份     | `pnpm dev:worktree -- doctor --mode test`                                                                                         | 自有库 `aap_wt_c2820af87ec94679_test`（head 0174），owner `aap-worktree:c2820af87ec94679:test:*` [V]                                                                  |

## 5. 限制

- 本地单工作树、单个本机 PostgreSQL 16.14（容器 `postgres:16-alpine`，`pg_dump`/`pg_restore`
  同为 16.14）；未在 CI 跑（无 push），CI caller 由 P8 owner 集成后再验证。
- 备份/恢复是**逻辑** `pg_dump -Fc` 快照 + `pg_restore`，不是物理 PITR、不含 WAL 归档、不做
  异地/远端备份与保留策略演练；证明的是「转换前受保护快照可恢复到同服务器新库」这一文档化路径。
  若生产恢复依赖外部对象存储与异地副本，那部分不在本 runner 覆盖范围。
- 历史 `annotation_test`/`annotation_e2e` 在本机未被本 runner 用作目标；CI 上由 service 容器提供，
  仍是本地容器内的一次性库。
- 未改动已发布迁移历史；`IRREVERSIBLE` 策略只对当前单一不可逆（0174）成立，未来新增不可逆迁移
  会命中 fail-closed 分支，需要显式设计新的验证段。
- `--fail-after` 的失败清理自证覆盖 create/fresh/reversible/forward 阶段；restore 阶段并未删除
  forward 库（转换前快照已写入磁盘独立存在），所有已创建库与 dump 文件都由 run 结束时的 `finally`
  统一清理，restore 本身未单独枚举失败注入。
