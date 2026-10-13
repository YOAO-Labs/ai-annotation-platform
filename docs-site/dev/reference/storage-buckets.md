---
title: MinIO 存储桶布局
audience: [developer, ops]
type: reference
status: stable
last_reviewed: 2026-07-11
---

# MinIO 存储桶布局

平台所有对象存储数据按"生命周期 + 安全敏感度"分桶。各桶职责互不重叠,迁移与备份策略各自独立。

## 桶清单

| 桶名            | env 变量                     | 数据类型                                           | Key 前缀约定                                                                                                                         | Lifecycle                                                | 备份建议                                                                                                                           |
| --------------- | ---------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `annotations`   | `MINIO_BUCKET`               | 标注任务源文件、评论附件、引导手册资产             | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                            | 评论附件 90 天                                           | 备份                                                                                                                               |
| `datasets`      | `MINIO_DATASETS_BUCKET`      | 数据集源文件(原始图/视频/文本)                     | `{dataset_name}/{file_name}`                                                                                                         | 永久                                                     | 备份                                                                                                                               |
| `bug-reports`   | `MINIO_BUG_REPORTS_BUCKET`   | Bug 反馈截图                                       | `bug-report-attachments/{user_id}/{uuid}-{name}`                                                                                     | 整桶 180 天                                              | 不备份                                                                                                                             |
| `media-cache`   | `MINIO_MEDIA_CACHE_BUCKET`   | **派生缓存**: 缩略图、视频帧、chunk、playback 转码 | `thumbnails/{item_id}.webp`、`videos/{item_id}/frames/{idx}_{w}.{fmt}`、`videos/{item_id}/chunks/{id}.mp4`、`playback/{item_id}.mp4` | **仅 `videos/` 30 天**;`playback/`、`thumbnails/` 不过期 | **不备份**(`videos/` 帧/chunk 按需重生;`playback/`、`thumbnails/` 入库一次性生成、不惰性重建,丢失需重跑 `generate_video_metadata`) |
| `audit-archive` | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计冷分区归档                                     | `{YYYY}/{MM}.jsonl.gz`                                                                                                               | **永久**(合规)                                           | 强备份,建议开 versioning + object lock                                                                                             |
| `import`        | `MINIO_IMPORT_BUCKET`        | 预标注导入的临时产物                               | 一次性 key(ROI crop、导入包)                                                                                                         | 整桶 7 天                                                | 不备份(短生命周期)                                                                                                                 |
| `export`        | `MINIO_EXPORT_BUCKET`        | 导出标注产物                                       | 一次性 key(ZIP / 富格式导出)                                                                                                         | 整桶 7 天                                                | 不备份(短生命周期)                                                                                                                 |
| `avatars`       | `MINIO_AVATARS_BUCKET`       | **用户头像**(上传后规范化的 256×256 WebP)          | `{token}.webp`(`token` = 32 位随机十六进制)                                                                                          | **永久,且不挂 lifecycle**                                | 备份                                                                                                                               |

> `avatars` 不挂 lifecycle 是有意的:头像是不可重生的持久身份数据,由 `users.avatar_ref` 持久引用;一旦过期就会出现"DB 指向已删对象"的破图(与下方 playback 是同类故障)。更换头像时由 API 显式删除旧对象。

读取侧:头像图片由 `GET /api/v1/avatars/{token}` 提供(免鉴权、强缓存)。该 URL 是 128 位随机 token 的**能力 URL**——持有 URL 即可读取该图片,但 token 不可枚举、URL 不含用户 ID 或任何 PII、响应只有 `image/webp` 字节且无写操作。修改此端点前请先确认这一可见性边界仍可接受。

## 路由规则

写入侧(workers/media.py、audit_partition_service.py)直接写到目标桶。

读取侧(API)通过 `StorageService.bucket_for_cache_key(key)` 按前缀路由:

```python
MEDIA_CACHE_PREFIXES = ("thumbnails/", "videos/", "playback/")
```

匹配则走 `media-cache` 桶,否则回退到调用方传入的 default(通常 `datasets` 或 `annotations`)。

## 从旧布局迁移

历史数据仍在 `datasets` 与 `annotations` 桶里。`videos/` 帧/chunk 缓存可按需重生,迁移**不是必须的**;不迁移时 worker 会按需重生,旧 key 随 `videos/` lifecycle 衰减。**注意**:`playback/`(非 h264 视频的浏览器播放代理)与 `thumbnails/`(海报)由 `generate_video_metadata` 入库时一次性生成、不惰性重建,是 DB 元数据持久引用的耐久资产——故不在过期规则内(历史教训:整桶 30 天过期会把 playback 代理删掉而 DB 仍指向它,`<video>` 命中 404 → 浏览器 `MEDIA_ELEMENT_ERROR: Format error`)。

如需一次性整理,运维可在 `mc` 客户端跑:

```bash
# 1) 派生媒体缓存: datasets → media-cache
mc mirror --remove-source datasets/thumbnails/  media-cache/thumbnails/
mc mirror --remove-source datasets/videos/      media-cache/videos/
mc mirror --remove-source datasets/playback/    media-cache/playback/

# 2) 审计归档: annotations/audit-archive/ → audit-archive/
mc mirror --remove-source annotations/audit-archive/  audit-archive/
```

迁移完成后,可在 MinIO 控制台核对 `datasets` 桶根目录只剩 `{dataset_name}/` 子目录,`annotations` 桶只剩任务源文件、`comment-attachments/`、`projects/.../guide/`。

## 容量监控

- 前端: 超级管理员 → "存储管理" 页面,按卡片展示各桶对象数 / 总大小 / 错误状态(卡片集合由 `GET /api/v1/storage/buckets` 返回的桶清单决定)。
- API: `GET /api/v1/storage/buckets`、`GET /api/v1/admin/ml-integrations/overview`。
- `media-cache` 体积主要受 `videos/` 前缀的 30 天 lifecycle 约束(帧/chunk 缓存),异常增长通常意味着视频源被频繁访问 → 检查 video_frame_service 命中率。`playback/`、`thumbnails/` 不过期但单文件小、数量随 item 线性增长,占比通常很小。
- `avatars` 单对象 ≤ 约 40 KB(256×256 WebP),体积随用户数线性增长;数量对不上用户数时,检查是否有无引用的孤儿对象(`users.avatar_ref` 里不存在对应 token),可在确认后手工删除。
