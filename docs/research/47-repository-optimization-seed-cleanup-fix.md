# 仓库优化 P9 修复：owned seed 清理 500（deadlock 竞争）

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P9 owned 夹具清理 / P10 验证通道）
> 候选与分支：候选 `dc972be08a42855c836023da178487469aae2fc9`（P9 冻结候选）→ 本修复分支 `worktree-agent-opt-p9-cleanup-fix`
> 证据图例：**[V]** 本工作树/自有一次性环境实测；**[PG]** PostgreSQL 服务端日志原证；**[REUSE]** 对字节一致输入复用既有证据
>
> **P10 收尾（2026-09-21）**：本修复已并入集成根；最终 composed 必需套件审计见 [43](./43-repository-optimization-p9-shadow-comparison.md)，台账关闭见 [45](./45-repository-optimization-final-acceptance.md)。

## 0. 结论

1. **根因确认 [V][PG]**：失败测试**自身尚未结束的** `POST /annotations` 与 teardown 的 `POST /seed/owned-cleanup` 竞争。pointcloud/视频任务的标注创建会在事务内调用 `heartbeat_task_lock_for_legacy_video` → `TaskLockService.heartbeat`（`UPDATE task_locks`，`apps/api/app/api/v1/tasks/annotations.py:417`），而清理在 `DELETE task_locks` → `DELETE tasks` → `DELETE projects`/`datasets`/`users`。两个事务在 `task_locks`/`tasks` 上以相反顺序持有并请求锁，触发 PostgreSQL **deadlock**。服务端日志直接给出双方语句：`Process 961835: DELETE FROM tasks WHERE project_id = ANY($1)` ↔ `Process 961864: UPDATE task_locks SET expire_at=... WHERE task_locks.id=$2`；同一循环随后反复出现在 `DELETE projects`/`datasets`/`users`，并伴随级联 FK 报错（`tasks_project_id_fkey`、`project_members_user_id_fkey`）。
2. **为何表现为“清理残差”**：`_try_delete` 逐表吞掉异常。观察到的序列是：deadlock 之后，当次 DELETE 未生效，后续清理语句继续被吞掉，并因前置子行未删除而级联失败（`tasks_project_id_fkey`、`project_members_user_id_fkey`），严格 residual 守卫最终抛出 `e2e_seed_cleanup_incomplete`（`users:3, projects:2`）。也就是说 500 是**竞争的下游症状**，不是持久外键缺陷；这里只描述实际观察，不对 PostgreSQL savepoint 的可恢复性作一般性断言。
3. **隔离对照 [V]**：对保留的残差行集，在自有的字节副本上用**完全相同的清理代码与 HTTP 路由**执行返回 `200`、零 `seed_cleanup skip`；说明该行集本身可被清理，缺陷是竞争/时序。
4. **修复 [V]**（均在授权范围内，无 actor/seed API 重构）：
   - **测试生命周期（主因）**：`apps/web/e2e/tests/workbench-pointcloud-tools.spec.ts:379` 改为登记并 **await 匹配的 POST RESPONSE**，断言响应成功与持久化的 annotation `id`/`geometry`，同时保留原有 request-geometry 与双击断言。teardown 因此在该写入及其 task-lock heartbeat 完成后才运行，竞争消失。
   - **共享前缀越界**：owned 清理不再匹配共享 `BUG-E2E-FILTER-%` 前缀（`_test_seed.py`），该前缀只在 `owned is None` 的共享清理中使用，避免任一命名空间误删共享过滤夹具的 bug 行。
   - **命名空间化 takeover actor**：新增 `takeover-<namespace>@e2e.test`（仍经**真实 invite → register** 路径创建，见 `video-issue-context.spec.ts`），并纳入 `_OwnedFixtureScope.all_user_emails`，使**精确 owned 清理**删除它而不是把共享全局账号留给全局 teardown。
   - **fail-fast 诊断**：`_try_delete` 对事务级中止错误（`40P01` deadlock / `40001` serialization）**重新抛出**，不再吞掉；`25P02`（事务已中止）只作为下游症状，不作为独立信号。良性逐表漂移仍按原样跳过。代码注释只记录上述**实际观察**与**应用策略**（竞争类失败必须上抛、让原始原因可见），不声称 savepoint 回滚无法恢复死锁。
