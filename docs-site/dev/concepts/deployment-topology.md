---
audience: [dev, ops]
type: explanation
since: v0.1.0
status: stable
last_reviewed: 2026-07-29
---

# 部署拓扑

平台的核心应用边界由 web、API、按队列隔离的 Celery worker / beat、PostgreSQL、Redis 和对象存储组成。ML Backend 是通过 HTTP 协议注册的独立计算服务，可以与核心应用同机，也可以运行在独立 GPU 主机或集群中。

## 部署形态与演进

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/deployment-shapes.svg"
  alt="平台从核心应用与 ML 服务同机，演进到分离 ML 主机，再由项目启用逻辑服务池并路由到多个物理实例的部署形态"
  caption="部署演进：先稳定核心边界，再按计算资源和模型路由拆分"
/>

### 同机协作

生产核心栈由 `docker-compose.yml` 叠加 `docker-compose.prod.yml` 启动；ML 服务在独立的 `docker-compose.ml.yml` 中按 profile 启用。两个 Compose 文件使用同一 project 时，worker 可以通过 service DNS 访问 `grounded-sam2-backend:8001` 等实例，无需绕经宿主回环。

同机不等于必须同启所有模型。Grounded-SAM-2、SAM 3、YOLO、ONNXTools 和 RapidOCR 都是可选 profile，应按显存预算组合。容量上限取决于媒体尺寸、模型、worker 并发和存储性能，不用固定“标注员数”判定。

### 应用与 ML 计算分离

- ML Backend 注册 URL 必须是 worker 可达的 LAN 地址或内部域名，不能填只对 ML 主机自身有效的 loopback。
- worker 签发给 ML Backend 的媒体 URL 可用 `ML_BACKEND_STORAGE_HOST=<minio-host>:9000` 切换成 ML 网络视角。该值只写 host 与端口，协议由对象存储配置决定。
- 浏览器和远程 SDK 使用另一个视角：`MINIO_PUBLIC_URL` 必须是客户端可达的 HTTPS 地址或同源路径。它与 `ML_BACKEND_STORAGE_HOST` 不能互相替代。

详见 [容器网络与 loopback 限制](../troubleshooting/container-networking)。

### 多模型与多副本

物理实例保存在 ML Backend registry 中，服务池把能力一致的一个或多个实例组成逻辑计算单元。项目启用的是服务池，请求进入 `MLBackendRouter` 后才会根据健康、能力指纹、熔断、权重和并发上限选出物理实例。

prompt、媒体和 pipeline 参数是推理输入，不是直接选择物理主机的路由键。协议契约见 [ML Backend 协议](../reference/ml-backend-protocol)，服务池语义见 [ADR 0050 — ML Backend 服务池路由](../adr/archive/0050-ml-backend-service-pools-and-request-routing)。

Kubernetes 编排目前不是既定路线；如果 Compose 和服务池已无法满足调度、容错或运维要求，应先新建 ADR 论证迁移边界。

## 生产入口与端口

需要在现有主机上保留开发环境并新增独立生产业务数据时，另有[局域网生产部署](/ops/deploy/lan-production)：`docker-compose.lan-prod.yml` 使用独立 Compose 项目，Caddy 在局域网 3030/8080 终结内部 HTTPS；PostgreSQL 实例、MinIO 实例和模型服务复用，新数据库、八个桶、Redis、Worker、Beat、DuckDB 和 CA 卷独立。下面的 8088 拓扑仍适用于整栈生产覆盖文件，两个入口不要叠加使用。

<ExcalidrawDiagram
  src="/diagrams/shared/deployment/production-network-boundaries.svg"
  alt="生产环境在反向代理与容器同机时通过回环 8088 进入 web，异机时通过容器主机内网 IP 的 8088 进入 web，公网都只暴露 443"
  caption="生产网络边界：同机用回环，异机只绑定内网网卡"
/>

