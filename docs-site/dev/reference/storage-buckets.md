---
title: MinIO 存储桶布局
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# MinIO 存储桶布局

平台按九种用途管理对象存储。下表描述默认部署桶；已开放的用途可绑定登记的全局 S3 目标，也可共用物理桶。实际对象地址由各自保存记录决定，不能从用途名或当前桶配置推断历史位置。

## 桶清单与职责矩阵

| 桶名              | 环境变量                     | 存储数据类型                                         | Key 前缀约定                                                                                                                                                                                                                         | Lifecycle 过期策略                                                               | 备份建议                                                 |
| ----------------- | ---------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `annotations`     | `MINIO_BUCKET`               | 标注任务源文件、评论附件、标注引导手册资产           | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                                                                                                                            | 评论附件由独立owner执行90天期限及软删7天宽限，无前缀lifecycle                    | **备份**                                                 |
| `datasets`        | `MINIO_DATASETS_BUCKET`      | 数据集源文件（原始图像/视频/点云/传感器标定）        | `__aap_managed_datasets__/{dataset_id}/_objects/{upload_id}/{relative_path}`；历史真实键保留                                                                                                                                         | 永久                                                                             | **备份**                                                 |
| `bug-reports`     | `MINIO_BUG_REPORTS_BUCKET`   | 快捷 Bug 反馈截图附件                                | `bug-report-attachments/{user_id}/{uuid}-{name}`（外部目标加 `_aap_managed/bug-reports/` 前缀）                                                                                                                                      | 每次上传由 `bug_attachment_storage` 独立执行 180 天保留期限，不挂整桶过期规则    | 不备份                                                   |
| `media-cache`     | `MINIO_MEDIA_CACHE_BUCKET`   | 原图缓存、目录预览、普通缩略图、图片金字塔和视频派生 | 新注册目标在 `_aap_managed/media-cache/` 下使用 `original-cache/v1/`、`thumbnails/objects/`、`image-pyramids/objects/`、`video-derivatives/objects/`；旧地址保持                                                                     | 仅旧 `videos/` 前缀沿用30天规则；新owner按各自TTL／退休合同清理，不新增桶级规则  | 缓存不能代替原件备份；数据库归属及外部目标按实际责任保护 |
| `audit-archive`   | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计日志冷分区归档                                   | `{YYYY}/{MM}/{archive_uuid}.jsonl.gz`                                                                                                                                                                                                | **永久**（合规保留）                                                             | **强备份**（建议开启 Versioning + Object Lock）          |
| `import`          | `MINIO_IMPORT_BUCKET`        | 预标注与数据集导入的临时中转产物                     | `prediction-inputs/{attempt}/original/...`、`prediction-inputs/{attempt}/crops/...`、导入暂存键                                                                                                                                      | 预标输入需启用版本控制，按固定版本清理；移除平台整桶过期，未知旧对象待盘点       | 恢复演练保留责任记录、对象元信息及内容                   |
| `export`          | `MINIO_EXPORT_BUCKET`        | 异步导出的标注数据包与独立媒体交付                   | 一次性 Key（ZIP / 多目标富格式导出包；外部目标加 `_aap_managed/export/` 前缀）                                                                                                                                                       | `ExportOutput` 与 `ExportMediaBundle` 各自执行首次 7 天期限，默认桶保留 7 天兜底 | 不备份（短生命周期）                                     |
| `avatars`         | `MINIO_AVATARS_BUCKET`       | **用户头像**（上传后规范化的 `256×256` WebP）        | `{token}.webp`（`token` 为 32 位随机十六进制字符串）                                                                                                                                                                                 | **永久，且不挂 Lifecycle**                                                       | **备份**                                                 |
| `training`        | `MINIO_TRAINING_BUCKET`      | **在线学习与训练闭环资产**：冻结媒体、训练包与权重   | `projects/{pid}/datasets/{vid}/media/`、`projects/{pid}/datasets/{vid}/attempts/{aid}/dataset.zip`、`projects/{pid}/runs/{rid}/attempts/{aid}/`、`projects/{pid}/previews/{preview_id}/`、`projects/{pid}/sample-evaluations/{eid}/` | **永久，不挂 Lifecycle**                                                         | **与数据库一同强备份**                                   |
| `accepted-inputs` | `MINIO_INPUTS_BUCKET`        | Assets 已受理输入对象（固定版本输入）                | `{input_id}/g{generation}/a{lease_id}/source`                                                                                                                                                                                        | 不挂自动过期；**必须启用对象版本控制**                                           | 按输入回执与引用保留，不能与派生缓存同桶                 |

