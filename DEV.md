# 开发指南

同机另建生产环境时，使用独立 `docker-compose.lan-prod.yml`，详见[局域网生产部署](docs-site/ops/deploy/lan-production.md)。从现有 `.env` 派生本地 `.env.production`，设置 `LAN_BIND_IP`、`AAP_SHARED_NETWORK`、`AAP_IMAGE_TAG`、`AAP_PRODUCTION_STATE_DIR`；生产数据库、九个存储桶、Redis、Worker 和 DuckDB 独立，前端/API 使用 HTTPS 3030/8080。命令始终显式带 `--env-file .env.production -f docker-compose.lan-prod.yml`，不要与开发或整栈生产 Compose 叠加。

> 完整的开发文档（架构、How-to、测试、规范）在 VitePress 文档站：
> 本地预览 `pnpm docs:dev`，部署版 [GitHub Pages](https://yyq19990828.github.io/ai-annotation-platform/dev/)。
> 本文件仅保留快速参考。

## 本机开发服务定时重启

主 checkout 的 Web/API 可由用户 systemd 的 `aap-development-web.service`（3000）与 `aap-development-api.service`（8000）管理。单元模板位于 `infra/systemd/aap-development-{api,web}.service.in`；安装到 `~/.config/systemd/user/` 前替换 `@PROJECT_DIR@` 和 `@NODE_BIN_DIR@` 为实际绝对路径。API 直接使用本 checkout 的 Python 虚拟环境，Web 使用固定 Node 路径；Web 端口被占用时会失败，不自动换端口。首次接管已有进程需核对其 PID、会话和工作目录，不得按端口杀未知进程。

将 `scripts/development-restart.py` 复制到 `~/.local/lib/ai-annotation-platform/`，通过 `~/.local/bin/ai-annotation-platform-dev-restart` 调用，传入 `--project-dir` 主 checkout 绝对路径、`--state-dir ~/.local/state/ai-annotation-platform/development-restart`，并透传手动参数。定时器和恢复 service 使用 `infra/systemd/aap-development-restart.{service,timer}`。

`aap-development-restart.timer` 每天北京时间 04:00 重启开发 Web/API，以及开发 Compose 的七类 Celery worker 和 beat。每次先核验容器项目、服务、Compose 来源及 `ALEMBIC_AUTO_UPGRADE=false`，再重启现有容器并等待健康检查；worker 的优雅退出上限为 120 秒，长任务应提前避开维护窗口。它不重建镜像、不自动迁移，也不重启生产容器、共享 PostgreSQL/MinIO/模型服务或其他基础设施。生产 3030/8080 仍由独立的 `aap-production-health.timer` 维护。

```bash
# 立即重启并等待开发服务就绪
systemctl --user start aap-development-restart.service
# 安装单元后启用每日定时重启（用户需已启用 linger）
systemctl --user enable --now aap-development-restart.timer
systemctl --user list-timers aap-development-restart.timer
journalctl --user -u aap-development-restart.service -n 30 --no-pager
# 仅检查开发容器身份，不重启
~/.local/bin/ai-annotation-platform-dev-restart --check
```

定时器关闭错过时刻的补执行，避免登录或开机时补做一次意外重启。API/Web 单元可单独启用开机启动和进程退出恢复。开发日志写入用户 journal，旧脚本的 PID 文件不再作为管理依据。

临时跳过每日重启可创建 `~/.local/state/ai-annotation-platform/development-restart/maintenance`，恢复时删除；完全停用定时器使用 `systemctl --user disable --now aap-development-restart.timer`。开始部署前，创建维护标记后执行 `flock ~/.local/state/ai-annotation-platform/development-restart/restart.lock true`，等待已运行的重启结束；标记会阻止新一轮及后续 API/Web 重启阶段。开发 worker 关闭自动迁移，升级时应先显式运行 Alembic；修改 Compose 后需要重建旧 maintenance 容器使该设置生效。生产维护定时器独立运行。独立 worktree 仍使用 `pnpm dev:worktree`，不接入主 checkout 的定时器。

## 项目结构

```
ai-annotation-platform/
├── apps/
│   ├── web/                     # React 前端
│   │   ├── src/
│   │   │   ├── components/      # shell、业务组件与 shadcn 原语
│   │   │   ├── pages/           # Dashboard、Workbench、项目与管理页面
│   │   │   ├── api/             # API client、业务封装与 codegen 输出
│   │   │   ├── stores/          # Zustand UI 状态
│   │   │   ├── types/           # 手写的 UI / 领域类型
│   │   │   └── styles/          # Tailwind / shadcn runtime tokens
│   │   ├── e2e/                # Playwright E2E + screenshots 自动化
│   │   ├── vite.config.ts
│   │   └── tsconfig.json
│   │
│   ├── api/                     # FastAPI 后端
│   │   ├── app/
│   │   │   ├── api/v1/          # 路由处理器
│   │   │   ├── db/models/       # SQLAlchemy 模型
│   │   │   ├── schemas/         # Pydantic schemas
│   │   │   ├── services/        # 业务逻辑
│   │   │   ├── workers/         # Celery 任务
│   │   │   └── utils/           # 工具函数
│   │   └── pyproject.toml
│   │
│   ├── grounded-sam2-backend/   # Grounded-SAM-2 GPU backend
│   │   ├── vendor/              # IDEA-Research/Grounded-SAM-2 镜像副本（sync_vendor.sh）
│   │   ├── predictor.py         # 三种 prompt (point/bbox/text) 路由
│   │   ├── main.py              # FastAPI 4 端点 + /metrics + /cache/stats
│   │   └── Dockerfile           # build context 为 apps/，可复用共享包
│   ├── sam3-backend/            # SAM 3 图像 / 视频 backend
│   ├── yolo-backend/            # YOLO 批量推理 backend
│   ├── onnxtools-backend/       # 检测与属性推理 backend
│   ├── rapidocr-backend/        # OCR backend
│   │
│   └── _shared/                 # 跨子应用共享 Python 包
│       └── mask_utils/          # mask→polygon (sam2-backend / sam3-backend 共用)
│
├── infra/docker/                # Dockerfile + Nginx (web/api)
├── scripts/                     # 工具脚本（seed.py / eval_simplify.py / sync_vendor.sh）
├── docs-site/                   # VitePress 用户手册 + 开发文档 + API
├── docs/                        # ADR / changelogs / plans / research
├── docker-compose.yml           # 本地基础服务（postgres/redis/minio/celery + 监控 profile）
├── docker-compose.ml.yml        # ML Backend 叠加（GPU 推理服务 + 截图协议 stub）
└── .env.example                 # 环境变量模板
```

## 前置要求

- Node.js >= 22
- pnpm >= 10
- Python >= 3.11
- uv (Python 包管理)
- Docker & Docker Compose
- pre-commit（推荐，启用 git hooks）

## 一次性 setup

MinIO 使用 `icr.io/fusion-open/minio/minio` 的固定 digest，Compose 与两个 CI 启动入口保持一致。原 Quay 镜像已停止分发；若拉取失败，先按[CI 服务依赖排障](docs-site/dev/troubleshooting/ci-flaky-services.md)核验镜像和 manifest，不要改用浮动标签或延长应用超时。

```bash
pnpm install               # 自动跑 scripts/install-git-hooks.sh，已装 pre-commit 即启用 hooks
cd apps/api && uv sync --extra test && cd ../..
# 若上面提示 "pre-commit 未安装"：uv tool install pre-commit && pre-commit install
# （用 uv tool 独立安装 pre-commit，勿 pip 装进项目 venv；否则 uv sync 会把它卸载）
```

## Worktree 初始化

Codex 的 `.codex/environments/environment.toml` 将源目录和 worktree 路径传给
`scripts/orca-worktree-setup.sh`，与 Orca 共用初始化逻辑。需要提前安装 Node.js、
pnpm 和 uv；Codex 在找不到 pnpm 时会尝试 `corepack enable`。

只有依赖清单和锁文件一致、源目录的三个 Node 依赖目录都存在，且已安装的
`node_modules/.pnpm/lock.yaml` 与源目录锁文件一致时，才共享这些目录；
否则安装当前 worktree 的锁定依赖。Python 虚拟环境保持独立，通过
`uv sync --locked --extra test` 同步，随后运行 `pnpm codegen` 生成当前 checkout 的 API 类型。
若已有共享依赖链接与新依赖不匹配，或 `.venv` / API 生成目录是符号链接，初始化会报错；
检查链接目标后，仅解除需要替换的链接，再重新初始化。

`.env` 和可选的 `.env.local` 与源目录共享，已有 worktree 配置会保留。
源目录没有 `.env` 时会创建权限受限的空配置，使用应用默认值；不会链接或修改
`.env.example`。共享配置中的修改会影响其他 worktree。
Codex 清理脚本固定在当前 worktree 执行，并包含 API 测试缓存。

初始化只准备依赖和生成文件，不启动服务或准备测试数据库。执行 API tests 前，
需确认测试数据库可丢弃。新 worktree 使用其基准提交内的脚本，因此这些改动需要
进入所选基准提交后才会在后续 worktree 生效。

隔离回归测试使用临时目录和模拟命令，不安装依赖、不连接数据库，结束后自动清理：

```bash
apps/api/.venv/bin/python scripts/test-orca-worktree-setup.py
```

### 多 Worktree 并行启动

先在主目录确认本机 PostgreSQL/MinIO 可用（只需 `docker compose up -d postgres minio`）。
在每个已完成 setup 的 checkout 根目录分别运行：

```bash
pnpm dev:worktree
# 需要后台任务时，使用当前 checkout 的本机 worker
pnpm dev:worktree -- up --with-worker
```

启动器给工作树分配稳定身份，创建专属数据库、带归属标签的 Redis 容器和九个
MinIO bucket，并隔离 DuckDB/临时文件。共享 `.env` 只作为基础配置；迁移、API、
测试和可选 worker 的目标通过子进程环境统一覆盖，不改共享库。

在线学习使用 `MINIO_TRAINING_BUCKET`（默认 `training`）持久保存冻结原图和训练包，不设置自动过期。旧八桶工作树在显式 `init/up` 时核验归属并扩展为九桶；原资源与数据保留，`doctor/stop/destroy` 兼容旧清单。升级中断留下未标记新桶时，按 `.worktree/<mode>/training-bucket-upgrade.json` 核对空桶后处理，不能跳过归属检查。运行环境更新需应用迁移并刷新 export worker，核验 `app.workers.training_dataset.run_training_dataset` 已注册。

### 数据集工作台规模测量

数据集分页、筛选与关联项目统计的可复现本地基线使用 `scripts/benchmark_dataset_workbench.py`，仅运行于 owned、空业务库的 `e2e` 环境。两档索引各含 40 张真实目录图片，其余明确为未核验合成索引；测量前用当前命名空间的迁移账号准备完整统计，API 仍使用普通运行账号。[测量方法与读数](docs-site/dev/reference/dataset-workbench-benchmark.md) 包含运行、浏览器请求预算及清理步骤。

真实目录遍历另使用 `scripts/benchmark_dataset_scan.py`，同样要求空业务库及完整归属核验的 `e2e` 环境。真实专属 Worker 测量 1,000／10,000／40,000 文件、取消恢复和既有预算边界；它不改扫描预算，也不代替图片读取性能。命令、计量口径及清理见[目录扫描资源基线](docs-site/dev/reference/dataset-scan-benchmark.md)。

目录原图的实际核验／HTTP传输使用 `scripts/benchmark_dataset_read.py`：三档真实编码并登记的图片，各测首次、单路与四路完整读取，记录 SQL、源字节、API资源和响应断开后的临时文件收尾。仅运行于同样归属核验且业务为空的 `e2e`，不修改生产读取和预算；方法、实际读数及清理见[原图读取资源基线](docs-site/dev/reference/dataset-read-benchmark.md)。

已采纳目录来源的完整增量同步使用 `scripts/benchmark_dataset_sync.py`：真实Worker测量1,000／10,000文件的无变化轮次和1%纯新增自动采纳，汇总全部扫描／预检／登记分块及旧文件重读。它在同样空业务库且归属核验的e2e环境运行，显式推进阶段唤醒，不计生产定时周期；复现、计量边界和清理见[增量同步资源基线](docs-site/dev/reference/dataset-sync-benchmark.md)。

### 任务数据集管理关联与回填

任务版本流的增量迁移只增加 `project_task_datasets` 与 `task_dataset_version_memberships`，不重写历史冻结记录。先核对实际 API／worker 的 checkout、挂载、数据库与迁移图，再应用迁移，刷新手动创建的 API 和执行自动学习策略的 worker／scheduler。Celery 不会因 Python 文件更新自动重载；仅本次管理与分析代码变化不要求重建未改变的 GPU 训练／推理镜像。

数据集生命周期升级须先应用 Alembic 0214，再接入新模型并刷新执行导入、建任务、扫描/登记、同步、质量检测及复核的 worker；原件与业务关系不回填。迁移是加列，历史为活动且生命周期修订1；发生归档/恢复后，降级会拒绝删除生命周期事实。独立工作树环境先核实资源所有权，使用其迁移连接；保留人工数据，禁止用测试数据库准备过程代替应用迁移。

历史关联使用 `apps/api/scripts/backfill_task_datasets.py`。目标必选其一：`--project-id UUID` 或 `--all-projects`；模式必选其一：`--dry-run`、`--apply`、`--verify`。以下以已核对的当前工作树 `dev` 模式为例；先停止同模式运行进程再执行，避免与启动器模式锁竞争。生产环境使用部署中明确选定的应用环境和现有虚拟环境，不能沿用不明来源的共享 `.env`。

```bash
pnpm dev:worktree -- doctor --mode dev
pnpm dev:worktree -- stop --mode dev
pnpm dev:worktree -- exec --mode dev -- sh -c 'cd apps/api && .venv/bin/python scripts/backfill_task_datasets.py --all-projects --dry-run'
pnpm dev:worktree -- exec --mode dev -- sh -c 'cd apps/api && .venv/bin/python scripts/backfill_task_datasets.py --all-projects --apply'
pnpm dev:worktree -- exec --mode dev -- sh -c 'cd apps/api && .venv/bin/python scripts/backfill_task_datasets.py --all-projects --verify'
```

只处理一个项目时，将每条命令的 `--all-projects` 换为 `--project-id` 和目标 UUID。预演／校验使用数据库只读事务，不分配序号；只有 apply 写入，每批默认 25、可用 `--batch-size 1..100` 控制。JSON Lines 报告包含扫描、已关联、本次关联、待关联、无法识别及异常数量和版本 ID／原因，不输出标注或存储凭据。校验发现待关联、关联不一致或高水位异常返回非零；无法识别记录保留原样并单独报告，需核对诊断而非自动修复。

中断后直接重跑 apply；已完成批次不重复分配，不重排旧序号。首次关联稳定按创建时间与 UUID 排序，后发现的旧版本追加高水位。滚动升级时有效未关联版本继续显示空管理字段；替换旧创建者后再次 apply／verify 完成尾部关联。回填不是测试夹具，测试与浏览器种子仍应使用已验证的一次性 test／e2e 环境。结束后按原需要重新启动当前 checkout 的服务。

回退使用面时停止新关联与回填，回退兼容的 API/UI 代码并保留两个管理表、membership 及 `next_ordinal`。旧代码仍使用原冻结版本；不以降级迁移删表、重排序号、重建包或回写摘要作为常规回退。分析缓存可随兼容修订重新派生。无需新增服务、依赖或永久环境变量。

### 训练执行器配置

受控训练默认关闭。配置 `TRAINING_ENABLED`、随机 `TRAINING_EXECUTOR_TOKEN`、`TRAINING_NODE_ID`、单张物理 `TRAINING_GPU_UUID` 及 `TRAINING_CATALOG_JSON` 后刷新 API，并重新创建 `celery-worker`，使定期策略检查使用同一训练目录。目录中的框架版本、镜像身份和基础权重 SHA-256 必须与实际执行器一致。构建输入位于 `apps/training-executor`，使用锁定依赖和独立虚拟环境；PyTorch 的 CUDA 版本需满足宿主驱动要求。

主开发 API 仅监听宿主 `127.0.0.1:8000` 时，训练执行器的本机 Compose override 需设置 `network_mode: host`，并将 `TRAINING_API_URL` 指向 `http://127.0.0.1:8000/api/v1/internal/training`。凭据文件保持 `0600`，将执行器 `user` 配置为其所有者的 UID/GID，并让持久 `/state` 卷归该用户所有；执行器移除了全部 capabilities，不能依靠 root 绕过文件权限。该 override 只用于独立训练 Compose 项目，不加入核心 worker 的 Compose 配置。

训练镜像固定 Ultralytics 8.4.183 与 LibreYOLO 1.6.0。共享注册表登记 YOLOv8／YOLO11／YOLO26 的 n/s/m/l/x 检测与实例分割，以及 LibreYOLO YOLO9 t/s/m/c 检测；目录只启用已准备权重并完成对应资格核验的型号。旧实验保留原框架、参数和镜像身份，不自动授权跨版本续训。LibreYOLO 目录项使用同样的 `version`、`image_digest` 和 `base_models` 结构，基础模型 ID 为 `LibreYOLO9t`，文件名 `LibreYOLO9t.pt`。官方模型来自 `LibreYOLO/LibreYOLO9t` 的固定修订 `66ac39d1ac9b880076b96ad4674ef9235ca1a15e`，大小 8,645,036 字节，SHA-256 为 `b4d7e93f9e0393830fb42e6135c0e3464b2673b05e5ecf4b7f2374ec18e39eb2`。先下载并核验字节和原生加载，再将只读文件及实际镜像身份配置到目录；执行器不会自动下载基础权重。

启用实例分割前，核验 [官方 YOLO11n 分割权重](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n-seg.pt)：大小 6,182,636 字节，SHA-256 为 `55ed65c56c91713d23e8402371c6c49a6fd84f257f7dce452e8d70e41dcbe152`。将文件加入只读基础权重目录，在 Ultralytics 的 `base_models` 中加入 `yolo11n-seg`，其 `filename` 为 `yolo11n-seg.pt`，`sha256` 为上述摘要。任务与资格从受控参数定义派生，不能通过添加任意模型 ID 开放新规格。共享契约变化后重建实际训练／推理镜像，再刷新目标 API、Celery 和 scheduler，确认目录中的分割参数身份与执行器相容。原生掩码训练需执行器明确上报 `instance_rle_consumer`；分割受控回流、逐样本 mask 评估、项目权重初始化、任务独立策略和主动选样各自核验资格，增强预览及参数搜索仍受原有任务限制。升级应用迁移后重建开发 API worker、YOLO 与训练执行器镜像，保持目录中的实际 registry image digest 一致；原有实验需要目录明确声明兼容镜像。见[实例分割链路](docs-site/dev/concepts/instance-segmentation-learning.md)。

```bash
hf download LibreYOLO/LibreYOLO9t LibreYOLO9t.pt \
  --revision 66ac39d1ac9b880076b96ad4674ef9235ca1a15e --local-dir ./training/models
sha256sum ./training/models/LibreYOLO9t.pt
```

LibreYOLO 使用自己的回调、完整状态恢复与评估，不接受 Ultralytics 专属参数。最终指标记录实际最佳权重摘要，原生 `precision`／`recall` 别名分别表示 mAP50-95／AR100，暂无分类别指标；训练可取消并续训，但不保证逐位复现。成功实验中的 LibreYOLO YOLO9 t/s/m/c 原生检测最佳权重可注册候选，通过原生 CPU 安装与固定样本试用后由负责人发布。E 不在固定 LibreYOLO 的原生实现中，不可新建执行；其他任务、量化及外部格式尚未通过回流资格检查；共享样本评估与主动学习排名仍仅支持 Ultralytics。移除训练目录项可停止新实验，但不改变已有模型或发布。

可用 `docker compose -f docker-compose.training.yml --profile training up -d training-executor` 启动独立服务。启动前先准备已发布的训练镜像，Compose 不再兼任构建。`TRAINING_IMAGE` 填仓库名（可带标签，但不得已有 `@digest`），`TRAINING_IMAGE_DIGEST` 填该镜像的 registry manifest digest，例如 `sha256:` 加 64 位小写十六进制。使用 registry 返回的摘要或 `docker image inspect` 的 `RepoDigests`，不能使用 `.Id` 的镜像配置摘要。Compose 将同一个 `repository@sha256` 引用用于实际镜像和 `TRAINING_IMAGE_REFERENCE`，执行器从该引用派生上报摘要；目录的 `image_digest` 也须一致。仅修改 API/Web 业务代码不要求重建训练镜像。

`TRAINING_EXECUTOR_TOKEN_PATH` 为仅管理员可读的凭据文件，`TRAINING_MODELS_PATH` 为校验过的只读基础权重目录，`TRAINING_API_URL` 指向目标 API 的 `/api/v1/internal/training`。不要将 executor 指向其他工作树。该服务不挂 Docker socket，不读取 API `.env`，不持有数据库或 MinIO 管理凭据。

模型回流需要重建 YOLO 镜像、应用迁移并刷新预测 worker。内置 YOLO 默认使用 `managed:yolo`，平台自动管理私有凭据；已有手动 `YOLO_MANAGED_MODELS_TOKEN` 优先，继续使用匹配的手动 `token`。Ultralytics 训练与推理使用相同的已验证版本；LibreYOLO 原生 CPU 适配器固定 LibreYOLO、Torch 与 torchvision 版本，并严格核对全部主模型张量、类别顺序和任务。此扩展无需新增迁移或环境变量；重建包含共享 qualification helper 的 YOLO 镜像并刷新 API／预测 worker。安装文件持久保存于 YOLO checkpoints 卷的 `managed/`，不能删除仍被部署使用的权重与收据。先更新实例健康与能力，再执行候选加载、固定验证图试用和发布。路由仅允许对应部署的确切实例；`off/observe` 模式下该实例还须是服务池的 legacy 实例。重新加载可恢复缺失或损坏的本地副本，不能改变版本身份。

五个第一方推理服务默认使用独立系统托管认证，API 启动时在 `data/inference-auth/<provider>/token` 安全生成一次并持久保存。目录为 `0700`、文件为 `0600`，并发初始化复用相同文件，异常文件不会覆盖。各 backend 只读自己的子目录，业务协议和 GPU 控制路由要求 Bearer；`/health` 与 `/metrics` 保持公开，GPU 控制签名仍独立验证。平台 worker 使用 registry 中已同步的凭据，无需读取这些文件。

`ML_MANAGED_BACKEND_URLS` 是部署固定的服务 ID → HTTP origin 映射，默认宿主桥接端口为 8001–8005，生产 Compose 默认使用内部服务 DNS。API 与所有 worker 必须使用相同映射；URL 不能重复或包含路径、userinfo、query、fragment。注册新环境实例默认采用对应 `managed:<provider>`；已有手动 Token 与无认证实例不会在启动时改写。超管显式初始化同步或保存系统托管时验证 `/managed-auth` 身份，禁止代理与重定向；普通健康检查不作为认证证明。

首次升级须重建五个启用的 backend 镜像并应用 Compose 的只读凭据挂载；API 容器对同一目录可写。目录骨架随 checkout 提供，Compose 禁止隐式创建宿主目录。非 root API 应与 checkout 目录 UID 一致；root API 发布前将文件归属设为目录 UID，便于宿主开发进程复用。目录专用于本部署，不能共享到其他 worktree；相对 `ML_MANAGED_AUTH_DIR` 锚定当前 checkout。保留目录与已有手动 Token，后续初始化无需修改 `.env` 或重建服务。

超管「设置 → 系统设置 → ML 服务认证与维护」可查看、初始化和同步认证。页面上的镜像/容器重建需要宿主机单独运行维护程序，安装与中断恢复见[本机 ML 服务维护](docs-site/ops/deploy/ml-backend-maintenance.md)；API 不直接访问 Docker socket。

同一物理 GPU 的所有执行器必须共享同一个持久 `/state` 卷和一致的节点 ID；子进程继承其中的资源锁。不要在进程运行时删除状态卷或强行释放数据库租约。先停止接收新实验，等待当前进程退出和产物上传，再轮换认证凭据或镜像。旧实验续训默认仍需原镜像；修订 1 仅使用明确配置的历史镜像兼容列表。修订 2 的检测检查点只有通过实际续训验收后，才可在对应框架目录的 `compatible_config_images` 中配置「来源镜像 → 完整既有 `configuration.identity` 列表」，同时核验目标镜像、框架版本和原生参数定义。不得改写冻结摘要，也不能用检测验收放行分割或未经验证的其他组合。执行器重启后恢复持久日志，上传故障只重试上传。

训练子进程退出后，永久性产物清单、配置或遥测校验拒绝会持久化为 `upload_failed`，经原租约 fencing 回报失败终态；收到确认或明确退役才清理本地记录。若失败终态本身也被永久拒绝，执行器停止并保留 journal 与租约，管理员应核对配置及日志后恢复，不能直接删除状态卷。网络故障、5xx、限流、具名锁繁忙和过期存储签名继续重试；已发出的完成回执只原样重放，避免响应丢失后改写已接受结果。

CPU 增强预览使用相同的发布分类与持久回执保护。预览失败终态无法确认时，仅暂停该 CPU 槽位，保留请求和租约，不改变正在运行的 GPU 训练；修复配置并重启执行器后重放原回执。

`--with-worker` 同时启动普通 worker 和单并发维护 worker。普通 worker 使用运行账号；
维护 worker 只消费 `maintenance`，使用同库的 owner 连接处理分区维护和统计刷新。
`doctor` 分别显示两者状态；任一个退出都会停止同次启动的服务。

迁移前检查 revision 唯一性、单 head 和数据库版本可达性；通过后才运行 Alembic。
API 默认从 `8100`、Web 从 `3100` 扫描空闲端口，并验证 HTTP 就绪。
端口锁保留到进程退出，Web 的 API/WebSocket 代理指向当次 API。
`Ctrl+C` 停止本次 API/Web 和 worker，保留资源。

手动体验筛选分支时，可直接启动带验收数据的独立环境：

```bash
pnpm dev:worktree -- up --mode e2e --scenario filtering
```

首次使用要求该模式没有业务数据；入口复用筛选测试夹具，输出账号、项目链接和预期结果。
再次运行会保留你的修改。账号为 `admin@e2e.test`、`anno@e2e.test`、`rev@e2e.test`，
初始密码均为 `Test1234`。验收记录位于 `.worktree/e2e/data/filtering.json`；存在该记录时，
启动器拒绝同环境的 `exec`，防止自动化测试重置数据。恢复初始数据或切换自动化测试前，
先停止并显式 `reset --mode e2e --confirm <确认值>`。详细场景与清理见
[工作树手动验收](docs-site/dev/how-to/worktree-environments.md#手动验收筛选功能)。

可指定自定义的扫描起点，已被占用时仍会继续向上寻找：

```bash
pnpm dev:worktree -- --api-port 8200 --web-port 3200
```

```bash
pnpm dev:worktree -- doctor
pnpm dev:worktree -- init --mode test
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && .venv/bin/python -m pytest tests/test_alembic_drift.py'
pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e
pnpm dev:worktree -- stop --mode test
```

`dev/test/e2e` 各有独立资源；同一模式不能同时运行两个管理/测试会话。`exec` 默认
为 `test`，其他命令默认为 `dev`。`stop` 还停止该模式的 Redis，保留数据及 AOF。
`reset/destroy` 必须提供 `doctor` 显示的精确 `--confirm '<id>:<mode>'`，
并拒绝有连接或归属不符的资源。不要删除或复制 `.worktree/` 身份目录。

`--skip-migrations` 只接受已在当前 checkout head 的数据库，不再作为未知 revision
的逃生开关。直接执行原来的 `dev:api`、pytest 或默认 Compose 不经过这个隔离入口。
支持范围为 macOS/Linux 的本机开发基础设施；不会自动注册共享 ML 后端或启动 GPU/beat。
完整的首次数据、权限要求、重建与清理说明见[独立工作树开发环境](docs-site/dev/how-to/worktree-environments.md)。

验证数据库与对象的一致恢复，可在当前 checkout 的 API 虚拟环境运行：

```sh
apps/api/.venv/bin/python scripts/verify_worktree_recovery.py
```

该命令只创建两套随机 owned test 资源，不接收现有环境目标，不启动应用或调度；源销毁后从完整备份代次和独立演练密钥恢复，再校验冻结包、标注、物理映射及未完成任务。成功或注入失败均核对清理；清理失败会保留私有清单。纯守卫测试为 `PYTHONPATH=scripts:apps/api apps/api/.venv/bin/python -m unittest test_storage_backup_bundle test_worktree_recovery`。详细预算、报告、故障注入与生产边界见[一致备份与隔离恢复演练](docs-site/ops/deploy/storage-recovery.md)。

对象存储源端迁移清单使用当前API虚拟环境和显式部署配置：`apps/api/.venv/bin/python scripts/inventory_storage_migration.py --output /private/source.jsonl`；`--verify`可离线核对同一清单。对象账号可限制为只读，命令不会建桶或修改业务行。九用途处理选择、预算、退出码及一次性实际演练见[源端迁移清单](docs-site/ops/deploy/storage-migration-inventory.md)。

## Codex 会话启动硬件上下文

仓库内的 `.codex/hooks.json` 会在 Codex 会话启动、恢复、清空或压缩后运行
`.codex/hooks/session-start.mjs`，把当前机器的平台、CPU、内存和 GPU 摘要注入
agent 上下文。脚本只使用 Node.js 标准库和系统自带查询命令，支持 macOS、Windows
和 Linux；未安装对应查询命令时仍会返回基础信息。

项目级 hook 需要通过两层信任才会运行：先信任项目的 `.codex/` 配置层，再在 Codex
中打开 `/hooks`，审核并信任当前 hook 定义。脚本或配置变更后哈希变化，需要重新审核。
未信任项目、未批准 hook，或无法完成交互式信任的非交互环境会跳过该 hook，因此不会
收到硬件上下文。

```bash
node scripts/test-codex-session-start.mjs
```

## 快速开始

### 1. 启动基础服务

```bash
docker compose up -d
```

这会启动：

- PostgreSQL 16 — `localhost:5432` (user/pass/annotation)
- Redis 7 — `localhost:6379`
- MinIO — `localhost:9000` (控制台 `localhost:9001`, minioadmin/minioadmin)
- Mailpit — SMTP `localhost:1025`、收件箱 `localhost:8025`
- Celery default / GPU / CPU / export worker 与 beat；按任务队列隔离并发

如需测试 MinIO 放在指定磁盘上的性能，可在 `.env` 设置：

```bash
MINIO_DATA_DIR=/mnt/fast-disk/ai-annotation-platform/minio
```

该变量只影响 MinIO 的 `/data` 挂载；留空时仍使用 Docker 托管的 `miniodata` 命名卷。切换前后数据不会自动迁移，已有对象需要先复制到新目录。

> **GPU profile（可选，需要标注工作台 SAM 工具或 `/ai-pre` 文本批量预标）**：GPU backend 在叠加文件 `docker-compose.ml.yml`，须同时 `-f` 两个文件：
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend
> ```
>
> 嫌麻烦可在 `.env` 设 `COMPOSE_FILE=docker-compose.yml:docker-compose.ml.yml`，之后省去 `-f`。首次启动自动下载 ~900MB checkpoints（cache 在 `gsam2_checkpoints` volume）；启动 health 探活周期 120s，`curl http://localhost:8001/health` 应返回 `{"ok":true,"loaded":true}`。需 NVIDIA driver ≥ 525 + nvidia-container-toolkit。
>
> **多卡机器指定 GPU backend 用哪张卡**：`docker-compose.ml.yml` 里各 GPU profile backend 固定绑定一张物理卡（`deploy.reservations.devices.device_ids`），不再是 `count: 1` 由 Docker 自动挑卡。默认 GSAM2/YOLO/ONNXTOOLS/RAPIDOCR 用卡 0、SAM3 用卡 1（双卡机器错开显存）；可在 `.env` 用 `GSAM2_GPU_DEVICE_ID` / `SAM3_GPU_DEVICE_ID` / `YOLO_GPU_DEVICE_ID` / `ONNXTOOLS_GPU_DEVICE_ID` / `RAPIDOCR_GPU_DEVICE_ID` 覆盖。**单卡机器必须把 `SAM3_GPU_DEVICE_ID=0`**，否则容器找不到卡 1 起不来。

### 2. 启动前端

```bash
pnpm install
pnpm codegen
pnpm dev:web
```

> 首次 clone / 清空 `apps/web/src/api/generated/` 后，先跑 `pnpm codegen` 再跑
> `pnpm dev:web`。`pnpm dev:web` 直接启动 Vite，不触发 `prebuild` 的生成检查；若缺
> `src/api/generated/capabilityVocab.gen.ts`，页面会报
> `Failed to resolve import "./generated/capabilityVocab.gen"`。如果
> `apps/api/capability-registry.snapshot.json` 也缺失，先运行：
> `cd apps/api && uv run python ../../scripts/export_capability_registry.py`。

打开 http://localhost:3000

### 3. 启动后端

```bash
pnpm dev:api
```

该命令先执行 `alembic upgrade head`，再启动
`uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 3`。若 `DATABASE_URL`
使用无 DDL 权限的普通运行角色，在 `.env` 另设 schema owner 的
`MIGRATION_DATABASE_URL`；FastAPI 仍只使用 `DATABASE_URL`。依赖由一次性 setup 中的
`uv sync --extra test` 安装，不要在本地手工拼装依赖列表。迁移完成后启动脚本会清空
owner 连接，运行进程不保留 DDL 凭据。

> `--timeout-graceful-shutdown 3`：代码改动触发 `--reload` 时，若有浏览器标签页还连着
> WebSocket（工作台 / AI 预标页），uvicorn 默认会无限等待这些连接结束，卡在
> `Waiting for background tasks to complete`，新代码迟迟不生效。设 3s 上限后到点强制
> 掐断 WS 放行 reload，客户端收到 1006 自走指数退避重连。

API 文档：http://localhost:8000/docs

## 技术栈

| 层        | 技术                                                 |
| --------- | ---------------------------------------------------- |
| 前端框架  | React 18 + TypeScript                                |
| 构建工具  | Vite 6                                               |
| 状态管理  | Zustand                                              |
| 后端框架  | FastAPI (Python 3.12)                                |
| ORM       | SQLAlchemy 2.0 (async)                               |
| 数据库    | PostgreSQL 16                                        |
| 缓存/队列 | Redis 7                                              |
| 对象存储  | MinIO (开发) / 阿里云 OSS (生产)                     |
| 任务队列  | Celery（default / GPU / CPU / export worker + beat） |
| 容器化    | Docker Compose                                       |

## 前端开发

### 设计系统

前端设计系统基于 Tailwind CSS + shadcn/ui。运行时主题 Token 定义在 `apps/web/src/styles/shadcn.css`：

```css
--sc-background: #ffffff;
--sc-foreground: #171717;
--sc-card: #ffffff;
--sc-border: #ebebeb;
--sc-brand: #0070f3;
```

组件样式优先使用 Tailwind 语义类（如 `bg-card`、`text-muted-foreground`、`border-border`）或直接读取 `--sc-*`。不要在 CSS 中继续使用旧 `--color-*` 变量。

### UI 组件

底层原语在 `apps/web/src/components/shadcn/ui/`。`apps/web/src/components/ui/` 保留应用兼容适配层，继续通过既有 import 使用：

```tsx
import { Button, Badge, Card, Avatar, StatCard, Icon } from "@/components/ui";
```

### 页面路由与数据访问

页面路由由 React Router 定义在 `apps/web/src/App.tsx`，不是 Zustand page switch。`/projects/:id/annotate` 与 `/projects/:id/review` 使用全屏工作台；Dashboard、数据集、存储、`/ai-pre`、模型市场、在线学习训练数据集（`/training` 与 `/training/:id`）、项目设置、审核、用户与系统管理等页面通过 `AppShell` 路由承载。

业务数据通过 `apps/web/src/api/` 的 API client 和 React Query 获取；Zustand 仅保存认证、界面偏好和短生命周期交互状态。测试 fixture 不能作为运行时数据来源。

### 前端 codegen

OpenAPI → TypeScript 类型由 `@hey-api/openapi-ts` 生成，落到 `apps/web/src/api/generated/{types.gen.ts, sdk.gen.ts}`。**该目录在 `apps/web/.gitignore` 中，不入仓**（避免 PR diff 噪声与 git lfs 麻烦）。

```bash
pnpm codegen                       # 手动重新生成（OpenAPI 或 capability snapshot 变动后）
```

日常无需手动跑：

- `pnpm dev:web` 只启动 Vite，不触发 codegen。`pnpm build:web` 的 `prebuild` hook 会在 snapshot 比生成输出新时增量生成。
- **首次 clone 或清空生成目录后**：在跑 `pnpm typecheck` 前先运行 `pnpm codegen`，否则 `src/api/generated/` 不存在会令强类型 import 失败。
- 改动路由或 Pydantic schema 后，先 `pnpm openapi:export` 刷新 snapshot，再 `pnpm codegen`；只改 capability registry 时，运行 `cd apps/api && uv run python ../../scripts/export_capability_registry.py` 后再执行同一 codegen 命令。

如果 PR 改动了 ProjectUpdate / ProjectOut 等共享 schema，提交前手动 codegen + typecheck 确认无回归。

## 后端开发

### API 端点与数据模型

HTTP API 统一挂载在 `/api/v1`，而 WebSocket 单独挂载在 `/ws/...`。路由与 Pydantic schema 是完整实现，不维护手写的端点清单：以 [API 文档](docs-site/api/index.md)、FastAPI `/docs` 与 `apps/api/openapi.snapshot.json` 为准。路由聚合入口为 `apps/api/app/api/v1/router.py`，视频任务与 tracker job 等 task 子路由由 `apps/api/app/api/v1/tasks/` 管理。

数据模型位于 `apps/api/app/db/models/`。除用户、项目、任务和标注外，运行时还依赖数据集 / scene、batch、prediction、ML backend registry、视频 frame / segment / tracker job、审计与通知等模型；变更前先查对应 schema、service 与迁移，而不要假设只有四张核心表。

### 配置

通过环境变量或 `.env` 文件配置，参考 `.env.example`。

系统设置白名单内的运营参数支持数据库覆盖，部署环境变量保留为默认值。邀请上限、离线判定、导入预算、同步建任务阈值与视频预热分别在业务消费入口读取；导入任务在受理时固定预算快照。详见[运行时覆盖契约](docs-site/dev/concepts/system-settings.md)。首次更新这些消费者代码后刷新对应 API/Celery 进程；此后通过设置页调参不需要重启，跨进程缓存最多约 30 秒，在线状态另需等待下一轮扫描。不要通过修改进程中的 `settings` 对象模拟后台设置生效。

## Grounded-SAM-2 ML Backend（v0.9.x）

`apps/grounded-sam2-backend/` 独立 GPU 服务，提供工作台 `S` 工具与 `/ai-pre` 文本批量预标的 SAM mask 推理。三种 prompt（point / bbox / text）路由到 SAM 2.1 + GroundingDINO；mask→polygon 简化用 `apps/_shared/mask_utils`（v0.9.4 phase 3 抽出共用，与 v0.10.x sam3-backend 共享）。

### 起 / 停

> GPU backend 在叠加文件 `docker-compose.ml.yml`，下方命令均须带 `-f docker-compose.yml -f docker-compose.ml.yml`（或在 `.env` 设 `COMPOSE_FILE=docker-compose.yml:docker-compose.ml.yml` 后省略）。

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend   # 起
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu down                          # 停（保留 checkpoints / hf_cache volumes）
docker logs -f ai-annotation-platform-grounded-sam2-backend-1     # 看 "models loaded; device=cuda"
curl -fsS http://localhost:8001/health                             # 探活
curl -fsS http://localhost:8001/cache/stats                        # SAM 2 image embedding LRU 命中率（v0.9.1）
```

### Rebuild（改了 backend 代码 / Dockerfile / mask_utils 后必跑）

build context **从 v0.9.4 phase 3 起升级到 `apps/`**（让 Dockerfile 能 COPY 兄弟目录 `_shared/mask_utils`）。**历史命令 `docker build apps/grounded-sam2-backend/` 已不可用**。

```bash
# 推荐：走 docker compose
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu build grounded-sam2-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend   # 重建后重起容器

# 或直接 docker build（需手指定 Dockerfile 路径 + 父目录 context）
docker build -f apps/grounded-sam2-backend/Dockerfile apps/
```

> **首次构建 ~10 min**（vendor Deformable Attention CUDA 算子要 nvcc 现场编译）；只改业务代码时只重做后两层 COPY ~3 min。改 vendor 才会触发完整重编。

### 协议契约

- **请求**：`POST /predict { task: { id, file_path }, context: { type, ... } }`
- **`context.type`**：`point` / `bbox` / `text`
- **v0.9.4 phase 2 字段**：`output: "box" | "mask" | "both"`（仅 type=text 生效，默认 mask 老前端兼容）
- **v0.9.4 phase 3 字段**：`simplify_tolerance: float | null`（仅 mask/both 路径，默认 1.0；像素级；调高减顶点 / 调低保细节；顶点 > 200 后端 logger.warning）
- 完整协议见 [docs-site/dev/reference/ml-backend-protocol.md](docs-site/dev/reference/ml-backend-protocol.md)

### `mask_utils` 共享包

```bash
# 评测 mask→polygon simplify tolerance（fixtures 含 84 张真实 SAM mask + 6 张合成）
uv run --project apps/_shared/mask_utils python scripts/eval_simplify.py \
    --masks-dir apps/_shared/mask_utils/tests/fixtures/real_sam_masks \
    --tolerances 0.5,1.0,2.0,3.0,5.0 \
    --out docs/research/13-simplify-tolerance-eval.md
```

最新评测见 [docs/research/13-simplify-tolerance-eval.md](docs/research/13-simplify-tolerance-eval.md)。

## 部署

> 开发态 / staging / 生产态的整体差异（谁进容器、profile、`ENVIRONMENT` 断言行为）见概念页 [运行环境形态](docs-site/dev/concepts/runtime-environments.md)；逐项环境变量与运维细则见 [部署指南](docs-site/ops/deploy/docker-compose.md)。

### 开发环境

```bash
docker compose up -d     # 基础服务
pnpm dev:web             # 前端 :3000
pnpm dev:api             # 后端 :8000
```

### 生产（api/web 进容器）

```bash
# 1. 准备生产配置：复制 .env.example → .env.production，逐项审过
#    （ENVIRONMENT=production 下 SECRET_KEY / CORS_ALLOW_ORIGINS / MINIO_SECRET_KEY 等必填）
cp .env.example .env.production

# 2. 叠加 prod 文件，基础设施 + api/web 容器一起拉起、worker 改用生产配置
#    --env-file 不可省：用于覆盖 worker 硬编码的 dev 凭据（详见 docker-compose.prod.yml 注释）
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

> 开发态不带 `-f docker-compose.prod.yml`，api/web 仍跑宿主机。
> 单独构建镜像：`docker build -f infra/docker/Dockerfile.web -t anno-web .` / `docker build -f infra/docker/Dockerfile.api -t anno-api .`。API 镜像需要仓库根目录作为 context，以复制 `apps/_shared/protocol_v2`。

## 测试与文档

```bash
# 全仓格式与静态检查
pnpm format:check                # Ruff + Prettier，只读校验
pnpm lint                        # 全仓 Python Ruff + Web ESLint / CSS token
pnpm format                      # 自动修复 Ruff / Prettier 格式
pnpm typecheck                   # Web TypeScript
uv tool run --from pre-commit==4.6.1 pre-commit run \
  --all-files --hook-stage manual --show-diff-on-failure  # CI 同款第二层复核

# CI 命名检查
node scripts/check-workflow-names.mjs --strict
node --test scripts/check-workflow-names.test.mjs

# 前端测试
pnpm test                        # vitest 单测
pnpm --filter @anno/web test:coverage  # 前端带覆盖率
pnpm test:e2e                    # Playwright E2E（自启 :3001/:8010，使用 annotation_e2e）
pnpm --filter @anno/web test:e2e:layout  # 无 API / 数据库的滚动布局回归（自启 :3218）
pnpm --filter @anno/web test:e2e:visual  # 独立视觉基线
pnpm --filter @anno/web test:e2e:stress  # 完整布局压力矩阵

# 后端 / 共享包测试
cd apps/api && uv run pytest                                            # FastAPI 平台后端
cd apps/_shared/mask_utils && uv run --extra test pytest tests/         # mask→polygon 共享包（grounded-sam2-backend / sam3-backend 共用）
cd apps/grounded-sam2-backend && uv run --extra dev pytest tests/       # SAM backend 单测（无 GPU 走 mock）

# OpenAPI 契约
pnpm openapi:export              # 改了 API 后必须刷新 snapshot
pnpm openapi:check               # CI 校验

# 文档站
pnpm docs:dev                    # http://localhost:5173
pnpm docs:build
# 从共享训练契约更新两训练仓库的完整参数表；--check 只核对一致性
apps/api/.venv/bin/python docs-site/scripts/generate-training-reference.py
apps/api/.venv/bin/python docs-site/scripts/generate-training-reference.py --check
pnpm --filter @anno/docs-site check:all  # 文档元数据、导航与生成物检查
```

后端 CI 输出最慢 30 个 pytest 阶段，完整用例耗时保存在 `backend-test-results`
artifact 的 JUnit XML 中。测试用户工厂在进程内复用默认密码 `Test1234` 的真实 bcrypt
哈希；自定义密码、生产密码哈希与验证逻辑保持原样。四角色 fixture 与 E2E 造数
共用这一工厂，避免为每个测试账号重复计算默认密码哈希。

`seed.injectToken()` 用一次性的同源空文档写入身份，避免切换账号时先启动旧账号的
首页请求。调用后需显式导航到测试页面；已有 localStorage 偏好会保留，真实请求
错误检查不作放宽。

CI E2E 每个 PR 保留 4 个功能分片，以及 readonly Mask、native Mask、native Mask AI
三个任务。应用、共享依赖和相关 CI 配置变更时，路由脚本
`scripts/plan-e2e-suites.mjs` 追加独立视觉与压力套件；main 始终运行全部九套件，
`E2E extended` 每晚和手动运行两个扩展套件。默认配置排除 `@visual` / `@stress`，
但六种布局上下文的 14 次短流程及全部恢复断言仍属于每个 PR 的功能回归。
每个任务通过可复用的 `e2e-run.yml` 拥有独立 PostgreSQL、Redis 和 MinIO；同一数据库
只用一个 Playwright worker，因为 seed/reset 和 teardown 会清理共享数据。
默认、视觉和压力套件使用构建产物，Mask 任务由 Playwright 启动隔离 API 和 Vite dev。
功能用例最多重试一次；扩展用例中视觉基线不重试、压力用例最多重试一次；进程/步骤/job 保留 15/20/30 分钟限时。
`Frontend E2E` 汇总门禁同时要求路由和全部已选任务成功。每个套件的 HTML、JSON、
失败截图和 trace 分别上传到 `playwright-report-<suite>`，Actions 摘要展示结果与耗时，
并区分失败、flaky、跳过及全局错误。详见 [E2E 运行说明](apps/web/e2e/README.md)。
同一 PR 的新提交会取消旧 CI；main 的每次 push 验证均保留。

独立套件和分片缩短并行等待时间，可能增加 runner 总用量。调整分片数量时，使用
`pnpm exec playwright test --list --shard=1/4`（依次检查四片）确认默认用例无遗漏、
无重复，并观察远端最慢分片的耗时与排队时间；不要在共享本地数据库上并发运行分片。

CI 检查名称统一使用 `领域 职责`，例如 `Backend tests`、`Frontend verification`、
`Docs validation`。所有 job（包括单 job 工作流）都显式设置 `name`，并在仓库内保持唯一；
整个名称使用 sentence case，保留 Python、SDK 等专有名词和缩写。工具明细放在 step 名称中。GitHub 会展示 `工作流 / job (事件)`，因此 job 名称不再添加斜杠；
例如 `CI / Frontend verification (pull_request)`、`CI / Backend tests (pull_request)`。
工作流文件使用 `<domain>-<action>.yml`（聚合工作流保留 `ci.yml`），顶层名称使用 sentence case。
命名检查器不依赖额外安装包，按仓库统一的块式 YAML 检查：job 键缩进两格，job 属性缩进四格。
本地 pre-commit 和 CI 均严格检查文件名、工作流名称、job 名称格式及重名。
修改检查名称时，应核对仓库分支保护或 ruleset 中引用的 required checks，并同步迁移对应名称。

完整测试指南见 [docs-site/dev/testing.md](docs-site/dev/testing.md)。

真实超大图回归使用固定来源、字节数和 SHA-256 的开发夹具。基础服务和专用
`image-pyramid` Worker 启动后，可下载、入库并等待生成完成：

```bash
pnpm --filter @anno/web image:seeds
cd apps/api
PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --enqueue-pyramids --wait-seconds 1800
```

命令幂等创建 `P-LARGE-IMG` / `DS-LARGE-IMG`，原图只保存在 gitignored 的
`test-results/image-seeds/` 与当前开发对象存储；production 环境会拒绝运行。完整门槛、回填与诊断见
[图片金字塔运行手册](docs-site/ops/runbooks/image-pyramid.md)。

## 截图自动化

用户手册截图（`docs-site/user-guide/images/`）由 Playwright 脚本驱动重生成，
不进默认 CI，由 maintainer 手动触发。截图脚本只使用 `screenshots`
seed profile 和只读 catalog，不会随机选取开发库里的项目。`--repair` 只收敛带截图
seed 标记的对象。整套自动化固定使用 `annotation_screenshots_test`，不连接
开发库 `annotation`。

截图 seed 会同步初始化真实离线分析视图，无需等待 `cleanup` 队列。PostgreSQL `_test` / `_e2e` 库未显式设置 `DUCKDB_PATH` 时，默认使用当前 checkout 的 `.artifacts/analytics/` 私有文件，按数据库主机、端口和库名区分；seed 与 API 必须使用同一 checkout 和数据库。显式 `DUCKDB_PATH` 优先，相关进程须指向同一个专属文件，截图 seed 拒绝共享开发默认分析文件。详见[截图维护指南](docs-site/dev/how-to/update-screenshots.md#快速运行)。

交互流程的点击与笔迹坐标来自 catalog 中随任务版本化的归一化语义锚点：锚点可由模型候选生成，但进入截图 seed 前必须经过标签或人工复核，录制器不在运行时猜测画布目标。点云静态场景同时包含轻量 PCL RGB-D 夹具与 nuScenes 六相机环视夹具；后者固定携带六路同步图像、内外参与单帧激光雷达，专门覆盖多相机画布状态。

nuScenes 缓存需包含地图、元数据和关键帧传感器文件；初始化时会校验地图是否齐全。录制 catalog 根据数据集 UUID 解析导入器的隔离存储路径，逻辑帧名称保持固定。

### 前置条件

```bash
docker compose up -d postgres redis minio
cd apps/api
export SCREENSHOT_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test
DATABASE_URL="$SCREENSHOT_DATABASE_URL" uv run python scripts/prepare_e2e_db.py
DATABASE_URL="$SCREENSHOT_DATABASE_URL" uv run alembic upgrade head
cd ../..

# 另开窗口启 API；截图 catalog/login 需要显式测试路由门禁
cd apps/api && DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test \
  E2E_SEED_ENABLED=true ENVIRONMENT=development \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8010

# 另开窗口，让截图 Web 只代理到上面的专用 API
cd apps/web && API_PROXY_TARGET=http://127.0.0.1:8010 PORT=3001 pnpm dev --host 127.0.0.1
pnpm exec playwright install chromium   # 首次需下载浏览器

# 有 GPU/真实 backend：按能力发现并绑定图片、视频、OCR backend
cd apps/api && DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test \
  PYTHONPATH=. uv run python scripts/seed.py \
  --profile screenshots --repair --ml-backend-mode live
```

没有 GPU 时使用协议 stub。它仍经过 `/health`、`/setup`、全局 registry、项目启用关联和
主 backend 绑定，不会在数据库里伪造“已连接”状态：

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile screenshots up -d --build screenshot-ml-stub

cd apps/api && DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test \
  PYTHONPATH=. uv run python scripts/seed.py \
  --profile screenshots --repair --ml-backend-mode stub
```

stub 地址默认复用 `ML_BACKEND_STORAGE_HOST` 的主机部分并使用 `9100` 端口，确保宿主 API
和 Celery 容器都能访问；Docker 网关不同时可显式传
`--ml-backend-url http://<gateway>:9100`。seed 会按能力而非 backend 名称选择：图片要求
point、interactive box 与 exemplar，视频要求交互 tracker，OCR 要求整图输入及文本属性输出。
任一能力、连接、启用关联或主绑定缺失都会退出非零。

录制超大图渐进细节前，还需要把固定 Cosmic Cliffs 夹具导入同一截图数据库，并让专用
`image-pyramid` Worker 使用该数据库生成到 ready。只有这个可选项目存在时，catalog 才会返回
`large_image_demo`；其他截图与流程不依赖它。

```bash
pnpm --filter @anno/web image:seeds
cd apps/api
DATABASE_URL="$SCREENSHOT_DATABASE_URL" PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --id nasa-cosmic-cliffs --enqueue-pyramids --wait-seconds 1800
```

Worker 与上述命令必须使用同一 `SCREENSHOT_DATABASE_URL`；开发库 Worker 不会为截图库回写生成状态。

### 触发

Mac / Linux 分工录制优先使用 `pnpm --filter @anno/web screenshots:record -- --list`，
再用 `--flow bbox-draw --plan` 预览依赖。正式录制默认采集 `marketing` 高清母版，
再同源衍生项目需要的视频、GIF 与封面；`--profile docs --validate-only` 只预演，不生成正式源。
手工场景无需模型，真实 ML 母版目前使用 Linux X11/NVIDIA，模型服务可独立部署。
入口会修复隔离夹具，运行前须核对数据库和 Redis，并准备完整离线素材缓存。
详见 [跨平台录制与隔离要求](docs-site/dev/how-to/update-screenshots.md#高清母版统一采集与衍生)。

无显示器的 Linux NVIDIA 主机可在同一入口添加 `--headless-display`，为本次营销母版采集启动独立 Xorg 和 Metacity，
结束时清理自己的图形会话，继续执行硬件合成、帧率与母版门禁。需要当前用户已有 GPU 设备访问权。
专用会话关闭屏保和 DPMS，保持长时间准备夹具期间的显示输出。
可在采集进程中通过 `MARKETING_HEADLESS_GPU` 指定 `nvidia-smi` 报告的 PCI bus ID，
通过 `MARKETING_FFMPEG` / `MARKETING_FFPROBE` 指定编码工具；默认选择第一张 GPU，验证 PATH 中工具并在需要时尝试 `/usr/bin`。
这些选项不需要写入共享 `.env`；入口不修改系统图形配置或持久化桌面偏好。

```bash
export PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001
export PLAYWRIGHT_API_BASE=http://127.0.0.1:8010

SCREENSHOT_VALIDATE_ONLY=1 pnpm --filter web screenshots  # 只验证场景，不写 PNG/manifest
pnpm --filter web screenshots                              # 生成 desktop-light 正式图
pnpm --filter web screenshots:dark                         # 显式声明的深色场景
pnpm --filter web screenshots:matrix                       # desktop-light/dark/mobile
pnpm --filter web screenshots:flows                        # 流程预演，不生成正式素材
pnpm --filter web screenshots:marketing                    # 4K60 MKV + MP4/H.264，需本机 X11/NVIDIA/ffmpeg
pnpm docs:media:derive                                      # 从最新营销批次统一派生视频 / GIF / 封面
pnpm docs:media:audit                                       # 检查引用、来源与人工复核版本
pnpm --filter web screenshots:regression                   # 比较 9 张高价值视觉基线
pnpm --filter web screenshots:regression:update            # 有意 UI 变化后更新基线
```

不要手工维护资产数量；静态 manifest、流程 manifest、磁盘文件和文档引用共同给出当前清单。
所有视频录制入口只归档，不直接覆盖站点文件。标准源使用
`pnpm docs:media:derive -- --quality standard --run <run-id> --asset video-track --format video --clip 2:8`；
其中 `2:8` 必须换成实际审阅过的窗口；标准源 GIF 同样需要 `--clip`。仅派生 GIF 时使用 `--format gif`；多 GIF 资产须通过 `--gif-target` 分别指定各自窗口。
派生器校验源哈希和质量证据，不放大或补帧提升标准源等级，并保留未选择资产的清单记录。
宣传文章入口 `scripts/derive-article-media.mjs` 也复用同一校验、转码和来源记录实现。
生成后人工审阅 PNG，以及 GIF / MP4 的核心动作与最终结果；完整 matrix 成功后才原子重建静态 manifest，定向运行和 validate-only 不会替换它。
营销母版单独写入 Git 忽略的 `.artifacts/marketing/<run-id>/`，不可变的 4K60 MKV/H.264 采集源和
4K60 MP4/H.264 通用母版分别按 SHA-256 命名。录制使用 1440×810 逻辑 viewport 与 1.8× 设备像素倍率，
先由 X11 按 60Hz 主动采样 2592×1458 的真实动作，交给 NVENC 硬件编码，再用 Lanczos 统一到 3840×2160；工作台组件保持正常视觉比例。
启动器会拒绝软件渲染、有效独立画面低于 55fps、独立帧占比低于 90%，以及其它重复帧伪装 60fps 的情况，并把校准实测值写入 manifest。每个目标需在高清营销资产目录登记主题、分镜和独立时长规格；
组合多个功能的教程不归档为单项母版。归档时会强制检查 3840×2160、时长范围和 MP4/H.264 编码，
通用 MP4 会按真实操作窗口去掉首屏加载；单次命令即使因失败重启 Playwright worker，也只使用一个固定批次和一份 manifest。
内容规格、源 commit、两种格式的预留存储键以及 MP4 派生裁剪区间都会写入 manifest。
该命令不直接覆盖站点媒体；运行 `pnpm docs:media:derive` 后才从母版统一生成文档 MP4、首页 WebM / MP4 fallback 与 WebP 封面。上传远端并校验前不要删除本地运行目录。
本机显示不足 2700×1750 时，显式设置 `MARKETING_CAPTURE_DISPLAY`、必要时设置
`MARKETING_XAUTHORITY`，并在命令参数中追加 `--resize-display`；启动器在结束后恢复原显示尺寸。
流程脚本结束时会通过 `--repair` 恢复截图 seed 的期望状态。资产检查命令：

```bash
pnpm --filter web screenshots:lint
node docs-site/scripts/check-image-manifest.mjs --release
node docs-site/scripts/check-orphan-images.mjs --strict
pnpm docs:media:audit -- --release
```

`flow-manifest.json` 保存 GIF、文档 MP4、首页视频与封面的生成 commit、seed、哈希和母版 lineage；
`docs-site/maintainers/media-reviews.json` 单独保存人工复核 commit。只有工作树干净、媒体已提交并完成视觉检查后，才运行
`pnpm docs:media:approve -- --asset <仓库相对路径>`。
日常 `Docs validation` 用 `media:audit -- --integrity` 检查文件缺失与生成清单哈希一致性，
人工复核过期或关联源码变化不阻塞 PR。每周一北京时间 11:17 的 `Docs media audit` 生成阶段待办报告，
也支持手动运行。功能稳定、发布文档前，在 GitHub Actions 手动运行 **Release readiness** 并选择待验收分支，
执行完整文档检查、截图 `--release` 检查及媒体 `--release` 审核；失败时也保留媒体报告。
手动入口需工作流先进入默认分支，验收不会自动批准素材或发布版本。
具体步骤见[生成来源与人工复核版本](docs-site/dev/how-to/update-screenshots.md#生成来源与人工复核版本)。

`pnpm test:e2e` 使用 `annotation_e2e`，截图使用
`annotation_screenshots_test`，两者都与开发库 `annotation` 隔离。不要为省略建库步骤
而把两套自动化的 `DATABASE_URL` 指回开发库。

### 改场景

- 60 个 desktop-light 场景按功能放在 `apps/web/e2e/screenshots/scenes/`；新增项目场景
  必须声明 `fixture` 并通过 catalog 逻辑键生成 `route`。
- 主入口 `apps/web/e2e/screenshots/screenshots.spec.ts` 在浏览器导航前校验项目、任务、
  批次、backend 和场景能力，并分别使用 `admin`、`anno` 和 `qa` 的真实身份。
- 独立 config：`apps/web/playwright.screenshots.config.ts` —— 与默认 `playwright.config.ts`
  分离（默认 `testMatch: ["**/tests/**/*.spec.ts"]` 不收录 screenshots）。
- 用 `SCREENSHOT_VALIDATE_ONLY=1` 开发场景：它仍执行登录、导航、能力与 locator
  检查，但不覆盖正式资产或 manifest。

### 已知坑

- **远程浏览器只显示模糊占位图**：根 `.env` 保持 `MINIO_PUBLIC_URL=/minio`，
  DEV 会让签名媒体走页面同源的 `:3000/minio` 并由 Vite 转发到 MinIO，远程机器
  只需打通 3000 端口。若返回 `localhost:9000` 或宿主机 `:9000`，会分别命中
  访问者自己或受跨网段端口策略影响。`ML_BACKEND_STORAGE_HOST` 是容器的另一层
  地址，可以继续使用 Docker 网关。`MINIO_PROXY_TARGET` 必须与 `MINIO_ENDPOINT` 的协议、Host及端口完全一致，确保代理恢复签名 Host；worktree 启动器自动对齐。模型端点在签名前单独选择，不能替换已签名 URL。修改 `.env` 或 Vite 代理配置后重启 API/Web。
- **截图 seed 报 backend 能力不足**：live 模式只接受当前可达且能力快照覆盖截图场景的
  registry，不按名称猜测。确认目标 backend 已启动并被 API 同步；无 GPU 时启动
  `screenshot-ml-stub` 后改用 `--ml-backend-mode stub`。不要把浏览器的 `/minio`
  地址拿来配置 backend，后者必须同时对宿主 API 和 Celery 容器可达。
- **远程工作台一直「重连中」**：DEV 中的 WebSocket 默认跟随页面同源，由
  Vite 将当前页面端口的 `/ws` 升级并转发到本机 API；LAN 与 SSH LocalForward
  访问也保持同源。不要把 Docker 默认 IP 或服务器端 `localhost:8000` 发给远程浏览器；
  只有刻意绕过 Vite proxy 时才设置浏览器可达的 `VITE_WS_HOST`。
- **seed repair 后点项目被退回总览**：`--repair` 可能为 seed 自有项目生成新 UUID。
  旧页签或已缓存的项目卡片仍指向旧 UUID 时会跳回总览；修复数据后强制刷新
  项目总览一次。
- **中文路径**：仓库根含中文（`AI标注平台设计/`）时，`import.meta.url` 会 percent-encode；
  `screenshots.spec.ts` 已 `decodeURIComponent` 兜底，写到正确位置而非 `AI%E6%A0%87...` 镜像目录。
- **时间或动画导致截图漂移**：截图 driver 已固定测试时钟、语言、时区、DPR 并禁用动画；
  新场景还必须等待字体、图片解码和具体业务 ready selector，不要依赖单纯延时。

## 测试账号

> 仅 `development` / `staging` 环境可用（seed.py 拒绝在 production 执行）。

| 账号     | 角色          | 密码   | 初始视图           |
| -------- | ------------- | ------ | ------------------ |
| `admin`  | super_admin   | 123456 | Dashboard          |
| `pm`     | project_admin | 123456 | 项目总览           |
| `qa`     | reviewer      | 123456 | ReviewerDashboard  |
| `anno`   | annotator     | 123456 | AnnotatorDashboard |
| `viewer` | viewer        | 123456 | ViewerDashboard    |
| `anno2`  | annotator     | 123456 | (标注组A)          |
| `anno3`  | annotator     | 123456 | (标注组B)          |

初始化：`cd apps/api && uv run python scripts/seed.py`。首次运行会从官方地址下载
nuScenes mini 到 `~/.cache/ai-annotation-platform/nuscenes-mini`（约 4 GB，支持断点续传），
并导入 scene-0061 的 39 个关键帧；归档和解压内容都不会写入 Git 仓库。
重跑 seed 会保留已有任务和标注，并从 nuScenes `sample_data` 幂等回填旧相机条目缺失的
像素宽高，供 3D 投影、持久化 2D 成员和残差质检共用。

### COCO8 在线训练夹具

只生成在线训练测试项目，不导入视频、点云等其他示例：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed.py --profile online-training
```

该入口复用版本化素材清单，首次下载并校验 COCO8 与 COCO8-seg 两份归档（合计约 0.9 MB）到
`~/.cache/ai-annotation-platform/seed-assets`；可用 `--cache-dir` 指定缓存、
`--offline` 复用已缓存素材，或通过 `--asset-dir` 提供同时包含 `coco8/` 与 `coco8-seg/` 的本地素材目录。
需要当前开发栈的 PostgreSQL、MinIO 和 Redis；数据包在脚本内同步构建，不依赖 export worker。

生成项目 **COCO8 在线训练（dev）**（`P-COCO8-TRAIN`）和数据集 `DS-COCO8-TRAIN`，
使用 COCO 80 类、8 张原图、30 个原始检测框及 30 个实例掩膜，任务均已完成审核。
分割标注来自官方 COCO8-seg 多边形，经 COCO 栅格化后以原图 RLE 保存；不会将检测框转成掩膜。`pm` 是负责人，
`anno` / `anno2` / `anno3`、`qa`、`viewer` 获得各自的项目职责。检测和实例分割分别生成独立的初始训练数据版本，均包含真实原图与对应标注，
保留官方 4 张训练图 / 4 张验证图划分；后续创建版本时选择同任务的父版本可继承分区。
升级旧检测夹具时只在原任务尚未人工修改、无锁或草稿时增补分割；不符合条件会拒绝增补。
分割已生成后重跑会保留人工修改或删除的掩膜，原检测快照、训练记录及发布均不变。
重跑保留已有任务、标注、数据版本、实验和策略；固定编号被其他数据占用时拒绝覆盖。
初始版本构建失败、取消或因进程退出遗留在运行状态时，重跑命令会基于同一冻结快照重新构建；
若上次构建仍在执行或清理，命令会提示稍后重试。也可到项目「训练数据」查看失败原因并重试。

登录 `pm`（密码 `123456`），打开项目「训练数据」，选择初始版本后新建实验。
已配置训练执行器时，可先选当前训练目录中的 Ultralytics / YOLO11n，试跑
`epochs=1`、`batch=4`、`imgsz=320`、`seed=42`。自动训练可引用该初始数据版本，
将变化门槛设为 `1`、检查间隔设为 `1` 分钟、冷却设为 `0`，再明确保存并启用。
首次成功后，修改并重新审核训练分区标注可验证下一轮触发；仅验证分区变化不触发训练。
实例分割选择 `yolo11n-seg`、原生 RLE 数据版本，建议 `epochs=3`、`batch=1`、`imgsz=640`、`seed=42`；
本夹具在 640 输入及验证器 160 掩膜平面预检中没有实例完全消失。
完整人工验收及目标 GPU 故障、受控模型生命周期验收见
[实例分割在线学习验收](docs-site/dev/how-to/validate-segmentation-online-learning.md)。
夹具不创建实验或启用策略，也不要求生成时已开启训练目录。COCO8 用于流程试跑，不能据此评估模型质量。

## 下一步计划

详见 [CHANGELOG.md](CHANGELOG.md) 顶部的 roadmap 与 [docs/plans/](docs/plans/) 下的具体计划。

自动训练策略默认关闭。部署迁移后刷新 default/export worker 与 beat，确认注册 `app.workers.learning_policy.check_policies`；beat 每分钟分批检查策略，独立训练执行器仍按固定 GPU 领取。负责人明确保存门槛、检查间隔和冷却时间后才启用；没有 beat 的工作树环境可通过「立即检查与同步」验收相同路径。关闭策略不要求训练目录仍在线，成功训练不会自动发布。

### 受控存储根与节点探测

`STORAGE_FILESYSTEM_ROOTS` 是根句柄到当前进程可见绝对目录的 JSON 映射，默认 `{}`；示例 `{"ceph":"/mnt/cephfs"}`。`.env.example` 只提供空默认，不给每个数据集配置环境变量。统一挂载后新增子目录、注册位置无需重启；新 volumes/容器环境需重建受影响容器，无需重建镜像。不要修改其他 checkout 的共享 `.env` 或挂载。

`STORAGE_SFTP_ROOTS` 默认 `{}`，声明经部署核验的只读 SFTP jail 及精确 host/port/username/完整主机公钥。连接 base_path 必须为 `/`；整个账号可读范围作为位置授权范围，不能通过来源子目录缩小安全边界。API 与指定扫描/读取 worker 均须加载相同资格；环境调整需刷新相应进程/容器，无需重建镜像。变更已有资格前先在平台禁用位置并提交修订，再协调所有节点，历史引用不会自动重绑新锚点。完整配置与操作顺序见[只读 SFTP 引用](docs-site/ops/deploy/docker-compose.md#只读-sftp-引用)。

新 probe task 使用 Celery `worker_direct`；部署本次代码时刷新受影响 worker 并核对任务注册和实际专属队列，再执行指定 hostname 探测。只重启 API 无法更新 Celery。隔离测试可启动归属当前 checkout 的临时 worker；不得刷新未知共享进程。配置和限制见[存储位置 API](docs-site/api/guides/storage-locations.md)及[部署示例](docs-site/ops/deploy/docker-compose.md#受控存储根目录)。

目录观测扫描复用指定 worker 的专属队列；修改 `workers/dataset_scan.py` 后刷新当前部署的相关 worker 并验证 `app.workers.dataset_scan.run_dataset_scan` 注册。源码挂载时无需重建镜像。来源和扫描账本迁移为0196；验证使用独立 test 环境和临时根，不要对真实业务目录做故障注入。参见[扫描接口](docs-site/api/guides/dataset-sources.md)。

内容核验账本迁移为0197；无新 worker task 或镜像构建；目录首次核验还需刷新使用 mounted 源码的扫描/probe worker、重新探测并扫描，记录实际主机证据，旧未知证据明确拒绝。托管严格读取只需当前 API。目录读取要求该 API 实例完成有效 read 探测；只挂载扫描 worker 时明确不可用。每进程快照上限256MiB临时字节，部署时给系统临时目录留出对应空间，多进程需累加；不写业务来源。见[严格读取接口](docs-site/api/guides/verified-content.md)。

目录原地引用使用 Alembic0198：应用回退须保留已有 Reference/Asset 身份，仅空引用表可 schema downgrade。管理员逐条登记后，普通手工标注会在 Task 锁内重新捕获有界图片；慢挂载会延长锁占用。按节点配置只读根和检查 API/worker 访问，详见[引用内容合同](docs-site/api/guides/verified-content.md#已登记目录引用)。工作树启动器接受 Celery 自动创建的本节点定向队列，仍拒绝缺失职责队列或意外订阅，避免指定节点扫描/探测造成就绪误判。

### 位置访问维护部署核验

迁移0215新增访问 Gate 与未收尾账本。先向经过所有权核验的目标应用迁移，再刷新该命名空间的 API 和所有实际队列消费者；Celery 不会热重载。无依赖或镜像变更时不重建镜像，工作树通过其拥有的服务刷新两个 Worker 并验证任务注册、队列和实际派发，保留原端口与人工业务。

`STORAGE_MAINTENANCE_PROTOCOL_ENABLED` 默认 false：新代码始终登记受控访问，但预览/提交控制保持关闭。确认旧 API/Worker 都已退出、新准入与旧委托拒绝已实测后，才为该命名空间的全部进程设置 true 并刷新。不能仅重启一个 API，或以初始空账本推断无旧访问。共享 `.env` 不应为单个工作树更改。

维护仅针对单个逻辑位置。取消或超时会等待实际 I/O 收尾；进程退出或账本删除失败留下记录，不会 TTL 到期自动清零。等待中的记录必须先核实责任，平台没有强制清零接口。独立冻结内容与平台自有清理不追加源维护限制，也不把共享根、其他位置和外部客户端当作已停止。回退代码保留账本；有未收尾或非初始维护事实时禁止丢弃0215。详见[维护协议](docs-site/dev/concepts/storage-connections.md#位置访问维护协议)。

### 托管原件保留账本

迁移0216新增独立原件位置记录。部署先向核实归属的目标应用迁移，再刷新实际执行Dataset/Item删除代码的API与Worker；无依赖或镜像变化时不重建镜像。旧代码删除不会生成新记录，不能将升级前或未登记的原件解释为已覆盖。

删除前的capture与业务删除同事务，无独立提交或对象I/O。没有保留策略时`retain_until=null`表示无限期保护；预演不签发物理执行凭证。回退保留账本，有记录时拒绝0216降级。离线恢复映射须包含该物理owner，未知身份拒绝静默映射；详见[原件保留合同](docs-site/dev/concepts/storage-connections.md#托管原件移除后的保留证据)。

迁移0217增加声明保留要求、定位摘要索引及历史不可变约束。先在核实归属的目标应用迁移，再接入新的保留/预演读取；刷新实际使用这些模型的所属API/Worker，不需要重建镜像。仅超级管理员与全权Key可通过五分钟预览确认修改一条记录的期限、登记或显式解除要求；未指定期限仍无限期，不设置生产默认天数。所有保留修改共用组事务锁，既有业务删除捕获继续追加默认无限期记录。

回退保留已登记和已解除历史；存在任何hold或非初始策略事实时0217拒绝降级。离线恢复只映射原有退休锚点的物理字段，新hold按稳定ID关联且所有字段保持。已声明要求仅约束托管原件预演，不能据此宣称已验证真实备份、迁移、S3对象锁或其他清理器均已纳管；详见[保留策略与要求](docs-site/dev/concepts/storage-connections.md#显式策略与声明保留要求)。

迁移0218新增原件核验历史。先对核实归属的目标应用迁移，再刷新所属API和执行default队列的Worker，确认`app.workers.managed_original_verification.verify_managed_original`实际注册；不需要重建镜像。核验使用独立持久访问记录，回调在SDK线程开始前提交Permit ID，取消或失败不等于实际I/O已关闭。running无TTL重跑，pending可显式重派；关闭不确定或进程丢失时保留责任，不能删除记录强行解除。

核验仅对当前可证明的平台定位和可信历史SHA/大小读取，固定5GiB、256KiB块、60秒协作预算，客户端短超时且单次尝试；超预算返回明确未完成。纯流完整摘要适用于多种文件，不引入图像解码或完整暂存。任意核验事实阻止0218破坏性降级；恢复历史不做物理映射，旧context失效后显式核验目标。见[核验合同](docs-site/dev/concepts/storage-connections.md#已登记原件的完整内容观测)。
