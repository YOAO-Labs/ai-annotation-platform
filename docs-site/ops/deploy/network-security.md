---
title: 端口暴露与网络安全
audience: [ops, dev]
type: explanation
since: v0.15.17
status: stable
last_reviewed: 2026-10-26
---

# 端口暴露与网络安全

> 适用读者：把平台搬出本机（暴露给团队 / 公网 / 远程 SDK 调用方）的运维与开发者。
>
> **本页只管「网络/部署层」**：哪些端口该对外、反代怎么挡在前面、远程 SDK 怎么安全连。**平台内置的安全机制**（JWT、RBAC、限流、审计、CSP、CORS）见 [安全模型](/ops/security/)——两者互补，本页是它「§威胁模型不包含：物理访问 / SSH 入侵 / 网络隔离」那部分的展开。

---

## 1. 一句话原则

**对公网只暴露最外层反向代理（HTTPS / 443）。后端 API（uvicorn）、对象存储、监控等端口一律只绑内网或回环，由反代转发。**

裸暴露 8000（FastAPI/uvicorn）到公网会同时踩中三个坑——明文凭据、绕过反代防护、绕过防火墙，下文逐条拆解。

---

## 2. 端口清单：该不该对外

平台默认涉及的宿主端口（生产 `docker-compose.prod.yml` + 基础 `docker-compose.yml`）：

| 端口               | 服务                                | 生产是否发布到宿主  | 说明                                                                     |
| ------------------ | ----------------------------------- | :-----------------: | ------------------------------------------------------------------------ |
| 443                | 外层反代（nginx / Caddy）           | ✅ **唯一公网入口** | 终结 TLS，转发到 web / api 容器                                          |
| 8088               | web 容器（`8088:80`）               | 仅回环 `127.0.0.1`  | 托管静态产物 + 反代 `/api/` `/ws/` → `api:8000`；交给外层反代指向它      |
| 8080               | api 容器（`8080:8000`）             | 仅回环 `127.0.0.1`  | FastAPI/uvicorn。**最该警惕的端口**——绝不直接开公网                      |
| 5432               | postgres                            |    ❌ **不发布**    | prod 叠加文件 `ports: !reset []`；api/worker 经内网 `postgres:5432` 直连 |
| 6379               | redis                               |    ❌ **不发布**    | 同上 `redis:6379`；broker / 限流 / 黑名单                                |
| 9000 / 9001        | MinIO API / 控制台                  |    ❌ **不发布**    | 同上 `minio:9000`；presigned URL 可达性见 §5                             |
| 8025 / 1025        | mailpit（dev）                      |    ❌ **不发布**    | 假收件箱，**生产应禁用**，改真实 SMTP                                    |
| 8001–8005          | 可选 ML Backend profile             |  ⚠️ 当前发布到宿主  | `docker-compose.ml.yml` 不自动收紧绑定；只允许 worker 所在可信网络访问   |
| 9090 / 3001 / 9093 | Prometheus / Grafana / Alertmanager |    ⚠️ 仅内网/VPN    | 监控 profile，默认不启动；启用时务必限内网                               |

> **两层绑定**：① 容器**内部**互通走 compose 内网 service DNS（`postgres:5432` 等），不需要发布到宿主——所以 prod 用 `ports: !reset []` 把基础文件给 dev 用的 `0.0.0.0` 发布**整个移除**，PG/Redis/MinIO 在宿主网卡上完全不可见。② api/web 仍需让外层反代够到，故发布但绑回环 `${PROXY_BIND_HOST:-127.0.0.1}`。
>
> 端口映射写法决定绑定范围：`"8080:8000"` 绑 `0.0.0.0`（所有网卡，含公网）；`"127.0.0.1:8080:8000"` 只绑回环。注意 compose 的 `ports` 合并是 **append**——叠加文件里再写一条绑回环**盖不掉**基础的 `0.0.0.0`，必须用 `!reset []` 清空（Compose v2.24+）。详见 §4。

---

## 3. 威胁模型（网络层）

裸暴露后端端口到公网，对应的具体威胁：

