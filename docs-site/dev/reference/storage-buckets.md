---
title: MinIO 存储桶布局
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# MinIO 存储桶布局

平台按九种用途管理对象存储。下表描述默认部署桶；已开放的用途可绑定登记的全局 S3 目标，也可共用物理桶。实际对象地址由各自保存记录决定，不能从用途名或当前桶配置推断历史位置。

## 桶清单与职责矩阵

| 桶名            | 环境变量                     | 存储数据类型                                         | Key 前缀约定                                                                                                                                                                                                                         | Lifecycle 过期策略                                                              | 备份建议                                                 |
| --------------- | ---------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `annotations`   | `MINIO_BUCKET`               | 标注任务源文件、评论附件、标注引导手册资产           | `{project_id}/{task_id}/...`、`comment-attachments/{aid}/...`、`projects/{pid}/guide/...`                                                                                                                                            | 评论附件由独立owner执行90天期限及软删7天宽限，无前缀lifecycle                   | **备份**                                                 |
| `datasets`      | `MINIO_DATASETS_BUCKET`      | 数据集源文件（原始图像/视频/点云/传感器标定）        | `__aap_managed_datasets__/{dataset_id}/_objects/{upload_id}/{relative_path}`；历史真实键保留                                                                                                                                         | 永久                                                                            | **备份**                                                 |
| `bug-reports`   | `MINIO_BUG_REPORTS_BUCKET`   | 快捷 Bug 反馈截图附件                                | `bug-report-attachments/{user_id}/{uuid}-{name}`                                                                                                                                                                                     | 整桶 180 天                                                                     | 不备份                                                   |
| `media-cache`   | `MINIO_MEDIA_CACHE_BUCKET`   | 原图缓存、目录预览、普通缩略图、图片金字塔和视频派生 | 新注册目标在 `_aap_managed/media-cache/` 下使用 `original-cache/v1/`、`thumbnails/objects/`、`image-pyramids/objects/`、`video-derivatives/objects/`；旧地址保持                                                                     | 仅旧 `videos/` 前缀沿用30天规则；新owner按各自TTL／退休合同清理，不新增桶级规则 | 缓存不能代替原件备份；数据库归属及外部目标按实际责任保护 |
| `audit-archive` | `MINIO_AUDIT_ARCHIVE_BUCKET` | 审计日志冷分区归档                                   | `{YYYY}/{MM}/{archive_uuid}.jsonl.gz`                                                                                                                                                                                                | **永久**（合规保留）                                                            | **强备份**（建议开启 Versioning + Object Lock）          |
| `import`        | `MINIO_IMPORT_BUCKET`        | 预标注与数据集导入的临时中转产物                     | `prediction-inputs/{attempt}/original/...`、`prediction-inputs/{attempt}/crops/...`、导入暂存键                                                                                                                                      | 预标输入需启用版本控制，按固定版本清理；移除平台整桶过期，未知旧对象待盘点      | 恢复演练保留责任记录、对象元信息及内容                   |
| `export`        | `MINIO_EXPORT_BUCKET`        | 异步导出的标注数据包                                 | 一次性 Key（ZIP / 多目标富格式导出包）                                                                                                                                                                                               | 整桶 7 天                                                                       | 不备份（短生命周期）                                     |
| `avatars`       | `MINIO_AVATARS_BUCKET`       | **用户头像**（上传后规范化的 `256×256` WebP）        | `{token}.webp`（`token` 为 32 位随机十六进制字符串）                                                                                                                                                                                 | **永久，且不挂 Lifecycle**                                                      | **备份**                                                 |
| `training`      | `MINIO_TRAINING_BUCKET`      | **在线学习与训练闭环资产**：冻结媒体、训练包与权重   | `projects/{pid}/datasets/{vid}/media/`、`projects/{pid}/datasets/{vid}/attempts/{aid}/dataset.zip`、`projects/{pid}/runs/{rid}/attempts/{aid}/`、`projects/{pid}/previews/{preview_id}/`、`projects/{pid}/sample-evaluations/{eid}/` | **永久，不挂 Lifecycle**                                                        | **与数据库一同强备份**                                   |

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
MEDIA_CACHE_PREFIXES = ("thumbnails/", "videos/", "playback/")
```

旧路径命中这些前缀时路由至原`media-cache`桶，否则沿原调用方默认桶。带新owner或受控根的路径不能落入此兜底，也不能通过当前绑定猜读。

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

## 容量监控与日常巡检

- **前端入口**：存储管理展示真实位置、分节点探测和九用途映射，不再重复数据集目录。用途映射来自 `GET /api/v1/storage-locations/bindings`；共享桶不合并用途行。`read_policy=consumer_owned_compatibility`保留各业务固定定位及旧读取路径，`write_policy=deployment_mapping`描述新写入的兼容部署目标，头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件使用`write_policy=current_binding`，分别通过`reviewed_avatar_switch`、`reviewed_bug_attachment_switch`、`reviewed_audit_archive_switch`、`reviewed_export_switch`、`reviewed_import_switch`、`reviewed_dataset_switch`、`reviewed_media_cache_switch`、`reviewed_annotations_switch`和`reviewed_training_switch`支持经预览确认切换。位置配置修订与用途绑定代次独立返回；配置匹配不代表健康或迁移资格。
- **统计边界**：平台对象字节读取持久盘点快照，已采纳源索引规模按准确来源地址去重，二者分列。物理容量、提供方配额及其他缺少可靠测量的指标保持未提供；刷新只查询已有记录，不同步列举全部桶。快照和分项口径见下文。
- **巡检要点**：
  - `media-cache`旧`videos/`前缀保留30天Lifecycle；新缓存需分看原图额度／TTL、缩略图、金字塔和视频owner状态，不能为新受控根添加整前缀过期来绕过读租约及退休清理。
  - `avatars` 单对象不超过约 40 KB（`256×256` WebP），总体积随注册用户数线性增长；若对象数显著多于用户数，可排查是否存在 `users.avatar_ref` 未引用的孤儿对象。

数据集名称只用于展示。托管原件的新键采用不可变数据集身份和独立上传身份，同名不同内容不会覆盖旧样本，头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件可经真实托管探针及预览确认切换新写入位置；已固定对象按各自owner记录读取，历史头像定位缺失时阻止该用途切换，不声称全部历史定位已覆盖。用途策略不复用外部引用适配器的只读能力声明。

独立Task新上传的最终原件位于annotations用途的 `__aap_task_originals__/<Task UUID>/<Creation UUID>/payload`。浏览器只写import暂存区，完整核验封存后保存独立TaskOriginal；删除业务Task或测试fixture不会按前缀删除该原件。历史未知Task不补造归属，全部原件读取者的固定位置接入仍须独立完成。

## 来源故障状态

`storage_fault_states`是独立于对象占用、业务许可和保留账本的稀疏观测表。现有来源扫描、核验／原件读取和预览生成在实际I/O前取得数据库观测序号；稳定范围包含已授权的来源、目标／内容摘要、节点与实际配置依赖。故障去重、次数和水位更新与转换审计同事务，成功仅更新已有匹配状态，不为所有健康文件建行。

恢复必须位于现有最终验证／业务提交之后；原图缓存命中不证明源在线，预览ready的未提交内存状态不证明发布。重复与较旧结果不覆盖较新的故障或恢复，已恢复行保留水位。无行的首次晚到失败保守待复核，不将先前没有行解释为已证明健康。

观测通过当前调用者Engine的独立有界连接执行，不提交调用者业务、不持有父Location／Source锁、不触发物理重试；失败不能改变原业务结果。状态表没有对父业务行的强制锁或删除授权，历史审计不补造状态。迁移0249保留既有内容，应用回退保留观测事实，破坏性降级只允许新增表为空。

公开查询按位置和当前可管理来源裁剪，配置／内容范围状态只由数据库保存的事实比对，不访问物理来源。此链覆盖四类已接入来源故障，不提供完整消费者／站点健康、容量阈值、自动消警或外部通知服务。

## 配置源迁移清单任务

`storage_migration_inventory` AsyncJob将现有只读清单核心用于应用执行；核心位于API服务模块，运维脚本保留兼容入口，不从Worker shell启动CLI，也不复制一套LIST／版本／SHA算法。固定范围为部署端点和九用途桶映射，当前绑定及在途写入仅记录为观测，不能据此证明已停写或全业务覆盖。其他位置的新绑定不使配置源清单失去启动资格。

`storage_migration_inventories`保存冻结范围、当前授权证明、预算、领取／取消状态、32MiB以内的私密报告BYTEA及SHA。单记录和总输出都在写入前约束，公开DTO与通知只返回白名单摘要；报告经独立当前超级管理员与全权Key校验下载，GET不访问源。报告沿现有终态Job30天保留及删除级联，不引入新S3对象用途或无限保留。

Worker对保存task ID仅作一次pending→running领取，显式重投只针对pending，不按TTL接管运行中的原生读流。读取在无数据库锁的受控线程中执行，权限／Key、取消和冻结源范围在检查点及最终发布复核；数据库Session只在所属事件循环使用。协作停止穿透核心issue捕获路径，SDK／Body／输出真实关闭后才发布终态，未封存或关闭不明不提供成功报告。已封存的partial报告、未知覆盖、取消和失败分开表达，`migration_ready`始终false。

应用0255后刷新实际执行迁移清单的所属Worker及任务注册；挂载API源码不需要重建镜像。清单Job只表示源端观测结果，不能推出复制、切换或退役状态。

### 应用侧初次复制

`storage_migration_copy`使用同一引用目录及复制核心，先从B17保存的封存报告生成配对目录及SQLite计划，审阅后再执行初次复制。0256的复制owner独立于单次AsyncJob，保存冻结输入、目标物理身份、处理选择、基线及摘要；逐对象delta在独立短事务中提交。执行时恢复同一基线＋delta，writing／nonce先于PUT，ACK版本先于完整读回，权限或取消的晚期变化不删除已发生事实。

目标为已登记全局S3位置／连接，由薄适配器复用S3Access当前凭据、allowlist与请求guard，在保存base_prefix下生成独立迁移命名空间；不借用业务purpose或改变Binding。仅轮换密钥不会重解释旧地址，运行中修订变化停止下一次I/O，后续attempt重新解析当前配置。新owner计入位置引用保护与维护影响，任务终态和30天清理不解除实际保存责任。

源与目标分别取得Permit，两个operation ID先提交，再启动原生线程；回到所属loop的短Session处理检查及对象delta，不跨线程共享Session，不跨S3 I/O持有事务。流、客户端、SQLite和临时正文真正关闭后才发布；应用正文使用匿名临时文件，检查点权威在数据库。未知关闭保留责任，不按超时清除。

显式续跑创建新attempt，共享Job只描述该次执行。原有全失败项重试保持，附加对象ID选择时只重试这些failed项，本次结果与全计划完整性分列。硬中断的同主机进程结束证明仅结清其准确Permit，nonce及不明写入仍由原条件创建算法恢复；无法证明旧执行已结束时保持unknown。复制子集完整始终不等于`migration_ready`，最终停写、业务引用切换、回滚和退役仍待独立执行。

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

媒体缓存新增使用`thumbnail_outputs`、`original_cache`、`image_pyramid_outputs`、`video_derivative_outputs`四个生产者，共用私有媒体记账helper和原有 mutation 表。共享缩略图入口涵盖目录、Dataset和Task；金字塔及视频按每个物理对象记录，包含manifest／metadata。固定地址来自已保存owner，逐实际条件PUT提交前持久化独立意图；同步物理线程只通过显式局部记账对象回到所属事件循环提交短事务，不传递AsyncSession或替换全局client。

这些受控写入使用独占单次提交client，避免SDK把丢应答后的自动重试412压成一次零变化；旧schema1与schema2默认位置均显式传递该选项，普通读取及其他用途保留原行为。意图事务失败仍是PUT前已知拒绝，不能当成未知物理I/O；发布期间取消须排空记账及原owner检查点，再向调用者继续传递。

保留PUT返回版本及完整读回证据，再核对当前HEAD；只有不可变非null版本、大小和内容一致且原生client／Body真正收尾才确认正增量。部分生成失败或业务发布回滚不抹去已发生事实；应答未知、无版本、后来替换或关闭失败保持unknown，账目发布失败保留pending。该接线只增加观测，不改变原件、生成器或清理算法；旧按key清理不据此产生负量，不新增版本GC或清理门禁。生产者列表是接线覆盖声明，未把历史对象重新记作新增，完整覆盖与推算总量仍未知。

`export_media_outputs`复用同一Recorder，显式指定`purpose=temporary`，四类媒体缓存默认值保持`media_cache`。共享`export_media_transfer.copy_source`负责所属Engine、短事务及实际收尾，`export_media_storage.write_object`在原条件PUT前登记意图并关联原始返回回执及完整读回；schema2默认目标的记账写入显式选择独占单次client。线程、Body、输出client、借用FD和源stage都真正关闭后才独立发布，后续guard拒绝或业务回滚不抹去物理事实；发布期间取消仍排空后继续传递。图片、视频和点云格式保持共用接点，原返回形状、授权与清理算法不变。

`export_zip_outputs`沿ExportOutput保存地址，在普通导出和独立媒体导出的两个既有异步入口接入同一ZIP观测helper。每次managed upload先独立登记一个窗口，独占client保留原SDK配置、分段上传和重试；公开事件核对冻结Bucket／Key、实际final发送次数与最终ACK，不改全局共享实例或上传请求。Complete前失败、没有事件或多次final发送保持unknown。正常一次提交通过HEAD→完整有界版本集合→HEAD观测前后状态，并复用实际完整SHA读回；旧集合无删改、只新增本次返回版本且当前／读回版本一致时确认新旧当前大小之差。集合稳定身份排除IsLatest，不以404、VersionId排序或HEAD大小相同推断新增。明确零值不会解释过去的未决窗口，覆盖缩小的负量只表示当前内容减少。读取观测异常不改变业务结果，native线程、SDK／Body和client真正关闭后才发布；取消和业务回滚保持既有顺序。仍不统计导出清理、退休、历史版本或未完成multipart的物理占用，覆盖保持partial。

`bug_attachment_uploads`在`bug_attachment_uploads.upload`→`bug_attachment_storage.write`共用入口接入同一Mutation表和Recorder，owner为独立上传UUID、分类`other_platform`。耐久意图先于条件PUT；所属独占client保留原SDK配置与重试，公开参数／before-send事件只核对冻结Bucket／Key／IfNoneMatch和实际发送次数。单次明确成功、不可变返回版本、已有准确版本完整读回和当前HEAD一致且原生资源与permit收尾后确认正量；只对明确单次412确认本窗口零，其他冲突、重复发送、无hook或未知证据保持unknown。不为记账引入历史版本LIST、条件写能力前置或新上传器；后续业务失败不抹去写入，普通发布异常保留未决事实且不改业务结果，取消排空后传播。普通读取、期限、退休与清理保持，附件移除与其他生产者仍未覆盖。

盘点页每次最多1,000对象，保存准确key、实际大小和观测窗口；不解码key，不以ETag代替摘要。稳定代次为AsyncJob UUID，执行块的fence代次仅用于拒绝迟到发布。整桶成功才替换基准和对账，保留两个成功代次及当前暂存；重复key、失败和取消不发布不完整基准。数据库按对象比较两次观测，只有完全位于观测之间的已确认事件参与增量核对；跨窗口／pending／unknown及首次无基准单列未知。没有接入的写入方和外部变化只称未解释变化，不宣称精确账目漂移或物理磁盘容量。GET仅返回持久固定摘要。

周期计划存于 `storage_usage_schedules`，默认关闭，管理员通过显式PUT授权开启，间隔1至168小时。已有30秒recover任务复核保存的授权及范围并调度，避免重叠，撤权／配置变化暂停，不能自动换操作者。应用迁移0246后刷新执行盘点、临时输入与托管原件写入的Worker；媒体记账接线改变时也要刷新实际执行缓存／派生物写入的Worker，无镜像或依赖变化。回退先关闭计划，再回退保留schema的旧应用；显式降级仅允许新增表为空，禁止丢弃流水及基准。

导出 ZIP 与独立媒体可经用途映射共同切换新分配位置。新外部目标使用连接基前缀下 `_aap_managed/export/`，保存完整物理 key／prefix；在途媒体包及其配套 ZIP、已有缓存保持原位置和首次七天期限。外部目标的浏览器和脚本下载直接按自身 endpoint 签名，默认目标继续使用平台代理。所有状态归属参与位置保护；盘点按 endpoint、bucket、完整键分类，不把外部未覆盖字节计入当前部署总量，增量账目仍为 partial。

`datasets`的新原件分配可经预览确认切换到已登记global S3；默认目标保留原UUID键，外部目标在连接基前缀下附加`_aap_managed/datasets/`再保留原身份后缀。最终位置与import暂存独立；旧原件及在途Creation按保存地址读取，普通凭据轮换不重解释冻结身份。DatasetAssetLocation／DatasetAuxiliaryOriginal保存当前副本引用，Creation只留历史写入责任；Retirement不代表物理删除，外部目标不自动进入当前九桶占用总量。

`annotations` 管理独立 Task 原件、持久 RLE 对象、评论附件和指南图片；普通几何仍在 PostgreSQL，Mask 格式暂存属 import，缩略图属 media-cache。新外部地址使用连接基前缀下 `_aap_managed/annotations/`，保持原逻辑 UUID 键。五类历史责任记录分别统计，不相加作为对象总数；所有保存状态及 Task Creation 继续保护位置。已有与在途对象按保存地址读取，正常凭据轮换不改写地址；评论附件首次90天期限和既有清理边界保持。外部目标按实际 endpoint、桶和完整 key 分类，不计入默认部署 A 的九桶总量。

training 的五类新分配使用已登记 Binding；外部目标在连接基前缀下附加 `_aap_managed/training/`，原 projects/{project}/{family}/{owner}/ 逻辑路径保持。数据包、输入媒体、实验输出、初始化来源与辅助输出分别沿各自 owner，Version／Run 的删除意图保留原完整前缀。六类责任记录分别统计，不能相加作为对象总数；所有保存状态继续保护位置。当前对象盘点按真实endpoint、桶和完整key识别，外部B不纳入A总量；保持现有当前对象清理边界，不扩展版本历史GC。
