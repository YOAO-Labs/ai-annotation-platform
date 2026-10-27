---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 任务模块

本文是面向开发者的 task 手册，说明 task 的数据模型、工作流状态机、锁机制、审核流，以及它如何与 batch、annotation、scheduler 联动。

如果你要改：

- 工作台拿题与任务可见性
- task 状态机
- submit / withdraw / reopen / reject / approve
- task lock
- annotation 写入后 task / batch 的联动

先读这页。

## 模块定位

Task 是系统中的最小工作单元，一条数据最终总是落实到一条或多条 task 的状态变化。Project 归属必填，DatasetItem 与 TaskBatch 归属可空；当前 React 工作台通过 `GET /tasks` 拉列表后在客户端选题，再分别读取详情、annotations、predictions 并获取编辑锁。

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/task-module-map.svg"
  alt="Task 模块关系图，展示数据模型归属、当前 React 工作台取题和锁流程、未被当前工作台消费的 tasks next 调度旁路，以及 annotation 计数对 task 和 batch 的条件回写"
  caption="Task 模块全景：当前工作台主链与 scheduler side API 分开表示"
/>

一句话理解：

- project 决定规则
- batch 决定分组推进
- task 决定一条数据当前是否能被标、审、退回、重开

## 代码入口

| 位置                                                                                   | 作用                                                                                                                                                   |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `apps/api/app/db/models/task.py`                                                       | Task 主模型                                                                                                                                            |
| `apps/api/app/db/enums.py`                                                             | `TaskStatus` 枚举                                                                                                                                      |
| `apps/api/app/schemas/task.py`                                                         | Task 响应模型与 lock 响应                                                                                                                              |
| `apps/api/app/api/v1/tasks/`                                                           | task 路由包（list / video / annotations / predictions / lifecycle / review / locks 子模块 + `_shared.py` 共享依赖）；lifecycle / review 不持有状态写入 |
| `apps/api/app/application/task_lifecycle.py` · `task_review.py` · `task_submission.py` | 组合送审/恢复/审核、贡献证据、计数、审计、反馈、通知与提交后质检派发                                                                                   |
| `apps/api/app/services/labeling_tasks.py`                                              | Task 生命周期状态、审核领取/结果和对应 Project 计数写入                                                                                                |
| `apps/api/app/services/identity_contracts.py`                                          | 生命周期、送审及审核工作流使用的锁后账号事实，不承担项目授权                                                                                           |
| `apps/api/app/services/scheduler.py`                                                   | 下一题派发、batch 可见性过滤                                                                                                                           |
| `apps/api/app/services/task_lock.py`                                                   | task 锁服务                                                                                                                                            |
| `apps/api/app/services/annotation.py`                                                  | annotation 写入对 task 的状态回写                                                                                                                      |
| `apps/api/app/services/annotation_mutation_contract.py`                                | 手工 PATCH/DELETE、批量编辑的数据命令、结果及预期错误                                                                                                  |
| `apps/api/app/application/annotation_mutation.py`                                      | 绑定原 Task/actor/Session，组合单条与批量标注修改、删除、审计和提交                                                                                    |
| `apps/api/app/services/candidate_decision_contract.py`                                 | 人工候选接受/驳回的数据命令、结果及预期错误                                                                                                            |
| `apps/api/app/application/candidate_decision.py`                                       | 绑定原 Task/actor/Session，组合候选决策、视频范围和提交                                                                                                |
| `apps/api/app/services/annotation_cross_frame_contract.py`                             | 手工传播/插值和作业逐目标暂存的独立值接口                                                                                                              |
| `apps/api/app/application/annotation_cross_frame.py`                                   | 绑定原 Session/源任务/actor/access，保留两种原提交边界                                                                                                 |
| `apps/web/src/api/tasks.ts`                                                            | 前端 task API wrapper                                                                                                                                  |
| `apps/web/src/hooks/useTaskLock.ts`                                                    | 前端 task lock 生命周期                                                                                                                                |
| `apps/web/src/pages/Workbench/`                                                        | 工作台主消费方                                                                                                                                         |