`accepted-inputs` 是 Assets 已受理输入对象的独立存储空间，默认由 `MINIO_INPUTS_BUCKET` 指向。存储初始化和 worktree `init/up` 会核验该桶已启用 versioning；不能把该设置应用到 `media-cache`。不可变输入按对象版本读取和签名，不能配置生命周期删除版本或删除标记；清理由输入回执的引用感知保留流程负责。

### 关键桶设计说明

- **`avatars` 桶（持久身份资产与能力 URL）**：
  - 头像是不可重生的持久数据，由 `users.avatar_ref` 直接引用，因此不挂载过期策略；更换头像时由 API 显式删除旧对象。
  - 读取侧由 `GET /api/v1/avatars/{token}` 提供（免鉴权、强缓存）。该地址是基于 128 位随机熵的能力 URL（Capability URL）：token 不可枚举、不含用户 ID 或任何 PII，且响应仅返回 `image/webp` 字节流。
- **`training` 桶（在线学习不可变资产）**：
  - `projects/{pid}/datasets/{vid}/media/` 与同版本的 `attempts/{aid}/dataset.zip`：存放 `TrainingDatasetVersion` 快照冻结时的源图副本与打包好的标准训练压缩包（`dataset.zip`），确保后续源任务重标或删除不影响历史实验复现。
  - `projects/{pid}/runs/{rid}/attempts/{aid}/`：存放 `training-executor` 回传的训练最佳权重（`best.pt`）、末次权重、训练曲线、混淆矩阵及原生报告制品。
  - `projects/{pid}/previews/{preview_id}/` 与 `projects/{pid}/sample-evaluations/{eid}/`：存放数据增强预览图与冻结逐样本评估报告；主动学习素材另存于 `projects/{pid}/active-learning/{round_id}/media/`。

## 读写路由规则

