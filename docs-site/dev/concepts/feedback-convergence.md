---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-24
---

# 反馈收敛与双写对账

这页讲 ADR-0027「反馈统一表」迁移中的两件事：

- **收敛目标**：把历史上散在 4 处的反馈入口收口为 `annotation_feedbacks` 单一写入源
- **对账安全网**：在切单源之前，用一个每日定时任务持续给出「双写零漂移」的证据

如果你要改 `bug_reports` / `annotation_comments` / `tasks.reject_reason` 的写路径，或想理解 superadmin 收到「反馈双写对账发现不一致」通知意味着什么，先读这页。背景规范见 [ADR-0027](/dev/adr/archive/0027-annotation-feedback-unified-table)，审计与通知机制见 [审计与通知](./audit-and-notifications)。

## 为什么需要收敛

历史上反馈分散在 4 张表 / 字段里，语义重叠但各写各的：

| 来源                  | 含义           |
| --------------------- | -------------- |
| `bug_reports`         | 产品 BUG       |
| `annotation_comments` | 标注评论       |
| `tasks.reject_reason` | 审核驳回理由   |
| pixel-anchored issue  | 像素锚点 issue |

ADR-0027 立新表 `annotation_feedbacks`，用 `anchor_type`（project / task / annotation / pixel）+ `kind`（issue / comment / reject / bug）统一锚点与类型，目标是收口为单一写入入口。迁移按三段式推进，每段独立可回退（详见 [审计与通知 §反馈统一表](./audit-and-notifications#反馈统一表-adr-0027)）。

当前处于「双写已稳定、准备切单源」的窗口期：旧三处写路径仍通过 `FeedbackService.mirror_*` helper 同事务双写到新表。切单源前必须先证明双写没有持续丢行——这就是对账任务存在的理由。

## 双写对账任务

### 它做什么

每日定时比对「旧表中应被 mirror 的行」与「新表中实际镜像的行」，统计每个来源的缺失数（drift）。drift 长期为 0 是删旧写路径的前置条件。

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/feedback-reconciliation-guard.svg"
  alt="bug report、标注评论和任务驳回在原业务事务中镜像到 annotation_feedbacks，定时任务逐源按业务键与分组行号定位缺失，并在漂移时审计、通知和告警的迁移门禁"
  caption="反馈双写对账：检测应镜像旧行的缺失，为切单源提供长期零漂移证据"
/>

### 代码入口

| 位置                                          | 作用                                                                      |
| --------------------------------------------- | ------------------------------------------------------------------------- |
| `apps/api/app/services/feedback_reconcile.py` | `compute_feedback_drift()` 纯对账逻辑                                     |
| `apps/api/app/workers/feedback_reconcile.py`  | `reconcile_annotation_feedback()` Celery 任务包装：跑对账 + 落 audit/通知 |
| `apps/api/app/workers/celery_app.py`          | beat schedule 注册（`reconcile-annotation-feedback`）+ worker include     |
| `apps/api/app/services/feedback.py`           | `mirror_*` 将三条旧写路径在同一事务中镜像到统一表                         |
| `apps/api/app/services/audit.py`              | `AuditAction.FEEDBACK_RECONCILE_DRIFT = "feedback.reconcile_drift"`       |

### 对账逻辑

`compute_feedback_drift()` 对三类 mirror 行逐源比对三个维度：

- `expected`：旧表中「按业务口径应被 mirror 的行数」（例如 `bug_reports.project_id IS NULL` 的登录页 bug 不 mirror，故排除）
- `actual`：旧表应 mirror 行中，能在 `annotation_feedbacks` 找到对应镜像的覆盖数，即 `expected - missing`；它不是统一表中该 `kind` 的原始总数
- `missing_ids`：旧表中未在统一表找到镜像的主键列表

匹配是**按业务字段回连而非主键**，因为 mirror 行有独立主键。实现会对同一业务键下的旧行做 `ROW_NUMBER()`，再与对应 mirror 行的 `COUNT(*)` 比较：如果 N 条内容完全相同的旧行只镜像了 M 条，仍能精确返回 N-M 个缺失 ID，不会因“存在任一镜像”而漏报。

| 旧表                  | mirror `kind` | 回连字段                                           |
| --------------------- | ------------- | -------------------------------------------------- |
| `bug_reports`         | `bug`         | `(project_id, title, body=description, author_id)` |
| `annotation_comments` | `comment`     | 评论业务字段                                       |
| `tasks`（reject）     | `reject`      | 任务驳回业务字段                                   |

对账查询直接读 `annotation_feedbacks`，不会把 `v_annotation_feedback_unified` 里由 legacy 表组成的读模型分支误当成 mirror。pixel-anchored issue 原生写统一表，没有旧表权威源，因此也不参与这三类 mirror 对账。

### 调度与配置

```python
# apps/api/app/workers/celery_app.py
"reconcile-annotation-feedback": {
    "task": "app.workers.feedback_reconcile.reconcile_annotation_feedback",
    "schedule": crontab(hour=3, minute=0),  # 每日 03:00 UTC，避开 03:30 分区维护
},
```

无专有环境变量，复用通用 celery broker/backend 配置（`settings.effective_celery_broker`）。

::: tip 改 worker 业务代码后要重启
Celery 没有 `--reload`，编辑 `apps/api/app/workers/**` 后需重启受影响的 Celery worker / beat 容器才会生效。详见 [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart)。
:::

## 发现漂移时会发生什么

当 `total_missing > 0`：

1. **审计**：写一条 `action=FEEDBACK_RECONCILE_DRIFT`、`target_type='feedback_reconcile'` 的 audit，`detail` 含完整 drift 字典与 `total_missing`
2. **通知**：向所有 superadmin 发 `type='feedback.reconcile_drift'` 通知，`target_id` 用 nil UUID（无单一对象），payload 为 `{total_missing, missing_by_source}`，前端展示文案「反馈双写对账发现不一致」
3. **日志**：`log.warning` 打印 drift 详情，便于排障（配合 ADR-0027 双写日志关键字 `[ADR-0027 double-write]`）

drift=0 时只写 `log.info`，不打扰任何人。

当前门禁只回答“应 mirror 的旧行是否缺失”：它不自动 backfill，也不检测或清理多出的孤立 mirror 行。

::: warning superadmin 收到该通知意味着什么
说明某条旧表反馈没成功双写到 `annotation_feedbacks`。这是切单源的阻塞信号：应先按 `missing_by_source` 定位丢行来源、修复双写路径或 backfill，确认漂移归零后再推进删旧写路径。
:::

## 与统一表迁移的关系

对账任务是 ADR-0027 第三段（切单源）的安全网：

- 本任务提供长期零漂移证据
- drift 持续为 0 后，删旧写路径、旧表保留只读一个版本作回退

切单源完成后该任务仍可保留为回归守卫；旧表彻底下线后再考虑移除。

## 报告附件的独立存储归属

报告的附件元数据用于展示和登记关系，`bug_attachment_uploads` 保存物理文件责任。每次上传使用独立 UUID，记录上传者 UUID 快照、严格 `bug-reports` 存储引用、原始字节的 SHA-256、大小、真实 MIME、安全文件名和固定保存期限。上传者与绑定报告均不设外键，删除账号或报告后仍保留可追溯的清理依据。

对象键固定为 `bug-report-attachments/{owner_id}/{upload_id}.{png|jpg|webp}`，JPEG 使用 `jpg`。新分配核实平台位置及九种用途的已登记绑定；历史记录始终解析自身保存的端点、桶和精确键。切换当前新写桶不能改变既有附件的读取或删除位置。存储引用只允许表所有者在受限恢复事务中同时映射桶与存储身份，其余内容、身份和期限不可变。

附件状态只按 `pending → ready → retired` 向前，也允许失败候选从 `pending` 直接退休。`pending` 必须未绑定；报告关系只能在 `ready → ready` 时首次从空值写入，随后不能解除或改绑。退休时间一旦写入便不可改变。保存期限从创建时固定，最长 180 天，不随下载、报告绑定或重试延长；任何已有附件责任都会阻止破坏性迁移降级。

存储服务接受最多 10 MiB 的 PNG、JPEG、WebP 原始字节，核实声明 MIME、真实格式、4000 万像素预算和图片完整性，不重编码。写入使用保存桶上的条件创建，禁止覆盖同键对象；写后按保存回执完整回读。读取使用 HEAD、带 `If-Match` 的有界 GET、完整 SHA/大小校验、注册句柄关闭及最终 HEAD，区分缺失、损坏和存储暂不可用。关闭句柄的结果未知时保留未收尾证据，不能降级为“文件不存在”。失败不授权补偿删除。

业务工作流负责请求身份复核、报告可见性、一次性绑定、期限和耐久读写准入。存储服务只处理严格归属与字节 I/O，不自行推进数据库状态。历史上没有独立上传记录的附件不能据当前桶猜测归属，应保留原文件待核对；`pending` 也不能仅因年龄自动回收。

## 相关文档

- [ADR-0027 反馈统一表](/dev/adr/archive/0027-annotation-feedback-unified-table)
- [审计与通知](./audit-and-notifications)
- [审核模块](./review-module)
- [工作台 Shell 架构](./workbench-shell)
