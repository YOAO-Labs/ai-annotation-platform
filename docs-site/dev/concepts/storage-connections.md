---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-22
---

# 存储连接器（Storage Connections）

存储连接器让平台以**受控、可审计**的方式接入外部对象存储（S3 / OSS / MinIO 兼容）与 SFTP，并把其中的文件导入数据集。本页解释它的设计：密钥加密、SSRF 白名单、作用域模型与导入链路。

面向使用者的操作指南见 [用户手册 · 存储连接器](/user-guide/datasets/storage-connections)；逐端点契约见 [API · 存储连接器](/api/guides/storage-connections)；密钥/白名单的部署配置见 [参考 · 连接器安全](/dev/reference/connector-security)。

## 代码入口

| 关注点          | 位置                                                                                               |
| --------------- | -------------------------------------------------------------------------------------------------- |
| 路由            | `apps/api/app/api/v1/storage_connections.py`（前缀 `/storage-connections`）                        |
| 模型            | `apps/api/app/db/models/storage_connection.py`（表 `storage_connections`）                         |
| Schema          | `apps/api/app/schemas/storage_connection.py`                                                       |
| 业务服务        | `apps/api/app/services/storage_connection.py`                                                      |
| 密钥加解密      | `apps/api/app/core/crypto.py`                                                                      |
| SSRF 白名单门禁 | `apps/api/app/services/connector_guard.py`                                                         |
| 源适配器        | `apps/api/app/services/sources/`（`s3.py` / `sftp.py`）                                            |
| 导入任务        | `apps/api/app/workers/dataset_import.py`                                                           |
| 迁移            | `apps/api/alembic/versions/0086_storage_connections.py`、`0087_storage_connections_owner_scope.py` |

## 数据模型

`storage_connections` 表的核心字段：

| 字段         | 类型          | 说明                                                                                   |
| ------------ | ------------- | -------------------------------------------------------------------------------------- |
| `kind`       | `String(20)`  | `s3` 或 `sftp`（CHECK 约束）                                                           |
| `config`     | `JSONB`       | **非密钥**配置（endpoint/bucket/host…），写入前经 `_validate_and_sanitize_config` 过滤 |
| `secret_enc` | `LargeBinary` | Fernet 加密后的密钥密文，可空                                                          |
| `scope`      | `String(20)`  | `global` 或 `owner`（CHECK 约束，默认 `owner`）                                        |
| `created_by` | `UUID`        | 创建者                                                                                 |

关键不变量：**明文密钥永不落库、永不出 API**。`config` 与 `secret` 在 schema 层就分离——`config` 是可读的连接参数，`secret`（access/secret key、密码、私钥）只进 `secret_enc`，对外仅以 `secret_set: bool` 表达"是否已配密钥"。

> 历史字段 `project_id` 已弃用：当前作用域是「个人 / 全局」，新建连接器不再写入项目级归属。

### config / secret 形态

```text
S3   config: { endpoint, bucket, region?, base_prefix?, use_ssl? }
     secret: { access_key, secret_key }

SFTP config: { host, username, port?, base_path?, auth_type }
     secret: { password }  或  { private_key, passphrase? }
```

## 作用域模型

| scope    | 谁能建            | 谁能看            | 谁能改/删     |
| -------- | ----------------- | ----------------- | ------------- |
| `owner`  | 项目管理员 / 超管 | 创建者本人 + 超管 | 创建者 + 超管 |
| `global` | 仅超管            | 所有登录用户      | 仅超管        |

列表端点据此过滤：超管看全部，普通用户看「全局 + 自己创建的」。创建权限限 `SUPER_ADMIN` / `PROJECT_ADMIN`（`_MANAGERS`），其中 `global` 作用域仅超管可建。

## 密钥加密

密钥用 **Fernet**（对称加密，自带时间戳）加密：

- 加密：`encrypt_secret(secret: dict) -> bytes`，对 `json.dumps(sort_keys=True)` 后的明文加密，结果存入 `secret_enc`（`create`/`update` 时调用）。
- 解密：`decrypt_secret(token: bytes) -> dict`，仅在 `test` 与导入任务里临时解密用于建立连接，解密失败抛 `ConnectorCryptoError`。
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

