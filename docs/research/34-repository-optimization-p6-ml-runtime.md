# 仓库优化 P6（ML/共享运行时子集）：managed_pool 去重、边界审计与注释 provenance 清理

> 完成日期：2026-09-20 · 隶属计划：`docs/plans/archive/1789880018_repository-optimization-plan.md`（P6 工作包；只做 ML/共享运行时子集）
> 工作树基线：`af3afdfb54ff3cede438424b3922ad307e4ccdcb`（分支 `worktree-agent-opt-p6-ml`，由 root `0588129ed` 起的工作树）
> 输入：`/tmp/aap-opt-p6-preaudit.md` rev2（静态证据，非执行测试）、`docs/research/26`/`27`（基线/台账，只读）
> 证据图例：**[V]** 本工作树实际执行或逐条核对；**[KEEP]** 明确保留并给出理由；**[LIMIT]** 未执行/留待后续
> 并行边界：不修改 `apps/api`、`apps/web`、`packages/python-sdk`、根 `scripts/`、CI、`docs/research/README.md` 与 `26`/`27`/TSV。P5 拥有 Workbench，P7 拥有 E2E/seed。

## 0. 结论

1. **ML-1 / ML-2 完成**：`managed_pool.py`（`grounded-sam2-backend` 与 `sam3-backend` 各 820 行、仅 2 行差异）提升为已存在的共享包 `apps/_shared/backend_runtime/src/aap_backend_runtime/managed_pool.py`，两份本地副本删除。日志身份通过新构造参数 `logger_name` 保留（见 §2）。
2. **并发保护唯一化**：逐字节相同的 `tests/test_managed_pool_concurrency.py`（307 行）移动为 `apps/_shared/backend_runtime/tests/test_managed_pool_concurrency.py`，两份 backend 副本删除。对守卫做定向负向变异后该套件按预期失败，恢复后全绿（§3）。
3. **`gpu_lifecycle.py` 与 `embedding_cache.py` 明确不合并**：两者分别存在真实的池拓扑差异与 vendor 耦合的缓存值契约，按计划 §4.2「同形 ≠ 同规则」保留为各自 backend 实现（§4）。
4. **注释 provenance 清理**：按计划 §7.1/§7.3 的「当前契约 vs 历史叙事」规则，删除活动源码/测试中「从某版本起/某版本新增/升级到」的版本叙事，保留协议、vendor、迁移与兼容字面等当期契约（§5）。
5. **测试**：在本地虚拟环境实测 §6；五个直接 `import torch` 的用例在安装 torch 后被收集并执行（CPU tensor + fake predictor），未因其 import 即排除。

## 1. 范围与边界

- 只改：`apps/{grounded-sam2,sam3,yolo,rapidocr,onnxtools}-backend` 与 `apps/_shared/backend_runtime`，以及本文件。
- 不改：API/web/SDK/根脚本/CI/共享研究 README、`docs/research/26`、`27`、TSV。
- 行为零变化：不新增/删除运行时功能，不改协议字段、API 路径、数据库模型、迁移元数据、锁语义与产品语义。
- 未新增转发模块：删除的两份 `managed_pool.py` 不保留 `import` shim；所有调用方直接改为从 `aap_backend_runtime` 导入（无 live 兼容理由）。

## 2. ML-1：`managed_pool` 提升到共享运行时

### 2.1 旧 → 新映射

| 旧位置                                               | 旧符号 / import                                                                                     | 新位置                                                                 | 新 import                                                                                                                 |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `apps/sam3-backend/managed_pool.py`（删除）          | `from managed_pool import BuildArtifact, ManagedBuildTimeout, ManagedLruPool, ManagedPoolBusyError` | `apps/_shared/backend_runtime/src/aap_backend_runtime/managed_pool.py` | `from aap_backend_runtime import BuildArtifact, ManagedBuildTimeout, ManagedLruPool, ManagedPoolBusyError, ResourceLease` |
| `apps/grounded-sam2-backend/managed_pool.py`（删除） | 同上                                                                                                | 同上                                                                   | 同上                                                                                                                      |

