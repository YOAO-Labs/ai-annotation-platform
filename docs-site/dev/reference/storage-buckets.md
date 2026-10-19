---
title: MinIO 存储桶布局
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# MinIO 存储桶布局

平台所有对象存储数据按“生命周期 + 安全敏感度”划分为 9 个逻辑存储桶。各桶职责互不重叠，生命周期过期策略与备份策略各自独立。

## 桶清单与职责矩阵

| 桶名            | 环境变量                     | 存储数据类型                                       | Key 前缀约定                                                                                                                                                                               | Lifecycle 过期策略                                        | 备份建议                                                                                                                        |
| --------------- | ---------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `annotations`   | `MINIO_BUCKET`               | 标注任务源文件、评论附件、标注引导手册资产         | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                                                                                  | 评论附件 90 天                                            | **备份**                                                                                                                        |
| `datasets`      | `MINIO_DATASETS_BUCKET`      | 数据集源文件（原始图像/视频/点云/传感器标定）      | `{dataset_name}/{file_name}`                                                                                                                                                               | 永久                                                      | **备份**                                                                                                                        |
| `bug-reports`   | `MINIO_BUG_REPORTS_BUCKET`   | 快捷 Bug 反馈截图附件                              | `bug-report-attachments/{user_id}/{uuid}-{name}`                                                                                                                                           | 整桶 180 天                                               | 不备份                                                                                                                          |
| `media-cache`   | `MINIO_MEDIA_CACHE_BUCKET`   | **派生媒体缓存**：缩略图、视频抽帧、分片、转码代理 | `thumbnails/{item_id}.webp`、`videos/{item_id}/frames/{idx}_{w}.{fmt}`、`videos/{item_id}/chunks/{id}.mp4`、`playback/{item_id}.mp4`、`pyramids/{item_id}/...`                             | **仅 `videos/` 30 天**；`playback/`、`thumbnails/` 不过期 | **不备份**（`videos/` 帧与分片按需重生；`playback/` 与 `thumbnails/` 入库时一次性生成，若丢失需重跑 `generate_video_metadata`） |
| `audit-archive` | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计日志冷分区归档                                 | `{YYYY}/{MM}.jsonl.gz`                                                                                                                                                                     | **永久**（合规保留）                                      | **强备份**（建议开启 Versioning + Object Lock）                                                                                 |
| `import`        | `MINIO_IMPORT_BUCKET`        | 预标注与数据集导入的临时中转产物                   | 一次性 Key（ROI crop 裁剪图、导入压缩包）                                                                                                                                                  | 整桶 7 天                                                 | 不备份（短生命周期）                                                                                                            |
| `export`        | `MINIO_EXPORT_BUCKET`        | 异步导出的标注数据包                               | 一次性 Key（ZIP / 多目标富格式导出包）                                                                                                                                                     | 整桶 7 天                                                 | 不备份（短生命周期）                                                                                                            |
| `avatars`       | `MINIO_AVATARS_BUCKET`       | **用户头像**（上传后规范化的 `256×256` WebP）      | `{token}.webp`（`token` 为 32 位随机十六进制字符串）                                                                                                                                       | **永久，且不挂 Lifecycle**                                | **备份**                                                                                                                        |
| `training`      | `MINIO_TRAINING_BUCKET`      | **在线学习与训练闭环资产**：冻结媒体、训练包与权重 | `projects/{pid}/datasets/{vid}/media/`、`projects/{pid}/datasets/{vid}/packages/`、`projects/{pid}/attempts/{aid}/`、`projects/{pid}/previews/{pid}/`、`projects/{pid}/evaluations/{eid}/` | **永久，不挂 Lifecycle**                                  | **与数据库一同强备份**                                                                                                          |

### 关键桶设计说明

- **`avatars` 桶（持久身份资产与能力 URL）**：
  - 头像是不可重生的持久数据，由 `users.avatar_ref` 直接引用，因此不挂载过期策略；更换头像时由 API 显式删除旧对象。
  - 读取侧由 `GET /api/v1/avatars/{token}` 提供（免鉴权、强缓存）。该地址是基于 128 位随机熵的能力 URL（Capability URL）：token 不可枚举、不含用户 ID 或任何 PII，且响应仅返回 `image/webp` 字节流。
- **`training` 桶（在线学习不可变资产）**：
  - `projects/{pid}/datasets/{vid}/media/` 与 `packages/`：存放 `TrainingDatasetVersion` 快照冻结时的源图副本与打包好的标准训练压缩包（`dataset.zip`），确保后续源任务重标或删除不影响历史实验复现。
  - `projects/{pid}/attempts/{aid}/`：存放 `training-executor` 回传的训练最佳权重（`best.pt`）、末次权重、训练曲线、混淆矩阵及逐样本评估诊断制品。
  - `projects/{pid}/previews/{pid}/` 与 `evaluations/{eid}/`：存放数据增强预览图及模型对比评估产物。

## 读写路由规则

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

迁移完成后，可在 MinIO 控制台核对：`datasets` 桶根目录仅保留 `{dataset_name}/` 子目录，`annotations` 桶仅保留任务源文件、`comment-attachments/` 与 `projects/.../guide/`。

## 容量监控与日常巡检

- **前端入口**：超级管理员 →「存储管理」页面，按卡片展示全部 9 个存储桶的对象数量、总体积与健康状态（卡片列表由 `GET /api/v1/storage/buckets` 返回的桶清单驱动）。
- **API 端点**：`GET /api/v1/storage/buckets`、`GET /api/v1/admin/ml-integrations/overview`。容量统计与存储健康检查运行在独立后台线程池中，避免大桶扫描阻塞常规 API 请求。
- **巡检要点**：
  - `media-cache` 体积主要受 `videos/` 前缀的 30 天 Lifecycle 约束；若出现异常增长，通常意味着大量长视频被高频随机寻帧，可结合 `video_frame_service` 缓存命中率排查。
  - `avatars` 单对象不超过约 40 KB（`256×256` WebP），总体积随注册用户数线性增长；若对象数显著多于用户数，可排查是否存在 `users.avatar_ref` 未引用的孤儿对象。
