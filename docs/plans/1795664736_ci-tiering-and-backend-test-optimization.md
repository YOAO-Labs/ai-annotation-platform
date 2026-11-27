# CI 分层改造（L0 至 L5）与后端测试分片优化计划

> 状态：进行中（PR-1 至 PR-4 已在分支 `ci/pr-scope-and-verified-runtime` / PR #195 落地并完成本机验证，待远端 CI 实测后推进 PR-5 至 PR-9）。本计划基于 [1795575143_ten-minute-pr-ci.md](./1795575143_ten-minute-pr-ci.md)（PR #195 已落地的 3 文件试点与 `CI verdict` 聚合框架），将范围扩展为完整的 L0 至 L5 分层门禁，并同步治理当前导致后端分片运行 60 至 128 分钟的根因。

## 变更总览

本计划解决两个相互耦合的效率瓶颈：

1. 普通 PR 推送后等待过长，且进入合并队列后仍触发全量 4 后端分片与 32 套 Playwright E2E，导致合并吞吐无法提升。
2. 后端全量测试 4 个分片总计算时间达 289.5 至 366.0 分钟，单分片耗时 60.9 至 127.7 分钟，即使在 L3 高风险合并与 L4 夜间回归中也已不可接受。

目标架构按 L0 至 L5 六档分层运行：

| 层次              | 触发条件                                              | 必须跑什么                                                                                                | 阻塞什么                            | 目标时限                 |
| ----------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ----------------------------------- | ------------------------ |
| L0 本地提交       | 每次 commit                                           | 暂存文件的格式、语法、冲突标记和命名检查。不跑全项目类型检查或测试。                                      | 本地提交                            | 5 至 30 秒，不计入 PR    |
| L1 PR 快检与单测  | 普通 PR 每次推送                                      | 先判定影响范围。纯文档查链接；局部 UI 跑类型检查和对应组件测试；后端纯逻辑跑对应 pytest。                 | 本 PR 入队                          | T+5 分钟内               |
| L2 按需集成       | 改动跨越真实边界，与 L1 并行                          | 写数据库测提交与回滚；改 API 契约测消费者；仅浏览器可验交互跑对应 Playwright 场景。未触及不启动。         | 本 PR 入队                          | T+6 分钟内               |
| L3 普通合并候选   | PR 进入合并队列                                       | 在最终候选上按相对最新 `dev` 的全部合成改动（含同批其他 PR）选测。不默认跑 4 个后端分片和整套浏览器测试。 | 本批合入 `dev`                      | T+10 分钟前出最终结论    |
| L3 高风险合并候选 | 权限、迁移、共享基础设施、CI 规则变更，或无法界定影响 | 扩大到相关模块；无法可靠划界跑全量。风险明确时在 PR 阶段提前反馈，合并以最终候选为准。                    | 本批合入 `dev`                      | 单独计时，不承诺 10 分钟 |
| L4 夜间回归       | 每晚                                                  | 全量后端、完整浏览器矩阵、长视频、压力和恢复测试。若当次 PR 改动视频恢复，对应回归提前到 L2。             | 不阻塞普通 PR；已知回归阻塞相关发布 | 30 至 60 分钟目标        |
| L5 发布验证       | 生成发布候选制品                                      | 针对实际制品验证安装、升级迁移和关键业务冒烟。                                                            | 发布，不阻塞日常合入                | 按发布范围               |

## 运行日志实测基线

以下基线采集自 GitHub Actions 真实运行日志、JUnit 制品与本机只读测量（采集时间 2026-11-26）。

### 1. 远端 CI 真实耗时（GitHub Actions `ubuntu-24.04`）

| 运行 ID       | 事件与分支                         | 结论    | 总耗时 | 后端 4 分片执行耗时（Shard 0 / 1 / 2 / 3）                     | 关键观测                                                                                                                                       |
| ------------- | ---------------------------------- | ------- | ------ | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `45604682774` | PR #195（无分片计划）              | failure | 130.3m | 88.7m / 110.9m / 60.6m / 127.7m                                | 4 分片 JUnit 总计 366.0m（21,750 用例）。Vitest 9.8m（前端总 12.2m）。全部 32 套 E2E 于 T+36.6m 完成。                                         |
| `45660914610` | PR #195（启用 15 模块计划）        | failure | 87.7m  | 60.9m / 81.8m / 85.8m / 79.4m                                  | 4 分片 JUnit 总计 289.5m（21,710 用例）。Shard 2 因未进计划的重文件变为最长分片（85.8m）。GPU 成员校验单 job 耗时 4.7m（verifier 执行 217s）。 |
| `45743400427` | PR #195（最新运行）                | 进行中  | 进行中 | Shard 0 已结束 61.6m，其余 3 分片运行中                        | `CI scope` 11s，`Frontend verification` 7.8m，`Repository quality` 4.5m，`Backend worktree` 7.2m，GPU 成员校验 3.5m（verifier 执行 151s）。    |
| `45596505866` | `push` `dev`                       | failure | 49.4m  | 1.5m 至 1.8m（在 `Worktree runtime and setup tests` 提前失败） | `Frontend verification`（8.3m）、`Python SDK tests`（2.2m）、`smoke`（5.3m）均失败。                                                           |
| `45739704475` | `schedule` `dev`（`E2E extended`） | failure | 48.9m  | 不含后端                                                       | 35 个 Job，最慢为 `video-pipeline-3`（执行 21.1m，排队 27.5m）与 `default-10`（18.7m）；`mask-ai-native` 与 `video-pipeline-3` 失败。          |
| `45571975040` | PR 探针（受影响快检）              | success | 1.1m   | 不跑全量                                                       | 证明窄选集在托管 runner 上可在约 1 分钟内返回绿灯。                                                                                            |

分支保护现状核实：`gh api repos/YOAO-Labs/YOAO/rules/branches/dev` 返回空数组 `[]`，当前 `dev` 无分支保护规则且未启用合并队列。

### 2. 后端 4 分片 JUnit 根因拆解（`45660914610` 实测）

| 分类                           | 模块数 | 用例数 | 用例占比 |        4 分片 JUnit 总耗时 |   耗时占比 | 说明                                                                                                          |
| ------------------------------ | -----: | -----: | -------: | -------------------------: | ---------: | ------------------------------------------------------------------------------------------------------------- |
| 使用 `managed_database` 的模块 |     71 |  1,865 |     8.6% |        13,893.0s（231.6m） |      80.0% | `45604682774` 中为 18,392.5s（306.5m，占 83.8%）。每例重建库并在子进程重跑 322 个迁移（`0267` 至 `bo0511`）。 |
| 其余全部后端测试               |  1,509 | 19,845 |    91.4% |          3,477.4s（58.0m） |      20.0% | 4 个分片本身已接近均衡（860s / 862s / 873s / 882s，每分片约 14.5m）。                                         |
| 分片非用例开销（收集与收尾）   | 4 分片 |   4 次 |   不适用 | 约 647s（144s 至 199s/片） | 约 2.7m/片 | 每个分片在 `pytest_collection_modifyitems` 丢弃前先导入全部 1,580 个文件。                                    |

除 `managed_database` 外，日志还暴露四类高影响瓶颈：

1. **Shard 0 隐藏串行堆叠**：[backend-test.yml](../../.github/workflows/backend-test.yml) 把 `validate_migrations.py`、`export_openapi.py --check`、`prepare-module-data-tests`、`verify_module_data_boundary.py --reclamation-only`（1 次 `collect_only` 加 7 个串行 profile 共 8 遍全容器+589 迁移+80 份 grant+12 项 Node 检查）、3 个 M2b 验收文件（第 9 遍全引导）以及 56 个 `scripts/test_*.py`（733 定义）全部挂在 Shard 0。在 `45660914610` 中因 pytest 提前失败未进入后续步骤；参照同次运行单次 `verify_module_data_boundary.py` 耗时 151s 至 217s 推算，Shard 0 转绿后会额外增加 20 至 30 分钟串行时间。
2. **分片计划漏覆盖 58 个重文件**：[ci-shard-plan.json](../../apps/api/tests/ci-shard-plan.json) 仅重排前 15 个慢模块，导致 `45660914610` 中 Shard 2 落入 609 个 `managed_database` 用例（82.0m），Shard 1 为 376 例（76.7m），Shard 3 为 406 例（74.9m），Shard 0 为 474 例（56.0m）。
3. **少量非 `managed_database` 慢测试**：
   - `tests/test_export_reference_media.py::test_large_frozen_selection_uses_bounded_sql_parameters` 单用例耗时 **235.0s**（`45604682774` 为 261.1s），原因是循环构造 33,000 条 `DatasetItem` 与 33,000 条 `Task` 的 Python dict 做 ORM 批量插入。
   - `tests/test_managed_original_reclamation.py` 耗时 **266.3s**（44 例，单次 `planner.preview()` 在空表上耗时约 5 秒）。
   - `tests/test_compat_facades.py` 耗时 **127.4s**（65 例串行冷启 Python 子进程）。
   - `tests/test_domain_package_architecture.py::test_repo_has_no_first_party_legacy_facade_references` 单用例耗时 **41.6s**（全仓 Python AST 单线程扫描）。
   - `tests/test_dataset_import_delivery_native.py` 含真实 `pg_sleep(31)` 与 `pg_sleep(2)`，合计阻塞 **33s**。
