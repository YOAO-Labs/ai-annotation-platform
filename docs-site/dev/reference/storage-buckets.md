---
title: MinIO 存储桶布局
audience: [developer, ops]
type: reference
status: stable
last_reviewed: 2026-10-17
---

# MinIO 存储桶布局

平台所有对象存储数据按"生命周期 + 安全敏感度"分桶。各桶职责互不重叠,迁移与备份策略各自独立。

## 桶清单

| 桶名              | env 变量                     | 桶级 Lifecycle                                  | 备份建议                                                  |
| ----------------- | ---------------------------- | ----------------------------------------------- | --------------------------------------------------------- |
| `annotations`     | `MINIO_BUCKET`               | 评论附件 90 天；原生 Mask 由引用感知 GC 管理    | 备份                                                      |
| `datasets`        | `MINIO_DATASETS_BUCKET`      | 不挂自动过期                                    | 备份                                                      |
| `bug-reports`     | `MINIO_BUG_REPORTS_BUCKET`   | 整桶 180 天                                     | 不备份                                                    |
| `media-cache`     | `MINIO_MEDIA_CACHE_BUCKET`   | 仅视频帧/chunk 前缀 30 天；其他派生物不套此 TTL | 不备份；非惰性重建的派生物丢失需重跑物化                  |
| `audit-archive`   | `MINIO_AUDIT_ARCHIVE_BUCKET` | 不挂自动过期                                    | 强备份；versioning/object lock 是运维建议，不是代码已启用 |
| `import`          | `MINIO_IMPORT_BUCKET`        | 整桶 7 天                                       | 不备份                                                    |
| `export`          | `MINIO_EXPORT_BUCKET`        | 整桶 7 天                                       | 不备份                                                    |
| `avatars`         | `MINIO_AVATARS_BUCKET`       | 不挂自动过期                                    | 备份                                                      |
| `training`        | `MINIO_TRAINING_BUCKET`      | 不挂桶级自动过期；各业务清理见下表              | 与数据库一同备份                                          |
| `accepted-inputs` | `MINIO_INPUTS_BUCKET`        | 不挂自动过期；必须启用对象版本控制              | 按输入回执与引用保留，不能与派生缓存同桶                  |

`accepted-inputs` 是 Assets 已受理输入对象的独立存储空间，默认由 `MINIO_INPUTS_BUCKET` 指向。存储初始化和 worktree `init/up` 会核验该桶已启用 versioning；不能把该设置应用到 `media-cache`。不可变输入按对象版本读取和签名，不能配置生命周期删除版本或删除标记；清理由输入回执的引用感知保留流程负责。

> `avatars` 不挂 lifecycle 是有意的:头像是不可重生的持久身份数据,由 `users.avatar_ref` 持久引用;一旦过期就会出现"DB 指向已删对象"的破图(与下方 playback 是同类故障)。更换头像时由 API 显式删除旧对象。

读取侧:头像图片由 `GET /api/v1/avatars/{token}` 提供(免鉴权、强缓存)。该 URL 是 128 位随机 token 的**能力 URL**——持有 URL 即可读取该图片,但 token 不可枚举、URL 不含用户 ID 或任何 PII、响应只有 `image/webp` 字节且无写操作。修改此端点前请先确认这一可见性边界仍可接受。

## 路由规则

写入侧(workers/media.py、audit_partition_service.py)直接写到目标桶。

读取侧(API)通过 `StorageService.bucket_for_cache_key(key)` 按前缀路由:

```python
MEDIA_CACHE_PREFIXES = ("thumbnails/", "videos/", "playback/", "image-pyramids/")
```

匹配则走 `media-cache` 桶,否则回退到调用方传入的 default(通常 `datasets` 或 `annotations`)。

这是派生缓存读取提示，不是全局命名空间隔离保证。现有 dataset 存储名检查额外保留 trusted nuScenes 命名空间，并未完整预留这些缓存前缀；读取 canonical 源对象必须携带明确 bucket，不能仅凭 key 首段猜测。改变名称约束或迁移存量对象需要单独设计，不在所有权迁移时悄悄追加校验。

## 对象前缀所有者与删除边界

桶角色与部署名称由上表 env 决定；这里记录逻辑前缀所有者。SQL 行删除、bucket lifecycle、引用感知 GC 和外部源文件清理是不同操作，不能互相替代。

为避免长 key 遮挡所有者列，单元格中的代码片段用“＋”表示按顺序直接拼接；分隔符与两侧空格不属于实际 key。

