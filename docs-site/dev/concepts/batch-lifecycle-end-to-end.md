---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-07-29
---

# 批次生命周期（端到端）

这页不再按“模块职责”拆，而是按一条真实批次从创建到收尾的业务链路来讲。

适合回答这类问题：

- 为什么 batch 看起来只是一个状态字段，实际上却牵着 task、annotation、scheduler 和 review 一起动
- 一次“AI 预标 → 人工接管 → 批次驳回 → 重做”的全链路到底经过哪些入口
- 哪些状态是自动推进，哪些必须人工操作

## 全链路总图

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/batch-lifecycle-end-to-end.svg"
  alt="Batch 从 owner 创建分派、激活、可选 AI 预标、task 自动回写、送审、整批退回到归档和重置的端到端生命周期"
  caption="Batch 端到端主链：区分自动推进、batch-only 手工迁移与带 task 副作用的业务退回"
/>

图中的“创建 → 分派 → 激活”是常用组合，不是一个强制线性事务；“task 动作 → check_auto”也不意味着 batch 一定迁移。当前 `pre_annotated` 对普通 assigned annotator 不可见，而 `reviewing` 中的 task reject 也不会自动把 batch 拉回 `annotating`；这些实现断点在图和下文中都显式保留。

## 参与者与真值源

| 角色 / 模块                     | 负责什么                                                                  |
| ------------------------------- | ------------------------------------------------------------------------- |
| owner / super_admin             | 建批、分派、激活、逆向迁移、重置                                          |
| annotator                       | 编辑 task、推动 `annotating`、提交批次送审                                |
| reviewer                        | task 审核、batch 放行或驳回                                               |
| `BatchService`                  | batch 状态机、计数、reset、reject                                         |
| `AnnotationService`             | annotation 写入后触发 task / batch 自动推进                               |
| `scheduler.get_next_task()`     | `/tasks/next` 调度旁路；当前 React 工作台主链是 `GET /tasks` 后客户端选题 |
| `workers/tasks.py:_run_batch()` | AI 预标 worker，写 predictions 并把 batch 置为 `pre_annotated`            |

## 阶段 1：创建与分派

### 创建

batch 初始通常从 `draft` 开始。

此时它的语义是：

- 已经是一个业务分组
- 可以挂 task
- 但还不参与正常生产、审核和常规派题

`POST /batches` 只创建空 draft；常见的“建批并挂任务”走 `/batches/split`，把未归类 / `B-DEFAULT` task 改挂到新 batch。

### 分派

通过 PATCH / distribute / bulk-reassign 修改分派时，batch 层字段会级联回写 task：

- `annotator_id`
- `reviewer_id`
- `assigned_at`
- task 的 `assignee_id / reviewer_id`

所以“改的是 batch 分派”往往会影响：

- task 列表显示
- reviewer 侧待审树
- annotator 侧我的批次

`split` payload 直接带 annotator / reviewer 时，当前 `_assign_tasks` 只写 `task.batch_id`，不会做上述 task assignee / reviewer 级联；后续仍应经过正常分派入口收敛。

## 阶段 2：激活进入生产

`draft → active` 是 owner 主动动作，不是自动迁移。

关键约束：

- 空批次不能激活
- 激活后 batch 才进入正常工作流
- 单个 transition API 的硬条件是“非空”；bulk activate 和当前 Web UI 还会要求已分派 annotator

从这里开始，batch 会影响两个主要入口：

1. 工作台 `GET /tasks` 能否返回该批次的题
2. 旁路 `/tasks/next` 是否会从该批次派题

当前常规派题只从：

- `active`
- `annotating`

中挑题。

## 阶段 3：人工标注或 AI 预标接管

### 路径 A：纯人工

annotator 开始编辑任意 task 后，annotation 首次写入会触发：

- `task.status: pending → in_progress`
- `BatchService.check_auto_transitions()`

若 batch 原本是 `active`，则会自动：

- `active → annotating`

这个迁移不是前端显式点按钮，而是 annotation 写入驱动。

### 路径 B：先跑 AI 预标

owner 从项目级入口触发：

- `POST /projects/{project_id}/preannotate`

worker 会：

1. 显式 `task_ids` 时当前只按 ID 选取；否则拉指定 batch 或本项目的 `pending` task
2. 调 ML backend 产出 predictions
3. 写 `predictions / prediction_metas / failed_predictions / async_jobs(kind=batch_predict)`
4. 指定 batch、处理单元非空、不是全部失败，且收尾时仍为 `active` 时：
   `active → pre_annotated`

`execution_unit=frame` 的 fan-out 走另一收尾器，当前只完成 AsyncJob，不推进 batch 到 `pre_annotated`。worker 直接写 batch.status，也不会产生普通 `BatchService.transition` 的状态 WS 事件和 transition audit。

`pre_annotated` 的业务语义是：

- AI 候选框已经就绪
- 还没有人工真正开工
- 当前只有 owner / super_admin 可通过 `/ai-pre` 深链进入首次接管

