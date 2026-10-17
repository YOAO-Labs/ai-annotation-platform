---
title: 通知、反馈与预标注
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 通知、反馈与预标注

这些入口让脚本与 Agent 发现待办、跟进反馈，并显式准备和提交推理。开始前先离线查看安装包里的参数与约束，再使用当前账号读取服务器状态：

```bash
aap capabilities --tag notifications --status covered --json
aap describe feedbacks.thread --json
aap describe pipelines.apply --json
aap describe pipelines.preannotate --json
```

## 读取通知并保留处理状态

`client.notifications.list(unread_only=True, limit=20, offset=0)` 返回当前账号的 `items`、匹配总数 `total` 与未读数 `unread`。列表上限为 100；偏移分页期间若通知变化，不能把页码当作稳定快照。通知类型与 payload 可扩展，先检查类型和目标，再使用目标资源的 API 核对当前权限与状态。

`unread_count()` 读取角标计数。业务处理完成后显式调用 `mark_read(notification_id)`；已读或不存在的通知可能返回 404。`mark_all_read()` 改变整个当前账号的未读状态，不等于逐项完成业务。`delete()` 删除一条，`clear_read()` 清理当前账号所有已读通知；这些操作不删除原始反馈或任务。

```bash
aap notifications list --unread-only --limit 20 --offset 0 --json
aap notifications unread-count --json
aap notifications preferences --json
aap notifications delete "$NOTIFICATION_ID" --yes --json
```

偏好更新使用 `NotificationPreferenceUpdate`，只发送明确修改的频道。例如从 `preferences()` 返回的项目中取得合法 `type` 后，提交 `toast=False` 只改变弹窗选择，保留未提供的 `in_app`。显式 null 与未包含任何频道的更新会被拒绝；`email` 是读取字段，当前更新接口不接受它。通知类型是否仍有效由服务器决定。

## 阅读完整反馈线程并明确处理

```python
from ai_annotation import Client, AnnotationFeedbackPatch, AnnotationFeedbackReply

ROOT_ID = "<root-feedback-uuid>"
FEEDBACK_ID = "<feedback-uuid>"

with Client() as client:
    page = client.feedbacks.thread(ROOT_ID, limit=50)
    # root 单独返回；items 是这一页活跃后代，total 不包含 root。
    print(page.root, page.items, page.total)
    if page.next_cursor:
        next_page = client.feedbacks.thread(ROOT_ID, cursor=page.next_cursor, limit=50)
    client.feedbacks.reply(FEEDBACK_ID, AnnotationFeedbackReply(body="已核对，稍后复查"))
    client.feedbacks.patch(ROOT_ID, AnnotationFeedbackPatch(status="resolved"))
```

线程按创建时间和 ID 倒序分页，游标绑定根反馈 ID；将 `next_cursor` 原样用于同一根线程。不要用普通反馈列表的游标替代线程游标。删除的中间回复仍可能有可见后代，不能据本页重建完整树或把 `total` 解释为直接回复数。

回复仅提交正文与附件，不携带父项的锚点、项目、类型或提及；服务器从父项继承范围。无正文也无附件的回复会被拒绝。回复没有幂等键，网络结果不确定时先重新读取线程核对，避免重复发言。

补丁区分省略和显式 null：当前服务器把 `title`、`severity`、`body` 等字段的 null 当作不修改，不能用 null 清空内容；`mentions: []` 才清空提及。正文编辑未显式给出 mentions 时，服务器会清理过时的提及偏移。质检员的状态修改权限与作者／负责人内容编辑权限不同，混入内容字段，即使是 null，也可能被拒绝。

```bash
aap feedbacks thread "$ROOT_ID" --limit 50 --json
aap --error-format json feedbacks reply "$FEEDBACK_ID" --body reply.json --json
aap --error-format json feedbacks patch "$ROOT_ID" --body - --json < patch.json
aap feedbacks delete "$FEEDBACK_ID" --yes --json
```

