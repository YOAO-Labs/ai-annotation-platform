---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 状态机总览

平台的核心业务与在线学习闭环由多套相互协作的状态机驱动：

- **标注生产主链**：`Project` 状态、`Batch` 状态、`Task` 状态
- **在线学习闭环**：`TrainingDatasetVersion`、`TrainingRun` / `TrainingAttempt`、`TrainingStudy`、`SampleEvaluationRun`、`ActiveLearningRound` 与 `LearningTrigger`

---

## Project 状态

当前项目状态较轻量。下图表达管理视图采用的生命周期约定；后端目前只有模型默认值和枚举，没有像 Batch 一样的受控迁移守卫或自动推进。`ProjectUpdate.status` 仍是未受枚举约束的字符串，owner 可以通过通用 PATCH 直接写入，因此它不是派题或权限校验的唯一真值：

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/project-status-convention.svg"
  alt="Project status 的 in_progress、pending_review、completed、archived 只按产品约定用虚线相连，通用 PATCH 仍接受任意字符串；运行时实际由 Batch、Task、权限与 Task Lock 决定能否标注审核和派题"
  caption="Project.status 的管理视图约定与真正的运行时控制边界"
/>

关键边界：

- `Project.status` 主要服务于项目管理视图；不过当项目处于 `completed` 或 `archived` 时，在线学习子系统会阻断自动训练策略触发与新模型发布回流。
- 四个枚举值（`in_progress`、`pending_review`、`completed`、`archived`）是产品层约定集合。
- 真正决定「工作台能不能标、能不能审」的核心真值是 `Batch`、`Task`、项目角色权限与 `Task Lock`。

---

## Batch 状态

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/batch-lifecycle.svg"
  alt="Batch 从草稿激活，可经 AI 预标或人工标注进入审核，审核后通过、退回或归档，并支持 owner 逆向恢复"
  caption="Batch 完整状态机：正常推进、审核结果与 owner 逆向迁移"
/>

关键点：

- `active → pre_annotated`：由 Celery worker 在批量 AI 预标注成功结束后自动推进。
- `active | pre_annotated → annotating`：当首个任务发生人工实写标注时自动迁移。
- `annotating → reviewing`：全部任务提交后自动推进，也可由被分派标注员主动送审。
- `reset_to_draft`：是绕过常规迁移边的 owner 恢复操作；`admin_lock` 仅暂停派题和自动推进，本身不是生命周期状态。

---

## Task 状态

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/task-lifecycle.svg"
  alt="Task 从待处理进入编辑，也可从待处理或编辑中直接送审；审核可撤回、通过或退回，完成和退回后可重新编辑"
  caption="Task 完整状态机：编辑、送审、审核与返工"
/>

关键点：

- 首个有效 annotation 触发 `pending → in_progress`；删除最后一个有效 annotation 触发 `in_progress → pending`。
- `pending / in_progress` 均可通过 `submit / skip` 直接进入 `review`。
- `withdraw`、`reopen`、`accept-rejection` 均会回到 `in_progress`。
- 直接上传任务会先经过 `uploading`；关联数据集创建的任务直接从 `pending` 开始。
- 批次驳回会将 `review / completed` 任务回退到 `pending`；批次终极重置会将所有非 `pending` 任务回退到 `pending`。

---

## 三者怎么联动

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/derived-state-propagation.svg"
  alt="Annotation 和 Prediction 事实分别更新 Task 物化字段，Task 状态变化有条件地触发 Batch 自动迁移与计数重算，Project 计数直接扫描项目 Task，Dashboard、Scheduler 与工作流判断各自读取不同层"
  caption="事实写入如何有条件地传播到 Task、Batch、Project 与读模型"
/>

联动规则：

- **按需传播**：Annotation 写入只有在引发 `pending ↔ in_progress` 状态翻转时，才从 `_update_task_stats()` 继续推动 Batch；批量导入可显式关闭自动迁移。
- **锁定豁免**：处于 `admin_locked` 的批次会跳过自动推进。
- **反向约束**：Batch 状态会反向决定 Task 的可见性与派题资格。
- **调度解耦**：Project 配置决定 Scheduler 如何在候选 Task 间排序，但 `Project.status` 本身不参与工作台候选查询。

---

## 在线学习与训练状态机

除了标注与质检主链，在线学习与模型闭环引入了受控的数据集物化、独立算力训练与评估选样状态机：

### 训练数据版本（TrainingDatasetVersion）

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/training-dataset-version-lifecycle.svg"
  alt="TrainingDatasetVersion 从创建快照进入 pending，由 Export worker 领取并物化至 running，校验发布至 ready；失败或取消支持复用原图重试，终态无引用版本支持清理"
  caption="训练数据版本状态机：快照、导出物化、校验发布与失败重试"
/>

关键点：

- 失败或取消版本可基于同一冻结快照重试，沿用既有序号（`ordinal`）与版本 ID，仅在 `training_dataset_versions.attempt_id` 上分配新 UUID 并写入 `attempts/{attempt_id}/dataset.zip`。
- 终态且无下游实验或子版本引用的数据版本，可通过 `training_storage_deletions` 两阶段清理意图表安全删除数据库记录与 MinIO 对象。
- 序号（`next_ordinal`）在所属 `(project_id, canonical_task)` 版本流内严格单调递增，失败与取消版本占号不回收。

### 训练实验与执行尝试（TrainingRun & Attempt）

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/training-run-lifecycle.svg"
  alt="TrainingRun 从 queued 入队，独立执行器 claim 并在锁定 GPU flock 后进入 preparing 和 training；完成后经 evaluating 和 uploading 达到 succeeded；支持 lost 超时恢复和从不可变检查点续训"
  caption="训练实验与执行尝试状态机：算力排他租约、原生训练守护、评估发布与断点续训"
/>

关键点：

- 训练运行在独立拉取式执行器（`apps/training-executor`）上，通过 OS `flock` 文件锁防止协作执行器重复认领资源，不占用普通 Celery 队列。
- 心跳超时仅标记为 `lost`，**不立刻释放 GPU 租约**，等待确认子进程退出或重连恢复。
- 断点续训或失败重试会分配新的 `TrainingAttempt`，沿用原配置与验证基准，新尝试绝不覆盖历史指标曲线或前次产物。
- 用户请求停止后先进入 `cancelling`，经单调时钟 watchdog 确认整个进程组退出后才转为 `cancelled` 并释放算力锁。

### 配套学习与评估状态流转

| 实体                                              | 状态流转路径                                                       | 驱动者与关键约束                                                                                       |
| :------------------------------------------------ | :----------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------- |
| **`TrainingStudy` / `TrainingTrial`**             | `queued → running → succeeded \| failed \| cancelled`              | 由 Beat `check_studies`（每 10s）串行推进；入队前预扣 `native_budget` 计时额度                         |
| **`SampleEvaluationRun` / `ActiveLearningRound`** | `queued → running → cancelling → completed \| failed \| cancelled` | 由 `ml.cpu` 队列执行；每分钟 `maintain_evaluations` / `maintain_rounds` 回收失联任务并重试存储墓碑清理 |
| **`LearningTrigger`**                             | `building → queued → running → completed \| failed \| cancelled`   | 由 Beat `check_policies`（每 60s）巡检触发；部分唯一索引保证同项目同时仅一个活跃自动实验               |

---

## 深入阅读

- [项目模块](./project-module)
- [批次模块](./batch-module)
- [任务模块](./task-module)
- [在线学习与训练体系](./training-datasets)
- [Scheduler 与派题](./scheduler-and-task-dispatch)
