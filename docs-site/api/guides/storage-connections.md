---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# 存储连接器 API

管理外部存储连接器（S3 / OSS / MinIO 兼容、SFTP）并测试连通性，供数据集从连接器导入文件。设计背景见 [概念 · 存储连接器](/dev/concepts/storage-connections)。

所有端点前缀 `/api/v1/storage-connections`，需登录。**密钥永不在响应中回吐**——响应仅含 `secret_set: bool` 表达是否已配密钥。

> 本地工作流使用 `CONNECTOR_ENCRYPTION_KEY`；独立 Source 使用 `ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY` 或对应的 `_FILE`，有效密钥需与导入进程一致。创建、轮换或解密凭据缺少密钥时返回 `503`。读取列表、名称更新和空补丁不需要密钥。

## 端点一览

| 方法   | 路径                                          | 权限                               | 说明                                           |
| ------ | --------------------------------------------- | ---------------------------------- | ---------------------------------------------- |
| GET    | `/storage-connections`                        | 项目管理员 / 超管                  | 配置列表（超管全部，项目管理员 global + 自建） |
| POST   | `/storage-connections`                        | 项目管理员 / 超管                  | 新建连接器                                     |
| GET    | `/storage-connections/{id}`                   | 项目管理员 / 超管                  | 获取可见连接器的配置                           |
| PATCH  | `/storage-connections/{id}`                   | owner 创建者 / 超管；global 仅超管 | 更新名称 / 配置 / 密钥                         |
| DELETE | `/storage-connections/{id}`                   | owner 创建者 / 超管；global 仅超管 | 删除（`204`）                                  |
| POST   | `/storage-connections/{id}/test`              | 可使用该连接器者                   | 测试连通性                                     |
| GET    | `/storage-connections/allowlist`              | 仅超管                             | 读主机白名单及来源                             |
| PUT    | `/storage-connections/allowlist`              | 仅超管                             | 保存数据库覆盖                                 |
| DELETE | `/storage-connections/allowlist`              | 仅超管                             | 删除覆盖并恢复部署默认                         |
| GET    | `/storage-connections/deployment-sftp-preset` | 仅超管                             | 读取部署主机 SFTP 快捷预设                     |

`global` 连接器对所有登录用户可用（测试与导入），`owner` 连接器仅创建者和超管可用；读取配置本身仍受上表的管理角色限制。创建 `owner` 连接器需要项目管理员或超管，创建和管理 `global` 连接器仅限超管。`owner` 创建者即使后来降为 employee 或 viewer，仍可管理自己创建的连接器。创建请求中历史兼容字段 `project_id` 会被忽略，新连接器的 `project_id` 始终为 `null`。

这六个公开端点继续由 API 认证并保留原响应模型。Assets Source 持有同一套连接器工作流：未配置远程服务时，API 在本地调用它并通过当前数据库连接执行原生能力；配置 `ASSETS_SOURCE_URL` 和 `ASSETS_SOURCE_TOKEN` 后，请求发送到内部 `/v1/connections/{list,read,create,update,delete,test}`。远程错误直接失败，不会改走本地实现。公开列表仍返回所有可见连接器，网关内部按有界页面读取并验证快照和游标，不把分页参数暴露给调用方。

创建、`PATCH`、删除和测试接受可选 UUID `Idempotency-Key`。省略时 API 会生成新的 UUID；成功响应都会回传实际使用的键，创建时连接器 ID 也等于该键。结果不确定时，调用方可用首次请求中的同一键和相同请求确认原结果；相同键配不同请求会冲突。SDK 与 CLI 提供显式传键的参数，客户端不会自动重试这些写入和测试请求。

配置列表和单项 GET 均要求管理角色，使用 API Key 时还需 `datasets:read`（或 `*`）。普通人员不能通过直接请求读取全局或先前自建的配置，返回403；项目管理员查询其他人的 owner 连接返回404。连接可被业务使用不代表使用者可以读取配置，密码和密钥始终不回显。

## 数据形态

### 请求体（创建 / 更新）

```jsonc
{
  "name": "aliyun-oss-prod",
  "kind": "s3", // "s3" | "sftp"，创建后不可改
  "scope": "owner", // "owner" | "global"，默认 owner
  "config": {
    /* 见下 */
  },
  "secret": {
    /* 见下，写入时加密 */
  },
}
```

`config` / `secret` 按 `kind` 区分：

```text
S3   config: { endpoint, bucket, region?, base_prefix?, use_ssl? }
     secret: { access_key, secret_key }

SFTP config: { host, username, port?, base_path?, auth_type: "password"|"key" }
     secret: { password }  或  { private_key, passphrase? }
```

PATCH 必填读取到的 `expected_revision`（正整数）。先 GET 或列表读取当前 `revision`，编辑时固定该值：

```json
{ "expected_revision": 1, "name": "new-name" }
```

缺少修订返回422；修订过期返回409，`detail.code=connection_revision_conflict`。取消编辑、重新读取再决定是否采用草稿，不能自动重试覆盖新版本。每次接受的编辑递增连接 `revision`；名称编辑不递增位置 `config_revision`。

配置实际变化或提交新凭据时，服务器先在行锁外验证候选的认证与目录列举，成功后重新核对账号、管理权限、原 API Key、修订、白名单和引用，最后原子保存。失败返回400，保留原名称、配置、密文和修订；验证期间发生竞争或撤权不会发布候选。PATCH 的 API Key 需要 `datasets:write`，测试需要 `datasets:read`，JWT 登录不受 scope 限制。创建仍只做参数、加密和目标白名单校验。

