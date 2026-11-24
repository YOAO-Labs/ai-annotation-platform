# 0187 — 通知渠道扩展点与跨扩展清单分层

- **Status:** Accepted
- **Date:** 2026-11-24
- **Deciders:** 项目维护者确认采纳（M3a 设计决定，代理起草并按维护者意见定稿）
- **Supersedes:** —（沿用 [ADR-0186](0186-modular-monolith-and-on-demand-extraction.md) 的单一写入 owner 与按需拆出原则，兼容 [ADR-0180](../0180-installable-inference-plugins.md) 推理插件与 [ADR-0025](../0025-webhook-event-envelope-versioning.md) 事件信封）

## Context

平台插件化计划（`docs/plans/1795366269_platform-pluginization.md`）要求用第二个扩展点（通知渠道，首个实现为飞书私信）验证插件契约不只适用于推理服务。当前代码库已有两处既成基线需要衔接，而不是推倒重来：

1. **推理插件安装身份与清单绑定在 Inference 领域内**：`public.ml_backend_registry.extra_params->'plugin'` 由 `apps/inference/src/yoao_inference/sql/0388_inference_plugin_installation.sql:68,233` 及其后续 15 个推理插件围栏迁移（`0390`–`0395`、`0398`–`0403`、`0410`、`0412`–`0413`）维护；公开清单 `PluginImageManifest` / `PluginManifest`（`apps/_shared/inference_contract/src/yoao_inference_contract/plugin.py:154-212`）把通用元数据（`plugin_id`、`version`、`publisher`、`license`、`artifact`、`entrypoint`）和推理专属字段（`sdk_compatibility`、`models`、`pools`、`resource.device`、`resource.gpu_memory_mb`、`asset_manifest_sha256`、`managed_model_compatibility`、`allowed_asset_sources`）平铺在同一对象中。通知渠道插件不需要 GPU、模型池或 Python Plugin-SDK，无法直接复用推理扁平清单。
2. **Notifications 模块已有固定 owner 函数、偏好合同与最小权限发布器，但存在四处需在设计期闭合的缺口**：
   - **偏好合同封闭**：`apps/notifications/src/yoao_notifications/preferences.policy.json:1-40`、`apps/notifications/src/yoao_notifications/preferences_contract.py:23-82`、`apps/api/alembic/sql/0311_notifications_preferences.sql:1-277`（`bo0474` 切入 `yoao_notifications_v1` 命名空间，见 `apps/api/alembic/sql/0474_owner_namespaces.json`，保留既有 v1 `aap-notification-preferences/v1` 线协议标识）及 `packages/python-sdk/src/yoao/notification_models.py:45-65` 目前只允许读写 `in_app` 与 `toast`，`email` 预留未消费。
   - **既有 Redis outbox 不可混用**：`public.notification_delivery_outbox`（`apps/api/alembic/sql/0241_deactivation_notifications.sql:3-34`、`apps/notifications/src/yoao_notifications/sql/0484_notifications_terminal_outbox.sql:1-8`）以 `notification_id` 为单列主键，专用于账号注销与异步作业终态的 Redis Pub/Sub 交接，其隔离账号 `yoao_notification_publish_client` 启动时对权限做失败关闭核验（`apps/notifications/src/yoao_notifications/notification_delivery_postgres.py:110-151`）。该表无法承接 `1 事件 × N 渠道` 扇出，也不能把外部 HTTP 密钥暴露给仅持 Redis 权限的发布器。
   - **`in_app` 静音短路**：`apps/api/app/services/notification.py:136-137,196-197` 在 `in_app = false` 时直接返回而不写入事件记录，若沿用该顺序会导致“关闭站内信但开启飞书”失效或无去重键。
   - **可见性复核与自由文本泄露缺口**：`NotificationDeliveryReadPolicy`（`apps/notifications/src/yoao_notifications/delivery_read.py:65-229`）与 `apps/notifications/src/yoao_notifications/sql/0325_notifications_delivery_read.sql:49-306` 仅校验项目成员与导出权限；对无 `project_id` 的 `bug_report.*`（`apps/api/app/api/v1/bug_reports.py:321-334,456-486`）默认放行（fail-open），且未校验 `task.*`、`batch.*`、`feedback.*`、`annotation.comment_mentioned` 目标是否被删除或撤权（`task_access.assert_task_visible` 在 API 层，Notifications 角色当前无权直读 Labeling/Diagnostics 私表）。同时站内载荷在 `apps/api/app/application/task_review.py:399`（`reject_reason`）、`apps/api/app/api/v1/batches.py:381`（`reason`）、`apps/api/app/api/v1/bug_reports.py:332,466`（`resolution`/`snippet`）与 `apps/api/app/workers/export.py:347,351`（`download_url`/`error`）包含自由文本或原始链接，外部渠道必须经白名单投影脱敏。

本 ADR 在实施 M3b（平台侧）与 M3c（飞书插件侧）之前，冻结安装身份归属、跨扩展清单分层、以及通知渠道扩展点的协议、安全、数据模型与投递边界。

