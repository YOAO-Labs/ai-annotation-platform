---
audience: [super_admin]
type: how-to
since: v0.9.0
status: stable
last_reviewed: 2026-06-10
---

# 失败预测排查

`async_jobs.status='failed'` 的 `batch_predict` / `prediction_retry` 记录，以及 `failed_predictions` 明细行，就是“AI 预标跑过但失败的”。项目内查看、取消、重试和候选数据边界见[AI 任务与失败恢复](../workflows/failed-prediction-recovery)；本页只补充超管的跨项目诊断、失败项管理和监控信息。

## 入口

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/superadmin/failed-predictions/list.png — /ai-pre/jobs?status=failed 列表（状态筛选 + 重试/放弃/显示已放弃 toggle） [manual] -->

- 超管 / 项目管理员：`/ai-pre/jobs?status=failed`（图像 tab 加状态过滤器）
- 仪表盘快捷卡：Dashboard → 失败预测卡片 → 点击直跳

> **注意**：旧路由 `/model-market` → Failed Predictions tab 已在 v0.9.12（BUG B-14）移除。旧书签访问时前端自动跳转 `/ai-pre/jobs?status=failed`。

## 失败常见根因

| 错误信号                                     | 看哪                                                                                                  |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `Connection refused` / `Name resolution`     | [容器网络与 loopback](../../dev/troubleshooting/container-networking)                                 |
| `TypeError: ... unexpected keyword argument` | [Docker rebuild vs restart](../../dev/troubleshooting/docker-rebuild-vs-restart)（worker stale code） |
| ML Backend `422` / `400`                     | prompt 格式问题；工作台一键预标会自动用项目 alias 拼 prompt，避免空 prompt 导致 DINO 422              |
| `Timeout`                                    | ML Backend 推理太慢；看监控 P95                                                                       |
| `403` / `401`                                | API Key 配置错误                                                                                      |
| 前端看不到候选但 job=succeeded               | [Schema 适配器陷阱](../../dev/troubleshooting/schema-adapter-pitfalls)                                |

## 排查流程

1. **拿 job_id**：从失败 tab 列表点击展开
2. **看 error 字段**：普通 Backend 失败显示异常类型与消息；GPU 仲裁拒绝直接显示稳定 `gpu_*` 错误码和干净消息。如需区分容量、并发、drain 超时或账本不可用，再对照异步作业结果中的 `gpu_arbiter_failures`（含 HTTP 状态和可选重试窗口）
3. **对照 async_jobs 时间戳**：
   - `created_at` → `started_at` 间隔大 → broker / worker 拥堵
   - `started_at` → `completed_at` 间隔大 → 推理超时
4. **看 worker 日志**：`docker logs ai-annotation-platform-celery-worker-1 --since 1h | grep <job_id>`
5. **看 ML Backend 日志**：grounded-sam2-backend 在 `/metrics` + 容器日志
6. **复现**：拿 job 的 prompt + 一张样本图，直接 `curl` ML Backend `/predict`

## 重试与放弃

失败 job 不会自动重试（避免雪崩）。支持以下操作：

### 重试单条

在失败列表中点击「重试」，后端投递 `prediction_retry` Celery 任务（HTTP 202）。启动通知类型为 `failed_prediction.retry.started`；终态记录在统一作业 `async_jobs`，并沿用 `job.completed` / `job.failed` 通知。

**重试上限**：每条 `failed_prediction` 最多重试 **3 次**，由 `apps/api/app/services/prediction_retry.py` 的 `MAX_RETRY_COUNT` 定义。超限后调用返回 `HTTP 409`；放弃和恢复不会重置已用次数。

### 批量重跑

- 项目侧 `/ai-pre`：选回该批次 + 同 prompt + 「跑预标注」
- 重置批次 `pre_annotated` 标记位（项目设置 → 数据 → 重置）后再跑

### 放弃/恢复

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/superadmin/failed-predictions/dismiss-restore.png — 显示已放弃后含「已放弃」badge + 恢复按钮 [manual] -->

- **放弃**：在列表点「放弃」→ `POST /admin/failed-predictions/{id}/dismiss`，软删除（写 `dismissed_at` 时间戳）。放弃后该行从默认列表消失；前端「显示已放弃」开关可重新显示。
- **恢复**：在已放弃行点「恢复」→ `POST /admin/failed-predictions/{id}/restore`，清空 `dismissed_at`，该行重回默认列表；未达到次数上限且未在排队时可再次重试。
- 重复 dismiss 幂等（不更新 `dismissed_at`）；重复 restore 也幂等。
- 审计事件：`failed_prediction.dismissed` / `failed_prediction.restored`。

## 大量失败如何排查

- 模型市场 → Health Overview → 看错误率峰值
- 同一时间窗集中失败 → 多半是 ML Backend 自身挂了，重启容器即可
- 跨多个 backend 同时失败 → 看 worker 容器、Redis、网络

## 监控告警

平台暴露的 Prometheus 指标中，与预测失败相关的是 `ml_backend_request_duration_seconds{backend_id, outcome}` Histogram（`outcome="error"` 代表 ML backend 调用出错，`apps/api/app/observability/metrics.py:16`）。**不存在**名为 `prediction_job_failed_total` 的 Counter 指标。可通过 `rate(ml_backend_request_duration_seconds_count{outcome="error"}[5m])` 等 PromQL 设定告警阈值，详见 [可观测性](../../ops/observability/)。
