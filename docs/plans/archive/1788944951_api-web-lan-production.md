# API 与 Web 局域网生产部署计划

状态：部署完成，基础验收通过；完整业务流程验收范围见下方交付记录。用户确认新建生产账号、业务数据独立、复用底层服务和模型，以及局域网 HTTPS；内部证书需客户端首次导入信任。首个生产管理员为 John，登录标识由用户指定，密码保存在本机受限文件中。

## 目标与已确认边界

- 基于当前根目录 `.env` 派生生产配置，保持现有环境可用。
- 前端使用宿主端口 3030，后端使用宿主端口 8080。
- 生产重新创建账号；不复制现有用户、项目、数据集、任务、标注、审计和业务设置。
- 复用当前主机的 PostgreSQL 实例、MinIO 实例及模型推理服务和权重。
- 当前主机局域网地址为 `172.26.1.23`。绑定该网卡，部署前重新确认地址；长期使用应固定地址或设置 DHCP 地址保留。
- 不发布公网，不购买域名，不迁移现有数据，不启用 GPU enforce，不进行应用版本发布。

## 服务与数据隔离

| 对象           | 生产安排                                                                                                                                             |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| PostgreSQL     | 复用实例，创建独立数据库 `annotation_production`；运行账号 `anno_prod_app` 与迁移 owner `anno_prod_owner` 分离，运行账号另负责限定分区及物化视图维护 |
| MinIO          | 复用实例及现有磁盘，新建七个 `prod-` 前缀的桶；生产凭据仅访问这些桶                                                                                  |
| 模型服务       | 使用现有服务地址和权重；在新数据库中根据现有环境配置重新注册，重新探测能力                                                                           |
| Redis          | 新建生产专用容器 `redis-production`，开启 AOF，使用独立持久卷，不发布宿主端口                                                                        |
| Celery         | 生产单独运行六类 Worker 和一个 Beat，使用生产数据库、Redis、存储桶                                                                                   |
| DuckDB         | 独立持久卷；API 与所需 Worker 挂载同一生产文件，API 只读                                                                                             |
| 用户及业务配置 | 运行迁移得到初始结构和必要系统默认值，随后新建管理员，不复制旧库业务行                                                                               |
| HTTPS          | 入口代理及其内部 CA 使用独立持久卷，备份 CA；不得因重建容器改变信任根                                                                                |

Redis 不能只将 `/0` 改成 `/1`：当前代码有固定的 `global:prediction-jobs`、`ml-backend-stats:global` 推送频道，而 Redis Pub/Sub 不按逻辑数据库隔离。独立实例避免跨环境消息混入，无需修改业务频道设计。

生产仍然与现有环境共享 PostgreSQL、MinIO、模型服务的可用性及机器资源。业务数据独立不等于故障和性能完全独立。共享模型服务的重启、模型切换和繁忙状态可能影响两边；初期生产 GPU Worker 并发设为 1，维持 `.env` 的 `observe` 和关闭 rollout 状态，不创建第二套权威 GPU 控制面。

## 编排与访问入口

采用独立 Compose 文件 `docker-compose.lan-prod.yml` 和项目名 `aap-production`，复用现有 Dockerfile 与 Nginx 配置。不能直接叠加现有 `docker-compose.prod.yml` 执行全栈更新，因为它会修改旧基础设施的端口和旧 Worker 的运行配置。

生产定义自己的私有网络，按需接入已存在的 `ai-annotation-platform_default` 外部网络访问 PostgreSQL、MinIO 和模型。生产 Redis 使用独有服务名，避免与共享网络中的旧 Redis DNS 别名冲突。共享服务和现有数据卷不归生产 Compose 管理。

```text
局域网客户端
  ├─ HTTPS 172.26.1.23:3030 → Caddy → Web Nginx:80
  │                                      ├─ /          → 静态文件
  │                                      ├─ /api/ /ws/ → 生产 API:8000
  │                                      └─ /minio/    → 共享 MinIO:9000
  └─ HTTPS 172.26.1.23:8080 → Caddy → 生产 API:8000

生产 API / 六类 Worker / 单实例 Beat
  ├─ 共享 PostgreSQL → annotation_production
  ├─ 独立 redis-production
  ├─ 共享 MinIO → 七个生产桶
  └─ 共享模型服务
```

Caddy 在两个宿主端口终结 TLS；API 和 Web 的原生 HTTP 端口只在容器网络开放。因此 `8080` 仍是后端访问端口，但由代理转发到容器 8000。前端日常访问使用 3030 下的同源 API 和媒体代理，不跨域调用 8080。