| 决策问题        | 选项                                                                                                      | 主要卖点                                                                                                                                   | 主要劣势                                                                                                      |
| --------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| 安装身份归属    | **A. 领域 owner 各自维护权威安装身份 + 共享便携契约（选用）**                                             | 保持 ADR-0186 单一写入 owner、调用方同事务原子性与按域凭据隔离；不破坏既有推理迁移与 GPU 围栏                                              | 跨扩展点全局列表与后续前端插件市场（M5）需通过各 owner 只读投影聚合，不能直接查单表                           |
| 安装身份归属    | B. 宿主级中心扩展注册表（单表 `plugin_installations`）                                                    | 跨领域统一列表与单一管理路由                                                                                                               | 破坏跨模块无私表读写约束，迫使重写 Inference 16 个冻结迁移或制造双写真值，且把不同安全域凭据集中到单表        |
| 安装身份归属    | C. 宿主目录表 + 领域绑定表双写                                                                            | 兼顾统一目录与领域语义                                                                                                                     | 引入跨表分布式同步与崩溃漂移风险；后续前端插件市场（M5）可由各 owner 只读投影聚合，无需提前引入第二套写入权威 |
| 清单分层        | **A. 通用核心信封 `yoao-extension-manifest/v1` + `extension` 能力专属块，保留 Inference v1 兼容（选用）** | 新扩展点无推理/GPU 污染，支持任意语言免 Plugin-SDK；不破坏 M1a/M1b 已验收推理镜像与收据                                                    | 过渡期存在 Inference v1 扁平清单与通用 v1 分层清单两套解析入口                                                |
| 清单分层        | B. 原地重构 `PluginManifest` 并迁移所有推理插件与 SQL                                                     | 立即统一成单一 schema                                                                                                                      | 高风险重写冻结 SQL、SDK 与已构建 OCI 镜像，收益与风险失衡                                                     |
| 外部投递 Outbox | **A. Notifications 在 `yoao_notifications_v1` 下新增独立外部渠道表（选用）**                              | 与既有 Redis outbox 及 `yoao_notification_publish_client` 完全解耦；原生支持 `1 × N`、独立 `operation_id` 去重、删站内信保留历史与凭据隔离 | 需维护第二条 Notifications 投递巡检与专用数据库角色                                                           |
| 外部投递 Outbox | B. 复用并改造 `public.notification_delivery_outbox`                                                       | 少建一张表                                                                                                                                 | 破坏单列 PK 与冻结的 `aap-notifications-delivery/v1\|v2` 契约，迫使 Redis 发布器接触外部 HTTP 凭据与出网      |

## Decision

### 1. 插件安装身份由领域 owner 各自维护，共享便携契约而非中心写表

1. **写所有权归领域 owner**：
   - **Inference** 继续作为推理插件安装身份的唯一写入 owner（`public.ml_backend_registry.extra_params->'plugin'` 与 `yoao_inference_topology_v1`），不迁移到宿主表，不改动已有 GPU 围栏、路由代次与训练发布外键。
   - **Notifications** 作为通知渠道安装/注册身份的唯一写入 owner，在 `yoao_notifications_v1` schema 下维护私有表（渠道表、用户地址绑定表、外部投递 outbox 表）。普通 API 与 worker 账号只持有 `yoao_notifications_v1.*` 固定 `SECURITY DEFINER` 函数的 `EXECUTE` 权限，不直读私表。数据库命名空间统一采用 `yoao_notifications_v1`（对齐 `bo0474` 命名切换），既有已冻结的 `aap-*` 线协议标识（含偏好 v1 与投递 v1/v2）保持不变；M3b 新引入的偏好 v2 线协议直接定为 `yoao-notification-preferences/v2`。
2. **共享契约而非共享表**：跨扩展点复用同一套便携生命周期语义（四状态 `prepared` / `active` / `disabled` / `retired`、CAS `revision`、固定 `@sha256:` OCI 摘要引用、禁止宿主 API 挂载 Docker socket、停用/退役保留历史、直连档与安装档双轨接入），并在安装/注册记录上统一记录平台权威的 **`source_tier`（`official` / `third_party`）**。
   - **来源层级防伪造边界**：`source_tier` 只能由平台安装或注册边界写入，默认值为 `third_party`，**绝不采信镜像清单或插件 `/setup` 的自报值**；仅当超级管理员显式确认官方来源（或后续前端插件市场接入经签名／固定摘要核验的官方插件源目录）时才记为 `official`。`source_tier` 仅用于来源区分、审计与前端市场筛选展示，**不豁免**任何隔离、鉴权、SSRF 防护或载荷脱敏门禁。
3. **跨领域视图服务于后续前端插件市场（M5）**：M3 先落地统一清单与第三方安全边界；后续前端插件市场（含官方源与第三方插件安装）通过聚合各领域 owner 暴露的只读安装投影直接读取 `plugin_id`、`version`、`extension_point`、`display_name`、`description`、`homepage`、`source_tier` 与 `status`，不引入第二写入权威。既有推理插件暂不改写 16 个冻结 SQL 迁移，进入 M5 只读聚合时由 Inference 只读投影补齐默认 `source_tier` 与展示字段映射。

### 2. 跨扩展点清单采用「通用核心 + `extension` 能力专属块」分层