所有写操作与测试都记审计（见 [审计与通知](/dev/concepts/audit-and-notifications)）：`storage_connection.create / update / delete / test`、`connector.allowlist_update`。`update` 的 detail 含 `secret_rotated` 标记是否轮换了密钥；审计 detail 不含任何明文密钥。

## 相关文档

- [用户手册 · 存储连接器](/user-guide/datasets/storage-connections)
- [API · 存储连接器](/api/guides/storage-connections)
- [参考 · 连接器安全](/dev/reference/connector-security)
- [参考 · 存储桶布局](/dev/reference/storage-buckets)
- [审计与通知](/dev/concepts/audit-and-notifications)

## 内容身份与兼容回填

外部连接器仍负责复制导入，StorageService 继续持有平台对象存储凭据并执行 I/O。内部 storage_locations/storage_bindings 记录默认平台位置和九用途的固定桶，不复制连接器密码，也没有变更用途绑定的公开入口。配置与固定记录不符时登记会失败；切换桶或 endpoint 前应独立完成历史对象定位、迁移与对账。

每个 DatasetItem 可反向关联一份 DatasetAsset 内容修订及 DatasetAssetLocation。新上传、ZIP 与连接器复制使用独立上传 UUID 的稳定托管键，新 Item 在 metadata.storage_relative_path 保存逻辑相对路径，供 Scene、点云帧分组和导出复用；扫描保留真实键并单独分配重名后缀。托管根下 `_objects/<upload UUID>/` 为平台写入保留区，手工导入目录应放在该保留区以外，普通 UUID 场景目录完整保留。已核验内容以 SHA-256 和大小固定，数据库拒绝修改；派生图片继续由 ImagePyramidAsset/generation 管理。

回填只根据实际 file_path 增加管理映射。历史 content_hash 保留为不可信 hint，不能根据长度当作 MD5/SHA-256，更不能视作 ETag。未读取的旧原件和上传占位仍为 unverified；成功报告表示映射完整，不能证明对象存在或历史内容一致。Item/Task 路径、Item.updated_at、项目关联与训练冻结 JSON 不变。

在已经选择正确数据库和存储配置的 API 环境内运行：

```bash
cd apps/api
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --dry-run
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --apply --batch-size 100
.venv/bin/python scripts/backfill_dataset_assets.py --dataset-id <UUID> --verify
```

全量目标用 `--all-datasets`。dry-run/verify 使用数据库 READ ONLY，并核对默认位置配置和全部九用途；缺失 bootstrap 也计入配置错误。apply 按最多 500 条的批次提交，JSONL 只含 ID/计数/错误码。中断后可原命令重跑；`--after <item UUID>` 从已报告游标后继续，最终须不带游标全量 verify，覆盖恢复期间新增的样本。

退出码：0 表示该模式检查无待处理错误；1 表示配置/定位不一致或 verify 尚有待映射项；2 表示运行异常，已提交批次保留。dry-run 的 pending 数允许非零；映射工具不执行字节校验。应用回退保留 Alembic 0194；仅尚无内容身份和用途绑定的未使用 schema 可 downgrade，已使用则明确拒绝。

设计决策见 [ADR 0091](/dev/adr/archive/0091-dataset-content-and-location-identity)，接口见[数据集上传与扫描](/api/guides/datasets)。

## 受控目录与动态注册

`STORAGE_FILESYSTEM_ROOTS` 将稳定句柄映射到部署者允许的绝对根，默认 `{}`。StorageLocation 仅保存句柄、相对基目录、授权、修订和内部节点指纹；公开 API 不返回根路径或指纹。统一挂载后，新建子目录和数据库注册都无需重启；全新宿主机根必须先由运维挂载，修改 Compose volumes 通常需重建受影响容器，无需重建镜像。

`services/filesystem_storage.py` 使用 Linux openat2，要求 Linux5.6+、x86_64/aarch64；不支持时拒绝访问，无普通 open fallback。打开根时拒绝路径中的链接；根内使用 BENEATH / NO_SYMLINKS / NO_XDEV，拒绝链接及跨子挂载。根与位置基目录按节点固定路径摘要、dev/inode；节点内替换拒绝静默重新绑定。读取样本先验证常规文件，目录预算包含被过滤条目，FD 均按调用释放。支持有效 UTF-8 名称。

