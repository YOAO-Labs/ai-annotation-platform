---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-09-14
---

# Scheduler 与派题

本文讲的是 `GET /tasks/next` 背后的在线 `scheduler`。它不是 Celery 定时调度器，而是“**当前用户现在应该拿到哪一题**”的服务端决策层。当前 React 工作台主路径会先分页读取 `/tasks` 并在客户端选题，再调用显式 lock 端点；`/tasks/next` 仍是可用的调度旁路 API，不应把本页流程误写成唯一前端主链。

代码真值源：

- `apps/api/app/services/scheduler.py`
- `apps/api/app/api/v1/tasks/`
- `apps/api/app/db/models/project.py`
- `apps/api/app/services/task_lock.py`

## 它解决什么问题

`GET /tasks/next` 背后需要同时回答这些问题：

- 用户是否已经手里有一题没做完
- 当前用户在这个项目里能看见哪些 task
- 哪些 batch 允许出题
- 同一 task 是否已经被我标过
- 多人重叠标注项目里还能不能继续派这题
- 应该按顺序、随机还是 uncertainty 来排

这些逻辑集中在 `scheduler.get_next_task()`。

## 执行流程

当前派题主流程如下：

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/task-dispatch-and-lock.svg"
  alt="在线派题先复用旧锁或过滤、排序最多二十个候选，再逐个尝试占锁；TaskLockService 在单题 advisory lock 下清理过期行、判断接管并用 upsert 创建或续期锁，同时标出旧锁复用和自定义 TTL 尚未端到端的实现边界"
  caption="Scheduler 候选选择、Task Lock 占锁决策与受保护写入边界"
/>

## `get_next_task()` 的 6 步

### 1. 先查当前用户是否已有锁题

当前实现先查询该用户在项目下的 lock 行，按 `expire_at DESC` 取最近一行；只要对应 task 存在且 `is_labeled == false` 就直接返回。

这条查询没有 `expire_at > now` 条件，也没有先调用 cleanup；返回前不会 renew 或重新 acquire。因此它可能把尚未清理的过期行当作“当前题”。前端随后显式 acquire 能重新建立互斥，但 `/tasks/next` 响应本身不能作为持锁证明。

目的：

- 避免用户刷新页面时被派到另一题
- 避免同一个用户在同一项目里并行占多题

### 2. 读取项目配置

后续会用到这些 project 字段：

- `sampling`
- `maximum_annotations`
- `task_lock_ttl_seconds`

所以 scheduler 本质上是 project-aware 的。

### 3. 构造候选 task 集合

候选题至少要满足：

- 属于当前 `project_id`
- `is_labeled == False`（候选查询不另外按 `Task.status` 过滤）
- 当前用户还没有对它留下 `is_active=true` 的 annotation
- 有 batch 时，其状态为 `active / annotating` 且未被 `admin_locked`
- 无 batch 时仍需满足角色和任务显式指派范围
- 多人重叠项目里 `total_annotations < maximum_annotations`

项目 owner 与 superadmin 可越过角色 / 分派过滤，但仍受上述基础候选条件约束。

### 4. 叠加角色与可见性过滤

如果不是 `super_admin` 或项目 owner，还要叠加 `task_visibility_clause(user)`。这个 helper 的通用“可见范围”比 scheduler 的基础候选更宽，有批次的任务在 `/tasks/next` 中会与 `active / annotating` 取交集：

当前规则：

- reviewer：helper 可见 `active / annotating / reviewing`，实际可派仍只有 `active / annotating`
- annotator：
  - `active / annotating` 且实际标注员为本人或未指派；`Task.assignee_id` 优先，空值回退批次标注员
  - helper 还允许本人被分派的 `rejected`，但基础候选会把它排除，因此不会由 `/tasks/next` 派出

这意味着“列表里可见”不等于“scheduler 可派”；派题同时依赖 task 投影、batch 状态、管理锁和分派关系。

未分批任务使用外连接保留，由显式任务标注员或审核员取得访问范围；未指派的未分批任务不会进入普通用户的候选池。Data Manager 改派不改变同批其他任务，显式指派在批次后续改派时保留，清除任务覆盖值会恢复批次默认。

### 5. 按项目采样策略排序

调度顺序由 `Project.sampling` 决定。**`TaskBatch.priority` 始终是主排序键**，sampling 策略只决定同 priority 内的二级顺序——这点对“为什么我创建的紧急批次没立即出现”很关键：

| sampling      | 排序键（从主到次）                                                                              |
| ------------- | ----------------------------------------------------------------------------------------------- |
| `sequence`    | `TaskBatch.priority DESC NULLS LAST` → `Task.sequence_order ASC NULLS LAST` → `Task.created_at` |
| `uniform`     | `TaskBatch.priority DESC NULLS LAST` → `random()`                                               |
| `uncertainty` | `TaskBatch.priority DESC NULLS LAST` → 每题最低 `Prediction.score ASC NULLS LAST`（低分优先）   |

