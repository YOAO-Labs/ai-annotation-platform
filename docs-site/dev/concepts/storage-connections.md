---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-24
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

### 独立 Task 原件

DatasetItem托管原件继续由DatasetAsset/AssetLocation和原创建记录承担。独立Task直传没有DatasetItem，使用TaskOriginal保存当前annotations位置、完整SHA/大小和源写入回执；Task.original_id引用它，原件记录不FK业务Task/Project/User。历史点云即使没有直接dataset_item_id，也可能通过primary_lidar关联DatasetItem，不能仅凭NULL直接认定独立原件。

ManagedOriginalCreation显式区分dataset/task作用域。Task分配保存project_id/task_id、独立随机最终键和import暂存键；沿既有条件multipart writer执行完整回读、真实关闭和取消收尾。Creation的历史桶、版本及回执不可改，当前位置通过TaskOriginal表达。两个业务发布检查之间释放Task/凭证锁，末端短锁重核同一上下文才关联原件；失败或业务删除保留独立记录，不补偿删除。

离线恢复只映射TaskOriginal当前bucket/storage_identity，保留源Creation和原ETag/VersionId事实。源写回执不是恢复目标的版本选择器；后续读取必须观察目标当前对象、条件读取并完整核对SHA。未固定的历史Task不自动认领。原件全调用者接入和回收仍有独立门禁，上传封存完成不开放完整用途切换。

严格图片内容入口与数据集质量检查使用 `original_sources` 冻结当前关系及保存位置。Task直接Item与primary_lidar必须一致；Dataset选择唯一present托管原件，不把缓存纳入候选，也不要求历史核验记录已有Creation。物理读取前、取得耐久读取许可后及响应前，分别重核当前权限、原凭证、关系、位置和维护修订；业务行锁不跨物理读取。原地引用的来源读取与完整缓存命中同样在许可取得后复核原凭证，响应前比较完整引用context并锁定同一Gate代次；撤权不把合格缓存误记损坏，也不会回退读源。

`managed_original_reader` 只接受端点、保存桶/键、完整SHA和大小，以及调用者显式预算。它建立独立客户端，从目标当前HEAD取得条件，验证错误If-Match确实失败，完整读取并核对摘要、EOF及最终HEAD，再真实关闭Body和客户端。历史写入VersionId不参与目标GET。返回的BinaryStage拥有临时文件和对应准入名额，取消和超时等待实际I/O退出；关闭无法确认时保留未收尾许可。图片适配器在同一文件上解码并移交给响应，不重新下载或复制。通用二进制消费者使用下文独立预算；原生视频及其余当前用途定位仍有各自门禁。

### 认证通用原件交付

浏览器公共读取模块 `apps/web/src/api/originals.ts` 只构造Task/Item/精确camera link的认证请求，核对身份中的完整下载路径，不给任意URL附加Bearer。原件下载共用两路可取消排队，覆盖完整响应体，前景可提升预取；只有明确容量不足才有限重试。资源owner按账号/session划分，同session令牌刷新后在途旧凭据结果最多重试一次，不能覆盖新凭据；跨账号、session及不完整的跨标签凭据停止读取。该公共层不建立原件缓存，各显示消费者仍须管理自己的内容版本、地址与释放。

`original_binary` 按 Task、精确相机 link 或 DatasetItem 冻结来源；`originals` 路由提供零物理 I/O 的身份及绑定 content UUID/SHA 的完整下载。相机只借当前 Task 可见性读取其明确 camera\_ 链接；不借此开放任意 Item。Item 入口仍要求 Dataset 管理权限，并校验路径中的 dataset_id。来源、原请求凭证和 Gate 在准入后与交付前复核；引用图片继续使用既有严格图片入口。

`original_binary_response` 复用严格 reader，单文件512MiB、独立两槽，槽覆盖复制与响应到真实文件关闭，声明大小的最大同时暂存为1GiB。匿名临时文件不产生命名缓存；响应前断连、取消、超时及发送失败等待在途线程收尾。复制和响应各120秒为合作式期限，真实收尾可能更长。octet-stream attachment 不执行图片解码，也不支持 Range/HEAD 或条件304；大文件明确413，不隐式回退签名地址。既有图片四槽独立保留，原生视频 Range 仍使用独立链路。

非视频 DTO 与点云 manifest 只提供延迟身份路径，未知历史不导致整页列取失败。显示消费者按账号/session、资源及内容 UUID/SHA 管理 Blob、Bitmap、RGBA 与 GPU 资源；取消一个共享消费者不终止仍有前景消费者的读取。图片预取最多保留两份、原始字节共64MiB，前景读取仍受512MiB单文件与既有解码预算约束。普通切帧保留就绪几何至原子替换，owner退休则销毁旧资源并取消待写回视角。SVG 使用不透明的 data URL，普通位图使用可撤销 Blob URL；格式探测只决定显示 MIME，不转换原件。必需金字塔预览不触发完整原件下载。缓存不提供当前在线或持续授权的证明，原生视频与完整用途切换仍独立推进。

### 存储连接器

`storage_connections` 表的核心字段：

| 字段         | 类型          | 说明                                                                                   |
| ------------ | ------------- | -------------------------------------------------------------------------------------- |
| `kind`       | `String(20)`  | `s3` 或 `sftp`（CHECK 约束）                                                           |
| `config`     | `JSONB`       | **非密钥**配置（endpoint/bucket/host…），写入前经 `_validate_and_sanitize_config` 过滤 |
| `secret_enc` | `LargeBinary` | Fernet 加密后的密钥密文，可空                                                          |
| `scope`      | `String(20)`  | `global` 或 `owner`（CHECK 约束，默认 `owner`）                                        |
| `revision`   | `Integer`     | 独立编辑修订，PATCH 使用 expected_revision 做条件保存                                  |
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

位置登记/修改仅超管；项目管理员按 global 或 use_user_ids 使用和探测，员工不得枚举目录。每页重新检查当前授权，游标绑定位置、修订、节点、路径摘要和目录快照。现有连接通过 connection_id 关联位置；凭据仍由 StorageConnection/Fernet 持有，未复制。连接编辑使用独立 revision（迁移0210，旧行初始为1），按 connection→location 顺序加锁。候选认证与有界列举在行锁外进行，不修改 ORM 原配置；验证匹配的 SFTP 位置时复用 SftpAccess 的固定公钥与地址约束，普通复制连接继续使用系统 known_hosts。验证成功后加锁、重读当前修订、活跃账号/管理权限、原 API Key、最新白名单和引用，再原子发布。有效名称编辑仅推进连接 revision，实际配置变化或显式凭据轮换才推进位置 config_revision；旧探测因此失效。有内容/用途引用时拒绝实际配置改向或删除。删除无引用连接级联清理关联与探测，既有项目级外键行为保持一致。

九用途绑定查询只读，缺失不自动 bootstrap，共桶不合并用途。当前没有用途切换入口；DatasetSource 提供下文的只读观测扫描及显式登记。完整接口见[受控存储位置](/api/guides/storage-locations)，决策见[ADR 0092](/dev/adr/archive/0092-controlled-storage-registry)。

## 持久目录扫描基座

DatasetSource 以 `inventory` 模式绑定已有数据集、filesystem 位置、相对目录、固定 worker hostname 和规则；这是观测合同，不是可标注样本。API 当前限超管，通用 Job 列表/详情也拒绝降权账号查看扫描任务。每批及 claim 重验操作者、来源/位置修订与启停，启动、恢复和 claim 要求当前节点 list 验证有效。

DatasetScanRun 独立持有快照、代次、60秒租约与随机 token；DatasetScanEntry 同时保存目录队列和文件观测。以 `(run_id, path_digest)` 唯一，路径本身用非索引 TEXT。每次完整目录捕获以一个事务分块写入，目录标记与观测一起提交。恢复和收尾复核目录摘要，stat 不提升内容 verified，也不判 missing。