`delete` 为软删除，成功响应没有 JSON body；CLI 只在成功后输出删除确认。根项被删后线程可能不可读；删除回复不代表递归删除后代。`actions` 描述当前可见操作，提交时服务器仍重新检查账号、项目、任务、锚点新鲜度与操作资格。

## 保存编排与复制到目标项目

`client.pipelines` 提供 `list`、`get`、`create`、`update`、`delete`、`apply` 和 `preannotate`。库列表可按 scope、项目和组织筛选；当前 API 返回完整数组，没有 limit/offset，不应把本地截断伪装成服务端分页。

| 范围           | 创建请求的归属                        | 默认行为                     |
| -------------- | ------------------------------------- | ---------------------------- |
| `private`      | 提供 `project_id`，不提供组织 ID      | 可显式设为项目默认           |
| `organization` | 提供 `organization_id`，不提供项目 ID | 不能设为项目默认             |
| `public`       | 不提供项目或组织 ID                   | 创建要求服务器允许的管理身份 |

更新只发送明确提供的字段，迁移范围时保留显式清除旧归属的 null。服务器对合并后的范围、组织成员关系与默认编排资格做最终校验。

`apply(project_id, ProjectPipelineApplyRequest(...))` 把可见库模板复制为目标项目的私有编排；`set_default=True` 会切换项目默认编排。它不执行推理，也不生成异步 Job。服务器在复制时核对引用的 backend 是否已在目标项目启用。

```bash
aap pipelines list --scope private --project "$PROJECT_ID" --json
aap pipelines get "$PIPELINE_ID" --json
aap pipelines apply --project "$PROJECT_ID" --body apply.json --yes --json
```

保存时校验阶段结构，复制时校验目标项目 backend，执行时还校验模型能力与任务状态；这些是不同步骤。当前没有独立的 validate 或 dry-run API，保存成功不能代替执行资格检查。

## 带明确范围和幂等身份提交预标注

```python
from ai_annotation import Client, PipelineStage, PreannotateRequest

PROJECT_ID = "<project-uuid>"
TASK_ID = "<eligible-task-uuid>"
BACKEND_ID = "<enabled-backend-uuid>"
IDEMPOTENCY_KEY = "<caller-persisted-idempotency-key>"

request = PreannotateRequest(
    task_ids=[TASK_ID],
    output_mode="box",
    pipeline_stages=[PipelineStage(stage=7, ml_backend_id=BACKEND_ID, task="detect")],
)

with Client() as client:
    accepted = client.pipelines.preannotate(
        PROJECT_ID, request, idempotency_key=IDEMPOTENCY_KEY,
    )
    print(accepted.job_id, accepted.status, accepted.total_tasks)
    current = client.jobs.get(accepted.job_id)
```

根阶段由 `parent_stage is None` 决定，不要求编号为零。动态模型参数与 source/input/roi/write 等配置保留原始字典，由服务器现有 schema 与能力校验器负责资格判断。

`bulk` 范围只处理服务器允许的待标注任务，批次须为 active 或尚未分配人员的 draft；任务、批次、项目、锁状态必须一致。`workbench` 范围要求一个明确的当前任务且不传 batch_id，并沿用当前题的编辑资格。不要在被拒绝后切换范围来绕过限制。显式任务列表最多 200 项。

```bash
aap --error-format json pipelines preannotate --project "$PROJECT_ID" --body request.json --idempotency-key "$IDEMPOTENCY_KEY" --yes --json
aap jobs get "$JOB_ID" --json
```

发送前保存完整请求与幂等键。相同键和不同请求会冲突；结果不确定时核对原 Job 并沿用原键／原请求，不生成新键。SDK 不自动重试提交，CLI 不隐式等待；HTTP 成功或 acknowledgement 的 status 不代表推理已经完成。可用 `jobs.get` 读取当前状态，或调用带明确超时的 Python `jobs.wait`。

所有接口沿用当前账号与服务器授权。可见性、组织权限、项目管理权和任务范围不能仅按 HTTP 方法推断；机器发现目录不授予权限，也不证明 backend 或执行器在线。错误处理与确认规则见 [Agent 接入与操作](./agents) 和 [CLI 参考](./cli)。
