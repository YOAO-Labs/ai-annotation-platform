# 仓库优化 P1：契约固化、旧角色测试数据修正与信号兜底回归

> 盘点日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P1 工作包）
> 基线提交：`a24fb644e3d301c8a345e6683d5f95612be08343`（P0 台账合入点，等于本工作树 P1 起点）
> 输入台账：`docs/research/26-repository-optimization-baseline.md` §4（不变量映射）、`docs/research/27-repository-optimization-ledger.md` §1（P1 验收门）
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[M]** 负向变异验证（破坏→失败→恢复）；**[GAP]** 未执行或留待后续阶段

## 0. 结论

1. 计划第 3 节 8 条业务约束（C1–C8）在 HEAD 全部映射到现存测试：57 个引用文件、39 个具名测试 ID 逐条 `rg` 复核存在，无未映射项 [V]。
2. P0 §4 记录的两处 GAP：C6「`signals.py` 信号兜底零直接测试」在本阶段补齐（`tests/test_worker_signals.py`，8 例 [V]），并以变异验证证明终端守卫确实被测试捕获 [M]；C8「conftest 默认测试库回退吞掉配置错误」仍属 P2 整改项 [GAP]。
3. 修正了 7 个既有后端测试文件与 27 个前端测试文件中的**旧平台角色**测试数据（`annotator`/`reviewer` → 现行平台身份 `employee`），并新增 1 个后端信号回归文件；合法项目职责字符串（`ProjectRole = annotator/reviewer/viewer`）按 P1 约定原样保留 [V]。
4. 未发现需要产品修复的真实缺陷；产品代码零改动（`git diff --name-only apps/api/app` 为空）[V]。
5. P0 §4 关于 `useProjectAccess.test.tsx`「fixture 仍用旧 annotator」的判断不成立：该处是 `project_role: "annotator"`，`platform_role` 已是 `employee`，属合法项目职责，无需修改 [V]。

## 1. 范围与边界

- **只做 P1**：契约映射复核、旧平台角色测试数据修正、必要的负向回归；不迁移 ORM shim、不删别名、不重构前端流程（P2/P3 未开始）。
- **保持业务语义**：API 路径、生成类型、模型、迁移元数据、产品行为不变；产品代码文件未改动。
- **真实缺陷独立提交**：本轮未发现需要独立 fix 提交的产品缺陷（见 §7 的观测项与归属阶段）。
- **测试库隔离**：后端测试在 `pnpm dev:worktree --mode test` 供给的本工作树自有库 `aap_wt_3ca3e833cfeaf760_test`（head `0174`）上执行；未触碰共享 `annotation_test`/`annotation_e2e`。

## 2. 第 3 节不变量 → 测试映射（HEAD 复核）

层级：**U** 单元/纯函数 · **I** 后端集成 · **B** 浏览器 E2E。全部引用文件与具名 ID 由本工作树脚本逐条确认存在 [V]。

