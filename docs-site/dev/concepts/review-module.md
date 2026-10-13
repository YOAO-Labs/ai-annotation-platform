---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-07-29
---

# 审核模块

本文是面向开发者的 review 手册。这里的“审核”不是单独一张表，而是一组跨 `task`、`batch`、通知和工作台的协作规则。

如果你要改：

- reviewer 的 task 审核动作
- annotator 的 submit / withdraw / reopen / accept-rejection
- batch `reviewing / approved / rejected` 的业务语义
- reviewer 页面、通知和审计联动

先读这页。

## 模块定位

当前系统的 review 分两层：

1. **task 级审核**
   reviewer 对单题做 `claim / approve / reject`
2. **batch 级审核**
   reviewer 对整批做 `approved / rejected`

这两层不是重复实现：task 事件会重算 counters，并可能触发 batch 自动送审，但 task reject 不会直接把 batch 设为 `rejected`。

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/review-module-map.svg"
  alt="Review 模块关系图，分开表示 task 与 batch 审核，拆分 withdraw、reopen 和 accept rejection 三条恢复路径，并标出 claim、Mask QC、计数、审计与通知规则"
  caption="Review 模块全景：上下两层仅通过 task counters 与自动迁移关联"
/>

## 代码入口

| 位置                                                    | 作用                                                           |
| ------------------------------------------------------- | -------------------------------------------------------------- |
| `apps/api/app/api/v1/tasks/lifecycle.py` · `review.py`  | task 级 submit / withdraw / review / reopen / accept-rejection |
| `apps/api/app/api/v1/batches.py`                        | batch 级 transition / reject / reset                           |
| `apps/api/app/services/batch.py`                        | batch 状态校验、reject_batch、counter 回写                     |
| `apps/api/app/services/scheduler.py`                    | reviewer 可见性的 batch 过滤                                   |
| `apps/web/src/api/tasks.ts`                             | 前端 task review API                                           |
| `apps/web/src/pages/Review/ReviewPage.tsx`              | reviewer 列表页与批量审核                                      |
| `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` | `mode="review"` 的审核工作台外壳                               |
| `apps/web/src/pages/Workbench/modes/useReviewMode.tsx`  | reviewer claim / approve / reject / diffMode 策略              |
| `apps/web/src/pages/Annotate/AnnotatePage.tsx`          | annotator 侧整批送审                                           |

## Review 不是单一状态机

### Task 级审核

`task` 在审核相关的主路径如下：

- `pending / in_progress → review`：`submit / skip`
- `review → in_progress`：`withdraw`
- `review → completed / rejected`：reviewer `approve / reject`
- `completed / rejected → in_progress`：annotator `reopen / accept-rejection`