定义通用扩展清单规范 `yoao-extension-manifest/v1`（用于托管安装档的 OCI 标签 `io.yoao.plugin.manifest` 及部署配置；直连档无需 OCI 清单，由 `GET /setup` 声明运行能力）：

#### 2.1 通用核心字段（跨扩展点共享）

| 字段               | 归属           | 约束                                                                                                                                                |
| ------------------ | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `manifest_schema`  | 镜像内         | 固定字面量 `"yoao-extension-manifest/v1"`                                                                                                           |
| `extension_point`  | 镜像内         | 枚举，首期支持 `"notification_channel"`，预留 `"inference"`、`"format_adapter"` 等                                                                  |
| `plugin_id`        | 镜像内         | `^[a-z0-9]+(?:[._-][a-z0-9]+)*$`，最长 128                                                                                                          |
| `version`          | 镜像内         | 1..128 非空规范字符串                                                                                                                               |
| `display_name`     | 镜像内（可选） | 1..64 字符纯文本（不含控制字符），面向未来前端插件市场与管理列表展示；省略时 UI 回退展示 `plugin_id`                                                |
| `description`      | 镜像内（可选） | 1..280 字符纯文本（不含控制字符），前端按纯文本转义渲染，禁止 HTML/Markdown 注入                                                                    |
| `homepage`         | 镜像内（可选） | 最长 255 字符的 `https://` 公开 URL，禁止 userinfo、控制字符、`javascript:`/`data:`/`http:` 及 loopback/私网主机；不参与网络访问或信任判断          |
| `publisher`        | 镜像内         | 1..255 非空规范字符串                                                                                                                               |
| `license`          | 镜像内         | 1..255 非空规范字符串                                                                                                                               |
| `protocol_version` | 镜像内         | 所实现的扩展点线协议版本（如 `"yoao-notification-channel/v1"`）；取代仅面向 Python Plugin-SDK 的 `sdk_compatibility`，确保任意语言无需 SDK 即可接入 |
| `entrypoint`       | 镜像内         | 固定非 shell 参数向量（沿用 M1b 规则：禁止大小写 shell 与 `python* -c`/捆绑 `-c`）                                                                  |
| `capabilities`     | 镜像内         | 非空去重词表，由对应 `extension_point` 校验合法取值                                                                                                 |
| `resource`         | 镜像内         | 通用容器预算，仅含 `memory_mb`（正整数），**不含** GPU 设备或显存字段                                                                               |
| `extension`        | 镜像内         | 按 `extension_point` 严格校验的能力专属对象（`extra="forbid"`）                                                                                     |
| `artifact`         | 安装期绑定     | 固定 `@sha256:` OCI 摘要引用；**禁止写入 OCI 镜像标签内**，由安装器核验 `RepoDigests` 后绑定（沿用 M1b `ImageManifest.bind_artifact()` 模式）       |

#### 2.2 能力专属字段划分

- **通知渠道专属（`extension_point = "notification_channel"`）**：
  - `channel_id`：`^[a-z][a-z0-9_-]{1,31}$`（如 `"feishu_dm"`），禁止保留名 `in_app`、`toast`、`email`、`external`。
  - `address_schema`：抗 ReDoS 的封闭 JSON Schema 子集（见 §3.2）。
  - `allowed_egress_hosts`：声明插件访问的外部 API 主机白名单（如 `["open.feishu.cn"]`），禁止 loopback、链路本地与私有网段，供运维审核与出网策略使用。
  - `capabilities` 词表：必含 `"deliver"`，可选 `"verify_address"`。
  - 展示字段复用通用核心的 `display_name`、`description`、`homepage`，不在 `extension` 内重复定义（无 OCI 清单的直连档注册由 `GET /setup` 提供 `display_name` 及可选 `description`、`homepage`，应用相同纯文本与 `https://` 校验规则）。
  - 清单内**禁止声明 `source_tier`**（由平台安装/注册记录独立维护，防第三方插件自报官方身份）。
- **推理专属（`extension_point = "inference"`，未来下一主版本路径）**：
  - 原 `PluginManifest` 中仅服务推理的 `sdk_compatibility`、`models`、`pools`、`compute`（`device` / `gpu_memory_mb`）、`asset_manifest_sha256`、`managed_model_compatibility`、`allowed_asset_sources` 逻辑上归属推理能力专属块。
  - **零破坏兼容规则**：M3 不修改现有 `apps/_shared/inference_contract/src/yoao_inference_contract/plugin.py` 中的 Inference v1 扁平清单与 `apps/inference/src/yoao_inference/sql/0388_inference_plugin_installation.sql`。读取 OCI 标签 `io.yoao.plugin.manifest` 时，若含 `manifest_schema: "yoao-extension-manifest/v1"` 则按 `extension_point` 分派；若无 `manifest_schema` 则走既有 Inference v1 解析路径。

### 3. 通知渠道扩展点契约（`yoao-notification-channel/v1`）

#### 3.1 两档接入、凭据隔离与出网防护

