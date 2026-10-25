---
title: MinIO 存储桶布局
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# MinIO 存储桶布局

平台所有对象存储数据按“生命周期 + 安全敏感度”划分为 9 个逻辑存储桶。各桶职责互不重叠，生命周期过期策略与备份策略各自独立。

## 桶清单与职责矩阵

| 桶名            | 环境变量                     | 存储数据类型                                       | Key 前缀约定                                                                                                                                                                                                                         | Lifecycle 过期策略                                                         | 备份建议                                                                                                                        |
| --------------- | ---------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `annotations`   | `MINIO_BUCKET`               | 标注任务源文件、评论附件、标注引导手册资产         | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                                                                                                                            | 评论附件由独立owner执行90天期限及软删7天宽限，无前缀lifecycle              | **备份**                                                                                                                        |
| `datasets`      | `MINIO_DATASETS_BUCKET`      | 数据集源文件（原始图像/视频/点云/传感器标定）      | `__aap_managed_datasets__/{dataset_id}/_objects/{upload_id}/{relative_path}`；历史真实键保留                                                                                                                                         | 永久                                                                       | **备份**                                                                                                                        |
| `bug-reports`   | `MINIO_BUG_REPORTS_BUCKET`   | 快捷 Bug 反馈截图附件                              | `bug-report-attachments/{user_id}/{uuid}-{name}`                                                                                                                                                                                     | 整桶 180 天                                                                | 不备份                                                                                                                          |
| `media-cache`   | `MINIO_MEDIA_CACHE_BUCKET`   | **派生媒体缓存**：缩略图、视频抽帧、分片、转码代理 | `thumbnails/{item_id}.webp`、`videos/{item_id}/frames/{idx}_{w}.{fmt}`、`videos/{item_id}/chunks/{id}.mp4`、`playback/{item_id}.mp4`、`pyramids/{item_id}/...`                                                                       | **仅 `videos/` 30 天**；`playback/`、`thumbnails/` 不过期                  | **不备份**（`videos/` 帧与分片按需重生；`playback/` 与 `thumbnails/` 入库时一次性生成，若丢失需重跑 `generate_video_metadata`） |
| `audit-archive` | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计日志冷分区归档                                 | `{YYYY}/{MM}/{archive_uuid}.jsonl.gz`                                                                                                                                                                                                | **永久**（合规保留）                                                       | **强备份**（建议开启 Versioning + Object Lock）                                                                                 |
| `import`        | `MINIO_IMPORT_BUCKET`        | 预标注与数据集导入的临时中转产物                   | `prediction-inputs/{attempt}/original/...`、`prediction-inputs/{attempt}/crops/...`、导入暂存键                                                                                                                                      | 预标输入需启用版本控制，按固定版本清理；移除平台整桶过期，未知旧对象待盘点 | 恢复演练保留责任记录、对象元信息及内容                                                                                          |
| `export`        | `MINIO_EXPORT_BUCKET`        | 异步导出的标注数据包                               | 一次性 Key（ZIP / 多目标富格式导出包）                                                                                                                                                                                               | 整桶 7 天                                                                  | 不备份（短生命周期）                                                                                                            |
| `avatars`       | `MINIO_AVATARS_BUCKET`       | **用户头像**（上传后规范化的 `256×256` WebP）      | `{token}.webp`（`token` 为 32 位随机十六进制字符串）                                                                                                                                                                                 | **永久，且不挂 Lifecycle**                                                 | **备份**                                                                                                                        |
| `training`      | `MINIO_TRAINING_BUCKET`      | **在线学习与训练闭环资产**：冻结媒体、训练包与权重 | `projects/{pid}/datasets/{vid}/media/`、`projects/{pid}/datasets/{vid}/attempts/{aid}/dataset.zip`、`projects/{pid}/runs/{rid}/attempts/{aid}/`、`projects/{pid}/previews/{preview_id}/`、`projects/{pid}/sample-evaluations/{eid}/` | **永久，不挂 Lifecycle**                                                   | **与数据库一同强备份**                                                                                                          |

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

- **前端入口**：存储管理展示真实位置、分节点探测和九用途映射，不再重复数据集目录。用途映射来自 `GET /api/v1/storage-locations/bindings`；共享桶不合并用途行。`read_policy=consumer_owned_compatibility`保留各业务固定定位及旧读取路径，`write_policy=deployment_mapping`描述新写入的兼容部署目标，其余六用途的`change_policy=legacy_mapping_locked`保持锁定；头像、报告附件和审计归档使用`write_policy=current_binding`，分别通过`reviewed_avatar_switch`、`reviewed_bug_attachment_switch`和`reviewed_audit_archive_switch`支持经预览确认切换。位置配置修订与用途绑定代次独立返回；配置匹配不代表健康或迁移资格。
- **统计边界**：平台对象字节读取持久盘点快照，已采纳源索引规模按准确来源地址去重，二者分列。物理容量、提供方配额及其他缺少可靠测量的指标保持未提供；刷新只查询已有记录，不同步列举全部桶。快照和分项口径见下文。
- **巡检要点**：
  - `media-cache` 体积主要受 `videos/` 前缀的 30 天 Lifecycle 约束；若出现异常增长，通常意味着大量长视频被高频随机寻帧，可结合 `video_frame_service` 缓存命中率排查。
  - `avatars` 单对象不超过约 40 KB（`256×256` WebP），总体积随注册用户数线性增长；若对象数显著多于用户数，可排查是否存在 `users.avatar_ref` 未引用的孤儿对象。