被提升的内核保留全部语义：builder 单飞（`_get_or_start_builder_locked`）、borrower 串行（`use_lock`）、容量/LRU、租约（`ResourceLease`）、清理 quarantine 与强制回收、`snapshot()` 驻留判定、取消安全释放。`__init__.py` 追加导出 `BuildArtifact` / `ManagedBuildTimeout` / `ManagedLruPool` / `ManagedPoolBusyError` / `ResourceLease`。

### 2.2 日志身份（logging identity）

原两份文件仅差 docstring 与 `logger = logging.getLogger(...)`。共享后新增构造参数：

```python
def __init__(..., pool_name: str = "resources", logger_name: str | None = None):
    ...
    self._logger = logging.getLogger(logger_name) if logger_name else logger
```

`_record_evict_locked` 与 `_build_and_publish` 的异常日志改用 `self._logger`。消费方传回原始 logger 名，日志记录身份与重构前逐字一致：

| 消费方                                                 | 传入 `logger_name`                   |
| ------------------------------------------------------ | ------------------------------------ |
| `grounded-sam2-backend/model_pool.py`                  | `grounded-sam2-backend.managed-pool` |
| `grounded-sam2-backend/video_pool.py`                  | `grounded-sam2-backend.managed-pool` |
| `sam3-backend/main.py`（image / multiplex / PVS 三池） | `sam3-backend.managed-pool`          |

### 2.3 直接调用方与构建路径更新

| 文件                                                                  | 改动                                                                                                 |
| --------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `grounded-sam2-backend/model_pool.py`                                 | import 改从 `aap_backend_runtime`；构造传 `logger_name`                                              |
| `grounded-sam2-backend/video_pool.py`                                 | 同上                                                                                                 |
| `sam3-backend/pool_domain.py`                                         | `from aap_backend_runtime import ManagedLruPool`                                                     |
| `sam3-backend/main.py`                                                | import 合并进既有 `aap_backend_runtime` 块；三处构造传 `logger_name`                                 |
| `grounded-sam2-backend/pyproject.toml`、`sam3-backend/pyproject.toml` | `py-modules` 移除 `"managed_pool"`                                                                   |
| `grounded-sam2-backend/Dockerfile`、`sam3-backend/Dockerfile`         | 移除 `COPY ... managed_pool.py`                                                                      |
| `grounded-sam2-backend/README.md`、`sam3-backend/README.md`           | 目录树注明 LRU 内核来自 `_shared/backend_runtime`                                                    |
| `apps/_shared/backend_runtime/README.md`                              | 「包含」增加池内核；「不包含」改为各 backend 池包装（`model_pool`/`video_pool`/`pool_domain`）仍本地 |

后端测试内的 import 同步改为 `from aap_backend_runtime import ...`（sam3：`test_idle_unload`、`test_device_policy`、`test_gpu_lifecycle_contract`、`test_lifecycle_endpoints`、`test_pool_status_v14_14`）。

## 3. ML-2：并发套件下沉与负向验证

- 旧：`apps/grounded-sam2-backend/tests/test_managed_pool_concurrency.py` 与 `apps/sam3-backend/tests/test_managed_pool_concurrency.py` **逐字节相同**（`cmp` 证实，307 行）。两者删除。
- 新：`apps/_shared/backend_runtime/tests/test_managed_pool_concurrency.py`（import 改为 `aap_backend_runtime`）。9 个用例直接覆盖共享内核：同键单飞 + per-entry use lock、活动条目不可被驱逐、取消 waiter 释放 borrower、预启动 builder 取消仍清理被驱逐 root、附件清理失败保留 root 供强制重试、builder 文件错误进入 unknown、异常 traceback 中的部分 root 不丢、preflight 失败不污染 builder、executor 线程非 daemon 假设。
- **不再存在**的旧保护 = 两个 backend 各一份的同名套件；替代保护 = 共享包内的一份（同一实现只被验证一次）。测试文件路径变化已在此记录，作为台账要求的确切 old→new 不变式替换。

### 3.1 定向负向变异（证明保留测试仍有效）

临时移除 `_get_or_start_builder_locked` 的「活动条目不可驱逐」守卫：

```python
# 变异前
if (candidate.borrowers == 0 and self._waiters.get(candidate_key, 0) == 0):
# 变异后
if True:  # MUTATION: active-entry eviction guard removed
```

