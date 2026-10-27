---
title: 原件验证读取、缓存与保留回收架构
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 原件验证读取、缓存与保留回收架构

本页解释平台如何以严格内容身份读取托管与原地引用原件（`OriginalSource` / `managed_original_reader`）、如何交付认证通用二进制与原生视频分片舞台、如何治理按需完整原图缓存与条件创建直传隔离、如何在 27 个存储家族中固定消费方保存目标（`SavedTarget` / `storage_ref`），以及如何在逻辑删除后通过保留证据、声明保留要求、后台完整内容核验与引用栅栏完成准确版本回收。

- 连接器、受控存储位置与九用途映射见 [存储位置、连接器与用途绑定](./storage-connections)
- 数据集内容身份、目录来源扫描与增量同步见 [数据集、目录来源与增量同步架构](./dataset-sources-and-sync)
- 对象存储迁移、站点 Agent 与容量观测见 [对象存储迁移、站点 Agent 与容量观测架构](./storage-migration-and-ops)
- 面向使用者的指南见 [原件访问、归档与保留回收](/user-guide/datasets/lifecycle-and-retention) 与 [九用途映射与受控切换](/user-guide/datasets/purpose-bindings)

## 统一原件解析与严格读取（`OriginalSource`）

### 独立 Task 原件与严格 Reader

`DatasetItem` 托管原件继续由 `DatasetAsset` / `DatasetAssetLocation` 和原创建记录承担。独立 Task 直传没有 `DatasetItem`，使用 `TaskOriginal`（迁移 0231）保存当前 `annotations` 位置、完整 SHA/大小和源写入回执；`Task.original_id` 引用它，原件记录不 FK 业务 Task/Project/User。历史点云即使没有直接 `dataset_item_id`，也可能通过 `primary_lidar` 关联 `DatasetItem`，不能仅凭 NULL 直接认定独立原件。

`ManagedOriginalCreation` 显式区分 `dataset` / `task` 作用域（迁移 0230）。Task 分配保存 `project_id/task_id`、独立随机最终键和 `import` 暂存键；沿既有条件 multipart writer 执行完整回读、真实关闭和取消收尾。Creation 的历史桶、版本及回执不可改，当前位置通过 `TaskOriginal` 表达。两个业务发布检查之间释放 Task/凭证锁，末端短锁重核同一上下文才关联原件；失败或业务删除保留独立记录，不补偿删除。离线恢复只映射 `TaskOriginal` 当前 `bucket/storage_identity`，保留源 Creation 和原 `ETag/VersionId` 事实。

严格图片内容入口（单进程预算 `256 MiB`、独立四槽）与数据集质量检查使用 `original_sources` 冻结当前关系及保存位置。Task 直接 Item 与 `primary_lidar` 必须一致；Dataset 选择唯一 `present` 托管原件，不把缓存纳入候选，也不要求历史核验记录已有 Creation。物理读取前、取得耐久读取许可后及响应前，分别重核当前权限、原凭证、关系、位置和维护修订；业务行锁不跨物理读取。原地引用的来源读取与完整缓存命中同样在许可取得后复核原凭证，响应前比较完整引用 context 并锁定同一 Gate 代次；撤权不把合格缓存误记损坏，也不会回退读源。

`managed_original_reader` 只接受端点、保存桶/键、完整 SHA 和大小，以及调用者显式预算。它建立独立客户端，从目标当前 HEAD 取得条件，验证错误 `If-Match` 确实失败，完整读取并核对摘要、EOF 及最终 HEAD，再真实关闭 Body 和客户端。历史写入 `VersionId` 不参与目标 GET。返回的 `BinaryStage` 拥有临时文件和对应准入名额，取消和超时等待实际 I/O 退出；关闭无法确认时保留未收尾许可。图片适配器在同一文件上解码并移交给响应，不重新下载或复制。

### 样本评估、主动学习与点云质检的原件输入

