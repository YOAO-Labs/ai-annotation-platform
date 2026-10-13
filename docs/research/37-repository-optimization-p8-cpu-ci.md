# 仓库优化 P8：ML 后端 CPU 测试 CI 执行

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P8 工作包；本子任务只落地 CPU 测试执行，不等同 P8 收口）
> 工作树基线：`826fa042d99f105a766d7cf661bc017874b77334`（分支 `worktree-agent-opt-p8-cpu`）
> 输入：`/tmp/aap-opt-p8-cpu-audit.md`（89 个 not-wired 文件的分类审计）、`docs/research/34`、`docs/research/35`
> 证据图例：**[V]** 本工作树实际执行；**[A]** 采用审计结论；**[LIMIT]** 未在本阶段执行或归属后续

## 0. 结论

1. 新增可复用工作流 `.github/workflows/ml-cpu-test.yml`（`workflow_call` + `workflow_dispatch` 全量入口），覆盖 3 个共享包与 5 个 ML backend 的 **88 个 Python 测试文件**；无 GPU、无权重、无网络依赖，仅按需安装 CPU `torch`。
2. 新增 `scripts/run-ml-cpu-tests.sh`（`plan` / `run` / `list`）与 `scripts/ml-cpu-deps/` 三个依赖文件；每个套件在自己的临时虚拟环境中安装并运行，避免已提交的 `sys.modules` torch 桩跨套件泄漏。
3. yolo 使用 test-only 依赖文件、**不安装 `ultralytics` 与 torch**；rapidocr 不需要 `rapidocr`/`onnxruntime`/`cv2`；onnxtools 显式补 `cv2` 并如实保留 3 个上游 `importorskip` skip；grounded-sam2/sam3 安装 CPU torch。
4. 本地以脚本实跑 6 个轻量/共享套件全部通过（yolo 224、backend_runtime 99、mask_utils 41、protocol_v2 163、onnxtools 70+3 skip、rapidocr 83），torch 两个套件走 dry-run 命令校验（安装路径与 P6 已验收命令一致）。

## 1. 调用契约（交给 P8 GLM 的 callers / 路径过滤）

```yaml
jobs:
  ml-cpu:
    uses: ./.github/workflows/ml-cpu-test.yml
    with:
      # 省略 = 默认 "all"；或逗号/空格分隔的子集
      suites: "yolo,onnxtools,shared-protocol-v2"
```

- 合法 suite 名（8 个）：`shared-backend-runtime`、`shared-mask-utils`、`shared-protocol-v2`、`yolo`、`rapidocr`、`onnxtools`、`grounded-sam2`、`sam3`。
- `suites: all`（或不传）运行全部；分隔符接受逗号、空格、制表符、回车与换行。**空、纯空白、纯分隔符、未知名字、通配符都返回非零且不输出任何 `true` 标记**（不会静默空跑，也不会把 `*` 当 glob 展开）——判据是解析后的数组非空（`read -a`），而不是对原始字符串做空格裁剪；`read -a` 不做路径名展开，所以字面 `*` 只会作为未知名字报错。
- 手动全量入口：Actions → “ML CPU tests” → Run workflow（同一 `suites` 输入）。
- 工作流内部由 `plan` job 解析输入，输出 8 个布尔量；8 个套件 job 各自 `if: needs.plan.outputs.<suite> == 'true'`，彼此独立（无 matrix，因此 `fail-fast` 语义不适用，各 job 显式 `strategy.fail-fast: false` 以保持既有矩阵约定）。
- 本工作流**只做执行**：触发范围 / 路径过滤 / 与 `ci.yml` 汇总的接线由 P8 GLM 的 caller 负责；根 `scripts/image-reference-utils.test.mjs`（Node）也归 P8 GLM。

## 2. 套件、依赖与运行方式

| suite                    | 路径                           | Python | 安装方式                                                                   | 关键依赖                                                                                                                                                                    | 期望                  |
| ------------------------ | ------------------------------ | ------ | -------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| `shared-backend-runtime` | `apps/_shared/backend_runtime` | 3.10   | `-e ".[test]"`                                                             | pytest、httpx、pillow                                                                                                                                                       | 99 passed             |
| `shared-mask-utils`      | `apps/_shared/mask_utils`      | 3.10   | `-e ".[test]"`                                                             | pytest、numpy、opencv-python-headless、shapely                                                                                                                              | 41 passed             |
| `shared-protocol-v2`     | `apps/_shared/protocol_v2`     | 3.10   | `-e ".[test]"`                                                             | pytest、numpy（test extra）、cryptography、fastapi、pydantic、PyJWT                                                                                                         | 163 passed            |
| `yolo`                   | `apps/yolo-backend`            | 3.10   | 仅 `-r scripts/ml-cpu-deps/yolo-test-only.txt`，**不** `-e .`              | pytest、pytest-asyncio、fastapi、prometheus-client、httpx、pydantic、numpy、pillow、opencv-python-headless、cryptography、PyJWT、psutil、pynvml；**无 ultralytics / torch** | 224 passed            |
| `rapidocr`               | `apps/rapidocr-backend`        | 3.10   | `-e ".[dev]"`                                                              | 后端声明依赖；conftest 用假 `rapidocr`；`onnxruntime` 由测试内 `monkeypatch` 伪造                                                                                           | 83 passed             |
| `onnxtools`              | `apps/onnxtools-backend`       | 3.10   | `-e ".[dev]"` + `-r scripts/ml-cpu-deps/onnxtools-extra.txt`               | 后端依赖 + `opencv-python-headless`（pyproject 未声明）                                                                                                                     | 70 passed / 3 skipped |
| `grounded-sam2`          | `apps/grounded-sam2-backend`   | 3.10   | `-e ".[dev]"` + `-r scripts/ml-cpu-deps/torch-cpu.txt --index-url .../cpu` | CPU torch（5 个直接 import + 4 个经 `predictor`/`main` 间接）                                                                                                               | 150 passed            |
| `sam3`                   | `apps/sam3-backend`            | 3.12   | `-e ".[dev]"` + CPU torch                                                  | CPU torch（3 个文件）                                                                                                                                                       | 196 passed            |

