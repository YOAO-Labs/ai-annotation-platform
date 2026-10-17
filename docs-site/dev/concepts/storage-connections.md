---
audience: [dev]
type: explanation
since: v0.11.14
status: stable
last_reviewed: 2026-10-17
---

# 存储连接器（Storage Connections）

存储连接器让平台以**受控、可审计**的方式接入外部对象存储（S3 / OSS / MinIO 兼容）与 SFTP，并把其中的文件导入数据集。本页解释它的设计：密钥加密、SSRF 白名单、作用域模型与导入链路。

面向使用者的操作指南见 [用户手册 · 存储连接器](/user-guide/datasets/storage-connections)；逐端点契约见 [API · 存储连接器](/api/guides/storage-connections)；密钥/白名单的部署配置见 [参考 · 连接器安全](/dev/reference/connector-security)。

## 代码入口

| 关注点          | 位置                                                                                      |
| --------------- | ----------------------------------------------------------------------------------------- |
| 路由            | `apps/api/app/api/v1/storage_connections.py`（前缀 `/storage-connections`）               |
| 模型            | `apps/api/app/db/models/storage_connection.py`（表 `storage_connections`）                |
| Schema          | `apps/api/app/schemas/storage_connection.py`                                              |
| 业务服务        | `apps/api/app/services/storage_connection.py`                                             |
| 密钥加解密      | `apps/api/app/core/crypto.py`                                                             |
| SSRF 白名单门禁 | `apps/api/app/services/connector_guard.py`                                                |
| 源适配器        | `apps/api/app/services/sources/`（`s3.py` / `sftp.py`）                                   |
| 导入任务        | `apps/api/app/workers/dataset_import.py`                                                  |
| 迁移            | `alembic/versions/0086_storage_connections.py`、`0087_storage_connections_owner_scope.py` |

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

<!-- history: project-scoped storage connections were migrated to owner/global scope by migration 0087. -->

### config / secret 形态

```text
S3   config: { endpoint, bucket, region?, base_prefix?, use_ssl? }
     secret: { access_key, secret_key }

SFTP config: { host, username, port?, base_path?, auth_type }
     secret: { password }  或  { private_key, passphrase? }
```

校验与安全边界：

- **未知配置严格拒绝**：`_validate_and_sanitize_config` 严格限制各类型的白名单配置字段，传入未知 key 抛出校验异常，防止意外污染或脏配置落库。
- **S3 Endpoint 凭据隔离**：S3 `endpoint` 禁止在 URL 中内嵌用户名或密码（如 `http://user:pass@host`），创建与更新时校验拒绝；对外转 Out 响应时自动对历史残存 userinfo 执行脱敏。
- **SFTP 认证互斥与一致性**：SFTP 凭据中 `password` 与 `private_key` **严格二选一且互斥**，不得同时提供或均为空；配置项 `auth_type` 必须与所选凭据一致（未显式提供时由服务层自动推导并回填 `password` 或 `key`）。

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

```mermaid
flowchart TD
    Request["import-from-connection：冻结预算并提交作业"] --> Worker["run_dataset_import"]
    Worker --> Adapter["build_adapter：解密 / 白名单 / 路径沙箱"]
    Adapter --> Enumerate["按受理时 max_files / max_total_bytes 枚举"]
    Enumerate --> Asset["DatasetService.ingest_one：入库与去重"]
    Asset --> Added{"新增素材？"}
    Added -->|是| Tasks["DatasetTaskCreationWorkflow.append_items"]
    Tasks --> Labeling["LabelingTaskService：建题与项目计数"]
    Labeling --> Commit["当前文件提交"]
    Commit --> Media["提交后尽力派发媒体"]
    Added -->|重复或失败| Outcome["沿用原跳过 / 错误 / 回滚处理"]
```

原逐文件提交和取消检查不变。资产服务不再在 `ingest_one` 内代建 Task；素材行、追加任务和项目任务数仍绑定同一本地事务。连接器 worker 不自动推断 scene，时序数据集仍在原收尾位置校验；已提交文件不会因为后续校验失败自动回滚。

通用作业行只由 `services/async_job.py` 写入。受理时的 `settings_snapshot` 固定该次导入预算；历史作业缺少快照时，worker 先以 `FOR UPDATE` / `populate_existing` 重读作业，在锁内捕获预算并通过 `update_payload` 浅合并，提交后才进入枚举。无关 payload 字段保留，后续部署或系统预算变化不改写已固定快照。

取消仍在文件边界检查，不能中断已经开始的单个文件。HTTP 会立即将导入作业记为 cancelled；已投递 worker 仍可能先完成枚举及阶段进度，之后通过通用所有者为已经 cancelled 的行替换最终计数，不把进度强制改为 100。逐文件已提交素材与任务继续保留，尚未开始的文件计入 skipped。终态通知按既有去重规则保留一次，不承诺再次刷新 HTTP 取消时已经发布的计数；数据库提交、对象存储与 Celery 派发仍没有分布式原子性。

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
