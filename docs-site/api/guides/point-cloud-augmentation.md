---
audience: [developer, project_admin]
type: reference
status: experimental
last_reviewed: 2026-10-14
---

# 点云辅助资产与生成

部署开关与项目 `point_cloud_enhancement_config.enabled` 同时启用才可使用。
项目配置的 `schema_version=1`，`profile` 为 `omega-current-mono-512` 或
`omega-current-mono-512-full-lidar`，`ml_backend_pool_id` 选择独立生成服务池。
创建、克隆和模板应用不会继承实验启用状态。

| 方法与路径（`/api/v1`）                                           | 权限                                               | 用途                                            |
| ----------------------------------------------------------------- | -------------------------------------------------- | ----------------------------------------------- |
| `GET /tasks/{task_id}/point-cloud/augmentation`                   | 任务可见且有标注读取权限                           | 获取辅助资产状态和相机摘要                      |
| `POST /tasks/{task_id}/point-cloud/augmentation/assets`           | 同上                                               | 获取指定 asset/generation/camera 的短时签名 URL |
| `GET /projects/{project_id}/point-cloud/augmentation/preparation` | `project.manage`；API key 还需 `annotations:read`  | 获取部署状态、已启用生成池和场景                |
| `POST /projects/{project_id}/point-cloud/augmentation/jobs`       | `project.manage`；API key 还需 `annotations:write` | 创建后台准备任务                                |

状态为 `disabled`、`not_available`、`missing`、`building`、`ready`、`failed` 或 `stale`。
ready 响应包含 `asset_id`、`generation`、`input_digest`、`profile`、
`coordinate_frame=primary_pointcloud`、轴约定和相机的 role、点数、字节数、SHA-256。
签名请求为 `{"asset_id":"UUID","generation":1,"camera_role":"camera_front"}`，
返回的 URL 有效期不超过 300 秒，格式为 binary PCD。源文件、标定或 generation
已变化时不签发旧 URL。
读取时同时校验 PCD 与审计 NPZ；审计文件缺失、大小或哈希不符时返回 `failed`，不再复用该 generation。
旧源对象缺少 SHA-256 元数据时，服务按对象版本和 ETag 缓存成功的内容校验，后续读取仍检查 HEAD，标识变化后重新校验。

生成请求指定一种范围：

```json
{ "scope": "scene_id", "scene_id": "UUID" }
```

或者 `{"scope":"task_ids","task_ids":["UUID"]}`，最多 500 帧。
重复的活跃请求返回同一 `job_id` 和 `reused=true`。后台生成要求完整 LiDAR profile。
进度和结果读取 [AsyncJob](./async-jobs)，kind 为 `point_cloud_densification`；
结果包含新生成、复用、失败计数及 `failed_task_ids`。
重试请求同时携带 `retry_job_id` 和该历史任务的失败帧 ID。
中断恢复时 `failed_task_ids` 包含已有失败帧和未处理帧，排除已成功和已跳过的帧。
创建请求遇到源文件大小缺失或无效时返回 HTTP 409，`detail.reason=source_size_mismatch`。
取消通过现有 `/async-jobs/{id}/cancel`，协作终止于当前推理结束后的边界。

这些资产与预标注 prediction 分开，不能通过接受预测写入标注。
容器仅返回深度和置信度，平台负责校准、筛选及原子发布；
部署与修复命令见 [运维说明](../../ops/vggt-omega)。
