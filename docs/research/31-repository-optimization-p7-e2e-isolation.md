# 仓库优化 P7：E2E 用例下沉、fixture 命名空间隔离与请求失败分类收敛

> 盘点日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P7 工作包）
> 基线提交：`0144b734c`（P0+P1 整合点）；工作分支已 rebase 到根工作树当前 HEAD `3a94e7560`，其上含 P2 直连模型 fixture、P3 前端测试边界、P4 后端规则归属
> 证据图例：**[V]** 本工作树实际执行/逐条核对；**[M]** 运行时探针；**[GAP]** 未执行或留待后续阶段

## 0. 结论

1. 请求失败分类收敛为一处有类型策略：`apps/web/e2e/helpers/request-errors.ts`。每条允许项都是「方法 + 精确路径 + 原因」规则，按产品域分组（session 遥测、bootstrap、dashboard、任务上下文、Issue 线程、对象评论、Mask 编辑器、视频媒体），按流程组合，**任何套件的白名单都不因其他套件的条目而变宽** [V]。
2. 边界测试（`scripts/video-request-errors.test.ts`，16 例）同时覆盖允许侧、相邻路径/方法/kind 的禁止侧、跨流程不渗透（image preset 拒绝 video-only 条目、反之亦然），并把 before/after 迁移映射写成可执行断言 [V]。
3. `seed.reset()` 的 163 个调用点（59 个 spec 文件）全部迁移为 `seed.owned()`：按 spec 文件 + 测试标题确定性派生命名空间（重试稳定、跨测试/套件/分片不碰撞），测试结束后 seed fixture teardown 精确清理 [V]。
4. 后端 `_test_seed` 路由获得 owned 变体（`/seed/owned`、`/seed/owned-cleanup`、`/seed/lidar`、`/seed/project-roles`、`/seed/filtering` 的命名空间参数化），全部复用同一套 FK 顺序删除引擎与同一 fixture 构建器，不设第二套实现；`seed/reset` 保留为已记录的串行例外（收敛所有 E2E 命名空间的破坏性重建）[V]。
5. 实际运行验证（owned `aap_wt_*` 资源、隔离端口、真实浏览器）：default 图像工作台 5×2（重复运行）、filter-operational-lists 9、mask-readonly 2 passed+11 skipped、mask-native 20 passed+2 skipped、mask-ai-native 7 passed、无矩阵 default 下共享 Mask spec 9 passed+19 skipped [V]。
6. 运行时探针：受控 retry 探针（attempt 0 构建后受控失败，attempt 1 同命名空间重建通过，Playwright 判定 1 flaky）；邻居探针（A/B 命名空间同时构建，清理 A 后 A 登录 404 + A 存储 object NoSuchKey，B 登录/任务/存储字节级不变 + presign 仅指向 B 键，随后 B teardown 清理）[M]。

## 1. 范围与边界

- **拥有面**：`apps/web/e2e/**`、`apps/web/scripts/video-request-errors.test.ts`、`apps/api/app/api/v1/_test_seed.py`、`apps/api/app/api/v1/_test_seed_filters.py`、新建 `apps/api/tests/test_seed_owned.py`；另经协调方转达 P5 授权，在既有 owner 内扩展了 `apps/web/src/pages/Workbench/stage/shared/geometry/maskOperations.test.ts` 与 `maskRle.test.ts`（未触碰 P5 生产源码与其余测试）。
- **不触碰**：P2 既有后端测试/conftest/factory（只在运行时 import）；P3 的 `vitest.setup.ts`/`vite.config.ts`/`src/test`/data-manager 流程测试；P4/P5 领域。
- **workers 保持 1**。迁移完成后同一命名空间不再跨测试共享，但 launcher 的单 e2e 会话与端口仍为串行边界；未做 worker 并发扩容（计划 §6.4-4 留待隔离验收后的后续阶段）。
- 全部后端测试在 `pnpm dev:worktree --mode test`（`aap_wt_*_test`，head 0174）执行；全部 E2E 在 `--mode e2e`（`aap_wt_*_e2e`）执行。未触碰共享 `annotation_test`/`annotation_e2e`。

## 2. 请求失败分类（commit 17513bdbb）

### 2.1 规则组与流程组合（迁移前后映射）