5. **实测 [V]**：修复后 `workbench-pointcloud-tools.spec.ts:379` 单测 `--retries=0` → **1 passed (28.3s)**，无 deadlock/残差；`video-issue-context` G2-5 相关用例 → **3 passed (1.3m)**；`apps/api/tests/test_seed_owned.py` → **22 passed**（含 3 例新增回归）；`apps/web` `tsc --noEmit`、`eslint`（两个 spec）、`ruff check`/`ruff format --check` 均通过；自有 e2e 库运行后 `users=0 / projects=0`。

## 1. 复现与证据

- **自有隔离复现 [V]**：本工作树自有 e2e 模式 `aap_wt_c2820af87ec94679_e2e`，命令
  `pnpm dev:worktree -- exec --mode e2e -- bash -lc 'cd apps/web && pnpm exec playwright test e2e/tests/workbench-pointcloud-tools.spec.ts --project=pointcloud -g "多边形双击" --retries=1 --reporter=line'`
  修复前：attempt 0 失败、attempt 1 通过（flaky），错误 `seed/owned-cleanup failed: 500 ... users:3, projects:2`，与 P9 domain 运行一致。
- **API 侧 traceback [V]**：`asyncpg.exceptions.DeadlockDetectedError: deadlock detected`，SQL 为 `UPDATE task_locks SET expire_at=$1 ... WHERE task_locks.id = $2`，调用链 `create_annotation → heartbeat_task_lock_for_legacy_video → TaskLockService.heartbeat → db.flush()`。
- **PostgreSQL 服务端日志 [PG]**（同集群）：
  - `Process 961864: UPDATE task_locks ...` ↔ `Process 961835: DELETE FROM tasks WHERE project_id = ANY($1)`；
  - 随后 `DELETE FROM projects` / `DELETE FROM datasets` / `DELETE FROM users` 依次 deadlock，并出现 `tasks_project_id_fkey`、`project_members_user_id_fkey` 级联报错。
- **隔离对照 [V]**：把残差行集按字节复制到自有库（只读 dump + restore），直接调用 `_cleanup_e2e_fixtures` 与经 HTTP 调用 `/seed/owned-cleanup` 均返回 `200`、无 skip。
- **P9 原始结果保留**：`/tmp/aap-p9-domain-raw/pointcloud.json`、`/tmp/aap-p9-domain-status/*`、`/tmp/aap-opt-p9-domain-report.md` 未改动；未要求重跑整套 campaign。

## 2. 变更文件

