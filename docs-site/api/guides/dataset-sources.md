---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# 目录来源与观测扫描

这些接口提供只读目录清单和可恢复扫描，均仅限当前超级管理员。来源的 `mode` 固定为 `inventory`：扫描不创建 DatasetItem、Asset 或标注任务，不读取原图字节，扫描本身不生成内容摘要，也不把历史文件判为 missing。数据集页面可在扫描完成后逐条核验并登记引用样本；扫描与登记是独立步骤。

先用[存储位置 API](./storage-locations)登记受控根内位置，成功探测指定 worker 的 list 能力，再创建来源。所有路径均以 `/api/v1` 为前缀。

| 方法  | 路径                                 | 行为                                                              |
| ----- | ------------------------------------ | ----------------------------------------------------------------- |
| POST  | `/dataset-sources`                   | 为已有数据集创建只读观测来源，返回201                             |
| GET   | `/dataset-sources`                   | 按 dataset_id 可选过滤，limit 默认100、最多200，offset 分页       |
| GET   | `/dataset-sources/{source_id}`       | 查看来源配置，不返回绝对根或内部节点指纹                          |
| PATCH | `/dataset-sources/{source_id}`       | expected_revision + name/enabled；活动扫描须先取消                |
| POST  | `/dataset-sources/{source_id}/scans` | 启动手动扫描，返回202；同来源已有活动运行时复用                   |
| GET   | `/dataset-sources/{source_id}/scans` | 分页查看独立扫描历史                                              |
| GET   | `/dataset-scans/{run_id}`            | 查看状态、阶段、完整性、累计数和当前 async_job_id                 |
| GET   | `/dataset-scans/{run_id}/entries`    | limit/offset 分页读取观测、verified_content 与 registered_item_id |
| POST  | `/dataset-scans/{run_id}/resume`     | 恢复未完成运行，保留目录检查点，返回202和新任务ID                 |

## 创建来源

```json
{
  "dataset_id": "已有数据集 UUID",
  "location_id": "已登记位置 UUID",
  "name": "车辆目录观测",
  "relative_path": "traffic/vehicle_detection",
  "node_id": "celery@scan-host",
  "recursive": true,
  "include_globs": ["*.jpg", "*.png"]
}
```

`relative_path` 相对存储位置，默认空字符串；不能使用绝对路径、父级跳转、链接或跨子挂载。`node_id` 必须是完整 Celery hostname。过滤使用区分大小写的 fnmatch，默认 `["*"]`；未匹配文件、链接和特殊类型记录为 skipped，不递归链接。源路径、节点、递归和过滤配置创建后不可重定向，需要新建来源；名称/启停用 expected_revision 防止覆盖并发修改。

## 完整性与恢复

Run 固定来源/位置修订、执行节点、根身份和预算。每个目录完整枚举后，观测、子目录队列及完成标记在同一事务提交；中断最多重列当前未提交目录。恢复及完成前复核已捕获目录的身份和摘要，变化返回 `directory_changed`，需要重新启动完整扫描。修改位置配置后旧 Run 不得恢复。

单目录最多5000原始条目，循环预算2秒；单次运行最多50000条观测、5000目录。相对路径最多1000字符、32层，支持中文和空格。过滤项也占预算；目录过大、路径不可读、节点不可用、超时及取消都保留已提交清单并保持 `complete=false`。单次 worker 尝试软限240秒、硬限300秒，内核阻塞调用仍取决于运行平台。

Run 的 `completed` 和 `complete=true` 只表示约定范围枚举与复核完成，不是全树某一时刻的原子快照，不证明内容一致。文件大小、inode 和时间只是观测；未单独核验时 `content_verified=false`。通过[内容核验接口](./verified-content)成功核验后，条目保存独立 verified_content 并返回 true，不表示原图持续在线。任何结果均不自动改变历史样本或 missing 状态。

取消复用 `POST /async-jobs/{async_job_id}/cancel`，已提交观测保留。失去60秒租约后可调用 resume 创建新 job/task_id；仍有有效租约时返回409。旧消息、旧 worker 和旧失败信号不能写入新尝试。发布失败返回的 Run 标记 `failed / dispatch_unavailable`，可显式恢复；API在提交后、发布前中断所留的 pending 同样可恢复。

任务中心显示“已扫描数量 / 总量未知”，不展示伪造百分比。终态任务可能被30天保留策略删除，ScanRun 和观测继续保留，async_job_id 会变为 null；失败/取消运行仍可在当前配置和节点验证有效时恢复。数据集删除后来源失效，账本保留，外部原图不受影响。

## 逐条登记原地图片

`POST /dataset-scans/{run_id}/entries/{path_digest}/register` 要求当前超级管理员及 `datasets:write`，返回200：`{item_id, created, sha256}`。Run 必须完整，条目为匹配的已观测文件；API 当前节点须有有效 read 探测。登记会重新核验字节与图片，创建不可变 Reference、verified Asset 和 DatasetItem，复用已有项目关联创建 Task，不上传原图或创建 S3 副本。只接受普通图片数据集及普通图片项目。

同 Source/path 幂等返回 `created=false`；相同摘要的其他路径不会合并样本。来源或内容变化拒绝，繁忙的数据集返回409 `dataset_busy`，可重试。登记在最终锁内复查原始 API Key 和账号权限，不会把轮换后的 Key 认作原请求。接口是逐条事务；中断保留已提交条目，entries 的 `registered_item_id` 可用于恢复。

`GET /dataset-items/{item_id}/content?sha256=...` 仅当前超管及 `datasets:read` 可预览已登记图片；响应、预算与失败语义沿用[严格内容读取](./verified-content)。员工通过 Task 内容接口读取分配任务，不开放此管理入口。