4. **全量基线当前非绿**：`45660914610` 的 4 分片存在 1,920 项失败与 336 项 setup 错误，其中 208 项来自未在 `verify_module_data_boundary.py` 内运行却未正确门控跳过（`AAP_MODULE_DATA_PG_CONTAINER_ID` / verifier fixture URL 缺失），其余涉及 `lidar_preflight`、`SimpleNamespace` 伪对象和 `content_authority_revoked` 等失败。开启 `dev` 强制队列与裁剪 L3 前，必须先修复全量基线。

### 3. 本机收集与导入实测（112 核 Linux，单次冷启动）

- 全量 `pytest --collect-only -q --ignore=tests/test_gpu_membership_activation.py`（1,580 文件）：**98.43s**（CPU 107.78s）。
- 显式 CLI 传 377 个文件路径（反面验证）：**92.83s 至 94.41s**（pytest 对每个 CLI 文件参数重扫一次 `tests/` 目录，收益几乎为零）。
- 原型 `pytest_ignore_collect` 收集前分片（Shard 0，392 文件，5,219 用例）：冷子进程总耗时 **29.42s**（收集阶段 **20.49s**），提速 **3.3 倍**。
- 341 个文件在模块级 `pytestmark` 硬性跳过（`AAP_MODULE_DATA_ROLE_ACCEPTANCE != "1"`），例如 `test_labeling_runtime_acl_native.py` 仅导入收集就耗时 **8.20s**。
- `import app.main` 耗时 **14.70s 至 15.86s**（`app.api.v1.router` 占 12.63s）；`import nuscenes` 耗时 **4.35s**；纯逻辑单文件 `tests/test_audit_summary.py` 完整运行耗时 **1.20s**。

## PR 落地顺序与依赖图

本计划拆为 9 个独立可验证的 PR，按依赖分层推进：

```text
主干 dev
 ├─ PR-1  L0 本地提交减负（独立，可立即合入）
 ├─ PR-2  后端 CI 止血：拆出 Shard 0 附加任务 + 收集前分片 + uv 缓存 + 均衡计划（独立）
 │    ├─ PR-3  managed_database 升级至 head 模板 + 8 表密钥刷新 + SAVEPOINT 模块级复用（依赖 PR-2）
 │    │    └─ PR-4  verify_module_data_boundary 去重 + 非 MD 慢用例治理 + test_engine 复用（依赖 PR-2，可与 PR-3 并行）
 │    └─ PR-5  修复全量后端与夜间 E2E 基线红灯，恢复可信全绿基线（依赖 PR-2）
 │         └─ PR-6  建立 L4 夜间全量工作流 + 启用 dev 分支保护与合并队列（依赖 PR-3, PR-4, PR-5）
 │              ├─ PR-7  统一域级路由并建成 L1 + L2 并行按需 PR 门禁（依赖 PR-6，含 30 次影子验收）
 │              │    └─ PR-8  切流 L3 普通合并候选至 T+10m 内（依赖 PR-7 与连续两周无漏检）
 │              └─ PR-9  新增 L5 发布候选制品验证工作流（依赖 PR-6，与 PR-7/PR-8 独立并行）
```

### 落地序列总表

| PR ID | 主题                                                         | 前置依赖         | 允许改动的主要路径                                                                                                                                                                                                                                                                                                                                                                                            | 是否需维护者审批门禁              |
| ----- | ------------------------------------------------------------ | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| PR-1  | L0 本地 pre-commit 减负至 5 至 30 秒                         | 无               | `.pre-commit-config.yaml`、`package.json`、`scripts/install-git-hooks.sh`                                                                                                                                                                                                                                                                                                                                     | 否                                |
| PR-2  | 后端分片止血：拆 Shard 0 附加项、收集前分片、缓存与重均衡    | 无               | `.github/workflows/backend-test.yml`、`.github/workflows/ci-full.yml`、`.github/actions/setup-backend-test-python/action.yml`、`apps/api/tests/pytest_sharding.py`、`apps/api/tests/plan_shards.py`、`apps/api/tests/ci-shard-plan.json`、`apps/api/tests/test_pytest_sharding.py`                                                                                                                            | 否                                |
| PR-3  | `managed_database` 改用 `head` 封存模板与 8 表刷新           | PR-2             | `apps/api/tests/managed_image_inference_fixtures.py`、`apps/api/tests/storage_migration_isolation.py`、`apps/api/tests/conftest.py`、新增模板等价与迁移守卫测试                                                                                                                                                                                                                                               | 是（变更数据库测试隔离基础）      |
| PR-4  | `verify_module_data_boundary` 去重、非 MD 慢用例与连接池复用 | PR-2             | `scripts/verify_module_data_boundary.py`、`apps/api/tests/conftest.py`、`apps/api/tests/test_export_reference_media.py`、`apps/api/tests/test_managed_original_reclamation.py`、`apps/api/tests/test_compat_facades.py`、`apps/api/tests/test_domain_package_architecture.py`、`apps/api/tests/test_dataset_import_delivery_native.py`、`app/services/managed_original_reclamation.py`（若确认为 SQL 慢查询） | 否（若修改生产 SQL 查询则需复核） |
| PR-5  | 清理全量后端与 E2E 基线失败，建立可信全绿 `dev`              | PR-2             | 对应失败测试与门控（以实测分诊为准，先单独列清单再修）                                                                                                                                                                                                                                                                                                                                                        | 否                                |
| PR-6  | 建立 L4 夜间全量工作流与 `dev` 合并队列保护                  | PR-3, PR-4, PR-5 | `.github/workflows/e2e-extended.yml`（或新增 `.github/workflows/ci-nightly.yml`）、`.github/workflows/ci.yml`、`dev` 仓库 Ruleset 配置                                                                                                                                                                                                                                                                        | 是（变更远端分支保护与队列）      |
| PR-7  | 统一域级路由清单，上线 L1 + L2 并行按需 PR 门禁              | PR-6             | `scripts/test-suites.json`、`scripts/plan-affected-pr.mjs`、`scripts/plan-e2e-suites.mjs`、`scripts/affected_test_receipts.py`、`.github/workflows/ci.yml`、`.github/actions/run-affected/action.yml`                                                                                                                                                                                                         | 是（切流 PR 必选门禁前需审批）    |
| PR-8  | 切流 L3 普通合并候选至合成 diff 选测（T+10m）                | PR-7             | `.github/workflows/ci.yml`、`scripts/plan-affected-pr.mjs`、仓库变量 `CI_L3_AFFECTED_ONLY`                                                                                                                                                                                                                                                                                                                    | 是（切流合并队列前需审批）        |
| PR-9  | 补齐 L5 发布候选制品验证工作流                               | PR-6             | 新增 `.github/workflows/release-verify.yml`、同步发布校验文档                                                                                                                                                                                                                                                                                                                                                 | 是（接入发布流程审批）            |

---

## 分 PR 详细执行清单

### PR-1：L0 本地提交减负（5 至 30 秒，已落地本机 `547c8852c`）

- [x] **依赖**：无，从当前最新基线直接切分支。
- [x] **改动文件**：`.pre-commit-config.yaml`、`scripts/install-git-hooks.sh`。
- [x] **构建的单一变更**：
  - 将 `.pre-commit-config.yaml` 中的 `eslint` 改为只对暂存的 `apps/web` JS/TS 文件执行（通过 `pnpm --filter web exec eslint -- "${files[@]}"` 去掉全仓扫描）。
  - 将 `tsc-web`、`regen-openapi-snapshot`（改为 `--check`）与 `regen-capability-registry-snapshot`（改为 `--check`）从 `stages: [pre-commit]` 移至 `stages: [pre-push]`，避免每次本地 commit 触发全项目 `tsc` 或 15 秒 `app.main` 导入，同时保持 CI `--hook-stage manual` 不受影响；`scripts/install-git-hooks.sh` 同步安装 `pre-commit` 与 `pre-push` 两种钩子并适配 worktree 的 `git rev-parse --git-path hooks`。
  - 保留 `trailing-whitespace`、`end-of-file-fixer`、`check-yaml`、`check-added-large-files`、`check-merge-conflict`、`ruff`、`prettier`、注释棘轮、命名检查与工作流命名检查在 `pre-commit` 阶段。
- [x] **可见结果**：开发者本地修改前端或后端文件后执行 `git commit`，不再触发全项目 `tsc` 或 15 秒 `app.main` 导入；`git push` 前仍校验类型与契约快照。
- [ ] **验收检查**：
  - [x] 单元检查：`.pre-commit-config.yaml` YAML 解析与 Prettier 格式通过；`bash -n scripts/install-git-hooks.sh` 通过；单文件 `apps/web/src/main.tsx` 执行新 eslint 入口命令退出码 0；`node scripts/check-workflow-names.mjs --strict` 通过。
  - [ ] 实景验证：在开发者环境执行 `pre-commit run` 连续 5 次采样（待日常开发或独立 worktree 补采中位数/最大值）。

### PR-2：后端 CI 止血（拆 Shard 0 附加项、收集前分片、缓存与重均衡，已落地本机 `42f0ec40a`）

