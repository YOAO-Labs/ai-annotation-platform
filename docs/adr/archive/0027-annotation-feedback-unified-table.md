# 0027 — AnnotationFeedback 统一反馈表

- **Status:** Accepted
- **Date:** 2026-05-19
- **Deciders:** core team
- **Supersedes:** —

## Context

平台目前有 **4 处分散的反馈入口**, 各自维护独立 schema 与 router:

| 表 / 字段 | 文件 | 用途 |
|---|---|---|
| `bug_reports` | `app/db/models/bug_report.py` | BugReportDrawer 提交的产品 BUG, 带 severity / 截图 / API/console 上下文 |
| `annotation_comments` | `app/db/models/annotation_comment.py` | 单 annotation 评论 + canvas 批注 + mentions / attachments |
| `tasks.reject_reason` / `reject_reason_type` | `app/db/models/task.py` | reviewer 驳回时的自由文本 + 4 类结构化枚举 |
| (未实现) Issue | — | ROADMAP §C.7 I18 计划的"图像像素位置图钉评论" |

**问题症状**: 越演化越难合并(取经合集 §2.2)。reviewer 想"在图像空白处指着说漏标了"无落点 — `annotation_comments` 必须挂 annotation, `tasks.reject_reason` 是 task 级单字符串, `bug_reports` 又是产品反馈通道。新增 pixel-anchored issue 时, 如果再开一张独立 `issues` 表就成第 5 个反馈入口。

CVAT `Issue` + `Comment` 走的就是统一锚点模型 (`anchor_type`: project/task/annotation/pixel), 实践证明可行。

## Decision

新立 `annotation_feedbacks` 表作为**未来唯一反馈写入入口**, 字段覆盖 4 处的并集 + 统一锚点模型:

```python
class AnnotationFeedback:
    id: UUID
    kind: Literal["issue", "comment", "reject", "bug"]
    anchor_type: Literal["project", "task", "annotation", "pixel"]
    project_id: UUID                 # 所有 kind 必填
    task_id: UUID | None             # task/annotation/pixel anchor 必填
    annotation_id: UUID | None       # annotation anchor 必填
    anchor_position: JSONB | None    # pixel anchor: {x, y, frame?}
    status: Literal["open", "resolved", "wont_fix"]
    severity: Literal["info", "warn", "blocker"] | None
    title: str | None
    body: Text
    attachments: JSONB                # 复用 AnnotationComment.attachments 形态
    thread_parent_id: UUID | None    # 自引用, 形成评论链
    author_id / created_at / updated_at / resolved_at / resolved_by_id / is_active
```

**关键约束 (CHECK constraint, 而非仅应用层校验)**: anchor_type 与 task_id/annotation_id/anchor_position 的存在性强一致, 防止脏数据 (例如 anchor_type='task' 却带 anchor_position)。

API: `GET /feedbacks?project_id=&task_id=&annotation_id=&kind=&anchor_type=&status=` + POST/PATCH/DELETE/replies。

任务反馈只写反馈记录，不修改批次或任务。命令按批次、任务顺序持有 `FOR SHARE NOWAIT`，在同一事务内重验任务归属与可见性。共享锁允许原件读取并行，但会拒绝实际编辑竞争；使用排他锁会让正常播放触发无关的「批次忙」。标注和反馈线程沿用各自的写锁。

## Migration 策略 (三段式, 单步可回退)

**v0.10.19 (本切片)**: 仅立新表 + 新 API。旧 `bug_reports` / `annotation_comments` / `tasks.reject_reason` 读写路径**完全不动**。前端 IssueLayer (I18) 直接读新表, 因为这本来就是新功能, 没有旧数据。

