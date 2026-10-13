# 仓库优化 P10 验证通道：后端 / SDK / ML 协议契约本地验收

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P10 验证通道，非 canonical P10 完成）
> 分支：`worktree-agent-opt-final-backend`（自 P5 验收点 `9e34dfafb` 起本通道仅新增本文档提交）
> 测试时产品输入状态：`9e34dfafb`（P5 验收点，已包含 `0d044d4d` 的迁移脚本修复——`0d044d4d` 是 `9e34dfafb` 的祖先）；本通道零产品源码变更，仅新增本文档（见 §2 复用判定）
> 证据图例：**[V]** 本工作树实际执行；**[GAP]** 未执行/明确限制
> 边界：无产品源码变更；不 push；不做 P8/P9/台账编辑；不做前端/浏览器全量；未触碰 main 8000 / feedback 8100 / Grafana 3001 及其他 worktree 运行时。

## 1. 后端 pytest + coverage（CI 等价命令，自有 test 模式库）

命令（`dev:worktree -- init --mode test` 后经 `exec --mode test` 执行，专用测试库 `aap_wt_de57dbd75d11794b_test`，迁移位于 head `0174`）：

```
cd apps/api && uv run pytest -q --cov=app --cov-report=xml --cov-report=term-missing --durations=30 --junitxml=pytest-results.xml
```

结果 **[V]**：退出码 0；**4428 tests / 0 failures / 0 errors / 15 skipped / 631.17s**（junitxml 统计）；coverage line-rate 73.61% / branch-rate 55.33%（`coverage.xml`）。15 个 skip 为既有环境门控用例（未逐条保留清单，junitxml 已按清理约定删除）。

## 2. OpenAPI 快照 + 迁移策略

- OpenAPI 快照漂移检查：`uv run python ../../scripts/export_openapi.py --check` → "✓ openapi snapshot 与当前路由一致"，退出码 0 **[V]**。
- 迁移策略纯逻辑测试：`apps/api/.venv/bin/python scripts/test_alembic_migration_policy.py` → **Ran 33 tests / OK**，退出码 0 **[V]**。
- 真实 4 阶段迁移验证（fresh/reversible/forward/restore）**复用已验收证据**：被测状态 `9e34dfafb` 已包含 `0d044d4d` 的迁移脚本修复；`git diff 0d044d4d..HEAD -- scripts/alembic_reversible_floor.py scripts/validate_migrations.py apps/api/alembic` 为空（自 0d044d4d 验收证据以来输入字节不变）[V]。当前输入指纹：`alembic_reversible_floor.py` = `f2fce7e6…`、`validate_migrations.py` = `d95a7850…`、`apps/api/alembic` 聚合 = `84c2531b…`。

## 3. Python SDK / CLI / TUI

- `uv run --extra test pytest -q`（`packages/python-sdk`，uv 独立环境）：**322 passed in 44.60s**，退出码 0 **[V]**。
- `uv build`：成功产出 `ai_annotation_sdk-0.20.0-py3-none-any.whl` 与 `ai_annotation_sdk-0.20.0.tar.gz`；版本与既有包声明一致，未发生版本号变更 **[V]**。

## 4. docs-site/dev/examples ML 后端协议测试

- `echo-ml-backend`：`uv run --extra test pytest -q` → **5 passed**，退出码 0 **[V]**。
- `mock-v2-backend`：同命令 → **15 passed**，退出码 0 **[V]**。

## 5. ML CPU runner（`scripts/run-ml-cpu-tests.sh`）实跑补验

P6/P8 已验收：6 个轻量/共享套件实跑通过（yolo 224、backend_runtime 99、mask_utils 41、protocol_v2 163、onnxtools 70+3 skip、rapidocr 83），torch 两组当时仅 dry-run。本通道补齐实跑入口验证 **[V]**：

- `bash scripts/run-ml-cpu-tests.sh run grounded-sam2` → **150 passed in 7.80s**（Python 3.10 + CPU torch），退出码 0。
- `bash scripts/run-ml-cpu-tests.sh run sam3` → **196 passed in 10.00s**（Python 3.12 + CPU torch），退出码 0。
- 两组计数与 P6（`docs/research/34` 表）及 P8（`docs/research/37` 表）记录完全一致；新 runner 相对 P8 验收版（`ab7fc3af7`）的差异仅在 `plan()` 参数硬化与新增 `selftest()`，8 个套件的安装/pytest 分支未变。

**6 组复用的字节一致性判定**：`git diff --stat ab7fc3af7..HEAD -- apps/_shared/backend_runtime apps/_shared/mask_utils apps/_shared/protocol_v2 apps/yolo-backend apps/rapidocr-backend apps/onnxtools-backend scripts/ml-cpu-deps` 为空（runner 脚本本身的差异如上，且其实际安装/pytest 路径已经由两组实跑验证）[V]。

## 6. 环境与隔离

- 后端测试经 `dev:worktree -- init --mode test` + `exec --mode test` 专用一次性库执行；验收完成后该 test 模式资源已用其专属确认值销毁（见 §8），junitxml/coverage.xml/dist/venv 等任务产物已删除，仅保留本报告与文档。
- 未触碰 main 8000 / feedback 8100 / Grafana 3001；未做全局 pkill；未改动任何其他 worktree 的 PIDs/locks。

## 7. 边界与未验证 [GAP]

- 全部结果为**本地执行**；该候选未运行远程 CI，P9 的本地/远程对照仍未完成。
- 后端 15 个既有 skip 未逐条归因（属既有环境门控，非本通道引入）。
- grounded-sam2/sam3 为 CPU torch 契约测试；不含 GPU、真实权重推理与渲染资格验证。
- OpenAPI/迁移/SDK 覆盖的是契约与策略面；前端与浏览器链路由 P5 浏览器验收（research/33 §7）与 P8 smoke 证据另行覆盖。

## 8. 运行时清理证明

- e2e 模式销毁：`pnpm dev:worktree -- destroy --mode e2e --confirm de57dbd75d11794b:e2e` → "已删除本环境资源"。销毁后 doctor：全部 8 个自有 bucket 为 `null`、"独立数据库尚未创建"、"独立 Redis 未运行"（符合 skill 的销毁后预期：manifest 移除、identity/lock 保留）。
- test 模式销毁：`pnpm dev:worktree -- destroy --mode test --confirm de57dbd75d11794b:test` → "已删除本环境资源"。销毁后 doctor：`database: null`、`redis: null`、自有 bucket 全部 `null`。
- 两个模式均已停止（`state: stopped`）且基础设施清单移除，identity/lock 文件按 skill 要求保留。
- 任务产生的 scratch（`/tmp/aap-opt-p5-e2e`、`/tmp/opencode/p5`、`/tmp/migration-inputs.sha256`、`/tmp/ml-cpu-test.*` venv 由 runner trap 自清）均已删除或验证不存在。
