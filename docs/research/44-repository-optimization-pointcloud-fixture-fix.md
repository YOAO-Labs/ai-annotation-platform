# 仓库优化 P7 修复：点云 E2E owned 夹具消费者回归

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P7 owned 夹具隔离；本轮修复消费者回归）
> 工作树/分支：`/home/hehao/桌面/ai-annotation-platform-worktree-agent-opt-p6-ml`（`worktree-agent-opt-p7-pointcloud-fix`，基于 accepted root `b8897df28`）
> 证据图例：**[V]** 本工作树实测

## 0. 结论

1. **回归确认 [V]**：P7 的 `seed.owned()` 按 namespace 派生身份（`apps/api/app/api/v1/_test_seed.py:83`：`admin-{namespace}@e2e.test`），但 6 个点云 spec 仍以全局 `admin@e2e.test` 调 `injectToken`/`accessToken`/`createTaskAnnotation`/`deleteTaskAnnotation`/`setPetEnabled`，共 **37 处**；干净一次性库上该账号不存在，`seed/login` 404，测试在登录夹具处即失败。
2. **修复 [V]**：改为捕获 `SeedData.admin_email` 并把该命名空间的 exact actor 贯穿到每个用例的注入与本地调用；无别名回退、不创建全局 admin、不使用 `reset()`、不做隐式 email 改写。6 文件（edit 17、tools 9、smoke 5、quality 4、layout-matrix 1、playback 1）。
3. **实测 [V]**：在**全新**自有一次性 e2e 库（`users_total=0`，无 `admin@e2e.test`）上，代表性用例 `workbench-pointcloud-tools.spec.ts`「B 键一次激活…」通过（1 passed，32.2s）；运行后该库仍 `users_total=0`、`owned_admins=0`、`global_admin=0`，即 namespace 精确清理且未创建全局账号。
4. **诊断 [V]**：`seed.injectToken` 的失败信息补充响应体（与 `accessToken` 一致），使 404「用户不存在」可直接读出；行为不变。
5. **越界消费者修正（follow-up）[V]**：`apps/web/e2e/tests/lidar-export-trusted.spec.ts` 的同源回归已在本 follow-up 修正——`seed.owned()` 返回的 `admin_email` 已贯穿 `injectToken`；此外同一 P7 提交 `589f95855` 还把 lidar 项目名从字面量改为 `E2E Lidar {namespace}`（`_test_seed.py:129`），该 spec 的 `hasText: "E2E Lidar Project"` 随之过期。最终按 `lidar.lidar_project_id` 经既有 API 边界 `GET /api/v1/projects/{id}`（`projects.py:875`）解析该项目的精确 `name`，再用精确项目名定位自有行——不匹配邻位夹具的共享前缀，也不改动 UI 菜单/导出流程与断言。全新库实跑 **1 passed (20.1s)**，运行后 `global_admin=0 / owned=0 / users_total=0`。`video-issue-context.spec.ts:1495` 的 `takeover@e2e.test` 是测试经真实邀请路径**创建**的账号（非回归），`seed-auth-injection.spec.ts` 使用合成 SeedAPI（不触库）。

## 1. 修复范围

| 文件                                                   | 硬编码处 | 处理                                                                                                                                    |
| ------------------------------------------------------ | -------: | --------------------------------------------------------------------------------------------------------------------------------------- |
| `e2e/tests/workbench-pointcloud-edit.spec.ts`          |       17 | `const data = await seed.owned()` + `data.admin_email`                                                                                  |
| `e2e/tests/workbench-pointcloud-tools.spec.ts`         |        9 | 同上（含 `setPetEnabled`）                                                                                                              |
| `e2e/tests/workbench-pointcloud-smoke.spec.ts`         |        5 | 同上（含 `createTaskAnnotation`/`deleteTaskAnnotation`/`accessToken`）                                                                  |
| `e2e/tests/workbench-pointcloud-quality.spec.ts`       |        4 | 同上（`accessToken` + `injectToken`）                                                                                                   |
| `e2e/tests/workbench-pointcloud-layout-matrix.spec.ts` |        1 | 同上                                                                                                                                    |
| `e2e/tests/workbench-pointcloud-playback.spec.ts`      |        1 | `prepare()` 内捕获 `owned`，注入 `owned.admin_email`                                                                                    |
| `e2e/tests/lidar-export-trusted.spec.ts`（follow-up）  |   1 + 名 | `const data = await seed.owned()` + `data.admin_email`；按 `lidar_project_id` 经 `GET /api/v1/projects/{id}` 解析精确项目名以定位自有行 |
| `e2e/fixtures/seed.ts`                                 |        — | `injectToken` 失败信息补响应体（fixture 诊断）                                                                                          |

`workbench-pointcloud-layout-hydration.spec.ts` 与 `workbench-pointcloud-settings.spec.ts` 原本就用 `data.admin_email`，未改。

## 2. 验证

| 检查              | 命令/证据                                                                                                                                                             | 结果                                                                                       |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 类型检查          | `pnpm --filter @anno/web exec tsc --noEmit`                                                                                                                           | exit 0                                                                                     |
| Lint（改动文件）  | `pnpm --filter @anno/web exec eslint <6 specs + seed.ts>`                                                                                                             | exit 0                                                                                     |
| 干净库前置        | `SELECT count(*) FROM users WHERE email='admin@e2e.test'`                                                                                                             | `0`（`users_total=0`）                                                                     |
| 代表性用例        | `pnpm dev:worktree -- exec --mode e2e -- bash -lc 'cd apps/web && pnpm test:e2e e2e/tests/workbench-pointcloud-tools.spec.ts --project=pointcloud -g "B 键一次激活"'` | **1 passed (32.2s)** [V]                                                                   |
| lidar follow-up   | `pnpm dev:worktree -- exec --mode e2e -- bash -lc 'cd apps/web && pnpm test:e2e e2e/tests/lidar-export-trusted.spec.ts --project=chromium'`                           | **1 passed (20.1s)** [V]                                                                   |
| lidar 库前置/后置 | `SELECT count(*) FROM users ...`（跑前/跑后）                                                                                                                         | 跑前 `global_admin=0 / users_total=0`；跑后 `global_admin=0 / owned=0 / users_total=0` [V] |
| 运行后库状态      | 同库计数                                                                                                                                                              | `global_admin=0 / owned_admins=0 / users_total=0`（namespace 精确清理） [V]                |
| 邻居隔离契约      | `apps/api/tests/test_seed_owned.py`（namespace 派生、构建/清理不匹配他命名空间、清理幂等且报残）                                                                      | 复用既有后端契约证据，未重跑                                                               |

**回归守护口径**：本轮不新增“扫描文件名/统计字面量”的空洞守卫；防同一回归的**行为**证据是上面的干净库代表性运行（无全局 admin 时 owned actor 仍能贯通到画布交互）。若后续要加自动守卫，应加在 owned login 契约层（断言 `accessToken(data.admin_email)` 成功且命名空间级清理），而不是统计 spec 里的字符串。

## 3. 限制

- 仅运行一个代表性点云用例（工具键盘守护，GPU 无关）；未跑整套点云矩阵。渲染资格（严格 WebGPU）由 renderer owner 集成后执行，P9 负责软件矩阵全量。
- follow-up 已修正并实跑 `lidar-export-trusted.spec.ts`（1 passed）；该 spec 同源还有**项目名过期**一面（P7 `589f95855` 把 lidar 项目名改为 `E2E Lidar {namespace}`），已改为按 `lidar_project_id` 经既有 API 边界解析精确项目名、定位自有行，export/trust/授权断言保留。
- 无 push；最终按协调方 root rebase。