| 旧允许项（来源流程）                                                                                                                                           | 新规则组                                 |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| POST `auth/me/heartbeat`、`auth/me/task-events:batch`（全部流程；唯一写形态例外，unload 后 API 已受理）                                                        | `sessionTelemetryAborts`                 |
| GET `auth/me`、`feedbacks`、`tasks`、`tasks/{id}`（image+video）                                                                                               | `coreBootstrapAborts`                    |
| GET `auth/registration-status`、`projects`、`audit-logs`（image dashboard 进入路径）                                                                           | `dashboardViewAborts`                    |
| GET `tasks/{id}/annotations`、`discussion/page`、`discussion/annotation-counts`、`projects/{id}/access`（image+video）                                         | `taskContextAborts`                      |
| GET `tasks/{id}/predictions`、`projects/{id}/mention-candidates`、`feedbacks/{id}/thread`（video Issue 流程）                                                  | `videoTaskContextAborts`                 |
| GET `annotations/{id}/comments/page`（bbox-center-out、mask-slice）                                                                                            | `objectCommentsAborts`                   |
| GET `annotations/{id}/mask-content`（mask-slice）                                                                                                              | `maskEditorAborts`                       |
| GET `tasks/{id}/video/manifest(-v2)`、`video/frames/N`、`videos/{id}/chunks/N(/samples)`、`video/segments`、`frame-timetable`、`videos/{id}/chapters`（video） | `videoMediaAborts`                       |
| 预设 `imageWorkbenchAborts` = 遥测+bootstrap+dashboard+任务上下文（polygon-\* 三件套）                                                                         | 同左                                     |
| bbox-center-out = `imageWorkbenchAborts` + `objectCommentsAborts`                                                                                              | 同左                                     |
| mask-slice = 上者 + `maskEditorAborts`                                                                                                                         | 同左                                     |
| 预设 `videoWorkbenchAborts` = 遥测+bootstrap+任务上下文+videoTaskContext+videoMedia（video-issue-frame/context）                                               | 同左                                     |
| video issue 套件的 fixture 级例外（mediaLatency 下 lock DELETE、fixture 媒体 GET、actorChanged 下 projects/audit-logs）                                        | 套件内 `fixtureAbortRules`，仍为显式规则 |

引擎 `isExpectedRequestAbort` 只归类 `kind === "request"` 且 `message === "net::ERR_ABORTED"` 的请求失败；HTTP 响应、console/page 错误、其余写请求一律不算取消。预期业务 HTTP 失败（如 mask-slice 的冲突响应、video-issue-context 的 `expectedHttpErrors`）由各套件自己的 `allowedErrors`/expected 注册表显式声明，与本引擎互不替代。

### 2.2 迁移时对 union-widening 的修正

初版把全部条目并成一个 union 让各流程共用，评审指出这会隐性加宽各调用方白名单；已改为按域分组 + 显式流程组合，测试固化「image preset 拒绝 video-only 条目」「video preset 拒绝 dashboard/object 条目」「comments/mask 条目仅经显式组合加入」。

## 3. Owned fixture（commit 95fbd98db + a5c58dadd）

### 3.1 后端（`_test_seed.py`、`_test_seed_filters.py`）

- 命名空间校验 `^[a-z0-9]{4,12}$`，长度上限保证 `DS-E2E-{ns}`≤20、`B-E2E-{ns}`≤30、`T-E2E-{ns}{i:06d}`≤30 等列限。
- `/seed/owned`：与 `seed/reset` 共用 `_build_image_workbench_fixture` 构建器（仅标识不同），返回同一 `SeedData` 形状；构建前仅清理本命名空间（重试收敛），不触碰邻居。
- `/seed/owned-cleanup`：与共享路径共用同一 FK 顺序删除引擎；owned 谓词全部为精确匹配（email ANY、project name/display_id ANY、dataset display_id ANY、registry url 精确、模板 display_id ANY、审计行 namespace 标签），对象存储按 `e2e/owned/{ns}/`、`e2e/lidar/{ns}/`、`e2e/owned/{ns}/project-roles/`、`e2e/owned/{ns}/filtering/` 前缀删除并校验清空；残留未清零即 500（`e2e_seed_cleanup_incomplete` 附 residuals）。
- `/seed/lidar`：namespace 必填，复用本命名空间 admin/annotator，lidar 项目/数据集/对象前缀命名空间化。
- `/seed/project-roles`、`/seed/filtering`：namespace 可选；缺省保留旧共享行为（既有后端测试与未迁移调用方的兼容面，已记录），传入即全量命名空间化。filtering 的固定值经 `_FilterIds` 映射（display id、邀请邮箱与唯一 token、项目/数据集/模板/用户显示名、作业 prompt、审计 scope），operations manifest 新增 `display_names`/`invitation_emails`/`search_keys`/`audit_scopes` 供 spec 以 manifest 值断言。
- 共享 `seed/reset`/`seed/cleanup` 不改名不隐藏：仍是「收敛所有 E2E 数据」的破坏性串行例外（含会清扫 owned 命名空间的宽谓词——这是共享路径的既有语义），过滤型 spec 已全部迁移后其实际消费者仅剩兼容路径。