审核认领、通过与退回，跳过、撤回、重开、接受退回，以及单题和整批送审都向对应工作流传 actor UUID。共享锁 helper 保留当前账号/项目/成员及批次/任务锁，在原访问绑定和目标复核后返回 `IdentityActorFacts`；用例继续执行能力和可见性检查，由这些最新标量填入审计与通知姓名。整批送审仍按 200 项分块提交，并在后续分块和最终计数/状态回填前重新取权、生成新事实。初始认证缓存不提供锁后身份事实；全局 role 与项目职责/能力保持分离。Task/Project 仍在本地事务内，原证据、计数、反馈、通知与提交时序不变。

对应九个 HTTP 处理器通过现有 `get_current_user_id` 取得 UUID，访问依赖继续复用同一请求的认证与 Session；不携带 User ORM 进入这些处理器，也不重复解析凭据。其他需要资料字段的入口仍有各自身份契约。

手工标注 PATCH/DELETE 在 HTTP 中保留 Task-first 锁和当前编辑许可，再通过纯命令进入绑定该事务的实现。修改保留 If-Match 首次检查、Mask 准备及 Annotation 锁后二次核验，返回 `AnnotationOut` 后按 version 设置原 ETag；删除保留软删、贡献、计数、级联和 204。预期错误保留原 HTTP status/detail/headers，属性 diff 审计与普通修改继续同事务提交。

## 数据模型

`Task` 当前的关键字段：

| 字段                                      | 含义                                                                                         |
| ----------------------------------------- | -------------------------------------------------------------------------------------------- |
| `project_id`                              | 所属项目                                                                                     |
| `batch_id`                                | 所属批次，可空                                                                               |
| `dataset_item_id`                         | 数据集项引用，可空                                                                           |
| `display_id`                              | 人类可读任务 ID                                                                              |
| `file_name` / `file_path` / `file_type`   | 原始素材信息                                                                                 |
| `status`                                  | 任务工作流状态                                                                               |
| `assignee_id`                             | 当前标注责任人                                                                               |
| `reviewer_id`                             | 当前审核责任人                                                                               |
| `assigned_at`                             | 分配时间                                                                                     |
| `is_labeled`                              | 是否已有有效标注                                                                             |
| `total_annotations` / `total_predictions` | 聚合计数                                                                                     |
| `submitted_at`                            | 送审时间                                                                                     |
| `reviewer_claimed_at`                     | 审核员 claim 时间                                                                            |
| `reviewed_at`                             | 审核完成时间                                                                                 |
| `reject_reason`                           | 退回原因                                                                                     |
| `skip_reason` / `skipped_at`              | 跳过原因与时间                                                                               |
| `reopened_count` / `last_reopened_at`     | 重开编辑历史                                                                                 |
| `version`                                 | annotation 写入时的乐观并发辅助                                                              |
| `sequence_order`                          | 帧内序号；scene 模式下与 frame_index 关联（batch `by_scene` 切分时写入），用于排序与跨帧导航 |

数据集关联项目时会为该数据集的现有 `dataset_items` 创建 task；关联关系存在期间，后续上传、ZIP 导入或扫描导入新增的 item 也会扇出为对应项目的 task，并同步更新 `project.total_tasks`。

## Task 状态机

task 状态枚举当前是：

```text
uploading
pending
in_progress
completed
review
```

但业务上还叠加了一个事实：

- 审核退回时，`LabelingTaskService.reject_review()` 会把 `status` 写成字符串 `"rejected"`

这意味着现仓里存在“枚举定义”和“运行时业务状态”并不完全对齐的历史现象。开发时不要只看 `TaskStatus` 枚举，还要看 `services/labeling_tasks.py`、三个 task 应用工作流和 `services/annotation.py` 的真实分支。

### 主要工作流

<ExcalidrawDiagram
  src="/diagrams/shared/state-machines/task-lifecycle.svg"
  alt="Task 可由待处理进入编辑，也可从待处理或编辑中送审；审核可撤回、通过或退回，完成和退回后可重新编辑"
  caption="Task 主工作流（运行时代码包含 rejected 状态）"
/>

补充：