| 文件                                                    | 变更                                                                                                                                                                                                                                                          |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/web/e2e/tests/workbench-pointcloud-tools.spec.ts` | 379 用例：`waitForRequest` → `waitForResponse`，断言响应成功 + 持久化 `id`/`annotation_type`/`geometry`；保留 request-geometry/doubleclick 断言                                                                                                               |
| `apps/web/e2e/tests/video-issue-context.spec.ts`        | takeover 账号改为命名空间化 `takeover-<ns>@e2e.test`，仍走真实 invite → register 路径                                                                                                                                                                         |
| `apps/api/app/api/v1/_test_seed.py`                     | `takeover_email` 属性 + 纳入 `all_user_emails`；`BUG-E2E-FILTER-` 前缀仅 `owned is None`；`_is_transaction_abort` + `_try_delete` fail-fast                                                                                                                   |
| `apps/api/tests/test_seed_owned.py`                     | 3 例回归：本命名空间 takeover 被删且**另一命名空间 takeover 与普通账号存活**；owned 清理不匹配共享 BUG 前缀（含 `bug_comments → bug_reports` 两次删除边界注释）；**受控中止行为回归**（真实清理路径注入 `40P01`，原错误上抛且后续 projects/users 删除不执行） |

## 3. 边界与限制

- 只修复授权范围内的文件；未重跑整套 P9 campaign、未改 CI/planner、未 push、未用全局管理员或 `reset()` 绕过隔离。
- 远程 CI 未运行（无 push）；所有结论来自本工作树自有一次性环境与同集群 PostgreSQL 服务端日志。
- `_try_delete` 仍保留良性的逐表跳过（表/列漂移不应中断整个有序清理）；严格 residual 守卫保持不变，仍是最终闸门。
- 本次不做更广的 actor/seed API 重构；`takeover` 仍由真实邀请路径创建，只是命名空间化。

## 4. 复发防护口径

- **行为回归**：新增 `test_seed_owned.py` 用例固定「精确 owned 清理必须删掉命名空间 takeover actor 且不触碰邻居」与「owned 清理不得匹配共享 `BUG-E2E-FILTER-` 前缀」。
- **诊断/行为回归**：受控 `40P01` 注入真实清理路径，断言原错误上抛且后续销毁语句（projects/users）不执行，清理不会被报告为成功；`_is_transaction_abort` 只识别 `40P01`/`40001`，不把 `25P02` 当作独立信号。良性逐表失败的行为由既有 `test_owned_cleanup_exposes_residuals_on_partial_failure` 覆盖。
- **浏览器侧**：涉及写请求的用例必须 await 对应 **response**（成功 + 持久化断言），避免 teardown 与自身写入竞争；本次只改被授权的 `pointcloud-tools:379`。

## 5. Review 收口与共享预览产物验证（2026-09-20）

> 本节追加 root 复核后的修正与在 **P9 共享构建产物**上的聚焦验证；不重写 §0–§4 的历史证据。

- **注释修正**：`_is_transaction_abort` 的 docstring 与 `_try_delete` 注释不再断言“ROLLBACK TO SAVEPOINT 无法恢复 deadlock / 每个错误都会中止整个事务”这类无受控证明的 PostgreSQL 结论；改为描述**实际观察**（deadlock 后当次及后续清理语句未生效、最终表现为误导性 residual）与**应用策略**（竞争类失败必须上抛，让原始原因可见）。
- **测试修正**：删除只镜像分类器实现的白盒单测，替换为真实路径的**行为回归** `test_owned_cleanup_propagates_controlled_abort_and_skips_rest`（在 `tasks` DELETE 注入受控 `40P01`，断言原始错误上抛、`DELETE projects`/`DELETE users` 未执行、解除注入后仍能 200）。**加强 actor 回归**：除普通 `example.com` 账号外，新增**另一命名空间**的 `takeover-<B>@e2e.test`，断言本命名空间 takeover 被删、邻居 takeover 与普通账号均存活；保留共享 `BUG-E2E-FILTER-` 负例，并在注释中说明 `bug_comments → bug_reports` 的两次删除边界。
- **共享产物聚焦验证 [V]**：使用 P9 的**既有共享构建产物**（不重建 app 源码）：
  - 产物：`/tmp/opencode/web-e2e-dist-253b54a03c4f0fc663b26cd9038913bce188e6b7f772de67dad1a5ec616015e1.tar.gz`，sha256 `754d61b9…` 与 `SHA256SUMS` 一致；解包 **270 文件**，`dist/index.html` sha256 `f31c3ecb…`。
  - 配置：`apps/web/playwright.preview.e2e.config.ts`（仅覆盖 webServer：owned API + `vite preview`），自有 e2e 模式，`--retries=0`，JSON + 完整日志。
  - 结果：修正后的 pointcloud 双击用例 **1 passed (20.4s)**（JSON `expected=1 / unexpected=0 / flaky=0`）；`video-issue-context` G2-5 **3 passed (55.8s)**（JSON `expected=3 / unexpected=0 / flaky=0`，0 global errors）。运行后自有 e2e 库 `users=0 / projects=0 / takeover=0`；无 deadlock、无 residual、无 `owned-cleanup failed`。
  - 未重跑整套 campaign，未重建未改动的 app 源码。

## 6. 追加诊断：discussion 外来项目 FK、视频瞬时超时与 default-4 deadlock 分诊（2026-09-20）

- **workbench-discussion:312 第二个 `seed.owned()` 500（已修复）[V][PG]**：
  - 根因：该用例创建一个名为 `E2E Demo Project` 的“外来项目”，但其 `owner_id` 是**本命名空间的 admin**；owned 预清理按命名空间名称/display_id 选择项目，不会删掉这个外来项目，于是 `DELETE users` 撞 `projects_owner_id_fkey`（PG 原证：`Key (id)=<admin> is still referenced from table projects`），strict residual 守卫报 `users:3, projects:0`。
  - 复现：自有 disposable 环境经 API 直接复现（`POST /seed/owned` → 用该 admin 建 `E2E Demo Project` → 再 `POST /seed/owned` 返回 500）。
  - 修复（仅用例）：在 `finally` 中先删除该外来项目并断言 `204`，再 `seed.owned()`，且保证 `page.close()` 无条件执行。共享产物预览 `-g 失效讨论地址` → **1 passed (22.6s)**，运行后 `users=0 / projects=0`。提交 `8e50b35c3`。
- **video-issue-context G2-1/G2-2：未决的瞬时超时（不修，如实记录）[V][PG]**：
  - 现象：P9 default-3 中 `POST /seed/video-webcodecs`（`h264-issue-context`）客户端 10s 超时，随后 teardown residual `users:3, projects:1`。
  - 证据：该路由在隔离下很快（实测 `generate_fixture` **0.27s**；`put_object` 已在 `asyncio.to_thread`，`_test_seed.py:2388/2443`）；自有共享产物预览 `G2-1` 单跑与 `G2-1|G2-2` 合跑均通过（1 passed / 2 passed），运行后 residual 0；P9 运行窗口（13:31–13:50）PostgreSQL 日志**无任何 ERROR/deadlock/lock 记录**，API 进程也未重启。
  - 结论：**精确不确定性**——未能确定是编码、存储还是进程/主机层面的瞬时停顿；没有证据支持“放弃写入的竞态”或某处同步阻塞，因此**不做**超时/重试或后端改动，留给最终受影响套件复跑判定。
- **default-4 deadlock 精确定位与修复（更正先前归属）[PG][V]**：
  - 配对语句（PG 原证 13:48:16）：进程 964615 `SELECT annotations ... FOR UPDATE`（`update_annotation`）↔ 进程 964513 `DELETE FROM tasks WHERE project_id = ANY($1)`（**owned 清理**）。
  - **精确定位（更正先前归属）**：保留的 P9 日志显示该 traceback 紧跟在 `[34/67] ... workbench-pointcloud-edit.spec.ts:562`「点选 box_3d → PSR 面板出现 → 改 cx → 几何 PATCH 落库」之后；该用例只 `waitForRequest` 取请求体、**未 await 响应**，断言后即结束，teardown 清理与该几何 PATCH 的 `SELECT annotations FOR UPDATE` 行锁竞争 → deadlock，写入 500 未被断言。先前把归属记到 candidate-decisions 属误记（该 spec 无 PATCH、两个用例通过），现更正。
  - 修复（测试侧，已授权）：`waitForRequest` → `waitForResponse`；断言响应 `ok()` 与持久化 `geometry.center[0] ≈ 3`；保留原请求体几何断言与真实 PSR 编辑/双击断言。
  - 验证：共享产物 `253b54a0` 预览 `--project=pointcloud -g "几何 PATCH 落库"`、`--retries=0`，连续两次 **1 passed**，日志无 deadlock/ASGI 异常/500，运行后 `users=0 / projects=0`。
  - default-4 的 3 个 unexpected 为：`workbench-tool-dock`（浏览器被关闭）、`workbench-topbar`（P9-owned 布局）、`workbench-pointcloud-tools`（已由 `90c80922b`/`8024717c8` 修复的 pointcloud 清理残差）。
  - 同文件其余「仅 `waitForRequest`/`page.on('request')` 计数、结束时仍可能留写」的写法（`:670`/`:960`/`:1020`）属同型潜在竞态；本轮未对其取证，故未改动。