| 约束                                            | 权威保护（现有测试，摘要）                                                                                                                                                                                                                                                                                                                                                                                                                    | P1 动作                                                         |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| C1 平台身份不推导职责；能力不串用；未知拒绝     | I `test_project_access.py`（capability matrix / stale identity / non-member hidden / viewer×work fails closed / export denied）、`test_employee_project_roles_acceptance.py`（跨项目、bulk 能力、自审/未知证据）；U `useProjectAccess.test.tsx`、`usePermissions.test.ts`（legacy 角色 fail closed）；B `employee-project-roles.spec.ts`、`workbench-secondary-permissions.spec.ts`                                                           | 修正旧平台角色测试数据；保留 `project_role` 字符串；变异 B2 [M] |
| C2 自审禁止、证据冻结、未知不可审核             | I `test_review_evidence_guard.py`（7 例）、`test_project_member_concurrency.py::test_unknown_review_evidence_blocks_review_receiver`、`test_batch_review_evidence_guard.py`；B `review-approve-loop.spec.ts`、`review-feedback-loop.spec.ts`                                                                                                                                                                                                  | 变异 C 覆盖自审/证据拒绝 [M]                                    |
| C3 撤权/停用/转移后重新校验授权                 | I `test_project_member_concurrency.py`（revocation atomic、stale actor、ownership/receiver change、handoff scoped）、`test_employee_project_roles_acceptance.py::test_notification_delivery_uses_actual_job_project_after_revocation`、`test_self_deactivation.py`、`test_users_delete_transfer.py`、`test_project_transfer.py`、`test_task_actor_lifecycle_guard.py`；U `offlineQueue.test.ts`、`useWorkbenchOfflineQueue.test.tsx`          | 修正 `test_users_delete_transfer.py` 旧角色数据                 |
| C4 已确认标注不被切换/旧响应/取消/重试覆盖      | U `useAnnotationHistory.test.ts`、`offlineQueue.test.ts`；I `test_tracker_final_write_guard.py`、`test_task_actor_lifecycle_guard.py`；B `video-issue-context.spec.ts`、`guide-autosave.spec.ts`、`workbench-video-candidate-decisions.spec.ts`                                                                                                                                                                                               | 无数据修正项                                                    |
| C5 任务锁、版本冲突、幂等键、锁顺序             | I `test_task_lock.py::TestTaskLockFlow`（5 例）、`test_task_lock_dedup.py`、`test_annotation_create_idempotency.py`、`test_batch_predict_idempotency.py`、`test_project_member_concurrency.py`（same-version race、lock bounding、materialized prelock、duplicate change）；U `canEditMask.test.ts`、`maskPrimaryActions.test.ts`；B `mask-lock-bypass.spec.ts`、`mask-session-guard.spec.ts`                                                 | 无数据修正项                                                    |
| C6 通知与事务提交顺序；失败/取消/部分完成不混淆 | I `test_discussion_notifications_commit.py`（4 例真实提交 + Redis 故障）、`test_notifications.py`、`test_discussion_notifications.py`、`test_notification_delivery_scope.py`、`test_task_reopen_notification.py`、`test_video_tracker_partial_review.py`、`test_mask_qc_m2.py::test_mask_qc_cancel_updates_generic_and_domain_ledgers`                                                                                                        | **新增 I `test_worker_signals.py`（8 例）**；变异 A [M]         |
| C7 图片/视频/点云/Mask 坐标、帧、工具、持久化   | U 几何家族（`transforms.test.ts`、`geometryTranslate.test.ts`、`geometry/*` 等）；I `test_axis_convention.py`、`test_video_frame_service.py`、`test_pointcloud_box3d_annotation.py`、`test_raster_mask_write_gate.py`、`test_image_raster_mask_api.py`、`test_video_mask_corrections.py` 等；B `workbench-image-konva-smoke.spec.ts`、`annotation-canvas-pixels.spec.ts`、`raster-mask-native.spec.ts`、`mask-advanced-operations.spec.ts` 等 | 无数据修正项                                                    |
| C8 不可逆迁移不承诺有损回滚；测试数据隔离       | I `test_alembic_drift.py::test_models_match_database`、14+ `test_migration_0NNN_*.py`；本工作树 `aap_wt_*` 自有库隔离                                                                                                                                                                                                                                                                                                                         | 映射成立；conftest 回退缺口 [GAP] 留 P2                         |

**跨层不冗余**：C1 的 U/I/B 分别保护纯能力矩阵、入口不可绕过规则、关键 UI 链路接通；C6 的 commit 顺序由集成测试证明，信号兜底由新增直接测试证明。

## 3. 旧平台角色测试数据修正

现行真值：平台身份 `users.role ∈ {super_admin, project_admin, employee, viewer}`；`annotator`/`reviewer` 仅作为**历史**可读值与**项目职责** `project_members.role` 合法存在。修正只针对被当作**平台身份**使用的字符串。

### 3.1 后端（7 个既有文件 + 1 个新增）

| 文件                                    | 旧值 → 新值                                                                   | 说明                                                               |
| --------------------------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `tests/test_factory.py`                 | `make_user_dict("annotator"/"reviewer")` → `"employee"/"viewer"`              | 工厂透传平台角色；断言同步更新                                     |
| `tests/test_users_delete_transfer.py`   | 临时用户 `role="annotator"` → `"employee"`                                    | 软删除用例，角色仅为占位                                           |
| `tests/test_management_api.py`          | `create_user(..., "annotator")` → `"employee"`                                | 目标账号平台身份；成员职责仍为项目 `annotator`                     |
| `tests/test_management_consistency.py`  | `create_user(... "annotator")`、`UserInvitation(role="annotator")` → employee | CSV 注入用例，避免构造非法平台角色                                 |
| `tests/test_invitations.py`             | 邀请 `"role": "annotator"` → `"employee"`                                     | 该用例断言 group_name 超长 422；旧角色会先被校验拦截，掩盖真实断言 |
| `tests/test_auth_refresh.py`            | `_seed_user(role="annotator")` → `"employee"`                                 | 刷新令牌用例，角色无关；避免伪造历史账号                           |
| `tests/test_users_role_matrix.py`       | 见下                                                                          | 旧角色矩阵语义修正                                                 |
| **新增** `tests/test_worker_signals.py` | 新文件                                                                        | C6 直接回归（见 §4）                                               |

