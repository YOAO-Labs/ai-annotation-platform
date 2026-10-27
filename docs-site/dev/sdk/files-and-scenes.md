---
title: 文件上传与数据集场景
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-27
---

# 文件上传与数据集场景

`client.files` 管理直接挂在项目任务上的文件上传，`client.scenes` 管理全局数据集目录中的场景与有序位姿。文件 task ID、dataset ID 和 scene ID 是不同身份；这组文件接口不等同于 `client.datasets.upload_files()` 的数据集素材上传。

## 分步上传任务文件

上传初始化、上传字节、确认完成是三个独立阶段。保存初始化返回的 task_id；任何阶段结果不确定时先核对原任务和存储状态，不要重新创建占位任务。初始化和完成要求项目负责人或超级管理员；平台项目管理员不能操作他人负责的项目。

```python
from ai_annotation import Client, FileUploadInitRequest

PROJECT_ID = "<project-uuid>"

with Client() as client:
    upload = client.files.upload_init(FileUploadInitRequest(
        project_id=PROJECT_ID, file_name="frame.jpg", content_type="image/jpeg",
    ))
    print(upload.task_id)
    client.files.upload_content(upload, "frame.jpg", content_type="image/jpeg")
    receipt = client.files.upload_complete(upload.task_id)
    print(receipt.status, receipt.task_id)
```

初始化创建 uploading 任务并返回约 15 分钟有效的 PUT URL。`upload_content` 流式发送本地文件，使用与初始化相同的 Content-Type，不携带平台 Authorization，不自动跟随重定向或重试。字节上传成功后才明确调用 upload_complete；它核验对象存在，将任务改为 pending，并为图片排队缩略图。完成回执不是缩略图处理成功，也不是通用 AsyncJob。

```bash
aap files upload-init --project-id "$PROJECT_ID" --file-name frame.jpg --content-type image/jpeg --yes --json > upload.json
aap files upload-content --init upload.json --file frame.jpg --content-type image/jpeg --yes --json
aap files upload-complete "$TASK_ID" --yes --json
aap files download "$TASK_ID" -o downloaded-frame.jpg
aap files backfill-thumbnails "$PROJECT_ID" --yes --json
```

TASK_ID 取自 upload.json。该文件含短期签名凭据，只在本次上传中使用；URL 不是持久资源身份。
下载先读取当前可见原件的身份，再通过携带平台认证的内容入口传输，校验 SHA-256 与大小后原子替换目标文件。
不再获取可绕过后续权限检查的原件下载签名。backfill 需要项目管理权限，返回包含
`operation_id`、`job_id`、`status`、`accepted_before` 的持久作业回执；通过 `jobs.get(job_id)`
查询执行状态，复用 `Idempotency-Key` 处理接收回执丢失，不把接收成功当成处理完成。

## 读取与更新场景

```python
from ai_annotation import Client, SceneUpdate

DATASET_ID = "<dataset-uuid>"

with Client() as client:
    scenes = client.scenes.list(DATASET_ID)
    if scenes:
        scene = client.scenes.get(scenes[0].id)
        trajectory = client.scenes.trajectory(scene.id)
        print(scene.name, len(trajectory.poses))
```

list 返回未分页数组；trajectory 返回 `{scene_id, poses}`，位姿按 frame_index 排序，没有位姿时是空数组。每项包含时间戳、ego_translation、ego_rotation 和来源元数据，不是任务标注或追踪候选。当前数据集目录是全局读取策略，不按项目成员关系或 created_by 私有化；API key 的三个读取操作要求 `datasets:read`。

场景更新沿用数据集的管理员写策略：只有 project_admin 或 super_admin 可改名称、来源格式和来源元数据。PATCH 只发送明确字段，当前服务器将 null 视为不修改；`source_metadata={}` 可清空元数据。没有 CAS revision，不应把旧 GET 响应整份回写。重复名称返回 409。

```bash
aap scenes list --dataset-id "$DATASET_ID" --json
aap scenes get "$SCENE_ID" --json
aap scenes trajectory "$SCENE_ID" --json
aap scenes update "$SCENE_ID" --body scene-patch.json --yes --json
```

CLI 更新需要至少一个非 null 字段，支持 `--body -`，确认后发送一次请求。项目文件签名／验证／排队前会执行授权；被拒绝的调用不会创建上传任务、签发 URL 或排队后台工作。
