---
title: MinIO 存储桶布局
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# MinIO 存储桶布局

平台所有对象存储数据按“生命周期 + 安全敏感度”划分为 9 个逻辑存储桶。各桶职责互不重叠，生命周期过期策略与备份策略各自独立。

## 桶清单与职责矩阵

| 桶名            | 环境变量                     | 存储数据类型                                       | Key 前缀约定                                                                                                                                                                                                                         | Lifecycle 过期策略                                            | 备份建议                                                                                                                        |
| --------------- | ---------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `annotations`   | `MINIO_BUCKET`               | 标注任务源文件、评论附件、标注引导手册资产         | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                                                                                                                            | 评论附件由独立owner执行90天期限及软删7天宽限，无前缀lifecycle | **备份**                                                                                                                        |
| `datasets`      | `MINIO_DATASETS_BUCKET`      | 数据集源文件（原始图像/视频/点云/传感器标定）      | `__aap_managed_datasets__/{dataset_id}/_objects/{upload_id}/{relative_path}`；历史真实键保留                                                                                                                                         | 永久                                                          | **备份**                                                                                                                        |
| `bug-reports`   | `MINIO_BUG_REPORTS_BUCKET`   | 快捷 Bug 反馈截图附件                              | `bug-report-attachments/{user_id}/{uuid}-{name}`                                                                                                                                                                                     | 整桶 180 天                                                   | 不备份                                                                                                                          |
| `media-cache`   | `MINIO_MEDIA_CACHE_BUCKET`   | **派生媒体缓存**：缩略图、视频抽帧、分片、转码代理 | `thumbnails/{item_id}.webp`、`videos/{item_id}/frames/{idx}_{w}.{fmt}`、`videos/{item_id}/chunks/{id}.mp4`、`playback/{item_id}.mp4`、`pyramids/{item_id}/...`                                                                       | **仅 `videos/` 30 天**；`playback/`、`thumbnails/` 不过期     | **不备份**（`videos/` 帧与分片按需重生；`playback/` 与 `thumbnails/` 入库时一次性生成，若丢失需重跑 `generate_video_metadata`） |
| `audit-archive` | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计日志冷分区归档                                 | `{YYYY}/{MM}.jsonl.gz`                                                                                                                                                                                                               | **永久**（合规保留）                                          | **强备份**（建议开启 Versioning + Object Lock）                                                                                 |
| `import`        | `MINIO_IMPORT_BUCKET`        | 预标注与数据集导入的临时中转产物                   | 一次性 Key（ROI crop 裁剪图、导入压缩包）                                                                                                                                                                                            | 整桶 7 天                                                     | 不备份（短生命周期）                                                                                                            |
| `export`        | `MINIO_EXPORT_BUCKET`        | 异步导出的标注数据包                               | 一次性 Key（ZIP / 多目标富格式导出包）                                                                                                                                                                                               | 整桶 7 天                                                     | 不备份（短生命周期）                                                                                                            |
| `avatars`       | `MINIO_AVATARS_BUCKET`       | **用户头像**（上传后规范化的 `256×256` WebP）      | `{token}.webp`（`token` 为 32 位随机十六进制字符串）                                                                                                                                                                                 | **永久，且不挂 Lifecycle**                                    | **备份**                                                                                                                        |
| `training`      | `MINIO_TRAINING_BUCKET`      | **在线学习与训练闭环资产**：冻结媒体、训练包与权重 | `projects/{pid}/datasets/{vid}/media/`、`projects/{pid}/datasets/{vid}/attempts/{aid}/dataset.zip`、`projects/{pid}/runs/{rid}/attempts/{aid}/`、`projects/{pid}/previews/{preview_id}/`、`projects/{pid}/sample-evaluations/{eid}/` | **永久，不挂 Lifecycle**                                      | **与数据库一同强备份**                                                                                                          |

### 关键桶设计说明