`test_users_role_matrix.py` 具体修正：

- `test_sa_upgrade_legacy_annotator_to_employee` 原先复用 `annotator` fixture（实际是 `employee`），等于空转；改为在库里真正构造 `role="annotator"` 的历史行，再验证 super_admin 可升级为 `employee` → 200。这才是迁移升级路径的真实保护。
- `test_sa_upgrade_reviewer_to_pa` → `test_sa_promotes_employee_to_project_admin`；`test_annotator_cannot_change_roles` → `test_employee_cannot_change_roles`；`test_reviewer_cannot_change_roles` → `test_non_admin_denied_even_with_legacy_role_input`；`test_pa_cannot_promote_to_pa` 注释同步。断言值与守卫逻辑未放宽。
- 保留 `test_sa_rejects_legacy_global_staff_role_input`（提交 `reviewer` 被 400 拒绝）——这是现行负向契约。

### 3.2 前端（27 文件，59 处）

只替换被当作 `MeResponse.role` / `useAuthStore.user.role` / `usePermissions().role` / 平台角色过滤值的字符串（`annotator`/`reviewer` → `employee`）。涉及注册邀请、通知偏好与 WebSocket、作业铃铛、离职、TopBar、路由守卫、stores、Admin/Users URL 状态、data-manager 权限 mock 等。

> 分类勘误：`src/utils/screenshotMatrix.test.ts` 的 `ScreenshotScene.role` 属文档媒体场景枚举（`"admin" | "annotator" | "reviewer"`），不是平台身份；`tsc` 复核时已还原，未计入本阶段修正。

**明确保留（合法项目职责 / 成员能力，非平台角色）**：

- `project_role` / `project_member_role` / `my_project_role` / `current_role` / `target_role` 为 `annotator`/`reviewer` 的用法（`useProjectAccess.test.tsx`、`MemberRoleChangeModal.test.tsx`、`EmployeeDashboard.test.tsx`、`AnnotatorDashboard.test.tsx` 等）。
- 成员列表/批量分派对象上的 `role: "annotator"|"reviewer"`（`AssignMemberModal.test.tsx`、`BatchAssignmentModal.test.tsx`、`DataManagerTaskActions.test.tsx`、`useProjectMentionCandidates.test.tsx`）。
- `AdminPeoplePage` / `adminPeopleUrlState` / `useDashboard` 的 `role` 过滤：后端 `dashboard/admin/people` 已将 `annotator/reviewer` 解释为**项目成员职责**（`app/api/v1/dashboard/admin.py` L223-238），属合法值；仅把两个响应 fixture 中的平台角色 `role` 改为 `employee`。
- `usePermissions.test.ts` 与 `test_batch_permissions.py` 中故意使用历史 `reviewer`/`annotator` 的 **fail-closed 负向断言**。

**P0 映射勘误**：P0 §4 C1 曾记 `useProjectAccess.test.tsx`「fixture 仍用旧 `"annotator"` 字符串 → P1 数据修正」。实际该文件 `platform_role: "employee"`、`project_role: "annotator"`（合法），无需修改。此条按合法项目职责处理。

## 4. 新增回归：`signals.py` 信号兜底直接测试（C6 GAP）

`tests/test_worker_signals.py`（8 例，真实库 + 独立连接）通过 `monkeypatch` 把 `signals.settings.database_url` 指向夹具 `TEST_DATABASE_URL`，先提交种子行再用信号函数自己的 engine 读取，避免 SAVEPOINT 会话掩盖跨连接可见性问题。

| 测试 ID                                                            | 保护内容                                                               |
| ------------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `test_mark_failed_marks_job_and_notifies_once`                     | crash → `failed` + `error_message` + `job.failed` 通知仅一次           |
| `test_mark_failed_never_overwrites_terminal_job`                   | 终态作业不被迟到的信号覆盖、不产生错误通知                             |
| `test_mark_failed_unknown_task_id_is_silent`                       | 查不到 celery_task_id（如 ml_health）静默、不抛错                      |
| `test_mark_failed_unknown_kind_marks_generic_without_notification` | 非白名单 kind 只改通用状态、不伪造通知                                 |
| `test_mark_cancelled_sets_cancelled_and_notifies`                  | revoke → `cancelled` + `job.cancelled` 通知                            |
| `test_mark_cancelled_mask_format_import_keeps_partial`             | 有 committed 项 → `partial`；无 → `cancelled`（不被通用 handler 抹平） |
| `test_mark_failed_mask_repair_rollback_keeps_rollback_failed`      | 回滚失败 → `rollback_failed`；普通修复失败 → `failed`                  |
| `test_signal_handlers_ignore_missing_task_id`                      | 无 task_id 时 `task_failure`/`task_revoked` 处理函数不动库、不抛错     |