| 桶 / key 家族               | 当前形状                                                                                                                                                                    | 所有者与删除责任                                                                                        |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| annotations：旧任务源对象   | `{project_id}/{task_id}/...`                                                                                                                                                | labeling 的历史任务源引用；通用项目/素材解绑不据此递归清空源字节                                        |
| annotations：指南资产       | `projects/{project_id}/guide/` ＋ `{uuid}-{safe_name}`                                                                                                                      | workspace 登记/移除，应用工作流绑定对象传输与当前权威                                                   |
| annotations：评论附件       | `comment-attachments/` ＋ `{annotation_id}/` ＋ `{uuid}-{safe_name}`                                                                                                        | labeling 评论所有者；软删行清理与桶生命周期分别执行                                                     |
| annotations：内容寻址 Mask  | `raster-masks/sha256/` ＋ `{sha[:2]}/{sha[2:4]}/` ＋ `{sha}.json[.gz]`                                                                                                      | labeling；只按引用/预留及二次锁内确认 GC，不挂整桶过期                                                  |
| datasets：canonical 源文件  | `{dataset_name}/{relative_path}`；trusted 根为 `__aap_trusted_nuscenes__-` ＋ `{dataset_id.hex}/`，含 `_nuscenes/source/` 与 scene 媒体/标定                                | assets；Dataset 删除不等于递归删除 canonical 源对象，连接器外部源更不在删除授权内                       |
| accepted-inputs：已受理输入 | `{input_id}/g{generation}/a{lease_id}/source`                                                                                                                               | Assets 固定具体 S3 `VersionId` 与对象摘要；Labeling 的预测/标注引用阻止过期回收，桶 versioning 不可关闭 |
| media-cache：普通派生媒体   | `thumbnails/`、`playback/`、`videos/{item_id}/frames/`、`videos/{item_id}/chunks/`                                                                                          | assets；帧/chunk 可按需重生，playback/普通缩略图入库时物化，丢失需重新执行媒体物化                      |
| media-cache：图片金字塔     | `image-pyramids/{asset_id}/` ＋ `g{generation}/`，其下为 `tiles/`、`overview.webp`、`manifest.json`                                                                         | assets；发布验证 generation/lease，清理使用已捕获的废弃 generation 前缀，不删除源图                     |
| bug-reports：附件           | `bug-report-attachments/` ＋ `{user_id}/` ＋ `{uuid}-{safe_name}`，兼容已存历史附件引用                                                                                     | supporting issues；SQL 删除不保证对象立即消失，桶级保留独立执行                                         |
| audit-archive：冷分区       | `{YYYY}/{MM}.jsonl.gz`                                                                                                                                                      | audit；先确认上传才删除对应热分区，不能把推荐对象锁当成已经启用                                         |
| import：ROI / 导入暂存      | `roi-crops/` ＋ `{job_id}/{task_id}/{box}.jpg`；`roi-crops/secondary/` ＋ `{annotation_id}/{box}.jpg`；`mask-formats/` ＋ `{project_id}/{user_id}/` ＋ `{uuid}/{safe_name}` | inference 的临时裁剪与 labeling 的格式导入暂存；不是标注/数据集真值                                     |
| export：导出包              | `{media}/{project_id}/` ＋ `{async_job_id}.zip`                                                                                                                             | exporting；产物索引与桶生命周期分别清理，上传后的可见性仍需重查                                         |
| avatars：身份源对象         | `{opaque_upload_token}.webp`，`users.avatar_ref=upload:{token}`                                                                                                             | identity；新对象→SQL 提交→best-effort 删除旧对象，不以用户目录或短期缓存 TTL 管理                       |
| training：冻结数据          | `projects/{project_id}/` ＋ `datasets/{version_id}/` ＋ `media/{task_id}`；同版本 `attempts/{attempt_id}/dataset.zip`                                                       | learning datasets；版本/attempt 与保留引用参与删除准入，包发布不取代源身份                              |
| training：训练运行          | `projects/{project_id}/` ＋ `runs/{run_id}/` ＋ `attempts/{attempt_id}/{inventory}` 与脱敏日志                                                                              | learning runs/observability；业务删除与保存 bucket/key 的删除意图同事务，提交后清理对象                 |
| training：预览              | `projects/{project_id}/` ＋ `previews/{preview_id}/{output}`                                                                                                                | learning previews；保留既有到期清理与晚到 PUT 宽限，不把 lease 到期当进程已退出                         |
| training：样本评估          | `projects/{project_id}/` ＋ `sample-evaluations/` ＋ `{run_id}/{attempt_id}/{sha}.json`                                                                                     | learning sample-evaluations；引用/保留检查与删除意图保留冻结对象身份                                    |
| training：主动学习媒体      | `projects/{project_id}/` ＋ `active-learning/{round_id}/` ＋ `media/{ordinal}.png`                                                                                          | active-learning；报告在 Attempt JSON；源码未发现独立媒体 GC，不套用 preview TTL 或臆造 report 前缀      |