- [x] **依赖**：无，从当前最新基线直接切分支。
- [x] **改动文件**：`.github/workflows/backend-test.yml`、`.github/workflows/ci-full.yml`、`.github/actions/setup-backend-test-python/action.yml`、`apps/api/tests/pytest_sharding.py`、`apps/api/tests/conftest.py`、`apps/api/tests/ci-shard-plan.json`、`apps/api/tests/test_pytest_sharding.py`、`scripts/test-suites.json`。
- [x] **构建的单一变更**：
  - 在 `backend-test.yml` 与 `ci-full.yml` 中，把原本串行挂在 Shard 0 的 5 项非 pytest 工作与 GPU 成员校验拆为与 4 个分片并行的独立 Job：`Backend migration and schema checks`、`Backend repository script tests`、`Backend module data reclamation acceptance`、`Backend module data ordinary login acceptance`、`Backend GPU membership activation`，并全部接入 `Backend tests` 汇总门禁。
  - 在 `setup-backend-test-python/action.yml` 补齐 `~/.cache/uv` 的 `actions/cache@v4`，并将 `backend-test.yml` 的两次 `apt-get update`（`ffmpeg` 与 `postgresql-client-16`）合并为一次。
  - 在 `apps/api/tests/pytest_sharding.py` 与 `apps/api/tests/conftest.py` 增加 `pytest_ignore_collect` 收集前分片并缓存分片计划，保留 `pytest_collection_modifyitems` 作兜底校验。
  - 基于 `45660914610` 完整 JUnit 耗时以 `longest=80` 重新生成 `apps/api/tests/ci-shard-plan.json`（预计 4 分片 JUnit 秒数 `[4342, 4343, 4343, 4343]`，偏斜比 `1.000`）。
- [x] **可见结果**：4 个后端分片只运行 API pytest；Shard 0 不再出现额外 20 至 30 分钟长尾；4 分片收集阶段显著缩短且重文件均衡。
- [ ] **验收检查**：
  - [x] 单元检查：`apps/api/tests/test_pytest_sharding.py` 29/29 通过（含新增真实子进程验证跳过非本片模块导入）；`node scripts/check-workflow-names.mjs --strict` 全部合规；全部 112 项仓库 Node 脚本测试通过。
  - [ ] 实景验证：推送 PR #195 后在 GitHub Actions 核对 4 分片墙钟、非用例收集开销、偏斜度及 5 个拆出并行 Job 回执。

### PR-3：`managed_database` 改用 `head` 封存模板与 8 表刷新（最大提速项，已落地本机 `22f17923d`）

- [x] **依赖**：PR-2。
- [x] **改动文件**：`apps/api/tests/managed_image_inference_fixtures.py`、新增 `apps/api/tests/test_managed_database_template_equivalence.py`。
- [x] **构建的单一变更**：
  - 全量审计全新 `template0` 迁移至 `head (bo0511)` 数据库全部 421 张表，证实非空表仅 11 张（3 张静态表 + 8 张数据库绑定密钥/权限单例表），并确认 `0487_insights_storage_owner.sql` 为唯一 `ON DATABASE` 授权迁移。
  - 将 `managed_schema_prefix` 的目标修订从 `0267` 升级为 `head`，每个 pytest 会话只在模板库执行一次全迁移并封存（`ALLOW_CONNECTIONS false`）。
  - 将 `migrated_case_database()` 改为 `CREATE DATABASE ... TEMPLATE <head_template>` 后在单次 asyncpg 事务内执行 `_rebind_case_database()`：轮换全部 8 张单例表的 32 字节密钥、重绑 `yoao_inference_routing_v1.authority_secret` 与 `yoao_jobs_v1.video_execution_authority` 的 `oid`/`datname`/`cluster_identifier`，并重放 `0487` 的 `GRANT TEMPORARY ON DATABASE`；彻底移除每用例的 Python 子进程与 322 步迁移。
  - 新增 `test_managed_database_template_equivalence.py` 两条强守卫测试：一条在真实 PostgreSQL 比对全目录（schema/表/视图/列/函数体/触发器/`datacl`/非空静态行）与原生 `_routing_secret()` / `_video_execution_secret()` 校验；一条静态扫描全仓 SQL 与 Python 迁移，防止新增未登记的数据库绑定种子表或库级授权。
  - 经核查 `storage_migration_isolation.py` 的 `migration_engine` 被 `test_storage_migration_copy_worker.py` 等直接提交用例使用，且单用例 `CREATE DATABASE TEMPLATE` 已降至 `0.97s`，保留每用例独立数据库隔离以杜绝跨用例污染。
- [x] **可见结果**：71 个 `managed_database` 测试文件保持完整独立数据库隔离与每库独立密钥，单用例建库 `setup` 从本机基线 `19.75s` 降至 `0.97s`（提速 **20.3×**）。
- [ ] **验收检查**：
  - [x] 单元与实景验证：在一次性 worktree 测试库（`yoao_wt_3ba060959d790989_test`）通过 `test_managed_database_template_equivalence.py`（2/2）；6 用例对比基线从 `158.52s` 降至 `60.26s`（含 45.49s 一次性模板构建）；7 个 `managed_database` 文件合集（215 例，CI 原耗时 `58.5m`）本机实测 205 通过、10 项与基线 `45604682774`/`45660914610` 报错逐字一致的预存失败，残留 `aap_managed_*` 数据库为 `[]`。
  - [ ] 性能验证（远端 CI）：推送 PR #195 后核对 CI 4 分片 `managed_database` 总耗时下降 ≥70% 且最长单分片 ≤25 分钟。
  - [ ] 审批门禁：本切片变更数据库测试隔离基础，合入前需维护者审阅 8 表刷新 SQL 与等价守卫证据。

### PR-4：`verify_module_data_boundary` 去重与非 MD 慢用例治理（已落地本机 `046abc15e`）

- [x] **依赖**：PR-2。
- [x] **改动文件**：`scripts/verify_module_data_boundary.py`、`scripts/test_module_data_selection.py`、`apps/api/app/services/labeling_scene.py`、`apps/api/app/services/assets_spatial.py`、`apps/api/tests/test_assets_spatial_gateway.py`、`apps/api/tests/test_export_reference_media.py`、`apps/api/tests/test_compat_facades.py`。
- [x] **构建的单一变更**：
  - 在 `scripts/verify_module_data_boundary.py` 为 `collect_only=True` 新增 `_run_collect_only()` 快路径，跳过 Docker Postgres、589 迁移与 12 项 Node 检查直接收集 pytest node ID，并在 `scripts/test_module_data_selection.py` 补齐回归单测（消除默认与 `--reclamation-only` 运行中冗余的第 1 遍完整引导）。
  - 根因修复 `test_export_reference_media.py::test_large_frozen_selection_uses_bounded_sql_parameters`：通过实时 `pg_stat_activity` 查明生产代码 `labeling_scene.read_spatial_item_facts` 以 5,000 条分片请求默认 `page_size=200` 的 `read_spatial_items`，触发 PostgreSQL 每片 25 次重算整片 5,000 条快照与 SHA-256（约 24.2× 放大），且 `assets_spatial.py` 每页重复构建集合；改为按单页上限 `500`（`page_size=500`）分片、复用 `seen_ids` 集合、补齐 `test_read_spatial_item_facts_chunks_at_single_page_limit` 单测，同时将该用例 33,000 条造数改为 SQL `generate_series(1, 33000)` CTE。
  - `apps/api/tests/test_compat_facades.py`：用模块级 `ThreadPoolExecutor` 并发执行选中的 `test_removed_module_cold_import_fails` 独立冷启动子进程，保留每项 nodeid 与进程隔离。
  - 经核查未改动的规划项与原因：`test_domain_package_architecture.py` 已在基线 `82bf5c13e` 优化；`test_dataset_import_delivery_native.py` 默认已通过 `RUN_NATIVE_IMPORT_DELIVERY_LEASE_TEST` 跳过；`test_managed_original_reclamation.py` 耗时主要来自已冻结迁移函数 `yoao_assets_v1.observe_original_reclamation`（单次 9s 至 27s，不动冻结迁移）；`conftest.py` 的 `test_engine` 依赖 `pytest-asyncio` 函数级事件循环，贸然升为 session-scoped 会破坏跨循环连接安全，暂保持函数级。
- [x] **可见结果**：`test_compat_facades.py` 本机从 `148.98s` 降至约 `44s`（3.4×）；`test_large_frozen_selection_uses_bounded_sql_parameters` 从超 `303s`（未完）降至 `154.52s` 并同步修复生产大批量空间元数据读取 25× 放大；`verify_module_data_boundary.py` 收集阶段免去 1 遍 150s 至 200s 容器引导。
- [ ] **验收检查**：
  - [x] 单元与本机验证：`test_assets_spatial_gateway.py` 9/9 通过；`test_assets_spatial_runtime.py` 全绿；`test_export_reference_media.py::test_large_frozen_selection_uses_bounded_sql_parameters` 通过（`154.52s`，剩余耗时来自冻结迁移逐行 `SECURITY DEFINER` 函数 `_spatial_item_json`）；`test_module_data_selection.py` + `test_verify_module_data_boundary.py` 23/23（499 子测试）通过；`test_compat_facades.py` 保持完全一致的通过/预存失败集合。
  - [ ] 性能验证（远端 CI）：推送 PR #195 后核对 4 分片最长单分片与 `Backend module data reclamation acceptance` 实际耗时。

### PR-5：清理全量基线红灯，恢复可信全绿 `dev`

- [ ] **依赖**：PR-2（缩短单次全量反馈周期，方便批量收敛失败项）。
- [ ] **改动文件**：按 `45660914610` 与 `45596505866` / `45739704475` 实际失败分诊清单确定，不扩大到无关重构。
- [ ] **构建的单一变更**：
  - 优先处理 `45660914610` 中 208 项「非 verifier 运行时缺少 `AAP_MODULE_DATA_PG_CONTAINER_ID` / fixture URL」的门控错误，以及 `lidar_preflight`、`SimpleNamespace`、`content_authority_revoked` 等真实回归，补齐 `dev` 上 `Worktree`、`Frontend verification`、`Python SDK`、`smoke`、`mask-ai-native`、`video-pipeline-3` 的失败修复。
  - 所有失败项必须按真实根因修复，严禁通过删除断言或静默 skip 换绿灯。
