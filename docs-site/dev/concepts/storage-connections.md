---
title: 存储位置、连接器与用途绑定
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 存储位置、连接器与用途绑定

平台将**外部数据源接入**、**受控存储位置注册（StorageLocation）**与**内部九用途目标分配（StorageBinding）**显式分层：

- **存储连接器（StorageConnection）**：管理 S3 / OSS / MinIO 兼容对象存储与 SFTP 凭据（Fernet 加密）、SSRF 白名单校验、条件编辑修订（`expected_revision`）与连通性探测。
- **受控存储位置（StorageLocation）**：统一表达平台默认位置、受控本地目录（`filesystem`）、外部对象存储（`s3`）、只读 SFTP（`sftp`）与只读跨网络站点 Agent（`agent`），维护授权范围、节点探测健康与位置级访问门禁（Gate）。
- **九用途绑定与保存目标隔离（StorageBinding & SavedTarget）**：将 `avatars`、`bug_reports`、`audit_archive`、`export`、`import`、`datasets`、`media_cache`、`annotations`、`training` 九种平台用途的新分配目标与历史已保存对象的物理引用（`storage_ref`）解耦——切换用途绑定只影响后续新分配，绝不隐式改写或搬迁已有对象。

面向使用者的操作指南见 [存储位置与连接器](/user-guide/datasets/storage-connections) 与 [九用途映射与受控切换](/user-guide/datasets/purpose-bindings)；逐端点契约见 [API · 存储连接器](/api/guides/storage-connections) 与 [API · 受控存储位置](/api/guides/storage-locations)；密钥与白名单部署配置见 [参考 · 连接器安全](/dev/reference/connector-security)。

数据集与存储领域的其余三篇核心架构文档：

- [数据集、目录来源与增量同步架构](./dataset-sources-and-sync)：内容身份模型、受控本地目录与 S3/SFTP 原地引用、可恢复扫描与批量登记、增量同步状态机、数据集工作台查询与图片质量复核
- [原件验证读取、缓存与保留回收架构](./original-access-and-retention)：统一 `OriginalSource` 与严格快照、认证下载与原生视频分片舞台、按需原图缓存、条件创建与直传隔离、各消费方固定存储归属及托管原件保留回收
- [对象存储迁移、站点 Agent 与容量观测架构](./storage-migration-and-ops)：配置源迁移清单、可恢复复制计划与增量批次、迁移维护窗口与原生源端写封闭、已封存读取映射、九用途原子切换与回滚、只读站点 Agent 与容量观测

## 代码入口

| 关注点                   | 位置                                                                                                                                                                                                                                                                                    |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 连接器与位置路由         | `apps/api/app/api/v1/storage_connections.py`、`storage_locations.py`、`storage_maintenance.py`、`storage.py`                                                                                                                                                                            |
| 数据集与来源路由         | `apps/api/app/api/v1/datasets.py`、`dataset_sources.py`、`dataset_lifecycle.py`、`dataset_views.py`、`dataset_quality.py`、`dataset_quality_review.py`、`dataset_reference_replicas.py`                                                                                                 |
| 原件、回收与迁移路由     | `apps/api/app/api/v1/originals.py`、`original_playback.py`、`storage_reclamation.py`、`storage_migration_inventories.py`、`storage_migration_copies.py`、`storage_migration_windows.py`、`site_agents.py`、`site_agent_transport.py`                                                    |
| 核心 ORM 模型            | `apps/api/app/db/models/storage_connection.py`、`dataset_asset.py`（含 `StorageLocation` / `StorageBinding`）、`storage_maintenance.py`、`dataset.py`、`dataset_source.py`、`original_cache.py`、`managed_original_*.py`、`storage_migration_*.py`、`storage_usage.py`、`site_agent.py` |
| 连接器与安全门禁         | `apps/api/app/services/storage_connection.py`、`connector_guard.py`、`app/core/crypto.py`、`storage_presigning.py`                                                                                                                                                                      |
| 位置、绑定与维护协议     | `apps/api/app/services/storage_location.py`、`storage_location_list.py`、`storage_location_change.py`、`dataset_binding_change.py`、`storage_access.py`、`storage_maintenance.py`、`storage_maintenance_impact.py`、`storage_maintenance_targets.py`                                    |
| 数据集、来源与质量服务   | `apps/api/app/services/dataset.py`、`dataset_assets.py`、`dataset_sources.py`、`dataset_scans.py`、`dataset_registration.py`、`dataset_references.py`、`dataset_source_sync.py`、`dataset_workbench.py`、`dataset_quality.py`                                                           |
| 原件读取、创建与保留回收 | `apps/api/app/services/original_sources.py`、`managed_original_reader.py`、`managed_original_creation.py`、`original_cache.py`、`managed_original_retirement.py`、`managed_original_retention.py`、`managed_original_verification.py`、`managed_original_reclaim.py`                    |
| 迁移、Agent 与容量统计   | `apps/api/app/services/storage_migration_*.py`、`site_agents.py`、`agent_transport.py`、`agent_storage.py`、`storage_usage.py`、`storage_usage_categories.py`、`storage_usage_accounting.py`、`storage_observation_rates.py`                                                            |
| 共享只读原语与 Agent     | `apps/_shared/filesystem_read`、`apps/_shared/site_agent_contract`、`apps/storage-agent`                                                                                                                                                                                                |
| 数据库迁移范围           | `apps/api/alembic/versions/0086_storage_connections.py`、`0087`、`0194`–`0267`（涵盖内容身份、受控位置、扫描登记、原图缓存、维护 Gate、保留回收、九用途固定归属、迁移封存、站点 Agent 与显式引用副本）                                                                                  |

