# 仓库优化改造台账（P0–P10）

> 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md` · 基线与证据：`docs/research/26-repository-optimization-baseline.md`
> 状态日期：2026-09-20。维护约定：每个阶段落地后更新本表，写明实际执行的验收命令与结果；未完成项不得标成完成。

## 1. 阶段台账

| 阶段                  | 范围                                                                                   | 依赖                | 状态                                                                                             | 验收门（详见计划 §8；证据落本表）                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------- | -------------------------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0 清单与基线         | 全仓目录分类、测试清单、CI 实证、不变量映射                                            | 无                  | **完成**（a24fb644e + 复审修正提交）                                                             | 每个受管目录有分类结论 [26§2]；CI ≥20 次实际结果且计数可加和 [26§5]；8 条约束全部映射测试 ID，缺口显式列出 [26§4]；机器可读测试文件清单 1042 项 [26§3 + `data/26-…tsv`]；已执行/未执行检查分列 [26§7]                                                                                                                                                                                                                |
| P1 固化契约           | 第 3 节 8 条不变量映射复核；旧平台角色测试数据修正；补 `signals.py` 直接测试；负向验证 | P0                  | **完成**（本提交）                                                                               | 57 文件/39 具名 ID 映射无缺口；旧平台角色命中逐条分类（7 后端 + 27 前端修正，合法项目职责保留）；新增 `test_worker_signals.py` 8 例；3 处变异破坏→失败→恢复（signals 终态守卫、viewer×work fail-closed、自审/证据拒绝）；完整后端套件退出码 0；证据 [28]                                                                                                                                                             |
| P2 后端 fixture       | 迁移旧 kwargs fixture；删 ORM shim 与 `httpx_client_bound`；收敛 factory               | P1                  | **完成**（`docs/research/29`）                                                                   | 43 处旧 kwargs 迁 `tool_bindings`；`httpx_client_bound` 70 文件归一；测试库守卫 fail-closed + 13 个纯规则测试；生产 `coalesce_legacy_into_tool_bindings` 保留在兼容边界；后端套件绿                                                                                                                                                                                                                                  |
| P3 前端测试边界       | vitest.setup/vite.config/test/mocks、data-manager 流程样板                             | P1                  | **完成**（`docs/research/30`）                                                                   | data-manager 样板改 MSW API 边界；纯 URL 规则下沉 `dataManagerUrlState.test.ts`；阈值 45/45/45/70 不放宽；schema 重试缺陷独立修复 + 回归                                                                                                                                                                                                                                                                             |
| P4 后端规则归属       | `signals.py` 封闭分派；权限推导审计                                                    | P2                  | **完成**（`docs/research/32`）                                                                   | 授权真值收敛 `project_access`；`signals.py` 薄骨架 + `async_job_terminal` 封闭分派；`partial`/`rollback_failed` 不被抹平；成员 factory 收敛 97 处                                                                                                                                                                                                                                                                    |
| P5 工作台收敛         | 装配 model 瘦身、纯策略先行                                                            | P3（后端耦合处 P4） | **完成**（`9e34dfafb`；证据 [33]）                                                               | 切换/取消/拒绝/刷新回归绿；无循环依赖；不是搬进更大 helper                                                                                                                                                                                                                                                                                                                                                           |
| P6 残留与注释         | §7 文件清单、版本叙事、候选 #10                                                        | P2–P5 稳定后        | **完成**（[34] ML + [35] 剩余 + Users MSW（0e90da462）+ §7.2 迁移验证（[39]）；快照见 [26§10]）  | ML-1/2 池内核下沉共享包 + 并发套件合并（负向变异验证）；`gpu_lifecycle`/`embedding_cache` KEEP 有据；seed persona/成员职责修复 + 一次性库实测；AdminPeople 徽章按平台身份呈现；gpu_arbitration provenance + 扫描器 allowlist 同步；SDK/scripts/config 版本叙事清理；`CLAUDE.md` provenance 指导收敛；通知谓词与回滚取消为有据 KEEP；UsersPage 整体 MSW 化；§7.2 迁移真实验证 runner/策略；P0 候选 #1–#10 与 TSV 收口 |
| P7 E2E 下沉与隔离     | 错误分类器统一、fixture 命名空间隔离、超时/断言家族分诊                                | P1                  | **完成**（`b8b45797e`；证据 [31] + P7 报告）                                                     | 列表内测试连续 2 次不靠 retry 通过；允许/禁止两侧负向测试；隔离验收前保持 `workers:1`                                                                                                                                                                                                                                                                                                                                |
| P8 CI 选择与报告      | planner docs-only 白名单、构建复用、报告区分 not-run/flaky                             | P7                  | **完成**（root `6c47d68de`；[36] CI 选择/报告 + [37] CPU 接线 + [38] 核心 smoke；迁移验证 [39]） | planner 全分支有测试（18 passed）；非法选择不空跑；必需 suite 缺失不能过汇总；影子选择仅告警、门禁未切（门禁切换属 P9）                                                                                                                                                                                                                                                                                              |
| P9 影子验证与门禁切换 | 新旧选择对照、条件式必需检查                                                           | P8                  | **完成**（最终 composed 必需套件审计在集成根 `13d274326` 实际退出 0；报告 `8c4276297`；[43]）    | 差异可解释；冻结 `898` 的 12 套件 campaign 保持失败历史（default-four 13P/1F/53not-run，实跑审计 exit 1）；`layout-stress`/`default-four` 在修正构建 `dabedc89`/`449aac28` 上重跑替换，其余 10 套件按变更影响范围复用；`main` 现状无远端保护 [26§5.4]                                                                                                                                                                |
| P10 完整验收          | before/after 指标、迁移门硬化、台账关闭                                                | P0–P9               | **完成**（2026-09-21；最终验收记录 [45]，本台账关闭）                                            | 计划 §10 全部 22 项按实际证据勾选（见 [45]）；唯一跨项限制为远端 CI 未运行、无 push；渲染资格由 [42] 单独接受                                                                                                                                                                                                                                                                                                        |

## 2. 当前有效决定

1. CI 证据以校准 P0-B 为权威源（60 次运行、49/6/4/1）；断言差异类失败一律“证据不足/疑似”，修复归属按提交主题仅作线索。
2. `main` 无分支保护/ruleset（三项只读检查一致）；是否新建保护须先核实现行要求，不作为本计划的默认交付。
3. 本工作树运行后端/E2E 测试只允许 `pnpm dev:worktree` 供给的 `aap_wt_*` 自有数据库（经 `TEST_DATABASE_URL` 注入，conftest L117-118 环境变量优先 [26§1.1]）；共享遗留库 `annotation_test`/`annotation_e2e` 禁止。conftest 本身的剩余缺口是 `_default_test_db_url` 异常回退静默吞错（P2 整改），不是“库名固定”。
4. 真实缺陷独立提交“缺陷修复 + 回归测试”，不夹带在重构内；不得放宽断言、覆盖率、重试或预算。
5. 计划文件按原件入轨：入轨时仅 prettier 表格对齐 + 去除尾随空格（仓库 pre-commit `trailing-whitespace` 钩子要求）；去空白规范化后哈希一致 `b9d3b944…`。
6. `"annotator"` 字符串命中（20 个前端测试文件）是**候选**而非既证废弃角色：该字符串仍是合法项目职责名（`apps/web/src/types/index.ts` `ProjectRole`、`constants/roles.ts` `PROJECT_ROLES`）；P1 逐条分类后再决定修数据或保留。
7. 测试文件清单以机器可读 TSV 为准：`docs/research/data/26-repository-test-file-inventory.tsv`（P0 时点 1042 行；P6 收口后 **1136 行 = 1132 可执行测试 + 4 测试支撑模块**；`git ls-files` 确定性发现，列 path/layer/runner/dependency/decision/reason/replacement/status；P0 一律 KEEP + pending-review，P1–P7 逐层复核为 KEEP，接线状态为静态读取而非执行证据；`reason` 列已按 [26§14] 补齐为具名理由或区域级保守保留理由（无 `pending-review`），后者明示为区域级保留、非逐文件语义审计）。

## 3. 未决事项

- 89 个测试文件（`apps/_shared` 三个包 + 五个 ML backend + 根 `scripts/image-reference-utils.test.mjs`）的 PR 门禁接线**已在 P8 收口**：执行入口为可复用 workflow `.github/workflows/ml-cpu-test.yml` + `scripts/run-ml-cpu-tests.sh`（每套件独立 venv；gs2/sam3 装 CPU torch，yolo 不用 torch/ultralytics），`ci.yml` 新增 `ML CPU contract tests` caller（[36]/[37]）；`scripts/image-reference-utils.test.mjs` 已接入 `ci.yml` 的 node 测试列表。独立 CPU 审计已证明这些文件**全部 CPU 可运行**（12 个需 CPU torch），不存在“硬件专属”测试。
- 首次执行耗时与每测试历史失败类型量化：P8 已交付 `scripts/summarize-e2e-results.mjs` 的分类统计（首过 / 重试后过 / 超时 / 中断 / 未运行）与 4 例单测（[36]），但**候选级量化与真实 CI 运行**仍属 P9 / 远程 CI（未收口）。
- ~~`test_v0_7_6.py` 等版本命名文件的内容级审阅（P2/P6）~~ **P6 已关闭**：改名 `test_project_attribute_schema_and_batch_reset.py`（内容用现行 fixture/模型，头部版本叙事删除，`batch-module.md` 与 TSV 同步）。
- ~~断言/超时失败家族（filter-operational-lists、employee-project-roles、auth、bbox-center-out、mask-slice、video-tracker-local-review、NotificationPreferencesPanel）首败分诊（P7.1）~~ **P7 已收口**（[31] + P7 报告；按首败分类，保留 `workers:1` 串行边界）。

## 4. 未执行检查记录

**P0 阶段**（原记录）：后端 pytest、前端 vitest、Playwright、迁移类检查均未执行（需工作树自有 `aap_wt_*` 数据库或 e2e 隔离服务，当轮授权仅覆盖文档检查）；`plan-e2e-suites.test.mjs` 未复跑（沿用 P0-A 执行记录，协调方确认无需重复）。

**P1 阶段**（已执行，本工作树自有库 `aap_wt_3ca3e833cfeaf760_test`，head 0174）：改动后端文件 64 passed；新增 `test_worker_signals.py` 8 passed；完整后端套件执行到 `[100%]` 且**退出码 0**（最终计数行被 pytest quiet addopts 静默，按退出码与 0 `FAILED`/`ERROR` 判定，日志 `/tmp/aap-p1-backend.log`）；前端全量 vitest 5580 passed；`pnpm --filter @anno/web typecheck`、`lint`（0 error）、`ruff check`/`ruff format --check`、`prettier --check`、`git diff --check` 均通过；3 处破坏→失败→恢复变异验证见 [28§5]。

**仍未执行**：Playwright/E2E、迁移 round-trip、覆盖率全量、Python SDK/示例协议与 `pnpm docs:build`（归 P3/P7/P8/P10）；`plan-e2e-suites.test.mjs` 未复跑（沿用 P0-A 记录）。

## 5. P0 交付与检查记录（持久，替代 /tmp 报告）

- **交付提交**：初审 `a24fb644e`（基线 + 台账 + 计划入轨）；复审修正为本节所在提交（更正测试库解析表述、`"annotator"` 候选定性、`apps/web/scripts` 测试接线归属，并新增机器可读测试文件清单）。
- **实际执行的检查与结果**：全仓 `prettier --check .`（初审与复审均通过）；`pnpm docs:build`（初审通过，32.45s；复审未改 `docs-site/`，无导航风险，未重建）；`node scripts/check-doc-version-prefix.mjs --staged`（无发现）；`git diff --check`（干净）；提交时 pre-commit 钩子全部通过（trailing-whitespace / end-of-file / prettier / large-files / merge-conflict）；清单行数校验（TSV 1042 数据行 = 分层计数之和：backend-api 359 + frontend-unit 549 + e2e-browser 67 + screenshots 8 + frontend-tooling 5 + repo-scripts 4 + worktree-runtime 8 + python-sdk 25 + ml-examples 2 + ml-backend-shared 8 + mask-utils-shared 5 + docs-tooling 2；且 TSV 路径集合与独立 `git ls-files` 多模式计数逐行比对一致；发现初版漏掉根 `scripts/*.test.mjs` 4 行，已修正）。P6 补录后的行数校验见 [26§9]（1128 行：ci-wired 1024 / script-wired 15 / not-wired 89）。
- **复审产生的接线更正**：`apps/web/scripts/` 5 个测试文件中 4 个（check-bundle-size、video-bench、video-request-errors、video-timeline-seek）由 vitest 收集、随 CI Frontend verification 运行（`vite.config.ts` test.exclude 及其注释 [V]）；`media-derivation.test.mjs` 为 npm script `node --test`（script-wired）。真正无接线的只有 `apps/_shared` 两个 tests 目录（13 个）与根 `scripts/image-reference-utils.test.mjs`。
- **证据留存**：工作树运行时与一次性数据库身份、conftest 解析三层行为已固化于 [26§1.1]，不再依赖 /tmp 交接文件；CI 失败/取消/重试/超时口径在 [26§5]；计划文件等价性证据（规范化哈希 `b9d3b944…`）在 [26§8] 与本表决定 5。

## 6. P6 收口对账（2026-09-20，历史快照；最新状态见 §7）

- **接受提交**：P6 实现在 root `11d81d05c` 接受（[34] ML/shared + [35] 剩余稳定域 + P5 残留清理 + UsersPage MSW 化 `0e90da462` + §7.2 迁移真实验证 [39]）。本表据此把 P5/P6/P7 标为**完成**（P5 `9e34dfafb`、P7 `b8b45797e`），P8 标为**进行中（未接受）**，P9/P10 未开始。**（本行为 P6 收口时点事实；后续 P8 已于 root `6c47d68de` 接受，P9 进行中、P10 pending，见 §7。）**
- **清单对账**：TSV 现为 **1136 行 = 1132 可执行测试 + 4 测试支撑模块**（新增 `test-support` 层，与原 `backend-api` 行分离）；发现侧双向比对 0 缺口。本轮补录 8 行：`apps/api/tests/test_conftest_db_url.py`（P2 纯规则）、6 个 P5 Workbench 测试、`scripts/test_alembic_migration_policy.py`（§7.2 策略回归）。分层计数：backend-api 360 / frontend-unit 558 / repo-scripts 5 / test-support 4 / docs-tooling 2 / e2e-browser 67 / frontend-tooling 5 / mask-utils-shared 5 / ml-backend 70 / ml-backend-shared 13 / ml-examples 2 / python-sdk 25 / screenshots-tooling 12 / worktree-runtime 8。接线状态：ci-wired 1031 / script-wired 15 / not-wired 90。
- **未接线口径**：89 个 ML/shared 测试 + 1 个新增迁移策略测试为 `not-wired`；前者经独立 CPU 审计证明**全部 CPU 可运行**（12 个需 CPU torch），执行入口为 `.github/workflows/ml-cpu-test.yml` + `scripts/run-ml-cpu-tests.sh`，PR caller 待 P8 收口。不存在“硬件专属”测试的归类。**（更新：P8 已在 `ci.yml` 接入 `ML CPU contract tests` caller；见 §7。）**
- **明细与快照**：见 [26§10]（P6 时点 delta）与 [26§11]（最新）；候选 #1–#10、P5 `canBatchConvert`/helpers KEEP、Users 27 用例与 [39] 迁移验证的最终去向见 [35§3]/[35§6]。
- **未决**：P8 选择器/工作流修正未接受；P9/P10 未开始。P0 台账决定 1–7 继续有效。**（更新：P8 已于 root `6c47d68de` 接受；P9 进行中、P10 pending。见 §7。）**

## 7. P8 接受与 P10 验证通道之后的状态（2026-09-20）

> 本节是 §6（P6 收口）之后的当前状态；只追加，不改写 §0–§6 的历史记录。P10 完整验收证据矩阵草稿见 [45](./45-repository-optimization-final-acceptance.md)。

- **接受提交（当前验收根 `e91ac8dfd`）**：P0–P8 全部接受；P8 修正在 root `6c47d68de`（[36] CI 选择/报告、[37] CPU 接线、[38] 核心 smoke、[39] 迁移真实验证）；P10 验证通道 [40]（后端/SDK/ML 契约 `7efcb27a0`/`f90dea73e`）、[41]（前端 acceptance `6fb6b13e6`/`b8897df28`）、[42]（渲染器资格 `5e2ad830e`/`ac2eae8d8`/`e91ac8dfd`）；点云/lidar 消费者回归修复 `d807b3482`/`e41e4973c`/`112df2009`（[44]）。
- **阶段状态**：P0–P8 **完成**；P9 **进行中**；P10 **未开始（pending）**，仅产出草稿证据矩阵 [45]（非完成）。**（更新：P9/P10 均已收尾，见 §8。）**
- **清单（已收口）**：TSV 已按确定性发现回填至 **1138 行 = 1134 可执行 + 4 测试支撑**；`git ls-files` 发现与 TSV 路径双向比对 0 缺口；状态 **ci-wired 1123 / script-wired 15 / not-wired 0**（含 P8 的 2 个新测试与 90 个原 `not-wired` 的接线）。方法与依据见 [26§12]。
- **覆盖率口径**：阈值 `45/45/45/70` 未变；前端实测 72.28/79.33/66.92/72.28（[41]），后端 line 73.61 / branch 55.33（[40]）。
- **未决**：P9 影子对比与门禁切换（他处进行中）、P10 终验（按复用规则，仅复跑实际变更/未验证的行为）、P9 的 `43` 就绪后的最终 docs 构建；候选远程 CI 如实记录为未运行（无 push，非门禁要求）。见 [45§5]。P0 台账决定 1–7 继续有效。**（更新：P9/P10 已收尾，见 §8。）**

## 8. P9/P10 收尾（2026-09-21）

> 本节是 §7 之后的当前状态；只追加，不改写 §0–§7 的历史记录。

- **P9 完成**：最终 composed required-suite 审计在集成根 `13d274326` 上实际**退出 0**（10 套件按**变更影响范围**复用 + `layout-stress`/`default-four` 在修正构建 `dabedc89`/`449aac28` 上重跑替换）；冻结 `898505469` 的 12 套件 campaign 保持**失败历史**（default-four 13P/1F/53 not-run，实跑审计 exit 1）；修正产品提交 `13115f71f`，文档修正 `9e11103ab`/`13d274326`，P9 报告 `8c4276297` [43]。
- **P10 完成（台账关闭）**：最终验收记录 [45]；计划 §10 的 22 项按实际证据勾选。复用依据是**变更影响范围**（共享 Workbench 源码在修正构建中确有变化），不是全应用逐字节等价；唯一跨项限制为**远端 CI 未运行**（无 push）。
- **P10 收尾清理**：移除三个临时 preview 配置（`apps/web/playwright.preview.e2e.config.ts` / `playwright.preview-visual.config.ts` / `playwright.preview-stress.config.ts`，历史装置）；报告文档引用改为 `apps/web/e2e/helpers/request-errors.ts`；campaign 的**全部 task-owned 原始产物**（含 custody：`p9-final-evidence`、`p9-followup-provenance`、`p9-followup-status`、`p9-final-status`、`p9-followup-unit.log` 及全部 dist archive）已全部移除，仅保留简洁 English handoff 摘要；产物与结果身份以 [45] §3/§8 与 [50] 记录的 fingerprint/计数为准。
- **清单**：TSV **1139 行 = 1135 可执行 + 4 测试支撑**，采用保守 KEEP 语义（区域级保留理由），不是逐文件唯一性/等价性声明。
- **仍未运行**：远端 CI；渲染器通道 [42] 已接受严格 WebGPU 21 / 严格 WebCodecs 9（浏览器自报 adapter nvidia/ampere、Chromium 147），硬件视频解码未测量，不构成硬件资格主张。
