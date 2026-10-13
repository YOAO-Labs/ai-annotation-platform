# 仓库优化 P4：后端规则归属、作业分派与入口授权审计

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P4 工作包，§4.2 / §4.3 / §5.3.7）
> 基线提交：`963633e47008ee77e63ded064653a632036328ee`（P1 真实 signals 测试 + P2 fixture 迁移 + P3 前端测试已合入点，等于本工作树起点）
> 输入：`docs/research/26`（不变量映射）、`27`（台账）、`28`（P1 契约）、`29`（P2 fixture 与 §5.3.7 缺口）
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[GAP]** 未执行或留待后续阶段
> 并行边界：本阶段不修改 `docs/research/26`、`27`、README 与 TSV；`apps/api/app/api/v1/_test_seed*` 与 `tests/test_filter_seed.py`、`tests/test_seed_router.py` 归 P7，未触碰。

## 0. 结论

1. 项目授权真值收敛到 `apps/api/app/services/project_access.py` 的两个纯函数：`platform_role_is_manager` 与新增的 `is_privileged_for_project`。删除 `project_aggregates` 中重复的 `platform_role_is_manager`，并把 scheduler、worker、批次状态机、成员变更、绩效、data-management、mask、video 的全部直接调用方改为从 `project_access` 导入 [V]。
2. `workers/signals.py` 收敛为薄适配器：只保留“连接 → 查作业 → 落通用终态 → 调用领域回填 → 通知 → 提交 → 释放”的骨架；四类领域账本（`MaskQCRun` / `PointCloudQualityRun` / `MaskRepairBatch` / `MaskFormatImport`）的终态差异进入 `services/async_job_terminal.py` 的封闭分派。`partial` / `rollback_failed` / `cancelled` 语义与“终态不被迟到信号覆盖”原样保留 [V]。
3. `async_jobs` 硬取消端点中 mask_qc / point_cloud_quality 的重复领域更新改为复用同一 `cancelled` 回填；mask_repair / mask_format_import 的“仅 pending 硬取消、running 转 `cancel_requested`”语义与回填规则不同，明确保留独立实现 [V]。
4. 通知的发布时序没有改动：`notify_job_terminal` 仍按调用方契约在 `mark_*` 之后、调用方 `commit()` 之前发布，WS 投递闸门短暂重试读取未提交行。P4 只做规则归属，不机械替换通知发布方式 [V]。
5. 新增 `tests/factory.py::create_membership`，收敛 10 个重复的本地 `_add_member` helper 与 87 处内联 `db.add(ProjectMember(...))`（合计 97 处）；其余 63 处测试内联构造按语义差异保留，另有工厂自身构造器 1 处不计入调用点（§5）[V]。
6. 纯规则测试与真实事务测试分层不变：fixture 惰性求值，纯规则测试无需测试库即可运行；`test_worker_signals.py`、`test_discussion_notifications_commit.py`、`test_project_member_concurrency.py`、`test_export_final_guard.py` 的独立连接/提交保护原样保留（§6）[V]。
7. 本阶段未发现需要独立修复提交的真实产品缺陷；产品行为零变化。观察到一处“取消回滚作业不落领域终态”的非对称行为，判断依据不足，记录为 P6 复核候选（§7.4）[GAP]。

## 1. 范围与边界

- 只做 P4 / §4.2 / §4.3 与 P2 遗留的 §5.3.7 成员 factory 收敛；不改 API 路径、协议、数据库模型、迁移元数据与产品语义。
- 不改前端、E2E、`_test_seed*` 与 P7 所属测试；`tests/factory.py` 只新增函数，既有导出与 seed 调用方签名不变。
- 保留 `resolve_project_access` 的请求期语义（每次重读账号/项目、`FOR SHARE` 锁定、未知角色 fail closed）。纯函数只服务已经加载/锁定行的调用方。

## 2. 授权真值收敛（提交 `aa8b2965a`）

### 2.1 单一真值源

`apps/api/app/services/project_access.py`：

