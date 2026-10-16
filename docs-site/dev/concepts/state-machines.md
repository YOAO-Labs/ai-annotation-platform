---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-07-29
---

# 状态机总览

平台里至少有三类会影响业务流的状态机：

- project 状态
- batch 状态
- task 状态

它们的复杂度并不相同：真正承载生产流转压力的是 batch 和 task。

## Project 状态

当前项目状态较轻。下图表达管理视图采用的生命周期约定；后端目前只有模型默认值和枚举，没有像 Batch 一样的受控 transition guard 或自动推进。`ProjectUpdate.status` 仍是未受枚举约束的字符串，owner 可以通过通用 PATCH 直接写入，因此它不是可据此校验请求的状态真值：

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/project-status-convention.svg"
  alt="Project status 的 in_progress、pending_review、completed、archived 只按产品约定用虚线相连，通用 PATCH 仍接受任意字符串；运行时实际由 Batch、Task、权限与 Task Lock 决定能否标注审核和派题"
  caption="Project.status 的管理视图约定与真正的运行时控制边界"
/>

说明：

- project 状态更多是管理视图，不是工作台派题的唯一真值
- 四个枚举值是约定集合，不是 PATCH 输入白名单
- 当前服务端没有自动写入 `pending_review / completed / archived` 的项目状态迁移链
- 真正影响“能不能标、能不能审”的，主要还是 batch / task

## Batch 状态

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/batch-lifecycle.svg"
  alt="Batch 从草稿激活，可经 AI 预标或人工标注进入审核，审核后通过、退回或归档，并支持 owner 逆向恢复"
  caption="Batch 完整状态机：正常推进、审核结果与 owner 逆向迁移"
/>

关键点：

- `active → pre_annotated` 由 AI worker 在批量预标成功结束后推进
- `active | pre_annotated → annotating` 是自动迁移
- `annotating → reviewing` 可以自动推进，也可以由被分派 annotator 主动送审
- owner 有多条逆向迁移兜底路径
- `reset_to_draft` 是绕过正常迁移边的 owner 恢复操作；`admin_lock` 只暂停派题和自动推进，不是一个生命周期状态

## Task 状态

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/task-lifecycle.svg"
  alt="Task 从待处理进入编辑，也可从待处理或编辑中直接送审；审核可撤回、通过或退回，完成和退回后可重新编辑"
  caption="Task 完整状态机：编辑、送审、审核与返工"
/>

关键点：

- 首个有效 annotation 触发 `pending → in_progress`，删掉最后一个有效 annotation 会触发 `in_progress → pending`
- `pending / in_progress` 都可以通过 `submit / skip` 直接进入 `review`
- `withdraw`、`reopen`、`accept-rejection` 都会回到 `in_progress`
- 直接上传任务会先经过 `uploading`；普通数据集关联创建的 task 直接从 `pending` 开始
- 批次驳回会把 `review / completed` task 回退到 `pending`，批次终极重置会把所有非 `pending` task 回退到 `pending`
- 现仓运行时存在 `rejected` task 状态，虽然枚举定义仍有历史差异

## 三者怎么联动

最重要的联动链路：

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/derived-state-propagation.svg"
  alt="Annotation 和 Prediction 事实分别更新 Task 物化字段，Task 状态变化有条件地触发 Batch 自动迁移与计数重算，Project 计数直接扫描项目 Task，Dashboard、Scheduler 与工作流判断各自读取不同层"
  caption="事实写入如何有条件地传播到 Task、Batch、Project 与读模型"
/>

也就是说：

- annotation 写入只有在引发 `pending ↔ in_progress` 状态翻转时，才从 `_update_task_stats()` 继续推动 batch；批量导入还可以显式关闭这条自动迁移
- `admin_locked` batch 会跳过自动推进
- batch 状态会反过来影响 task 可见性和派题
- project 配置决定 scheduler 如何在 task 间做选择，但 Project.status 本身不参与当前候选查询

## 在线学习与训练状态机

除了标注与质检业务的 Project / Batch / Task，在线学习与模型闭环引入了受控的数据版本与训练执行状态机：

### 训练数据版本（TrainingDatasetVersion）

```mermaid
stateDiagram-v2
    [*] --> pending: 校验参数并创建快照
    pending --> running: Export worker 领取消耗并物化图片
    running --> ready: 素材校验通过、ZIP 发布至 MinIO training 桶
    running --> failed: 素材损坏或校验失败
    pending --> cancelled: 取消排队
    running --> cancelled: 取消构建（图片边界安全退出）
    failed --> running: 重试构建（复用已复制原图）
    cancelled --> running: 重试构建
```

- 失败或取消版本可以重试同一快照，沿用既有序号与请求身份；
- 终态且无子版本引用的数据版本可以物理删除。

### 训练实验与执行尝试（TrainingRun & Attempt）

```mermaid
stateDiagram-v2
    [*] --> queued: 提交配置并排队
    queued --> preparing: 独立执行器 Claim 实验
    preparing --> training: 拉取训练包、启动原生子进程
    training --> evaluating: 训练完成、开始最终评估
    evaluating --> uploading: 上传权重与产物清单
    uploading --> succeeded: 产物 SHA 校验通过、发布完成
    training --> cancelling: 用户请求停止
    cancelling --> cancelled: 确认进程组退出并释放锁
    preparing --> failed: 准备失败或 OOM 退出
    training --> failed: 原生异常退出
    training --> lost: 执行器心跳超时（保留 GPU 租约）
    lost --> preparing: 恢复执行
    failed --> queued: 从不可变检查点续训（新增 Attempt）
    cancelled --> queued: 从不可变检查点续训（新增 Attempt）
```

- 训练运行在独立拉取式执行器上，利用 OS 文件锁独占物理 GPU，不占普通 Celery 队列；
- 心跳超时只标记 `lost`，不立刻释放 GPU 租约，等待确认子进程退出；
- 续训产生新的 `TrainingAttempt`，沿用原配置与验证基准，新执行不会改写历史曲线或覆盖前次产物。

## 为什么需要单独一页

最常见的误解是：

- 只改 task 状态机，不看 batch
- 只改 batch 状态机，不看 scheduler
- 只改枚举，不看路由分支

这页的作用就是把“有哪些状态机”先一次讲清，再去看各模块页细节。

## 深入阅读

- [项目模块](./project-module)
- [批次模块](./batch-module)
- [任务模块](./task-module)
- [在线学习与训练体系](./training-datasets)
- [Scheduler 与派题](./scheduler-and-task-dispatch)
