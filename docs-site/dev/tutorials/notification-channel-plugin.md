# 通知渠道插件接入教程

本教程面向需要为 YOAO 接入飞书、钉钉、企微或 Slack 等外部消息通道的开发者。通知渠道插件遵循语言无关的 HTTP 契约 `yoao-notification-channel/v1`，无需引入 YOAO Python 代码或 `Plugin-SDK`。

官方参考实现（零依赖 Node.js 飞书私信插件）维护在 [YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/notification-channel/feishu-dm)（`plugins/notification-channel/feishu-dm`），可直接作为独立语言模板。

## 职责边界

YOAO 平台与渠道插件之间有严格边界：

| 职责         | YOAO 平台（Notifications owner）                                 | 通知渠道插件                         |
| ------------ | ---------------------------------------------------------------- | ------------------------------------ |
| 收件人与授权 | 计算收件人并在投递前复核账号有效、目标可见、偏好开启与地址绑定   | 不接触用户列表，不判断业务权限       |
| 持久化与重试 | 业务同事务写入渠道发件箱，提交后领取租约、限速派发、指数退避重试 | 对重复 `delivery_id` 保持幂等        |
| 消息载荷     | 按白名单构造脱敏 5 字段信封（不含评论正文或驳回自由文本）        | 将信封渲染为目标 IM 卡片或消息       |
| 凭据与出站   | 加密保存与插件之间的 Bearer 鉴权密钥，连接期绑定 IP 阻断 SSRF    | 自行保管第三方 IM 凭据并调用外部 API |

插件容器或进程不得获得 YOAO 数据库凭据、用户 JWT、平台管理令牌或任意用户目录。

## 两种部署档位

- **直连档（Direct Tier）**：在宿主机或内网服务器上直接运行插件进程（无需 Docker），管理员按 URL 与 Bearer 密钥注册。适合自建内网机器人与快速开发。
- **安装档（Managed Tier）**：将插件打包为非 root OCI 镜像，在镜像标签 `io.yoao.plugin.manifest` 中嵌入 `yoao-extension-manifest/v1` 清单，并以 `--read-only --cap-drop ALL --security-opt no-new-privileges` 等最小权限约束运行。

## 必须实现的 HTTP 接口

协议版本固定为 `yoao-notification-channel/v1`。除 `/health` 外，其余接口必须校验 `Authorization: Bearer <token>`（建议使用定长摘要恒定时间比较）。

| 方法与路径             | 鉴权   | 用途                                                                 |
| ---------------------- | ------ | -------------------------------------------------------------------- |
| `GET /health`          | 无     | 存活探测，返回 `200 {"status": "ok"}`                                |
| `GET /setup`           | Bearer | 返回通道元信息、能力列表与收件地址 JSON Schema                       |
| `POST /deliver`        | Bearer | 接收脱敏事件信封与单个收件地址，投递给外部 IM                        |
| `POST /verify-address` | Bearer | 可选（声明 `verify_address` 能力时实现），用于用户绑定时发送测试卡片 |

### `GET /setup` 响应示例

`address_schema` 必须使用封闭且无正则的 JSON Schema 子集（仅支持顶层 `object`、`additionalProperties: false` 与标量字段 `string` / `integer` / `boolean`）：

```json
{
  "protocol_version": "yoao-notification-channel/v1",
  "channel_id": "feishu_dm",
  "display_name": "飞书私信",
  "description": "通过飞书自建应用向已绑定 open_id 的平台成员发送脱敏任务与作业卡片通知。",
  "homepage": "https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/notification-channel/feishu-dm",
  "capabilities": ["deliver", "verify_address"],
  "address_schema": {
    "type": "object",
    "properties": {
      "open_id": {
        "type": "string",
        "minLength": 4,
        "maxLength": 128
      }
    },
    "required": ["open_id"],
    "additionalProperties": false
  }
}
```

### `POST /deliver` 请求与返回约定

YOAO 平台在请求体中发送固定 5 字段顶层信封（请求体不超过 32 KiB，客户端总超时 10 秒，因此插件应为单次投递设置不超过 9 秒的共享总截止时间）：

