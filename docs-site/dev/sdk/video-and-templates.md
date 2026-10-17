---
title: 视频、模板与存储运维
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 视频、模板与存储运维

SDK 为视频媒体、分段协作、追踪候选和质量审阅提供显式操作。先确定当前账号的任务权限、任务 ID 与 dataset-item ID，再读取必要的状态。运维操作沿用服务端管理员权限；接口进入 SDK 不会扩大账号的访问范围。

## 读取媒体描述与下载

`client.videos` 使用 dataset-item ID，`client.task_videos` 使用 task ID。同一素材可能关联多个任务，任务工作流优先使用明确的 task ID。两套路径分别保留，不能互换 UUID。章节在素材上维护，创建、更新和删除要求项目管理权限。

manifest、chunk 和 frame 都返回 JSON 描述，不直接返回媒体字节。签名 URL 是短期读取凭据，过期后重新读取对应描述；不要把它持久化为资源身份。frame/chunk 的 HTTP 202 表示尚未就绪，读取 body 中的 `status`、`retry_after`，ready 且存在 URL 后才能下载。SDK 下载不会隐式轮询。

读取 segments 可能建立分段记录；chunks/chunk/frame 查询可能建立缓存、排队生成或预热相邻资源。限定帧范围，避免把全视频查询放进频繁轮询。时间表的 HTTP 参数叫 `from`／`to`，chunk 范围叫 `from_frame`／`to_frame`；Python 方法提供可读参数并映射实际别名。帧默认 WebP、宽度 512，可选 JPEG，宽度范围 1–4096。

prefetch 每次 1–500 帧；retry 最多 500 帧，空列表重试匹配的失败记录，force 会清缓存重新生成。task-scoped retry 还要求 `annotations:write`，当前 dataset-item 路径只按可见任务校验；不能把后者理解为绕过任务写权限的通用通道。WebCodecs samples 是 codec、时间戳、偏移和 packet 大小等 JSON 元数据，不是 SDK 本地解码器。

下载原子替换目标文件，失败保留原文件；签名 URL 使用不带平台认证的连接。只接受完整文件响应，不把 HTTP 206 或重定向正文保存为完整文件。需要浏览器 Range 播放时使用相应媒体客户端。

```python
from ai_annotation import Client

TASK_ID = "<task-uuid>"

with Client() as client:
    frame = client.task_videos.frame(TASK_ID, 0, width=512, format="webp")
    if frame.status == "ready" and frame.url:
        client.task_videos.download_frame(frame, "frame.webp")
    else:
        print(frame.status, frame.retry_after, frame.error)
```

## 分段、追踪与审阅

普通标注员先读取任务分段并 claim，按服务端 TTL 显式 heartbeat，处理完后 release 或 submit。创建与接纳追踪时服务端会重验租约，不能只在开始时检查一次。submit 最后一个分段可能将任务转入 review，并触发相邻分段质量检查；reopen 会清审阅证据并使相关质量结果 stale，unassign 会改变分配。这些操作应由明确的工作流决定执行。

追踪模型 key 来自当前注册表与 backend 能力，不是 SDK 固定枚举。首选 task 级 `video:track`，支持单源、多源与无源发现；旧 annotation 路径的 propagate 仍可调用。源 annotation 必须属于任务，无源发现需要已配置的目标类别与工具单位，帧窗口和 segment 必须匹配当前可写范围。

追踪创建没有幂等键。网络中断后先查询该 task 的 active jobs 和 reviewable jobs，再决定是否创建；不要直接重发。轮询 tracker job 的领域状态：`queued/running` 是计算中，`pending_review/partially_reviewed` 是候选待决定，`accepted/discarded` 才是审阅结果。通用 AsyncJob 的 completed 只表示计算结束。

```bash
aap task-videos segments "$TASK_ID" --json
aap task-videos claim-segment "$TASK_ID" "$SEGMENT_ID" --json
aap video-tracker tasks active "$TASK_ID" --json
aap video-tracker tasks reviewable "$TASK_ID" --json
aap video-tracker tasks track "$TASK_ID" --body track.json --json
aap task-videos heartbeat-segment "$TASK_ID" "$SEGMENT_ID" --json
aap video-tracker jobs get "$JOB_ID" --json
aap video-tracker jobs preview "$JOB_ID" --json
aap video-tracker jobs decide "$JOB_ID" --body decision.json --yes --json
aap task-videos release-segment "$TASK_ID" "$SEGMENT_ID" --json
```

这些是分步命令：先核对现有 jobs，再决定是否执行 track；保存返回的 Job ID，在租约有效期间观察并审阅，完成后再释放。`track.json` 的单源示例为下列形状；模型、帧窗口和 UUID 必须换成实际可用值：

```json
{
  "from_frame": 0,
  "to_frame": 120,
  "segment_id": "11111111-1111-4111-8111-111111111111",
  "source_annotation_id": "22222222-2222-4222-8222-222222222222",
  "model_key": "sam2_video",
  "direction": "forward"
}
```