结果：`test_active_entry_cannot_be_evicted` **失败**（活动条目被驱逐后 borrower 释放抛 `RuntimeError: invalid borrower release for 'active'`，而非预期的 `ManagedPoolBusyError`）。恢复守卫后 `sha1` 回到 `9c335a682cadff6562544f0e230811ce4921f99a`，全套件重新通过。证明该并发保护没有被空套件掩盖。

## 4. `gpu_lifecycle.py` / `embedding_cache.py`：保留边界（KEEP）

按计划 §4.2，仅「长得像」不足以合并。逐文件审计：

### 4.1 `gpu_lifecycle.py` — KEEP 两份（80 行 diff / ~1086 vs 1100 行）

- **池拓扑不同**：grounded-sam2 是 image + video 两池（`snapshot_for("image")`、`unload_legacy_image()` 返回 `(unloaded, loaded)`）；sam3 是 image + multiplex + PVS **三**池（聚合 `snapshot()`、`unload_legacy_all()` 返回 `(unloaded, loaded, video_loaded)`）。
- **legacy unload 响应形状不同**：sam3 需要 `_legacy_unload_response` 汇总两个视频池的 `video_loaded`，gs2 只需图片池。这不是重命名差异，而是请求/响应契约差异。
- **类型/类身份不同**：`GroundedSam2GpuLifecycle`/`GroundedSam2Pools` vs `Sam3GpuLifecycle`/`Sam3Pools`；admission / generation fencing / drain 的池集合由此不同。
- 结论：机械合并会抹平三池与两池的拓扑及 `video_loaded` 契约，属「同形 ≠ 同规则」。保留两份。

### 4.2 `embedding_cache.py` — KEEP 两份（~66 行 diff / ~125 行量级）

- **缓存值契约 vendor 耦合**：grounded-sam2 缓存 `SAM2ImagePredictor` 内部 `_features`/`_orig_hw`；sam3 缓存 `Sam3Processor.set_image()` 写入 state 的 `backbone_out` 字典（含 inst_interactivity 的 sam2_backbone_out）。两者 tensor 载荷、生命周期与 vendor 属性名不同。
- **默认参数与语义不同**：gs2 cap 16 / variant `"tiny"`；sam3 cap 32 / variant `"sam3"`，并保留 `is_batch` 未来位。
- 结论：只有先把缓存值契约泛化后才谈合并（preaudit ML-4 的 medium 优先级）。本期不做。两者均不 `import torch`（CPU 安全），但这不是可合并的充分理由。

### 4.3 其他近邻（同样不合并）

- `observability.py`（sam3 vs gs2）per-model 指标语义不同；`pool_domain.py` 双池 vs 三池语义不同——均在合并范围外。
- yolo/rapidocr/onnxtools 的 `model_pool.py`/`engine_pool.py`/`handle_pool.py` 不引用 `ManagedLruPool`，是不同服务模式，无重复证据。

## 5. 注释 provenance 清理（计划 §7）

**规则**：标识「wire 协议兼容集合」的版本串作为当期事实保留；「从某版本起 / 某版本新增 / 升级到 / 某阶段解除」的叙事移出源码（Git/CHANGELOG/ADR）。保留：协议版本（`v2`/`v2.1`/`v2.2`、`PROTOCOL_VERSION`）、依赖版本约束、迁移元数据、vendor commit（如 sam3 `4cbac14`）、ultralytics assets release 标签（`v8.3.0`/`v8.4.0`）、RapidOCR 模型仓 tag（`v3.9.0`）、许可与版权。

- **grounded-sam2**：`observability.py`、`predictor.py`、`schemas.py`、`main.py`、`video_predictor.py`、`embedding_cache.py`、`scripts/download_checkpoints.py` 及测试头。
- **sam3**：`predictor.py`、`schemas.py`、`main.py`、`video_predictor.py`、`pvs_video_predictor.py`、`embedding_cache.py`、`observability.py`、`scripts/download_checkpoints.py` 及测试头。
- **yolo / onnxtools**：源码与测试的平台版本前缀叙事（`# v0.x · …`、`"""v0.x · …"""`）。
- **rapidocr（保留）**：`download_models.py`/`catalog.py` 仅剩对归档计划文件名 `2026-06-29-v0.20.0-rapidocr-backend.md` 的引用（真实历史文档指针，非实现版本叙事），保留。