统一短锁顺序为 Location→Source→Job→Run，与 Job retention 的 Job→Run 外键清理一致；文件系统 I/O 在事务提交后执行。来源行锁加活动运行部分唯一索引防并发启动，任务只 claim 一次；失租后显式 resume 创建新 Job/task_id。每批核对 task ID、来源代次、generation、token、有效期与授权；扫描 signal 在通用 fallback 前分派到同一 owner，旧信号不影响新尝试。

Job 外键 SET NULL，数据集删除只清空来源关联，长期观测账本保留；来源引用参与位置改向/删除保护。无来源数据时0196可降级，有数据则保留表并回退应用入口。接口与支持预算见[目录来源与观测扫描](/api/guides/dataset-sources)，决策见[ADR0093](/dev/adr/archive/0093-resumable-directory-inventory)。

## 图片内容核验与请求快照

目录观测的 stat 不证明内容一致。`content_snapshot.py` 为目录及固定托管 S3 共用有界 TemporaryFile，完整 SHA-256/图片验证后才交付同一快照。`dataset_content.py` 处理真实当前权限、来源/位置修订、绑定与响应前复检，I/O 在数据库锁外。ScanEntry.verified_content 与原始观测独立，旧摘要不改写；首次核验只证明此次捕获字节。API 使用自身 read 探测和 anchor，不把 worker 的 dev/inode 当跨节点内容身份。扫描 claim 固定实际内核主机证据；首次核验必须与 API 同主机，再比对来源目录及文件 identity，未知/跨主机先拒绝；已核验摘要可跨节点验证相同字节。回执在数据库触发器中禁止改写。

现有任务内容身份与严格读取接口要求当前任务可见性，不开放整个来源或数据集。Range 和持久原图缓存的产品读取入口尚未提供；原地样本注册、工作台 Blob 生命周期及标注提交保护见下文。详见[接口合同](/api/guides/verified-content)与 [ADR0094](/dev/adr/archive/0094-verified-request-content-snapshots)。

## 可标注的目录引用

完整观测中的图片可通过 register 逐条登记。DatasetReference 固定 Item→Source/Run/Entry，Asset 固定已核验 SHA-256；同 Source/path 幂等，不创建 S3 原件副本。Item 与 Task 保存专用不透明引用标识，通用 S3 resolver 及未适配的后台消费者拒绝该标识。legacy 去重与 asset 回填排除引用项，不根据标识猜桶。

登记和项目关联共用 Dataset 锁；登记按扫描锁序取得来源后对 Dataset 使用 NOWAIT，繁忙返回409。Link 在锁后查重，各建档批次在锁内再次查已有 Task。普通标注写入与单任务生命周期在 Task 锁内完成有界原图核验并保持原事务，避免等待锁期间来源变化仍被保存；快照 helper 不 commit。独立扫描与 verify 仍锁外读取。最初 API Key 身份、当前授权和绑定在 I/O 后复查。

业务从来源读取时使用当前 API 节点已固定锚点与实际完整读取，不因探测 TTL 到期自动重绑目录；缺失锚点、来源定位改变、停用或摘要不符会失败。来源改名、停用后恢复保留内容身份。首次登记仍需有效 read 探测。工作台按登录/任务/请求代次持有鉴权 Blob URL，原图未就绪禁用编辑；退休请求不能污染新任务或新账号，URL 按 owner 回收。

