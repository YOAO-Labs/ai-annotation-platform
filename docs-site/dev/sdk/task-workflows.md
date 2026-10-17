---
title: 任务上下文、租约与几何工作流
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-17
---

# 任务上下文、租约与几何工作流

`task_context`、`image_pyramids`、`cross_frame_jobs`、`task_locks`、`point_cloud`、`scene_tracks` 和 `annotation_operations` 提供任务级读取、媒体与跨帧处理。能力发现仍以具体方法为入口；UUID、源帧、原始 shape_index、版本、摘要及预检凭据不能互相替代。

| 资源                  | 工作流                                       | 需要保留                                                    |
| --------------------- | -------------------------------------------- | ----------------------------------------------------------- |
| task_context          | 标注分页、邻帧、Mask 能力、预测候选          | cursor、video_segment_id、原始 shape_index                  |
| image_pyramids        | manifest、asset_urls、retry                  | generation、签名 URL 有效期、构建状态                       |
| cross_frame_jobs      | list、create、retry_failed                   | 实际 AsyncJob ID、源／目标任务、失败帧                      |
| task_locks            | acquire、heartbeat、release                  | 当前账号租约、300 秒 TTL                                    |
| point_cloud           | 点云清单、相机成员与标定历史／修改           | member version、track revision、calibration revision/digest |
| scene_tracks          | 时间线、诊断、轨迹命令及兼容接口             | snapshot_token、idempotency_key、原 operation_id            |
| annotation_operations | 转换、延续、插值、推理、Mask／切片、预测决定 | actor/task 绑定凭据、作用域版本、fingerprint、If-Match      |

## 读取当前任务上下文

标注分页 limit 为 1–1000，cursor 原样用于同一范围的下一页；协作视频必须给出实际 video_segment_id。Mask 能力读取解释部署、项目、区域和任务状态门槛，不表示执行器此刻可用。

```python
from ai_annotation import Client

TASK_ID = "<task-uuid>"
with Client() as client:
    page = client.task_context.annotations_page(TASK_ID, limit=100)
    print(page.items, page.next_cursor)
```

邻帧 UUID 是导航元数据，不授予后续读取或编辑权限。邻帧标注结果会按任务可见性过滤几何；空列表不证明对应帧没有标注。预测按置信度过滤／排序后仍保留持久化数组中的 shape_index，采纳／驳回使用这个值，不能使用过滤后列表位置。API key 读取预测几何需要 `annotations:read`。采纳还校验路径 task 与 prediction 的实际关系，跨任务 ID 返回 404。

## 媒体清单与跨帧作业

图像金字塔 manifest GET 可能使过期 generation 失效，并在自动构建启用时排队生成。它仍遵循 SDK 的 GET 429／5xx 重试策略；提交后的状态阻止顺序重复派发，但并发旧 generation 读取仍可能产生重复 Celery 派发。不要把读取当作无副作用的就绪检查。

asset_urls 要求当前 generation，每次最多 128 项，返回限时地址。retry 是有冷却／限流的显式构建操作，HTTP 202 中的 Celery ID 不能当作 AsyncJob ID 交给 `jobs.wait`。读取当前 manifest 判断资源是否就绪，SDK 不自动等待、更新 URL 或重复构建。

```bash
aap task-context mask-capabilities "$TASK_ID" --json
aap image-pyramids manifest "$TASK_ID" --json
aap cross-frame-jobs list "$TASK_ID" --json
```

跨帧创建要求源和目标任务可见、可编辑，最多 100 帧与 500 个唯一 box_3d 来源。相同活动请求有服务端 singleflight，没有调用方幂等键。list/retry 仅当前用户的作业；retry_failed 只处理可重试的终态失败帧。实际 AsyncJob 响应可以用 jobs 观察；超时先读取已有作业，不自动重新创建。

## 显式任务租约

acquire、heartbeat、release 是当前账号的任务租约操作，TTL 为 300 秒。获取可能接管符合条件的旧锁；心跳可能返回 404；释放只针对当前账号持有的锁。响应 unique_id 不是 CAS token。

CLI 在变更租约前要求确认或 `--yes`。SDK 不启动后台续租线程，也不在几何请求前隐式获取／续租。协作视频的 segment lease 是另一种范围，不能由 task lock 代替。

## 相机成员与标定

点云 manifest 仅用于可见 LiDAR 任务，PCD／相机图像 URL 为限时签名。camera_members 的 SceneTrack 必须与路径任务属于同一实际 scene；仅属于同一项目不足以通过。返回 current 与 stored calibration 的关系状态，历史元数据可能表现为 created_at 为空的虚拟 revision 1。

成员创建绑定源 box_3d、track revision 和 calibration revision/digest；更新／恢复还带 member version。创建请求没有源 annotation version 字段，不应自行添加。删除为软停用，成功后 member version 和 track revision 都会变化。恢复要重新校验关系、版本、标定和重复活动成员。

```bash
aap point-cloud camera-members "$TASK_ID" --json
aap describe point_cloud.update_camera_member --json
aap describe point_cloud.update_calibration --json
```

标定修改要求项目管理权限；同一 DatasetItem 被多个项目共用时仅超级管理员可改。矩阵按行排列：extrinsic 16 项、intrinsic 9 项、可选 rect 16 项，数值必须有限。提交原 expected revision/digest；409 后重新读取并重新决定，不自动刷新版本重发。

## Scene track 命令

时间线最多 200 个连续帧；不可见帧不返回 selected_track_present 的存在信息。轨迹详情／诊断可能要求整个 scene 的任务可见。候选 GET 仍需要写权限，最多 20 项。

优先用 preview_command → execute_command，保存原命令、snapshot_token 和调用方 idempotency_key；revert_command 使用原 operation_id 与自己的明确幂等键。修改前校验全部受影响任务可见且可编辑。

```bash
aap scene-tracks timeline "$TASK_ID" --start-frame 0 --end-frame 99 --json
aap describe scene_tracks.execute_command --json
```

兼容的 preview_track_operation／execute_track_operation 单独保留。其执行幂等键由服务端生成，调用方在响应不确定后不能安全重发原请求；不要把它当作新命令失败后的自动回退。

## 标注变换、预测与 Mask

跨帧延续和插值也会修改源标注的轨迹身份／版本，因此源任务必须可编辑。completed 任务需先显式 reopen；review 状态仍按审核微调规则处理。预测采纳／驳回没有调用方幂等键，提交后不自动重试。

转换 dry-run 的计划 token 有效 10 分钟，绑定原 actor/task。execute 保留原计划和幂等身份，并明确确认 replace／lossy。多 Mask commit 保留全作用域 expected versions 与 fingerprint，AI Mask 采纳保留原候选 receipt，refine 还需 If-Match。切片恢复保留原 operation_id 和版本集合。

这些接口使用已验证的不可变 COCO RLE 内容引用，不接受任意二进制 Mask，也不由 SDK 重新计算几何。secondary inference 的 params／model_variants 由具体 backend 能力决定，可能写入属性或创建子标注；先用 describe 读取实际 schema 与影响，再确认提交。