PATCH 中省略或传入 null 的字段不变更；`config` 提供时整体替换，`secret` 提供非 null 值时整体轮换，省略或 null 表示沿用已存密钥。`kind` 与 `scope` 创建后不可变更。

config 拒绝未支持的键，避免拼写错误被忽略。S3 endpoint 不得内嵌用户名／密码；历史响应会移除 endpoint 的 userinfo，凭据应放入 secret。SFTP 密码和私钥必须二选一，并与 `auth_type` 一致；省略认证方式时可按唯一凭据推断，config-only 更新也会核对已存密钥。SDK/CLI 用法见 [账户、管理与运维](../../dev/sdk/account-and-operations)。

### 响应体（`StorageConnectionOut`）

```jsonc
{
  "id": "…",
  "revision": 1,
  "name": "aliyun-oss-prod",
  "kind": "s3",
  "config": { "endpoint": "…", "bucket": "…", "use_ssl": true },
  "scope": "owner",
  "secret_set": true, // 是否已配密钥；明文绝不回吐
  "created_by": "…",
  "created_at": "2026-05-26T…",
  "updated_at": "2026-05-26T…",
}
```

## 测试连通性

```http
POST /api/v1/storage-connections/{id}/test
```

先过最新主机白名单门禁，再用解密后的密钥真实建连和列举，最多采样20项。DNS 单次等待5秒，远程列举单次等待35秒；超时请求停止等待，后台调用按 SDK 超时收尾，SFTP 另有到期关闭传输的保护。最多采样20项不代表整个远端目录已枚举完成。响应：

```jsonc
{
  "ok": true,
  "message": "认证与目录列举成功",
  "sample_count": 12, // 探测到的样本对象数，可能为 null
}
```

`ok=false` 时 `message` 返回固定错误分类（认证失败、无目录列举权限、目标不存在、超时、网络不可达或目标不可用等），不透传底层异常、签名 URL 或凭据。白名单门禁拒绝返回400；测试期间连接修订变化返回409，撤权拒绝返回结果。

匹配部署 `STORAGE_SFTP_ROOTS` 的 SFTP 连接复用严格读取的固定公钥与已验证地址连接；其他 SFTP 复制连接仍使用系统 known_hosts 和 RejectPolicy，不接受未知主机密钥。独立 Source 可通过 `ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE` 指定可读的 `known_hosts` 文件，未指定时使用进程的系统文件。

这项测试只证明 API 节点的认证与列举，不记录 read/range/write 能力，不替代存储位置的指定节点探测或浏览器预签名验收。

## 主机白名单（仅超管）

SSRF 防护的主机白名单，所有连接器目标必须命中白名单才放行。详见 [概念 · SSRF 防护与主机白名单](/dev/concepts/storage-connections#ssrf-防护与主机白名单)。

```http
GET /api/v1/storage-connections/allowlist
PUT /api/v1/storage-connections/allowlist
DELETE /api/v1/storage-connections/allowlist
```

```jsonc
// PUT 请求体
{ "entries": ["10.0.3.0/24", "192.168.1.50", ".aliyuncs.com"] }

// 响应；source 为 "database" 或 "environment"
{
  "entries": ["10.0.3.0/24", "192.168.1.50", ".aliyuncs.com"],
  "source": "database",
}
```

条目形态：CIDR、单 IP、精确域名、前导点后缀域名（匹配任意子域）。PUT 会规范化并去重后写入系统设置，覆盖部署 env `CONNECTOR_HOST_ALLOWLIST`；DELETE 删除数据库覆盖。loopback / link-local / 保留地址等**无条件拒绝**，白名单也无法放行。

PUT 必须明确提交 `entries`；`entries=[]` 表示拒绝所有目标。省略字段返回参数错误，恢复部署默认使用 DELETE。

## 部署主机 SFTP 预设（仅超管）

```http
GET /api/v1/storage-connections/deployment-sftp-preset
```

返回 `{ "enabled": true, "host": "deploy-sftp.internal", "port": 22 }`；未配置时 `enabled=false` 且 `host=null`。响应不包含用户名、凭据、路径或白名单规则，且不会绕过普通 SFTP 连接器的创建校验。

## 从连接器导入数据集

属于数据集端点，触发异步任务：

```http
POST /api/v1/datasets/{dataset_id}/import-from-connection   →  202 Accepted
```

```jsonc
{
  "connection_id": "…",
  "source_path": "batch-a/", // 相对连接器 base_prefix / base_path
  "recursive": true,
  "include_globs": ["*.jpg", "*.png"], // 可空
}
```

返回 `job_id`，进度与结果通过 [AsyncJob](/api/guides/async-jobs) 查询。受理时保存连接器配置和加密凭据快照；之后删除连接器不会删除已受理作业的快照。显式取消、作业终态、父数据集和当前租约仍会阻止迟到结果提交。导入受 `DATASET_IMPORT_MAX_FILES` / `DATASET_IMPORT_MAX_TOTAL_BYTES` 上限约束，超限任务失败。

## 审计

`create / update / delete / test` 与 `allowlist` 更新均写审计日志，动作名见 [审计与通知 · AuditAction](/dev/concepts/audit-and-notifications#auditaction)。

## 关联存储位置

每个连接自动关联一份存储位置，复用原 ID 关系、owner/global 授权及加密凭据；位置接口不返回 endpoint 或密钥。连接配置实际变化或密钥轮换递增位置修订，旧能力证据失效。连接被内容位置或用途绑定引用后，修改配置改向和删除返回409；名称更新与密钥轮换仍可用。无引用连接删除会清理关联位置及探测记录。参见[受控存储位置](./storage-locations)。
