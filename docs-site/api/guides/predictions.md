---
audience: [dev]
type: reference
since: v0.9.0
status: stable
last_reviewed: 2026-05-27
---

# Predictions / Async Jobs

候选预测和后台任务分离：

| 表            | 用途                                                         | 端点前缀                 |
| ------------- | ------------------------------------------------------------ | ------------------------ |
| `predictions` | 当前可采纳的候选框（按 task）                                | `/tasks/:id/predictions` |
| `async_jobs`  | 批量预标、失败重试、导出、视频追踪、连接器导入等后台任务历史 | `/async-jobs`            |

<!-- history: batch prediction history used to live in prediction_jobs; current SoT is async_jobs(kind=batch_predict). -->

## 触发预标

```http
POST /api/v1/projects/:id/preannotate
{
  "batch_id": "uuid",
  "prompt": "person . car . bicycle",
  "output_mode": "both",        // box / mask / both
  "ml_backend_id": "uuid",
  "params": { "box_threshold": 0.35 }
}
```

返回 `202` + `job_id`。任务进 Celery，状态机见 [预标注流水线](../../dev/concepts/prediction-pipeline)。

## 查询 jobs

```http
GET /api/v1/async-jobs?kind=batch_predict&kind=prediction_retry&status=running&search=
GET /api/v1/async-jobs/{job_id}
POST /api/v1/async-jobs/{job_id}/cancel
POST /api/v1/async-jobs/{job_id}/retry-failed
```

`kind` 支持重复 query 参数过滤多种任务。批量预标取消是协作式取消：pending job 会直接置 cancelled；running job 写 `payload.cancel_requested=true` 并 revoke Celery task，worker 在下一条预测边界收敛终态。

## 查询当前快照

```http
GET /api/v1/admin/preannotate-queue?project_id=&status=
```

只看当前 `pre_annotated` 批次（与 jobs 历史区分）。批量清理预标队列仍走 `/admin/preannotate-queue/bulk-clear`。

支持 `limit`（接口默认 50，1–200；界面默认 20）和 `offset`（默认 0）；按更新时间、批次 ID 倒序稳定分页。`project_id` 与 `status` 在分页前应用，不传时保留原范围。响应继续为批次数组；客户端可请求下一页判断是否结束，不能把单页长度当作全部记录数。

## 查询任务预测

```http
GET /api/v1/tasks/:task_id/predictions?model_version=&min_confidence=&limit=&offset=
```

返回 `PredictionOut[]`。每条 prediction 的 `source` 表示候选来源：

| source            | 含义                           |
| ----------------- | ------------------------------ |
| `ml_backend`      | 平台内 ML backend 生成的预标   |
| `external_import` | 通过预测导入端点写入的外部预测 |

Workbench 会用该字段显示来源标识，并支持按来源隐藏 / 恢复候选框。

## 按来源清理预测

```http
POST /api/v1/projects/:project_id/predictions/purge
{
  "source_scope": "external_import",
  "task_ids": null,
  "dry_run": true
}
```

权限：项目 owner 或 super_admin。`source_scope` 可为：

| source_scope      | 含义                                                     |
| ----------------- | -------------------------------------------------------- |
| `external_import` | 只清外部导入预测，适合重导前预览或手动回滚导入           |
| `ml_backend`      | 只清平台 ML Backend 生成的预标，清理后需重新运行模型恢复 |
| `all`             | 清理当前项目范围内全部 prediction                        |

`task_ids=null` 表示项目级清理；传 UUID 数组时仅清这些 task。建议先传 `dry_run=true` 读取计数，再用同一参数正式执行：

```json
{
  "source_scope": "external_import",
  "task_ids": null,
  "dry_run": true,
  "counts": {
    "ml_backend": 0,
    "external_import": 12,
    "unknown": 0,
    "total": 12
  }
}
```

正式清理会先删 `prediction_metas`，再删 `predictions`，并写 `predictions.purge` 审计。当前 `annotations.parent_prediction_id` 没有外键级联，已采纳的人工标注不会被删除。

## 重置批次

```http
POST /api/v1/admin/batches/:id/reset
```

清掉该批次所有 `predictions` / `failed_predictions` / 对应 `batch_predict` async job 索引，并释放相关锁。重置后可重新跑预标。

## 接受 / 驳回

详见 [任务与标注](./tasks-and-annotations#采纳预测)。

## WebSocket 进度

| 通道                       | 何时订阅                      |
| -------------------------- | ----------------------------- |
| `project:{id}:preannotate` | 工作台 / `/ai-pre` 该项目页   |
| `global:prediction-jobs`   | 任何 admin（Topbar 进度徽章） |

消息体：

```json
{ "job_id": "...", "type": "progress", "i": 3, "n": 10, "ts": ... }
{ "job_id": "...", "type": "error", "message": "..." }
{ "job_id": "...", "type": "done", "succeeded": 9, "failed": 1 }
```

## Schema 边界

- DB 写入：LabelStudio 标准（保持导出兼容）
- API 读出：经 `to_internal_shape` 转内部 schema
- 前端只接受内部 schema

详见 [API Schema 边界](../../dev/concepts/api-schema-boundary) 与 [Schema 适配器陷阱](../../dev/troubleshooting/schema-adapter-pitfalls)。