对比：清理前后 `grep -rn 'v0\.[0-9]' <owned> --include='*.py'`（排除 vendor 与 `aap_protocol_v2`）由 358 处降到：

```text
rapidocr-backend/download_models.py:8: docs/plans/2026-06-29-v0.20.0-rapidocr-backend.md
rapidocr-backend/catalog.py:4:         docs/plans/2026-06-29-v0.20.0-rapidocr-backend.md
```

## 6. 实测（本地虚拟环境）

环境隔离：每个包/后端使用各自工作树内的 `.venv`（`.gitignore` 覆盖，不入库）；不修改全局 site-packages，不改动 `.env`/`node_modules` 符号链接目标（两者均指向主 checkout，未改其内容）。

```bash
# 共享运行时（pytest，含下沉的并发套件）
cd apps/_shared/backend_runtime
uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[test]"
.venv/bin/python -m pytest -q

# grounded-sam2（Python 3.10；torch 由 CPU wheel 提供）
cd apps/grounded-sam2-backend
uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
VIRTUAL_ENV=.venv uv pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pytest -q

# sam3（Python 3.12）
cd apps/sam3-backend
uv venv --python 3.12 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
VIRTUAL_ENV=.venv uv pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pytest -q

# yolo（Python 3.10；ultralytics 间接拉入 torch，用例经已提交 sys.modules 桩）
cd apps/yolo-backend
uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q

# rapidocr（Python 3.10）
cd apps/rapidocr-backend
uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q

# onnxtools（Python 3.10；pyproject 未声明 cv2，本地补装 opencv-python-headless）
cd apps/onnxtools-backend
uv venv --python 3.10 .venv
VIRTUAL_ENV=.venv uv pip install -e ".[dev]"
VIRTUAL_ENV=.venv uv pip install opencv-python-headless
.venv/bin/python -m pytest -q
```

### 6.1 结果

| 目标                           | Python  | 结果                                 | 备注                                                                                                                        |
| ------------------------------ | ------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `apps/_shared/backend_runtime` | 3.10.18 | **99 passed / 0 skipped / 0 error**  | 含下沉的 `test_managed_pool_concurrency.py`（9 用例）；`--collect-only` = 99                                                |
| `apps/sam3-backend`            | 3.12.11 | **196 passed / 0 skipped / 0 error** | 全量 `tests/`；其中 `test_predictor_exemplar.py` + `test_pvs_mask_seed.py`（两个 `import torch` 文件）单独收集 32 / 通过 32 |
| `apps/grounded-sam2-backend`   | 3.10.18 | **150 passed / 0 skipped / 0 error** | 全量 `tests/`；其中三个 `import torch` 文件单独收集 34 / 通过 34                                                            |
| `apps/rapidocr-backend`        | 3.10.18 | **83 passed / 0 skipped / 0 error**  | 仅注释变更回归                                                                                                              |
| `apps/onnxtools-backend`       | 3.10.18 | **70 passed / 3 skipped / 0 error**  | 仅注释变更回归；其 `pyproject` 未声明 opencv，测试需补装 `opencv-python-headless`；3 个 skip 见 §6.3                        |
| `apps/yolo-backend`            | 3.10.18 | **224 passed / 0 skipped / 0 error** | 仅注释变更回归；`ultralytics` 依赖拉入默认 torch wheel，但用例经已提交的 `sys.modules` 桩替身，不做 GPU 计算                |

### 6.2 各后端发现配置（逐 backend，配置并不相同）

| 目标                      | `testpaths` | `pythonpath`（其余为 `"."`）                                                                | 其他                           |
| ------------------------- | ----------- | ------------------------------------------------------------------------------------------- | ------------------------------ |
| `grounded-sam2-backend`   | `["tests"]` | `../_shared/mask_utils/src`、`../_shared/protocol_v2/src`、`../_shared/backend_runtime/src` | 无                             |
| `sam3-backend`            | `["tests"]` | 同 gs2（mask_utils / protocol_v2 / backend_runtime）                                        | 无                             |
| `yolo-backend`            | `["tests"]` | `../_shared/protocol_v2/src`、`../_shared/mask_utils/src`、`../_shared/backend_runtime/src` | `asyncio_mode = "auto"`        |
| `rapidocr-backend`        | `["tests"]` | `../_shared/protocol_v2/src`、`../_shared/backend_runtime/src`（无 mask_utils）             | 无                             |
| `onnxtools-backend`       | `["tests"]` | 同 rapidocr（protocol_v2 / backend_runtime）                                                | 无                             |
| `_shared/backend_runtime` | `["tests"]` | 无 `pythonpath`；经自身 editable 安装解析 `aap_backend_runtime`                             | `python_files = ["test_*.py"]` |

