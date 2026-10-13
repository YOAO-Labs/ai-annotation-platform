# 仓库优化 P3：前端测试边界与高 mock 流程清理

> 盘点日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P3 工作包 + §5.2）
> 基线提交：`0144b734c`（P0+P1 已合入，等于本工作树 P3 起点）
> 输入台账：`docs/research/26-repository-optimization-baseline.md` §4（不变量映射）、`docs/research/28-repository-optimization-p1-contracts.md`（P1 契约固化）
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[F]** 产品缺陷修复（附回归与失败前/修复后证据）；**[GAP]** 未执行或留待后续阶段

## 0. 结论

1. data-manager 页面测试已重建为 MSW API 边界集成：页面运行真实 query / 权限 hooks，测试在 HTTP 边界描述响应，不再一次性替换整页的 query hook、权限 hook 与 store **[V]**。
2. URL / 视图纯规则（视图 key、请求视图解析、缺失视图回退、URL 覆盖优先级、失效排序回退）下沉到 `dataManagerUrlState.test.ts`，页面复用同一批导出函数 **[V]**。
3. 未处理的目标 API 请求现在会让改造后的集成测试明确失败；非目标资源通过已记录的允许边界保持仅告警 **[V]**。
4. 发现并修复一个真实缺陷：schema 请求失败时页面反复重新挂载错误分支，持续重取 schema 而无法显示重试入口。修复为独立提交并附回归测试 **[F]**。
5. 覆盖率阈值未变（`lines/statements/functions=45`、`branches=70`）；删除逐版本阈值流水账，记录 `src/test/**` 作为测试基础设施的排除口径。画布 / WebGL 排除项保留，并明确几何单测仍在、真实渲染仍由浏览器验证 **[V]**。
6. 样板已在下一个合理候选 `ProjectSettingsPage.test.tsx` 落地：query 与权限 hooks 走真实 MSW + 账号播种，重型 section 明确作为装配替身，并补充「非负责人 employee 被拒」用例。其余高 mock 页面在 §2.1 逐文件记录边界与责任阶段 **[V]**。

## 1. 范围与边界

- **只做 P3 / §5.2。** 前端测试基础设施、Projects data-manager 页面测试及直接关联的纯 URL 模块；不改 `apps/web/e2e/**`、`apps/web/scripts/video-request-errors.test.ts`、Workbench 几何测试（P7 负责）和后端文件。
- **保持业务语义。** API 路径、产品行为与无关工作未改动，唯一例外是独立的 schema 重试缺陷修复。
- **保留画布 / 浏览器区分。** react-konva DOM 替身及其局限说明保留；Konva stage 与 Three.js 渲染器的 jsdom 覆盖率排除保留，理由记录在 `vite.config.ts`。
- **保留覆盖率阈值。** 未下调阈值，也未通过扩大排除项来补绿。

## 2. 相对 §5.2 的改动

| §5.2 条目                                             | 改动                                                                                                                                                                                                                      |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 · 保留 MSW，在 API 边界描述响应                     | 流程套件改用 `installDataManagerApi()`（`src/test/dataManagerApi.ts`）与真实页面 hooks；`server.use` 覆盖 project、access、schema、task views、task query、summary、matches 与任务详情。                                  |
| 2 · 纯 URL / 视图规则下沉                             | `dataManagerUrlState.ts` 导出 `dataManagerViewKey`、`requestedDataManagerViewKey`、`findDataManagerView`、`resolveDataManagerViewKey`、`shouldUseDataManagerUrlOverrides`；页面复用，`dataManagerUrlState.test.ts` 覆盖。 |
| 3 · 真实的 `employee + 项目职责` 数据                 | access fixture 返回 `platform_role: "employee"` 与项目级 `project_role`、显式 capability 集；不伪造已退役的平台 `annotator` / `reviewer` 身份。                                                                           |
| 4 · 小型 render / QueryClient / Router / auth fixture | `src/test/queryClient.ts`、`src/test/auth.ts`、`src/test/renderWithProviders.tsx` 只提供每测试独立 QueryClient、真实 auth store 播种与 Router/Provider 包装，不做包含所有页面状态的万能 helper。                          |
| 5 · 意外 API 请求失败                                 | `src/test/apiRequestGuard.ts` 记录 MSW 未处理的目标 API 请求；`vitest.setup.ts` 接线，改造后的套件在每个用例后断言 `expectNoUnexpectedApiRequests()`。                                                                    |
| 6 · 不把扩大全局异步等待当常规修复                    | 保留 5s 全局 `asyncUtilTimeout` 作为安全网，并改写说明：它不是错误 mock 或未等待 timer 的替代品；新增集成测试的等待都限定在对应操作范围内。                                                                               |
| 7 · 保留 Konva 局限                                   | `vitest.setup.ts` 保留 react-konva DOM 替身，并写明它不验证真实 canvas 渲染。                                                                                                                                             |
| 8 · 清理覆盖率叙事                                    | `vite.config.ts` 删除逐版本阈值 / case 数历史，保留当前阈值与排除口径说明。                                                                                                                                               |

