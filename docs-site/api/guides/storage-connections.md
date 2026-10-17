---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-17
---

# 存储连接器 API

管理外部存储连接器（S3 / OSS / MinIO 兼容、SFTP）并测试连通性，供数据集从连接器导入文件。设计背景见 [概念 · 存储连接器](/dev/concepts/storage-connections)。

所有端点前缀 `/api/v1/storage-connections`，需登录。**密钥永不在响应中回吐**——响应仅含 `secret_set: bool` 表达是否已配密钥。

> 若部署未配置 `CONNECTOR_ENCRYPTION_KEY`，涉及密钥的端点（create / test / 导入）返回 `503`。

## 端点一览

| 方法   | 路径                                          | 权限              | 说明                                         |
| ------ | --------------------------------------------- | ----------------- | -------------------------------------------- |
| GET    | `/storage-connections`                        | 登录用户          | 列表（超管看全部，普通用户看 global + 自建） |
| POST   | `/storage-connections`                        | 项目管理员 / 超管 | 新建连接器                                   |
| GET    | `/storage-connections/{id}`                   | 可见该连接器者    | 获取单个                                     |
| PATCH  | `/storage-connections/{id}`                   | 创建者 / 超管     | 更新名称 / 配置 / 密钥                       |
| DELETE | `/storage-connections/{id}`                   | 创建者 / 超管     | 删除（`204`）                                |
| POST   | `/storage-connections/{id}/test`              | 可见该连接器者    | 测试连通性                                   |
| GET    | `/storage-connections/allowlist`              | 仅超管            | 读主机白名单及来源                           |
| PUT    | `/storage-connections/allowlist`              | 仅超管            | 保存数据库覆盖                               |
| DELETE | `/storage-connections/allowlist`              | 仅超管            | 删除覆盖并恢复部署默认                       |
| GET    | `/storage-connections/deployment-sftp-preset` | 仅超管            | 读取部署主机 SFTP 快捷预设                   |

`global` 作用域的连接器仅超管可创建。

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

PATCH 中 `secret` 可省略——省略表示不轮换，沿用已存密钥；提供则整体替换。`kind` 与 `scope` 创建后不可变更。

config 拒绝未支持的键，避免拼写错误被忽略。S3 endpoint 不得内嵌用户名／密码；历史响应会移除 endpoint 的 userinfo，凭据应放入 secret。SFTP 密码和私钥必须二选一，并与 `auth_type` 一致；省略认证方式时可按唯一凭据推断，config-only 更新也会核对已存密钥。SDK/CLI 用法见 [账户、管理与运维](../../dev/sdk/account-and-operations)。

### 响应体（`StorageConnectionOut`）

```jsonc
{
  "id": "…",
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

先过主机白名单门禁，再用解密后的密钥真实建连（S3 list / SFTP 登录）。响应：

```jsonc
{
  "ok": true,
  "message": "连接成功",
  "sample_count": 12, // 探测到的样本对象数，可能为 null
}
```

`ok=false` 时 `message` 给出失败原因（白名单拒绝、鉴权失败、网络不可达等）。

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

返回 `job_id`，进度与结果通过 [AsyncJob](/api/guides/async-jobs) 查询。受 `DATASET_IMPORT_MAX_FILES` / `DATASET_IMPORT_MAX_TOTAL_BYTES` 上限约束，超限任务失败。

## 审计

`create / update / delete / test` 与 `allowlist` 更新均写审计日志，动作名见 [审计与通知 · AuditAction](/dev/concepts/audit-and-notifications#auditaction)。