```json
{
  "protocol_version": "yoao-notification-channel/v1",
  "channel_id": "feishu_dm",
  "locale": "zh-CN",
  "recipient_address": { "open_id": "ou_example_user" },
  "envelope": {
    "event_version": "1.0",
    "event": "task.rejected",
    "delivery_id": "11111111-1111-4111-8111-111111111111",
    "occurred_at": "2026-11-24T09:10:00Z",
    "data": {
      "target_type": "task",
      "target_id": "22222222-2222-4222-8222-222222222222",
      "task_display_id": "T-123",
      "project_name": "RoadDet",
      "action_url": "/projects/p1/workbench/t123"
    }
  }
}
```

插件按外部投递结果返回对应状态：

- **投递成功**：`200` 与 `{"status": "delivered", "provider_message_id": "..."}`。对相同 `delivery_id` 的重试应返回缓存的成功结果，避免重复发送。
- **永久失败（不再重试）**：`422` 与 `{"status": "permanent", "reason": "..."}`。仅用于收件人地址无效、用户离职或不在应用可见范围等单用户永久错误。
- **可重试失败**：`503` 与 `{"status": "retryable", "reason": "...", "retry_after_seconds": 30}`（可附带 `Retry-After` 响应头）。外部 IM 限流、5xx、超时以及插件自身应用凭据或权限配置异常均应返回 `retryable`，避免永久丢弃消息。

## 快速运行飞书私信示例插件

1. 准备飞书企业自建应用并开通 `im:message:send_as_bot` 权限。
2. 检出 [YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins) 并以直连档启动零依赖 Node.js 插件（Node.js 20+）：

```bash
cd plugins/notification-channel/feishu-dm

export YOAO_CHANNEL_HOST=0.0.0.0
export YOAO_CHANNEL_PORT=8090
export YOAO_CHANNEL_AUTH_TOKEN="replace-with-strong-random-token"
export FEISHU_APP_ID="cli_xxxxxxxx"
export FEISHU_APP_SECRET="xxxxxxxxxxxxxxxx"
export YOAO_PUBLIC_BASE_URL="https://yoao.example.com"

node src/server.mjs
```

3. 确认 YOAO 后端已配置独立的 `NOTIFICATION_CHANNEL_ENCRYPTION_KEY`；若插件部署在内网或开发期使用 HTTP，将插件地址加入 `NOTIFICATION_CHANNEL_HOST_ALLOWLIST` 与 `NOTIFICATION_CHANNEL_ALLOW_INSECURE_HTTP_HOSTS`（详见[环境变量参考](/dev/reference/env-vars)）。
4. 超级管理员通过 REST API 或 Python SDK 注册并启用通道：

```python
from yoao import Client
from yoao.notification_models import NotificationChannelRegisterRequest

with Client(base_url="http://localhost:8000", api_key="...") as client:
    channel = client.notifications.register_admin_channel(
        NotificationChannelRegisterRequest(
            endpoint_url="http://172.17.0.1:8090",
            auth_token="replace-with-strong-random-token",
            source_tier="official",
            mode="direct",
            status="prepared",
        )
    )
    client.notifications.enable_admin_channel(
        channel.channel_id,
        expected_revision=channel.revision,
    )
```

5. 平台成员绑定个人 `open_id`、触发测试卡片并在通知偏好中开启 `external_channels` 下的 `feishu_dm`：

```python
from yoao.notification_models import (
    NotificationPreferenceUpdate,
    UserChannelBindingUpsertRequest,
)

client.notifications.upsert_channel_binding(
    "feishu_dm",
    UserChannelBindingUpsertRequest(
        address={"open_id": "ou_xxxxxxxx"},
        locale="zh-CN",
    ),
)
client.notifications.verify_channel_address("feishu_dm")
client.notifications.update_preference(
    NotificationPreferenceUpdate(
        type="task.rejected",
        external_channels={"feishu_dm": True},
    )
)
```

如需构建安装档非 root 容器镜像，参见 `YOAO-Plugins` 仓库中 [`plugins/notification-channel/feishu-dm/README.md`](https://github.com/YOAO-Labs/YOAO-Plugins/blob/main/plugins/notification-channel/feishu-dm/README.md) 的 Dockerfile 构建与受限运行命令。
