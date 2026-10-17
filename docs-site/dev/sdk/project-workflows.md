---
title: 项目、批次与数据运维
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 项目、批次与数据运维

SDK 在现有 `projects`、`batches`、`datasets`、`dashboard`、`predictions` 和 `annotations` 上扩展工作流，并提供 `project_performance`、`search`、`guide_assets` 与 `failed_predictions`。先读取当前范围和预检结果，再提交明确的变更。离线 `describe` 展开实际模型与 CLI 参数，不会替调用方生成预览版本或扩大权限。

| 资源       | 主要入口                                                     | 权限与范围                                                      |
| ---------- | ------------------------------------------------------------ | --------------------------------------------------------------- |
| 项目       | query、readiness、类别维护、关联、转移、孤儿清理             | query 按当前可见项目；维护要求管理权限，转移仅超级管理员        |
| 批次       | 分派、切分、管理锁、送审、批量归档／删除                     | 核对项目／批次关系及当前项目职责；导出另需 `export.annotations` |
| 数据集     | 连接器导入、扫描、媒体／尺寸／场景回填                       | 平台项目管理员或超级管理员；数据集目录是全局目录                |
| 成员绩效   | members、member、events、export_members                      | 项目负责人／超级管理员；成员身份本身不授予他人绩效读取权        |
| 搜索       | search.query                                                 | 每类独立 limit；任务额外按分派、批次和协作视频规则过滤          |
| 指引资源   | upload_init、upload_bytes、upload_complete、sign_url、delete | 上传／删除要求项目管理；签名读取按项目可见性                    |
| 预测与标注 | 失败重试／隐藏／恢复、purge、annotations.import_file         | 管理权限与具体 API key scope 分别核对                           |

## 发现与项目查询

`projects.query` 的 `page_size` 为 1–100，支持重复类型条件、成员／项目角色和日期筛选。旧数组 list 保持可用。`readiness` 说明指引、类别、数据集、任务、成员和批次是否已准备；它不证明某个执行器此刻在线。

```bash
aap projects query --page-size 20 --json
aap projects readiness "$PROJECT_ID" --json
aap search query --q inspection --limit 5 --json
aap describe batches.distribution_apply --json
```

搜索的 limit 按项目、任务、数据集和成员分别计算。任务在 limit 之前按当前项目职责、分派和批次状态筛选；启用的协作视频保留项目内读取规则。不要把搜索结果当作之后操作的授权凭据，提交时服务端会重新校验。

类别重命名可指定 `tool_unit_id` 限制配置变更，但同名 annotation 会跨项目内任务更新。`cleanup_annotation_orphans` 默认为 dry-run；实际执行会停用已移除类别的标注，并剪除不再配置的用户属性。它与 `preview_orphan_tasks/cleanup_orphan_tasks` 不同：后者清理无数据来源的任务及关联工作。任务预览只有计数，没有 CAS token；执行会重新计算范围。

ownership transfer 只允许超级管理员执行，目标必须是启用的 `project_admin`。关联数据集和提及候选仍是未分页数组，候选 user UUID、dataset-item UUID、task UUID 与成员关系 ID 不可混用。

## 分派、切批与送审

分派使用 preview → apply。保存最初的 `batch_ids`、接收人、`only_unassigned` 与返回的 `preview_version`，确认后原样提交。409 后重新预览并重新决定，不自动换成新版本重发旧选择。接收人必须是当前项目内启用、职责匹配的成员。

```python
from ai_annotation import BatchDistributionRequest, Client, ReviewedBatchDistribution

PROJECT_ID = "<project-uuid>"
USER_ID = "<annotator-user-uuid>"
BATCH_ID = "<batch-uuid>"

with Client() as client:
    request = BatchDistributionRequest(
        batch_ids=[BATCH_ID], annotator_ids=[USER_ID], only_unassigned=True,
    )
    preview = client.batches.distribution_preview(PROJECT_ID, request)
    print(preview.items, preview.recipient_summary)
    # After reviewing the exact selection and its impact:
    result = client.batches.distribution_apply(PROJECT_ID, ReviewedBatchDistribution(
        **request.model_dump(), preview_version=preview.preview_version,
    ))
    print(result)
```

替换示例中的 UUID。单批次 assignment 的标注员／审核员字段必须明确给出；null 表示清除对应职责。项目 distribution 的 `only_unassigned=false` 才允许覆盖已有分派，确认时核对该影响。

split 只处理未归类或默认批次中的任务。`id_range.item_ids` 是 dataset-item UUID；scene 项目要求 `by_scene`。随机／顺序切分的份数不能超过当前可切任务数，避免创建空批次；不带幂等键，响应不确定时先读取已有批次。

bulk archive/delete 返回 `succeeded/skipped/failed`。CLI 输出完整 JSON，存在 failed 时退出 1；不能将部分成功视为整批失败后全部重发。force delete 会扩大清理范围，可能重置任务并删除预测。管理锁和解锁会发送站内通知。

submit-review 仅允许批次分派标注员或管理者，从 annotating/reviewing 状态执行。它按块提交，可能触发 Mask QC；检查 submitted、skipped 和 remaining，不假定全批原子完成。CLI 在 remaining 非零时输出完整回执后退出 1。批次审计校验父子关系；已删除批次仅保留有可靠项目 detail 或精确路径归属的历史。