- `platform_role_is_manager(platform_role)`：平台角色是否管理型（`super_admin` / `project_admin`），不包含所有权。
- `is_privileged_for_project(user, project)`：活跃账号且为超管，或“该项目 owner 且平台角色为管理型”。等价于 `resolve_project_access()` 的 `ACCESS_KIND_SUPER_ADMIN` / `ACCESS_KIND_OWNER`。

`project_aggregates.py` 删除本地重复定义，改为从 `project_access` 导入（模块内 `project_scope_clause` / `is_platform_manager` 继续使用同一实现）。

### 2.2 迁移的调用方

| 入口族             | 文件                                                                                                | 处理                                                                  |
| ------------------ | --------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| scheduler / 派题   | `services/scheduler.py`                                                                             | 内部改用 canonical；对外 `is_privileged_for_project` 名称保留         |
| 批次状态机         | `services/batch_permissions.py`                                                                     | `_is_owner` 成为有文档的批次域委托（`services/batch.py` re-export）   |
| 成员变更           | `services/project_membership.py`                                                                    | `_assert_actor_can_manage` 复用 canonical（先判活跃、再判管理身份）   |
| 绩效               | `services/project_performance.py`                                                                   | `resolve_performance_access` 复用 canonical                           |
| worker             | `app/workers/tasks.py`                                                                              | 两处改从 `project_access` 导入                                        |
| data-management    | `services/data_management/service.py`、`task_metrics.py`、`task_filters.py`                         | 改从 `project_access` 导入；`task_visibility_clause` 仍来自 scheduler |
| Mask / 视频        | `services/mask_mutation.py`、`mask_repair.py`、`annotation_conversion.py`、`video_tracking/jobs.py` | 改从 `project_access` 导入                                            |
| 任务路由           | `api/v1/tasks/video.py`                                                                             | 改从 `project_access` 导入                                            |
| 聚合 / 通知 / 看板 | `services/notification.py`、`api/v1/dashboard/reviewer.py`、`tests/test_project_aggregate_scope.py` | `platform_role_is_manager` 改为从 `project_access` 直接导入           |

架构说明同步在 `docs-site/dev/concepts/visibility-and-permissions.md` 记录两个纯函数与 `resolve_project_access` 的关系。

### 2.3 明确不合并的相似代码

- `deps.require_roles` 与 `deps.require_active_task_actor`：用于**平台级**管理入口（users / groups / storage / 部分 dashboard），不是项目授权矩阵。
- `services/storage_connection.py::_is_super_admin`：连接器为全局域（`scope` / `created_by`），与项目成员关系无关。
- `services/management.py` / `user_lifecycle.py` 的平台角色比较：全局身份管理，不读项目职责。
- `api/v1/dashboard/reviewer.py::_reviewable_claim_clause`：SQL 内联 `platform_role_is_manager(user.role) AND owner` 与 canonical 同义；属于聚合 SQL 的谓词拼装，依赖 `project_scope_clause` 已提供的范围，不另建 Python 判定。

## 3. 入口族授权审计结论

| 入口族   | 现有真值                                                                                                                                                                                                       | 结论                                                                                         |
| -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 路由     | `require_project_capability` / `resolve_project_access`（data_manager、tasks、point_cloud_quality、async_jobs、batches 等）                                                                                    | 已是 canonical；未见第二套能力矩阵。`require_roles` 仅覆盖平台级入口 [V]                     |
| worker   | `project_write_guard.assert_phase_write_allowed`、export/video_tracker/cross_frame 的 `resolve_project_access`                                                                                                 | 写前重解析 + 锁序 account→membership/project 保留；本次仅统一 scheduler 纯函数的导入来源 [V] |
| 导出     | 创建 `require_project_capability(EXPORT_ANNOTATIONS)`；worker `_assert_export_task_scope` / `_reauthorize_export_final_write` 重解析能力并复核任务可见性；结果访问 `_can_access_job` 再查 `EXPORT_ANNOTATIONS` | 创建/执行/缓存命中/结果交付四点同源，未改动 [V]                                              |
| 通知     | `NotificationService._scope_conditions`（列表）与 `_allowed_delivery_indices`（发布/投递）两套可见性；均复用 `valid_membership_conditions`                                                                     | 两处语义同源但 SQL 形态不同（相关子查询 vs 批量 pair）；未强制合并，见 §4.3 [V]              |
| 离线重放 | 后端无独立离线入口；前端离线队列重放落到任务/标注写入口，经 `resolve_project_access` + `_assert_task_editable` + 证据守卫                                                                                      | 与项目写入口同源；Socket 撤权重校验走 `resolve_project_access` / `_assert_task_visible` [V]  |