| 威胁                             | 后果                                                                                                   | 缓解                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------- |
| **API Key 明文传输**             | SDK 直连 `http://IP:8000` 时 `Authorization: Bearer <key>` 明文过网，中间人可截获，Key 泄露 = 账号沦陷 | 对外仅 HTTPS，反代终结 TLS（§5、§6）                           |
| **绕过反代防护层**               | uvicorn 自身无限流 / 无 WAF / 无连接数上限，裸奔公网易被打爆                                           | 后端只绑内网，公网入口只有反代（反代承担限流 / 超时 / WAF）    |
| **Docker 端口映射绕过 ufw**      | `ports: 0.0.0.0:8080` 直接写 iptables `DOCKER` 链，优先级高于 ufw，你以为挡了实际没挡                  | 绑 `127.0.0.1:` 前缀或 `DOCKER-USER` 链（§4）                  |
| **`/docs` `/openapi.json` 泄露** | FastAPI 默认带交互文档，暴露完整 API 结构                                                              | 只走反代、反代按 path 控制；或生产关闭 docs                    |
| **`/metrics` 泄露内部路由**      | Prometheus exposition 含 path label，暴露内部端点与流量特征                                            | `/metrics` 仅内网 allow（反代里 `allow 10.0.0.0/8; deny all`） |
| **presigned URL 指向内网**       | 后端签发的对象存储 URL 是内网地址，远程调用方根本连不上（API 通了但上传/下载失败）                     | 配 `MINIO_PUBLIC_URL` 为对外可达地址（§5）                     |

> 本页不覆盖凭证撞库、越权、审计篡改等**应用层**威胁——那些由平台内置机制处理，见[安全模型 §1 威胁模型](/ops/security/)。

---

## 4. 端口绑定姿势（防 Docker 绕过 ufw）

**这是最隐蔽的坑**：Docker 发布端口时直接操作 iptables 的 `DOCKER` 链，绕过 ufw/firewalld 的用户规则。所以「我配了 ufw deny 8080」往往是无效的，公网照样能访问。

正确做法二选一：

**① 端口只绑回环 / 内网网卡（推荐，最简单）**

`docker-compose.prod.yml` **已默认这么做**——api/web 端口绑 `${PROXY_BIND_HOST:-127.0.0.1}`，开箱即只在宿主回环可达：

```yaml
# docker-compose.prod.yml（现状）
services:
  api:
    ports:
      - "${PROXY_BIND_HOST:-127.0.0.1}:8080:8000"
  web:
    ports:
      - "${PROXY_BIND_HOST:-127.0.0.1}:8088:80"
  # PG/Redis/MinIO 容器间走内网 service DNS，生产根本不需要发布到宿主——
  # 直接清空基础文件的 ports（绑定写法是 append，盖不掉，必须 !reset）：
  postgres: { ports: !reset [] }
  redis: { ports: !reset [] }
  minio: { ports: !reset [] }
```

外层反代**同机**时无需任何配置，`proxy_pass http://127.0.0.1:8088;` 转发即可。反代在**另一台机器**时，于 `.env.production` 设 `PROXY_BIND_HOST=<内网IP>`（如 `10.0.0.5`），**切勿设 `0.0.0.0`**——那等于放回公网。

#### 为什么绑哪个地址这么关键：网卡 = 谁能连进来

一台机器同时有多张网卡，「端口绑哪张网卡」直接决定谁能连到它：

| 绑定地址              | 含义                       | 谁能连进来                               |
| --------------------- | -------------------------- | ---------------------------------------- |
| `127.0.0.1`           | 回环网卡，数据包出不了本机 | **只有本机自己**                         |
| `10.0.0.5`（内网 IP） | 内网网卡                   | 同内网的其他机器（含公网网卡时仍碰不到） |
| `0.0.0.0`             | **所有网卡**通配           | 本机 + 内网 + **公网任何人**（危险）     |

端口映射 `"8080:8000"` 冒号左边是宿主机口子、右边是容器内服务；**左边不写 IP 时默认绑 `0.0.0.0`**——这就是收紧前的隐患。下面两种部署形态决定 `PROXY_BIND_HOST` 怎么填：

