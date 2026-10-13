# 仓库优化 前端最终验收：coverage / build / size / typecheck / lint / format / docs

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P10 前端验收前置；**不是** P10 收口）
> 工作树：`/home/hehao/桌面/ai-annotation-platform-worktree-agent-opt-p6-ml`（分支 `worktree-agent-opt-final-frontend`）
> 被测提交：`c62aa8364`（基于已接受的 P6 收口 root `725fc5101`）；settle 前 rebase 到 `f90dea73e`（仅新增 `docs/research/40` 后端证据，产品输入不变）
> 证据图例：**[V]** 本工作树实测

## 0. 结论

1. **覆盖率套件 [V]**：`pnpm --filter @anno/web test:coverage` 通过，`562` 个测试文件 / `5626` 个用例全部通过；`All files` 为 statements 72.28% / branches 79.33% / functions 66.92% / lines 72.28%，既定阈值 `45/45/45/70` 未放宽。
2. **生产构建与预算 [V]**：`OPENAPI_URL=…/apps/api/openapi.snapshot.json pnpm --filter @anno/web build` 通过（24.07s，含快照 codegen），随后 `pnpm --filter @anno/web size` 全部 `[OK]`。
3. **静态质量 [V]**：web `typecheck`、web `lint`（eslint + `check-tw-tokens`）、根 `format:check`、根 `pnpm lint` 全部 exit 0（0 error；既有 5 warning）。
4. **文档构建 [V]**：§3 的必需修复后 `pnpm docs:build` 通过（34.24s）。
5. **边界**：本 lane 未运行 E2E / 浏览器 / 数据库；P9 负责候选对比。未发现产品缺陷，无需修复提交（§3 的 docs 链接除外）。

## 1. 被测身份（供 P8/P9 复用判断）

| 项                                      | 值                                                                 |
| --------------------------------------- | ------------------------------------------------------------------ |
| 被测提交                                | `c62aa83642de779151254ba1fdc410643cd335a1`                         |
| 基线 root                               | `725fc5101d5117d262e52c20a87e87e2e3ce5ce3`（P6 收口接受）          |
| settle 前 rebase                        | `f90dea73e`（仅 `docs/research/40` 后端证据；产品输入不变）        |
| node / pnpm                             | `v22.23.1` / `10.28.1`                                             |
| `pnpm-lock.yaml` sha256                 | `cdc681d92820e792607244edc9ec98018cc0d4f21041202fd1a6b326095363ec` |
| `apps/api/openapi.snapshot.json` sha256 | `327935b8991b0ef16920e53245b363a73464f8e6ab31c02035b650e665f1ec36` |
| `apps/web/src` tree                     | `91ece578ba408d19e4c9875be61bcbd32863f2c0`                         |
| `apps/web/package.json`                 | `709451321c9052946f2e1006a936c80c4072a708`                         |
| `apps/web/vite.config.ts`               | `5bb04bae884c80ba84e8362e71cee5ddbfc85bc8`                         |
| `apps/web/vitest.setup.ts`              | `759306923d6ba9225f67ac586aa06c5c14a658d0`                         |
| `apps/web/playwright.config.ts`         | `145f718ed24bd1cef8a8a41bc2d0fdf853c6b02d`                         |
| `docs-site/dev/concepts` tree           | `7df8551dc70348ad0a52a5496f519d05422ce110`                         |

依赖侧：`node_modules`、`apps/web/node_modules`、`.env` 为本检出指向 primary checkout 的符号链接（未安装、未改链接）；`apps/api/.venv` 与 `apps/web/src/api/generated` 属本检出本地生成物。

## 2. 实测命令与结果

| 检查                     | 命令（本工作树，cwd=repo root）                                                     | 结果                                                                                                         |
| ------------------------ | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 覆盖率（含全套件）       | `pnpm --filter @anno/web test:coverage`                                             | exit 0；`Test Files 562 passed`、`Tests 5626 passed`；`All files 72.28 / 79.33 / 66.92 / 72.28`；wall 61.77s |
| 生产构建                 | `OPENAPI_URL="$(pwd)/apps/api/openapi.snapshot.json" pnpm --filter @anno/web build` | exit 0；`tsc -b && vite build`，24.07s                                                                       |
| 体积预算                 | `pnpm --filter @anno/web size`                                                      | exit 0；main/vendor-konva/vendor-markdown/vendor-dockview 全部 `[OK]`，`all bundles within budget`           |
| web 类型检查             | `pnpm --filter @anno/web typecheck`                                                 | exit 0                                                                                                       |
| web lint（含 CSS token） | `pnpm --filter @anno/web lint`                                                      | exit 0；0 error / 5 warning；`check-tw-tokens` 通过                                                          |
| 根格式检查               | `pnpm format:check`                                                                 | exit 0；`1277 files already formatted`                                                                       |
| 根 lint                  | `pnpm lint`                                                                         | exit 0；0 error / 5 warning（既有 warning，未新增）                                                          |
| 文档构建                 | `pnpm docs:build`                                                                   | exit 0；34.24s（见 §3 修复）                                                                                 |

## 3. 必需 docs 修复（独立提交）

- 问题：`docs/changelogs/0.10.x.md:6` 的 `[服务导入切换迁移说明](../migration/2026-07-17-v0.23.2-service-import-cutover.md)` 在 `docs/` 树内有效，但 `docs-site/scripts/mirror-changelog.mjs` 会把该文件镜像进 gitignored 的 `docs-site/changelog/`，相对链接解析到不存在的 `docs-site/migration/…`，使 VitePress 报 `1 dead link(s) found`、`pnpm docs:build` 失败。
- 修复：改为行内代码并保留完整历史路径——`docs/migration/2026-07-17-v0.23.2-service-import-cutover.md`，事实与路径不丢，镜像页可构建。
- 提交：`6fb6b13e6`（`docs(changelog): make the v0.23.2 migration pointer mirror-safe`）；修复后 `pnpm docs:build` exit 0。

## 4. 复用与复跑规则（对 P8/P9）

- 若后续只有 CI / workflow / planner 编辑（`.github/workflows/**`、`scripts/plan-e2e-suites*` 等），不改变上表 `apps/web` 产品输入，则**不要求**复跑本 lane 的覆盖率 / 构建 / size / typecheck / lint。
- 若改动 `apps/web/src`、`apps/web/package.json`、`vite.config.ts`、`vitest.setup.ts`、`playwright.config.ts`、`pnpm-lock.yaml` 或 `apps/api/openapi.snapshot.json`，需按新身份重跑相应检查。
- P9 的 E2E 候选对比属另一 lane（独立 owned runtime/端口），本 lane 未运行 E2E/浏览器/DB。

## 5. 限制

- 本地单工作树、无远程 CI 结果；覆盖率为本地单次执行（无历史 flaky 统计）。
- 未单独重复全量单测（覆盖率命令已执行整套件）；未运行 E2E/浏览器/数据库。
- 生成物：`apps/web/dist`、`apps/web/coverage` 为本次构建产生（构建前不存在），证据留存后已清理；`docs-site/.vitepress/dist` 为既有忽略产物，保留未删。
- 本文件是前端验收证据，不代表 P10 收口；P8/P9/P10 状态见台账。