### 3.2 前端（`e2e/fixtures/seed.ts` + 60 个 spec 文件）

- `SeedAPI.owned(namespace?)`：无参时由 `sha256(specFile::titlePath)` 派生命名空间（重试重建同一命名空间、跨测试唯一）；实例按测试新建并登记命名空间，fixture teardown 在测试体（含证据附件）结束后逆序 `cleanupOwnedFixtures()`；清理失败使测试失败（隔离欠账不是警告）。
- `seed.filtering()` 始终走 owned 命名空间（自动派生）。
- 全部 163 处 `seed.reset()` 调用点改为 `seed.owned()`；spec 内不再出现 `seed.reset(`。

### 3.3 后端回归（`tests/test_seed_owned.py`，新建，15 例）

命名空间边界（合法/非法/边界 token）、完整命名空间 fixture 构建（用户/项目/成员/batch/dataset/tasks/registry/pool/存储对象/登录）、精确幂等清理 + 邻居与普通数据完好 + 重建、重试安全（崩溃残留后重建）、共享 fixture 不被 owned 清理触碰、FK 阻塞部分失败的残留显式暴露、OpenAPI 隐藏、owned filtering/lidar/project-roles 命名空间化与收敛。均以真实测试库 + 真实存储服务运行（exit 0）。

## 4. 实际运行证据（`--mode e2e`，owned `aap_wt_ac713f1d22f1474d_e2e`，真实浏览器 chromium）

| 运行                                                                                | 配置                                     | 结果                                                                                                                                                    |
| ----------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| polygon-auto-points.spec.ts（owned fixture 图像工作台真实绘制链路）                 | default                                  | 5 passed（3.7m）；紧接重复运行 5 passed（3.7m）→ 重复运行证据                                                                                           |
| filter-operational-lists.spec.ts（filtering fixture 命名空间化后全局搜索/CSV 断言） | default                                  | 9 passed（1.3m）                                                                                                                                        |
| raster-mask-native.spec.ts                                                          | `PLAYWRIGHT_RASTER_MASK_MATRIX=readonly` | 2 passed + 11 skipped（21.9s）：native write 矩阵被运行时跳过                                                                                           |
| raster-mask-native + mask-advanced-operations                                       | `PLAYWRIGHT_RASTER_MASK_MATRIX=native`   | 20 passed + 2 skipped（3.6m）：readonly 矩阵被运行时跳过；1080p Worker 用例在本地以源码 Worker 执行                                                     |
| native-mask-ai + video-tracker-local-review                                         | `PLAYWRIGHT_RASTER_MASK_MATRIX=native`   | 7 passed（1.5m）：native-mask-ai 使用 page.route 拦截的 mock SAM backend；video-tracker-local-review 使用替代本地 tracker 服务——不构成真实模型/GPU 验证 |
| mask-advanced-operations #2/#3（下沉后删减版）                                      | `PLAYWRIGHT_RASTER_MASK_MATRIX=native`   | 2 passed（43.4s）：真实画布链路 + 提交/离开守卫/持久断言保留                                                                                            |

说明：本机浏览器为 headless Chromium + SwiftShader 软渲染（pointcloud project 配置）；上述结果不主张 GPU/硬件解码资格。mask-advanced-operations 第 4 例（1080p Worker）在本地 default 套件不跳过（CI 且无矩阵时才跳过）——这是 default 与 Mask 矩阵的又一处实际行为差异。

### 4.1 default 与 Mask 配置的收集/执行对比（`--list` + 实际执行）

| 配置                                                     | 收集                 | 实际执行                                                                                                                                            | 运行时跳过              |
| -------------------------------------------------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| default（全量）                                          | 271（chromium 计数） | 含 raster-mask-native 的 13 条、mask-advanced-operations 9 条、native-mask-ai 全部运行时跳过（MATRIX≠native/readonly）；mask-advanced #4 本地不跳过 | —                       |
| readonly（raster-mask-native.spec.ts）                   | 13                   | 9. read-on/create-off、11. closed gate                                                                                                              | 11（native write 矩阵） |
| native（raster-mask-native + mask-advanced-operations）  | 22                   | 12 native write + 9 advanced                                                                                                                        | 2（readonly 矩阵）      |
| ai-native（native-mask-ai + video-tracker-local-review） | 7                    | 7                                                                                                                                                   | 0                       |