该方案增加 11 个常驻容器：API、Web、Caddy、Redis、六类 Worker、Beat；迁移和管理员初始化是一次性进程。六类 Worker 对应通用、GPU、CPU、导出、图像金字塔和 GPU control，保留现有队列归属。GPU control 在 rollout 关闭时跳过权威修复，不挂载旧数据库 collector 凭据。

## HTTPS 的客户端成本

无域名时由内部 CA 为局域网 IP 签发证书。每台访问设备需要首次安装并信任 CA 公共证书；部分浏览器需要单独导入。未信任时浏览器会报警，简单点击继续不能作为完整功能验收标准。只分发公共证书，私钥始终保留在服务器。

证书信任通常需要用户或 IT 管理员操作，受公司设备策略限制；完成后日常使用无额外步骤。正常续签站点证书不需要重新安装 CA；更换设备、浏览器信任库或 CA 时需要重新配置。

IP + HTTP 是配置成本更低的替代入口，但 WebGPU、WebCodecs 依赖安全上下文，源码中也存在直接调用 `crypto.randomUUID()` 的编辑路径，不能承诺完整功能。用户若要求免客户端配置，重新选择域名和受信任证书方案；域名与 HTTPS 并不要求服务向公网开放。

## 生产配置

生成本地、被 Git 忽略且权限为 0600 的 `.env.production`。从当前 `.env` 按用途复制配置，显式覆盖容器地址和生产数据边界，不把模型下载令牌等无关凭据整体注入每个容器。

| 配置                                               | 计划值或来源                                                |
| -------------------------------------------------- | ----------------------------------------------------------- |
| `ENVIRONMENT` / `DEBUG`                            | `production` / `false`                                      |
| `E2E_SEED_ENABLED`                                 | `false`                                                     |
| `DATABASE_URL`                                     | `postgres:5432/annotation_production`，使用新建运行角色凭据 |
| `MIGRATION_DATABASE_URL`                           | 同一生产库，使用独立 owner，仅注入迁移入口                  |
| `ALEMBIC_AUTO_UPGRADE`                             | 常驻 API、Worker、Beat 均为 `false`；上线时先单独执行迁移   |
| `REDIS_URL`                                        | `redis://redis-production:6379/0`                           |
| `CELERY_BROKER_URL`                                | 不设置独立旧地址，使 broker/result backend 跟随生产 Redis   |
| `MINIO_ENDPOINT` / `MINIO_USE_SSL`                 | `minio:9000` / `false`；共享实例内部连接沿用 HTTP           |
| `MINIO_PUBLIC_URL`                                 | `/minio`                                                    |
| `MINIO_BUCKET`                                     | `prod-annotations`                                          |
| `MINIO_DATASETS_BUCKET`                            | `prod-datasets`                                             |
| `MINIO_BUG_REPORTS_BUCKET`                         | `prod-bug-reports`                                          |
| `MINIO_MEDIA_CACHE_BUCKET`                         | `prod-media-cache`                                          |
| `MINIO_AUDIT_ARCHIVE_BUCKET`                       | `prod-audit-archive`                                        |
| `MINIO_IMPORT_BUCKET`                              | `prod-import`                                               |
| `MINIO_EXPORT_BUCKET`                              | `prod-export`                                               |
| `ML_BACKEND_STORAGE_HOST`                          | `minio:9000`，实施时从所有复用模型容器验证可达              |
| 模型地址与能力设置                                 | 来源为现有 `.env`，在新库重新探测，不复制旧项目关联         |
| `GPU_ARBITER_MODE` / `GPU_ARBITER_ROLLOUT_ENABLED` | `observe` / `false`                                         |
| `DUCKDB_PATH`                                      | `/var/lib/duckdb/analytics.duckdb`，来自生产独立卷          |
| `FRONTEND_BASE_URL`                                | `https://172.26.1.23:3030`                                  |
| `CORS_ALLOW_ORIGINS`                               | 仅生产前端的完整 HTTPS origin                               |
| `SECRET_KEY`                                       | 独立随机值，避免新旧环境登录令牌互通                        |
| `ALLOW_OPEN_REGISTRATION`                          | `false`，通过管理员创建用户开始使用                         |

创建 MinIO 凭据时覆盖应用当前必需的桶检查、对象访问及桶生命周期设置权限，并限定到生产桶；不能仅授予对象读写后忽略启动时的生命周期调用。生产入口的 `/minio/` 路由限定七个生产桶，验证路径改写、签名参数和上游 Host，不代理管理接口。

本方案不新增第三方账户或外部 API。需要的本地凭据包括新数据库运行/迁移密码、MinIO 生产访问凭据、JWT 签名密钥、内部 CA 私钥，以及首个管理员的邮箱标识、姓名和初始密码。管理员信息在初始化时由部署负责人输入，不沿用旧账号；密码不写入计划、提交或命令历史。现有模型如要求认证，按调用需求使用当前配置中的对应凭据。