## 4. 作业生命周期骨架（提交 `96adcde94`）

### 4.1 结构

- `apps/api/app/services/async_job_terminal.py`：按 `AsyncJob.kind` 封闭分派到各领域回填函数；只更新领域账本，不碰通用作业状态、通知与提交。
- `apps/api/app/workers/signals.py`：`_apply_terminal(celery_task_id, cancelled, error)` 统一骨架；`_mark_failed` / `_mark_cancelled` 为薄包装，供信号回调 `asyncio.run`。信号回调本身是同步函数，用 `asyncio.run` 驱动异步工作（每次构建/释放 `AsyncEngine`）。

### 4.2 保留的差异

| kind                              | failed                             | cancelled                                                     |
| --------------------------------- | ---------------------------------- | ------------------------------------------------------------- |
| `mask_qc` / `point_cloud_quality` | pending/running → `failed` + error | pending/running → `cancelled`                                 |
| `mask_repair`                     | 无状态守卫 → `failed`              | pending/running → 有完成分片 `partial`，否则 `cancelled`      |
| `mask_repair_rollback`            | 无状态守卫 → `rollback_failed`     | 兜底取消不回填（维护原行为，§7.4）                            |
| `mask_format_import`              | 无状态守卫 → `failed`              | pending/running → 有 committed 项 `partial`，否则 `cancelled` |
| 其他 kind                         | 只改通用作业状态                   | 只改通用作业状态                                              |

`failed` 路径先判通用作业是否已终态，迟到信号短路；`cancelled` 路径依赖 `mark_cancelled` 幂等 + 领域状态守卫。通用作业 `mark_*`、领域回填、通知、提交顺序不变。

### 4.3 通知发布时序为何不变

`notify_job_terminal` 的文档契约是“写通知 + 在调用方提交前发布”，被 60+ worker 调用点依赖；WS 投递闸门在独立会话中短暂重试读取尚未可见的行。计划 §4.2 / §6.5 明确“通知发布方式按调用方当前契约处理，不机械全仓替换”。P4 未改该契约，也未把发布挪到提交后。

## 5. 成员 factory 收敛（提交 `bc33713bd`，§5.3.7）

### 5.1 新增

`tests/factory.py::create_membership(db, *, project_id, user_id, role, assigned_by=None, version=None, weekly_target=None)`：只创建一条显式成员关系，保持“平台身份”和“项目职责”分离，不从平台角色推断 membership。

### 5.2 实际迁移

AST 统计：迁移前 `tests/`（不含 `tests/factory.py` 自身定义）共有 160 处 `ProjectMember(...)` 调用点（P2 报告的“75 个文件”是文件数，实际调用点 160）。本次：

- 迁移 10 个本地 `_add_member` / `_add_member_row` helper；
- 迁移 87 处异步函数内的 `db.add(ProjectMember(...))`（54 个文件）；
- 合计 **97 处**测试调用点收口到 `create_membership`（10 + 87，等于迁移前 160 − 迁移后保留 63）。

### 5.3 保留内联的 63 处（语义差异，非未完成）

迁移后 `tests/` 仍有 64 处 `ProjectMember(...)` 文本：其中 63 处是测试调用点，另 1 处是 `tests/factory.py` 自身的 `ProjectMember(**kwargs)` 构造器（不属于测试调用点）。这 63 处按外层构造方式**互斥**归类，合计 63：

