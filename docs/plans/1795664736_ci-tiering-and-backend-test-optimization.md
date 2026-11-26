# CI 分层改造（L0 至 L5）与后端测试分片优化计划

> 状态：待审批与启动执行。本计划基于 [1795575143_ten-minute-pr-ci.md](./1795575143_ten-minute-pr-ci.md)（PR #195 已落地的 3 文件试点与 `CI verdict` 聚合框架），将范围扩展为完整的 L0 至 L5 分层门禁，并同步治理当前导致后端分片运行 60 至 128 分钟的根因。

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

### PR-1：L0 本地提交减负（5 至 30 秒）

- [ ] **依赖**：无，从当前最新基线直接切分支。
- [ ] **改动文件**：`.pre-commit-config.yaml`、`scripts/install-git-hooks.sh`、`package.json`（仅在需要新增显式 `pre-push` 或本地完整检查脚本时）。
- [ ] **构建的单一变更**：
  - 将 `.pre-commit-config.yaml` 中的 `eslint` 改为只对暂存的 `apps/web` JS/TS 文件执行（去掉全仓扫描，或把 `check-tw-tokens` / `check-filter-ownership` 仅对暂存文件运行）。
  - 将 `tsc-web`、`regen-openapi-snapshot`（导入 `app.main` 需约 15s）与 `regen-capability-registry-snapshot` 从 `stages: [pre-commit]` 移至 `manual` 或 `pre-push`，保留 `trailing-whitespace`、`end-of-file-fixer`、`check-yaml`、`check-added-large-files`、`check-merge-conflict`、`ruff`、`prettier`、注释棘轮、命名检查与工作流命名检查。
  - 确保 OpenAPI 与 capability registry 漂移检查在 L1/L2 CI 仍为硬门禁，不遗漏快照同步。
- [ ] **可见结果**：开发者本地修改前端或后端文件后执行 `git commit`，不再触发全项目 `tsc` 或 15 秒 `app.main` 导入。
- [ ] **验收检查**：
  - [ ] 单元检查：`uv tool run --from pre-commit==4.6.1 pre-commit validate-config` 通过；`node scripts/check-workflow-names.mjs --strict` 通过。
  - [ ] 实景验证：在独立 worktree 中分别暂存 1 个 `.tsx` 文件与 1 个 `.py` 路由文件执行真实 `pre-commit run`，确认仅检查暂存文件且冲突标记、格式错误、新注释违规仍可准确拦截。
  - [ ] 性能验证：指标为单次 `pre-commit run` 墙钟耗时；探针为 `time pre-commit run` 连续 5 次（修改 1 个 `.tsx` 与 1 个 `.py`）；基线为当前全仓 eslint + tsc + OpenAPI 导入；失败阈值：中位数 >30 秒或最大值 >45 秒判失败。

### PR-2：后端 CI 止血（拆 Shard 0 附加项、收集前分片、缓存与重均衡）

- [ ] **依赖**：无，从当前最新基线直接切分支。
- [ ] **改动文件**：`.github/workflows/backend-test.yml`、`.github/workflows/ci-full.yml`、`.github/actions/setup-backend-test-python/action.yml`、`apps/api/tests/pytest_sharding.py`、`apps/api/tests/plan_shards.py`、`apps/api/tests/ci-shard-plan.json`、`apps/api/tests/test_pytest_sharding.py`。
- [ ] **构建的单一变更**：
  - 在 `backend-test.yml` 与 `ci-full.yml` 中，把原本串行挂在 Shard 0 的 5 项非 pytest 工作拆为与 4 个分片并行的独立 Job：`migration-validation`（含 `validate_migrations.py` 与 `test_alembic_migration_policy.py`，同时删除其余 3 分片的重复运行）、`module-data-reclamation`、`module-data-m2b`、`scripts-pytest`，把 `export_openapi.py --check` 移入轻量的质量/契约检查。
  - 在 `setup-backend-test-python/action.yml` 补齐 `~/.cache/uv` 的 `actions/cache`，并将 `backend-test.yml` 的两次 `apt-get update`（`ffmpeg` 与 `postgresql-client-16`）合并为一次。
  - 在 `apps/api/tests/pytest_sharding.py` 增加 `pytest_ignore_collect` 收集前分片（保留 `pytest_collection_modifyitems` 作兜底校验），并在未设置 `AAP_MODULE_DATA_ROLE_ACCEPTANCE=1` 时通过受审清单跳过 341 个整文件跳过的 verifier 专测。
  - 将 `plan_shards.py` 扩展为按历史完整 JUnit 耗时对前 80 或全量慢模块做 LPT 均衡，并生成新版 `ci-shard-plan.json`。
