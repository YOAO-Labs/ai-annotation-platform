---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-07-29
---

# AI 预标注接管

这页专门讲 `pre_annotated` 相关链路：项目如何触发预标，worker 如何写预测结果，batch 为什么会卡在 `pre_annotated`，以及人工接管和回滚路径是怎么落地的。

如果你要改：

- AI 预标注入口
- `async_jobs` / `predictions` / `failed_predictions`
- `pre_annotated` 的状态语义
- `/ai-pre` 页面和批量清理
- 人工接管时的状态联动

先读这页。

## 这条链路解决什么问题

AI 预标注的目标不是直接把 task 变成“完成”，而是：

1. 先把机器候选框写进系统
2. 让标注员在工作台里接管、修正、补全
3. 把 AI 产物和人工产物都保留在统一数据模型里

因此它天然分成两段：

- **机器阶段**：生成 prediction
- **人工阶段**：prediction 被采纳或改写成 annotation

## 全链路总图

<ExcalidrawDiagram
  src="/diagrams/shared/ai/ai-preannotation-handoff.svg"
  alt="AI 预标注从 owner 触发、Celery 派发、worker 建立 AsyncJob、服务池路由推理、预测落库到 owner 首次人工接管的完整流程"
  caption="AI 预标注与人工接管：区分 Celery 派发 ID、持久化作业 ID 和当前接管断点"
/>

当前实现有两个不可互换的 ID：触发端点返回 Celery task ID，worker 启动后才创建新的 `async_jobs.id` 并把前者记入 `celery_task_id`。因此刚返回 `status="queued"` 时尚无可轮询的 `async_jobs` 行，前端不应把返回 ID 直接当作 AsyncJob 主键。

`pre_annotated` 还存在一个当前接管断点：普通 assigned annotator 的 task 列表和 `/tasks/next` 都排除该状态；现阶段需 owner / super_admin 从 `/ai-pre` 深链进入工作台，先产生有效 annotation 使批次进入 `annotating`，普通标注员才恢复常规可见性。

> **模态说明**：`/ai-pre` 入口按项目 `data_type` 分流。图像项目走上图所示批量链路；视频项目显示引导卡片，实际追踪由工作台逐轨迹 Shift+T 发起，不走本页批量 Celery 链路；lidar 项目暂显占位。

<!-- history: batch_predict job history moved from prediction_jobs into async_jobs; the handoff flow now documents the current source of truth. -->

## 代码入口

| 位置                                       | 作用                                                 |
| ------------------------------------------ | ---------------------------------------------------- |
| `apps/api/app/api/v1/projects.py`          | 触发 `POST /projects/{project_id}/preannotate`       |
| `apps/api/app/workers/tasks.py`            | Celery worker `_run_batch()`                         |
| `apps/api/app/services/prediction.py`      | prediction / failed prediction 写入                  |
| `apps/api/app/db/models/prediction.py`     | `Prediction` / `PredictionMeta` / `FailedPrediction` |
| `apps/api/app/db/models/async_job.py`      | `AsyncJob` 汇总表                                    |
| `apps/api/app/api/v1/admin_preannotate.py` | `/admin/preannotate-queue` 与批量清理                |
| `apps/api/app/services/batch.py`           | `pre_annotated` 相关状态迁移与 reset                 |
| `apps/web/src/pages/AIPreAnnotate/`        | 管理端 AI 预标入口与历史表                           |
| `apps/web/src/hooks/usePredictions.ts`     | 前端 prediction 查询与采纳                           |

## 触发入口

项目级入口：

- `POST /projects/{project_id}/preannotate`

请求体核心字段：

