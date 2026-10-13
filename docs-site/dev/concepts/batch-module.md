---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-07-29
---

# 批次模块

本文是面向开发者的 batch 手册，覆盖批次的数据模型、状态机、调度联动、前后端入口与测试锚点。

如果你要改：

- 批次状态迁移
- 批次分派
- AI 预标后的接管流程
- 批量操作
- 工作台里“我的批次 / 下一题派发”的行为

先读这页。

## 模块全景

批次不是孤立表，它夹在项目、任务、工作台调度和审核流之间。管理 API、task 事件与管理预标入口通过 `BatchService` 修改批次；Celery 批量预标完成时直接推进 `active → pre_annotated`；scheduler 则反向查询 TaskBatch / Task 作为派题过滤条件。

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/batch-module-map.svg"
  alt="Batch 模块关系图，展示管理 API、任务事件、批量预标 worker、BatchService、TaskBatch、scheduler 与项目计数和 WebSocket 的关系"
  caption="Batch 模块全景：区分管理调用、派题查询、task 状态回写与异步 worker 旁路"
/>

批次模块的关键职责：

- 把任务按业务维度分组
- 承载分派关系：`annotator_id` / `reviewer_id`
- 驱动 batch 级状态机
- 作为工作台筛题和“下一题派发”的边界
- 承接审核结果、逆向迁移、归档和终极重置
- 通过 `admin_locked` 冻结自动推进，并阻止 scheduler 派发新的 task

## 代码入口

| 位置                                                         | 作用                                      |
| ------------------------------------------------------------ | ----------------------------------------- |
| `apps/api/app/db/models/task_batch.py`                       | SQLAlchemy 模型                           |
| `apps/api/app/db/enums.py`                                   | `BatchStatus` 枚举                        |
| `apps/api/app/schemas/batch.py`                              | 请求 / 响应 schema                        |
| `apps/api/app/services/batch.py`                             | 状态机、分派、split、bulk、reset 核心逻辑 |
| `apps/api/app/services/batch_permissions.py`                 | 手工与反向迁移权限                        |
| `apps/api/app/api/v1/batches.py`                             | HTTP 路由、权限、审计、通知编排           |
| `apps/api/app/api/v1/admin_preannotate.py`                   | 预标队列清理与接管管理入口                |
| `apps/api/app/services/scheduler.py`                         | `/tasks/next` 派题时的 batch 过滤         |
| `apps/api/app/services/annotation.py`                        | annotation 写入后触发 batch 自动迁移      |
| `apps/api/app/api/v1/tasks/lifecycle.py` · `review.py`       | task 生命周期与审核事件对 batch 的回写    |
| `apps/web/src/api/batches.ts`                                | 前端 batch API wrapper                    |
| `apps/web/src/pages/Projects/sections/BatchesSection.tsx`    | 项目设置页的批次主 UI                     |
| `apps/web/src/pages/Projects/sections/BatchesKanbanView.tsx` | 批次看板与前端迁移 dry-run                |

## 数据模型

`TaskBatch` 的核心字段：

| 字段                                                                                     | 含义                                       |
| ---------------------------------------------------------------------------------------- | ------------------------------------------ |
| `project_id`                                                                             | 所属项目                                   |
| `dataset_id`                                                                             | 关联数据集，可空                           |
| `display_id`                                                                             | 人类可读 ID，如 `B-12`                     |
| `status`                                                                                 | batch 状态机当前状态                       |
| `priority`                                                                               | 工作台派题和列表排序的主序                 |
| `annotator_id`                                                                           | 单值标注员                                 |
| `reviewer_id`                                                                            | 单值审核员                                 |
| `assigned_user_ids`                                                                      | 兼容旧路径的派生字段，当前由前两者同步生成 |
| `review_feedback`                                                                        | 批次被退回时的审核反馈                     |
| `reviewed_at` / `reviewed_by`                                                            | 最近一次批次级审核元数据                   |
| `admin_locked` / `admin_lock_*`                                                          | 管理锁状态、原因、时间与操作者             |
| `total_tasks` / `completed_tasks` / `review_tasks` / `approved_tasks` / `rejected_tasks` | 聚合计数                                   |

设计要点：

- 当前是“单批次单标注员 + 单审核员”语义，不再是 batch 级多人 list
- `assigned_user_ids` 还保留着，但属于兼容层；新代码优先读 `annotator_id` / `reviewer_id`

## 状态机

当前合法状态集合：

```text
draft
active
pre_annotated
annotating
reviewing
approved
rejected
archived
```

状态机总图：

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/batch-lifecycle.svg"
  alt="Batch 的全部正常合法迁移，包括 AI 预标、标注、审核、归档和 owner 逆向恢复路径"
  caption="Batch 正常状态迁移（对应 BatchService.VALID_TRANSITIONS）"