- `uploading` 只用于直接 `/files/upload-init` 上传旁路，普通数据集关联创建的 task 直接进入 `pending`
- 只有首个有效 annotation 会触发 `pending → in_progress`；拿题、加锁或打开编辑器本身不会改变状态
- `pending / in_progress` 都可以通过 `submit / skip` 直接进入 `review`
- 删掉最后一个有效 annotation 会触发 `in_progress → pending`
- 批次级驳回会把 `review / completed` task 回退到 `pending`；批次终极重置或强制删除则会把所有非 `pending` task 回退到 `pending`
- `rejected` 虽不在当前 `TaskStatus` 枚举中，但在业务代码中是实际存在状态

## 可见性与派题

### 列表 / 点查可见性

非特权用户在 `GET /tasks` 和 `GET /tasks/{id}` 上受 batch 可见性限制。

规则在 `scheduler.batch_visibility_clause()` 与 `task_access.assert_task_visible()`：

- reviewer：可见 `active / annotating / reviewing` 批次中的任务
- annotator：可见
  - `active / annotating` 且 `annotator_id == self` 或 batch 未分派
  - `rejected` 且 `annotator_id == self`
- `super_admin` / project owner：越权放行

所以 task 的可见性不只是看 task 本身状态，也看它挂在哪个 batch 上。

### 列表查询过滤参数

`GET /tasks` 在可见性约束之上叠加若干可选过滤参数（与可见性正交）：

| 参数                 | 作用                                                                           |
| -------------------- | ------------------------------------------------------------------------------ |
| `assignee_id`        | 按标注责任人过滤                                                               |
| `reject_reason_type` | 按 `Task.reject_reason_type`（退回原因 enum）过滤                              |
| `class_name`         | 命中存在该 `class_name` 有效 annotation 的 task（`EXISTS` 子查询，不展开多行） |

`reject_reason_type` 与 `class_name` 服务于成员绩效页的退回原因与类别维度下钻，同时作用于列表查询与计数查询，确保分页口径一致。

### `/tasks/next`

`scheduler.get_next_task()` 是 `/tasks/next` 的派题入口。当前 React 工作台没有调用这个端点：它通过 `GET /tasks` 获取列表，再按 URL、上次选择或首项在客户端选题。其它客户端调用 `/tasks/next` 时，它会：

1. 先看用户是否已有未完成锁任务
2. 基于 project 配置、batch 状态、可见性和采样策略构造候选集
3. 按顺序尝试候选 task 的 lock，只返回成功加锁的一题

关键过滤条件：

- `Task.is_labeled == False`
- task 不在“我已经标过”的集合里
- `TaskBatch.status in ["active", "annotating"]`
- 命中 batch 可见性约束

因此，task 派发行为本质上是 project + batch + task 三方共同决定的。派题细节见 [scheduler-and-task-dispatch](scheduler-and-task-dispatch)。

## Annotation 对 Task 的回写

`AnnotationService._update_task_stats()` 是 task 状态自动推进的关键入口。

当 annotation 数量变化时，它会：

- 更新 `task.total_annotations`
- 更新 `task.is_labeled`
- 首次有有效标注时：
  `pending → in_progress`
- 标注全删空时：
  `in_progress → pending`

只有当 `_update_task_stats()` 确实触发 `pending ↔ in_progress` 状态翻转、允许 batch transition，且 task 挂在某个 batch 下时，才会继续：

- 调 `BatchService.check_auto_transitions(batch_id)`
- 调 `BatchService.recalculate_counters(batch_id)`

这就是为什么“只是改 annotation”也会连带改变 task 和 batch。

## 审核流

`apps/api/app/api/v1/tasks/`（`lifecycle.py` / `review.py`）中和审核相关的主要动作：

- `POST /tasks/{id}/submit`
- `POST /tasks/{id}/withdraw`
- `POST /tasks/{id}/review/claim`
- `POST /tasks/{id}/review/approve`
- `POST /tasks/{id}/review/reject`
- `POST /tasks/{id}/reopen`
- `POST /tasks/{id}/accept-rejection`

状态修改由 `LabelingTaskService` 持有；应用工作流组合贡献证据、首次审核事实、反馈镜像、计数与审计。单题状态、证据、反馈和持久通知仍在原本地事务内提交，审核/重开通知在提交后经 `publish_committed()` 发布并复核接收人；这不代表批次自动迁移事件等其他发布也已改成提交后。请求侧只构造不可变 `AuditOriginContext`，工作流不接收 FastAPI `Request`。

