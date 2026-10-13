# 仓库优化 P6（剩余稳定域）：seed 修复、页面收尾、provenance 清理与审计

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P6 工作包，ML/共享运行时子集见 [34](./34-repository-optimization-p6-ml-runtime.md)）
> 工作树基线：`3a94e75607393c552f58ccad6256c3a6afea14cb`（ML 子集已并入 root 后的检出点，分支 `worktree-agent-opt-p6-main`）
> 输入：`/tmp/aap-opt-p6-preaudit.md` rev2、[26](./26-repository-optimization-baseline.md)、[27](./27-repository-optimization-ledger.md)、[28](./28-repository-optimization-p1-contracts.md)、[29](./29-repository-optimization-p2-fixtures.md)、[30](./30-repository-optimization-p3-frontend-tests.md)、[32](./32-repository-optimization-p4-backend-ownership.md)、`/tmp/aap-opt-p4-report.md`
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[KEEP]** 明确保留并给出理由；**[GAP]** 未执行或归属后续阶段
> 并行边界：P5 拥有 Workbench 及其架构文档；P7 拥有 `apps/web/e2e/**`、`_test_seed*`、`test_seed_owned.py` / `test_filter_seed.py` / `test_seed_router.py` 与浏览器夹具文档；P8 拥有后续 workflow / planner / 构建变更。以上均未触碰。

## 0. 结论

1. **seed 缺陷修复 [V]**：`apps/api/scripts/seed.py` 的开发账号从已废弃的平台 `annotator` / `reviewer` 改为 `employee`（`viewer` 保持只读），并新增幂等的显式项目成员职责；`seed_scale.py` 改经 `project_members` 解析标注员并为其压测项目写职责；截图种子 persona 键不变、平台角色同步为 `employee`。在一次性 `aap_wt_*_test` 库上完成播种 / 复跑 / 访问解析实测。
2. **AdminPeople 徽章修复 [V]**：卡片徽章改为按平台身份呈现（含中文标签），删除「平台角色 === `annotator`」这一恒为假的比较；行为以组件测试固定。
3. **ReviewPage 纯 URL 规则下沉 [V]**：新增 `reviewUrlState.ts`（读取 / 比较作用域、选批次、清 assignee、回概览、开合任务抽屉），页面只保留装配；保留 P3 已论证的异步迟到响应 mock 边界。UsersPage 去掉重复的 `ApiError` 假类；其整体 MSW 化已在后续 P6 子任务完成（§3.2）。
4. **provenance 清理 [V]**：`gpu_arbitration/ledger` 6 处「Extracted verbatim from legacy …」叙事删除，`check_removed_service_modules.mjs` 的 5 项 `PROVENANCE_FILES` 白名单同步删除（扫描器保持绿）；该扫描器 allowlist 里不存在的根路径改为真实路径 `apps/api/scripts/check_removed_service_modules.py`。SDK / scripts / config / instruction 的版本叙事按「当前契约保留、历史叙事删除」清理。
5. **两条 P4 复核候选均为有据 KEEP [V]**：通知「列表 vs 投递」谓词差异被请求期认证挡住（停用账号无法到达列表路径）；`mask_repair_rollback` 的信号取消不回填领域行是有意契约。均未改语义。
6. **测试清单缺口修复 [V]**：TSV 从 1042 行补录到 **1128** 行（ML backend 70 + 共享 5 + P1/P3/P4 新增 4 + 截图 spec 4 + 本阶段自产测试 2），并在最终提交 HEAD 上以双向比对证明无残余缺口；`protocol_v2`（163 passed）与`mask_utils`（41 passed）的 CPU 实测补齐。P6 收口后更新为 **1136 行 = 1132 可执行 + 4 支撑**（§10/§11，[26§10]）。
7. **文档 [V]**：新增 `docs-site/dev/concepts/repository-map.md`（模块归属与调用链），`docs-site/dev/testing.md` 增加分层归属与 ML/shared 未接线事实。

## 1. seed：平台身份与项目职责（缺陷修复 + 独立提交）

### 1.1 修复内容

