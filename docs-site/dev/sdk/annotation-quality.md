---
title: 讨论、缺陷报告与质量处理
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 讨论、缺陷报告与质量处理

SDK 提供 `comments`、`annotation_history`、`bug_reports`、`mask_formats`、`mask_qc` 和 `point_cloud_quality`，覆盖讨论、附件、格式预检、质量问题与受控修复。先保留实际资源 UUID、版本、摘要和回执，再决定执行；任务受理、计算结束、人工决策与配置发布是不同状态。

| 资源                | 主要入口                                                                                                     | 关键约束                                             |
| ------------------- | ------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| comments            | list、page、task_page、discussion_page、create/update/delete、附件上传／下载                                 | 当前实际任务可见性；修改／删除另校验作者或管理权限   |
| annotation_history  | annotation、task                                                                                             | 未分页、最新在前；软删除记录仍按实际任务鉴权         |
| bug_reports         | list/list_mine/get、create/update/delete、comment、cluster、截图                                             | 提交者管理自身内容；平台管理员使用全局队列           |
| mask_formats        | list、export_preflight、upload_init/upload_bytes、import_preflight、execute_import、get_import/resume_import | 服务端适配器；精确导入回执、计划摘要及有损确认       |
| mask_qc             | run、issues、compare/content、修复计划／执行／恢复／回滚                                                     | 审核权限与实际任务范围；版本、摘要、有效期与部分结果 |
| point_cloud_quality | runs、issues、evaluations、promote                                                                           | 冻结评审样本；晋升使用基线配置 revision/digest       |

## 讨论与历史

旧 `comments.list` 返回未分页数组。新 page、task_page 和混合 discussion_page 使用 1–200 的 limit 和不透明 cursor；cursor 与父资源、过滤条件绑定，改变条件时从首页开始。混合流保留 comment/feedback 来源类型和服务端 actions，不能仅凭作者 ID 推断编辑权限。

```bash
aap comments page "$ANNOTATION_ID" --limit 20 --json
aap comments discussion "$TASK_ID" --limit 20 --json
aap annotation-history annotation "$ANNOTATION_ID" --json
aap describe comments.create --json
```

历史按最新在前返回，包括软删除记录。权限依据 annotation 实际所属任务的项目、批次与分派；冗余 project_id 不是授权依据。历史不是可恢复快照，也没有分页游标。

创建评论时，空正文必须带附件或画布内容。编辑／删除会重新校验当前任务及作者／管理权限；删除是软删除。评论可能同时生成反馈、审计和提及通知。没有幂等键，响应不确定时先读取讨论，再决定是否重发。

附件分两步：`upload_attachment` 返回可登记的 metadata，随后显式放入 `AnnotationCommentCreate.attachments`；上传本身不创建评论。初始化 PUT URL 有效 15 分钟，没有 complete 接口。评论被拒绝后可能留下未关联对象。

`attachment_download_url` 使用显式 JSON 变体；`download_attachment` 验证 API 的 302 后，通过不带平台认证的连接下载绝对 HTTP(S) 地址。下载签名有效 5 分钟，模型 repr 隐藏 URL，显式 JSON 保留 URL。存储响应必须是完整 HTTP 200，失败保留旧目标文件。

## 缺陷报告

普通账号只能读取、评论自己的报告，并修改 title、description、severity；这三个字段不能显式置 null。平台 `project_admin` 和 `super_admin` 使用全局缺陷队列，不要求拥有报告 context 中的项目。context 的项目／任务 ID 仅用于诊断，不授予项目访问权。

管理员可修改状态、分派、重复关系和处理结果。`duplicate_of_id`、`assigned_to_id`、`fixed_in_version`、`resolution` 的显式 null 表示清除。混合提交内容与无权管理字段会整体拒绝。delete 永久删除报告；cluster 只计算最多 50 个新报告的重复建议，不自动合并。

```bash
aap bug-reports mine --limit 20 --json
aap bug-reports list --markdown
```

列表使用 limit/offset，limit 最多 200。默认 JSON 是 typed page，Markdown 格式返回 `text/markdown` 文本。提交者对 fixed、wont_fix、duplicate 报告发表评论可能重新打开为 triaged，每天最多 5 次；评论另有每小时 60 条限制。

截图支持 PNG、JPEG、WebP。`upload_screenshot` 在任何请求前拒绝超过 10 MiB 的本地文件；该限制对应可登记 metadata 的上限，服务端并未按此字段验证实际对象大小。上传返回 metadata 后仍需显式创建报告。签名下载校验 302 后使用无平台凭据的完整下载。