项目解绑只为被删 Task 自有的金字塔记录 `asset_storage_deletions`：业务提交后，Assets 清理 worker 按冻结的 bucket 和 `image-pyramids/{asset_id}/` 删除并确认，失败保留待重试。配置 bucket 不匹配时不执行对象删除；原始素材和 item-owned 金字塔保留。该 outbox 不接管其他既有金字塔回收路径。

外部 S3/SFTP 连接器有独立 endpoint、bucket/base-prefix 与访问凭据，属于 assets 的读取/复制入口，不授予本项目删除外部 canonical 对象的权力。对象 key 不是授权依据；各业务仍按当前账号/项目与冻结资源身份检查。

## 非对象存储状态

| 介质 / 命名空间                                                         | 所有者与真值                                                                                                   | 故障 / 清理边界                                                                         |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Redis：`token_bl:{jti}`、`token_gen:{user}`、`login_failed:{ip}`        | identity 的凭据失效与登录防护状态                                                                              | 保留各自 TTL/失败语义；凭据代际推进与 SQL 不构成一个事务                                |
| Redis：`bug:reopen:*`                                                   | issues 的重开配额                                                                                              | 部分配额先于 SQL 写入，不因 SQL 回滚自动撤销                                            |
| Redis：`ml-router:v1:*`、`gpu-arbiter:v1:*`                             | inference 的路由、分配、队列、lease 与控制回执；SQL 围栏和实际 backend 生命周期仍分别验证                      | 不把过期 lease、队列移除或取消请求当成设备/子进程已退出                                 |
| Redis：项目/批次/预测/追踪进度、`notify:{user}`、frame-preannotate 计数 | 观察与派送通道；持久通知及领域账本仍在 SQL                                                                     | pub/sub 不持久重放，计数不证明任务成功                                                  |
| Redis：`export-build:*`、`celery:hb:*`、ML stats 订阅/锁                | exporting、worker 与观测的协调状态                                                                             | 按原 token/TTL 释放；心跳不替代子进程退出证明                                           |
| Celery broker / result backend                                          | 作业传输与执行结果介质，部署可与业务 Redis 分离                                                                | 内部 key 布局属于 Celery 实现，不建立第二套模块协议或 SQL/broker 原子性                 |
| DuckDB：`sync_state`、`task_events`、`audit_logs`                       | insights 的可重建 PostgreSQL 读投影；API 只读                                                                  | 数据批次与 high-watermark 按既有分步语句维护，不是跨介质事务                            |
| executor SQLite：`metadata`、`telemetry`、`logs`                        | 每 attempt 的有界观察缓冲，由训练执行器持有                                                                    | 接收/ACK 后确认；不是取消或 GPU 资源租约所有者                                          |
| executor 文件系统                                                       | `executor.json`、`assignment.json`、事件 journal、checkpoint/artifact/finish receipts；独立 preview journal/锁 | journal 的 fsync/rename 与 HTTP/SQL 分阶段确认；GPU 子进程继承锁，实际退出后才释放/发布 |
| backend 权重、receipt 与缓存卷                                          | 五个推理服务各自持有；YOLO 的 managed-model/evaluation 回执保留原存储身份和重放约束                            | 原子 rename、文件锁/配额与进程内 epoch/重放门禁不因改目录变成跨进程事务                 |
| 进程内缓存 / 池                                                         | backend 的 `ManagedLruPool`、tracker 会话、设置缓存、训练产物缓存等                                            | 可回收缓存不取代 SQL 领域状态；借用者/生命周期代际仍需遵守原协议                        |

