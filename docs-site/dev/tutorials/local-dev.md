---
audience: [dev]
type: tutorial
since: v0.1.0
status: stable
last_reviewed: 2026-07-11
---

# 本地开发

## 前置依赖

- Node.js >= 22
- pnpm >= 10
- Python >= 3.11
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose
- 可选：[pre-commit](https://pre-commit.com/)（推荐装上）

仓库提供 `.nvmrc`（Node 22）；`.npmrc` 开启了 `engine-strict`，低于基线的
运行时会在 `pnpm install` 阶段直接失败（Node 20 已于 2026-04-30 EOL）。

## 一次性 setup

```bash
# 仓库根
pnpm install
# pnpm install 会运行 prepare；若提示缺 pre-commit，再安装后执行 pre-commit install

# 后端
cd apps/api
uv sync --extra test       # 安装 + dev 依赖

# 起基础设施
cd ../..
docker compose up -d       # postgres / redis / minio / mailpit / workers / beat
```

## Orca 工作树初始化

仓库根目录的 `orca.yaml` 调用 `scripts/orca-worktree-setup.sh`，并等待 setup
成功后启动 Agent。配置文件和脚本需要包含在新工作树使用的基准分支中。
也可以在 Orca 的 **Settings → Repository → Hooks** 中，将本机 Setup 命令设为
`bash "$ORCA_ROOT_PATH/scripts/orca-worktree-setup.sh"`，让尚未包含配置的分支复用主目录脚本。
使用本机 Setup 命令时，同时将 Agent 启动策略设为等待 setup 完成。

- `.env` 软链接到主目录；主目录缺少该文件时创建只含说明的空配置，保留应用默认值。
  主目录已有 `.env.local` 时也会链接。修改共享文件会影响所有链接它的工作树。
- pnpm 锁文件、工作区清单和三个 `package.json` 一致，主目录依赖存在，
  且已安装的 `node_modules/.pnpm/lock.yaml` 与主目录锁文件一致时，
  `node_modules`、`apps/web/node_modules`、`docs-site/node_modules` 使用软链接。
  不满足条件时在工作树内运行 `pnpm install --frozen-lockfile`。
- `apps/api/.venv` 由 `uv sync --project apps/api --locked --extra test` 单独建立：
  Python editable 包包含绝对源码路径，共享虚拟环境会误用主目录代码。
- `pnpm codegen` 根据当前工作树的快照生成本地 API 类型，生成目录不共享。

已有文件和目录不会被覆盖。工作树修改依赖后，先移除上述三个 `node_modules`
软链接，再执行 `pnpm install --frozen-lockfile`；不要通过共享链接安装或更新依赖。
无需另外配置 Orca 的 Worktree Shared Paths 或 `.worktreeinclude`。

setup 只准备依赖和生成文件，不自动运行 Docker、数据库迁移或开发服务器。
主目录已有本机 PostgreSQL/MinIO 后，在每个工作树运行 `pnpm dev:worktree`：
启动器会创建独立数据库、Redis、bucket 和本地状态，再迁移并启动 API/Web。
后台任务使用 `pnpm dev:worktree -- up --with-worker`，不复用主目录 worker。
测试、诊断和重建流程见[独立工作树开发环境](../how-to/worktree-environments)。

```bash
# 显式运行仓库 setup，创建后再启动 Agent
orca worktree create --name my-task --agent codex --setup run --json

# 验证 hook 的软链接、幂等性和依赖隔离逻辑
python3 scripts/test-orca-worktree-setup.py
```

参考：[Orca Hooks](https://www.onorca.dev/docs/agents/hooks-memory)、
[工作树共享路径](https://www.onorca.dev/docs/model/worktrees)。

## 日常启动

```bash
# 1. 基础设施（如未运行）
docker compose up -d

# 2. 后端（终端 1）
pnpm dev:api               # 等价于 cd apps/api && uvicorn app.main:app --reload --port 8000

# 3. 前端（终端 2）
pnpm dev:web               # http://localhost:3000

# 4. worker 已由 compose 拉起；按需查看队列消费者
docker compose ps celery-worker celery-worker-gpu celery-worker-cpu celery-worker-export celery-beat
```

API 文档：

- 实时 Swagger UI：http://localhost:8000/docs
- 静态化文档：[/api/](/api/)（来自 openapi.snapshot.json）

## 常用脚本

```bash
# 测试
pnpm test                  # 前端 vitest
pnpm test:e2e              # 前端 Playwright
cd apps/api && uv run pytest

# 代码生成
pnpm codegen               # 从 snapshot 生成 TS 类型
pnpm openapi:export        # 重新生成 snapshot（改了 API 后必须）
pnpm openapi:check         # 校验 snapshot 与运行时一致

# Lint / Typecheck
pnpm format:check
pnpm lint
pnpm typecheck

# 自动修复全仓格式
pnpm format

# 文档
pnpm docs:dev              # VitePress 本地预览 :5173
pnpm docs:build
```

## 数据库迁移

```bash
cd apps/api
uv run alembic upgrade head                          # 升到最新
uv run alembic revision --autogenerate -m "..."      # 生成新迁移
uv run alembic downgrade -1                          # 回滚 1 步
```

详见 [How-to / Alembic 迁移](../how-to/add-migration)。