/>

后端真值源在 `apps/api/app/services/batch.py:VALID_TRANSITIONS`。
`reset_to_draft` 是正常状态机外的 owner 管理旁路，可从任意状态回到 `draft`，不属于 `VALID_TRANSITIONS`。

### 自动迁移

运行时有三类自动迁移来源：

- `active → pre_annotated`
  条件：AI 批量预标成功结束且不是全量失败，由 worker 推进
- `active | pre_annotated → annotating`
  条件：batch 内存在 `Task.status in ["in_progress", "rejected"]`
- `annotating → reviewing`
  条件：batch 内不存在 `Task.status in ["pending", "in_progress", "rejected"]`

注意：

- `reviewing → annotating` **不会**被 auto transition 自动反推
- `withdraw` / `reopen` / `accept-rejection` 这类 task 事件通过业务路由显式回写 task 状态，再调用 `check_auto_transitions()` 和 `recalculate_counters()`

### 手工迁移权限

主要权限矩阵在 `assert_can_transition()`：

- owner / super_admin：最终兜底，几乎覆盖所有合法迁移
- annotator：只允许把自己负责的 `annotating` 批次送到 `reviewing`
- reviewer：只负责 `reviewing → approved/rejected`
- reverse transition：
  `archived → active`
  `approved → reviewing`
  `rejected → reviewing`
  `pre_annotated → active`
  这些都要求 owner，并强制填写 `reason`

## AI 预标联动

`pre_annotated` 是 AI 预标后的中间态，语义是：

- AI 文本批量预标已经跑完
- predictions 还在
- 批次等人工接管

这会影响 3 个地方：

1. `VALID_TRANSITIONS`
   `active → pre_annotated`、`pre_annotated → annotating | active | archived`；其中通用的 `pre_annotated → active` 只改变状态，是否清理 predictions 取决于具体业务入口
2. `check_auto_transitions()`
   标注员开始动 task 后，`pre_annotated → annotating`
3. 前端展示
   `BatchStatusBadge`、Kanban、AdminDashboard、`/ai-pre` 页面都需要认识这个状态

如果你新增一个 batch 状态，至少要同步这些位置；只改枚举通常不够。

## 调度与任务联动

### `/tasks/next`

`apps/api/app/services/scheduler.py:get_next_task()` 是可用的“下一题派发”入口；当前 React 工作台主链使用 `GET /tasks` 后在客户端选题，并不消费 `/tasks/next`，但其它客户端调用该端点时仍遵守同一批次约束。

它对 batch 的依赖包括：

- 只从 `TaskBatch.status in ["active", "annotating"]` 中选任务
- 排除 `admin_locked=True` 的批次
- 非特权用户还要命中 batch 可见性条件
- 按 `TaskBatch.priority` 排序

因此，改 batch 状态机会直接影响工作台拿题行为。

上传、ZIP 导入或扫描新增的数据项会先生成 task；如果 task 仍是 `batch_id=None`，非特权用户不会在工作台派题中看到它。项目管理员需要把新增 task split / 分配到批次并让批次进入可工作状态后，标注员才会拿到这些题。

### annotation 写入

`apps/api/app/services/annotation.py:_update_task_stats()` 在首次产生标注时会：

- `task.is_labeled = True`
- `task.status: pending → in_progress`
- 如果这次计数变化实际触发 `pending ↔ in_progress`，且 task 属于某个 batch：
  调 `BatchService.check_auto_transitions(batch_id)`
  再调 `recalculate_counters(batch_id)`

这就是为什么 batch 状态并不只由 `/batches/{id}/transition` 控制。

### task 事件

task 路由中的这些事件也会影响 batch：

- `POST /tasks/{id}/submit`
- `POST /tasks/{id}/skip`
- `POST /tasks/{id}/withdraw`
- `POST /tasks/{id}/review/approve`
- `POST /tasks/{id}/review/reject`
- `POST /tasks/{id}/reopen`
- `POST /tasks/{id}/accept-rejection`

它们都会在 task 状态变化后重新计算 batch 状态和计数。

## 批次级操作

`BatchService` 当前承载的主要能力：

- `create` / `update` / `delete`
- `transition`
- `split`
- `assign_tasks_to_batch`
- `distribute_batches_in_project`
- `reject_batch`
- `reset_to_draft`
- `clean_task_predictions`
- `bulk_archive`
- `bulk_delete`
- `bulk_reassign`
- `bulk_activate`
- `bulk_approve` / `bulk_reject`
- `admin_lock` / `admin_unlock`
- `check_auto_transitions`
- `recalculate_counters`