运行权限在实施时按代码进一步细化：现有 Worker 需要创建/归档月分区和并发刷新物化视图。`anno_prod_app` 拥有审计/预测父表、全部子分区、两个统计物化视图，以及 `public` CREATE 和数据库 TEMPORARY；它不是纯 DML 角色。迁移 owner 单向继承 app，app 不继承 owner；其他业务表所有权不转移，GPU membership/fence DELETE 和迁移版本写入仍被撤销。生产 SFTP 使用独立 `known_hosts` 文件及连接器加密密钥。

## 实施工作与文件范围

按一个可完整交付的部署变更实施，不把缺少异步任务或媒体代理的中间状态当作上线完成。预计涉及超过 8 个文件，其中主要目标如下：

- 新增 `docker-compose.lan-prod.yml` 与 `infra/docker/Caddyfile.lan-prod`，为两个入口配置内部 TLS。
- 更新 `infra/docker/nginx.conf`，补齐生产媒体代理、上传限制、WebSocket、转发头以及 API 容器替换后的 DNS 恢复；保留现有 CSP nonce 和内部端点拦截规则。
- 更新 `infra/docker/Dockerfile.api`，按 `uv.lock` 构建隔离的容器 Python 环境，保留当前共享包路径布局及媒体依赖，验证所有复用镜像的 Worker 入口。
- 更新 `infra/docker/Dockerfile.web`，固定仓库指定 pnpm 版本，确保构建所需代码生成脚本和快照均在镜像构建上下文中。
- 更新 `.dockerignore`，排除 `.env`、生产配置、秘密文件、数据目录和本地依赖；检查 API 目录复制不会带入敏感文件。
- 按上述边界生成本地 `.env.production`，不提交。
- 同步 `.env.example`、`DEV.md`、`README.md`、`docs-site/ops/deploy/docker-compose.md` 和 `docs-site/dev/concepts/deployment-topology.md`；有新增环境变量时运行环境变量文档生成命令。
- 为可复用且有行为风险的编排约束增加针对性检查；不为纯文档或静态端口修改编写镜像式测试。

## 上线顺序与验收

1. 只读复核当前网络、数据卷、监听地址、`.env` 和模型运行状态。记录旧环境基线，检查 3030/8080 无冲突。对共享数据服务准备可恢复备份。
2. 明确管理员初始化输入，生成新凭据、生产配置和 CA。创建新数据库、角色、七个桶及受限 MinIO 凭据；不修改旧数据库或旧桶的内容和生命周期策略。
3. 对独立生产 Compose 执行 `docker compose --project-name aap-production --env-file .env.production -f docker-compose.lan-prod.yml config --quiet`；完整合并配置仅在本地脱敏检查，不输出密钥。
4. 构建并用当前 Git 提交标识镜像，启动生产 Redis；通过新库专用迁移入口执行 `alembic upgrade head`。必须核验实际数据库名和角色后再执行。
5. 启动生产 API、六类 Worker 和单实例 Beat；使用现有 `scripts.bootstrap_admin` 初始化新管理员，不运行开发 seed。
6. 启动 Web 和 Caddy，导出 CA 公共证书，并在验收客户端建立信任。配置根证书和镜像备份，记录恢复步骤。
7. 从真实局域网客户端验证两个 HTTPS 入口：证书无警告，`window.isSecureContext` 为 true，首页及深链接可打开，API 健康探针成功，登录后创建项目和数据集、上传、加载媒体、保存标注、执行预标和导出成功；视频支持 Range，WebSocket 连接和重连正常。WebGPU/WebCodecs 在支持它们的客户端检查能力探测，不把 HTTPS 等同于硬件支持保证。
8. 在隔离的临时验收数据库和存储桶运行写入型自动测试，覆盖签名失效、超限上传、共享模型不可用、生产 Redis/Worker 重启后的恢复；验证旧账号不能登录新环境，两边队列和全局推送不交叉。不能把计划中的生产库或旧开发库当作可随意清空的测试库。
9. 清理本次验收创建的专用数据库、桶、容器、临时文件及受控测试数据；保留生产数据库、CA 卷和其他运行持久卷。核对旧环境基线、审阅最终 diff，执行 `git diff --check`。

生产配置与持久卷恢复属于部署交付。用户已授权按本计划实施、启动、提交及推送；不进行应用版本发布。客户端系统信任的安装由客户端用户或 IT 完成，不自动修改其他设备的信任库。

## 失败处理与回退