### 语义要点

- `submit / skip`
  `pending / in_progress → review`
- `withdraw`
  标注员在 reviewer 未 claim 前把 `review → in_progress`
- `review/claim`
  reviewer 抢占审核所有权，设置 `reviewer_claimed_at`
- `review/approve`
  `review → completed`
- `review/reject`
  `review → rejected`，并写 `reject_reason`
- `reopen`
  标注员把 `completed → in_progress`
- `accept-rejection`
  标注员把 `rejected → in_progress`，保留 `reject_reason`

注意：

- `reviewer_claimed_at` 一旦存在，会冻结 withdraw 入口
- `reopen` 与 `accept-rejection` 都会回到 `in_progress`，但语义不同

## 跨帧 / 邻帧能力

scene 模式把一段时序录像切成多个 task，每个 task 通过其关联 `dataset_item` 上的 `scene_id` + `frame_index` 在 scene 内定位帧序（详见 [scene-and-frame-index](scene-and-frame-index) 与 [Scene Track 与轨迹生命周期](scene-track-lifecycle)）。

### 邻帧查询

`GET /tasks/{task_id}/neighbors` 返回该 task 在所属 scene 内的前后 `k` 个邻居 task，供工作台取相邻帧框叠加显示与前后导航：

- 参数 `k`：方向上各取多少帧，`1 ≤ k ≤ 20`，默认 `1`
- 返回 `NeighborsResponse`：scene 元数据（`scene_id` / `scene_name` / `frame_index` / `scene_total_frames`）+ `prev[]` / `next[]`
  - `prev` / `next` 按“距 cur 的远近”排序：`cur-1` 在 `prev[0]`，`cur+1` 在 `next[0]`，元素为 `{ task_id, frame_index }`
  - 首帧 `prev` 为空、末帧 `next` 为空，不报错
- 边界口径：
  - 历史未 backfill / 关联不到 `dataset_item` / item 无 `scene_id` → `200` + 空 `prev`/`next`
  - `scene_id` 非空但 `frame_index` 为 NULL（异常数据）→ `409`
- 服务层在 `app/services/labeling_scene.py:get_neighbors_for_task()`；按 `frame_index` 排序后反查 task，主帧（`dataset_item_id`）与多模态点云链接（`TaskDatasetItemLink`）两条路径合并去重。

### 跨帧标注传播与插值

`POST /tasks/{task_id}/annotations/{annotation_id}/propagate-to-task`（以及批量入口 `propagate-batch`、区间插值 `interpolate-range`）用于把源 task 的标注延续或插值到同 scene 的目标 task：

