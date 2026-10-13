---
audience: [dev]
type: how-to
since: v0.1.0
status: stable
last_reviewed: 2026-09-19
---

# How-to：调试 WebSocket

> 本文记录 WS 的现行端点、开发态直连规则与排障方法。协议字段见 [WebSocket 协议](../reference/ws-protocol)。

## WS 拓扑总览

后端 WS 端点全部注册在 `apps/api/app/api/v1/ws.py` 的 `router = APIRouter()`，由 `apps/api/app/main.py:108` `app.include_router(ws_router)` **无 prefix** 挂载。所以浏览器侧 URL 必须是 `/ws/<name>`，**不要写 `/api/v1/ws/<name>`**：

| 端点                               | 用途                                  | 鉴权                 | 前端 hook                       |
| ---------------------------------- | ------------------------------------- | -------------------- | ------------------------------- |
| `/ws/notifications`                | 单用户通知推送                        | JWT token            | `useNotificationSocket.ts`      |
| `/ws/prediction-jobs`              | 全局预标 job 进度 (admin only)        | JWT + role           | `useGlobalPreannotationJobs.ts` |
| `/ws/projects/{id}/preannotate`    | 单项目预标进度条                      | JWT/`ak_` + 项目访问 | `usePreannotation.ts`           |
| `/ws/batches/project/{project_id}` | 项目 batch 状态同步                   | JWT/`ak_` + 项目访问 | `useBatchEventsSocket.ts`       |
| `/ws/video-tracker-jobs/{job_id}`  | 视频 tracker 运行与候选审阅事件       | JWT + task 可见性    | `useVideoTrackerJobs.ts`        |
| `/ws/ml-backend-stats`             | PerfHud GPU/容器实时指标 (admin only) | JWT + role           | `useMLBackendStats.ts`          |

production：6 个端点都走 nginx `/ws/` location 反代到 `api:8000`（[infra/docker/nginx.conf](https://github.com/anthropics/ai-annotation-platform/blob/main/infra/docker/nginx.conf)）。

> 项目作用域频道（`preannotate` / `batches`）以 query `token` 传 JWT 或 `ak_` API key，并要求当前账号在该 `project_id` 上有实际访问；凭据无效 / 撤销或无项目访问在 accept 前 close `1008`。每个受保护 payload 前都重新校验当前权限，撤销后立即停止该项目的受限投递，**没有 5 秒宽限**。直连排查时带上 `?token=...`，不要假设它们“无需鉴权”，也不要把 project UUID 当作访问控制。

## 常见问题

### 1. 浮窗一直显示"正在连接 /ws/...", DevTools Network 看 ws 请求 "已完成 0.0kB"

**根因**：WS 在 server accept 之前 close（鉴权失败 / 路径 404）。Starlette 在 accept 前 close 会以 HTTP 403/404 拒绝握手，浏览器 onclose code 是 1006（abnormal closure），看不到具体原因。

**定位**：

```js
// 在浏览器 DevTools Console 跑（替换 token 取自 localStorage）
const token = localStorage.getItem("token");
console.log(
  "exp:",
  JSON.parse(atob(token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))).exp,
  "now:",
  Math.floor(Date.now() / 1000),
);
// 直连 :8000 绕 vite proxy
const ws = new WebSocket(
  `ws://localhost:8000/ws/ml-backend-stats?token=${encodeURIComponent(token)}`,
);
ws.onopen = () => console.log("OPEN");
ws.onmessage = (e) => console.log("msg:", e.data.slice(0, 200));
ws.onclose = (e) => console.log("CLOSED code=", e.code, "reason=", e.reason);
```

观察 `exp` vs `now`：差为正且 token 已过期是最常见原因。

**修复**：

- token 过期 → logout / 清 `localStorage.token` 重新登录
- role 不匹配（admin-only 端点）→ 切 admin 账号
- URL 写错 → 检查 `/ws/<name>` 不带 `/api/v1` 前缀

### 2. WS 卡 CONNECTING 永不返回 onopen / onclose

**根因**：vite 6 的 `/ws` proxy 在多个 ws hook 并发 upgrade 时偶发卡死（vite 内部 http-proxy ws 模式 race condition），单个 WS 通常 OK，2+ 个并发就有概率重现。

**定位**：

```bash
# 直连后端 :8000 绕过 vite proxy 验证后端 OK
curl -s http://127.0.0.1:8000/health  # API 在线
# Console 跑上面的直连脚本（端口 8000 而非 3000）
```

如果直连 :8000 能拿到 OPEN + msg，但通过 :3000 vite proxy 卡 CONNECTING，就是 vite proxy 问题。

**修复**：前端统一通过 `buildWsUrl()` 选择连接地址。浏览器开发态默认走页面同源 `/ws` 代理，本机、LAN、Tailscale 与 SSH LocalForward 因而使用相同路径；Vite 再按 `API_PROXY_TARGET` 转发到对应 API。只有刻意绕过 Vite proxy 时才用 `VITE_WS_HOST` 显式覆盖：

```ts
const host = import.meta.env.VITE_WS_HOST || window.location.host;
const url = `${proto}://${host}/ws/<name>?token=...`;
```

新增 ws hook 时**沿用 `buildWsUrl()`**，不要在 hook 内重复拼接 host。

### 3. uvicorn `--reload` 改完 .py 后卡住不重启

**症状**：编辑 `app/workers/celery_app.py` 等文件后，uvicorn 终端打印：

```
WARNING:  WatchFiles detected changes in '...'. Reloading...
INFO:     Shutting down
INFO:     connection closed
INFO:     Waiting for background tasks to complete. (CTRL+C to force quit)
```

然后无限期卡在最后一行，新代码永不加载。

**根因**：uvicorn graceful shutdown 等所有 background tasks 完成。浏览器持有的 WS 长连接是 background task，永远不会"完成"。

**修复**：开发命令已带 `--timeout-graceful-shutdown 3`，并在 shutdown 关闭 WS Redis pool。若仍被外部任务卡住，再重启开发 API 进程：

```bash
ss -lntp | grep :8000           # 找老 uvicorn worker pid
kill -9 <pid>                    # 强杀
pnpm dev:api
```

### 4. 后端 WS 端点 def 改完了但调不到（404）

**根因**：开发态 FastAPI 跑宿主机，不是 compose 的 `api` service；生产叠加 compose 的 API 镜像才是冻结源码。开发 worker 的 `./apps/api:/app` 为 bind mount，但 Celery 没有自动重载。

```bash
# 开发：改 WS API 后让 uvicorn reload；改 worker 代码后重启实际消费队列的 worker。
docker compose restart celery-worker-gpu

