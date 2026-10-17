---
title: 公开事件流与有界监听
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 公开事件流与有界监听

SDK 的 `client.streams` 提供六条固定 WebSocket 频道。它们独立于 HTTP 覆盖数；核心安装可以离线发现频道，连接时才需要 `ai-annotation-sdk[streams]`。CLI 监听还需 `cli` extra，TUI 已包含 WebSocket 依赖。

```bash
pip install 'ai-annotation-sdk[cli,streams]'
aap streams list --json
aap streams describe batch-events --json
aap describe streams.batch_events --json
```

| 方法                      | 路径                                  | 凭据与范围                   |
| ------------------------- | ------------------------------------- | ---------------------------- |
| notifications             | /ws/notifications                     | JWT；当前用户通知            |
| preannotation(project_id) | /ws/projects/{project_id}/preannotate | JWT 或 API key；当前项目访问 |
| batch_events(project_id)  | /ws/batches/project/{project_id}      | JWT 或 API key；当前项目访问 |
| prediction_jobs           | /ws/prediction-jobs                   | 管理员 JWT；全局预测进度     |
| video_tracker(job_id)     | /ws/video-tracker-jobs/{job_id}       | JWT；job 实际任务可见        |
| ml_stats                  | /ws/ml-backend-stats                  | 管理员 JWT 或 API key        |

这些路径在 `/ws` 下。token 由 SDK 百分号编码到 query，连接错误与 SDK 的 socket 日志不显示该凭据。JWT 专用频道会在连接前拒绝 `ak_` key；真正的当前账号、项目和任务授权仍由服务端判断。ML stats 订阅会启用服务端现有的 backend 统计轮询。

## Python 异步消费

```python
import asyncio
from ai_annotation import Client

PROJECT_ID = "<project-uuid>"

async def observe():
    with Client() as client:
        async with asyncio.timeout(30):
            async with client.streams.batch_events(PROJECT_ID) as events:
                async for event in events:
                    print(event.type, event.batch_id, event.from_status, event.to_status)
                    break

asyncio.run(observe())
```

每个方法返回 async context manager，事件通过 async for 读取；它不是可直接序列化的 JSON 结果。退出、异常或取消时关闭连接。事件模型保留新增字段和开放业务状态，batch 的 from／to 映射为 from_status／to_status，序列化别名仍是原字段。通知分别是业务 Notification 和 notifications.sync 提示；ML stats 复用 MLBackendStatsSnapshot。无来源／多来源 tracker 的 annotation_id 为 null，SDK 也将旧服务端的字符串 None 还原为空值。

五条频道每 30 秒发送 JSON ping，SDK 忽略该心跳；单项目 preannotation 没有该心跳。SDK 使用一次连接，不自动重连、refresh token、重新订阅或提交工作。异常 StreamError 保留安全的 close_code／HTTP handshake status 和稳定 code，不包含原始签名地址或事件输入。

## CLI 的观察窗口

```bash
aap streams batch-events "$PROJECT_ID" --max-events 20 --timeout 30
aap --error-format json streams video-tracker "$JOB_ID" --max-events 10 --timeout 15
```

监听总是每行输出一个 JSON 事件，不使用普通成功结果的 `--json`。默认最多 20 个事件或 30 秒；max-events 为 1–1000，timeout 为 0.1–3600 秒。达到任一边界退出 0，表示观察窗口结束，不表示目标任务完成，也不表示所有事件已收齐。关闭连接可能另外需要短暂清理时间。

连接或解码失败退出 1，保留已经输出的 NDJSON；结构化 stderr 的 kind 为 stream_error，附 code 和可用的 close_code/status。消费者按行处理 stdout，不把已收到的数据与失败错误拼成一个 JSON 文档。

## 恢复与事实来源

Pub/Sub 没有确认、重放游标或交付保证。握手可能被拒绝，连接期间权限变化也可能停止投递而不返回特定关闭码；保持连接不证明权限仍有效。断线后先重新读取对应 REST：

- notifications：读取通知列表及实际目标。
- preannotation／prediction_jobs：读取持久化 job／任务／预测；Celery task ID 不一定是 AsyncJob ID。
- batch_events：重新读取项目批次。状态事件是 type、batch_id、from、to、at；分派事件使用 batch_ids。
- video_tracker：读取 job 与候选 preview。frame_result 的实例 ID 可能只在处理窗口内有效，事件不表示候选已成为标注。
- ml_stats：读取 registry／health，之后再明确决定是否建立新连接。

没有通用 subscribe、unsubscribe 或 reauth 消息协议。离线 describe 包含各频道 schema、鉴权和恢复入口；部署协议见 [WebSocket](../../api/guides/websocket)。