- **样本评估（`sample_evaluation_originals`）**：沿 `original_sources.resolve_task` 固定当前 `TaskOriginal` 或 `DatasetAsset`，取得同一位置的耐久许可后调用 `managed_original_reader`（独立两槽、每图最多 `16 MiB`）。先验证当前已登记 SHA，再比较冻结评估 SHA；未知归属零原件 I/O。Pillow 在同一临时文件上执行 1,600 万像素、单帧及 EXIF 检查。
- **主动学习（`active_learning_originals`）**：收口预览／创建、Worker 缺失 PNG 重建及接纳前整组复核。新内部快照仅冻结原件身份、SHA／大小及逻辑位置 ID，实际 locator 始终取 canonical owner 的保存位置（单图 `16 MiB`、独立两槽）。HTTP 权限证明与后台持续授权分开：后台保存并核对原 Key proof 指纹。
- **点云质检（`point_cloud_quality.originals`）**：新范围声明 `original_contract=1`，固定安全原件收据（`owner_kind`、内容 UUID、完整 SHA-256、大小与逻辑 `location_id`）及轴约定（默认 `iso_8855`）。读取复用 `managed_original_reader`（单 PCD `256 MiB`、独立两槽；复制 120 秒、传输 30 秒、解析与归一化 30 秒）。所有实际读取与解析收尾后，末端锁定当前授权、Task、Scene、标注、轨迹并共同提交。

### 认证通用原件交付（`512 MiB`）

浏览器公共读取模块 `apps/web/src/api/originals.ts` 只构造 Task/Item/精确 camera link 的认证请求，核对身份中的完整下载路径，不给任意 URL 附加 Bearer。原件下载共用两路可取消排队，覆盖完整响应体，前景可提升预取；只有明确容量不足才有限重试。资源 owner 按账号/session 划分，同 session 令牌刷新后在途旧凭据结果最多重试一次，不能覆盖新凭据；跨账号、session 及不完整的跨标签凭据停止读取。

`original_binary` 按 Task、精确相机 link 或 `DatasetItem` 冻结来源；`originals` 路由提供零物理 I/O 的身份（`/api/v1/files/tasks/{id}/original-identity`）及绑定 content UUID/SHA 的完整下载。相机只借当前 Task 可见性读取其明确 `camera_` 链接；Item 入口要求 Dataset 管理权限并校验 `dataset_id`。

`original_binary_response` 复用严格 reader，单文件 `512 MiB`、独立两槽，槽覆盖复制与响应到真实文件关闭，声明大小的最大同时暂存为 `1 GiB`。匿名临时文件不产生命名缓存；响应前断连、取消、超时及发送失败等待在途线程收尾。复制和响应各 120 秒为合作式期限。`octet-stream` attachment 不执行图片解码，也不支持 Range/HEAD 或条件 304；大文件明确 413，不隐式回退签名地址。

### 视频原件临时表示与范围租约

`original_binary.authorize_original / recheck_original / capture_authorized` 为完整下载与视频准备共用原 Authority 和保存来源合同。`original_playback` 只接托管 video，Bearer 准备完整核验；`original_playback_registry` 按 Authority/session/资源/完整来源保存进程内匿名 stage 及独立短租约。能力秘密只在准确路径的 `HttpOnly/SameSite=Strict` Cookie 中发送，JSON 与 `media_path` 不含秘密，不保存原 JWT/API Key。

每次 native GET/HEAD 先取得响应 pin，重核原凭证、当前 User/Key/scope、Task 可见性或 Dataset 管理权限、同一源和 Gate，再同步重核租约仍有效，才返回 200/206/HEAD/范围错误。浏览器同源判断同时采用 Fetch Metadata；跨站与同站异源媒体请求拒绝。`original_playback_response` 用 `os.pread` 独立偏移，各响应取消后 drain 真实线程再 unpin，不关闭其他消费者的文件。完整 SHA 证明来自准备时的全量捕获。

单 stage `5 GiB`、两 stage、最多 8 响应，每 stage 32 租约；准备 900 秒、transport 30 秒、响应 120 秒为合作式期限。租约 300 秒，stage 最迟 3600 秒退休，30 秒 reaper 与 lifespan 关闭负责收尾。此 API 只资格化当前单 Uvicorn 实例，其他实例或重启后的 410 必须重新 Bearer 准备；方案与取舍记录于 [ADR 0107](/dev/adr/0107-scoped-native-video-stages)。