LiDAR 项目和批次共用预检请求／结果模型。预检需要 `export.annotations`，最多返回 200 条 issue，并不生成绑定后续导出的摘要。导出时仍会重验当前数据；普通标注员和观察者不能仅凭项目可见性提交批次导出。同步 VOC 下载同样受此权限约束。

## 数据导入与回填

连接器导入验证连接器可用性、主机规则和 source path，并冻结受理时的导入预算。HTTP 202 返回 `job_id`；用 jobs 显式查询该 Job，不自动重复创建或等待。scan 是同步扫描和入库，之后可能排队媒体生成，返回 new_items/linked_tasks 不表示媒体已就绪。

```bash
aap datasets import-from-connection "$DATASET_ID" --body import.json --yes --json
aap datasets backfill-scenes "$DATASET_ID" --mode auto --dry-run --json
aap datasets sniff-axis "$DATASET_ID" --json
```

scene backfill 默认 `dry_run=false`，实际写入须确认；已有 scene 时可能只返回 notes。axis sniff 虽使用 POST，却只返回分析建议，不修改坐标系配置。

尺寸回填每次 1–200 条，返回 processed、failed、remaining_hint。当前 API 没有排序或游标，失败项会继续符合 `width IS NULL`，可能反复占满后续批次；`processed=0` 且 `failed>0` 不能证明完成。CLI 在 failed 非零时输出回执后退出 1，SDK 不自动循环。先检查失败素材；backfill-media 可为缺缩略图的图片同时尝试补尺寸，但不处理“已有缩略图、仍缺尺寸”的全部情况。

backfill-media 只返回 queued，没有可轮询的 AsyncJob ID。核对数据集素材状态；不要把 dataset UUID 填入 jobs.get。

## 指引图片三步上传

init 支持 PNG、JPEG、WebP、GIF、SVG，单文件最多 5 MiB，返回 15 分钟有效的 PUT URL 和 key。直传只发送文件字节和与初始化相同的 Content-Type，隔离平台认证，不跟随重定向；PUT 成功后才明确 complete。

```python
from pathlib import Path
from ai_annotation import Client, GuideAssetUploadCompleteRequest, GuideAssetUploadInitRequest

PROJECT_ID = "<project-uuid>"
source = Path("guide.png")
request = GuideAssetUploadInitRequest(
    filename=source.name, size=source.stat().st_size, content_type="image/png",
)
with Client() as client:
    prepared = client.guide_assets.upload_init(PROJECT_ID, request)
    client.guide_assets.upload_bytes(prepared.upload_url, source, request.content_type)
    asset = client.guide_assets.upload_complete(PROJECT_ID, GuideAssetUploadCompleteRequest(
        key=prepared.key, original_name=source.name, content_type=request.content_type,
    ))
    print(asset.key)
```

complete 重试可能重复登记 key。响应不确定时，先读取当前项目的 guide_assets；不自动重新 init 或 complete。签名 URL 是短期凭据，模型 repr 和默认文本输出隐藏 URL，显式 JSON 保留它。下载签名有效一小时。删除可能在对象存储删除失败后仍清除元数据，成功响应不保证物理对象已擦除。

## 绩效、看板与部分导入

`project_performance.members/member/events` 提供指标、证据和 cursor。路径中的 member_id 是用户 UUID；服务端重新校验当前或历史名册资格。日期默认按 Asia/Shanghai 解释，范围最多 90 个本地日。cursor 为 offset 型，筛选变化后重置；保留 null、partial 和 unknown，不用零值替代缺失证据。

```bash
aap project-performance members "$PROJECT_ID" --work-type annotation --limit 20 --json
aap project-performance events "$PROJECT_ID" "$USER_ID" --limit 20 --json
aap project-performance export "$PROJECT_ID" performance.csv --json
```

绩效 CSV 含 BOM 和范围头，导出整个筛选集；cursor/limit 不限制导出，服务端会在内存构造完整内容。dashboard 的人员列表／导出也不分页。项目管理员必须指定自有项目；人员详情目标须属于该项目，project_count 与活动流按该项目过滤。成本面板仅超级管理员可见，内部统计失败可能返回零值，不能据此证明没有推理。

failed-predictions 列表另要求 API key 的 `predictions:read`；修改仍按管理角色和目标项目归属校验，没有单独 write scope。retry 最多三次，返回 queued 与 failed_id，不是可直接传给 jobs.get 的 Job ID；dismiss/restore 是可恢复的软状态操作。API key 的读 scope 不等于所有接口的只读隔离。

predictions.purge 默认只清 `external_import`，all 还可覆盖 ML 与未知来源。先 dry-run 核对计数，再明确删除。annotation import 使用单个 multipart `file`，只接受 AAP JSON，最多 64 MiB，要求项目管理权限及 API key 的 `annotations:write`。overwrite 只替换导入标注，保留人工标注；返回同步部分回执，CLI 输出完整 JSON 后在 errors 非空时退出 1，不创建可轮询的 AsyncJob。