API/worker 各自加载根映射；Celery `worker_direct` 加实际 hostname 校验提供指定节点探测。首次节点探测信任部署映射，后续保留首次取得的有效根身份；失败、超时、配置变化与晚到结果不能重新绑定。数据库按 `(location_id, node_id)` 保留最新观察，证据有效5分钟。API本地等待3秒，远端等待5秒；超时线程结果不会自行落库，阻塞文件系统调用仍依赖内核恢复。跨节点能力证据不能证明字节相同，后续原地读取仍需内容核验。

位置登记/修改/探测仅超管；项目管理员按 global 或 use_user_ids 使用，员工不得枚举目录。每页重新检查当前授权，游标绑定位置、修订、节点、路径摘要和目录快照。现有连接通过 connection_id 关联位置；凭据仍由 StorageConnection/Fernet 持有，未复制。连接修改按 connection→location 顺序加锁；有内容/用途引用时拒绝配置改向或删除。删除无引用连接级联清理关联与探测，既有项目级外键行为保持一致。

九用途绑定查询只读，缺失不自动 bootstrap，共桶不合并用途。当前没有用途切换或原地读取入口；DatasetSource 仅提供下文的只读观测扫描。完整接口见[受控存储位置](/api/guides/storage-locations)，决策见[ADR 0092](/dev/adr/archive/0092-controlled-storage-registry)。

## 持久目录扫描基座

DatasetSource 以 `inventory` 模式绑定已有数据集、filesystem 位置、相对目录、固定 worker hostname 和规则；这是观测合同，不是可标注样本。API 当前限超管，通用 Job 列表/详情也拒绝降权账号查看扫描任务。每批及 claim 重验操作者、来源/位置修订与启停，启动、恢复和 claim 要求当前节点 list 验证有效。

DatasetScanRun 独立持有快照、代次、60秒租约与随机 token；DatasetScanEntry 同时保存目录队列和文件观测。以 `(run_id, path_digest)` 唯一，路径本身用非索引 TEXT。每次完整目录捕获以一个事务分块写入，目录标记与观测一起提交。恢复和收尾复核目录摘要，stat 不提升内容 verified，也不判 missing。

统一短锁顺序为 Location→Source→Job→Run，与 Job retention 的 Job→Run 外键清理一致；文件系统 I/O 在事务提交后执行。来源行锁加活动运行部分唯一索引防并发启动，任务只 claim 一次；失租后显式 resume 创建新 Job/task_id。每批核对 task ID、来源代次、generation、token、有效期与授权；扫描 signal 在通用 fallback 前分派到同一 owner，旧信号不影响新尝试。

Job 外键 SET NULL，数据集删除只清空来源关联，长期观测账本保留；来源引用参与位置改向/删除保护。无来源数据时0196可降级，有数据则保留表并回退应用入口。接口与支持预算见[目录来源与观测扫描](/api/guides/dataset-sources)，决策见[ADR0093](/dev/adr/archive/0093-resumable-directory-inventory)。

## 图片内容核验与请求快照

目录观测的 stat 不证明内容一致。`content_snapshot.py` 为目录及固定托管 S3 共用有界 TemporaryFile，完整 SHA-256/图片验证后才交付同一快照。`dataset_content.py` 处理真实当前权限、来源/位置修订、绑定与响应前复检，I/O 在数据库锁外。ScanEntry.verified_content 与原始观测独立，旧摘要不改写；首次核验只证明此次捕获字节。API 使用自身 read 探测和 anchor，不把 worker 的 dev/inode 当跨节点内容身份。扫描 claim 固定实际内核主机证据；首次核验必须与 API 同主机，再比对来源目录及文件 identity，未知/跨主机先拒绝；已核验摘要可跨节点验证相同字节。回执在数据库触发器中禁止改写。

现有任务内容身份与严格读取接口要求当前任务可见性，不开放整个来源或数据集。Range/持久缓存、原地样本注册、工作台 Blob 生命周期及标注提交的内容上下文仍须独立接入。详见[接口合同](/api/guides/verified-content)与 [ADR0094](/dev/adr/archive/0094-verified-request-content-snapshots)。