- [ ] **可见结果**：4 个后端分片只运行 API pytest；Shard 0 不再出现额外 20 至 30 分钟长尾；4 分片收集阶段显著缩短且重文件均衡。
- [ ] **验收检查**：
  - [ ] 单元检查：`pytest apps/api/tests/test_pytest_sharding.py` 全绿，覆盖 `pytest_ignore_collect` 与旧 `partition_items` 选集严格等价、跨 4 分片并集无遗漏无重复；`node scripts/check-workflow-names.mjs --strict` 通过。
  - [ ] 实景验证：在 GitHub Actions 触发完整后端测试，对账 4 分片 JUnit 合并后的有效执行用例集合与基线 `45660914610`（扣除已登记 verifier 整文件跳过后）逐项一致，且拆出的 4 个并行 Job 全部产出非空 JUnit/回执。
  - [ ] 性能验证：
    - 收集耗时：本机冷子进程 `--collect-only` 从基线 `98.43s` 降至 `≤35s`；远端分片（wall 减 JUnit 总和）从基线 `144s 至 199s` 降至 `≤75s`。
    - 分片偏斜度：4 分片 JUnit 最大值与最小值比值从 `45660914610` 的 `1.46×`（82.0m / 56.0m）降至 `≤1.15×`。不达标判失败。

### PR-3：`managed_database` 改用 `head` 封存模板与 8 表刷新（最大提速项）

- [ ] **依赖**：PR-2（先拿到干净且均衡的 4 分片耗时基线）。
- [ ] **改动文件**：`apps/api/tests/managed_image_inference_fixtures.py`、`apps/api/tests/storage_migration_isolation.py`、`apps/api/tests/conftest.py`、新增 `apps/api/tests/test_managed_database_template_equivalence.py`。
- [ ] **构建的单一变更**：
  - 将 `managed_schema_prefix` 的目标修订从 `0267` 升级为 `head`，每个 pytest 会话只在模板库执行一次全迁移并封存（`ALLOW_CONNECTIONS false`）。
  - 将 `migrated_case_database()` 改为 `CREATE DATABASE ... TEMPLATE <head_template>` 后在单次 asyncpg 事务内刷新已审计的 8 张单行密钥/OID 表（`public.mask_gc_write_permit_secrets`、`yoao_inference_backend_probe_v1.probe_secret`、`yoao_inference_health_observation_v1.observation_secret`、`yoao_inference_routing_v1.authority_secret`、`yoao_jobs_v1.video_execution_authority`、`yoao_inference_backend_endpoint_v1.endpoint_secret`、`yoao_assets_v1.storage_fault_observation_key`、`yoao_assets_v1.embedded_media_claim_key`），彻底移除每用例的 Python 子进程与 322 步迁移。
  - 新增两条强守卫测试：一条在真实 PostgreSQL 上比对「`head` 模板克隆+刷新」与「空库全量迁移至 `head`」的表/视图/函数/触发器目录摘要一致、且每库密钥与 OID 互不相同；一条静态扫描 `apps/api/alembic/sql/` 与 `versions/`，若有新迁移在顶层写入 `gen_random_bytes` 或 `current_database` 但未登记到刷新表则直接失败。
  - 在 `storage_migration_isolation.py` 中，将仅供 SAVEPOINT `db_session` 使用的 `migration_engine` 改为 `scope="module"`，免去回滚用例重复建库。
- [ ] **可见结果**：71 个 `managed_database` 测试文件（1,865 用例）保持完整独立数据库语义与每库独立密钥，但单用例建库开销从 10 至 20 秒降至百毫秒级。
- [ ] **验收检查**：
  - [ ] 单元检查：新增模板等价与迁移防漂移守卫测试通过；所有 71 个 `managed_database` 文件在一次性测试库下验证零新增失败、零残留数据库。
  - [ ] 实景验证：在独立 worktree 通过 `pnpm dev:worktree -- exec --mode test` 运行 `test_reference_image_inference.py`、`test_managed_image_inference.py`、`test_storage_migration_finalizations.py`、`test_prediction_input_owner.py` 与 `test_video_prediction_routes.py`，确认跨会话提交、密钥隔离和销毁校验全部生效。
  - [ ] 性能验证：指标为 CI 4 分片 `managed_database` 总 JUnit 耗时与最长单分片耗时；探针为真实 CI 完整运行对比 PR-2 基线；基线为 `45660914610` 的 `231.6m`（13,893s，单分片最长 85.8m）；失败阈值：`managed_database` 总耗时未下降 70% 以上，或最长单分片仍 >25 分钟即判失败。
  - [ ] 审批门禁：本 PR 变更数据库测试隔离基础，合入前需维护者审阅 8 表刷新 SQL 与等价守卫证据。

### PR-4：`verify_module_data_boundary` 去重、非 MD 慢用例治理与 `test_engine` 复用