当前它**不是**常规 `/tasks/next` 继续出题的主状态，普通 annotator 的 task 列表 / 点查可见性也排除它。因此当前链路存在实际断点：需 owner / super_admin 先产生 annotation 或显式把 batch 转入 `annotating`，assigned annotator 才能继续常规工作流。

## 阶段 4：任务推进批次

一旦 annotator 在某题上真正开始工作，批次会进入自动推进区。

### 触发点

最常见触发点有：

- 新建 annotation 或采纳 classic prediction，使有效对象计数从 0 变为非 0
- 删除 annotation 或改变对象数的原子操作，使计数再次归零
- task 退回 / 重开 / 接受退回

只有这些动作真正使 task 在 `pending ↔ in_progress` 之间翻转、路径允许 `trigger_batch_transitions`，且 task 属于某 batch 时，annotation service 才会继续调自动迁移与计数重算。普通几何 / 类别 / 属性 PATCH 不会无条件触发它。

### 自动迁移规则

`BatchService.check_auto_transitions()` 当前只管两类：

1. `active | pre_annotated → annotating`
   条件：存在 `Task.status in ["in_progress", "rejected"]`
2. `annotating → reviewing`
   条件：不存在 `Task.status in ["pending", "in_progress", "rejected"]`

这说明：

- `rejected` 在 batch 维度仍算“还在标注中”
- batch 是否进入 `reviewing` 看的是“是否还有未完成或待重做任务”
- `admin_locked` 时 `check_auto_transitions()` 直接跳过，不会自动推进

## 阶段 5：送审与 reviewer 处理

### task 提交的自动送审

当工作台提交 / 跳过一题时，task 进入 `review`、当前锁被释放，再调 `check_auto_transitions()`。最后一个 `pending / in_progress / rejected` blocker 消失时，batch 会自动 `annotating → reviewing`；这是当前 React 工作台的常用主链。

### annotator 手工提交整批

annotator 在自己负责的批次上手工触发：
`POST /projects/{id}/batches/{id}/submit-review`

当前行为是**整批送审**：批次内所有 `pending / in_progress` task 逐个走与工作台提交相同的路径（审核轮次、任务锁释放、Mask QC 自动运行、contributor 快照），随后由 `check_auto_transitions` 把批次推进到 `reviewing`。因此它既提交 task，也会锁住这些 task；已经 `rejected` 的 task 需要先 `accept-rejection` 重做，不进入本次批量集合，并且会作为 blocker 让批次保持 `annotating`。批次已处于 `reviewing` 但仍有未送审 task（例如先手工送审、随后 withdraw / reopen）时，入口仍然可用，只补交剩余 `pending / in_progress` task。

请求鉴权与 `annotating → reviewing` 一致：被分派标注员或 owner / super_admin。标注页的「提交质检」按“是否还有未送审任务”显示，不再只看批次状态。

### reviewer 决策

reviewer 有两类决定：

1. **task 级**
   `review/approve` 或 `review/reject`
2. **batch 级**
   `reviewing → approved`
   或 `POST /batches/{id}/reject`

task 级动作改变的是单题状态；batch 级动作改变的是整批是否放行。

`reviewing → approved` 当前只改 batch 状态，不检查所有 task 是否已 completed，也不修改 task 状态、lock 或 review metadata。因此 `approved` 表示批次级放行，不是“所有 task 必然通过”的派生真值。

## 阶段 6：驳回、重做与复审

### task 级退回

当 reviewer 对某题 `review/reject`：

- `task.status = rejected`
- `reject_reason` 保留
- annotator 后续可 `accept-rejection`

只有 batch 原本还在 `annotating`（例如其他 task 仍是 blocker）时，它才会继续维持在该语义上。如果 batch 已是 `reviewing`，task reject / reopen / accept-rejection 都不会自动将它拉回 `annotating`，而且普通 annotator 对 reviewing batch 不可见。

### batch 级驳回

当 reviewer 走专用 `POST /batches/{id}/reject` 驳回整批：

- `batch.status = rejected`
- `review_feedback` 写入批次
- 所有 `review / completed` task 回到 `pending`
- annotation 与单批路径的 `is_labeled` 保留，同步重算 batch / project counter，写 audit 并通知 annotator

专用 reject 当前接受 `active / annotating / reviewing` 三种来源状态：审核页会列出部分送审（`review_tasks > 0`）但状态尚未推进到 `reviewing` 的批次，这些批次同样要能整批打回重做。通用 `transition target=rejected` 仍然只允许 `reviewing → rejected`，不会放宽。

这是一种“整批退回重做”，不清 annotation 历史，但会把生产流重新拉回前段。

不要把它与通用 `transition target=rejected` 混为一条边：通用迁移只改 batch.status，不写 feedback、不重置 task、不做业务退回通知。bulk reject 又额外把 `is_labeled=False`，并使用不同计数同步路径；这是当前仍待收敛的副作用差异。

