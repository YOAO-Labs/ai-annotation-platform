# 仓库优化 P10 最终验收记录（台账关闭）

> 完成日期：2026-09-21 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（§3 约束 / §9 记录要求 / §10 完成标准）
> 不可变基线：`9cec9751a9f7a5518cfa09af6d1d75a789f28758`（计划 §2 审阅基线，= PR #129 合并点）
> 集成根（最终验收根）：`13d274326ee4f8bb8b80b405c7dc2fd3d15d05fc`；最终报告提交：`8c4276297`（本 P10 分支在其上追加文档收尾）
> 阶段状态：P0–P9 **完成**；P10 **完成**（本记录关闭计划 §10 与改造台账）
> 证据类型图例：**EXEC** 本地实际执行 · **STATIC** 配置/代码/清单静态读取 · **REUSE** 既有已接受证据按变更影响范围复用 · **REMOTE-CI** 远程 CI 实际运行 · **LIMIT** 明确未验证边界
> **复用口径（重要）**：证据复用按**变更影响范围**判定，**不是**全应用逐字节等价——共享 Workbench 源码在修正构建中确有变化（`13115f71f`）；只有相关输入/配置/依赖未变的套件才复用其冻结 `898` 状态（见 [43](./43-repository-optimization-p9-shadow-comparison.md)）。

## 0. 结论

1. **计划 §10 共 22 项，全部按实际证据勾选**（逐条见 §2）。代码结构 6 / 测试 5 / CI E2E 7 / 注释与文档 4；不存在未映射项。
2. **P9 最终门禁**：冻结 `898505469` 的 full-12 campaign 是**失败历史**（`default-four` 13 expected / 1 unexpected / 53 not run，实跑审计 exit 1）；集成根 `13d274326` 的 **composed** 必需套件审计实际 **exit 0**（`layout-stress` 6/0/0/0 与 `default-four` 68/0/0/0 在修正构建 `dabedc89`/`449aac28` 上重跑替换，其余 10 套件按变更影响范围复用冻结 `898` 状态）。不主张“新 SHA 全量 12 raw 全跑”。
3. **唯一跨项限制**：**远端 CI 未运行**（无 push），因此构建复用、审计、ml-cpu 的首次真实 runner 观察未获得；`summarize-e2e-results.mjs` 的分类与同构建复用仅在本地探针/单测上验证。这如实记录为 LIMIT，不是新授权要求或阻塞项。
4. **渲染资格**由渲染器通道 [42] 单独承担：已接受的是严格 WebGPU 21 例与严格 WebCodecs 9 例（浏览器自报 adapter nvidia/ampere、Chromium 147）；**硬件视频解码未测量**，因此不主张硬件解码/硬件资格。它不是本清单的缺口。
5. **before/after 只列可复核数字**；未测量项（运行/构建耗时、覆盖率百分比 delta、包体 delta、候选 flaky 率、候选远程 CI 通过率、P9 影子选择差异）明确列为 unknown，不编造收益。

## 1. before/after（不可变 `9cec9751a` → 集成根 `13d274326` / 最终报告 `8c4276297`）

> 只列可复核数字。基线侧“未测”表示计划基线阶段没有执行该检查，不填推测值。