| 保留形态                                        | 数量 | KEEP 理由（语义差异）                                                                 |
| ----------------------------------------------- | ---- | ------------------------------------------------------------------------------------- |
| `db.add_all([...])` 批量成员                    | 42   | 多条成员在一个工作单元里一起 flush；逐条 `create_membership` 会改变 flush 次数与顺序  |
| `x = ProjectMember(...)` 后 add/flush（Assign） | 14   | 需要持有对象引用 / 独立 seeding 会话 + commit 的并发 seed / 故意非法角色 / 带注释构造 |
| `db.add(ProjectMember(...))` 且显式 `id=`       | 3    | 调用方自行提供主键；工厂刻意不暴露 `id` 覆盖，迁移会静默丢掉这个显式选择              |
| 列表推导 / return / 生成器表达式                | 4    | 成员是集合表达式的一个元素，不是单条显式 seed（含迁移测试的 `version=3` 成员）        |

特殊字段说明（与上表重叠，不另计数）：3 处显式 `id=` 即上表第三行；1 处 `version=3` 位于 `test_project_role_migration.py` 的列表推导 + `add_all` 中。工厂虽已支持可选 `version=`，此处保留是因为它与推导及批量 add 绑定，**不是**因为工厂未建模版本。

这些保留项是“工厂只表示单条显式成员”的语义边界，**不是本轮未完成的工作**；若将来确实需要批量 factory，属新需求，需与 P7 的 seed 所有权协调。

## 6. 纯规则测试与事务测试分层

- 现状：`test_db_url` / `apply_migrations` / `test_engine` / `db_session` 都是按需 fixture；未请求数据库的测试不会触发迁移。`reset_rate_limiter` 是唯一 autouse fixture 且不访问数据库。
- 验证：清空 `TEST_DATABASE_URL` 与 `AAP_WORKTREE_MODE` 后，`.venv/bin/python -m pytest tests/test_batch_permissions.py tests/test_conftest_db_url.py tests/test_project_access_predicates.py` 通过（115+7 例）[V]。
- 本阶段新增 `tests/test_project_access_predicates.py`（7 例，无数据库），直接保护两个 canonical 纯函数。
- 真实事务/跨连接保护保留：`test_worker_signals.py`（独立 engine + committed 行）、`test_discussion_notifications_commit.py`、`test_project_member_concurrency.py`、`test_export_final_guard.py`。
- 未引入测试标记体系（沿用 P2 决定），也未把集成测试改成无库测试。

## 7. 运行时与后续归属

### 7.1 本工作树运行时

- 测试库：本 checkout 自有 `aap_wt_08d4cd28d3fcbbc6_test`（head `0174`），由 `pnpm dev:worktree --mode test` 供给；未触碰共享 `annotation_test`/`annotation_e2e`。
- Redis：本模式自有 `aap-wt-08d4cd28d3fcbbc6-test-redis`。

### 7.2 Celery 挂载与刷新处置 **[V]**

运行中的 worker 属于其他 checkout：`ai-annotation-platform-celery-worker-1` 挂载 `/home/hehao/桌面/ai-annotation-platform/apps/api -> /app`（主 checkout），另有 `aap-production-celery-worker-*` 生产栈。本工作树只启动了 test Redis，没有针对本 checkout 的 worker。因此：

- 本阶段未重启任何主 checkout / 生产 worker；
- 改动后的 `workers/signals.py` 未挂载到任何运行中的 worker，无需刷新，也**没有**在真实 Celery 进程内验证新签名；
- 信号新代码通过 `tests/test_worker_signals.py` 的独立 engine 直接验证（读取 committed 行），未做真实 worker crash 演习。

### 7.3 后续归属（P6 复核候选，非本轮缺口）

以下两项是需要后续阶段基于真实行为再决定是否统一 / 加固的**行为问题**，与“本轮未完成”区分开：

- **通知两套可见性 SQL 未合并**：`_scope_conditions`（列表，相关子查询）与 `_allowed_delivery_indices`（发布 / 投递，批量 pair）语义同源、形态不同；`_accessible_project_clause` / `_export_capable_project_clause` 与 `project_scope_clause` 的差异是前者不检查账号活跃，直接替换会收紧非活跃管理者的可见行为。归 P6 评估是否显式统一。
- **兜底取消的 `mask_repair_rollback`**：见 §7.4，作为行为问题交 P6。