`docker-compose.prod.yml` 不直接提供 443；443 由宿主或独立边缘主机上的 nginx / Caddy 终结 TLS。外层反代通常只需转发到 web 容器的 8088，web 容器内的 Nginx 再把 `/api/` 和 `/ws/` 转给 `api:8000`。

| 边界 / 服务                | 当前端口语义                                     | 生产暴露策略                                                                                |
| -------------------------- | ------------------------------------------------ | ------------------------------------------------------------------------------------------- |
| 外层反向代理               | HTTPS 443                                        | 唯一公网入口                                                                                |
| web                        | `${PROXY_BIND_HOST:-127.0.0.1}:8088` → 容器 80   | 反代同机绑回环；异机只绑容器主机内网 IP                                                     |
| API                        | `${PROXY_BIND_HOST:-127.0.0.1}:8080` → 容器 8000 | 仅回环 / 内网，不作为公网 SDK 入口                                                          |
| PostgreSQL / Redis / MinIO | Compose 内网 5432 / 6379 / 9000                  | prod 叠加文件用 `!reset []` 移除宿主端口                                                    |
| 可选 ML Backend            | 当前 profile 发布 8001–8005                      | 只允许 worker 所在可信网络访问；现有 ML 叠加文件不会自动绑定内网 IP，需防火墙或部署覆盖收紧 |
| 可选监控                   | 9090 / 3001 / 9093                               | 默认 profile 不启动；启动后只限内网 / VPN                                                   |

客户端通过 `MINIO_PUBLIC_URL` 获取对象的预签名 URL，不意味着要把 MinIO 原生 9000 端口直接公开。可以使用受 TLS 保护的同源反代路径或独立对象存储域名。详细边界见 [端口暴露与网络安全](/ops/deploy/network-security)。

## 数据卷与持久化

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/persistence-boundaries.svg"
  alt="平台将 PostgreSQL 与 MinIO 中的业务真值列为最高恢复优先级，将 DuckDB、模型权重、监控数据和无持久卷的 Redis 按可重建性分层"
  caption="持久化分层：备份数据真值，为可重建资产保留可验证的来源"
/>

- `pgdata` 是 PostgreSQL 业务真值，必须备份。数据库恢复要与 Alembic 版本和对象存储快照协调。
- MinIO 的 `/data` 来源是 `${MINIO_DATA_DIR:-miniodata}`：默认为 `miniodata` 命名卷，也可设为宿主绝对路径的 bind mount。它包含原始媒体、派生媒体、导入导出、反馈附件与审计归档等多个 bucket。
- Redis 当前**没有持久卷**。它不是业务系统真值，但丢失 broker、锁、限流和路由账本状态后，在途或排队作业不能假定自动恢复，应以 PostgreSQL 作业记录进行重试或对账。
- `gsam2_*`、`sam3_*` 和 `yolo_checkpoints` 为模型权重 / 缓存卷，可重新下载但恢复时间较长。ONNXTools 与 RapidOCR 的模型是运维注入的只读目录，需保留下载源或单独备份。
- `./data/duckdb` 本就是 bind mount；`prometheus_data`、`grafana_data` 和 `alertmanager_data` 分别保留可观测历史、看板配置和告警运行状态。

`docker-compose.hostvols.yml` 只重定向其中明确列出的命名卷，不会迁移现有数据。当前它覆盖 PostgreSQL、MinIO、Grounded-SAM-2、SAM 3、Prometheus 和 Grafana；未列出的卷仍由 Docker 管理。切换前必须停服、复制并校验数据。

## 相关

- [运行环境形态](./runtime-environments)（同一套代码在开发与生产环境的运行差异）
- [生产部署](/ops/deploy/docker-compose)（Compose 叠加、反向代理、卷与环境变量）
- [ADR 0012 — SAM Backend 独立 GPU 服务](../adr/archive/0012-sam-backend-as-independent-gpu-service)
- [容器网络与 loopback 限制](../troubleshooting/container-networking)