- `ml_backend_id` —— 源阶段 backend；多阶段编排时等价 `pipeline_stages[0].ml_backend_id`
- `task_ids`
- `prompt`
- `output_mode`
- `batch_id`
- `params` —— 选中 backend 的 `/setup.params` 值；worker 合并进 `/predict` context，覆盖项目级阈值兜底；无此字段时行为不变
- `pipeline_stages` —— **多阶段编排**，声明源 + 下游 stage 列表，把单 backend 调用扩展为「检测 → 分类 / 几何分割 → 写回属性」跨 backend 流水线；缺省时与原单阶段批量预标逐字等价，完全向后兼容。形态、ROI 路由、并行兄弟语义详见 [prediction-pipeline §多阶段预标注](./prediction-pipeline#多阶段预标注pipeline_stages路径-b) 与 [ADR 0043](../adr/archive/0043-staged-preannotation-pipeline)

成功派发返回 HTTP 200 和：

- `job_id`：Celery task ID，不是 `async_jobs.id`
- `status: "queued"`
- 仅 batch 模式可得的 `total_tasks` 提示
- 项目进度频道与可选警告

### 当前约束

指定 `batch_id` 时，后端会校验（API 与 worker 复校验共用 `allows_bulk_preannotation`）：

- batch 属于当前 project
- `batch.status == active`，或 `batch.status == draft` 且 `annotator_id` / `reviewer_id` 均未分派（Issue #124，支持先跑 AI 再分派人工）

所以现在不能直接对：

- 已分派人员的 `draft`
- `pre_annotated`
- `annotating`

再发同一类 batch 预标请求。显式 `task_ids` 的批量路径同源校验任务所属批次（工作台单题 `execution_scope=workbench` 走独立的 #121 口径，允许可编辑任务与 draft 批次）。

`/ai-pre` 首页项目卡片的「可预标」计数也复用同一准入规则（`batch_permissions.bulk_preannotation_eligible_condition`），统计 `active` 加未分派人员的 `draft`，避免卡片显示 0 而详情页仍列有草稿。

批次准入依赖 `annotator_id` / `reviewer_id`，而改派不改变 `batch.status`。因此 `BatchService` 在单条更新、批量改派与项目分派后额外广播 `batch.assignment_changed`（频道与 `batch.status_changed` 相同），前端 `useBatchEventsSocket` 对两者都 invalidate `["batches", projectId]`，让其它管理员打开的面板实时收敛可预标资格。

## Worker 侧写入

`apps/api/app/workers/tasks.py:_run_batch()` 是预标真值源。

### 任务选择

worker 会按三种模式选任务：

1. 显式 `task_ids`：当前只按 ID 选取，不叠加 project / batch / `pending` 约束
2. 显式 `batch_id` 时，取该 batch 下 `pending` task
3. 否则取整个 project 下 `pending` task

`skip_predicted` 会在三种模式上再排除 `total_predictions > 0` 的 task。“默认只跑 `pending`”只对 batch / project 选取分支成立；显式 `task_ids` 是当前需要特别防范的实现边界。同时传 `task_ids + batch_id` 时，API 的进度提示按 batch 计数，worker 却优先按 `task_ids` 执行，调用方不应混用两种 scope。

### 输出产物

成功时写：

- `Prediction`（多阶段下:下游 attributes union 后整体写一条父框预测,几何分割阶段产出的 polygon 作 new_shape 追加进同一 `result` 数组）
- `PredictionMeta`(多阶段额外写 `extra.pipeline = { stage_count, enriched_attr_keys, stages: [...] }` 追溯每个属性来自哪个 backend / model)
- `task.total_predictions` 聚合

失败时写：

- `FailedPrediction`
- GPU 仲裁拒绝时，`error_type` 为稳定仲裁错误码，`extra.gpu_arbiter_error` 保留 `status_code` / `retry_after_s` / 干净消息；普通 Backend 异常保持原有字段语义

整批运行级别还会写：

- `AsyncJob(kind=batch_predict)`；它由 worker 创建，`pending → running` 在同次提交前完成，通常首次可见就是 `running`。多阶段下额外写 `result.pipeline_stages`(终态逐阶段统计 `{detected, targeted, ok, failed, skipped_geometry}`,WS 重连/运行后回看都走它,不丢)
- 存在 GPU 仲裁拒绝时，`result.gpu_arbiter_failures` 按错误码给出有界聚合摘要；逐帧执行单元只落作业摘要，不生成无法按帧重试的失败行

其中 `AsyncJob` 会记录：

- `project_id`
- `payload.batch_id`
- `payload.ml_backend_id`
- `payload.prompt`
- `payload.output_mode`
- `status`
- `progress_pct`
- `result.total_tasks`
- `result.success_count`
- `result.failed_count`
- `result.duration_ms`
- `result.total_cost`

## `pre_annotated` 的语义

当 worker 跑完且满足：

- 这次请求指定了 `batch_id`
- 本轮至少有一个可处理单元，且不是全部失败
- batch 当前仍是 `active`（未分派人员的 `draft` 批次保持 `draft`：候选挂任务保留，不隐式推进状态或分派人员，管理员随后正常分派并 `draft → active` 激活即可接管；Issue #124）
- 走常规 task 批处理收尾；当前 `execution_unit=frame` 的 fan-out finalizer 不会推进 batch

就会自动：

- `active → pre_annotated`

这个状态的含义不是“已完成”，而是：

- AI 候选已生成
- 人工还未真正开始接管
- 这批需要在工作台或 `/ai-pre` 里被人处理

### 为什么不直接变 `annotating`

因为 `annotating` 在当前系统里意味着：

- 已有人工工作痕迹
- 或至少已有 `in_progress / rejected` task

只有当 annotation 真正落库后，系统才认为“人开始做了”。

## 人工接管

### 接管入口

当前接管主要依赖这些前端路径：

- owner / super_admin 在 `/ai-pre` 页面查看历史批次并深链进入工作台
- task 详情 / 画布里加载 prediction 候选
- 首个有效 annotation 让 batch 进入 `annotating` 后，assigned annotator 才能从常规 task 列表和调度路径继续处理

### 接管动作

首次接管者可选择：

1. `accept prediction`(候选层 → annotation)
2. 在 prediction 基础上继续改
3. 完全忽略 prediction，手工新建 annotation
4. **Magic Box**: 拖一个粗框 → SAM bbox prompt → 返回 polygon → 自动取紧凑外接矩形 → **直接**落 bbox(不经候选层 UI 确认)。产出按几何归 `bbox` 工具单位；交互式 AI 由项目级 `ai_interactive_enabled` 控制, 详见 [ADR-0026](../adr/archive/0026-tool-unit-class-and-attribute-binding)

### 采纳前候选属性预览 + 分步采纳

工作台选中**尚未采纳**的 AI 候选时,画布选中卡 + 右栏标注详情都以**只读** `AttributeForm` 预览候选 `attributes`(项目 schema 的 select options 解析为中文 label),候选列表行也补属性摘要 chip。免去先采纳再核对车型 / 颜色等多阶段产出属性。

进入「分步采纳」时,属性审阅区表单可编辑(面板底部专用「采纳」按钮已退役——与列表行 / 画布采纳入口重复,不重复挂)——先看多阶段预标产出的 select / multiselect 属性、改后再从**列表行**采纳,`AIInspectorPanel` 内部 wrapper 会把审阅区草稿改动自动作为 `attribute_overrides` 附带给端点原子落库;画布贴框采纳(SelectionOverlay / BoxRenderer 走单参 `onAcceptPrediction`)读不到审阅区本地 state,仍走候选原值。落库路径:

- `POST /api/v1/tasks/{task_id}/predictions/{prediction_id}/accept`
  - `body.attribute_overrides: dict | None` —— 按属性键覆盖 shape 自带 attributes 落库(内部键 `_shape_index` 等不受影响);为 `None` 时沿用候选原值
  - **返回**:`list[AnnotationOut]`,**仅**本次新建的 annotation(单 shape 一条,多 shape 一次多条)。prediction 不存在或 `shape_index` 越界 → **404** `"Prediction not found or shape_index out of range"`。历史实现返回整题全量并被前端误当作"刚新建"回写属性,污染人工标注——现在收窄成"新建即返回"契约,前端不再需要 diff。

实现:[apps/api/app/api/v1/tasks/predictions.py:135](../../../apps/api/app/api/v1/tasks/predictions.py)、`AnnotationService.accept_prediction` 内对 select 字段做软校验(值不在 options 内只告警、不阻断,避免 backend 枚举与项目配置漂移时丢数据)。

只要出现有效 annotation，`AnnotationService._update_task_stats()` 就会把：

- `task.pending → in_progress`

随后 `BatchService.check_auto_transitions()` 会把：

- `pre_annotated → annotating`

这就是“AI 已就绪”到“人工已接管”的真正分界点。

## `/ai-pre` 管理面

`apps/api/app/api/v1/admin_preannotate.py` 提供两类接口。

**统一任务历史**：`/ai-pre/jobs`（`AIPreAnnotateJobsPage.tsx`）提供「图像 / 视频」两个模态 tab（`?tab` 深链）——图像 tab 拉 `async_jobs(kind=batch_predict|prediction_retry)`，视频 tab 拉 `async_jobs(kind=video_tracker)`。

### 1. 预标队列

- `GET /admin/preannotate-queue`

返回当前所有 `pre_annotated` 批次，并补齐：

- `prediction_count`
- `failed_count`
- `last_run_at`
- `can_retry`

这个列表回答的是：“哪些批次已经跑完 AI，正在等人工接手？”

### 2. 批量清理

- `POST /admin/preannotate-queue/bulk-clear`

支持两种模式：

1. `predictions_only`
   只清 prediction 相关表，并把 `pre_annotated → active`
2. `reset_to_draft`
   复用 `BatchService.reset_to_draft()` 做彻底重置

## 失败与回滚路径

### 单条失败

ML backend 对某题失败时，不会中断整批；worker 会写：

- `FailedPrediction`

这样批次仍可部分成功，`/ai-pre` 页面也能显示失败数。

### 清理 prediction 但保留 task 进度

如果你只是想把 AI 产物清掉，让 batch 回到“未接管但可继续生产”的状态，走：

- `bulk-clear` 的 `predictions_only`

它会删除：

- `prediction_metas`
- `predictions`
- `failed_predictions`
- `async_jobs`（`kind=batch_predict`，按 batch payload 清理）

并在 `batch.status == pre_annotated` 时回：

- `pre_annotated → active`

### 彻底回到草稿

如果整批要推倒重来，走：

- `reset_to_draft`

它除了删 prediction 相关产物，还会：

- 清 task lock
- task 非 `pending` 全回 `pending`
- 清 batch review 元数据

## 常见误解

### 误解 1：prediction 就是 annotation

不是。

- prediction 是 AI 候选
- annotation 才是最终人工结果

两者通过 `parent_prediction_id` 关联，但不共用一张表。

### 误解 2：batch 进入 `pre_annotated` 后 assigned annotator 会像 `active` 一样继续取题

当前不是。`scheduler.get_next_task()` 仍只从 `active / annotating` 中选题，task 列表 / 点查可见性也对普通 annotator 排除 `pre_annotated`。

`pre_annotated` 更像等待 owner / super_admin 首次接手的过渡态；这是当前需要修复的普通标注员可见性断点，不应在文档中被描述成已打通的主链。

### 误解 3：清理 prediction 不需要动 batch 状态

不对。如果 prediction 清空但 batch 还停在 `pre_annotated`，UI 会出现“状态说 AI 已就绪，但实际没有候选”的矛盾态。

这也是 `predictions_only` 会主动把状态拉回 `active` 的原因。

## 前端同步点

| 文件                                                                    | 为什么要看                                                                                                                                                                      |
| ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `apps/web/src/pages/AIPreAnnotate/AIPreAnnotatePage.tsx`                | 批量预标主入口；按项目 `data_type` 路由到对应面板                                                                                                                               |
| `apps/web/src/pages/AIPreAnnotate/components/ProjectDetailPanel.tsx`    | 图像项目详情面板（含 backend 选择器、SchemaForm 参数面板、RunPanel、HistoryTable）                                                                                              |
| `apps/web/src/pages/AIPreAnnotate/components/PreannotateConfigForm.tsx` | 阶段卡基底——model-first 单一下拉(`value=model_id`、文案 = 模型市场 `display_name`);doc / 几何 / 文本三路径共用同一交互                                                          |
| `apps/web/src/pages/AIPreAnnotate/components/StageCard.tsx`             | 多阶段编排阶段卡;`parent_class_filter` / `write.keys` chip 多选 + 键冲突配置期预警 + 运行态徽标 + `ProgressBar` 实时统计                                                        |
| `apps/web/src/pages/AIPreAnnotate/components/VideoPreannotateGuide.tsx` | 视频项目引导卡片（提示在工作台逐轨迹 Shift+T 发起，提供跳工作台 + `/ai-pre/jobs?tab=video` 深链）                                                                               |
| `apps/web/src/pages/AIPreAnnotate/components/RunPanel.tsx`              | 运行触发与进度提示                                                                                                                                                              |
| `apps/web/src/pages/AIPreAnnotate/components/HistoryTable.tsx`          | `pre_annotated` 历史列表与 bulk clear                                                                                                                                           |
| `apps/web/src/pages/AIPreAnnotate/AIPreAnnotateJobsPage.tsx`            | AI 任务历史；含「图像」(`async_jobs: batch_predict / prediction_retry`) 和「视频」(`async_jobs: video_tracker`) 两个模态 tab，`?tab=video` 深链                                 |
| `apps/web/src/pages/Projects/sections/ClassesSection.tsx`               | 项目类别与属性区;头部「从 ML Backend 预填」按钮挂 `PrefillFromBackendDialog`,「属性 JSON」下拉挂属性 schema 导入 / 导出                                                         |
| `apps/web/src/pages/Projects/sections/PrefillFromBackendDialog.tsx`     | 「从 ML Backend 预填」对话框——列出自报 `output_attribute_schema` 的所有在线 backend / model,预览勾选字段后一键合并进当前工具单位的 `attribute_schema`(同 key 覆盖、新 key 追加) |
| `apps/web/src/hooks/usePredictions.ts`                                  | prediction 查询与采纳(含 `attribute_overrides`)                                                                                                                                 |
| `apps/web/src/utils/batchPreannotation.ts`                              | 前端可预标准入筛选（与后端 `allows_bulk_preannotation` 同源：`active` 或未分派人员的 `draft`）                                                                                  |
| `apps/web/src/hooks/useBatchEventsSocket.ts`                            | 订阅 `batch.status_changed` / `batch.assignment_changed`，invalidate 批次缓存（改派会让未分派草稿的可预标资格实时变化）                                                         |
| `apps/web/src/pages/Workbench/shell/Topbar.tsx`                         | 当前 task 所属批次的 `pre_annotated` 提示                                                                                                                                       |
| `apps/web/src/components/badges/BatchStatusBadge.tsx`                   | `pre_annotated` 徽章                                                                                                                                                            |

## 相关文档

- [预标注流水线](./prediction-pipeline)
- [批次模块](./batch-module)
- [标注模块](./annotation-module)
- [批次生命周期（端到端）](./batch-lifecycle-end-to-end)