预标临时输入及帧／ROI供图由独立PredictionInputLease保存import位置和`prediction-inputs/<attempt UUID>/`前缀，注册S3目标在连接基前缀下使用`_aap_managed/import/`命名空间。上传前持久登记，处理结束主动回收，失败或过期由`cleanup`队列按保存位置与准确版本恢复；未知写入／关闭保留责任。平台整桶七天过期规则已移除，不能用lifecycle兜底替代归属清理；旧无归属帧／crop不自动认领。完整预算见[预测流水线](../concepts/prediction-pipeline.md#crop-投递通用化)。

Dataset／Task直传的Creation单独保存暂存引用，最终原件位置保持独立；MaskFormatUpload在签发前保存位置，MaskFormatImport继承准确引用并保持逻辑键／解析计划。两类远端暂存当前不自动删除，不借本次切换新增GC。平台盘点按准确endpoint、bucket和完整物理键分类，外部B不算入默认A的对象字节；增量账目仍为已接入生产者的partial覆盖。

完整原图缓存由独立配额／TTL账本与`cleanup`队列回收，不挂桶级过期规则。严格引用读取在来源和位置均允许按需策略、额度足够时，可保留完整核验副本；新Entry固定media-cache分配地址，旧NULL记录保留原键。完整副本以`original_cache` AssetLocation登记，和预览、源原件、托管原件及训练副本分开，不能用当前绑定否定旧缓存。详见[完整原图缓存的资源归属](../concepts/storage-connections.md#完整原图缓存的资源归属)。

- **写入侧**：已开放用途在分配时固定当前绑定地址，后续服务和Worker按保存owner写入；已有及在途对象不改向。media-cache复用原图缓存、缩略图、金字塔和视频派生的责任及清理链。
- **读取侧**：新media-cache输出通过保存引用选择当前客户端，浏览器使用同源短期能力URL。`StorageService.bucket_for_cache_key(key)`仅兼容已明确保留的旧路径：

```python
MEDIA_CACHE_PREFIXES = (
    "thumbnails/",
    "videos/",
    "playback/",
    "media-assets/",
    "image-pyramids/",
)
```

旧路径命中这些前缀时路由至原`media-cache`桶，否则沿原调用方默认桶。带新owner或受控根的路径不能落入此兜底，也不能通过当前绑定猜读。

## 对象前缀所有者与删除边界

桶角色与部署名称由上表 env 决定；这里记录逻辑前缀所有者。SQL 行删除、bucket lifecycle、引用感知 GC 和外部源文件清理是不同操作，不能互相替代。

为避免长 key 遮挡所有者列，单元格中的代码片段用“＋”表示按顺序直接拼接；分隔符与两侧空格不属于实际 key。

| 桶 / key 家族               | 当前形状                                                                                                                                                                     | 所有者与删除责任                                                                                        |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| annotations：旧任务源对象   | `{project_id}/{task_id}/...`                                                                                                                                                 | labeling 的历史任务源引用；通用项目/素材解绑不据此递归清空源字节                                        |
| annotations：指南资产       | `projects/{project_id}/guide/` ＋ `{uuid}-{safe_name}`                                                                                                                       | workspace 登记/移除，应用工作流绑定对象传输与当前权威                                                   |
| annotations：评论附件       | `comment-attachments/` ＋ `{annotation_id}/` ＋ `{uuid}-{safe_name}`                                                                                                         | labeling 评论所有者；软删行清理与桶生命周期分别执行                                                     |
| annotations：内容寻址 Mask  | 可移植逻辑键 `raster-masks/sha256/` ＋ `{sha[:2]}/{sha[2:4]}/` ＋ `{sha}.json[.gz]`；物理对象为 `raster-masks/objects/` ＋ `{uuid}.json[.gz]`                                | labeling；按原生引用根、生产宽限、持久围栏与精确回执 GC，不挂整桶过期                                   |
| datasets：canonical 源文件  | `__aap_managed_datasets__/{dataset_id}/_objects/{upload_id}/{relative_path}`；历史名称键保留，trusted 根为 `__aap_trusted_nuscenes__-` ＋ `{dataset_id.hex}/`                | assets；Dataset 删除不等于递归删除 canonical 源对象，连接器外部源更不在删除授权内                       |
| accepted-inputs：已受理输入 | `{input_id}/g{generation}/a{lease_id}/source`                                                                                                                                | Assets 固定具体 S3 `VersionId` 与对象摘要；Labeling 的预测/标注引用阻止过期回收，桶 versioning 不可关闭 |
| media-cache：普通派生媒体   | `thumbnails/`、`playback/`、`videos/{item_id}/frames/`、`videos/{item_id}/chunks/`；新注册目标使用 `original-cache/v1/`、`thumbnails/objects/`、`video-derivatives/objects/` | assets；帧/chunk 可按需重生，playback/普通缩略图入库时物化，丢失需重新执行媒体物化                      |
| media-cache：图片金字塔     | `image-pyramids/objects/` ＋ `{output_id}/`，其下为 `tiles/`、`overview.webp`、`manifest.json`                                                                               | assets；发布验证 generation/lease 与输出归属，清理使用已捕获的输出前缀，不删除源图                      |
| bug-reports：附件           | `bug-report-attachments/` ＋ `{user_id}/` ＋ `{uuid}-{safe_name}`，兼容已存历史附件引用                                                                                      | supporting issues；SQL 删除不保证对象立即消失，桶级保留独立执行                                         |
| audit-archive：冷分区       | `{YYYY}/{MM}.jsonl.gz`                                                                                                                                                       | audit；先确认上传才删除对应热分区，不能把推荐对象锁当成已经启用                                         |
| import：预测输入 / 导入暂存 | `prediction-inputs/` ＋ `{attempt}/` ＋ `original/` 或 `crops/`；`mask-formats/` ＋ `{project_id}/{user_id}/` ＋ `{uuid}/{safe_name}`                                        | inference 的临时裁剪（PredictionInputLease）与 labeling 的格式导入暂存；不是标注/数据集真值             |
| export：导出包              | `{media}/{project_id}/` ＋ `{async_job_id}.zip`                                                                                                                              | exporting；产物索引与桶生命周期分别清理，上传后的可见性仍需重查                                         |
| avatars：身份源对象         | `{opaque_upload_token}.webp`，`users.avatar_ref=upload:{token}`                                                                                                              | identity；新对象→SQL 提交→best-effort 删除旧对象，不以用户目录或短期缓存 TTL 管理                       |
| training：冻结数据          | `projects/{project_id}/` ＋ `datasets/{version_id}/` ＋ `media/{task_id}`；同版本 `attempts/{attempt_id}/dataset.zip`                                                        | learning datasets；版本/attempt 与保留引用参与删除准入，包发布不取代源身份                              |
| training：训练运行          | `projects/{project_id}/` ＋ `runs/{run_id}/` ＋ `attempts/{attempt_id}/{inventory}` 与脱敏日志                                                                               | learning runs/observability；业务删除与保存 bucket/key 的删除意图同事务，提交后清理对象                 |
| training：预览              | `projects/{project_id}/` ＋ `previews/{preview_id}/{output}`                                                                                                                 | learning previews；保留既有到期清理与晚到 PUT 宽限，不把 lease 到期当进程已退出                         |
| training：样本评估          | `projects/{project_id}/` ＋ `sample-evaluations/` ＋ `{run_id}/{attempt_id}/{sha}.json`                                                                                      | learning sample-evaluations；引用/保留检查与删除意图保留冻结对象身份                                    |
| training：主动学习媒体      | `projects/{project_id}/` ＋ `active-learning/{round_id}/` ＋ `media/{ordinal}.png`                                                                                           | active-learning；报告在 Attempt JSON；源码未发现独立媒体 GC，不套用 preview TTL 或臆造 report 前缀      |

项目解绑只为被删 Task 自有的金字塔记录 `asset_storage_deletions`：业务提交后，Assets 清理 worker 按冻结的 bucket 与已捕获前缀删除并确认，失败保留待重试。配置 bucket 不匹配时不执行对象删除；原始素材和 item-owned 金字塔保留。该 outbox 不接管其他既有金字塔回收路径。

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

## 从旧版单桶布局迁移

若早期部署将派生媒体或审计归档存放在 `datasets` 与 `annotations` 桶内，可按需整理。其中 `videos/` 帧与分片缓存支持按需重生，不强制迁移；但 **`playback/`（非 H.264 视频的浏览器播放代理）与 `thumbnails/`（海报缩略图）由 `generate_video_metadata` 入库时一次性生成、不惰性重建**，属于数据库持久引用的资产，因此排除在 30 天过期规则之外。

以下步骤仅用于尚未建立独立归属的旧单桶布局，不适用于已保存地址的缓存或其他owner对象；已有固定引用须沿各自迁移与恢复合同处理。运维人员可在 `mc`（MinIO Client）中执行：

```bash
# 1) 派生媒体缓存：datasets → media-cache
mc mirror --remove-source datasets/thumbnails/  media-cache/thumbnails/
mc mirror --remove-source datasets/videos/      media-cache/videos/
mc mirror --remove-source datasets/playback/    media-cache/playback/

# 2) 审计冷归档：annotations/audit-archive/ → audit-archive/
mc mirror --remove-source annotations/audit-archive/  audit-archive/
```

迁移完成后，可在 MinIO 控制台核对：`datasets` 桶保留稳定托管根 `__aap_managed_datasets__/` 与历史名称目录，`annotations` 桶仅保留任务源文件、`comment-attachments/` 与 `projects/.../guide/`。

## 软删评论附件清理

每天 03:00 UTC 的 `purge_soft_deleted_attachments` 在 `cleanup` 队列中清理 inactive 且 `updated_at` 严格早于当前时刻减 7 天的评论。数据库查询每次最多选 500 条评论，选择没有指定顺序；空附件评论仍占用候选名额，但 worker 会跳过它们。

对于非空附件，worker 只把 JSON 项中的 `storage_key` 原样交给 MinIO，不会把 `storageKey` 或其他形状转换成新格式。单对象删除异常会记日志，但该评论附件行仍清空；`objects` 只计入成功返回的删除调用。没有自动重试或 outbox。

MinIO 删除与数据库提交不是原子事务：所有附件行清空命令后只执行一次提交。若提交失败，数据库可回滚而已删除的对象无法恢复；若对象删除失败，数据库行仍会清空，之后不会由此任务自动重试。

## Raster Mask 引用与清理

原生 Mask 的可移植引用是内容摘要键 `raster-masks/sha256/...json[.gz]`，物理对象固定为 `raster-masks/objects/{uuid}.json[.gz]` 并另存规范 RLE 与字节的双份 SHA／大小回执，均保存在 `MINIO_BUCKET`，不套用媒体缓存桶的生命周期。内容摘要地址由标注持有；共享 S3 transport 不决定是否可以删除。

`RasterMaskRetentionWorkflow` 组合领域过期命令，再调用持久回收协调器。Labeling 原生根登记及存活检查保护以下记录，协调进程只调用函数能力，不直接扫描业务表。该边界仍依赖同一个 PostgreSQL owner 协议。

| 引用来源                                   | 参与保护的记录                                                                                                |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| 标注 `geometry`                            | `is_active=true`；不按标注类型、来源或取消标记另作排除                                                        |
| 预测 `result`                              | 所有记录，包括已拒绝候选                                                                                      |
| 未结算批量结果与运行中跟踪输出             | 已保存 raw/prepared 结果及 running Job 的临时根，直至结算、转移或原生终结释放                                 |
| 视频追踪 `staged_result`                   | `pending_review`、`partially_reviewed`、`cancelled`                                                           |
| AI Mask 采纳 `response_json`               | `expires_at > now()`                                                                                          |
| Mask revision `geometry`                   | `expires_at > now()`，包括 PostgreSQL infinity                                                                |
| Mask 质检 `source_snapshot`                | `pending`、`running`                                                                                          |
| Mask 质检问题 `region_mask_ref.object_key` | 所有状态；解决问题不自动释放该引用                                                                            |
| 审核范围与受理跟踪输入                     | 审核范围自身仍存在；输入按固定作业、候选或派生标注的原生保留规则判定，异常时保留                              |
| Mask 修复 `plan_json`                      | `planned` 且回执未过期；`pending/running/rolling_back`；或 `completed/partial/rollback_failed` 且回滚期限未过 |

采纳回执、转换计划和 revision 在自身截止时间小于等于当前时间时过期；追踪候选按 `completed_at <= now()-24h` 过期，完成时间缺失不参与。待确认/部分确认追踪改为 `discarded`，取消状态保留，候选清空且 revision 推进；修正轨迹的活动唯一索引随状态变化释放。切割历史通过 revision 的几何保护内容，恢复保留期只延长、不缩短，infinity 不改成有限期限。转换预检计划本身不作为额外对象根。

原 Celery 清理任务仍路由到 `cleanup`，每天 03:20 UTC 触发。先过期记录并提交，释放指标读取事务，再调用协调器；**Celery 的 `dry_run=True` 仍会提交领域过期记录，独立回收服务的预览完全只读。** `expired_tracker_candidates` 是受影响行数，不是新释放对象数；既有 `cancelled` 行的 JSON null 候选可能再次计入，不能把该计数当作唯一候选或物理删除数。

对象的 `LastModified` 必须严格早于原生 owner 的 24 小时宽限截止；等于截止或时间未知的对象保留。近期内容生产及上传预约另有同样宽限，活动意图和有效根始终保留。每轮扫描 1–20 页、每页最多 1000 项，独立领取 1–100 次删除，默认 1 页和 100 次。预览返回 `next_after_key` 与 `scan_complete`，正式执行的游标持久保存；保留或删除失败的前缀不阻塞后续扫描页。

原生操作先记录删除意图、递增每键代次并建立持久围栏，再提交并执行字节删除。字节端以不可变操作 UUID 声明唯一执行权，记录条件删除的精确终态回执。原生完成确认须匹配操作、对象事实及当前代次／token；只有明确终态才能解除活动围栏，旧代次继续失效。传输超时、租约过期、进程重启或 HEAD 不存在都不补造完成事实。

正式执行的 `eligible` 仅计实际 staged 意图，预览时计当前可回收项，均不等于物理删除数。`referenced` 为全局当前存活引用键去重数，无法观察时为 null。Celery 的 `errors` 汇总扫描、数据、完成确认与持久不确定结果，`SUCCESS` 仍可能带非零错误；清单失败后仍协调既有意图，先前领域过期提交不会撤销。持久意图和字节日志分别恢复数据库与对象存储的阶段结果，不提供跨介质原子提交。

## 容量监控与日常巡检

- **前端入口**：存储管理展示真实位置、分节点探测和九用途映射，不再重复数据集目录。用途映射来自 `GET /api/v1/storage-locations/bindings`；共享桶不合并用途行。`read_policy=consumer_owned_compatibility`保留各业务固定定位及旧读取路径，`write_policy=deployment_mapping`描述新写入的兼容部署目标，头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件使用`write_policy=current_binding`，分别通过`reviewed_avatar_switch`、`reviewed_bug_attachment_switch`、`reviewed_audit_archive_switch`、`reviewed_export_switch`、`reviewed_import_switch`、`reviewed_dataset_switch`、`reviewed_media_cache_switch`、`reviewed_annotations_switch`和`reviewed_training_switch`支持经预览确认切换。位置配置修订与用途绑定代次独立返回；配置匹配不代表健康或迁移资格。
- **统计边界**：平台对象字节读取持久盘点快照，已采纳源索引规模按准确来源地址去重，二者分列。物理容量、提供方配额及其他缺少可靠测量的指标保持未提供；刷新只查询已有记录，不同步列举全部桶。快照和分项口径见下文。
- **巡检要点**：
  - `media-cache`旧`videos/`前缀保留30天Lifecycle；新缓存需分看原图额度／TTL、缩略图、金字塔和视频owner状态，不能为新受控根添加整前缀过期来绕过读租约及退休清理。
  - `avatars` 单对象不超过约 40 KB（`256×256` WebP），总体积随注册用户数线性增长；若对象数显著多于用户数，可排查是否存在 `users.avatar_ref` 未引用的孤儿对象。

数据集名称只用于展示。托管原件的新键采用不可变数据集身份和独立上传身份，同名不同内容不会覆盖旧样本，头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件可经真实托管探针及预览确认切换新写入位置；已固定对象按各自owner记录读取，历史头像定位缺失时阻止该用途切换，不声称全部历史定位已覆盖。用途策略不复用外部引用适配器的只读能力声明。

独立Task新上传的最终原件位于annotations用途的 `__aap_task_originals__/<Task UUID>/<Creation UUID>/payload`。浏览器只写import暂存区，完整核验封存后保存独立TaskOriginal；删除业务Task或测试fixture不会按前缀删除该原件。历史未知Task不补造归属，全部原件读取者的固定位置接入仍须独立完成。

## 存储迁移、容量分类与增量账目索引

平台的对象存储受控迁移、六分类占用盘点、受控写入/删除增量记账以及各用途新分配 `storage_ref`（`schema2`）合同已收敛至专题架构文档 [对象存储迁移、站点 Agent 与容量观测架构](../concepts/storage-migration-and-ops) 与 [原件验证读取、缓存与保留回收架构](../concepts/original-access-and-retention)：

| 主题                               | 核心模型 / 服务                                                               | 架构说明与契约入口                                                                                                                                                                                                                                                                      |
| ---------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **任务推进观测**                   | `AsyncJob.last_progress_at`、`storage_job_progress.py`                        | 扫描、登记、容量盘点、迁移清单与迁移复制五类任务仅在实际进展 marker 推进时刷新时间；运行中达 900 秒无新进展显示核实提示。详见 [任务推进观测](../concepts/storage-migration-and-ops#任务推进观测（迁移-0259）)。                                                                         |
| **来源故障状态與窗口观测率**       | `storage_fault_states`、`StorageObservationBucket`                            | 稀疏记录来源扫描、原件核验读取和预览生成的故障水位与恢复，并按 UTC 日期独立累加回源与新建预览尝试的成功率。详见 [来源故障状态](../concepts/storage-migration-and-ops#来源故障状态（迁移-0249）)。                                                                                       |
| **配置源迁移清单与复制批次**       | `StorageMigrationInventory`、`StorageMigrationCopy`、`StorageMigrationWindow` | 32 MiB 私密清单报告、27 家族引用配对目录、可恢复初次复制与增量后继批次、迁移维护窗口及 `storage_source_fence.py` 原生源端写封闭。详见 [配置源迁移清单与可恢复复制计划](../concepts/storage-migration-and-ops#配置源迁移清单与可恢复复制计划)。                                          |
| **已封存读取映射与九用途切换**     | `StorageMigrationReadPublication`、`StorageMigrationCutover`                  | `PhysicalReadPlan` 透明将源 A 读取路由至目标 B 准确版本；单事务切换九用途新分配绑定并支持带新写入受控回滚。详见 [维护窗口、源端封闭、读取映射与原子切换](../concepts/storage-migration-and-ops#维护窗口、源端封闭、读取映射与原子切换)。                                                |
| **平台对象占用快照与当前对象分类** | `storage_usage_snapshots`、`storage_usage_inventories`                        | `POST /api/v1/storage/usage-inventory` 后台分页盘点九用途桶，按保存归属将字节归入托管原件、预览与缓存、训练版本文件、临时产物、其他平台对象和未归类六大类。详见 [平台对象占用快照与六分类归属](../concepts/storage-migration-and-ops#平台对象占用快照与六分类归属（迁移-0209、0242）)。 |
| **当前对象增量与周期核对**         | `storage_usage_mutations`、`storage_usage_schedules`                          | 在条件 PUT / CompleteMultipart 与版本/按键 DELETE 前持久登记幂等意图，整桶盘点保留两个成功代次对账，支持 1–168 小时周期盘点。详见 [当前对象增量与周期核对](../concepts/storage-migration-and-ops#当前对象增量与周期核对（迁移-0246）)。                                                 |
| **九用途 `schema2` 外部目标前缀**  | `managed_s3_target`、各用途 `*_storage.py`                                    | 外部全局 S3 目标统一在连接 `base_prefix` 下使用 `_aap_managed/<purpose>/` 命名空间，已有与在途对象始终沿保存 `storage_ref` 访问，外部 B 对象不计入默认部署 A 的九桶总量。详见 [九用途绑定与保存目标隔离](../concepts/storage-connections#九用途绑定与保存目标隔离)。                    |

### 当前对象分类

盘点每页保留最多 1,000 个 `Key/Size`，核对已保存的端点身份、桶及准确键／归属前缀后归入**托管原件**、**预览与缓存**、**训练版本文件**、**临时产物**、**其他平台对象**与**未归类**六类，整桶完成后写入 `storage_usage_snapshots.category_totals`。完整分类依据表见 [对象存储迁移、站点 Agent 与容量观测架构 · 平台对象占用快照与六分类归属](../concepts/storage-migration-and-ops#平台对象占用快照与六分类归属（迁移-0209、0242）)，API 契约见 [平台对象占用盘点](/api/guides/storage-locations#平台对象占用盘点)。
