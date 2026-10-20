---
title: 账户、管理与运维
audience: [dev]
type: how-to
status: beta
last_reviewed: 2026-10-20
---

# 账户、管理与运维

SDK 收录稳定公开的账户与运维接口。调用前先用 `aap describe` 读取请求 schema 和操作约束；管理员权限、项目归属和资源版本继续由服务端核验。

| Python 命名空间                                    | CLI                                                | 主要用途                                       |
| -------------------------------------------------- | -------------------------------------------------- | ---------------------------------------------- |
| `auth`                                             | `auth`                                             | 登录、会话、注册、邀请接受、密码恢复与邮箱验证 |
| `account`、`avatars`                               | `account`                                          | 当前账户资料、偏好、头像、自助注销与任务事件   |
| `api_keys`                                         | `api-keys`                                         | 当前账户密钥创建、补丁、轮换与吊销             |
| `users`、`groups`、`invitations`                   | 同名                                               | 用户生命周期、分组、邀请与分页查询             |
| `health`、`ml_capabilities`                        | `health`、`ml-capabilities`                        | 根健康报告、协议与在线能力发现                 |
| `system_settings`、`audit_logs`                    | `system-settings`、`audit-logs`                    | 系统覆盖配置、审计读取与导出                   |
| `storage_connections`                              | `storage-connections`                              | 外部存储配置、主机白名单与连通性测试           |
| `admin_operations`、`ml_diagnostics`、`project_ml` | `admin-operations`、`ml-diagnostics`、`project-ml` | 预标清理、分析、后端探测与项目推理控制         |

## 身份与敏感信息

`Client.me()` 和 `aap me` 继续读取当前身份。`client.auth.login/refresh/logout_all` 返回的 JWT 不会自动替换客户端凭据或写入配置；保存返回值，并明确创建下一段会话的 Client。`refresh` 和 `logout` 需要 JWT；API key 不能代替 JWT 完成这些会话操作。`logout_all` 撤销当前会话并返回新 JWT。

登录、注册、重置密码等 CLI 请求使用 JSON 文件或 `--body -` 读取 stdin；密码也可用相应隐藏提示输入。创建和轮换 API key、生成邀请或临时密码时，文本输出需要 `--show-secret`，或明确选择 `--json`。这些响应中的凭据只应交给需要它们的调用方，避免把成功 JSON 存入普通日志。模型 repr 隐藏密码与令牌；显式 JSON 序列化保留 API 所要求的凭据。

邀请创建和 resend 都不发邮件；resend 会轮换或恢复 token，旧链接失效。`send-email` 才发送当前邀请。平台 `role` 与 `project_member_role` 分别表示账号角色和项目职责，不能互换。注册密码须为 8–128 字符，并包含大写、小写和数字。找回密码、验证码与验证邮件受服务端限流约束。

```bash
aap describe auth.login --json
aap auth registration-status --json
aap account preferences get --json
aap api-keys list --json
```

## 当前账户与偏好

偏好 GET 返回可扩展 JSON。PATCH 只发送需要改变的键，保留未知字段、null 和服务端合并语义；不要将完整 GET 结果重新提交。命名预设变更携带当前 `namedPresetsRevision`，409 后重新读取并核对草稿，不自动刷新版本重试。

