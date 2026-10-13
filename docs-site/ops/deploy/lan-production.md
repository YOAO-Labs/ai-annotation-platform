---
audience: [ops]
type: how-to
status: stable
last_reviewed: 2026-09-09
---

# 局域网生产部署：复用基础设施，隔离业务数据

`docker-compose.lan-prod.yml` 在已有开发环境旁运行独立生产应用。它复用 PostgreSQL、MinIO 和模型服务，生产账号、项目、数据集、标注、消息队列与分析文件独立。适合先在一台现有机器上向局域网团队提供正式入口。

这与[整栈生产部署](./docker-compose)是两种入口：**本文件单独使用，不叠加 `docker-compose.yml` 或 `docker-compose.prod.yml`**。项目名默认为 `aap-production`，不会管理已有 PostgreSQL、MinIO 或模型容器，也不会修改它们已发布的端口。

## 服务边界

| 服务或数据 | 生产安排                                                                  |
| ---------- | ------------------------------------------------------------------------- |
| Web        | Caddy 的 HTTPS 3030 → Nginx 静态站点；同源代理 `/api/`、`/ws/`、`/minio/` |
| API        | Caddy 的 HTTPS 8080 → Uvicorn 容器 8000；不发布原生 HTTP 端口             |
| PostgreSQL | 复用实例，新库 `annotation_production`；不复制旧业务表内容                |
| MinIO      | 复用实例，八个独立 `prod-` 桶与受限访问凭据                               |
| Redis      | 独立 `redis-production` 容器与 AOF 卷；无宿主端口                         |
| 异步任务   | 七类 Worker 与一个 Beat，全部连接生产库和生产 Redis                       |
| 模型       | 复用运行中的实例及权重，在新库重新注册和探测能力                          |
| DuckDB     | 生产独立卷，通用 Worker 写，API 只读                                      |