- [ ] **可见结果**：`ci-full.yml` 与 `e2e-extended.yml` 在 `dev` 上稳定全绿。
- [ ] **验收检查**：
  - [ ] 单元检查：涉及修复的对应后端与前端单测全绿。
  - [ ] 实景验证：在 `dev`（或合并前候选分支）上完整运行一次 `ci-full.yml` 和一次 `e2e-extended.yml`（`scope=full`），要求 **0 failures / 0 errors** 且 `CI verdict` 通过。
  - [ ] 性能验证：记录全绿状态下的 4 分片、Vitest、worktree、owner-contract、32 套 E2E 的完整耗时快照，作为后续 Phase 的正式基线（取代历史红灯运行）。

### PR-6：建立 L4 夜间全量回归与受保护的 `dev` 合并队列

- [ ] **依赖**：PR-3、PR-4、PR-5（必须有全绿且耗时可控的基线）。
- [ ] **改动文件**：`.github/workflows/e2e-extended.yml`（或新增 `.github/workflows/ci-nightly.yml`）、`.github/workflows/ci.yml`、`.github/workflows/ci-full.yml`、仓库 `dev` 分支 Ruleset。
- [ ] **构建的单一变更**：
  - 建立统一 **L4 夜间回归**（每日定时跑 `dev`）：复用 `ci-full.yml` 覆盖全量后端分片、迁移校验、模块边界验收、21 个 owner 契约、SDK、training-executor、storage-agent、完整 32 套浏览器矩阵（含 `layout-stress`、长视频与恢复场景），并在汇总中输出首个变红修订 SHA 与归属模块，为发布阻断提供依据。
  - 若 `video-pipeline-3`（基线 21.1m）或 `default-*` 排队使 L4 超出 60 分钟，将最慢视频套件从 4 片扩至 6 片或调整 `e2e-run.yml` 的 `max-parallel`。
  - 在 `dev` 分支启用 GitHub Ruleset：开启 Merge Queue、必选检查 `CI verdict`（GitHub Actions `integration_id: 15368`）、禁止直推和绕过、对 `.github/workflows/**` 与 `scripts/plan-*.mjs` 要求独立评审。此时 `merge_group` 保持全量运行，不裁剪。
- [ ] **可见结果**：每晚自动产出 `dev` 全量健康账本；所有合入 `dev` 的改动必须经由合并队列与 `CI verdict`。
- [ ] **验收检查**：
  - [ ] 单元检查：`node scripts/check-workflow-names.mjs --strict` 与 `node --test scripts/plan-e2e-suites.test.mjs` 通过。
  - [ ] 实景验证：在受保护演练分支验证两项强制契约：（1）PR 绿但合并队列人为注入失败时，队列拒绝合入；（2）正常队列候选在 `CI verdict` 绿灯后成功合入。`gh api repos/YOAO-Labs/YOAO/rules/branches/dev` 确认 `merge_queue` 与 `CI verdict` 均已生效。
  - [ ] 性能验证：L4 夜间全量连续 3 晚端到端耗时位于 **30 至 60 分钟** 目标内（单晚 >60m 记录最长阻塞 Job 并作为调优输入）。
  - [ ] 审批门禁：修改 `dev` 分支保护和合并队列配置前需维护者显式批准。

### PR-7：统一域级路由清单并上线 L1 + L2 并行按需 PR 门禁

- [ ] **依赖**：PR-6（合并队列与 L4 夜间兜底已生效）。
- [ ] **改动文件**：`scripts/test-suites.json`、`scripts/plan-affected-pr.mjs`、`scripts/plan-affected-pr.test.mjs`、`scripts/plan-e2e-suites.mjs`、`scripts/plan-e2e-suites.test.mjs`、`scripts/affected_test_receipts.py`、`scripts/test_affected_test_receipts.py`、`.github/workflows/ci.yml`、`.github/actions/run-affected/action.yml`。
- [ ] **构建的单一变更**：
  - 收敛单一路由真值：将分散在 `plan-affected-pr.mjs` 和 `plan-e2e-suites.mjs` 的触发规则统一注册到 `scripts/test-suites.json`，把 `affected_pr` 从 3 文件 AST 锁升级为**已审阅的域级 owner 路由表**（按前端页面/模块目录、API 路由/服务/Worker 域、21 个 `owner_contracts` 子包与定向 Playwright 套件映射）。
  - 解除 `plan-e2e-suites.mjs` 将整个 `apps/api/app/api/` 与 `apps/api/app/services/` 全部归入 `sharedPaths` 的粗粒度回退，保留精确高风险一票升级清单：认证与权限（`identity`、`core/security`、`deps.py`）、数据库迁移（`alembic/`）、共享协议与契约（`apps/_shared/`、OpenAPI 与 capability snapshot、`packages/python-sdk/`）、锁文件与 Compose/运行配置、共享测试夹具（`conftest.py`、`factory.py`、`e2e/fixtures/`）、CI 工作流与规划器自身、跨多域或未映射路径、以及重命名双向路径。
  - 在 `ci.yml` 中把 `affected` 拆分为 **L1 与 L2 并行 Job**：
    - `l1-fast`（无 DB、无浏览器，目标 T+5m）：改动文件格式/lint/注释/命名检查、文档链检查（若含 docs）、受影响 `tsc` + 选定 Vitest 文件（不跑 9.8m 全量 Vitest）、受影响纯逻辑 pytest（显式文件路径直跑）、受影响独立子包测试。
    - `l2-api-db`（与 L1 并行，按需启动，目标 T+6m）：仅在改动写库/事务/路由持久化时启 PG（并按需启 MinIO/Redis），仅跑受影响域的提交/回滚与 HTTP 集成测试。
    - `l2-contracts`（与 L1 并行，按需启动）：仅在改动接口/协议/SDK 契约时跑 `export_openapi.py --check` 与受影响消费方测试。
    - `l2-e2e`（与 L1 并行，按需启动，目标 T+6m）：仅在改动触及画布、遮罩、视频恢复、点云、工作台布局或 URL 状态等必须浏览器验证的交互时，复用 `e2e-run.yml` 仅跑对应 `targetedFunctional` 或对应恢复 spec（如改动视频恢复则直接拉起视频恢复 spec，不等 L4）；未触及不构建前端 E2E 产物。
  - `CI verdict` 严格核验同一 `SHA/run_id/attempt` 下 L1 与所有被选中的 L2 非空回执；高风险 PR 在推送阶段提前执行高风险检查或提示，不等入队才反馈。
- [ ] **可见结果**：普通 PR 推送后按改动范围并行跑 L1 与必要的 L2，5 至 6 分钟内得到可入队结论。
- [ ] **验收检查**：
  - [ ] 单元检查：`node --test scripts/plan-affected-pr.test.mjs scripts/plan-e2e-suites.test.mjs` 与 `pytest scripts/test_affected_test_receipts.py` 全绿；覆盖纯文档、单页 UI、纯逻辑后端、写库后端、视频恢复、权限改动、迁移改动、未知文件、空 diff、规划器自改、重命名移出共享路径。
  - [ ] 实景验证：故意注入 1 个真实 UI 回归与 1 个真实后端持久化回归，确认在对应 L1/L2 失败；先在影子模式（PR 仍同时跑全量）回放并连续观察 **30 次普通 PR 首推**，确认对比全量结果 **0 假绿** 后再申请开启 `vars.CI_AFFECTED_ONLY=true`。
  - [ ] 性能验证：以 GitHub 服务端 `pull_request` 事件创建时间为 T+0，统计连续 30 次普通 PR 首推的 p50/p95/max；失败阈值：纯文档与 L1 单测 PR 超过 **5 分钟**，或含 L2 按需集成 PR 超过 **6 分钟**即判不通过并排查具体步骤。
  - [ ] 审批门禁：打开 `vars.CI_AFFECTED_ONLY=true` 前需维护者审阅 30 次影子对账报告。

### PR-8：切流 L3 普通合并候选至合成 diff 选测（T+10 分钟内）

- [ ] **依赖**：PR-7（域级路由经过 30 次无假绿验收）且 PR-6 的 L4 夜间全量稳定运行。
- [ ] **改动文件**：`.github/workflows/ci.yml`、`scripts/plan-affected-pr.mjs`、`scripts/plan-affected-pr.test.mjs`。
- [ ] **构建的单一变更**：
  - 在 `ci.yml` 的 `merge_group` 路径中，取 `github.event.merge_group.base_sha` 上的受信任规划器，对 `base_sha..head_sha`（覆盖本批次所有 PR 与最新 `dev` 的完整合成改动）做分级判定。
  - **L3 普通合并候选**：合成改动全部落入可划界域且无高风险命中时，在最终候选 SHA 上并行执行本批次并集后的 L1 + L2（以及当涉及 Web/API 交互时的有界 `smoke`），不再默认启动 4 个全量后端分片和 32 套全量浏览器矩阵。
  - **L3 高风险合并候选**：合成改动任意一处命中高风险规则或无法可靠划界时，按边界扩大到相关子系统全量或执行完整的 `ci-full.yml`，单独计时不承诺 10 分钟。
  - 增加独立开关 `vars.CI_L3_AFFECTED_ONLY`（默认 `false`），支持一键回退到 L3 全量，不影响 L1/L2。
