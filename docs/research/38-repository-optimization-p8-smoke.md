# 仓库优化 P8：核心 smoke 契约（成员收敛与实测证据）

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（§6.2 核心 smoke）
> 依据：`/tmp/aap-opt-p8-membership-audit.md`（M1：现 smoke 成员 `workbench-image-konva-smoke.spec.ts` 带 `@visual`，
> 在默认 `test:e2e` 下收集 0；M3：§6.2 行为未逐条映射到实测用例）
> 工作树：`/home/hehao/桌面/ai-annotation-platform-worktree-agent-opt-p6-ml`（分支 `worktree-agent-opt-p8-smoke`，根 `bc220bfd1`）
> 证据图例：**[V]** 本工作树实测；**[M]** 只读探针

## 0. 结论

1. **不新增 spec [V]**：§6.2 核心 smoke 的六个行为已由现有 4 个 spec 的 9 条用例覆盖；新增
   `core-workflow-smoke.spec.ts` 只会重复既有真实 UI 链路（任务明确要求"既有覆盖齐全时报告精确成员而非新增重复用例"）。
2. **收敛成员 [V]**：把 planner 的 `smoke.command` 从"5 个 spec 文件、实际收集 8 条"改为下列 9 条显式成员
   （4 个既有文件 + 一条 `--grep`），并移除收集为 0 的 `workbench-image-konva-smoke.spec.ts`。
3. **实测首次通过 [V]**：在工作树自有的 disposable e2e 模式（独立库/桶/Redis/端口，workers=1，本地 `retries=0`）上
   一次跑通 `9 passed (57.5s)`，无重试、无 `force`、无 store 注入、无放宽的超时。

## 1. 精确 smoke 成员与命令

```bash
pnpm test:e2e \
  e2e/tests/auth.spec.ts \
  e2e/tests/annotation.spec.ts \
  e2e/tests/employee-project-roles.spec.ts \
  e2e/tests/mask-session-guard.spec.ts \
  --grep '健康检查|正确凭证|错密码|未登录访问|annotator 登录|bbox 真实绘制、选类、落库并刷新恢复|same employee annotates A|opposite project actions are denied|切工具离开 dirty session'
```

`--list` 实测 **[V]**：`Total: 9 tests in 4 files`。逐条成员：

| #   | 行为                              | 文件                             | 用例（grep 命中）                                                                                  |
| --- | --------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------- |
| 1   | 健康检查（服务可用）              | `auth.spec.ts`                   | auth › 健康检查接口可用                                                                            |
| 2   | 登录                              | `auth.spec.ts`                   | auth › 正确凭证 → 跳角色默认首页                                                                   |
| 3   | 登录失败                          | `auth.spec.ts`                   | auth › 错密码 → 仍在登录页 + 错误提示                                                              |
| 4   | 未登录拒入                        | `auth.spec.ts`                   | auth › 未登录访问 /dashboard → 跳 /login                                                           |
| 5   | 登录 + 项目进入（路由）           | `annotation.spec.ts`             | annotation workbench › annotator 登录 → /annotate 路由可达（smoke）                                |
| 6   | 真实画布标注保存 + 刷新恢复       | `annotation.spec.ts`             | annotation workbench › bbox 真实绘制、选类、落库并刷新恢复                                         |
| 7   | **真实 UI 提交 → 审核**           | `employee-project-roles.spec.ts` | project-scoped employee roles › same employee annotates A and reviews B in two tabs of one account |
| 8   | **有意义的权限拒绝**              | `employee-project-roles.spec.ts` | project-scoped employee roles › opposite project actions are denied and project C stays invisible  |
| 9   | 工作台切换（dirty session guard） | `mask-session-guard.spec.ts`     | mask session guard › 涂抹后切工具离开 dirty session 弹未保存提示                                   |

> 该 grep 只命中上述 9 条（无越界匹配）；`auth`/`annotation` 其余用例（prompt-first 能力协商、422 拒绝）
> 与 `employee-project-roles` 其余 4 条（成员改职、撤权草稿、员工首页、viewer 入口）不进入核心 smoke，
> 仍由既有的功能分片/专项执行。

## 2. §6.2 行为映射（claim → 实测用例）

