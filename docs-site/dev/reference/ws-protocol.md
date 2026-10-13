---
audience: [dev]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-07-29
---

# WebSocket 协议

> 适用读者：要在前端订阅平台实时事件、或在第三方系统消费平台推送的工程师。
>
> 平台实现：`apps/api/app/api/v1/ws.py`、`apps/api/app/services/notification.py`、`apps/web/src/hooks/{useNotificationSocket,usePreannotation,useReconnectingWebSocket}.ts`。

平台目前公开 6 个 WS 频道，全部通过 Redis Pub/Sub 串接：publisher 向 Redis 频道发消息，WS 端订阅后转发到客户端。具有持久状态的业务仍以 REST / Postgres 为真值，WS 只是「在线提示」快通道，不承担可靠队列语义。

<ExcalidrawDiagram
  src="/diagrams/shared/realtime/durable-notification-delivery.svg"
  alt="用户通知在业务事务中先 flush Postgres 行、再尝试发布 Redis PubSub，调用方之后才 commit 或 rollback；浏览器将 WebSocket 推送当作刷新提示并用 REST 读已提交真值"
  caption="通知的在线快路径、事务时序与两个失败窗口"
/>

---

## 1. 端点总览

| 频道                    | URL                                           | 鉴权                                                                                       | Redis 频道                                               | 用途                                                                                                                                       |
| ----------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| 用户通知                | `/ws/notifications?token=<jwt>`               | JWT (query param)                                                                          | `notify:{user_id}` (`notification.py:27`)                | 任务分配、AI 进度、导出完成、@提及 等                                                                                                      |
| 预标注进度（单项目）    | `/ws/projects/{project_id}/preannotate`       | JWT/`ak_`（query）+ 项目访问                                                               | `project:{project_id}:preannotate` (`ws.py`)             | 工作台单次自动预标注的逐 batch progress                                                                                                    |
| Batch 状态广播          | `/ws/batches/project/{project_id}`            | JWT/`ak_`（query）+ 项目访问                                                               | `project:{project_id}:batch` (`ws.py:112`)               | 项目级 batch 状态翻转事件（B-15），让标注员/admin 多端实时同步                                                                             |
| Prediction Jobs（全局） | `/ws/prediction-jobs?token=<jwt>`             | JWT (query, `super_admin` / `project_admin`)                                               | `global:prediction-jobs` (`ws.py:168`)                   | Topbar 徽章 + 切项目 toast 用，仅在 job 开始/结束/失败 3 时点带 `job_meta` 推一条                                                          |
| 视频 tracker job        | `/ws/video-tracker-jobs/{job_id}?token=<jwt>` | JWT (query)，并按 task 可见性校验                                                          | `video-tracker-job:{job_id}` (`video_tracker_runner.py`) | 单条 tracker job 的运行、候选暂存与人工决策事件                                                                                            |
| ML Backend Stats        | `/ws/ml-backend-stats?token=<jwt 或 ak_key>`  | JWT **或 `ak_` api_key**（query, `super_admin` / `project_admin`；SDK / TUI 可用 api_key） | `ml-backend-stats:global` (`ws.py:246`)                  | Celery beat 每 1s 拉取 backend `/health` 快照后 publish；通过 `ml-backend-stats:subscribers` INCR/DECR 计数门控 — 0 订阅者时 beat 跳过实拉 |

base URL：`ws://<api-host>/ws/...` 或 `wss://...`。多数频道复用 `apps/web/src/hooks/useReconnectingWebSocket.ts`；用户通知为了 token refresh 使用 `useNotificationSocket.ts` 内的专用重连循环。浏览器中的 DEV 连接统一使用页面同源 `/ws` 代理，避免 LAN 或 SSH LocalForward 访问把浏览器自己的 localhost 误当成 API 主机。

---

## 2. `/ws/notifications`

### 2.1 鉴权

握手时必须在 query 参数携带 JWT：

```
ws://api.example.com/ws/notifications?token=eyJhbGciOi...
```

服务端 `decode_access_token` 校验 sub 字段（`ws.py:80-88`）。失败立刻关闭 frame，code = `1008 Policy Violation`。

> 为什么走 query 而不是 `Authorization` header：浏览器原生 `WebSocket` API 不允许设置自定义 header。如果前端用 `subprotocols` 走 token 也可以，但当前实现选 query 一致简单。query 参数可能进入反向代理或应用 access log，部署时必须对 `token` 脱敏。