显示消费者按账号/session、资源及内容 UUID/SHA 管理 Blob、Bitmap、RGBA 与 GPU 资源；图片预取最多保留两份、原始字节共 `64 MiB`，前景读取仍受 `512 MiB` 单文件与既有解码预算约束。SVG 使用不透明的 data URL，普通位图使用可撤销 Blob URL；必需金字塔预览不触发完整原件下载。

## 完整原图缓存的资源归属

位置详情按独立位置修订管理缓存，不与远端连接凭据混合。用量接口只读账本；清理请求按当前 `Actor → Key → Location → Entry` 锁序封存截止观测前的最多 100 代，锁后复查原 Key 和角色、跳过活跃租约，实际对象删除由周期 owner 执行。`original_cache_recorded` 及登记/到期时间是元数据证据，不覆盖原来的来源状态；样本原图读取动作允许尝试合格旧副本，仍由统一 reader 实际核验。

`services/original_cache.py` 提供完整副本资源所有者，统一引用 reader 与位置/来源管理已接入。公开策略按 `none < preview < on_demand` 取来源请求和位置上限的较小值，按需原图包含预览；完整缓存要求来源和位置均选用按需策略、位置有显式正额度，默认额度为零。只有超管配置位置额度/TTL，来源管理不能扩大预算。`reference_context` 保存当前业务授权与逻辑内容身份，`reference_source_context` 只在实际读源或质量检查时获取来源节点、锚点和连接配置；`reference_snapshot` 在缓存/来源 I/O 前后复验身份与 Key，本资源服务不签发读取权限。

缓存命中只替代来源物理可达性，不绕过任务分派、成员、账号、来源/位置启用和接受修订；最终事务仍锁住 `Connection/Location/Source/Item/Asset`，不再锁住与逻辑身份无关的 Probe。缓存缺失或损坏时重新按来源物理合同读取。首次严格读源成功后，独立连接提交可选保留；不提交调用方事务，外层持有业务锁时允许有界跳过保留。额度满时最多淘汰八代未被读取的完整缓存，再尝试保留；单图超过位置总额度时不淘汰。质量检查保持 `capture_for_quality` 的来源语义和坏图诊断，不借用缓存掩盖来源变化。

`original_cache_entries`（迁移 0213）独立保存来源位置、Asset/SHA、大小、策略修订、保存目标事实和 generation；没有指向业务对象的级联外键。上传前独立提交预约，位置级事务锁保证并发请求不超订；上传中、已就绪与待删除对象均占用原图额度。同一 Asset 只允许一代正在填充或发布的缓存，旧代回收记录继续保留。

成功 PUT 后按块重新核验实际完整对象的大小和 SHA，再在短事务内复验策略、额度、来源修订及保存目标，发布独立 `original_cache` `DatasetAssetLocation`。读租约固定 60 秒，正常捕获结束即释放；请求取消但 SDK 线程未结束时保留租约至到期。TTL 默认一天，从预约时固定，访问只更新最近使用时间；配额收缩、策略禁用、业务删除和过期均由 `cleanup` 队列每分钟分批核对。先移除精确旧代 locator，再删除自有 key，确认删除后才释放额度；不确定 PUT 和失联写入保留七天重扫窗口。

旧对象保留 `media-cache` 的 `original-cache/v1/<Asset>/<SHA>/<generation>`；新对象以保存引用中的物理前缀定位，逻辑后缀为 `original-cache/v1/<Asset>/<SHA>/<Entry UUID>/payload`，不受 `videos/` 生命周期影响。

## 托管原件条件创建与直传隔离

`managed_original_creation` 是新数据集直传、ZIP、连接复制和目录 `managed_copy` 的共同创建 owner。迁移 0219 独立账本先提交最终键、入口和预算、当前配置/绑定及权限快照；无业务/Location 级联外键，避免未提交业务外键自锁。状态经准备、冻结输入、单次最终写入、实际回执、完整回读进入 `verified`；所有初始事实、冻结输入、写回执和终态均受数据库约束。来源与目标维护 Permit 分别登记，目标 operation 在首个物理 I/O 前独立提交。