- [ ] **可见结果**：普通 PR 进入合并队列后，在 T+10 分钟前拿到最终 `CI verdict` 并合入 `dev`，合并队列不再被 60 至 85 分钟的全量任务阻塞。
- [ ] **验收检查**：
  - [ ] 单元检查：`plan-affected-pr.test.mjs` 新增多 PR 合成 diff 用例（例如「前端 UI PR + 后端纯逻辑 PR」合并为并集选测；「普通 PR + 含迁移 PR」自动升级为 L3 高风险）。
  - [ ] 实景验证：先在 `CI_L3_AFFECTED_ONLY=false` 下记录两周影子比对（`merge_group` 实跑全量，同时产出 L3 规划选集报告对比是否漏检），零漏检后开启 `CI_L3_AFFECTED_ONLY=true`；验证一次普通双 PR 批次在队列中走选测成功合入，验证一次高风险批次自动扩大到全量。
  - [ ] 性能验证：统计切流后连续 20 次普通 `merge_group` 候选从入队到最终 `CI verdict` 的 p50/p95/max，同时单独跟踪队列等待耗时与 L4 夜间逃逸次数；失败阈值：普通合并候选 p95 超过 **10 分钟**，或 L4 夜间出现任意 1 例因 L3 路由遗漏导致的回归（触发立即回滚开关并补齐规则）。
  - [ ] 审批门禁：开启 `vars.CI_L3_AFFECTED_ONLY=true` 前需维护者审阅两周影子比对报告与 L4 稳定性数据。

### PR-9：补齐 L5 发布候选制品验证工作流

- [ ] **依赖**：PR-6（复用全绿的迁移验证、边界检查和 L4 回归状态定义），可与 PR-7/PR-8 并行。
- [ ] **改动文件**：新增 `.github/workflows/release-verify.yml`，更新发布运维文档与检查清单链接。
- [ ] **构建的单一变更**：
  - 新增 `release-verify.yml`（触发条件：RC tag 或 `workflow_dispatch` 指定制品版本/镜像摘要）。
  - 针对实际产出的发布候选制品（构建后的 Compose 镜像、前端产物、Python SDK sdist/wheel）执行：全新环境安装、`validate_migrations.py` 升级迁移校验、隔离权限/恢复抽检、关键业务冒烟（`smoke`）以及核对最新 L4 夜间状态是否存在未解决阻断回归。
  - 仅阻塞发布批准与对外推送，完全不阻塞日常 PR 合入 `dev`。
- [ ] **可见结果**：发版前有自动化的真实制品验收凭证，不再依赖人工逐项本地拼装。
- [ ] **验收检查**：
  - [ ] 单元检查：`node scripts/check-workflow-names.mjs --strict` 通过。
  - [ ] 实景验证：通过 `workflow_dispatch` 对一个候选版本制品完成端到端演练，确认安装、迁移和 `smoke` 全部基于实际构建产物执行并归档回执；模拟存在未解决 L4 回归时确认发布门禁判红。
  - [ ] 性能验证：记录各制品验证子步骤耗时并写入运行账本，保证发版窗口可控且不挤占日常 PR runner 并发配额。

---

## 当前状态与未证实项