- **`avatars` 桶（持久身份资产与能力 URL）**：
  - 头像是不可重生的持久数据，由 `users.avatar_ref` 直接引用，因此不挂载过期策略；更换头像时由 API 显式删除旧对象。
  - 读取侧由 `GET /api/v1/avatars/{token}` 提供（免鉴权、强缓存）。该地址是基于 128 位随机熵的能力 URL（Capability URL）：token 不可枚举、不含用户 ID 或任何 PII，且响应仅返回 `image/webp` 字节流。
- **`training` 桶（在线学习不可变资产）**：
  - `projects/{pid}/datasets/{vid}/media/` 与同版本的 `attempts/{aid}/dataset.zip`：存放 `TrainingDatasetVersion` 快照冻结时的源图副本与打包好的标准训练压缩包（`dataset.zip`），确保后续源任务重标或删除不影响历史实验复现。
  - `projects/{pid}/runs/{rid}/attempts/{aid}/`：存放 `training-executor` 回传的训练最佳权重（`best.pt`）、末次权重、训练曲线、混淆矩阵及原生报告制品。
  - `projects/{pid}/previews/{preview_id}/` 与 `projects/{pid}/sample-evaluations/{eid}/`：存放数据增强预览图与冻结逐样本评估报告；主动学习素材另存于 `projects/{pid}/active-learning/{round_id}/media/`。

## 读写路由规则

