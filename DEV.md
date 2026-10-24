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
Insights、Diagnostics、Export、Audit 十一个无登录函数 owner；另有两个独立登录角色
`append_client` 和 `asset_cleanup_client`。后者只可使用 `aap_assets_v1` 的四项清理函数：
既有 storage outbox 的领取/确认，以及图片垃圾的领取/确认；`append_client` 不可调用这些清理函数。
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

## 独立 Identity 运行时

`apps/identity` 与 `apps/_shared/mail_transport` 提供独立运行包。复用
`docker-compose.identity.yml` 配置独立 owner、邮件和通知投递；公开 API 通过
`IDENTITY_URL`、`IDENTITY_TOKEN` 选择远程入口，所需的签名密钥和各项能力 DSN 留在
owner 进程。未配置远程入口时，原有调用方事务绑定继续可用。

先在隔离数据库验证迁移与凭据，再采用到日常环境。完整 Identity 消费者所需的线性
迁移链从 `0234` 到 `0306`，不能挑选其中部分 Identity 迁移安装。密码恢复／邮箱验证
的旧令牌在升级中转为 SHA-256，并绑定签发邮箱；有状态时应使用验证过的快照恢复
路径回退，不能仅修改 Alembic revision。

`infra/docker/identity-consumer-grants.sql` 应在 owner 和普通应用授权之后执行。普通 API
只读取两张公开关系，执行六项固定能力，包括旧模板／编排所需的组织 ID 读取；不能
直接读取 Users、Groups、Organizations、凭据、邀请或回执表。该兼容读取复用已有
Identity 函数，不要求切换 Workspace 实现。

重复的环境变量和 file selector 会失败关闭。新增 API 设置见 `.env.example` 和
[Identity 协议](docs-site/api/guides/identity-runtime.md)。受限 API 的兼容检查使用
独占验收器，Node 调用也不需要 Python 提供方运行时：

```bash
AAP_MODULE_DATA_ROLE_ACCEPTANCE=1 apps/api/.venv/bin/python \
  scripts/verify_module_data_boundary.py \
  --output .worktree/identity-primary-compatibility.json \
  --test-file tests/test_identity_primary_compatibility.py
```

API 的冷镜像与完整 owner 装配另行验收。上述定向检查不证明主环境已切换、完整
Workspace 已完成或生产容量达标，Go 保持实验性质。

## 独立 Notifications 运行时

`apps/notifications` 持有偏好、收件箱、通知写入、投递授权、即时发布、持久投递与
WebSocket owner。`docker-compose.notifications-publisher.yml` 与
`docker-compose.notifications.yml` 分别装配 publisher 和 socket，不发布宿主机端口。
它们和 Identity、调用方必须接入同一选定私网；不会由默认 Compose 自动启动。

完成 secret 和私网配置后，显式选择服务；兼容 `identity-notification-publisher`
与新 publisher 共享同一 outbox，不应在替换过程中无意同时启动：

```bash
docker compose --env-file .env.notifications -f docker-compose.yml \
  -f docker-compose.identity.yml -f docker-compose.notifications.yml \
  -f docker-compose.notifications-publisher.yml up -d --build \
  identity-admission notifications-socket notifications-publisher
```

先确认旧 publisher 已停止，再启动替代进程；原 Identity CLI 仅作为兼容入口保留。

API 以 `NOTIFICATION_PUBLICATION_URL`、`NOTIFICATION_PUBLICATION_TOKEN(_FILE)` 和
`NOTIFICATION_SOCKET_URL` 选择远程发布与 socket；留空保留内嵌绑定，显式地址故障
不回退。独立 publisher 使用 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)`、
`NOTIFICATION_PUBLISH_REDIS_URL(_FILE)`；设置 `NOTIFICATION_PUBLISH_API_TOKEN(_FILE)`
后在同一进程启用即时 HTTP，否则保持 durable-only。socket 的七函数只读数据库、
订阅专用 Redis 与 Identity admission-only bearer 使用独立 secret。

迁移必须保留 `0306 → 0325` 的完整历史。其间包含 Workspace、Audit 和视频输入
保留迁移，特别是 `0313` 会改变资产垃圾领取围栏。采用前应核对最新功能合并和资产
变更范围；源码候选不代表这些迁移已应用到日常数据库。不能抽取或重编号其中的
Notifications 迁移。配置、授权顺序和恢复语义见
[Notifications 运行时](docs-site/api/guides/notifications-runtime.md)。

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

先将迁移应用至当前 head `0218`、完成 module-data owner 授权，再为独立 `asset_source_client` LOGIN 应用：

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

日常开发库已采用通过整模块本地验收的 Identity 检查点，并从 `0234` 升到 `0306`。采用前保留私有数据库与集群角色备份，实际备份在独占 PostgreSQL 恢复后与原数据一致；升级后 171 张原有业务关系及唯一业务行保持。主 checkout 按锁文件只新增 Identity 和共享邮件包，原 macOS LaunchAgent 已重启；DB／Redis／MinIO、注册策略读取、未认证拒绝、OpenAPI 快照及真实源码热重载通过。日常 API 仍使用既有内嵌绑定，独立 owner 拓扑没有切换。八类 worker／beat 仍未启动，其旧镜像尚未按本检查点刷新；启用前需要重建并核对实际安装内容，因此默认 `/health` 和 `/health/celery` 仍为 503。Go 不加入正常运行路径。

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