跨介质提交与语言提取限制见[事务边界](../concepts/backend-layers#事务与外部副作用边界)；入口与注册身份见[仓库地图](../concepts/repository-map#公开契约与入口台账)。

## 软删评论附件清理

每天 03:00 UTC 的 `purge_soft_deleted_attachments` 在 `cleanup` 队列中清理 inactive 且 `updated_at` 严格早于当前时刻减 7 天的评论。数据库查询每次最多选 500 条评论，选择没有指定顺序；空附件评论仍占用候选名额，但 worker 会跳过它们。

对于非空附件，worker 只把 JSON 项中的 `storage_key` 原样交给 MinIO，不会把 `storageKey` 或其他形状转换成新格式。单对象删除异常会记日志，但该评论附件行仍清空；`objects` 只计入成功返回的删除调用。没有自动重试或 outbox。

MinIO 删除与数据库提交不是原子事务：所有附件行清空命令后只执行一次提交。若提交失败，数据库可回滚而已删除的对象无法恢复；若对象删除失败，数据库行仍会清空，之后不会由此任务自动重试。

## Raster Mask 引用与清理

原生 Mask 的普通 JSON 与 gzip 对象均保存在 `MINIO_BUCKET` 的 `raster-masks/sha256/` 下，不套用媒体缓存桶的生命周期。内容摘要地址由标注持有；共享 S3 transport 不决定是否可以删除。

`RasterMaskRetentionWorkflow` 组合领域过期命令，再扫描以下八类引用。当前读取依赖本地 PostgreSQL，不是独立服务之间的保留协议。

| 引用来源                                   | 参与保护的记录                                                                                                |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| 标注 `geometry`                            | `is_active=true`；不按标注类型、来源或取消标记另作排除                                                        |
| 预测 `result`                              | 所有记录，包括已拒绝候选                                                                                      |
| 视频追踪 `staged_result`                   | `pending_review`、`partially_reviewed`、`cancelled`                                                           |
| AI Mask 采纳 `response_json`               | `expires_at > now()`                                                                                          |
| Mask revision `geometry`                   | `expires_at > now()`，包括 PostgreSQL infinity                                                                |
| Mask 质检 `source_snapshot`                | `pending`、`running`                                                                                          |
| Mask 质检问题 `region_mask_ref.object_key` | 所有状态；解决问题不自动释放该引用                                                                            |
| Mask 修复 `plan_json`                      | `planned` 且回执未过期；`pending/running/rolling_back`；或 `completed/partial/rollback_failed` 且回滚期限未过 |

采纳回执、转换计划和 revision 在自身截止时间小于等于当前时间时过期；追踪候选按 `completed_at <= now()-24h` 过期，完成时间缺失不参与。待确认/部分确认追踪改为 `discarded`，取消状态保留，候选清空且 revision 推进；修正轨迹的活动唯一索引随状态变化释放。切割历史通过 revision 的几何保护内容，恢复保留期只延长、不缩短，infinity 不改成有限期限。转换预检计划本身不作为额外对象根。

原 Celery 清理任务仍路由到 `cleanup`，每天 03:20 UTC 触发。先过期记录并提交，再扫描引用与对象；**`dry_run=True` 只禁止对象删除，仍会提交过期记录。** `expired_tracker_candidates` 是受影响行数，不是新释放对象数；既有 `cancelled` 行的 JSON null 候选可能再次计入，不能把该计数当作唯一候选或物理删除数。

对象的 `LastModified` 必须严格早于 24 小时宽限截止；等于截止或时间未知的对象保留。每次最多处理 1000 个候选，但 S3 分页列表与根集合没有同样的内存上限。`eligible` 仅表示初筛通过，不等于物理删除成功。

物理删除保持两阶段内容围栏：取得 advisory 锁并复查引用，删除旧 reservation 后先提交；随后重取同一锁，检查新 reservation 和当前引用，再持锁删除对象。首次 reservation 删除或其提交失败时回滚，不进入物理删除；首次提交之后的存储失败保留字节，但旧 reservation 已提交删除。物理删除之后的数据库提交失败不能恢复字节。新上传在首次提交之后落入的新 reservation 会阻止该次删除；**首次内容锁之前已提交的同 key 去重上传不具备独立的新鲜 reservation 保留保证**，不能把两阶段协议解释为所有新上传都安全的 TTL。

逐对象故障累计到 `errors` 并继续，Celery `SUCCESS` 仍可能带非零 `errors`；列表失败直接向调用方传播，此时先前过期记录的提交不会撤销。数据库与对象存储不具备分布式原子性，当前没有新增 outbox 或自动重试协议。

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

迁移完成后,可在 MinIO 控制台核对 `datasets` 桶根目录只剩 `{dataset_name}/` 子目录,`annotations` 桶只剩任务源文件、`comment-attachments/`、`projects/.../guide/` 和 `raster-masks/`。

## 容量监控

- 前端: 超级管理员 → "存储管理" 页面,按卡片展示各桶对象数 / 总大小 / 错误状态(卡片集合由 `GET /api/v1/storage/buckets` 返回的桶清单决定)。
- API: `GET /api/v1/storage/buckets`、`GET /api/v1/admin/ml-integrations/overview`。
- `media-cache` 体积主要受 `videos/` 前缀的 30 天 lifecycle 约束(帧/chunk 缓存),异常增长通常意味着视频源被频繁访问 → 检查 video_frame_service 命中率。`playback/`、`thumbnails/` 不过期但单文件小、数量随 item 线性增长,占比通常很小。
- `avatars` 单对象 ≤ 约 40 KB(256×256 WebP),体积随用户数线性增长;数量对不上用户数时,检查是否有无引用的孤儿对象(`users.avatar_ref` 里不存在对应 token),可在确认后手工删除。
