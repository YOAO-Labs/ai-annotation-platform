# 平台插件化 M3b 运行账本

## 2026-11-24：M3b 外部通知通道平台侧实现与隔离验收

**基线与范围。** 分支 `plugin/plugin-opt`，按已采纳的 [ADR-0187](../../../adr/archive/0187-notification-channel-extension-point.md) 分片交付通知通道扩展点平台侧全部能力：便携契约与出站安全边界（`yoao-extension-manifest/v1`、`yoao-notification-channel/v1`、ReDoS 安全 `address_schema`、白名单脱敏投影、连接期 IP 绑定 SSRF 护栏、独立 `NOTIFICATION_CHANNEL_ENCRYPTION_KEY` Fernet 失败关闭加密）、Alembic 迁移 `bo0510` 与三张私有表及最小权限授权脚本、`yoao-notification-preferences/v2` 动态外渠偏好、同事务发件箱入队与租约投递 worker、超管／用户 12 个 REST 接口、OpenAPI 快照、Python SDK 全量覆盖、Web 偏好类型与环境变量文档。所有运行写入均限定在临时可销毁容器，未触碰维护者日常 `annotation` 数据库或默认端口；未推送远端，未发布 PyPI。

### 分片提交与单测验证

- `620465398`（Slice 1 契约／SSRF／加密／HTTP 客户端）：`apps/notifications/tests/test_channel_contract.py` 与 `test_channel_host_guard_and_client.py` 全部通过（后续补齐 `_PinnedTransport` 原始 `httpcore` 流包装回归用例，共 **28** 项通过）。
- `e8eff1192`（Slice 2 迁移 `bo0510`／`yoao-notification-preferences/v2`／owner SQL／可见性函数／最小权限授权）：
  - `apps/notifications/tests/test_preferences_contract.py`、`test_channel_*.py` 与 `channel_delivery_worker` 共 **44** 项通过。
  - 可销毁 `postgres:16-alpine` 实测 `alembic upgrade head` → `alembic downgrade bo0509` → `alembic upgrade head` 往返无报错；`scripts/alembic_migration_policy.py` 输出单头 `bo0510`。
  - `infra/docker/notification-channels-owner-grants.sql`、`notification-channels-runtime-grants.sql` 与 `notification-channel-delivery-grants.sql` 在真实 PostgreSQL 16 执行通过。
- `7967df7f2`（Slice 3 发件箱入队／投递 worker／Celery Beat／REST API／OpenAPI 快照）：
  - 可销毁 `postgres:16-alpine` + `redis:7-alpine` 实测 API 通知全套用例（`test_notification_channels_api.py`、`test_notification_preference_commands.py`、`test_notification_append_boundary.py`、`test_notification_terminal_append.py`、`test_notifications.py`）：**65** 项通过，1 项既有跳过，0 失败。
  - `scripts/export_openapi.py --check` 与 `apps/api/tests/test_openapi_contract.py` 通过。
  - `apps/jobs/src/yoao_jobs/queue_routes.json` 与 `celery_app` 注册 `app.workers.notification_channel_delivery.relay_notification_channel_deliveries` 校验通过。
- `03ddaf4ff`（Slice 4 Python SDK／Web 类型与 Hook／环境变量文档／Changelog）：
  - `packages/python-sdk` 契约与目录检查（`test_notifications.py`、`test_cli_notifications.py`、`test_openapi_contract.py`、`test_discovery.py`）：**74** 项通过；12 条新路由全部归入 `api-coverage.toml` `covered`。
  - 离线发行包全套验证 `UV_OFFLINE=1 python3.11 scripts/test-sdk-distributions.py` 退出码 `0`：`sdk-repository` **49** 项、`sdk-installed` **1237** 项、`yoao_inference_contract` **30** 项、`yoao_protocol_v2` **29** 项、`yoao_training_contract` **32** 项全部通过。
  - Web 类型与单测：`pnpm --filter web exec tsc -b --noEmit` 与 `eslint` 通过，`useNotificationPreferences.test.tsx` + `NotificationPreferencesPanel.test.tsx` 共 **21** 项通过。
  - 文档：`pnpm docs:gen-env-vars`（58 节、680 项变量）与 `pnpm docs:build` 通过。

### 隔离环境全量 7 条不变量与受限角色实测

验收批次 ID `2ff9bdda83`（2026-11-24T08:30:40Z），证据回执保存在 `.artifacts/test-results/m3b-acceptance-2ff9bdda83.json`。环境组成：可销毁 `postgres:16-alpine`（执行 `alembic upgrade head` 并安装 `notifications_code`／`labeling_code`／`diagnostics_code` owner 授权、`app_runtime` 运行授权、`yoao_notification_channel_delivery_client` 仅函数投递授权，以及既有 `yoao_notification_publish_client` 授权）、`redis:7-alpine`、挂载 `app_runtime` 会话的真实 FastAPI 应用、连接 `yoao_notification_channel_delivery_client` 的真实 `NotificationChannelDeliveryWorker`，以及监听 Docker 桥接私网地址 `172.17.0.1:36867`（显式配置于 `NOTIFICATION_CHANNEL_HOST_ALLOWLIST` 与 `NOTIFICATION_CHANNEL_ALLOW_INSECURE_HTTP_HOSTS`）的真实 HTTP 替身插件进程。

