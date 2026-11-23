---
audience: [dev, ops]
type: how-to
status: stable
last_reviewed: 2026-10-20
---

# 开发部署（本地）

> 适用读者：第一次在本机把平台跑起来做开发 / 联调的人。
>
> 开发态的核心特征：**基础设施进容器，API / Web 跑宿主机进程，源码热更新**。它和生产部署在「方式」与「严谨性」上刻意不同——生产见[生产部署](/ops/deploy/docker-compose)，两态差异的原理见[运行环境形态](/dev/concepts/runtime-environments)。

---

## 1. 一句话形态

默认 `docker-compose.yml` 起**基础设施 + Celery**（PG／Redis／MinIO／Mailpit、八类 worker 和单实例 Beat）。开发的 API 与 Web 留在宿主机，源码改动可热重载：

- API：`uvicorn --reload`，改 Python 业务码自动重载
- Web：`vite` dev server，HMR 热替换
- Celery：容器内但挂载了 `./apps/api:/app` 源码，改 worker 业务码后 `docker restart` 即可（Celery 无 `--reload`）

```
┌─ 宿主机进程 ──────────────┐   ┌─ docker compose 容器 ─────────┐
│ uvicorn  :8000 (--reload) │──▶│ postgres :5432                │
│ vite     :3000 (HMR)      │   │ redis    :6379                │
│   └ /api,/ws → 127.0.0.1:8000 │ minio    :9000 / :9001        │
└───────────────────────────┘   │ mailpit  :1025 / :8025        │
                                 │ 8 workers + celery-beat      │
                                 └───────────────────────────────┘
```

> 开发态默认端口全绑在本机，**不要**把它们对公网开——端口暴露的风险见[端口暴露与网络安全](/ops/deploy/network-security)。

---

## 2. 启动步骤

<span id="development-cold-start" aria-hidden="true"></span>

### 2.1 首次启动基础设施