- **直连档（Direct Tier，M3c 首选验证路径）**：超管通过 `POST /api/v1/admin/notification-channels` 注册外部 HTTP 服务 URL 与独立共享 Bearer Token。平台主动请求 `GET /setup` 校验 `protocol_version`、`channel_id`、`capabilities` 与 `address_schema` 合法后落库。
- **安装档（Managed Tier，按需）**：镜像声明 `io.yoao.plugin.manifest`（`extension_point = "notification_channel"`），通过管理员 CLI 在独立非 root、`--cap-drop ALL`、`no-new-privileges`、无 GPU 容器中运行；插件自有第三方凭据（如飞书 App ID / App Secret）只挂载给插件容器，绝不提交给平台数据库或 API。
- **凭据存储与角色边界（明确单选）**：
  - 渠道 Bearer Token 在 `yoao_notifications_v1.notification_channels.auth_token_ciphertext` 中使用**专用失败关闭密钥 `NOTIFICATION_CHANNEL_ENCRYPTION_KEY`** 加密（参照 `apps/api/app/core/crypto.py:6-12` 独立 Fernet 密钥，**严禁复用 `settings.secret_key`**；未配置时拒绝注册或解密并返回 503）。
  - **不改动** `yoao_notification_publish_client`（既有 Redis 发布器账号不获得任何渠道表或函数权限，避免触发其失败关闭角色校验）。外部渠道投递使用独立数据库角色 `yoao_notification_channel_delivery_client`（仅持有 `claim_channel_deliveries`、`finish_channel_delivery` 及可见性复核函数权限）；`GET /setup` 与 `POST /verify-address` 仅由专门受控的管理/绑定能力打开对应单渠道令牌，不向普通 `application_role` 暴露批量读令牌接口。
  - 渠道退役（`disabled -> retired`）时**不可逆清除 `auth_token_ciphertext = NULL`**，可选保留 `auth_token_sha256` 审计摘要（对齐 `apps/inference/src/yoao_inference/sql/0388_inference_plugin_installation.sql:235-237`）。
- **出网安全与抗 DNS 重绑定**：
  - 既有 `_validate_ml_backend_url`（`apps/api/app/schemas/ml_backend.py:17,35-49`）只拦四个 loopback 字面量，不足以防 SSRF/DNS 重绑定。通知渠道出网客户端必须复用 `apps/assets-source/src/yoao_assets_source/connectors/host_guard.py:114-191` 同款策略：
    - 拒绝 URL 中的 userinfo、query 与 fragment；默认要求 `https://`，仅在运维显式配置 `NOTIFICATION_CHANNEL_ALLOW_INSECURE_HTTP_HOSTS` / 主机白名单（用于本地隔离容器或受控私网直连）时允许 `http://`。
    - 禁用重定向与环境代理（`httpx.AsyncClient(follow_redirects=False, trust_env=False)`，对齐 `apps/notifications/src/yoao_notifications/publication_client.py:86-90`）。
    - 在**连接时刻**解析 DNS 并拦截全部 `is_loopback`、`is_link_local`（含 `169.254.169.254`）、`is_multicast`、`is_unspecified`、`is_reserved` 及 IPv4 映射地址（`::ffff:127.0.0.1`），把连接固定到已校验 IP；RFC1918 私网目标仅在命中管理员显式配置的 `NOTIFICATION_CHANNEL_HOST_ALLOWLIST` 时放行。
- **插件权限上限**：平台发往插件的请求只带该渠道实例独立的 `Authorization: Bearer <channel_auth_token>`。插件绝不接收平台数据库 DSN、用户 JWT、平台管理 API Key 或任意用户列表。

#### 3.2 插件 HTTP 接口与严格错误语义

1. **`GET /health`**：返回 `200 {"status": "ok"}`，响应体上限 `4 KiB`。
2. **`GET /setup`**（需 Bearer 鉴权，响应体上限 `16 KiB`）：返回 `protocol_version`（`"yoao-notification-channel/v1"`）、`channel_id`、`display_name`（必填，1..64 字符纯文本）、可选 `description`（1..280 字符纯文本）与 `homepage`（`https://` URL，最多 255 字符）、`capabilities`、`address_schema`。`/setup` 返回的展示元数据均为不可信展示字段，不得包含或影响平台侧 `source_tier`。
   - **`address_schema` 抗 ReDoS 封闭子集**：顶层必须 `type: "object"`、`additionalProperties: false`、最多 8 个属性，序列化体积 `<= 8 KiB`；每个属性只允许白名单关键字 `type`（`"string"` / `"integer"` / `"boolean"`）、`title`、`description`、`minLength`、`maxLength`、`minimum`、`maximum`、`enum`（最多 32 个标量值）。**明确禁止** `$ref`、`$defs`、`pattern`、`patternProperties`、`allOf`/`anyOf`/`oneOf`/`not`、`unevaluatedProperties` 及嵌套对象/数组，彻底消除不可信正则回溯（ReDoS）。
   - **地址体积上限**：用户提交的 `recipient_address` JSON 序列化后必须 `<= 2 KiB`，在**绑定、验证（`/verify-address`）与投递（`/deliver`）三处统一校验**。