### 2.1 向其他页面候选的推广

计划要求「先以 data-manager 为样板，再按清单推广到其他页面」。统计 `apps/web/src/pages` 中 mock 内部依赖最多的套件后，得到下表。区分**已接受的合理 mock 边界**与**真正延后的关注点**，并给出明确责任阶段，避免把延后写成永久解决。

| 候选                                                                                                                                                           | mock 的内部依赖                                                                                                   | 处理决定 | 类别           | 理由与责任阶段                                                                                                                                                                                                                                                                                                                         |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | -------- | -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Projects/ProjectSettingsPage.test.tsx`                                                                                                                        | `useProject`、`useIsProjectOwner`、`usePermissions` + 12 个 section                                               | REWRITE  | 已迁移         | 页面自身契约是路由 / 深链 / 项目角色装配；query 与权限 hooks 现走 MSW + 播种 `project_admin`，重型 section 明确为装配替身，并新增非负责人 `employee` 拒绝用例。                                                                                                                                                                        |
| `Users/UsersPage.test.tsx`                                                                                                                                     | `useUsers`、`useProjects`、`useGroups`、`usePermissions`、`authStore` + `@/api/users` + `@/api/client` + 7 个弹窗 | REWRITE  | 已迁移（P6）   | P6 已将整套改到 MSW API 边界：移除上述 7 处假桩，改用真实 query hooks / 权限表 / auth store + `src/test/usersApi.ts`，保留 7 个弹窗窄替身与 toast `push` 探针；契约（URL/防抖/历史、请求塑形、导出、删除、403、权限与生命周期、离线两态）全部保留并新增两条回归，`Users` 目录 27 用例全绿（[35§3.2]）。原「P6 复核是否仍合适」已关闭。 |
| `Admin/AdminPeoplePage.test.tsx`                                                                                                                               | `useDashboard`、`@/api/dashboard`、`useProjects`、`usePermissions`、`@/api/tasks`、`react-router-dom`             | KEEP     | 合理边界已接受 | 保护管理端人员列表的查询参数与路由 search-param 映射，`@/api/dashboard` 正是被测的请求塑形边界；P1 记录的平台角色徽章缺陷归 **P6**。                                                                                                                                                                                                   |
| `Dashboard/DashboardPage.test.tsx`、`AIPreAnnotate/components/ProjectDetailPanel.test.tsx`、`Datasets/DatasetsPage.test.tsx`、`Annotate/AnnotatePage.test.tsx` | query hooks + 重型子组件                                                                                          | KEEP     | 合理边界已接受 | 各自的 mock 是不同关注点的明确契约（dashboard 聚合、预标注配置、数据集向导、标注路由），均未像 data-manager 样板那样替换整页 query + 权限 + store 栈。复核责任归 **P6**。                                                                                                                                                              |
| `Review/ReviewPage.test.tsx`                                                                                                                                   | `useDashboard`、`useTasks`、`useBatches`、`authStore` + 重型子组件                                                | KEEP     | 真正延后       | 与审核工作台渲染器重叠**不足以证明**当前 mock 边界已最优：需在 **P6 followup** 复核队列路由与批次 / 任务 hooks 是否应下沉；与 **P5** 的审核工作台职责存在重叠，故本阶段不并入。                                                                                                                                                        |
| Workbench shell / panel 套件（`CommentsPanel`、`AIInspectorPanel`、`WorkbenchLayout`、几何）                                                                   | workbench state hooks                                                                                             | KEEP     | 真正延后       | 归属 **P5**（Workbench 职责收敛），几何 / 渲染部分归属 **P7**；此处改边界会与 Workbench 工作流冲突。                                                                                                                                                                                                                                   |

## 3. data-manager 流程测试处理

处理决定只使用计划的规范标签：`KEEP` / `REWRITE` / `MOVE_DOWN` / `MERGE` / `DELETE`。

| 原测试                                                                                                    | 处理决定  | 去向 / 理由                                                                                                                                                                                                   |
| --------------------------------------------------------------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| enter / members 区域不挂任务查询                                                                          | REWRITE   | API 边界进入用例，另加 capability 门控的成员区用例。                                                                                                                                                          |
| URL 水合完成后才查询                                                                                      | MOVE_DOWN | 纯规则进 `dataManagerUrlState.test.ts`；页面侧仍断言水合后只发起一次查询。                                                                                                                                    |
| layout / sort 覆盖时保留保存视图条件                                                                      | MOVE_DOWN | 由 `shouldUseDataManagerUrlOverrides` 与恢复用例的请求体共同覆盖。                                                                                                                                            |
| 视图切换                                                                                                  | REWRITE   | 保存视图切换断言 URL 与下一次查询条件。                                                                                                                                                                       |
| 浏览器前进 / 后退                                                                                         | MOVE_DOWN | 视图 key 解析由纯测试覆盖；页面只保留一条代表性历史往返。                                                                                                                                                     |
| 深链选中任务                                                                                              | REWRITE   | 使用真实 `GET /tasks/:id` 边界。                                                                                                                                                                              |
| 失效保存排序回退                                                                                          | MOVE_DOWN | 新增 `resolveDataManagerSort` 回退用例。                                                                                                                                                                      |
| 旧 feedback 列归一化                                                                                      | REWRITE   | 同时断言查询列载荷与渲染表头。                                                                                                                                                                                |
| schema 失败重试                                                                                           | REWRITE   | API 边界 500 + 专门的「不循环重取」回归。                                                                                                                                                                     |
| 创建完成后所有者已变更                                                                                    | REWRITE   | 延迟 MSW create handler 仍证明所有者守卫。                                                                                                                                                                    |
| gallery 复选框详情                                                                                        | KEEP      | 在边界重新保留为 `does not open task detail from the gallery selection checkbox`；无其他套件覆盖，因此保留而非删除。                                                                                          |
| 筛选未完成不查询                                                                                          | KEEP      | 重新保留为 `does not query while a newly added condition has no value`；纯规则在 `dataManagerFilterExpression.test.ts`，页面级「不查询」此前无覆盖。                                                          |
| 嵌套过滤上限                                                                                              | MOVE_DOWN | 纯规则 `dataManagerFilterExpression.test.ts > rejects structural depth and node budgets before recursive validation`；页面级「告警且不查询」重新保留为 `rejects a deeply nested URL filter before querying`。 |
| 该套件对 `useTaskViews`、`useProjectAccess`、`usePermissions`、`useTask`、stores 与重型子组件的 `vi.mock` | DELETE    | 改为真实 hooks + MSW；页面层仅保留路由 stub。                                                                                                                                                                 |

## 4. 真实缺陷与修复 **[F]**

`apps/web/src/hooks/useTaskViews.ts::useDataManagerSchema` 现设置 `retryOnMount: false`。

- **现象：** schema 请求失败时，`ProjectDataManagerPage` 因 `schemaQ.isLoading` 为真而显示加载壳，卸载错误分支；错误分支重新挂载又让 React Query 重试已失败的 query（`retryOnMount` 默认 `true`），回到加载并循环。本地复现产生数百次 schema 请求，重试界面始终不出现。
- **回归：** `apps/web/src/pages/Projects/ProjectDataManagerPage.schemaError.test.tsx` 对 500 schema 响应渲染页面，断言重试入口出现、不误发任务查询，并在点击重试后 MSW handler 记录**恰好两次** schema 请求（首次加载 + 显式重试）。
- **失败前 / 修复后 [V]：** 去掉 `retryOnMount: false`（修复前行为）后回归在 13.6s 因 `Unable to find role="alert"` 失败（页面在 loading ↔ error 间循环）；恢复该行后约 0.3s 通过，且 handler 记录两次 schema 请求（首次加载 + 显式重试）。证据留存 `/tmp/aap-p3-schema-before.log`。
- **提交：** 与测试重构分离的 `fix(web): ...` 提交，包含 `CHANGELOG.md` 的 Fixed 条目与 `docs-site/user-guide/projects/data-manager.md` 的加载失败 / 重试说明。

## 5. 实际执行的检查

除特别说明外，命令均在仓库根目录执行。

| 检查                              | 命令                                                                                                                                                                                                                                                                       | 结果                                                            |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 改造目标套件（最终）              | `pnpm --filter @anno/web test src/pages/Projects/ProjectDataManagerPage.flow.test.tsx src/pages/Projects/ProjectDataManagerPage.schemaError.test.tsx src/pages/Projects/ProjectSettingsPage.test.tsx src/pages/Projects/data-manager/dataManagerUrlState.test.ts src/test` | 5 文件 / 34 用例通过 **[V]**                                    |
| Projects + 测试基础设施（评审前） | `pnpm --filter @anno/web test src/pages/Projects src/test`                                                                                                                                                                                                                 | 34 文件 / 255 用例通过 **[V]**                                  |
| 前端全量套件（评审前）            | `pnpm --filter @anno/web test`                                                                                                                                                                                                                                             | 555 文件 / 5581 用例通过，exit 0 **[V]**                        |
| 覆盖率阈值                        | `pnpm --filter @anno/web test:coverage`                                                                                                                                                                                                                                    | 语句 / 行 72.37%、分支 79.42%、函数 66.6%，达标，exit 0 **[V]** |
| 类型                              | `pnpm --filter @anno/web typecheck`                                                                                                                                                                                                                                        | exit 0 **[V]**                                                  |
| Lint + CSS tokens                 | `pnpm --filter @anno/web lint`                                                                                                                                                                                                                                             | exit 0，无新增告警 **[V]**                                      |
| 格式                              | 对改动文件执行 `prettier --check`                                                                                                                                                                                                                                          | 干净 **[V]**                                                    |
| 空白                              | `git diff --check`                                                                                                                                                                                                                                                         | 干净 **[V]**                                                    |

### 5.1 全量套件与覆盖率

改动前基线为 553 文件 / 5581 用例、语句 / 行 72.48%。改动后全量套件为 555 文件 / 5581 用例通过，语句 / 行 72.37%、分支 79.42%、函数 66.6%，与基线在噪声范围内，且远高于未变的 `45/45/45/70` 阈值。小幅变化来自新增的 `src/test/**` 排除与页面测试重写，不是扩大排除项：未新增任何产品代码排除。日志：`/tmp/aap-p3-test-full.log`、`/tmp/aap-p3-coverage.log`。

评审回合在既有文件上新增 4 个用例（三条恢复的 data-manager 行为 + 一条 settings 拒绝用例）；上述目标套件在改动后已重跑并通过。按评审要求未为统计而重复全量套件，该回合也没有改动与覆盖率口径相关的排除项。

## 6. 未完成与后续归属

- **增量迁移：** 其余高 mock 页面候选在 §2.1 逐文件记录边界与责任阶段。严格意外请求 guard 按 §5.2 第 5 条分批接入，这些套件在各自迁移前仍保持仅告警。
  - `UsersPage`：**P6 已改到 MSW API 边界**（REWRITE，27 用例全绿，见 §2.1 与 [35§3.2]）；`AdminPeoplePage`、`DashboardPage`、`ProjectDetailPanel`、`DatasetsPage`、`AnnotatePage`：当前 mock 边界已由 **P6** 复核确认仍合适（KEEP），无新证据推翻。
  - `ReviewPage`：**P6 followup**，需证明或改进当前 mock 边界（渲染器重叠不构成最优证明），并与 **P5** 的审核工作台职责协调。
  - Workbench shell / panel：**P5**；几何 / 渲染部分 **P7**。
- **顺带缺陷：** P1 记录的 `AdminPeoplePage` 平台角色徽章比较仍归 **P6**，不阻塞 P3。
- **浏览器验证：** 改造后的套件是 jsdom 集成测试。真实 canvas / WebGL、焦点与指针行为未变，仍由 Playwright 覆盖；本阶段未运行。

## 7. 回退边界

- 改造后的测试与 fixture 是增量、隔离的；回退 P3 测试提交即可恢复原页面流程套件，不影响产品行为。
- 缺陷修复只改一行 hook 选项；`git revert` 去掉该行，回归测试记录被有意回退时的预期行为。
- 未改动迁移、锁文件、生成类型、API 契约与 CI 工作流。