## 5. 负向变异验证（破坏 → 确认失败 → 恢复）

按计划 §5.1，对安全/持久化约束做最小定向破坏，确认保留测试确实会失败，再恢复。生产文件均通过 `git checkout --` 还原，最终 `git diff --name-only apps/api/app` 为空 [V] [M]。

| 变异 | 破坏点                                                                              | 观察到的失败测试                                                                                               | 结果         |
| ---- | ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------ |
| A    | `workers/signals.py::_mark_failed` 删除终态短路                                     | `test_worker_signals.py::test_mark_failed_never_overwrites_terminal_job`                                       | 由绿转红 [M] |
| B2   | `services/project_access.py::resolve_project_access` 删除 viewer×work fail-closed   | `test_project_access.py::test_viewer_with_work_membership_fails_closed`（200 ≠ 403）                           | 由绿转红 [M] |
| C    | `services/annotation_evidence.py::assert_review_evidence_current` 删除自审/证据拒绝 | `test_review_evidence_guard.py` 4 例（frozen contributor / submitter / effective annotator / malformed actor） | 由绿转红 [M] |

（附注：对 `membership_role_compatible` 纯函数的 viewer 分支破坏未使 `test_project_access.py` 变红，因其运行期授权由 `resolve_project_access` 内的独立守卫承担；该函数由成员写入路径覆盖，非本阶段新增缺口。）

## 6. 实际执行的检查与结果

均在 `pnpm dev:worktree --mode test`（自有库 `aap_wt_3ca3e833cfeaf760_test`，head `0174`）执行。

| 检查            | 命令                                                                                                                                                                    | 结果                                                                                                                                   |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 新增信号回归    | `pytest -q tests/test_worker_signals.py`                                                                                                                                | 8 passed [V]                                                                                                                           |
| 改动后端文件    | `pytest -q test_users_role_matrix.py test_factory.py test_management_api.py test_management_consistency.py test_invitations.py test_users_delete_transfer.py`           | 64 passed [V]                                                                                                                          |
| 完整后端套件    | `pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python -m pytest -q --durations=10 -p no:cacheprovider'`（留存 `/tmp/aap-p1-backend.log`） | 执行到 `[100%]`，进程退出码 **0**，`FAILED`/`ERROR` 计数为 0；最终计数行被 pytest quiet addopts 静默，按退出码与无失败标记判定通过 [V] |
| 前端全量 vitest | `pnpm --filter @anno/web test`                                                                                                                                          | 5580 passed / 0 failed（首次发现的 1 处 fixture 断言已修复）[V]                                                                        |
| 映射复核        | 57 文件 / 39 具名测试 ID 逐条核对                                                                                                                                       | 全存在 [V]                                                                                                                             |

## 7. 未执行、保留与后续归属

- **[GAP] C8 `conftest._default_test_db_url()` 回退**：仍会吞掉配置错误并落到历史默认连接串；显式失败/日志化是计划 §5.3-8 的整改项，归 **P2**。本阶段只用 `aap_wt_*` 自有库运行，未改 conftest。
- **P0 §4 勘误**：`useProjectAccess.test.tsx` 的 `annotator` 是项目职责，非旧平台角色（见 §3.2）。
- **观测项（不属 P1）**：`apps/web/src/pages/Admin/AdminPeoplePage.tsx` 的角色徽章 `item.role === "annotator"` 比较的是平台角色（后端返回 `users.role`），PR #129 后恒为假，仅影响徽章配色，非业务语义缺陷；归 **P6** 复核。
- **非测试残留**：`apps/api/scripts/seed_scale.py` 仍查询 `users WHERE role='annotator'`（迁移后恒空），属脚本陈旧引用；归 **P6**。
- **截图场景**：`apps/web/e2e/screenshots/**` 与 `apps/api/tests/test_screenshot_seed_catalog.py` 的角色键属文档媒体域；归 **P8/P6**，本轮不动。
- **台账更新**：因 P0 文档修正工作树并发占用 `docs/research/26`、`27`，本阶段证据落在本文件；`27` 的 P1 行与「当前有效决定/未执行检查」由协调方合并时回填。

## 8. 回退边界

- 本轮只改测试数据与新增测试，**产品代码零改动**，回退代价为删除对应测试 diff 即可，不影响运行语义。
- 若需回退：`git revert <P1 提交>`；`test_worker_signals.py` 为独立新增文件，可直接删除而不影响其他测试。
- 未触碰迁移、锁文件、生成类型、CI 工作流与远端保护。