直传 `init` 先预留最终 locator，只向 `import` 桶 `__aap_upload_staging__/` 签 PUT；该根为保留名称且不进入 managed 扫描。`complete` 完整条件读取后冻结源 SHA/大小/ETag/真实 `VersionId`，用单 part `UploadPartCopy` 准备不超过 5 GiB 的暂存对象，最终 Complete 带 `IfNoneMatch=*`；空对象走条件 PUT。版本化源的复制响应若缺少源版本回显，先确认中止本次 MPU，再按冻结版本与 ETag 流式读取、完整核对 SHA/MD5/大小并准备新的分块上传；关闭读取句柄后重核来源仍为当前版本，复制与回退共用原截止预算。显式错误的版本回显仍拒绝。普通导入分块计算完整摘要并以相同条件完成 MPU。禁止 `CopyObject` 封存与无条件降级；目标命名空间不签客户端 PUT。已有旧直传、扫描及历史原件不回填创建事实。

SDK 每步单次尝试，真实成功响应先独立持久化，再按真实非 `null` `VersionId` 或 ETag 条件完整回读、核对 SHA/大小；无真实版本时最终 HEAD 复查当前观测。调用者在原事务重验权限、配置、维护代次和精确内容后关联 `AssetLocation.creation_id`。业务回滚、去重或提交应答不明保留最终对象及独立责任；仅可 abort 明确的本次 `UploadId`。写入/落库应答丢失、构造或关闭不确定保留 `unknown`/中间状态与未收尾 Permit，不能靠 TTL、HEAD 404、同 SHA 或后来的 412 提升为已创建。

退休断言在同事务捕获可空 `creation_id`，并通过 `RESTRICT` 保留原始证据；已有断言禁止补写创建关联。离线恢复对创建账本所有列计算受保护摘要，不重定位或伪造目标创建响应。

## 消费方固定存储归属（SavedTarget）

平台所有持久或临时对象均通过独立的消费方归属记录（SavedTarget）保存分配时的 `storage_ref`，与当前用途绑定解耦：

### 数据集原件与 NuScenes 辅助原件

- **数据集原件（迁移 0251）**：`ManagedOriginalCreation` 承担唯一写入责任；新 `dataset` / `dataset_auxiliary` 分配把无密钥的原件地址放入 `configuration`。`DatasetAssetLocation` 和 `DatasetAuxiliaryOriginal` 的 nullable `storage_ref` 保存当前副本地址；外部目标在连接基前缀下附加 `_aap_managed/datasets/` 再保留原身份后缀。
- **NuScenes 辅助原件（`DatasetAuxiliaryOriginal`，迁移 0235）**：保存 verified Creation 对应的独立当前位置与内容事实，没有 `Dataset/Item/Scene` 业务级联。Creation 的 `dataset_auxiliary` 范围使用专属 `__aap_auxiliary_originals__/{dataset UUID}/{creation UUID}/payload`。辅助 resolver 只绑定原有导出权限下的准确 `Task/主 Item/Scene`。

### 标注用途四家族（迁移 0227–0231、0253）

`annotations_storage` 集中解释新 `schema2` 的封闭十字段引用（`schema / purpose / location_id / config_revision / binding_generation / endpoint_url / bucket / object_key / storage_identity / key_prefix`），受控根连同 `_aap_managed/annotations/` 最多 896 字符／920 个 UTF-8 字节：

1. **独立 Task 原件（`TaskOriginal`）**：最终原件位于 `__aap_task_originals__/<Task UUID>/<Creation UUID>/payload`，与 `ManagedOriginalCreation` 配合隔离客户端直传。
2. **持久 RLE 光栅掩码（`RasterMaskObject` & `RasterMaskPublication`）**：不依赖 Task、Project 或 User 外键。可移植引用继续使用 `raster-masks/sha256/...json[.gz]`，物理位置固定为 `raster-masks/objects/{uuid}.json[.gz]`，同时保存规范 RLE 与实际存储字节的两份 SHA/大小回执。`purge_unreferenced_raster_masks` 保留 24 小时窗口，首轮只退休并提交，后续轮次才完整核验并删除固定 UUID。
3. **项目指南图片（`GuideAssetUpload` & `ProjectGuideAsset`）**：`GuideAssetUpload` 保存上传者与原项目快照、SHA-256、大小（1 B 至 5 MiB）与固定 `storage_ref`（`projects/{original_project_uuid}/guide/{upload_uuid}`）；`ProjectGuideAsset` 是当前项目读取权和存活引用的权威。普通图片使用 Blob URL，SVG 用原始字节 `data:image/svg+xml;base64,...` 在不透明源中展示。最后一个引用解除时同事务退休归属，由 `purge_guide_assets` 两阶段清理。
4. **评论附件（`CommentAttachmentUpload`）**：保存上传者、标注、任务、项目及首次绑定评论的 UUID 快照（无业务外键），最大 20 MiB，键为 `comment-attachments/{annotation_uuid}/{upload_uuid}`，固定 2160 小时（90 天）期限及软删 7 天宽限。