- **身份与轨迹绑定**：复制 geometry / class / attributes，共享 `track_id`（源框无 `track_id` 时由 `_new_track_id()` 分配），并在 3D Scene 中同步绑定 `scene_track_id`、维护 `SceneTrackInterval` 与 `temporal_role`（详见 [标注模块 · 跨帧 propagate 与插值](./annotation-module#跨帧-propagate-与插值3d-时序)）。
- **Ego 位姿运动补偿**：当 `box_3d` 的源帧与目标帧均存在 `scene_frame_poses` 位姿时，服务层通过 `compensate_psr` 按世界系位置不变反算目标帧 PSR，并在响应中标记 `motion_compensated: true`；缺位姿时退化为原样复制，`convention_at_create` 取目标 dataset 的 `axis_convention`。
- **覆盖扩展位**：`override_psr`（可空）给定时按 key 覆盖 box_3d 的 center/size/rotation，前端本期总传 `None` = 完全复制源几何。
- **权限与可写性**：源 task 与目标 task 均需对当前用户可见且可编辑，统一通过 `task_access.assert_task_visible()` 与 `task_access.assert_task_editable()`；源任务已 `completed` 时需先 reopen，避免通过可写目标帧隐式改动只读源帧的轨迹身份。标注员不能直接编辑 `review` / `completed`；审核态调整仍要求项目审核能力和冻结非本人证据。
- 返回 `201` + `PropagateResponse{ annotation }`（新建的目标 annotation）

> 与 `POST /tasks/{task_id}/video/tracks/{annotation_id}:propagate` 区分：后者是**单 task 视频内的 AI 轨迹传播**（提交异步 tracker job），而 `propagate-to-task` 是**跨 task（scene 帧间）的同步几何延续**，二者用途不同。

## 任务文件上传生命周期

任务素材可直接挂载到项目任务上，使用分步上传链路（`apps/api/app/api/v1/files.py`）：

1. **初始化占位**：`POST /files/upload-init` 校验项目管理权限，创建状态为 `uploading` 的 task 占位行，并签发约 15 分钟有效的预签名 S3 PUT URL。
2. **直传存储**：客户端流式直传原始素材字节，不携带平台业务 Bearer token。
3. **完成回执**：`POST /files/upload-complete/{task_id}` 验证对象存储中文件存在与大小，将任务状态推进至 `pending`，并为图片格式排队 Celery 缩略图生成任务（回执成功不等于缩略图已渲染完毕）。
4. **下载与缩略图**：`GET /files/tasks/{task_id}/file-url` 严格按照当前任务的可见性与指派授权签发短期读取 URL；`POST /files/projects/{project_id}/backfill-thumbnails` 允许管理员对缺失缩略图的任务排队异步回填。

## Task Lock

Task lock 是工作台并发编辑保护，不等于 task 状态机。

### 入口

- `POST /tasks/{id}/lock`
- `POST /tasks/{id}/lock/heartbeat`
- `DELETE /tasks/{id}/lock`

服务层在 `services/task_lock.py`。

### 语义

- 一个 task 可以因历史残留出现多行锁，需要服务层清理兜底
- 同一用户重进时优先续期自己的锁
- assignee 重进时可 `force_takeover`
- 锁过期会自动清理

默认 TTL：

- `TaskLockService.DEFAULT_TTL = 300`
- 真实 TTL 还会受 `project.task_lock_ttl_seconds` 影响

### 常见坑

- 不要把 task lock 理解成“谁都不能看这题”
  它只防并发编辑，不决定可见性
- 修改 `acquire()` 时要特别小心多行残留、并发插入和 stale takeover 分支

## 批次联动

task 事件会回写 batch，主要体现在：

- `in_progress` 出现后，batch 可能 `active/pre_annotated → annotating`
- batch 内任务都做完后，batch 可能 `annotating → reviewing`
- `withdraw` / `reopen` / `accept-rejection` 等动作会重新计算 batch counters

所以改 task 状态机时，必须同步审视 batch 状态机；这两者不是各自独立演化的。

## 前端同步点

改 task 逻辑时，至少检查这些位置：

| 文件                                                    | 为什么要看               |
| ------------------------------------------------------- | ------------------------ |
| `apps/web/src/api/tasks.ts`                             | 前端 task API wrapper    |
| `apps/web/src/hooks/useTaskLock.ts`                     | 锁获取、续期、释放       |
| `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` | 工作台主状态流           |
| `apps/web/src/pages/Workbench/shell/TaskQueuePanel.tsx` | 任务列表与 rejected 提示 |
| `apps/web/src/pages/Annotate/AnnotatePage.tsx`          | 标注页面上的批次送审入口 |
| `apps/web/src/pages/Review/`                            | 审核工作台               |

高频联动风险：

- 后端新增 task 状态，前端 badge / 分支没补
- 可见性改了，但 `/tasks/next` 和 `/tasks` 规则不一致
- lock 行为改了，前端 heartbeat / 过期提示没同步

## 测试锚点

建议优先看这些测试：

| 文件                                     | 覆盖重点                                      |
| ---------------------------------------- | --------------------------------------------- |
| `apps/api/tests/test_task_lock_dedup.py` | task lock 残留 / 去重                         |
| `apps/api/tests/test_batch_lifecycle.py` | task 事件对 batch 的反作用                    |
| 相关 `tasks.py` 路由测试                 | submit / withdraw / reopen / reject / approve |

调试顺序通常是：

1. 先确认 task 状态变化是否正确
2. 再确认 lock / reviewer claim 是否符合预期
3. 最后检查 batch 与 dashboard counters 是否一起被改对