> 项目作用域频道（`/ws/projects/{id}/preannotate`、`/ws/batches/project/{id}`）同样以 query `token` 传 JWT 或 `ak_` API key，但额外要求当前账号在 `project_id` 上有实际访问，并在每个受保护 payload 前重新校验；见 §3.1 / §4.1。

### 2.2 消息格式

服务端 → 客户端，JSON 文本帧。两种 type：

#### 业务消息（NotificationOut）

由 `NotificationService.notify` 写表后 publish（`notification.py:51-94`）：

```json
{
  "id": "<notification_uuid>",
  "type": "task.assigned" | "task.review_rejected" | "ai.preannotate_done" | "comment.mention" | ...,
  "target_type": "task" | "project" | "annotation" | ...,
  "target_id": "<uuid>",
  "payload": { ... },
  "created_at": "2026-05-06T08:30:00+00:00"
}
```

`type` 是开放枚举，由各业务模块定义（grep `NotificationService` 调用点可枚举）。前端不强校验未知 type，但只对已注册 type 显示 toast / 跳转。

#### 心跳（系统消息）

每 30s 服务端推一帧（`ws.py:33-43`）：

```json
{ "type": "ping" }
```

客户端**不需要**响应，只用来保活——防止反向代理（nginx 默认 60s `proxy_read_timeout`、AWS ALB 默认 60s idle）主动断连。前端 `useNotificationSocket` 收到 `type=="ping"` 时直接忽略，不触发 `invalidateQueries`。

#### 同步控制事件（`notifications.sync`）

同一账号在另一会话完成已读 / 删除 / 清空或修改通知偏好后，服务端在**业务事务提交成功后**发布：

```json
{ "type": "notifications.sync", "reason": "read" | "deleted" | "preferences" }
```

客户端处理：`read` / `deleted` 只失效通知查询（`["notifications"]`）；`reason=preferences` 额外失效 `["notification-preferences", userId]`，并在新一轮偏好数据落地前抑制瞬时提醒。`notifications.sync` 永远不产生 toast，也绝不会由它创建通知或改变未读数。

#### 连接生命周期与瞬时提醒

`useNotificationSocket` 挂在应用根组件（`App.tsx`），主界面与全屏标注 / 审核工作台共用一条连接，路由切换不拆链；登出、替换账号或刷新 token 时由 hook 清理并重连。连接打开 / 重开都会先失效通知查询，补拉断线期间的持久化状态。

业务消息除失效查询外，还按偏好决定是否在**可见标签页**弹出瞬时提醒：该类型的接收（`in_app`）与弹出（`toast`）都开启才弹，1 秒内的多条重要事件合并为一条 toast，按通知 ID 去重（最多 200 条）。偏好查询不可用或刚被 `reason=preferences` 标记失效时抑制提醒，只更新计数与列表。

### 2.3 可靠性 — 断线兜底

WS 不保证 at-least-once，当前也没有 transactional outbox。`NotificationService.notify()` 的实际顺序是：

1. 检查用户的 `in_app` 偏好；被静音时不写表也不 publish。
2. `db.add()` 并 `flush()` 通知行，但不在 service 内 commit。
3. 立即尝试 publish Redis；失败只记 warning，不中断调用方事务。
4. 调用方之后才 commit 或 rollback。

这产生两个非事务窗口：publish 失败而事务提交时，在线提示缺失但 REST 能补齐；publish 已成功而事务后续回滚时，客户端可能短暂收到数据库中不存在的提示。因此客户端应把 push 视为「可以刷新了」，再通过 `GET /api/v1/notifications` 读已提交真值：

- 默认前端 30s 一次轮询（即使 WS 在线），回前台或网络重连时立即补拉一次。
- 收到业务 push 时 `invalidateQueries(["notifications"])`。
- 通知 socket 连接打开 / 重开时也会失效通知查询；30s REST 轮询仍是断线补齐保障。

读 / 删除 / 清空与偏好修改不经过这条快路径：它们的 `notifications.sync` 事件
由 API handler 在 `db.commit()` 成功后发布（见上），不再有提交前发布的窗口。

如果新业务要求「只在提交后推送」或可重试投递，需引入 after-commit hook / transactional outbox，不能把现有 publish 当作这类保证。

---