### 头像、报告附件与审计归档（迁移 0223、0225、0226、0243、0245、0247）

- **上传头像（`AvatarUpload`）**：随机 token 对应独立 `AvatarUpload`，保存端点、精确键、位置修订、绑定代次、物理身份、规范化 WebP 的 SHA-256/字节数及一次性 `VersionId` 回执（外部目标位于 `_aap_managed/avatars/`）。替换或清除先提交引用变化及 `retired` 意图，无任何用户引用时由 `cleanup` 队列精确删除。
- **报告附件（`BugAttachmentUpload`）**：上传前持久化 `pending`，固定准确 `Location/endpoint/bucket/完整受控键`（`_aap_managed/bug-reports/bug-report-attachments/<owner>/<upload>.<ext>`）与 `VersionId`；180 天保留期限由 `bug_attachment_storage` 按单次上传独立执行，不依赖整桶生命周期。
- **审计归档（`AuditArchiveOutput`）**：`audit_archive_storage` 在候选提交前分配固定目标（`_aap_managed/audit-archive/{YYYY}/{MM}/{archive_uuid}.jsonl.gz`），物理读取沿用 `HEAD/IfMatch/完整 SHA` 与 JSONL 行数及语义核验。

### 导出 ZIP 与独立媒体交付（迁移 0211、0224、0234、0236、0237、0248）

`export` 的物理归属由 `ExportOutput` ZIP 和 `ExportMediaBundle` 独立媒体共同承担，`ExportArtifact` 仅索引缓存。媒体包分配后，配套 ZIP 继承同一保存目标（`_aap_managed/export/`），共用首次 168 小时（7 天）截止期限和一次 3600 秒 claim。

#### 引用图片的后台导出

`services/exporting/reference_media.py` 负责引用导出的固定选择、原请求授权证明和原图快照生命周期；项目、批次与 Data Manager 在入队时封存私有 `export_authority`（含原 API Key hash 与任务/Item/Asset/来源修订）。导出按项目 `EXPORT_ANNOTATIONS` 能力授权；实际读源时由 Celery hostname 决定 `worker:<hostname>` 锚点，每份快照校验 SHA、大小和图片尺寸后再写包。引用包采用 job 隔离 cache key 并禁用 cache hit。

#### 点云导出的独立媒体责任

`ExportMediaBundle` 在新 Job 事务内保存有界私有输入、规范原件事实、逻辑路径和不可变预期 SHA／大小清单。源读取与交付复制使用两槽 admission，受耐久 `export_media_<bundle UUID>` 许可保护。覆盖 KITTI、Point Mask、多相机 COCO 和 NuScenes（相机沿准确 Task link，raw／map 沿 `DatasetAuxiliaryOriginal` 指针）。

#### 图片冻结输入与交付

异步图片导出使用独立输入合同和 `image_media` 私有证明，复用 `ExportMediaBundle` 的物理复制、固定截止、一次 claim、ZIP 配对、缓存核验和退役账本。HTTP 先 SQL 计量数量／JSON，再有界固定 Project、精确范围与 Task、canonical 原件和格式所需业务行；Worker 仅从冻结业务视图序列化，最终短锁重新核对父范围与标注。

#### 视频冻结输入与交付