# 生产：镜像内代码变更需要重建并重启。
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

**开发路径**：本地 uvicorn `--reload` 跑 API，compose 跑基础设施与各类 worker。改默认 / media / cleanup / audit worker 代码重启 `celery-worker`；改分区维护或统计刷新代码重启 `celery-worker-maintenance`；改 GPU 预标或视频 tracker 代码重启 `celery-worker-gpu`；CPU 预标和导出分别重启 `celery-worker-cpu`、`celery-worker-export`。

详见 CLAUDE.md §7 Docker rebuild vs restart。

### 5. Celery beat 发任务但 worker 不消费

**症状**：`docker logs celery-beat` 看到 `Sending due task ...` 每秒一次，但 `docker logs celery-worker` 没有 `received` / `succeeded`。Redis `LLEN celery` 很大，`LLEN default` 是 0。

**根因**：task 没在 `task_routes` 显式声明，落到 `task_default_queue`；若该值是 Celery 内置默认 `celery`，而当前 default worker 启动为 `-Q default,media,cleanup,audit`，就无人消费该队列。

**修复**：把默认队列设为 default worker 实际订阅的 `default`，未路由任务自动落到被消费的队列：

```python
# apps/api/app/workers/celery_app.py · celery_app.conf.update(...)
task_default_queue="default",   # 不再是 Celery 内置的 "celery"
```

只有需要专用队列（`ml` / `media` / `gpu` / `cleanup` / `audit` / `maintenance`）的 task 才在 `task_routes` 显式路由；其余兜底任务无需逐个补 route。排查时 `redis-cli llen celery` 看死队列是否堆积。队列与订阅模型详见 [backend-infrastructure 的「队列与订阅模型」一节](../concepts/backend-infrastructure)。

### 6. asyncpg `cannot perform operation: another operation is in progress`

**根因**：Celery prefork pool（`--concurrency=N` N>1）+ 全局 `async_session` engine 共享。fork 子进程继承父进程 engine 后 connection 被多 worker 同时使用触发 asyncpg 内部断言。

**修复**：高频 / 并发 task 用 per-task engine 模式（与 `tasks._run_batch` 一致）：

```python
async def _my_async_task():
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with SessionLocal() as db:
            ...
    finally:
        await engine.dispose()
```

低频 task（每分钟以下 + concurrency=1 工人）可继续用全局 `async_session()`。

## 检查清单：新增 WS 端点时

后端：

- [ ] `apps/api/app/api/v1/ws.py` 注册路径用 `/ws/<name>` 形式
- [ ] accept 之前 close 走 1008，避免无 close frame 的 abnormal close
- [ ] 长连接里不持有全局 DB engine（per-task engine 或 NullPool）

前端：

- [ ] hook 使用 `buildWsUrl()`；Vite 后端端口使用 `API_PROXY_TARGET`，仅直连覆盖使用 `VITE_WS_HOST`
- [ ] URL 是 `/ws/<name>` 不带 `/api/v1`
- [ ] onclose code 1008 / 1006 区分鉴权失败 vs 网络断；不要静默兜底（v0.6.9 通知 bug 教训）
- [ ] 加 e2e 或 hook 单测覆盖 URL 派发，避免 14 个月无人发现的二次重演

运维：

- [ ] nginx.conf 的 `location /ws/` 已含 Upgrade / Connection header（见 v0.9.11 nginx.conf）
- [ ] 开发态 worker 代码重启实际消费队列的 worker；生产镜像代码变更用 production compose 重建

## 相关 ADR / 文档

- 后端 ws 端点协议 → [架构文档 frontend-layers.md](../concepts/frontend-layers.md)
- PerfHud 的 WS 实时推送架构 → [架构文档 perfhud.md](../concepts/perfhud.md)
- 通知系统设计 → 评估 v0.6.9 通知 WS / 30s 兜底 fallback 设计文档（暂无独立 ADR）