## 3. `/ws/projects/{project_id}/preannotate`

### 3.1 鉴权

握手时必须以 query 参数携带 `token`，值可以是 JWT 或 `ak_` 开头的 API key：

```
ws://api.example.com/ws/projects/{project_id}/preannotate?token=...
```

服务端要求当前账号启用并在该 `project_id` 上有实际访问（`super_admin` / 负责人 / 有效成员）；凭据无效 / 已撤销、无项目访问或 project UUID 不匹配时在 accept 前 close `1008 Policy Violation`。**没有 5 秒鉴权宽限**：每个受保护 payload 之前都重新校验当前权限，成员被撤销后立即停止该项目的受限投递。不要把可猜测的 project UUID 当作访问控制。

### 3.2 消息格式

由 `app/workers/tasks.py:batch_predict` 在每 batch 结束时 publish：

```json
{
  "current": 12,
  "total": 50,
  "status": "running" | "completed" | "error",
  "error": "string"            // status="error" 时携带
}
```

进度 100% 时再发一帧 `status="completed"` 后频道结束。前端 `usePreannotationProgress`（`useNotificationSocket.ts:50-102` 区域内的同模块 hook）据此驱动进度条；收到 completed/error 后断开 WS。

### 3.3 心跳

此频道**没有心跳**——预标注任务通常 < 5 分钟，且每 batch 都会 push 一帧消息天然保活。如果你的 backend 单 batch 推理 > 60s，需要在前端 LB / nginx 把 `proxy_read_timeout` 调高（推荐 ≥ 120s）或者参考 `/ws/notifications` 的心跳模式补丁。

---

## 4. `/ws/batches/project/{project_id}`

### 4.1 鉴权

与 `/ws/projects/{id}/preannotate` 一致：query `token` 传 JWT 或 `ak_` API key，并要求当前账号在该 `project_id` 上有实际访问；凭据无效 / 已撤销或无项目访问时在 accept 前 close `1008`。每个受保护 payload 前都重新校验当前权限，撤销成员后立即停止其该项目的受限投递，不影响该账号其他已授权频道；**没有 5 秒宽限**。不要把可猜测的 project UUID 当作访问控制。

### 4.2 消息格式

`BatchService.transition()` / `check_auto_transitions()` 在改 `TaskBatch.status` 后调 `publish_batch_status_change()` 推 `batch.status_changed`：

```json
{
  "type": "batch.status_changed",
  "batch_id": "<uuid>",
  "project_id": "<uuid>",
  "from_status": "in_progress",
  "to_status": "in_review",
  "actor_id": "<user_uuid>"
}
```

前端 `useBatchEventsSocket` 收到后 `queryClient.invalidateQueries(["batches", projectId])` 拉最新列表。

### 4.3 心跳

30s ping，同 `/ws/notifications`。

---

## 5. `/ws/prediction-jobs`（全局）

### 5.1 鉴权

JWT query param，且 `role` 必须是 `super_admin` 或 `project_admin`，否则握手前 close `1008`。

> 与 `/ws/projects/{id}/preannotate` 的关键区别：本端点**跨项目**，仅在 job 开始 / 结束 / 失败 3 时点带 `job_meta` 推一条；用于 Topbar 徽章 + 切换项目 toast。逐 batch 进度仍走单项目频道。

### 5.2 消息格式

由 worker 的 `_publish_progress` 在带 `job_meta` 时同时 publish 到 `global:prediction-jobs`：

```json
{
  "job_id": "<uuid>",
  "project_id": "<uuid>",
  "project_name": "演示项目-A",
  "status": "running" | "completed" | "failed" | "cancelled",
  "started_at": "...",
  "finished_at": "..."
}
```

### 5.3 心跳

30s ping。

---

## 6. `/ws/video-tracker-jobs/{job_id}`

### 6.1 鉴权

JWT query param，并按 task 可见性二次校验：服务端用 `decode_access_token` 拿 `user_id`，再用 `_assert_task_visible(task, user)` 检查角色 + 可见域；任一失败 close `1008`。

### 6.2 事件序列

publisher: `apps/api/app/services/video_tracking/runner.py`；channel: `video-tracker-job:{job_id}`。