- 初次部署失败只停止 `aap-production` 项目，保留其数据、日志和 CA；旧环境保持运行。不得使用包含旧基础设施的 Compose 命令执行全栈重建或 `down -v`。
- 共享模型不可用时显示失败或排队状态，恢复后按任务状态重试，不切换到旧环境的 Worker。
- 后续镜像更新保留上一提交的镜像。无数据库结构变化时可回退镜像；发生不兼容迁移时需协调新库备份与对象数据恢复，不自动执行数据库 downgrade。
- 备份应包含生产 PostgreSQL 数据库、七个 MinIO 桶、生产配置、CA 和必要恢复凭据；Redis 开启 AOF 后仍需依据数据库任务状态核对异常中断的作业。

## 交付记录

本机已启动独立 `aap-production` 的 11 个常驻容器。前端入口为 `https://172.26.1.23:3030`，API 为 `https://172.26.1.23:8080`；应用镜像标识为 `6429a41d3603-lan2`，包含本次未提交变更，不代表该提交本身已有这些修改。生产迁移完成至 `0162`，首个超级管理员 John 已创建，项目、数据集、任务和标注均为零，五个复用模型服务已重新探测并在线。

已完成的验证：

- API、Web、独立 Redis 和六类 Worker 健康检查通过；Beat 持续运行，`/health` 返回数据库、Redis、MinIO、六类 Worker 正常。
- 使用导出的 CA 校验两个 HTTPS 入口的证书链和 IP；真实 Chrome 验证登录、概览、项目和数据集页面，安全上下文成立，WebGPU/WebCodecs API 可用，控制台与 CSP 违规均为空。浏览器自动化按已验证的站点公钥固定信任，未替其他客户端安装系统证书。
- 两个同源 WebSocket 通道（通知、预测任务）在 CA 校验通过的 WSS 连接上收到服务端心跳。
- 媒体代理完成 2 MiB 对象上传下载、Unicode/空格路径、Range 206、错误签名 403 和旧桶入口 404；生产凭据访问旧桶被拒绝，验收对象已删除。
- 运行账号不能访问旧库业务表或继承迁移 owner，不能删除 GPU membership/fence；实际 Worker 创建后续月份分区、刷新两个物化视图和初始化 DuckDB 成功。
- 在专用临时数据库及临时 Redis 中运行 20 项 API 回归测试，通过；通知组件 CSP nonce 与现有相关前端测试共 9 项通过。完整 API/Web 镜像构建、文档构建、文档检查和 CSS token 检查通过。
- 原有 20 个容器的 ID、镜像、启动时间和挂载与部署前一致；测试数据库、Redis 容器、验收截图、临时补丁编辑目录和本次文档构建产物已清理。

证书及客户端安装说明位于本机 `outputs/lan-production/`。管理员初始凭据、生产环境配置副本、迁移连接、CA 私钥备份、部署前开发库备份、生产初始备份及验证记录位于本机受限目录 `~/.local/state/ai-annotation-platform/lan-production/`，不进入 Git。生产数据库备份已验证目录可读，尚未做恢复演练。

本次没有运行真实模型预标注、完整浏览器标注/导出流程、8 GiB 超限上传、共享模型故障或 Redis 重启恢复演练，也未替另一台客户端安装 CA；不能将基础部署验收等同于这些场景已通过。模型 API 在线和浏览器能力可见不代表推理或 GPU 渲染性能已完成资格验证。后续正式客户端按安装说明导入公共 CA 后访问。本次不进行应用版本发布。

## 检查依据

- 仓库中的 Compose、Dockerfile、Nginx、API 配置、Celery 路由、存储服务、WebSocket 频道与管理员引导脚本，以及本机只读网络和挂载检查。
- [Docker Compose 环境变量优先级](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/)。
- [Redis Pub/Sub 不按数据库编号隔离](https://redis.io/docs/latest/develop/pubsub/)。
- [Caddy 内部 HTTPS 与客户端信任](https://caddyserver.com/docs/automatic-https)。
- [WebGPU 安全上下文要求](https://gpuweb.github.io/gpuweb/) 与 [WebCodecs 安全上下文要求](https://www.w3.org/TR/webcodecs/)。

## Outcome

- Landed commits: 见 `## 交付记录`（本计划未记录仓库提交哈希；应用镜像标识 `6429a41d3603-lan2`）
- Release milestone: Not yet determined
- User documentation: `docs-site/ops/deploy/docker-compose.md`、`docs-site/ops/deploy/lan-production.md`
- Developer documentation: `docs-site/dev/concepts/deployment-topology.md`
- ADR: 无
- CHANGELOG: 未添加（未进行应用版本发布）
- Remaining work: 未运行真实模型预标注、完整浏览器标注 / 导出、8 GiB 超限上传、共享模型故障与 Redis 重启恢复演练，也未替另一台客户端安装 CA；生产数据库备份尚未做恢复演练。
