# 开发指南

同机另建生产环境时，使用独立 `docker-compose.lan-prod.yml`，详见[局域网生产部署](docs-site/ops/deploy/lan-production.md)。从现有 `.env` 派生本地 `.env.production`，设置 `LAN_BIND_IP`、`AAP_SHARED_NETWORK`、`AAP_IMAGE_TAG`、`AAP_PRODUCTION_STATE_DIR`；生产数据库、十个存储桶、Redis、Worker 和 DuckDB 独立，前端/API 使用 HTTPS 3030/8080。命令始终显式带 `--env-file .env.production -f docker-compose.lan-prod.yml`，不要与开发或整栈生产 Compose 叠加。

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

启动器给工作树分配稳定身份，创建专属数据库、带归属标签的 Redis 容器和十个
MinIO bucket，并隔离 DuckDB/临时文件。共享 `.env` 只作为基础配置；迁移、API、
测试和可选 worker 的目标通过子进程环境统一覆盖，不改共享库。

在线学习使用 `MINIO_TRAINING_BUCKET`（默认 `training`）持久保存冻结原图和训练包，不设置自动过期。已受理的不可变输入对象使用独立 `MINIO_INPUTS_BUCKET`（默认 `accepted-inputs`），该桶必须启用 versioning，且不能设置自动过期；`media-cache` 保持非版本化。旧八桶/九桶工作树只会在显式 `init/up` 时按阶段扩展到十桶；`doctor/stop/destroy` 仍按旧清单只读、停止或销毁，不会隐式创建新桶。升级中断会留下 `.worktree/<mode>/training-bucket-upgrade.json` 或 `inputs-bucket-upgrade.json`；缺少归属标签的新桶不会被自动接管。运行环境更新需应用迁移并刷新 export worker，核验 `app.workers.training_dataset.run_training_dataset` 已注册。

### 任务数据集管理关联与回填

任务版本流的增量迁移只增加 `project_task_datasets` 与 `task_dataset_version_memberships`，不重写历史冻结记录。先核对实际 API／worker 的 checkout、挂载、数据库与迁移图，再应用迁移，刷新手动创建的 API 和执行自动学习策略的 worker／scheduler。Celery 不会因 Python 文件更新自动重载；仅本次管理与分析代码变化不要求重建未改变的 GPU 训练／推理镜像。

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

受控训练默认关闭。配置 `TRAINING_ENABLED`、随机 `TRAINING_EXECUTOR_TOKEN`、`TRAINING_NODE_ID`、单张物理 `TRAINING_GPU_UUID` 及 `TRAINING_CATALOG_JSON` 后刷新 API。目录中的框架版本、镜像身份和基础权重 SHA-256 必须与实际执行器一致。构建输入位于 `apps/training-executor`，使用锁定依赖和独立虚拟环境；PyTorch 的 CUDA 版本需满足宿主驱动要求。

已验证适配器为 Ultralytics 8.4.177 与 LibreYOLO 1.6.0；两者支持图片检测，Ultralytics 还支持受控 `yolo11n-seg` 简单多边形实例分割。LibreYOLO 目录项使用同样的 `version`、`image_digest` 和 `base_models` 结构，基础模型 ID 为 `LibreYOLO9t`，文件名 `LibreYOLO9t.pt`。官方模型来自 `LibreYOLO/LibreYOLO9t` 的固定修订 `66ac39d1ac9b880076b96ad4674ef9235ca1a15e`，大小 8,645,036 字节，SHA-256 为 `b4d7e93f9e0393830fb42e6135c0e3464b2673b05e5ecf4b7f2374ec18e39eb2`。先下载并核验字节和原生加载，再将只读文件及实际镜像身份配置到目录；执行器不会自动下载基础权重。

启用实例分割前，核验 [官方 YOLO11n 分割权重](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n-seg.pt)：大小 6,182,636 字节，SHA-256 为 `55ed65c56c91713d23e8402371c6c49a6fd84f257f7dce452e8d70e41dcbe152`。将文件加入只读基础权重目录，在 Ultralytics 的 `base_models` 中加入 `yolo11n-seg`，其 `filename` 为 `yolo11n-seg.pt`，`sha256` 为上述摘要。任务与资格从受控参数定义派生，不能通过添加任意模型 ID 开放新规格。共享契约变化后重建实际训练／推理镜像，再刷新目标 API、Celery 和 scheduler，确认目录中的分割参数身份与执行器相容。分割模型回流、逐样本评估、增强预览、项目权重初始化、策略和参数搜索仍需独立资格验证。

```bash
hf download LibreYOLO/LibreYOLO9t LibreYOLO9t.pt \
  --revision 66ac39d1ac9b880076b96ad4674ef9235ca1a15e --local-dir ./training/models
sha256sum ./training/models/LibreYOLO9t.pt
```

LibreYOLO 使用自己的回调、完整状态恢复与评估，不接受 Ultralytics 专属参数。最终指标记录实际最佳权重摘要，原生 `precision`／`recall` 别名分别表示 mAP50-95／AR100，暂无分类别指标；训练可取消并续训，但不保证逐位复现。成功实验中的 LibreYOLO YOLO9 t/s/m/c 原生检测最佳权重可注册候选，通过原生 CPU 安装与固定样本试用后由负责人发布。E、其他任务、量化及外部格式尚未通过回流资格检查；共享样本评估与主动学习排名仍仅支持 Ultralytics。移除训练目录项可停止新实验，但不改变已有模型或发布。

可用 `docker compose -f docker-compose.training.yml --profile training up -d training-executor` 启动独立服务。启动前先准备已发布的训练镜像，Compose 不再兼任构建。`TRAINING_IMAGE` 填仓库名（可带标签，但不得已有 `@digest`），`TRAINING_IMAGE_DIGEST` 填该镜像的 registry manifest digest，例如 `sha256:` 加 64 位小写十六进制。使用 registry 返回的摘要或 `docker image inspect` 的 `RepoDigests`，不能使用 `.Id` 的镜像配置摘要。Compose 将同一个 `repository@sha256` 引用用于实际镜像和 `TRAINING_IMAGE_REFERENCE`，执行器从该引用派生上报摘要；目录的 `image_digest` 也须一致。仅修改 API/Web 业务代码不要求重建训练镜像。

`TRAINING_EXECUTOR_TOKEN_PATH` 为仅管理员可读的凭据文件，`TRAINING_MODELS_PATH` 为校验过的只读基础权重目录，`TRAINING_API_URL` 指向目标 API 的 `/api/v1/internal/training`。不要将 executor 指向其他工作树。该服务不挂 Docker socket，不读取 API `.env`，不持有数据库或 MinIO 管理凭据。

模型回流需要重建 YOLO 镜像、应用迁移并刷新预测 worker。`YOLO_MANAGED_MODELS_TOKEN` 至少 32 个随机字符；将相同凭据配置到平台已启用的 YOLO 推理实例（认证方式 `token`）。Ultralytics 训练与推理使用相同的已验证版本；LibreYOLO 原生 CPU 适配器固定 LibreYOLO、Torch 与 torchvision 版本，并严格核对全部主模型张量、类别顺序和任务。此扩展无需新增迁移或环境变量；重建包含共享 qualification helper 的 YOLO 镜像并刷新 API／预测 worker。安装文件持久保存于 YOLO checkpoints 卷的 `managed/`，不能删除仍被部署使用的权重与收据。先更新实例健康与能力，再执行候选加载、固定验证图试用和发布。路由仅允许对应部署的确切实例；`off/observe` 模式下该实例还须是服务池的 legacy 实例。重新加载可恢复缺失或损坏的本地副本，不能改变版本身份。

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