## Mask 格式导入与导出

list 返回项目媒体类型下的服务端适配器能力。SDK 不实现另一套格式转换器。export_preflight 要求 `export.annotations`，给出格式损失、跳过项和估算；其摘要是参考信息，后续项目导出没有消费该摘要的请求字段，仍会检查当时的数据。

导入明确分为 init → PUT → preflight → execute → query：

1. `upload_init` 获取 15 分钟有效的地址与 object_key。
2. `upload_bytes` 只 PUT 文件字节和匹配的 Content-Type；不带平台凭据、不跟随重定向，只有 2xx 才成功。
3. `import_preflight` 提交该 object_key、实际文件 SHA-256、format_id、mapping 和 options。
4. 核对计划的 losses/skips/warnings，保存 import_id、receipt 和 plan_digest。回执有效 15 分钟。
5. `execute_import` 原样提交回执和摘要；有损计划另需明确 `confirm_lossy=true`。查询 import domain status 与 async_job_id，不能只根据 Job 结束判断全部成功。

```python
from ai_annotation import Client, MaskFormatImportExecuteRequest

PROJECT_ID = "<project-uuid>"
RECEIPT = "<preflight-receipt>"
PLAN_DIGEST = "<sha256-plan-digest>"

with Client() as client:
    # Use the exact reviewed preflight receipt and digest.
    result = client.mask_formats.execute_import(PROJECT_ID, MaskFormatImportExecuteRequest(
        receipt=RECEIPT, plan_digest=PLAN_DIGEST, confirm_lossy=False,
    ))
    print(result.id, result.status, result.async_job_id)
```

receipt 是短期凭据，默认 repr 隐藏、显式 JSON 保留。过期、冲突或失败后不自动刷新证明重发。resume 只接受服务端判断可恢复的失败导入。

## Mask QC 与修复

项目范围扫描要求项目管理权限；task_ids／annotation_ids 范围允许具备审核权限的主体，仍校验实际任务与 annotation 归属。输入相同的 run 可被复用；HTTP 202 只说明受理。问题列表使用最多 200 条的 keyset page。

compare 绑定 annotation_version、baseline 和可选原始帧。compare_content／issue_region_content 返回 COCO RLE JSON，保留精确 digest；SDK 不发送条件缓存头。若 compare side 的 source 为 tracker_candidate，用 `video_tracker.get_mask_content(side.candidate_job_id, side.digest)` 读取该候选，不能把任意 content_path 作为通用执行地址。

`dry_run_repairs` 会持久化计划和回执，但不修改标注。检查每项适用性和跳过原因后，使用 15 分钟内的 actor-owned receipt 与原 plan_digest 执行。保留 repair batch、AsyncJob 和逐项结果；CLI 遇到 partial、failed、cancelled 或 rollback_failed 会先输出完整回执，再退出 1。

```bash
aap mask-qc task-summary "$TASK_ID" --json
aap describe mask_qc.dry_run_repairs --json
aap describe mask_qc.rollback_repairs --json
```

resume 只针对可恢复的 failed/partial。rollback 只接受保留期内 completed/partial 的 batch，并要求 expected_result_digest；它是显式异步修改，不是失败后的自动补偿。409 后重新读取并重新决定，SDK 不更换版本、摘要或回执自动重试。

## 点云质量与评估晋升

读取要求 `review.write` 与当前任务可见性，project/scene 扫描另要求管理权限。问题保留 scene、frame、annotation 和 source_versions。list_issues/get_issue 可能根据输入变化持久化 stale 状态；GET 不一定没有服务端状态影响。wont_fix 必须给 reason，并保留实际 review_verdict。

run 创建可能复用同一来源／配置／范围。派发失败的 503 会返回 `reason`、`run_id`、`async_job_id`，对应已持久化的失败记录。Python 异常 detail 保留原字段；`aap --error-format json` 提供 reason、run_id、job_id。先用 get_run／jobs.get 查看记录，再决定是否重新创建。

```bash
aap point-cloud-quality issues "$PROJECT_ID" --limit 20 --json
aap point-cloud-quality run-get "$PROJECT_ID" "$RUN_ID" --json
aap describe point_cloud_quality.promote_evaluation --json
```

evaluation 同步冻结已人工评审且未过期的样本。候选 PointCloudQualityConfig 严格校验未知字段；Project 的 PointCloudQualityConfigRead 保留新增字段与较新 schema 的读取内容。评估通过只代表可晋升；promote 由项目管理者明确调用，检查当前基线 revision/digest，不自动重新评估或覆盖已改变的配置。