3. **`POST /deliver`**（需 Bearer 鉴权，附带 `X-YOAO-Delivery-Id: <delivery_id>`，连接超时 `3s`、读超时 `10s`，响应体硬上限 `4 KiB`）：
   - 请求体（封闭对象，JSON 体积 `<= 32 KiB`）：
     - `protocol_version`: `"yoao-notification-channel/v1"`
     - `channel_id`: string
     - `envelope`: **沿用 [ADR-0025](../0025-webhook-event-envelope-versioning.md) 的 5 字段顶层信封形状**（`event_version: "1.0"`、`event`、`delivery_id`、`occurred_at`、`data`）。`event` 取自 Notifications 允许外发的事件名（不耦合 `apps/api/app/schemas/event_envelope.py:12-21` 中的 8 项 Webhook 占位 `Literal`）；`delivery_id` 为本次投递行主键 UUID，全部重试保持不变。
     - `recipient_address`: 用户当前绑定的 JSON 对象（`<= 2 KiB`）。
     - `locale`: BCP-47 语言标签（默认 `"zh-CN"`）。
   - **载荷脱敏与显式白名单投影（硬约束）**：外部 `envelope.data` 必须按 `notification_type` 经显式字段白名单生成，只包含结构化定位元数据（如 `notification_id`、`target_type`、`target_id`、`project_id`、`project_name`、`task_display_id`、`reject_reason_type`、`actor_name`、`action_url`）。**严禁透传**评论正文、反馈正文、`task.rejected` 的 `reject_reason`、`batch.*` 的 `reason`、`bug_report.*` 的 `snippet`/`resolution`、`export.*` 的原始 `error`/`download_url` 或用户邮箱等敏感/自由文本字段。
   - **HTTP 状态码与 3 态响应映射（完整闭合）**：
     - `HTTP 200` + 合法 JSON `{"status": "delivered", "provider_message_id"?: str(<=256)}` → 终态 `delivered`。
     - `HTTP 200 / 400 / 422` + 合法 JSON `{"status": "permanent", "reason": str(<=128)}`，或非 JSON / 不可恢复客户端错误 `HTTP 400 / 404 / 410 / 413 / 415 / 422` → 终态 `failed_permanent`，**不重试**。
     - `HTTP 200 / 429 / 503` + 合法 JSON `{"status": "retryable", "reason": str(<=128), "retry_after_seconds"?: int}`，或 `HTTP 408 / 429 / 5xx / 3xx`、网络超时、连接失败、响应超 `4 KiB` 或 `200` 响应体非法 → `retryable`；优先尊重 `retry_after_seconds` 或 HTTP `Retry-After` 头（钳制在 `[5, 3600]` 秒），否则按指数退避推迟 `available_at`；达到 `max_attempts`（默认 6）后进入终态 `failed_exhausted`。
     - `HTTP 401 / 403`（渠道认证失败）：记录渠道级认证告警并将渠道进入短期冷却（如 60 秒后重试），按 `retryable` 处理以避免把全局令牌失效误判为用户地址永久错误；连续认证失败超过阈值时向超管健康检查暴露 `auth_error` 状态。
   - 幂等性：同一 `delivery_id` 在重试期间不变，插件须按 `delivery_id` 去重（飞书 `im/v1/messages` 原生支持 `uuid` 参数去重）。
4. **`POST /verify-address`**（可选，需 Bearer 鉴权，仅在 `capabilities` 含 `"verify_address"` 时调用，连接超时 `3s`、读超时 `10s`，响应体上限 `4 KiB`）：
   - 请求体：`{"protocol_version": "yoao-notification-channel/v1", "channel_id": "...", "verification_id": "<uuid>", "recipient_address": {...}, "locale": "zh-CN"}`。
   - 限速与同步 API 映射：在 Redis 维护独立的每 `(user_id, channel_id)` 滑动窗口限速（默认 `5 次/分钟`，不消耗投递 worker 的渠道 `rate_limit_per_minute`）；`delivered` 返回 `200` 并写入 `verified_at`，`permanent` 返回 `422`，`retryable` / 超时 / 上游不可用返回 `503` 与 `Retry-After`。

### 4. Notifications 数据模型、偏好合同升级与投递状态机

#### 4.1 偏好策略与契约升级（同步 `preferences.policy.json`、owner SQL 与消费方）

- `apps/notifications/src/yoao_notifications/preferences.policy.json` 新增策略字段（与 `yoao_notifications_v1._preference_policy()` 保持单一真值及一致性测试）：
  - `reserved_channel_ids`: `["in_app", "toast", "email", "external"]`
  - `external_channel_default_enabled`: `false`（所有外部渠道按类型**默认关闭**）
  - `external_channel_id_pattern`: `"^[a-z][a-z0-9_-]{1,31}$"`
  - `external_deliverable_types`: 明确列出允许外发的通知类型，**排除** `user.deactivation_requested`、`user.deactivation_completed`、内部迁移作业及未登记的 `feedback.reconcile_drift`。