多阶段预标的临时 ROI 使用 import 桶 `prediction-inputs/<attempt UUID>/`。上传前持久登记，处理结束主动回收，失败或过期由 `cleanup` 队列恢复；不确定 PUT 的记录保留重扫窗口，桶的七天生命周期兜底。此策略只处理有归属的新 attempt，旧 `roi-crops/` 与其他导入产物继续采用各自原有合同。完整预算及恢复合同见[预测流水线](../concepts/prediction-pipeline.md#crop-投递通用化)。

完整原图缓存的内部资源层使用 media-cache 桶 `original-cache/v1/<Asset>/<SHA>/<generation>`，由独立配额/TTL 账本与 `cleanup` 队列回收，不挂桶级过期规则，也不依赖旧的缓存前缀自动路由。完整副本以 `original_cache` AssetLocation 登记，和预览、源原件、托管原件、训练副本分开。公开按需策略与离线读取仍待接入；现有来源读取不会自动创建该缓存。详见[完整原图缓存的资源归属](../concepts/storage-connections.md#完整原图缓存的资源归属)。

- **写入侧**：各领域服务与 Celery Worker（如 `workers/media.py`、`audit_partition_service.py`、`training_datasets.py`）直接指定目标桶写入。
- **读取侧（派生缓存路由）**：API 通过 `StorageService.bucket_for_cache_key(key)` 按前缀自动分流：

```python
MEDIA_CACHE_PREFIXES = ("thumbnails/", "videos/", "playback/")
```

命中上述前缀时自动路由至 `media-cache` 桶；否则回退至调用方传入的默认桶（通常为 `datasets` 或 `annotations`）。

## 从旧版单桶布局迁移

若早期部署将派生媒体或审计归档存放在 `datasets` 与 `annotations` 桶内，可按需整理。其中 `videos/` 帧与分片缓存支持按需重生，不强制迁移；但 **`playback/`（非 H.264 视频的浏览器播放代理）与 `thumbnails/`（海报缩略图）由 `generate_video_metadata` 入库时一次性生成、不惰性重建**，属于数据库持久引用的资产，因此排除在 30 天过期规则之外。

如需一次性清理迁移，运维人员可在 `mc`（MinIO Client）中执行：

```bash
# 1) 派生媒体缓存：datasets → media-cache
mc mirror --remove-source datasets/thumbnails/  media-cache/thumbnails/
mc mirror --remove-source datasets/videos/      media-cache/videos/
mc mirror --remove-source datasets/playback/    media-cache/playback/

# 2) 审计冷归档：annotations/audit-archive/ → audit-archive/
mc mirror --remove-source annotations/audit-archive/  audit-archive/
```

迁移完成后，可在 MinIO 控制台核对：`datasets` 桶保留稳定托管根 `__aap_managed_datasets__/` 与历史名称目录，`annotations` 桶仅保留任务源文件、`comment-attachments/` 与 `projects/.../guide/`。

## 容量监控与日常巡检

- **前端入口**：存储管理展示真实位置、分节点探测和九用途只读映射，不再重复数据集目录。用途映射来自 `GET /api/v1/storage-locations/bindings`；共享桶不合并用途行。
- **统计边界**：物理容量、配额、已索引源规模和平台对象字节没有可靠测量时显示未提供/待统计。旧 `GET /api/v1/storage/buckets` 在线程中逐桶列举，只是旧同步逻辑统计，不提供磁盘容量或异步采样合同；存储页刷新不调用它。分项、去重、时间及过期合同由后续运维统计提供。
- **巡检要点**：
  - `media-cache` 体积主要受 `videos/` 前缀的 30 天 Lifecycle 约束；若出现异常增长，通常意味着大量长视频被高频随机寻帧，可结合 `video_frame_service` 缓存命中率排查。
  - `avatars` 单对象不超过约 40 KB（`256×256` WebP），总体积随注册用户数线性增长；若对象数显著多于用户数，可排查是否存在 `users.avatar_ref` 未引用的孤儿对象。

数据集名称只用于展示。托管原件的新键采用不可变数据集身份和独立上传身份，同名不同内容不会覆盖旧样本。内部用途绑定固定当前实际桶名；其他八用途继续由原 owner 读取，尚未固定历史位置前不支持独立切换目标。

## 平台对象占用快照

`GET /api/v1/storage/buckets` 与 ML 集成 overview 读取 PostgreSQL `storage_usage_snapshots`，不执行 S3 HEAD/LIST。仅显式 `POST /api/v1/storage/usage-inventory` 开始盘点；当前有效超级管理员与全权 API Key 可读/启动/取消。九用途复用 `PURPOSE_BUCKET_ATTRIBUTES`，执行前和每页提交前核验现有 `StorageBinding` 部署映射。按 endpoint、TLS 和桶名固定统计身份，共用桶合并 `roles`，不将占用分摊为九份。

`storage_usage_inventories` 保存单例活动轮次、内部授权证明、分页 token、累计计数、租约及执行代次；公开 AsyncJob payload/result 仅含范围和计数，不返回配置、凭据或 token。每页最多 1000 个对象，每段最多 10 页或 30 秒，段间延迟 1 秒；每桶最多 10000 页、整轮最多 24 小时。S3 连接超时 3 秒、读超时 10 秒、最多 2 次尝试。只累加当前页，不物化全桶对象列表。

每页 I/O 在数据库锁外完成，发布前重新锁定并核对任务 ID、代次、租约、账号/Key 与配置。检查点和进度同事务提交；整桶分页成功才替换该桶成功值。失败/取消仅更新最近尝试，不覆盖成功值。各桶独立成功；因此合计可能来自不同观测窗口。完整分页也不能保证并发写入时的原子时点一致，`is_estimate=true`。

Celery default worker 执行盘点，Beat 每 30 秒回收超过 120 秒租约的任务；投递失败保留可恢复租约。部署本切片后必须应用 0209 并刷新 worker/Beat 的代码和任务注册；挂载源码不需要重建镜像。若没有 Beat，租约恢复不会自动运行。取消使用合作式栅栏，不强杀 provider 请求。AsyncJob 保留期删除通过 SET NULL 解除引用，成功观测继续存在。

统计覆盖当前可见对象（含目录标记），不含历史版本、删除标记、未完成分片或物理存储开销。`measured_at`、`measurement_started_at`、`source=s3_list_objects_v2`、`scope=current_objects`、`coverage` 和最近尝试独立返回；未知计数为 null，真空桶为零。24 小时过期或最近失败/取消显示 stale。外部源逻辑字节、物理总量/可用空间和配额没有本轮采样，不由对象字节推算。