即：收集层面各配置都包含对方 describe；差异发生在运行时 skip 与实际执行，且双向均有通过证据（readonly 与 native/ai-native 各自实际跑过）。

#### 4.1.1 default 下共享 Mask spec 的实际执行（非推断）

`pnpm exec playwright test e2e/tests/raster-mask-native.spec.ts e2e/tests/mask-advanced-operations.spec.ts e2e/tests/native-mask-ai.spec.ts --project=chromium`（无矩阵变量）→ **9 passed + 19 skipped（1.8m）**：

- `raster-mask-native.spec.ts`：13 收集，13 运行时跳过（native write 与 readonly 两个 describe 的 `test.skip(MATRIX !== ...)` 均成立）；
- `native-mask-ai.spec.ts`：全部运行时跳过（`test.skip(MATRIX !== "native")`）；
- `mask-advanced-operations.spec.ts`：9 例全部实际执行并通过；#4（1080p Worker）在本地 default 执行（其 skip 条件为 `CI && 无矩阵`），CI default 会跳过——这是 default 与 Mask 矩阵之间的真实行为差异。
- default 全量收集 271 条（chromium）仅来自 `--list`，未整跑。

### 4.2 运行时探针（临时 spec，取证后已删除）

- **Retry（真实 Playwright retry）**：`zz-owned-retry-probe.spec.ts` + `--retries=1`。attempt 0 构建 owned fixture 后受控抛错；attempt 1 在同一命名空间上重建（清理残留后重新创建）并通过；Playwright 判定 **1 flaky**（失败后重试通过的标准归类）。证明 retry 与崩溃残留收敛语义。
- **Neighbor（A/B 同时存在，字节级存储验证）**：临时 spec `zz-owned-neighbor-probe.spec.ts`（取证后删除，无残留消费者）。同时构建 zznba/zznbb；通过 API venv 的真实存储客户端读取两侧对象（临时 sidecar 脚本，见下）；清理 A 后断言：A 登录 404、A 对象 `NoSuchKey`、B 登录 200、B 任务可读、B 对象字节级不变（sha256 与 size 相等）、presign 仅指向 `/e2e/owned/zznbb/`；随后 B teardown 清理。1 passed（18.4s）。
  复现方式（临时脚本取证后同样删除；`apps/api/.venv/bin/python` + `PYTHONPATH=apps/api`，`storage_service.client.get_object(Bucket=storage_service.datasets_bucket, Key=key)` → sha256/size）：`pnpm dev:worktree --mode e2e` 下 `PLAYWRIGHT_RASTER_MASK_MATRIX` 未设，`pnpm exec playwright test e2e/tests/zz-owned-neighbor-probe.spec.ts --project=chromium`。

### 4.3 运行环境与代理备注

presign URL 指向 `/minio/...`，由 Vite dev server 代理到对象存储；测试进程直连 3001 的读取曾返回 404（代理/签名链路细节），故存储存活断言改用 owned runtime 的真实存储客户端按字节验证， presign 仅作键归属断言。此代理行为已单独记录，不影响产品功能（工作台图像经同一链路渲染成功，见 polygon 运行 `data-image-ready`）。

## 5. 下沉评估与替代映射（P5 授权后落地）

授权：协调方转达 P5 授权 P7 专有编辑既有 `stage/shared/geometry/maskOperations.test.ts` 与 `maskRle.test.ts`，优先扩展现有 owner，不建平行文件；生产文件不动。

### 5.1 低层新增（单位：既有 describe 内新 it）

| 新单测 ID（文件 › describe › it）                                                                                                                                   | 固化的组合                                                                                                                                                                                                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `maskOperations.test.ts › connectivity and membership › keeps the hit diagonal component in 4-connectivity while the 8-connected merge makes the same keep a no-op` | diagonal_two 拓扑：4 邻域 keep 命中 → changed 4 / 8→4 / 2→1；8 邻域同一命中 → changed 0 / 8→8 / 1→1（即 E2E 原来的「变化 0 px / 面积 32→32 / 组件 1→1」排列）                                                                                    |
| `maskOperations.test.ts › component and hole editing › drives the donut-and-islands matrix: hole count, threshold pruning and keep-to-one`                          | donut_three 拓扑：3 组件（24/4/3）+ 1 个 4px 封闭孔；fill-holes → 31→35、changed 4、孔洞清零；threshold `maxArea=3` → 3→2、afterArea 28（E2E 原「面积 612→456 / 组件 3→2」）；keep 命中 → 3→1、afterArea 24（E2E 原「面积 612→260 / 组件 3→1」） |
| `maskRle.test.ts › COCO uncompressed RLE › counts area as the odd-index run sum, the contract E2E foregroundArea helpers assume`                                    | `cocoRleArea` == 奇偶 run 奇数和 == decode 后前景计数（E2E `foregroundArea`/`maskContent` 面积断言的单元契约）                                                                                                                                   |