## 访问端点与签名

`storage_presigning` 在原客户端的 `before-sign.s3` 阶段选择访问端点，不在签名后替换 Host。调用者显式选择 `browser`、`internal` 或 `ml`；下载默认用于浏览器。临时预测输入继续绑定确切 `VersionId`，有效期、对象键及响应参数保持原合同。

- `internal` 使用已核对归属的客户端端点。
- `ml` 使用内部协议与 `ML_BACKEND_STORAGE_HOST`；该值为空时使用内部端点，不继承浏览器公共地址。
- `browser` 的绝对 `MINIO_PUBLIC_URL` 使用公共 origin 签名；相对 `/minio` 使用内部端点签名并仅返回代理路由。

签名目标通过上下文隔离并在异常后恢复；物理客户端的端点、凭据、读写围栏和关闭归属不改变，也不为了签名创建额外客户端。跨 origin 签名要求路径式桶地址；发现虚拟主机桶地址时明确拒绝，不能丢掉桶名。相对代理和绝对公共代理对上游 Host 的要求不同，见[部署签名契约](/ops/deploy/docker-compose#对象存储访问与签名契约)。真实跨站点、TLS 与代理部署须在目标环境验证，本机合同测试不证明其网络可达。

## 数据模型

### 位置列表的持久摘要

`GET /storage-locations` 在可见位置分页后批量组装列表项：连接与访问维护状态、保存的节点探测，以及当前用户可见来源的已采纳索引规模。`StorageLocationHealth` 按统一事务时间统计当前配置且未到期的节点结果，保留最近历史记录和最早有效到期时间；不把位置停用解释成网络故障。列表 GET 没有物理 I/O，不调用每行详情或源扫描。

`storage_source_usage.read_usage_page` 将既有去重查询按位置分区，详情单位置统计也调用同一实现。候选关联和地址验证保留在 SQL 中，不把逐文件事实载入应用；跨位置域不混合，权限裁剪在聚合之前生效。列表的源观测时间、探测时间与查询计算时间各自保留，平台对象快照和未知物理容量继续使用独立口径。

### 存储连接器

`storage_connections` 表的核心字段：

| 字段         | 类型          | 说明                                                                                   |
| ------------ | ------------- | -------------------------------------------------------------------------------------- |
| `kind`       | `String(20)`  | `s3` 或 `sftp`（CHECK 约束）                                                           |
| `config`     | `JSONB`       | **非密钥**配置（endpoint/bucket/host…），写入前经 `_validate_and_sanitize_config` 过滤 |
| `secret_enc` | `LargeBinary` | Fernet 加密后的密钥密文，可空                                                          |
| `scope`      | `String(20)`  | `global` 或 `owner`（CHECK 约束，默认 `owner`）                                        |
| `revision`   | `Integer`     | 独立编辑修订，PATCH 使用 `expected_revision` 做条件保存                                |
| `created_by` | `UUID`        | 创建者                                                                                 |

关键不变量：**明文密钥永不落库、永不出 API**。`config` 与 `secret` 在 schema 层就分离——`config` 是可读的连接参数，`secret`（access/secret key、密码、私钥）只进 `secret_enc`，对外仅以 `secret_set: bool` 表达“是否已配密钥”。

> 历史字段 `project_id` 已弃用：当前作用域是「个人 / 全局」，新建连接器不再写入项目级归属。

### config / secret 形态

```text
S3   config: { endpoint, bucket, region?, base_prefix?, use_ssl? }
     secret: { access_key, secret_key }

SFTP config: { host, username, port?, base_path?, auth_type }
     secret: { password }  或  { private_key, passphrase? }
```

## 作用域模型

| scope    | 谁能建            | 谁能读配置                  | 谁能改/删     |
| -------- | ----------------- | --------------------------- | ------------- |
| `owner`  | 项目管理员 / 超管 | 具有管理角色的创建者 + 超管 | 创建者 + 超管 |
| `global` | 仅超管            | 项目管理员 / 超管           | 仅超管        |

配置列表及单项 GET 要求 `SUPER_ADMIN` 或 `PROJECT_ADMIN`（`_MANAGERS`），API Key 还需 `datasets:read`。超管看全部，项目管理员看「全局 + 自己创建的」；角色降为普通人员后，即使仍是创建者也不可读配置。前端查询按当前角色启用，并保留会话／角色隔离。服务内部的 `can_use_connection` 仍表达业务使用范围，不作为读取配置的授权依据；导入入口本身要求管理角色。创建权限同样限管理角色，其中 `global` 仅超管可建。

## 密钥加密

密钥用 **Fernet**（对称加密，自带时间戳）加密：

- 加密：`encrypt_secret(secret: dict) -> bytes`，对 `json.dumps(sort_keys=True)` 后的明文加密，结果存入 `secret_enc`（`create`/`update` 时调用）。
- 解密：`decrypt_secret(token: bytes) -> dict`，仅在候选更新验证、`test` 与导入/引用读取中临时解密用于建立连接，解密失败抛 `ConnectorCryptoError`。
- 密钥来源：环境变量 `CONNECTOR_ENCRYPTION_KEY`（与 JWT `secret_key` 隔离）。**未配置时**加解密路径直接拒绝，相关 API 返回 `503`。

生成 key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

部署细节见 [参考 · 连接器安全](/dev/reference/connector-security)。

## SSRF 防护与主机白名单

连接器目标地址用户可控，因此每次**创建 / 更新 / 测试 / 导入**前都过 `connector_guard.assert_connection_target_allowed()` 门禁，杜绝 SSRF（如让平台去访问 `169.254.169.254` 云元数据）。

校验逻辑（`assert_host_allowed`）：

1. **空白名单 = 全拒**。未配置白名单时任何目标都拒绝，提示联系超管先配置。
2. **HARD_BLOCK 优先**：目标解析出的 IP 若命中 loopback / link-local / multicast / unspecified / reserved，**无条件拒绝**，即使白名单覆盖也不放行。
3. **白名单匹配**：DNS 解析目标 host 得到 IP 列表，逐个校验——落在 CIDR 条目内放行；或域名条目（精确 / 前导点后缀）匹配且 IP 为公网放行；否则拒绝。

白名单条目支持四种形态：CIDR（`10.0.3.0/24`）、单 IP（`192.168.1.50`）、精确域名（`oss-cn-hangzhou.aliyuncs.com`）、后缀域名（`.aliyuncs.com` 匹配任意子域）。

白名单存储在系统设置 `system_settings.connector_host_allowlist`：部署默认值来自环境变量 `CONNECTOR_HOST_ALLOWLIST`，超管通过系统设置或 `PUT /storage-connections/allowlist` 写入数据库后覆盖 env，通过 DELETE 删除覆盖并恢复部署默认值。保存时统一规范化 IP、CIDR 和域名；显式空数组是 deny-all 的数据库覆盖。

部署方可通过 `CONNECTOR_DEPLOYMENT_SFTP_HOST` 向超级管理员提供一个非敏感的 SFTP 地址快捷预设。它只复用现有连接器创建流程，不自动探测宿主机、不加入白名单，也不生成凭据。API 与导入 worker 必须能访问同一地址，并共享部署侧维护的只读 `known_hosts`。

## 导入链路

数据集从连接器导入由 `POST /datasets/{id}/import-from-connection` 触发，返回 `202` 并投递 Celery 任务 `app.workers.dataset_import.run_dataset_import`，进度走 [AsyncJob](/api/guides/async-jobs)。

```text
import-from-connection (202)
  └─ AsyncJob(payload: connection_id, source_path, recursive, include_globs, …)
       └─ run_dataset_import  (Celery)
            ├─ build_adapter()           # 解密 secret + 白名单门禁 + path 沙箱
            ├─ adapter.list(source_path, recursive, include_globs)
            ├─ 采样上限校验             # max_files / max_total_bytes，超限直接失败
            └─ 逐文件 svc.ingest_one(...) # 支持中途取消
```

请求参数：

- `source_path`：导入起始路径，相对于连接器的 `base_prefix`（S3）/ `base_path`（SFTP）。
- `recursive`：是否递归子目录。
- `include_globs`：glob 过滤（如 `["*.jpg", "*.png"]`），在应用层用 `fnmatch` 过滤。

两道安全护栏避免「误扫全桶」：`DATASET_IMPORT_MAX_FILES`（默认 50,000）与 `DATASET_IMPORT_MAX_TOTAL_BYTES`（默认 200 GiB），超限任务直接失败。SFTP / S3 适配器均对 path 做沙箱化，不允许 `..` 逃逸出 base 路径。

## 审计

所有写操作与测试都记审计（见 [审计与通知](/dev/concepts/audit-and-notifications)）：`storage_connection.create / update / delete / test`、`connector.allowlist_update`。`update` 的 detail 含 `secret_rotated` 和新 `revision` 标记是否轮换了密钥及编辑代次；审计 detail 不含任何明文密钥。

## 受控目录与动态注册

`STORAGE_FILESYSTEM_ROOTS` 将稳定句柄映射到部署者允许的绝对根，默认 `{}`。StorageLocation 仅保存句柄、相对基目录、授权、修订和内部节点指纹；公开 API 不返回根路径或指纹。统一挂载后，新建子目录和数据库注册都无需重启；全新宿主机根必须先由运维挂载，修改 Compose volumes 通常需重建受影响容器，无需重建镜像。

`services/filesystem_storage.py` 使用 Linux `openat2`，要求 Linux 5.6+、x86_64/aarch64；不支持时拒绝访问，无普通 `open` fallback。打开根时拒绝路径中的链接；根内使用 `BENEATH / NO_SYMLINKS / NO_XDEV`，拒绝链接及跨子挂载。根与位置基目录按节点固定路径摘要、dev/inode；节点内替换拒绝静默重新绑定。读取样本先验证常规文件，目录预算包含被过滤条目，FD 均按调用释放。支持有效 UTF-8 名称。

API/worker 各自加载根映射；Celery `worker_direct` 加实际 hostname 校验提供指定节点探测。首次节点探测信任部署映射，后续保留首次取得的有效根身份；失败、超时、配置变化与晚到结果不能重新绑定。数据库按 `(location_id, node_id)` 保留最新观察，证据有效 5 分钟。API 本地等待 3 秒，远端等待 5 秒；超时线程结果不会自行落库，阻塞文件系统调用仍依赖内核恢复。跨节点能力证据不能证明字节相同，后续原地读取仍需内容核验。

位置登记/修改仅超管；项目管理员按 `global` 或 `use_user_ids` 使用和探测，员工不得枚举目录。每页重新检查当前授权，游标绑定位置、修订、节点、路径摘要和目录快照。现有连接通过 `connection_id` 关联位置；凭据仍由 `StorageConnection`/Fernet 持有，未复制。连接编辑使用独立 `revision`（迁移 0210，旧行初始为 1），按 `connection → location` 顺序加锁。候选认证与有界列举在行锁外进行，不修改 ORM 原配置；验证匹配的 SFTP 位置时复用 `SftpAccess` 的固定公钥与地址约束，普通复制连接继续使用系统 `known_hosts`。验证成功后加锁、重读当前修订、活跃账号/管理权限、原 API Key、最新白名单和引用，再原子发布。有效名称编辑仅推进连接 `revision`，实际配置变化或显式凭据轮换才推进位置 `config_revision`；旧探测因此失效。有内容/用途引用时拒绝实际配置改向或删除。删除无引用连接级联清理关联与探测，既有项目级外键行为保持一致。

九用途绑定查询只读，缺失不自动 bootstrap，共桶不合并用途。头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件提供经预览确认的用途切换入口；`DatasetSource` 提供只读观测扫描及显式登记。完整接口见[受控存储位置](/api/guides/storage-locations)，决策见[ADR 0092](/dev/adr/archive/0092-controlled-storage-registry)。

## 九用途绑定与保存目标隔离

内部 `storage_locations` / `storage_bindings` 记录默认平台位置和九用途的桶，不复制连接器密码。封闭的 `managed_s3_target` 只接受以下九个用途，不承担通用存储注册职责：

| 用途标识（`purpose`） | 默认桶配置                   | 新分配切换契约                   | 保存目标（`storage_ref`）隔离语义                                                                               |
| --------------------- | ---------------------------- | -------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `avatars`             | `MINIO_AVATARS_BUCKET`       | `reviewed_avatar_switch`         | `AvatarUpload` 保存独立 `storage_ref`（`_aap_managed/avatars/`）与一次性 `VersionId` 回执                       |
| `bug_reports`         | `MINIO_BUG_REPORTS_BUCKET`   | `reviewed_bug_attachment_switch` | `BugAttachmentUpload` 保存独立 `storage_ref`（`_aap_managed/bug-reports/`）与 180 天单上传期限                  |
| `audit_archive`       | `MINIO_AUDIT_ARCHIVE_BUCKET` | `reviewed_audit_archive_switch`  | `AuditArchiveOutput` 在候选提交前分配固定目标（`_aap_managed/audit-archive/`）                                  |
| `export`              | `MINIO_EXPORT_BUCKET`        | `reviewed_export_switch`         | `ExportOutput` 与 `ExportMediaBundle` 共享保存目标（`_aap_managed/export/`）与首次 7 天期限                     |
| `import`              | `MINIO_IMPORT_BUCKET`        | `reviewed_import_switch`         | `PredictionInputLease`、`ManagedOriginalCreation.staging_ref`、`MaskFormatUpload` 独立保存暂存位置              |
| `datasets`            | `MINIO_DATASETS_BUCKET`      | `reviewed_dataset_switch`        | `ManagedOriginalCreation` 唯一写入，`DatasetAssetLocation` / `DatasetAuxiliaryOriginal` 保存当前副本引用        |
| `media_cache`         | `MINIO_MEDIA_CACHE_BUCKET`   | `reviewed_media_cache_switch`    | `OriginalCacheEntry`、`ThumbnailOutput`、`ImagePyramidOutput`、`VideoDerivativeOutput` 保存引用并走同源能力 URL |
| `annotations`         | `MINIO_BUCKET`               | `reviewed_annotations_switch`    | `TaskOriginal`、`RasterMaskObject`、`CommentAttachmentUpload`、`GuideAssetUpload` 保存封闭十字段引用            |
| `training`            | `MINIO_TRAINING_BUCKET`      | `reviewed_training_switch`       | `TrainingDatasetVersion`、实验、预览、样本评估、主动学习五类 owner 保存独立前缀引用                             |

核心隔离规则：

1. **分配时固定目标**：新对象分配时按 `Connection → Location → Binding` 锁序捕获当前目标并写入业务 owner 的 `storage_ref`（`schema2` 包含 `location_id`、`config_revision`、`binding_generation`、`endpoint_url`、`bucket`、完整物理键/前缀与 `storage_identity`），事务提交后才打开对象存储客户端。
2. **凭据动态读取、地址不可变**：后续读取、校验与删除始终沿业务记录保存的 `storage_ref` 定位，凭据从该位置关联的当前连接实时获取；常规密钥轮换不要求历史 `config_revision` 等于当前位置修订，但端点、桶或受控前缀变化会在 I/O 前拒绝。
3. **用途切换不搬迁历史**：真实托管探针（条件 PUT、非空分段上传、完整读回、列举、删除与收尾校验）在 SQL 锁外执行，预览签发 5 分钟确认令牌；切换仅推进 `binding_generation` 并作用于新分配对象，已有及在途对象保持原地址。各消费方固定归属细节见 [原件验证读取、缓存与保留回收架构](./original-access-and-retention#消费方固定存储归属（savedtarget）)。

## 位置维护的存储关系盘点

`storage_maintenance_impact` 在查询前后检查当前超级管理员、原 Key 身份及 `location_revision`。单条 SQL 以位置全部保留来源为范围，通过采纳历史/引用/Asset 位置关系合并去重业务成员，再连接数据集、项目绑定、Task 和标注；不遍历来源页、不执行外部 I/O。活动扫描/登记读取持久 run，不依赖短期 AsyncJob。完整缓存按 `scope_location_id` 计入独立孤儿账本，读租约使用同一数据库时间。

原来源范围中的临时输入只有当前 Task 关联可归因，冻结来源只有已保存 `source_lineage.location_id` 可归因；缺少证据的历史保持未覆盖。计数是已记录关系，副本记录不是可读性证明；响应明确 `execution_permission=false`，不能被维护写入当作完整无引用或安全停用证明。

保存目标由 `storage_maintenance_targets` 独立聚合现有 27 家族，保留来源计数原义。按实际保存的 JSON locator 或物理 `location_id` 统计九用途全部状态责任，不借助当前 Binding 定位历史。Creation 最终原件与存在的暂存是两个 role，`DatasetAssetLocation` 只计 `managed_original`；无物理地址的失败 Preview 不虚构目标。`OriginalCacheEntry.location_id` 是目标，`scope_location_id` 仍是来源配额范围；NULL 引用但有明确 scalar 位置的旧记录不算未定位。责任可重叠，不能解释成对象数或字节数。

业务关联只使用直接保存 ID 和可证明的当前关系并去重：金字塔经 `ImagePyramidAsset`，训练 Run／Preview／Evaluation 的 `dataset_id` 指训练版本，Thumbnail 按 `owner_kind` 关联 Source／Item／Task，指南共享项目使用 `ProjectGuideAsset`。孤儿责任保留，缺少项目字段的 RLE 不能从键推断项目。来源 A／输出 B 分别进入来源血缘及 B 的保存目标，不互相替代。当前 Binding 计数独立；全局 `unlocated_targets` 仅说明未归因范围，不按今日用途补猜，也不进入选中位置的确认摘要。

已知默认托管位置可以 GET 只读影响，配置改址和 drain／stop 仍沿原限制；其他无连接且无法识别的平台位置不自动获得该入口。

当前位置 PATCH 锁序为 `User/Key 共享锁 → Location 排他锁`，`expected_revision` 防止配置覆盖；每次 PATCH 递增修订。扫描/登记在后续检查点拒绝旧快照，周期计划在后续唤醒进入 blocked；预测/修复在自身的授权/内容屏障复核，并无位置级同步取消协议。完整缓存严格匹配位置修订，改名、人员和额度/TTL 等变更也可能令旧缓存失配；预览按当前策略、内容和规格复用。来源管理授权与 Task 项目读取权限分开；停用位置阻止新的逻辑引用读图，包括缓存。来源变化不改写独立冻结媒体的归属，其读取仍复核项目权限与自身保存目标；不应把来源关联版本数当作必失效数。

`storage_location_change` 复用无副作用候选校验和维护聚合：POST 预览签发五分钟 HS256 令牌，绑定当前 actor/Key 证明、位置配置及连接修订、标准化候选与关系指纹。聚合在同一 SQL 内计算计数及各表身份/相关状态的有序 SHA256 摘要，不以计数代替关系身份。保存目标指纹包含 `family/role`、稳定主键、locator、可证明业务关联和 `state/status`，当前 Binding 包含 `purpose/location/bucket/generation`；同数量替换或关联变化仍需重新预览。不包含心跳／最近访问／重试时间或标注内容编辑，全局未定位覆盖摘要不参与选中位置的确认计数。PATCH 必须提供令牌，并在配置锁下重算快照，完成期限及绑定比对后才更新配置；失败不提交业务变化。位置访问维护确认复用同一统计和摘要。成功业务审计脱敏记录差异、修订与两次盘点时间，失败状态由独立审计中间件保留。

位置锁并不覆盖所有 Task/标注/冻结写入，不能在持有 Location 后阻塞等待这些 owner 锁；连接参与时遵循 `User → Key → Connection → Location`。检查只代表提交时的 SQL 快照，配置 CAS 与 owner 各自的检查点共同约束逻辑更新。全异步 owner 静止、归档/恢复和物理回收仍须各自实现，令牌不提供这些执行许可。

## 位置访问维护协议

迁移 0215 新增独立维护 Gate 和未收尾访问账本。Gate 使用逻辑位置 UUID，但故意没有业务 Location 外键：业务记录删除后仍保留未完成责任，独立登记事务也不等待调用方尚未提交的 Location 行。Receipt 外键指向 Gate，禁止删除仍有责任的 Gate。初始为 `active/1`，读取不存在的 Gate 返回虚拟初始状态；实际准入与锁检查才独立补行。

每次源访问以独立短事务取得 Gate SHARE、校验 `active` 和捕获修订并提交 receipt，再执行有界 I/O。最终业务发布在原 owner 事务持 Gate SHARE NOWAIT，重新检查捕获修订，不替调用方提交。结束责任只在真实线程、快照或流句柄结束后独立删除；取消或超时不会放弃后台线程，必要时返回时间晚于逻辑超时。进程退出或结束落库失败留下未收尾记录，无到期清除和强制清零。缓存命中仍取得当前准入，缓存填充发布及预测、修复、导出、质量与新冻结证明携带修订；历史缺字段按 1 归一化。

控制操作锁序为 `User → Key → Location NOWAIT → Gate NOWAIT`，与准入共享锁和最终发布串行化。开始维护拒绝后续新准入，但不会中止已登记 I/O；确认停止需要预览和提交两次零记录。每次 drain/stop/resume 均递增修订，既有 Task 的新请求取当前修订，不把历史扫描修订永久绑定到 Task。已保存独立冻结副本继续可读，只有回源路径受门禁；已交付远端的临时副本不能撤回，结果发布仍检查修订。

升级先应用 0215，再刷新该部署命名空间全部 API、Worker 和实际队列消费者，核实旧进程已经退出及新任务签名和准入生效。新代码始终登记访问，`STORAGE_MAINTENANCE_PROTOCOL_ENABLED=false` 仅禁用控制转换，不能跳过协议。确认消费者完整升级后在相同命名空间开启该选项；不能将初始空账本当作没有旧访问的证据。代码回退保留 Gate/receipt；存在未收尾记录或非初始维护事实时 schema downgrade 拒绝丢弃。

此协议表达一个逻辑 Location 的平台受控访问，不是整块挂载、服务器、桶或所有物理读者的静止证明。多个 Location 可共享根；候选/内联连接验证、直接外部客户端、远端已复制输入及平台自有清理不在源静止保证内。当前片不交付退役、卸载或物理 GC；这些能力仍须独立核对身份、保留、引用及实际物理访问范围。

---

## 跨域架构锚点与延伸索引

以下小节已按领域拆分至对应的专题架构页面，此处保留锚点与核心结论摘要以兼容外部引用：

## 内容身份与兼容回填

每个 `DatasetItem` 关联不可变的 `DatasetAsset`（SHA-256 + 字节大小）与 `DatasetAssetLocation`；新上传写入 `_objects/<upload UUID>/` 保留区，旧数据通过 `scripts/backfill_dataset_assets.py` 只读校验或分批回填映射。完整架构与命令参数见 [数据集、目录来源与增量同步架构 · 内容身份与兼容回填](./dataset-sources-and-sync#内容身份与兼容回填)。

### 显式补算旧托管原件

`scripts/verify_legacy_dataset_assets.py` 在定位回填后通过 `--preview` 与 `--apply-plan` 对旧 `unverified` 托管原件进行完整条件读取并建立当前 SHA-256 基线。详见 [数据集、目录来源与增量同步架构 · 显式补算旧托管原件](./dataset-sources-and-sync#显式补算旧托管原件)。

### 同摘要观测重新核验

完整预检中的 `observation_changed` 可显式接纳为同 `Item/Asset` 的新 `DatasetSourceRevision`，继承 `owns_item` 且不改变业务身份或重传托管副本。详见 [数据集、目录来源与增量同步架构 · 同摘要观测重新核验](./dataset-sources-and-sync#同摘要观测重新核验)。

### 显式定位迁移

同来源完整预检中的 `not_seen → added` 同 SHA 配对经人工确认后建立跨路径 `DatasetSourceRevision`，通过 `acceptance_order` 序列追踪最新接纳路径，保持原 `DatasetReference` 稳定。详见 [数据集、目录来源与增量同步架构 · 显式定位迁移](./dataset-sources-and-sync#显式定位迁移)。

## 位置内的来源诊断

`storage_source_summary`、`storage_source_diagnostics` 与 `storage_source_usage` 在数据库快照内按位置聚合可见来源的六态可用性、预览记录、扫描/预检轮次与去重索引规模，全程零物理源 I/O。详见 [数据集、目录来源与增量同步架构 · 位置来源诊断与已采纳源索引规模](./dataset-sources-and-sync#位置来源诊断与已采纳源索引规模)。

## 位置维护影响

位置详情的维护影响汇总来源关系、27 家族保存目标与当前九用途绑定，并在配置变更或访问维护切换前签发绑定关系指纹的 5 分钟确认令牌。详见上文 [位置维护的存储关系盘点](#位置维护的存储关系盘点)。

## 托管原件移除后的保留证据

迁移 0216 的 `managed_original_retirements` 在数据集或样本删除同一事务内捕获已登记托管原件的历史定位与内容证据，默认 `retain_until=null` 无限期保护，只读预演固定返回 `execution_permission=false`。详见 [原件验证读取、缓存与保留回收架构 · 托管原件移除后的保留证据](./original-access-and-retention#托管原件移除后的保留证据)。

### 显式策略与声明保留要求

迁移 0217 支持按同 `bucket/object_key` 组保守归并保留期限（`retain_until`）与声明保留要求（`managed_original_retention_holds`），经 5 分钟预览令牌确认后同事务写入。详见 [原件验证读取、缓存与保留回收架构 · 显式策略与声明保留要求](./original-access-and-retention#显式策略与声明保留要求)。

### 已登记原件的完整内容观测

迁移 0218 的核验任务以独立持久访问许可流式读取退役原件（上限 5 GiB、60 秒预算），按 `VersionId/IfMatch` 完整比对历史 SHA-256 与大小，不修改退休断言。详见 [原件验证读取、缓存与保留回收架构 · 已登记原件的完整内容观测](./original-access-and-retention#已登记原件的完整内容观测)。

### 准确版本回收与引用栅栏

迁移 0260 的 `ManagedOriginalReclaimAttempt` 与 `ManagedOriginalReclaimClaim` 在共享事务引用栅栏、位置停止排空与完整重查保护下，仅对保存准确非 `null` 创建版本的退役托管原件执行指定版本删除。详见 [原件验证读取、缓存与保留回收架构 · 准确版本回收与引用栅栏](./original-access-and-retention#准确版本回收与引用栅栏)。

### 原图与预览的窗口观测率

迁移 0261 的 `StorageObservationBucket` 按完成时 UTC 日期、位置、来源和指标独立累加实际严格回源与新建预览的成功/失败次数，供故障面板只读展示当日观测率。详见 [对象存储迁移、站点 Agent 与容量观测架构 · 原图与预览的窗口观测率](./storage-migration-and-ops#原图与预览的窗口观测率)。

### 已封存的原件读取映射

迁移 0263 的 `StorageMigrationReadPublication` 与不可变 Route 将已封存全量复制的逻辑源 A 映射至目标 B 的准确版本（`PhysicalReadPlan`），覆盖原件、训练产物、导出 ZIP、保留文件、导出媒体、媒体缓存 Range、Mask 暂存与脱敏训练日志读取。详见 [对象存储迁移、站点 Agent 与容量观测架构 · 已封存的原件读取映射](./storage-migration-and-ops#已封存的原件读取映射)。

### 原生源端写入封闭

运维命令 `scripts/storage_source_fence.py` 通过私有 Journal、固定拓扑拒写入口与源进程屏障封闭已签发 URL 和在途写入。详见 [对象存储迁移、站点 Agent 与容量观测架构 · 原生源端写入封闭](./storage-migration-and-ops#原生源端写入封闭)。

### 最终清单与复制封存

可信本地运维命令将原生 Journal 的耐久应用 claim 与数据库最终核验记录（迁移 0262）绑定，重新物化 baseline 与全部 delta 并核验完整源清单与逐对象决策。详见 [对象存储迁移、站点 Agent 与容量观测架构 · 最终清单与复制封存](./storage-migration-and-ops#最终清单与复制封存)。

### 完整原图缓存的资源归属

`services/original_cache.py` 与 `original_cache_entries`（迁移 0213）在来源和位置均启用 `on_demand` 且配额充足时管理按需原图副本、60 秒读租约与跨位置 GC 保护。详见 [原件验证读取、缓存与保留回收架构 · 完整原图缓存的资源归属](./original-access-and-retention#完整原图缓存的资源归属)。

### 图片冻结输入与交付

异步图片与视频导出通过 `ExportMediaBundle` 冻结输入快照、原件身份与固定 168 小时交付期限。详见 [原件验证读取、缓存与保留回收架构 · 图片冻结输入与交付](./original-access-and-retention#图片冻结输入与交付)。