| 维度                                   | 基线 `9cec9751a`                            | 当前（集成根）                                                            | 证据              |
| -------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------------- | ----------------- |
| 受管源文件分类                         | 5873 个文件全部归类                         | 分类口径沿用；热点模块已收敛                                              | [26] §2（STATIC） |
| 测试文件清单                           | 1042（P0 机器可读清单）                     | **1139 行 = 1135 可执行 + 4 测试支撑**（保守 KEEP 语义），双向比对 0 缺口 | [26] §3/§12/§13   |
| 前端单测                               | 未在基线执行                                | **562 文件 / 5626 用例**                                                  | [41] EXEC         |
| 后端测试                               | 未在基线执行                                | **4428 collected / 0 failed / 0 errors / 15 skipped**                     | [40] EXEC         |
| `useWorkbenchShellModel.tsx`           | 8919 行                                     | **6646 行**                                                               | [33] STATIC       |
| `…helpers.ts`                          | 480 行                                      | **306 行**                                                                | [33] STATIC       |
| `apps/api/app/workers/signals.py`      | 225 行                                      | **121 行**                                                                | [32] STATIC       |
| `ProjectDataManagerPage.flow.test.tsx` | 946 行                                      | **401 行**                                                                | [30] STATIC       |
| 覆盖率阈值                             | 45/45/45/70                                 | **45/45/45/70（未变）**                                                   | [30] STATIC       |
| 前端覆盖率实测                         | 未测                                        | 72.28 stmts / 79.33 branch / 66.92 funcs / 72.28 lines                    | [41] EXEC         |
| 后端覆盖率实测                         | 未测                                        | line 73.61% / branch 55.33%                                               | [40] EXEC         |
| §7.1 指定文件版本叙事命中              | 36                                          | **0**                                                                     | [34]/[35] EXEC    |
| CI E2E PR 默认选择                     | 7（docs-only/未知）或 9（含 visual+stress） | planned 选择 + `legacy=` 对照 + `E2E_SELECTION_MODE=legacy` 回退          | [43] STATIC       |
| P9 门禁（frozen 898 → 集成根）         | 未运行                                      | frozen 898 **审计 exit 1**；集成根 **composed 审计 exit 0**               | [43] §6.2 EXEC    |
| 迁移验证                               | `alembic stamp` 式 round-trip               | 真实四阶段 runner + fail-closed 策略（33 例）                             | [39] EXEC         |
| 渲染资格                               | 未测                                        | 严格 WebGPU 21 / WebCodecs 9（adapter 证据）                              | [42] EXEC         |

**明确未测量（不得编造）**：运行时长/性能 before-after、覆盖率百分比 delta、包体 delta、构建时长 delta、候选 flaky 率、候选远程 CI 通过率、P9 影子选择差异。

## 2. 计划 §10 逐条证据矩阵

状态：`满足`（有 EXEC/STATIC 证据）；`满足（LIMIT）`（实现与本地证据已具备，但远端 CI 未运行）。

### 2.1 代码结构

| §10 标准                                             | 权威来源（文件/符号）                                                                                                                   | 最新实际证据                                                                                                                             | 类型   | 状态 |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- | ------ | ---- |
| 全仓台账覆盖所有受管目录，非热点也有结论             | 源码范围：`docs/research/26` §2（5873 个受管源文件分类）；测试范围：`data/26-…inventory.tsv`                                            | 源码分类口径沿用；测试清单独例：TSV 1139 行（1135 可执行 + 4 支撑），双向比对 0 缺口；`reason` 为具名/区域级保守保留理由（两者口径分开） | STATIC | 满足 |
| 同一规则唯一权威实现；跨语言靠合同校验               | `apps/api/app/services/project_access.py`；`hooks/useProjectAccess.ts`；OpenAPI 快照                                                    | [32]：单一授权源；`export_openapi.py --check` 通过、SDK 322 passed（[40]）                                                               | EXEC   | 满足 |
| 工作台装配/领域/命令/副作用/视图边界清楚             | `Workbench/state/{taskNavigation,useMaskMutationWorkflows,maskMutationPolicy,…}`；`shell/SelectionCardContent.tsx`；`repository-map.md` | [33]：model 8919→6646，六条规则下沉 + 两组视图迁入 shell                                                                                 | EXEC   | 满足 |
| `signals.py` 封闭分派去重复骨架，特殊终态保留        | `apps/api/app/workers/signals.py`（121 行）；`services/async_job_terminal.py`                                                           | [32]/[28]：`partial`/`rollback_failed`/`cancelled` 语义保留；`test_worker_signals.py` 8 例                                               | EXEC   | 满足 |
| 无新万能 helper、平行权限、包装层或循环依赖          | `project_access.py` 单一授权源；P5 领域模块归属                                                                                         | [33]：逐行等价核对 + 有据 KEEP                                                                                                           | STATIC | 满足 |
| 旧 shim/别名/导出/fixture/历史兼容实删；保留有消费者 | `apps/api/tests/conftest.py`；`coalesce_legacy_into_tool_bindings`                                                                      | [29]：ORM shim 与 `httpx_client_bound` 删除；`coalesce` 因生产路由仍在用而保留在兼容边界                                                 | EXEC   | 满足 |

### 2.2 测试