只在新的本地环境执行本节。已有拆分栈和维护者日常环境先看[升级门禁](/ops/upgrade-guide#split-topology-upgrade)；并行 checkout 使用 `pnpm dev:worktree`，不要另起会占用同一端口的默认项目。

从 `.env.example` 创建自己的 `.env`，配置本机数据库和存储。首先只起基础设施，避免 worker 在未授权的数据库上启动：

```bash
docker compose up -d --wait postgres redis minio mailpit
```

Mailpit 只收不发，在 `http://localhost:8025` 查看。`identity-mail-v2` 用 `mailpit:1025` 作为开发中继；宿主机仅执行管理员 SMTP 测试时才用 `localhost:1025`。生产不启动 Mailpit。监控 `monitoring` 和五个模型插件 `gpu*` profile 默认不启用。

若与其他部署共用 MinIO，开发 API 和 Celery 使用仅授权开发桶的 `MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`。已有 MinIO 服务沿用自己的 root 凭据和数据目录；不要用应用凭据重建共享服务。

有历史数据时，对照已登记的存储端点配置宿主 `MINIO_ENDPOINT`、容器 `MINIO_ENDPOINT_DOCKER` 和 Vite 的 `MINIO_PROXY_TARGET`。宿主和 worker 必须解析到原服务，浏览器 `/minio` 代理使用与签名匹配的 Host。不要改写原件地址或重置桶。给开发导出桶单独授权 `s3:GetBucketVersioning`：即使桶未开启版本控制，worker 也需要读取版本状态以核验写入回执；不需要开启版本控制或授权修改其设置。

### 2.2 迁移、授权及启动 worker

准备普通应用 LOGIN 与独立迁移 LOGIN。宿主 API 的 `DATABASE_URL` 连回环数据库；`.env` 的 `DATABASE_URL_DOCKER` 用容器地址 `postgres:5432`。二者使用同一个普通角色，迁移连接用 `MIGRATION_DATABASE_URL`。先应用当前迁移：

```bash
(cd apps/api && uv sync --locked --extra test && uv run alembic upgrade head)
```

按[受限 Jobs 账号初始化](./docker-compose#jobs-recovery-provisioning)安装普通运行授权及 claim／publication LOGIN。用 `docker compose exec postgres sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` 进入内置数据库交互终端设置密码。配置容器可达的 `JOBS_DISPATCH_DATABASE_URL_FILE`、`JOBS_PUBLICATION_DATABASE_URL_FILE`，并按[媒体执行角色](./docker-compose#media-executor-provisioning)准备 `ASSETS_CELERY_VIDEO_DATABASE_URL_FILE`、`ASSETS_CELERY_IMAGE_DATABASE_URL_FILE`。下节另为宿主 API 提供宿主可达的 publication DSN。授权后再启动：

```bash
docker compose up -d --wait
docker compose ps celery-worker-jobs-recovery celery-beat
```

`celery-worker-jobs-recovery` 只消费 `jobs.recovery`。缺少或混用受限凭据时不能启动该 worker；Beat 等待恢复 worker 就绪。其余服务不挂载 claim 文件。

### 2.3 起 API（宿主机，热更新）

宿主进程不一定能解析 Compose 的 `postgres`。把同一 publication LOGIN 的宿主可达 DSN 存入独立私有文件（`HOST_JOBS_PUBLICATION_FILE`）；只覆盖 API 子进程环境，不修改容器的连接：

```bash
JOBS_PUBLICATION_DATABASE_URL_FILE="$HOST_JOBS_PUBLICATION_FILE" pnpm dev:api
```

`pnpm dev:api` 先显式运行 Alembic，再启动当前 checkout 的 Uvicorn。

`--reload` 监听 `apps/api/**` 改动自动重启。`ENVIRONMENT` 默认 `development`，使用本地
CORS 策略（见[运行环境形态](/dev/concepts/runtime-environments)）。测试 seed 路由默认不挂载；
仅 Playwright 专用进程会对隔离测试库显式设置 `E2E_SEED_ENABLED=true`。

安装或更新 editable 共享包后，必须重启 Uvicorn 父进程；已有父进程会把旧
`sys.path` 交给热重载子进程。若新解释器能导入包、常驻 API 却报缺包，先核对服务
工作目录和虚拟环境，再重启拥有该 API 的服务，而非反复触碰源码。macOS 主 checkout 若由
`local.ai-annotation-platform.api` LaunchAgent 管理，先确认其 `WorkingDirectory` 和
`ProgramArguments` 指向该 checkout，再执行
`launchctl kickstart -k "gui/$(id -u)/local.ai-annotation-platform.api"`。独立工作树仍用自己的 launcher。
重启后检查 `/health/db`、`/health/redis` 和 `/health/minio`，再确认一次源码热重载。

### 2.4 起 Web（宿主机，HMR）

```bash
pnpm install
pnpm dev:web
```

Vite 跑在 `:3000`，把 `/api` `/ws` 反代到 `127.0.0.1:8000`（`apps/web/vite.config.ts`）。前端 API base 是硬编码同源相对路径 `/api/v1`，**不读 `VITE_API_URL`**，所以无需配后端地址。

本地若未在系统设置中覆盖 `frontend_base_url`，还要把 `FRONTEND_BASE_URL` 设为 `http://localhost:3000`，否则邀请、验证和重置密码链接会指向配置中的旧默认地址。

> 多 worktree 并行开发优先使用 `pnpm dev:worktree`，隔离数据库、Redis、bucket 与服务端口；需要后台任务时加 `--with-worker`。仅覆盖 `API_PROXY_TARGET` 不会隔离数据库和队列。详见[独立工作树开发环境](/dev/how-to/worktree-environments)。

### 2.5 首个 super_admin

平台没有「第一个用户自动当管理员」的逻辑，第一次部署后手动跑一次：

```bash
cd apps/api
ADMIN_EMAIL=dev@example.com \
ADMIN_PASSWORD='set-a-strong-one' \
ADMIN_NAME='本地管理员' \
uv run python -m scripts.bootstrap_admin
```

之后用浏览器打开 `http://localhost:3000` 登录。

---

## 3. 改了东西要不要重启？

完整规则见 CLAUDE.md §8 与[升级指南](/ops/upgrade-guide)，开发态高频场景速查：

| 改动                                 | 操作                                                                        |
| ------------------------------------ | --------------------------------------------------------------------------- |
| `apps/api/app/**` 业务码（API 进程） | 自动 reload，无需手动                                                       |
| `apps/api/app/workers/**` worker 码  | `docker restart ai-annotation-platform-celery-worker-1`（Celery 无 reload） |
| 新增 alembic 迁移                    | `uv run alembic upgrade head`（worker 改码同样 restart）                    |
| `apps/web/src/**`                    | HMR 自动                                                                    |
| `.env` 运行期变量                    | 重启对应进程 / `docker compose up -d` 重建容器                              |
| `pyproject.toml` / `uv.lock`         | 按锁文件同步环境并重启 API 父进程；worker 容器需重新构建                    |
| `package.json` / `pnpm-lock.yaml`    | `pnpm install`                                                              |

---

### 本机开发服务定时重启 {#development-restart}

只为已完成[本地冷启动](#development-cold-start)的主 checkout 安装。独立工作树仍用 `pnpm dev:worktree`，旧拆分环境和生产不接入该定时器。

1. 用 `infra/systemd/aap-development-{api,web}.service.in` 安装用户服务，替换 `@PROJECT_DIR@` 和 `@NODE_BIN_DIR@` 为实际绝对路径。接管已有进程前，核对 PID、会话和工作目录。
2. 将 `scripts/development-restart.py` 安装到 `~/.local/lib/ai-annotation-platform/`。让 `~/.local/bin/ai-annotation-platform-dev-restart` 调用该脚本，传入 `--project-dir` 主 checkout 绝对路径和 `--state-dir ~/.local/state/ai-annotation-platform/development-restart`，并透传参数。
3. 将 `infra/systemd/aap-development-restart.{service,timer}` 安装到 `~/.config/systemd/user/`。启用用户 linger 后执行：

   ```bash
   systemctl --user daemon-reload
   ~/.local/bin/ai-annotation-platform-dev-restart --check
   systemctl --user start aap-development-restart.service
   systemctl --user enable --now aap-development-restart.timer
   systemctl --user list-timers aap-development-restart.timer
   journalctl --user -u aap-development-restart.service -n 30 --no-pager
   ```

定时器每天北京时间 04:00 重启开发 API、Web、Celery worker 和 Beat。脚本先验证容器归属和配置。它不创建容器、不重建、不迁移，也不重启共享基础设施。

部署前，在状态目录创建 `maintenance` 文件，再运行 `flock ~/.local/state/ai-annotation-platform/development-restart/restart.lock true` 等待已开始的重启退出。部署验证后删除标记。彻底停用用 `systemctl --user disable --now aap-development-restart.timer`。

## 4. dev 环境用 SDK / CLI

本机 SDK 直连 host 上的 API 即可，明文 HTTP 在本地无所谓：

```bash
yoao login --url http://localhost:8000 --api-key ak_...
# 或环境变量
export YOAO_BASE_URL=http://localhost:8000
export YOAO_API_KEY=ak_...
```

API Key 在 web 端「我的 API Keys」或 `yoao` 登录后生成，写入 `~/.config/yoao/config.toml`（`0600`）。

> ⚠️ **不在本机、要从另一台机器连**就完全是另一回事了——必须走 HTTPS、确认 presigned URL 对外可达。详见[端口暴露与网络安全](/ops/deploy/network-security)。

---

## 5. 关停

```bash
docker compose down          # 停容器，保留数据卷
docker compose down -v       # 连数据卷一起删（清空 PG / MinIO，慎用）
```

宿主机的 `pnpm dev:api` / `pnpm dev:web` 直接 Ctrl-C。

## 可选邮件 sender 和 Identity issuer

API 默认内嵌 Identity 签发与查询，Notifications WebSocket 留在 API；不为此启动独立容器。若要用 Mailpit 测试真实恢复／验证／邀请，按[邮件配置](./docker-compose#production-mail)在当前隔离库准备 MailV2／Settings 登录和 Fernet key。将 `IDENTITY_MAIL_SMTP_HOST=mailpit`、`IDENTITY_MAIL_SMTP_PORT=1025`、`IDENTITY_MAIL_SMTP_FROM` 和专用 secret 文件路径写在独立的 `IDENTITY_OWNER_ENV`；嵌入 producer 使用相同 key 的 API 专用文件。

```bash
docker compose --env-file .env --env-file "$IDENTITY_OWNER_ENV" \
  -f docker-compose.yml -f docker-compose.identity.yml up -d --build --wait identity-mail-v2
```

测试私有 signer 时，另配置独立登录 Redis、函数 LOGIN 与 `IDENTITY_RUNTIME_SIGNING_SECRET_FILE`，用同一文件链加 `--profile identity-issuer`，显式启动 `identity-issuer identity-mail-v2`。宿主 API 用 `IDENTITY_URL=http://127.0.0.1:8170` 和私有 `IDENTITY_TOKEN`，不带远程 signer、producer key 或 SMTP／mail LOGIN。若只验嵌入签发，不开启 issuer。节点健康不替代真实邮件投递或授权拒绝测试。

---

## 相关

- [运行环境形态](/dev/concepts/runtime-environments) —— 开发 / staging / 生产三态差异原理
- [生产部署](/ops/deploy/docker-compose) —— 容器化 + 反代 + TLS 的生产形态
- [端口暴露与网络安全](/ops/deploy/network-security) —— 端口该不该对外、SDK 远程访问安全
