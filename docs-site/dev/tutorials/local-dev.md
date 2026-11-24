---
title: 本地开发环境搭建
audience: [dev]
type: tutorial
status: stable
last_reviewed: 2026-10-19
---

# 本地开发

本教程引导你安装平台开发环境：API／Web 在宿主机热重载，PostgreSQL、Redis、MinIO、Mailpit、Celery worker 和 Beat 运行在 Compose。在线学习需要另外配置独立 `training-executor`。

## 前置依赖

- **Node.js** `>= 22`（仓库 `.nvmrc` 指定 Node 22，`.npmrc` 开启 `engine-strict`）
- **pnpm** `>= 10`
- **Python** `>= 3.11` 与 **[uv](https://docs.astral.sh/uv/)**
- **Docker** 与 **Docker Compose**
- 可选：**[pre-commit](https://pre-commit.com/)**（推荐安装以在提交前自动运行门禁）

## 一次性初始化（Setup）

平台 API 和 SDK 测试使用 [Plugin-SDK 的 runtime 快照](https://github.com/YOAO-Labs/Plugin-SDK/releases/tag/platform-ci-runtime-8daeb0cf02ac)。在仓库根目录运行下列命令；清单摘要固定在这里，清单中的 wheel 摘要与平台依赖锁一致。不需要另一份 SDK checkout。

```bash
# 1. 安装前端与工作区依赖
pnpm install
# pnpm install 会自动运行 prepare；若本机安装了 pre-commit，可执行 pre-commit install

# 2. 安装后端 Python 环境（含测试依赖）
SDK_MANIFEST=https://github.com/YOAO-Labs/Plugin-SDK/releases/download/platform-ci-runtime-8daeb0cf02ac/manifest.json
SDK_MANIFEST_SHA256=5af0e5e8c3cb68436446b2f917193a024771ec2daf94de29fc35056da75ef8a5
python3 scripts/fetch-python-artifacts.py --manifest "$SDK_MANIFEST" --sha256 "$SDK_MANIFEST_SHA256" \
  --output .artifacts/python/plugin-sdk --package yoao-backend-runtime
cd apps/api
uv sync --locked --extra test

# 3. 首次只启动基础设施
cd ../..
docker compose up -d --wait postgres redis minio mailpit
```

按[本地冷启动](/ops/deploy/development#development-cold-start)迁移目标库，安装普通角色、Jobs claim／publication、视频及图片执行授权，准备各自的文件。不要在未配置受限凭据时启动完整栈，也不要用现有日常拆分环境试跑新配方；多工作树使用下节的隔离 launcher。

---

## Orca 多工作树初始化（Worktree）

仓库根目录的 `orca.yaml` 使用 `scripts/orca-worktree-setup.sh` 完成依赖软链接与独立虚拟环境初始化。候选 wheel 必须先暂存到新 checkout；创建工作树时先跳过自动 setup，暂存后再调用既有脚本。`.artifacts` 不会从源 checkout 自动复制或共享。

- **环境变量与依赖共享**：`.env`（及 `.env.local`）软链接至主工作树；当锁文件与 `package.json` 未变更时，`node_modules`、`apps/web/node_modules` 与 `docs-site/node_modules` 自动建立软链接，否则回退至 `pnpm install --frozen-lockfile`。
- **Python 虚拟环境与生成类型隔离**：`apps/api/.venv` 由 `uv sync --project apps/api --locked --extra test` 独立创建（避免 editable 包路径串回主目录）；`pnpm codegen` 根据当前工作树的 `openapi.snapshot.json` 在本地生成 `apps/web/src/api/generated/`。
- **一键启动隔离开发栈**：主目录已有本机 PostgreSQL / MinIO 后，在工作树内运行 `pnpm dev:worktree` 即可自动创建隔离数据库、Redis 和用途存储桶并启动 API 与 Web（需后台任务时使用 `pnpm dev:worktree -- up --with-worker`）。详见[独立工作树开发环境](../how-to/worktree-environments)。

```bash
# 先创建工作树；以下路径替换为实际输出的新 checkout 和源 checkout
orca worktree create --name my-task --agent codex --setup skip --json
cd /path/to/new-worktree
python3 scripts/fetch-python-artifacts.py --manifest "$SDK_MANIFEST" --sha256 "$SDK_MANIFEST_SHA256" \
  --output .artifacts/python/plugin-sdk --package yoao-backend-runtime
ORCA_ROOT_PATH="/path/to/source-checkout" ORCA_WORKTREE_PATH="$PWD" bash scripts/orca-worktree-setup.sh

# 验证工作树 setup 脚本的软链接与隔离逻辑
python3 scripts/test-orca-worktree-setup.py
```

---

## 日常启动流

仅用于已完成迁移和权限配置的当前开发项目。`HOST_JOBS_PUBLICATION_FILE` 指向给宿主 API 的受限 DSN 文件，不需要 claim 凭据。

```bash
# 1. 第一次启动须先完成上文的窄授权
docker compose up -d --wait

# 2. 启动后端 API（终端 1，先迁移后热重载）
JOBS_PUBLICATION_DATABASE_URL_FILE="$HOST_JOBS_PUBLICATION_FILE" pnpm dev:api

# 3. 启动前端开发服（终端 2）
pnpm dev:web               # http://localhost:3000

# 4. 查看 Compose 中的 Celery Worker 与 Beat 状态
docker compose ps celery-worker celery-worker-gpu celery-worker-cpu celery-worker-export \
  celery-worker-image-pyramid celery-worker-gpu-control celery-worker-maintenance \
  celery-worker-jobs-recovery celery-beat
```

`celery-worker-jobs-recovery` 只消费 `jobs.recovery`；Beat 不读取 claim／publication 文件。Identity、Notifications socket 和目录默认不额外起服务，其他可选 owner 及其凭据见[部署矩阵](/ops/deploy/docker-compose#module-process-selection)。

> **提示**：Celery 不会热重载挂载的 Python 代码。修改 `apps/api/app/workers/` 或相关服务后，请执行 `docker compose restart celery-worker ...` 刷新对应队列的消费者。

API 文档预览入口：

- **实时 Swagger UI**：`http://localhost:8000/docs`
- **静态化文档站 API 参考**：[/api/](/api/)（源自 `apps/api/openapi.snapshot.json`）

---

## 准备演示数据（可选） {#demo-data}

仅在已确认目标数据库和存储的开发或 staging 环境运行；脚本会拒绝 production。

```bash
cd apps/api
uv run python scripts/seed.py
```

首次运行会下载约 4 GB 的 nuScenes mini 到 `~/.cache/ai-annotation-platform/nuscenes-mini`。需要视频示例时传入 `--video /path/to/video.mp4`，并核对素材使用许可。默认演示账号 `admin`、`pm`、`qa`、`anno`、`anno2`、`anno3` 和 `viewer` 的初始密码为 `123456`，重跑不重置已有账号，也不覆盖人工标注。需要自动化 E2E 夹具时，改用[隔离测试入口](../testing)。

---

## 在线学习与训练执行器联调（可选）

当开发或验证「在线学习与模型训练闭环」（[架构文档](../concepts/training-datasets)）时，需要额外启动独立训练执行器 `apps/training-executor`：

### 1. 准备训练执行器

执行器需要与 API 一致的训练目录、独立凭据、节点身份、单张物理 GPU UUID、固定镜像摘要和已校验的基础权重。仅填写 Token 或安装 Python 包不能完成部署。

按[在线训练执行器部署](/ops/deploy/docker-compose#_8-5-受控训练执行器-training-executor)准备并启动执行器。`TRAINING_API_URL` 必须指向当前开发 API 的 `/api/v1/internal/training`。如果 Linux 上的宿主 API 只监听 `127.0.0.1:8000`，在独立训练 Compose 项目的本机覆盖中使用 `network_mode: host`，并将该 URL 设为 `http://127.0.0.1:8000/api/v1/internal/training`。执行器以非 root 身份运行，确保其 UID/GID 能读取 `0600` 凭据并写入持久 `/state`。

### 2. 准备 COCO8 检测夹具

同一入口同时准备实例分割夹具。

在已经确认的开发数据库和对象存储中运行：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed.py --profile online-training
```

这个显式 profile 创建 8 张已审核图片、30 个检测框和 30 个实例掩膜，保留原始 4 张训练图与 4 张验证图，并同步构建检测与原生 RLE 分割两个初始数据版本。旧检测夹具只有在任务未被人工修改、没有锁或草稿时才能增补分割；已有分割夹具重跑保留人工修改。它不会自动创建训练实验、开启自动策略或发布模型。失败、取消或中断后可重跑同一命令；仍在执行或清理时按提示稍后重试。

夹具需要 PostgreSQL、MinIO 和 Redis，不需要 export worker。首次运行会校验并缓存 COCO8 与 COCO8-seg 归档，默认缓存为 `~/.cache/ai-annotation-platform/seed-assets`。用 `--cache-dir` 指定其他缓存目录，或用 `--offline` 复用现有缓存。通过 `--asset-dir` 指定本地素材时，该目录必须同时包含 `coco8/` 和 `coco8-seg/`。

进入界面后依次创建实验、确认成功执行记录、添加模型、加载试用并人工发布，参见[在线学习用户指南](/user-guide/online-learning/)和[模型回流链路排查](../how-to/debug-model-return)。实例分割的逐项操作和目标 GPU 独立验收见[实例分割在线学习验收](../how-to/validate-segmentation-online-learning)。

---

## 常用开发脚本速查

```bash
# 单元测试与端到端测试
pnpm test                  # 前端 Vitest 单元测试
pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e  # 前端 Playwright E2E 测试
cd apps/api && uv run pytest
cd apps/training-executor && uv run pytest

# OpenAPI 契约与前端类型生成
pnpm codegen               # 从 openapi.snapshot.json 生成前端 TS 类型
pnpm openapi:export        # 修改后端路由/Schema 后重新导出快照
pnpm openapi:check         # 校验快照与当前后端运行时零漂移

# 代码质量与格式门禁
pnpm format:check          # 只读检查全仓 Prettier + Ruff 格式
pnpm format                # 自动修复全仓格式
pnpm lint                  # 前端 ESLint + CSS 设计令牌门禁
pnpm typecheck             # 前端 TypeScript 类型检查

# 文档站开发与构建
pnpm docs:dev              # 启动 VitePress 本地预览（默认 http://localhost:5173）
pnpm docs:build            # 构建生产文档站并校验死链
```

## 数据库迁移（Alembic）

```bash
cd apps/api
uv run alembic upgrade head                          # 升级至最新迁移版本
uv run alembic revision --autogenerate -m "..."      # 基于模型变更自动生成迁移脚本
uv run alembic downgrade -1                          # 回滚最近一步迁移
```

详细操作规范请参见 [新增数据库迁移](../how-to/add-migration)。