未分批任务的批次优先级为空，在三种采样策略下均排在可派发的批次任务之后。

因此，"下一题为什么是这题"很多时候不是 bug，而是 batch priority + 项目级 sampling 配置共同在起作用：调高某批次的 `priority` 可以让它在所有 sampling 策略下都被优先派出。

### 6. 在候选窗口内逐个尝试上锁

排序后最多取前 20 个候选，依次调用 `TaskLockService.acquire()`：

- acquire 成功：返回该题。
- 查询后被另一请求抢先占锁：跳过，继续试下一个。
- 20 个都失败：本次返回 `None`，由客户端后续重试。

这一步很关键，因为当前实现不是：

- 先把题给前端
- 前端再慢慢去申请锁

而是：

- 后端选题
- 后端同步占坑
- 再把题返回

候选查询与 acquire 之间存在 TOCTOU 窗口，服务用“只返回首个实际占锁成功的候选”收口竞态，而不是假设查询和写入原子完成。外层 `/tasks/next` 在成功后 commit，再构造 `TaskOut`。

## scene 连续派题

对时序数据集（同一 scene 下按 `frame_index` 排序的多帧），逐帧切换 scene 会打断标注节奏。项目可以开启一条**可选**的派题优先级，让同一标注员尽量连续拿到同一 scene 的下一帧。

触发与控制（均为 project 级字段，见 [项目模块](./project-module)）：

- `prefer_same_scene_continuation`：开关。**默认 OFF**，关闭时 `get_next_task()` 完全走既有 sampling 流程，**既有项目零回归**。
- `scene_continuation_window_min`：时间窗口，默认 `30` 分钟。

开关打开后，scheduler 会在套用既有 sampling 策略**之前**先尝试 `_next_same_scene_task(...)`，逻辑是：

1. 找该用户在 `window_min` 分钟内**最近创建的 active annotation** → 对应 task → 该 task 的 `scene_id` 与 `frame_index`（scene / 帧索引含义见 [scene 与 frame_index](./scene-and-frame-index)）。
2. 在同一 scene 内，取 `frame_index` **严格大于**当前帧、按帧升序的第一个**可分配** task（可分配判定复用既有的 batch 状态 / 角色可见性 / 多人重叠等过滤）。
3. 命中则直接 acquire lock 并返回该题。

回退行为：上述任何一步缺数据——窗口内无最近 active annotation、task 无 `scene_id`、没有后续帧、或后续帧全被他人占用——`_next_same_scene_task` 返回 `None`，scheduler **回退到既有 sampling 策略**，与开关关闭时一致。

注意：scene 连续**不独占 scene**。它只是优先把下一帧派给同一个人，同 scene 的其它帧仍可正常分配给其他标注员。

## 它与 task lock 的关系

两者关系非常紧，但不是同一个概念：

- scheduler：决定“该给谁哪一题”
- task lock：决定“这题此刻谁能编辑”

耦合点有 3 个：

1. scheduler 开头会直接查询本人最近的 lock 行；当前该分支未过滤过期时间。
2. same-scene 和经典候选路径最终都主动 acquire，竞态失败时不能返回该题。
3. scheduler 首次 acquire 会传项目 TTL，但显式 acquire / heartbeat 当前仍使用 300s 默认值；详见 [Task Lock](./task-locking#ttl-与-takeover)。

所以可以把 scheduler 看成“派题器”，把 task lock 看成“编辑互斥层”。

## 常见误解

### 误解 1：scheduler 是定时任务系统

不是。这里的 scheduler 只管在线派题。

### 误解 2：task lock 决定可见性

不是。可见性主要由项目权限和 batch 可见性规则决定；lock 只负责并发编辑保护。

### 误解 3：只改 task 逻辑不会影响 scheduler

不对。只要你改了这些东西，scheduler 结果都可能变化：

- batch 状态集合
- annotator/reviewer 可见性
- `is_labeled` 的回写时机
- sampling 相关字段

## 常见修改落点

| 你想改什么             | 先看哪里                                                        |
| ---------------------- | --------------------------------------------------------------- |
| 派题顺序               | `services/scheduler.py` + `db/models/project.py`                |
| 角色可见性             | `scheduler.py` + `api/v1/tasks/_shared.py:_assert_task_visible` |
| 锁题优先返回           | `scheduler.py` + `services/task_lock.py`                        |
| `/tasks/next` 结果结构 | `api/v1/tasks/list.py` + `schemas/task.py`                      |

## 相关文档

- [任务模块](./task-module)
- [批次模块](./batch-module)
- [Task Lock](./task-locking)
- [可见性与权限](./visibility-and-permissions)
