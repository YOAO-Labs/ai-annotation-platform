---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-25
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

## 访问端点与签名

`storage_presigning` 在原客户端的 `before-sign.s3` 阶段选择访问端点，不在签名后替换 Host。调用者显式选择 `browser`、`internal` 或 `ml`；下载默认用于浏览器。临时预测输入继续绑定确切 `VersionId`，有效期、对象键及响应参数保持原合同。

- `internal` 使用已核对归属的客户端端点。
- `ml` 使用内部协议与 `ML_BACKEND_STORAGE_HOST`；该值为空时使用内部端点，不继承浏览器公共地址。
- `browser` 的绝对 `MINIO_PUBLIC_URL` 使用公共 origin 签名；相对 `/minio` 使用内部端点签名并仅返回代理路由。

签名目标通过上下文隔离并在异常后恢复；物理客户端的端点、凭据、读写围栏和关闭归属不改变，也不为了签名创建额外客户端。跨 origin 签名要求路径式桶地址；发现虚拟主机桶地址时明确拒绝，不能丢掉桶名。相对代理和绝对公共代理对上游 Host 的要求不同，见[部署签名契约](/ops/deploy/docker-compose#对象存储访问与签名契约)。真实跨站点、TLS 与代理部署须在目标环境验证，本机合同测试不证明其网络可达。

## 数据模型

### 位置列表的持久摘要

`GET /storage-locations`在可见位置分页后批量组装列表项：连接与访问维护状态、保存的节点探测，以及当前用户可见来源的已采纳索引规模。`StorageLocationHealth`按统一事务时间统计当前配置且未到期的节点结果，保留最近历史记录和最早有效到期时间；不把位置停用解释成网络故障。列表GET没有物理I/O，不调用每行详情或源扫描。

`storage_source_usage.read_usage_page`将既有去重查询按位置分区，详情单位置统计也调用同一实现。候选关联和地址验证保留在SQL中，不把逐文件事实载入应用；跨位置域不混合，权限裁剪在聚合之前生效。列表的源观测时间、探测时间与查询计算时间各自保留，平台对象快照和未知物理容量继续使用独立口径。

### 独立 Task 原件

DatasetItem托管原件继续由DatasetAsset/AssetLocation和原创建记录承担。独立Task直传没有DatasetItem，使用TaskOriginal保存当前annotations位置、完整SHA/大小和源写入回执；Task.original_id引用它，原件记录不FK业务Task/Project/User。历史点云即使没有直接dataset_item_id，也可能通过primary_lidar关联DatasetItem，不能仅凭NULL直接认定独立原件。

ManagedOriginalCreation显式区分dataset/task作用域。Task分配保存project_id/task_id、独立随机最终键和import暂存键；沿既有条件multipart writer执行完整回读、真实关闭和取消收尾。Creation的历史桶、版本及回执不可改，当前位置通过TaskOriginal表达。两个业务发布检查之间释放Task/凭证锁，末端短锁重核同一上下文才关联原件；失败或业务删除保留独立记录，不补偿删除。

离线恢复只映射TaskOriginal当前bucket/storage_identity，保留源Creation和原ETag/VersionId事实。源写回执不是恢复目标的版本选择器；后续读取必须观察目标当前对象、条件读取并完整核对SHA。未固定的历史Task不自动认领。原件全调用者接入和回收仍有独立门禁，上传封存完成不开放完整用途切换。

严格图片内容入口与数据集质量检查使用 `original_sources` 冻结当前关系及保存位置。Task直接Item与primary_lidar必须一致；Dataset选择唯一present托管原件，不把缓存纳入候选，也不要求历史核验记录已有Creation。物理读取前、取得耐久读取许可后及响应前，分别重核当前权限、原凭证、关系、位置和维护修订；业务行锁不跨物理读取。原地引用的来源读取与完整缓存命中同样在许可取得后复核原凭证，响应前比较完整引用context并锁定同一Gate代次；撤权不把合格缓存误记损坏，也不会回退读源。

`managed_original_reader` 只接受端点、保存桶/键、完整SHA和大小，以及调用者显式预算。它建立独立客户端，从目标当前HEAD取得条件，验证错误If-Match确实失败，完整读取并核对摘要、EOF及最终HEAD，再真实关闭Body和客户端。历史写入VersionId不参与目标GET。返回的BinaryStage拥有临时文件和对应准入名额，取消和超时等待实际I/O退出；关闭无法确认时保留未收尾许可。图片适配器在同一文件上解码并移交给响应，不重新下载或复制。通用二进制消费者使用下文独立预算；原生视频及其余当前用途定位仍有各自门禁。

### 样本评估的当前来源核对

`sample_evaluation_originals` 为既有 source API 提供窄适配：沿 `original_sources.resolve_task` 固定当前 TaskOriginal 或 DatasetAsset，取得同一位置的耐久许可后调用 `managed_original_reader`，独立两槽、每图最多 16 MiB。先验证当前已登记 SHA，再比较冻结评估 SHA；未知归属零原件 I/O，不从当前用途桶、旧路径或冻结 SHA 猜测当前对象。Pillow 在同一临时文件上执行既有 1,600 万像素、单帧及 EXIF 检查，不扩大格式或尺寸预算。

适配保持评估的项目管理权限与两个读取 scope，捕获并复核原 JWT／Key，不借用要求 `tasks:read` 的通用下载授权。复制、解码、标签分类、临时文件关闭都在同一责任范围；取消和超时等待真实线程退出，未知 Body、client 或本地文件关闭保留显式准入额度与维护许可。关闭后再比较当前任务及标注，阻止慢关闭期间的变化成为成功回执。主动学习原件输入见下节，冻结评估图片、报告、训练及其他 Worker 消费者分别推进。

### 主动学习原件输入

`active_learning_originals` 收口预览／创建、Worker 缺失 PNG 重建及接纳前整组复核，移除按现行桶自行 HEAD／GET 的共享同步读取。新内部快照仅冻结原件身份、SHA／大小及逻辑位置 ID，实际 locator 始终取 canonical owner 的保存位置；每次物理操作固定完整关系和维护代次。单图 16 MiB、独立两槽及既有归一化预算保持，正常维护恢复与受控恢复桶映射不改写跨轮次内容身份。

HTTP 权限证明与后台持续授权分开：原请求复核准确 JWT／Key，后台保存并核对原 Key proof 指纹，范围仍为原三个 scope。最终创建与接纳检查复用已取得的业务锁，不在辅助复核中提交 Job／Batch。Worker 准备输入时保持异步会话，以 claim／generation／status guard 限制读回与交付；源读取、PNG 写回／完整读回及真实关闭都先 drain，未知关闭保留许可。已有 PNG 只核对自身输出 owner，旧无原件事实的规格不能回源重建或新增接纳，历史报告和拒绝仍保持。

### 点云质检原件与统一发布

`point_cloud_quality.originals` 为既有质检 Worker 提供 PCD 输入适配。新范围声明 `original_contract=1`，已有 pointcloud 记录固定安全原件收据：owner_kind、内容 UUID、完整 SHA-256、大小与逻辑 location_id。轴约定随来源快照冻结，未指定时使用 `iso_8855`；当前桶、路径、旧 content_hash 或派生物不构成原件身份。每次沿 `original_sources.resolve_task` 解析保存位置并复核 direct／primary 关系，物理位置和 Gate 代次只属于本次操作。缓存以内容收据及轴约定为键，命中仍检查当前权限、原件和 Gate。

创建请求捕获准确 JWT／Key；后台委派在 AsyncJob 私有 `point_cloud_quality_authority` 中保存 actor、Key ID 和 Key proof 的不可逆指纹，并核对 Job／Run／项目／创建者关系。后台持续读取当前账号、原 Key 和项目职责，不持久保存 Bearer，也不沿用 JWT 的短寿命。授权保持 `REVIEW_WRITE` 与准确 Task 可见性，project／scene 范围另限 manager，不复用训练的管理者授权，也不新增 API Key scopes。singleflight 包含委派身份，后来的请求不改写已有运行的授权。

读取复用 `managed_original_reader` 的完整条件捕获，单 PCD 256 MiB、独立两槽；复制 120 秒、传输 30 秒、解析与归一化 30 秒为合作式期限，超时和取消 drain 真实线程。临时文件实际关闭后才释放额度与耐久许可，未知关闭保留责任。缺失、未支持或读取／解析失败可产生明确 skip；权限、绑定、原件关系或 Gate 变化终止整轮，不能被宽泛异常吞成 skip。既有 5,000 Task／20,000 标注以及 ASCII／非压缩 binary PCD 预算保持，相机仅使用标定与尺寸。

Job→Run 短锁原子领取 pending 并固定 Celery 身份；计算阶段只收集有界 finding 候选与更新进度，不写 Issue。所有实际读取与解析收尾后，末端锁定当前授权、Task、Scene、标注、轨迹、区间、pose、Dataset 和 canonical 原件事实，并重核完整来源／配置摘要。最终 Issue upsert、Run／Job 结果和通知共同提交；提交前再次检查自然期限。取消使用相同 Job→Run 顺序，异常先 rollback 本轮问题再登记终态，既有人工复核不被半轮结果覆盖。业务锁不跨远端读取。

旧 completed 继续作为历史读取；缺少合同的旧 pending／running 在派发或重放时转为 stale，零原件 I/O，不批量回填或自动迁移静置记录。新问题保存安全原件收据及轴，staleness 按 Task 去重复核 owner、内容、逻辑位置和轴变化；旧原件相关问题缺少事实时明确过期，纯轨迹历史沿原合同保留。此适配不新增独立 Task 的 Scene 注册，不开放其他原件消费者或完整用途切换。

### 认证通用原件交付

浏览器公共读取模块 `apps/web/src/api/originals.ts` 只构造Task/Item/精确camera link的认证请求，核对身份中的完整下载路径，不给任意URL附加Bearer。原件下载共用两路可取消排队，覆盖完整响应体，前景可提升预取；只有明确容量不足才有限重试。资源owner按账号/session划分，同session令牌刷新后在途旧凭据结果最多重试一次，不能覆盖新凭据；跨账号、session及不完整的跨标签凭据停止读取。该公共层不建立原件缓存，各显示消费者仍须管理自己的内容版本、地址与释放。

`original_binary` 按 Task、精确相机 link 或 DatasetItem 冻结来源；`originals` 路由提供零物理 I/O 的身份及绑定 content UUID/SHA 的完整下载。相机只借当前 Task 可见性读取其明确 camera\_ 链接；不借此开放任意 Item。Item 入口仍要求 Dataset 管理权限，并校验路径中的 dataset_id。来源、原请求凭证和 Gate 在准入后与交付前复核；引用图片继续使用既有严格图片入口。

`original_binary_response` 复用严格 reader，单文件512MiB、独立两槽，槽覆盖复制与响应到真实文件关闭，声明大小的最大同时暂存为1GiB。匿名临时文件不产生命名缓存；响应前断连、取消、超时及发送失败等待在途线程收尾。复制和响应各120秒为合作式期限，真实收尾可能更长。octet-stream attachment 不执行图片解码，也不支持 Range/HEAD 或条件304；大文件明确413，不隐式回退签名地址。既有图片四槽独立保留，原生视频 Range 仍使用独立链路。

### 视频原件临时表示

`original_binary.authorize_original / recheck_original / capture_authorized`为完整下载与视频准备共用原Authority和保存来源合同。`original_playback`只接托管video，Bearer准备完整核验；`original_playback_registry`按Authority/session/资源/完整来源保存进程内匿名stage及独立短租约。能力秘密只在准确路径的HttpOnly/SameSite=Strict Cookie中发送，JSON与media_path不含秘密，不保存原JWT/API Key。session是客户端资源owner，不授予服务端权限。

每次native GET/HEAD先取得响应pin，重核原凭证、当前User/Key/scope、Task可见性或Dataset管理权限、同一源和Gate，再同步重核租约仍有效，才返回200/206/HEAD/范围错误。既有Vite代理会改Host，浏览器同源判断同时采用Fetch Metadata；跨站与同站异源媒体请求拒绝。`original_playback_response`用os.pread独立偏移，各响应取消后drain真实线程再unpin，不关闭其他消费者的文件。完整SHA证明来自准备时的全量捕获，单段响应不伪称重新完整核验远端。

单stage5GiB、两stage、最多8响应，每stage32租约；准备900秒、transport30秒、响应120秒为合作式期限。租约300秒，stage最迟3600秒退休，30秒reaper与lifespan关闭负责收尾。先退休、禁止新pin，真实读/关闭完成后释放槽；未知操作或关闭保留本进程额度。同键在途准备明确429，ready复用仍在所有await后重核权限/期限。没有持久副本或迁移表，API重启后临时表示消失。

此API只资格化当前单Uvicorn实例，其他实例或重启后的410必须重新Bearer准备；多实例不共享本地文件，不按每个Range重新复制远端文件。托管视频DTO/manifest不再返回整段原件签名；原生工作台通过身份查询和prepare取得同源租约。逻辑舞台按账号/session/Task/内容身份退休，租约传输轮换保留当前帧和草稿；先拆旧video源再释放。HEAD用于区分过期与权限/解码失败，旧账号清理不触发新账号退出。转码/精确帧派生及相关Worker使用各自保存位置；媒体缓存用途允许受控切换后续新分配，已登记派生输出保持原地址。原件暂存不能替代派生物自己的owner、内容回执或恢复协议。

方案与取舍记录于[ADR0107](/dev/adr/0107-scoped-native-video-stages)。

托管原件DTO与点云manifest只提供延迟身份路径，未知历史不导致整页列取失败。显示消费者按账号/session、资源及内容 UUID/SHA 管理 Blob、Bitmap、RGBA 与 GPU 资源；取消一个共享消费者不终止仍有前景消费者的读取。图片预取最多保留两份、原始字节共64MiB，前景读取仍受512MiB单文件与既有解码预算约束。普通切帧保留就绪几何至原子替换，owner退休则销毁旧资源并取消待写回视角。SVG 使用不透明的 data URL，普通位图使用可撤销 Blob URL；格式探测只决定显示 MIME，不转换原件。必需金字塔预览不触发完整原件下载。缓存不提供当前在线或持续授权的证明，完整用途切换仍独立推进。

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

| scope    | 谁能建            | 谁能读配置                  | 谁能改/删     |
| -------- | ----------------- | --------------------------- | ------------- |
| `owner`  | 项目管理员 / 超管 | 具有管理角色的创建者 + 超管 | 创建者 + 超管 |
| `global` | 仅超管            | 项目管理员 / 超管           | 仅超管        |

配置列表及单项GET要求`SUPER_ADMIN`或`PROJECT_ADMIN`（`_MANAGERS`），API Key还需`datasets:read`。超管看全部，项目管理员看「全局 + 自己创建的」；角色降为普通人员后，即使仍是创建者也不可读配置。前端查询按当前角色启用，并保留会话／角色隔离。服务内部的`can_use_connection`仍表达业务使用范围，不作为读取配置的授权依据；导入入口本身要求管理角色。创建权限同样限管理角色，其中`global`仅超管可建。

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

外部连接器负责复制导入；平台对象按保存位置使用默认 StorageService 或受控 S3 连接的当前凭据执行 I/O。内部 storage_locations/storage_bindings 记录默认平台位置和九用途的桶，不复制连接器密码。目前头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件开放受控的新写入位置变更。配置与固定记录不符时登记会失败；用途新分配切换不搬迁历史对象；改变已有对象的物理地址须独立完成定位、迁移与对账。

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

### 显式补算旧托管原件

`scripts/verify_legacy_dataset_assets.py` 在定位回填后建立当前原件的 SHA-256 基线。它不证明当前字节与历史标注时相同；历史摘要未知时，应先由数据负责人核对原件，再决定是否应用。工具不会搬移、覆盖或删除原件，也不更改样本、任务、标注、项目关联或历史冻结快照。

在选定数据库与平台存储配置的 API 环境内执行：

```bash
cd apps/api
.venv/bin/python scripts/verify_legacy_dataset_assets.py \
  --preview --dataset-id <UUID> --batch-size 100 --output /secure/legacy-plan.jsonl
# 核对清单中的 Item、Asset、SHA-256 和大小后，按同一清单应用
.venv/bin/python scripts/verify_legacy_dataset_assets.py \
  --apply-plan /secure/legacy-plan.jsonl
```

也可用 `--item-id <UUID>` 选单项，或显式 `--all-datasets`。批次最多500项，按 Item UUID 分页；预览的 `--after <UUID>` 可缩小续查范围。清单以0600权限独占创建，不覆盖旧文件，仅含业务ID、SHA、大小和不透明指纹，不含物理定位或凭据；应按内部数据保管。预览不发布内容身份，但会写入并收尾访问台账，因此不是数据库 READ ONLY 模式。

预览及应用分别读取完整原件：单项最多5GiB，按分块计算SHA，读取预算60秒，客户端单次传输等待15秒；超时／取消等待物理读取真实结束，不提交该项。HEAD、条件 GET、完整长度及末次当前 HEAD 必须一致，条件读取不受支持时拒绝。应用前先验证并冻结整份清单；缺失尾记录、校验和不符或非法条目均在首项发布前拒绝。随后逐项重读并比较SHA／大小、Item／Asset身份、精确定位、配置、生命周期与维护修订；有变化须重新预览，不按当前桶补猜路径。

应用只把原有 `unverified` Asset 及其唯一旧托管副本发布为 `verified`／`present`，同事务写入 `dataset_content.verify`，目的为 `legacy_current_baseline`。已核验内容不能通过此工具重绑定；重放同一清单返回 `already_verified`，这仅表示身份已发布，不表示再次检查了当前原件。批量预览跳过已核验项，外部引用不适用。未映射项先执行定位回填；原件缺失、旧大小不符、配置变化或定位歧义保持未知状态并报告。

退出码0表示所选清单无失败；1表示逐项失败或原预览中仍有失败项，成功项已经单独提交；2表示清单／运行异常。中断或锁竞争后可重跑完整清单，已成功项幂等跳过；未完成的预览文件不能直接应用，应生成新文件。清单SHA用于发现文件变化，不是历史内容证明或签名授权。

设计决策见 [ADR 0091](/dev/adr/archive/0091-dataset-content-and-location-identity)，接口见[数据集上传与扫描](/api/guides/datasets)。

## 受控目录与动态注册

`STORAGE_FILESYSTEM_ROOTS` 将稳定句柄映射到部署者允许的绝对根，默认 `{}`。StorageLocation 仅保存句柄、相对基目录、授权、修订和内部节点指纹；公开 API 不返回根路径或指纹。统一挂载后，新建子目录和数据库注册都无需重启；全新宿主机根必须先由运维挂载，修改 Compose volumes 通常需重建受影响容器，无需重建镜像。

`services/filesystem_storage.py` 使用 Linux openat2，要求 Linux5.6+、x86_64/aarch64；不支持时拒绝访问，无普通 open fallback。打开根时拒绝路径中的链接；根内使用 BENEATH / NO_SYMLINKS / NO_XDEV，拒绝链接及跨子挂载。根与位置基目录按节点固定路径摘要、dev/inode；节点内替换拒绝静默重新绑定。读取样本先验证常规文件，目录预算包含被过滤条目，FD 均按调用释放。支持有效 UTF-8 名称。

API/worker 各自加载根映射；Celery `worker_direct` 加实际 hostname 校验提供指定节点探测。首次节点探测信任部署映射，后续保留首次取得的有效根身份；失败、超时、配置变化与晚到结果不能重新绑定。数据库按 `(location_id, node_id)` 保留最新观察，证据有效5分钟。API本地等待3秒，远端等待5秒；超时线程结果不会自行落库，阻塞文件系统调用仍依赖内核恢复。跨节点能力证据不能证明字节相同，后续原地读取仍需内容核验。

位置登记/修改仅超管；项目管理员按 global 或 use_user_ids 使用和探测，员工不得枚举目录。每页重新检查当前授权，游标绑定位置、修订、节点、路径摘要和目录快照。现有连接通过 connection_id 关联位置；凭据仍由 StorageConnection/Fernet 持有，未复制。连接编辑使用独立 revision（迁移0210，旧行初始为1），按 connection→location 顺序加锁。候选认证与有界列举在行锁外进行，不修改 ORM 原配置；验证匹配的 SFTP 位置时复用 SftpAccess 的固定公钥与地址约束，普通复制连接继续使用系统 known_hosts。验证成功后加锁、重读当前修订、活跃账号/管理权限、原 API Key、最新白名单和引用，再原子发布。有效名称编辑仅推进连接 revision，实际配置变化或显式凭据轮换才推进位置 config_revision；旧探测因此失效。有内容/用途引用时拒绝实际配置改向或删除。删除无引用连接级联清理关联与探测，既有项目级外键行为保持一致。

九用途绑定查询只读，缺失不自动 bootstrap，共桶不合并用途。头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件提供经预览确认的用途切换入口；DatasetSource 提供下文的只读观测扫描及显式登记。完整接口见[受控存储位置](/api/guides/storage-locations)，决策见[ADR 0092](/dev/adr/archive/0092-controlled-storage-registry)。

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

`original_cache_entries` 独立保存来源位置、Asset/SHA、大小、策略修订、保存目标事实和 generation；没有指向业务对象的级联外键。上传前独立提交预约，位置级事务锁保证并发请求不超订；上传中、已就绪与待删除对象均占用原图额度。该额度与预览占用、整个对象桶和物理磁盘容量分别计量。同一 Asset 只允许一代正在填充或发布的缓存，旧代回收记录继续保留。

成功 PUT 后按块重新核验实际完整对象的大小和 SHA，再在短事务内复验策略、额度、来源修订及保存目标，发布独立 `original_cache` AssetLocation。它不会覆盖 `managed_original` 或未知 locator。验证复用输入严格快照的图片资格，以相同 SHA 证明完整字节，不额外占用请求快照名额。读取缓存仍需捕获完整严格快照，不能用预览替换。

读租约固定六十秒，正常捕获结束即释放；请求取消但 SDK 线程未结束时保留租约至到期。淘汰在获得缓存行锁后复查租约。TTL 默认一天，从预约时固定，访问只更新最近使用时间；配额收缩、策略禁用、业务删除和过期均由 `cleanup` 队列每分钟分批核对。先移除精确旧代 locator，再删除自有 key，确认删除后才释放额度；删除失败继续计费并持久重试。已确认 PUT 的验证失败可立即回收；不确定 PUT 和失联写入保留七天重扫窗口。服务器指纹不符时保留记录并停止向新目标发送删除。窗口与有期限租约不承诺覆盖无限期停顿的远端 I/O。

旧对象保留 media-cache 的 `original-cache/v1/<Asset>/<SHA>/<generation>`；新对象以保存引用中的物理前缀定位，逻辑后缀为 `original-cache/v1/<Asset>/<SHA>/<Entry UUID>/payload`，不受 `videos/` 生命周期影响。清理只处理这些资源所有者生成的精确键，不触及来源文件、托管原件或冻结训练副本。迁移 `0213` 在有缓存记录、原图缓存 locator 或按需策略时拒绝降级；应用回退须保留账本及恢复任务。

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

## 外部原件责任声明

`DatasetSource.external_protection` 保存责任方、恢复方式、内部参考编号和服务器登记主体／时间，`external_protection_revision` 从0开始独立递增。旧来源保持空声明；同一位置下不同来源可以有不同责任方。声明不放入 `source.config` 或位置配置，因此不会改变内容修订、规则证明、历史扫描、有效缓存或原件身份。

`dataset_source_protection.update` 复用当前来源权限、Key复验和Location→Source锁顺序，仅比较声明自身的 expected revision。它没有物理I/O、在线探测、活动作业或数据集可写门槛；停用／注销和归档后的责任维护仍使用既有来源权限，孤立来源继续仅对超级管理员可见。登记主体为保留UUID事实，不以用户外键删除联动抹去责任。审计动作是 `dataset_source.protection_update`。

迁移0244仅增加两个字段及一致性约束，旧来源不自动补责任。非空声明拒绝破坏性降级；应用回退应保留新增列及记录。完整／保护数据库指纹均包含声明所有字段和修订，不属于物理位置重定位白名单。恢复演练通过业务服务登记后备份，再核对原声明和修订保留；外部原件仍不在九桶备份内，缺失／同名替代仍严格拒绝。声明只表达责任与方案，不作为可恢复证明、备份保留要求或物理删除许可。

## 当前已采纳源索引规模

`storage_source_usage` 在现有 source-summary 授权后，以 `source_scope_clause` 裁剪整个位置的当前 `DatasetSourceItem`。Head 与 current revision、该修订 Run／Entry、Item／verified Asset 的关系及 SHA／完整大小在数据库校验。LEFT JOIN 保留缺失关系；不要求当前头等于最初 DatasetReference 的 run，也不按当前配置修订重新解释冻结地址。reference 和 managed_copy 均属于来源规模，复制形成的平台对象仍独立计量。暂停／注销保留的头仍计量，已删除的头不从修订历史补造。

候选、去重和所有计数在同一 SQL statement 快照内聚合，应用只接收固定大小结果，不加载全部路径，也无文件或 provider I/O。来源页诊断仍仅处理当前页，usage 不受分页影响。观测范围取保存扫描的完成时间，计算时间使用 `statement_timestamp()`；这不是实时文件盘点。

- filesystem：单位置内以冻结 host／root／base 锚点和完整相对路径归组。host 是扫描时的 boot-id 摘要，不能用 node_id 代替；来源根 identity 不参与键，父子来源因而可合并。位置内存在多个无法比较的主机／挂载域时，整个范围进入未知；不同相对路径也不足以证明可以相加，不推断共享磁盘。
- S3：冻结 endpoint／bucket、准确完整 key 和明确记录的 VersionId；JSON null 与字符串 `null` 同为未版本化，缺字段或错误类型保持未知。保留重复分隔符、点段和编码字符，不调用文件系统规范化或二次解码。不同已采纳版本分别计量。
- SFTP：冻结 root_handle／qualification 和完整受控根内路径；校验收据 `protocol=sftp` 与准确远端路径。

同址任一关系或收据不可信即不报告完整；SHA／大小冲突排除整组，不选择最新或最大值。已确认小计与未知总量独立提供，只有完整空范围才返回总量零。去重限当前逻辑位置的已保存地址，不证明硬链接、跨位置别名或文件系统实际分配量。响应不外露内部路径、锚点、端点、桶、键或逐文件列表；Web 在已有摘要权限门禁内展示并复用刷新失败保留／撤权隐藏规则。

## 数据集归档与恢复

迁移0214为Dataset增加archived_at、archived_by与lifecycle_revision，历史数据为active/1。`dataset_lifecycle`维护独立的预览/确认和共享写屏障，沿用当前创建者/超级管理员及Key权限；不通过停用Source/Location实现归档，避免破坏Task严格读取。单SQL汇总当前成员、主/次Task关联、活动run/Job/同步及有持久数据集归属的冻结版本，并以有序关系指纹检测等量替换；历史冻结归属缺失保持未知。

归档只持有User→Key→Dataset NO KEY UPDATE NOWAIT，写入状态/操作者/递增修订和审计，不反向等待Location/Source/Task。来源执行遵循Location→Source→Dataset NOWAIT；托管管理写入沿用Dataset先于Item/Task的串行化，写前重新核验当前状态与请求票据。Scene/标定同样保护Dataset结构，派生缓存的既有读取/清理仍由原owner执行。

托管接入和建任务授权、Scan/Registration快照、Sync委托及Quality/Review授权都捕获lifecycle_revision，旧记录缺失按1。最终提交前检查当前状态和捕获修订；归档再恢复不会复活旧排队工作、已完成观测的旧采纳许可或同步委托。新的显式请求可在当前权限下建立新轮；恢复不回放人员、位置、项目、Key或来源启用状态。失败/取消收尾保留，避免生命周期变化阻止清理。

缩略图、视频元数据等既有派生媒体回填仍由原维护owner执行，不携带生命周期委托，也不因归档停用。这些维护可以更新派生字段以保持已有任务可读，不授权接入新样本或更换来源身份；本阶段的旧票据失效保证不涵盖所有派生媒体维护。

训练新选择在合格样本集合上检查归档并保留生命周期票据，媒体核验后、发布版本前再次锁定复核；全部项目/数据集/批次/显式Task选择共用此路径。已排队或就绪的冻结版本使用自身不可变内容，不在历史媒体读取/worker打包路径追加归档限制。Task读图与标注继续遵守当前项目授权，个人阅读视图不是Dataset内容写入。

升级先向已验证目标应用纯加列迁移，再接入模型并刷新实际执行这些服务的workers；Celery不会热重载。API/Web与worker可短暂使用旧代码读取新增列，业务关系不回填或删除。发生任何生命周期变更后，迁移降级拒绝删除这些事实；代码可回退，但不能以丢弃归档状态为代价回滚。此治理尚未提供物理GC、退役或保留窗口删除。

## 平台当前对象的分类计量

平台桶盘点在每页 LIST 后，仅查询已有持久归属，将实际字节归入托管原件、预览／缓存、训练版本文件、临时产物、其他平台对象及未归类。同一物理地址只计一次，跨分类冲突保留为未归类；来源规模、预约额度和业务大小不加入平台物理对象总量。已退休、到期或业务删除的归属仍可解释尚未消失的字节。

分类随原有检查点推进，完整桶结束后与总量及测量时间一起发布；旧快照没有分类时保持未知，失败保留上次成功值。读取只聚合快照，不访问来源，也不改变消费者或清理授权。归属适配、覆盖与排除范围见[当前对象分类](/dev/reference/storage-buckets#当前对象分类)，响应见[平台对象占用盘点](/api/guides/storage-locations#平台对象占用盘点)。

## 位置维护的存储关系盘点

`storage_maintenance_impact` 在查询前后检查当前超级管理员、原Key身份及location revision。单条SQL以位置全部保留来源为范围，通过采纳历史/引用/Asset位置关系合并去重业务成员，再连接数据集、项目绑定、Task和标注；不遍历来源页、不执行外部I/O。活动扫描/登记读取持久run，不依赖短期AsyncJob。完整缓存按scope_location_id计入独立孤儿账本，读租约使用同一数据库时间。

原来源范围中的临时输入只有当前Task关联可归因，冻结来源只有已保存source_lineage.location_id可归因；缺少证据的历史保持未覆盖。计数是已记录关系，副本记录不是可读性证明；响应明确execution_permission=false，不能被维护写入当作完整无引用或安全停用证明。

保存目标由`storage_maintenance_targets`独立聚合现有27家族，保留来源计数原义。按实际保存的JSON locator或物理location_id统计九用途全部状态责任，不借助当前Binding定位历史。Creation最终原件与存在的暂存是两个role，DatasetAssetLocation只计managed_original；无物理地址的失败Preview不虚构目标。OriginalCacheEntry.location_id是目标，scope_location_id仍是来源配额范围；NULL引用但有明确scalar位置的旧记录不算未定位。责任可重叠，不能解释成对象数或字节数。

业务关联只使用直接保存ID和可证明的当前关系并去重：金字塔经ImagePyramidAsset，训练Run／Preview／Evaluation的dataset_id指训练版本，Thumbnail按owner_kind关联Source／Item／Task，指南共享项目使用ProjectGuideAsset。孤儿责任保留，缺少项目字段的RLE不能从键推断项目。来源A／输出B分别进入来源血缘及B的保存目标，不互相替代。当前Binding计数独立；全局unlocated_targets仅说明未归因范围，不按今日用途补猜，也不进入选中位置的确认摘要。

已知默认托管位置可以GET只读影响，配置改址和drain／stop仍沿原限制；其他无连接且无法识别的平台位置不自动获得该入口。

当前位置PATCH锁序为User/Key共享锁→Location排他锁，expected_revision防止配置覆盖；每次PATCH递增修订。扫描/登记在后续检查点拒绝旧快照，周期计划在后续唤醒进入blocked；预测/修复在自身的授权/内容屏障复核，并无位置级同步取消协议。完整缓存严格匹配位置修订，改名、人员和额度/TTL等变更也可能令旧缓存失配；预览按当前策略、内容和规格复用。来源管理授权与Task项目读取权限分开；停用位置阻止新的逻辑引用读图，包括缓存。来源变化不改写独立冻结媒体的归属，其读取仍复核项目权限与自身保存目标；不应把来源关联版本数当作必失效数。

`storage_location_change` 复用无副作用候选校验和维护聚合：POST预览签发五分钟HS256令牌，绑定当前actor/Key证明、位置配置及连接修订、标准化候选与关系指纹。聚合在同一SQL内计算计数及各表身份/相关状态的有序SHA256摘要，不以计数代替关系身份。保存目标指纹包含family／role、稳定主键、locator、可证明业务关联和state／status，当前Binding包含purpose／location／bucket／generation；同数量替换或关联变化仍需重新预览。不包含心跳／最近访问／重试时间或标注内容编辑，全局未定位覆盖摘要不参与选中位置的确认计数。PATCH必须提供令牌，并在配置锁下重算快照，完成期限及绑定比对后才更新配置；失败不提交业务变化。位置访问维护确认复用同一统计和摘要。成功业务审计脱敏记录差异、修订与两次盘点时间，失败状态由独立审计中间件保留。

位置锁并不覆盖所有Task/标注/冻结写入，不能在持有Location后阻塞等待这些owner锁；连接参与时遵循User→Key→Connection→Location。检查只代表提交时的SQL快照，配置CAS与owner各自的检查点共同约束逻辑更新。全异步owner静止、归档/恢复和物理回收仍须各自实现，令牌不提供这些执行许可。

## 引用图片的后台导出

`services/exporting/reference_media.py` 负责引用导出的固定选择、原请求授权证明和原图快照生命周期；`exporting/scope.py` 为大范围固定 ID 清单使用有界 SQL 参数。项目、批次与 Data Manager 在入队时封存私有 `export_authority`，包括原 API Key hash 与任务/Item/Asset/来源修订；`AsyncJobOut` 不返回该清单，幂等复投不替换它。

导出按现有项目 `EXPORT_ANNOTATIONS` 能力授权；显式任务选择继续逐项检查可见性。严格 reader 的默认任务权限不变，导出通过内部 task 授权回调复用读取基座，避免冒用管理员或把整项目导出缩成个人待办范围。实际读源时由 Celery hostname 决定 `worker:<hostname>` 锚点；完整缓存路径按同一逻辑身份读取，每份快照校验 SHA、大小和图片尺寸后再写包。

manifest 暂存于独占 TemporaryFile，避免同时打开两个 ZIP 写句柄；原图按块复制，逐项关闭快照，同时执行 entry、文件数和临时磁盘预算。标签、COCO、AAP 及掩码元数据共用包内路径，引用不经过平台桶签名器。引用包采用 job 隔离 cache key 并禁用 cache hit，托管缓存保持原合同。迁移 0211 将产物的完整格式列表扩展为 Text，旧值保留；已有长列表时拒绝降级，避免静默截断。

上传候选保存独立位置与 ZIP 内容回执。最终短事务持有 job 取消锁、账号/项目/成员、原 Key、批次可见性、任务/样本/Asset 及来源/位置/连接锁，复验后原子发布产物、完成结果及通知。普通失败或取消只清理本地临时资源；pending 与未确认对象保留独立责任，后续按保存归属和退休账本处理，不依赖整桶生命周期删除。图片媒体与 ZIP 的配对、缓存及恢复沿下文[图片冻结输入与交付](#图片冻结输入与交付)。后台图片预标按独立合同核验并投递临时快照；完整原图缓存通过独立账本、额度与租约管理，不复用临时输入的生命周期。

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

training 用途绑定提供新建训练对象的初始写入候选，已有归属不随它变化。版本的独立 `storage_ref` 保存已登记 Location/配置修订、绑定代次、端点、实际桶、版本前缀及物理身份；整个冻结前缀由同一 owner 负责，ZIP、保留图片/掩码、重试、读取和删除均沿用保存值。改变当前用途桶不重解释旧版本，端点身份变化则在 I/O 前拒绝。新分配位置可经用途映射的受控流程切换，固定版本继续保留其保存地址。

历史归属为空时，显式核验操作只取已登记的 legacy A 位置、实际平台端点及部署 training 桶，不通过当前环境桶猜测或在 GET 中 bootstrap。完整只读核验保存“当前保留副本”的证据，不改 snapshot/manifest，也不宣称历史创建身份。活动版本必须先取消并等构建退出；证据不足、对象变化或关闭不确定时不接管，原对象和责任保留。具体权限、预算及失败合同见[训练版本 API](/api/guides/training-datasets#固定存储归属与历史版本核验)。

实验、原生预览、逐样本评估和主动学习轮次同样拥有独立固定归属，分别限制 runs、previews、sample-evaluations、active-learning 根前缀；它们与输入版本及来源模型独立。历史接管共用完整 Reader、每进程容量及耐久维护 permit，取消等真实关闭，发布前重验权限／Key、全部记录和 legacy Location／Gate。预览按自身 count 限定文件，评估按 attempt+SHA 限定报告，主动学习按冻结 ordinal／派生 SHA 限定 PNG，不回源修复历史证明。Preview 原行和评估删除意图行承担清理责任；主动学习没有新增删除入口。参见[辅助输出合同](/api/guides/training-datasets#辅助产物归属与历史核验)。五类 owner 的新分配沿统一 training Binding 选择，已有归属不随它改变。

版本移除时将完整归属复制到独立删除意图；未知旧版本意图不按当前配置执行删除。离线恢复先验证源归属，再由具有表 owner 权限的工具在显式事务标记下，仅映射 bucket/storage_identity 并记录账本；prefix、端点和核验证据保持。该映射不能作为在线用途切换、跨后端迁移或物理回收资格，部署及恢复顺序见[存储恢复说明](/ops/deploy/storage-recovery)。

## 托管原件条件创建

`managed_original_creation` 是新数据集直传、ZIP、连接复制和目录 managed_copy 的共同创建 owner。0219独立账本先提交最终键、入口和预算、当前配置/绑定及权限快照；无业务/Location级联外键，避免未提交业务外键自锁。状态经准备、冻结输入、单次最终写入、实际回执、完整回读进入 verified；所有初始事实、冻结输入、写回执和终态均受数据库约束。来源与目标维护 Permit 分别登记，目标 operation 在首个物理 I/O 前独立提交。

直传 init 先预留最终 locator，只向 import 桶 `__aap_upload_staging__/` 签 PUT；该根为保留名称且不进入 managed 扫描。complete 完整条件读取后冻结源 SHA/大小/ETag/真实 VersionId，用单 part UploadPartCopy 准备不超过5GiB的暂存对象，最终 Complete 带 IfNoneMatch=\*；空对象走条件 PUT。版本化源的复制响应若缺少源版本回显，先确认中止本次 MPU，再按冻结版本与 ETag 流式读取、完整核对 SHA/MD5/大小并准备新的分块上传；关闭读取句柄后重核来源仍为当前版本，复制与回退共用原截止预算。显式错误的版本回显仍拒绝。普通导入分块计算完整摘要并以相同条件完成 MPU。禁止 CopyObject 封存与无条件降级；目标命名空间不签客户端 PUT。已有旧直传、扫描及历史原件不回填创建事实。

SDK 每步单次尝试，真实成功响应先独立持久化，再按真实非 null VersionId 或 ETag 条件完整回读、核对 SHA/大小；无真实版本时最终 HEAD 复查当前观测。调用者在原事务重验权限、配置、维护代次和精确内容后关联 AssetLocation.creation_id。业务回滚、去重或提交应答不明保留最终对象及独立责任；仅可 abort 明确的本次 UploadId。写入/落库应答丢失、构造或关闭不确定保留 unknown/中间状态与未收尾 Permit，不能靠 TTL、HEAD404、同 SHA 或后来的412提升为已创建。

退休断言在同事务捕获可空 creation_id，并通过 RESTRICT 保留原始证据；已有断言禁止补写创建关联。离线恢复对创建账本所有列计算受保护摘要，不重定位或伪造目标创建响应；目标 AssetLocation/退休定位可按既有白名单映射，但原回执仍明确属于源物理存储。恢复不自动继续未完成创建。

真实 VersionId 仅为本次响应证据；无版本不视为不可变版本。离线备份允许每键恰好一个当前版本的内容快照，拒绝额外历史版本及删除标记；恢复保留源回执，不把目标新版本改写成源版本。条件创建与客户端直传隔离建立平台新写入协议，不能证明生产专属权限或阻止外部写者；所有消费者的固定版本/内容读取与物理回收资格仍需独立完成。

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

上传头像的随机token对应独立 `AvatarUpload`，而非从 `User.avatar_ref` 推测当前桶。归属只在创建时从已登记的 avatars 绑定分配，保存端点、精确键、位置修订、绑定代次与物理身份；规范化WebP的SHA256和字节数与其一起不可变。读取和删除不比较当前avatars桶，端点不符则拒绝。现有匿名token URL和用户DTO不暴露内部归属。schema1继续解释旧默认位置；schema2保存全局S3连接位置的完整键，位于连接基前缀下`_aap_managed/avatars/`命名空间。物理地址按保存引用，凭据按当前连接读取，秘密轮换不要求历史配置修订等于当前位置修订。实际PUT返回VersionId时单独保存一次性版本回执，完整回读及后续读取／清理固定该版本；未版本化目标也支持，不虚构版本。

上传先提交pending事实，再在durable storage permit中写入并完整回读。真实线程结束后，重新锁定账号、复核Key原证明、原头像引用CAS及维护修订，才同事务发布ready新引用和旧对象retired意图。pending不按时间自动回收；进程崩溃、发布应答不确定或未确认物理收尾时保留待核对。历史token缺少独立记录时不从当前桶读删，也不以文件名或当前metadata自动接管，用户重新上传即可建立新归属。

匿名读取只接受ready记录，按精确键有界读取，关闭句柄并验证完整SHA/大小后再返回图片或304。替换、内置选择和清除先提交引用变化及退休意图，再清理原对象；退休行无任何用户引用时由cleanup队列重试保存位置的精确删除。失败保留责任，pending不进入自动清理。旧默认映射的离线恢复只允许更换bucket/identity，状态、账号快照和内容回执均受保护；现有九桶恢复尚不覆盖新代次或外部目标。头像新写入绑定可由超级管理员经真实托管探针和五分钟预览确认切换，报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件另有同等受控入口。历史用户引用缺少owner时阻止切换，不能从当前桶补造。

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

合格 ready 首轮只退休并提交，后续轮次才完整核验并删除其固定 UUID。旧代退休后若相同内容已由另一条合格 ready 承担，逻辑引用不再无限保留旧 UUID。扫描记录尝试时间，避免持续保留的前一批对象饿死后续对象；dry-run 不修改期限、状态或尝试记录。未认领上传预约超过 24 小时释放额度，但未知历史文件不随之删除。离线恢复只映射桶与物理身份，其余状态、代次、发布记录及内容事实保持；其保存地址沿下述annotations新分配合同使用，切换不扩大回收权限。

## 点云导出的独立媒体责任

`ExportMediaBundle` 在新 Job 事务内保存有界私有输入、规范原件事实、逻辑路径和不可变预期 SHA／大小清单；Job 只带 bundle UUID 与摘要。源读取与交付复制使用两槽 admission，客户端构造到真实关闭均受耐久 `export_media_<bundle UUID>` 许可保护。取消和普通软时限等待真实 I/O 排空；未知关闭保留许可与物理责任。

Worker 从冻结 DTO 构建 ZIP，不在序列化中重读业务行；最终按 Job／授权、Batch→Task、Dataset→Item 和场景轨迹父行检查并锁定输入，SQL 锁内不进行对象存储 I/O。预分配的 `ExportOutput.media_bundle_id` 与 bundle 首次发布在同一事务配对，固定登记时起 168 小时。重复投递不获新 claim，缓存验证保存位置的 ZIP 和全部媒体，签名不延期。

回收通过独立 bundle 责任行执行。已发布 latch 保护到共同截止；未发布尝试只在业务终态、可证明替换或租约／交付期限结束时退休。媒体与 ZIP 可分别重试，活跃／未知操作在候选 LIMIT 前排除并在锁内重查，未知额外对象保留。离线恢复只允许映射 bucket／storage_identity，冻结输入、逻辑路径、UUID、摘要和期限均保留；含不可改写媒体 URL 的 G1 缓存准确失效。此合同覆盖 KITTI、Point Mask、多相机 COCO 和 NuScenes；NuScenes 的全部相机沿准确 Task link，raw／map 沿辅助原件指针，纯格式与混合格式使用相同副本和缓存流程。输入合同区分新代，旧 G1 责任仍可验证、退休和恢复，新 NuScenes 请求不采纳旧部分归属。

### NuScenes 辅助原件

`DatasetAuxiliaryOriginal` 保存 verified Creation 对应的独立当前位置与内容事实，没有 Dataset／Item／Scene 业务级联。Item 的 raw 指针和 Scene 的地图指针核对同 Dataset、种类及来源合同；同 Dataset 的多 Scene 可以共享同一确切地图 owner。不同内容的 raw 与平台 PCD 不作为同一 Asset 的副本，也不建立可见辅助 Item。

Creation 的 dataset_auxiliary 范围使用专属 `__aap_auxiliary_originals__/{dataset UUID}/{creation UUID}/payload`，禁止普通直传且不被数据集目录扫描收集。导入器从单次有界原始字节产生 raw 收据与坐标转换后的 PCD；受控写入、完整回读及真实关闭后才关联业务。已登记媒体幂等重导先核对并复用，历史缺归属仅能由明确提供的原始数据重新创建，不能从当前桶推断。

辅助 resolver 只绑定原有导出权限下的准确 Task／主 Item／Scene，不授予新读取能力。冻结保存指针和格式事实，物理复制前后重新检查，最终发布再核整个输入。原件保留合同与七天交付回收分开：业务删除保留辅助 owner、创建历史及未知责任，当前不提供辅助原件自动物理删除。恢复仅映射辅助当前位置的 bucket／storage_identity，不改变原始 Creation、内容证据、token、filename、指针、冻结输入或已签期限。

### 图片冻结输入与交付

异步图片导出使用独立输入合同和 `image_media` 私有证明，复用 ExportMediaBundle 的物理复制、固定截止、一次 claim、ZIP 配对、缓存核验和退役账本。协议适配器按每次尝试显式选择，不在进程全局切换；LiDAR 历史合同继续独立校验。纯引用／空项目可持有空媒体清单，仍保留冻结输入与独立 ZIP 责任。

HTTP 先 SQL 计量数量／JSON，再有界固定 Project、精确范围与 Task、canonical 原件和格式所需业务行；引用授权从同一冻结事实捕获，不再先全量水合范围或连接配置。Worker 仅从冻结业务视图序列化，最终短锁重新核对父范围、所有活跃或待恢复标注、AAP 预测／轨迹及来源。物理读取、复制与 RLE 解析不在这些锁内执行。

托管图片沿已登记位置完整核验并复制原字节；引用保留准确 Key／节点／来源授权和严格嵌包，不跨 Job 缓存。统一 Task 逻辑路径覆盖各格式的图片、标签和 mask 引用；同源副本可去重，Task 绑定逐个核对。退役与恢复仍只处理保存清单与当前 bucket／身份，保留原期限、冻结事实及历史 URL，不提供原件自动回收或完整用途切换资格。

### 视频冻结输入与交付

视频异步导出使用输入 schema4、私有 `video_media` 证明和同一 ExportMediaBundle 生命周期，覆盖十种现有视频目标、项目／批次与空项目。媒体角色只能为 `video`，没有引用图片路径。canonical resolver 固定 DatasetAsset 或独立 TaskOriginal 及准确 Task 关系，完整身份相同的原件可共用物理副本，各 Task 绑定继续分别复核；同 SHA 不同归属不会合并。

HTTP 仅有界收集并固定身份、请求范围和业务输入，不取原件字节，也不信任历史视频 JSON。Worker 沿保存位置完整核对条件读取、SHA 与大小，在同一受许可保护的 stage FD 上运行本地受限 `ffprobe -count_frames`。实际解码帧数、fps、尺寸和时长与原件 SHA／大小形成 `metadata_receipt`，随 ZIP manifest、Video JSON 和 AAP 来源保存。探测不更新 Item 或视频派生 generation，不借用播放转码；范围与全部标注帧必须落在实际原件内，失败不补长或截短。

冻结事实包括采样／协作配置、原 scope、相关 Segment、原始标注、质检 Run／pairs 和 AAP 的预测及场景轨迹。freshness 由两侧分段全部有效原始标注计算，再从仍新鲜的 accepted 结果重放 same-track 合并；输出按分段核心区裁剪、合并、请求范围裁剪的顺序生成。最终按稳定锁顺序重采完整摘要：Project／Batch／Task 与 Dataset／Item 父行防插入，所有相关标注、Segment、Run 和 AAP 关系行防恢复或更新；授权、范围或内容变化均阻止 ready／缓存发布。对象存储 I/O 和解析不持有这些业务锁。

schema4 单件原件／副本5GiB、最多1000Task、唯一原件20GiB；旧媒体合同保持原有上限。冻结 JSON256MiB，实际源帧每视频100万、累计采样及额外精确帧100万，求值1000万次、解析工作估算1亿、展开字节估算256MiB，在分配插值结构前检查；Mask、栅格和 ZIP 另受[视频预算](/api/guides/export#视频独立媒体交付)约束。stage、探测进程和打包线程须真实收尾后释放访问许可与槽，未知关闭保留责任。

交付保存原视频字节，Task UUID 逻辑路径与物理 UUID 对象路径分开；`frame_outputs` 分别记录各格式编号及 YouTube-VOS 精确 Mask 帧。bundle 与 ZIP 配对发布，共用首次168小时截止和一次3600秒 claim，不因缓存、重投或恢复续期。缓存核验保存 ZIP 和全部副本，退休／恢复沿共享清单；恢复仅映射当前位置的 bucket／storage_identity，冻结输入、质检事实、逻辑路径、内容回执、期限与 ZIP 字节保持。旧嵌入链接不会自动迁移，准确关联的缓存失效后须新建导出，不改写旧 URL。

## 审计归档的受控新分配

`audit_archive_storage` 沿既有 `AuditArchiveOutput` 在候选提交前分配固定目标，连接→位置→绑定的锁序与预览确认串行。旧 schema1 保持初始解释；schema2 保存实际 Location、用途代次、端点／桶／完整受控键及物理身份。命名空间为连接基前缀下 `_aap_managed/audit-archive/`，公开逻辑键不变。封闭的 `managed_s3_target` 只接受头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件九个用途，不承担通用存储注册职责。

已经提交的 pending 不随绑定更新改变目标，ready 永久保留；分配新归档才采用当前绑定。I/O 前从保存位置捕获当前连接凭据，允许正常密钥轮换，不比较历史配置修订；端点、桶或受控前缀变化仍拒绝。历史记录保护位置和连接。物理读取沿用 HEAD／IfMatch／完整 SHA 与 JSONL 语义核验，不新增供应商版本历史合同。

分区服务只将捕获的目标传给既有读写流程，压缩生成、源内容和分区 OID 复核、DROP／ready／Job 同事务均保留。不新增清理器、删除权限或增量账目生产者。当前对象分类匹配准确端点、桶和完整键；外部未覆盖仍单列。现有九桶恢复在任何映射修改前拒绝 schema2，新事实存在时不得破坏性降级；应用回退保留归属，可用相同流程切回后续分配。

## 报告附件的受控新分配

报告附件与头像、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件和训练文件共用封闭九用途 S3 目标／探针实现，不增加配置注册中心。新 `BugAttachmentUpload` 在物理写入前持久化 pending，分配锁按连接、位置、用途绑定排序，与确认切换串行；已经分配的 owner 不因绑定更新改变。旧 schema1 保持 legacy、原键与代次解释；schema2 固定准确 Location／endpoint／bucket／完整受控键和物理身份，公开逻辑键继续与报告绑定。

物理访问从保存位置取得当前连接凭据，只比较准确物理身份，不要求历史配置修订等于密钥轮换后的修订。实际 PUT 返回的 VersionId 单独保存一次，后续 HEAD／GET／DELETE 固定该版本；显式 null version 可缺少响应版本头，非 null 回执须准确匹配。原权限、完整内容核验、期限、读许可和退休责任继续有效，未知写入保留 pending。

所有状态的附件都保护其位置不被重定向或删除。分类按 endpoint、bucket、完整 key 匹配，未覆盖外部目标不计入部署桶总量。九桶恢复只接受原 schema1／代次且无固定版本的范围，在改写任何物理映射前拒绝新位置或版本事实。历史报告无 owner 早已在业务入口拒绝；受控新分配不扫描或修复这些历史引用。

## 导出的受控保存目标

export 的物理归属由 `ExportOutput` ZIP 和 `ExportMediaBundle` 独立媒体共同承担，`ExportArtifact` 仅索引缓存。分配沿连接→位置→用途锁序捕获目标；schema1 保留旧解释，schema2 保存实际 Location、代次、endpoint、bucket、完整受控 key／prefix 和物理身份。媒体包分配后，配套 ZIP 继承同一保存目标，不在 Worker 中按当前绑定重新分配。

物理 I/O 在现有许可、真实执行与关闭边界内使用保存位置的当前连接凭据；最终发布继续复核授权、归属、维护状态和首次期限。两个 owner 的所有状态保护历史位置及连接不被改向或删除。旧 ready 和缓存复用不改写冻结输入、ZIP、清单或首次七天截止，既有库存、退休和版本／删除标记清理规则保持。

外部 ZIP 与媒体均使用自身 endpoint 签名；legacy 保留全局浏览器代理规则，不在签名后替换 Host。已经冻结的 URL 沿用原有效期和凭据撤销语义，不引入下载网关。受控探针同时验证此链实际需要的对象／版本列举能力。当前盘点仅覆盖已测量的部署目标，增量账目仍为部分生产者覆盖；本地九桶恢复对尚未覆盖的新定位在映射前拒绝。

## 导入暂存的保存位置

import新分配沿现有连接、位置、用途锁顺序固定实际Location／代次／endpoint／bucket和受控物理前缀；后续I/O只查看保存位置，凭据从该位置的当前连接取得，普通轮换不重解释地址。旧schema1继续明确legacy，新schema2接注册全局S3。完整key按UTF-8字节约束，物理前缀与Mask公开逻辑键分开。

PredictionInputLease保留既有contract与版本清单，两个帧入口及非图片secondary裁剪只将现有字节交给同一临时owner，不增加原件或Tracker证明。Creation的nullable staging_ref与最终原件归属独立，旧行保留完整configuration摘要并仅解释保存legacy暂存地址；当前import绑定变化不否定旧Creation。跨端点读取使用保存源client的条件完整GET，交原目标prepare_stream及原条件提交／readback；两个位置的许可保持到真实关闭。

MaskFormatUpload是签发前持久的独立责任，不随业务删除丢失地址；batch固定复制引用，preflight、执行与resume不重取当前绑定。旧无引用batch继续legacy及原SHA校验，不冒充保存历史endpoint或提供方版本。格式adapter、计划摘要与重试期限不变。只有PredictionInputs沿已有准确版本清理；直传和Mask远端输入没有新增GC或自动删除。

外部URL按保存端点及当前凭据签名，legacy才使用全局browser／ML映射。元数据捕获与实际client构建／DNS／关闭分离，物理阶段在已有StorageOperation内完成，最终授权锁只检查已捕获事实。所有保存状态保护位置及连接；统计和恢复分别遵守准确物理地址与已声明覆盖范围，不把数据库归属当成外部对象已备份。

## 数据集原件的保存位置

ManagedOriginalCreation继续承担唯一写入责任：新dataset／dataset_auxiliary分配把无密钥的原件地址放入configuration，完整旧配置摘要仍先校验。目标选择及Creation插入在同一短事务按Connection→Location→Binding复核，提交后释放锁再进行实际写入；目录复制在源位置行锁之前分配候选。import暂存与最终目标独立，Task只验证自己的annotations依赖，不因datasets改绑而失效。

DatasetAssetLocation和DatasetAuxiliaryOriginal的nullable storage_ref保存当前副本地址；旧NULL明确走原legacy解释，新记录与位置／桶／完整key逐项对应。不可变Creation是历史写入证据，不能替代当前副本定位。辅助原件沿原UUID后缀和来源合同，逻辑路径与Dataset改名分离。原来的空目录marker没有读取者，新建Dataset只保留扫描前缀元数据。

OriginalSource、ReplicaFacts、LocationFacts及辅助原件FACTS的字段集合保持，旧收据与冻结摘要无需改写。新引用的location_revision使用分配时修订；独立传输准备捕获当前连接凭据、白名单及实时修订，物理I/O只在既有维护许可内进行。原认证原件、预标、派生、导出和训练等消费者只透传该客户端上下文，业务算法和权限规则保持。当前物理endpoint／桶／受控根必须与保存地址相符，正常凭据轮换不改变内容身份。

Retirement复用现有位置／桶／key／storage_identity及保存修订；只读核验以当前登记端点作为候选，并用保存的物理身份严格核对后准备target，将引用保存到既有Verification配置。位置保护包含所有状态的Creation、当前副本、辅助原件和Retirement；这不开放物理回收。九桶恢复遇到新地址事实在修改映射前拒绝，禁止把未知新目标改解释为默认桶。

## 媒体缓存的保存位置与同源交付

`media_cache_storage`统一解释media-cache新分配的schema2地址，`managed_s3_target`只在物理阶段使用当前连接凭据。引用包含位置、分配时配置修订、用途代次、endpoint、bucket、完整物理prefix、storage identity和受控`key_prefix`；地址无密钥且不可变。实时目标须仍对应相同endpoint／bucket／受控根，普通密钥轮换不重写地址或内容事实。旧NULL和schema1明确沿原A解释，不从当前binding推断历史。

| 所有者                  | 保存责任                                                                                                                             |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `OriginalCacheEntry`    | 新增nullable `storage_ref`；来源位置修订、scope、配额、TTL和读租约保持原语义，缓存读取及清理核对保存目标。                           |
| `ThumbnailOutput`       | 独立保存目录预览、Dataset和Task普通缩略图的输入／目标、首次写期限、完整SHA／长度及提供方回执；业务行仅关联可空指针，不级联删除责任。 |
| `ImagePyramidOutput`    | 既有inventory、写入租约和状态合同保持，新增schema2目标；算法及瓦片规格不变。                                                         |
| `VideoDerivativeOutput` | 既有内容清单、尝试和清理责任保持，新增schema2目标；转码和采样合同不变。                                                              |

分配按Connection→Location→Binding短事务固定目标并保存owner，提交后才进行条件PUT和完整读回，不跨远端I/O持有管理／业务行锁。目录预览在最终来源锁前预分配并封存候选，最终只在原授权、内容和世代复核通过后关联；普通缩略图使用canonical原件reader取得实际保存位置，维持既有编码、尺寸和blurhash。失败或未知关闭留下独立责任，已有清理任务按保存位置收尾，不新建服务、调度或公开Worker签名。

`thumbnail_outputs`及金字塔／视频适配器在原有业务授权和完整输出核验后签发`media_cache_urls`短期能力。`media_cache_response`只交付准确输出／relative，支持GET、HEAD、单Range及If-Range；HMAC audience与登录分离，token包含保存引用摘要、SHA、大小和到期，不含endpoint或凭据。读取重核当前目标及Gate，远端Body／client与实际线程收尾继续由StorageOperation管理。新URL同源，不依赖B端点的浏览器可达性或CORS，也不交给A的`/minio`代理；旧schema1签名合同保持。

同源交付每进程最多4路物理传输，另允许最多64个请求异步等候、最长30秒；等待阶段不打开数据库许可或provider。取消／超时释放等待名额，实际传输名额仍在物理关闭与许可结算后释放，正常网格请求不会仅因瞬时超过4张而立即缺图。

历史统计分别列六类owner记录，不相加冒充对象数；平台占用按实际endpoint、bucket和完整key分类，外部B不计入默认A总量。building、failed、retiring和deleted等保存状态均继续保护位置，不能通过删除业务行消除责任。迁移0252只增列、表和约束，不补写旧定位；有新事实时拒绝破坏性降级，现有九桶恢复在修改映射前拒绝未覆盖的新地址。此功能不提供历史对象搬迁、跨目标完整恢复或物理退役许可。

## 标注文件的保存位置

`annotations_storage` 集中解释新 schema2 的封闭十字段引用：`schema / purpose / location_id / config_revision / binding_generation / endpoint_url / bucket / object_key / storage_identity / key_prefix`。purpose 固定 annotations，object_key 是受控根加既有逻辑 UUID 键；不携带密钥。受控根连同 `_aap_managed/annotations/` 最多896字符／920个UTF-8字节，完整 key 保持1000字符／1024字节上限。旧 schema1 和 TaskOriginal 的旧 NULL 保留原位置解释，不回填或跟随当前 Binding。

ManagedOriginalCreation 与 TaskOriginal 保留独立任务原件责任，另外三组沿 RasterMaskObject／Publication、CommentAttachmentUpload 和 GuideAssetUpload。新分配在短事务内按 Connection→Location→Binding 共享锁固定目标并持久 owner，提交后才打开 provider；确认切换按同序排他锁 Binding。Task Creation 的 configuration.schema2／original_ref 按 owner_kind 使用 annotations，datasets 分支保持；旧在途 Task 以自身封存配置和原地址核对，不与今天的 Binding 比较。TaskOriginal.FACTS 与 OriginalSource／ReplicaFacts／LocationFacts 字段合同保持，canonical Task 从实际 TaskOriginal 捕获保存地址及当前 transport。

三个 family codec 仅把新 schema2 交共享 helper；原逻辑键、SHA／长度、RLE portable reference、权限、发布状态和清理规则保持。物理 client 在既有 permit 内从保存目标打开并关闭，当前 endpoint、桶和受控根必须与保存地址一致，正常密钥轮换不改冻结事实和首次期限。评论附件保留首次90天期限，指南公开 entry 返回原逻辑键以保持四段 identifier；未知旧对象维持原拒绝，不从当前桶接管。

控制面复用真实条件 PUT／非空 MPU、完整读回及准确收尾探针与五分钟确认，history 分列五类责任记录并限定 Creation 的 task owner；不得相加作为对象总数。所有保存状态保护位置和连接，usage 按实际 endpoint、桶、完整 key 分类，外部 B 不计入默认 A。0253 增量扩展地址约束与 Task 保存引用，不回填旧记录；新事实拒绝破坏性降级。本地九桶恢复在首次映射写入前拒绝尚未覆盖的新 Task／Creation／RLE／评论／指南地址，即使回切 A 或已退休也不绕过。

## 训练用途的保存目标

training_dataset_storage 及四个薄 adapter 复用五组现有前缀 owner；TrainingStorageDeletion 仅承担 Version／Run 的既有删除责任。新 schema2 为原封闭11字段增加 key_prefix，family purpose 仍是 training-dataset／training-run／training-preview／sample-evaluation／active-learning，Binding purpose 为 training。prefix_for 保持逻辑前缀；Owner.prefix 是完整物理前缀，logical_prefix 纯派生并用于项目／owner 关系核对，resolve_ref.expected_prefix 仍表示完整物理前缀。根上限266字符／707 UTF-8字节，不做Unicode正规化，保留既有500字符列宽和完整key1024字节限制。

分配按 Connection→Location→Binding 短事务捕获当前目标并在原业务事务保存 owner，提交后才打开 provider。I/O sidecar 选择保存目标的当前凭据，实际client／body在对应访问许可内打开和关闭；禁止仅把外部引用转成bucket交给全局客户端。旧 schema1、NULL 与 verified-retained 证据不改写，历史 storage-pin 的 legacy A 候选及最终复核独立于今天 Binding。后续重试、续训、初始化、派生日志、模型来源和现有清理沿保存位置，首次期限与冻结身份保持。

控制面历史分列六类全部保存状态，未知旧对象维持原拒绝和显式核验，不作为全库接管门槛。普通PUT／非空MPU／完整SHA读回／精确LIST／批量DeleteObjects探针在SQL锁外执行，未知结果保留责任；不要求条件创建、空文件、版本化桶或版本历史列举。位置保护覆盖六类全部状态；盘点按endpoint、bucket和完整key分类，外部B不计入A。0254仅扩展现有地址约束并保护新schema2不可变，不回填旧行；新事实拒绝破坏性降级，九桶恢复在任何映射写入前拒绝未覆盖新事实。