完整状态和批次级覆盖路径见 [Task 状态机](./task-module#task-状态机)。

这里最重要的事实有两个：

- `review` 是“等待 reviewer 处理”的 task 态
- `rejected` 是“退回给原标注员重做”的 task 态

### Batch 级审核

`batch` 的 review 相关主路径如下：

- `annotating → reviewing`：全部任务完成后自动推进，或被分派 annotator 主动送审
- `reviewing → approved / rejected`：reviewer 通过或退回
- `approved / rejected → reviewing`：owner 填写理由后重开审核
- `rejected → active`：owner 重新激活返工

完整状态、归档路径和终极重置见 [Batch 状态机](./batch-module#状态机)。

要区分：

- task review 看的是单题质量
- batch review 看的是“这批是否整体可以放行”

## 角色矩阵

### Annotator

annotator 主要负责：

- 在 `pending / in_progress` 下编辑 annotation
- `submit`
- `skip`
- `withdraw`
- `reopen`
- `accept-rejection`
- 把自己负责的 `annotating` 批次送到 `reviewing`

annotator **不能**：

- 对 task 做 `approve / reject`
- 对 batch 做 `approved / rejected`

### Reviewer

reviewer 主要负责：

- `review/claim`
- `review/approve`
- `review/reject`
- `reviewing → approved`
- `reviewing → rejected`

reviewer 能看到：

- `active`
- `annotating`
- `reviewing`

这就是 reviewer 页面为什么既能处理 review 中任务，也能提前看到待审批次。

### Owner / Super Admin

owner 是反向迁移和兜底操作的最终执行者，例如：

- `approved → reviewing`
- `rejected → reviewing`
- `rejected → active`
- `archived → active`
- `reset_to_draft`

其中 `approved → reviewing`、`rejected → reviewing`、`archived → active` 和 `pre_annotated → active` 要求填写 `reason`；`rejected → active` 当前不要求理由。所有操作都会落审计。

## Task 级审核细节

### `submit`

入口：

- `POST /tasks/{id}/submit`

行为：

- `pending / in_progress → review`
- 若 `assignee_id` 为空，则把当前提交者补成 assignee
- 释放当前 task lock
- 清空上一轮 reviewer 痕迹
- 对启用了相关 Mask QC 配置的任务运行提交前检查；`skip` 不运行这一步

这解释了为什么提交后 annotator 不能继续直接编辑当前题。

### `skip`

入口：

- `POST /tasks/{id}/skip`

语义与 submit 很像，但它允许无标注进入 review，并额外写：

- `skip_reason`
- `skipped_at`

所以 skip 不是“丢弃任务”，而是“把任务连同跳过理由一起送 reviewer 复核”。
`submit` 与 `skip` 都写审计，但不发送 task 通知。

### `withdraw`

入口：

- `POST /tasks/{id}/withdraw`

前提：

- `task.status == "review"`
- 当前用户是 assignee，或是 admin 兜底
- `reviewer_claimed_at is NULL`

一旦 reviewer claim 成功，withdraw 入口就冻结。

### `review/claim`

入口：

- `POST /tasks/{id}/review/claim`

它是 reviewer 的抢占锁，不是 task lock。

语义：

- 第一个 reviewer 写入 `reviewer_id + reviewer_claimed_at`
- 后续调用幂等读取，不覆盖
- 一旦 claim，annotator 侧 withdraw 被锁死

普通 reviewer 与非 owner 的 project admin 必须先 claim 才能 approve / reject；project owner 与 super admin 可以绕过 claim，也能处理已被他人领取的任务。

### `review/approve`

入口：

- `POST /tasks/{id}/review/approve`

行为：

- `review → completed`
- 补 `reviewed_at`
- 如无 reviewer 记录则回写当前 reviewer
- project / batch counters 回写
- 给 annotator 发 `task.approved` 通知

approve 还受 Mask QC 摘要新鲜度、阻断项与 warning 确认门禁约束。

### `review/reject`

入口：

- `POST /tasks/{id}/review/reject`

行为：

- `review → rejected`
- `reject_reason_type` 必填，补充说明文本 `reject_reason` 可选
- 回写 `reviewed_at`
- project / batch counters 回写
- 给 annotator 发 `task.rejected` 通知
- 同步写入 `annotation_feedbacks`，供标注侧展示退回反馈

`reject_reason` 会在 `accept-rejection` 后保留，前端可继续提示“重做中”。

### `reopen`

入口：

- `POST /tasks/{id}/reopen`

语义不是 reviewer 撤销，而是 **annotator 对已通过任务重新打开编辑**：

- `completed → in_progress`
- `reopened_count += 1`
- 清空 reviewer 元数据
- 保留原 annotation，继续在原地修改

系统还会给原 reviewer 发 `task.reopened` 通知。

### `accept-rejection`

入口：

- `POST /tasks/{id}/accept-rejection`

行为：

- `rejected → in_progress`
- 不清 `reject_reason`

这和 `reopen` 的区别是：

- `reopen` 发生在任务已通过后
- `accept-rejection` 发生在 reviewer 已明确退回后

## 审核贡献者证据（增量 A2）

`tasks` 上有三个可空字段，用于在项目级角色模型启用前先可靠记录“谁在标注阶段动过这题”：

| 字段                         | 含义                                                                                                                                |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `annotation_contributor_ids` | 标注阶段 actor 的累积集合。`NULL` = 完整性未知（legacy 行 / 回滚区间）；`[]` = 已知为空（本记录二进制创建的新题）；其余为累积集合。 |
| `review_contributor_ids`     | 当前 `review_round_id` 冻结的贡献者集合。                                                                                           |
| `review_submitter_id`        | 实际提交该轮的 actor。                                                                                                              |

写入规则（`app/services/annotation_evidence.py`）：

- 每一次标注阶段写动作（create / update / delete、批量写、撤销/恢复、转换、导入、AI 采纳、视频/场景命令、多相机、tracker 接收）都在同一事务里把实际 actor 累积到 `annotation_contributor_ids`，不读取 `Annotation.user_id` 推断编辑者。
- 累积是 task 行锁下的 read-modify-write；刷新证据前先 flush 待定变更，避免同事务内第二次写入丢失第一次的累积。混合顺序边界先在禁止 autoflush 的上下文中取 NOWAIT 锁，再 flush 和刷新，防止待定 UPDATE 抢先阻塞。`NULL` 具有粘性：在 legacy 未知题上新增一笔标注不会把未知收窄成“只有新 actor”的已知集合。畸形证据（非数组、空元素，或含非 UUID 元素）同样保持未知，绝不静默丢弃。
- 优先在取 Annotation / SceneTrack 行锁之前锁定 task（或复用调用方已持有的 task 锁）；不能保证该顺序的边界采用下述非阻塞冲突处理。失败事务会连同证据一起回滚。冻结与进入 review 的状态迁移共用同一把 task 锁。
- 无法在取资源锁前锁定全部 task 的混合顺序边界（AAP 导入、区间插值、scene resume、tracker 接受）使用 `FOR UPDATE NOWAIT`：task 忙时整体回滚并返回可重试的 409（`task_evidence_busy`），不形成阻塞等待环。用户触发的预测清理把发起 actor 记为贡献；系统清理无法归属 actor 时，把受影响 task 的累积集合保守置回未知。
- `submit` / `skip` / 批量送审 / 视频分段完成后进入 review 时，将累积集合、存活标注作者、有效 assignee 与提交者一起冻结为 `review_contributor_ids`，并写 `review_submitter_id` 与新的 `review_round_id`。未知累积量不会伪造已知的 review 集合。
- 离开 review（`withdraw` / `reopen` / `accept-rejection` / `review/reject` / 批次退回或重置 / 视频分段重开）会清空当前轮的 review 证据；标注累积集合按“保守”原则保留（删除、重置、改派、重开都不清除）。
- 审核阶段的修改属于审核动作，不把审核员加入标注贡献者集合。首次审核统计（`first_review_*`）保持不变。

本增量只记录证据，不据此放行/拒绝；基于该证据的自审拦截属于后续项目级角色增量。

## Batch 级审核细节

### annotating → reviewing

该迁移有两条入口：系统检测到批次内不再存在 `pending / in_progress / rejected` task 时自动推进，或 annotator 在批次页 / 标注页上主动提交整批。自动路径有上述 task 就绪条件；手工 transition 只校验角色与合法状态，没有同等的服务端就绪门禁。

对应：

- `POST /projects/{project_id}/batches/{batch_id}/transition`
- `target_status = "reviewing"`

因此手工路径不要求每条 task 都已 `completed`；批次 counters 和 reviewer 页面会立刻反映整批送审。

### reviewing → approved

reviewer 通过 batch transition 放行整批。

关键点：

- `BatchService.transition()`
- `target_status == approved` 时会触发 `on_batch_approved()`
- 批次通过只修改 batch，不修改任何 task；通常已审核任务是 `completed`，但服务端没有强制批次内所有 task 都必须处于该状态
- 写 `BATCH_STATUS_CHANGED` 审计，不发送 batch 通知

### reviewing → rejected

整批驳回走独立端点：

- `POST /projects/{project_id}/batches/{batch_id}/reject`

`BatchService.reject_batch()` 会：

- 把该 batch 下 `review` / `completed` task 统一回 `pending`
- 写 `batch.review_feedback`
- 写 `reviewed_at` / `reviewed_by`
- 重新计算 batch counters
- 写审计，并向标注员发送 `batch.rejected`

这是一种“批次级软重做”，不会删 annotation 历史。

### reverse transitions

这些不是 reviewer 日常动作，而是 owner 兜底：

- `approved → reviewing`
- `rejected → reviewing`
- `rejected → active`

系统会：

- `approved → reviewing`、`rejected → reviewing` 强制填写 `reason`；仅当目标 reviewer 不是操作者时发送 `batch.review_reopened`
- `rejected → active` 当前不要求理由，也不发通知
- `archived → active` 按对象发送 `batch.unarchived`
- 所有迁移写 `BATCH_STATUS_CHANGED` 审计；`reset_to_draft` 是独立管理动作，不发通知

## 审计与通知

review 链路几乎每个动作都带审计。

常见审计动作：

- `TASK_SUBMIT`
- `TASK_SKIP`
- `TASK_WITHDRAW`
- `TASK_REVIEW_CLAIM`
- `TASK_APPROVE`
- `TASK_REJECT`
- `TASK_REOPEN`
- `TASK_ACCEPT_REJECTION`
- `BATCH_REJECTED`
- `BATCH_STATUS_CHANGED`
- `BATCH_RESET_TO_DRAFT`

常见通知类型：

- `task.approved`
- `task.rejected`
- `task.reopened`
- `batch.rejected`
- `batch.review_reopened`
- `batch.unarchived`

改 review 路径时，不要只改状态写入；通常还要同步检查：

- 审计 detail 是否完整
- fan-out 收件人是否正确
- 前端 toast / badge / sidebar 是否跟上

## 讨论面板：评论 / Issue / 历史统一入口

reviewer 与 annotator 的沟通收敛到工作台右栏下段的 `DiscussionPanel`（详见 [工作台 Shell 架构 §右栏](./workbench-shell#右栏-ai-检查器-讨论面板)），不再有独立的 issue 浮层。它有三个常驻 tab：

- **comments**：标注级评论；当前无选中标注时降级为任务级评论（合并 `annotation_comments` 与任务级反馈）。
- **history**：标注 / 任务级 audit 历史（任务级走 `GET /tasks/{id}/audit-history`）。
- **issues**：`kind=issue` 的反馈（含像素锚点 `anchor_type=pixel`），可按 status 过滤（未解决 / 已解决 / 搁置）。

### Issue 图钉与列表双向联动

issue 在画布上以图钉呈现，与 issues tab 列表双向联动：

- 点列表项（有 pin）→ `focusIssue(feedback)` 携带完整目标，图片平移到图钉，视频交给任务与视图恢复入口；保留 `focusIssue(id)` 的旧调用兼容。
- 图钉单击 → `useActiveIssueStore` 高亮对应行并自动把面板切到 issues tab。
- 视频 Stage 的图钉按 `anchor_position.frame` 帧级显隐（`VideoKonvaIssueLayer.tsx`），颜色按 status 映射（open=warning / resolved=success / wont_fix=muted）。

视频落点通过 `useIssuePins` 保存当次请求的归一化坐标和源帧，等待 `seekToFrameReady` 返回精确的 `ready` 后再打开创建框。Issue 表单以每次打开及 project/task 作为会话边界，冻结提交目标和帧号；清空坐标仍使用任务级空锚点。迟到定位、提交回调不能关闭或改写新任务的表单。讨论页的任务级问题入口直接使用空锚点打开同一表单，并使未完成的像素定位请求失效，不依赖视频准备结果；此会话固定为任务级，不允许手填坐标绕过源帧就绪检查。

列表和时间轴共用受绘制 / Mask 草稿保护的帧定位入口。`VideoStageControls.seekToFrameReady` 返回 `ready / cancelled / timeout / unavailable`；`ready` 要求当前任务与请求仍有效、实际源帧匹配并收到媒体层绘制回执。WebCodecs 位图和原生回退分别验证来源；时间轴的乐观帧号、邻帧容差和计时结束都不能作为成功证据。Mask QC 定位也检查这一结果，失败时不选择标注、不加载比较或聚焦区域。

视频像素锚可包含 `video_context`，写入合同见 [视频问题反馈](../../api/guides/tasks-and-annotations.md#视频问题反馈)。原有 JSONB 保存对象版本、源帧范围、归一化视口中心、相对于 fit 的缩放以及源帧时间窗；没有新增表列或历史回填。读取未知版本时保留原值，但恢复端只使用旧帧与像素锚。对象软删除、版本变化或外键清空不使历史反馈不可读；回复原样继承已有锚点，不能借此修改或伪造上下文。

`useVideoIssueNavigation` 以项目和每次导航意图管理取消信号。先重新检查目标任务权限，再经 `selectTask` 与最新导航调度器处理绘制和 Mask 草稿；将检查过的任务写入已有查询缓存，等待目标标注、真实媒体尺寸及首次 fit。精确帧就绪后，通过 Stage 的恢复租约抑制该次选中触发的普通聚焦，待选中 props、viewport 和 Overlay 时间窗实际提交后才报告成功。各 live 状态仍归原有 Stage / Overlay 所有，租约只保存本次恢复输入。

Stage 的用户操作通知会取消权限预检、媒体等待或视图恢复中的旧意图；内部精确 seek、不吸附的暂停与带有任务复位来源的清空选择不会误取消。所有等待后检查任务、来源代次与取消信号，完成、错误、超时及卸载均释放订阅和租约。原对象已变化时退回帧与像素位置并提示；媒体缩短或现有缩放/时间窗规则夹取目标时说明边界变化。视频问题列表支持项目范围和游标分页，点击事件不依赖当前任务的反馈查询。

### 评论画布批注

评论支持附带画布绘制（`canvas_drawing`），用于在图上圈点说明：

- 评论可以**只有画布批注**（无正文 / 附件）；schema 校验要求 body / attachments / canvas_drawing 至少一个非空。
- 创建时 `canvas_drawing` 会序列化为 JSONB（`model_dump(by_alias=True, mode="json")`）。
- 编辑中评论的待定批注会实时预览到主画布；点击已有评论卡片可把其批注 pin 到画布。预览优先级为 `hover > composing > pinned`（`useHoveredCommentStore`）。

收敛背景与「为何统一」见 [反馈收敛与双写对账](./feedback-convergence)。

## 前端同步点

| 文件                                                    | 为什么要看                                   |
| ------------------------------------------------------- | -------------------------------------------- |
| `apps/web/src/api/tasks.ts`                             | task review 端点定义                         |
| `apps/web/src/hooks/useTasks.ts`                        | approve / reject / reopen mutation           |
| `apps/web/src/pages/Review/ReviewPage.tsx`              | reviewer 列表与批量处理                      |
| `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` | 审核模式共用工作台                           |
| `apps/web/src/pages/Workbench/modes/useReviewMode.tsx`  | reviewer 画布预览、AI 对比与 topbar 操作策略 |
| `apps/web/src/pages/Annotate/AnnotatePage.tsx`          | annotator 整批送审入口                       |
| `apps/web/src/hooks/useBatches.ts`                      | batch reject / reset / transition            |

## 常见误解

### 误解 1：reviewer claim 等于 task lock

不是。

- `reviewer_claimed_at` 锁的是审核所有权
- `task lock` 锁的是标注编辑权

### 误解 2：batch reject 会清 annotation

不会。它只把 task 工作流回退并写 `review_feedback`。

### 误解 3：reopen 和 accept-rejection 是一回事

不是。两者都回到 `in_progress`，但：

- `reopen` 代表“已通过后重新编辑”
- `accept-rejection` 代表“接受退回并重做”

## 相关文档

- [任务模块](./task-module)
- [批次模块](./batch-module)
- [批次生命周期（端到端）](./batch-lifecycle-end-to-end)
- [反馈收敛与双写对账](./feedback-convergence)
- [工作台 Shell 架构](./workbench-shell)
- [ADR-0005](/dev/adr/archive/0005-task-lock-and-review-matrix)