依赖细节与“哪些文件真正需要 torch”的逐文件分类见 `/tmp/aap-opt-p8-cpu-audit.md`。

## 3. 隔离设计（避免跨包 `sys.modules` 污染）

- 每个 suite 独立 job；每个 `run` 调用用 `mktemp -d` 建**自己的** venv，`trap` 在退出时清理（成功与非零退出都清理）。
- 该隔离是必要的：`apps/sam3-backend/tests/test_lifecycle_endpoints.py` 等文件会在 `sys.modules` 装入 MagicMock `torch`；同一解释器内后续套件可能“借用”该桩，产生误导性通过（审计实测：无 torch 全目录跑 sam3 得 173 passed / 12 failed / 1 error，gs2 得 70 passed / 12 failed / 15 errors）。
- 因此 CI 对 gs2/sam3 安装 CPU torch 跑**整目录**；对 yolo/rapidocr 使用各自最小依赖跑整目录；不允许用全局桩或改断言来掩盖差异。

## 4. 本地实际执行与证据

命令（脚本自身创建/清理 venv）：

```bash
bash scripts/run-ml-cpu-tests.sh plan all
bash scripts/run-ml-cpu-tests.sh selftest                         # plan 契约回归（14 例）
bash scripts/run-ml-cpu-tests.sh run shared-backend-runtime   # 99 passed
bash scripts/run-ml-cpu-tests.sh run shared-mask-utils        # 41 passed
bash scripts/run-ml-cpu-tests.sh run shared-protocol-v2       # 163 passed
bash scripts/run-ml-cpu-tests.sh run yolo                     # 224 passed
bash scripts/run-ml-cpu-tests.sh run rapidocr                 # 83 passed
bash scripts/run-ml-cpu-tests.sh run onnxtools                # 70 passed / 3 skipped
ML_CPU_DRY_RUN=1 bash scripts/run-ml-cpu-tests.sh run grounded-sam2  # 命令校验（torch 安装路径同 P6）
ML_CPU_DRY_RUN=1 bash scripts/run-ml-cpu-tests.sh run sam3           # 命令校验
```

- **[V]** 上述 6 个套件在本地脚本路径下全部退出 0；日志 `/tmp/opencode/p8ci2-*.log`。
- **[V]** 失败清理与非零保持：`PYTEST_ADDOPTS="-k __no_such_test__"` 强制失败时退出码 5 原样返回，临时 venv 归零（`/tmp/ml-cpu-test.*` 无残留）。
- **[V]** `bash -n scripts/run-ml-cpu-tests.sh`、`node scripts/check-workflow-names.mjs --strict`（全部合规）、`Pyyaml` 解析、`uvx --from actionlint-py actionlint .github/workflows/ml-cpu-test.yml`（exit 0）。
- **[V]** `plan` 契约回归（`selftest`，14 例：`all`、逗号/空格/制表符/换行/混合分隔、双逗号、重复、空、纯空白、纯分隔符、未知、`yolo,bogus`、`*`）全部通过；修复前复现的阻塞用例（仅制表符/换行）现在退出 1 且不输出任何 `true`；`*` 不再被 glob 展开。`uvx --from shellcheck-py shellcheck scripts/run-ml-cpu-tests.sh` exit 0。
- **[A]** gs2/sam3 的完整依赖安装未在本阶段重跑（其安装命令与 P6 已验收的 `-e ".[dev]"` + CPU torch 完全一致，仅加载路径变化），故以 dry-run 校验命令构造。

## 5. 边界与限制

- **不声称完成 P8**：caller/路径过滤、与 `ci.yml` 汇总的连接、Node `image-reference-utils` 测试均归 P8 GLM；本工作流是被复用单元。
- 未改 `ci.yml`、planner、其他 workflow、根测试清单 TSV、`docs/research/26/27/36`、README、P5 文件。
- 3 个 onnxtools skip 为已提交 `pytest.importorskip("onnxtools")` 守卫（上游包由 Dockerfile 从 git 安装），**如实保留、不伪造成通过**；未安装该重依赖。
- torch 从 `https://download.pytorch.org/whl/cpu` 安装，未固定具体版本；如需可复现可后续在依赖文件里加版本上界（P8 决定）。
- 本地只用 CPython 3.10（sam3 声明 3.12 由 `setup-python` 提供），未跑全 Python 版本矩阵。

## 6. 变更文件

- `.github/workflows/ml-cpu-test.yml`（新增）
- `scripts/run-ml-cpu-tests.sh`（新增）
- `scripts/ml-cpu-deps/{yolo-test-only,onnxtools-extra,torch-cpu}.txt`（新增）
- 本文件（新增）
