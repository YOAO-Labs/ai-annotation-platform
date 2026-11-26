# 0188 — 格式适配器扩展点与纯值对象契约

- **Status:** Accepted
- **Date:** 2026-11-24
- **Deciders:** 项目维护者选定格式适配器作为 M4b 扩展点并授权同步收口 M4c 边界
- **Supersedes:** —（沿用 [ADR-0186](0186-modular-monolith-and-on-demand-extraction.md) 单一写入 owner 原则与 [ADR-0187](0187-notification-channel-extension-point.md) `yoao-extension-manifest/v1` 通用清单与安全边界）

## Context

平台插件化计划（`docs/plans/1795366269_platform-pluginization.md` §4 M4b / M4c）要求在内置模块声明式装配（M4a）完成后，由维护者选定下一个扩展点并只收口其实际触及的 owner 边界。维护者已选定**格式适配器**。

既有格式适配接缝（`apps/api/app/services/mask_formats/`）已具备分阶段暂存上传、SHA-256 核验、预检收据、逐项持久日志与可恢复执行（`mask_format_imports` / `mask_format_uploads`），但存在两处阻碍插件化的边界问题：

1. **内置适配器契约直接暴露数据库会话与 ORM 对象**：`MaskFormatAdapter`（`apps/api/app/services/mask_formats/contracts.py`）的 `preflight_import`、`execute_import_item` 与 `preflight_export` 均直接接收 `AsyncSession` 和 ORM `Project`，内置适配器内部还直接读取 `annotation_row`、`task_row` 与 `db.get(DatasetItem, ...)`（跨越 Assets 边界直接读私表 ORM），并直接调用 `import_native_json_annotations`。这导致外部进程或跨语言实现无法对接同一契约。
2. **缺少外部格式适配器的注册、鉴权、隔离与生命周期管理**：`MaskFormatRegistry` 仅在进程启动时静态注册 22 个内置格式，无法在不改平台源码的前提下接入第三方或独立语言实现的外部格式适配器，也没有面向未来前端插件市场（M5）的统一展示投影与 `source_tier` 区分。

## Decision

### 1. M4c：从公开格式适配器契约移除 `AsyncSession` 与 ORM 模型

1. **纯值对象契约**：`apps/api/app/services/mask_formats/contracts.py` 不得导入 `sqlalchemy` 或 `app.db.models.*`。`MaskFormatAdapter` 仅接收纯值对象上下文 `FormatProjectContext` 与窄端口协议 `FormatHostPort`：
   - `FormatProjectContext` 仅携带 `project_id`、`data_type`、`owner_id`、`allowed_region_classes`。
   - `FormatHostPort` 仅暴露只读任务匹配、媒体尺寸读取、导出几何／Mask 快照读取、原生 JSON 预检（`dry_run=True`）与原子提交（`dry_run=False`），输入输出均为冻结 dataclass 或字节串。
2. **核心运行时持有全部数据库与权限权威**：新增 `apps/api/app/services/mask_formats/runtime.py`（`SqlAlchemyFormatHost`），集中承接数据库查询、`AssetsTaskMediaSummaryReader.current_image_summary`（闭合原 `image_adapters.py` 直读 `DatasetItem` ORM 的 Assets 边界缺口）、`current_video_summary` 与 `import_native_json_annotations` 原子提交。
3. **可执行守卫**：新增 `apps/api/tests/test_mask_format_boundary.py`，以 AST 检查强制 `contracts.py`、`registry.py`、`planning.py`、`adapters.py`、`image_adapters.py`、`video_adapters.py` 零导入 `sqlalchemy`、`app.db.models` 与数据库服务层。

### 2. M4b：外部格式适配器扩展点（`yoao-format-adapter/v1`）

#### 2.1 写所有权与表归属

- **Labeling** 作为格式适配器插件注册身份的唯一写入 owner，在 `yoao_labeling_v1` schema 下维护私有表 `yoao_labeling_v1.format_adapter_plugins`（撤销 `PUBLIC` 全部权限），普通运行账号仅持有固定 `SECURITY DEFINER` 函数的 `EXECUTE` 权限：
  - `list_format_adapter_plugins(boolean)` / `get_format_adapter_plugin(text)`：只读公开投影，不返回令牌密文，供管理接口、项目格式目录与未来 M5 前端市场读取。
  - `register_format_adapter_plugin(jsonb)`：校验并写入 `format_id`、`display_name`、`description`、`homepage`、`source_tier`（默认 `third_party`，绝不采信清单或 `/setup` 自报值）、`mode`（`direct` / `managed`）、`status`（`prepared` / `active` / `disabled` / `retired`）、`adapter_version`、`manifest_version`、`media_types`、`capabilities`、`option_schema`、`endpoint_url`、`auth_token_ciphertext`、`auth_token_sha256`、`manifest`。
  - `transition_format_adapter_plugin(jsonb)`：CAS `revision` 状态机（`prepared -> active`、`active <-> disabled`、`* -> retired`），退役时不可逆清空 `auth_token_ciphertext = NULL`。
  - `open_format_adapter_plugin_secret(text)`：仅在超管探测与实际调用活跃插件时按单个 `format_id` 打开令牌密文。