头像上传使用 multipart，文件上限 2 MiB；服务端校验图片并转换为 WebP，替换会清理旧上传对象。公开头像下载使用 `upload:` 引用里的 32 位小写十六进制 token；下载原子替换目标文件。修改密码成功返回 204，不表示同时吊销全部 JWT/API key。自助注销有七天冷静期，可在期间取消；被管理员临时 suspend/reactivate 不会撤销已接受的申请。超级管理员申请及到期注销还会重新检查是否留下另一名启用管理员；完整行为见[认证与账号生命周期](../../api/guides/auth#自助注销申请)。

heartbeat 显式更新在线状态；SDK 不自动发送。任务事件只提交真实、已结束的时间区间，调用方保存稳定的 `client_id`，重试沿用原 ID。结果可能部分丢弃；CLI 输出完整回执后在 `discarded>0` 时退出 1。`queued_async=true` 只是排队证明。

## 用户生命周期与批量结果

Agent 优先使用 `users.query`、`groups.query`、`invitations.query` 的分页结果。兼容数组列表仍保留；项目管理员查询 active employee 候选时，`users.list` 可返回平台候选并忽略 `project_id`，需要项目范围时使用 `query`。可见用户不一定可停用或转移，核对 `is_lifecycle_managed` 等当前授权标记。

offboarding 先 preview，再原样提交 `preview_version` 与明确的转移选择。409 后重新预览并重新决定，不自动替换证据。平台角色预览用于核对影响，角色变更端点没有 CAS token，且仅超级管理员可调用。紧急停用会吊销 API key，重新启用不会恢复已吊销密钥。群组是全局目录；删除群组会清除用户关联。

批量邀请和分组 apply 可以部分成功。CLI 输出全部结果，`failed>0` 时退出 1；preview 仍退出 0。分组请求必须明确包含 `group_id`：UUID 表示分配，`null` 表示清除；省略不会被 SDK 接受。不要直接重复提交整批部分成功的邀请请求，应核对失败项和旧 token 是否已失效。

```json
{
  "user_ids": ["11111111-1111-4111-8111-111111111111"],
  "group_id": null
}
```

## 健康与能力发现

`client.health` 请求 origin 根路径 `/health*`，不带 `/api/v1`。CLI 只需配置平台地址，无需 API key。组件故障返回 503 JSON；SDK 仍返回 typed report，CLI 输出报告并退出 1，不重复探测这个已明确的降级结果。公开报告不含内部异常详情；超级管理员通过 `admin_operations.system_health()` 获取受限诊断。

```python
from ai_annotation import Client

with Client() as client:
    report = client.health.get()
    print(report.status, report.checks)
    vocabulary = client.ml_capabilities.protocol()
    print(vocabulary.version)
```

协议词表是静态定义；`instances` 在缺少缓存能力时可能请求后端 setup。SDK 不发送条件缓存头，取得 JSON 后再解析，不处理空 304 为有效结果。

## 系统配置与审计

系统设置仅超级管理员可用。GET 返回 `version`、有效值和来源元数据；SMTP 密码只返回 `password_set`。PATCH 省略和 null 都表示不修改，`smtp_password=""` 明确清除；恢复部署默认使用 reset。update/reset 推荐携带原 `expected_version`，409 后核对最新配置。

```python
from ai_annotation import ApiKeyUpdateRequest, Client, SystemSettingsUpdate

KEY_ID = "<key-uuid>"

with Client() as client:
    current = client.system_settings.get()
    client.system_settings.update(SystemSettingsUpdate(
        expected_version=current.version,
        video_chunk_warmup_lookahead=0,
    ))
    client.api_keys.patch(KEY_ID, ApiKeyUpdateRequest(expires_in_days=None))
```

API key 的 typed `patch` 区分省略与 null：`expires_in_days=None` 清除有效期；旧 `update(..., expires_in_days=None)` 保持省略语义。名称与 scopes 不接受显式 null。`system-settings test-smtp --yes` 会真实发送邮件到当前管理员，受每分钟三次限流约束。

审计列表允许项目管理员查看其当前负责项目的关联事件，过滤在计数和分页之前；超级管理员可查看全局。主对象归属优先于矛盾的上下文字段。归档、UTC 月汇总和导出仅超级管理员可用；游标翻页时 `total` 仍是完整筛选总数。`detail_key`／`detail_value` 必须成对提交，空字符串值有效。

```bash
aap audit-logs list --query filters.json --json
aap audit-logs monthly-summary 2026-10 --json
aap audit-logs export --out audit.csv --yes --json
```

导出是一次附件下载，会记录导出审计，最多 50,000 行，超限返回 413。JSON 文件含 `items`，CSV 含元数据头；在线保留期外查归档。归档分页当前会先加载完整月份，注意对象大小。下载失败保留已有目标文件。

## 连接器与 ML 运维

连接器读取遵循 global／创建者范围，密钥只写入不回读。config 只接受对应 S3/SFTP 的支持字段；S3 endpoint 不得包含 URL 用户名或密码，凭据放入 secret。SFTP 密码与私钥只能二选一，并与 `auth_type` 一致。主机白名单的显式空 `entries=[]` 会拒绝所有目标，省略 entries 不被 SDK 接受。

`storage_connections.test` 真实连接存储并写审计，HTTP 200 下仍须检查 `ok`。`ml_diagnostics.probe/observe` 也会发起网络请求，probe 的 bearer token 会发给指定 URL。ML 诊断保留管理员对内网、私有和新后端地址的探测能力；地址须为有效 HTTP(S) base URL，不接受 userinfo、query 或 fragment。smoke test 可能 reload/unload；`auto_unloaded` 不证明显存已释放，应再读 residency。

项目 warmup 要求该 backend 已启用于项目，且可能逐出共享权重。项目 disable 只删关联；全局后端配置、reload/unload 的权限与副作用不同。predict-test 返回临时推理；predict-frame 会保存 Prediction。帧推理与交互使用 multipart，JSON context/config 保持原协议；`context.mask_prompt_source` 还要求 `annotations:read`。

预标队列和摘要对项目管理员只返回其负责项目。bulk-clear 的清预测和 `reset_to_draft` 影响不同，按批次核对 `succeeded/skipped/failed`；重复 batch ID 可能重复处理，不应靠重发获得幂等。固定分析面板的 days 为 1–365，DuckDB 未就绪可能返回 503。CLI 确认表达执行意图，不会绕过权限、版本或正在使用中的资源保护。