```mermaid
stateDiagram-v2
    [*] --> job_started
    job_started --> processing
    state processing {
        [*] --> step
        step --> step: frame_result (每帧)
        step --> step: job_progress (窗口结束)
    }
    processing --> job_completed: 候选已暂存，pending_review
    processing --> job_failed: 出错（带 error）
    processing --> job_cancelled: DELETE / cancel_requested_at
    job_completed --> review: GET preview
    job_cancelled --> review: tracking 且有部分 staged_result
    job_cancelled --> [*]: 无候选 / correction 取消
    review --> job_partially_reviewed: 决定子集后仍有未决
    job_partially_reviewed --> review: 重拉 preview + revision
    review --> job_accepted
    review --> job_discarded
    job_accepted --> [*]
    job_discarded --> [*]
    job_failed --> [*]
```

文字版（事件按时间先后）：

```
job_started                            # 一次
  ↓
(job_progress + frame_result)*         # 多次，frame_result 与 job_progress 并发
  ↓
job_completed | job_failed | job_cancelled       # 推理阶段终止
  ↓（completed / tracking 且有部分结果的 cancelled）
job_partially_reviewed*                         # 局部决定后仍有未决
  ↓
job_accepted | job_discarded                    # 未决集合清空
```

事件类型与触发点：

| 事件                     | 来源                      | 含义                                                   |
| ------------------------ | ------------------------- | ------------------------------------------------------ |
| `job_started`            | `video_tracker_runner.py` | tracker 进程已起，开始处理                             |
| `job_progress`           | `video_tracker_runner.py` | 阶段性进度更新（窗口/帧/检查点）                       |
| `frame_result`           | `video_tracker_runner.py` | 单帧推理结果，包含框/掩码 payload                      |
| `job_completed`          | `video_tracker_runner.py` | 正常结束，结果已暂存为待审候选，尚未写入 annotation    |
| `job_failed`             | `video_tracker_runner.py` | 出错终止，带 `error` 字段                              |
| `job_cancelled`          | `video_tracker_runner.py` | 用户取消或外部信号中止；若已有结果，可携带部分待审候选 |
| `job_partially_reviewed` | `video_tracker_runner.py` | HTTP 局部决策已提交，`staged_result` 仍有未决候选      |
| `job_accepted`           | `video_tracker_runner.py` | 用户接受候选，结果已写入源轨迹 / 新实例轨迹            |
| `job_discarded`          | `video_tracker_runner.py` | 用户丢弃候选，committed annotation 未改变              |

`frame_result` 是运行期 live event，实例 id 可能仍是窗口内局部值；最终审阅必须以 `GET /video-tracker-jobs/{job_id}/preview` 返回的 staged result 为准。当前 Web 前端通过 HTTP `/decisions` 或整批兼容 accept / discard 主动完成决策，不依赖同一页面持续监听 `job_partially_reviewed / job_accepted / job_discarded`。

> 旧版文档曾列出 `queued / window_progress / window_completed` 等事件，**这些事件在当前实现中不存在**；如有依赖需迁移到上表中的事件名。

### 6.3 心跳

30s ping。

---

## 7. `/ws/ml-backend-stats`（PerfHud）

### 7.1 鉴权

JWT query param，`role ∈ {super_admin, project_admin}`，否则 close `1008`。

### 7.2 订阅计数门控

服务端在 `subscribe` 后立刻 `INCR ml-backend-stats:subscribers`，断开时 `DECR`（异常退出场景兜底 `max(0, ...)`）。Celery beat `publish_ml_backend_stats` 每 1s 启动前读这个键：**0 订阅者时直接 skip**，避免每秒空转 GPU 探活。

### 7.3 消息格式

`ml-backend-stats:global` 上是所有 `is_active=true` ML backend 的 `/health` 快照列表：

```json
[
  {
    "backend_id": "<uuid>",
    "name": "grounded-sam2",
    "status": "healthy" | "degraded" | "down",
    "meta": { /* health_meta 见 ml-backend-protocol.md */ }
  }
]
```

### 7.4 心跳

30s ping。

---

## 8. 前端重连策略

`apps/web/src/hooks/useReconnectingWebSocket.ts` 是多数 WS 频道的通用基础：

- 初始重连间隔 **1s**，每次失败 ×2，上限 **30s**（`useReconnectingWebSocket.ts:18,31`）
- 最多重试 **8 次**，超过后状态为 `failed`；URL 或 `enabled` 变化才会开启新一轮连接。
- onOpen 回调可用于 `invalidateQueries` 补齐断线期间的状态