<ExcalidrawDiagram
  src="/diagrams/shared/deployment/production-network-boundaries.svg"
  alt="生产环境的外层反代与容器同机时通过回环 8088 进入 web，异机时通过容器主机内网 IP 的 8088 进入 web，两者的公网都只暴露 443"
  caption="反向代理同机与异机时的网卡绑定边界"
/>

**情形 A —— 反代与容器同机（绝大多数，默认即安全，无需改）**

nginx 和容器是「邻居」，走回环 `127.0.0.1:8088` 就能互通；公网只摸得到 443，8080/8088 因绑回环而进不来。

**情形 B —— 反代与容器不同机（需设 `PROXY_BIND_HOST=内网IP`）**

nginx 要**跨机器**连 server-2 的容器。若 server-2 端口绑 `127.0.0.1`，回环出不了 server-2，nginx 永远连不上 → 必须绑内网 IP（`PROXY_BIND_HOST=10.0.0.5`），让 nginx 走内网到达。仍**不要**绑 `0.0.0.0`：绑内网 IP 时公网网卡依旧碰不到，安全性不变。

**② 用 `DOCKER-USER` 链做限制**（端口必须 `0.0.0.0` 暴露时的兜底）

```bash
# 只允许内网网段访问 Docker 发布的端口，其余 drop
iptables -I DOCKER-USER -i eth0 ! -s 10.0.0.0/8 -j DROP
```

> 验证当前是否真的对公网开着：`ss -tlnp | grep -E ':(8000|8080|8088)'`，看 Local Address 是 `127.0.0.1:` 还是 `0.0.0.0:`。后者 = 对公网开放，需立即收紧。

---

## 5. 远程 SDK / CLI 安全访问

SDK（`packages/python-sdk`）本质是 HTTP 客户端：所有请求拼在 `base_url + /api/v1`，认证注入 `Authorization: Bearer <api_key>`（`src/ai_annotation/_http.py`）。从**非平台所在机器**调用时，三件事必须做对：

### 5.1 base_url 走 HTTPS 域名，不走 IP:8000

```bash
# ✅ 远程：走外层反代的 HTTPS
export AAP_BASE_URL=https://app.example.com
export AAP_API_KEY=ak_...

# ❌ 绝不：明文直连后端端口，Key 裸奔公网
export AAP_BASE_URL=http://1.2.3.4:8000
```

部署侧务必把 `app.example.com` 的 443 指向外层反代，反代再转到 web 容器的 8088，由 web 容器内 Nginx 把 `/api/` 和 `/ws/` 转给 `api:8000`（[生产部署 §3](/ops/deploy/docker-compose) 有 nginx 示例）。

### 5.2 API Key 管理

- Key 在 web 端「我的 API Keys」或 `aap login` 后通过 `me/api-keys` 端点生成。
- 落盘在调用方机器 `~/.config/ai-annotation/config.toml`，权限 `0600`（`src/ai_annotation/config.py`）。**不要提交进 git、不要共享该文件**。
- 怀疑泄露时立即 `rotate` / `delete`（SDK 支持 `me/api-keys/{id}/rotate`）。

### 5.3 ⚠️ presigned URL 的对外可达性（远程最隐蔽的坑）

上传下载走**预签名 URL 直连对象存储**，不经过平台 API：

- 上传：`upload-init` 拿到预签名 PUT URL → SDK 用不带平台 auth 的裸客户端直接 PUT 到 MinIO/S3（`_http.py:put_presigned`）。
- 下载：后端返回绝对 URL 时同样裸客户端直连（`_http.py:stream_download`）。

如果后端签发的 URL 指向 MinIO **内网地址**（`http://minio:9000` 或内网 IP），远程机器**连不上**——表现为「API 调用成功，但上传/下载卡住或连接失败」。

