---
title: 本地开发环境搭建
audience: [dev]
type: tutorial
status: stable
last_reviewed: 2026-10-19
---

# 本地开发

本教程引导你在本机搭建完整的平台开发环境，包括前端（Vite/React）、后端（FastAPI + Celery）、基础设施容器（PostgreSQL / Redis / MinIO / Mailpit）以及在线学习独立训练执行器（`training-executor`）。

## 前置依赖

- **Node.js** `>= 22`（仓库 `.nvmrc` 指定 Node 22，`.npmrc` 开启 `engine-strict`）
- **pnpm** `>= 10`
- **Python** `>= 3.11` 与 **[uv](https://docs.astral.sh/uv/)**
- **Docker** 与 **Docker Compose**
- 可选：**[pre-commit](https://pre-commit.com/)**（推荐安装以在提交前自动运行门禁）

## 一次性初始化（Setup）

```bash
# 1. 安装前端与工作区依赖
pnpm install
# pnpm install 会自动运行 prepare；若本机安装了 pre-commit，可执行 pre-commit install

# 2. 安装后端 Python 环境（含测试依赖）
cd apps/api
uv sync --extra test

# 3. 启动基础服务与后台队列容器
cd ../..
docker compose up -d       # postgres / redis / minio / mailpit / celery workers / beat
```

---

## Orca 多工作树初始化（Worktree）

仓库根目录的 `orca.yaml` 会在新建工作树时调用 `scripts/orca-worktree-setup.sh`，完成依赖软链接与独立虚拟环境初始化：

- **环境变量与依赖共享**：`.env`（及 `.env.local`）软链接至主工作树；当锁文件与 `package.json` 未变更时，`node_modules`、`apps/web/node_modules` 与 `docs-site/node_modules` 自动建立软链接，否则回退至 `pnpm install --frozen-lockfile`。
- **Python 虚拟环境与生成类型隔离**：`apps/api/.venv` 由 `uv sync --project apps/api --locked --extra test` 独立创建（避免 editable 包路径串回主目录）；`pnpm codegen` 根据当前工作树的 `openapi.snapshot.json` 在本地生成 `apps/web/src/api/generated/`。
- **一键启动隔离开发栈**：主目录已有本机 PostgreSQL / MinIO 后，在工作树内运行 `pnpm dev:worktree` 即可自动创建隔离的数据库、Redis DB、9 个 MinIO 存储桶并启动 API 与 Web（需后台任务时使用 `pnpm dev:worktree -- up --with-worker`）。详见[独立工作树开发环境](../how-to/worktree-environments)。

```bash
# 显式运行仓库 setup 创建工作树
orca worktree create --name my-task --agent codex --setup run --json

# 验证工作树 setup 脚本的软链接与隔离逻辑
python3 scripts/test-orca-worktree-setup.py
```

---

## 日常启动流

```bash
# 1. 确保基础设施容器在线
docker compose up -d

# 2. 启动后端 API（终端 1，支持热重载）
pnpm dev:api               # 等价于 cd apps/api && uv run uvicorn app.main:app --reload --port 8000

# 3. 启动前端开发服（终端 2）
pnpm dev:web               # http://localhost:3000

# 4. 查看 Compose 中的 Celery Worker 与 Beat 状态
docker compose ps celery-worker celery-worker-gpu celery-worker-cpu celery-worker-export celery-worker-image-pyramid celery-worker-gpu-control celery-worker-maintenance celery-beat
```

> **提示**：Celery 不会热重载挂载的 Python 代码。修改 `apps/api/app/workers/` 或相关服务后，请执行 `docker compose restart celery-worker ...` 刷新对应队列的消费者。

API 文档预览入口：

- **实时 Swagger UI**：`http://localhost:8000/docs`
- **静态化文档站 API 参考**：[/api/](/api/)（源自 `apps/api/openapi.snapshot.json`）

---

## 在线学习与训练执行器联调（可选）

当开发或验证「在线学习与模型训练闭环」（[架构文档](../concepts/training-datasets)）时，需要额外启动独立训练执行器 `apps/training-executor`：

### 1. 准备训练执行器

执行器需要与 API 一致的训练目录、独立凭据、节点身份、单张物理 GPU UUID、固定镜像摘要和已校验的基础权重。仅填写 Token 或安装 Python 包不能完成部署。

按 [DEV.md 的受控训练执行器说明](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md#训练执行器配置)准备配置，再按[在线训练执行器部署](/ops/deploy/docker-compose#_8-5-受控训练执行器-training-executor)启动。`TRAINING_API_URL` 指向目标 API 的 `/api/v1/internal/training`，避免将执行器接到另一工作树。

### 2. 准备 COCO8 检测夹具

同一入口同时准备实例分割夹具。

在已经确认的开发数据库和对象存储中运行：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed.py --profile online-training
```

这个显式 profile 创建 8 张已审核图片、30 个检测框和 30 个实例掩膜，保留原始 4 张训练图与 4 张验证图，并同步构建检测与原生 RLE 分割两个初始数据版本。旧检测夹具只有在任务未被人工修改、没有锁或草稿时才能增补分割；已有分割夹具重跑保留人工修改。它不会自动创建训练实验、开启自动策略或发布模型。失败、取消或中断后可重跑同一命令；仍在执行或清理时按提示稍后重试。缓存和离线参数见 [COCO8 夹具说明](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md#coco8-在线训练夹具)。

进入界面后依次创建实验、确认成功执行记录、添加模型、加载试用并人工发布，参见[在线学习用户指南](/user-guide/online-learning/)和[模型回流链路排查](../how-to/debug-model-return)。实例分割的逐项操作和目标 GPU 独立验收见[实例分割在线学习验收](../how-to/validate-segmentation-online-learning)。

---

## 常用开发脚本速查

```bash
# 单元测试与端到端测试
pnpm test                  # 前端 Vitest 单元测试
pnpm test:e2e              # 前端 Playwright E2E 测试
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