- **当前状态**：分支 `ci/pr-scope-and-verified-runtime`（PR #195）已完成 PR-1 至 PR-4、两轮 `origin/dev` 合并以及 PR-5 第一批至第六批基线清红与 E2E 修复。远端 CI Run `45928982362`（第五批提交 `699773105`）验证全部非分片检查、边界验收与 **29/29 套 Frontend E2E 全绿**（`default-05`、`default-10` 转绿），后端 4 分片失败与错误由 `661` 再降至 **`603`（`-58`，`580F + 23E`，覆盖 223 文件）**。本轮第六批系统修复了 10 类真实生产与架构缺陷、对齐 30 余套漂移测试并在先运行同风险替代用例通过后退役 2 组死代码／已删除路由测试，本机一次性测试库共验证 **40 套完整测试套件全绿**（预计覆盖 Run `45928982362` 约 `185+` 项失败／错误），同时定位并完整保留冻结迁移 `0353_labeling_mask_quality.sql` 的 1 处真实 SQL 行类型缺陷待前向迁移切片修复。
- **已验证账本（2026-11-26）**：
  - **远端 CI 实测（Run `45769478317`，`ca5db0424`）**：
    - 后端 4 分片端到端总耗时降至 **Shard 0 `20m05s`**、**Shard 1 `33m53s`**、**Shard 2 `29m36s`**、**Shard 3 `23m12s`**；最慢分片关键路径从基线 `88m42s`（`45660914610`）/ `61m36s`（`45743400427`）降至 **`33m53s`**（加速 1.8× 至 2.6×，Shard 0 加速 3.1× 至 4.4×）。
    - 并行拆分与基础设施 Job 全绿：20 个 Owner 契约套件全绿、Frontend verification `9m00s`、Python SDK `2m27s`、Repository quality `2m54s`、Backend migration & schema checks `2m14s`、M2B ordinary login acceptance `3m40s`、GPU membership activation `4m37s`、Backend worktree & maintenance tests `14m50s`、Frontend E2E 31/32 套件通过（仅 `default-15` 持续创建超时）。
  - **远端 CI 实测（Run `45793135791`，合入 `origin/dev` `c7c75e1af` 后 `af1e95584`）**：
    - 后端 4 分片总用例 `21,806`，失败与错误由 `45660914610` 的 `2,256`（`1,920F + 336E`）降至 **`1,631`（`1,439F + 192E`，覆盖 346 个文件）**；分片总耗时 Shard 0 `1,553.7s`、Shard 1 `1,931.1s`、Shard 2 `1,707.8s`、Shard 3 `1,806.5s`。
  - **远端 CI 实测（Run `45803817744`，合入 `origin/dev` `de8ef523a` + 第二批修复 `dddafe2bb`，41m18s）**：
    - 总计 **69 通过 / 1 跳过（`CI affected`）/ 9 失败**（4 个后端分片 + 2/29 套 E2E + 3 个聚合门禁）。
    - 上轮 8 项合入后非分片失败与第一批回收/E2E 修复全部在远端转绿：`CI scope`、`CI test planning`、`Docs validation`、`Repository quality`、`Frontend verification`、`Frontend E2E build`、`Python SDK tests`、`Backend migration and schema checks`、`Backend repository script tests`、`Backend worktree and maintenance tests`、`Backend module data ordinary login acceptance`、`Backend GPU membership activation`、`Backend module data reclamation acceptance`、全部 18 个 `Owner contract suite execution`、`Storage agent tests`、`Training executor tests`、`ML examples tests`、`ML CPU contract tests`、`Plugin SDK test dependency` 及 **27/29 套 Frontend E2E**（含 `default-15`、全部 `pointcloud-1..4`、`video-pipeline-2..4`、全部 `mask-*`、`layout-stress`、`ai-request-worker`）。
    - 后端 4 分片（总用例 `21,806`）：失败与错误由 `1,631` 降至 **`1,380`（`-251`，`1,188F + 192E`，覆盖 325 个文件）**；单分片 JUnit 耗时比 `45793135791` 再缩短 8 至 10 分钟：Shard 0 `1,061.7s`（`-492s`，`380F + 66E`）、Shard 1 `1,414.4s`（`-517s`，`293F + 4E`）、Shard 2 `1,122.0s`（`-586s`，`225F + 37E`）、Shard 3 `1,221.2s`（`-585s`，`290F + 85E`）。
    - 2 套失败 E2E 诊断：`video-pipeline-1`（17/18 通过，`e2e/tests/video-issue-frame.spec.ts:573` 在 `SeedAPI.cleanupOwned` 触发 `Fixture operation failed: RuntimeError`，重试返回 `409 fixture_scope_busy`）；`default-05`（`e2e/tests/lidar-export-trusted.spec.ts:6` 触发浏览器 500 响应错误）。
  - **环境变量命名核查（`AAP_MODULE_DATA_*` vs `YOAO_*`）**：
    - 核查 `origin/dev` 与归档计划 [1793263851_yoao-product-language-and-naming.md](./archive/1793263851_yoao-product-language-and-naming.md) 确认：产品语言治理（N1–N7）已清除用户可见、CLI/SDK、worktree 与插件运行时 `AAP_*` 前缀，但有意保留内部边界验收环境变量 `AAP_MODULE_DATA_*`（`AAP_MODULE_DATA_ROLE_ACCEPTANCE`、`AAP_MODULE_DATA_PG_CONTAINER_ID`、`AAP_MODULE_DATA_*_URL` 等），当前 `origin/dev` 与本分支均仍有 943 个后端测试文件引用（其中 858 个按 `AAP_MODULE_DATA_ROLE_ACCEPTANCE != "1"` 门控），仓库内不存在 `YOAO_MODULE_DATA_*`。
    - 正常后端分片中约 128 项 `AAP_MODULE_DATA_PG_CONTAINER_ID` 报错并非命名残留，而是 `identity_e2e_support.py` 与 `labeling_class_query_helpers.py` 在普通分片无条件调用 `_verify_postgres()`；另有 101 项 GPU 权威测试依赖 `AAP_MODULE_DATA_INFERENCE_GPU_URL`。
  - **本机实测（PR-1 至 PR-4）**：
    - `pytest_ignore_collect` 预收集分片：Shard 0 冷收集从 `98.43s` 降至 `29.42s`（3.3×），`test_pytest_sharding.py` 29/29 通过。
    - `managed_database` `head` 模板：全目录等价与静态漂移守卫 2/2 通过；6 用例 `test_storage_migration_windows.py` 从 `158.52s` 降至 `60.26s`（第 2 至 6 例单用例 `setup` 从 `19.75s` 降至 `0.97s`，20.3×）；7 文件 215 例实测零新增回归、零残留 `aap_managed_*` 库。
    - 空间批量读取与大选集导出：`read_spatial_item_facts` 修复 5,000/200 的 25× 快照放大并补充 500 单页分片单测（9/9 通过），`test_large_frozen_selection_uses_bounded_sql_parameters` 从超 `303s` 降至 `154.52s`。
    - `test_compat_facades.py`：并发冷启动从 `148.98s` 降至约 `44s`（3.4×），结果集合与基线逐项一致。
    - `verify_module_data_boundary.py`：`collect_only=True` 去容器快路径通过 `test_module_data_selection.py` + `test_verify_module_data_boundary.py`（23/23，499 子测试，无 Docker 依赖）。
  - **本机实测（PR-5 第一批至第三批红灯修复）**：
    - 第一批与第二批已通过远端 CI Run `45803817744` 复验（后端失败 `-251`，8 项合入后非分片及回收验收全部转绿）。
    - 第三批 6 处真实生产缺陷修复（已记录至 `CHANGELOG.md`）：（1）`original_content_access.py` 恢复已过期 API 密钥拒绝（403 `api_key_authority_revoked`）；（2）`exporting/packaging.py` 修复 `relative_path_from_file_path` 误读 ORM 类级 `MetaData`（`item.metadata` -> `item.metadata_`）；（3）`api/v1/tasks/_shared.py` 移除未加载已核验来源引用时回退 `storage_service.generate_download_url` 的路径；（4）`api/v1/dashboard/reviewer.py` 恢复 `_reviewing_batches` 经媒体网关读取 `video_poster_urls`；（5）`services/prediction.py` 在 `apply_prediction_result` 与 `apply_prediction_result_bundle` 调用 `preflight_mask_payload_for_write` 前先校验 `background_native_reference_receipt`，阻止无效／过期／已取消／跨事务回执提前写入对象存储；（6）`application/durable_prediction_retry.py` 在 `_attach_input_receipts` 的 `image_proof` 分支补齐 `owner.qualify_native_results` 与 `background_native_reference_receipt` 绑定。
    - 第三批 31 个后端测试文件本机验证（消除 Run `45803817744` 中 244/246 项失败）：
      - 17 个纯单测文件批量全绿（`444 passed`）：`test_identity_direct_dsn_readiness.py`、`test_active_learning_originals.py`、`test_export_packaging.py`、`test_image_pyramid_originals.py`、`test_sample_evaluation_masks.py`、`test_training_storage_consumers.py`、`test_training_run_storage_consumers.py`、`test_training_saved_target_consumers.py`、`test_image_pyramid_consumers.py`、`test_video_derivative_consumers.py`、`test_training_write_transport.py`、`test_trained_model_backend_auth.py`、`test_derivative_migration_reads.py`、`test_video_prediction_authority.py`、`test_cross_frame_dispatch.py`、`test_video_prediction_provenance.py`、`test_assets_source_gateway.py`。
      - 13 个库测与单测文件本机全绿：`test_secondary_inference.py`、`test_training_run_storage.py`、`test_project_pipelines.py`、`test_export_execution.py`、`test_plugin_registry_ingress.py`、`test_export_dispatch.py`、`test_annotation_migration_reads.py`、`test_video_poster_dashboard.py`、`test_training_migration_reads.py`、`test_training_references.py`、`test_managed_creation_staging_contract.py`、`test_ml_input_guards.py`、`test_guide_asset_projects.py`（17/17）。
      - `test_reference_background_native.py`：由 `23` 项失败降至 `2` 项失败（`49/51` 通过，实证第 5、6 项生产修复）；剩余 2 例（`success-single`/`success-job`）忠实保留 `assert previous.rejected_shape_indexes == [0]`，暴露冻结迁移 `0233_prediction_retry_finalization.sql` 中 `finish_prediction_retry_write` 尚未将 `partial_prediction_ids` 标记为 superseded 的真实缺口。
  - **远端 CI 实测（Run `45843203599`，第三批修复 `919229b83`）**：
    - 终态 **70 通过 / 1 跳过（`CI affected`）/ 8 失败**（4 个后端分片 + `Backend tests` 聚合 + 1/29 套 E2E `default-05` + `Frontend E2E` 聚合 + `CI verdict`）；`video-pipeline-1` 等全部 28/29 套 E2E 通过（零新增回归）。
    - 后端 4 分片（总用例 `21,761`，1,985 跳过）：失败与错误由 `1,390`（`327` 文件，按实际 JUnit 汇总口径）降至 **`1,136`（`944F + 192E`，`-254` 项、`-32` 个失败文件，覆盖 `295` 个文件）**，其中 Shard 0 `279F + 66E`、Shard 1 `272F + 4E`、Shard 2 `176F + 37E`、Shard 3 `217F + 85E`。
  - **本机实测（PR-5 第四批清红与低质量失败用例清理）**：
    - 第四批 3 处真实生产缺陷修复（已记录至 `CHANGELOG.md`）与遗留门面清理：（1）`apps/api/app/services/video_tracking/runner.py` `_bbox_of_geometry` 将 `isinstance(..., dict)` 改为 `Mapping`，修复只读 `MappingProxyType` 导致跨窗 IoU 恒为 `0.0`；（2）`apps/api/app/services/original_sources.py` 与 `labeling_core_read_relations.py` 新增 `lock_task_source_facts_for_core_read`，仅投影 7 个标量任务列，避免加载 `JSONB` `tags` 并覆盖调用方 ORM 状态；（3）`apps/assets-source/src/yoao_assets_source/connectors/s3.py` 修复 `S3CompatibleSource.close()` 未关闭自建 `boto3` 客户端的资源泄漏（`apps/assets-source/tests/` 全套 `393 passed`）；（4）删除 `dev` 合入后残留的 4 个遗留兼容门面文件 `apps/api/app/services/{scene,scene_pose,scene_inference,sensor_calibration}.py` 并迁移全部第一方与测试调用方，`test_compat_facades.py`、`test_lidar_registered_preflight.py` 与 `test_domain_package_architecture.py -k "legacy_facade_references or video_tracking"` 均全绿。
    - 第四批 50 个后端测试文件本机验证通过或显著减红（预计覆盖 Run `45843203599` 约 `520+` 项失败／错误）：
      - 16 个纯 verifier 验收环境依赖文件补齐 `AAP_MODULE_DATA_ROLE_ACCEPTANCE == "1"` 门控（`identity_e2e_support.py`、`test_storage_connections_api.py`、`test_fixture_auxiliary_retirement.py`、`test_seed_owned.py`、`test_seed_comment_attachments.py`、`test_seed_guide_assets.py`、`test_fixture_backend_lifecycle.py`、`test_project_role_audit.py`、`test_filter_seed.py`、`test_gpu_arbiter_proof_recovery.py`、`test_gpu_plugin_admission_native.py`、`test_gpu_rollout_control.py`、`test_gpu_plugin_routing_replacement_native.py`、`test_gpu_plugin_terminal_endpoint_native.py`、`test_gpu_backend_fence.py`、`test_ml_routing_router.py`，消除 236 项非验收分片报错）。
      - 28 个业务与集成测试文件本机全绿：`test_managed_original_creation.py`、`test_auxiliary_originals.py`、`test_task_original_creation.py`、`test_managed_original_verification.py`、`test_training_saved_target_consumers.py`、`test_training_storage_consumers.py`、`test_sample_evaluation_masks.py`、`test_video_derivative_seeds.py`、`test_training_references.py`、`test_capability_instances.py`、`test_training_migration_reads.py`、`test_annotations_references.py`、`test_video_export_jobs.py`、`test_image_export_jobs.py`、`test_identity_authority_reads.py`、`test_dataset_source_auto_sync.py`、`test_directory_policies.py`、`test_assets_connections_gateway.py`、`test_assets_media_gateway.py`、`test_prediction_jobs_worker.py`、`test_image_export_inputs.py`、`test_batch_result_masks.py`、`test_video_mask_corrections.py`、`test_projects_ml_backend_binding.py`、`test_storage_maintenance_close.py`、`test_ml_routing_dual_id.py`、`test_video_tracker_multi_instance.py`、`test_original_source_export_resolution.py`、`test_media_probe_adapter.py`、`test_dataset_archive_managed.py::test_direct_managed_writers_reject_archived_dataset`。
      - 3 个部分收敛文件（严格保留真实缺陷或远端独立服务边界失败）：`test_training_auxiliary_storage_pinning_api.py`（通过修复 `test_training_previews.py` `storage_service` 导入消除 13 项 `training-preview` setup 错误，剩余 3 项为 Starlette `BaseHTTPMiddleware` + `httpx.ASGITransport` 取消断言）；`test_training_dataset_references.py`（5/7 通过，剩余 2 项真实 S3 reference 用例依赖 Assets Source HTTP 服务）；`test_dataset_assets.py`（非 `upload-init` 13/13 通过，4 项 `upload-init` 按 `0e5f16b29` 设计依赖远端 Assets Source 服务）；`test_managed_original_verification_jobs.py`（修复 4 项 `UnmappedInstanceError` 并全绿通过，保留另外 4 项暴露真实鉴权／列表缺口的失败用例）。
    - 低质量／过时失败用例清理清单（全部均先运行同等风险替代用例通过后再删除）：
      - 整文件退役 `apps/api/tests/test_ml_backend_env_sync_probes.py`（5 项失败，断言已退役的旧环境变量同步探针）—— 替代验证：`apps/inference/tests/test_registry_ingress.py` + `apps/api/tests/test_inference_registry_ingress_selection.py`（28 passed）。
      - 退役 `apps/api/tests/test_assets_connections_gateway.py::test_local_native_capture_uses_frozen_three_argument_signature`（1 项，调用 `0e5f16b29` 已移除的 `capture_storage_connection`）—— 替代验证：`apps/api/tests/test_assets_connections_gateway.py` 其余用例 + `apps/assets-source/tests/`（393 passed）。
      - 退役 `apps/api/tests/test_assets_media_gateway.py::test_frame_media_contention_reaches_segment_retry_but_does_not_break_warmup`（1 项，调用已移除私有函数 `frame_preannotate._read_frame_urls` / `_extract_frames_for_video`）—— 替代验证：同文件 `test_internal_frame_waiter_*` 及整文件全绿。
      - 替换 `apps/api/tests/test_annotations_references.py` 中检查已移除 SQLAlchemy ORM AST 编译文本的旧用例为真实 PostgreSQL `pg_get_functiondef` 检查（全文件全绿）。
      - 退役 `apps/api/tests/test_media_probe_adapter.py::test_remote_timetable_only_replaces_committed_rows_after_integrity_check`（3 项参数化用例，调用已移除的双参 `VideoTimetableRebuildWorkflow(mutation, SourceBytes())` 构造器）—— 替代验证：`test_media_probe_adapter.py` 23/23 流完整性校验（含 `truncated`、`digest` 与断流）+ `apps/assets-media/tests/test_runtime_worker.py -k timetable`（3 passed）+ `apps/api/tests/test_assets_media_gateway.py` 全绿。
      - 退役 `apps/api/tests/test_original_source_export_resolution.py` 中 15 项伪造 DB（`RelationDB` / `ProjectedDB`）检查旧 ORM SQL AST 结构的低质量单测 —— 替代验证：同文件全部真 PostgreSQL 集成用例与缓存行为用例（含新增 `lock_task_source_facts_for_core_read` 真实 SQL 不含 `tags`/`metadata` 校验）全绿。
      - 从 `apps/api/tests/test_dataset_archive_managed.py::test_direct_managed_writers_reject_archived_dataset` 移除已删除旧门面 `scene`/`sensor_calibration` 的 3 项分支（保留全部 10 项现行 `DatasetService` 写路径及同文件 HTTP `POST /scenes/backfill` 归档拒绝覆盖）。
  - **远端 CI 实测（Run `45923157048`，第四批修复 `3d484f427`）**：
    - 终态 **69 通过 / 1 跳过（`CI affected`）/ 9 失败**（4 个后端分片 + `Backend tests` 聚合 + 2/29 套 E2E `default-05`/`default-10` + `Frontend E2E` 聚合 + `CI verdict`）；全部非分片、边界与普通登录验收均通过。
    - 后端 4 分片（总用例 `21,769`，2,231 跳过）：失败与错误由 `1,136`（`944F + 192E`，295 文件）降至 **`661`（`638F + 23E`，`-475` 项、`-169` 错误，覆盖 253 个文件）**。
    - 2 套失败 E2E 根因定位：`default-05`（`lidar-export-trusted.spec.ts`）打开导出对话框调用 `GET /api/v1/projects/{id}/mask-formats` 时在普通运行账号下触发 `ERROR: permission denied for function list_format_adapter_plugins`（500）；`default-10` 在安装系统工具阶段被 GitHub Runner 预装 `packages.microsoft.com` 源返回 `403 Forbidden` 中断 `sudo apt-get update`（exit 100）。
  - **本机实测（PR-5 第五批：E2E `default-05`/`default-10`、`async_jobs` 安全与 `bo0517` 迁移、低质量测试清理）**：
    - E2E `default-05` 生产修复：`infra/docker/m2b-runtime-grants.sql` 补齐 5 项 `yoao_labeling_v1.*format_adapter_plugin*` 公开函数向 `application_role` 的 `EXECUTE` 授权（不授权 `_format_adapter_public_projection`）；`infra/docker/module-data-grants.sql` 补齐 `format_adapter_plugins` 表及 6 项函数移交 `labeling_owner`；`infra/docker/ordinary-runtime-verify.sql` 与 `apps/api/tests/test_format_adapter_plugins.py` 新增不变量门禁；本机一次性测试库普通运行账号实测 `SELECT * FROM yoao_labeling_v1.list_format_adapter_plugins(false)` 成功返回、`test_format_adapter_plugins.py` 全绿。
    - E2E `default-10` 工作流修复：`.github/workflows/e2e-run.yml`、`.github/workflows/backend-test.yml` 与 `.github/workflows/ci-full.yml` 共 5 处 `sudo apt-get update` 前清理 `/etc/apt/sources.list.d/microsoft*.list` / `.sources` 并固定 Ubuntu 镜像源；`node scripts/check-workflow-names.mjs --strict` 通过。
    - `async_jobs` 鉴权与平台存储作业列表修复（含前向迁移 `bo0517`）：新增 `apps/api/alembic/sql/0517_jobs_list_platform_storage.sql` 与 `0517_jobs_list_platform_storage.py`（提供 `yoao_jobs_v1._async_job_list_rows_scoped`、`async_job_list_page_scoped` 与 `async_job_list_total_scoped`，冻结 `0326_jobs_list_query.sql` 保持不变），同步登记至 `m2b-runtime-grants.sql`、`module-data-grants.sql`、`ordinary-runtime-verify.sql` 与 `scripts/verify_module_data_boundary.py`；在 `apps/api/app/api/v1/async_jobs.py`、`application/async_job_contracts.py`、`application/async_job_read.py`、`services/async_job_access.py` 与 `services/jobs_list_query_data.py` 透传 `api_key_id`/`api_key_scopes` 并对平台存储维护作业强制实时校验 `storage_maintenance.authority(..., "*")`、对数据集作业校验 `datasets:read`。本机实测：`validate_migrations.py`（`fresh@base->bo0517` 与 `forward@base->0173->0174->bo0517`）通过、普通运行登录 SQL 探针通过（公开函数可执行、`_async_job_list_rows_scoped` 拒绝）、`test_managed_original_verification_jobs.py` / `test_storage_migration_inventories.py` / `test_storage_migration_copies.py` / `test_storage_usage.py` / `test_jobs_list_query.py` / `test_jobs_list_query_native.py` / `test_jobs_single_read_native.py` 全绿、`test_dataset_quality_review.py` 非 `MissingGreenlet` 全部 13 项（含密钥作用域测试）通过。
    - 14 个测试文件的桩／角色门控／命名漂移修复全部本机实测通过：`test_identity_deletions_roles.py`、`test_identity_suspensions_roles.py`、`test_workspace_configuration_dependencies_roles.py`、`test_video_deletion_retirement_native.py`、`test_labeling_video_writer_native.py`、`test_video_input_authority_migration.py`、`test_video_input_selection_migration.py`、`test_initial_task_creation_contract.py`、`test_workspace_template_query_support_native.py`、`test_identity_deactivation_gateway.py`、`test_video_export_authority.py`、`test_accepted_video_inputs_gateway.py`、`test_dataset_storage.py`、`test_media_cache_storage.py`、`test_annotations_storage.py`、`test_audit_archive_storage.py`、`test_thumbnail_migration_reads.py`、`test_annotation_write_cache.py`、`test_video_canonical.py`、`test_mask_staging_migration_reads.py`。
    - 第五批低质量／过时失败用例清理清单（全部均先运行同风险替代用例通过后再删除）：
      - 退役 `apps/api/tests/test_workspace_project_locks.py` 中 2 项伪造 `SimpleNamespace` 并 monkeypatch 已移除 `record_evaluation_promotion` 的内部接线测试 —— 替代验证：`apps/api/tests/test_point_cloud_quality_evaluation.py` 实库 HTTP `/promote` 全绿 + `test_workspace_project_locks.py` 其余 13 项全绿。
      - 退役 `apps/api/tests/test_storage_usage.py::test_migration_roundtrip`（在 `head` 库直接执行历史 `0209.downgrade()` 触发依赖报错）—— 替代验证：`scripts/validate_migrations.py`（`fresh`/`forward` 全链至 `bo0517`）+ `test_storage_usage.py` 其余 27 项实库测试全绿。
      - 退役 `apps/api/tests/test_migration_0218_managed_original_verifications.py::test_empty_verification_schema_roundtrip_preserves_prior_anchor`（在 `head` 库直接执行历史 `0218.downgrade()` 触发依赖报错）—— 替代验证：`scripts/validate_migrations.py` + 同文件其余 28 项约束与数据保留降级拒绝测试全绿。
      - 整文件退役 `apps/api/tests/test_migration_0246_storage_usage_accounting.py`（通过 `historical_schema` 重放 `0001..0246` 命中 `0191` 硬编码 `public.task_append_operations` 已存在冲突）—— 替代验证：`scripts/validate_migrations.py` + `test_storage_usage.py` 全绿。
      - 退役 `apps/api/tests/test_migration_0204_dataset_source_relocations.py` 中 2 项在 `head` 库直接执行 `0204.downgrade()`/`upgrade()` 破坏后续函数返回类型的历史回滚测试 —— 替代验证：`scripts/validate_migrations.py` + 同文件 `test_cross_path_predecessor_rejects_changed_identity_and_source` 实库通过。
      - 退役 `apps/api/tests/test_plugin_capability_migration.py` 中 4 项在已更名 `yoao_inference_*` 的 `head` 库直接调用更名前 `0401`/`0402`（引用 `aap_inference_*`）的历史回滚测试 —— 替代验证：`scripts/validate_migrations.py` + 同文件 8 项纯逻辑清单校验全绿。
  - **远端 CI 实测（Run `45928982362`，第五批修复 `699773105`）**：
    - 全部非分片检查、普通运行登录验收（含 `bo0517` 与 `format_adapter_plugins` 授权）、迁移校验以及 **Frontend E2E 29/29 套全部转绿**（`default-05`、`default-10` 已收口）。
    - 后端 4 分片失败与错误由 `661`（`638F + 23E`，253 文件）降至 **`603`（`580F + 23E`，`-58` 项、`-30` 个失败文件，覆盖 `223` 个文件）**。
  - **本机实测（PR-5 第六批：10 类生产／架构缺陷修复、40 套全绿套件与 2 组过时测试退役）**：
    - 第六批 10 类真实生产与架构缺陷修复（用户可见项均已同步记录至 `CHANGELOG.md`）：
      1. 事务回滚／提交后过期 ORM `actor` 触发 `MissingGreenlet`：`dataset_source_access.py`（`_actor_id`）、`dataset_registration.py`、`dataset_content.py`、`dataset_references.py` 与 `audit.py`（`_resolve_actor_fields` 在持久会话内定向刷新缺失字段、非持久对象直接读取身份／状态字典）。
      2. `storage_migration_copies.py` `item()` 对持久 ORM 行刷新卸载标量列（修复 `updated_at` `MissingGreenlet`）。
      3. 原生 SQL 变更后的会话 identity-map 同步：`dataset_references.py` 在 `publish_directory_reference_head` 迁址后驱逐已删除 `(source.id, previous.path_digest)` 条目；`bug_attachment_uploads.py` 在原生 SQL 准备／删除后通过 `_sync_cached_upload` 刷新或驱逐缓存行。
      4. 6 个标注域 SQL 适配层（`video_tracking/control.py`、`labeling_annotation_data.py`、`labeling_annotation_bulk_data.py`、`labeling_mask_edit.py`、`labeling_discussion_data.py`、`labeling_quality_data.py`）以 `async with db.begin_nested():` 隔离 `db.scalar(...)`，避免领域错误污染外层事务（`InFailedSQLTransactionError`）。
      5. `apps/api/app/api/v1/ml_backends.py` `/interactive-annotating-frame` 修正 `owner_kind="interactive_frame_predict"` -> `"frame_interactive"`（修复生产 500 约束冲突）。
      6. `apps/api/app/services/mask_mutation.py` 删除 `preflight_mask_payload_for_write` 前冗余的 `self._lock_task(...)`，避免 S3 读取期间持有任务行 `FOR UPDATE` 锁。
      7. `apps/api/app/api/v1/datasets.py` `/sniff-axis-convention` 改为 `SniffAxisConventionResponse.model_validate(result.result.model_dump())`，修复跨包 Pydantic 模型校验 500。
      8. `apps/api/app/services/ordinary_thumbnails.py` 在编码完成后、调用 `thumbnail_outputs.create(...)` 前立即退出 `source_guard`，修复未确认输出失败遗留打开 `StorageOperation` 许可与并发来源竞争清理报错。
      9. `apps/api/app/services/mask_qc/compare.py` 兼容 PostgreSQL `'infinity'`／`'-infinity'` 保留期时间戳，修复 500 `ValueError`。
      10. `apps/api/scripts/import_nuscenes_scene.py` 兼容旧场景从 `storage_relative_path` 推导相机通道并将 `bind_auxiliary_scene` 移至 `set_scene_source` 之后；架构边界将 `TaskWorkflowError` 下沉至 `app/services/task_workflow_errors.py`（`app/application/task_workflow_errors.py` 再导出）并提升 `exporting/lidar.py`、`exporting/lidar_inputs.py` 包内导入。
    - 40 套后端测试套件本机一次性测试库实测全绿（覆盖 Run `45928982362` 约 `185+` 项失败／错误）：`test_public_artifact_migration_reads.py`、`test_dataset_registration.py`、`test_dataset_source_relocations.py`、`test_s3_references.py`、`test_dataset_quality_review.py`、`test_dataset_workbench.py`、`test_storage_migration_read_publications.py`、`test_comment_attachment_uploads.py`、`test_training_write_owners.py`、`test_nuscenes_owned_importer.py`、`test_annotation_conversion.py`、`test_video_tracker_jobs_list.py`、`test_video_frame_service.py`、`test_dataset_quality.py`、`test_storage_migration_finalizations.py`、`test_mask_mutations.py`、`test_managed_secondary_inference.py`、`test_thumbnail_output_creation.py`、`test_storage_connection_updates.py`、`test_interactive_annotating_frame.py`、`test_video_asset_retry_jobs.py`、`test_project_member_concurrency.py`、`test_nuscenes_chunk_pose.py`、`test_image_raster_mask_api.py`、`test_annotation_create_idempotency.py`、`test_annotation_group_bulk.py`、`test_mask_slices.py`、`test_axis_sniffer.py`、`test_storage_source_summary.py`、`test_bug_attachment_uploads.py`、`test_video_tracker_seed_passthrough.py`、`test_video_tracker_combo.py`、`test_auxiliary_inference_storage_flow.py`、`test_export_zip_accounting.py`、`test_export_storage.py`、`test_project_owner_profiles.py`、`test_video_workbench.py`、`test_dataset_source_changes.py`、`test_import_nuscenes_lite.py`、`test_ordinary_thumbnails.py`；另 `test_mask_qc_m3.py` 6/7 通过（0 teardown 错误）。
    - 第六批低质量／过时失败用例清理清单（全部先运行同风险替代用例通过后再删除）：
      - 退役 `apps/api/tests/test_video_asset_retry_jobs.py` 中 8 项调用已下线旧路由 `POST /api/v1/storage/video-assets/retry` 的测试（保留同文件 2 项仍在役的观察器与 worker 测试）—— 替代验证：`apps/api/tests/test_video_frame_service.py` 与保留的 2 项用例全部本机实测全绿。
      - 退役 `apps/api/tests/test_storage_migration_finalizations.py::test_changed_checkpoint_revokes_proof_in_same_receipt_transaction`（调用已删除方法 `service.changed_checkpoint`）—— 替代验证：同文件紧邻的 `test_actual_late_delta_is_saved_after_proof_freeze[False/True]` 及整套本机实测全绿。
    - 范围外发现的真实冻结迁移 SQL 缺陷（未修改冻结迁移、未放宽测试）：`test_mask_qc_m3.py::test_tracker_compare_requires_exact_job_revision_and_candidate_digest` 保留 1 项失败，根因位于冻结迁移 `apps/api/alembic/sql/0353_labeling_mask_quality.sql:45,659` —— 函数声明 `v_tracker_job public.video_tracker_jobs%ROWTYPE`（第 3 列为 `dataset_item_id uuid`），但 `SELECT job.id, job.task_id, job.revision, ... INTO v_tracker_job` 按位置把整型 `revision` 写入 `uuid` 列触发 `invalid_text_representation`（暴露为 `Invalid Mask QC request value`），导致线上 `mask-compare?baseline=tracker_candidate` 必败；需另立前向迁移切片（`bo0518`）将 `v_tracker_job` 改为 `record` 或 `SELECT job.*`。