- `public.notification_preferences.channels` JSONB 保持既有顶层键 `in_app` / `toast` / `email` 不变，外部渠道开关统一存于嵌套对象 `channels -> 'external' -> <channel_id>`。
- **协调升级至 `yoao-notification-preferences/v2`**（M3b 在同一变更波次同步更新 `apps/notifications/src/yoao_notifications/preferences_contract.py`、`yoao_notifications_v1.preference_command`、`infra/docker/notification-preferences-grants.sql`、`apps/api/app/api/v1/notifications.py:34-58,211-214`、`packages/python-sdk/src/yoao/notification_models.py:45-65`、OpenAPI 快照、`apps/web/src/api/notifications.ts` 与 `apps/api/tests/test_notification_preference_commands.py:176-181`）：
  - `GET /notification-preferences` 在每条偏好上新增封闭字段 `external_channels: dict[str, bool]`，仅投影 `status IN ('active', 'disabled')` 的已登记渠道（**绝不暴露未核验的 `prepared` 渠道**），且非 `external_deliverable_types` 条目返回空对象 `{}`。
  - `PUT /notification-preferences` 允许只提交 `external_channels`（放宽 `in_app` / `toast` 至少必填其一的旧限制，要求 `in_app` / `toast` / `external_channels` 至少提供一项），并拒绝为非 `external_deliverable_types` 类型或 `prepared`/`retired` 渠道开启外部开关。
  - SQL 在单次 `UPDATE` 中先合并顶层 `in_app`/`toast` 再以 `jsonb_set(..., '{external}', COALESCE(channels->'external', '{}'::jsonb) || v_external_patch, true)` 合并外部键，确保混合请求与并发标签页互不覆盖。

#### 4.2 `yoao_notifications_v1` 私有表与固定 SQL 能力

全部新表建于 `yoao_notifications_v1` schema 下并撤销 `PUBLIC` 全部权限，普通运行角色仅通过固定 `SECURITY DEFINER` 函数访问：

1. **`yoao_notifications_v1.notification_channels`**（渠道注册与安装权威表）：
   - `id uuid PRIMARY KEY`，`channel_id text UNIQUE NOT NULL`，`display_name text NOT NULL`，`description text`（可选，最长 280 字符纯文本），`homepage text`（可选，`https://` URL，最长 255 字符），`source_tier text NOT NULL DEFAULT 'third_party' CHECK (source_tier IN ('official', 'third_party'))`（由平台注册／安装入口写入，不从清单或 `/setup` 继承），`mode text CHECK (mode IN ('direct', 'managed'))`，`status text CHECK (status IN ('prepared', 'active', 'disabled', 'retired'))`，`revision integer NOT NULL DEFAULT 1`，`endpoint_url text NOT NULL`，`auth_token_ciphertext text`（退役时置 `NULL`），`auth_token_sha256 text`，`protocol_version text NOT NULL`，`capabilities jsonb NOT NULL`，`address_schema jsonb NOT NULL`，`manifest jsonb`（直连档 `NULL`），`rate_limit_per_minute integer NOT NULL DEFAULT 60 CHECK (rate_limit_per_minute BETWEEN 1 AND 600)`，`next_dispatch_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp()`，`consecutive_auth_errors integer NOT NULL DEFAULT 0`，`created_at` / `updated_at` / `disabled_at` / `retired_at`。
   - 停用与退役绝不删除行，保留历史与外键完整性；只读查询直接返回 `display_name`、`description`、`homepage` 与 `source_tier`，供未来前端插件市场消费。
2. **`yoao_notifications_v1.user_notification_channel_bindings`**（用户地址绑定表）：
   - `id uuid PRIMARY KEY`，`user_id uuid NOT NULL`，`channel_id text NOT NULL REFERENCES yoao_notifications_v1.notification_channels(channel_id) ON DELETE RESTRICT`，`address jsonb NOT NULL`，`locale text NOT NULL DEFAULT 'zh-CN'`，`verified_at timestamptz`，`created_at` / `updated_at`，`UNIQUE (user_id, channel_id)`。
   - 解绑即删除该绑定行，立即擦除用户外部地址 PII 且不级联影响历史投递记录。
3. **`yoao_notifications_v1.notification_channel_deliveries`**（外部渠道持久 outbox 与投递历史表）：
   - `delivery_id uuid PRIMARY KEY`（同时作为 ADR-0025 幂等键），`operation_id uuid NOT NULL`（业务事件/作业幂等键），`notification_id uuid`（可空、无级联删除，支持 `in_app = false` 独立外发与站内信清理不删历史），`channel_id text NOT NULL REFERENCES yoao_notifications_v1.notification_channels(channel_id) ON DELETE RESTRICT`，`recipient_id uuid NOT NULL`，`notification_type text NOT NULL`，`target_type text NOT NULL`，`target_id uuid NOT NULL`，`project_id uuid`（可空），`task_id uuid`（可空，记录评论/反馈关联任务以便复核），`envelope_data jsonb NOT NULL`（已白名单脱敏，不含正文与外部地址），`state text NOT NULL CHECK (state IN ('pending', 'delivered', 'failed_permanent', 'failed_exhausted', 'skipped'))`，`attempt integer NOT NULL DEFAULT 0`，`max_attempts integer NOT NULL DEFAULT 6`，`available_at timestamptz NOT NULL`，`worker_id uuid`，`lease_id uuid`，`lease_expires_at timestamptz`，`last_reason text`，`provider_message_id text`，`created_at`，`finished_at timestamptz`。
   - **强幂等去重约束（覆盖 `in_app = false`）**：
     - `UNIQUE (operation_id, recipient_id, channel_id, notification_type)`（对齐 `apps/api/alembic/sql/0241_deactivation_notifications.sql:23-24`，保证无论 `notification_id` 是否为空，同一业务事件/终态作业重试绝不生成第二条 `delivery_id`）。
     - 终态通知去重（`notify_terminal`）同步检查 `public.notifications` 与 `yoao_notifications_v1.notification_channel_deliveries`，避免 `in_app = false` 时重复入队。
   - **不快照 `recipient_address`**：投递前实时读取绑定表，解绑即时生效且历史表不滞留地址 PII。

