---
audience: [project_admin, super_admin, developer]
type: reference
since: v0.10.16
status: stable
last_reviewed: 2026-09-10
---

# 异步任务（async_jobs）

平台把所有用户可见的长任务统一进 `async_jobs` 表，做为前端任务铃铛
（Topbar `JobsBell`）和 `/ai-pre/jobs` 历史页的统一数据源；视频追踪记录在该页的「视频」标签（`?tab=video`）。底层各类长任务（预标、视频追踪、审计归档、预测导入、数据集导入、导出）
按需保留各自专表作为 domain 真值（VideoTrackerJob 等），`async_jobs` 只记
最小元数据作为汇总索引。视频追踪的候选预览、接受和丢弃以 VideoTrackerJob 为准，仍在视频工作台完成；这种"双写双轨"让前端只需 polling 一个端点就能看到全部进行中的任务。

<!-- history: async_jobs started as a release slice and now describes the current long-job surface. -->

## kind 取值

下表列出常见类型与入口，其他领域作业还保留各自的执行和结果接口。

| kind                      | 触发位                                                                                   | 是否支持 cancel API                 |
| ------------------------- | ---------------------------------------------------------------------------------------- | ----------------------------------- |
| `batch_predict`           | 项目 / 批次预标按钮（AIPreAnnotate 或 ProjectDetailPanel）                               | ✅（协作取消）                      |
| `prediction_retry`        | 失败预测列表的重试按钮                                                                   | ❌                                  |
| `video_tracker`           | 视频工作台 tracker 触发                                                                  | ❌（走 video tracker 自身取消接口） |
| `audit_archive`           | Celery beat 每月 2 日 03:00 UTC                                                          | ✅                                  |
| `predictions_import`      | 外部 prediction 上传（[Import guide](./import)）                                         | ✅                                  |
| `dataset_import`          | 数据集连接器导入文件                                                                     | ✅                                  |
| `point_cloud_cross_frame` | 3D Scene 跨帧传播                                                                        | ✅（逐帧协作取消）                  |
| `create_tasks`            | 数据集关联项目且 item 数 > `TASK_CREATE_SYNC_THRESHOLD`（默认 2000）时，建 task 转入后台 | ❌                                  |
| `export`                  | 项目 / 批次导出                                                                          | ❌                                  |

## 端点

### `GET /api/v1/async-jobs`

按 `user_id` 过滤（super_admin 看全部）。

| Query        | 类型               | 说明                                                                                                          |
| ------------ | ------------------ | ------------------------------------------------------------------------------------------------------------- |
| `status`     | enum, repeatable   | `pending` / `running` / `completed` / `failed` / `cancelled`；可重复传入，如 `?status=pending&status=running` |
| `kind`       | string, repeatable | 上表 kind 字符串；可重复传入，如 `?kind=batch_predict&kind=prediction_retry`                                  |
| `project_id` | uuid               | 只看某个项目的任务                                                                                            |
| `search`     | string             | 匹配 payload 中的 `prompt` / `batch_display_id` / `model_key`                                                 |
| `limit`      | int (1-200)        | 默认 50                                                                                                       |
| `offset`     | int                | 默认 0                                                                                                        |

响应：

```json
{
  "items": [
    {
      "id": "01JX...",
      "kind": "batch_predict",
      "project_id": "uuid",
      "user_id": "uuid",
      "project_display_id": "PROJ-1",
      "project_name": "Demo Project",
      "status": "running",
      "progress_pct": 42,
      "payload": {
        "total_tasks": 100,
        "batch_display_id": "BATCH-1",
        "output_mode": "mask"
      },
      "result": {
        "failed_prediction_ids": []
      },
      "error_message": null,
      "celery_task_id": "celery-uuid",
      "started_at": "2026-05-19T10:00:00Z",
      "completed_at": null,
      "created_at": "2026-05-19T09:59:50Z",
      "updated_at": "2026-05-19T10:00:30Z"
    }
  ],
  "total": 1
}
```

