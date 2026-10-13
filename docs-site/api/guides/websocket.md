---
audience: [dev]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-09-19
---

# WebSocket

完整协议见 [WebSocket 协议](../../dev/reference/ws-protocol)。每个 WebSocket URL 都是一个固定的 Redis Pub/Sub 订阅；当前服务**没有**通用 `/ws` 入口，也不支持在同一连接发送 `subscribe`、`unsubscribe` 或 `reauth` 消息。

## 端点

| URL                                           | 鉴权                            | 用途                                        |
| --------------------------------------------- | ------------------------------- | ------------------------------------------- |
| `/ws/notifications?token=<jwt>`               | JWT                             | 当前用户的通知与异步任务事件。              |
| `/ws/projects/{project_id}/preannotate`       | JWT 或 `ak_` API key + 项目访问 | 单项目批量预标进度。                        |
| `/ws/batches/project/{project_id}`            | JWT 或 `ak_` API key + 项目访问 | 项目 batch 状态变化。                       |
| `/ws/prediction-jobs?token=<jwt>`             | `super_admin` / `project_admin` | 全局预标任务摘要。                          |
| `/ws/video-tracker-jobs/{job_id}?token=<jwt>` | JWT + job 所属 task 可见性      | 单条视频 tracker job 的运行与候选审阅事件。 |
| `/ws/ml-backend-stats?token=<jwt-or-api-key>` | `super_admin` / `project_admin` | ML backend 运行时指标。                     |

浏览器中的 DEV 与生产连接都使用页面同源 `ws(s)://<page-host>`；DEV 由 Vite 将 `/ws` 转发到 API。手工调试后端时可以直连 `ws://localhost:8000`。这些路径不在 `/api/v1` 下。

## 连接示例

```js
const token = "<access-token>";
const socket = new WebSocket(
  `ws://localhost:8000/ws/video-tracker-jobs/${jobId}?token=${encodeURIComponent(token)}`,
);

socket.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  if (event.type !== "ping") console.log(event);
};
```

需要鉴权的端点在握手时校验；失败会在 accept 前以 `1008 Policy Violation` 关闭。浏览器不能为 WebSocket 设置 `Authorization` header，因此 token 使用 query 参数。部署的 access log 必须脱敏 `token` 参数。

项目作用域频道（`preannotate` / `batches`）同样以 query 参数 `token` 鉴权，值可以是 JWT 或 `ak_` 开头的 API key：

```text
ws://<api-host>/ws/projects/{project_id}/preannotate?token=<encoded-token>
ws://<api-host>/ws/batches/project/{project_id}?token=<encoded-token>
```

服务端要求当前账号处于启用状态，并在该 `project_id` 上有实际访问（`super_admin` / 负责人 / 有效成员）；凭据无效或已撤销、无项目访问或项目 UUID 不匹配会在 accept 前以 `1008 Policy Violation` 关闭。**没有 5 秒鉴权宽限**：每个受保护 payload 之前都按当前权限重新校验，撤销后立即停止投递该项目的受限事件。不要把可猜测的 project UUID 当作访问控制。

## 事件与恢复

除单项目预标进度频道外，长生命周期频道每 30 秒发送 `{"type":"ping"}`，客户端无需回复。预标进度频道以 batch 进度消息为主。发布消息没有交付保证，断线后应从对应 REST API 重新读取状态：

- 通知：`GET /api/v1/notifications`。
- 批量预标：对应 job / prediction API。
- 视频 tracker：`GET /api/v1/tasks/{task_id}/video/tracker-jobs/reviewable`，再读取每条 job 的 `/preview`；候选接受前不在 annotation 中。

视频 tracker 的 `job_started`、`job_progress`、`frame_result`、`job_completed`、`job_failed`、`job_cancelled`、`job_accepted` 和 `job_discarded` 的完整语义见 [Video Tracker Jobs](./video-tracker-jobs) 与 [WebSocket 协议](../../dev/reference/ws-protocol#6-wsvideo-tracker-jobsjob_id)。
