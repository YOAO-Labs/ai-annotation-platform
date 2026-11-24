---
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-11-23
---

# 运行模块与领域专项测试

先用[测试指南](../testing)选择验证层级。只有涉及独立制品、真实受限角色、组件恢复或专用硬件时，才运行下面的专项。每个需要独占资源的场景单独运行，保留 collection、逐项结果与清理报告。

## 模块边界定向验收

模块验证器从仓库根目录运行，使用当前 API 虚拟环境，并为本次验收创建独立的 PostgreSQL、Redis 和对象存储。冷工件场景另需要 Docker 与 Compose；运行前确认私网和镜像匹配。用新的报告路径，每个需要独占资源的场景单独运行：

```bash
PYTHONPATH="$PWD" apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-host-acceptance.json \
  --test-file tests/test_workspace_module_assembly_live.py
```

Workspace 宿主验收默认使用 `strict` 授权档，指南上传应在分配上传记录和写对象前拒绝。另用新环境和 `--workspace-consumer-profile default` 验证默认 LAN 角色的上传、读取、解除引用；最后引用移除单独选择 `tests/test_guide_clone_api_login_native.py`。这些 LAN 角色结果不代替生成工作树 LOGIN 的验收。

Workspace 包和共享契约分别运行 `(cd apps/workspace && uv run --extra test --extra runtime pytest -q)` 和 `(cd apps/_shared/workspace_contract && uv run --extra test pytest -q)`。用 `--test-file` 定向选择 Native 成员、负责人转移、配置、模板、项目查询、目录查询和普通角色用例；文件名见 `scripts/verify_module_data_boundary.py` 的 `TEST_FILES`。

独立 Workspace 成员与负责人转移用例需要私有解释器。用 `UV_PROJECT_ENVIRONMENT` 选择单独环境，运行 `uv sync --project apps/workspace --frozen --no-dev --extra runtime --no-editable`，将 `AAP_WORKSPACE_RUNTIME_PYTHON` 指向该解释器。

Workspace 的冷 worker 与 CLI 用例使用当前源码构建的 `infra/docker/Dockerfile.api` 工件，保留 `org.aap.acceptance=workspace-module-api`、唯一 `org.aap.acceptance-id` 与 `org.aap.source-revision` 标签。设置 `AAP_WORKSPACE_MODULE_API_IMAGE` 为该镜像，`AAP_WORKSPACE_MODULE_API_IDENTITY` 为对应 acceptance ID。仅在完整的 Docker COPY 输入集合与哈希匹配时复用镜像，不以标签代替源码核对。复用上面的 runner，将选项改为下表对应的环境变量及 `--test-file`。

| 开关（值为 `1`）                          | `--test-file`                                       |
| ----------------------------------------- | --------------------------------------------------- |
| `AAP_WORKSPACE_WORKER_ASSEMBLY_LIVE`      | `tests/test_workspace_worker_assembly_live.py`      |
| `AAP_WORKSPACE_CROSS_FRAME_WORKER_LIVE`   | `tests/test_workspace_cross_frame_worker_live.py`   |
| `AAP_WORKSPACE_EXPORT_WORKER_LIVE`        | `tests/test_workspace_export_worker_live.py`        |
| `AAP_WORKSPACE_VIDEO_TRACKER_WORKER_LIVE` | `tests/test_workspace_video_tracker_worker_live.py` |
| `AAP_WORKSPACE_IMPORT_CLI_LIVE`           | `tests/test_workspace_import_cli_live.py`           |
| `AAP_WORKSPACE_SEED_OPERATOR_LIVE`        | `tests/test_workspace_seed_operator_live.py`        |
| `AAP_WORKSPACE_MEDIA_SEED_LIVE`           | `tests/test_media_seed_workspace_live.py`           |
| `AAP_SEED_PARENT_OPERATOR_LIVE`           | `tests/test_seed_parent_operator_live.py`           |
| `AAP_IDENTITY_E2E_FIXTURE_LIVE`           | `tests/test_identity_e2e_live.py`                   |
| `AAP_IDENTITY_SEED_SELECTION_LIVE`        | `tests/test_identity_seed_selection_live.py`        |
| `AAP_LEGACY_VIDEO_FENCE_LIVE`             | `tests/test_legacy_video_fence_live.py`             |