| §6.2 核心 smoke 行为 | 覆盖成员 | 真实链路证据                                                                                                                                                                                         |
| -------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 登录 / 项目进入      | #2–#6    | UI 登录跳转、错密码、未登录跳转；`annotation.spec.ts` 进入 `/projects/:id/annotate?task=` 并等 `data-image-ready`                                                                                    |
| 代表性标注保存与刷新 | #6       | 真实指针画框 → 选类别 → 断言 `POST /annotations` 201 + body → `data-user-box-count=1` → 读回 API → `reload` 后仍为 1 **[V]**                                                                         |
| 任务提交审核         | #7       | 真实 UI 点击 `workbench-submit`，等 `POST /tasks/:id/submit` 200；随后另一标签页真实 UI `review-approve` → `POST .../review/approve` 200；服务端任务态 `review`→`completed`                          |
| 核心权限拒绝         | #8       | 真实 403：annotator 对 A 的 `review/claim`、reviewer 对 B 的 `submit`、对 C 的 `POST /annotations`；UI 深链 `/projects/{c, a-review, b-annotate}` 均 fail-closed 跳 `/dashboard`（不挂载可写编辑器） |
| 工作台切换           | #9       | dirty mask session 下切工具弹"有未保存的 Mask 稿件"确认，选"继续编辑"恢复旧工具与 Buffer                                                                                                             |

> 说明：#7/#8 未使用 `review-approve-loop.spec.ts`，因为该 spec 的提交走 `request.post(.../submit)`（API 播种），
> 不是真实 UI 提交；`employee-project-roles.spec.ts` 才是真实 `workbench-submit` 点击链路。
> #8 不是"特性可见性"断言（`workbench-secondary-permissions.spec.ts` 那种），而是真实 403 写入 + UI 深链拒绝。

## 3. 实测证据

| 检查           | 命令                                                                               | 结果                                                                                                                                                                                                                    |
| -------------- | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 收集（过滤后） | `playwright test --list --project=chromium <4 files> --grep '<regex>'`             | `Total: 9 tests in 4 files` **[V]**                                                                                                                                                                                     |
| 首次执行       | `pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e <4 files> --grep '<regex>'` | `9 passed (57.5s)`，exit 0，无 retry **[V]**                                                                                                                                                                            |
| 运行所有权     | `pnpm dev:worktree -- doctor --mode e2e`                                           | 我的工作树模式：DB `aap_wt_c2820af87ec94679_e2e`（head `0174`）、Redis `aap-wt-c2820af87ec94679-e2e-redis`（port `32832`）、8 个桶 `aap-wt-c2820af87ec94679-e2e-*`、owner `aap-worktree:c2820af87ec94679:e2e:*` **[V]** |
| 隔离边界       | `workers: 1`（配置强制）；本地 `retries: 0`；独立库名以 `_e2e` 结尾                | 与开发库/其它工作树 e2e 环境互不复用 **[V]**                                                                                                                                                                            |

未复用 root 点名的既有服务：port 3001 是既有 Grafana 容器、8101 是遗留 P7 API，均未连接。

## 4. 与现 planner smoke 的差异（供 P8 owner 接线）

现 `scripts/plan-e2e-suites.mjs:118` 的 `smoke.command` 为 5 个 spec 文件，实测收集 **8 条 / 4 文件**：
其中 `e2e/tests/workbench-image-konva-smoke.spec.ts` 的唯一条用例带 `{ tag: "@visual" }`，
而默认 `apps/web/playwright.config.ts:76` 有 `grepInvert: /@visual|@stress/`，因此该文件在 `test:e2e` 下收集 0，
"工作台进入 + 保存/刷新"这一步实际未执行。本契约：

- **移除** `workbench-image-konva-smoke.spec.ts`（`@visual`，应在 `test:e2e:visual` 执行）；
- **新增** `employee-project-roles.spec.ts` 的 #7/#8（真实 UI 提交→审核 + 真实权限拒绝）；
- **保留** `auth` + `annotation` + `mask-session-guard` 的相关子集（用一条显式 `--grep` 限定，避免整文件改名式扩张）。

本文件只登记契约与证据，**不修改** planner/workflow（保留给 P8 owner；P9 才切门禁）。

## 5. 为什么不新增 spec

- §6.2 六个行为全部命中既有真实 UI 用例（第 2 节），重复实现会得到"整份默认套件改名"的反效果；
- 唯一曾疑似缺失的"真实 UI 提交"与"权限拒绝"其实已在 `employee-project-roles.spec.ts` 覆盖，
  审计 M3 当时只看了 `review-approve-loop` / `workbench-secondary-permissions` 两份，未覆盖该文件；
- 因此新增文件会把 9 条既有可靠用例再写一遍，违反"只补真正缺口、不重复整个默认套件"。

## 6. 边界与限制

- 仅本工作树、Chromium、headless、单 worker 的本地实测；未在 CI、未跑完整分片、未验证跨 runner 稳定性。
- smoke 是"代表性用例集合"，不是一条连续脚本；成员之间各自 `seed.*` 准备前置，属于有界核心而非全链路单测。
- 运行开销：本机 9 条 57.5s（含 2 条 `employee-project-roles` 重型用例，各自内部超时 90–120s）；CI 上会叠加构建与启动。
- 请求错误策略沿用既有 `helpers/request-errors.ts`（#7/#8 未新增白名单）；未放宽 retry/超时，未使用 `force`/store 注入。
- 本文件不改变 `Frontend E2E` 门禁；P9 才评估切换。