#### 4.3 同事务入队、限速领取与投递前全量可见性复核

1. **同事务原子写入（移除 `in_app` 早退短路）**：
   - `NotificationService.notify` / `notify_terminal` 不再在应用层用 `_is_in_app_muted` 提前返回，而是在调用方业务事务内调用统一 `yoao_notifications_v1` 追加能力：若 `in_app = true` 写入 `public.notifications`；对每个 `status = 'active'`、用户已绑定、偏好 `external.<channel_id> = true` 且 `type` 属于 `external_deliverable_types` 的渠道同事务写入 `notification_channel_deliveries`（`state = 'pending'`）。业务事务回滚则站内信与外部 outbox 一并回滚，绝不外发。
2. **先限速再领租约与崩溃恢复**：
   - `claim_channel_deliveries` 在 SQL 内先筛选 `channel.next_dispatch_at <= now` 的渠道并按 `rate_limit_per_minute` 推进 `next_dispatch_at`（在**递增 `attempt` 之前**限速），再以 `FOR UPDATE SKIP LOCKED` 领取到期 `pending` 行、分配 `lease_id` 与 `lease_expires_at`（`lease_seconds = 60`，严格大于单次 HTTP 3s 连接 + 10s 读取 + 提交开销）。
   - 对租约已过期且 `attempt >= max_attempts` 的崩溃遗留 `pending` 行，领取前直接收口为 `failed_exhausted`，防止僵尸行永不终结。
3. **投递前四项权威复核（严格遵循 ADR-0186 跨模块只读能力边界）**：
   - 发起外部 HTTP 请求前，worker 在不直读外域私表的前提下按事件类型执行四项复核（任一不满足即记终态 `skipped`，不发 HTTP）：
     1. **渠道状态**：`notification_channels.status = 'active'`（否则 `skipped: channel_disabled`）。
     2. **账号与目标可见性**：一律校验 `yoao_identity_v1.account_profiles.is_active = true`，并按事件类型分派：
        - `task.*`、`feedback.*`、`annotation.comment_mentioned`：校验 `NotificationDeliveryReadPolicy` 项目权限，并调用 `yoao_labeling_v1` 发布的窄只读 `SECURITY DEFINER` 能力校验任务/评论仍存在且对接收者可见（不再在 Notifications 内直查 `public.tasks`）。
        - `batch.*`、`failed_prediction.*`：校验项目权限及 `yoao_labeling_v1` 窄只读能力确认批次/失败记录存在且角色可见。
        - `bug_report.*`（无 `project_id`，**绝不 fail-open**）：调用 `yoao_diagnostics_v1` 窄只读能力确认反馈记录仍存在且接收者为 `super_admin`、`project_admin` 或原提报人。
        - `export.*`、`job.*`：沿用 `NotificationDeliveryReadPolicy`（含导出权限与私有迁移作业超管围栏）。
        - 任一不满足记 `skipped: access_revoked`。
     3. **实时偏好**：`channels -> 'external' -> <channel_id>` 仍为 `true`（否则 `skipped: preference_disabled`）。
     4. **实时绑定**：`user_notification_channel_bindings` 仍存在有效记录（否则 `skipped: binding_removed`），取最新 `address`（校验 `<= 2 KiB`）与 `locale`。
   - 复核通过后调用插件 `POST /deliver`，再在独立短事务中以 `(delivery_id, worker_id, lease_id)` CAS 提交终态或退避重试。

## Consequences

正向：

- **坚持单一 owner 与最小权限**：Inference 与 Notifications 各自掌控强事务状态与敏感凭据；`yoao_notification_publish_client` 权限零变动；跨模块可见性复核走 Labeling / Diagnostics 窄 `SECURITY DEFINER` 能力，不破坏 ADR-0186 私表围栏。
- **验证多语言、免 SDK 扩展能力**：`yoao-extension-manifest/v1` 与 `yoao-notification-channel/v1` 彻底脱离 Python Plugin-SDK 与 GPU/模型概念，M3c 可以用任意语言以极简实现完成飞书直连接入。
- **隐私、幂等与安全由结构保障**：白名单投影阻断评论与自由文本外泄；`operation_id` 唯一约束使 `in_app = false` 下仍强去重；`host_guard` IP 固定与无正则 `address_schema` 堵住 SSRF/DNS 重绑定与 ReDoS；不快照地址与投递前实时复核满足 M3 全部 7 条验收不变量。
- **零存量破坏**：既有 `PluginManifest`（Inference v1）、16 个 Inference 冻结 SQL 迁移、既有 Redis `notification_delivery_outbox` 与 `yoao-notifications-publisher` 均无需改写。

负向：