| §10 标准                                      | 权威来源                                                                                      | 最新实际证据                                                                                  | 类型   | 状态 |
| --------------------------------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ------ | ---- |
| 关键约束均有测试；删测试有替代/失效依据       | `docs/research/28`（C1–C8）；`test_seed_owned.py`、P5 领域回归                                | C1–C8 无未映射项；P0 两处 GAP 已补                                                            | EXEC   | 满足 |
| 前端不靠旧角色数据或大规模无关 hook mock      | 27 个前端旧角色文件修正；`ProjectDataManagerPage.flow.test.tsx` 改 MSW 边界                   | [30]/[41]：data-manager 重建为 API 边界 + 纯 URL 下沉；5626 用例通过                          | EXEC   | 满足 |
| 后端直接用现行模型，不全局替换 ORM 构造器     | `apps/api/tests/conftest.py`、`factory.py`                                                    | [29]/[40]：43 处旧 kwargs 迁 `tool_bindings`，shim 删除；4428 collected / 0 failed / 0 errors | EXEC   | 满足 |
| 安全/并发/事务/幂等/离线/渲染保护未因精简丢失 | `test_project_member_concurrency.py`、`test_review_evidence_guard.py`、`test_task_lock*`；E2E | [29]/[31]/[32]：独立事务与锁序保留；[42] 渲染资格                                             | EXEC   | 满足 |
| 覆盖率口径/排除项/阈值可信，无自动降门槛      | `apps/web/vite.config.ts` thresholds；`src/test/**` 排除理由                                  | [30]：阈值 45/45/45/70 未变；删除逐版本流水账并记录排除口径；实测 [41]                        | STATIC | 满足 |

### 2.3 CI E2E

| §10 标准                                          | 权威来源                                                                                  | 最新实际证据                                                                                                          | 类型   | 状态          |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------ | ------------- |
| PR 核心/专项/全量职责明确；7/9 默认策略已替换     | `scripts/plan-e2e-suites.mjs`；`ci.yml`；`e2e-run.yml`                                    | [43] 最终：planned 选择 + 必需 suite 审计 + `core-flaky` 门禁；`E2E_SELECTION_MODE=legacy` 回退；composed 审计 exit 0 | EXEC   | 满足          |
| fixture 清理不误删；未隔离部分安全串行            | seed owned 命名空间；`test_seed_owned.py`；`workers:1`                                    | [31]：邻居探针 + 失败证据先留                                                                                         | EXEC   | 满足          |
| 请求取消判定集中且有负向测试；写入失败不被忽略    | `apps/web/e2e/helpers/request-errors.ts`；`apps/web/scripts/video-request-errors.test.ts` | [31]：16 例同时覆盖允许/相邻禁止侧与跨流程不渗透                                                                      | EXEC   | 满足          |
| 首次失败/flaky/超时/环境/取消/未执行分别统计      | `scripts/summarize-e2e-results.mjs`（+ 单测）                                             | 分类实现存在；**远端 CI 未运行**，仅本地探针/单测验证                                                                 | STATIC | 满足（LIMIT） |
| 同构建条件复用产物；不同 mode 不误用缓存          | `e2e-run.yml` build job 指纹 + `SHA256SUMS`                                               | 本地 archive 建/验、篡改拒绝、多 archive 拒绝探针；**远端 CI 未运行**                                                 | EXEC   | 满足（LIMIT） |
| 汇总稳定；必需 suite 缺失不能过；纯文档跳过有理由 | `scripts/summarize-e2e-results.mjs` fail-closed；docs-only 白名单                         | [36]/[43]：audit/planner 18 passed；冻结 898 实跑审计 exit 1，composed 修正门 exit 0；纯文档双侧回归                  | EXEC   | 满足          |
| 定时全量与候选发布入口明确，有保守回退方案        | `plan-e2e-suites` scope（extended/full）；`E2E_SELECTION_MODE`                            | [43]：schedule=full、dispatch 显式 scope、legacy 回退；候选发布记录见本文件 §1；**无远程 runner 观察**                | STATIC | 满足（LIMIT） |

### 2.4 注释与文档