通知频道是例外：`useNotificationSocket.ts` 自己维护无限次重连，间隔同样从 1s 指数增长到 30s，并在鉴权关闭码下先刷新 token；它不受通用 hook 的 8 次上限约束。新增自定义客户端时应按频道的可靠性与鉴权契约选择策略，同时保留有上限的指数退避，避免重连风暴。

### 8.1 鉴权过期重连

`useNotificationSocket` 在 `onclose` 收到 `1008`（policy violation）或 `4001`（自定义鉴权失败）时，主动调 `POST /auth/refresh` 用旧 token 换新 token：

```
ws.onclose code=1008
   ↓
authApi.refresh()         // 旧 token grace 期 7 天内有效
   ↓ success
authStore.setToken(new)
scheduleRetry()           // 用新 token 重连
   ↓ failure (401)
client.ts 已自动 logout()  // 路由层会跳 /login
```

关键点：

- **后端关闭码必须是 1008**（`apps/api/app/api/v1/ws.py:87` 用 `WS_1008_POLICY_VIOLATION`），其他 close code 走原有指数退避，不调 refresh。
- **同一次过期只调一次 refresh**：hook 内 `refreshing` flag 防止重连风暴打 `/auth/refresh` 限流（5/min）。
- **refresh 端点详细规约**：见 [ADR-0011](../adr/archive/0011-websocket-token-reauth)。

---

## 9. Redis ConnectionPool

服务端使用模块级共享 `ConnectionPool`（`ws.py:17-30`），`max_connections=200`。多副本部署时每副本 200 上限——如果你的 WS 副本数 ×200 接近 Redis 实例的 `maxclients`（默认 10000），调小 `max_connections` 或加 Redis 实例。

`close_redis_pool()` 在 lifespan shutdown 时带 2s timeout `disconnect(inuse_connections=True)`，让悬挂 WS 收到 abnormal closure（1006）后走自带的指数退避重连；进程退出后内核回收剩余 socket。

---

## 10. 扩展新频道（开发者 how-to）

新增一个 WS 频道大致 4 步：

1. **定义 Redis 频道命名**：放到对应 service 模块顶部（参考 `notification.py:27` 的 `channel_for(user_id)`）。
2. **写 publisher**：先定义持久化真值与事务边界，再调用 `r.publish(channel, json.dumps(payload))`。当前 `NotificationService.notify()` 是事务内 `flush` 后、调用方 `commit / rollback` 前 best-effort publish，因此客户端必须把推送视作刷新提示；需要可靠投递时应使用 after-commit 或 transactional outbox，不能只靠“先 flush 再 publish”。
3. **写 WS 端点**：`@router.websocket("/ws/...")`，accept → SUBSCRIBE → 转发循环 → finally UNSUBSCRIBE。复制 `ws.py:notifications_socket` 模板即可，注意：
   - 鉴权写在 `accept` 之前；失败用 `await websocket.close(code=1008)`，不要先 accept 再 close（会被 LB 当成正常关闭）
   - 长生命周期频道补 `_heartbeat_loop` 防 LB idle
   - 用模块级 `_get_redis_pool()`，不要每连接 `aioredis.from_url`（连接数会爆）
4. **写前端 hook**：多数频道基于 `useReconnectingWebSocket`；只有需要通知式 token refresh 等特殊契约时才单独实现。收到事件后重新读取持久化真值，不要把 Pub/Sub payload 当作唯一数据源。

加到本文档 §1 端点表，并在 PR 描述里附上抓包样本。

---

## 11. 关键文件索引

| 主题                        | 路径                                             |
| --------------------------- | ------------------------------------------------ |
| WS 端点                     | `apps/api/app/api/v1/ws.py`                      |
| Notification 服务 + publish | `apps/api/app/services/notification.py`          |
| Notification 表             | `apps/api/app/db/models/notification.py`         |
| 自动预标注 worker           | `apps/api/app/workers/tasks.py`                  |
| Notification 兜底 REST      | `apps/api/app/api/v1/notifications.py`           |
| 前端通知 hook               | `apps/web/src/hooks/useNotificationSocket.ts`    |
| 前端预标注 hook             | `apps/web/src/hooks/usePreannotation.ts`         |
| 前端重连基础                | `apps/web/src/hooks/useReconnectingWebSocket.ts` |