视频异步导出使用输入 `schema4`（单件原件 5 GiB、最多 1000 Task、唯一原件 20 GiB）、私有 `video_media` 证明和同一 `ExportMediaBundle` 生命周期，覆盖十种视频目标。Worker 沿保存位置完整核对条件读取、SHA 与大小，在同一受许可保护的 stage FD 上运行本地受限 `ffprobe -count_frames`，形成 `metadata_receipt` 随 ZIP manifest、Video JSON 和 AAP 来源保存。

### 媒体缓存与同源交付（迁移 0232、0233、0252）

`media_cache_storage` 统一解释 `media-cache` 新分配的 `schema2` 地址（`_aap_managed/media-cache/`），涵盖四类所有者：`OriginalCacheEntry`、`ThumbnailOutput`、`ImagePyramidOutput` 与 `VideoDerivativeOutput`。

`thumbnail_outputs` 及金字塔／视频适配器在原有业务授权和完整输出核验后签发 `media_cache_urls` 短期同源能力 URL。`media_cache_response` 只交付准确输出／relative，支持 GET、HEAD、单 Range 及 `If-Range`；HMAC audience 与登录分离，token 包含保存引用摘要、SHA、大小和到期，不含 endpoint 或凭据。同源交付每进程最多 4 路物理传输，另允许最多 64 个请求异步等候（最长 30 秒）。

### 训练用途五家族固定归属（迁移 0220–0222、0254）

