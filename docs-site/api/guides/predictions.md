---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
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

成功派发返回 `200` 与持久 `job_id`，可立即通过 `/async-jobs/{job_id}` 查询状态，worker 启动前为 pending。普通无幂等键请求也先提交同一作业，再交给 Celery；派发异常仅将仍为 pending 的作业转为 failed，已认领或已结束的作业保留其状态。状态机见 [预标注流水线](../../dev/concepts/prediction-pipeline)。

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
GET  /api/v1/admin/preannotate-queue?limit=50
GET  /api/v1/admin/preannotate-summary
POST /api/v1/admin/preannotate-queue/bulk-clear
```

`/admin/preannotate-queue` 只看当前 `pre_annotated` 批次（按最近预标时间倒序，含预测与失败计数，与 jobs 历史区分）。`/admin/preannotate-summary` 提供各项目的 ML backend 状态与就绪批次聚合。项目管理员访问这两处端点时结果自动限制为其负责的项目；超级管理员可查看全局。批量清理预标队列走 `/admin/preannotate-queue/bulk-clear`。

项目级模型预热 `POST /api/v1/projects/{project_id}/ml-backends/{backend_id}/warmup` 还会强制校验目标 backend 当前已在项目中启用，未启用或关系不匹配时返回 404。

批量清空接收 1–50 个 `batch_ids`、`mode` 和 10–500 字符的 `reason`。重复 ID 保留输入次序并重复记入 `succeeded`，级联计数仅累计实际删除行；不存在的批次进入 `skipped/not_found`，非当前负责人且非平台管理员的目标进入 `skipped/forbidden`。执行前刷新并锁定当前账号和项目，项目转移与清空并发时返回冲突，不沿用缓存负责人权限。

- `predictions_only`：解绑标注父预测引用，再删除预测 Meta、成功/失败预测及该批次的 `batch_predict` 作业；仅 `pre_annotated` 批次回到 `active`。人工与已采纳 AI 标注保持有效，任务状态、物化计数、贡献证据和编辑锁不变。
- `reset_to_draft`：额外软删 `prediction_based` 标注、重算任务标注/预测计数、将任务回到 `pending`、清编辑锁及审核证据/批次审核元数据，并回到 `draft`；人工标注保留，显式任务分派覆盖不因成员失效被抹掉。

领域清理不提交；该入口最后提交业务行与一条汇总审计，没有逐项 savepoint。领域锁竞争若回滚整个事务，立即保留原 409 并停止，不继续使用已经释放的权限围栏；数据库语句使事务失败或最终提交失败时，整次业务写入回滚。不能把此前内存中的成功列表视为已经持久化的部分成功。此清理不直接删除对象存储字节。

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