工作区基础配置若共用迁移账号，会创建本模式的普通运行 LOGIN，并把随机密码仅
保存在 `0600` 的 `.worktree/<mode>/runtime-credentials.json`；API／普通 worker 不再
使用 owner。迁移账号需要 `CREATEROLE`，否则先预配置分离账号，不会回退 owner；
已有分离账号配置继续使用原账号。`grant_runtime` 保留内嵌 Identity 能力并撤销私表及
列权限，所有 Identity 专用选择器也会清空。停止保留凭据，销毁先核对角色归属、
连接及外部依赖；凭据和清单保留到全部清理成功。直接 `dev:api` 或普通 Compose
不经过此工作区流程。具体范围见
[Identity 内嵌运行权限](docs-site/api/guides/identity-runtime.md#内嵌运行角色的私表权限)。

新增本地 editable 包或同步 Python 依赖后，要重启 Uvicorn 的父进程。源码热重载的
子进程会继承旧父进程的 `sys.path`，因此新解释器已经能导入的包仍可能在常驻 API
中报 `ModuleNotFoundError`。先按锁文件同步当前 checkout 的环境，再重启实际拥有
API 的服务；单独触碰源码不能刷新父进程的导入路径。

macOS 已安装 `local.ai-annotation-platform.api` LaunchAgent 时，先核对其
`WorkingDirectory` 与 `ProgramArguments` 确实指向当前主 checkout，再执行：

```bash
launchctl kickstart -k "gui/$(id -u)/local.ai-annotation-platform.api"
```

该服务若调用 `pnpm dev:api`，重启会先执行上述数据库迁移步骤。完成后检查
`/health/db`、`/health/redis` 和 `/health/minio`，再触发一次源码热重载确认新子进程
仍可启动。独立工作树的 API 继续由 `pnpm dev:worktree` 管理。

若服务管理器已退出，8000 仍被旧 reloader 占用，先核对该 PID 的完整命令、父子关系
与 cwd 确实属于主 checkout，再结束这个残留进程。新服务的端口占用错误不代表旧 API
仍能处理请求；恢复后仍需执行健康检查与源码重载验证。

> `--timeout-graceful-shutdown 3`：代码改动触发 `--reload` 时，若有浏览器标签页还连着
> WebSocket（工作台 / AI 预标页），uvicorn 默认会无限等待这些连接结束，卡在
> `Waiting for background tasks to complete`，新代码迟迟不生效。设 3s 上限后到点强制
> 掐断 WS 放行 reload，客户端收到 1006 自走指数退避重连。

API 文档：http://localhost:8000/docs

### 独立素材探测服务

`apps/media-probe` 提供视频元数据、完整帧时间表和分块 packet 样本的独立 HTTP
服务，只接收文件字节与探测参数，不需要业务数据库、Redis 或对象存储凭据。
单独构建并绑定回环端口：

```bash
export MEDIA_PROBE_TOKEN="$(openssl rand -hex 32)"
docker compose -p aap-media-probe-dev -f docker-compose.modules.yml up -d --build media-probe
curl --fail http://127.0.0.1:8092/health
export MEDIA_PROBE_URL=http://127.0.0.1:8092
```

将同一 `MEDIA_PROBE_URL` 和专用 `MEDIA_PROBE_TOKEN` 传给需要远程探测的 API、
media worker 或 CLI 进程；容器内 URL 必须从该容器实际可达。`MEDIA_PROBE_URL`
为空时直接调用同一包内的本地实现；选择远程服务后，失败会按原调用链处理，不自动
切回本地。HTTP 各 I/O 阶段超时由 `MEDIA_PROBE_TIMEOUT_SECONDS` 控制，默认 90 秒；
原 ffprobe 的 30 秒子进程上限继续生效。修改运行配置后刷新对应进程。

独立服务默认最多接收 1 GiB 文件，可显式设置 `MEDIA_PROBE_MAX_FILE_BYTES`。
请求流式写入临时文件，元数据为 JSON，帧和 packet 为带数量及 SHA-256 完成行的
NDJSON；完整性通过后才能替换持久帧索引。探测仍会在服务端整体解析 ffprobe 输出，
这不是长视频内存或生产容量资格。每个操作都上传源文件，会增加网络和临时磁盘成本。

仅清理本次单独启动的项目：

```bash
docker compose -p aap-media-probe-dev -f docker-compose.modules.yml down
unset MEDIA_PROBE_URL MEDIA_PROBE_TOKEN
```

模块整体状态见 [模块部署与语言替换](docs-site/dev/concepts/module-deployment.md)，
协议、独立运行和验证命令见 [media-probe](apps/media-probe/README.md)。

### 同库事务的数据接口

已有数据集的追加建题通过 `aap_labeling_v1.append_dataset_items` 执行，输入为 dataset
UUID、素材 UUID 数组与唯一操作 UUID，返回 `aap-task-append/v1` JSON。函数与来源素材
写入共享调用方事务，不内部提交。调用端可以使用任何 PostgreSQL 驱动；不可读取其他
模块私有表来代替公开接口。SQL 和 JSON Schema 位于 `apps/api/alembic/sql/0190*`、
`0191*` 至 `0210*`，迁移链安装同一份冻结 SQL。初始建题另有当前管理权、
关联创建、Jobs 生命周期和持久化计划/分块回执合同，参数及恢复规则见
[同库事务数据接口](docs-site/api/guides/module-data.md)。

项目删除由 Workspace 原生命令复核当前身份与管理权，组合各 owner 清理、学习证据保留、
审计和不可变回执；它们共同提交或回滚，不删除原始素材或对象。删除后作业保留权威项目范围，
不允许改绑项目；旧的范围缺失作业不会从 payload 补造授权。详见
[ADR-0094](docs/adr/0094-transactional-project-deletion.md)。

项目取消数据集关联也使用 Workspace 原生命令：当前账号/项目管理权、关联代次、学习和标注引用检查、任务删除与计数、Audit、Workspace 回执及 Assets 的派生金字塔清理计划在调用方同一事务中提交或回滚。Jobs 只取消匹配当前 dataset/project/association 代次的冻结 `create_tasks` 作业；任务源文件与共享来源金字塔保留。公开 Dataset/Item 删除使用 Assets 协调命令，保留关联任务、标注与学习证据，活动导入返回冲突。两种删除都只写持久 outbox，由独立清理进程轮询；API 不派发清理 Celery 消息。API 的 `Idempotency-Key` 可选 UUID，成功时回传；SDK/Web 重试应复用原 key。允许的跨域客户端可读取该响应头；production 自定义 `CORS_ALLOW_HEADERS` 时需包含 `Idempotency-Key`。

独立数据接口使用 Identity、Workspace、Assets、Jobs、Labeling、Learning、Inference、
Insights、Diagnostics、Export、Audit、Settings、Notifications 十三个无登录函数 owner；基础授权脚本另使用
`append_client`、`asset_cleanup_client` 和 `labeling_mask_gc_client` 三个独立登录角色。
其中 `asset_cleanup_client` 只可使用 `aap_assets_v1` 的八项清理函数：
storage outbox、图片垃圾、保留图片输入和保留视频输入各自的领取/确认；`append_client` 不可调用这些清理函数。
`infra/docker/module-data-grants.sql` 只向显式数据库中的既有角色授权，不创建角色或
修改密码。部署操作者先提供角色及迁移者向其转交函数的权限，再运行：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v jobs_owner=jobs_code \
  -v workspace_owner=workspace_code -v assets_owner=assets_code \
  -v labeling_owner=labeling_code -v learning_owner=learning_code \
  -v inference_owner=inference_code -v insights_owner=insights_code \
  -v diagnostics_owner=diagnostics_code -v export_owner=export_code \
  -v audit_owner=audit_code -v settings_owner=settings_code \
  -v notifications_owner=notifications_code -v workflow_role=append_client \
  -v asset_cleanup_role=asset_cleanup_client \
  -v mask_gc_role=labeling_mask_gc_client \
  -f infra/docker/module-data-grants.sql
```

连接参数由操作者在本地显式配置。该脚本拒绝带管理权限、继承 owner 或能直接读写
业务表的调用角色；不会自动缩减现有主 API 的宽权限账号。`apps/assets-cleanup` 以
专用 `ASSET_CLEANUP_DATABASE_URL`（或对应 `_FILE`）连接；API/普通 workflow 不得领取或确认
清理 outbox。该进程不需要 Redis、Celery、主 API 或 SQLAlchemy，也不使用它们的环境配置。
现有主 API/worker 迁移期仍使用原运行角色，分离迁移账号时需在迁移后
执行原环境授权流程以获取新接口执行权。

显式本地部署清理进程前，准备三个仅供此服务读取的 secret 文件：普通 PostgreSQL URL
（`postgresql://`，受限 cleanup LOGIN）、专用 S3 access key 和 secret key。S3 身份只允许
对选定 media-cache 桶的 `image-assets/*` 和 `image-pyramids/*` 执行带 prefix 条件的
`ListBucket` 与 `DeleteObject`；输入桶另仅允许 `accepted-inputs/*` 的版本列举、指定 VersionId 删除及 multipart 列举/终止，不能读写对象内容。容器用户为 10001，secret 文件必须允许该
用户读取；不要把 API 的宽权限账号写入这些文件。设置 `.env.example` 的三个
`ASSET_CLEANUP_*_FILE` 宿主路径、`ASSET_CLEANUP_BUCKET` 和容器可达的
`ASSET_CLEANUP_S3_ENDPOINT` 后运行：

清理输入副本的桶须与媒体进程完全一致。Compose overlay 从 `MINIO_INPUTS_BUCKET` 绑定；直接启动进程时显式设置 `ASSET_CLEANUP_INPUTS_BUCKET`，其值必须与 `MINIO_INPUTS_BUCKET` / `ASSETS_MEDIA_INPUTS_BUCKET` 相同，并与 `ASSET_CLEANUP_BUCKET` 不同。复用同一受限 cleanup S3 身份，按桶和前缀划定删除权限。

```bash
docker compose -p aap-assets-cleanup-dev -f docker-compose.assets-cleanup.yml up -d --build
docker compose -p aap-assets-cleanup-dev -f docker-compose.assets-cleanup.yml exec assets-cleanup python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health/ready', timeout=2)"
```

进程启动时检查数据库最小权限和 S3 prefix 可用性；数据库身份过宽会拒绝启动。分页游标
越过失败记录，下一轮从头重试。构建中的金字塔延迟到已记录 lease 之后才可领取和确认，
writer 配置要求 lease 严格长于其 hard time limit。旧 cleanup Celery task 已移除，更新后
须刷新旧 worker/beat，避免同时保留旧消费者。仅停止这个本地项目可使用上述同一
`-p` / `-f` 参数执行 `down`。

切换顺序为先迁移和授权、准备专用存储身份、启动并确认新清理进程就绪，再刷新 API
与旧 worker/beat。清理服务故障时保留 outbox 并恢复该服务，不手工标记完成。需要回退
数据库协议时，先停止相关业务写入及金字塔构建，等待已记录租约结束并由新进程清空
待处理责任，再停止新进程、回退迁移并恢复旧代码和消费者。迁移的隔离空库回退测试
不代替在途工作排空；不要在仍有未到期责任时直接删除 `not_before` 列。

上传初始化只保留 intent 并签发 import staging PUT，不再产生可被建题消费的占位 Item。
完成时按 ETag 条件拷贝到本次 UUID 路径，再在同一 SQL 命令中登记 Item、追加 Task 和
冻结回执；完成响应重放不会重新拷贝。旧的无 intent 占位记录沿用非破坏性兼容完成，
缺失或重复时不删除旧行/对象，也不补造原上传者。临时 staging 仍按 import 桶生命周期
过期；未完成 intent 和不确定提交留下的原始拷贝目前保留，需计入存储用量。

本地验收使用专属 PostgreSQL 容器、test mode 数据库、Redis 与十个隔离桶，不创建或
修改日常 PostgreSQL 的角色。运行命令及参数见
`scripts/verify_module_data_boundary.py --help`；验证器会创建 workflow、cleanup、catalog 三类受限登录角色并向测试进程注入
`AAP_MODULE_DATA_APPEND_URL`、`AAP_MODULE_DATA_ASSET_CLEANUP_URL` 和 `AAP_MODULE_DATA_ASSET_CATALOG_URL`，无需改动日常凭据。
记录该命令的 JUnit 与 JSON 结果。
它不验证生产容量，也不提供完整 assets/labeling 模块的独立部署资格。具体事务约束
见 [ADR-0092](docs/adr/0092-transactional-module-data-capabilities.md)。

### Audit 同事务追加能力

迁移 `0315` 与 `apps/audit` 发布 `aap-audit-append/v1`，固定函数为 `aap_audit_v1.append_events(jsonb)`。`AuditService.log`／`log_many`、HTTP 同步回退、原 Celery 持久化任务及筛选夹具的追加复用该能力。原始 actor／request 快照和开放 action 保留；业务追加先在原事务视图中写入，再合并干净 ORM 对象并执行原 full flush，不额外读取 Audit 私表。数据库仍分配 ID 和事务时间，业务调用方保留提交／回滚权。HTTP 元数据继续 best-effort；不存在的 actor 仍回滚后以匿名重试，没有增加 exactly-once 语义。

先安装当前迁移和 module-data owner grants。`infra/docker/audit-append-grants.sql` 仅向明确的 API 角色及独立函数客户端授予追加能力，撤销现有 Audit 父表／分区的 INSERT 和对应序列写权限，保留其他尚未迁移操作需要的权限。它会拒绝 API 拥有分区树或未来默认授权不安全的配置，不修改整个 public schema 的默认权限。现有 LAN 脚本仍把分区树交给 API，因此完整严格配置须等待维护所有权和未来分区权限收口；新增的固定函数授权只保证既有绑定可调用新代码。**排空旧 API／worker 并部署新工件后才撤销直接写权限**。

模块 Node peer 的锁定依赖按 `apps/audit/conformance/node/README.md` 安装到该目录，验收器在创建资源前检查三项 Node peer。组合冷验收复用同一 API 镜像执行真实 Audit task，覆盖相同载荷的两次投递和孤儿 actor 的匿名保存；该三次真实投递已通过组合验收；它只证明功能路径，冷 worker 的旧 Audit 分区所有权尚未收口。查询／领域事实、分析投影、分区归档及整模块资格按[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)分别跟踪。

### Audit 全局查询能力

迁移 `0317` 安装 `aap_audit_v1.query_events(jsonb)`，`apps/audit` 发布 `aap-audit-query/v1`。super-admin 的全局列表及 CSV／JSON 导出使用原 Session 调用固定 owner；项目管理员范围和其他历史事实读取继续使用原入口。总数在游标／分页前计算，导出保留 50,000 行上限以及审计提交后才返回流的顺序。公开 HTTP 参数和响应不变。

先安装匹配迁移及 module-data grants，再使用 `infra/docker/audit-query-grants.sql` 为明确 API LOGIN 和独立查询账号授权。后者只有固定查询能力，无 Audit／Identity 私表、序列及追加权限；API 尚需其他读取权限，不能提前统一撤销。`apps/audit/conformance/node` 提供同合同的 query peer，验收器在创建资源前自检。操作级真实数据库／API、Node 及相邻迁移往返已通过；完整迁移、当前新镜像与完整 Audit 资格见[唯一台账](docs-site/dev/concepts/module-deployment.md#audit-global-query-owner)。

### Audit 公开事件关系

迁移 `0320` 发布 `aap_audit_v1.audit_events` 与 `aap-audit-read-relations/v1`。项目管理员查询、标注历史、批次日志、管理看板和评审贡献者快照通过该关系组合既有范围条件；授权仍由 API 持有。关系保留 14 个原生字段，包含 PostgreSQL INET，物理行键为 `(id, created_at)`；全局查询客户端同样允许 ID 相同、时间不同的合法记录。ORM 兼容绑定仍沿用现有仅按 ID 的 identity map，不改变调用方事务及 autoflush。

先安装迁移和 `module-data-grants.sql`，再按 `infra/docker/audit-read-grants.sql` 文件头向预建 `LOGIN NOINHERIT` 只读账号授权。该账号不得有角色成员关系或数据库 TEMP 权限，只能 SELECT 公开关系，不能读取私表或调用追加函数。普通 API 另获关系 SELECT；月报尾段、DuckDB 和绩效已读取公开关系，维护及尚未迁移的消费者仍需原有权限，不能提前全局撤销。该公开关系本身不按用户过滤，可信调用方必须继续组合授权范围。

### Audit 日聚合与固定刷新

迁移 `0322` 发布 `aap_audit_v1.audit_daily_facts` 与 `aap-audit-daily-projection/v1`。月报的已完成日期读取六列聚合事实，原始尾段继续读取公开事件关系。DATE 保留日历日期；BIGINT 在 JSON 中使用无损十进制字符串，数据库仍保留原生类型。

`module-data-grants.sql` 将 `mv_audit_bi_daily`、公开聚合关系和固定无参 `refresh_audit_bi_mv()` 交给 Audit owner。并发刷新需要 owner 的数据库 TEMP 权限；只读客户端和刷新客户端均不需要 TEMP。普通 API 和只读账号只能读取事实，不能读取私有 MV 或刷新；维护调用方沿用现有迁移连接的明确 EXECUTE，保留调用方提交／回滚及维护队列。尚未提供独立维护凭据或完整 Audit 运行工件。LAN 和工作树授权在广泛授权后再次撤销 MV 的表／列权限，不能恢复旧 MV owner 绑定。

### Jobs 通知范围事实

迁移 `0323` 与 `apps/jobs` 发布 `aap-jobs-notification-scope-facts/v1`。`aap_jobs_v1.notification_scope_facts` 仅含 `id`、开放 `kind` 和 `scope_project_id`；后者保留当前项目优先、历史保留项目回退的顺序。通知筛选与批量投递读取公开关系，API 继续持有作业类型白名单、导出能力和当前成员授权；缺失作业仍 fail-closed，原 autoflush／no_autoflush 与调用方事务保持。

安装迁移及 module-data grants 后，按 `infra/docker/jobs-notification-scope-grants.sql` 文件头为明确 API 与独立只读 LOGIN 授权。只读账号不能读取 Jobs 私表、写入关系或调用 Jobs 函数。普通 API 的其他作业消费者尚需原表权限，因此不能整体撤销 `async_jobs` 读取。没有新增 DSN、服务、执行器或环境变量；父线 27 项组合案例、真实独立 LOGIN／Node 及完整 `0323→0189→0323` 已通过，新 API 镜像的文件／导入／依赖检查为离线证据，详见[唯一台账](docs-site/dev/concepts/module-deployment.md#support-daily-and-jobs-facts)。

### Jobs 私有 Celery 队列（本地实验）

可选 `aap-jobs-queue` 只接收现有 Celery 的 JSON 派发和协作撤销命令。API 的 `JOBS_QUEUE_URL` 留空时继续使用内嵌 Celery；显式配置后，远程失败不会回退。API 与 owner 使用同一服务 bearer：`JOBS_QUEUE_TOKEN` 或 `JOBS_QUEUE_TOKEN_FILE`。Owner 只持有 broker URL 与 bearer，不接收数据库或 Identity 凭据。

在本地组合启动私有容器：

```bash
docker compose -f docker-compose.yml -f docker-compose.jobs-queue.yml up -d --build jobs-queue
```

先在 Compose 环境中设置 `JOBS_QUEUE_BROKER_URL_FILE` 和 `JOBS_QUEUE_TOKEN_FILE`，分别指向只含 broker URL 与共享 bearer 的文件；本地 Redis broker URL 可用 `redis://redis:6379/0`。API 的 secret file 也必须读到相同 bearer。容器不发布宿主机端口。只有同一 Compose 网络中的 API 可通过 `http://jobs-queue:8190` 调用；宿主机热重载 API 保持内嵌模式。目前唯一接入的消费者是跨帧作业，发送一条 positional job ID、`default` 队列和预先持久化的执行 ID；独立 API／owner／worker、完整数据库恢复及恢复后新请求已通过限定本地验收，见[唯一台账](docs-site/dev/concepts/module-deployment.md#jobs-lifecycle-queue)。`accepted` 代表 broker 已接收请求，不代表任务完成；撤销使用 `terminate=False`，不代表任务已停止。其他任务载荷、自定义 Celery options、canvas 和非 JSON serializer 尚未验收，训练与资产作业也未切换。

### Notifications 偏好事务内能力

`apps/notifications` 发布 `aap-notification-preferences/v1` 合同、类型与默认值策略，以及借用 API `AsyncSession` 的 PostgreSQL 客户端。迁移 `0311` 安装 `aap_notifications_v1.preference_command(jsonb)`；GET、局部更新和通知静音检查共用该能力。owner 计算新行默认值，JSONB 原子合并保留未提供的频道及扩展键，API 仍在成功提交之后发布 `notifications.sync`，没有新增 HTTP 服务或环境变量。

先按既有 module-data 步骤安装 owner 授权，再对明确的目标数据库运行 `infra/docker/notification-preferences-grants.sql`。它向原 API LOGIN 与 `notification_preferences_client` 授予固定函数，并撤销 API 和 `PUBLIC` 对偏好表的直接权限。**先更新 API／worker 工件并排空旧 ORM 消费者，再应用严格授权**；旧镜像不能在撤权后继续读取偏好表。当前仅完成隔离的操作级验收，日常主环境尚未采用此配置。

合同、SQL／策略镜像和独立包检查见 `apps/notifications/README.md`；受限数据库、真实 API 和 Node 同合同检查复用 `scripts/verify_module_data_boundary.py` 的独占临时资源。运行前先按 `apps/notifications/conformance/node/README.md` 在该独立目录安装锁定依赖，不向共享的根 Node 目录安装；验收器会在创建数据库和容器前执行 peer 自检，缺少 Node 或依赖时直接保留前置失败记录。完整 Notifications 投递、重放和通道装配仍按模块台账推进。

### Notifications 通知追加事务内能力

迁移 `0314` 安装 `aap_notifications_v1.append_notification(jsonb)`，合同为 `aap-notification-append/v1`。`NotificationService.notify`／`notify_many` 保留既有静音预检、逐用户去重、顺序及返回的 ORM 对象，并通过该固定能力写入。追加在原完整 flush 之前执行；在 `no_autoflush` 下保留旧预检视图和待删除用户的级联顺序，不为新用户添加特殊 flush。新记录的 `created_at` 仍来自数据库事务时间；调用方控制提交／回滚和原 `defer_publish` 行为。

先安装当前迁移与 module-data owner 授权，再使用 `infra/docker/notification-append-grants.sql` 的明确数据库／角色参数。严格配置撤销 API 的通知表直接 INSERT，函数账号拥有偏好、普通追加、终态追加、已读状态和删除五项固定能力；Node 用同一连接和事务组合调用。必须先更新 API／worker 并排空旧写表消费者再撤权。普通工作树生成的运行角色已列出这些函数并检查授权，无新增服务或环境变量。回退迁移时也要匹配调用代码与旧 grants，不能让新代码调用已移除的函数。

操作级证据来自明确自有的工作树测试数据库，包含普通 API 角色及函数账号检查；它不等同于完整冷装配或整模块验收。与 Audit 的组合新工件、受限 Node 和完整迁移往返已通过，具体边界见唯一台账。收件箱查询、WebSocket、通用投递／重放和独立通道装配继续按[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。

### Notifications 作业终态原子追加

迁移 `0321` 安装 `aap_notifications_v1.append_job_terminal_notification(jsonb)`，合同与冻结 schema 为 `aap-notification-terminal-append/v1`。它按收件人、类型、`async_job` 目标和作业 ID 原子查重，返回 `created` 及数据库通知快照；只由新建者执行原有提交前即时发布。`notify_job_terminal(db, job_id=...)` 的调用入口、作业类型白名单、payload 投影和静音预检保持，长 scope 不新增大小上限。新函数不读取 Jobs 或 Identity 私表，与冻结的数据集导入通知函数共用原事务锁键。

该能力要求 `READ COMMITTED`，其他隔离级别返回 `unsupported_isolation` 且不写入。API 在借用连接的局部保存点内组合偏好、追加和回包校验，通知 SQL 失败不再静默丢失外层作业更新；保存点不会强制提交或提前 flush `no_autoflush` 的待处理状态。授权沿用上节五函数账号与普通 API／工作树角色，Node peer 复用同一数据库参数；没有新增 DSN、服务或环境变量。完整合同和运行证据见 `apps/notifications/README.md` 与[模块台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### Notifications 已读事务内能力

迁移 `0316` 安装 `aap_notifications_v1.read_state_command(jsonb)`；`aap-notification-read-state/v1` 覆盖单条和全部已读。API 仍传认证用户 ID 和原 caller UTC 时间，保留单条 404、全部更新数量、完整事务及提交后 `notifications.sync`。固定 owner 只更新该用户的未读行，回执含实际更新 ID；API 不回读私表，owner 调用后复用锁定 SQLAlchemy 的纯内存批量同步，保留新插入、脏值、过期字段及保存点回滚行为。该兼容绑定只本地编译旧语句，不执行另一条 UPDATE；升级 ORM 前须重验。

先部署匹配代码与迁移，再按上节安装 module-data、preferences 和 notification-append grants。追加函数账号拥有上节列出的五项固定能力；严格配置撤销普通 API 的通知表／列 UPDATE，owner 只可更新 `read_at`。**先更新并排空旧直接 UPDATE 消费者后才撤权**，回退也须匹配旧代码与 grants。独立测试库的 API／Node、事务回滚、旧 ORM 对照、临时类型遮蔽及完整迁移往返已通过；它不包含当前新镜像、收件箱和完整投递装配，见[已读台账](docs-site/dev/concepts/module-deployment.md#notification-read-state-owner)。

### Notifications 删除事务内能力

迁移 `0318` 安装 `aap_notifications_v1.delete_command(jsonb)`，`aap-notification-delete/v1` 覆盖单条删除和清空当前用户已读行。API 保留原 boolean／数量、404、autoflush、事务及提交后同步，删除回执不携带全量 ID；本地缓存按锁定 ORM 的删除规则同步，升级 SQLAlchemy 前须重验。通知 outbox 仍随通知级联删除，业务目标保留。

先部署匹配代码和迁移，排空旧直接 DELETE 消费者后，再应用 module-data、preferences 与 notification-append grants。普通 API 直接 DELETE 被撤销，函数客户端组合上节五项能力；回退时须匹配旧代码与授权。完整迁移和授权阶段与修复后的 Node 定点证据分别保留，原整轮失败记录不改写。详情见[删除台账](docs-site/dev/concepts/module-deployment.md#notification-delete-owner)；完整收件箱、WebSocket 与投递装配尚未完成。

### Notifications 公开通知关系

迁移 `0319` 发布 `aap_notifications_v1.notification_facts` 与 `aap-notification-read-relations/v1`，保留通知的八个原生字段。专用只读客户端与 Notifications owner 保留该关系；API 的列表／计数使用收件箱函数，单条、终态预查和投递读取使用下节固定能力，保持原 Session identity、部分／完全过期对象和 autoflush 行为。关系不提供用户授权，不能直接暴露给不可信客户端。

先部署匹配 API／worker 工件并排空旧私表读取者，再安装迁移、module-data grants 和 `infra/docker/notifications-read-grants.sql`。普通工作树和 LAN 授权源撤销 API 对事实关系及三张私表 `notifications`、`notification_preferences`、`notification_delivery_outbox` 的表／列读取权限；专用 `notification_read_client` 才通过 `infra/docker/notifications-read-grants.sql` 获得关系 SELECT。偏好、追加、已读、删除和 publisher 仍通过既有固定函数。回退时须先匹配调用代码与旧授权，不能让仍读取新关系的进程遇到关系被移除。

统一验收器分别验证只读 LOGIN、普通 API 与 LAN 授权，拒绝私表读取及关系写入，并记录四阶段 `pg_get_viewdef`、原生列类型和 owner 转移。源码 SQL 哈希与数据库反解析哈希分开保存；操作验收、当前冷工件和完整模块资格见[读取台账](docs-site/dev/concepts/module-deployment.md#support-read-relations)。

### Notifications 收件箱查询

迁移 `0324` 安装 `inbox_page(uuid,text,boolean,integer,integer)`、`inbox_total_count(uuid,text)` 和 `inbox_unread_count(uuid,text)`，合同为 `aap-notifications-inbox-query/v1`。API 在原 Identity 状态读取后传认证 actor UUID 与该次角色快照，按 page → total → unread 三条语句查询；未读接口只调用未读计数。固定函数持有原筛选、排序、分页与计数，借用原 Session，保留 autoflush／no_autoflush、ORM identity 和 caller 提交／回滚。原生 JSONB 与 HTTP 响应不变；批量投递仍保留自身的字典／键存在性语义。

先安装匹配迁移和 `module-data-grants.sql`，再对明确的数据库与角色运行 `infra/docker/notification-inbox-grants.sql`。它向原 API 和 `notification_inbox_query_client` 授予三项 EXECUTE；私有 `_visible_inbox_rows(uuid,text)` 对 API、函数账号和 PUBLIC 都不可调用。Notifications owner 只通过 Identity／Workspace／Jobs 的公开事实及固定能力组合权限，不能直接读取这些领域的私表。单条读取、批量投递和 WS 的持久行检查使用下节固定能力，API 对公开通知关系也没有表／列 SELECT。函数账号 DSN 仅用于隔离验收，不新增应用环境变量或服务。

统一 verifier 验证实际 LOGIN、Node 原生行及 bigint 字符串、API／LAN 调用和完整迁移往返；权限负向探针在事务内故意增加 owner 成员或 PUBLIC EXECUTE，要求实际 guard 拒绝后完整回滚。源码、操作资格与冷工件状态见[收件箱台账](docs-site/dev/concepts/module-deployment.md#notification-inbox-owner)。日常主环境未切换，回退时须匹配调用代码、迁移和授权。

### Notifications 按 ID 读取与投递授权

迁移 `0325` 与 `aap-notifications-delivery-read/v1` 提供七项只读能力：`read_notification(uuid)`、`find_terminal_notification(uuid,text,uuid)`、`delivery_job_scopes(uuid[])`，以及接收成对 UUID JSON 数组的 `delivery_member_pairs`、`delivery_reviewer_pairs`、`delivery_owner_pairs`、`delivery_super_pairs`。投递纯策略由 Notifications 包持有；Jobs 只在实际目标存在时查一次，四项授权查询保留原有次序、独立语句快照和 ORM autoflush。JSONB 非对象、字典键存在性、宽容的 UUID 解析、实际 Jobs 范围与异常后 fail-closed 规则保持，不能共用收件箱的 SQL `->>` 规则。

按 ID 读取保留调用方 Session 的缓存短路、脏值、部分／完全过期和缺失 identity 清理，终态预查保留原 flush／savepoint／no_autoflush 及短路顺序。普通提交后发布仍是 best-effort，`publish_by_id` 的 Redis 错误继续向 durable relay 传播；WS 的四次缺行查询及取消清理不变。

先部署匹配调用代码并排空旧直接关系读取者，再安装迁移、module-data 和 `infra/docker/notification-delivery-read-grants.sql`。该脚本为 API 与 `notification_delivery_read_client` 授予七项固定函数并撤销 API 的事实关系表／列读取；普通 worktree 和 LAN 授权源同步执行相同限制。`notification-inbox-grants.sql` 要求 API 无该读取权限；`notifications-read-grants.sql` 只用于专用只读关系客户端，不能再向 API 重授 SELECT。回退时也要先匹配代码、迁移与旧授权。

Node peer 使用 `NOTIFICATION_DELIVERY_DATABASE_URL`、`_USER_ID`、`_NOTIFICATION_ID`、`_EXPECTED_ROLE` 及可选 `_PROJECT_ID`／`_JOB_ID`，全部只由隔离验收器提供，不新增应用环境配置或服务。真实受限账号、外部提交撤权、原 ORM 对照、当前冷工件及整模块资格见[投递读取台账](docs-site/dev/concepts/module-deployment.md#notification-delivery-read-owner)。

### 独立 Notifications WebSocket

`apps/notifications/Dockerfile.socket` 安装 `aap-notifications[websocket]`，入口为
`aap-notifications-socket`。服务持有 JWT 准入、单独的当前账号活动检查、逐帧认证及
投递授权、Redis 订阅、30 秒心跳与取消清理；原 publisher 命令仍单独运行。
API 设置 `NOTIFICATION_SOCKET_URL=ws://notifications-socket:8180` 后，将原
`/ws/notifications` 的文本和二进制消息直接转发给它。空值使用同一 owner 的内嵌
装配；显式地址失效或拒绝握手时关闭连接，不回退本地 DB／Redis。SDK、Web 与反代
继续使用 API 原地址。新服务 CLI 禁用访问日志，避免记录 query 中的用户 JWT。
托管工作树清空继承的 `NOTIFICATION_SOCKET_URL`，避免误连共享 owner；独立验收夹具
显式注入自己私网内的地址。

独立服务只需要三组秘密，每组直接值与 `_FILE` 二选一：

- `NOTIFICATION_SOCKET_DATABASE_URL`：先应用既有 `0325` 和
  `notification-delivery-read-grants.sql`，使用仅七函数的 LOGIN，无私表或视图读取。
- `NOTIFICATION_SOCKET_REDIS_URL`：只允许 `notify:*` 的 SUBSCRIBE／UNSUBSCRIBE
  和连接所需 PING／SELECT／CLIENT SETINFO；不授予 PUBLISH、key 或其他 channel 权限。
- `NOTIFICATION_SOCKET_IDENTITY_TOKEN`：与 Identity 通用控制 token 不同的
  admission-only bearer。`NOTIFICATION_SOCKET_IDENTITY_URL` 是私网 HTTP(S) 根 origin。

Identity 配置可选 `IDENTITY_ADMISSION_TOKEN(_FILE)`，仅接受该 bearer 调用
`/v1/identity/authenticate` 和 `/v1/identity/actor-status`。状态读取另设
`IDENTITY_ACTOR_STATUS_DATABASE_URL(_FILE)`，使用
`identity-actor-status-grants.sql` 的 `identity_actor_status_client`，只获得既有
`read_actor_facts(uuid)`。它不扩展原 AUTH 五函数，不取锁或核对 admitted role；
缺失账号返回 null，未配置／错误的状态数据源返回 503，不回退其他连接。

可选 `docker-compose.notifications.yml` 与 `docker-compose.identity.yml` 放在同一
Compose 私网，socket 不发布宿主端口。前者使用上述三组 `_FILE`；后者另用宿主
selector `IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE` 与
`IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE`。直接启动默认监听
`127.0.0.1:8180`，可设 `NOTIFICATION_SOCKET_HOST`／`PORT`；镜像固定监听容器内部
8180。`/health/live` 检查进程，`/health/ready` 检查数据库与 Redis，Identity 故障
由握手／逐帧检查拒绝。独立冷运行与恢复资格仍按模块台账验证，日常环境未切换。

### Notifications 实时消息合同

独立即时发布使用现有 `aap-notifications-publisher` 进程。配置
`NOTIFICATION_PUBLISH_API_TOKEN(_FILE)` 后，同一进程在 `NOTIFICATION_PUBLISH_HOST`
与 `NOTIFICATION_PUBLISH_PORT` 提供私网发布和健康接口；不配置 token 时保留原
durable-only 循环，`--once` 仍只处理一页耐久消息后退出。
`docker-compose.notifications-publisher.yml` 只装配这个已有工件，可与 socket 的
Compose 放在同一私网；使用它替换兼容发布服务，保持同一 outbox 的服务装配明确。
HTTP 端口不发布到宿主机，服务只接收独立发布 bearer、原有函数 DSN 和 publish-only
Redis 凭据，不持有 Identity 控制或签名密钥。

API 可设 `NOTIFICATION_PUBLICATION_URL=http://notifications-publisher:8181`，并通过
`NOTIFICATION_PUBLICATION_TOKEN(_FILE)` 读取同一发布 bearer。两组 token 的直接值和
文件均二选一，文件只挂载给对应 API／publisher。空 URL 使用原内嵌装配；显式选择
远程 owner 后，错误不回退本地 Redis。工作树清空继承的 URL 和 token 选择器，验收
夹具只注入自己拥有的地址。该 HTTP 入口使用版本化即时发布合同，不创建普通通知
的 outbox 或自动重试；普通 best-effort、终态提交前发布与按 ID 发布的错误传播
分别保留。事务内的查找、缓存与四次授权事实读取仍由调用方 Session 适配，publisher
使用受信调用方提供的快照，不另开连接读取未提交行。

`apps/notifications` 发布 `aap-notification-realtime/v1` 和 `realtime_event.schema.json`，API 与 durable publisher 共用六个业务事件字段的构造，保留各自的日期字符串和 JSON 编码。合同不向既有帧增加 schema 字段，业务 type 与 sync reason 保持开放；原生 JSONB 的对象、数组、标量与 null 均可保留。通用 ping 仍属于 WebSocket 层。

普通通知仍由调用方提交后 best-effort 发布，终态 job 保留原提交前路径，durable publisher 保留 lease／重试／ack。WS 继续逐帧认证与当前投递权限检查，缺失行保留四次查询及三次 150ms 等待；公开消息合同不会收紧既有 WS gate。订阅被取消或退订失败时也会关闭 PubSub。

Node peer `apps/notifications/conformance/node/realtime_event_provider.mjs --stdio` 校验 stdin 的单条事件／sync 并原字节回显，不读取连接凭据。真实本地 socket 案例 `tests/test_notifications_socket_live.py` 通过现有 module-data verifier 运行，要求独占临时 PostgreSQL／Redis，分别使用受限 API、认证角色和单频道订阅 ACL。它覆盖原始事件、Node 帧、历史 JSONB、提交可见性、撤权及断连／handler 取消；既有 8 项组合案例继续复用；当前冷工件、即时发布、受限 API Redis 及完整数据库恢复已通过一次限定本地组合验收，见[发布与恢复台账](docs-site/dev/concepts/module-deployment.md#notification-publication-owner)。源码检查、实际运行与整模块资格见[唯一台账](docs-site/dev/concepts/module-deployment.md#notification-realtime-wire)。

### 视频输入回收的保留前提

视频输入保留迁移 `0312` 通过 Jobs 事实让 Labeling 保留未退役及未知旧执行的输入，并保留现有 pin、审核宽限期和已证明安全的释放。`0313` 还在领取已有垃圾前缀时重新核对当前输入引用；保留项按本次租约时长延后，已被新一代替代的前缀继续清理。升级前先停止全部 `apps/assets-cleanup` 进程并确认退出，盘点前缀与租约，等待剩余租约到期后再恢复清理；无法确认进程退出或在途 S3 请求结束时，须先隔离对象删除凭据。默认清理租约为 600 秒，Native claim 接受 60–7200 秒；租约到期本身不能证明物理删除已停止。

两项迁移均不恢复历史输入的 expired 状态或已删除的对象版本。回退 `0313` 会恢复旧领取路径，回退 `0312` 还会恢复旧保留判断，均须按相同停机／围栏条件处理。定向验收和进程内取消测试不代替实际部署的停机及凭据围栏验证。日常主环境尚未采用这两项迁移，旧输入修复与无账本退役入口仍按[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)跟踪。

### Workspace 配置值与依赖能力

API 的 locked sync 会安装本仓库 `apps/workspace` 和 `apps/inference`，不要从旧 API schema/service 路径导入已迁移的配置或预标注值。配置／模板模型与纯 kind、克隆和规范化策略位于 `aap_workspace`；预标注、能力词表／归一化和保存编排校验位于 `aap_inference`。API 的 `inference_limits` 适配器显式传入当前 tracker 设置。

迁移0255与 `module-data-grants.sql` 配套安装四项跨 owner 配置依赖，仅向 `workspace_code` 开放，不能直接授权主应用角色调用。配置写入继续使用 API 原事务；当前没有独立配置 endpoint、LOGIN、URL 或 DSN 选择器。其余 Workspace owner 的部署命令保持各自范围。边界与后续工作见 [ADR-0135](docs/adr/0135-workspace-configuration-values-and-dependencies.md)。

### Workspace 项目成员生命周期 owner

项目成员新增、角色预览／变更和移除使用 `aap-workspace-membership/v1`，内部私有 endpoint 为 `POST /v1/workspace/membership/execute`。业务策略仍在便携 Workspace manager；Native prepare/apply 在同一 PostgreSQL 事务中读取并锁定当前事实、执行 Labeling 职责交接、更新成员、写固定 Audit 和成功回执。原公开 API、DTO、权限依赖、错误映射和提交边界保持不变；邀请接受继续使用既有 Identity owner。

API 默认保留现有 caller Session；`WORKSPACE_MEMBERSHIP_DATABASE_URL(_FILE)` 可选择本地专用事务数据连接，`WORKSPACE_MEMBERSHIP_URL` 可选择私有 Workspace runtime。远程调用必须配置独立 `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN(_FILE)`，API timeout `WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS` 默认 30 秒；显式选择的 URL/DSN 失败不回退另一种 owner。独立 runtime 必须配置其 `WORKSPACE_MEMBERSHIP_DATABASE_URL(_FILE)` 和 control token，默认监听 `0.0.0.0:8172`、请求超时 30 秒、最多 8 个并发请求。Compose 宿主机通过 `WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE` 与 `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` 选择 secret 文件。运行合同、私有错误 envelope、窄 SQL grants 与当前范围见[Workspace runtime guide](docs-site/api/guides/workspace-runtime.md)。

成员命令切片已通过隔离本地的受限权限、独立包／进程／镜像、真实 API、并发交接、重启与备份恢复验收，详见 [ADR-0133](docs/adr/0133-workspace-membership-lifecycle-owner.md)。Native 的 `opaque_text_codec` 在执行事务内校验，迁移与进程需一起更新。它不迁移 Project 创建／转移、配置、模板、列表或 mention，也不完成整个 Workspace/Identity、另一语言提供方、生产容量验证或 primary cutover。

## 独立 Assets 目录服务

`apps/assets-catalog` 提供目录列表、详情、文件分页、公开创建/更新、数据集与项目的双向关联列表，以及 Scene、位姿、标定和点云素材准备。数据集关联写入、建题、
导入、上传和来源删除继续使用自己的既有事务合同；不要把需要保留调用方锁的内部
`DatasetService` 操作替换为 HTTP。完整范围见[目录协议](docs-site/api/guides/assets-catalog.md)和[空间素材协议](docs-site/api/guides/assets-spatial.md)。

先应用迁移并完成上面的 module-data owner 授权，再创建一个无管理权限、无角色成员资格、
不拥有业务表的独立 `asset_catalog_client` LOGIN，运行专用授权脚本：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v assets_owner=assets_code -v asset_catalog_role=asset_catalog_client \
  -f infra/docker/assets-catalog-grants.sql
```

这里的数据库和连接参数应换成明确选择的环境；不要把日常 API 的宽权限账号当作目录
LOGIN。该 LOGIN 只可执行 27 个目录、关联与空间素材函数，不可读取表、序列或其他 owner schema。启动时的
权限检查会拒绝过宽身份。迁移期主 API 仍保持既有角色，未因此获得整体隔离资格。

目录服务使用独立的四个 secret：数据库 URL、Bearer token、S3 access key、S3 secret key。
容器通过 `.env.example` 对应 `_FILE` 变量选择宿主文件，用户 10002 必须可读。S3 身份仅允许
datasets 桶的 `GetObject` 和 media-cache 中 `thumbnails/`、`videos/`、`playback/`、
`image-pyramids/` 的 `GetObject`，不允许列桶、Put/Delete 或桶管理。原始文件可能使用历史
数据集名称路径，不能只授予新的 `dataset-sources/` 前缀。桶须预先准备，普通创建不写目录标记。

配置浏览器可达的固定签名 origin 及两个桶后，显式启动本地项目。空间素材准备还从 datasets 桶只读标定 JSON；`ASSETS_CATALOG_S3_ENDPOINT` 可另设服务可达 origin，留空沿用公开 origin：

```bash
docker compose -p aap-assets-catalog-dev -f docker-compose.assets-catalog.yml up -d --build
docker compose -p aap-assets-catalog-dev -f docker-compose.assets-catalog.yml exec assets-catalog python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/ready', timeout=2)"
```

该 Compose 默认不发布宿主端口。网关与目录服务应在可互通的容器网络中运行；宿主 API
联调可在本机用 `uv run --project apps/assets-catalog --extra server python -m aap_assets_catalog`
启动目录服务，并为该进程显式传入专用配置，再将网关指向 `http://127.0.0.1:8766`。
目录进程不读取主 API 的通用数据库或 AWS 凭据。

网关的 `ASSETS_CATALOG_URL` 留空使用同一原生 SQL 合同；设置 URL 后还需设置实际
`ASSETS_CATALOG_TOKEN`，独立服务的同名 secret 可用 `_FILE`。刷新网关进程使配置生效。
远程不可用时返回错误，不切换成本地写入；HTTP 各 I/O 阶段默认 5 秒，上限 60 秒。
内部请求/响应预算分别为 1/16 MiB，超出明确失败，不截断数据。

先暂停受影响写入，将 SQL 升至 0216 并重跑 owner/目录授权，升级目录镜像，确认 `/health` 和 `/ready` 后再切换网关配置。旧镜像的权限预检不接受新的 27 项角色，需协调升级。创建、更新、Scene PATCH、场景回填和标定 PATCH 使用保存的
UUID `Idempotency-Key`，结果不确定时以原内容和原键重放；回执保留原快照，重复创建不会
生成第二个数据集。回退服务实现可先清空网关 URL、重启网关并保留兼容 SQL 和回执，
再停止同一 `-p` / `-f` 的独立项目。若要回退迁移，必须先停止目录写入并处理仍需确认的
请求；删除回执会丢失重放依据。目录、来源和空间素材回执目前没有自动清理。

`verify_module_data_boundary.py` 在自有 PostgreSQL、Redis、桶和专属只读 S3 身份上验证
真实目录容器、公开 API 消费、响应丢失、重启和本地/远程切换；不修改日常角色，不提供
生产容量或完整 Assets 语言替换资格。Go 月报仍是单独实验，不参与默认运行。

## 独立 Audit 维护运行时

`apps/audit` 提供 `aap-audit-maintenance`，仅按明确命令执行一次操作；分区创建、
快照／退役、归档事件和日聚合刷新由固定 `maintenance_command(jsonb)` 能力持有。
原 API／Celery 适配器借用调用方事务，独立 CLI 自己提交或回滚。

```bash
uv run --project apps/audit --locked --extra runtime aap-audit-maintenance ensure-partitions
uv run --project apps/audit --locked --extra runtime aap-audit-maintenance refresh-daily
# 完成独立归档桶凭据和保留范围核对后，显式执行归档
uv run --project apps/audit --locked --extra runtime aap-audit-maintenance archive-partitions
```

CLI 使用 `AUDIT_MAINTENANCE_DATABASE_URL(_FILE)` 的单函数 LOGIN；归档另需独立
`AUDIT_MAINTENANCE_ARCHIVE_S3_*`，只检查已配置桶，不创建桶。可选
`docker-compose.audit.yml` 使用文件 secret 和 `maintenance` profile，不自动运行归档。
API 的 `AUDIT_ARCHIVE_READ_S3_*` 只配置归档读取凭据；配置的 endpoint 留空时保持
兼容绑定，显式配置失败不回退。环境变量见 `.env.example`，授权与迁移顺序见
[Audit 运行时](docs-site/api/guides/audit-runtime.md)。本轮未切换日常维护调度。

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

### Workspace 项目负责人转移 owner

`POST /api/v1/projects/{project_id}/transfer` 的公开合同不变。默认数据绑定继续借用调用方事务，
不自行 commit：账号锁错误沿用显式 rollback，项目锁冲突保留失败事务，直到调用方 rollback。
专用 DSN／HTTP owner 拥有自己的事务，不能跨进程提交 API 中无关的 pending work。

API 可选 `WORKSPACE_OWNER_TRANSFER_URL`，或二选一的
`WORKSPACE_OWNER_TRANSFER_DATABASE_URL`／`WORKSPACE_OWNER_TRANSFER_DATABASE_URL_FILE`；
显式选择失败不回退。复用 `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN(_FILE)` 和
`WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS`。私有接口为
`POST /v1/workspace/owner-transfer/execute`，封闭协议是 `aap-workspace-owner-transfer/v1`。
同一个私有 operation UUID 仅在传输失败时最多重试一次；业务 409 不重试。

独立 runtime 可只配置转移 DSN、只配置成员 DSN，或同时配置；ready 检查所有已配置能力。
独立 Compose 文件 `docker-compose.workspace-owner-transfer.yml` 使用宿主端口 8173，
secret 选择器为 `WORKSPACE_RUNTIME_OWNER_TRANSFER_DATABASE_URL_FILE` 和
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`，不改变原成员 Compose 所需 secret。
受限 LOGIN `workspace_owner_transfer_client` 仅有 policy 和 transfer 两项 RPC。
授权脚本在 `infra/docker/workspace-owner-transfer-grants.sql`，迁移与授权须指向同一数据库。

Node conformance provider 只用于本地替换实验，Native 持有业务策略与原子写入；
它不代表整个 Workspace 已能替换或已选定生产实现语言。部署与失败语义见
[Workspace 运行指南](docs-site/api/guides/workspace-runtime.md)。

### Workspace 配置命令

配置命令的独立服务实现见 [ADR-0136](docs/adr/0136-workspace-configuration-command-owner.md)。
六项公开配置路由已接入配置 owner，默认仍借用调用方事务；独立连接、进程、受限镜像、恢复与本地并发执行已完成限定验收。项目／模板读取投影已由各自查询 owner 提供；其余 Workspace 入口继续迁移。
类别改名已接入同一端点；独立提供方的同名操作需要在持锁事务内完成项目投影，
使用原 Compose 加 `-f docker-compose.workspace-class-rename.yml`，补充现有的
`WORKSPACE_RUNTIME_PROJECT_QUERY_DATABASE_URL_FILE` 与
`WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE` 两份 secret。
该操作没有持久回执，传输失败不自动重发；原六种操作保留既有重试。
三路径、独立安装包、受限镜像、重启、恢复与完整迁移已通过限定本地验收，见 [ADR-0143](docs/adr/0143-workspace-project-class-rename-owner.md)。
私有端点为 `POST /v1/workspace/configuration/execute`；独立运行使用
`WORKSPACE_CONFIGURATION_DATABASE_URL(_FILE)` 和共享 control token。
`docker-compose.workspace-configuration.yml` 仅绑定本机端口8174，宿主 secret
选择器为 `WORKSPACE_RUNTIME_CONFIGURATION_DATABASE_URL_FILE` 与
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`。运行时的
`WORKSPACE_CONFIGURATION_SETUP_TIMEOUT_SECONDS` 和三个视频参数应与 API
的 `ML_HEALTH_TIMEOUT`、`VIDEO_SEGMENT_SIZE_FRAMES`、
`VIDEO_TRACKER_WINDOW_SIZE_FRAMES`、`VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES`
一致；Compose 已映射这些值。API 可通过 `WORKSPACE_CONFIGURATION_URL`／
`WORKSPACE_CONFIGURATION_DATABASE_URL(_FILE)` 选择 HTTP／专用数据连接，显式失败不回退；
`WORKSPACE_CONFIGURATION_TIMEOUT_SECONDS` 默认30秒，默认借用事务不新增超时。

### Workspace 模板查询

模板列表／详情和创建、更新、复制后的响应投影使用 `aap-workspace-template-query/v1`。
默认借用调用方事务，保留自动刷新与提交边界；可选
`WORKSPACE_TEMPLATE_QUERY_DATABASE_URL(_FILE)` 或 `WORKSPACE_TEMPLATE_QUERY_URL`，
两者不能同时配置。显式失败不回退，远程超时由
`WORKSPACE_TEMPLATE_QUERY_TIMEOUT_SECONDS` 控制，默认30秒。

独立服务可只配置模板读取 DSN，复用 Workspace control bearer。私有端点为
`POST /v1/workspace/template-queries/execute`，专用账号
`workspace_template_query_client` 只有 policy 与 step 两项函数权限，并在只读事务中执行。
`docker-compose.workspace-template-query.yml` 绑定本机8175，使用
`WORKSPACE_RUNTIME_TEMPLATE_QUERY_DATABASE_URL_FILE` 和
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` 两份宿主 secret 文件。

配置写入仍先提交，再用私有成功回执读取响应；查询故障不能撤销已提交的配置。
公开 DTO 校验继续在 API 完成。源码、三路径响应对照、独立安装包、受限镜像、
故障恢复与0259完整迁移均已完成限定本地验收，详见 [ADR-0137](docs/adr/0137-workspace-template-query-owner.md)。

### Workspace 项目查询

项目列表、分页、详情、统计、就绪度、访问上下文、写入后的响应投影及项目搜索使用
`aap-workspace-project-query/v1`。默认借用调用方事务；
`WORKSPACE_PROJECT_QUERY_DATABASE_URL(_FILE)` 和 `WORKSPACE_PROJECT_QUERY_URL`
分别选择专用连接与 HTTP owner，不能同时配置。显式失败不回退，远程截止时间由
`WORKSPACE_PROJECT_QUERY_TIMEOUT_SECONDS` 控制，默认30秒。

独立运行时使用 `workspace_project_query_client` 的 policy/step 函数权限和只读事务，
私有入口为 `POST /v1/workspace/project-queries/execute`。非空项目响应还需要显式的
Identity Briefs 依赖：`WORKSPACE_PROJECT_QUERY_IDENTITY_URL` 及独立 token/file，
或 `WORKSPACE_PROJECT_QUERY_IDENTITY_BRIEFS_DATABASE_URL(_FILE)`；HTTP 优先。
空列表、统计和就绪度不初始化资料依赖。资料故障保留独立的503错误文案。

可选 `docker-compose.workspace-project-query.yml` 绑定本机8176，读取三份宿主 secret：
`WORKSPACE_RUNTIME_PROJECT_QUERY_DATABASE_URL_FILE`、
`WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE`、
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`。选定查询能力已完成三路径响应对照、安装包、受限镜像、
恢复和完整迁移的本地验收；有限容器采样不代表生产容量，具体范围见
[ADR-0139](docs/adr/0139-workspace-project-query-owner.md)。

`GET /api/v1/projects/{project_id}/access` 使用同一个查询配置，保留可见性检查后
第二轮当前账号／项目／成员读取，最终成员职责与版本来自同一条成员记录。
该操作不访问显示资料，独立进程无需为它配置 Identity Briefs。
原本地读写解析器仍供其他工作流使用，只复用 Workspace 的纯权限策略，锁序不变。
部署此扩展时先应用当前迁移，再刷新 Workspace owner/API 工件；旧服务不支持新操作时显式失败，不回退。
访问上下文扩展已通过三路径、独立安装包、受限镜像、重启、恢复与完整迁移的限定本地验收；具体证据与范围见 [ADR-0141](docs/adr/0141-workspace-project-access-query.md)。

### Workspace 项目成员目录查询

成员列表与讨论区提及候选使用 `aap-workspace-directory-query/v1`，公开接口不变。
默认借用调用方事务，保留 autoflush／no_autoflush；可选
`WORKSPACE_DIRECTORY_QUERY_DATABASE_URL(_FILE)` 或 `WORKSPACE_DIRECTORY_QUERY_URL`，
两者不能同时配置。`WORKSPACE_DIRECTORY_QUERY_TIMEOUT_SECONDS` 默认30秒，
只约束显式独立调用；失败不回退。

私有入口是 `POST /v1/workspace/directory-queries/execute`，复用 Workspace control bearer。
独立进程使用 `workspace_directory_query_client` 的两项函数权限和只读事务。
非空成员列表需要既有 `WORKSPACE_PROJECT_QUERY_IDENTITY_*` 资料配置；提及候选使用
Identity 的窄账号事实，不初始化 Briefs。空成员列表同样不访问 Briefs。

`docker-compose.workspace-directory-query.yml` 只发布本机8177，读取
`WORKSPACE_RUNTIME_DIRECTORY_QUERY_DATABASE_URL_FILE`、
`WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE` 和
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`。先迁移到当前 head，再对同库应用
`infra/docker/module-data-grants.sql` 与 `infra/docker/workspace-directory-query-grants.sql`。
共享 PostgreSQL、独立连接的快照差异和剩余范围见
[ADR-0140](docs/adr/0140-workspace-directory-query-owner.md)。
三路径响应对照、独立安装包、受限 owner/API 镜像、故障恢复、备份恢复和完整迁移
已通过限定本地验收；完整 Workspace、另一语言提供方和生产容量仍未完成。

### Workspace 同事务导入与质量配置

nuScenes 导入与点云质量评估晋升通过 `aap-workspace-project-maintenance/v1` 的三项
Native 数据能力写项目；调用方继续拥有原外层事务，不新增 HTTP 进程或运行时 DSN
selector。导入保持稳定 display ID 和原始唯一／长度／外键错误；晋升保留原权限重验、
Project 锁、评估和 Audit，失败一起回滚。Labeling 的计数列继续由其 owner 持有。

先安装当前迁移及 `module-data-grants.sql`，再为预建、无角色成员关系的 LOGIN 应用
精确授权。普通应用／工作树连接仍使用原来的连接与显式函数清单：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v workspace_owner=workspace_code \
  -v workspace_project_maintenance_role=workspace_project_maintenance_client \
  -f infra/docker/workspace-project-maintenance-grants.sql
```

这个数据账号有六项业务 Native 执行权，包含下述三项开发种子写入，没有私表／列、
序列、DDL 或 TEMP 权限；系统及历史 public invoker helpers 不计入业务能力。它不承载完整导入工作流的其他
领域权限。复验使用验收器拥有的新 PostgreSQL／Redis／桶：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-project-maintenance.json \
  --test-file tests/test_workspace_project_maintenance_native.py \
  --test-file tests/test_point_cloud_quality_evaluation.py \
  --test-file tests/test_import_nuscenes_lite.py
```

合同与适配器检查、实际受限 LOGIN、完整导入默认值、锁等待、原始 SQLSTATE、Node
直接调用和整事务回滚已通过本地定向验收，详见
[ADR-0164](docs/adr/0164-workspace-transactional-project-maintenance.md)。完整 Workspace
冷安装／容器／完整迁移／恢复仍在一致模块里程碑集中验收，日常主环境未切换。

### Workspace 开发点云种子

`scripts/seed_pointcloud.py` 的 Project 读取、目录种子创建和 nuScenes 重设通过固定
Workspace 能力执行。目录模式默认关闭 scene，保留建题内部提交；nuScenes 模式默认
开启 scene，并保留调用方提交及提交后旧对象清理。API/worker 普通消费者配置不追加
运维写入。受信任运维连接在普通 Workspace 围栏后显式组合：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v workspace_owner=workspace_code -v consumer_role=legacy_runtime \
  -f infra/docker/workspace-seed-operator-grants.sql
```

该示例数据库必须由当前任务拥有，不能用于日常开发库的测试重置。此配置追加两项
固定 Project 写入及一项演示成员写入，其他领域沿用已有运维权限，不能据此认定整个种子已使用最小凭据。
受限数据库复验：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-seed-native.json \
  --test-file tests/test_workspace_seed_operator_native.py
```

冻结旧行为涵盖完整默认值、已有项目先跳过缺失夹具、三次目录内部提交和 nuScenes
调用方回滚，以及 changed/equal/missing 与 autoflush 两种设置。Node 直接调用、原始
数据库错误、缓存过期字段与加载中本地修改也有定向检查。独立冷运行证据与完整模块
剩余项见[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### 主种子账号与演示成员

`seed.py` 自身的账号创建和演示成员写入分别使用固定 Identity／Workspace SQL 能力，
保留原调用方事务、已有账号完整复用及有变更才提交成员的行为。可信运维连接先应用
两个普通消费者围栏，再应用上节 Workspace 追加配置及 Identity 追加配置：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=legacy_runtime \
  -f infra/docker/identity-seed-operator-grants.sql
```

不要把追加配置用于 HTTP／worker 账号。受限数据库复验只使用验收器创建的自有资源：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/seed-parent-native.json \
  --test-file tests/test_seed_parent_operator_native.py
```

22 项旧行为案例已在拒绝 User／Project／Member 私表的凭据下复验；另有 Node 跨 owner
事务和四项失败后再次提交检查。主脚本的媒体子入口仍单独跟踪，完整状态见
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### 门控 E2E 路由的 Identity 边界

`__test/seed` 仍只在非 production 且显式开启 `E2E_SEED_ENABLED` 时挂载；每个请求
再核对实际数据库名，按原规则转小写后检查 `_test`／`_e2e` 后缀。普通 API 账号不
获得测试写入能力。迁移到当前 head 后，可信测试运维 LOGIN 先应用普通围栏，再追加
固定的七函数与完整资料关系：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=e2e_operator \
  -f infra/docker/identity-consumer-grants.sql
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=e2e_operator \
  -f infra/docker/identity-e2e-fixture-grants.sql
```

两个角色必须已存在：Identity owner 为非 LOGIN，operator 为无管理／继承权限的 LOGIN。
追加配置拒绝普通 `identity_api_consumer` 及仍有 Identity 私表／列权限的角色。其他领域
继续使用各自可信测试运维权限；这些入口不是一般账号管理 API。资料关系保留登录
`UserOut` 的 21 字段，原 Session 缓存、精确邮箱及 peek 排序保持。写入和清理在调用方
事务内执行，含组织成员和密码重置 token 依赖；没有独立提交、自动重试或新增审计。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-e2e-native.json \
  --test-file tests/test_identity_e2e_replay.py \
  --test-file tests/test_identity_e2e_native.py \
  --test-file tests/test_gpu_registry_delete_noop_native.py
```

27 项不同定向检查及 64 项既有路由／隔离回归通过。源码检查 393 项通过、150 项既有
架构规则跳过；工具检查 58 项及 OpenAPI 核对通过。原始基线与失败收据保留，不相加
作为整模块覆盖率。API 镜像固定携带原 `smart-point.webm`，源码环境继续使用文档目录
内同一文件。GPU 清理前置修复只跳过不存在围栏的空 DELETE；围栏存在时仍执行原权限
与 GC 证明守卫。

按 Workspace 组合私网验收表设置四项冷镜像及身份变量后，可验证安装后的原始 HTTP：

```bash
AAP_IDENTITY_E2E_FIXTURE_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-e2e-cold.json \
  --test-file tests/test_identity_e2e_live.py
```

此显式测试使用验收器创建的独占桶 IAM 身份，只追加固定 E2E 对象前缀与 mask 内容
前缀；其他 API bootstrap 验收的默认元数据策略保持（对象读写仍拒绝）。八次独立
API 容器运行已通过，含真实 HTTP、完整启动生命周期、撤权失败、取消 137、实际
后端释放、回滚、恢复，以及邻居／token／对象版本清理。安装核对为 owner 288、
API 1,322、Inference 88、Assets-source 154 个文件。
收据见 `.worktree/identity-e2e-qualification.json`，整体状态按
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)记录，日常主环境
不因本地验收切换。

### 截图夹具的 Identity 边界

作业恢复／拒绝夹具读取精确 email 对应的账号 ID，不新增角色或启用过滤；已有项目、
数据集来源及任务路径守卫保持。偏好恢复仍先校验 URL 的 `_test`／`_e2e` 后缀与 JSON
object，再完整替换账号偏好。公开偏好接口会深合并，不能代替此恢复操作。

迁移到当前 head 后，可信录制 LOGIN 先应用 `identity-consumer-grants.sql`，再显式
追加一项固定写入能力。以下命令仅用于自己拥有的隔离数据库：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=recording_operator \
  -f infra/docker/identity-recording-fixture-grants.sql
```

该 LOGIN 必须无管理／角色继承权限，且已撤销 User 私表和列权限；普通 API 角色不能
作为录制 operator。实际数据库名也必须以 `_test` 或 `_e2e` 结尾。运行原脚本时配置
`DATABASE_URL`，偏好恢复也接受优先级更高的 `SCREENSHOT_DATABASE_URL`。其他领域的
夹具写入仍需要其既有可信权限，本步骤只收口 Identity 操作。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-recording-native.json \
  --test-file tests/test_identity_recording_native.py \
  --test-file tests/test_identity_fixture_preferences_native.py
```

修改前捕获的 37 项行为已通过受限凭据回放，另有 5 项 Node、共同回滚、真实行锁和
实际数据库名检查。参照 Workspace 组合私网验收表设置四项镜像及身份变量后，可运行
安装后的三项原始 CLI：

```bash
AAP_IDENTITY_RECORDING_FIXTURE_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-recording-cold.json \
  --test-file tests/test_identity_recording_live.py
```

一个冷案例的 23 次原始 CLI 容器调用已通过，包含权限撤销／恢复、物理取消 137、
真实后端释放、未提交值回滚及恢复。owner／API 工件分别匹配 286／1,318 个文件。
收据见 `.worktree/identity-recording-qualification.json`，整体状态按
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)记录。这不替代实际
浏览器录制、ML 推理或完整录制工作流验收；旧媒体资产和人工审阅来源不被重写。

### 历史角色审计的 Identity 与 Workspace 读取

`audit_project_roles.py` 通过 `aap_identity_audit_v1` 的三个固定关系读取账号角色、
邀请审计字段和准备列事实；通过 `aap_workspace_audit_v1` 的三个窄关系读取项目 ID／
所有者／状态、成员 ID／项目／账号／原职责以及成员准备列事实。它保持单一 `REPEATABLE READ, READ ONLY` 事务，未安装
关系时返回退出码 2，不建表、不修改 revision，也不回退到 User／邀请／项目／成员私表。

在迁移前数据库上，由明确的迁移账号安装只读能力；以下角色必须已由维护者准备：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=role_auditor \
  -f infra/docker/identity-role-audit-grants.sql
psql -v expected_database=annotation_test -v migration_role=owner \
  -v workspace_owner=workspace_code -v consumer_role=role_auditor \
  -f infra/docker/workspace-role-audit-grants.sql
```

`identity_owner` 和 `workspace_owner` 是既有非 LOGIN 角色，`consumer_role` 是无管理权限、无继承成员关系的
专用审计 LOGIN。两个安装器分别撤销对应私表的表级和列级权限，各追加三个审计
关系的 SELECT。普通 Workspace 消费者授权会再次撤销这类特权审计读取，必须使用
独立的审计 LOGIN。其他领域所需的只读权限和 Alembic 元数据读取由原领域维护者提供；
普通 Identity API 的五函数／两视图配置保持。

将单次命令的 `DATABASE_URL` 指向审计 LOGIN 后，按原方式执行。安装器在命令之外
显式运行，不依赖较新的应用迁移，也不改变 Alembic revision：

```bash
cd apps/api
uv run python scripts/audit_project_roles.py --pretty
```

原始角色、停用状态、NULL／未知值和邀请状态不被规范化。旧库尚无邀请项目角色列时
报告未知；成员版本／更新时间准备列也通过 owner 的元数据关系报告。加列后既有
关系自动反映真实能力，安装器和审计 CLI 均不推进历史数据库 revision。原 Identity
资格继续保留，Workspace 组合资格见下方收据。参照下文组合私网
验收表设置当前四项镜像及身份变量后，可运行同一自有资源案例：

```bash
AAP_IDENTITY_ROLE_AUDIT_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-role-audit-cold.json \
  --test-file tests/test_identity_role_audit_live.py
```

原 Identity 收据见 `.worktree/identity-role-audit-qualification.json`；组合 Workspace 收据
见 `.worktree/workspace-role-audit-qualification.json`。这是本机 ARM64 的有限正确性
资格，未覆盖完整 Workspace 或生产容量；整体剩余项见
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### 图片导入与规模种子的 Identity 选择

`import_images.py` 只从公开账号事实选任意首账号，保留不检查角色／启用状态的行为。
`seed_scale.py` 使用 `aap-identity-scale-seed-actors/v1` 两项固定只读能力，创建时间留在
Identity 内部。普通 Identity API／worker 配置保持五项函数；可信运维账号在普通围栏后
显式追加选择能力：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v consumer_role=legacy_runtime \
  -f infra/docker/identity-scale-seed-grants.sql
```

此例只用于自己拥有的测试资源；不能把追加能力交给普通 HTTP／worker。实际图片导入
还需要既有 Assets 运维能力。规模种子仍使用其他领域的原可信写入权限，不代表它们
的最小凭据已经收口。原有清理先提交，后续选择失败或中断不会恢复旧标记图谱。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-seed-selection-native.json \
  --test-file tests/test_identity_seed_selection_native.py
```

创建时间并列仍不规定次序；annotator 是项目职责，不是平台角色。成员事实与启用账号
必须在同一 SQL 语句内读取，不能先拉取 ID 再单独请求 Identity。
参照下文 Workspace 组合私网验收表设置当前四项镜像及身份变量后，运行真实 CLI：

```bash
AAP_IDENTITY_SEED_SELECTION_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-seed-selection-cold.json \
  --test-file tests/test_identity_seed_selection_live.py
```

14 项定向检查和一个包含 11 次容器调用的冷案例已通过，包括真实版本化图片对象、
重复导入、权限拒绝、已提交清理和分阶段写入、137 退出后的实际后端释放与恢复。
这是本机 ARM64 的有限正确性资格，未验收大规模容量或全部种子工作流。
收据见 `.worktree/identity-seed-selection-qualification.json`。
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)记录实际资格与剩余项。

### 媒体种子的 Identity 边界

COCO8／视频／OCR 的账号创建使用现有 persona owner；截图准备和目录只读取公开账号
事实并传递 UUID，大图仍按原 email 查找 owner。截图准备保留角色校验，目录另保留
启用状态校验；旧账号不被重置。可信运维账号先使用普通 Identity 围栏，再应用
`identity-seed-operator-grants.sql`，共五项普通函数加精确查询和 persona 创建。
该配置不增加普通 HTTP／worker 权限，其他领域仍沿用原运维权限。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/media-seed-identity-native.json \
  --test-file tests/test_media_seed_identity_native.py \
  --test-file tests/test_screenshot_seed_catalog.py \
  --test-file tests/test_seed_large_images.py
```

参照下文 Workspace 组合私网验收表，准备当前 API 冷镜像及身份变量后执行：

```bash
AAP_MEDIA_SEED_IDENTITY_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/media-seed-identity-cold.json \
  --test-file tests/test_media_seed_identity_live.py
```

该案例只接受验收器自有环境。冷凭据禁止 User 私表／列，保留大图图谱的旧 Project／
Dataset 运维权限，不冒充其他领域的最小凭据。77 项定向检查和十八次冷容器调用已通过，
包含 137 退出后的实际后端释放、共同回滚和同邮箱恢复；十秒 SQL 阻塞不代表即时取消
任意查询。收据见 `.worktree/media-seed-identity-qualification.json`；其他运维入口见
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)，日常主环境未切换。

### Workspace 锁定权限与会话快照

`lock_current_project_rows`／`lock_current_project_access` 保持原接口，通过已有
`aap-workspace-project-access-step/v1` 读取锁定的 Project／成员快照。账号仍用 Identity
SHARE NOWAIT，Project 用 KEY SHARE NOWAIT，非 manager 再锁成员 SHARE NOWAIT；
连接、`no_autoflush`、提交及回滚仍由原调用方持有。

完整快照直接同步原 Session 的 ORM 对象，保持训练冻结、标注配置及成员缓存刷新。
不在锁后另行查询 Project，不丢失其他 pending work。现有应用账号授权清单须包含
`aap_workspace_v1.project_access_step(uuid,uuid,text)`；本地资源装配的 `grant_runtime`
及 `infra/docker/lan-production-grants.sql` 已列出，不扩展 append 或删除专用账号。

限定复验使用验收器独占的测试资源：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-authority-locks.json \
  --test-file tests/test_workspace_project_authority_locking.py \
  --test-file tests/test_project_access_authority_lock.py \
  --test-file tests/test_labeling_write_scope.py
```

时间字段 HTTP 兼容案例另需现有测试变量 `AAP_WORKSPACE_RUNTIME_PYTHON` 指向当前
Workspace 提供方解释器；源码检查可使用本工作树 API 解释器来启动独立进程，不据此
声称非 editable／冷工件已通过。锁、真实 API LOGIN 私读拒绝、Node、缓存与外层回滚
证据见 [ADR-0166](docs/adr/0166-workspace-locked-authority-snapshots.md)。完整模块装配
资格仍按唯一台账集中收口，日常主环境未切换。

### Workspace 无锁权限与调用方快照

`resolve_project_access_by_id` 保留初始 Project → 当前 Identity → 刷新 Project →
非 manager 成员的顺序；直接 resolve 仍先检查 Identity。初始 Project 使用既有
`project_access_step` 新增的 `read_project_row` 阶段，之后复用 Project Query 的
`get_row`／`access_member_rows`，请求角色来自实际 Identity facts。

所有步骤借用原 Session，不创建或结束 SQL 事务，不加行锁，保留原 autoflush 与
外层 `no_autoflush`。完整快照刷新原 ORM 对象与字段历史；公开 GET access 的第二轮
权威读取及写入路径原锁不变。应用授权清单须包含既有 Project Query schema、policy
及 step；`lan-production-grants.sql` 和本地 `grant_runtime` 均已列出。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-access-unlocked.json \
  --test-file tests/test_workspace_project_access_unlocked_native.py \
  --test-file tests/test_project_access_unlocked_query.py \
  --test-file tests/test_workspace_project_access_read_phases.py
```

验收器只使用自有临时测试资源，覆盖受限真实 LOGIN、READ ONLY、锁缺失、缓存、
自动 flush、时序与外层回滚。边界见
[ADR-0167](docs/adr/0167-workspace-unlocked-authority-snapshots.md)。

### Workspace 独占 Project 快照

`aap_workspace_v1.lock_project_row(uuid,boolean)` 只在调用方事务中获取 Project
UPDATE 锁并返回完整快照。`nowait=true` 用于孤儿标注清理，保持原 409 project_busy、
无 autoflush 和刷新缓存；`false` 用于点云晋升，保留等待、原 autoflush 和普通 ORM
缓存行为。权限、evaluation、业务写入、Audit 及提交仍由原调用方持有。

等待成功后在锁内生成新语句快照，保证 typed Labeling 计数与 Project 字段一致。
只恢复普通查询的 expired／unloaded 列；需要刷新时才完整合并。新函数须随当前迁移
和 `module-data-grants.sql` 安装，并加入应用授权清单；没有独立 DSN 或新服务。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-exclusive.json \
  --test-file tests/test_workspace_project_exclusive_native.py \
  --test-file tests/test_workspace_project_locks.py \
  --test-file tests/test_project_orphan_cleanup_transactions.py
```

实际 API 夹具使用验收器的专用 Identity AUTH 凭据；受限业务 LOGIN 无三类私表 SELECT。
带 Audit 的夹具留到已验证自有数据库销毁，保持审计不可变约束。基线、失败最小复验和
限定本地证据见 [ADR-0168](docs/adr/0168-workspace-exclusive-project-snapshots.md)。

### Task／Training 的 Workspace 项目快照

`get_workspace_project` 通过已有无锁事实阶段读取完整 Project；普通缓存命中不查询、
不 flush，刷新模式、完全过期、删除状态及原 pending work 行为保留。Task access、
最终阶段写检查和选定 Training authority／study／claim 使用实际请求或保存的 actor ID。

Project 引用围栏仍按账号 SHARE → Project KEY SHARE 获取：等待模式复用原函数，
NOWAIT 用 `lock_project_reference_nowait(uuid)`。两者只返回 UUID／null，不物化
Project 配置；当前迁移、owner 授权和应用清单须一致。原调用方继续持有整个事务。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-project-get.json \
  --test-file tests/test_workspace_project_get_native.py \
  --test-file tests/test_workspace_project_reference_native.py \
  --test-file tests/test_workspace_project_get.py \
  --test-file tests/test_workspace_reference_locks.py
```

冻结 getter 样本是一次性基线，不设置捕获变量重写。实际受限 LOGIN、Node、缓存、
回滚及权限资格见 [ADR-0169](docs/adr/0169-workspace-task-training-project-snapshots.md)。

### Workspace 普通业务项目快照

普通 Project getter 统一使用 `get_workspace_project`，保留当前 Session、普通缓存命中、
autoflush 和显式刷新。缺省 actor 使用 `read_project_row(uuid)`，无需为后台元数据
构造 actor；已有 actor-aware 调用继续走原事实阶段。空项目 ID 先遵循原 autoflush
再返回 None。当前权限、最终写入围栏和提交仍由原调用方持有。

迁移／owner 与应用授权清单须一起更新。以下验收使用独占数据库，不修改日常角色：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-project-read.json \
  --test-file tests/test_workspace_project_get_native.py \
  --test-file tests/test_workspace_actorless_project_read_native.py
```

已有 40 项 ORM golden 只作复验，不设置 capture 变量覆盖。两条绑定、受限 LOGIN、
Node、空 ID 和调用方回滚见
[ADR-0171](docs/adr/0171-workspace-actorless-business-snapshots.md)。批量查询、排序
Project 集合锁与两处计数投影见下一节；其他集合查询按模块台账收口，不能改成逐行 RPC。

### Workspace 项目集合锁与 Labeling 计数写入

批次决策使用 `lock_project_references_for_update(uuid[])`，一次按 UUID 排序取得
NOWAIT 排他锁；缺失项目仍由后续读取判断，锁竞争沿用 `batch_decision_busy` 409
及外层回滚。计数重算保留原 Task／Batch 查询、autoflush 和显式 flush 时点，已登记
的五个 Project 计数字段由 Labeling owner 的 `write_project_counter_projection`
写入。它只写实际变化列；时间戳、旧缓存无变化和调用方事务保持。未登记的字段
仍使用原 ORM 行为，适配器不成为通用写代理。

更新迁移、owner／应用授权及 worktree 兼容运行授权后，使用独占数据库验收：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/project-owner-persistence.json \
  --test-file tests/test_labeling_project_counter_owner_native.py \
  --test-file tests/test_project_owner_persistence_native.py \
  --test-file tests/test_workspace_project_locks.py
```

32 项旧行为 golden 已冻结，不设置 `AAP_COUNTER_OWNER_CAPTURE` 重写。Session
关闭、失败回滚后重用、受限 LOGIN、Node 和实际消费者边界见
[ADR-0172](docs/adr/0172-project-set-locks-and-labeling-counters.md)。完整 Workspace
凭据、冷工件与恢复继续按唯一台账集中验收；这些源码改动没有切换日常运行环境。

### Workspace 项目范围与列表读取

聚合范围保持一条 SQL 内过滤，随后再排序、分页和计数。成员范围使用
`actor_project_memberships(uuid)` 的项目／职责事实，与 Identity `account_profiles`
联接后应用原活跃账号、已知角色及 viewer 配对规则；它只需要 actor ID。所有者与
看板元数据复用 `project_search_scope`，不把候选范围当授权结果。超级管理员全局
分支及原 owner 分支的调用方状态判断保持不变。

读取工厂位于 `workspace_read_relations.py`，不导入 Workspace 提供方运行时；原
`workspace_project_query_data` 导入路径保留兼容导出。窄 ORM 列别名保持独立执行
Select 时的 autoflush，未读取完整成员行或时间字段。更新迁移和三处运行授权清单：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-actor-scope.json \
  --test-file tests/test_workspace_actor_scope_legacy.py \
  --test-file tests/test_workspace_actor_scope_native.py \
  --test-file tests/test_project_aggregate_scope.py
```

126 个旧场景已冻结，每个含 autoflush 开关观测，不设置 `AAP_ACTOR_SCOPE_CAPTURE`
重写。受限 HTTP 验收使用既有 Identity AUTH 和 Briefs owner 连接，不向 API 角色
放开认证或资料命令。角色范围、列表、Node 和事务边界见
[ADR-0173](docs/adr/0173-workspace-actor-scope-relations.md)。其他集合见后文事实关系，完整模块
装配仍见唯一台账；本地 API／Celery 主环境未切换。

### Workspace 非锁定成员事实

剩余成员集合、完整绩效名册和批次相关存在性读取使用
`aap_workspace_v1.project_member_facts`，共享便携描述位于 `aap_workspace_contract`。
原生九列可在同一条 SQL 中组合；ORM 别名保持旧缓存及 autoflush。授权规则由各调用
保留，通知仍是四个独立语句，批次成员判断仍嵌在原 UPDATE 中。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-member-facts.json \
  --test-file tests/test_workspace_member_facts_legacy.py \
  --test-file tests/test_workspace_member_facts_native.py \
  --test-file tests/test_project_aggregate_scope.py
```

验收器创建并清理自己拥有的测试资源；旧行为 fixture 只重放，不重新捕获。受限 API
撤掉成员私表 SELECT，但包含原始 Project 查询的场景仍保留其权限。默认应用宽角色、
Workspace 完整凭据和冷工件不因本轮通过而获得资格，见
[ADR-0174](docs/adr/0174-workspace-nonlocking-member-facts.md)及唯一台账。

### Workspace Project 原生集合读取

部分项目读取使用 `project_facts()`；完整 ORM 行使用 `project_rows()`，在同一条
SQL 中组合 Workspace 字段和既有 Labeling 计数。两项事实关系的 SELECT 已加入应用
和 worktree 授权，不增加 DSN 或服务。在线计数增减使用下述固定写入能力。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-project-facts.json \
  --test-file tests/test_workspace_project_facts_legacy.py \
  --test-file tests/test_workspace_project_facts_native.py \
  --test-file tests/test_workspace_project_facts_consumers_native.py
```

验收使用独立测试资源，撤掉 API 的 User／Project／Member 私表 SELECT，实际 HTTP
认证及资料读取使用既有 Identity owner。旧基线只重放，完整 Workspace 凭据和冷工件
仍集中验收，见 [ADR-0175](docs/adr/0175-workspace-project-fact-relations.md)。

### Labeling 生命周期与审核计数

撤回／驳回的 review 减一、重开的 completed 减一，以及批准的 completed 加一与
review 减一分别通过 `aap_labeling_v1` 的三项固定函数执行。每次仍是一条 SQL，
同一连接／事务；正常行锁等待、autoflush、已缓存计数和数据库时间戳保持原行为。
不将原子增减改为读取后覆盖，也不新增 DSN 或独立提交。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/labeling-counter-deltas.json \
  --test-file tests/test_labeling_counter_deltas_legacy.py \
  --test-file tests/test_labeling_counter_deltas_native.py \
  --test-file tests/test_labeling_counter_deltas_workflows_native.py
```

验收撤销测试 API 的 Project 私表 SELECT／UPDATE，使用真实 Identity owner 及原
权限围栏；69 项旧行为 fixture 只重放，不设置 `AAP_COUNTER_DELTA_CAPTURE` 重写。
空／缺失 ID 也会尝试 UPDATE，保留 READ ONLY 的 25006；溢出与 Task 共同回滚。
迁移与三处运行授权须一致，范围见
[ADR-0176](docs/adr/0176-labeling-lifecycle-counter-deltas.md)。完整模块工件仍集中验收。

### Workspace 数据集解绑的借用事务

API 的 `DatasetUnlinkData` 保留原 SQLAlchemy Session、写入前 flush、缓存失效和外层
提交；合同与回执校验已在 Workspace 包。独立调用方使用
`DatasetUnlinkManager(DatasetUnlinkPostgresData(connection))`，连接须已有事务。
预览也锁定当前权限，不能使用 READ ONLY。原始 JSON 与 Python UUID／tuple 输入均
可校验；调用方继续负责提交失败后的回滚与同 operation ID 重放。

先创建专用 LOGIN 并应用既有 owner 授权，再在明确数据库中运行
`infra/docker/workspace-dataset-unlink-grants.sql`；传入 `expected_database`、
`migration_role`、`workspace_owner`、`workspace_dataset_unlink_role`。最后一项固定为
`workspace_dataset_unlink_client`，仅能执行预览／解绑两项函数，不授予私表或建库能力。
验收器会在自己拥有的一次性资源内配置该角色：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-dataset-unlink.json \
  --test-file tests/test_workspace_dataset_unlink_native.py \
  --test-file tests/test_dataset_unlink_bindings.py \
  --test-file tests/test_dataset_unlink_coordinator.py \
  --test-file tests/test_dataset_unlink_http.py
```

这验证调用边界；视频退役门禁的运行授权与回归见下一节，完整模块凭据／工件仍按唯一台账收口。
不为本切片新增 DSN 或 HTTP 服务配置，见
[ADR-0177](docs/adr/0177-workspace-caller-transaction-dataset-unlink.md)。

### 视频退役后的项目删除与解绑

两条命令共用 Labeling 的删除足迹锁和 Jobs 当前 run 的退役证明。未退役返回
`video_execution_not_retired` 409；父行／Job／execution／journal 的 NOWAIT 竞争仍为
`project_busy`。API、独立 Python／Node 调用方都保留原事务和成功回执，不自行取消
或退休执行。取消后由现有 worker 完成退役，再重试删除。

迁移安装 guard、原协调器的插入式扩展及 Jobs 私有 `video_deletion_clearances`。
该表只记录锁内证明未启动的 Job，随删除共同提交／回滚；用于后续识别输入 scope
留下的合法缺失 Job。已有 execution 时必须继续提供当前 run 的 retired journal。
权限清单只给 Jobs 增加 started_at 的窄列读取与私表所有权，以及内部 owner 之间的
固定执行权；API 和独立命令凭据无需新增私表或 helper 权限。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/video-deletion-retirement.json \
  --test-file tests/test_video_deletion_retirement_native.py \
  --test-file tests/test_video_deletion_retirement_http_native.py \
  --test-file tests/test_video_deletion_retirement_bindings.py
```

已启动但无账本的旧 Job、无证明的旧孤儿仍返回 409；当前无可靠自动排空／修复入口，
不能修改状态或伪造 journal 放行。clearance 非空时降级先以 55000 拒绝并保留证据。
升级、owner 授权和调用方代码须一致；本轮只验证独占测试环境，未切换日常服务。
范围见 [ADR-0178](docs/adr/0178-video-retirement-deletion-fences.md)及唯一台账。

### Workspace 同事务成员锁

最终写入围栏使用 scalar `lock_project_member_reference(uuid,uuid,boolean)`，保留
账号 → 项目 → 成员顺序、等待／NOWAIT 和 membership_busy。它不解码无关时间列。
批量分派使用 `project_member_rows(uuid,uuid[],boolean)`：一次排序集合等待 SHARE 锁，
完整九列复用已有解码器并刷新原 Session。false 分支仅用于过期脏缓存的无锁 preflush
读取，随后再执行原 flush 与锁定集合读取。所有操作仍由调用方提交或回滚。

安装当前迁移并同步 owner／应用授权清单，不扩张建题或删除专用 LOGIN。验证器使用
独占 PostgreSQL 与隔离资源，不向日常数据库授予新权限：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-member-locks.json \
  --test-file tests/test_workspace_member_locks_native.py \
  --test-file tests/test_identity_authority_reads.py \
  --test-file tests/test_batch_assignment_guards.py
```

冻结成员锁样本不重新捕获。限定边界见
[ADR-0170](docs/adr/0170-workspace-transactional-member-locks.md)；完整 Workspace 资格仍
以模块台账为准。

### Workspace 调用方事务内的项目删除

`aap-project-delete/v1` 的可移植命令和回执位于 `aap-workspace`。API 继续使用原请求
Session；连接式客户端 `ProjectDeletionPostgresData` 只借用调用方已经开启的 asyncpg
事务。调用成功后仍须由调用方提交，外层回滚或最终提交失败会同时恢复项目图、Audit
与回执。不要把它替换成提前提交的独立 HTTP／DSN 请求。

安装当前迁移及 `module-data-grants.sql` 后，为预建且无成员关系的 LOGIN 应用：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v workspace_owner=workspace_code \
  -v workspace_project_deletion_role=workspace_project_deletion_client \
  -f infra/docker/workspace-project-deletion-grants.sql
```

该账号仅有 `aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)` 一项业务 owner
函数执行权，无私表／列、序列、DDL、TEMP 或子清理函数权限。自动提交连接会被便携
客户端拒绝；该绑定不拥有连接、提交、回滚或重试。限定复验入口使用验收器独占资源：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-project-deletion.json \
  --test-file tests/test_workspace_project_deletion_native.py \
  --test-file tests/test_project_deletion_data.py \
  --test-file tests/test_project_deletion_retention.py \
  --test-file tests/test_project_labeling_delete_contract.py
```

调用边界通过 76 项不同定向案例，含 Node 原始 JSON 整数及实际公开 API。视频退役
门禁另通过限定验收；无证明旧作业的排空／修复及完整模块验收仍开放。见
[ADR-0165](docs/adr/0165-workspace-caller-transaction-project-deletion.md) 和
[唯一剩余台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### Labeling 类别与属性统计查询

`class-usage` 的 API 先调用 Workspace `require_project_manager`，再调用独立 Labeling
统计边界；管理权限只解析一轮，两个 owner 使用各自的数据和凭据。
默认借用调用方事务。显式 `LABELING_CLASS_QUERY_DATABASE_URL(_FILE)` 与
`LABELING_CLASS_QUERY_URL` 二选一，HTTP 另配 `LABELING_CLASS_QUERY_CONTROL_TOKEN(_FILE)`。
`LABELING_CLASS_QUERY_TIMEOUT_SECONDS` 默认30秒；显式选择失败或 DSN 文件为空时不回退。

独立进程运行 `uv run --project apps/labeling --extra runtime python -m aap_labeling`，
只需要自己的 DSN 和 control bearer；无 Workspace／Identity Briefs／对象存储配置。
`docker-compose.labeling-class-query.yml` 只发布本机8178，并读取
`LABELING_RUNTIME_CLASS_QUERY_DATABASE_URL_FILE` 与
`LABELING_RUNTIME_CLASS_QUERY_CONTROL_TOKEN_FILE` 两个宿主 secret 文件。
先迁移当前 head，再应用 module-data 和 Labeling class-query 两份 grants。
匹配的 Workspace owner 也必须支持单轮管理权检查；不对旧提供方静默降级。

独立安装包、三路径响应、真实语句间提交、受限镜像与备份恢复已完成限定本地验收。
命令、权限和范围见[内部运行协议](docs-site/api/guides/labeling-runtime.md)及
[ADR-0142](docs/adr/0142-labeling-class-usage-query-owner.md)。

### Labeling 视频 worker 最终写入 owner

这是一项仍在本地验收的后端切片，不会自动切换日常服务。它把视频跟踪 worker
成功、普通取消和失败时的最终 Job／候选结算交给 Labeling 私有 owner；公开 API
不变。worker 候选从 Jobs 已封存的视频窗口重建，API 不会重新请求模型。API 可以
借用 Session、配置专用数据库连接，或显式使用远程 HTTP owner；URL 与 DSN 不能同时
设置，显式绑定失败会保留任务重试，不回退。人工 accept/decide/discard 和
start/progress 写仍由 API ORM 持有。

API 侧使用 `LABELING_VIDEO_WRITER_URL` 与
`LABELING_VIDEO_WRITER_CONTROL_TOKEN(_FILE)` 选择 HTTP；使用
`LABELING_VIDEO_WRITER_DATABASE_URL(_FILE)` 选择专用 DSN；两者留空时借用现有
Session。`LABELING_VIDEO_WRITER_TIMEOUT_SECONDS` 默认 90 秒。HTTP 私有路由为
`POST /v1/labeling/video-writer/execute` 和
`POST /v1/labeling/video-writer/status`。status 支持两种封闭请求：原 operation、
执行证明和摘要用于不确定提交恢复；只有 execution proof 的查询调用 Native
read_publication action，只读查找当前 job/run 已有 publication 回执。普通取消需数据库取消标记；纠错取消跳过
candidate stage 授权。新代次清理先复用既有 publication，未命中才重建封存前缀。

独立进程使用 Labeling 包的 runtime extra 启动：
`uv run --project apps/labeling --locked --extra runtime aap-labeling-video-writer`

进程必需 `LABELING_VIDEO_WRITER_DATABASE_URL(_FILE)` 和
`LABELING_VIDEO_WRITER_CONTROL_TOKEN(_FILE)`。默认 host 为 `127.0.0.1`、port 为
8180、最大并发为 8，超时默认 90 秒；可通过 `LABELING_VIDEO_WRITER_HOST`、
`PORT`、`MAX_CONCURRENCY` 和 `TIMEOUT_SECONDS` 调整。处理 Mask 候选时，还须配置
Assets Mask Content 的 `ASSETS_MASK_CONTENT_URL` 和
`ASSETS_MASK_CONTENT_TOKEN(_FILE)`；bbox-only 结果不访问 Mask owner。

可选 `docker-compose.labeling-video-writer.yml` 只发布本机 8180，host 文件选择器是
`LABELING_RUNTIME_VIDEO_WRITER_DATABASE_URL_FILE`、
`LABELING_RUNTIME_VIDEO_WRITER_CONTROL_TOKEN_FILE` 和
`LABELING_RUNTIME_VIDEO_WRITER_MASK_CONTENT_TOKEN_FILE`。三个容器凭据均以只读 secret 挂载；bbox-only 使用空 Mask token 文件。该开发
Compose 为 1 CPU／768 MiB，
不是生产容量结论，也不会接入默认服务。先应用当前数据库迁移和
`infra/docker/module-data-grants.sql`，再按专用角色应用
`infra/docker/labeling-video-writer-grants.sql`。

协议、执行证明、候选重建与准确限制见
[Labeling 运行协议](docs-site/api/guides/labeling-runtime.md)、
[同库数据接口](docs-site/api/guides/module-data.md)和
[ADR-0159](docs/adr/0159-independent-labeling-video-writer.md)。当前尚未通过完整
三路径消费者、崩溃取消、Mask、备份恢复、受限镜像和容量验收；不作为生产切换依据。

### Inference 命名流水线

命名流水线默认借用 API 的调用方事务。显式配置
`INFERENCE_PIPELINES_DATABASE_URL(_FILE)` 可改为独立事务连接，或配置
`INFERENCE_PIPELINES_URL` 和 `INFERENCE_PIPELINES_TOKEN(_FILE)` 选择独立进程。
URL 与 DSN 不能同时配置；`INFERENCE_PIPELINES_TIMEOUT_SECONDS` 默认30秒，
显式配置失败不回退。账号资料投影仍复用 API 现有的 Identity Briefs。

独立进程运行 `uv run --project apps/inference --locked --extra runtime python -m aap_inference`，
需要 `inference_pipelines_client` 的 `INFERENCE_PIPELINES_DATABASE_URL(_FILE)` 及
`INFERENCE_CONTROL_TOKEN(_FILE)`。默认监听 `127.0.0.1:8768`，
`INFERENCE_MAX_CONCURRENCY` 默认8；请求及响应上限均为16 MiB，超限不截断。
先迁移当前 head，再对同一数据库安装 `infra/docker/module-data-grants.sql` 和
`infra/docker/inference-pipelines-grants.sql`；后者要求显式指定数据库与 owner／client 角色。

`docker-compose.inference.yml` 只发布本机8768，宿主 secret 文件选择器为
`INFERENCE_RUNTIME_PIPELINES_DATABASE_URL_FILE` 与 `INFERENCE_RUNTIME_CONTROL_TOKEN_FILE`。
容器使用 UID10008、只读文件系统及1 CPU／256 MiB 预算；这是开发运行配置，
容量资格须由实际验收记录确认。它不替换日常服务或生产部署。
合同、事务及故障恢复见 [Inference 运行指南](docs-site/api/guides/inference-runtime.md)。

### Inference 服务池策略与项目主绑定清理

能力规范化、指纹及 diff 的单一实现位于 `aap_inference.pool_capability`，API 调用方
直接使用该包。注册实例删除仍由 API 原事务协调；项目主绑定清理改为 Workspace 固定
函数 `aap_workspace_v1.clear_primary_backend_bindings(uuid)`。先应用当前迁移，再应用
`module-data-grants.sql`；本地 compatibility runtime 和 LAN grant source 显式列出
真实 API 角色所需的这一项执行权。LAN source 修改不等于已应用到生产。

没有新增配置、独立端口或 LOGIN。函数不自行提交，仍与 registry 删除及 Audit 同进退；
原项目服务池启用关系保留。完整 Backend control 的 health、routing、GPU 与真实消费者
边界继续推进，见 [ADR-0147](docs/adr/0147-inference-pool-policy-and-workspace-bindings.md)。

### Inference 后端 HTTP 与删除依赖

注册后端 HTTP 的实现位于 `aap_inference.backend_transport`，API 的
`MLBackendClient` 只装配现有 GPU 权威、动态超时、指标和公开错误。没有新增 endpoint
或配置；完整 Backend control 的独立运行继续推进。注册删除的 Learning 评估引用与
Jobs 运行预测计数使用固定 Native 函数；应用当前迁移后需重授 owner 与实际 API 角色
的精确权限，不能给 Inference 外域原表权限。范围见
[ADR-0148](docs/adr/0148-inference-backend-transport-and-deletion-dependencies.md)。

### Workspace 后端能力探测

Workspace 的后端事实只包含 registry ID 和不透明 `probe_subject`；实际 `/setup` 请求由
Inference 持有凭据并执行。API 默认在现有 caller connection 打开同一事务快照；可选
`INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` 或 `INFERENCE_BACKEND_PROBE_URL`，两者互斥。
HTTP 使用 `INFERENCE_BACKEND_PROBE_TOKEN(_FILE)` 和默认30秒的
`INFERENCE_BACKEND_PROBE_TIMEOUT_SECONDS`。显式选择的提供方失败不回退。

独立 Workspace 配置进程使用 `WORKSPACE_INFERENCE_PROBE_URL`、
`WORKSPACE_INFERENCE_PROBE_TOKEN(_FILE)` 与 `WORKSPACE_INFERENCE_PROBE_TIMEOUT_SECONDS`。
它不接收后端 URL 或 token。独立 Inference 进程可以只启用 probe，使用
`INFERENCE_BACKEND_PROBE_DATABASE_URL(_FILE)` 和独立的
`INFERENCE_BACKEND_PROBE_CONTROL_TOKEN(_FILE)`；不可复用 pipeline bearer。
先应用当前迁移和 `module-data-grants.sql`，再安装
`infra/docker/inference-backend-probe-grants.sql`，指定专用 `inference_backend_probe_client`。
该 LOGIN 只能执行 policy/open 两项能力，没有 registry、私钥表或其他 owner 原表权限。

`docker-compose.inference-probe.yml` 发布本机8768，宿主 selector 为
`INFERENCE_RUNTIME_BACKEND_PROBE_DATABASE_URL_FILE` 与
`INFERENCE_RUNTIME_BACKEND_PROBE_CONTROL_TOKEN_FILE`。它与已有流水线 Compose 示例共用
宿主端口，分别启动时只能选择其一；同一二进制也支持双能力和两个不同 bearer。
Workspace 配置服务叠加 `docker-compose.workspace-inference-probe.yml`，使用
`WORKSPACE_RUNTIME_INFERENCE_PROBE_TOKEN_FILE` 并设置同网络 URL。
所有 secret 文件保持只读挂载；示例不切换日常或生产环境。

Subject 绑定原数据库、registry 和仍在进行的原事务。它保留未提交后端和配置轮换前的事实，
不新增业务提交，不在外部 I/O 前持 registry 行锁。事务结束后不能再次打开；已经接受的
外部请求不会被这一检查追溯取消。后端不可达仍按既有行为允许继续；内部提供方不可用返回
503，旧 JSON 数值无法物化仍在原阶段失败。请求16 MiB／回复4 KiB 是此元数据协议的边界，
不用于预测请求，也不声称覆盖无限大的历史 extra_params。见
[ADR-0149](docs/adr/0149-credential-owning-workspace-backend-probe.md)。

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

页面路由由 React Router 定义在 `apps/web/src/App.tsx`，不是 Zustand page switch。`/projects/:id/annotate` 与 `/projects/:id/review` 使用全屏工作台；Dashboard、数据集、存储、`/ai-pre`、模型市场、项目设置、审核、用户与系统管理等页面通过 `AppShell` 路由承载。`/training` 仍是唯一明确的占位入口。

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

正常月报由 `app/services/audit_summary.py` 本地计算，API、Compose、工作树启动和普通 CI 均不依赖 Go；生产 API 不读取 `AUDIT_SUMMARY_URL` 或选择实验适配器。

Go 只通过单独的 `docker-compose.experiments.yml`、`audit-summary-experiment` profile 或手动 workflow 启用。需要验证时，运行 `cd apps/audit-summary && go test -race ./... && go vet ./...`；Python 实验适配器与测试位于 `apps/api/experiments`，显式运行 `cd apps/api && .venv/bin/python -m pytest experiments/tests`。普通 API/Web 镜像排除此目录及 Go 实验源码。

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
pnpm --filter @anno/docs-site check:all  # 文档元数据、导航与生成物检查
```

后端 CI 输出最慢 30 个 pytest 阶段，完整用例耗时保存在 `backend-test-results`
artifact 的 JUnit XML 中。测试用户工厂在进程内复用默认密码 `Test1234` 的真实 bcrypt
哈希；自定义密码、生产密码哈希与验证逻辑保持原样。四角色 fixture 与 E2E 造数
共用这一工厂，避免为每个测试账号重复计算默认密码哈希。

逐帧保存的真实 Celery 调度验收需显式启用，在隔离测试库与 Redis 中启动临时 worker：

```bash
pnpm dev:worktree -- exec --mode test -- sh -c 'cd apps/api && AAP_FRAME_CELERY_ACCEPTANCE=1 .venv/bin/python -m pytest tests/test_frame_preannotate_celery.py'
```

该验收使用固定推理与缓存帧夹具，实际执行 `default → ml → RETRY → finalize`、PostgreSQL 行锁、Redis chord 与进度续期，覆盖连续多轮争锁后的恢复、取消和任务版本变化。普通 pytest 默认跳过该验收；测试结束关闭临时 worker，资源按工作树测试环境的正常流程保留或销毁。

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

真实超大图回归使用固定来源、字节数和 SHA-256 的开发夹具。API、共享媒体控制端与
`assets-images-worker` 启动后，可下载、入库并等待生成完成；共享控制端和图片 Worker 使用同一测试数据库及缓存桶，API 的 `ASSETS_MEDIA_URL` 指向共享控制端：

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

截图夹具准备、恢复和清理脚本会直接构造、更新及删除多领域测试行，
`SCREENSHOT_DATABASE_URL` 必须指向自有一次性截图库的 schema-owner 连接。
普通 API runtime 账号已撤销 Jobs 私表 INSERT／UPDATE／DELETE，不能用于这些脚本；
截图 API／worker 的普通运行连接仍按既有授权装配。不要使用日常开发库或生产库。

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

录制超大图渐进细节前，还需要把固定 Cosmic Cliffs 夹具导入同一截图数据库，并确认共享媒体控制端与
`assets-images-worker` 都使用该截图数据库和对象存储，且截图 API 的 `ASSETS_MEDIA_URL` 指向共享控制端。只有这个可选项目存在时，catalog 才会返回
`large_image_demo`；其他截图与流程不依赖它。

```bash
pnpm --filter @anno/web image:seeds
cd apps/api
DATABASE_URL="$SCREENSHOT_DATABASE_URL" PYTHONPATH=. uv run python scripts/seed_large_images.py \
  --id nasa-cosmic-cliffs --enqueue-pyramids --wait-seconds 1800
```

图片 Worker 与截图 API/控制端必须使用同一 `SCREENSHOT_DATABASE_URL`；开发库 Worker 不会为截图库回写生成状态。

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
  地址，可以继续使用 Docker 网关。修改 `.env` 或 Vite 代理配置后重启 API/Web。
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

## 下一步计划

详见 [CHANGELOG.md](CHANGELOG.md) 顶部的 roadmap 与 [docs/plans/](docs/plans/) 下的具体计划。

自动训练策略默认关闭。部署迁移后刷新 default/export worker 与 beat，确认注册 `app.workers.learning_policy.check_policies`；beat 每分钟分批检查策略，独立训练执行器仍按固定 GPU 领取。负责人明确保存门槛、检查间隔和冷却时间后才启用；没有 beat 的工作树环境可通过「立即检查与同步」验收相同路径。关闭策略不要求训练目录仍在线，成功训练不会自动发布。

## 独立 Assets 来源服务

`apps/assets-source` 的现有 HTTP runtime 执行单文件上传初始化、条件发布完成、Dataset/Item 删除、同步 ZIP 入库和旧对象桶扫描，也承载六项连接管理操作：列表、读取、创建、更新、删除和连通性测试。公开 API 保留认证、scope、ZIP/扫描各自既有角色规则和同步响应；来源服务不连接 Redis/Celery。连接操作继续使用现有 API 路由和 Source runtime，不另加一个连接服务。协议与重放见[来源控制](docs-site/api/guides/assets-source.md)。

先将迁移应用至当前 head、完成 module-data owner 授权，再为独立 `asset_source_client` LOGIN 应用：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v assets_owner=assets_code -v asset_source_role=asset_source_client \
  -f infra/docker/assets-source-grants.sql
```

数据库名和连接参数应指向明确选择的环境。Source LOGIN 精确执行 26 项 `aap_assets_v1` 能力：14 项来源/入库、9 项连接管理和 3 项图片来源/作业受理能力。它不读业务关系或序列，不使用其他 owner schema，也不授予任何方向的角色成员资格。两项批量媒体派发 claim/ack 能力只给 API maintenance relay。导入执行器仍使用独立的九函数 LOGIN，Catalog LOGIN 仍是 29 项；不要把日常宽权限 API 账号用于这些工件。

服务显式配置专用 DSN、token、S3 access/secret；可用 `.env.example` 的 `_FILE` 变量。secret 必须能被用户 10003 读取。S3 授予 import 桶 `dataset-uploads/*` Get/Put、datasets 桶历史来源的条件读取和该配置桶自身的 ListBucket，以及 `dataset-sources/*` Put/multipart Abort。不要授予来源 Delete、其他桶 List 或桶管理；cache 桶只用于 SQL outbox。内部 I/O endpoint 与浏览器签名 endpoint 分开，均从服务配置取得。ZIP 的 200 MiB 压缩/展开预算与 JSON 请求体预算独立。

连接管理凭据使用可选 `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` 或 `_FILE`，值与本地 API/导入执行器的 `CONNECTOR_ENCRYPTION_KEY` 相同。该 key 留空时 Source 仍可启动；创建凭据或需要解密既有密文的操作会返回配置错误，单纯改名或空 patch 不需要 key。`ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST` 是 JSON 字符串数组回退值，数据库 `system_settings.connector_host_allowlist` 覆盖它；`ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE` 可指定只读的可信 SFTP 主机文件。空白回退列表拒绝连接器网络目标。DNS 和探测在锁外完成，最终提交只短暂共享锁定 Settings 表并复核策略。API 的 `ASSETS_SOURCE_URL` 留空时用本地 Source 工作流；一旦配置远程 URL，远程错误不会回退到本地。

Assets 当前由 31 张映射表组成；全仓 ORM 所有权台账共 133 张表。连接操作写入新的 `storage_connection_operations` 不可变回执表，该表没有指向实时连接的外键。连接删除保留已接受的导入快照；后续导入仍要重新读取当前连接。

0215 SQL 工件升级时先安装 `0215_dataset_source_ingest.sql`，再安装 `0215_dataset_source_ingest_delivery.sql`；降级由迁移包装器按依赖移除函数和表。每个独立 Source 进程同时只接收一个 ZIP 请求，Compose 限制内存为 512 MiB、`/tmp` 为 256 MiB。公开网关另为 multipart 边界和头部保留 64 KiB，流式拒绝超出包体预算的上传；文件本身仍最多 200 MiB。ZIP 新对象只写 `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}`，扫描只读取原生能力返回的精确 Dataset 前缀。回滚不删除对象存储中的原始字节。

准备固定桶与 origin 后显式运行独立项目：

```bash
docker compose -p aap-assets-source-dev -f docker-compose.assets-source.yml up -d --build
docker compose -p aap-assets-source-dev -f docker-compose.assets-source.yml exec assets-source python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8767/ready', timeout=2)"
```

该项目不进入默认 Compose，默认只 expose 内部 8767。宿主联调可使用 `uv run --project apps/assets-source --extra server python -m aap_assets_source_runtime`，传入专用环境，再将网关指向实际可达地址。网关 `ASSETS_SOURCE_URL` 留空为本地模式，设置后还需同一 token；刷新 API 生效。默认网关超时 300 秒，上限 3600 秒；服务默认池 1..8、取得连接 2 秒、SQL 5 秒、S3 连接/读取 2/10 秒、请求/响应 1/16 MiB。就绪检查验证 SQL 身份，实际对象权限由业务调用及本地验收验证。

同一二进制还可选择 `ASSETS_SOURCE_MODE=guide-only` 或 `source-and-guide`；默认 `source` 不变。
`guide-only` 不需要数据库或原来源凭据，只开放健康检查及私有指南对象接口。
该能力使用独立的 `ASSETS_SOURCE_GUIDE_S3_ENDPOINT`、`_PUBLIC_URL`（默认 `/minio`）、
`_REGION`（默认 `us-east-1`）、`_BUCKET` 和 `_ACCESS_KEY`／`_SECRET_KEY`（均支持 `_FILE`）。
凭据只覆盖配置标注桶的 `projects/*/guide/*`，允许 Get/Put/普通 Delete，显式禁止版本删除；
无需 ListBucket 或桶管理权限。`ASSETS_SOURCE_GUIDE_MAX_IN_FLIGHT` 默认 4，超额立即返回 busy；
指南 S3 连接／读取超时由 `_CONNECT_TIMEOUT_SECONDS`／`_READ_TIMEOUT_SECONDS` 指定，默认 2／5 秒。
服务与客户端均不自动重试，网络超时后的对象效果可能不确定。公共指南工作流由 Workspace 编排；
字节与命令的本地验收边界见 [ADR-0144](docs/adr/0144-workspace-guide-assets-owner.md)。

指南命令使用同一个 Workspace Configuration owner。先应用当前 Native 迁移及 module-data
owner grants，再配置已有的 `WORKSPACE_CONFIGURATION_URL` 或专用 DSN；两者不同时设置。
默认借用 API 事务，完成／删除由 HTTP 提交后完整刷新；独立 owner 自行提交与刷新。
独立 Workspace 必须配置 `WORKSPACE_GUIDE_OBJECTS_URL` 和 `_TOKEN` 或 `_TOKEN_FILE`，
指向已启用指南能力的 Assets Source，token 与该进程的 `ASSETS_SOURCE_TOKEN` 相同。
字节 RPC 超时由 `WORKSPACE_GUIDE_OBJECTS_TIMEOUT_SECONDS` 指定，默认 15 秒，调用不自动重试。
可将 `docker-compose.workspace-guide-assets.yml` 叠加到现有配置 owner，额外挂载一份只读 bearer；
它不创建新服务，也不给 Workspace S3 凭据。API 未设置字节 URL 时复用现有客户端；
设置后失败不回退。指南命令的三路径、实际锁跨度、独立工件、容器重启与备份恢复均已在隔离本地验证。
恢复验收从非空登记和真实对象开始，克隆库的新写入及撤权不影响源库。此结果不构成生产切换、容量或完整模块资格。

复制后进程退出或 SQL 回滚可能留下未登记来源拷贝；目前保留这些字节，尚无自动回收。单文件上传重试保持原 Item/操作 UUID，并使用新 publication 代次，不能通过删除原始文件或旧条目恢复。ZIP/扫描在一个外层 SQL 事务中提交素材、Labeling append、计数、ZIP Scene/frame 变更、收据和媒体派发意图。媒体派发由 API maintenance relay 领取，交付回执只表示 broker 接受，不代表媒体已完成。

回退时可先清空网关 URL、刷新 API 并保留 SQL/回执，再停止同一 `-p`/`-f` 独立项目。回退迁移须先停止写入并处理活动操作及待确认媒体派发；0215 会拒绝降级仍在准备的已提交操作或待派发记录，0217 会拒绝删除仍保留的连接操作回执。完整 Assets 的其他 CRUD、派生媒体和 Blob 边界仍在推进；此服务不代表全模块独立部署或生产容量资格。

## 独立连接器导入执行器

`apps/assets-source` 另提供 `python -m aap_assets_source_import`，领取已受理的连接器导入；它与来源 HTTP 服务使用不同的数据库和 S3 身份。公开导入 API 继续返回 202 和 Job，执行器离线时作业保持待领取。旧 Celery 导入入口已移除，首次迁移到 0214 前必须让旧运行时排空或显式取消活动的 `dataset_import`；迁移不会自动转移旧作业。

完成迁移与 `infra/docker/module-data-grants.sql` 的全部 owner 授权后，为独立的 `asset_import_client` LOGIN 应用：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v assets_owner=assets_code -v asset_import_role=asset_import_client \
  -f infra/docker/assets-import-grants.sql
```

连接参数和数据库名须指向选定环境。该账号仅能执行七项导入能力，不能读业务关系、序列或其他模块 schema，也不能有任一方向的角色成员资格。受理/取消和派发能力留给 API/维护运行时，不授予导入账号。

准备 `.secrets/assets-import-database-url`、`assets-import-s3-access-key`、`assets-import-s3-secret-key` 和 `connector-encryption-key`，内容分别为专用 DSN、目标存储身份及与 API 相同的 Fernet key；文件须能被容器用户 10004 读取。可用 `.env.example` 中对应的 `_FILE` 变量改路径。目标存储身份只允许 datasets 桶的 `dataset-sources/*` Put 与 AbortMultipartUpload，不授予 Get、List、Delete 或桶管理。来源连接器使用自身的只读凭据，不复用目标凭据。

图片身份来自成功的 PutObject 或 CompleteMultipartUpload 回执中的 ETag、可选 VersionId 和实际传输字节数；回执缺失或格式无效时保留明确的文件失败。目标只写身份不执行上传后的 HEAD/GET。导入心跳的 `dataset_busy` 重试受最后一次确认的租约到期时间约束，续租调用本身也受该期限限制；无法确认时停止 I/O，保留由后续执行者接管的租约状态。

显式设置 `ASSETS_IMPORT_S3_ENDPOINT` 和 `ASSETS_IMPORT_DATASETS_BUCKET`。执行器每次读取当前数据库白名单，未覆盖时才用 `CONNECTOR_HOST_ALLOWLIST` 部署默认值；显式空数组全拒。SFTP 需将可信指纹文件路径赋给 `ASSETS_IMPORT_SFTP_KNOWN_HOSTS`；默认示例为空，只支持不依赖 SSH 的来源，未知 SFTP 主机被拒绝。

```bash
docker compose -p aap-assets-import-dev -f docker-compose.assets-import.yml up -d --build
docker compose -p aap-assets-import-dev -f docker-compose.assets-import.yml exec assets-import python -m aap_assets_source_import --healthcheck
```

该项目不进入默认 Compose，不开放 HTTP 端口，不连接 Redis/Celery。宿主联调可用 `uv run --project apps/assets-source --extra importer python -m aap_assets_source_import`；`--once` 处理至多一项已受理操作。健康检查验证配置和精确 SQL 权限，不能代替来源连通性或实际对象权限验收。

默认租约 120 秒、续租 20 秒，连接池 1..4，SQL 超时 10 秒；来源读取超时与续租独立。复制按对象大小使用 8..64 MiB 分块，最多 10000 块；单对象协议上限 625 GiB，另受受理时固定的文件数、总字节数和 16 MiB 清单上限约束。这些限制不表示已验证生产容量。

媒体和通知由维护队列每 10 秒中继持久派发记录，仍需现有媒体消费者和 Redis；中继确认代表派发被接受，不代表媒体计算完成。部署后应刷新受影响的维护 worker/beat 并检查任务 `app.workers.dataset_import_delivery.relay_dataset_import_deliveries` 注册及 maintenance 路由。

可停止上述独立项目后重启接替，无需更换操作 UUID；过期租约会被重新领取，已提交条目不重复建题，未提交复制使用新 publication 路径。显式取消阻止迟到提交。回退先暂停新的受理并排空/取消当前操作、处理待派发记录，再停止该项目；降级迁移会拒绝活动作业或未确认派发，删除回执会失去重放依据。未登记来源拷贝保留，当前没有自动回收，也不自动恢复已移除的 Celery 导入器。

## 可信本地导入器的 Dataset 与 Item 写入

`apps/api/scripts/import_images.py` 和 `apps/api/scripts/import_nuscenes_scene.py` 通过 `AssetsOperatorGateway` 调用 Assets 的六项受信任 SQL 能力。它们使用 API 本地装配已有数据库连接和对象存储设置；该路径不经过 `ASSETS_SOURCE_URL` 或 Source HTTP，不创建新的独立服务，也没有 HTTP 回退。公开 nuScenes 数据集资格检查及 actor 先于 Dataset 的锁定顺序保持原样；本地图片脚本沿用现有 owner 选择，owner UUID 表示数据归属，不替代 operator 凭据的执行权限，也不增加管理员角色门槛。

迁移应用至 head `0218` 后，按既有 module-data 初始化流程创建 Assets NOLOGIN owner 及独立 `asset_operator_client` LOGIN。先应用完整的 `infra/docker/module-data-grants.sql` owner 授权，再应用 `infra/docker/assets-catalog-grants.sql`（供现有 nuScenes Catalog 资格查询使用），随后应用既有 Source 和导入角色 grants，最后由 migration role 安装 operator 精确授权：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v assets_owner=assets_code -v asset_operator_role=asset_operator_client \
  -f infra/docker/assets-operator-grants.sql
```

`asset_operator_client` 必须与 migration role 和 `assets_code` owner 分离，作为 LOGIN 使用且不继承其他角色。它只能执行 `aap_assets_v1` 中六项 operator 函数，不直接读写表、不访问其他 owner schema；`dataset_operator_operations` 保存不可变操作回执且没有 live Dataset/Item 外键。不要把 Source 的 26 项、Catalog 的 29 项或导入执行器的 9 项能力授给该角色。现有脚本继续使用原装配连接；六函数账号供来源能力的独立消费者使用，不能直接运行还包含 Identity、Workspace 或 Labeling 步骤的完整脚本。operator grants 会拒绝数据库 CREATE/TEMP 权限，包含从 PUBLIC 获得的权限；初始化应撤销 PUBLIC 的 TEMP，只对既有确需临时表的角色显式授予。

登记操作继续使用调用方同一 SQL 事务；nuScenes 导入可将 Dataset/Item、Scene、标定、工作区关联和 Labeling 任务共同提交或回滚。条目按输入顺序以至多 500 项分批追加或更新。重导读取通过排序游标返回稳定快照；单页最多 500 项，记录 JSON 上限 15 MiB，完整响应上限 16 MiB，单次请求上限 8 MiB。调用方校验页序、总数和快照摘要后再使用数据；source snapshot 是不透明 CAS 值，更新时由 Assets 重新校验并只合并请求字段。

SQL 回滚不会撤销对象存储 I/O。图片上传和 nuScenes 文件写入/覆盖若发生在数据库提交前，失败后对象字节仍保留；不得以回滚成功推断对象已删除。更多函数、快照与事务字段见[同库数据接口](docs-site/api/guides/module-data.md#可信导入器数据集与素材登记)及 [ADR-0104](docs/adr/0104-operator-source-registration.md)。该边界继续依赖现有本地导入脚本和共享 PostgreSQL，不代表完整 Assets、CLI 或导入器可独立部署。

## Mask 内容的独立字节服务

`apps/assets-mask-content` 提供 `aap-assets-mask-content/v1`，仅处理现有内容地址 RLE 对象的字节。`content` 进程只提供 `put/get`；`cleanup` 进程只提供分页 `list` 与带 ETag、修改时间围栏的 `delete`。两个进程没有数据库、Celery 或 Redis 凭据。任务权限、图像／视频限制、上传预约和引用可达性继续由 Labeling 判断。

API 与需要读写 Mask 的 worker 配置 `ASSETS_MASK_CONTENT_URL`、`ASSETS_MASK_CONTENT_TOKEN`；持久回收的字节端另配 `ASSETS_MASK_CLEANUP_URL`、`ASSETS_MASK_CLEANUP_TOKEN`。全部地址留空时使用同一实现的本地装配；配置任何远程地址后，缺少所选角色的地址／token 或远程失败都会明确报错，不会改用 API 的 S3 凭据。

业务回收统一使用 Labeling 持久协调器，通过字节端的 `scan/execute/status` 协议恢复。HTTP 超时或断连后，远端删除可能继续执行；原生每键围栏会一直保留，直到当前代次收到精确终态回执。业务回收不调用旧的条件 `delete` 接口。

运行身份使用固定桶中的 `raster-masks/sha256/*`。根据角色生成策略并安装到各自的 S3 身份，不授予整个共享桶的对象权限：

```bash
python scripts/mask_content_s3_policy.py --bucket annotations --role content
python scripts/mask_content_s3_policy.py --bucket annotations --role cleanup
```

`MASK_CONTENT_BUCKET` 必须与既有 `MINIO_BUCKET` 中的 Mask 对象一致。内容身份仅 Put/Get；清理身份按 Mask 前缀 List、Get（用于 HEAD）和 DeleteObject，并在独立的 `raster-mask-gc/v1/*` 日志前缀获得 Get/Put，不能写入 Mask 内容或删除日志；两者仅有 GetBucketLocation 用于就绪检查。这里删除当前逻辑键，历史 S3 版本回收仍由桶生命周期／独立清理责任处理。

可选 `docker-compose.assets-mask-content.yml` 不进入默认或生产 Compose。分别准备两组 token、access-key、secret-key 文件后显式启动；只暴露容器内 8770，普通内容进程不挂载清理 secret。宿主进程使用 `MASK_CONTENT_ROLE` 选择角色，token 和 S3 凭据支持 inline 与 `_FILE` 二选一：

```bash
uv run --project apps/assets-mask-content --extra server python -m aap_assets_mask_content_runtime
docker compose -f docker-compose.assets-mask-content.yml up -d assets-mask-content assets-mask-cleanup
```

Labeling 的原生能力检查引用根、上传预约及内容生产宽限期，登记删除意图后释放 SQL 事务，再调用字节端。同内容重传会续期预约；已验证内容在最终连接时还须匹配原代次。每个字节角色进程同时处理一个内容操作，忙时返回 503。接口、预算和现有引用兼容规则见[Mask 字节接口](docs-site/api/guides/mask-content.md)。

### Mask 回收的独立运行时

`apps/labeling-mask-gc` 提供持久删除围栏、只读预览和回执协调。Celery 回收器默认本地装配同一协调器；`LABELING_MASK_GC_URL`、`LABELING_MASK_GC_TOKEN` 显式选择独立 HTTP 服务，`LABELING_MASK_GC_TIMEOUT_SECONDS` 默认 300 秒。远程失败不回退。先应用迁移及 owner 授权，再为独立的 `labeling_mask_gc_client` LOGIN 安装六项 GC 精确授权：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v labeling_owner=labeling_code -v mask_gc_role=labeling_mask_gc_client \
  -f infra/docker/labeling-mask-gc-data-grants.sql
```

该进程仅接收 `MASK_GC_DATABASE_URL`、`MASK_GC_TOKEN`、`MASK_GC_CONTENT_URL` 和 `MASK_GC_CONTENT_TOKEN`，四项均支持 `_FILE`；内容地址必须指向 cleanup 字节进程。运行 `uv run --project apps/labeling-mask-gc --extra server python -m aap_labeling_mask_gc_runtime --once` 执行一次有界协调，省略 `--once` 后提供轮询及私有 run/progress 路由。`MASK_GC_POLL_ENABLED=false` 关闭后台轮询，保留手动 HTTP 操作与健康检查；默认值为 `true`。它不接收 S3、Celery 或 Redis 凭据。GC 登录没有四项业务生产能力；默认本地装配的受信任应用角色另持有六项 GC 能力，每次调用使用当前任务事件循环内的新事务。

私有 `POST /v1/mask-gc/run` 接受 `scan_pages`（默认 1，上限 20）、`delete_limit`（默认及上限 100）、`dry_run` 和仅预览可用的 `after_key`。预览不创建 sweep、删除意图或字节日志，也不改变破坏性扫描游标；用结果的 `next_after_key` 继续，直到 `scan_complete=true`。`referenced` 是全局当前存活引用键的去重计数，无法观察时为 null，不代表删除许可。

Celery 原任务仍在 `cleanup` 队列运行，兼容原 `dry_run` 参数，并支持上述三个预算／续扫参数。它先提交领域过期记录并释放指标读取事务，再调用协调器；因此 Celery 的 `dry_run` 仍会提交领域过期清理，独立服务的 dry-run 完全只读。`errors` 汇总扫描、数据、完成确认及持久不确定结果，须和 `deleted`、`pending`、`indeterminate` 一起判断；任务 `SUCCESS` 不等于所有删除成功。两类运行镜像均只保留已安装环境，可用只读根文件系统和非 root 用户运行。

工作树启动器清空从共享配置继承的 Mask 服务 URL/token，防止一次性环境调用日常服务；需要跨进程验收时，在隔离命令内部显式提供本次服务地址。

升级前停止旧回收消费者，迁移会在持锁期间回填已有标注、预测、预约及保留记录的完整引用根；回填显式序列化整行，避免与 `source` 字段重名。安装最新 owner、应用和 GC 精确授权后再启动消费者。已有根、代次、意图或 sweep 时，禁止降级删除 GC 状态；应保留状态并向前恢复兼容运行时。只回退预览函数不会移除这些持久事实，恢复后须重新安装对应函数授权。迁移和回退演练使用一次性环境，不改日常开发库。

日志和终态回执不设置自动过期或删除策略；没有回执的操作持续保留围栏，不能按租约或时间自动释放。普通批处理及重试的结果交接、独立回收镜像和固定本地样本已通过隔离验收；日常 API 使用同一协调器的内嵌装配，远程配置仍关闭。完整模块部署和生产容量仍未完成。详见 [ADR-0112](docs/adr/0112-durable-mask-gc-fences.md)。

## Identity 独立认证准入

`apps/identity` 提供同一个 JWT／API key 验证器及私有 `POST /v1/identity/authenticate`。`IDENTITY_URL` 留空时在 API 内装配；设置后使用 `IDENTITY_TOKEN` 和独立服务，错误不会回退。`IDENTITY_AUTH_DATABASE_URL` 或 `_FILE` 可在内嵌模式选择函数专用 LOGIN，两项不能同时设置；显式文件错误不会改用应用连接。未配置时，本地请求复用调用方事务，socket 使用独立短事务。

准入成功后，`deps.get_current_user` 使用同一 AUTH capability 调用私有 `POST /v1/identity/actor-context`，只读取当前账号的 name、email、role、active 状态和 weekly target。请求传入已准入的 actor UUID 与 role，不再重复验签、generation/revocation、API key last-used 或 scope；缺失、停用或 role drift 仍映射为原 401/Bearer challenge，服务失败或无效响应为原 503，选定的远程/专用绑定不回退 ORM。此读取沿用现有 `IDENTITY_URL`、`IDENTITY_AUTH_DATABASE_URL(_FILE)`、control bearer 和连接限额，不新增 DSN、LOGIN 或环境变量。迁移 0250 将 `read_actor_context(uuid,text)` 加入既有 AUTH LOGIN 的第五项函数能力；本切片已通过受限 SQL、调用方事务／专用 DSN／远程三种绑定、真实 API、双向语言 peer 与恢复的限定本地验收。写路径账号锁、目标 User 查询与 ORM 状态同步仍留在各自调用事务中。字段和失败语义见 [ADR-0130](docs/adr/0130-identity-post-admission-actor-context.md)。

同一运行时也提供 `POST /v1/identity/api-keys/execute`，持有 API key 五项管理命令和可重放回执。公开路由只消费主体和值消息，不水合 User 或写 key ORM。内嵌装配可另设 `IDENTITY_KEY_DATABASE_URL(_FILE)` 使用命令专用 LOGIN；两项最多一项，显式错误不会回到应用连接。远程装配复用 `IDENTITY_URL`，写入由独立服务持有。

先安装迁移及 module-data owner grants，再为预先创建、无继承／成员关系的四个 LOGIN 安装精确权限：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code \
  -v identity_auth_role=identity_auth_client \
  -v identity_command_role=identity_command_client \
  -v identity_fence_role=identity_fence_client \
  -v identity_operator_role=identity_session_operator \
  -f infra/docker/identity-auth-grants.sql
```

PUBLIC 的 TEMP 须在数据库初始化时撤销。准入 LOGIN 只有五项函数能力，包括迁移 0250 增加的 `read_actor_context(uuid,text)`；仍不持有业务表、Redis、队列或对象存储权限。主体上下文不是写权限或目标授权票据；密码恢复、登出和账号生命周期仍由调用方事务及各自账户锁维护持久 generation／JTI 与写入围栏。

另为预先创建、无继承／成员关系的 `identity_key_client` 安装两项命令能力：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v identity_key_role=identity_key_client \
  -f infra/docker/identity-keys-grants.sql
```

此 LOGIN 只有 key 策略和命令执行函数，不含准入函数或直接表权限。运行时分别使用准入与命令两个连接；命令前的哈希工作不持有最终业务锁，原生函数锁当前账号、凭据和目标 key 后共同提交修改与回执。非空 key 操作回执阻止删除该协议的降级，回执随其 Identity 账号的物理删除级联清除。

核心会话使用同一运行时的 `POST /v1/identity/sessions/execute`。会话 LOGIN 安装命令如下，参数与其他函数专用角色采用同一初始化要求：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code -v identity_session_role=identity_session_client \
  -f infra/docker/identity-sessions-grants.sql
```

该 LOGIN 只有策略、登录候选和会话执行三项能力，不含准入、key、Audit 或表权限。登录／刷新／登出／全端登出的当前账号、密码 hash、generation、JTI 和 key 校验与在线状态、业务审计和成功操作回执共同提交。有效登录失败也保留业务审计；格式或主体绑定错误不能确认事务。内嵌装配可设 `IDENTITY_SESSION_DATABASE_URL(_FILE)` 使用该角色，远程服务故障不会回退。

独立进程显式设置 `IDENTITY_DATABASE_URL(_FILE)`、`IDENTITY_TOKEN(_FILE)`、`IDENTITY_SIGNING_SECRET(_FILE)`，每对恰好一项；它不读取 API `.env`。key 命令另设 `IDENTITY_KEY_DATABASE_URL(_FILE)`；会话另设 `IDENTITY_SESSION_DATABASE_URL(_FILE)` 和 `IDENTITY_LOGIN_REDIS_URL(_FILE)`。账户与在线扫描分别设置 `IDENTITY_ACCOUNT_DATABASE_URL(_FILE)`、`IDENTITY_PRESENCE_DATABASE_URL(_FILE)`。未配置某类命令 DSN 时，该 RPC 返回 503，已配置能力仍可就绪。独立 Identity 的签发／派生 secret 不必等于 API edge 的 `SECRET_KEY`；远程调用以 `IDENTITY_TOKEN` 鉴权。现有 Identity 发行与回执账本仍须使用稳定密钥，不能把新隔离环境的不同密钥验收当作已有部署的轮换方案。运行 `uv run --project apps/identity --locked --extra runtime python -m aap_identity_runtime`，或使用可选 `docker-compose.identity.yml` 并提供所启用 capability 的 `IDENTITY_RUNTIME_*_FILE` selector。`/health` 是进程存活，`/ready` 检查原生协议及已配置能力的精确函数权限。Secret 文件须对容器用户可读，源码目录不挂入运行镜像。

会话计数的 Redis 身份只需 `~login_failed:*`、`+get +incrby +expire +del +ping +select +client|setinfo +client|setname`，清除其他命令和频道权限。客户端的 `incr()` 实际发送 `INCRBY key 1`。API 边缘与提供方使用同一计数实例／命名空间；提供方拥有增量、清零及阈值复查，API 保留通用限流和 Turnstile 验证。Redis 不可用时维持现有计数 fail-open。直启配置 `IDENTITY_EXPIRE_MINUTES=1440`、`IDENTITY_EMAIL_VERIFICATION_REQUIRED=false`、`IDENTITY_LOGIN_CAPTCHA_THRESHOLD=5`、`IDENTITY_LOGIN_FAILED_WINDOW_SECONDS=3600`；Compose 使用相应的 `IDENTITY_RUNTIME_*`。这些值须与 API 的签发时长、有效邮箱验证策略、阈值和窗口一致，生产模式下有效邮箱策略可能为 true。Web 登录页收到 HTTP 400、`detail: "captcha_required"` 后展示验证，不从失败次数推算阈值；`X-Login-Failed-Count` 仅用于提示。挑战后的失败会清除已用证明并重建组件；登录成功清除本地挑战。

这次认证切换要求旧 JWT 重新登录，API key 保留原 SQL 状态。先停止全部旧 API／WebSocket／签发与刷新进程，再受控迁移并启动新验证器；不能新旧认证滚动并行或回到接受旧 JWT 的代码。已有用户或撤销记录时，迁移拒绝降级；保留数据向前恢复。独立安装包、只读容器、真实 API／WebSocket 和 Node 准入客户端已通过一次性本地联调，备份还原后保留原生权限和撤销状态。当前日常环境尚未切换；整模块的本地私网资格及部署边界见下文“Identity 整模块私网验收”。详见 [ADR-0115](docs/adr/0115-identity-admission-and-durable-revocation.md)。

复验使用 `scripts/verify_module_data_boundary.py` 创建的独立 PostgreSQL、Redis 和测试桶。先用 `uv sync --project apps/identity --locked --extra runtime --no-editable --reinstall-package aap-identity` 安装当前源码的独立包，再构建 `apps/identity/Dockerfile`。镜像须带 `org.aap.acceptance=identity-admission` 和唯一的 `org.aap.acceptance-id` 标签。设置 `AAP_IDENTITY_RUNTIME_PYTHON` 为该虚拟环境 Python 的绝对路径，`AAP_IDENTITY_RUNTIME_IMAGE` 为镜像名或摘要，`AAP_IDENTITY_RUNTIME_IDENTITY` 为对应标签值，然后运行：

```bash
AAP_IDENTITY_LIVE_ACCEPTANCE=1 AAP_IDENTITY_KEYS_LIVE_ACCEPTANCE=1 AAP_IDENTITY_SESSIONS_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-live.json \
  --test-file tests/test_identity_admission_live.py \
  --test-file tests/test_identity_keys_live.py \
  --test-file tests/test_identity_sessions_live.py \
  --test-file tests/test_identity_recovery.py
```

这组测试启动独立服务和 API，验证当前角色、JWT／API key、登出、全设备登出、WebSocket 重验、停服拒绝及备份恢复；Node 仅是无 Python 依赖的契约客户端。安装内容逐文件匹配源码；容器只读、非 root，限制为 1 CPU／256 MiB。32 次 JWT 请求、并发 8 的本地采样只用于联通与并发冒烟，不能作为生产容量结论。验收器清理自己的基础设施，JSON 和 JUnit 保留实际退出状态；全部跳过不会记为通过，镜像留作工件。

key 命令验收另覆盖无身份表权限的真实 API 调用层、Node 五项操作、丢响应后重启重放、有效期／省略字段／Unicode、撤销与缺失能力拒绝，以及备份恢复后的回执和 secret 变更冲突。公开请求保留原有单次 POST 行为；重放始终要求当前有效凭据，用 key 轮换自身后须换有效凭据重放。算法与轮换边界见 [ADR-0116](docs/adr/0116-independent-identity-key-commands.md) 和[私有运行协议](docs-site/api/guides/identity-runtime.md)。

会话验收检查四项命令的无身份表 API 转发、受限 Redis、精确 64 位声明、登录计数／审计／验证码、重启重放，以及登录实际签发的 JWT 在 REST 和 socket 中使用。回执冻结原始声明，重放仍要求当前认证，不重新登出或推进代次；返回历史 token 不代表它当前有效。公开开放注册与账号签发使用既有 Recovery owner；整模块私网验收见下文，日常环境尚未切换，见 [ADR-0117](docs/adr/0117-independent-identity-session-commands.md)。

### Identity 整模块私网验收

整模块入口复用 `docker-compose.identity.yml`，由验收器启动一个配置全部能力的 HTTP owner、
MailV1、MailV2、Notifications publisher 和单独的 API 冷镜像。PostgreSQL／Redis 只连接
该次验收拥有的 Compose 私网；测试结束先停服务、断开独占依赖，再删除自己的容器／网络。
数据库 clone 使用独占名称、所有权标记和连接检查后删除，不强制终止未知连接。

先按上文准备当前源码的非 editable Identity 安装和镜像，再构建当前 API 镜像。
两个镜像都设置 `org.aap.acceptance=identity-admission`，各自使用不同的唯一
`org.aap.acceptance-id`。除上文的 `AAP_IDENTITY_RUNTIME_*` 外，显式设置
`AAP_IDENTITY_API_RUNTIME_IMAGE` 与匹配标签的 `AAP_IDENTITY_API_RUNTIME_IDENTITY`。
不要把 API 镜像标签值设为 owner 镜像的标签值。

以下案例每次单独选择，使用新的验收数据库；不与会写入共享夹具的其他案例拼接：

```bash
AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-module-private-network.json \
  --test-file tests/test_identity_module_assembly_live.py::test_complete_compose_remote_api_workers_restart_and_restore

AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-module-migration.json \
  --test-file tests/test_identity_module_assembly_live.py::test_complete_identity_migration_roundtrip
```

验收器保留原有 seed profile，另建无继承／成员关系的 `identity_api_consumer`；先应用兼容
应用授权，最后应用 `identity-consumer-grants.sql`。API 的 Identity 权限只有两项公开关系
和五项事务函数，不能读密码／key hash／私有回执或写 User。API 的签名密钥与 owner 不同，
也没有 Identity 命令 DSN、owner 私钥或邮件 key。API 启动仅获得隔离桶的元数据／生命周期
权限；UserBrief 使用不需要读取对象内容的点云任务夹具。

当前私网装配连续两轮通过真实 API／Socket、Node 重放、邮件、通知和完整备份恢复；
非 editable 安装包 658 项、包／镜像指纹及完整五阶段迁移均通过。旧宿主机数据库端口转发
实验已复现底层 TCP 建连故障，但历史单次业务 503 的归因仍未确认，不能将私网通过视为修复。固定预算和有限并发样本、
精确证据及尚未覆盖的环境见[模块台账](docs-site/dev/concepts/module-deployment.md#identity-整模块独立装配)。
日常运行环境没有切换。

旧宿主机 PostgreSQL 转发路径的定向诊断复用上述镜像和凭据。两项用例各自单独
选择并使用新的验收数据库；普通 API 的凭据、签名隔离与 Compose 资源检查保持。

```bash
AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-forwarding-traced.json \
  --test-file 'tests/test_identity_module_assembly_live.py::test_host_forwarded_identity_rpc_failures[traced]'

AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-forwarding-untraced.json \
  --test-file 'tests/test_identity_module_assembly_live.py::test_host_forwarded_identity_rpc_failures[untraced]'
```

插桩只记录 RPC 路径、阶段、耗时、原始状态、异常类型、SQLSTATE、函数和数据库身份，
不记录请求内容、令牌、密码或任意 SQL。每项包含真实邀请／离职、连接拒绝及恢复，
以及完整备份恢复后的资料读取；受控拒绝通过修改一次性 PostgreSQL 中普通邀请
LOGIN 的连接上限实现，并在 finally 恢复。诊断收据路径写入 JUnit property。
两项均已通过；它们仍未复现历史间歇故障，不能作为故障已修复或生产容量结论。

保留原始完整业务顺序的复放入口如下，仍须每项使用全新验收数据库：

```bash
AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-forwarding-original-traced.json \
  --test-file 'tests/test_identity_module_assembly_live.py::test_host_forwarded_identity_original_sequence[traced]'

AAP_IDENTITY_LIVE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/identity-forwarding-original-untraced.json \
  --test-file 'tests/test_identity_module_assembly_live.py::test_host_forwarded_identity_original_sequence[untraced]'
```

这两项复用原整模块用例，覆盖邮件／通知交互、Node／Socket、停服、重放和完整恢复。
插桩收据在停止／切库前保留日志，并记录 API 响应体读取、异常转换及连接取消的安全
堆栈帧。两项已通过，但恢复期间的就绪探测实际遇到过一次连接超时。独立传输探针
还复现了宿主机转发的 TCP `sock_connect` 超时；这与应用 SQL 错误不同，且不能据此
逐次归因历史三类业务 503。当前独立部署仍使用已通过验收的 Compose 私网直连。

手动筛选 CLI 的 Identity 边界另用真实生成的普通 E2E LOGIN 验收：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/identity-filtering-cli.json \
  --test-file tests/test_worktree_filtering_identity_native.py
```

同一案例实际运行 CLI，检查停用账号也会触发非空库拒绝、首次播种可完成、重复运行保留
用户修改与数据计数，且普通账号全程不能访问 Identity 私表。该检查已通过；与旧源码的
失败对照、调用签名修复及完整记录见 `.worktree/identity-forwarding-original-qualification.json`。

### 独立自助账户命令

安装当前迁移并重新应用 `module-data-grants.sql` 后，为无角色成员关系的 `identity_account_client`、`identity_presence_client` LOGIN 授权；数据库须已撤销 PUBLIC TEMP。账户角色只有策略、准备、提交三个函数，扫描角色只有一项批量更新函数；不能直接访问 User、回执或 Audit 表：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code \
  -v identity_account_role=identity_account_client \
  -v identity_presence_role=identity_presence_client \
  -f infra/docker/identity-accounts-grants.sql
```

API 的 `IDENTITY_ACCOUNT_DATABASE_URL`／`_FILE` 可选择内嵌装配的账户函数连接，`IDENTITY_PRESENCE_DATABASE_URL`／`_FILE` 选择在线扫描连接。每对至多一项，DSN 为 `postgresql://`；留空使用当前调用方事务。显式 `IDENTITY_URL` 使用同一独立进程中的账户与扫描端点，失败不回退。Compose 对应 `IDENTITY_RUNTIME_ACCOUNT_DATABASE_URL_FILE` 和 `IDENTITY_RUNTIME_PRESENCE_DATABASE_URL_FILE`。工作树启动器清除继承的共享 Identity 连接配置。

资料读取、改名、偏好读写、心跳、自助改密和头像引用均走账户命令。准备、偏好合并／bcrypt 和提交保留在同一所有者连接与事务，按 User → 凭据次序锁定；偏好等待最新行后深合并，其他写入竞争仍返回 409。只有名称／偏好／密码／头像修改保存成功回执；心跳按数据库时间执行，不按每次轮询堆积回执。自助改密仍保留当前 JWT 代次和 API key。

账号偏好不继承会话请求的 64 KiB 或准入响应的 16 MiB 限额，原有多快照与任意 AI 参数仍可传递。部署的内存、CPU、并发与截止时间须按实际数据测量；较大偏好会增加序列化及持锁时间，不能用小样本外推生产容量。

头像字节处理不持远程 SQL 锁。确定拒绝时清理新暂存对象；提交结果未知时保留它，先读取当前资料或重放已保存的私有操作 UUID 确认结果，再清理旧对象。公开接口未增加自动重试或幂等 header。自助改密的 422 响应保留定位／说明，剔除原始输入和异常上下文；API SQL 调试输出隐藏绑定参数。

账户的隔离验收通过同一验证器显式启用：`AAP_IDENTITY_ACCOUNTS_LIVE_ACCEPTANCE=1`，并选择 `tests/test_identity_accounts_live.py`、`tests/test_identity_recovery.py`。安装包／镜像设置沿用上文，Node 夹具覆盖七项账户操作与扫描；它是消费者，尚非另一语言提供方。完整协议、回执、头像清理和剩余范围见 [ADR-0118](docs/adr/0118-independent-identity-account-commands.md)。

### 注册、密码恢复与凭据邮件

Identity recovery 包含五个私有操作：`register_open`、`request_password_reset`、`resend_email_verification`、`consume_password_reset` 和 `consume_email_verification`，通过 `POST /v1/identity/recovery/execute` 的 `aap-identity-recovery/v1` 协议调用。API 仍持有 CAPTCHA、每分钟三次限制与公开 HTTP 翻译；忘记密码和验证邮件重发在 owner 接受后返回原有通用 202，投递由 durable outbox 异步完成。开放注册要求邮箱验证时，User、挑战、初始邮件 intent 与 `USER_REGISTER` Audit 同一事务提交，不返回 JWT；关闭验证时签发既有 JWT 形状。新账号默认 `viewer`、无项目分组。

Native 0239 将两个 token 表的现存明文 token 原地改为 SHA-256 摘要，保留已经签发的链接，并冻结 `issued_email`。Recovery owner 保存 operation HMAC 回执；mail outbox 仅存 token 摘要及 Fernet 密文。原 token 不进入 API／RPC 结果或日志。Reset 会消费挑战、更新密码、清除管理员重置标记并推进 JWT generation；API key 不因此撤销。邮箱验证可验证 inactive 用户而不重新激活或改变 generation。两个挑战在 `now >= expires_at` 时失效；同一邮箱的旧有效链接仍各自可用一次。

为预先创建、无成员关系的 `identity_recovery_client`、`identity_mail_client`、`identity_mail_settings_client` 安装最小函数权限；先应用当前迁移并重新应用 `module-data-grants.sql`，数据库初始化须已撤销 PUBLIC TEMP：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v identity_owner=identity_code \
  -v audit_owner=audit_code -v settings_owner=settings_code \
  -v identity_recovery_role=identity_recovery_client \
  -v identity_mail_role=identity_mail_client \
  -v identity_mail_settings_role=identity_mail_settings_client \
  -f infra/docker/identity-recovery-grants.sql
```

Recovery LOGIN 只有 `recovery_policy()`、`prepare_recovery_command(jsonb)`、`apply_recovery_command(jsonb)`、`read_registration_status(jsonb)`；MAIL LOGIN 只有 `mail_policy()`、`claim_mail(jsonb)`、`renew_mail(jsonb)`、`finish_mail(jsonb)`；`MAIL_SETTINGS` LOGIN 只有 Settings 的 `mail_delivery_policy(jsonb)`。Identity code owner 另只能调用 `aap_settings_v1.identity_recovery_policy(jsonb)` 读取开放注册与 frontend URL 策略；它用 Settings SHARE 锁围栏注册策略。SMTP snapshot 只含 `smtp_host`、`smtp_port`、`smtp_user`、`smtp_password`、`smtp_from`，由 `MAIL_SETTINGS` function 一次返回，数据库值覆盖环境 fallback；credential mail 不读取通知偏好。角色不得拥有 recovery token、outbox、User 或 Settings 的直接表权限，也不得是其他角色的成员。

`identity-mail` 使用同一 `apps/identity/Dockerfile` 制成的 `aap-identity:local` 镜像，命令为 `python -m aap_identity_runtime.mail_worker`。它只得到 MAIL DSN、`MAIL_SETTINGS` DSN、Fernet key，以及 `IDENTITY_MAIL_SMTP_HOST`、`IDENTITY_MAIL_SMTP_PORT`、`IDENTITY_MAIL_SMTP_USER`、`IDENTITY_MAIL_SMTP_PASSWORD`、`IDENTITY_MAIL_SMTP_FROM` fallback；不配置 AUTH、control bearer 或 JWT signing secret，也不接收 Recovery DSN。Worker 每次领取一条 intent 并先提交 lease，再做 SMTP；claim、续租和 finish 使用短事务，绝不持 SQL 锁发信。默认 lease 60 秒，发送期间续租；lease 与 retry delay 限在 1–300 秒。稳定 Message-ID 为 `<aap.{delivery_id}.{purpose}@aap.invalid>`。SMTP 是 at-least-once：发送开始后的断连会按 unknown 处理，并用相同 token 密文与 Message-ID 重排队，允许重复邮件。`delivered` 和确定 `rejected` 终态；`not_configured` 也是确定拒绝，只回收该 reset token，验证 token 仍有效。retry、unknown 与 invalid_key 保留 intent；过期 intent 终止。

Fernet key 应单独配置，不与 JWT secret 共用。轮换时保留旧 key，直到旧密钥加密的待投递行排空；换 key 不会改变 hash-only 链接的有效性，但缺少旧 key 的行无法解密投递。SMTP 超时后仍可能产生重复邮件，不能把结果未知当成未发送。

`.env.example` 区分 API 配置与 Compose 宿主机的 secret-file 选择器。API 可配置八个字段，即四组直接值／文件：`IDENTITY_RECOVERY_DATABASE_URL` 与 `IDENTITY_RECOVERY_DATABASE_URL_FILE`、`IDENTITY_RECOVERY_MAIL_KEY` 与 `IDENTITY_RECOVERY_MAIL_KEY_FILE`、`IDENTITY_MAIL_DATABASE_URL` 与 `IDENTITY_MAIL_DATABASE_URL_FILE`、`IDENTITY_MAIL_SETTINGS_DATABASE_URL` 与 `IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE`；每对最多设置一项。Recovery DSN 留空时使用 API 调用方事务；指定 Recovery DSN 后，MAIL 与 `MAIL_SETTINGS` 两个 DSN 必须同时指定，否则 fail closed，不会静默轮询 API 数据库。若两个 mail DSN 都留空，嵌入式 mail kick 复用 API session。若 API 选用远程 `IDENTITY_URL`，该 Identity owner 旁必须部署自己的 mail worker；本 API 不会扫描远程 outbox。Compose 主机路径选择器分别是 `IDENTITY_RUNTIME_RECOVERY_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE`、`IDENTITY_RUNTIME_MAIL_DATABASE_URL_FILE` 和 `IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE`；Admission 容器映射为 `IDENTITY_RECOVERY_DATABASE_URL_FILE`、`IDENTITY_RECOVERY_MAIL_KEY_FILE`，Mail worker 映射为 `IDENTITY_MAIL_DATABASE_URL_FILE`、`IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE`、`IDENTITY_MAIL_ENCRYPTION_KEY_FILE`。上述三个 PostgreSQL DSN 文件与共用 Fernet key 文件都必须显式配置；secret-file 读取失败不会回退。

普通单体 API 的 `application_role` 增加了 Identity public-function allowlist，源文件为 `infra/docker/lan-production-grants.sql`；这是 grants source 的改动，尚未应用到生产数据库。隔离 service 使用上面的三个专用 LOGIN 与同一函数集合，不把这项单体授权解释为 API 全角色已实现最小权限。

独立运行时启用 Recovery DSN 时，必须显式提供共享 Fernet key；API 默认内嵌模式才保留由签名 secret 派生邮件 key 的兼容路径。

Producer 的直启策略名是 `IDENTITY_ALLOW_OPEN_REGISTRATION`、`IDENTITY_EMAIL_VERIFICATION_REQUIRED` 和 `IDENTITY_FRONTEND_BASE_URL`；Compose 主机端以 `IDENTITY_RUNTIME_ALLOW_OPEN_REGISTRATION`、`IDENTITY_RUNTIME_EMAIL_VERIFICATION_REQUIRED`、`IDENTITY_RUNTIME_FRONTEND_BASE_URL` 提供值。SMTP 环境 fallback 与主 API 现存的 `SMTP_*` SMTP 测试邮件配置分开；Settings 中的 mail policy 优先。mail worker `/health` 为 liveness，`/ready` 检查 8171 端口上的原生能力、Settings 与 key；Compose 覆盖镜像原先检查 8170 的 healthcheck，容器只读并以 UID 10003 运行。

Recovery/mail、分组目录／指派、自助注销／outbox、完整 invitation lifecycle／MailV2、四项 Identity Directory reads、管理员临时密码重置、平台角色预览／变更、管理员暂停／恢复、管理员 DELETE、offboarding preview/commit 与批量 UserBrief reads 均通过各自限定的本地资格。Directory 有 38 个选定数据库／进程案例；admin-reset 有 42 个选定联合案例（含兼容路径）；platform-role 有 56 个选定 DB/process 案例，均按自己的切片统计。Suspension 有 77 个选定 DB/process 案例、零跳过（73 DB/restore 加 4 live）；管理员 DELETE 有 72 个 selected cases（71 DB/process、1 static artifact），零跳过；offboarding 有 77 个 selected cases（76 DB/process、1 static artifact），零跳过；UserBrief 有 64 个 distinct selected cases（59 DB/process、5 pure/static），零跳过。细节分别见 [ADR-0127](docs/adr/0127-independent-identity-deletion-commands.md)、[ADR-0128](docs/adr/0128-independent-identity-offboarding-commands.md) 与 [ADR-0129](docs/adr/0129-identity-batched-user-brief-reads.md)。各切片的精确哈希、样本和范围见[后端解耦计划](docs/plans/1792160873_backend-module-decoupling.md)。公开 API 继续聚合这些 owner 能力；Identity 的本地私网模块资格见上文，日常认证环境未切换，也没有另一语言完整 provider；Go 月报仍是独立实验。详见 [ADR-0119](docs/adr/0119-independent-identity-recovery-and-mail.md)、[ADR-0120](docs/adr/0120-independent-identity-group-catalog-and-assignment.md)、[ADR-0121](docs/adr/0121-identity-self-deactivation-and-notification-delivery.md)、[ADR-0122](docs/adr/0122-independent-identity-invitations-and-mail-v2.md)、[ADR-0123](docs/adr/0123-independent-identity-directory-reads.md)、[ADR-0124](docs/adr/0124-independent-identity-admin-password-reset.md)、[ADR-0125](docs/adr/0125-independent-identity-platform-role-commands.md) 与下方 [ADR-0126](docs/adr/0126-independent-identity-suspension-commands.md)。

### Identity 分组目录与指派

私有 `POST /v1/identity/groups/execute` 以 `aap-identity-groups/v1` 承载全局目录读取／查询、创建／改名／删除、单人指派、批量预览和批量执行八项操作。公开管理路由仍保留现有权限、错误映射与 `BulkGroupAssignmentPreview`／`BulkGroupAssignmentResponse` DTO。`GroupRecord.member_count` 只统计启用用户；查询按 Python Unicode `str.strip()` 规则修剪，并在组名／描述上做 ILIKE，不加搜索长度上限。批量 user_ids 保留重复值与顺序，长度为 1–500。

项目管理员和超级管理员都能管理全局目录。超级管理员可指派任意用户；项目管理员只可指派自己、当前所属项目成员或启用 employee，批量操作额外保留其所属项目内的停用成员；单用户指派目标必须启用。越权目标与不存在目标不能从响应中区分。批量仍逐项返回并允许部分成功；应用不存在的 group 时整体 404，预览则报告逐项错误。

先应用 migration 0240 与当前 `module-data-grants.sql`，再预建无成员关系、非继承的 `identity_group_client` LOGIN，并按 `infra/docker/identity-groups-grants.sql` 文件头配置 owner 与角色参数。该 LOGIN 仅获得 `group_policy()` 和 `execute_group_command(jsonb)` 两项执行权限；不能直接读写 Group、User、Project 或 ProjectMember 表，也不能继承 owner。不要另外给 API 角色表权限或绕过 Workspace 投影。

API 可用 `IDENTITY_GROUP_DATABASE_URL`／`IDENTITY_GROUP_DATABASE_URL_FILE` 为组命令选择专用函数 DSN，每对至多一种；留空使用 API 调用方事务，显式文件或远程服务失败不会回退。独立 Identity 进程使用 `IDENTITY_GROUP_DATABASE_URL(_FILE)`，缺少已选能力时返回 503。可选 Compose 的宿主机路径变量为 `IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE`，容器内映射为 `/run/secrets/identity_group_database_url`；文件必须可由 runtime 用户读取。两个 DSN 都应指向承载 Workspace 与 Identity owner schema 的同一个 PostgreSQL 数据库，以保留外层事务和投影锁。

Group 写入先锁目标 Group，再按 owner 规则锁 actor/target User。Workspace 的 `aap_workspace_v1.lock_group_assignment_scope(uuid,uuid[],boolean)` 按 Project 在先、membership 在后的顺序锁定并重查范围；写入锁持有到调用事务提交。改名／删除需按 UUID 顺序预锁受影响的全部用户。争用 NOWAIT 仅转换为可安全重试的 409／逐项结果，不得暴露 SQL 诊断。组名空白和重复名的状态码、旧错误文本及 Unicode trim 保持既有语义。

Create/update/delete、单人指派和批量 apply 写成功操作 receipt，并与业务状态及类型化 Audit 同事务提交；批量执行即便逐项全部失败也冻结受理结果。List/query/preview 不写 receipt。重放重新核验当前 manager；单人指派重放还必须重新确认目标当前可见，不能借历史回执泄露资料。Audit 仅允许 `group.create`、`group.update`、`group.delete`、`user.group_change` 和 `user.bulk_group_assign` 固定事件；审计入口接收封闭的 `target_type`、`target_id`、`detail`，只持久化允许的业务 detail。

The earlier group slice reused private `resolve_invitation_group(uuid,text)` only to preserve its legacy accept path; the full invitation owner below now moves resolve/register/accept and management operations to Identity. The helper remains private and is not granted to `identity_group_client`. User directory, platform-role administration and other lifecycle flows remain outside these slices. Group fields, errors, Project scope, Audit and receipts remain in [ADR-0120](docs/adr/0120-independent-identity-group-catalog-and-assignment.md).

### Identity 自助注销与 durable 通知投递

公开 `/api/v1/auth/me/deactivation-request` 的 POST/DELETE 保持现有路径和 `UserOut`；身份命令位于 `POST /v1/identity/deactivation/execute`，使用 `aap-identity-deactivation/v1` 的 request/cancel 两项用户命令。到期处理位于 `POST /v1/identity/deactivation/process-due`，使用 `aap-identity-deactivation-due/v1` 的 operator 页面；它不接收用户 token、指定 user IDs 或调用方时钟。七天冷静期和 Python `str.strip()`/reason 长度保持既有语义，申请／撤销响应不新增原因字段；Audit detail 与通知 payload 不包含原因正文。

申请、取消与到期处理共用 versioned Identity owner。自助申请已经接受后，管理员 suspension/reactivation 不会取消它；仅显式取消或成功到期注销清除三个 `deactivation_*` 请求字段。请求超级管理员注销时必须至少还有另一名启用且未预留注销申请的超级管理员；Legacy API 的超管降级／停用／删除／离职也共用同一全局 try-advisory fence。所有这些减员操作先取 fence 再按 UUID 排序锁用户，争用返回 `409`。到期对每个超级管理员再核对当前实际启用数；若它将成为最后一个，保留申请／ledger 并返回 `blocked_last_super_admin`，不批准、不审计、不发完成通知。

原 Celery task `app.workers.deactivation.process_deactivation_requests` 和每日 04:00 UTC 调度不变；每页 transaction limit 为严格整数 1–500（默认 256），不是全局上限。worker 用 Celery request UUID 作为稳定 run ID、`uuid5(run_id,"batch:N")` 固定 page operation ID；重试复用同一页回执，随后继续排空直到没有进展。旧 task 响应仍为 `{"processed": n}`，`n` 只统计成功注销用户，blocked/examined 留在私有页结果。到期 soft-delete 只标 inactive/offline 并更新既有停用 metadata，不推进 JWT generation、不吊销 API key，也不转交任务／项目。审批 Audit 仅在插入固定事件后将现有历史的 `actor_email`／`actor_role` 置空，保留 `detail_json` 和已存在通知 payload；这不是完整 GDPR 删除。

Native migration 0241 按顺序安装 `0241_deactivation_notifications.sql`、`0241_identity_deactivation.sql`，之后应用当前 module／legacy grant sources。隔离 LOGIN grant 文件是 `infra/docker/identity-deactivation-grants.sql`，只安装三类无继承角色：`identity_deactivation_client` 可执行 `deactivation_policy()`／`execute_deactivation_command(jsonb)`；`identity_deactivation_operator` 可执行 `due_policy()`／`process_due_deactivations(jsonb)`；`notification_publish_client` 可执行 Notifications publisher 的 policy／claim／finish 三项函数。它们没有业务表权限。API 的 legacy `application_role` 只经 `module-data-grants.sql` 与 `lan-production-grants.sql` 获得私有最终管理员状态／锁 helper；这份 grant source 修改未应用到生产。

API 与独立 Identity runtime 的用户能力 DSN 为 `IDENTITY_DEACTIVATION_DATABASE_URL(_FILE)`；到期 operator 使用 `IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL(_FILE)`。这两项在 API 可选，直接配置或 `_FILE` 每对最多一项；独立运行时省略所选能力时对应路由返回 503，显式 DSN 失败不回退本地 ORM。

注销 request/completion 两种 Notification 由 Notifications owner 在 Identity 业务事务中写入 `notification_delivery_outbox`，创建时应用现有 in-app preference truthiness，不含注销原因。单体 API/Celery relay 可选 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` 并复用现有 task Redis 配置，每 10 秒运行；Notifications 独立入口为 `aap-notifications-publisher` 或 `python -m aap_notifications.delivery_worker`，原 Identity 模块命令保留兼容转发。默认持续 poll，`--once` 仅领取一个 batch，只接收原 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` 与 `NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`。两种执行方式使用同一个 lease executor；claim 先提交，Redis 接受前不确认，重试沿用同一 notification ID，投递为 at-least-once。publisher 没有 HTTP health endpoint，也没有 Identity/AUTH/control/JWT/SMTP 配置。`apps/notifications/Dockerfile` 使用其独立锁文件构建非 editable 运行包；已有 Identity image 同时安装 Notifications，以保留原入口。

可选 `docker-compose.identity.yml` 中 `identity-admission` 仅挂载 deactivation user/operator DSN；独立 `identity-notification-publisher` 服务只获得 Notifications 数据库与 Redis 两个 secret，并以同一 Identity image、非 root、只读方式启动。宿主机路径选择器分别为 `IDENTITY_RUNTIME_DEACTIVATION_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_DEACTIVATION_OPERATOR_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE`；运行容器内使用 `IDENTITY_DEACTIVATION_DATABASE_URL_FILE`、`IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL_FILE`、`NOTIFICATION_PUBLISH_DATABASE_URL_FILE`、`NOTIFICATION_PUBLISH_REDIS_URL_FILE`。DSN 必须指向同一 PostgreSQL owner database，以共同提交身份状态、Audit 与通知 outbox。完整协议、最后管理员状态、Audit 边界和 worker recovery 见[Identity 私有运行协议](docs-site/api/guides/identity-runtime.md#自助注销与最后一名超级管理员围栏)、[审计与通知](docs-site/dev/concepts/audit-and-notifications.md#自助注销的-durable-delivery)和 [ADR-0121](docs/adr/0121-identity-self-deactivation-and-notification-delivery.md)。

Identity deactivation/outbox 的既有本地证据继续有效；发布器归属迁移的源码／独立安装与实际新进程验收分别记录，不把旧宿主机执行标为新 Notifications 镜像结果。Identity 整模块与 Notifications 剩余能力状态见[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)，逐项历史证据见[后端解耦计划](docs/plans/1792160873_backend-module-decoupling.md)。Go 月报仍是独立实验。

独立镜像的定向用例是 `tests/test_notifications_publisher_image_live.py`，通过现有验证器的 `--test-file` 选择。仅在新建、独占的验证器测试库中运行，并显式设置 `AAP_NOTIFICATIONS_DELIVERY_LIVE_ACCEPTANCE=1`、不可变 SHA 镜像引用 `AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE`、对应 label 的 `AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IDENTITY`／`AAP_NOTIFICATIONS_DELIVERY_RUNTIME_SOURCE_REVISION`，以及已安装非 editable Identity 工件的 `AAP_IDENTITY_RUNTIME_PYTHON`。验证器提供测试角色、隔离 Redis 与依赖容器身份；用例先拒绝已有 pending outbox／发布器会话，再通过私网验证发布授权、重试、空批次重启和进程死亡后的租约接管。受控推进只作用于该用例的回执时间，不能据此推断生产恢复时限；密钥文件在自有容器清理后删除。

### Identity 邀请 owner 与 MailV2 durable sender

本次把完整 invitation lifecycle 从 API ORM workflows 移到 Identity，而不是只挪 `InvitationService`。私有 `POST /v1/identity/invitations/execute` 使用 `aap-identity-invitations/v1`，覆盖 13 项操作：单人创建、批量预览／应用、数组读取、分页查询、统计、CSV/JSON 导出、撤销、换新链接、发送当前链接、匿名链接解析、首次注册和已有账号接受。公开路由、schemas、SDK fields、错误／HTTP status 与导出格式保持当前契约。

`identity_invitation_client` LOGIN 只有 `invitation_policy()`、`prepare_invitation_command(jsonb)`、`apply_invitation_command(jsonb)`。API 与独立 Identity runtime 分别配置 `IDENTITY_INVITATION_DATABASE_URL(_FILE)`；Compose Admission 用 host selector `IDENTITY_RUNTIME_INVITATION_DATABASE_URL_FILE` 挂载。没有此 capability 时所选 runtime 返回 503，明确选择连接失败绝不回退 API User/Invitation ORM。角色只可执行函数，不获得业务表、序列、DDL 或 GROUP/MAIL helper 权限。

Migration 0242 按顺序安装 `0242_identity_invitations_support.sql`、`0242_invitation_mail.sql`、`0242_identity_invitations.sql`，先建立跨 owner 协调能力和 mail support，再安装邀请 RPC。受理／接受以当前 inviter、User credentials、project owner、Invitation token 和 Group/Workspace member 权威共同围栏；事务提交原子包含 User、Group、ProjectMember、token 状态与 typed Audit。Labeling 的当前 active-batch count 只作完成页 work hint，不创建或分配任务。

邀请账号／项目角色、批量与查询保真、 quota/TTL、锁序、receipt/Audit 与安全失败细节见 [Identity 私有运行协议](docs-site/api/guides/identity-runtime.md#邀请生命周期) 和 [ADR-0122](docs/adr/0122-independent-identity-invitations-and-mail-v2.md)。项目管理员仍只能管理自己创建的邀请；管理员邀请角色仅由超级管理员签发；已接受邀请不编辑平台角色。老 token proof 列保持 authoritative，不迁移为 hash；值 1–64 Unicode 字符保持 opaque。

邀请 email 使用 separate `aap-identity-mail-native/v2`，不改变 recovery `aap-identity-mail/v1`。`identity_mail_v2_client` 只有 `mail_policy()`、`claim_mail(jsonb)`、`renew_mail(jsonb)`、`finish_mail(jsonb)` 四个函数；V1 client 保持仅领取 password-reset 与 email-verification purpose。API `send-email` 保留 HTTP 200/502 与原 invitation link；Identity durable-accept 后最多等默认 60 秒（上限 120）查询 private pending/sent/failed，timeout 后仍为 502 而不是 202，已接受 intent 仍能被 worker 接管。

独立 sender `python -m aap_identity_runtime.mail_worker_v2` 通过可选 `mail-v2` Compose profile 运行。它只得到 MailV2 DSN、`MAIL_SETTINGS` DSN、Recovery Fernet key 与 SMTP fallback，不带 Identity/Auth/control/JWT signing secret；邮箱/链接发送在 lease commit 后执行，不持 SQL 锁。host `IDENTITY_RUNTIME_MAIL_V2_DATABASE_URL_FILE` 选择自己的四函数 DSN；现有 `IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE` 与 `IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE` 挂载复用，旧的 `IDENTITY_MAIL_SMTP_*` host fallback 映射成 `IDENTITY_MAIL_V2_SMTP_*`。SMTP unknown outcome 是 at-least-once，同 Message-ID 可能重发；当前 invitation 仍保持 valid。API embedded kick 可选 `IDENTITY_MAIL_V2_DATABASE_URL` 或 `IDENTITY_MAIL_V2_DATABASE_URL_FILE`，不注入 SMTP 凭据到 producer。轮换 Fernet key 时保留旧 key，直到相关 pending 邮件送达或被人工处理。

邀请／MailV2 已通过本地限定资格；68 个数据库／进程用例、375 个已安装 Identity package 检查、324 个 API assembly 检查及其边界见[后端解耦计划](docs/plans/1792160873_backend-module-decoupling.md)。这些历史数字只覆盖 Invitation/MailV2；Directory 采用后文独立的 38 个选定数据库／进程案例和当前安装、API、镜像证据。

### Identity 用户目录只读 owner

Directory slice 只覆盖 `GET /api/v1/users`、`/users/query`、`/users/stats`、`/users/export` 四项读取，私有协议是 `aap-identity-directory/v1` 的 `list_users`、`query_users`、`user_stats` 和 `export_users`。API/runtime 可选专用 Identity Directory LOGIN DSN：环境名为 `IDENTITY_DIRECTORY_DATABASE_URL` 或 `IDENTITY_DIRECTORY_DATABASE_URL_FILE`，每对最多设置一项；独立 Compose 主机侧用 `IDENTITY_RUNTIME_DIRECTORY_DATABASE_URL_FILE` 选择 secret 文件，容器内映射回 `IDENTITY_DIRECTORY_DATABASE_URL_FILE`。Host selector 留空时 Directory capability 关闭、私有路由返回 503，既有 Identity Compose 服务仍可启动；只在启用该能力时设置对应 secret file。这项能力只有 `directory_policy()` 与 `execute_directory_command(jsonb)` 两个函数权限，不授予 User/Workspace/Labeling 表读权。

Public list 的 project-admin active-employee picker 与 query/stats/export 的 global read-only super-admin arm 是两套旧行为，必须分开保留。成员、管理和 lifecycle flags 经 Workspace 与 Labeling 的批量 typed projections 在同一数据库 snapshot 计算；export receipt 只冻结 8 个导出字段、metadata 与 Audit ID，重放前重新按当前身份及原 `project_id` 核验冻结用户可见性，filters 本身不重算。协议、CSV header、Audit detail 和导出失联行为见[Identity 私有运行协议](docs-site/api/guides/identity-runtime.md#用户目录读取)与 [ADR-0123](docs/adr/0123-independent-identity-directory-reads.md)。Directory 本地资格为 32 个 native/role cases、3 个 restore cases 和 3 个 live/API cases；详见后端解耦计划。之前邀请的 68/375/324 数字仍只属 Invitation/MailV2，不能算作 Directory 或完整 Identity qualification，也不代表生产或多语言 provider 已完成。

### Identity 内部批量 UserBrief 读取（已通过限定本地资格）

现有 `app.services.user_brief.resolve_briefs` 与 `resolve_briefs_with_project_role` 是内部 enrichment helpers，不是可供用户任意查询资料的 public API。所有调用方先授权自己的任务、项目或结果行，再传相关用户 ID；helper 本身不接收 actor，不承担 manager/Directory 权限判断。此 slice 保留调用签名与 `UserBrief` 六字段 `id/name/email/role/avatar_initial/avatar_ref`，不改 public HTTP、SDK、分页、错误或响应 schema。

批次输入省略 `None`，规范化 UUID 字符串，接受重复与超过 500 个 ID；相同 UUID 在输出 map 中只出现一次，键为规范 UUID 字符串。空输入直接返回 `{}`，无 SQL 或远程调用。非法 UUID 在 owner call 前继续抛原 `ValueError`；不存在的 User 省略。保留 inactive、offline、pending-retirement 与历史 role 的行，不增加过滤、ID/body cap、缓存、审计或名称/avatar 清理。Project 读取用该项目 truthy `ProjectMember.role` 覆盖全局 role；成员角色缺失或空字符串时回退到 User.role，owner 与历史 assignee 即使无成员行也能得到 brief。

`avatar_initial` 保持旧 Python `(name or email or "?").strip()[:1].upper()` 语义：空白 name 不退回 email，也不做 Unicode normalization；一个字符可能大写成多个字符，如 `ß` 得到 `SS`。Wire 结果按 canonical UUID 文本排序并校验六字段；用户缺失不制造占位记录。Native source 只返回 `id/name/email/role/avatar_ref` 五字段，初始字母由 Python owner 计算。

私有 endpoint 是 `POST /v1/identity/briefs/execute`，仅接受现有内部 control Bearer 与 `application/json`。Wire schema 为 `aap-identity-briefs/v1`；operation 为 `get_user_briefs` 或 `get_project_user_briefs`，后者带 `project_id`。没有 actor/token/nonce/operation ID/audit context、Directory manager gate 或新 public route。空/重复 ID 数组合法且不加 body-size cap；重复 JSON keys、NaN/Infinity、`protocol` 字段别名、未知字段、非字符串 UUID 与数值/布尔/null ID 均拒绝。无效请求为 400；坏 control bearer 为 401；未配置 capability 或超时/owner 错误返回 503，统一 unavailable detail；运行时沿用已有 semaphore 与 timeout。

API 配置可选 `IDENTITY_BRIEFS_DATABASE_URL`／`IDENTITY_BRIEFS_DATABASE_URL_FILE`（二选一）。留空沿用 caller Session；若已有 `IDENTITY_URL`，它优先并走 private HTTP。独立 Identity runtime 使用同一 DSN pair，留空时 briefs capability disabled；Compose host selector 是 `IDENTITY_RUNTIME_BRIEFS_DATABASE_URL_FILE`，缺省挂 `/dev/null`。Migration 0249 计划创建 `aap_identity_briefs_v1`，`identity_briefs_client` 仅获 `briefs_policy()` 与 `execute_briefs_command(jsonb)` 两个函数权限。Workspace 暴露只返回所给项目／用户 ID 的 `(user_id, role)` 只读 capability；Identity 不拿 ProjectMember 原表权限。原生源函数用稳定 `SECURITY DEFINER` 并封闭 search path，禁止 public execute/foreign-schema/DDL/TEMP grants。

Embedded SQLAlchemy adapter 必须留在调用方同一事务并且从不 commit/rollback；只有 `db.autoflush` 为 true 且不在 `no_autoflush` 块中时，text SQL 前才 flush。专用 asyncpg binding 拥有只读事务并总是关闭连接。两种绑定都不能在选定远程/DSN 出错时回退到 API ORM。Root 已在专用 0248 DB 上通过并清理旧 helper baseline，六用户夹具覆盖 global/project 与 1,201 IDs；legacy query calls 为 empty=0、global=1、project=2、large=1。这只固定兼容行为，不代表新 owner/remote runtime 性能结论。最终 owner union 为 64 个 distinct cases（59 DB/process、5 pure/static），零跳过；source/installed suites 各 549 项通过、114 个安装文件 hash 匹配，API assembly 625 项通过并明确延期 132 条规则，Node 21 与 SDK 43 项通过。Migration 0249 restore、受限 owner/cold API images、caller/API bindings 与 Compose selector checks 通过；精确 hashes、case 分类、清理和冷镜像时长边界见 [ADR-0129](docs/adr/0129-identity-batched-user-brief-reads.md)。这些数字只限定 UserBrief slice；整模块当前资格见“Identity 整模块私网验收”。生产容量、十模块／七门槛、另一语言完整 provider 与 primary cutover 仍未完成。

### Projects 成员列表的 UserBrief 消费

`projects.list_members` 保留 Workspace 的成员授权、`assigned_at DESC` 顺序、项目职责和成员元数据；它在完成 ACL 检查后，对选中的用户 ID 调用一次全局 `resolve_briefs`，补充原有响应中的姓名、邮箱、平台角色和头像引用。项目职责仍来自 membership，`platform_role` 仍是必填的四值枚举；不增加 `avatar_initial` 或任何公开字段。空列表不访问 Identity，缺失 profile 继续按旧 inner-join 行为省略，停用用户不额外过滤。无效历史平台角色仍触发既有 DTO 校验错误。相较旧 JOIN，这条路径增加一次批量资料读取，不产生逐用户 RPC；远程 Workspace 与 Identity 查询没有共同快照。

此消费者单独通过 21 个选定案例（18 DB/process、3 pure/static），零跳过；其中也包含对既有 remote/dedicated API 测试的扩展断言。SDK 序列化 44 项通过，含成员响应捕获序列化用例，SDK 生产代码未变。旧 public response 基线见 `apps/api/tests/fixtures/project_member_profiles_legacy.json`。这些数字独立于前一段 UserBrief owner 的 64 案例；不重复计算既有 owner package、镜像或恢复验收。其他项目成员读取与成员写入仍由各自现有流程负责。

### Projects owner 显示资料消费

`projects._serialize_project` 和 `_serialize_project_list` 使用同一个全局 `resolve_briefs` 批量 owner，只读取显示所需的 name/avatar 引用，不把资料用作授权事实。`owner_name` 与 `owner_avatar_ref` 继续可空，`owner_id` 仍是必需 UUID；缺少用户时两个显示字段保持 `null`。原始空值、空白、Unicode 名称与头像引用不清洗，停用和历史 owner 也不筛除。列表/查询先完成现有授权、过滤、排序、count 和分页，只查询该页返回的 owner IDs；一个 list/page 只发一次批量读取，共享 owner 不重复查询，空页不调用 Identity。详情、创建、patch、改名和转移对非空 owner ID 在现有授权及 commit/refresh 位置各查询一次。显式传入空 lookup 不会触发二次查询。`/datasets/{id}/projects` 保持两个 owner 显示字段为 `null`。

旧 HTTP 响应基线在 owned database 上通过一个案例并清理成功，记录于 `apps/api/tests/fixtures/project_owner_profiles_legacy.json`；fixture 覆盖四个项目、三个 owner 与 list/page/detail 的旧 owner 查询次数 4/2/1。新消费者验证通过 59 个选定案例（53 DB/process、6 pure/static），零跳过；SDK projects 23 项通过，包含两项 list/detail 捕获序列化用例，SDK 生产代码未改。已有 Identity package 与 owner image 沿用前一 UserBrief slice，没有重复计入其 64 案例或 114 个文件 hash。更新后的 API cold image 与 OpenAPI/codegen/catalog 核对均通过；镜像 source/hash 与检查范围见 ADR-0129。每个 list/page 只减少 owner 资料读取的 N+1；其他项目计数仍存在，不能推导整个列表 SQL 数量恒定。

### Identity 管理员临时密码重置

当前 owner slice 只迁移 `POST /api/v1/users/{user_id}/admin-reset-password`，不改公开 UUID/path、3 次／分钟限流、状态码或三字段响应 `temp_password/message/target_email`。固定成功提示仍为 `请通过安全渠道告知用户首次登录后立即修改密码`；临时密码固定 16 个 ASCII 字符，来自 `A-Z`、`a-z`、`0-9` 和 `!@#$%^&*?`，并至少包含四类中的各一字符。密码及 bcrypt 不进入 logs、errors、repr 或 Audit。

Identity 私有 endpoint 是 `POST /v1/identity/admin-passwords/execute`，operation 为 `admin_reset_password`。超级管理员 rank 0、项目管理员 1、审核员 2、employee/历史 annotator 3、viewer 4、未知历史 target role 99；只能重置严格更低级别的 active target。项目管理员可重置所有启用 employee，包括未分配或 foreign-project employee；其他低级 target 必须属于其所管理项目，membership/project 历史状态继续参与判断。Current actor/target User 先按 UUID 顺序 `FOR UPDATE NOWAIT`，再通过 Workspace 锁 Project→ProjectMember 并复查范围；没有 Labeling、任务转交或角色修改。

API/runtime 可选 `IDENTITY_ADMIN_PASSWORD_DATABASE_URL` 或 `IDENTITY_ADMIN_PASSWORD_DATABASE_URL_FILE`；Compose host selector 为 `IDENTITY_RUNTIME_ADMIN_PASSWORD_DATABASE_URL_FILE`。留空关闭此 capability，私有路由返回 503，但既有 Identity Compose startup 不受影响；显式 DSN 失败不回退 API ORM。`identity_admin_password_client` 只有 `admin_password_policy()` 与 `execute_admin_password_command(jsonb)` 两项执行权限。operation-bound HMAC 让保留 UUID 的内部请求可重放，但每个 public call 是新 UUID，公开接口没有 Idempotency-Key 或自动 retry；receipt 不存明文／bcrypt，password timestamp 与 state fingerprint 双重核验，已被后续改密替代时返回 409。该 slice 已通过 42 个选定数据库／进程案例（36 DB/restore、6 live，含既有功能兼容用例，全部零跳过）、source 与 installed package 各 420 项检查，以及真实 API/runtime/image 验收。精确分类、cold-image hash 和 host sample 限制见 [ADR-0124](docs/adr/0124-independent-identity-admin-password-reset.md)；本地资格不证明生产容量，也不新增其它管理员 lifecycle 能力。

### Identity 平台角色预览与变更

此 slice 只迁移超级管理员平台角色预览 `GET /api/v1/users/{user_id}/role/preview` 与变更 `PATCH /api/v1/users/{user_id}/role`。保留现有公开路径、请求/响应、错误及 SDK/CLI 行为；preview 的 UUID path 与 required role string 校验不变，mutation 的字符串 path、body extra-fields 忽略语义也不变。允许角色仅为 `super_admin`、`project_admin`、`employee`、`viewer`，不自动 trim/lowercase。当前角色、last-super-admin、项目／成员职责和未完成标注工作都由 Identity 在写入前复核；此命令不改变 ProjectMember、批次负责人或 Task。

Preview 返回既有 12-field impact model，按所有状态展示成员项目的直接 batch/task 计数；viewer 的 blocker 使用有效执行人 fallback，二者不能混为一个计数。Preview 与 mutation 的 blocker 顺序不同；Mutation 先取共享 last-super-admin fence 和排序 User locks 并校验 credential；同角色直接写 no-op receipt，真实变更先锁目标所拥有的 Project，再计算 work blockers，最后检查 last-SA。成功变更与 typed `user.role_change` Audit、冻结 User receipt 原子提交；同角色操作仍写 receipt 但不产 Audit。Replay 返回历史 accepted snapshot，不重新应用旧角色；A→B→A 后 replay 原 B 操作仍返回 B 快照，数据库保持 A。

私有协议是 `POST /v1/identity/platform-roles/execute`；API/runtime 可选 `IDENTITY_PLATFORM_ROLE_DATABASE_URL(_FILE)`，Compose host selector 为 `IDENTITY_RUNTIME_PLATFORM_ROLE_DATABASE_URL_FILE`。selector 留空只关闭此能力，route 返回 503，既有 Identity 服务仍可启动；显式 DSN 必须先过函数权限 readiness，不能回退。`identity_platform_role_client` 只获得 policy/execute 两个函数权限。56 个选定 DB/process cases（48 Native/旧 API、5 restore、3 live）全部通过、零跳过；installed package 有 94 个精确 hash，source 与 installed 各 447 项测试通过；API/architecture 499 项通过、127 条明确延期，Node 22 与 SDK 35 项通过。禁网、只读、1 CPU/512 MiB API cold image 也已通过；host-process latency 不构成生产容量/SLO。管理员重置密码的 42-case、420-package、467-architecture 与 Directory 的 38-case evidence 仍独立计数。平台角色 slice 已本地限定验收，但完整 Identity、十模块／七门槛目标、生产切换与另一语言 provider 均未完成。协议及精确顺序见 [ADR-0125](docs/adr/0125-independent-identity-platform-role-commands.md) 与[Identity 私有运行协议](docs-site/api/guides/identity-runtime.md#平台角色预览与变更)。

### Identity 管理员暂停与恢复账号

此 slice 只迁移超级管理员／项目管理员的 `POST /api/v1/users/{user_id}/deactivate` 与 `POST /api/v1/users/{user_id}/reactivate`，保留 public paths、body、error、`UserOut` 和 SDK/Web 行为。Suspend 保留字符串 path、无 body 和 malformed UUID 404；reactivate 保留 UUID path 的 422、可空 body 和最多 500 字符的 optional reason。

两操作先过 shared last-super-admin fence，再按 UUID 顺序锁 actor/target User，之后重新验 manager/current credential。Suspend 的 inactive target 提前返回完整 User，不做 project-admin scope/target-role 检查、不审计、不退凭据；真实 suspend 锁所有 derived Project 为 KEY SHARE，PA 只可停 employee，空 project 集合允许，非空集合必须全归 actor 管理。Reactivate 缺失目标 404 优先于 fresh manager 检查；PA 只可恢复 employee 且必须有非空、全部管理范围内的 derived projects，仅锁自己的 Project subset；SA 不取 Project 锁。恢复时先验 scope，再判断已启用或是否为可恢复停用类型。

真实 suspend/reactivate 都保留原凭据退休语义：generation +1、撤销当前 API keys、消费 unused reset links；旧凭据不会复活。Reactivate 先退休凭据再恢复账户，receipt replay 不撤销新签发凭据。Admin operations 不清除已接受的 self-deactivation reservation，也不改成员、批次或任务。Audit detail 分别为 `{email}` 与 `{reason_present}`，不保存恢复 reason 原文。Derived-project set 与两种 project lock mode、exact errors、replay 和未覆盖的 Labeling batch removal race 见 [ADR-0126](docs/adr/0126-independent-identity-suspension-commands.md)。该 slice 已通过 77 个选定 DB/process cases（73 DB/restore、4 live），零跳过；source/installed 各 468 项测试通过、99 个安装文件 hash 精确匹配，API assembly 543 项通过并延迟 128 条明确规则，Node 46 与 SDK 35 项通过。Identity owner image `sha256:b6973fcb4ee2be0612bc0bb6232827c07e1e53b60c9fb6cfadea49bd943d6fa7` 与 API cold image `sha256:01b29d8617f104be76eac817b14803605e733747c1e202df97ea2d58d173c63f` 已按受限配置通过。12 个 target、并发 4 的 host-process 样本含 Node/HTTP 开销，不是 1 CPU 容器时延、生产容量或 SLO。完整 Identity／十模块／七门槛、生产切换与其他语言 provider 仍未完成；详见 [ADR-0126](docs/adr/0126-independent-identity-suspension-commands.md)。

私有 endpoint 是 `POST /v1/identity/suspensions/execute`；API/runtime 可选 `IDENTITY_SUSPENSION_DATABASE_URL(_FILE)`，独立 Compose host selector 为 `IDENTITY_RUNTIME_SUSPENSION_DATABASE_URL_FILE`。Selector 留空只关闭此 capability，私有路由返回 503，既有 Identity service 仍可启动；显式 DSN 必须先通过 `suspension_policy()` readiness，失败不回退 ORM。专用 `identity_suspension_client` 只获得 `suspension_policy()` 和 `execute_suspension_command(jsonb)` 两个函数权限。Compose secretfile映射仍由 Root 维护；精确协议、授权与本地资格证据见 [ADR-0126](docs/adr/0126-independent-identity-suspension-commands.md)。

### Identity 管理员账号删除（已通过限定的本地资格）

此 owner slice 只迁移 `DELETE /api/v1/users/{user_id}`，保留字符串 path、malformed UUID 404、可空 `DeleteUserPayload`、可空 `transfer_to_user_id`、忽略 extra fields、原 manager 权限、errors 和 `UserOut`。每次 public 请求仍产生新的 private operation UUID，不新增 public CAS 或幂等 header。

命令先取共享 last-super-admin fence，再按 UUID 顺序锁 actor、target 和可选 receiver User 行，并在锁内重验当前 credential 与活跃 manager。Manager recheck 先于 target lookup；目标不存在返回 404，随后 self-delete 返回 400。Inactive target 再返回完整 User no-op，早于 target role、project、receiver 和 work 检查，不更新 metadata、转交任务或审计。Active target 锁定所有 derived Projects；项目管理员只能删除 employee，空 project set 可接受，非空则须全部由 actor 管理，旧 active-employee manages-target helper 不增加成员关系要求。之后保留 last-unreserved-super-admin 检查。

阻塞只看直接 `Task.assignee_id` 为 target、状态为 `pending/in_progress/review/rejected` 的任务，并计入所有 target TaskLocks（包括过期锁）。无 receiver 且任务数或锁数非零时返回既有 `has_pending_tasks` 409；最多列出五个按 `created_at DESC` 排序的任务 UUID。传入 receiver 时，即使没有工作也要验证其 active 状态、角色为 employee/project_admin 且不是 target；PA 还须让 receiver 成为每个待转交任务所在项目的成员，receiver 等于 actor 时保留旧例外。执行只改直接 assignee，并将 eligible Task 的 `assigned_at` 与 `updated_at` 都设为当前时间（保留旧 ORM `onupdate` 效果）；随后释放 target 的全部 TaskLocks。不改 reviewer、TaskBatch、ProjectMember、其他状态或 excluded Task 的 timestamps。

DELETE 设置账号 inactive/offline 与原有 deleted metadata，但**不退休凭据**：不推进 generation、不改 API-key revoked_at、reset-link used_at 或 JWT/JTI。已接受的 self-deactivation request/schedule/reason 也保持不变。Typed Audit helper 先清除 target 历史行的 `actor_email` 与 `actor_role` 并捕获数量，再 INSERT 固定 `USER_DELETE` 事件；仅数量非零时在新 Audit detail 中嵌入 `redacted_audit_rows`，不扩大 `detail_json` 的 UPDATE 权限。原有五个 transfer detail 字段保留；既有行的 `actor_id`、detail、IP 和 target 信息不变。这只是部分历史 actor 脱敏，不是完整个人数据擦除。业务状态、任务转交、Audit 与冻结 receipt 同事务提交，重放不重复副作用。

私有 endpoint 为 `POST /v1/identity/deletions/execute`，协议 `aap-identity-deletions/v1`、operation `delete_account`；migration 0247 的 `identity_deletion_client` 仅有 `deletion_policy()` 与 `execute_deletion_command(jsonb)` 两项函数能力。API/runtime 可选 `IDENTITY_DELETION_DATABASE_URL(_FILE)`，Compose host selector 为 `IDENTITY_RUNTIME_DELETION_DATABASE_URL_FILE`；空 selector 只关闭此能力并使私有路由返回 503，显式 DSN 必须通过 readiness 且不回退。Root 的最终最新用例并集通过 72 个 selected acceptance cases（71 DB/process、1 static artifact），零跳过；restore 与 dedicated-API 的最新通过结果覆盖早期夹具失败，重跑不重复计数。Migration 0247 downgrade/restore、grant 和资源清理均通过且无 cleanup errors。Source 与 installed package 各通过 502 项，104 个 installed 文件 hash 精确匹配；API assembly 576 项通过并明确延期 129 条规则，Node 21、SDK 35 项通过。OpenAPI 仅 DELETE description 有变化，public paths/schemas/fields 不变；codegen/catalog/schema 通过，环境文档为 459 个变量、48 个 sections。受限 Identity owner 与 API cold images 均通过。有限 host sample 是 12 targets/c4，34 attempts、22 busy retries、861.920 ms、p50 458.037 ms、p95 856.836 ms，含 Node/HTTP host overhead，不是容器 CPU 时延、生产容量或 SLO。回放确认后 12 receipts 与 12 Audit rows 不变，受理后新增的 work/locks/Audit/credentials 不受影响。精确镜像 hash、route counts 与 helper 行为见 [ADR-0127](docs/adr/0127-independent-identity-deletion-commands.md)；整模块资格见“Identity 整模块私网验收”；十模块／七门槛目标仍未完成。

### Identity 管理员离职预览与提交（已通过限定本地资格）

本 owner slice 只迁移 `GET /api/v1/users/{user_id}/offboarding-preview` 与 `POST /api/v1/users/{user_id}/offboarding`，保留原 UUID 422、public DTO、错误、调用方和默认值。Preview 无 body；Commit 继续接受 unbounded `preview_version`、最多 500 字符的 reason、`handoff`／`emergency_suspend` mode 与可空的 owner／annotator／reviewer receiver mappings，不增加项目数限制或公开 CAS/idempotency header。每次 public call 使用新的 private operation UUID。

Preview 的 `preview_version` 是旧 Python `json.dumps(resource_state, sort_keys=True, default=str)` 字节的 SHA-256，不是 HMAC 或授权凭据。保留 Python Unicode escape、默认 JSON 空格、UTC aware datetime、UUID 小写和 list order。快照覆盖 target、派生 project/member/batch/task/TaskLock、API-key metadata 与 receiver options；options 的 name/email/role 也参与摘要。SQL 排序和旧 `None` project ID 字符串语义不能换成 Python 排序或 SQL JSONB hash。`can_commit` 只给 UI 作预览提示。

Commit 先过共享 last-super-admin fence，再按 UUID 排序锁 actor/target/receiver Users、Project、相关 ProjectMember、target Batches（含 archived）、相关 Tasks、Task edit advisory locks 和全部 target TaskLocks。持有全部锁后重建 facts，按当前 manager scope 检查先于 digest CAS；之后依序检查 target state、重复 project 映射、receiver 与 blockers。Handoff 只处理四种 active Task status；owner→annotator→reviewer 顺序与 Batch participant projection、implicit `updated_at` 和 annotator-only `assigned_at` 保持旧行为。Emergency suspend 仍检查 scope/CAS/state 和重复映射，但跳过 receiver/blocker 检查并且不转移责任。

两种模式都将账号设为 inactive/offline，选择 `suspended` 或 `emergency_suspended`，保存 Python `str.strip()` 后的 reason，并在同一调用事务内退休 generation/API keys/reset links、释放 TaskLocks、写一个 typed `user.offboard` Audit 与 frozen receipt。原始 reason 不进入 Audit；normalized reason 仍会出现在 UserOut 和 frozen receipt。被接受的 self-deactivation reservation 保持不变。Replay 使用 accepted project scope basis；PA 按当前 project ownership/existence 复核受理项目，不从 handoff 后的空 target refs 重算工作。

HTTP composition 为 `apps/api/app/api/v1/offboarding_commands.py` 的 `require_offboarding_manager`、`get_offboarding_commands` 与 `execute_offboarding`；`services/identity_offboarding_commands.py` 提供 `OffboardingCommandsPort`／`IdentityOffboardingCommands`，`identity_offboarding_data.py` 的 `IdentityOffboardingCommandData` 将 prepare/apply 留在同一 SQL transaction。独立协议是 `POST /v1/identity/offboarding/execute`／`aap-identity-offboarding/v1`。Migration 0248 的 `identity_offboarding_client` 只持有 prepare/policy/apply 三项函数权限。API/runtime DSN 是 `IDENTITY_OFFBOARDING_DATABASE_URL(_FILE)`，Compose selector 为 `IDENTITY_RUNTIME_OFFBOARDING_DATABASE_URL_FILE`；独立 Identity runtime 留空时关闭此能力，嵌入式 API 留空时继续使用 caller transaction；显式 DSN readiness 失败不 fallback。旧 API lifecycle coordinator 和 Batch handoff/release helpers 已移除；`services/user_lifecycle.py` 仅保留共享 `lock_accounts`，Project transfer helper 继续供其他 workflow 使用。

Root 已完成该 owner 的限定本地资格：77 个 distinct selected cases（76 DB/process、1 static artifact），零跳过；source/installed suites 各 523 项、109 个安装文件 hash 精确匹配；API assembly 604 项通过、130 条 architecture rules 明确延期，Node 21、SDK 37 项通过。迁移/恢复、受限 owner image 与 cold API image 均通过；完整 hashes、样本边界和 legacy/owner 同夹具 SQL／内存／时间对照见 [ADR-0128](docs/adr/0128-independent-identity-offboarding-commands.md)。这些结果只限定该 owner slice，不代表完整 Identity、生产容量或切换完成。

## Identity 事务内账号事实锁

Migration 0251 在既有 Identity 数据库连接上提供三项 typed SQL capability：

| Function                                                  | Lock mode   |
| --------------------------------------------------------- | ----------- |
| `aap_identity_v1.lock_accounts_share(uuid[],boolean)`     | `SHARE`     |
| `aap_identity_v1.lock_accounts_update(uuid[],boolean)`    | `UPDATE`    |
| `aap_identity_v1.lock_accounts_key_share(uuid[],boolean)` | `KEY SHARE` |

它们只读并返回闭合的五字段事实 `id/name/email/role/is_active`，不返回 User ORM，也不筛除停用账号或历史／未知角色；不存在的账号省略。Role 是原始字符串，name/email 不清洗。输入 UUID 可重复且不加人为数量上限；锁及回执以排序去重的 UUID 集合为准。`nowait=false` 必须等待并返回实际锁住的当前行版本。SQLSTATE `55P03`、`40001` 和 `40P01` 由数据端口直接向上抛，不得在 Identity adapter 中转为 JSON、重试、savepoint 或 rollback。

`IdentityAccountLockData` 必须使用调用方既有 SQLAlchemy connection/transaction，账号锁在随后的 Workspace、Labeling、Inference 或 Identity 写入提交前持续有效；它不是 HTTP lock RPC、专用 DSN 或独立连接。端口不 commit/rollback、不捕获 DBAPIError，也不清除调用方已有 ORM pending work；text SQL 遵循 `autoflush`／`no_autoflush`。Portable wire 与解码器位于 `apps/identity/src/aap_identity/account_lock_contract.py`，调用方适配器为 `apps/api/app/services/identity_account_lock_data.py`。

函数执行权只加给已有 owner：Workspace owner 三项全有，Labeling 与 Learning 为 SHARE，Inference 为 UPDATE，既有 workflow LOGIN 三项全有。仅授予所需的 schema USAGE／function EXECUTE，不授权 User 原表／列；现有 AUTH 与 credential FENCE LOGIN 不变。`application_role` 的三项 EXECUTE 只写入 LAN production-grants source，未应用到生产数据库。Worker 的初始状态检查复用已存在的 `read_actor_facts(actor_id)`；这不是新增 credential capability。

本地 worktree 的兼容运行角色通过 `scripts/worktree_resources.py` 的明确白名单获取三项锁函数，以及已存在的 `read_actor_facts` 和 `read_actor_context`；prepare／grant refresh 会同步这些执行权。本轮只在一次性验收环境应用，未刷新 primary 环境。该兼容角色既有的表权限不代表独立模块的权限边界。

API wrappers 按自己的契约保留冲突语义：单账号 guard 映射 `account_busy`，账号与项目联合 guard 的任一争用映射 `project_busy`，`project_access` 分别保留 account/project/member busy。`UserLifecycleService.lock_accounts` 对任意 `DBAPIError` 显式 rollback，并维持原 busy 409 文案 `账号正在更新，请刷新后重试`；其他调用方不在数据端口 rollback。锁序仍是排序账号 → Project → membership → 资源，保留各调用方的 lock mode 和 NOWAIT/waiting 选择。

帧 worker 每帧重新获取锁内 actor facts 和当前 project/member/task 权威，不缓存跨帧 actor，也不以 `db.refresh(User)` 代替锁后事实。busy 仍沿用 `permission_changed` 结果，不新增重试；删除账号仍走 `propagate_failed`，停用仍走 `permission_changed`。权限变化保留原阶段映射，包括 `source_task_locked`、`target_task_locked`，不统一改写原因。未锁定的 Workspace 查询和 bootstrap／import User 写入不在这次迁移范围。

Root 的旧实现 baseline 在 owned disposable state 上通过 1 个测试、覆盖 15 个场景；`identity-write-fences-legacy.json` 与 `identity-write-fences-legacy-samples.json` 是旧行为记录，不应对新实现重跑，也不叠加到新资格数量。本切片的最新验收去重后 231 项全部通过、无有效跳过，覆盖三种真实锁模式、等待后当前行、SQLSTATE／rollback 差异、Node 受限调用、两种转移／角色变更顺序、八项恢复及新 Celery 进程的三次派发。源码与非 editable 安装各通过 595 项；119 个包文件指纹一致，新的 owner 容器及 API 冷加载容器也验证了这些文件，API 另核对 150 个应用／native 文件。架构检查通过 316 项，132 条既有不适用规则保持 deferred；OpenAPI、codegen 和 509-route／93-module 目录一致。整模块资格见“Identity 整模块私网验收”；十模块／七门槛、生产容量与 primary cutover 仍未完成。精确合同见 [ADR-0131](docs/adr/0131-identity-transactional-account-locks.md)。

## Identity 运维账号 provisioning

这条 Identity owner 只负责两种既有 CLI 账号操作，不增加 public HTTP/SDK 注册接口。Bootstrap 按单个 email “已存在则复用、缺失才创建”执行，不要求全局账号表为空；不同 email 的 bootstrap super-admin 可共存。`application/bootstrap_admin.py` 与 `scripts/bootstrap_admin.py` 的 CLI 继续 lowercase/trim email、trim name，owner 不增加 normalization、长度或密码强度规则。现有同 email 行不论 role、active、name/hash 均原样复用，不再 hash、不写 Audit；manager 返回 UUID 或 `None`，CLI 保持旧 boolean 返回。

新 bootstrap 账号保留 `super_admin`、active、`status=online`、null group、`email_verified_at=caller now`、generation 0、空 preferences、其余 nullable lifecycle 字段 null 与 DB transaction timestamp。Identity Native 同事务调用 Audit owner 写固定 `system.bootstrap_admin`：actor 是新账号，target 为该 UUID，detail 只含 email，request ID 来自现有 context；应用层不重复写 Audit。新建和 Audit 原子提交；同 email 竞态仍传播 PostgreSQL `23505` 并中止事务，不加 `ON CONFLICT`、自动重试、savepoint 或 adapter rollback。

`seed_pointcloud.py` 与 `import_nuscenes_scene.py` 精确查找 email `admin`。任意已存在账号（含停用或历史/非 admin role）都原样复用。缺失时只有 environment 字符串等于 `production` 才在 hash/create 前抛原 RuntimeError：

```text
生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。请先手动创建管理员账号再运行本导入脚本。
```

其它环境（包括 staging）仍创建 email `admin`、name `超级管理员`、bcrypt `123456`、role `super_admin`、active、`status=offline`、null `email_verified_at`，无 Audit。返回值仅 UUID。所有者和 caller 共用一条 SQL transaction；导入 rollback 会一起撤销新账号与 Assets/Workspace/Labeling 数据。Lookup 遵循 `autoflush`／`no_autoflush`，create 则即使在 `no_autoflush` 中也显式 flush caller pending work；不清除或 refresh unrelated ORM objects。

Migration 0252 新增 `read_provisioning_account(text)`、`create_provisioning_account(jsonb)` 和窄 LOGIN `identity_provisioning_client`；其可信 LOGIN 当前有这两项函数及主种子的 persona 创建能力，由 `infra/docker/identity-provisioning-grants.sql` 按标准 role/owner/database 检查安装。Identity 只调用固定 Audit function，不读写 Audit 原表；`module-data-grants.sql`、LAN application role 与 local compatibility runtime 的 allowlists 仅显式增加所需函数权，LAN source 未应用到 production。没有新 HTTP endpoint、runtime DSN selector、setting 或 dependency。`AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL` 仅是 verifier test override，不是 runtime config。本切片的最新验收去重后 236 项全部通过、无有效跳过，包括受限 Node client 与真实 CLI、全新 Celery worker 三次派发、八项恢复及当前权限／事务行为。旧实现记录在迁移前的 owned disposable database 捕获，1 个测试覆盖 10 个场景；原始失败尝试与定向复验保留，按最新用例结果去重。审计拒绝的原始 23514、后续事务中止、显式 rollback 及未提交账号／Audit／调用方 pending work 一起回滚均已验证。源码与非 editable 安装各通过 619 项检查、122 个包文件指纹匹配；新的独立 owner 与禁网只读 API 镜像通过，后者另核对 61 个本轮 API/native 输入和迁移头 0252。架构检查通过 316 项，132 条既有不适用规则明确延期；OpenAPI 保持字节一致，codegen 与 509-route／93-module 目录一致。之前 account-lock 231 项和 actor-context 114 项各自独立，不计入本 slice；全 Identity、十模块／七门槛及生产容量仍未完成。细节见 [ADR-0132](docs/adr/0132-identity-operational-provisioning-and-authority-reads.md)。

Authority consumer reuse 则复用既有 `read_actor_facts`、account-status value 与 0251 三种同事务 lock functions，不增加新的 SQL/wire/network surface。`project_access.resolve_project_access` 的非锁定读取调用 `IdentityAccountLockData.read_actor_status`，仍按 account→Project refresh→membership 顺序并保留必要 Project ORM refresh；`task_lock.py`、`batch.py`、`async_job_access.py` 只移除 authority-only User ORM 读取。旧 401/Bearer/403/404、锁 mode/wait/exception/None 和 row order 都保持，active-only guard 不增加 role 检查，不引入新 NOWAIT。它不扩展 member/task lifecycle，也不把已经关闭的 account-lock slice 重新打开。整模块资格见“Identity 整模块私网验收”；十模块／七门槛目标仍在进行，不代表生产／primary cutover。

## 普通批量结果检查点

安装当前迁移和 `module-data-grants.sql` 后，为预先创建、无角色成员关系的 `preannotation_result_client` LOGIN 安装精确能力。数据库初始化必须已撤销 PUBLIC 的 TEMP，并按需给现有角色单独重授；授权脚本会核验此项，不隐式修改 PUBLIC ACL。本地一次性验收器执行该初始化：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v jobs_owner=jobs_code -v labeling_owner=labeling_code \
  -v result_role=preannotation_result_client \
  -f infra/docker/preannotation-result-data-grants.sql
```

API／worker 默认用现有数据库连接保存检查点；设置 `PREANNOTATION_RESULTS_DATABASE_URL` 或 `PREANNOTATION_RESULTS_DATABASE_URL_FILE` 后，独立调用切到该函数专用 LOGIN。两项二选一，连接使用 `postgresql://` 格式，须指向与业务连接相同的数据库；这里只分离角色和进程，最终 Prediction／Meta／回执事务仍使用原 `DATABASE_URL`。Secret 文件最多 4096 字节，读取错误不会回退到原角色。

运行 `uv run --project apps/preannotation-results --extra postgres python -m aap_preannotation_results_runtime read_batch_result_execution < request.json`，请求文件仅包含对应冻结 DTO。CLI 的独立依赖只有 Pydantic 和 asyncpg，不读取应用、队列或 S3 凭据；业务写入操作不对执行 CLI 开放。读取大结果时使用固定块和摘要，不能把交互接口的 16 MiB 限额套给整个批量结果。

失败项重试复用同一受限连接和结果工件，并提供 `aap-prediction-retries` CLI（`python -m aap_preannotation_results_runtime.retry_main`）。CLI 仅开放 attempt 读取及两项派发能力；立即派发与 maintenance 队列中的定时 relay 也使用该连接。业务写入方保留准入、成功／失败结算与最终 Labeling 事务。旧 pending marker 没有绑定 Job 时，迁移释放占用而不改变失败次数，后续重试重新受理。升级前必须停止并排空旧重试消费者和队列，处理完旧的 pending/running 重试作业。存在无绑定占用时，普通升级会拒绝；确认排空后，在 `apps/api` 目录使用 `.venv/bin/python -m alembic -x prediction_retry_legacy_drained=true upgrade head` 显式确认本次迁移，不能让新旧 writer 滚动并行。此确认不跳过活动旧作业检查，也不写入长期配置。

本地真实重投递验收由 `scripts/verify_module_data_boundary.py` 的一次性资源承载；`RUN_BATCH_RESULT_WORKER_LIVE_TEST=1` 显式启用强制终止子进程用例。不要在日常开发库或共享 worker 上运行该用例。

日常开发库已采用通过整模块本地验收的 Identity 检查点，并从 `0234` 升到 `0306`。采用前保留私有数据库与集群角色备份，实际备份在独占 PostgreSQL 恢复后与原数据一致；升级后 171 张原有业务关系及唯一业务行保持。主 checkout 按锁文件新增 Identity 和共享邮件包，原 macOS LaunchAgent 已重启；DB／Redis／MinIO、注册策略读取、未认证拒绝、OpenAPI 快照及真实源码热重载通过。日常 API 仍使用既有内嵌绑定，独立 owner 拓扑没有切换；本集成分支的后续 Workspace／Notifications 能力尚未采用。八类 worker／beat 仍未启动，其旧镜像尚未按 Identity 检查点刷新；启用前需要重建并核对实际安装内容，因此默认 `/health` 和 `/health/celery` 仍为 503。Go 不加入正常运行路径。

## 视频媒体的独立控制与执行

`apps/assets-media` 定义 `aap-assets-media/v1` 与 `aap-assets-images/v1` 契约。视频与图片共用控制进程；视频和图片分别由独立 Worker 执行，图片通过固定 SQL 能力与来源/派生对象身份完成受理、发布和清理。视频控制/执行已通过隔离本地进程和受限容器联调；图片的可选运行方式与验证边界见下文及[图片金字塔运行手册](docs-site/ops/runbooks/image-pyramid.md)。Go 实验不进入这些默认运行路径。

安装当前迁移后，沿用 module-data 初始化流程，为现有 Assets NOLOGIN owner 创建分离的 `asset_media_control_client` 和 `asset_media_worker_client` LOGIN。受理视频输入及请求者撤权处理使用 `0225`、`0226`。先应用 owner 与既有 Catalog/Source/importer/operator grants，再由 migration role 安装媒体授权：

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v assets_owner=assets_code \
  -v asset_media_control_role=asset_media_control_client \
  -v asset_media_worker_role=asset_media_worker_client \
  -f infra/docker/assets-media-grants.sql
```

安装当前迁移及 grants 后，共享控制 LOGIN 为 49 项，视频 Worker 为 20 项，独立图片 Worker 为 10 项，Cleanup 为 8 项。各 LOGIN 都没有表、列、sequence、外部 owner schema、成员继承或 CREATE/TEMP 权限；初始化必须处理 PUBLIC 的 TEMP。来源身份、Task 可见性及任务级帧重试的标注写权限由 owner 能力组合验证，不把布尔权限结果当作授权。技术媒体派生作业沿用其受理、来源代次、作业状态和取消规则；这不授予预测写权。视频推理输入与帧仍检查当前请求者，停用或权限失效使对应单位终止，忙锁保持可重试。

为两个进程准备不同的数据库和 S3 身份。源桶只需对象 HEAD/GET，不能写或删除原素材；控制身份对缓存只需 HEAD/GET，执行身份另需 `media-assets/*` 的 Put/Get/List/Delete 及精确旧视频缓存清理，不授予桶管理。S3 不单独定义 HEAD 动作，相应权限由 GetObject 提供。浏览器可达的 `ASSETS_MEDIA_S3_PUBLIC_ENDPOINT` 必须与内部请求地址分开配置；不改写已签名主机名。

可选 `docker-compose.assets-media.yml` 不被默认 Compose 引用。准备其中列出的独立 secret 文件，使容器用户 10005 能读取，再显式启动两个服务；该文件只暴露内部 8768，不绑定宿主端口，不连接 Celery/Redis。宿主联调入口为：

```bash
uv run --project apps/assets-media --extra runtime --extra server python -m aap_assets_media_runtime
uv run --project apps/assets-media --extra runtime python -m aap_assets_media_runtime.worker
```

受理视频输入使用同一控制工件的 `POST /v1/video-inputs/{manifest,members,prepare,seal,input,verify,times,require-frame,frame,reference}`，协议为 `aap-assets-video-input-control/v1`。capture、worker、最终 Labeling 围栏及回收函数不作为 HTTP 命令暴露。视频 Worker 默认公平轮询技术媒体、受理原始视频、派生帧和跟踪原片四类队列；`--once` 最多执行一个已领取单位，互斥的 `--job-id`、`--input-id`、`--frame-id` 或 `--tracker-input-id` 精确选择各类工作。来源字节复用既有 Media 凭据与数据集桶，不要求为视频 Worker 增加图片 Worker 环境变量。

专用跟踪输入使用 `POST /v1/video-tracker-inputs/{input,prepare,reference}`，协议为 `aap-assets-video-tracker-control/v1`，复用 `ASSETS_MEDIA_URL`、token、工件及输入桶。`input` 返回状态与固定执行事实，`prepare` 只观察原始对象并登记准备；独立 Worker 随后物化，`reference` 才交付完整原片的精确版本链接。队列仍只携带规范 `VideoTrackerJob` UUID；输入准备未完成时保持 queued 并延后同一作业，通用作业表仍只是执行摘要。批量帧和跟踪输入使用各自的领取、回收能力，不互相借用权限。

启用这条跟踪路径前应用 `0228`，按当前 grants 更新受限身份，并刷新 API、跟踪 Celery consumer、Media 控制和视频 Worker。迁移会拒绝没有输入快照的旧 queued/running 作业及可审阅候选；先由旧运行时排空、采纳或丢弃，不能现场补造历史输入。保留新来源证据时拒绝回退。常驻开发环境也按这些前置条件核验后再切换；协议和凭据范围见[媒体接口](docs-site/api/guides/assets-media.md#视频跟踪的原片输入)。

视频 Worker 的 CLI 收到 `SIGTERM` 后停止领取新单位，保留当前单位的处理与租约心跳，完成后关闭运行资源；空闲轮询会立即唤醒退出。`--once` 同样可排空当前单位，`--healthcheck` 和 `SIGINT` 保持原行为。部署的停止宽限时间应容纳当前处理；超时强杀仍依靠租约和来源围栏恢复，不能据此宣称任意在途作业都能立即接管。

逐帧业务入口与 Celery 段任务已消费同一保留清单、原视频和精确帧。分离迁移账号与单体 runtime 时，工作树准备和 `lan-production-grants.sql` 为 runtime 补充精确的 25 项视频 workflow/control/worker/Labeling 能力，以及 22 项图片媒体和 16 项已受理图片输入、清单与 Labeling 引用能力；视频 sweep/垃圾回收、图片输入 sweep/垃圾回收和图片私有回执/Pin helper 仍由各自 owner 控制，不改变独立模块的受限账号配置。先应用迁移及授权，再刷新实际 API/Celery；旧逐帧消息缺少清单身份会明确失败，不按当前视频恢复历史来源。所有外部准备与来源核验都在最终业务写锁之外。

`apps/api/.venv/bin/python scripts/verify_legacy_image_runtime.py --output .worktree/legacy-image-runtime.json` 在一次性本地 PostgreSQL、专用 Redis 和隔离存储桶内分别验证工作树与 LAN 应用授权。它实际运行 LAN SQL，以非迁移账号验证图片原始输入、批量清单、标注证据继承和缩略图生成；此命令不修改日常开发库或真实部署。

受理对象使用专用、启用版本管理的 `ASSETS_MEDIA_INPUTS_BUCKET`。图片 Worker 仅可写 `accepted-inputs/*`，视频 Worker 仅可写 `accepted-video-inputs/*`；控制只读精确版本，Cleanup 按单个尝试前缀删除对象版本与 multipart。详细权限见 [Cleanup 工件说明](apps/assets-cleanup/README.md)。这些独立进程已完成隔离的本地对象存储与 Node 互操作验收；业务视频预标注、覆盖、失败重试和采纳已接入固定输入，API/Celery、Media 和 Cleanup 的容器链路、固定 720p 负载及在途执行交接均已有本地证据。切换常驻开发环境前仍需核验现有作业、运行进程及数据迁移。

容器验收入口为 `apps/api/.venv/bin/python scripts/verify_video_consumers_containers.py --output .worktree/video-containers.json`。先从当前源码构建下列四个镜像，并给四者设置同一组标签：`org.aap.acceptance=module-data-boundary`、`org.aap.acceptance-id=<同一24位十六进制标识>`。将对应镜像引用写入环境变量；验收不构建镜像，也不刷新常驻开发栈。

默认 `--profile smoke` 使用两项各 12 帧的小视频，并检查 Cleanup。`--profile capacity` 固定使用三项各 120 帧、1280×720 的移动图案视频，Job 等待上限为 900 秒；模型桩读取并解码真实版本化帧，在一项响应仍在处理时优雅停止对应 Celery 容器，由另一台继续完成。JUnit 保留对象读取、数据库 attempt、进度采样、内存及端到端耗时。模型回复中的固定推理耗时不作为实测延迟；逐帧任务当前使用提前确认，这项优雅交接也不证明强制杀进程后的消息重投。

`--profile media-handoff` 只运行媒体租约接管，所需镜像变量仅为 `AAP_VIDEO_CONSUMER_MEDIA_WORKER_IMAGE`。它暂停正在处理的 A 容器，只在一次性验收库中将同一租约的 input 与 attempt 到期时间一起推进，再由 B 领取第二代租约；恢复 A 后验证其旧 token 被拒绝，且 B 的封存计划和精确帧保持不变。业务场景使用 180 秒等待预算，资源清理另行完成；清理先恢复本次创建的暂停容器，再按身份移除。该时钟推进只属于隔离故障注入，不在业务运行时提供接口。

| 镜像环境变量                             | Dockerfile                       | 构建 target              |
| ---------------------------------------- | -------------------------------- | ------------------------ |
| `AAP_VIDEO_CONSUMER_API_IMAGE`           | `infra/docker/Dockerfile.api`    | 默认，API 与 Celery 共用 |
| `AAP_VIDEO_CONSUMER_MEDIA_CONTROL_IMAGE` | `apps/assets-media/Dockerfile`   | `control`                |
| `AAP_VIDEO_CONSUMER_MEDIA_WORKER_IMAGE`  | `apps/assets-media/Dockerfile`   | `worker`                 |
| `AAP_VIDEO_CONSUMER_CLEANUP_IMAGE`       | `apps/assets-cleanup/Dockerfile` | 默认                     |

该命令在一次性 PostgreSQL、专用 Redis 和十个归属桶内执行真实 HTTP、队列和版本化对象读写，再按归属清理。API 的启动身份只有这十个桶的元数据和生命周期权限，不能读写源对象；Media 持单独身份，Celery 使用无效直接存储凭据。默认视频执行策略需要的空闲临时盘至少为源大小加 128 MiB；验收的 256 MiB 临时盘仅覆盖小样本，不应直接作为大视频部署预算。

每个进程只接收自己的专用环境。必填的 DSN、token、S3 凭据可用对应 `_FILE`，直接值与文件变量不能同时存在，空值不作为回退来源。控制进程使用 `ASSETS_MEDIA_CONTROL_DATABASE_URL`，执行进程使用 `ASSETS_MEDIA_WORKER_DATABASE_URL`；S3 endpoint、region、两个桶和身份由 `ASSETS_MEDIA_*` 显式提供。API 设置 `ASSETS_MEDIA_URL` 和同一 token 后选择远程控制，任何远程配置或调用失败都不回退本地；留空使用同一工作流的本地 SQL/对象装配和薄 Celery 执行适配。

原生请求/完整回复上限为 8/16 MiB，记录页为 15 MiB/500 条；完整时间表和 packet 样本可以跨页。客户端验证顺序、总数、游标、来源和摘要，来源引用批量读取也按回复预算拆分。控制默认池为 1..8、取连接 2 秒、SQL 5 秒、S3 连接/读取 2/10 秒；执行默认轮询 5 秒、租约 1200 秒及定期续租，S3 连接/读取为 10/120 秒。帧和分块的空闲保留为 14/30 天，海报使用独立持久对象，不参与普通帧淘汰。Compose 的控制 512 MiB/0.5 CPU 和执行 2 GiB/2 CPU 是可选本地预算，不是生产容量证明；解码、完整索引及临时文件仍随视频大小增长。

切换前先排空或停止旧视频消费者；旧进程不会因迁移自动获得新写入栅栏。领取事务在下载、FFmpeg 或上传前提交，输出在不可变 attempt 目录，迟到执行者不能替换当前投影。首次观察绑定对象身份；后续外部覆盖使来源漂移并拒绝发布，需由 Source 登记所有者核对。旧视频缓存从未验证代次开始按需重建，原素材、标注和学习证据保持保留。清理先在 SQL 中脱离引用并持久登记垃圾，再删除对象并确认。

执行进程也保留 `MEDIA_PROBE_URL`、`MEDIA_PROBE_TOKEN` 和 `MEDIA_PROBE_TIMEOUT_SECONDS` 的选择；可选 Compose 显式转发这三项。宿主独立进程可用 `MEDIA_PROBE_TOKEN_FILE` 替代 token 实际值，二者只传其一；Compose 使用文件时需在自己的 override 中挂载并设置该入口。

控制 `/health/live` 只证明进程可响应，`/health/ready` 和执行器 `--healthcheck` 验证精确 SQL 身份；它们不能替代实际对象权限、解码和发布验收。数据 schema 与 HTTP OpenAPI 分别随包发布为 `protocol.schema.json`、`openapi.snapshot.json`。完整切片状态与本地证据记录在[后端解耦计划](docs/plans/1792160873_backend-module-decoupling.md)和 [ADR-0105](docs/adr/0105-independent-video-media-owner.md)。

## 图片金字塔的独立控制与执行

图片金字塔现由 `apps/assets-media` 中的共享媒体控制进程和专用图片 Worker 承担。共享控制端同时承载视频、图片与已受理输入契约，`asset_media_control_client` 共 49 项 SQL 能力（视频 18、图片 14、图片输入 4、图片批量清单 4、视频输入 9）；视频 Worker 使用 20 项，图片 Worker 使用独立的 `asset_image_worker_client` 和 10 项（图片 6、输入 4）。图片处理使用 Pillow 与受资源上限约束的 libvips 子进程，不依赖 Celery 执行队列。图片和输入垃圾由既有 `assets-cleanup` 进程处理，`asset_cleanup_client` 仅获 8 项清理能力。

API 设置 `ASSETS_MEDIA_URL` 和相同的 `ASSETS_MEDIA_TOKEN` 后，将图片控制请求发到共享控制进程；一旦配置远程地址，连接或请求失败不会退回本地。地址留空时，当前开发兼容路径仍在 API 进程内装配本地控制，并保留轻量 Celery 唤醒适配；这条路径不代表独立控制/执行部署。默认 Compose 不启动媒体 overlay，也不表示图片 Worker 已部署到主环境或生产环境。

图片 schema 属于迁移 `0220`，已受理输入属于 `0221`。按既有 module-data 与媒体授权顺序安装 owner/辅助能力、`infra/docker/assets-media-grants.sql` 和 `infra/docker/assets-image-grants.sql`。图片 Worker 只能执行自己的 10 项函数，不能直接访问表或其他 owner schema；清理 LOGIN 只能领取/确认 owner 发出的删除责任。所有 SQL LOGIN 必须与迁移角色和 NOLOGIN owner 分离。

批量图片输入清单的基础迁移为 `0222`，Scene 模式普通单图 Task 需要 `0227`，均不增加服务、桶或 secret。各自升级前先排空或明确处理对应图片项目中仍为 pending/running、scope 为 bulk 或缺省、且没有清单协议的旧预标作业；迁移会拒绝这些在途记录。旧 pending payload 没有完整配方，无法分类的文字／跟踪／场景配方也保守纳入排空；有明确保存的执行单位或配方标记时按原独立路径处理。刷新 API、普通预标 Celery consumer 和共享 Media 控制，核验三项 `accepted_input_manifest_*` 派发参数及四个 `/v1/input-manifests/` 路由；当前共享控制就绪探针核验 52 项能力，包含三项跟踪输入能力。清单封存前不交付 Input 引用。清单和成员是 native 管理的两张证据表，保留记录时拒绝删除表；仍有 Scene 模式清单时也拒绝恢复旧的普通图片范围，不自动清空历史。

可选 `docker-compose.assets-media.yml` 增加共享控制、视频 Worker 和图片 Worker；需要把它与默认服务文件组合，并只启动所需服务：

```bash
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml build assets-media-control assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml up -d assets-media-control assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml logs --tail=100 assets-media-control assets-images-worker
docker compose -f docker-compose.yml -f docker-compose.assets-media.yml exec assets-images-worker python -m aap_assets_images_runtime.worker --healthcheck
```

将 API 与控制进程接入同一受限网络，并配置 API 的控制 URL/token。overlay 不发布宿主端口；控制端仅在内部网络监听 8768。容器以 UID 10005 运行。镜像的 `image-worker` target 安装 libvips 和 ICC 文件；独立 Worker 的默认本地预算为 4 CPU、16 GiB 内存和 12 GiB 临时盘，这些是 Compose 资源限制，不是生产容量证明。回收派生对象时，另按现有部署方式运行 `docker-compose.assets-cleanup.yml` 中的 `assets-cleanup`，不要把删除权限交给控制端或图片 Worker。

源桶和历史 Task 桶只需读取原图。共享控制凭据可 HEAD/GET 来源与缓存并签发受限读取 URL，不写源、不删除对象；图片 Worker 只读源对象，并在缓存桶的 `image-assets/*`、`image-pyramids/*` 前缀写入不可变派生输出，孤儿扫描的列举范围限制在金字塔前缀，不写/删原图。清理身份仅能列举并删除这两个派生前缀下由数据库签发的精确 key/prefix；不得配置整桶生命周期删除这些前缀，不能把原图桶作为清理桶。

已受理推理输入使用独立 `ASSETS_MEDIA_INPUTS_BUCKET`（默认继承 `MINIO_INPUTS_BUCKET=accepted-inputs`），必须启用 versioning，不能与源桶、历史 Task 桶或 `media-cache` 共用。控制端与图片 Worker 只需读取该桶的版本状态；Worker 按受理记录写入具备非空、非 `null` VersionId 的输入对象，输入清理身份按保留账本列举版本、删除标记与未完成 multipart 并按精确版本删除。在 MinIO 上，清理按每个 `(key, VersionId)` 发送 DELETE，并需 `DeleteObjectVersion` 加上仅接受非空、非 `null` `s3:versionid` 的 `DeleteObject`；无版本 ID 与批量删除不授权。不要授予桶创建/版本配置权限，也不要对输入桶或媒体缓存桶配置生命周期自动过期。

Secrets 使用各 overlay 对应的独立数据库/S3 文件，不能共享高权限应用或迁移身份。

自动受理默认关闭；`IMAGE_PYRAMID_PROFILE_VERSION` 默认为 `pyramid-v1`，API 与共享控制端必须使用相同 profile。作业受理时固定 normalization/profile policy，修改 profile 应作为新输出线规划，而不是只切换一台服务。Worker 的 `ASSETS_IMAGES_SRGB_PROFILE`（默认镜像内 `/usr/share/color/icc/sRGB.icc`）必须是可读绝对路径。`VITE_EXPERIMENTAL_LARGE_IMAGE_TILES` 是 Web 构建开关，与自动受理无关，改动后需重建 Web。

Worker `--healthcheck` 仅验证配置、连接及精确 SQL 能力，不证明源/缓存 IAM、解码和发布成功。共享控制的 `/health/ready` 也不能替代 S3 验收。详细启动、profile、重试、回填和保留步骤见[图片金字塔运行手册](docs-site/ops/runbooks/image-pyramid.md)。

### Inference 健康观察

所有既有健康入口复用同一个观察 owner 执行 `/health` 及条件 `/setup`。默认嵌入调用方；
可选 `INFERENCE_HEALTH_OBSERVATION_DATABASE_URL(_FILE)` 或 `INFERENCE_HEALTH_OBSERVATION_URL`，
两者互斥。DSN 指向只读三函数的 `inference_health_observation_client`，HTTP 使用
`INFERENCE_HEALTH_OBSERVATION_TOKEN(_FILE)`。独立进程同样使用这两组 DSN/token selector；
单个进程可同时提供流水线、Workspace setup probe 和健康观察，但各 bearer 必须不同。
默认90秒的 `INFERENCE_HEALTH_OBSERVATION_TIMEOUT_SECONDS` 是整个 HTTP 调用预算，
`ML_HEALTH_TIMEOUT` 仍控制每次实际后端请求；挑战兼容回退与 setup 最多产生三次请求。

先应用当前 Alembic head，再应用 `infra/docker/module-data-grants.sql` 和
`infra/docker/inference-health-observation-grants.sql`。后者仅授予 policy/open/seal；
`inference-health-origin-grants.sql` 则测试独立签发与验证的三函数边界，不能用其 LOGIN
替换整套 API 数据库账号。嵌入模式继续使用 API 的过渡角色，不代表宽表权限已收口。

`docker-compose.inference-health.yml` 提供1 CPU／256 MiB、只读与降权示例，使用
`INFERENCE_RUNTIME_HEALTH_OBSERVATION_DATABASE_URL_FILE` 和
`INFERENCE_RUNTIME_HEALTH_OBSERVATION_TOKEN_FILE`。它和其他 Inference Compose 示例共用
宿主8768，分别启动时只能选其一；secret 文件只读挂载。本地验收不切换日常或生产环境。

请求/响应只携带不透明密封串；快照取自原 ORM 对象的已物化字段，保留关闭自动刷新时
尚未落库的 URL/凭据。观察回执只能在原连接、原事务内验证，提交或回滚后失效。
能力提取、数据库观察时钟、GPU 围栏、健康写回、服务池对账、Audit 与最终提交仍由
既有调用方事务完成。显式提供方不可用时返回503且不回退；后端网络故障与 setup 失败
继续使用既有行为。完整健康 Native apply、路由连续性与完整 Inference 控制面仍在实施。

### Registry 环境同步

启动同步可设置 `INFERENCE_REGISTRY_INGRESS_URL` 或
`INFERENCE_REGISTRY_INGRESS_DATABASE_URL(_FILE)`，两者互斥；均为空时借用原启动事务。
HTTP 使用 `INFERENCE_REGISTRY_INGRESS_TOKEN(_FILE)`，整体调用预算由
`INFERENCE_REGISTRY_INGRESS_TIMEOUT_SECONDS` 控制；实际后端请求仍使用 `ML_HEALTH_TIMEOUT`。
独立进程使用相同 DSN/token selector；bearer 必须与其他 Inference 能力不同。

先应用当前迁移，再以迁移角色应用 `infra/docker/module-data-grants.sql`，
随后应用 `infra/docker/inference-registry-ingress-grants.sql`，设置
`expected_database`、`migration_role`、`inference_owner` 和
`inference_registry_ingress_role=inference_registry_ingress_client`。专用 LOGIN 只能调用11项固定函数，
没有原表、列、序列、临时对象或其他模块权限。不要把日常 API 的宽权限 DSN 用于该独立进程。

本地容器示例为 `docker-compose.inference-registry.yml`，其两个
`INFERENCE_RUNTIME_REGISTRY_INGRESS_*_FILE` 变量指向 DSN 和独立 bearer 文件。
该进程独立监听，但尚不代表整个 Inference 模块可独立替换语言；截图 seed 的项目绑定、
普通 Registry/Pool 管理、路由排空和租约连续性仍在后续范围。

### 持久路由执行权（实施中）

路由执行账本使用独立短事务，不提交当前预测、标注、审计或发布事务。
API 的 `INFERENCE_ROUTING_URL` 与 `INFERENCE_ROUTING_DATABASE_URL(_FILE)` 互斥；均为空
时在当前 API engine 上开启独立事务。HTTP 使用 `INFERENCE_ROUTING_TOKEN(_FILE)`，
`INFERENCE_ROUTING_TIMEOUT_SECONDS` 限制 owner 调用；选定提供方失败后不回退。
HTTP begin 的配置 subject 仍由 API 本地短事务签发，不携带后端明文凭据。

独立 owner 进程使用 `INFERENCE_ROUTING_DATABASE_URL(_FILE)` 与独立的
`INFERENCE_ROUTING_TOKEN(_FILE)`；准入参数为
`INFERENCE_ROUTING_HEALTH_MAX_AGE_SECONDS` 与 `INFERENCE_ROUTING_LEASE_TTL_SECONDS`。
`inference_routing_client` 仅可执行 core policy、core step、
`scope_retirement_step(text,text)` 和 `video_execution_scope_step(text,text)` 四项固定
Native 能力，不能授予原表权限。
定向退役沿用相同进程、端口与控制认证，接口为 `/v1/routes/retire` 与
`/v1/routes/retirement-status`；调用前保存退役 ID，状态查询不发送后端控制。

五个内置后端共享路由控制器。设置 `AAP_ROUTE_CONTROL_TOKEN(_FILE)` 后启动为未绑定，
可选 `AAP_ROUTE_REGISTRY_ID` 固定实例身份；否则第一次认证管理交换固定身份。
控制 bearer 需与 Registry 内保存的 token 相同。每次启动均在实际 drain/bind 前拒绝计算，
不提供旧流量绕过开关；残留的 `AAP_ROUTE_ALLOW_LEGACY_EXECUTION=1` 配置会拒绝启动。
迁移时先停止旧实例，再配置控制凭据、启动并绑定新实例，不能用空账本或 Redis 零计数作为停流证明。
未配置控制 token 的普通旧推理仍可运行，管理连续性无法确认。

当前实现与验收状态见 [ADR-0155](docs/adr/0155-durable-inference-routing-authority.md)。
完整迁移、真实并发、受限部署和故障资格仍需完成；此配置不切换日常或生产环境。

视频执行权使用 `aap_jobs_v1.video_execution_step(text,text)`，迁移与 owner 授权后通过
`infra/docker/video-execution-grants.sql` 安装单独 `video_execution_client` LOGIN；参数为
`expected_database`、`migration_role`、`jobs_owner` 和 `video_execution_role`，必须先核验
目标数据库与角色归属。该账号不能与路由 LOGIN 合并。现有 API bridge 在原业务事务内
调用 guard，不新增运行时 DSN selector；`AAP_MODULE_DATA_VIDEO_EXECUTION_URL` 仅供
隔离 verifier 验收。完整恢复仍未开启，详见 [ADR-0157](docs/adr/0157-video-execution-claims-and-route-binding.md)。

### 离线视频恢复 operator（实施中）

此 operator 使用现有 Inference 工件，不增加普通服务配置或环境变量。源端进程、数据库／
对象存储写入凭据与在途写入须先由操作人员围住，目标保持业务停写；健康 marker 不能代替
这些前提。数据库身份、指纹与 SHA-256 绑定具体恢复，不允许手工改写冻结计划或旧回执。

先应用当前迁移和 `infra/docker/module-data-grants.sql`，再由迁移角色在明确目标库应用
`infra/docker/video-restore-grants.sql`，变量为 `expected_database`、`migration_role`、
`inference_owner=inference_code`、`video_restore_operator_role=video_restore_operator_client`。
LOGIN 只有一项 Inference Native 函数权限，没有表、TEMP、角色继承或 Jobs RPC 权限。

```bash
# DSN 文件仅当前用户可读；以下命令只指向明确的隔离恢复目标。
chmod 600 /secure/restore-database-url
python -m aap_inference.restore_cli --database-url-file /secure/restore-database-url --inspect
python -m aap_inference.restore_cli --database-url-file /secure/restore-database-url --command-file /secure/restore-command.json
python -m aap_inference.restore_cli --database-url-file /secure/restore-database-url --status RESTORE_UUID
```

命令固定 restore UUID、备份 SHA-256、外部围栏记录 SHA-256 及 inspect 返回的六项身份／
指纹，协议为 `aap-video-restore/v1`。首次准备后只重放同一文件；未知结果、活跃 drain 或
后台变化不会自动解开围栏。READ COMMITTED 是此 Native 事务合同的一部分。完成前实际
backend 的 epoch／incarnation 都需重新核验，完成后由普通 claim 恢复新代次。
完整契约、实施中的本地验收与尚未完成的对象存储恢复见
[Inference 运行协议](docs-site/api/guides/inference-runtime.md#离线视频恢复-operator实施中)
和 [ADR-0160](docs/adr/0160-coordinated-video-restore.md)。

### Identity 已接受 API-key 的最终授权

训练及相关持久操作的 key ID 最终检查通过
`aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])` 执行。先应用当前迁移和
`module-data-grants.sql`；过渡应用连接的工作树／LAN 权限清单已包含该固定函数，
不增加新的连接或服务。函数只给出 boolean，不返回 key hash 或凭据明文。

调用方继续持有账号 SHARE、项目 KEY SHARE，再等待 key SHARE；过期以取得锁后的真实
数据库时间判断。Native 锁错误与事务失败状态原样保留，适配器不提交或回滚。
普通 AUTH、key-management 与建题 LOGIN 不获得这项能力。源码及定向验收见
[ADR-0161](docs/adr/0161-identity-current-authority-consumers.md)，完整 Identity 的冷安装、
容器和恢复证据见上文“Identity 整模块私网验收”，与这里的定向结果独立保留。

### Workspace 普通调用账号权限

在迁移、`module-data-grants.sql` 和现有应用授权之后，由迁移账号对选定普通 API／
worker LOGIN 显式执行下面的配置。连接参数由当前隔离环境提供；不要使用日常库
进行验收或把 seed／截图操作账号作为普通调用账号。

```bash
psql -v expected_database=annotation_test -v migration_role=owner \
  -v workspace_owner=workspace_code -v consumer_role=ordinary_api_role \
  -f infra/docker/workspace-consumer-grants.sql
```

该配置只保留32项 Workspace 固定函数和两张事实视图，撤销私表／列、内部回执、
项目序列和私有 schema 创建权限，同时移除未来 public 表／序列的宽默认授权。
其他领域当前权限和 Labeling 计数关系保持。错误身份或遗留 PUBLIC 权限会使整份
配置回滚；迁移者继承运行账号的维护方向仍允许。迁移或重新执行宽授权后需重跑。
旧应用授权已补齐默认配置／模板／目录及成员搜索能力。完整边界见
[Workspace 运行指南](docs-site/api/guides/workspace-runtime.md#普通消费者的-sql-凭据边界)。

### Workspace 组合私网验收

准备当前源码的非 editable Workspace 工件，以及下表四个冷镜像。每个镜像设置唯一的
`org.aap.acceptance-id`，用构建时提交设置 `org.aap.source-revision`；对应 `IDENTITY`
变量须与镜像标签一致。若全部 Docker COPY 输入的文件集合与哈希仍匹配，可以复用
已验收工件；收据必须保留当前源码与工件输入对照。六个 Workspace 服务复用同一个
owner 镜像。

| Dockerfile                      | `org.aap.acceptance`             | 镜像／身份环境变量                                                                        |
| ------------------------------- | -------------------------------- | ----------------------------------------------------------------------------------------- |
| `apps/workspace/Dockerfile`     | `workspace-module-owner`         | `AAP_WORKSPACE_MODULE_IMAGE`／`AAP_WORKSPACE_MODULE_IDENTITY`                             |
| `infra/docker/Dockerfile.api`   | `workspace-module-api`           | `AAP_WORKSPACE_MODULE_API_IMAGE`／`AAP_WORKSPACE_MODULE_API_IDENTITY`                     |
| `apps/inference/Dockerfile`     | `workspace-module-inference`     | `AAP_WORKSPACE_MODULE_INFERENCE_IMAGE`／`AAP_WORKSPACE_MODULE_INFERENCE_IDENTITY`         |
| `apps/assets-source/Dockerfile` | `workspace-module-assets-source` | `AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IMAGE`／`AAP_WORKSPACE_MODULE_ASSETS_SOURCE_IDENTITY` |

配置上述变量后，每个案例单独使用一个新的验收数据库：

```bash
AAP_WORKSPACE_MODULE_ASSEMBLY_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-module-assembly.json \
  --test-file tests/test_workspace_module_assembly_live.py
```

该入口复用六个现有 Workspace Compose 片段，另起 API、Inference probe 和 Guide 对象
服务；只连接验收器拥有的 PostgreSQL／Redis 私网。测试保留 seeded 业务行直到整库销毁，
完整恢复 clone 先检查所有权标记和连接，再删除；不终止未知连接。对象身份只覆盖隔离桶／
前缀，API 启动凭据不能读写源对象。普通 API 最后应用 Workspace consumer 权限配置，
私表读取拒绝，借用事务操作通过固定函数执行。

API 的容器就绪检查覆盖 DB／Redis／MinIO；该组合未启动 Celery，总 `/health` 仍可返回
503。组合案例已通过故障拒绝、重启重放、事务共同回滚和真实恢复后的 API／owner 检查。
实际 Celery 的独占案例使用同一个 API 冷镜像，已通过真实投递、持久化结果与重启检查：

```bash
AAP_WORKSPACE_WORKER_ASSEMBLY_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-worker-assembly.json \
  --test-file tests/test_workspace_worker_assembly_live.py
```

worker 案例检查真实 broker 投递、Native 项目资料、结果 owner 写入、消息确认和进程重启；
确定性 HTTP peer 只用于协议联通，不证明真实 ML、来源读取或公共 retry 入队端点。
当前通过边界、有限本地采样与遗留视频恢复缺口见
[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。主环境不自动切换。

### Workspace cross-frame 冷 worker 验收

同一冷 API 工件可执行跨帧任务。先核对完整 COPY 输入集合及哈希，确保工件中的任务
实现匹配当前源码，再运行独占案例：

```bash
AAP_WORKSPACE_CROSS_FRAME_WORKER_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-cross-frame-worker.json \
  --test-file tests/test_workspace_cross_frame_worker_live.py
```

开关只用于验收夹具。worker 以受限 `identity_api_consumer` 连接自有 PostgreSQL／
Redis 私网，所有十个桶均显式绑定隔离资源。普通员工通过 annotator 项目成员身份执行，
不使用管理员权限绕过成员读取。默认组合授权同时包含既有 Spatial Native 合同，
供传播读取同场景 item 和 pose；仍通过 owner 能力读取。任务注册、实际签名、队列、
消息确认、数据库客户端
身份和重启由实际容器核对；跨帧传播不调用 ML 服务。

本案例验证 Workspace 的项目与成员调用边界，不代表完整 Labeling 或 Jobs 模块资格。
最终通过状态和证据按[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)
记录；主环境不自动切换。

### Workspace export 冷 worker 验收

复用前节全部冷镜像和身份变量，核对 API 镜像的完整 COPY 输入集合及哈希后运行：

```bash
AAP_WORKSPACE_EXPORT_WORKER_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-export-worker.json \
  --test-file tests/test_workspace_export_worker_live.py
```

该开关只用于隔离夹具。案例用普通 employee 的 reviewer 身份执行真实 AAP JSON 导出，
通过数据库锁卡住实际构建，再验证整容器 SIGKILL 后的新 task ID 接管、原始 unacked
消息精确恢复、prefork 子进程丢失后的自动同 ID 重投，以及排队后撤员、缓存复用、
产物和源图片的真实签名 URL 下载。Worker 无 Workspace 私表读取或桶管理权限。

整容器恢复使用按 task ID、任务名、唯一私有队列和索引验证的原始 broker 消息，不
修改 visibility timeout；因此此案例不证明整主进程丢失后的自动恢复耗时。PostgreSQL
查询阻塞时可能尚未察觉客户端断开，夹具先记录状态、释放测试锁，再等待实际旧 backend
消失。采用新的 PostgreSQL 会话锁前须排空旧 export worker；主环境不会自动切换。
当前资格及限制见[唯一台账](docs-site/dev/concepts/module-deployment.md#workspace-export-recovery)。

### Workspace video-tracker 冷 worker 验收

复用既有冷镜像与身份变量，重新构建包含当前夹具的 API 工件并核对完整 COPY 输入后，
选择独占的视频执行案例：

```bash
AAP_WORKSPACE_VIDEO_TRACKER_WORKER_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-video-tracker-worker.json \
  --test-file tests/test_workspace_video_tracker_worker_live.py
```

夹具沿用 `identity_api_consumer` 的 Workspace 围栏、私网 PostgreSQL／Redis 和真实
Celery 任务，分别绑定既有 Inference Routing 与 Labeling 视频写入能力。案例覆盖普通
annotator 的执行、HTTP 取消后保留已提交帧、排队后撤员／停用，以及 prefork 子进程
丢失后的同 ID 重投。模型对照读取真实冻结的视频输入，退役必须来自实际 owner 和
后端控制器；恢复时只将精确旧代次的租约置为到期以控制夹具耗时，不证明默认恢复延迟。

Media 与确定性推理 peer 仍通过 `host.docker.internal` 访问，沿用当前未修复的宿主机
转发依赖。当前已通过一个冷组合案例和一个独立 HTTP 撤权前置案例；五个 task ID
共六次真实接收，最终队列与 unacked 均归零，完整迁移及自有资源清理通过。
限定资格与恢复延迟限制见[唯一台账](docs-site/dev/concepts/module-deployment.md#workspace-video-tracker-cold-worker)。

### 本地旧视频凭据围栏验收

复用前节不可变 API 镜像及身份变量；额外将 `AAP_LEGACY_VIDEO_FENCE_API_SOURCE_REVISION` 设为该镜像的 `org.aap.source-revision` 标签。此开关仅用于验收器独占 PostgreSQL 和随机专用队列：

```bash
AAP_LEGACY_VIDEO_FENCE_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/local-video-fence.json \
  --test-file tests/test_legacy_video_fence_live.py
```

夹具播种历史 Job 和 scope-only 形态，不能代表旧二进制或有效 admission；不关闭 tracker permit，也不生成 Jobs execution、journal 或 clearance。先成功写入／读回专用 S3 对象，再核验进程、资源、凭据及 scope 身份；随后真实退役 scope、停止进程、撤销存储身份及该独占 PostgreSQL 的全部非 owner LOGIN，最后用相同旧凭据重投原任务并要求失败。未知 worker、DB 选择器或共享资源均拒绝继续；不 purge 全局 broker。

该案例必须最后执行，因为后续普通账号已不能登录。验收器在核对同一专属 PostgreSQL 容器后，仅用 owner 完成资源销毁，保留所有所有权检查，不重启已围栏角色。最新端到端案例及清理通过；它只关闭本地物理围栏前提，正式旧 Jobs 退役凭证和历史输入修复仍待实现。范围和证据见[组合验收台账](docs-site/dev/concepts/module-deployment.md#coherent-owner-operations)。

### Workspace 冷运维导入验收

复用上表 API 冷镜像及其身份变量，使用独占验收数据库和全部十个隔离桶：

```bash
AAP_WORKSPACE_IMPORT_CLI_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-import-cli.json \
  --test-file tests/test_workspace_import_cli_live.py
```

该开关只选择验收夹具，不是运行时部署配置。验收器在环境准备完成后收紧既有可信
`legacy_runtime` 的 Identity／Workspace 权限，仅补回两项 provisioning 能力；普通 API
凭据不变。真实 `python -m scripts.import_nuscenes_scene` 使用冷镜像和只读数据挂载，
存储身份仅能操作隔离 datasets 桶的 nuScenes 保留前缀，不持有桶管理权限。

一个集成案例覆盖缺失能力、后置 SQL 失败、实际容器中断、同名恢复导入、重复导入和
来源漂移。两场景各两帧的夹具成功产生十项素材和四项任务；中断时核对数据库客户端
地址，停止实际容器，等待连接释放后确认 SQL 回滚，再重新导入。PostgreSQL 可能在
当前查询结束后才发现客户端断开；十秒人工阻塞只证明最终回滚，不证明即时取消长 SQL。
失败保留的 S3 字节由隔离桶销毁清理；重放可能重新上传同键对象和追加操作回执。
这不证明真实数据集、容量或整模块完成，剩余边界继续见唯一台账。

### Workspace 冷开发种子验收

匹配当前源码构建 `infra/docker/Dockerfile.api`，按前节冷运维入口的镜像身份要求设置
`AAP_WORKSPACE_MODULE_API_IMAGE` 和 `AAP_WORKSPACE_MODULE_API_IDENTITY`，再执行：

```bash
AAP_WORKSPACE_SEED_OPERATOR_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-seed-cold.json \
  --test-file tests/test_workspace_seed_operator_live.py
```

验收器只在自己拥有的环境中给可信 `legacy_runtime` 组合 Identity／Workspace 围栏、
既有两项 provisioning 能力和三项 seed 写入；普通 API 保持 32 项 Workspace 函数。
实际脚本使用只读挂载的小夹具和真实 S3，不下载完整 nuScenes。目录成功、后置能力
缺失、显式回滚、旧种子替换和漂移重跑共五次容器调用，合计一个综合案例。
SQL 失败／回滚保留旧状态和原始字节，旧对象清理发生在调用方提交后。该验证已通过，
精确工件和日志见 `.worktree/workspace-seed-qualification.json`；主环境未切换。

### 门控 E2E 的 Workspace 边界

测试库中先安装 module-data owner grants、普通 Workspace consumer 围栏，再为明确
选择的 fixture 操作账号追加 `workspace-seed-operator-grants.sql` 与
`workspace-e2e-fixture-grants.sql`。后者只授予五项固定能力和模板 ID 关系；普通
API／worker 不得到 fixture 能力。既有工作树测试账号按数据库后缀获得固定能力，
不改变日常 Workspace 主环境的切换状态。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-e2e-native.json \
  --test-file tests/test_workspace_e2e_native.py \
  --test-file tests/test_worktree_filtering_identity_native.py
```

真实筛选 CLI 覆盖已有数据拒绝、首次创建及保留修改后的重启，使用普通 E2E LOGIN，
显式拒绝 Workspace 私表。冷工件验收复用 `test_identity_e2e_live.py`，同时检查
Identity／Workspace 围栏；设置 `AAP_IDENTITY_E2E_FIXTURE_LIVE=1` 及匹配当前源码的
`AAP_WORKSPACE_MODULE_*` 镜像和 identity 后运行。它不切换主环境，也不证明整模块
部署完成。清理遇到未退役视频返回 409，先取消并完成真实退役再重试。

### 规模种子与截图支持读取

应用当前迁移和 owner grants 后，可信操作账号先应用普通 Workspace consumer
围栏，再组合 `identity-scale-seed-grants.sql` 和 `workspace-scale-seed-grants.sql`。
后者追加四项 Workspace 能力、一项 Assets 来源 guard 和既有 Labeling 计数 writer，
保留其他领域的既有权限；普通 API 不得到这些规模操作能力。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-scale-native.json \
  --test-file tests/test_scale_seed_workspace_native.py \
  --test-file tests/test_identity_seed_selection_native.py
```

截图夹具的 golden 包含全库账号，需与图片导入夹具分开使用验收库，原基线保持：

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-screenshot-readers.json \
  --test-file tests/test_identity_recording_native.py::test_jobs_recording_identity \
  --test-file tests/test_identity_recording_native.py::test_review_recording_identity \
  --test-file tests/test_identity_recording_native.py::test_jobs_cached_email_keeps_original_id \
  --test-file tests/test_identity_recording_native.py::test_jobs_cached_project_preserves_original_pipeline_and_name
```

冷验收分别复用 `test_identity_seed_selection_live.py` 和
`test_identity_recording_live.py`，对应开关为 `AAP_IDENTITY_SEED_SELECTION_LIVE=1`
和 `AAP_IDENTITY_RECORDING_FIXTURE_LIVE=1`，使用匹配当前源码的
`AAP_WORKSPACE_MODULE_*` 工件。生产拒绝仍在 `seed_scale.py` 创建 engine 前执行；
保留引用或忙锁必须先解决再重试，不能补造退役证明。

### 媒体种子与截图 Workspace 边界

安装当前迁移并应用 module-data owner grants 后，可信操作账号先应用普通 Workspace
consumer 围栏，再追加 `workspace-seed-operator-grants.sql` 的六项写入能力。普通 API／
worker 保持原 32 项 Workspace 函数与事实读取。新的三项原生能力及事务语义见
[运行指南](docs-site/api/guides/workspace-runtime.md#媒体及截图种子)。

```bash
apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/workspace-media-native.json \
  --test-file tests/test_media_seed_workspace_native.py

AAP_WORKSPACE_MEDIA_SEED_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-media-cold.json \
  --test-file tests/test_media_seed_workspace_live.py
```

冷运行需要与当前源码匹配的既有 API 工件身份变量；复用 Workspace CLI 的隔离
PostgreSQL／Redis／S3 夹具。实际媒体入口使用只读小夹具与原始视频副本，检查权限
撤销后的回滚、恢复、重跑和对象哈希；不下载完整数据集，也不调用真实 ML 模型。
截图定向检查覆盖两种 autoflush、成员时间、项目计数和中间 flush 的后端字段，固定
夹具通过既有删除 owner 清理。本组 68 项去重 Native／消费者检查和一个冷组合案例已
通过。非 editable Workspace／API 工件分别核对 292／1326 个文件；六次冷命令包含
权限撤销与恢复、媒体重跑／哈希、截图元数据／成员及最终时间固定。完整截图录制与
其他领域资格继续见[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### 主种子冷运行验收

复用当前 API 冷工件及上节镜像身份变量：

```bash
AAP_SEED_PARENT_OPERATOR_LIVE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --output .worktree/seed-parent-cold.json \
  --test-file tests/test_seed_parent_operator_live.py
```

验收器在自有资源准备后给可信运维连接组合 Identity／Workspace 围栏与精确追加能力，
User／Project／Member 私表及列权限均被拒绝。九次冷容器调用只执行镜像中的真实
`_ensure_seed_accounts`／`_ensure_demo_memberships`，不替换其实现，也不执行媒体子脚本。
进程中断绑定 SQL 阻塞、唯一应用标签、容器 IP 与数据库 PID，实际退出 137 后等待该
后端消失再核对共同回滚；人工阻塞最多十秒，不代表即时终止任意 SQL。

恢复新建 25 条成员，漂移只修复原字段，无变更时调用方 pending work 不提交；桶版本
集合保持不变。该综合案例已通过，工件、原始失败和最终收据见
`.worktree/seed-parent-qualification.json`。这是本地小夹具资格，日常主环境未切换。

### Workspace 公共结构契约与混合负载验收

保存流水线结构由 `aap-inference-contract` 提供；Workspace 镜像与核心不再安装 Inference
提供方。阶段／请求 Schema 位于该包的 `preannotate.schema.json`，其跨字段规则和兼容
规范化见包 README。旧 Inference 导入仍为共享对象的兼容别名。

准备与当前源码匹配的四个镜像及身份变量，再运行：

```bash
AAP_WORKSPACE_CAPACITY_LIVE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py --output .worktree/workspace-capacity.json \
  --test-file tests/test_workspace_capacity_live.py
```

这个测试开关复用九个已有服务和普通 API 的受限 SQL 配置，同时提供既有受限启动存储
身份；不改变部署模式。并发 1／4／8 各使用十五秒准入窗口、每通道独立账号组与项目。
记录真实延迟／吞吐、稀疏资源和连接样本，并核对实际回执、审计和最终状态；在持续
流量中检查账号忙 409、撤权和旧预览拒绝。Node.js 通过实际 API 检查流水线规范化及
错误回滚。正常成功流量与明确的竞争／撤权拒绝分别报告，所有原始失败日志保留。
相同账号的管理写入仍可能按既有 NOWAIT 规则返回 409；不引入隐藏重试或放宽围栏。
有限本机数据与未完成范围见[唯一台账](docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

### Labeling 任务控制绑定

编辑锁与生命周期／审核证据分别调用
`aap_labeling_v1.task_edit_lock_command(jsonb)` 和
`aap_labeling_v1.task_workflow_command(jsonb)`。应用当前迁移后，重施
`infra/docker/module-data-grants.sql` 及既有 LAN／worktree runtime 授权；需要专用可信
调用账号时使用 `infra/docker/labeling-task-control-grants.sql`，参数见文件头。

调用继续借用业务连接及事务，不增加环境变量、HTTP 锁服务或独立提交点。更新 API 与
实际挂载这份源码的 Celery 消费者后再使用新函数；主工作树未采用集成源码时，不能只升级
日常数据库并把它当作运行切换。批次及标注内容尚未迁移的私有访问仍保留，完整 Labeling
资格与剩余范围见[运行协议](docs-site/api/guides/labeling-runtime.md#任务编辑锁与生命周期)。

### Labeling 人工标注写入

人工标注、轨迹关联及任务统计使用 `annotation_write_command(jsonb)`、
`scene_track_domain_command(jsonb)`、`annotation_statistics_command(jsonb)` 三项
`aap_labeling_v1` 能力。匹配的迁移同时更新 Mask 修订触发器引用，保留原快照和版本规则。
应用匹配迁移后重施 owner 及既有 runtime 授权；专用可信调用账号由
`infra/docker/labeling-annotation-writes-grants.sql` 配置，原任务控制账号不扩大执行权。

所有写入继续借用当前业务事务，当前权限、Mask 预检、审计和提交顺序保留，不新增部署
配置。集中验收器会创建自有数据库／角色，并把专用 DSN 交给保存的 Native 回归；
不手工把日常库填入测试选择器。[运行协议](docs-site/api/guides/labeling-runtime.md#人工标注与关联写入)
说明当前范围，完整 Labeling 的剩余写入及资格继续由唯一台账跟踪。

### 宿主机转发诊断

在 macOS 上使用已存在的不可变 Docker Python 镜像。镜像必须提供
`/opt/venv/bin/python`（Python 3.11 以上），并允许普通 UID `10003` 运行。先把镜像
SHA-256 ID 放入本地 shell 变量 `HOST_FORWARDING_IMAGE_ID`，再运行：

```bash
python3 scripts/verify_docker_host_forwarding.py \
  --image "$HOST_FORWARDING_IMAGE_ID" \
  --output .worktree/host-forwarding-diagnosis.json
```

诊断只创建带唯一所有权标签的临时 echo 容器、网络和宿主机 loopback 监听器，核对标记
后正常关闭，再用同一源端口重连。普通模式不需要 root、业务凭据或抓包能力，不拉取
镜像、不执行 SQL、不改变 Docker 配置，也不自动重试／切换失败路由。退出码非零并报告
`diagnosed_not_repaired` 表示复现了故障；没有复现也不能据此宣称平台缺陷已修复。

需要宿主机控制包元数据时，在**本机交互终端**先授权，再使用一个新的输出文件名：

```bash
sudo -v
python3 scripts/verify_docker_host_forwarding.py \
  --image "$HOST_FORWARDING_IMAGE_ID" --host-trace \
  --output .worktree/host-forwarding-host-trace.json
```

管理员口令只在本机终端输入，不发送到聊天、命令参数或日志。抓取限定为本次自有原生
监听端口的 TCP 元数据，不保存业务载荷；用后只停止已核对身份的自有抓取进程并清理
临时资源。未取得 BPF／sudo 权限时会明确报告前置条件失败，不能记录为通过。
[当前范围与未解决部分](docs-site/dev/concepts/module-deployment.md#runtime-host-forwarding)
仍按唯一台账跟踪；此入口不是 Docker 的修复开关。