`training_dataset_storage` 及四个薄 adapter 复用五组前缀 owner（`TrainingDatasetVersion`、实验 `runs`、原生预览 `previews`、逐样本评估 `sample-evaluations`、主动学习 `active-learning`）；`TrainingStorageDeletion` 承担 Version／Run 的删除责任。每个版本的独立 `storage_ref` 保存已登记 Location/配置修订、绑定代次、端点、实际桶、版本前缀（外部目标附加 `_aap_managed/training/`）及物理身份。历史归属为空时，显式核验操作只取已登记的 legacy A 位置，完整只读核验保存“当前保留副本”证据（详见 [训练版本 API](/api/guides/training-datasets#固定存储归属与历史版本核验)）。

### 导入暂存与预测临时输入回收（迁移 0212、0238–0241、0250）

`import` 新分配固定实际 `Location/代次/endpoint/bucket` 和受控物理前缀（`_aap_managed/import/`），涵盖 `PredictionInputLease`、`ManagedOriginalCreation.staging_ref` 与 `MaskFormatUpload`：

- 多阶段预标、视频预标及模型快照重试的帧／ROI 使用独立的 `prediction_input_leases`：上传前提交归属记录，前缀只由服务生成的 attempt UUID 推导（`prediction-inputs/<attempt UUID>/`）。正常处理结束主动清理，异常或失联由 `cleanup` worker 按保存位置与准确 `VersionId` 恢复。
- 后台图片预标通过独立授权 owner 消费严格引用快照，根阶段、geometry 和 ROI 使用同一份字节，最终短事务以专有 receipt 写入候选。详见 [预测输入生命周期](./prediction-pipeline#crop-投递通用化)。

## 托管原件保留证据、核验与准确版本回收

### 托管原件移除后的保留证据

迁移 0216 的 `managed_original_retirements` 保存已登记托管原件关系的逻辑移除断言。`DatasetService` 在既有 Dataset/Item 删除锁内、级联删除前，以同一业务事务流式批量保存历史 `Dataset/Item/Asset/Location ID`、精确内部 locator、已有内容证据及当时可证明的配置/绑定身份。无业务外键；失败或调用方回滚同时撤回断言，重复捕获以唯一摘要幂等。外部 reference、缓存及派生角色不进入此账本。

配置核对只证明移除时初始平台绑定与运行配置一致，不证明写入时所有权、独占性、对象存在或当前字节。不能确认的旧证据保存 `unknown`，不阻断原有合法逻辑删除。`policy_revision=1`、`retain_until=null` 默认无限期保护。全局只读预演以一个 SQL 观测重新汇总当前精确 locator 及样本/任务关联、保留训练 snapshot 和主动学习历史，再产生有界页，输出 `execution_permission=false`。

### 显式策略与声明保留要求

迁移 0217 为一条保留断言提供显式期限设置，以 `policy_revision` 进行 CAS，`null` 仍为无限期；不推定生产保留天数。独立 `managed_original_retention_holds` 以 `RESTRICT` 外键锚定该断言，没有业务或操作者级联外键。客户端 UUID 绑定预览、活动编号按锚点/类别唯一；创建字段及已解除历史由数据库约束保持，解除保留时间、原因、操作者和修订，不自动到期。

全量同 `bucket/object_key` 记录保守归组：任一 `null` 期限令整组无限期，否则取 `max`；任一 peer 的有效声明要求保护全组。定位采用有界摘要索引并保留精确等值条件，摘要碰撞不能合并不同定位。仅当前超级管理员/全权 Key 可读取、预览和提交。五分钟确认绑定规范候选/原因、锚点修订/历史事实、完整 peer/hold 指纹与本地/持久配置；成功变更和安全审计同事务。写入按 `User → Key → bucket/key 事务 advisory 锁 → 锚点 → hold`，所有保留控制共用组锁。

### 已登记原件的完整内容观测

核验表（迁移 0218）独立于业务对象，以 `RESTRICT` 关联退休锚点。创建时固定可信历史 SHA/大小、锚点内容/定位与本地/持久平台配置、操作者/Key 权威以及作业/任务身份，不重写退休断言或保留策略。已完成、失败及未完成责任均保留；通用 AsyncJob 删除后，领域历史不随之丢失。

GET 分页历史只读；显式 POST 创建或复用活动任务，要求当前超级管理员及 Key 全权。只允许已知平台身份、可信历史内容且在 `5 GiB` 预算内的原件开始读取；没有历史 SHA 时不通过新读取伪造历史匹配。任务按 `User → Key → Retirement → Verification → AsyncJob` 的锁序做短事务，I/O 前提交，结果发布前重新验证权限、配置、锚点和任务状态。`storage_access` 在 executor 开始前提交精确 Permit ID；取消、超时等待底层线程及流实际结束，关闭不确定保留访问责任。

读取使用短超时、单次 SDK 尝试及 `256 KiB` 流块，不做图片解码或完整临时文件：当前 HEAD 固定对象观测，验证条件语义后按 `VersionId/IfMatch` 读取全部字节，比较 SHA/大小，最后以无版本条件 HEAD 再次核对当前键。60 秒协作截止和总字节预算拒绝未完成结果。

### 准确版本回收与引用栅栏

`ManagedOriginalReclaimAttempt`（迁移 0260）固定退休锚点、Creation、保存目标、准确非 `null` `VersionId`、SHA/大小、操作者/Key、Gate 修订和原因。`ManagedOriginalReclaimClaim` 按完整 `bucket/key` 保守互斥；认领在首个 SDK 请求前提交，可能发出 DELETE 后永久保留 tombstone，Job 过期不移除责任。独立最近引用核对状态记录每日或显式 DB 重查的计数、未知覆盖和有界差异，不给执行授权。

引用发布先取得既有业务锁，再取同组共享事务栅栏并检查 claim，持有至引用事务提交；回收取得组独占锁，只以 MVCC 重查保护，不反向等待 Dataset/Task 锁。保护查询支持当前训练和主动学习的原件凭据，并检查 `TaskOriginal`、`DatasetReference` 及 `ScanEntry` 的保存地址。未知历史保持阻断，当前 Binding 不替换旧物理目标。

逻辑位置停止和真实 Operation 归零用于读取排空。同物理 S3 端点/桶的已登记位置合并检查；原件读取在登记 Permit 后、SDK 前，以独立短事务检查 claim。回收专用 Permit 只绑定当前 attempt、task、准确目标及 stopped Gate 修订；活动责任阻止 resume 和关窗。

执行仅支持永久分配、已验证并结清、保存准确创建版本的退役 Dataset 托管原件。对该版本完整读取 SHA/大小，删除前再次重查权威、组策略/引用、配置与 Gate 并提交 intent，随后只执行准确 `VersionId` 删除与缺失确认。读取、intent、ACK 及缺失分别持久检查点；取消或权限变化不抹去已发生的物理事实，线程、Body、client 和 Permit 实际结束后才结清。准确版本删除无需推断整桶独占，其他版本保留；缺少准确版本的旧对象仍不自动回收。