取得 preview 后保留 `job_revision`、`expected_source_versions`、候选 digest 和精确选择。决策选择器为 `instance_ids + from_frame + to_frame` 或 `qc_issue_id + candidate_digest`，不能混用。409 表示证据需要重读和重新决定；SDK 不自动更新版本或改为接纳最新候选。整批 accept/discard 不适用于 correction job，后者必须使用局部 decisions。mask-content 返回 COCO RLE JSON。

Mask 纠错先用当前 annotation version 保存人工关键帧，再提交携带源版本与 mask SHA 的定向 correction job。保存和关键帧操作发送 `If-Match: W/"version"`。bbox fallback 必须明确授权，不能在 mask 能力不满足时静默降级。内容寻址 mask 引用须来自现有 task 上传流程。

```bash
aap video-review mask-upload "$TASK_ID" --body rle.json --yes --json
aap video-review mask-save "$TASK_ID" "$ANNOTATION_ID" 12 --expected-version 7 --body keyframe.json --json
aap video-tracker tasks correct "$TASK_ID" "$ANNOTATION_ID" --body correction.json --yes --json
```

rle.json 包含 `encoding="coco_rle"`、`size=[height,width]` 和非负整数 counts，其和必须等于像素数；上传响应是 keyframe.json 的 mask 引用。保存后重新读取 annotation 的新版本，用它和该 mask SHA 构造 correction.json。上传、保存和提交追踪各自返回独立结果，不会隐式串联；版本数字仅作示例。

相邻已完成分段的 track-quality run 是独立 reviewer 工作流。提交 run 后检查自己的状态和 async_job_id；接受时提交原 input_digest 与完整 pair 决策，stale 结果不可直接接受。轨迹合并、拆分、聚合与转换会按操作停用源 annotation，尤其 `delete_sources=true` 和 split；确认前核对参与对象与范围。

## 项目模板

`client.project_templates` 提供 list/get/create/update/delete/duplicate，列表返回原始数组，没有分页或 CAS revision。list 可按 scope、重复 type_key 和名称 search 过滤。private 仅创建者可见，organization 依组织成员身份，public 对已认证用户可见，super_admin 可见全部；创建和复制要求管理员角色，public 创建仅限 super_admin。当前创建 organization 模板只要求 organization_id，不额外承诺组织成员资格校验。

```python
from ai_annotation import Client, ProjectTemplateCreate

SOURCE_PROJECT_ID = "<source-project-uuid>"

with Client() as client:
    template = client.project_templates.create(ProjectTemplateCreate(
        source_project_id=SOURCE_PROJECT_ID,
    ))
    project = client.projects.create("video-copy", template_id=template.id)
    print(project.id, project.data_type, project.type_label)
```

从源项目创建模板可省略名称与类型字段，服务端按可见源项目补齐。普通模板创建仍要求 name、type_label、type_key。模板／源项目作为默认配置，明确请求字段优先；只明确 data_type 或 type_key 时，另一项按明确的类型推导。未提供模板或源项目的普通项目创建保持既有默认行为。

```bash
aap templates list --scope private --json
aap templates create --body template.json --json
aap projects create --name video-copy --template "$TEMPLATE_ID" --json
aap projects create --name source-copy --source-project "$PROJECT_ID" --copy-annotation-guide --json
aap templates delete "$TEMPLATE_ID" --yes --json
```

template_id 与 source_project_id 互斥；copy_annotation_guide 只用于源项目，不能与模板一起传。模板继承 guide 文本但不复制 guide-assets。任务、标注、数据集、成员、批次、ID 和所有权不复制。duplicate 创建当前用户的 private 副本；PATCH 只发送明确字段，配置合并继续由服务器执行。创建／复制没有幂等键，不自动重试。

## 存储诊断与资产重试

```bash
aap storage health --json
aap storage buckets --json
aap storage video-asset-failures --limit 20 --offset 0 --json
aap storage retry-video-asset --body retry.json --yes --json
```

health 检查标注 bucket，buckets 汇总配置的 bucket；对象枚举可能耗时，应逐项检查 status/error，HTTP 成功不代表全部健康。视频失败列表与重试要求 project_admin 或 super_admin；项目管理员只管理自己拥有项目的关联资产以及无项目关联的素材，服务端在提交时再次检查。

```json
{
  "asset_type": "frame",
  "dataset_item_id": "11111111-1111-4111-8111-111111111111",
  "frame_index": 0,
  "width": 512,
  "format": "webp"
}
```

chunk 重试必须给 chunk_id，frame 必须给 frame_index、width 和 format；probe/poster/frame_timetable 会调用元数据生成。返回 HTTP 202 和 queued 仅确认排队，没有等待修复完成；没有幂等键，结果不确定时先查状态再明确重试。JSON 请求可从 `--body -` 输入，变更按各命令的 `--yes` 确认规则执行，错误保持 stderr 结构化输出能力。