### `split` 切分策略

`split(project_id, data, created_by)` 从项目里尚未归类（未分配批次）的 task 中切出新批次，`BatchSplitRequest.strategy` 决定切分方式：

| 策略                  | 说明                                                                                    |
| --------------------- | --------------------------------------------------------------------------------------- |
| `metadata`            | 按 `metadata_key` / `metadata_value` 过滤出匹配 task，归入一个新批次                    |
| `id_range`            | 按显式给定的 `item_ids` 列表把指定 task 归入一个新批次                                  |
| `random`              | 把未归类 task 切成 `n_batches` 个批次；`shuffle=False` 时按 task 创建顺序切分（不打乱） |
| `by_scene`（v0.14.4） | 适用 scene 模式项目：按 task 所属 scene 分组，每个 scene 切成一个独立批次               |

`by_scene` 细节：

- 通过 `resolve_task_scene_frames` 解析每个 task 的 `scene_id` / `scene_name` / `frame_index`，按 `scene_id` 分组，每组生成一个批次；无 scene 的 task 归入一个「无 scene」批次，排在最后。
- 批次命名为 `{name_prefix} · {scene_name}`（无 scene 时为 `{name_prefix} · 无 scene`）。
- 每个批次内调用 `_set_sequence_orders`，把 task 的 `sequence_order` 写为各自的 `frame_index`，使工作台派题顺序与 scene 内帧序一致；`frame_index` 为空的 task 不写 `sequence_order`。

**scene 模式项目强制只能 `by_scene`（v0.15.x）**：scene 项目同一 scene 的连续帧必须落同一批次，`random` / 顺序切分会把连续帧拆散给不同标注员。因此 `split` 入口加了一道门——当 `project.scene_mode` 为真且 `data.strategy != "by_scene"` 时直接抛 `HTTPException(422, "scene 模式项目分包只能按 scene(strategy=by_scene)")`，防御绕过前端直发 API。前端 `BatchesSection` 三个 split 入口对 scene 项目也只暴露「按 scene 建包」（横带的 `random` 一键建包对 scene 项目隐藏，「创建批次」直接走 `by_scene` 不弹对话框）。

scene / frame_index 的语义见 [scene 与 frame_index](scene-and-frame-index)。

### `delete` 与删批次保护（v0.11.25）

`delete(batch_id, *, force=False)` 不再是裸删，它先判断批次里有没有"不该被无声丢弃"的工作：

- `_count_protected_tasks(batch_id)` 返回 `(non_pending, predicted, affected)`：
  - `non_pending`：`Task.status != "pending"` 的数量（进行中 / 已完成 / 已审核等）
  - `predicted`：`Task.total_predictions > 0` 的数量（AI 预标过）
  - `affected`：`status != "pending" OR total_predictions > 0` 的并集去重数。`non_pending` 与 `predicted` 会重叠（同一 task 既 review 又预标），前端用 `affected` 展示「将影响 N 个任务」，避免 X+Y 误判
- 当 `force=False` 且 `affected` 非零时，抛 409：

  ```json
  {
    "code": "batch_has_active_work",
    "requires_force": true,
    "non_pending": 3,
    "predicted": 5,
    "affected_tasks": 6,
    "message": "批次含进行中/已完成或 AI 预标过的任务；删除将重置为待标注并清除 AI 预标。请改用归档，或确认强制删除。"
  }
  ```

- 当 `force=True`（或批次本就干净）时，先调 `_reset_and_clean_batch_tasks(batch_id)` 做级联清理，再把 task 的 `batch_id` 解绑、删除批次本体。

路由层（`api/v1/batches.py`）的 `DELETE /batches/{id}` 与 `POST /batches/bulk-delete` 都接 `force: bool = False` 查询参数，透传给 service；审计 detail 里带 `"forced": force`。前端拿到 409 `requires_force` 后应弹"改归档 / 确认强制删除"二次确认。

### `_reset_and_clean_batch_tasks` 级联清理

这是 `reset_to_draft`（v0.7.6）与 `delete` / `bulk_delete`（v0.11.23）共用的级联清理核心，返回各项清理计数：

- 非 `pending` task → `pending`
- 删除该批次 task 的 `TaskLock`
- 删除 `AsyncJob(kind="batch_predict")` 中 `payload.batch_id` 命中的历史任务
- 调 `clean_task_predictions(task_ids)` 清掉 AI 预标产物
- 返回 `{tasks_reset, prediction_jobs, predictions, failed_predictions, ai_annotations_deactivated}`

### `clean_task_predictions`

`clean_task_predictions(task_ids)` 是 task 维度的"清 AI 预标产物"原子操作，被 `_reset_and_clean_batch_tasks` 与 `batch_predict` 的 `overwrite` 模式（v0.11.24）共用：