| §10 标准                                                     | 权威来源                                                                                          | 最新实际证据                                                                                             | 类型   | 状态 |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------ | ---- |
| 活动源码/测试/CI 无用版本叙事已清理，未搬回注释              | §7.1 指定文件（conftest、vite.config、vitest.setup、playwright.config、helpers、signals、CI）     | [34]/[35]：指定文件命中 36→0；`CLAUDE.md` provenance 指导收敛为 Git/CHANGELOG/ADR（不放源码注释）        | EXEC   | 满足 |
| session 绑定/覆盖率/渲染限制/迁移回退/信号兜底说明与实现一致 | `conftest.py`、`vite.config.ts`、`playwright.config.ts`、`validate_migrations.py`、`signals.py`   | [29]（事务绑定）、[30]（覆盖率）、[42]（渲染限制）、[39]（迁移回退）、[28]（信号）                       | STATIC | 满足 |
| API/协议版本、迁移元数据、锁文件、许可、真实历史保留         | §7.3；`alembic` `revision/down_revision`；`pnpm-lock.yaml`                                        | [26]/[29]/[39]：迁移元数据与锁文件未改写                                                                 | STATIC | 满足 |
| 引用/命令/架构文档/共享指引同步，文档构建通过                | `docs-site/dev/concepts/repository-map.md`；`docs-site/dev/testing.md`；`docs/research/README.md` | P10：`pnpm docs:build` 通过（见 §5）；本计划附录 `request-errors.ts` 路径更正；三个临时 preview 配置移除 | EXEC   | 满足 |

## 3. P9 composed 门禁与证据分层（EXEC / STATIC / REMOTE-CI）

> 以下 `/tmp/opencode/...` 为**历史**证据路径；其摘要已持久化到本文件与 [43]。这些 task-owned 原始 custody（含目录、archive 与日志）已在 P10 收尾时**全部移除**；本节只保留结论与来源映射，路径本身不再存在。

- **冻结 `898505469` full-12（历史，失败）**：`default-four` 13P/1F/53 not-run；对该 12 个规范状态实跑审计 **exit 1**，唯一阻塞 `default-four`；证据 `/tmp/opencode/p9-final-evidence/failed-campaign-audit/`（已移除）。
- **集成根 `13d274326` composed（最终）**：`layout-stress` 6/0/0/0、`default-four` 68/0/0/0（修正构建 `dabedc89`/`449aac28`），其余 10 套件按变更影响范围复用冻结 `898` 状态；`node scripts/audit-e2e-requirements.mjs required-suites.json suite-status` → **exit 0**；逐套件来源 `frozen898-reuse` / `followup-replaced` 见 `/tmp/opencode/p9-followup-provenance/final-gate/PROVENANCE.json`（已移除）。
- **成员/执行覆盖**：shards 1–3 身份集合不变（83/73/88）；修正 shard 4 = 冻结 67 身份 + 1 新回归；冻结的 1F + 53 not-run 共 54 个身份均在新报告以 expected 执行（`/tmp/opencode/p9-followup-provenance/shard-membership-proof.md`，已移除）。
- **相关单测**：`/tmp/opencode/p9-followup-unit.log`（已移除）2 files / 97 tests，exit 0；post-68 原生取消补充 `workbench-layout.spec.ts:366` 1 passed（23.8s）。
- **STATIC**：全仓台账/TSV、阈值与排除口径、planner/审计/报告脚本契约、本文件的差异比对。
- **REMOTE-CI**：**未运行**（无 push）。P0 校准的 60 次历史运行属基线，不是候选证据。

## 4. P10 收尾产生的最终树变更

- 移除三个临时 preview 配置（历史装置，不再随树提供）：`apps/web/playwright.preview.e2e.config.ts`、`apps/web/playwright.preview-visual.config.ts`、`apps/web/playwright.preview-stress.config.ts`。
- 最终文档与台账：`docs/research/{26,27,43,45,46,47,48,49,50}.md`、`docs/research/README.md`、`docs/plans/archive/1789880018_repository-optimization-plan.md`、`docs-site/dev/testing.md`。
- 未做产品/CI/依赖/版本号变更；未 push；未改普通开发数据库。

## 5. P10 本地验收检查（实际命令与结果）

> 本节记录在 `worktree-agent-opt-p10-final`（基于集成根 `8c4276297`）上为文档收尾实际执行的检查。文档-only 收尾不重跑已接受的 [39]–[42] 套件或浏览器模式。