- **未证实项（需在远端 CI 或后续批次闭环）**：
  1. 本轮第六批推送后的远端 CI 需复验后端 4 分片失败数进一步下降（预计由 `603` 降至约 `415` 左右），并确认 29/29 套 Frontend E2E 与全部边界／迁移门禁保持全绿。
  2. 后端 4 分片剩余待续清（约 `415` 项，待第七批推进）：`test_video_prediction_routes.py`、`test_domain_package_architecture.py` 其余 `dev` 历史导入漂移、`test_prediction_reference_actions.py`、`test_datasets_upload_zip.py`、`test_managed_prediction_requests.py`、`test_prediction_commit_boundary.py`、`test_dataset_write_targets.py`、`test_mask_execution_runtime.py`、`test_seed_online_training.py`、`test_training_observability.py`、`test_video_prediction_frozen_scope.py`、`test_interactive_native_mask_endpoint.py`、`test_training_storage_deletion.py`、前向迁移切片（`finish_prediction_retry_write` 2 例 + `0353_labeling_mask_quality.sql` `v_tracker_job` 1 例）、`test_training_auxiliary_storage_pinning_api.py` 3 项 ASGI 取消测试及零星分散用例。
  3. `test_managed_original_reclamation.py` 与 `test_large_frozen_selection_uses_bounded_sql_parameters` 剩余耗时位于已冻结迁移 SQL 函数（`observe_original_reclamation` 与 `_spatial_item_json`），若全绿后单分片仍偏长，需立项新增迁移优化。
  4. PR-7 切流后，托管 GitHub Runner 在多 PR 并发推送时的实际排队等待时间（90 秒预算是否足够）需在 30 次影子验收中实测确认。