- 软删除 `source != "manual"`（即 prediction 派生）的 annotation；手工标注保留
- 把残留 annotation 的 `parent_prediction_id` 置空
- 删除 `prediction_metas` / `predictions` / `failed_predictions`
- `Task.total_predictions` 归零，重算 `total_annotations` / `is_labeled`
- `task_ids` 为空时是 no-op

### `reject_batch`

这是“软重置”：

- 只把 `review` / `completed` 任务回退到 `pending`
- 不清 annotations 历史
- batch 写入 `review_feedback`

### `reset_to_draft`

这是“重兜底”：

- 任意状态回 `draft`
- 清 review 元数据
- 复用 `_reset_and_clean_batch_tasks`：非 `pending` task 回 `pending`、删 `task_locks`、级联清掉 predictions / failed_predictions / prediction_jobs / prediction_metas

### bulk 系列

后端当前已实现：

- `bulk-archive`
- `bulk-delete`
- `bulk-reassign`
- `bulk-activate`
- `bulk-approve`
- `bulk-reject`
- admin lock / unlock

尚未实现：

- bulk pause / resume

### 批次导出

`POST /api/v1/projects/{project_id}/batches/{batch_id}/export` 与项目级导出共享导出目标语义。请求可通过 `targets` 同时选择多个输出；通常创建 `AsyncJob(kind=export)` 并返回 `job_id`，VOC 单目标保留同步下载特例。`format` 仍是兼容入口，不应作为新增调用的唯一表达。

视频轨迹导出可透传：

- `include_attributes`：控制是否输出属性 schema 与标注属性。
- `video_frame_mode=keyframes|all_frames`：控制视频轨迹只导出关键帧，或展开插值后的逐帧 bbox。

批次级 Video JSON 文件名使用 `*_video_tracks.json` 后缀，避免与图片 COCO JSON 混淆。

## 前端同步点

如果你改 batch 状态相关逻辑，至少检查这些文件：

| 文件                                                         | 为什么要看                     |
| ------------------------------------------------------------ | ------------------------------ |
| `apps/web/src/api/batches.ts`                                | 前端请求和返回类型             |
| `apps/web/src/pages/Projects/sections/BatchesSection.tsx`    | 批次列表页按钮、文案、状态标签 |
| `apps/web/src/pages/Projects/sections/BatchesKanbanView.tsx` | 前端 `VALID_TRANSITIONS` 镜像  |
| `apps/web/src/components/badges/BatchStatusBadge.tsx`        | 状态徽章                       |
| `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx`      | 工作台批次筛选                 |
| `apps/web/src/pages/Annotate/AnnotatePage.tsx`               | 标注侧送审操作                 |
| `apps/web/src/pages/Review/ReviewPage.tsx`                   | task 审核与批次可见性          |
| `apps/web/src/pages/AIPreAnnotate/`                          | 预标队列清理与接管             |

常见坑：

- 后端加了新状态，前端 badge / 看板没补
- `VALID_TRANSITIONS` 改了，Kanban 还在用旧镜像
- `BatchResponse` 类型没补，UI 上读不到新增字段

## 测试锚点

先看这些测试文件：

| 文件                                                              | 覆盖重点                                         |
| ----------------------------------------------------------------- | ------------------------------------------------ |
| `apps/api/tests/test_batch_lifecycle.py`                          | 批次状态机主干、权限矩阵、bulk 操作              |
| `apps/api/tests/test_project_attribute_schema_and_batch_reset.py` | `reset_to_draft` 与 `pre_annotated → annotating` |
| `apps/api/tests/test_batch_pre_annotated.py`                      | `pre_annotated` 合法迁移                         |
| `apps/web/src/pages/Projects/sections/BatchesSection.test.tsx`    | BatchesSection 烟测                              |
| `apps/web/src/pages/Projects/sections/BatchesKanbanView.test.tsx` | Kanban 前端迁移限制                              |

推荐调试顺序：

1. 先改 `BatchService` 或路由
2. 补 / 改后端测试
3. 刷 OpenAPI 与前端 wrapper
4. 再改前端状态展示与交互

## 文档一致性

当前与 batch 相关的文档入口：

- 用户视角：[批次与分配](/user-guide/projects/batch)
- 决策视角：[ADR-0008](/dev/adr/archive/0008-batch-admin-locked-status)

如果你改了以下内容，记得同步文档：

- `BatchStatus` 枚举
- `VALID_TRANSITIONS`
- 批量操作集合
- 批次导出的格式、查询参数或文件命名
- owner / annotator / reviewer 的迁移权限
- `pre_annotated` 或未来新状态的业务语义