数据集名称只用于展示。托管原件的新键采用不可变数据集身份和独立上传身份，同名不同内容不会覆盖旧样本。六用途保持兼容部署映射，头像、报告附件和审计归档可经真实托管探针及预览确认切换新写入位置；已固定对象按各自owner记录读取，历史头像定位缺失时阻止该用途切换，不声称全部历史定位已覆盖。用途策略不复用外部引用适配器的只读能力声明。

独立Task新上传的最终原件位于annotations用途的 `__aap_task_originals__/<Task UUID>/<Creation UUID>/payload`。浏览器只写import暂存区，完整核验封存后保存独立TaskOriginal；删除业务Task或测试fixture不会按前缀删除该原件。历史未知Task不补造归属，全部原件读取者的固定位置接入仍须独立完成。

## 平台对象占用快照

`GET /api/v1/storage/buckets` 与 ML 集成 overview 读取 PostgreSQL `storage_usage_snapshots`，不执行 S3 HEAD/LIST。仅显式 `POST /api/v1/storage/usage-inventory` 开始盘点；当前有效超级管理员与全权 API Key 可读/启动/取消。九用途复用 `PURPOSE_BUCKET_ATTRIBUTES`，执行前和每页提交前核验现有 `StorageBinding` 部署映射。按 endpoint、TLS 和桶名固定统计身份，共用桶合并 `roles`，不将占用分摊为九份。

`storage_usage_inventories` 保存单例活动轮次、内部授权证明、分页 token、累计计数、租约及执行代次；公开 AsyncJob payload/result 仅含范围和计数，不返回配置、凭据或 token。每页最多 1000 个对象，每段最多 10 页或 30 秒，段间延迟 1 秒；每桶最多 10000 页、整轮最多 24 小时。S3 连接超时 3 秒、读超时 10 秒、最多 2 次尝试。只累加当前页，不物化全桶对象列表。

每页 I/O 在数据库锁外完成，发布前重新锁定并核对任务 ID、代次、租约、账号/Key 与配置。检查点和进度同事务提交；整桶分页成功才替换该桶成功值。失败/取消仅更新最近尝试，不覆盖成功值。各桶独立成功；因此合计可能来自不同观测窗口。完整分页也不能保证并发写入时的原子时点一致，`is_estimate=true`。

Celery default worker 执行盘点，Beat 每 30 秒回收超过 120 秒租约的任务；投递失败保留可恢复租约。部署本切片后必须应用 0209 并刷新 worker/Beat 的代码和任务注册；挂载源码不需要重建镜像。若没有 Beat，租约恢复不会自动运行。取消使用合作式栅栏，不强杀 provider 请求。AsyncJob 保留期删除通过 SET NULL 解除引用，成功观测继续存在。

统计覆盖当前可见对象（含目录标记），不含历史版本、删除标记、未完成分片或物理存储开销。`measured_at`、`measurement_started_at`、`source=s3_list_objects_v2`、`scope=current_objects`、`coverage` 和最近尝试独立返回；未知计数为 null，真空桶为零。24 小时过期或最近失败/取消显示 stale。外部源逻辑字节、物理总量/可用空间和配额没有本轮采样，不由对象字节推算。

### 当前对象分类

每页保留最多 1000 个 Key／Size，以一个 SQL union 查询已保存的准确定位或候选主键。规范路径仅用于查候选记录；分类必须核对保存的端点身份、桶及准确键／归属前缀，并复用各 owner 的纯引用解析。Dataset 定位与预览的准确地址查询使用摘要索引及原值等式，摘要碰撞不能合并对象。未知历史归属不从桶用途或文件后缀推断。

| 分类         | 保存的归属依据                                                                                                        |
| ------------ | --------------------------------------------------------------------------------------------------------------------- |
| 托管原件     | 原件创建责任、Dataset 原件定位／保留账本、TaskOriginal 和辅助原件的当前独立定位。恢复后的独立定位与创建历史分别解释。 |
| 预览与缓存   | 来源预览、完整原图缓存、图片金字塔、视频派生和训练预览的保存归属。                                                    |
| 训练版本文件 | TrainingDatasetVersion 整个已登记前缀及其删除意图，包含准备中、各 attempt 和保留副本。                                |
| 临时产物     | 创建暂存区、预测输入、导出 ZIP 及独立媒体交付。                                                                       |
| 其他平台对象 | 训练实验／评估／选样、头像、报告／评论附件、指南图片、原生 Mask 和审计归档。                                          |
| 未归类       | 缺少一致归属或存在跨分类冲突的实际对象。                                                                              |