§5.3 的 63 处内联构造是**已解释的语义 KEEP，不列为缺口**。真实 worker / 浏览器验证（E2E、GPU / 模型链路）归 P7 / P8；工厂批量构造如将来有需要，属新需求。

### 7.4 观察项（证据不足，不声称缺陷，归 P6）

兜底取消路径只回填 `mask_repair`，不回填 `mask_repair_rollback`。API 硬取消不包含 `mask_repair_rollback`，回滚的终态由回滚 worker 自身负责；未复现“回滚作业被 revoke 后批次行停留 running”。记录为 P6 复核候选，不在本阶段改语义。

## 8. 实际执行的检查与结果

除特别说明外，命令在仓库根目录执行；后端使用 `pnpm dev:worktree -- exec --mode test`（自有库 `aap_wt_08d4cd28d3fcbbc6_test`）。

| 检查                  | 命令 / 范围                                                                                                                                                                                              | 结果                                                     |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| 授权真值导入一致性    | 全量 import smoke + 逐文件 `rg`                                                                                                                                                                          | 无残留从 scheduler 导入 `is_privileged_for_project` [V]  |
| 授权相关测试          | `test_batch_permissions`、`test_project_access`、`test_project_aggregate_scope`、`test_worker_signals`、`test_notification_delivery_scope`、`test_project_member_concurrency`、`test_project_role_audit` | 全部通过 [V]                                             |
| 作业/领域终态测试     | `test_worker_signals`、`test_async_jobs`、`test_mask_qc_m2/m3`、`test_mask_repair_m5`、`test_mask_formats_m6/m7/m8`、`test_point_cloud_quality_api`                                                      | 全部通过 [V]                                             |
| 成员 factory 迁移测试 | 10 个含本地 helper 的套件                                                                                                                                                                                | 全部通过 [V]                                             |
| 纯规则无库运行        | 清空 `TEST_DATABASE_URL`/`AAP_WORKTREE_MODE` 运行纯测试                                                                                                                                                  | 通过，证明 fixture 惰性、纯测试不依赖数据库 [V]          |
| **完整后端套件**      | `pytest -q -p no:cacheprovider --durations=15`，日志 `/tmp/opencode/aap-p4-backend.log`                                                                                                                  | 执行到 `[100%]`，退出码 0，`FAILED`/`ERROR` 计数为 0 [V] |
| ruff check / format   | `uvx --from ruff==0.15.22 ruff ...`（与 pre-commit 同版本）                                                                                                                                              | `All checks passed`；已格式化 [V]                        |
| `git diff --check`    | 最终提交前                                                                                                                                                                                               | 无空白错误 [V]                                           |

quiet addopts 抑制计数行：以退出码 + 日志中 0 个 `FAILED`/`ERROR` 标记为准，不重复已通过套件。

## 9. 提交与回退边界

| 提交        | 内容                                                                        |
| ----------- | --------------------------------------------------------------------------- |
| `aa8b2965a` | `refactor(api): consolidate project authority on project_access`            |
| `96adcde94` | `refactor(api): extract async job domain terminal reconciliation`           |
| `bc33713bd` | `test(api): add explicit membership factory and migrate duplicated seeding` |
| `00e1e0dfe` | `test(api): pin canonical project access predicates`                        |
| `af3afdfb5` | `docs(research): record repository optimization P4 backend ownership`       |

- 五个提交（`aa8b2965a`、`96adcde94`、`bc33713bd`、`00e1e0dfe`、`af3afdfb5`）都可独立 `git revert`；本次仅文档的修正提交（成员计数、KEEP 定性与提交清单）同样可单独 revert，且不含代码变更。
- signals 重构可用 `git revert 96adcde94` 恢复原单文件实现；领域回填函数与骨架拆分不引入新迁移、锁文件或生成类型。
- 成员 factory 迁移是测试增量；回退对应提交即恢复内联构造，不影响产品行为。
- 未触碰迁移、锁文件、生成类型、CI 工作流与远端保护。