### `GET /api/v1/async-jobs/{id}`

owner-scoped；非 owner（且非 super_admin）→ `403`，记录不存在返回 `404`。

任务铃、终态通知与历史页共用详情入口，按 ID 重新读取该次作业，展示项目或数据集、阶段、结果计数及可展开的失败原因。任务铃每页加载 20 条，支持继续加载更早记录。切换账号时清除当前详情及该账号的待处理操作。

数据集入口使用 `payload.dataset_id`，项目入口使用 `project_id`，打开前再次检查目标权限。导出只在已完成且 `result.download_url` 和 `result.expires_at` 均有效时提供下载；过期或缺少有效期时提示从项目列表重新导出。作业详情不为导入提供通用重试，只有满足已有重试条件的批量预标显示失败项重试。

### `POST /api/v1/async-jobs/{id}/cancel`

软取消。当前支持 `predictions_import`、`dataset_import`、`audit_archive`、`batch_predict`、
`point_cloud_cross_frame`、`point_cloud_quality`、`mask_qc`、`mask_repair`、
`mask_format_import`、`training_dataset` 和 `sample_evaluation`；其他 kind 返回
`400 not cancellable`。已终态（`completed` / `failed` / `cancelled`）调用返回 `409`，领域仍保留自身取消意图与终态策略。

成功响应由 `AsyncJobCancelResponse` 定义：`id` 为作业 UUID，`status` 为 `cancelled` 或 `cancel_requested`。后者只表示已保存停止意图，执行可能仍在收尾；客户端应继续查询作业/领域状态。HTTP 200 不代表进程已经退出。

跨帧 pending 请求直接保存 cancelled 与完整目标计数，HTTP 不强制进度 100；消息可能被撤销而不执行。运行中的请求写入取消意图，在下一帧边界停止，保留此前成功写入；worker 完成取消收尾时推进至 100。连接器导入则由 HTTP 立即保存 cancelled，worker 在文件边界停止并为已取消行回填最终计数，保留阶段进度而不强制 100；取消检查之前仍可能先枚举源文件。

`batch_predict` 使用协作取消：API 对 `pending` job 直接写 `cancelled`；对 `running` job 写
`payload.cancel_requested=true`，并对 Celery task 执行 `revoke(..., terminate=False)`。worker 不会强杀
进程，而是在下一条预测边界停止并写入取消结果：

```json
{
  "success_count": 12,
  "failed_count": 1,
  "done_count": 13,
  "skipped_count": 87,
  "cancelled_at_index": 13,
  "duration_ms": 42000,
  "total_cost": "0.0184"
}
```

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/api/v1/async-jobs/01JX.../cancel
# → 200 {"status": "cancelled", "id": "01JX..."}
```

### `POST /api/v1/async-jobs/{id}/retry-failed`

`batch_predict` job 如果在结果里记录了 `failed_prediction_ids`，项目管理员可以从 job 详情一键把这些失败项投递到既有 `failed_prediction.retry` 链路。端点要求调用者是该 job owner（或 super_admin），并且角色是 project_admin / super_admin。

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://api.example.com/api/v1/async-jobs/01JX.../retry-failed
# → 202 {"status":"queued","job_id":"01JX...","queued":3,"skipped":1}
```

跳过项包括：失败预测已被删除、已 dismiss，或超过最大重试次数。历史 job 没有 `failed_prediction_ids` 时返回 `409`，需要去「模型市场 → 失败预测」按单条处理。

## 终态通知

以下 kind 在有 `user_id` 且进入终态后会通过 `notifications` 体系发站内通知：