| 文件                                             | 改动                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/api/scripts/seed.py`                       | `qa` / `anno` / `anno2` / `anno3` 平台角色 `reviewer` / `annotator` → `employee`；新增 `DEMO_PROJECT_DISPLAY_IDS`（P-COCO8 / P-VIDEO-DEV / P-PC-DEV / P-PC-MULTI / P-OCR）与 `_ensure_demo_memberships()`：按 persona 授予最小职责（anno/anno2/anno3=标注员、qa=质检员、viewer=观察者），`pm` / `admin` 通过 ownership / 超管管理，**不**写成员行 |
| `apps/api/scripts/seed_scale.py`                 | 标注员改为 `users ⋈ project_members (role='annotator')` 解析；为压测项目显式插入 `annotator` 成员行（`ON CONFLICT DO NOTHING`）；`--purge` 级联清理成员                                                                                                                                                                                           |
| `apps/api/app/services/screenshot_seed_spec.py`  | `USER_SPECS` 的 persona 键不变（`annotator` / `reviewer` 是逻辑角色名），平台角色改为 `employee`；`_seed_users` / `build_screenshot_seed_catalog` 的校验随之成立                                                                                                                                                                                  |
| `apps/api/tests/test_seed_demo_memberships.py`   | 新增 4 个聚焦测试：全量 fixture 项目×职责矩阵、幂等与角色漂移修复、缺 persona 跳过、生产常量形状                                                                                                                                                                                                                                                  |
| `apps/api/tests/test_screenshot_seed_catalog.py` | 夹具用户平台角色同步为 `employee`（2 处）                                                                                                                                                                                                                                                                                                         |

职责只覆盖种子声明用途：`seed_scale` / 截图夹具没有任何把 `anno2` / `anno3` 指派到具体任务的逻辑，因此按 `seed.py` 自述身份授予与 `anno` 相同的标注员职责，不扩大到其他项目。

### 1.2 实测（一次性库）

环境：`pnpm dev:worktree`（本检出 `aap_wt_c2820af87ec94679_test`，迁移 head 0174）。全部命令经 `exec --mode test` 执行，`settings.database_url` 已核实解析到该一次性库。

```bash
pnpm dev:worktree -- init --mode test                       # READY
pnpm dev:worktree -- exec --mode test -- sh -c \
  'cd apps/api && PYTHONPATH=. .venv/bin/python scripts/seed.py --profile demo --offline'
# → exit 0；7 个账号全为合法平台角色；P-VIDEO-DEV / P-PC-DEV 建立；"add project memberships=10"
pnpm dev:worktree -- exec --mode test -- sh -c \
  'cd apps/api && PYTHONPATH=. .venv/bin/python /tmp/opencode/aap-p6-seed-validate.py'
# → exit 0；anno/anno2/anno3=member(annotator,4 caps)、qa=member(reviewer,5 caps)、
#   viewer=member(viewer,3 caps)、pm=P-VIDEO-DEV owner(kind=owner,9 caps)、
#   pm 对 admin 持有的 P-PC-DEV 为 fail-closed 404「项目不存在」；成员行 10，无重复
#   （再次运行 seed.py → exit 0，无任何 "add memberships" 行 = 幂等）
pnpm dev:worktree -- exec --mode test -- sh -c \
  'cd apps/api && PYTHONPATH=. .venv/bin/python scripts/seed_scale.py 20 2 5'   # exit 0
pnpm dev:worktree -- exec --mode test -- sh -c \
  'cd apps/api && PYTHONPATH=. .venv/bin/python scripts/seed_scale.py --purge'  # exit 0