同类重复声明合并，跨类冲突只在未归类计一次；大小仅取 LIST，不取业务行、额度预约或输出清单预计字节。记录过期、退休或业务删除不抹去仍存在的物理字节，分类不改变现有生命周期或删除许可。

分类累计随分页检查点提交，整桶成功后与对象总数、字节、采样时间一起写入 `storage_usage_snapshots.category_totals`。读取核对分类和总量、测量时间一致；旧快照及旧检查点不凭空补分类，下一轮完整盘点自然替换。失败／取消保留上次成功数据。GET 只聚合有效分类快照；API 字段与 partial／未知语义见[平台盘点 API](/api/guides/storage-locations#平台对象占用盘点)。

启用分类统计前应用 0242，并刷新执行盘点的 Worker；无需重建镜像。统计仍限当前配置的平台桶与当前对象，不覆盖历史版本、未完成分片、本地临时磁盘或提供方物理容量。外部已采纳源索引规模继续独立统计。

报告附件可采用已登记全局 S3 位置的新写入绑定。schema2 物理键位于基前缀下 `_aap_managed/bug-reports/bug-report-attachments/<owner>/<upload>.<ext>`，公开报告键仍从 `bug-report-attachments/` 开始。旧与在途上传按保存 endpoint／bucket／key／实际版本处理；当前平台部署桶盘点不包含外部目标。分类必须同时匹配端点，不能把同桶同键的外部声明套用到当前端点对象。

审计归档新分配同样可使用已登记全局 S3，schema2 的准确物理键位于基前缀下 `_aap_managed/audit-archive/{YYYY}/{MM}/{archive_uuid}.jsonl.gz`，公开逻辑键不带该前缀。旧归档和已分配 pending 保持原位置，当前凭据轮换不改历史归属。分类按保存的物理地址核对；外部未覆盖不计入当前部署桶总量，本用途尚未接入增量账目，摘要仍为 partial。

## 当前对象增量与周期核对

`storage_usage_mutations` 在物理I/O前由独立owner事务保存幂等意图。`prediction_inputs` 生产者中，lease／relative／write_nonce构成稳定逻辑组，每次实际条件PUT在I/O前保存独立尝试窗口，412只记零变化，业务发布失败后真正创建的新版本独立保存；未知旧尝试不被重试覆盖。删除按准确endpoint／bucket／key／不可变VersionId去重，不按owner声明次数扣减。同步SDK只收集有界回执，真正收尾后异步owner尽力发布。发布失败保留pending，不将已经成功的业务结果改成失败。物理版本事实与nullable的当前对象增量分开：条件创建成功、完整读回及当前HEAD匹配才增加；精确版本确实移除、删除前为当前对象且删除后无当前内容／回露版本／删除标记才减少。412复用、提交应答不明、进程消失或版本替换不根据最终存在推算增量。

`managed_original_creation` 生产者复用 Creation 现有 prepared→writing 独立事务和同一引擎，在最终条件 PUT／CompleteMultipart 前原子登记意图；通用入口接受调用方的 AsyncSession／AsyncConnection 事务，不提交其业务事务。地址只取冻结 configuration.endpoint、bucket 和完整 key，保留 Creation ID、owner_kind、origin 与独立尝试窗口，集中覆盖 Dataset direct／zip／connector／directory_copy、Task 和辅助原件。真实写回执、完整 SHA／大小读回、当前 HEAD 匹配返回的不可变非 null 版本，并完成原生 close／drain 后才尽力发布正增量；回执或读回持久化及后续业务发布失败不抹去已经取得的物理证据。关闭失败、无版本、null、应答不明或当前版本替换保持 unknown；已核验 Creation 重读不新增。准备分片／abort、浏览器暂存与旧预签名重放不计增量，Retirement／Reclamation 不执行最终物理 DELETE，不能扣减。生产者摘要仍为 partial，projected_total_bytes 保持 null。

盘点页每次最多1,000对象，保存准确key、实际大小和观测窗口；不解码key，不以ETag代替摘要。稳定代次为AsyncJob UUID，执行块的fence代次仅用于拒绝迟到发布。整桶成功才替换基准和对账，保留两个成功代次及当前暂存；重复key、失败和取消不发布不完整基准。数据库按对象比较两次观测，只有完全位于观测之间的已确认事件参与增量核对；跨窗口／pending／unknown及首次无基准单列未知。没有接入的写入方和外部变化只称未解释变化，不宣称精确账目漂移或物理磁盘容量。GET仅返回持久固定摘要。

周期计划存于 `storage_usage_schedules`，默认关闭，管理员通过显式PUT授权开启，间隔1至168小时。已有30秒recover任务复核保存的授权及范围并调度，避免重叠，撤权／配置变化暂停，不能自动换操作者。应用迁移0246后刷新执行盘点、临时输入与托管原件写入的Worker；无镜像或依赖变化。回退先关闭计划，再回退保留schema的旧应用；显式降级仅允许新增表为空，禁止丢弃流水及基准。
