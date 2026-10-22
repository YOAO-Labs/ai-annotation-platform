---
audience: [dev, super_admin]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# 目录来源与观测扫描

这些接口提供只读目录清单和可恢复扫描，均仅限当前超级管理员。来源的 `mode` 固定为 `inventory`：扫描不创建 DatasetItem、Asset 或标注任务，不读取原图字节，不生成内容摘要，也不把历史文件判为 missing。数据集页面继续使用上传或连接器复制导入；原地引用向导尚未接入。

先用[存储位置 API](./storage-locations)登记受控根内位置，成功探测指定 worker 的 list 能力，再创建来源。所有路径均以 `/api/v1` 为前缀。

| 方法  | 路径                                 | 行为                                                        |
| ----- | ------------------------------------ | ----------------------------------------------------------- |
| POST  | `/dataset-sources`                   | 为已有数据集创建只读观测来源，返回201                       |
| GET   | `/dataset-sources`                   | 按 dataset_id 可选过滤，limit 默认100、最多200，offset 分页 |
| GET   | `/dataset-sources/{source_id}`       | 查看来源配置，不返回绝对根或内部节点指纹                    |
| PATCH | `/dataset-sources/{source_id}`       | expected_revision + name/enabled；活动扫描须先取消          |
| POST  | `/dataset-sources/{source_id}/scans` | 启动手动扫描，返回202；同来源已有活动运行时复用             |
| GET   | `/dataset-sources/{source_id}/scans` | 分页查看独立扫描历史                                        |
| GET   | `/dataset-scans/{run_id}`            | 查看状态、阶段、完整性、累计数和当前 async_job_id           |
| GET   | `/dataset-scans/{run_id}/entries`    | limit/offset 分页读取观测，不包含文件内容或已核验摘要       |
| POST  | `/dataset-scans/{run_id}/resume`     | 恢复未完成运行，保留目录检查点，返回202和新任务ID           |

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

Run 的 `completed` 和 `complete=true` 只表示约定范围枚举与复核完成，不是全树某一时刻的原子快照，不证明内容一致。文件大小、inode 和时间只是观测，`content_verified` 恒为 false。任何结果均不自动改变历史样本或 missing 状态。

取消复用 `POST /async-jobs/{async_job_id}/cancel`，已提交观测保留。失去60秒租约后可调用 resume 创建新 job/task_id；仍有有效租约时返回409。旧消息、旧 worker 和旧失败信号不能写入新尝试。发布失败返回的 Run 标记 `failed / dispatch_unavailable`，可显式恢复；API在提交后、发布前中断所留的 pending 同样可恢复。

任务中心显示“已扫描数量 / 总量未知”，不展示伪造百分比。终态任务可能被30天保留策略删除，ScanRun 和观测继续保留，async_job_id 会变为 null；失败/取消运行仍可在当前配置和节点验证有效时恢复。数据集删除后来源失效，账本保留，外部原图不受影响。
