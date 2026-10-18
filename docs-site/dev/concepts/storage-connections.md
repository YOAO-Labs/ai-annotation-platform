---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 存储连接器（Storage Connections）

存储连接器让平台以**受控、可审计**的方式接入外部对象存储（S3 / OSS / MinIO 兼容）与 SFTP，并把其中的文件导入数据集。本页解释它的设计：密钥加密、SSRF 白名单、作用域模型与导入链路。

面向使用者的操作指南见 [用户手册 · 存储连接器](/user-guide/datasets/storage-connections)；逐端点契约见 [API · 存储连接器](/api/guides/storage-connections)；密钥/白名单的部署配置见 [参考 · 连接器安全](/dev/reference/connector-security)。

## 代码入口

| 关注点         | 位置                                                                        |
| -------------- | --------------------------------------------------------------------------- |
| 路由           | `apps/api/app/api/v1/storage_connections.py`（前缀 `/storage-connections`） |
| 模型           | `apps/api/app/db/models/storage_connection.py`（表 `storage_connections`）  |
| Schema         | `apps/api/app/schemas/storage_connection.py`                                |
| 公开网关       | `apps/api/app/services/assets_connections.py`                               |
| 连接管理工作流 | `apps/assets-source/src/aap_assets_source/connections_workflow.py`          |
| 密钥与目标校验 | `apps/assets-source/src/aap_assets_source/connectors/`                      |
| 白名单管理     | `apps/api/app/services/connector_guard.py` 与 Settings 原生能力             |
| 源适配器       | `apps/assets-source/src/aap_assets_source/connectors/`                      |
| 导入任务       | `apps/assets-source/src/aap_assets_source_import/`                          |
| 原生边界与回执 | `apps/api/alembic/sql/0217_assets_connections.sql`                          |

## 数据模型

`storage_connections` 表的核心字段：

| 字段         | 类型          | 说明                                                                                           |
| ------------ | ------------- | ---------------------------------------------------------------------------------------------- |
| `kind`       | `String(20)`  | `s3` 或 `sftp`（CHECK 约束）                                                                   |
| `config`     | `JSONB`       | **非密钥**配置（endpoint/bucket/host…），写入前经 `connectors.management.validate_config` 校验 |
| `secret_enc` | `LargeBinary` | Fernet 加密后的密钥密文，可空                                                                  |
| `scope`      | `String(20)`  | `global` 或 `owner`（CHECK 约束，默认 `owner`）                                                |
| `created_by` | `UUID`        | 创建者                                                                                         |

关键不变量：**明文密钥永不落库，也不出现在公开响应中**。`config` 与 `secret` 在 schema 层分离；`config` 是可读的连接参数，`secret`（access/secret key、密码、私钥）加密后只进 `secret_enc`，对外仅以 `secret_set: bool` 表达是否已配密钥。

六项连接管理操作复用 Assets Source 工作流。API 在本地模式调用同一工作流；独立模式经受信任的内部 HTTP 传入请求值，凭据由 Source 加密。Source LOGIN 精确执行 23 项能力，其中九项服务于连接管理，不能直接读取业务表。白名单与部署快捷项仍由 Settings 和部署配置持有。成功变更、审计和 `storage_connection_operations` 不可变回执原子提交，UUID 幂等键可找回首次结果。

> 历史字段 `project_id` 已弃用：当前作用域是「个人 / 全局」，新建连接器不再写入项目级归属。

<!-- history: project-scoped storage connections were migrated to owner/global scope by migration 0087. -->

### config / secret 形态

```text
S3   config: { endpoint, bucket, region?, base_prefix?, use_ssl? }
     secret: { access_key, secret_key }

SFTP config: { host, username, port?, base_path?, auth_type }
     secret: { password }  或  { private_key, passphrase? }
```

校验与安全边界：

- **未知配置严格拒绝**：`connectors.management.validate_config` 限制各类型的白名单配置字段，传入未知 key 抛出校验异常，防止意外污染或脏配置落库。
- **S3 Endpoint 凭据隔离**：S3 `endpoint` 禁止在 URL 中内嵌用户名或密码（如 `http://user:pass@host`），创建与更新时校验拒绝；对外转 Out 响应时自动对历史残存 userinfo 执行脱敏。
- **SFTP 认证互斥与一致性**：SFTP 凭据中 `password` 与 `private_key` **严格二选一且互斥**，不得同时提供或均为空；配置项 `auth_type` 必须与所选凭据一致（未显式提供时由服务层自动推导并回填 `password` 或 `key`）。

## 作用域模型

| scope    | 谁能建            | 谁能看            | 谁能改/删     |
| -------- | ----------------- | ----------------- | ------------- |
| `owner`  | 项目管理员 / 超管 | 创建者本人 + 超管 | 创建者 + 超管 |
| `global` | 仅超管            | 所有登录用户      | 仅超管        |

列表端点据此过滤：超管看全部，普通用户看「全局 + 自己创建的」。创建权限限 `SUPER_ADMIN` / `PROJECT_ADMIN`（`_MANAGERS`），其中 `global` 作用域仅超管可建。

创建者后来降为 employee 或 viewer，仍保留 owner 连接器的管理权；账号停用会拒绝后续操作。列表内部按条数和字节数分页，核对快照、总数及游标后返回完整公开结果。

## 密钥加密

密钥用 **Fernet**（对称加密，自带时间戳）加密：