Redis 使用新实例，因为目前全局任务和模型状态推送使用固定频道。Redis Pub/Sub **不按逻辑数据库编号隔离**，仅把 `/0` 改成 `/1` 会让新旧环境仍然收到同一频道消息。[Redis 官方说明](https://redis.io/docs/latest/develop/pubsub/)

生产私有网络连接应用、入口和 Redis；API、Web、Worker 按需接入 `AAP_SHARED_NETWORK` 指定的现有网络，通过 `postgres`、`minio` 等服务名访问共享基础设施。`redis-production` 的名称避免与旧网络的 `redis` 别名冲突。

共享意味着两边仍受同一台机器、数据库实例、存储和模型服务的负载及故障影响。GPU Worker 初始并发为 1，GPU 仲裁保持 `observe` 且 rollout 关闭。不得在这套独立数据库和 Redis 上直接开启针对同一批模型的第二个 enforce 控制面。

## 准备生产配置

从当前 `.env` 按用途派生被 Git 忽略的 `.env.production`，文件权限设为 `0600`。只复制应用实际需要的模型地址、能力和资源配置；不要把模型下载令牌、旧数据库 collector 凭据或旧业务设置整体复制过去。

| 变量                                               | 设置                                                                      |
| -------------------------------------------------- | ------------------------------------------------------------------------- |
| `AAP_IMAGE_TAG`                                    | 本次构建的源代码标识；保留上一镜像标识用于回退                            |
| `LAN_BIND_IP`                                      | 服务器固定局域网 IPv4 地址，例如 `192.168.1.20`；不填 `0.0.0.0`           |
| `AAP_SHARED_NETWORK`                               | `docker network ls` 中已有基础设施网络名称                                |
| `AAP_PRODUCTION_STATE_DIR`                         | 宿主绝对路径，权限 `0700`，存放迁移凭据及本机部署资料                     |
| `ENVIRONMENT` / `DEBUG`                            | `production` / `false`                                                    |
| `E2E_SEED_ENABLED` / `ALEMBIC_AUTO_UPGRADE`        | 均为 `false`                                                              |
| `DATABASE_URL`                                     | 新运行角色的 `postgresql+asyncpg://…@postgres:5432/annotation_production` |
| `REDIS_URL`                                        | `redis://redis-production:6379/0`；不沿用旧 `CELERY_BROKER_URL`           |
| `MINIO_ENDPOINT` / `MINIO_USE_SSL`                 | `minio:9000` / `false`，这里是内部连接协议                                |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`            | 只允许生产桶的新凭据                                                      |
| `MINIO_PUBLIC_URL`                                 | `/minio`，浏览器经 HTTPS 同源访问                                         |
| `ML_BACKEND_STORAGE_HOST`                          | 模型可达的 `minio:9000`，避免模型取文件时依赖客户端 CA                    |
| `GPU_ARBITER_MODE` / `GPU_ARBITER_ROLLOUT_ENABLED` | `observe` / `false`                                                       |
| `DUCKDB_PATH`                                      | `/var/lib/duckdb/analytics.duckdb`                                        |
| `FRONTEND_BASE_URL`                                | `https://192.168.1.20:3030`，替换为实际 IP                                |
| `CORS_ALLOW_ORIGINS`                               | 上述前端 origin 的 JSON 数组                                              |
| `SECRET_KEY`                                       | 新生成的独立随机密钥                                                      |
| `ALLOW_OPEN_REGISTRATION`                          | `false`，先由管理员创建账号                                               |

应用 `.env.production` 中**不放 `MIGRATION_DATABASE_URL`**。将完整迁移连接字符串作为一行写入 `$AAP_PRODUCTION_STATE_DIR/migration-database-url`，权限设为 `0400`；仅一次性 `migrate` 服务获得该文件。常驻服务不自动迁移，也不接收迁移 owner 的登录凭据。

在同一目录创建 `known_hosts` 文件，并只读挂载给 API 和消费 `media` 的通用 Worker。未配置 SFTP 时可以为空；接入 SFTP 前必须通过可信渠道核验目标主机公钥并写入该文件，不能自动信任扫描结果。新生产环境使用独立 `CONNECTOR_ENCRYPTION_KEY` 保存连接器凭据；该值与数据库一起备份，不能随重建轮换。

八个桶变量固定为：

| 变量                         | 桶                   |
| ---------------------------- | -------------------- |
| `MINIO_BUCKET`               | `prod-annotations`   |
| `MINIO_DATASETS_BUCKET`      | `prod-datasets`      |
| `MINIO_BUG_REPORTS_BUCKET`   | `prod-bug-reports`   |
| `MINIO_MEDIA_CACHE_BUCKET`   | `prod-media-cache`   |
| `MINIO_AUDIT_ARCHIVE_BUCKET` | `prod-audit-archive` |
| `MINIO_IMPORT_BUCKET`        | `prod-import`        |
| `MINIO_EXPORT_BUCKET`        | `prod-export`        |
| `MINIO_AVATARS_BUCKET`       | `prod-avatars`       |

边缘代理使用上述八个桶的明确路径列表。改变桶名时同时更新变量、MinIO 权限策略及 `infra/docker/Caddyfile.lan-prod`，不能只改其中一处。

## 初始化独立数据库和存储

先备份并记录现有服务、数据卷和网络状态。下面的角色和数据库必须是新资源；发现同名资源时先确认归属，不覆盖现有账号或清空数据库。

由 PostgreSQL 实例管理员创建：

```sql
CREATE ROLE anno_prod_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE anno_prod_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT anno_prod_app TO anno_prod_owner WITH INHERIT TRUE, SET TRUE;
CREATE DATABASE annotation_production OWNER anno_prod_owner;
REVOKE ALL ON DATABASE annotation_production FROM PUBLIC;
GRANT CONNECT ON DATABASE annotation_production TO anno_prod_app;
```

通过交互式 `psql` 的 `\password anno_prod_owner` 和 `\password anno_prod_app` 分别设置新密码，并写入对应的本机配置文件，避免把密码放进命令历史。迁移需要安装 `pgcrypto` 和 `btree_gist` 扩展；当前 PostgreSQL 镜像支持由新库 owner 安装这些受信任扩展。

由 MinIO 管理员预建八个空桶及一个新的应用账号，附加仓库中的 `infra/docker/lan-production-minio-policy.json`。该策略只授予这些桶的对象操作及当前应用实际使用的生命周期设置权限，不授予旧桶访问或 MinIO 管理权限。

API 启动会设置五个桶的生命周期：评论附件前缀 90 天、反馈附件 180 天、媒体缓存的 `videos/` 前缀 30 天、导入和导出桶 7 天。数据集、审计归档与头像桶没有自动过期（头像不可重生，过期会造成破图）。必须确认这些配置全部指向新桶。

## 构建、迁移和启动

从仓库根目录执行，所有命令显式选择独立 Compose 文件：

```bash
docker compose --env-file .env.production -f docker-compose.lan-prod.yml config --quiet
docker compose --env-file .env.production -f docker-compose.lan-prod.yml build api web
docker compose --env-file .env.production -f docker-compose.lan-prod.yml up -d redis-production
docker compose --env-file .env.production -f docker-compose.lan-prod.yml run --rm migrate
```

随后以 `anno_prod_owner` 连接 **`annotation_production`**，执行 `infra/docker/lan-production-grants.sql`，向 `psql` 传入：

```text
-v expected_database=annotation_production
-v application_role=anno_prod_app
-v migration_role=anno_prod_owner
```

每次迁移后都重新执行该授权脚本。它检查当前数据库和角色方向，授予业务 DML，禁止运行角色修改迁移版本或删除 GPU membership/fence 真值；同时把审计、预测父表及全部子分区、两个统计物化视图交给运行角色维护。

`celery-worker-maintenance` 专门消费 `maintenance` 队列，沿用下面限定维护对象的运行角色权限，不挂载迁移账号 secret。

**运行角色并非纯 DML 账号**：现有月分区任务需要 CREATE/DROP 子分区，PostgreSQL 的统计刷新需要视图所有权和数据库 TEMPORARY 权限。因此它拥有上述限定维护对象及 `public` schema CREATE 权限；其余业务对象仍由迁移角色所有。迁移角色单向继承运行角色，运行角色不能反过来成为迁移角色，也没有集群管理权限。

启动常驻服务：

```bash
docker compose --env-file .env.production -f docker-compose.lan-prod.yml up -d
docker compose --env-file .env.production -f docker-compose.lan-prod.yml ps
```

新库从零创建账号。首个管理员通过现有 `python -m scripts.bootstrap_admin` 初始化，交互指定 `ADMIN_EMAIL`、`ADMIN_NAME` 与随机初始密码；不要运行开发 seed。参数传递方式见[首个管理员](./docker-compose#_4-3-首个-super-admin-bootstrap-admin)。密码不写入提交或文档，初始化结束后删除临时环境文件。

## 首次信任 HTTPS 证书

Caddy 使用内部 CA 为 `LAN_BIND_IP` 签发证书，3030 与 8080 都使用 HTTPS。CA 放在 `caddy_data` 持久卷中；容器重建及普通站点证书续签不会更换它。首次访问前，从入口容器导出**公共根证书**：

```bash
docker compose --env-file .env.production -f docker-compose.lan-prod.yml cp \
  gateway:/data/caddy/pki/authorities/local/root.crt ./aap-root.crt
openssl x509 -in aap-root.crt -noout -subject -fingerprint -sha256
```

管理员通过可信渠道分发该公共证书并核对指纹，绝不分发 CA 私钥。用户需要按设备首次建立信任；受管理的公司电脑可以由 IT 统一下发。

- **Windows / Chrome / Edge**：双击证书，选择“安装证书”→“本地计算机”→“将所有的证书都放入下列存储”→“受信任的根证书颁发机构”，按提示授权并重启浏览器。
- **macOS**：在“钥匙串访问”中导入证书，打开证书“信任”，选择“始终信任”，按提示授权。
- **Ubuntu**：把 PEM 格式的 `.crt` 公共证书复制到 `/usr/local/share/ca-certificates/`，运行 `sudo update-ca-certificates`。部分浏览器使用独立信任库，需要另行导入。

未导入时会出现证书警告。简单点击“继续访问”不能替代可信 HTTPS 验收。IP + HTTP 无法保证 WebGPU、WebCodecs 或依赖安全上下文的编辑能力；客户端信任后仍需浏览器和硬件支持相应 API。

不希望客户端安装证书时，应改用域名和浏览器已信任的证书；服务仍可只在局域网开放。[Caddy 官方说明](https://caddyserver.com/docs/automatic-https)

## 验收和日常维护

从实际客户端验证：证书无警告、首页和深链接可用、登录正常、项目与数据集隔离、媒体上传下载、视频 Range、标注保存、预标注、导出、WebSocket 重连。检查每类 Worker 的实际队列和任务返回值；统计任务返回 `refreshed=false` 即使 Celery 显示 SUCCESS 也不算通过。

浏览器同源 API 使用 `https://服务器IP:3030/api/v1`，直接 API 使用 `https://服务器IP:8080/api/v1`。`/metrics` 和 `/api/v1/internal` 在两个入口均被拦截；原始服务只在 Docker 网络内访问。上传代理上限为 8 GiB，API 仍执行各业务接口更细的限制。

写入型自动验收必须指向专用的临时数据库和存储实例或测试桶，完成后清理本次创建的数据与容器。不要在旧开发库或正式生产库开启 E2E seed。

升级时更新 `AAP_IMAGE_TAG`，构建镜像、备份生产数据、执行新库迁移和授权，再重建应用服务。API 与 Worker 均从镜像运行，不挂载宿主源码；Celery 不会自动加载宿主文件改动。入口 Nginx 会重新解析 Docker 服务地址，API 容器替换后不需要依赖旧 IP。

Web 构建会应用 `patches/` 中的 Sonner 补丁，让通知组件在插入动态样式前读取页面 CSP nonce。升级该依赖时保留或重新验证此行为；不要通过放宽 `style-src` 处理样式被拦截的问题。

退出或回退只操作本项目：

```bash
docker compose --env-file .env.production -f docker-compose.lan-prod.yml stop
```

保留持久卷和上一个镜像标识；不要使用 `down -v`。有不兼容数据库迁移时，镜像回退必须配合新库及对象存储的恢复方案，不能自动 downgrade 或恢复整个共享实例覆盖旧环境。

备份范围包括生产数据库、八个桶、生产配置、迁移凭据与 Caddy CA 卷。CA 丢失后重新生成会要求全部客户端重新信任。Redis AOF 可降低队列丢失概率，但异常中断的作业仍应按数据库任务状态核对和恢复。

## 容器健康恢复与定时维护

该独立 Compose 的常驻服务使用 `restart: unless-stopped`，处理进程退出和 Docker 启动后的恢复；Docker 不会仅因为容器变为 `unhealthy` 自动重启它。宿主机的开发服务仍使用 3000/8000，生产入口使用 HTTPS 3030/8080。

`scripts/production-health-recover.py` 配合 `infra/systemd/aap-production-health.{service,timer}` 每两分钟检查生产环境：

- 固定使用本机 Docker `default` context，校验 `aap-production` 项目、服务、Compose 文件及工作目录标签。只操作当前已有容器 ID，不执行 Compose 部署、构建、迁移或共享基础设施重启，也不向宿主进程发送信号。
- 连续三次异常后，恢复一个已停止或不健康的容器；单服务两次恢复至少间隔 15 分钟，每小时最多三次。恢复后由下一轮检查确认状态，启动中、Docker 正在重启或人工暂停的容器不会被抢先操作。
- API 的无外部依赖路由能返回 HTTP 响应时，依赖检查失败只记录降级；Redis 异常时不连带重启 worker。数据库、MinIO 和共享模型服务需要按各自运维流程处理。
- 通过容器实际绑定 IP，使用导出的 Caddy 公共根证书校验 HTTPS `/healthz` 和 `/health/db`，不跳过证书检查，不使用代理环境变量。仅在 API、Web 均健康时尝试恢复入口。
- 缺失容器、标签冲突或重复副本会停止自动恢复并记录错误，需要人工核对部署。迁移容器不在恢复范围内。beat 当前只有运行状态检查，无法据此证明定时任务持续调度。

### 本机安装与检查

将脚本复制到 `~/.local/lib/ai-annotation-platform/production-health-recover.py`，将公共根证书保存到 `~/.local/state/ai-annotation-platform/production-health/root.crt`。脚本仅依赖 Python 标准库及 Docker CLI。保留现有 crontab 和旧脚本备份，再将 `~/.local/bin/ai-annotation-platform-restart` 替换为调用该副本的入口，传入以下参数（路径需替换为本机实际值）：

```bash
/usr/bin/python3 ~/.local/lib/ai-annotation-platform/production-health-recover.py \
  --project-dir /absolute/path/to/ai-annotation-platform \
  --state-dir ~/.local/state/ai-annotation-platform/production-health \
  --ca-file ~/.local/state/ai-annotation-platform/production-health/root.crt \
  --check
```

`--check` 不改变容器或失败计数；退出码 0 表示检查通过或维护暂停，1 表示异常、恢复待确认或检查失败。定时入口省略 `--check` 并透传手动参数。安装到用户目录的副本避免工作区切换分支直接改变运行中的维护程序；更新源码后应重新安装并验证。

复制两个 systemd 单元到 `~/.config/systemd/user/` 后启用：

```bash
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user start aap-production-health.service
systemctl --user enable --now aap-production-health.timer
systemctl --user list-timers aap-production-health.timer
journalctl --user -u aap-production-health.service -n 40 --no-pager
```

`Linger=yes` 保证退出登录后用户定时器仍运行，并可随系统启动。确认首次运行成功后，移除旧的每日全栈重启 cron 条目，保留其他任务。不要保留会按 3000/8000 杀进程、执行默认 Compose 全量重启或自动迁移的旧入口。检查其他用户及 root 的 crontab 需要相应权限，当前用户的检查不能替代它们。

开发环境如需每天定时重启，应另设 `aap-development-restart.timer`，仅重启开发 Web/API 与开发专用 worker/beat；不要重新启用旧的全栈 cron。其配置和暂停方式见仓库 `DEV.md` 的“本机开发服务定时重启”。两个定时器使用独立入口、锁和维护开关。

### 暂停、升级和回退

计划停止生产、部署或人工诊断前，先暂停恢复；否则已经停止但仍存在的生产容器会被视为待恢复：

```bash
touch ~/.local/state/ai-annotation-platform/production-health/maintenance
flock ~/.local/state/ai-annotation-platform/production-health/health.lock true
# 在此执行本项目的部署或 stop 操作。
rm ~/.local/state/ai-annotation-platform/production-health/maintenance
~/.local/bin/ai-annotation-platform-restart --check
```

彻底停用使用 `systemctl --user disable --now aap-production-health.timer`，并等待正在执行的 service 完成。故障记录保存在同目录的 `health-state.json`，执行日志进入用户 journal。回退健康恢复程序时保留旧程序备份；不要重新启用会影响开发端口的旧 cron。CA 发生更换时重新导出公共根证书，再验证两个 HTTPS 入口。