由于各后端 `pythonpath` 直接指向工作树 `src/`，测试无需为共享包做 editable 安装；但各后端可解析的共享包**并不一致**（gs2/sam3/yolo 有 mask_utils，rapidocr/onnxtools 没有），故不能用一个合并断言替代。

### 6.3 `onnxtools-backend` 3 个 skip 的精确归因

均来自已提交测试里的 `pytest.importorskip`，缺的是上游 `onnxtools` 包（非本仓模块）：

| 测试 ID                                                                                                | 位置                                                                        | 原因                  | 是否建议 P8 补装                                                                                                         |
| ------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `tests/test_attribute_schema.py::TestAlignmentWithOnnxtools::test_vehicle_type_values_match_onnxtools` | L42 `importorskip("onnxtools.config")`                                      | 上游 `onnxtools` 未装 | **否**：由 Dockerfile 从 git 安装且带 `[inference]` extras（onnxruntime-gpu / opencv-contrib / supervision），非廉价依赖 |
| `tests/test_attribute_schema.py::TestAlignmentWithOnnxtools::test_color_values_match_onnxtools`        | L47 `importorskip("onnxtools.config")`                                      | 同上                  | 否                                                                                                                       |
| `tests/test_provider_probe.py::test_upstream_pipeline_shim_contract`                                   | L154-155 `importorskip("onnxtools")` / `importorskip("onnxtools.pipeline")` | 同上                  | 否                                                                                                                       |

补充：两个需要 `cv2` 的用例（`tests/test_predictor_lazy.py`、`tests/test_predictor_mapping.py`）在本地补装 `opencv-python-headless` 后正常收集/通过；其 `pyproject.toml` 注释已说明 cv2 由本地环境提供。3 个 skip 与本次改动无关。

**五个 `import torch` 用例**（均为 CPU tensor + fake predictor，非 GPU/权重测试）：gs2 `test_multi_polygon_output.py`、`test_predict_text_output_modes.py`、`test_video_predictor.py`；sam3 `test_predictor_exemplar.py`、`test_pvs_mask_seed.py`。安装 CPU `torch` 后全部被收集并执行（未因 import 被排除）；无权重下载、无网络依赖。

补充依赖说明：`onnxtools-backend` 的运行时依赖未含 `opencv-python-headless`，但其测试需要 `cv2`；实测补装后 3 个 skip 来自环境/可选能力，非本次改动引入。

## 7. 保留边界与限制

- `gpu_lifecycle.py` / `embedding_cache.py` / `pool_domain.py` / `observability.py` 仍是各 backend 本地实现，理由见 §4。
- yolo/rapidocr/onnxtools 的 `README.md` 与归档计划引用保留版本化路径（文档历史），未改。
- 后端 `tests/` 仍**未接入 CI**（P8 归属）；本次只证明它们在本地 CPU 环境可收集/可跑，不新增 CI 接线。
- 未下载任何真实模型权重：五个 torch 用例是 CPU tensor + fake predictor，不触发 vendor 权重下载；vendor 导入为惰性且未被调用。
- 未推送、未 rebase；最终 rebase 到 root 由协调方在干净提交后进行。

## 8. 提交

- 变更提交：分支 `worktree-agent-opt-p6-ml` 的最新提交（代码、测试与本文件同一提交）。因提交后还会按协调方要求 rebase 到 root `feat/codebase_opt260920`，完整 40 位 SHA 以 P6 ML 实现报告与 `worker_done` 正文为准，不在本文件内嵌会随 rebase 变化的哈希。
- 复核：`git diff --check` 通过（rebase 后再次执行）。