专用 reject 后 batch 仍是 `rejected`，check_auto 不会替 annotator 自动恢复。要重新进入正常生产，owner 必须选择 `rejected → active`（无需 reason），或 `rejected → reviewing`（需 reason，直接复审）。

### owner 复审重开

owner 可把：

- `approved → reviewing`
- `rejected → reviewing`

用于发起复审。系统要求 `reason`，并会发 `batch.review_reopened` 通知。

`approved → reviewing` 会清 batch 上次 feedback / reviewed metadata；`rejected → reviewing` 保留上次 feedback。`pre_annotated → active` 的通用 reverse 只改状态，不清 predictions；要清 AI 候选并同步回 active，应走 `/admin/preannotate-queue/bulk-clear` 的 `predictions_only`。

## 阶段 7：归档或重置

### 归档

`archived` 是收尾态，表示这批暂时退出日常工作流。

归档后：

- 常规工作台与 reviewer 流程不再以它为主
- owner 仍可 `archived → active` 重新拉回生产

归档是状态迁移，不会清 task、annotation、prediction、lock 或 assignment。`draft / reviewing` 当前不能直接归档。

### reset_to_draft

这是最重的兜底动作：

- 任意状态回 `draft`
- task 全部回 `pending`
- 删除 `task_locks`
- 清批次 review 元数据
- 删除 `predictions / failed_predictions / prediction_metas / async_jobs(kind=batch_predict)`
- 软停用该批内 active `Annotation.source=prediction_based`，手工 annotation 保留
- 清除剩余 annotation 的 `parent_prediction_id`，重算 task 物化计数 / `is_labeled`

它不清 batch assignment 或 `admin_locked`，也没有统一清 task 级 submitted / reviewer / reject / skip 元数据。

它适用于：

- 预标产物要整体丢弃
- 这批需要重做分组或重跑流程
- `/ai-pre` 页面历史卡片需要彻底清掉

### delete（删批次 + 级联清理）

`delete` 不是 `reset_to_draft` 的反面，而是"解绑 task 并删批次本体"，但同样要做级联清理，避免删批次时留下悬挂的 AI 预标产物。

默认有保护：

- `delete(batch_id, force=False)` 先数 `_count_protected_tasks`：有进行中/已完成 task 或被 AI 预标过的 task 时，抛 409 `batch_has_active_work`（`requires_force=true`），提示改用归档或确认强制删除
- `force=True` 时才跳过保护；无论是否 force，真正删除前都调 `_reset_and_clean_batch_tasks`（task 回 `pending`、删 lock 和 AI 预标产物、软停用 prediction-based annotation），再把 task 移到 `B-DEFAULT` 或解绑 `batch_id`、删除批次

所以删批次和 `reset_to_draft` 共用同一套级联清理（`_reset_and_clean_batch_tasks` → `clean_task_predictions`），区别只在删完后批次本体是否还在、task 是否解绑。改删批次或重置逻辑时，两条路径要一起回归。

## 一个最常见的真实链路

下面这条链路基本覆盖了系统里最容易联动出 bug 的部分：

1. owner 创建并分派 batch
2. owner 激活到 `active`
3. owner 跑 AI 预标
4. worker 写 prediction，batch 变 `pre_annotated`
5. owner / super_admin 从 `/ai-pre` 进入任务，采纳、修改 prediction 或先强制转 `annotating`
6. 首条有效 annotation 产生且未被 `admin_locked` 抑制时，batch 自动变 `annotating`，assigned annotator 恢复可见
7. annotator 逐题提交，最后一个 blocker 消失后自动到 `reviewing`；或手工整批送审
8. reviewer 驳回其中若干 task 或走专用整批 reject
9. 整批 reject 后 owner 先 `rejected → active`，annotator 重做并再次送审
10. reviewer 在 batch 级放行到 `approved`
11. owner 归档，或必要时 reopen review / reset

如果你改的是下面任一项，最好按这条链路通读一遍：

- batch 状态机
- annotation 写入副作用
- reviewer 权限
- predictions 清理
- workbench 可见性

## 开发时的检查顺序

改 batch 生命周期相关代码时，建议按这个顺序检查：

1. `apps/api/app/services/batch.py`
2. `apps/api/app/api/v1/batches.py`
3. `apps/api/app/api/v1/tasks/`
4. `apps/api/app/services/annotation.py`
5. `apps/api/app/services/scheduler.py`
6. `apps/api/app/workers/tasks.py`
7. `apps/web/src/pages/Projects/sections/BatchesSection.tsx`
8. `apps/web/src/pages/Review/ReviewPage.tsx`
9. `apps/web/src/pages/Annotate/AnnotatePage.tsx`
10. `apps/web/src/pages/AIPreAnnotate/`

## 相关文档

- [批次模块](./batch-module)
- [任务模块](./task-module)
- [审核模块](./review-module)
- [AI 预标注接管](./ai-preannotate-handoff)