```

- **离线夹具的精确边界**：`--offline` 下 `coco8 夹具缺失: <worktree>/third-party/coco8`，因此 P-COCO8 未建，成员矩阵实测覆盖 P-VIDEO-DEV 与 P-PC-DEV（2×5=10 行）；P-OCR / P-PC-MULTI 属 `--profile screenshots`，本轮未运行。/helper 对**全部 5 个** display id 的行为由 `test_seed_demo_memberships.py` 覆盖，不依赖媒体。
- **区分两个问题**：`platform role=annotator/reviewer + 零成员关系` 是**既有缺陷**（迁移 0174 后即存在）；实现过程中一度出现的 `MissingGreenlet` 是**本次引入并被测试捕获的实现缺陷**（复用了被夹具 `rollback()` 过期的 ORM 实例），已改为在 helper 内按 email 重读用户，不属于历史产品缺陷。
- **失败首因：本人 seed 实测残留，不是 conftest 隔离缺陷 [V]**。第一次全量后端聚焦测试失败的重复键（`ix_users_email admin`、`uq_tasks_display_id T-1`）来自**同一一次性库中先前 seed 实测提交的行**：`next_display_id(db, "tasks")` 在空库上产出 `T-1`、`T-2`（`display_id.py` 的「前缀 + 序列值」格式），而既有测试 `test_project_attribute_schema_and_batch_reset.py` 直接硬编码 `display_id=f"T-{i}"`，两者必然相撞；`admin` 同理来自 `seed.py` 提交的账号。**没有跨用例残留通道**：仅重置本工作树自有库后重跑同组，38 个测试全部通过；运行结束后再查该库，`T-1`、`seed-anno`、`P-SEED-0`、`admin` 计数均为 0、`tasks` 总数为 0，证明 SAVEPOINT 隔离在干净库上正常、测试 teardown 未泄漏任何行。本阶段因此**没有**修改 conftest，也没有为适配残留而改测试语义；只对本工作树自有库执行了一次 `dev:worktree -- reset --mode test --confirm <doctor 确认值>`，处置方式与协调方口径一致。

## 2. AdminPeople 徽章（缺陷修复 + 独立提交）

- 修复：`apps/web/src/pages/Admin/AdminPeoplePage.tsx` 新增 `platformRoleLabel()` / `platformRoleBadgeVariant()`（super_admin=danger、project_admin=accent、employee=ai、viewer/default=默认），卡片徽章渲染中文平台角色标签。
- 删除的缺陷比较：`item.role === "annotator"` 把平台角色与项目职责比较，员工角色迁移后恒为假 → 所有卡片渲染同一 `ai` 徽章。未新增 API 字段：卡片本就以 `main_metric_label` 表达职责，平台徽章无需职责信息。
- 回归：`AdminPeoplePage.test.tsx` 新增「平台角色徽章按平台身份呈现」用例（标签 + 徽章色 + 职责字符串不命中旧分支），并修正原卡片断言；该文件 16 个用例全绿。

## 3. 前端 P3 followthrough

### 3.1 ReviewPage（本轮完成）

- 新增 `apps/web/src/pages/Review/reviewUrlState.ts`：`readReviewQueueParams` / `reviewQueueIsUnchanged` / `reviewQueueScopeKey`（保留 `\u001f` 连接契约）/ `selectReviewBatch` / `clearReviewAssignee` / `clearReviewSelection` / `readReviewTaskId` / `setReviewTaskId`。
- `ReviewPage.tsx` 三段重复的 `URLSearchParams` 复制-删除-写回与散落的 `searchParams.get` 全部改走该模块；`queueUrlRef` / `queueScopeKey` 用同一读取函数，快照 + 复查语义不变。
- `reviewUrlState.test.ts` 7 个用例覆盖：读取容错、作用域键五维变化、选批次保留 assignee 与无关参数、清 assignee/概览、抽屉开合。
- **保留** P3 已论证的异步守卫 mock 边界（`queueScopeKeyRef` / `checkedIdsKeyRef` 快照复查）：那是页面自有编排规则，hook mock 是正确层级。`Review` 目录 34 用例全绿。

### 3.2 UsersPage 整体 MSW 化（已完成）

- 移除 `@/hooks/useUsers`、`@/hooks/useProjects`、`@/hooks/useGroups`、`@/hooks/usePermissions`、`@/stores/authStore`、`@/api/users`、`@/api/client` 七处耦合假桩（含在假 hook 内按 role/search 过滤用户的 `useUserPage`）；页面改跑真实 query hooks / 权限表 / auth store，数据经 MSW 在 HTTP 边界描述（新增 `src/test/usersApi.ts`：`/users/query`、`/users/stats`、`/users`、`DELETE /users/:id`、`/users/:id/admin-reset-password`、`/users/export`、`/projects`、`/groups`），每个用例使用独立 QueryClient / MemoryRouter / 播种 auth。
- 保留（有据 KEEP）：7 个重弹窗组件的窄替身——保护的是页面路由/装配契约，弹窗各自有专测；`@/components/ui/Toast` 的 `push` 窄探针——真实 store 委托 sonner 且不持有列表，这是页面 toast 契约的可观察面。
- 契约映射（全部实测）：防抖恢复 + 浏览器前进/后退 + 历史分条（真实 URL→请求）；项目/角色/状态/搜索请求塑形（`/users/query`、`/users/stats` 同参）；分页（page/page_size）；导出（`/users/export?format=csv&…`）；确认删除（真实 `DELETE` + 成功 toast）；403 无权限文案（真实 `ApiError.status` 映射）；已停用账号的继续交接/恢复入口；project_admin 可操作范围与超管只读；跨项目 `is_lifecycle_managed=false` 隐藏生命周期写入口；离线暂停两态（`onlineManager`）。
- 结果：`Users` 目录 27 用例（UsersPage 22 + usersUrlState 5）全绿；`tsc --noEmit`、改动文件 eslint 通过；未发现产品缺陷（无需产品改动）。防抖边界用「先让真实初始查询落地，再窄开假时钟 249ms/1ms，随后恢复真实时钟」的方式复现，避免假时钟冻结 MSW 网络调度。

### 3.3 明确延后（有据，非静默）

- **Dashboard / ProjectDetailPanel / Datasets / Annotate**：KEEP。P3 [30§2.1] 已逐文件记录「各自 mock 是不同关注点的明确契约，未替换整页 query+权限+store 栈」，preaudit §5.4 复核同结论（12/11/7/7 个 mock，各有归属），无新证据推翻。

## 4. provenance 与守护脚本

| 项                                                                                                                      | 改动                                                                                                                                                                                                                                                            | 验证                                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `gpu_arbitration/ledger/{__init__,keys,store,types,validation,scripts/lua_sources}.py`                                  | 删除 6 处「Extracted verbatim from the legacy …」叙事，docstring 改述当前职责                                                                                                                                                                                   | `node scripts/check_removed_service_modules.mjs` → exit 0（23 modules guarded）                                      |
| `scripts/check_removed_service_modules.mjs`                                                                             | `PROVENANCE_FILES` 5 项及 `isAllowed` 分支删除；`ALLOWED_FILES` 中不存在的根路径 `scripts/check_removed_service_modules.py` 改为真实 `apps/api/scripts/check_removed_service_modules.py`；收口阶段把 7 个历史文档中 43 处指向已删模块的死链改为行内代码历史路径 | 同上；`--historical-links` **由红转绿**（原报 15 个 文件×模块 对，见 §7.1），现默认与 `--historical-links` 均 exit 0 |
| `CLAUDE.md`（`AGENTS.md` 符号链接保留）                                                                                 | 「Put provenance in comments or frontmatter」→「Record provenance in Git history, `CHANGELOG.md`, or ADRs — not in source comments or frontmatter」                                                                                                             | `AGENTS.md -> CLAUDE.md` 未动                                                                                        |
| `packages/python-sdk`                                                                                                   | `tui/ml_stats_ws.py`、`tui/app.py`、`models.py`、3 个测试文件的版本前缀与「自 vX 起接受」叙事删除；ADR-0044 引用（现行契约）保留；`pyproject.toml` 注释同步                                                                                                     | `grep -rn "v0\.[0-9]" src tests pyproject.toml` = 0 命中                                                             |
| `.pre-commit-config.yaml` / `.github/workflows/ci.yml` / `apps/api/tests/conftest.py` / `apps/web/playwright.config.ts` | 5 处版本前缀叙事删除（保留 ADR-0050、协议与工具版本 pin）                                                                                                                                                                                                       | 见 §7 候选清单                                                                                                       |

**保留（真实历史 / 契约）**：`scripts/eval_simplify.py` 的版本标签是评测报告对被评测变更集的描述（文档化手动工具）；`rapidocr-backend` 两处 `docs/plans/2026-06-29-v0.20.0-…md` 引用是真实归档文档指针；`docs/adr/archive`、`docs/changelogs` 的历史记录不动。

## 5. P4 复核候选：审计结论（未改语义）

### 5.1 通知「列表 vs 投递」谓词差异 — KEEP

- 事实：列表路径 `_accessible_project_clause()`（`notification.py:166`）对管理型账号加 owner 臂但不检查 `is_active`；投递路径 `_allowed_delivery_indices()`（`:372-381`）要求 owner 行 `User.is_active` 且平台角色为管理型。两者成员臂共用 `valid_membership_conditions()`（含活跃校验）。
- 追踪：列表路径唯一入口是 `GET /notifications` / `unread-count`（`api/v1/notifications.py`），依赖 `get_current_user`（`app/deps.py:111-112` 对 `not user.is_active` 抛 401）。停用账号**无法到达**列表路径；投递是后台动作，不在请求认证闸门内，必须自带活跃校验。
- 结论：不是可触发缺陷；把 `project_scope_clause()`（对停用返回空集）替换进列表路径不会改变任何可达行为，反而把「认证已挡」的假设藏进 SQL。两侧的成员契约已经单一来源。
- 证据：`test_notification_delivery_scope.py` 已覆盖投递侧成员/owner/超管组合；本轮未新增重复测试。

### 5.2 `mask_repair_rollback` 信号取消不回填领域行 — KEEP

- 事实：`async_job_terminal.reconcile_mask_repair()` 在 `cancelled=True` 且 `job.kind != "mask_repair"` 时直接返回（`:121-135`），docstring 明示意图；回滚终态由 worker 拥有（成功 `rolled_back` / 失败 `rollback_failed`，`workers/mask_repair.py:731,757`）。
- 追踪：回滚只能从已 `completed` 的修复发起；分片回滚逐片提交，`result_json["rollback"]` 只在成功/失败整体写入。硬取消（revoke）不是该生命周期的受支持转移，通用兜底若把它写成 `cancelled` / `partial`，会把一个仍属「已完成修复」的批次标成失败态，正是计划 §4.3 禁止的语义抹平。
- 结论：保持现状；「回滚被硬取消后分片已部分回退」属于需要独立设计的运维语义（断点续滚或补偿），不在重构内夹带。

## 6. P0 候选清单收口（[26§6] #1–#10）

| #   | 候选                                    | 状态                                                                                                                                                                               |
| --- | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `httpx_client_bound` 别名               | **P2 已关闭** [29]                                                                                                                                                                 |
| 2   | `_install_legacy_class_kwargs_shim`     | **P2 已关闭** [29]                                                                                                                                                                 |
| 3   | `coalesce_legacy_into_tool_bindings`    | **KEEP**（兼容边界，[29]）                                                                                                                                                         |
| 4   | 前端 `"annotator"` 字符串命中           | **P1 已分类** [28]；本轮修正其中一处真实比较缺陷（§2）                                                                                                                             |
| 5   | `signals.py` 重复生命周期               | **P4 已关闭** [32]                                                                                                                                                                 |
| 6   | 7 个 spec 内联 `ERR_ABORTED` 分类       | **P7 已收口** [31]：收敛为 `apps/web/e2e/helpers/request-errors.ts` 的有类型规则 + 16 例允许/禁止边界测试；本阶段未动其文件                                                        |
| 7   | 活动文件版本叙事                        | **本轮关闭**：`signals.py` / `vitest.setup.ts` / `vite.config.ts` 已由 P2–P4 清零；`conftest.py` / `playwright.config.ts` / `ci.yml` / `.pre-commit-config.yaml` 残留 5 处本轮删除 |
| 8   | `useWorkbenchShellModel.tsx` 混合 model | **P5 已收口** [33]：装配 model 8919 → 6647 行，8 条职责下沉领域模块/纯模块与 shell 组件；本阶段未动其文件                                                                          |
| 9   | `ProjectDataManagerPage.flow.test.tsx`  | **P3 已关闭** [30]                                                                                                                                                                 |
| 10  | `test_v0_7_6.py` 版本命名               | **本轮关闭**：改名 `test_project_attribute_schema_and_batch_reset.py`（内容本就用现行 fixture/模型），`batch-module.md` 与 TSV 同步                                                |

## 7. 测试清单补录与共享包 CPU 实测

- 本节记录 P6 主阶段快照 **1128** 行（ci-wired 1024 / script-wired 15 / not-wired 89），分层计数可加和；双向比对（tracked 测试路径 ↔ 清单，显式排除 vendor / fixtures / conftest / `__init__` / generated / node_modules / checkpoints）在**最终提交 HEAD** 上重跑后**无残余缺口**；[26§9] 记录了缺口成因分类。**P6 收口后已更新为 1136 行 = 1132 可执行测试 + 4 测试支撑模块**（新增 `test-support` 层；补录 `test_conftest_db_url.py`、6 个 P5 Workbench 测试、`scripts/test_alembic_migration_policy.py`），最新分层/接线计数见 [26§10]。
- 补录明细：ML backend 70（合并 2 个重复套件后的当前路径）、`backend_runtime` 下沉套件 1（`replacement` 列记录 old→new）、`protocol_v2` 4、P1/P4 新增后端测试 2、P3 新增前端测试 2、截图 spec 4、**本阶段自产测试 2**（`test_seed_demo_memberships.py`、`reviewUrlState.test.ts`，收口时回填）、**P7 新建 1**（`test_seed_owned.py`，rebase 后回填）。
- 收口比对确认清单中 4 行测试支撑模块（`apps/api/tests/{conftest,factory,_avatar_storage,__init__}.py`）是 P0 已登记的测试基建，不属于发现缺口（[26§9.1]）。`scripts/test-orca-worktree-setup.py` 是**真正的可运行测试脚本**（`ci.yml` 直接 `uv run python`，非 unittest discover），已登记为 `worktree-runtime` / `ci-wired`；首版说明误列为支撑文件，已在发现模式补上连字符 `test-*.py` 后更正，总数仍为 1127。
- 共享包 CPU 实测：`apps/_shared/protocol_v2` 163 passed / 0 skip；`apps/_shared/mask_utils` 41 passed / 0 skip（各自 `.venv`，`pytest -q`）。

## 7.1 历史死链清理（`--historical-links` 由红转绿）

`check_removed_service_modules.mjs --historical-links` 原报 15 个「历史 markdown 链接指向 v0.23.2 已删服务模块」（对应 43 处链接，扫描器按 文件×模块 去重计数）。处置：把 7 个历史文档（`docs/changelogs/0.10.x.md` 与 6 个 `docs/plans/archive/*.md`）中指向已删路径的链接全部改为**行内代码形式的历史路径**——保留「当时该模块叫什么」的事实，不再渲染成可点击死链；未改写任何历史结论、未删除事实，也未放宽扫描器或 allowlist。`docs/changelogs/0.10.x.md` 卷首补一条路径说明，指向现行的
[服务导入切换迁移说明](../migration/2026-07-17-v0.23.2-service-import-cutover.md)（`export→exporting.service`、`export_packaging→exporting.packaging`、`export_cache→exporting.cache`、`export_video→exporting.video`、`video_tracker_runner→video_tracking.runner`、`video_tracker_adapters→video_tracking.adapters`、`video_tracker_job_service→video_tracking.jobs`）。验证：默认与 `--historical-links` 两种模式均 exit 0。

## 8. 文档

- 新增 `docs-site/dev/concepts/repository-map.md`：模块归属、允许依赖方向、授权真值、进入标注→提交→异步→通知调用链、ML backend 调用链、测试层级速记；导航 `docs-site/.vitepress/navigation/dev.ts` 同步。
- `docs-site/dev/testing.md`：新增「分层归属与运行入口」，写明 `apps/_shared` 三个共享包与五个 ML backend 的测试**未接线**（静态检索事实）与本地运行方式；未新建 `testing-strategy.md`（复用现有页面，按协调方要求）。
- `CHANGELOG.md` Unreleased：seed 与 AdminPeople 两条 Fixed。
- `pnpm docs:build` 通过（33.36s，exit 0）。

## 9. 提交与复核

| 提交                        | 内容                                                                                                                                         |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `33cb40970`                 | chore: provenance/守护脚本/指令清理（ledger docstrings、mjs allowlist、CLAUDE.md、SDK、config/CI 注释、`test_v0_7_6.py` 改名）               |
| `a6bdf3c94`                 | fix(api): seed 平台身份与显式项目职责 + 聚焦测试                                                                                             |
| `dc782f793`                 | fix(web): AdminPeople 平台身份徽章 + 回归                                                                                                    |
| `e7570f40e`                 | refactor(web): Review 纯 URL 下沉 + UsersPage 去重桩                                                                                         |
| `60add232f`                 | docs: repository-map、testing 分层、26/27/README/TSV 台账与本文件                                                                            |
| `1518ee254`                 | chore: 删除旧版本命名测试文件路径 `apps/api/tests/test_v0_7_6.py`                                                                            |
| `4d0244dd3`                 | docs: TSV 收口至 1127 行（补录 2 个自产测试）+ 历史死链清理（`--historical-links` 绿）+ 本文件收口更新                                       |
| `本提交`（doc 35 所在提交） | docs: 把 `scripts/test-orca-worktree-setup.py` 归为可运行测试 + rebase 到 root `b8b45797e` 后补录 P7 的 `test_seed_owned.py`（合计 1128 行） |

`git diff --check` 通过；`node scripts/check-doc-version-prefix.mjs --staged` 无发现；pre-commit（trailing-whitespace / end-of-file / prettier / ruff / ruff-format）通过。

> 本表 SHA 为 P6 分支 rebase 到已接受 P7 root `b8b45797e` **之后**的值（rebase 无冲突；旧 SHA 已随变基重写）。P7 引入的 `apps/api/tests/test_seed_owned.py` 已在 rebase 后回填清单（见 §7）。

## 10. 未完成与归属

- **P5（已接受）**：Workbench 收敛已由 P5 交付并接受（`9e34dfafb`，证据 [33]：`canBatchConvert`/helpers 为有据 KEEP，R1/R2 注释与死导出已清理）；`workbench-shell.md` / `video-annotation-workbench.md` 与本文的 P5 归属已对账；`repository-map.md` 的 Workbench 调用链同步更新。
- **P7（已接受）**：P7 root `b8b45797e` + 证据 [31]；P7 的 `_test_seed*`、`apps/web/e2e/**`、`test_seed_owned.py` 与错误分类器保持 P7 语义，P6 未改其文件（仅回填测试清单）。
- **P8（未接受）**：workflow / planner / 构建变更为阶段产物（[37]/[38]），其选择器与工作流修正尚未接受，P6 未改其文件、未标记完成。89 个 not-wired ML/shared 测试经独立 CPU 审计证明**全部 CPU 可运行**（12 个需 CPU torch），执行入口为 `.github/workflows/ml-cpu-test.yml` + `scripts/run-ml-cpu-tests.sh`，PR caller 待 P8 收口。
- **§7.2 迁移验证**：真实验证 runner `scripts/validate_migrations.py` + fail-closed 策略 `scripts/alembic_reversible_floor.py` + 33 例策略测试已随 P6 接受（[39]）；CI caller 由 P8 接入（未收口）。
- **UsersPage 整体 MSW 化**：已完成（§3.2；提交 `0e90da462`，Users 目录 27 用例全绿）。
- **P0 候选 #1–#10**：全部关闭或有据 KEEP（§6）；无剩余 P6 owned 候选。
- **`test_project_attribute_schema_and_batch_reset.py` 的硬编码 `display_id=T-{i}`**：该文件与 `next_display_id` 的 `T-<seq>` 命名在同一库上互斥，属既有测试脆弱性（P2 验收库当时为空库）；本轮只记录，不夹带改测试语义。

## 11. P6 收口对账（2026-09-20）

- P6 实现已在 root `11d81d05c` 接受（[34] ML/shared + 本文剩余稳定域 + P5 残留 + Users MSW `0e90da462` + §7.2 迁移验证 [39]）；状态更新见 [27§1]/[27§6]，最新清单快照见 [26§10]。
- 收口后 TSV **1136 行 = 1132 可执行 + 4 支撑**（ci-wired 1031 / script-wired 15 / not-wired 90），双向比对 0 缺口；`test-support` 层与可执行测试分离。
- 未决仅剩 P8 选择器/工作流（未接受）与 P9/P10；本文不再新增 P6 owned 项。