实测期间额外捕获并修复一处真实传输缺陷：`NotificationChannelHttpClient._PinnedTransport` 原先将 `httpcore.Response.stream` 直接传给 `httpx.Response(stream=...)`，在真实 TCP 套接字读取响应体时触发失败；已改为 `_PinnedResponseStream(httpx.AsyncByteStream)` 包装并补充单元测试 `test_pinned_transport_wraps_raw_httpcore_stream`，修复后重跑全部实测通过：

1. **受限角色隔离与既有 publisher 未被污染**：`NotificationChannelDeliveryPostgresData.channel_delivery_policy()` 通过精确 10 函数 + 0 表权限自检（`yoao-notification-channel-delivery/v1`）；既有 `NotificationDeliveryPostgresData.delivery_policy()` 通过精确 3 函数 + 0 表权限自检（`aap-notifications-delivery/v1`）；`app_runtime` 查询三张私有通道表及调用 `claim_channel_deliveries`／`finish_channel_delivery` 全部返回 PostgreSQL `42501`；`yoao_notification_channel_delivery_client` 查询 `public.tasks`、`public.bug_reports` 及私有通道表全部返回 `42501`。
2. **SSRF 硬拦截**：向 `http://127.0.0.1:<port>`、`http://[::ffff:127.0.0.1]:<port>`、`http://169.254.169.254/latest/meta-data` 注册通道全部返回 HTTP `400`；向显式放行的私网地址 `http://172.17.0.1:36867` 完成真实 TCP `/setup` 与 `/verify-address`。
3. **不变量 1（业务回滚）**：业务事务内 `NotificationService.notify` 后 `rollback()`，发件箱 **0** 行、插件 **0** 次 HTTP 调用。
4. **不变量 2（插件不可用重试耗尽）**：先验证通道级 `next_dispatch_at` 限流（`claimed=0`）；再令插件返回 HTTP `503` + `Retry-After: 5`，两次尝试携带相同 `delivery_id`，最终进入 `failed_exhausted`（`attempt=2`）。
5. **不变量 3（永久失败不重试）**：插件返回 HTTP `422` `{"status":"permanent","reason":"invalid_open_id"}`，单次调用即落为 `failed_permanent`（`attempt=1`），无重试。
6. **不变量 4（投递前权限撤销／目标删除）**：`task.rejected` 改派任务、`bug_report.commented` 物理删除故障单（验证不 fail-open）、`export.ready` 无导出权限三种场景均在投递前被实时复核拦截为 `skipped: access_revoked`，**0** 次插件 HTTP 调用。
7. **不变量 5（停用与退役）**：通道 `disable` 后待发投递记为 `skipped: channel_disabled`（0 HTTP）；`retire` 后 `auth_token_ciphertext IS NULL` 且保留全部 8 条历史投递记录。
8. **不变量 6（重复入队 + `in_app=false` + 脱敏 5 字段信封）**：对 `in_app=false` 且开启 `external_channels={"feishu": true}` 的 `task.rejected` 用同一 `operation_id` 连续入队两次，写入 **0** 条 `public.notifications`、**1** 条发件箱记录；投递请求体为标准 5 字段信封（`event_version`、`event`、`delivery_id`、`occurred_at`、`data`），保留 `action_url` 与 `reject_reason_type`，原始 JSON 中零泄漏 `reject_reason` 自由文本。
9. **不变量 7（用户解绑）**：`DELETE /api/v1/notification-channels/feishu/binding` 立即物理删除绑定记录（剩余 `0` 行），待发投递记为 `skipped: binding_removed`（0 HTTP）。

验收结束后容器在 `finally` 中自动清理，复检 `docker ps -a --filter name=m3b-` 确认零残留。

### 明确未覆盖边界与既有无关问题

- **M3c 飞书真实集成未执行**：本轮仅使用本地 HTTP 替身插件验证平台侧契约与全部 7 条不变量；独立仓库／非 Python 语言飞书私聊插件与维护者真实飞书测试应用端到端投递留待 M3c。
- **前端通知通道管理／绑定界面未实现**：本轮更新前端偏好 API 类型与 `useNotificationPreferences` Hook 以兼容 `external_channels`；完整管理端与插件市场 UI 归入后续里程碑（M5）。
- **既有无关测试失败（按范围约束未改）**：
  - `apps/notifications/tests/test_realtime_event_contract.py::test_event_builder_rejects_invalid_uuid_and_datetime_without_rewriting[created_at-2026-10-24T24:00:00+05:30]`：当前环境中 `pydantic-core` 接受 ISO-8601 `24:00:00` 写法，与本次改动无关。
  - `apps/api/tests/test_alembic_drift.py::test_models_match_database`：既有 `yoao_fixture_v1.*` 与未注册私表漂移，未涉及本次新增的 `yoao_notifications_v1.*` 三张表。