修复：后端配 `MINIO_PUBLIC_URL` 为对外可解析、可访问的 HTTPS 地址（与容器内 `MINIO_ENDPOINT` 是两层视角，详见[生产部署 §2.3](/ops/deploy/docker-compose)）。`MINIO_USE_SSL` 控制的是 API / worker 到 `MINIO_ENDPOINT` 这一段：只有内部 endpoint 本身提供 TLS 时才设为 `true`，它不代替公网 URL 的 HTTPS 配置。

### 5.4 只读站点 Agent 的出站 HTTPS 边界

当数据目录位于中心无法主动入站访问的内网或远端机房时，可部署[只读站点 Agent](/ops/deploy/site-agents)（`aap-storage-agent`）：

- **零入站端口**：Agent 进程不监听任何本地或公网端口，仅向中心外层反代的 `443`（HTTPS origin）发起出站长轮询（`POST /api/v1/site-agents/commands/poll`）、分块上传与结果回执。数据服务器防火墙只需放行到中心 HTTPS 域名的出站 443。
- **严格 TLS 与无重定向**：Agent 强制校验中心 TLS 证书（内部私有 CA 通过本地 `0600` 配置中的 `ca_file` 指定），拒绝 HTTP 明文、拒绝任何 3xx 重定向，且不读取环境变量代理，防止中间人诱导分块上传流向非受控端点。
- **独立凭证域**：Agent 使用一次性配对换取的专用 Bearer token 与 `X-Agent-Session` 头访问 `/api/v1/site-agents/commands/*`，与用户 JWT / SDK API Key 完全隔离；中心反代无需为 Agent 额外暴露内部端口。

---

## 6. 纵深防御 checklist（上线前过一遍）

- [ ] 后端 8080 / web 8088 端口绑 `127.0.0.1:` 或内网 IP，**不是** `0.0.0.0`（prod compose 已默认绑回环，确认没被 `PROXY_BIND_HOST=0.0.0.0` 覆盖；`ss -tlnp` 验证）
- [ ] PG 5432 / Redis 6379 / MinIO 9000 **完全不向宿主发布**（prod 叠加文件 `ports: !reset []`；`docker compose ... config` 里这几个服务应无 `ports`，`ss -tlnp` 看不到对应监听）
- [ ] 如启用 ML profile，8001–8005 只对 worker 所在可信网络开放；不依赖默认宿主发布，用绑定覆盖、安全组或 `DOCKER-USER` 明确收紧
- [ ] 确认 ufw 没被 Docker 绕过（用 §4 的绑定姿势或 `DOCKER-USER` 链）
- [ ] 公网只开 443，外层反代终结 TLS（证书有效、HSTS 已开）
- [ ] `ENVIRONMENT=production`、`SECRET_KEY` 已换强随机、`CORS_ALLOW_ORIGINS` 显式列（这三项启动断言，见[安全模型 §6](/ops/security/)）
- [ ] `/metrics` 反代里限内网 `allow / deny`
- [ ] `MINIO_PUBLIC_URL` 指向对外可达的 HTTPS 地址；`MINIO_USE_SSL` 与内部 `MINIO_ENDPOINT` 的实际协议一致；MinIO 默认凭据已换
- [ ] Redis 设 `requirepass`（并同步 `REDIS_URL` 带密码）——纵深防御，即便端口不发布，共享宿主上 Redis 仍是经典横向移动目标
- [ ] mailpit 已禁用，改真实 SMTP
- [ ] 远程 SDK 调用方用 `https://` base_url，API Key 走 `0600` 配置文件、定期轮换
- [ ] 监控端口（9090/3001/9093）仅内网 / VPN 可达

---

## 相关

- [生产部署](/ops/deploy/docker-compose) —— 反向代理示例、环境变量、`MINIO_PUBLIC_URL` 细则
- [安全模型](/ops/security/) —— 平台内置的 JWT / RBAC / 限流 / 审计 / CSP / CORS
- [运行环境形态](/dev/concepts/runtime-environments) —— 开发 / 生产三态差异（含 `ENVIRONMENT` 代码行为）
- [部署拓扑](/dev/concepts/deployment-topology) —— 单机 / 分离 GPU 的物理形态
