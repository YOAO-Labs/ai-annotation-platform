---
audience: [dev, ops]
type: explanation
since: v0.11.13
status: stable
last_reviewed: 2026-07-23
---

# 运行环境形态：开发态 / staging / 生产态

> 本页解释**同一套代码在不同环境下的运行差异**——谁跑在容器里、哪些 profile 启用、`ENVIRONMENT` 变量如何改变后端行为。
>
> 它回答的是「dev 和生产有什么不一样」，而不是「机器怎么摆」——后者见[部署拓扑](./deployment-topology)（单机 / 分离 GPU / 多 backend 的物理形态）。

## 一句话区分

`docker-compose.yml` 只定义**基础设施 + Celery**（api/web 不在内）。开发态与生产态的差异由三件事决定：

1. **谁进容器** —— 开发态 API/Web 跑宿主机进程；生产态用叠加文件 `docker-compose.prod.yml` 把 api/web 以容器拉起
2. **哪些 profile 启用** —— `gpu`、`gpu-sam3`、`gpu-yolo`、`gpu-onnxtools`、`gpu-rapidocr` 与 `monitoring` 默认不启动
3. **`ENVIRONMENT` 变量取值** —— 驱动 `config.py` / `main.py` 的启动断言和环境安全策略

```bash
# 开发：只起基础设施，api/web 在宿主机跑
docker compose up -d

# 生产：叠加 prod 文件，api/web 进容器、worker 改用生产配置
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

> `docker-compose.prod.yml` 是**显式命名的叠加文件**，必须 `-f` 带上才生效——刻意没用会被自动 merge 的 `docker-compose.override.yml`，否则 dev 的 `docker compose up` 会把 api/web 容器一起拉起，与"开发态跑宿主机"相悖。
>
> `--env-file .env.production` 不可省：基础文件给 celery worker/beat 硬编码了 dev 凭据（inline `environment` 优先级高于 `env_file`，叠加文件光加 `env_file` 盖不掉），叠加文件用 `${VAR}` 插值覆盖这些 key，插值源由 `--env-file` 指定。

## 三态对照

| 维度                       | development（默认）                                                           | staging                                             | production                                                 |
| -------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------- |
| API / Web                  | 宿主机进程（`uvicorn --reload` + `pnpm dev:web`）；不在任何 compose 文件      | 同生产：`docker-compose.prod.yml` 容器 + nginx 反代 | `docker-compose.prod.yml` 容器 + 外层 nginx/Caddy 终结 TLS |
| 基础设施（PG/Redis/MinIO） | compose 拉起，本地默认值                                                      | 独立实例，贴近生产                                  | 托管 RDS、Redis 挂 AOF、MinIO 换 S3/OSS                    |
| Celery worker/beat         | 容器内 + 源码热挂载（`./apps/api:/app`），改码 `docker restart` 即可          | 镜像冻结源码                                        | 镜像冻结源码                                               |
| GPU ML Backend             | `gpu` / `gpu-sam3` / `gpu-yolo` / `gpu-onnxtools` / `gpu-rapidocr` 默认不启动 | 按需                                                | 按需，调显存预算                                           |
| 监控（Prometheus/Grafana） | `profile: monitoring` 默认不启动                                              | 按需                                                | 按需                                                       |
| 邮件                       | mailpit 假收件箱（`:8025`），不外发                                           | 真实 SMTP                                           | 真实 SMTP                                                  |

## `ENVIRONMENT` 的代码行为差异

合法取值定义在 [`apps/api/app/config.py`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/apps/api/app/config.py)：

```python
environment: Literal["development", "staging", "production"] = "development"
```

**关键认知：staging 仍在多数启动校验上按非 production 处理，不等同于真生产。**
但测试 seed 路由不再随非 production 环境自动开放：它有独立的显式开关和
数据库名守卫。

| 行为                                            | development                                               | staging        | production | 出处                          |
| ----------------------------------------------- | --------------------------------------------------------- | -------------- | ---------- | ----------------------------- |
| `SECRET_KEY` 仍为默认值 → 启动 RuntimeError     | 跳过                                                      | 跳过           | 强制       | `main.py:57`                  |
| `CORS_ALLOW_ORIGINS` 为空 → 启动断言失败        | 跳过                                                      | 跳过           | 强制       | `main.py:93`                  |
| `CORS_ALLOW_ORIGIN_REGEX`（放行 localhost）生效 | ✅                                                        | ✅             | 自动忽略   | `main.py:101`                 |
| 测试路由 `/api/v1/__test/seed/*`                | 仅 `E2E_SEED_ENABLED=true` 且库名以 `_e2e` / `_test` 结尾 | 同 development | 永不挂载   | `router.py` / `_test_seed.py` |
| `scripts/seed.py` 允许灌数据                    | ✅                                                        | ✅             | 拒绝       | `scripts/seed.py`             |
| `SENTRY_DSN` 为空 → 启动 WARN                   | 否                                                        | 否             | 是         | `main.py:66`                  |

::: warning staging ≠ 真生产
如果你想用 staging 做「贴近生产」的上线前验收，注意宽松 CORS 和部分启动校验仍与
真生产不一致。`E2E_SEED_ENABLED` 应保持默认关闭；它只应在指向专用
`*_e2e` / `*_test` 数据库的测试进程中临时开启。需要逐项验真生产行为时，
使用 `ENVIRONMENT=production` 的独立验收环境。
:::

## 镜像构建差异（为什么 dev 能热挂载）

- `infra/docker/Dockerfile.api`：按 `uv.lock` 安装依赖到 `/opt/venv`（不在 `/app` 下），容器 `PATH` 默认使用这个环境。所以开发态把 `./apps/api` 挂到 `/app` 不会覆盖依赖；匿名卷 `/app/.venv` 屏蔽宿主机 venv。Celery 无 `--reload`，改业务码后仍需 `docker restart`。
- `infra/docker/Dockerfile.web`：多阶段构建，`pnpm build` 产物交给 nginx 托管。前端 API base 硬编码同源相对路径 `/api/v1`（`apps/web/src/api/client.ts`），dev 由 vite proxy、生产由容器内 `nginx.conf` 反代 `/api/` `/ws/` 到 `api:8000`，**无需 build arg / API 地址变量**。

## 多工作树的本机隔离开发

`pnpm dev:worktree` 在现有开发形态上增加工作树级资源归属，不复用主目录的业务数据库
或后台任务消费者。PostgreSQL/MinIO 进程可以共享，但每个工作树、每种模式拥有独立的
database、八个 bucket、Redis 容器和本地数据目录；API/Web 与可选 worker 都运行当前 checkout。

运行时身份绑定工作树路径，配置通过子进程环境覆盖，不修改共享 `.env`。启动前检查迁移图
和数据库 revision；停止保留数据，重建需要精确确认和资源归属检查。不同 Redis DB 编号
不能隔离 Pub/Sub，因此这里使用独立 Redis 容器，并把 AOF 保存在工作树目录。

此模式不启动 GPU 控制面、beat 或生产服务，也不提供数据库账号级安全沙箱。首次环境为空，
不会复制其他分支的账号、项目和媒体。使用方法、测试模式和销毁边界见
[独立工作树开发环境](../how-to/worktree-environments)。

## 相关

- [部署拓扑](./deployment-topology) —— 物理形态（单机 / 分离 GPU / 多 backend）
- [后端基础设施（容器）](./backend-infrastructure) —— 各容器职责与依赖
- [部署指南](/ops/deploy/docker-compose) —— 逐项环境变量与运维细则