| kind                      | `completed`     | `failed`     | `cancelled`     |
| ------------------------- | --------------- | ------------ | --------------- |
| `batch_predict`           | `job.completed` | `job.failed` | `job.cancelled` |
| `prediction_retry`        | `job.completed` | `job.failed` | -               |
| `video_tracker`           | `job.completed` | `job.failed` | `job.cancelled` |
| `predictions_import`      | `job.completed` | `job.failed` | `job.cancelled` |
| `dataset_import`          | `job.completed` | `job.failed` | `job.cancelled` |
| `create_tasks`            | `job.completed` | `job.failed` | -               |
| `audit_archive`           | `job.completed` | `job.failed` | `job.cancelled` |
| `point_cloud_cross_frame` | `job.completed` | `job.failed` | `job.cancelled` |

通知 payload 至少包含 `kind` / `status`，并会带上可展示字段（如 `batch_display_id`、
`task_display_id`、`project_display_id`）和结果摘要（如 `success_count` / `failed_count` /
`imported` / `error_count`）。`export` 不使用通用 `job.*`，继续发 `export.ready` / `export.failed`
以保留下载链接。通用作业通知使用 `target_type=async_job`，导出通知使用 `target_type=export`；两者的 `target_id` 都是作业 ID，前端依据最新详情判断可执行操作。

通用作业的 `notify_job_terminal` 仍在调用方提交前保存并发布，数据库与 Redis 不构成原子事务；WebSocket 侧的短暂可见性等待不等于统一改为提交后发布。同一用户、类型和作业的终态通知去重，后续取消计数回填不保证刷新已发送通知；详情应重新读取作业。

## 进度上报模型

- **所有者显式写入**：worker 调用 `async_job_svc.update_progress(db, job_id, pct, ...)`，进度裁剪到 0–100；`extra_payload` 浅合并，显式 `result` 整体替换。跨帧每个目标提交进度，连接器导入沿用阶段与百分比步长；各执行器的频率和取消边界不统一。
- **Celery signals 兜底**：`task_failure` / `task_revoked` 按执行 ID 交给终态应用工作流，组合通用状态、各领域账本及通知，保留已完成保护、部分结果及领域状态机，不无条件覆盖所有作业。
- **调用方事务**：状态写命令不自行提交或发送通知；异常是否继续、逐项回滚或使作业失败由具体执行器保留。不能把通用所有者理解为一律吞错或自动重试。

## Retention

终态行（`completed` / `failed` / `cancelled`）每日 04:15 UTC 由 Celery beat 任务
`purge-old-async-jobs` 清理超过 30 天的记录。`running` / `pending` 行**永不**自动清。

## 与 `PreannotateJobsBadge` 的区别

| 维度      | PreannotateJobsBadge                                                   | JobsBell (本端点)                                    |
| --------- | ---------------------------------------------------------------------- | ---------------------------------------------------- |
| 数据通道  | Redis pub/sub（`project:{id}:preannotate` + `global:prediction-jobs`） | Polling `/api/v1/async-jobs?limit=20`（5s interval） |
| 实时性    | 秒级                                                                   | 5s                                                   |
| kind 覆盖 | 仅 `batch_predict`                                                     | 全部 async_jobs kind                                 |
| 历史记录  | ❌（仅 in-progress）                                                   | ✅（含最近完成）                                     |
| 用户范围  | super_admin / project_admin                                            | 所有登录用户（owner-scoped）                         |

两者**并存**，互补不冲突。

## 相关

- [预标注流水线](../../dev/concepts/prediction-pipeline)
- [Runbook：Celery Worker 卡死](../../ops/runbooks/celery-worker-stuck)
- [Runbook：ML Backend 不可用](../../ops/runbooks/ml-backend-down)
- [ROADMAP §1.7 async_jobs MVP](../../roadmap/2026-05-18-cvat-labelstudio-inspiration)
- 模型：[`apps/api/app/db/models/async_job.py`](../../../apps/api/app/db/models/async_job.py)
- service：[`apps/api/app/services/async_job.py`](../../../apps/api/app/services/async_job.py)
- 前端铃铛：[`apps/web/src/components/shell/JobsBell.tsx`](../../../apps/web/src/components/shell/JobsBell.tsx)