既有覆盖（未新增、已承担部分映射）：connectivity 的 4/8 对角分离与 flood fill（`flood fill changes only the selected 4- or 8-connected region`）、组件 keep/delete 命中成员（`keeps or deletes the foreground component hit by alpha membership`）、AABB 非成员（`does not use a component AABB as hit membership`）、阈值去小组件（`removes every foreground component at or below the threshold`）、填孔 hit/max_area/all（`fills only enclosed background...`）、RLE 往返与像素查询/边界。

### 5.2 E2E 实际删减（同一提交，先有替代后删）

| spec / 用例                           | 删减前的高层排列                                                                                                                            | 删减后保留                                                                                         | 替代映射                                                                                                                                                      |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `mask-advanced-operations.spec.ts` #2 | 4 邻域 keep（32→16，2→1）→ **8 邻域 keep no-op（0 px，32→32，1→1）** → **4 邻域擦除命中（32→16）** → 提交 → 持久面积 16                     | 4 邻域 keep → applyPreview → 提交 → PATCH → 刷新读回面积 16（真实用户链路）                        | 8 邻域 no-op 与擦除排列 → 新增 `keeps the hit diagonal component...`；擦除命中连通区域 → 既有 `flood fill changes only the selected 4- or 8-connected region` |
| `mask-advanced-operations.spec.ts` #3 | 填孔（612→676，1→0）→ **阈值 180 去小组件（612→456，3→2）** → **keep（612→260，3→1）** → applyPreview → 未保存 → 离开守卫 → 丢弃 → 持久不变 | 填孔（612→676，孔洞 1→0）→ applyPreview → 未保存 → 离开守卫 → 丢弃并离开 → 胶囊消失 → 持久内容不变 | 阈值去小组件与 keep-to-one → 新增 `drives the donut-and-islands matrix...`；填孔数值仍由 E2E 真实 UI 验证                                                     |

结果：两例 E2E 从各 3 段组合降为 1 段真实链路 + 提交/守卫/持久断言；被删排列全部有单元 ID 承接（§5.1），单测 40 passed，删减后的 #2/#3 在 native 矩阵实测 2 passed（43.4s）。

### 5.3 保留的高层保护

真实画布路径（进入工作台 → 画/填充/切片 → 预览 → 提交/丢弃 → 刷新校验）、连通性接线（工具栏 4/8 邻域选择、预览报表文案）、worker 取消（#4）、split/join/锁定重叠（#5–#7、#14）继续留在 E2E，与低层纯函数断言互补，不视为冗余。

## 6. 未执行、保留与归属

- [GAP] worker>1 并发收益验证：按计划 §6.4-4 属隔离验收后的独立步骤；本阶段维持 workers=1（launcher 单会话串行边界）。
- [保留] `seed/reset`/`seed/cleanup` 共享路径：兼容既有后端测试与截图 profile 流程的已记录串行例外；宽谓词收敛语义（含清扫 owned 命名空间）为其定义的一部分。
- [保留] `mask-advanced-operations` #4 的 CI/default skip 条件：Vite dev server 提供源码 Worker 模块的前提未变。
- 已 rebase 到根工作树当前 HEAD `3a94e7560`（含 P2/P3/P4）并重跑：seed 家族（`test_seed_owned.py`、`test_seed_router.py`、`test_filter_seed.py`、`test_screenshot_seed_catalog.py`）82 例 exit 0；几何/RLE/分类器单测 56 passed；确认 rebase 后无 `seed.reset(` 残留与旧 helper 引用。
- 远程 CI 未运行（不推送）；所有证据来自本工作树 owned 资源的实际执行。

## 7. 回退边界

- 各提交按职责独立可 revert（分类器、后端 owned 路由、E2E 迁移、filtering 命名空间修正、下沉与文档）；owned 路由与 `test_seed_owned.py` 为纯新增，revert 不影响既有行为。
- e2e 迁移提交 revert 后 spec 回到 `seed.reset()` 共享路径，行为与基线一致。
- 若 P5 ack 否决下沉路径，删除提议条目即可，不影响已落地部分。