- **M3b 需协调更新偏好协议消费方**：升级至 `yoao-notification-preferences/v2` 与 `external_channels` 字段需要同步更新 API、Python SDK 模型/目录、前端偏好类型、授权 SQL 与既有严格等值测试。
- **Labeling 与 Diagnostics 需新增窄只读可见性函数**：为避免 Notifications 越界读私表，M3b 需在 `yoao_labeling_v1` 与 `yoao_diagnostics_v1` 各补齐一个只读可见性能力并授权给 Notifications 投递角色。
- **清单解析过渡期双轨**：`io.yoao.plugin.manifest` 需按是否存在 `manifest_schema` 区分 Inference v1 与 `yoao-extension-manifest/v1`，直至推理插件在未来大版本迁入 `extension_point = "inference"`。

## Alternatives Considered（详）

**安装身份方案 B（宿主级中心扩展注册表 `plugin_installations`）**：若新建中心表承接推理与通知两类安装身份，Inference 的激活/停用/退役事务必须在 SQL 内跨边界锁定 `plugin_installations` 与 `public.ml_backend_registry` / `public.ml_backend_pool_members` / `public.trained_model_versions` / `public.project_model_releases`，直接违反 [ADR-0186](0186-modular-monolith-and-on-demand-extraction.md)“跨边界无私表读写”与“凭据按域隔离”硬约束；若不迁入 Inference，中心表沦为只有通知渠道的空壳抽象。否决。

**清单方案 B（立即重写 `PluginManifest` 并迁移推理 SQL）**：Inference 安装器与冻结 SQL 刚通过 M1a/M1b 验收；在第二个扩展点跑通前破坏 Inference 线格式与收据兼容性收益为负。保留 Inference v1 兼容并从新扩展点启用 `yoao-extension-manifest/v1` 风险更低。否决。

**Outbox 方案 B（复用 `public.notification_delivery_outbox`）**：现有表以 `notification_id` 为单列主键并受冻结协议 `aap-notifications-delivery/v1|v2` 约束，服务于仅持 Redis 权限的隔离 `yoao-notifications-publisher`。混用会破坏 `1 × N` 基数，还会破坏 `yoao_notification_publish_client` 的失败关闭权限检查并把外部 HTTP 密钥暴露给 Redis 发布器。否决。

**密钥方案 B（复用 `settings.secret_key` 加密渠道令牌）**：`settings.secret_key` 在开发默认值为静态占位串且用于 JWT/HMAC 签名，混用会扩大密钥泄露爆炸半径并违背 `apps/api/app/core/crypto.py:6-12` 的独立失败关闭加密规范。否决。

**偏好存储方案 B（把 `feishu_dm` 硬编码为 `apps/notifications/src/yoao_notifications/preferences.policy.json` 与 SQL 固定键）**：每新增渠道都要改 SQL 迁移，违背插件化目标。采用 `channels -> 'external' -> <channel_id>` 动态受控命名空间并校验渠道表存在性，可零迁移支持任意已安装渠道。否决。

## Notes

- 关联计划：`docs/plans/1795366269_platform-pluginization.md`（M3a / M3b / M3c）。
- 关联 ADR：[ADR-0015](0015-ml-backend-url-validation.md)、[ADR-0025](../0025-webhook-event-envelope-versioning.md)、[ADR-0180](../0180-installable-inference-plugins.md)、[ADR-0186](0186-modular-monolith-and-on-demand-extraction.md)。
- 关联现有代码：
  - `apps/_shared/inference_contract/src/yoao_inference_contract/plugin.py:154-212`
  - `apps/inference/src/yoao_inference/sql/0388_inference_plugin_installation.sql:68,233-237`
  - `apps/notifications/src/yoao_notifications/preferences.policy.json:1-40`
  - `apps/notifications/src/yoao_notifications/preferences_contract.py:23-82`
  - `apps/notifications/src/yoao_notifications/delivery_read.py:65-229`
  - `apps/notifications/src/yoao_notifications/notification_delivery_postgres.py:110-151`
  - `apps/notifications/src/yoao_notifications/publication_client.py:19,86-90`
  - `apps/notifications/src/yoao_notifications/sql/0325_notifications_delivery_read.sql:49-306`
  - `apps/notifications/src/yoao_notifications/sql/0484_notifications_terminal_outbox.sql:1-8,81-106,210-215`
  - `apps/api/alembic/sql/0241_deactivation_notifications.sql:3-34`
  - `apps/api/alembic/sql/0311_notifications_preferences.sql:1-277`
  - `apps/api/app/core/crypto.py:6-12`
  - `apps/assets-source/src/yoao_assets_source/connectors/host_guard.py:114-191`
  - `apps/api/app/schemas/event_envelope.py:12-47`
  - `apps/api/app/application/task_review.py:388-407`
  - `apps/api/app/services/notification.py:136-137,189-197`
  - `packages/python-sdk/src/yoao/notification_models.py:45-65`
- 采纳状态：已由维护者确认采纳并归档至 `docs/adr/archive/`，据此推进 M3b（Notifications/Labeling/Diagnostics owner SQL、偏好 v2、API/SDK 同步与投递 worker）→ M3c（独立语言最小飞书私信插件、假飞书 API 隔离验收，以及维护者真实飞书测试应用验证一次）。