旧视频围栏还要求设置 `AAP_LEGACY_VIDEO_FENCE_API_SOURCE_REVISION`，与镜像的源码标签一致。最后单独运行这一场景；它会撤销验收库里非 owner 角色的登录，不接触日常数据库。它不产生旧 Jobs 的退役证明。历史结果见[验收索引](../concepts/module-deployment#legacy-video-postgres-fence)。

在隔离环境单独排查时，可从仓库根目录运行 `apps/api/.venv/bin/python -m scripts.video_worker_fence --input-file <私有 JSON>` 检查 PostgreSQL，或用同样参数运行 `scripts.video_worker_observation` 检查退出的容器。容器观察在删除容器之前完成。对象存储探针须先证明同一凭据可以写入并读回，再验证围栏后同一写入被明确拒绝。超时、容器缺失或租约到期都不能当作退役证明。

Identity 整模块冷验收另使用当前源码的非 editable Identity 安装（`uv sync --project apps/identity --locked --extra runtime --no-editable --reinstall-package yoao-identity`）及 `apps/identity/Dockerfile`、`infra/docker/Dockerfile.api` 两个镜像。将 `AAP_IDENTITY_RUNTIME_PYTHON` 指向该安装的解释器。两个镜像都设置 `org.aap.acceptance=identity-admission`，各用不同的唯一 `org.aap.acceptance-id`，依次配置 `AAP_IDENTITY_RUNTIME_IMAGE`、`AAP_IDENTITY_RUNTIME_IDENTITY` 以及 `AAP_IDENTITY_API_RUNTIME_IMAGE`、`AAP_IDENTITY_API_RUNTIME_IDENTITY`。

设置 `AAP_IDENTITY_LIVE_ACCEPTANCE=1`，用同一 runner 分别选择 `tests/test_identity_module_assembly_live.py::test_complete_compose_remote_api_workers_restart_and_restore` 和 `tests/test_identity_module_assembly_live.py::test_complete_identity_migration_roundtrip`。分别保留 JSON、collection 和 JUnit；跳过不算通过。独立转发诊断见[验收运行时排障](../troubleshooting/local-acceptance-runtime#host-forwarding-diagnostics)。

受理视频输入与执行器联调另用 `apps/api/.venv/bin/python scripts/verify_video_consumers_containers.py --profile smoke --output .worktree/video-containers.json`。从当前源码构建 API、Media control、Media worker 与 Cleanup 镜像，并以 `AAP_VIDEO_CONSUMER_{API,MEDIA_CONTROL,MEDIA_WORKER,CLEANUP}_IMAGE` 指定。四个工件均用 `org.aap.acceptance=module-data-boundary` 和同一 24 位十六进制 `org.aap.acceptance-id`。`capacity` 运行固定负载和优雅交接，不能证明强杀后的自动重投；`media-handoff` 只需 Media worker 工件，验收过期租约接管。图片默认运行角色对照使用 `scripts/verify_legacy_image_runtime.py`。

只复验原件回收时，从仓库根运行 `apps/api/.venv/bin/python scripts/verify_module_data_boundary.py --reclamation-only --output .worktree/reclamation-acceptance.json`。该入口给各场景创建空数据库，核对实际登录、只读观察及指定版本删除；普通 pytest 因缺环境而跳过不算通过。

## Python SDK：仓内契约和发行物

在仓库根目录安装 `uv`，按[本地开发环境搭建](../tutorials/local-dev)将已校验的 `yoao-backend-runtime` wheel 放入 `.artifacts/python/plugin-sdk/`，然后运行：

```bash
python3.11 scripts/test-sdk-distributions.py "$(mktemp -d)/sdk-reports"
```

脚本构建 SDK 与平台维护的五个共享契约包。先从 SDK 源码包安装核心依赖，检查离线发现和未安装 CLI extra 时的错误；再显式安装带测试与插件依赖的 wheel。临时环境必须在仓库外，脚本不从相邻 checkout 或 `PYTHONPATH` 借用实现。

SDK 文档片段、HTTP 调用点、覆盖清单和生成目录与 monorepo 输入一起测试，缺失快照直接失败。其余 SDK 测试以及推理、协议、训练三组便携向量从各自 sdist 取出，对实际安装包执行。两份 SDK JUnit 的用例身份必须合起来恰好覆盖一次仓内收集清单；缺项、重复、跳过或缺少应携带的夹具都会失败。CI 的 `python-sdk-test-results` 保留五份 JUnit、`sdk-collection.txt` 和 `distributions.sha256`，不代表独立插件仓、服务部署或 GPU 资格。

## 在线学习与训练执行器测试

在线学习子系统的验证横跨控制面（`apps/api`）、执行面（`apps/training-executor`）与 [YOLO 推理插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/yolo)：

1. **执行器单元测试（无需数据库）**：
   ```bash
   cd apps/training-executor
   uv run --with pytest --with pytest-asyncio pytest
   ```
   覆盖不可变快照校验、Ultralytics / LibreYOLO 适配器、检查点恢复、原生报告与脱敏日志。原生增强预览使用 [Ultralytics 官方检测权重 `yolo11n.pt`](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt)。本地下载后对照[发布资产的 SHA-256](https://api.github.com/repos/ultralytics/assets/releases/assets/340060970) 校验，将 `PREVIEW_TEST_BASE` 设为文件的绝对路径；未设置时读取 `/models/yolo11n.pt`。本地缺文件则跳过，不计作通过。平台的有预算串行搜索由 API 控制面测试覆盖。
2. **控制面 API 与状态机测试**：
   ```bash
   pnpm dev:worktree -- exec --mode test -- \
     bash -lc 'cd apps/api && .venv/bin/python -m pytest -k training -v'
   ```
   覆盖训练数据集版本冻结、实验任务租约 CAS、心跳续租、尝试级观测指标、受控模型发布（`trained_models`）以及审核驱动的主动学习回流。
3. **真实链路验收**：按[本地开发](../tutorials/local-dev#在线学习与训练执行器联调-可选)准备独立环境、执行器和 COCO8 检测夹具，再通过界面完成训练、回流和人工发布。单元测试、夹具构建成功或模拟 API 页面测试均不能替代真实权重推理验收。

### 本地执行器交接与负载观测

真实 HTTP 交接、认领重放与租约恢复是显式选择的本地验收，不由普通 API 或执行器 CI 测试启动。准备模块边界验证器的一次性 PostgreSQL、MinIO 和 Node peer 前置条件后，在同一工作树仓库根运行：

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.artifacts/training-handoff-venv" \
  uv sync --locked --project apps/training-executor --only-group handoff
AAP_TRAINING_EXECUTOR_HANDOFF_ACCEPTANCE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --workspace-consumer-profile default \
  --test-file tests/test_training_executor_handoff.py::test_live_executor_claim_replay_competition_cancel_and_finish_replay \
  --output .artifacts/test-results/training-handoff.json
```

API 使用自己的运行环境，交接 peer 使用独立的 `.artifacts/training-handoff-venv` 和当前执行器源码；不继承 API 模块路径或数据库凭据，也不安装不需要的 CUDA 依赖。受控 CPU 子进程验证 HTTP 与租约恢复，不替代训练容器、GPU 或真实权重验收。

## 数据集、存储迁移与只读站点 Agent 测试

数据集与受控存储领域的验证分为纯标准库/组件单测、隔离容器拓扑演练与资源基线测量三层：

1. **只读文件原语、站点协议与 Agent 组件单测（无需数据库或 Redis）**：
   ```bash
   PYTHONDONTWRITEBYTECODE=1 \
   PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src \
   apps/api/.venv/bin/python -m pytest --import-mode=importlib \
     apps/_shared/filesystem_read/tests \
     apps/_shared/site_agent_contract/tests \
     apps/storage-agent/tests -p no:cacheprovider
   ```
   覆盖 Linux `openat2` 防符号链接/越界、`0600 VerifiedStage`、一次性本机 TLS 配对、`renameat2(RENAME_NOREPLACE)` 原子落盘、256 KiB 分块限速以及进程重启后沿同一 `session_id` 重放 stage 恢复。
   TLS peer 由本目录 `conftest.py` 按需提供；测试模块只从同包 `support.py` 导入共享 helper，不需要先收集其他测试文件。
2. **跨网络隔离拓扑真实演练（`scripts/verify_site_agent.py`）**：
   在双 Docker bridge 隔离网络（中心无法向站点发起入站连接）中验证端到端链路，支持三种场景（`--scenario basic | recovery | replicas`）：
   ```bash
   pnpm dev:worktree -- exec --mode test -- \
     apps/api/.venv/bin/python scripts/verify_site_agent.py \
     --state /tmp/aap-site-agent-verify \
     --runtime-image "$SITE_AGENT_RUNTIME_IMAGE_ID" \
     --scenario basic
   ```
   分别验证基础配对/扫描/预检登记/Range 读取（`basic`）、断网与重启恢复及撤销凭据关闭回执（`recovery`），以及主站点离线时显式同内容备用来源（`DatasetReferenceReplica`）的委托读取（`replicas`）。
   `SITE_AGENT_RUNTIME_IMAGE_ID` 使用本机已核对的 `sha256:…` 镜像 ID，每次使用新的私有状态路径。仅对归属已确认的残留，用同一状态路径运行 `--cleanup`；工作树资源仍由启动器清理。
3. **数据集与存储资源基线测量脚本**：
   在当前工作树专属且业务表为空的可丢弃 `e2e` / `test` 模式下运行，严禁指向非空开发库：
   - `scripts/benchmark_dataset_workbench.py`：[数据集工作台规模测量](../reference/dataset-workbench-benchmark)（1k / 10k 索引分页、组合筛选与项目统计）
   - `scripts/benchmark_dataset_scan.py`：[目录扫描资源基线](../reference/dataset-scan-benchmark)（真实 Worker 元信息扫描、取消与恢复）
   - `scripts/benchmark_dataset_read.py`：[目录原图读取资源基线](../reference/dataset-read-benchmark)（无缓存严格 SHA-256 核验与 HTTP 传输）
   - `scripts/benchmark_dataset_sync.py`：[本地增量同步资源基线](../reference/dataset-sync-benchmark)（Beat 调度、扫描、完整预检与新增登记闭环）
   - `scripts/benchmark_dataset_training.py`：[训练准备与重复传输资源基线](../reference/dataset-training-benchmark)（托管图片预检、物化打包与重复下载读取）

## 超大图金字塔与瓦片调度测试

大图调度和组件的窄测试：

```bash
pnpm --filter web exec vitest run \
  src/pages/Workbench/stage/imagePyramid.test.ts \
  src/pages/Workbench/stage/imageTileScheduler.test.ts \
  src/pages/Workbench/stage/useWorkbenchImageSource.test.ts \
  src/pages/Workbench/stage/useImageTileScheduler.test.tsx \
  src/pages/Workbench/stage/useImageStageFit.test.ts \
  src/pages/Workbench/stages/image/ImageWorkbench.test.tsx
```

真实 50MP/200MP 浏览器回归使用 `P-LARGE-IMG` / `DS-LARGE-IMG` 开发夹具（参见 [超大图金字塔派生资产](/dev/concepts/image-pyramid-assets#可复现开发夹具)）：

- `required` 大图任务必须断言自动加载路径不发起 `original` 原图请求。
- 快速平移/缩放/切题后，检查诊断快照中的 stale commit、reserved/retained bytes、live bitmap/ObjectURL 与请求数在停止操作后进入平稳期（plateau），离开任务后全部归零。
- 不同长宽比的大图切题回归需额外确认：新图像源不得渲染已释放的旧瓦片，缩放比例按新图尺寸重新 fit，放大后可通过滚轮或缩放浮条回到全图适应比例，且 Minimap 与右下角缩放浮条互不遮挡。

真实产品 E2E、Mask／异步 AI 专项及点云入口见 [E2E README](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/apps/web/e2e/README.md)。设备与像素验收须在实际浏览器执行；受控替身、CPU、软件解码或单独 Worker 通过不等于产品流水线或物理设备通过。