- `format_id` 匹配 `^[a-z][a-z0-9_-]{1,63}$`，禁止覆盖任意 22 个内置格式 ID。

#### 2.2 统一清单与安全边界（复用 ADR-0187）

- 托管安装档复用 `yoao-extension-manifest/v1`，设 `extension_point = "format_adapter"`、`protocol_version = "yoao-format-adapter/v1"`、`capabilities` 为 `{"import", "export"}` 非空子集，通用核心保留 `display_name`、`description`、`homepage`、`resource.memory_mb` 与固定 `@sha256:` 安装期绑定 `artifact`。
- `extension` 能力专属块（`extra="forbid"`）包含 `format_id`、`adapter_version`、`manifest_version`、`media_types`、`option_schema`（沿用抗 ReDoS 封闭 JSON Schema 子集）与 `allowed_egress_hosts`（通常为空列表）。
- 凭据加密使用独立失败关闭 Fernet 密钥 `FORMAT_ADAPTER_ENCRYPTION_KEY`（严禁复用 `settings.secret_key`；未配置时返回 503）。
- 出网客户端复用连接期 DNS 解析与 IP 绑定 SSRF 护栏（`FORMAT_ADAPTER_HOST_ALLOWLIST`、`FORMAT_ADAPTER_ALLOW_INSECURE_HTTP_HOSTS`），禁用重定向与环境代理。插件仅获发自身 `Authorization: Bearer <token>`，绝不获得数据库 DSN、用户 JWT、平台管理凭据或未授权项目数据。

#### 2.3 插件 HTTP 线协议（`yoao-format-adapter/v1`）

1. **`GET /health`**：返回 `200 {"status": "ok"}`（响应上限 4 KiB）。
2. **`GET /setup`**（Bearer 鉴权，响应上限 16 KiB）：返回 `protocol_version`、`format_id`、`adapter_version`、`manifest_version`、`display_name`、可选 `description`/`homepage`、`media_types`、`capabilities`、`option_schema`。
3. **`POST /import/parse`**（Bearer 鉴权，仅在 `capabilities` 含 `"import"` 时调用，请求上限 16 MiB，响应上限 32 MiB，超时 30s）：
   - 平台提交 `{protocol_version, format_id, media_type, staged_sha256, content_base64, mapping, options}`。
   - 插件返回纯规范化结果 `{protocol_version, items: [{media_path, width, height, losses, warnings, annotations, mask_objects}]}`。
   - **核心验证与提交权威不变**：平台在 `preflight_import` 中逐项通过 `FormatHostPort` 解析任务、校验尺寸、通过 `preview_native_json(dry_run=True)` 验证类别与几何并生成带摘要的 `MaskFormatPlan` 与收据；在 `execute_import_item` 中复核 `plan_digest`、`item_id`、`task_id`、插件仍处于 `active` 状态后，经 `commit_native_json` 走 `import_native_json_annotations` 原子落库与逐项日志，天然继承幂等、失败隔离、恢复与取消能力。
4. **`POST /export/render`**（可选，Bearer 鉴权，仅在 `capabilities` 含 `"export"` 时调用，请求上限 16 MiB，响应上限 32 MiB，超时 30s）：
   - 平台提交脱敏后的规范任务标注载荷（不含用户 PII、预签名链接或内部凭据），插件返回有界相对路径文件列表 `{protocol_version, files: [{path, content_base64}]}`；平台校验全部路径无越界与字节预算后写入导出包。

#### 2.4 动态接入与生命周期语义

- 项目格式目录（`GET /projects/{project_id}/mask-formats`）、导入预检／执行／恢复（`mask_formats` API 与 `run_mask_format_import` worker）以及导出预检均先查内置 `registry`，未命中时查询 `yoao_labeling_v1` 中 `status = 'active'` 的外部格式适配器并构造 `RemoteFormatAdapter`。
- 停用（`disabled`）或退役（`retired`）后：
  - 项目格式列表不再返回该格式；
  - 新导入预检、已签发收据的执行与恢复均拒绝调用插件；
  - 历史 `mask_format_imports` 记录与已提交标注完整保留。

## Consequences

- **正向**：内置格式适配器与外部格式插件共享同一纯值对象契约，消除 `AsyncSession`/ORM 与 `DatasetItem` 直读泄漏；任意语言零 SDK 即可实现格式适配器，并自动获得平台既有的暂存校验、预检收据、原子提交、失败隔离与可恢复执行。
- **负向**：导入解析路径经 HTTP 传递 base64 暂存载荷，单次载荷受 16 MiB 请求上限约束；超大视频包或 GB 级数据集仍适用内置适配器或后续流式暂存扩展。