**v0.10.20 (双写阶段)** ✅ 已落: alembic 0077 加 UNION ALL view `v_annotation_feedback_unified` (字段对齐到统一 schema, 带额外 `source_table` 列方便对账)。`FeedbackService.mirror_bug_report / mirror_annotation_comment / mirror_task_reject` 3 个 helper 接入旧三处写路径 (`BugReportService.create` / `annotation_comments.py:create_comment` / `tasks.py:reject_task`), 同事务 INSERT `annotation_feedbacks`, 失败一起回滚。**前端只读暂不切到 view** (view 仅作后端对账 + v0.10.21 切单源前的过渡; 切了会让旧 bug / reject 也出现在前端 useFeedbacks 列表, UX 未必合适)。bug_reports.project_id IS NULL 行不 mirror (登录页等无项目归属 bug 暂留旧表), v0.10.21 切单源时单独处理。

**v0.10.21 (切单源)**: 验证双写一致性后, 删除旧表的写路径, 旧表存量数据通过一次性 backfill migration 灌入 `annotation_feedbacks`, 旧表保留只读一个版本作回退。

**v0.11+**: 删除旧表。

每一步都可独立回退到上一步, 不需要一次性"大迁移"。

## Consequences

### 正向
- **新反馈类型零成本**: I18 (pixel issue) / 未来 §B "Slack/Webhook 集成" / §B LLM 聚类去重都基于同一张表, 不再发明新模型。
- **统一前端组件**: DiscussionPanel (I4) 一个组件渲染所有 kind, 按 kind 派生颜色 / icon / 卡片样式。
- **审计取证简化**: `audit_logs` 不再为 4 个 action 类型各维护 detail helper, 统一走 `FEEDBACK_CREATED` / `FEEDBACK_STATUS_CHANGED` / `FEEDBACK_DELETED`。
- **CVAT 对标**: 取经合集 §2.2 设计原样落地, 路线图未来对接 LLM-as-Judge reject 建议 (§5.1) 时直接走统一表。

### 负向 / 风险
- **双写阶段一致性风险**: v0.10.20 在两张表同时写时, 任一失败需要事务回滚或重试机制。**缓解**: 双写在同一 SQLAlchemy session 内, 一致性由 PG 事务保证;监控双写不一致行 (cron 每日跑 view 对账)。
- **JSONB anchor_position 校验只在应用层 + CHECK**: pixel anchor 的 `{x, y, frame}` 字段名 schema 没有强约束, 后续如果想扩 `{x, y, z}` (3D) 需要协议升级。**缓解**: pydantic `FeedbackAnchorPosition` 是显式字段而非 dict, 协议演进走版本号。
- **kind/status enum 走字符串 + CHECK 而非 PG enum 类型**: 修字典值时需要 alembic 改 CHECK, 不能直接 `ALTER TYPE`。**理由**: 与平台其它 status 字段 (Task.status, batch.status) 风格一致, 平台从不引入 PG enum 类型 (避免 PG 强类型与 alembic 协作的复杂度)。

## Alternatives Considered

### A. 扩展 `annotation_comments` 表加 anchor_type 字段
拒绝。该表已有 6 处生产代码读, 加多类型字段会让"评论"语义模糊;且 `body NOT NULL` / `annotation_id NOT NULL` 在 schema 层就阻止了 project/task/pixel anchor。

### B. 维持 4 张表, 仅前端做 union 渲染
拒绝。前端 union 4 个 API + 4 套类型每次新增 kind 都翻倍工作量, 与"早做比晚做便宜" (取经合集 §2.2) 相悖。

### C. 一次性大迁移 (v0.10.19 直接灌数据 + 删旧表)
拒绝。BugReport / AnnotationComment 都有生产数据;一次性迁移失败回滚成本巨大;三段式可在每步发现问题时停止。

## Related

- 取经合集 §2.2 (`ROADMAP/2026-05-18-cvat-labelstudio-inspiration.md`)
- ROADMAP §C.7 I18 (Issue 锚定到像素位置)
- ROADMAP §C.7 I4 (评论/历史常驻 — DiscussionPanel 复用统一表)
- 未来对接: §B Slack/Webhook 集成, §5.1 LLM-as-Judge reject 建议
