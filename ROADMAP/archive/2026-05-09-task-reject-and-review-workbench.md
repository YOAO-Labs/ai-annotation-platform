# 提案 · Task 退回态可见性 + 审核工作台升级

> 状态：**✅ 已归档（M1 + M2 均已实现，2026-05-09）**。M1：rejected 状态机 + 通知 + 前端可见性。M2：WorkbenchShell mode="review" 全功能工作台（方案 B）。方案 C（WorkbenchCore 拆分）已被 2026-05-11 WorkbenchShell decomposition / ADR-0017 取代：后续保留单 Shell + mode hooks + StageHost，不再按两套 wrapper 推进。
>
> 目标：
> 1. 让"被退回的 task"对标注员**可发现、可分流、可追溯**（当前只是 banner，完全依赖标注员主动点开任务）。
> 2. 让审核员能在**与标注员对齐**的工作台里完成"看 → 微调 → 通过 / 退回"，而不是在 268 行的简化页里只能粗粒度通过/退回。

---

## 0. TL;DR

- **议题 A · Task 退回态**：当前 `POST /tasks/{id}/review/reject` 只把 `task.status` 落回 `in_progress` + 写 `reject_reason` + audit log，**不发通知**；标注员只有打开任务才看见 banner。批次级有 `batch.rejected` 通知，task 级缺失。
  - 推荐方案：**新增 `rejected` 终态状态** + 补 `task.rejected` 通知 + 队列/Dashboard 可见徽章 + 标注员"接受退回"动作让 `rejected → in_progress`。比"沿用 in_progress + reject_reason 当哨兵"更干净，迁移成本可控（单 enum 调整 + 一处状态机分支）。
- **议题 B · 审核工作台**：[ReviewWorkbench.tsx](../../apps/web/src/pages/Review/ReviewWorkbench.tsx)（268 行）只复用了 ImageStage，缺 ToolDock / Topbar / TaskQueuePanel / Hotkey / StatusBar / Comments / Skip 全套。
  - 推荐方案：**复用 `WorkbenchShell` + `mode: "review" | "annotate"` prop**，把现有简化页的 diff / approve / reject / claim / ReviewerMiniPanel 当作 review 模式专属附加层注入；不新建独立工作台。比"造一个简化版工作台"省一半代码，且天然继承未来工作台所有性能优化（瓦片 / Hotkey 改造 / 等）。
- 两个议题切两个里程碑：**M1 退回态**（~2-3 天）→ **M2 工作台合并**（~5-6 天）。M2 收尾后 [ReviewWorkbench.tsx](../../apps/web/src/pages/Review/ReviewWorkbench.tsx) 删除。

---

## 1. 议题 A · Task 退回态可见性

### 1.1 现状盘点