- 加密：`connectors.crypto.encrypt_secret` 对排序、紧凑、ASCII 转义的有限 JSON 明文加密，结果存入 `secret_enc`。
- 解密：`connectors.crypto.decrypt_secret` 供探测、导入和 SFTP 保留凭据的认证方式校验临时使用，解密失败抛 `ConnectorCryptoError`。
- 密钥来源：本地工作流使用 `CONNECTOR_ENCRYPTION_KEY`，独立 Source 使用 `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` 或对应的 `_FILE`，需与导入进程保持同一有效 Fernet key，且与 JWT `secret_key` 隔离。缺少 key 时加解密路径返回 `503`；读取、名称更新和空补丁不需要 key。

生成 key：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

部署细节见 [参考 · 连接器安全](/dev/reference/connector-security)。

## SSRF 防护与主机白名单

连接器目标地址用户可控，因此**创建、替换 config、测试和导入**均校验主机白名单与 SSRF 规则（如阻止访问 `169.254.169.254` 云元数据）。名称更新、空补丁和仅轮换凭据不触发网络策略或出网检查。Assets 工作流使用可移植的 `connectors.host_guard`；API 白名单管理继续复用同一规则。

校验逻辑（`assert_host_allowed`）：

1. **空白名单 = 全拒**。未配置白名单时任何目标都拒绝，提示联系超管先配置。
2. **HARD_BLOCK 优先**：目标解析出的 IP 若命中 loopback / link-local / multicast / unspecified / reserved，**无条件拒绝**，即使白名单覆盖也不放行。
3. **白名单匹配**：DNS 解析目标 host 得到 IP 列表，逐个校验——落在 CIDR 条目内放行；或域名条目（精确 / 前导点后缀）匹配且 IP 为公网放行；否则拒绝。

白名单条目支持四种形态：CIDR（`10.0.3.0/24`）、单 IP（`192.168.1.50`）、精确域名（`oss-cn-hangzhou.aliyuncs.com`）、后缀域名（`.aliyuncs.com` 匹配任意子域）。

白名单存储在系统设置 `system_settings.connector_host_allowlist`：本地部署默认值来自 `CONNECTOR_HOST_ALLOWLIST`，独立 Source 使用 JSON 数组 `ASSETS_SOURCE_CONNECTOR_HOST_ALLOWLIST`。超管通过系统设置或 `PUT /storage-connections/allowlist` 写入数据库后覆盖 env，通过 DELETE 删除覆盖并恢复执行方的部署默认值。保存时统一规范化 IP、CIDR 和域名；显式空数组是 deny-all 的数据库覆盖。DNS 与探测完成后才短暂锁定当前策略并复核摘要，网络 I/O 不持有这些锁。

部署方可通过 `CONNECTOR_DEPLOYMENT_SFTP_HOST` 向超级管理员提供一个非敏感的 SFTP 地址快捷预设。它只复用现有连接器创建流程，不自动探测宿主机、不加入白名单，也不生成凭据。API 与导入 worker 必须能访问同一地址，并共享部署侧维护的只读 `known_hosts`。

## 导入链路

数据集从连接器导入由 `POST /datasets/{id}/import-from-connection` 触发，返回 `202` 与持久 `job_id`，进度走 [AsyncJob](/api/guides/async-jobs)。接受操作、Jobs 行和审计共同提交；执行进程轮询已接受操作，API 不再投递旧 Celery 导入任务。

```mermaid
flowchart TD
    Request["API：认证、范围、主机和路径检查"] --> Accept["原子接受：Assets、Jobs、Audit"]
    Accept --> Worker["独立进程领取租约"]
    Worker --> Manifest["持久保存预算内的源清单"]
    Manifest --> Copy["条件读取与不可变路径复制"]
    Copy --> Commit["原子登记：素材、Labeling、游标和计数"]
    Commit --> Outbox["持久媒体与通知派发记录"]
    Outbox --> Relay["API maintenance relay"]
    Relay --> Media["既有媒体与通知执行方"]
```

执行进程不导入 API、ORM、Celery 或 Redis。专用 LOGIN 仅有七项执行能力；Settings 和 Notifications 的读取/写入经各自 owner 能力，Jobs 自己持有通用作业状态。API 保留当前认证和 `datasets:write`，原生命令再次校验当前管理角色、连接器使用权和父数据集。

受理时固定预算及连接器版本，UUID 幂等键绑定用户的规范请求。相同请求重放保留原预算，不因之后修改系统设置而创建第二个作业。当前账户仍须有效；已经接受的执行保留原操作权，后续提交持续检查作业终态、显式取消、连接器版本与租约。

枚举清单持久后才开始复制。每个尝试使用不同 publication UUID，素材行、文件数、Labeling append 与项目计数、游标和派发记录在同一事务提交。数据库锁竞争留待重试，不变成逐文件失败。进程崩溃后，新的进程在旧租约到期后接续已提交游标。时序数据集仍在收尾检查已有 Scene；该检查失败保留已经提交的文件。

取消原子设置终态，保留已提交的部分。外部调用可能稍后才返回，但旧租约或终态结果不能登记新素材，因此取消后释放来源删除围栏不会产生迟到写入。S3 使用 ETag 条件读取；SFTP 使用已知主机指纹和目录/源身份检查。原始及不确定拷贝不自动删除。

媒体和实时通知继续使用既有执行方。持久派发记录可恢复提交到队列前后的中断；broker 消息可能重复，派发确认不代表媒体计算完成。同步 ZIP、旧前缀扫描和连接管理都复用 `apps/assets-source` 的独立工作流；媒体派生执行和其余 Assets 能力仍需迁移与验证，见 [ADR-0100](/dev/adr/0100-durable-connector-import)、[ADR-0101](/dev/adr/0101-assets-synchronous-zip-and-scan) 与[来源控制](/api/guides/assets-source)。

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