（实际执行结果见下表；文档-only 收尾，未重跑 [39]–[42] 测试/浏览器套件，也未初始化 test/e2e 模式。）

| 检查             | 命令                                             | 结果                                                                                                                                   |
| ---------------- | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| 文档构建         | `pnpm docs:build`                                | **exit 0**，`build complete in 35.29s`（VitePress dead-link 检查随构建通过）                                                           |
| 仓库格式         | `pnpm format:check`                              | **exit 0**，`1278 files already formatted`，Prettier 全通过                                                                            |
| 工作流命名       | `node scripts/check-workflow-names.mjs --strict` | **exit 0**，“全部合规”                                                                                                                 |
| 空白/冲突标记    | `git diff --check`                               | **exit 0**，无输出                                                                                                                     |
| 计划附录引用     | 手工核对                                         | `apps/web/e2e/helpers/request-errors.ts` 为集中分类器实际路径                                                                          |
| 临时预览配置移除 | `git status`                                     | 三个 `apps/web/playwright.preview*.config.ts` 已删除；无活动配置消费者（历史引用已在 [43]/[46]/[47]/[48]/[49] 标注为已移除的历史装置） |

## 6. 限制

- **远端 CI 未运行**（无 push）：构建复用、审计、ml-cpu 的首次真实 runner 未观察；这是唯一跨项限制。
- 渲染资格为 [42] 的独立接受结论（严格 WebGPU 21 / 严格 WebCodecs 9，浏览器自报 adapter）；硬件视频解码未测量，不构成 GPU/硬件解码资格主张。
- before/after 只含可复核静态/计数维度；全部性能与耗时对比未测量。
- 复用基于**变更影响范围**与输入/配置/依赖等价，不是全应用逐字节等价；若配置/依赖身份变化，复用失效并须复跑。

## 7. 关闭声明

P0–P10 全部工作包完成；计划 §10 的 22 项按实际证据勾选（§2）；`docs/research/27` 台账关闭（§8）。唯一未验证边界为**远端 CI 未运行**（无 push）；渲染器通道 [42] 已接受严格 WebGPU 21 / 严格 WebCodecs 9（浏览器自报 adapter），**硬件视频解码未测量**。本文件取代其早先的“draft 证据矩阵（仅准备）”性质。

## 8. P10 收尾清理回执（2026-09-21）

> 在执行本节前，P9/P10 的结论、命令、指纹与计数已持久化到本文件、[43] 与 [27]；root 已独立核对接受的 raw 报告与校验和。清理删除本 campaign 的**全部 task-owned 原始产物**（包括先前的 custody 目录与日志）；仅保留简洁 English handoff 摘要。

**已删除（全部 task-owned 原始产物）**

- 顶层 `p9-*` 原始文件（套件 JSON/log、探针、campaign 驱动脚本等，约 165 个）。
- 临时目录：`p9-dist-check`、`p9-injection`、`p9-status`、`p9-visual-failure`、`p9-final-dist-extract`、`p9-failed-campaign-audit`。
- 构建产物归档与顶层校验和：全部 `web-e2e-dist-*.tar.gz` 与顶层 `SHA256SUMS*`。
- **原始 custody（本轮更正为全部移除）**：`/tmp/opencode/p9-final-evidence/`（含已复制的最终 archive 与 `EVIDENCE-SHA256SUMS`）、`/tmp/opencode/p9-followup-provenance/`、`/tmp/opencode/p9-followup-status/`、`/tmp/opencode/p9-final-status/`、`/tmp/opencode/p9-followup-unit.log`。
- 本任务 scratch：`p10-docs-build*.log`、`p10-format-check.log`。

**保留**

- `/tmp/aap-opt-*.md` 简洁 English handoff 摘要（非原始产物）。

**结果（absence checks）**：上述五个 task-owned raw custody 路径均已确认不存在；`/tmp/opencode` 下已无 `p9*` 条目；未触碰无关 `/tmp` 文件、普通数据库或用户 `.orca-dag.config.json`。构建产物字节不再保留，最终产物的身份以本文件与 [50] 记录的 fingerprint `dabedc89…` / `dist/index` `449aac28…` 为准。