- [ ] **依赖**：PR-2（可与 PR-3 并行开发，合入后叠加收益）。
- [ ] **改动文件**：`scripts/verify_module_data_boundary.py`、`apps/api/tests/conftest.py`、`apps/api/tests/test_export_reference_media.py`、`apps/api/tests/test_managed_original_reclamation.py`（必要时含 `apps/api/app/services/managed_original_reclamation.py`）、`apps/api/tests/test_compat_facades.py`、`apps/api/tests/test_domain_package_architecture.py`、`apps/api/tests/test_dataset_import_delivery_native.py` 及 2 秒以上固定 sleep 的测试文件。
- [ ] **构建的单一变更**：
  - 在 `scripts/verify_module_data_boundary.py` 中，让 `collect_only=True` 走纯本地 pytest 收集而不启动 Docker Postgres、不跑 589 迁移和 12 项 Node 检查；让 `--reclamation-only` 的 7 个 profile 复用单个已迁移并授权好的容器模板库（或拆为并行矩阵），消除 8 遍串行全引导。
  - 优化非 `managed_database` 头部慢用例（不删除任何行为断言）：
    - `test_export_reference_media.py::test_large_frozen_selection_uses_bounded_sql_parameters`（基线 235.0s）：改用单条 `INSERT INTO ... SELECT generate_series(1, 33000)` 造数，保留 33,001 条记录下的 SQL 参数上限分块真实查询验证。
    - 诊断并修复 `test_managed_original_reclamation.py`（基线 266.3s，单次 `planner.preview()` 耗时约 5 秒）的查询计划或索引根因。
    - `test_compat_facades.py`（基线 127.4s）：用并发线程池或按组批量子进程执行独立模块导入检查，保留冷进程无旧模块加载的断言语义。
    - `test_domain_package_architecture.py::test_repo_has_no_first_party_legacy_facade_references`（基线 41.6s）：改为单次遍历缓存解析结果或多进程并行 AST 扫描。
    - `test_dataset_import_delivery_native.py`（基线 33s `pg_sleep`）及 2 秒以上固定 `asyncio.sleep` 改为注入短测试 TTL，保留真实过期竞态验证。
  - 在 `apps/api/tests/conftest.py` 将 `test_engine` 升级为 `scope="session"`（配合 `pytest-asyncio` session loop scope），保留 `db_session` 每用例 SAVEPOINT 回滚隔离，省去 616 个文件上万次连接池建毁。
- [ ] **可见结果**：4 个常规后端分片（含非 MD 19,845 用例）进一步从每分片约 14.5m 压低，且拆出的 `module-data-reclamation` Job 从 15 至 25 分钟降到 5 分钟级。
- [ ] **验收检查**：
  - [ ] 单元检查：改动的所有测试文件全绿，总断言与收集用例数不少于基线。
  - [ ] 实景验证：在独立 worktree 运行 `verify_module_data_boundary.py --reclamation-only` 与 M2b 目标，核验 `cleanup.errors == []`、`retained_databases == []`、`junit_reconciled == true`；运行跨连接可见性测试（`test_worker_signals.py`、`test_discussion_notifications_commit.py`）确认 session-scoped `test_engine` 不破坏独立事务隔离。
  - [ ] 性能验证：
    - `test_large_frozen_selection_uses_bounded_sql_parameters` 从 `235.0s` 降至 `≤15s`。
    - `test_compat_facades.py` 从 `127.4s` 降至 `≤30s`。
    - `module-data-reclamation` CI Job 总时长 `≤6m`。
    - 结合 PR-3 后，4 个后端 pytest 分片最长单分片在 CI 降至 `≤12m`（若仍 >10m，按实测评估扩至 6 分片或为纯逻辑/SAVEPOINT 组加 `pytest-xdist`）。

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

- **当前状态**：已完成真实运行日志分析（`45604682774`、`45660914610`、`45743400427`、`45739704475`、`45596505866`、`45571975040`）、`dev` 分支保护核查与本机收集基准测量；尚未修改任何业务代码或工作流文件，等待确认后按 PR-1 / PR-2 启动。
- **未证实项（需在对应 PR 实跑闭环）**：
  1. `managed_database` 升至 `head` 模板后，在 GitHub Actions 托管磁盘 I/O 上 `CREATE DATABASE ... TEMPLATE` 的单次真实耗时尚未实测（本机未接入一次性 PG 压测 71 文件，需在 PR-3 实测确认是否需要叠加模块内复用）。
  2. `test_managed_original_reclamation.py` 单次 `planner.preview()` 耗时约 5 秒的精确 SQL 计划尚未 `EXPLAIN ANALYZE` 定位（归 PR-4 实测定位）。
  3. `test_engine` 升为 session-scoped 后是否存在个别测试隐式依赖每例新建连接池或关闭事件循环，需在 PR-4 全量运行中逐项验证。
  4. `45660914610` 中 1,920 项失败与 336 项错误的完整分诊尚未展开（归 PR-5 逐类归因），因此目前尚无「全绿 4 分片含 Shard 0 全套收尾步骤」的实测总时长。
  5. PR-7 切流后，托管 GitHub Runner 在多 PR 并发推送时的实际排队等待时间（90 秒预算是否足够）需在 30 次影子验收中实测确认。