应用回退保留0198及引用账本；空表才可降级。更多取舍见 [ADR0095](/dev/adr/0095-immutable-directory-reference-tasks)，管理员流程见[服务器目录原地引用](/user-guide/datasets/import-images#服务器目录原地引用)。完整原图缓存的当前接入边界见下文。

## 可恢复的批量登记

扫描完成后创建独立 DatasetRegistrationRun/Entry，先预检全部匹配图片再显式确认登记；扫描状态、来源代次及核验回执均不充当登记进度。预检账本通过 INSERT SELECT 建立，逐项读取并提交，避免将全目录内容放入单次事务或内存。统计把索引、待核验、已核验、可用、异常和历史路径分开。

登记 Run 固定账号、初始 API Key 证明、配置修订和节点；每次执行有独立 task ID、generation、token 和60秒租约。单片25项/90秒，未提交项可重放；旧 worker、重复消息及迟到失败信号不能改变新分片。派发先提交持久 pending，再发布消息；beat 每30秒恢复待派发或到期租约。完成的 Job 可按既有策略清理，Run/Entry 保留，不把短期任务历史当恢复账本。

预检的 SHA-256 回执、正式登记业务对象和逐项检查点分别在各自事务提交。最终业务登记与该项计数共用事务；取消保留已提交部分，过期执行者不能在核验后继续落库。暂态数据集锁/快照容量不足退避重派；单文件错误隔离并允许显式重试，已核验摘要不可改写。确认固定预检尝试身份，I/O 期间出现新版预检时拒绝旧确认。

固定节点以锚点和逐项真实安全读取证明可读，探测 TTL 不终止已领取的长任务。API 确认同样重新读取固定摘要，不以扫描前几项损坏文件的探测结果否定后续正常候选。原图复制量与派生占用独立计量，目录支持 reference/managed_copy 与 none/preview/on_demand 的独立策略，默认 reference/preview；旧来源迁移为 reference/none。定位 config 与策略分列，Source 路径映射固定 Item/Asset/SHA；仅策略变化也要求新扫描和预检，旧引用内容身份不变。预览由独立记录固定父 Asset/SHA、规格、自身 SHA/字节和对象位置，走鉴权端点，不写 legacy thumbnail_path 或发签名 URL。来源停用/注销取消活动 lease，保留业务身份；托管复制原件已独立，继续可用。迁移0199增加可恢复账本；有历史记录时保留表，应用回退停止新尝试。取舍见 [ADR0096](/dev/adr/0096-durable-directory-registration)。

## 目录管理的最小授权

Dataset.created_by 是目录数据集的最小管理边界：活跃超管管理全部，活跃项目管理员只管理自己的数据集，来源操作另叠加位置可见性。中央 dataset_source_access 服务为来源列表、total、Scan/Registration/Entry 直接 ID 访问及保留的 AsyncJob 尝试提供相同 SQL 关系。旧数据集写入口只对已含来源的数据集叠加创建者守卫，保留其他数据集行为。混合上传、ZIP、桶扫描、连接器复制导入和同步/异步分块建任务保留初始授权，耗时 I/O 后及分块提交前复查；撤权中断未提交部分，复制路径清理本项新对象，已提交部分保留。旧排队作业若后来接入目录而缺少该授权证明，则拒绝继续。作业公开 payload 隐去内部授权证明。

浏览、探测、内容核验、批量登记及扫描在 I/O 前后重读账号和具体来源权限；尝试固定初始 Key 证明，排队和执行期间的轮换、撤销、过期或 scope 收回阻止后续写入。位置启停与可见性分离，停用位置的历史原因仍可查询。位置授权用于管理，任务内容读取继续沿用分派/成员权限；撤回管理员使用权不撤销员工既有任务，停用来源/位置才会阻止其业务读图。

目录策略与生命周期的进一步取舍见 [ADR0097](/dev/adr/0097-directory-ownership-previews-and-lifecycle)。请求取消不会停止同步 S3/PIL 线程，因此目录写入等待线程结束后才关闭快照、删除本次唯一对象；已发布预览的退休删除在提交后执行。目录预览清理失败、进程崩溃或提交结果未知需按精确键人工核对，预览尚无持久自动回收器。

### 完整原图缓存的资源归属

位置详情按独立位置修订管理缓存，不与远端连接凭据混合。用量接口只读账本；清理请求按当前Actor → Key → Location → Entry锁序封存截止观测前的最多100代，锁后复查原Key和角色、跳过活跃租约，实际对象删除由周期owner执行。`original_cache_recorded`及登记/到期时间是元数据证据，不覆盖原来的来源状态；样本原图读取动作允许尝试合格旧副本，仍由统一reader实际核验。

`services/original_cache.py` 提供完整副本资源所有者，统一引用 reader 与位置/来源管理已接入。公开策略按 `none < preview < on_demand` 取来源请求和位置上限的较小值，按需原图包含预览；完整缓存要求来源和位置均选用按需策略、位置有显式正额度，默认额度为零。只有超管配置位置额度/TTL，来源管理不能扩大预算。`reference_context` 保存当前业务授权与逻辑内容身份，`reference_source_context` 只在实际读源或质量检查时获取来源节点、锚点和连接配置；`reference_snapshot` 在缓存/来源 I/O 前后复验身份与 Key，本资源服务不签发读取权限。

缓存命中只替代来源物理可达性，不绕过任务分派、成员、账号、来源/位置启用和接受修订；最终事务仍锁住 Connection/Location/Source/Item/Asset，不再锁住与逻辑身份无关的 Probe。缓存缺失或损坏时重新按来源物理合同读取。首次严格读源成功后，独立连接提交可选保留；不提交调用方事务，外层持有业务锁时允许有界跳过保留。额度满时最多淘汰八代未被读取的完整缓存，再尝试保留；单图超过位置总额度时不淘汰。调用方显式管理外层 Connection 事务时跳过独立保留，继续严格来源读取。质量检查保持 `capture_for_quality` 的来源语义和坏图诊断，不借用缓存掩盖来源变化。

`original_cache_entries` 独立保存来源位置、Asset/SHA、大小、策略修订、目的 binding/服务器指纹和 generation；没有指向业务对象的级联外键。上传前独立提交预约，位置级事务锁保证并发请求不超订；上传中、已就绪与待删除对象均占用原图额度。该额度与预览占用、整个对象桶和物理磁盘容量分别计量。同一 Asset 只允许一代正在填充或发布的缓存，旧代回收记录继续保留。

成功 PUT 后按块重新核验实际完整对象的大小和 SHA，再在短事务内复验策略、额度、修订与 binding，发布独立 `original_cache` AssetLocation。它不会覆盖 `managed_original` 或未知 locator。验证复用输入严格快照的图片资格，以相同 SHA 证明完整字节，不额外占用请求快照名额。读取缓存仍需捕获完整严格快照，不能用预览替换。

读租约固定六十秒，正常捕获结束即释放；请求取消但 SDK 线程未结束时保留租约至到期。淘汰在获得缓存行锁后复查租约。TTL 默认一天，从预约时固定，访问只更新最近使用时间；配额收缩、策略禁用、业务删除和过期均由 `cleanup` 队列每分钟分批核对。先移除精确旧代 locator，再删除自有 key，确认删除后才释放额度；删除失败继续计费并持久重试。已确认 PUT 的验证失败可立即回收；不确定 PUT 和失联写入保留七天重扫窗口。服务器指纹不符时保留记录并停止向新目标发送删除。窗口与有期限租约不承诺覆盖无限期停顿的远端 I/O。

对象位于 media-cache 的 `original-cache/v1/<Asset>/<SHA>/<generation>`，不受 `videos/` 生命周期影响。清理只处理这些资源所有者生成的精确键，不触及来源文件、托管原件或冻结训练副本。迁移 `0213` 在有缓存记录、原图缓存 locator 或按需策略时拒绝降级；应用回退须保留账本及恢复任务。

## 外部 S3 持续引用

严格引用使用 sources/s3_reference.py，旧 sources/s3.py 继续服务复制导入。reference_storage.py 解析现有连接器位置和加密凭据；S3Access 只在线程内解密、建立客户端，不进入 Celery 消息或持久快照。Source/ScanEntry/Registration/Reference 与 opaque Item/Task 身份复用已有模型，不新增平行资产表。S3 扫描在根 ScanEntry.observed.checkpoint 中逐页提交游标与结果，使用现有代次和租约；不套用 filesystem 的 inode 或整树二次目录摘要。

LIST 显式 url 编码后严格解码一次，ContinuationToken 原样保存；完整 key 不裁剪、不折叠。HEAD/GET 使用原始 ETag 和固定版本，检查完整响应及 SHA；每次读都会验证错误条件被拒绝。读写权限及连接修订在 I/O 前后复查。既有 Connection→Location 更新锁序保留，读方在 Location 锁内只以 MVCC 读取 Connection，不等待反向 Connection 锁。

SDK 内建超时、条件和版本参数负责协议访问；before-send 限定配置 endpoint，关闭代理避免中间层路径规范化。主机白名单不能消除 SDK 再次 DNS 解析的风险。

## SFTP 持续引用

`sources/sftp_reference.py` 复用来源、扫描、登记和严格内容服务；旧 `sources/sftp.py` 保留复制导入调用者。部署者用 `STORAGE_SFTP_ROOTS` 声明已核验的服务器只读 jail，精确绑定 host/port/username/完整主机公钥；连接 base_path 必须为 `/`。整个账号可读命名空间就是位置授权范围，来源子目录仅作过滤。普通协议无法证明 jail，这项声明必须来自实际部署核验。

严格客户端使用独立 HostKeys 与 RejectPolicy，在认证前检查固定公钥，不加载系统 known_hosts；连接到白名单解析后固定的 socket 地址，防止客户端再做一次 DNS 解析。凭据在线程内解密，连接/认证/通道均设短超时。目录逐项有界枚举，复用 BFS 租约与末次清单核对；没有虚构持久 SFTP 游标、inode 或版本身份。

lstat 链、句柄 stat、读取前后观测与完整 SHA 负责内容一致性；服务端 jail 才是根边界，这些客户端检查不能实现原子 nofollow。资格摘要形成扫描锚点，历史引用必须匹配原锚点。资格变更先禁用位置并提交修订、阻止旧 worker 发布，再协调 API 与 worker；只改一个进程的环境不是集群撤权。不同资格应新建位置，旧引用不自动重绑。取舍见 [ADR0099](/dev/adr/0099-qualified-sftp-references)，部署见[只读 SFTP 引用](/ops/deploy/docker-compose#只读-sftp-引用)。

## 持续来源的手动增量比较

持续引用与连接器一次性复制导入共享连接配置，但保留独立的来源、扫描、预检及登记账本。预检在来源锁内从已接纳路径映射固定基线，完整读取内容后生成六类差异；上轮仅观测的条目不成为接纳基线。比较结果只有完整预检完成后才发布，取消或中断不推断缺失。

确认仅新增会固定选择范围，相同项跳过，变化和未见保留历史绑定；恢复不能扩大已确认范围。正式登记仍重新严格读取，并在同一事务建立样本/项目任务与登记结果。分页差异与短期 AsyncJob 历史独立保留；旧引用始终使用原摘要及资格；普通新扫描不重绑观测，只有显式同内容重新核验可以追加读取凭据。详见 [API 合同](/api/guides/dataset-sources#手动增量差异) 和 [决策记录](/dev/adr/0100-preflight-owned-source-differences)。

## 周期来源协调

`DatasetSourceSyncSchedule` 每来源一行，保存间隔、CAS 修订、原始授权证明、来源/位置修订及根锚点，以及当前 Scan/Registration 指针。既有 beat 每60秒在 default 队列触发 tick，按 next_check_at/source_id 取最多100个到期来源；协调消息以90秒数据库令牌和修订固定，路由到精确 worker hostname。重复消息不能重复建运行，提交后派发丢失可由持久指针补发。

协调器按 Location → Source → Schedule 锁序检查权限、冲突与阶段，在 I/O 前释放事务；指定 worker 真实探测后重新取得锁、核对令牌和原授权。嵌套领域入口捕获的 actor/Key/proof 必须与计划一致，不把轮换后的凭据当成原授权。扫描和预检沿用自己的检查点、租约和分片；人工登记只观察终态，恢复由原登记 owner 处理，不由计划更换执行人。

默认策略下，完整差异除全部相同外进入待核对门禁；显式纯新增策略的条件见下文。人工登记完成或显式跳过后按当前间隔安排下一轮；暂态失败按间隔退避，权限/配置/资格变化停用并阻断。暂停计划不撤销已创建业务任务，取消任务不被自动接续。历史作业可清理，领域账本和周期指针保留。迁移0202在已有计划时拒绝破坏性降级；应用回退需暂停计划，保留账本。

## 来源内容修订与路径头

DatasetSourceItem 是来源/路径的当前头；DatasetSourceRevision 保存不可变接纳历史，包含永久 Item/Asset ID、活动 FK、前驱、观测、SHA、确认预检及操作者。Change.before_revision_id 固定比较前驱，防止 SHA 往返造成 ABA；数据库保护修订事实与头绑定一致性。迁移回填旧头为修订1，不补造历史接纳身份或时间。

单项新修订仅用于完整预检中的实际内容变化。沿用严格读取、Source 序列化与 Dataset NOWAIT，在一个事务创建新样本及空白任务、追加修订并推进头；Key 原证明与当前授权在 I/O 后和提交前复查。托管复制跳过业务 SHA 去重，引用保持各自 ScanEntry。历史媒体所有权与来源影响统计查询全部修订；管理预览仅服务当前 Asset。删除历史 Item 不回退头，不丢失原业务身份。

差异分类、原预检异常和仅新增确认范围不变；新修订接纳结果独立查询。周期核对门禁不因逐项接纳自动开放。应用回退保留0203与业务历史，禁止破坏性降级。详见 [ADR0102](/dev/adr/0102-explicit-directory-content-revisions)。

### 同摘要观测重新核验

完整预检的 observation_changed 可明确接受为同 Item/Asset 的新 SourceRevision，继承 owns_item，不改业务身份、原 Reference、标注及 metadata。读取先验证原始绑定，再按同 Source/Item/Asset 选最新接纳观测；不按当前路径头或 SHA 搜索样本，不在失败时回退旧凭据。返回上下文包含选中修订 ID，I/O 后切换证据会被发现。资格锚点变化保持阻断。无需 schema 迁移；回退入口后仍需保留已接纳账本与其严格读取。见 [ADR0103](/dev/adr/0103-explicit-same-content-revalidation)。

### 显式定位迁移

同来源完整预检的 not_seen→added 配对必须由人选择、核验同 SHA 后接纳。SourceRevision 的前驱可以跨路径，路径内编号独立；acceptance_order 由数据库序列给出接纳顺序。跨路径前驱必须保留永久 Item/Asset/SHA 与所有权。原 Reference 不变，resolver 按同业务资产的最新接纳顺序取实际路径，不按时间或路径内编号猜测。

退役旧 SourceItem 与建立新头、目标 RegistrationEntry 消费、审计同事务提交；原差异保持冻结。旧预览精确键在提交后清理，新路径按当前策略生成派生物。旧路径回归是新增候选。迁移0204保留原账本事实，已有移动历史时拒绝降级到旧定位读取。见 [ADR0104](/dev/adr/0104-explicit-source-relocations)。

周期计划显式持久化 `new_file_policy=manual|pure_additions` 与有效 IANA 显示时区（默认 manual/UTC）。纯新增策略固定在扫描快照并传给预检；整轮只含新增/相同且无错误才以真实 worker 节点走既有 additions_only 确认。任何其他差异整轮留在门禁，策略中途打开不会接管人工轮次。确认嵌套 I/O 后复核计划 revision/token、原账号与 Key proof，自动正式登记仅在原授权完全相符时由协调器恢复；人工正式登记仅观察。0205降级前须暂停自动计划。固定分钟间隔不表达日历定时，时区仅控制显示。见 [ADR0105](/dev/adr/0105-opt-in-pure-addition-sync)。

选择规则通过独立 preview/apply 入口更新，仅 recursive/include_globs/exclude_globs。预览绑定提议规则、Source/Location修订和排序接纳头修订身份，防止移动或重新核验后复用同计数旧预览；仅评估已接纳路径，不读取外部来源。保存拒绝活动尝试，原子递增来源修订与代次并阻断旧周期计划。观测账本保持；新差异按复用的后端选择语义识别规则外路径。业务 reader 只排除这三个字段进行定位比较，其他未知配置、原根/资格/回执与内容屏障继续严格校验。见 [ADR0106](/dev/adr/0106-reviewed-source-selection-changes)。

## 同步导航与证据留存

来源历史通过数据库分页读取，最近完整扫描和活动扫描分别查询，不依赖最近20条。来源级活动登记独立于最新扫描。Web 深链保存同会话的 dataset/source/scan 上下文，再分别校验父子关系；直接来源查询支持第一页之外的来源。历史和停用来源禁用写操作，权限变更使旧会话视图退休。

位置摘要仅执行有界数据库查询：可见来源计数/分页、该位置全部可见来源的任务计数/分页。复用 dataset_source_access 的真实 Run/FK 或历史 payload 关联，不以提交者或 payload.source_id 授权；字符串比较避免把坏旧 ID 强转UUID。最小 DTO 不返回 payload。位置可见性不能扩大数据集所有权。

领域账本持续保留，无自动压缩、过期删除或缺失等待期。每来源最多一个周期计划、活动扫描和活动登记；不设部署级计划总量上限，调度每次有界唤醒100个到期来源，扫描受原预算约束。运维应监控账本增长，不能把清理 AsyncJob 当作压缩领域事实。

托管训练冻结复用独立媒体与快照。构建提交应答异常时，清理前重新读取 package_key；已被版本引用或查询无法确认的 attempt ZIP 保留，避免删除实际已发布包。明确未发布的本尝试前缀才可清理。重扫、新修订、未见及取消不修改旧训练版本。外部引用训练冻结同样通过严格读取保留独立媒体和来源证据，已就绪版本不依赖外部原件继续在线。

## 数据集质量证据

质量运行使用 `dataset_quality_runs` 和 `dataset_quality_results` 固定提交时的成员及 Item/Asset/SHA/size。已有 verified Asset 身份受保护，质量检测不升级旧哈希提示；未核验或非图片明确跳过。结果不依赖 Task 或训练冻结版本，不修改已有可用性观测。Item/Asset 删除后保留历史 UUID，当前查询计算 stale；AsyncJob 外键 SET NULL，通用任务清理不删除证据。

原图读取沿用受控 managed locator 或引用 receipt/anchor/来源授权，完整字节捕获与图片检查共用有界临时文件、并发槽、超时和清理。质量入口保留无法解码字节的完整 SHA 证明，严格图片响应入口仍拒绝这些字节。精确重复按本轮 bytes_verified 的 SHA+size 聚合；预算超限不报坏图，读取前后重新核查身份和权限。

Worker 以 run generation、task/job ID、租约共同 fencing，逐成员事务提交；每块最多25项或60秒。引用成员派到来源指定节点的 direct queue，托管成员进入 media；跨节点成员在块边界让出后续派。120秒租约过期由30秒 Beat 调度恢复，旧投递和旧失败信号不再有写权限。取消立即使 generation 失效，保留已提交证据；显式恢复创建新 AsyncJob，只消费 pending 成员。

人工决定与计算事实分离：成员保存当前决定、修订和操作者，每次变化追加到现有审计日志，乐观修订防止覆盖并发复核。检测与决定均不自动改变训练资格。回退迁移前应停止质量运行并导出证据；迁移只增加质量表，不迁移样本/来源或冻结数据。

批量复核另使用 `dataset_quality_review_batches` 和 `dataset_quality_review_members` 保存不可扩张的预览范围与逐项 checkpoint，执行继续复用 AsyncJob 和单项复核 owner。筛选 SQL 与质量结果列表共用；预览固定规则、内容证据和 review_revision，摘要绑定决定和原因，确认不再查询动态范围。Result 锁使用 populate_existing，防止等待行锁后沿用会话缓存的旧修订。

每个成员的决定、审计及处理结果同事务提交。冲突/阻止明确保留，部分完成作为领域终态；取消后恢复只遍历 pending。批次锁、当前 Job/task/generation 和租约共同阻止迟到 Worker 或 failure signal 写入；Beat 每30秒恢复过期租约，派发失败保留持久意图。每项前后检查当前管理、来源与 key 授权，无源文件 I/O；历史证据不继承到新内容。通用任务保留策略不删除复核账本。回退批量复核迁移前先停止执行并导出批次证据；已应用的单项决定与审计仍保留在原质量表及 AuditLog。

## 位置来源诊断的只读口径

`storage_source_summary` 先按当前账号裁剪位置和来源，再对当前来源页批量汇总。`storage_source_diagnostics` 复用 Workbench 的 `observations`，显式限定 `DatasetReference.source_id`，避免将托管样本的多来源 provenance 任意归到某个来源。无 Dataset 时必须提供引用来源范围，禁止意外扩大为全局观测。

计数单位是保留的业务样本，含旧内容身份，非当前路径头；原件仍使用已有持久观测六态。预览通过当前 SourceItem 的业务 Item/Asset/SHA 联结，允许显式移动后的同一身份，拒绝旧样本继承新内容预览；当前策略禁用或来源停用优先阻断。它是生成记录摘要，不执行对象存在性检查。当前内容的合法缓存复用沿用现有读取合同，不增加仅诊断端生效的策略修订条件。

扫描和预检分别按来源批量查询，活动轮次优先，并独立保留最近完整成功记录。配置匹配由当前修订判断，不外露 snapshot。AsyncJob 保留期不删除领域证据。整个 GET 无源 I/O、写入或后台派发，不需要迁移或刷新 Worker。

## 数据集归档与恢复

迁移0214为Dataset增加archived_at、archived_by与lifecycle_revision，历史数据为active/1。`dataset_lifecycle`维护独立的预览/确认和共享写屏障，沿用当前创建者/超级管理员及Key权限；不通过停用Source/Location实现归档，避免破坏Task严格读取。单SQL汇总当前成员、主/次Task关联、活动run/Job/同步及有持久数据集归属的冻结版本，并以有序关系指纹检测等量替换；历史冻结归属缺失保持未知。

归档只持有User→Key→Dataset NO KEY UPDATE NOWAIT，写入状态/操作者/递增修订和审计，不反向等待Location/Source/Task。来源执行遵循Location→Source→Dataset NOWAIT；托管管理写入沿用Dataset先于Item/Task的串行化，写前重新核验当前状态与请求票据。Scene/标定同样保护Dataset结构，派生缓存的既有读取/清理仍由原owner执行。

托管接入和建任务授权、Scan/Registration快照、Sync委托及Quality/Review授权都捕获lifecycle_revision，旧记录缺失按1。最终提交前检查当前状态和捕获修订；归档再恢复不会复活旧排队工作、已完成观测的旧采纳许可或同步委托。新的显式请求可在当前权限下建立新轮；恢复不回放人员、位置、项目、Key或来源启用状态。失败/取消收尾保留，避免生命周期变化阻止清理。

缩略图、视频元数据等既有派生媒体回填仍由原维护owner执行，不携带生命周期委托，也不因归档停用。这些维护可以更新派生字段以保持已有任务可读，不授权接入新样本或更换来源身份；本阶段的旧票据失效保证不涵盖所有派生媒体维护。

训练新选择在合格样本集合上检查归档并保留生命周期票据，媒体核验后、发布版本前再次锁定复核；全部项目/数据集/批次/显式Task选择共用此路径。已排队或就绪的冻结版本使用自身不可变内容，不在历史媒体读取/worker打包路径追加归档限制。Task读图与标注继续遵守当前项目授权，个人阅读视图不是Dataset内容写入。

升级先向已验证目标应用纯加列迁移，再接入模型并刷新实际执行这些服务的workers；Celery不会热重载。API/Web与worker可短暂使用旧代码读取新增列，业务关系不回填或删除。发生任何生命周期变更后，迁移降级拒绝删除这些事实；代码可回退，但不能以丢弃归档状态为代价回滚。此治理尚未提供物理GC、退役或保留窗口删除。

## 位置维护的存储关系盘点

`storage_maintenance_impact` 在查询前后检查当前超级管理员、原Key身份及location revision。单条SQL以位置全部保留来源为范围，通过采纳历史/引用/Asset位置关系合并去重业务成员，再连接数据集、项目绑定、Task和标注；不遍历来源页、不执行外部I/O。活动扫描/登记读取持久run，不依赖短期AsyncJob。完整缓存按scope_location_id计入独立孤儿账本，读租约使用同一数据库时间。

临时输入只有当前Task关联可归因，冻结只有已保存source_lineage.location_id可归因；缺少证据的历史保持未覆盖。计数是已记录关系，副本记录不是可读性证明；响应明确execution_permission=false，不能被维护写入当作完整无引用或安全停用证明。

当前位置PATCH锁序为User/Key共享锁→Location排他锁，expected_revision防止配置覆盖；每次PATCH递增修订。扫描/登记在后续检查点拒绝旧快照，周期计划在后续唤醒进入blocked；预测/修复在自身的授权/内容屏障复核，并无位置级同步取消协议。完整缓存严格匹配位置修订，改名、人员和额度/TTL等变更也可能令旧缓存失配；预览按当前策略、内容和规格复用。来源管理授权与Task项目读取权限分开；停用位置阻止新的逻辑引用读图，包括缓存。冻结已保留媒体可按原项目权限继续校验，不应把关联版本数当作必失效数。

`storage_location_change` 复用无副作用候选校验和维护聚合：POST预览签发五分钟HS256令牌，绑定当前actor/Key证明、位置配置及连接修订、标准化候选与关系指纹。聚合在同一SQL内计算计数及各表身份/相关状态的有序SHA256摘要，不以计数代替关系身份，不包含心跳/最近访问或标注内容编辑。PATCH必须提供令牌，并在配置锁下重算快照，完成期限及绑定比对后才更新配置；失败不提交业务变化。成功业务审计脱敏记录差异、修订与两次盘点时间，失败状态由独立审计中间件保留。

位置锁并不覆盖所有Task/标注/冻结写入，不能在持有Location后阻塞等待这些owner锁；连接参与时遵循User→Key→Connection→Location。检查只代表提交时的SQL快照，配置CAS与owner各自的检查点共同约束逻辑更新。全异步owner静止、归档/恢复和物理回收仍须各自实现，令牌不提供这些执行许可。

## 引用图片的后台导出

`services/exporting/reference_media.py` 负责引用导出的固定选择、原请求授权证明和原图快照生命周期；`exporting/scope.py` 为大范围固定 ID 清单使用有界 SQL 参数。项目、批次与 Data Manager 在入队时封存私有 `export_authority`，包括原 API Key hash 与任务/Item/Asset/来源修订；`AsyncJobOut` 不返回该清单，幂等复投不替换它。

导出按现有项目 `EXPORT_ANNOTATIONS` 能力授权；显式任务选择继续逐项检查可见性。严格 reader 的默认任务权限不变，导出通过内部 task 授权回调复用读取基座，避免冒用管理员或把整项目导出缩成个人待办范围。实际读源时由 Celery hostname 决定 `worker:<hostname>` 锚点；完整缓存路径按同一逻辑身份读取，每份快照校验 SHA、大小和图片尺寸后再写包。

manifest 暂存于独占 TemporaryFile，避免同时打开两个 ZIP 写句柄；原图按块复制，逐项关闭快照，同时执行 entry、文件数和临时磁盘预算。标签、COCO、AAP 及掩码元数据共用包内路径，引用不经过平台桶签名器。引用包采用 job 隔离 cache key 并禁用 cache hit，托管缓存保持原合同。迁移 0211 将产物的完整格式列表扩展为 Text，旧值保留；已有长列表时拒绝降级，避免静默截断。

上传使用 job 独占对象键。最终短事务持有 job 取消锁、账号/项目/成员、原 Key、批次可见性、任务/样本/Asset 及来源/位置/连接 share locks，复验后原子发布 artifact、完成结果及通知。普通失败或取消清理本任务对象；存储删除失败或进程硬崩溃遗留的桶对象由导出桶生命周期兜底，本地 ZIP 的崩溃恢复清理与持久回收仍需独立治理。导出历史仍依赖当前受控 export binding，本切片没有完成存储后端迁移或历史 binding 固定。后台图片预标按独立合同核验并投递临时快照；完整原图缓存通过独立账本、额度与租约管理，不复用临时输入的生命周期。

## 预测临时输入的回收

多阶段预标及模型快照重试的 ROI 使用独立的 `prediction_input_leases`：上传前提交归属记录，前缀只由服务生成的 attempt UUID 推导，不接受外部 locator。正常处理结束主动清理，异常或失联由 cleanup worker 按持久记录恢复；对象删除与数据库确认可重复执行。记录固定 bucket 与 endpoint 身份摘要，配置变化阻止错误后端上的清理，不保存凭据或 URL，也不随业务记录级联删除。

后台图片预标已通过独立授权 owner 消费严格引用快照，根阶段、geometry 和 ROI 使用同一份字节，最终短事务以专有 receipt 写入候选。临时输入 owner 仍不替代来源/位置/Task 授权或内容证明；引用失败重试冻结新授权并保留原模型/内容，普通几何及合格受控整图原生 Mask 采纳通过当前严格原图和专有凭证检查；同步交互、预测测试和原生掩码创建/精修采纳通过独立请求 owner 核验；候选凭证及续点会话绑定原图身份，批量掩码修复和受控整图后台原生掩码各有独立资格，不复用普通几何凭证。预算、重扫窗口及运维要求见[预测输入生命周期](prediction-pipeline.md#crop-投递通用化)。

## 位置访问维护协议

迁移0215新增独立维护 Gate 和未收尾访问账本。Gate 使用逻辑位置 UUID，但故意没有业务 Location 外键：业务记录删除后仍保留未完成责任，独立登记事务也不等待调用方尚未提交的 Location 行。Receipt 外键指向 Gate，禁止删除仍有责任的 Gate。初始为 active/1，读取不存在的 Gate 返回虚拟初始状态；实际准入与锁检查才独立补行。

每次源访问以独立短事务取得 Gate SHARE、校验 active 和捕获修订并提交 receipt，再执行有界 I/O。最终业务发布在原 owner 事务持 Gate SHARE NOWAIT，重新检查捕获修订，不替调用方提交。结束责任只在真实线程、快照或流句柄结束后独立删除；取消或超时不会放弃后台线程，必要时返回时间晚于逻辑超时。进程退出或结束落库失败留下未收尾记录，无到期清除和强制清零。缓存命中仍取得当前准入，缓存填充发布及预测、修复、导出、质量与新冻结证明携带修订；历史缺字段按 1 归一化。

控制操作锁序为 User→Key→Location NOWAIT→Gate NOWAIT，与准入共享锁和最终发布串行化。开始维护拒绝后续新准入，但不会中止已登记 I/O；确认停止需要预览和提交两次零记录。每次 drain/stop/resume 均递增修订，既有 Task 的新请求取当前修订，不把历史扫描修订永久绑定到 Task。已保存独立冻结副本继续可读，只有回源路径受门禁；已交付远端的临时副本不能撤回，结果发布仍检查修订。

升级先应用0215，再刷新该部署命名空间全部 API、Worker 和实际队列消费者，核实旧进程已经退出及新任务签名和准入生效。新代码始终登记访问，`STORAGE_MAINTENANCE_PROTOCOL_ENABLED=false` 仅禁用控制转换，不能跳过协议。确认消费者完整升级后在相同命名空间开启该选项；不能将初始空账本当作没有旧访问的证据。代码回退保留 Gate/receipt；存在未收尾记录或非初始维护事实时 schema downgrade 拒绝丢弃。

此协议表达一个逻辑 Location 的平台受控访问，不是整块挂载、服务器、桶或所有物理读者的静止证明。多个 Location 可共享根；候选/内联连接验证、直接外部客户端、远端已复制输入及平台自有清理不在源静止保证内。当前片不交付退役、卸载或物理 GC；这些能力仍须独立核对身份、保留、引用及实际物理访问范围。

## 训练版本的固定存储归属

training 用途绑定提供新建训练对象的初始写入候选，已有归属不随它变化。版本的独立 `storage_ref` 保存已登记 Location/配置修订、绑定代次、端点、实际桶、版本前缀及物理身份；整个冻结前缀由同一 owner 负责，ZIP、保留图片/掩码、重试、读取和删除均沿用保存值。改变当前用途桶不重解释旧版本，端点身份变化则在 I/O 前拒绝。整个training用途切换仍须独立对账全部消费者，固定版本不授予切换许可。

历史归属为空时，显式核验操作只取数据库里已登记的平台 training 绑定，不通过当前环境桶猜测或在 GET 中 bootstrap。完整只读核验保存“当前保留副本”的证据，不改 snapshot/manifest，也不宣称历史创建身份。活动版本必须先取消并等构建退出；证据不足、对象变化或关闭不确定时不接管，原对象和责任保留。具体权限、预算及失败合同见[训练版本 API](/api/guides/training-datasets#固定存储归属与历史版本核验)。

实验、原生预览、逐样本评估和主动学习轮次同样拥有独立固定归属，分别限制 runs、previews、sample-evaluations、active-learning 根前缀；它们与输入版本及来源模型独立。历史接管共用完整 Reader、每进程容量及耐久维护 permit，取消等真实关闭，发布前重验权限／Key、全部记录和绑定／Gate。预览按自身 count 限定文件，评估按 attempt+SHA 限定报告，主动学习按冻结 ordinal／派生 SHA 限定 PNG，不回源修复历史证明。Preview 原行和评估删除意图行承担清理责任；主动学习没有新增删除入口。参见[辅助输出合同](/api/guides/training-datasets#辅助产物归属与历史核验)。五类 owner 的实现仍不开放整个用途切换。

版本移除时将完整归属复制到独立删除意图；未知旧版本意图不按当前配置执行删除。离线恢复先验证源归属，再由具有表 owner 权限的工具在显式事务标记下，仅映射 bucket/storage_identity 并记录账本；prefix、端点和核验证据保持。该映射不能作为在线用途切换、跨后端迁移或物理回收资格，部署及恢复顺序见[存储恢复说明](/ops/deploy/storage-recovery)。

## 托管原件条件创建

`managed_original_creation` 是新数据集直传、ZIP、连接复制和目录 managed_copy 的共同创建 owner。0219独立账本先提交最终键、入口和预算、当前配置/绑定及权限快照；无业务/Location级联外键，避免未提交业务外键自锁。状态经准备、冻结输入、单次最终写入、实际回执、完整回读进入 verified；所有初始事实、冻结输入、写回执和终态均受数据库约束。来源与目标维护 Permit 分别登记，目标 operation 在首个物理 I/O 前独立提交。

直传 init 先预留最终 locator，只向 import 桶 `__aap_upload_staging__/` 签 PUT；该根为保留名称且不进入 managed 扫描。complete 完整条件读取后冻结源 SHA/大小/ETag/真实 VersionId，用单 part UploadPartCopy 准备不超过5GiB的暂存对象，最终 Complete 带 IfNoneMatch=\*；空对象走条件 PUT。普通导入分块计算完整摘要并以相同条件完成 MPU。禁止 CopyObject 封存与无条件降级；目标命名空间不签客户端 PUT。已有旧直传、扫描及历史原件不回填创建事实。

SDK 每步单次尝试，真实成功响应先独立持久化，再按真实非 null VersionId 或 ETag 条件完整回读、核对 SHA/大小；无真实版本时最终 HEAD 复查当前观测。调用者在原事务重验权限、配置、维护代次和精确内容后关联 AssetLocation.creation_id。业务回滚、去重或提交应答不明保留最终对象及独立责任；仅可 abort 明确的本次 UploadId。写入/落库应答丢失、构造或关闭不确定保留 unknown/中间状态与未收尾 Permit，不能靠 TTL、HEAD404、同 SHA 或后来的412提升为已创建。

退休断言在同事务捕获可空 creation_id，并通过 RESTRICT 保留原始证据；已有断言禁止补写创建关联。离线恢复对创建账本所有列计算受保护摘要，不重定位或伪造目标创建响应；目标 AssetLocation/退休定位可按既有白名单映射，但原回执仍明确属于源物理存储。恢复不自动继续未完成创建。

真实 VersionId 仅为本次响应证据；无版本不视为不可变版本。当前恢复格式拒绝版本化桶，本片不启用版本化。条件创建与客户端直传隔离建立平台新写入协议，不能证明生产专属权限或阻止外部写者；所有消费者的固定版本/内容读取与物理回收资格仍需独立完成。

## 托管原件移除后的保留证据

迁移0216的`managed_original_retirements`保存已登记托管原件关系的逻辑移除断言。DatasetService在既有Dataset/Item删除锁内、级联删除前，以同一业务事务流式批量保存历史Dataset/Item/Asset/Location ID、精确内部locator、已有内容证据及当时可证明的配置/绑定身份。无业务外键；失败或调用方回滚同时撤回断言，重复捕获以唯一摘要幂等。外部reference、缓存及派生角色不进入此账本，未登记或此前已丢失的位置不合成回填。

配置核对只证明移除时初始平台绑定与运行配置一致，不证明写入时所有权、独占性、对象存在或当前字节。不能确认的旧证据保存unknown，不阻断原有合法逻辑删除。`policy_revision=1`、`retain_until=null`默认无限期保护；已有cache/input的retain_until是迟到写入的账本恢复窗口，物理删除可能先发生，与本原件保留期限不同。

全局只读预演以一个SQL观测重新汇总当前精确locator及样本/任务关联、保留训练snapshot和主动学习历史，再产生有界页。全部保留训练状态均为根，失败/取消仍可能重试；不支持的历史JSON作为未知阻断，不使用不安全UUID转换。归档关系保持保护。计数单位是断言，可重复同一物理原件；只返回已记录大小，现场可释放容量保持未知。身份/Key与配置前后复验，输出`execution_permission=false`，不为后续写操作设置屏障。

本账本没有物理执行器，不改变其他owner清理合同，不宣称已有生产恢复点/迁移保留登记、独占所有权、最小副本或全部物理访问证明。0216存在断言时禁止破坏性降级；代码回退保留表。离线受控恢复仅在已知初始平台绑定和身份匹配时重定位bucket/storage_identity，原始assertion_digest及业务身份不重写，未知身份拒绝自动采用；见[恢复映射](/ops/deploy/storage-recovery#恢复顺序与物理映射)。

### 显式策略与声明保留要求

0217为一条保留断言提供显式期限设置，以policy_revision进行CAS，null仍为无限期；不推定生产保留天数。独立`managed_original_retention_holds`以RESTRICT外键锚定该断言，没有业务或操作者级联外键。客户端UUID绑定预览、活动编号按锚点/类别唯一；创建字段及已解除历史由数据库约束保持，解除保留时间、原因、操作者和修订，不自动到期。

全量同bucket/object_key记录保守归组：任一null期限令整组无限期，否则取max；任一peer的有效声明要求保护全组。定位采用有界摘要索引并保留精确等值条件，摘要碰撞不能合并不同定位。不同端点身份仍保守归组，可能过度保留，不能把group当作同一物理副本证明。统计单位保持断言，公开自身与整组期限分别表达；先完整聚合、后分类分页。

仅当前超级管理员/全权Key可读取、预览和提交。五分钟确认绑定规范候选/原因、锚点修订/历史事实、完整peer/hold指纹与本地/持久配置；成功变更和安全审计同事务。写入按User→Key→bucket/key事务advisory锁→锚点→hold，所有保留控制共用组锁，没有任意peer行锁。既有捕获不追加跨组锁；它只能追加默认无限期证据，因此提交摘要只承诺重新观测，不对捕获或后续物理执行提供屏障。

该表只记录管理员声明的备份/迁移要求；未登记覆盖仍unknown，不是可核验恢复点、S3 Object Lock或所有旧清理owner共用的物理锁。回收预演始终execution_permission=false，实际独占所有权/副本、恢复/迁移证据和物理读写互斥须另行交付。代码回退保留事实，存在hold或非初始策略时拒绝0217降级；离线恢复跟随稳定锚点，只改原有物理白名单，hold完整字段与策略事实均参与受保护数据库对账。

### 已登记原件的完整内容观测

核验表独立于业务对象，以RESTRICT关联退休锚点。创建时固定可信历史SHA/大小、锚点内容/定位与本地/持久平台配置、操作者/Key权威以及作业/任务身份，不重写退休断言或保留策略。已完成、失败及未完成责任均保留；通用AsyncJob删除后，领域历史不随之丢失。存在任何核验记录时0218拒绝破坏性降级。

GET分页历史只读；显式POST创建或复用活动任务，接口和通用任务列表/详情/取消都要求当前超级管理员及Key全权。只允许已知平台身份、可信历史内容且在5GiB预算内的原件开始读取；没有历史SHA时不通过新读取伪造历史匹配。pending显式重派相同任务身份，原子认领后重复投递不再次读取；running不靠TTL重跑。

任务按User→Key→Retirement→Verification→AsyncJob的锁序做短事务，I/O前提交，结果发布前重新验证权限、配置、锚点和任务状态。storage_access在executor开始前提交精确Permit ID；取消、超时等待底层线程及流实际结束，关闭不确定保留访问责任。取消请求、领域终态和实际收尾各自表达，信号兜底不以逻辑失败清除物理记录。

读取使用短超时、单次SDK尝试及256KiB流块，不做图片解码或完整临时文件：当前HEAD固定对象观测，验证条件语义后按VersionId/IfMatch读取全部字节，比较SHA/大小，最后以无版本条件HEAD再次核对当前键。60秒协作截止和总字节预算拒绝未完成结果；HTTP403、缺失、内容变化、条件不支持和关闭不确定分别记录。ETag仅作条件值；AWS的[条件GET](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html)及[ETag与内容完整性规则](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html)支持这一机制，S3兼容实现仍需实际验证。

结果只证明所示观测时段完整读取与历史摘要相符。context_matches只表示历史锚点/配置仍匹配，不证明现在对象未被外部覆盖、独占管理、最小副本或删除资格。既有回收预演继续纯SQL与execution_permission=false，不消费历史匹配来绕过其他门禁。离线恢复不重定位历史观测；锚点物理位置变化后旧context失效，完整历史保留，需显式重新核验目标。

## 上传头像的固定存储归属

上传头像的随机token对应独立 `AvatarUpload`，而非从 `User.avatar_ref` 推测当前桶。归属只在创建时从已登记的 avatars 绑定分配，保存端点、精确键、位置修订、绑定代次与物理身份；规范化WebP的SHA256和字节数与其一起不可变。读取和删除不比较当前avatars桶，端点不符则拒绝。现有匿名token URL和用户DTO不暴露内部归属。

上传先提交pending事实，再在durable storage permit中写入并完整回读。真实线程结束后，重新锁定账号、复核Key原证明、原头像引用CAS及维护修订，才同事务发布ready新引用和旧对象retired意图。pending不按时间自动回收；进程崩溃、发布应答不确定或未确认物理收尾时保留待核对。历史token缺少独立记录时不从当前桶读删，也不以文件名或当前metadata自动接管，用户重新上传即可建立新归属。

匿名读取只接受ready记录，按精确键有界读取，关闭句柄并验证完整SHA/大小后再返回图片或304。替换、内置选择和清除先提交引用变化及退休意图，再清理原对象；退休行无任何用户引用时由cleanup队列重试保存位置的精确删除。失败保留责任，pending不进入自动清理。离线恢复只允许更换bucket/identity，状态、账号快照和内容回执均受保护。整个avatars用途切换仍未开放，其他用途尚须独立完成消费者与历史位置对账。

## 评论附件的独立存储归属

`CommentAttachmentUpload` 保存上传者、标注、任务、项目及首次绑定评论的 UUID 快照，均不使用业务外键。归属取自已登记的 annotations 用途绑定，严格定位只保存位置修订、绑定代次、端点、实际桶、精确键和物理身份；SHA256、实际大小、MIME、安全文件名和固定 2160 小时期限是独立不可变列。键为 `comment-attachments/{annotation_uuid}/{upload_uuid}`，不使用用户文件名或扩展名推导。账号、评论或数据集删除后，独立记录仍承担原对象责任。

文件可为空，最大 20 MiB，保留原始字节且不做图片解码；MIME 支持通用 type/subtype，规范为小写并去掉参数，最长 128 字符，缺省为 application/octet-stream。物理创建使用 IfNoneMatch 条件，读取以保存桶的 HEAD、ETag／真实 VersionId 条件完整读取并核对 SHA 和大小，关闭句柄后复查。当前 annotations 桶变化不会重解释旧记录；端点身份不匹配在 I/O 前拒绝。

状态仅向 pending、ready、retired 的方向推进，首次评论绑定及退休时间不可重写。失败、取消、提交应答或物理关闭不确定均保留原对象与独立责任，不从镜像 Feedback、旧 metadata 或当前桶猜测归属，不按年龄自动回收 pending。逻辑授权、评论可见性和清理资格由业务流程分别复核；存储 receipt 不授予访问权限。受控离线恢复仅允许映射 bucket／storage_identity，其他快照、内容、绑定和期限事实保持；存在任意记录时拒绝破坏性降级。

## 项目指南图片的独立归属与引用

`GuideAssetUpload` 独立保存上传者及原项目 UUID 快照、创建时间、实际 SHA-256、大小、MIME、安全文件名和固定 `storage_ref`，不以业务外键依赖原账号或项目。新上传从 annotations 用途分配位置，精确键为 `projects/{original_project_uuid}/guide/{upload_uuid}`。已有记录始终沿保存位置读取或删除，不查询当前用途桶；历史元数据不据此自动接管。PNG、JPEG、WebP、GIF、SVG 均保存原始字节，大小为 1 字节至 5 MiB。

`ProjectGuideAsset` 是当前项目读取权和存活引用的权威，项目外键 CASCADE、上传归属外键 RESTRICT。`Project.guide_assets` 只保留外显元数据、顺序与历史条目，Markdown、JSON 或 key 前缀不构成读取权，也不用于扫描保活。上传先持久化 pending 与内容事实，在耐久访问许可内条件创建、完整回读核验，物理操作收尾后重新检查原授权和维护修订，才同事务发布 ready、项目引用与清单。

显式清单 PATCH 在项目锁下比较 `guide_assets_revision`，只接受当前已核验引用的原条目子集或重排，以及精确已有历史条目；缺失或过时修订拒绝。所有实际清单变更统一增加修订，正文自动保存不依赖图片修订。引用插入须锁同一上传归属并要求 ready，retired 不可重新挂接。复制项目在远程后端验证结束后，以短事务锁定并重新核对源正文、清单、修订和引用快照，原子建立目标项目引用，保留原 Markdown/key；源项目移除引用或删除不影响副本。模板只携带正文。

认证 content 接口沿当前项目引用读取完整原始字节，核对 SHA-256 和大小并关闭对象流后，末端再检查当前权限、原认证凭证和维护门禁。旧 init/complete/sign-url 不再签发访问地址；未固定的旧图片保留原元数据，读取提示重新上传。Web 图片 owner 按账号、登录会话、项目及 hook 实例隔离请求、缓存和地址；范围退役时中止请求、拒绝迟到结果并释放全部地址。普通图片使用 Blob URL，SVG 用原始字节的 `data:image/svg+xml;base64,...` 在不透明源中展示，不创建与应用同源的 SVG Blob URL。刷新保留同范围旧地址，直到范围退役，避免正文或放大图仍在使用时被撤销。

删除图片只解除当前项目引用，最后一个引用解除时在同事务退休归属，不修改正文。项目删除显式处理已知引用；数据库级级联留下的 ready 无引用归属由 `purge_guide_assets` 兜底扫描，首轮只退休并提交，后续轮次才在无引用且无未收尾访问时按精确保存位置删除。清理不扫描 JSON，不设图片到期时间，不按年龄回收 pending；未知关闭或失败保留责任重试。迁移有独立归属或引用时拒绝破坏性降级。部署和备份约束见[指南图片恢复说明](../../ops/deploy/storage-recovery.md#项目指南图片归属的部署顺序)。

## 持久 RLE 的独立归属与发布保护

`RasterMaskObject` 不依赖 Task、Project 或 User 外键。可移植引用继续使用 `raster-masks/sha256/...json[.gz]`，物理位置固定为 `raster-masks/objects/{uuid}.json[.gz]`；JSON 与 gzip 各有完整逻辑键。记录同时保存规范 RLE 的 SHA、大小、尺寸及 runs，以及实际存储字节的 SHA、大小和严格 `storage_ref`。gzip 历史时间头可保持原样，复用按规范内容比较，同时按原始存储回执完整读取。

新候选和 `RasterMaskPublication` 在物理 I/O 前由独立短事务提交；业务事务不被该辅助流程提交。完成实际写入、完整回读和句柄关闭后，候选才能成为 ready。每个逻辑键仅一个 ready，竞争失败的完整候选转为 retired；转移发布保护前须完整核验胜出对象。进度提交保留发布记录，最终业务引用或匿名上传预约与精确发布记录的删除原子提交，回滚会恢复保护。未知写入、取消或提交应答不明均保留责任，不按年龄自动处理 pending 或发布记录。

读取先按 ready 记录定位，验证固定端点、UUID 对象及两份内容回执，真实关闭后再次复查 owner 与维护 Gate，再返回内容或 304；没有独立记录的旧 SHA 键零物理 I/O 拒绝。同步训练和评估入口在工作线程建立并关闭自己的事件循环与数据库环境，不能退回当前桶。内容锁使用非阻塞申请，竞争返回可重试冲突；导入先取得 Task NOWAIT 锁，再保存掩码和业务引用。

`purge_unreferenced_raster_masks` 保留既有 24 小时窗口，按成功写入或复用时间计算，读取不延长窗口。回收查询包含当前标注、预测、未完成训练快照、模型试用、Tracker 候选、采纳响应、修订、质检、修复计划、当前标注版本的区域审阅范围，以及新鲜未认领上传；区域审阅的 job 或 issue 可为空。清理还检查持久发布记录和精确 UUID 的未收尾读写删除许可，不扫描当前桶的历史前缀。

合格 ready 首轮只退休并提交，后续轮次才完整核验并删除其固定 UUID。旧代退休后若相同内容已由另一条合格 ready 承担，逻辑引用不再无限保留旧 UUID。扫描记录尝试时间，避免持续保留的前一批对象饿死后续对象；dry-run 不修改期限、状态或尝试记录。未认领上传预约超过 24 小时释放额度，但未知历史文件不随之删除。离线恢复只映射桶与物理身份，其余状态、代次、发布记录及内容事实保持；这仍不开放整个 annotations 用途切换。