| 层 | 现状 | 文件 / 行号 |
|---|---|---|
| 后端 reject 接口 | `task.status = "in_progress"` + `task.reject_reason = reason` + audit log；**无 notification fan-out** | 历史 `apps/api/app/api/v1/tasks.py:894-948`；当前[审核接口](../../apps/api/app/api/v1/tasks/review.py) |
| 后端 approve 接口（对照） | 写 audit log + `notify_many(type="task.approved")` 给 assignee | 历史 `apps/api/app/api/v1/tasks.py:872-888`；当前[审核接口](../../apps/api/app/api/v1/tasks/review.py) |
| 后端 reopen 接口 | `task.reject_reason = None` 清空（即标注员重做就抹掉痕迹） | 历史 `apps/api/app/api/v1/tasks.py:981`；当前[任务生命周期接口](../../apps/api/app/api/v1/tasks/lifecycle.py) |
| 前端工作台横幅 | `status==="in_progress" && reject_reason` 时渲染审核员退回横幅 | [apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx:995](../../apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx#L995) |
| 前端任务队列 | 与普通 `in_progress` 无视觉差异，标注员需要逐个点开才知道哪个被退回 | [TaskQueuePanel.tsx](../../apps/web/src/pages/Workbench/shell/TaskQueuePanel.tsx) |
| 标注员 Dashboard | 仅 KPI"被退回率"百分比，无"待重做退回"清单 | [AnnotatorDashboard.tsx:85-90](../../apps/web/src/pages/Dashboard/AnnotatorDashboard.tsx#L85) |
| 批次级类比 | `batch.rejected` 已发通知 + Kanban 有"已退回"列 + Audit `BATCH_REJECTED` | [audit.py:39](../../apps/api/app/services/audit.py#L39) / [BatchesKanbanView.tsx](../../apps/web/src/pages/Projects/sections/BatchesKanbanView.tsx) |

**核心痛点**：task 级退回是"沉默事件"——除非标注员主动点开队列里的特定 task，否则察觉不到。这与批次级"红色 Kanban 列 + WS 通知 + 邮件"形成强烈反差。

### 1.2 方案选项

**方案 A：哨兵字段路线（最小改动）**

只补通知 + 前端可见性，不改状态枚举。
- 后端：reject 接口加 `notify_many(type="task.rejected")`，类型注册到 [notifications.py](../../apps/api/app/api/v1/notifications.py)。
- 前端：`TaskQueuePanel` / `AnnotatorDashboard` 在 `status==="in_progress" && reject_reason` 时显示"已退回"徽章 / 单独分组。

✅ 改动小（~150 行）；✅ 不动 DB；❌ 状态机仍隐式（"in_progress + reject_reason" 是元组哨兵），后续过滤 / 分组都要带这个三元判断；❌ 标注员"开始重做"和"刚被退回"无法区分（前者要不要清 reject_reason？现在是开新 PATCH 才清）。

**方案 B：新增 `rejected` 状态（推荐）**

| 状态机变化 | before | after |
|---|---|---|
| reviewer 退回 | `review → in_progress` + reject_reason | `review → rejected` + reject_reason |
| 标注员"接受退回 / 开始重做" | 无显式动作（直接编辑 → PATCH 清 reject_reason） | `rejected → in_progress`（显式 `POST /tasks/{id}/accept-rejection` 或前端首次编辑自动） |
| reopen | `completed → in_progress` | `completed → in_progress`（不变） |
| Dashboard 计数 | 看 reject_reason | 看 status |

✅ 状态机干净，过滤 `WHERE status="rejected"` 直接出列表；✅ 与批次级 `rejected` 状态语义对齐；✅ 标注员可在"待办" tab 里看到独立分组；❌ 改动多 1 个迁移 + ~5 处状态机分支扫描（scheduler / batch.check_auto_transitions / Dashboard 统计）。

### 1.3 推荐：方案 B

理由：
- v0.7.x ~ v0.8.x 已多次为 "in_progress + 哨兵" 这种隐式状态付过迁移代价（如 `skipped_at` / `submitted_at`）。再开一个会让"待重做退回任务"的列表查询变三元判断。
- 批次有显式 `rejected`，task 没有，是早期遗漏，不是有意设计。
- 状态扩枚举不破协议（status 字段是 `String(30)`，无 enum 约束，前端早就在处理多状态）。

### 1.4 范围（M1 验收清单）

**后端**
1. Alembic 迁移：无 schema 改动（status 是字符串），仅写一个 data backfill — 把现有 `status="in_progress" AND reject_reason IS NOT NULL` 的 task 迁到 `status="rejected"`。
2. `app/api/v1/tasks.py::reject_task`：`task.status = "rejected"`（替换 line 910）；保留 `reject_reason` + `reviewed_at` 写入；补 `NotificationService.notify_many(type="task.rejected", payload={task_display_id, project_id, reject_reason, reviewer_name})`。
3. 新增 `POST /tasks/{id}/accept-rejection`（或在前端首次 PATCH 时由后端自动 `rejected → in_progress`，二选一；倾向显式接口，便于审计）。
4. `app/services/scheduler.py`：把 `rejected` 加入 `ANNOTATOR_VISIBLE_*` 白名单（如有）；`reopen_task` 不受影响。
5. `app/services/batch.py::check_auto_transitions` 扫一遍，确认 `rejected` task 不会把 batch 误算为 `reviewing/approved`。
6. `notifications.py` 通知类型白名单加 `task.rejected`。
7. 单测：reject_task → status === "rejected"；accept-rejection → in_progress；通知落库 + WS 推送。

**前端**
1. `types`（`api/types.ts`）补 `"rejected"` 到 task status 联合类型；codegen 自动跟。
2. `TaskQueuePanel`：`rejected` 任务红色徽章 + 置顶分组（"待重做退回"）。
3. `WorkbenchShell` 横幅条件从 `status==="in_progress" && reject_reason` 改为 `status==="rejected"`；保留 reject_reason 文案；加"接受退回开始重做"按钮 → 调 `accept-rejection`。
4. `AnnotatorDashboard` 加 "退回待重做" 卡片（计数 + 跳工作台）。
5. `useNotifications` toast：`task.rejected` 文案 "任务 {display_id} 被审核员退回：{reject_reason}"。
6. `ReviewWorkbench` claim 守卫已经过滤 `task?.status !== "review"`；无需改。
7. 测试：`TaskQueuePanel.test`、`useNotifications.test`、`WorkbenchShell.test` 增 `rejected` 分支。

**文档**
- `docs-site/user-guide/workbench/`：补"任务被退回时的处理"段。
- `docs-site/dev/architecture/`：状态机图加 `rejected` 节点。
- ADR：可选 — 如果未来要让 `rejected` 也分给其他标注员（而非原标注员），写一个 ADR；当前版本只回原 assignee。

**估时**：2-3 个工作日。

---

## 2. 议题 B · 审核工作台升级

### 2.1 现状盘点

| 项 | 标注员 `WorkbenchShell` | 审核员 `ReviewWorkbench` |
|---|---|---|
| 行数 | 1280 | 268 |
| 工具栏 | 完整 ToolDock（H/V/B/P/S 6 工具 + 数字键 + Tooltip） | 无 |
| 顶栏 | Topbar（项目名 / 进度 / 主题切换 / 通知中心 / 跳过） | 无 |
| 队列 | TaskQueuePanel（左侧抽屉，prev/next/跳转/筛选） | onPrev/onNext 两按钮 |
| Hotkey | useHotkeys 全套（save/undo/redo/工具切换/导航） | 无 |
| 评论 | CommentsPanel | CommentsPanel ✓（已接入） |
| 标注微调 | ✓ | ❌ 只读，三模式（final/raw/diff） |
| Skip | ✓ | n/a |
| Minimap | ✓ | ✓（继承自 ImageStage） |
| Reviewer 专用 | n/a | ReviewerMiniPanel（今日通过/退回/平均耗时） + 通过/退回按钮 + claim |

**主要痛点**：
1. **审核员发现一个标注框轻微偏移就只能退回 → 标注员看到 reject_reason 修一处再交 → 审核员再 claim 一次**。最低成本应该是审核员**直接拖一下 → 通过**，在审计上挂"审核员微调"标记即可。
2. 缺 hotkey 和队列，审核员长批量审核时操作效率低（没法 J/K 跳上下条）。
3. 两份代码维护成本高，未来工作台所有改造（OpenSeadragon 瓦片 / 暗色按钮 / B-XX 修复）都要双向同步。

### 2.2 方案选项

**方案 A：在 ReviewWorkbench 上扩功能**

把 ToolDock / Topbar / Hotkey / TaskQueuePanel 一项项搬到 ReviewWorkbench。
- ❌ 后续每次工作台改动都要双修
- ❌ 268 → 估计涨到 800+ 行还是简化版
- ✅ 改动隔离，不影响标注员

**方案 B：复用 WorkbenchShell + `mode` prop（推荐）**

`WorkbenchShell` 加 `mode: "annotate" | "review"`，按 mode 切换：

| 区域 | annotate 模式 | review 模式 |
|---|---|---|
| Topbar 右侧 | "提交质检" 按钮 | "通过 / 退回" 按钮 + ReviewerMiniPanel chip |
| 底部状态栏 | StatusBar | StatusBar + Diff Mode 切换（final/raw/diff） |
| ToolDock | 全工具 | 只显 H / V / 选择编辑工具，禁用"新建框"工具（仅微调 + 删 AI 误识别） |
| TaskQueuePanel | 标注员视角的 task list | 当前 reviewer 待审 task list（status="review"） |
| Hotkey | 全 | 复用 + 加 `A` 通过 / `R` 退回 / `J/K` 上下条 |
| Banner | reject_reason | claim 信息 + skip_reason 提示 |

进入条件：
- `/tasks/:taskId/review` 路由 → `<WorkbenchShell mode="review" />`
- `/tasks/:taskId/work` 路由 → `<WorkbenchShell mode="annotate" />`（现状）

✅ 一份代码两种角色；✅ 审核员能微调；✅ 未来工作台改造一次到位；❌ shell 内 `mode` 分支需要清晰的命名空间，初版会有 ~30 处 `if (mode === "review")`；❌ 测试矩阵要乘 2。

**方案 C：拆 shell 出 `WorkbenchCore`，annotate / review 各做一个轻 wrapper**

把 stage / hotkey / queue 抽到 `WorkbenchCore`，annotate / review 各自只写自己的 Topbar / 操作按钮。
- ✅ 比 B 更干净
- ❌ 1280 行的 shell 大手术，估时翻倍；和 v0.10.x SAM 3 工作台改动会冲突

### 2.3 推荐：方案 B（mode prop）

理由：
- 项目期成本最低，且分支处都聚焦在 Topbar / 底栏 / ToolDock 子集开关，**不动 stage / hooks**。
- 审核员能"微调一下直接通过"是真实价值（议题 A 的 rejected 状态会因此降低使用频率，反向印证 A 的 backfill 数据量小）。
- 等 v0.10.x SAM 3 收尾后再做方案 C，那时 shell 已被打磨稳定。

### 2.4 范围（M2 验收清单）

**前置依赖**：M1 退回态合并（避免 reviewer 微调通过的代码路径与 reject 状态机变化撞车）。

**前端**
1. `WorkbenchShell` 加 `mode: "annotate" | "review"` prop（默认 `"annotate"`，向后兼容）。
2. `ToolDock`：mode="review" 时只显 Hand / Select / Edit，hide 新建框 / Polygon / SAM。
3. `Topbar`：mode="review" 右侧渲染 `<ReviewActionBar onApprove onReject />`（替代"提交质检"），加载 ReviewerMiniPanel 数字。
4. `TaskQueuePanel`：mode="review" 时数据源从"我的 task"切到"待我审 task"（已存在 reviewer queue API 复用）。
5. 新增 hotkey `A` (approve) / `R` (reject prompt) / `J/K` (queue prev/next)；与现有 hotkey 冲突检查（`A` 当前是？需 grep [hotkeys.ts](../../apps/web/src/pages/Workbench/state/hotkeys.test.ts) 复审）。
6. 标注微调 → save 时检查 `mode==="review"`，PATCH 走同一 `/annotations/:id` 接口；后端审计 action 用 `TASK_REVIEWER_EDIT`（新枚举）便于回溯"是审核员改的"。
7. `ReviewWorkbench.tsx` + `RejectReasonModal.tsx` 的 review 专属 UI 组件抽到 `apps/web/src/pages/Workbench/review-mode/`，然后从 `ReviewWorkbench.tsx` 删源文件 + 删旧路由组件。
8. ReviewerMiniPanel 保留为 chip 组件，挂到 Topbar。
9. Diff 模式（final/raw/diff）在 mode="review" 时挂在 StatusBar 右侧 segmented control；mode="annotate" 时不渲染。

**后端**
1. `AuditAction.TASK_REVIEWER_EDIT` 新枚举；`PATCH /annotations/:id` 在调用方是 reviewer + task.status==="review" 时写这个 action（其他情况维持现 `ANNOTATION_UPDATE`）。
2. 无 schema 改动。

**路由**
- `/projects/:id/review/:taskId` 渲染 `<WorkbenchShell mode="review" taskId={taskId} />`；旧 `<ReviewPage>` 内部从 `<ReviewWorkbench>` 切到新组件。
- `/workbench/:taskId` 不变。

**测试**
- `WorkbenchShell.review-mode.test.tsx`：mode 切换 → ToolDock 集合差异 / Topbar 按钮差异 / Hotkey A/R 触发。
- `useReviewClaim.test.ts`：保留。
- E2E（playwright，如有）：审核员微调 → 通过 → annotation history 出现 `TASK_REVIEWER_EDIT` 行。

**文档**
- `docs-site/user-guide/review/` 重写："审核员在工作台里直接微调标注后通过" 流程图。
- `docs-site/dev/architecture/`：工作台架构图加 mode 分支。

**估时**：5-6 个工作日。

---

## 3. 切片与依赖

```
M1 (Task 退回态)         M2 (Workbench mode prop)
   ├─ 后端状态机 + 通知       ├─ shell mode prop
   ├─ 前端 banner / queue     ├─ Topbar / ToolDock 分支
   └─ Dashboard               ├─ 审核员微调 + audit
                              └─ ReviewWorkbench.tsx 下线
   2-3 天                     5-6 天
   独立可合                   依赖 M1（rejected 状态可见）
```

可与 v0.10.x SAM 3 主线**并行**：M1 完全独立；M2 与 SAM 3 工作台改动有触碰风险（ToolDock / hotkey），建议 M2 先于 v0.10.x M1 / M2 合并，或与 SAM 3 工作台改动同 PR。

---

## 4. 风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| R1 | M1 引入 `rejected` 状态后，遗漏的旧代码路径仍按 `in_progress` 处理 | grep `status == "in_progress"` 全量过一遍 + 加联合断言（可在 status 入参处用 Literal 类型） |
| R2 | M2 `mode` prop 让 shell 长出大量分支，回到方案 A 的劣势 | 设硬上限：mode 分支不超过 5 处，超过就重构成 `useMode()` hook 暴露布尔；超出预算就回到方案 C |
| R3 | 审核员微调后通过，但标注员"被退回率"统计被污染（误以为自己没问题） | 审计区分 `TASK_REVIEWER_EDIT` 和 `ANNOTATION_UPDATE`；Dashboard "退回率" 只看 `task.status == "rejected"`，不看微调 |
| R4 | `accept-rejection` 显式接口 vs 自动转移，对标注员摩擦感不同 | 倾向自动 — 标注员首次编辑 → API 自动把 status 从 rejected 切回 in_progress，前端 banner 同时降级为"重做中" |

**开放问题**（待用户对齐）：
1. M1 的"接受退回"是否要显式按钮，还是首次编辑自动？倾向自动。
2. M2 审核员微调通过后，标注员能不能在 history 里看到"审核员动过哪些框"？倾向能看（diff），但 v0 先不上 UI，只入 audit。
3. `rejected` 任务是否仍然只能由原 assignee 重做，还是放开给"项目 annotator 池"？倾向先保留原 assignee（与 `reopen_task` 行为一致）。
