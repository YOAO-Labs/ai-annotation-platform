# 推理插件机制升级实施计划（统一清单、非容器直连档与框架去硬编码）

## 1. 背景与目标

本计划承接 [ADR-0190](../adr/archive/0190-inference-plugin-generalization-and-direct-tier.md)，在推进训练器插件化 T2（[1795600466_trainer-pluginization.md](1795600466_trainer-pluginization.md)）之前，先将既有推理插件机制升级至与通知渠道（[ADR-0187](../adr/archive/0187-notification-channel-extension-point.md)）、格式适配器与训练器一致的架构口径：

1. **支持非容器运行（`execution_mode = "direct" | "container"`）**：先在 I1 闭合 D1 非本机仲裁直连档（裸机 `venv`/`conda` CPU 插件、无 Docker 本机独立卡与远端 GPU/CPU 推理服务），让非容器推理服务享有与托管插件一致的注册、能力核验、受控模型兼容性与评估能力审批、来源层级标识与安全退役围栏；本机受管 CUDA 仲裁直连档（D2）单独沉淀为配套受管生命周期资格运行器的可选切片 I4。
2. **统一 `yoao-extension-manifest/v1` 与全量推理能力词表**：消除主仓 `yoao-sdk` 与独立仓 `YOAO-Labs/Plugin-SDK` 的清单分叉，移除 `Plugin-SDK` 中误植的 `"train"` 能力，解除主仓 `Literal["inference", "detection", "managed_models"]` 限制并对齐全量 `yoao_task_contract.Task`，零破坏兼容旧扁平清单。
3. **清除全部 LibreYOLO 特例硬编码**：从通用推理安装观察器（`plugin_installation_observation.py`）和部署器（`plugin_deployment.py`、`_plugin_docker.py`）中移除 LibreYOLO 专属版本常量、插件 ID 判断与入口名假设。
4. **沉淀共享窄容器运行器与 Web 直连管理体验**：抽离 `ContainerRuntimePort`（供后续训练器 T2 复用），并在 Web 管理页支持直连推理插件的注册、状态查看与生命周期操作。

## 2. 不可变红线与范围边界

- **严禁原地修改冻结历史 SQL**：`apps/api/alembic/sql/0388_inference_plugin_installation.sql` 至 `0434_prediction_execution_routing.sql`（共 19 个插件相关迁移，含 `0388`–`0413` 16 个安装围栏迁移与 `0404`/`0433`/`0434` 伴生迁移）一律保持原样，所有数据库函数升级仅通过新增前向迁移（`bo0513`；若实施 I4 则另建 `bo0514`）以 `CREATE OR REPLACE FUNCTION` 实施，并完整保留 `SECURITY DEFINER`、`SET search_path = pg_catalog, pg_temp` 与最小权限角色授权。
- **严禁弱化容器档与本机 GPU 仲裁安全围栏**：`execution_mode = "container"` 必须 100% 维持不可变 OCI `@sha256:...`、64 位十六进制 `container_id`、GPU 资格报告与受控重启替换围栏；D1 远端/外部 GPU 必须核验 `health.gpu_info.device_uuid` 不与本机 `GPU_ARBITER_RESOURCES_JSON` 物理卡 UUID 冲突。
- **API 永不接触 Docker socket、永不 `import` 第三方插件**：直连档通过 Inference 自有异步 IP 固定出网守卫（`httpcore.AsyncNetworkBackend`，不跨模块依赖 `yoao-assets-source`）主动反查端点；容器档仍仅由管理员客户端 CLI 操作本地容器运行时。
- **不与训练器 T2 混改**：本计划只改推理插件与双仓共享清单/运行器底座；训练器 `yoao_learning_v1.trainer_plugins` 与 `training-supervisor` 在本计划完成验收后再启动。

## 3. 分阶段实施切片与验收标准

### I0：ADR-0190 确认与双仓统一清单契约对齐

- **目标仓**：`YOAO-Labs/ai-annotation-platform` 与 `YOAO-Labs/Plugin-SDK`。
- **关键改动**：
  1. 在 `YOAO-Labs/Plugin-SDK`（`packages/python/src/yoao_plugin_sdk/manifest.py`、`inference/__init__.py`、`inference/authoring/manifest.py`、`cli.py`、`packages/js/src/manifest.mjs`、`packages/js/src/index.d.ts`）修正 `extension_point = "inference"` 的统一清单模型与脚手架模板：
     - 移除非法能力 `"train"`，对齐完整推理能力词表 `{"inference", "detection", "segmentation", "interactive", "video_tracking", "ocr", "classification", "obb", "pose", "doc_layout", "managed_models", "managed_evaluation"}`。
     - 放宽 `manifest.py:192-196` 与 `packages/js/src/manifest.mjs:70-103`，使 `direct` 模式下 `entrypoint` 与 `resource.memory_mb` 可选（`container` 模式仍必填）。
     - 补齐强类型推理 `extension` 专属块（`models`、`pools`、`compute`、可选 `sdk_compatibility`、`asset_manifest_sha256`、`allowed_asset_sources`、`managed_model_compatibility`），保留 `source_tier` 禁止自报守卫，并更新 `cli.py:121-123` 脚手架模板。
     - `inference/authoring/manifest.py` 支持同时解析新版 `yoao-extension-manifest/v1` 与旧扁平清单。
  2. 在主仓 `packages/python-sdk/src/yoao_inference_contract/plugin.py` 新增 `yoao-extension-manifest/v1`（`extension_point = "inference"`）解析与规范投影模型、扩展 `capabilities` 词表、新增 `execution_mode: Literal["container", "direct"]`、`source_tier: Literal["official", "third_party"]`、`display_name` / `description` / `homepage` 字段，并解耦 `PluginResourceRequirements` 与 `PluginInstallation.validate_binding` 对非仲裁直连 GPU（`gpu_resource_id = None`）的硬约束（向后兼容存量 `PluginManifest` 与 `PluginLaunchReceipt`）。
- **验收标准**：
  - Python `3.10` + 当前 Python 环境、`Plugin-SDK` Python/Node/TypeScript strict 与主仓 `yoao-sdk` 契约单测全绿。
  - 新增双仓 JSON Schema 对等校验测试，证明 `yoao_plugin_sdk` 与 `yoao_inference_contract` 的推理清单与能力词表零漂移，且存量旧扁平清单 100% 兼容解析。

### I1：前向 SQL 迁移（`bo0513`）与 D1 非容器直连档闭环

- **目标仓**：`YOAO-Labs/ai-annotation-platform`。
- **关键改动**：
  1. 新增前向 SQL 迁移 `apps/api/alembic/sql/0513_inference_plugin_direct_mode.sql` 与 `apps/api/alembic/versions/0513_inference_plugin_direct_mode.py`（`bo0512 -> bo0513`）：
     - 通过 `CREATE OR REPLACE FUNCTION` 更新 `yoao_inference_topology_v1.{prepare_plugin_installation, command_plugin_installation, set_plugin_asset_approval}` 与 `yoao_inference_routing_v1.prediction_scope_reserve`（`0434` `uses_gpu` 判定）：
       - 接受 `execution_mode`（`container` / `direct`）、`source_tier`（`official` / `third_party`）与展示元数据并落入 `extra_params->'plugin'`；当 `gpu_resource_id IS NULL` 时写入 `vram_budget_mb = NULL`。
       - `container` 档保持 64-hex `container_id`、OCI `@sha256:` 与资产审批严格校验 100% 不变。
       - `direct` 非仲裁档（`gpu_resource_id IS NULL`）支持经实时探活证据激活（`active`）、`disable`、`quiesce`（若端点声明 `terminal_drain_endpoint` 则要求终端排空证明；否则校验成员排空与活动路由/跟踪范围清零后通过）与安全 `retire`（校验 `active_releases == 0`、`frozen_executions == 0` 并不可逆擦除运行态凭据）。
       - `0434` `uses_gpu` 仅针对本机受管 GPU（`gpu_resource_id IS NOT NULL OR vram_budget_mb IS NOT NULL OR (execution_mode = 'container' AND device <> 'cpu')`），避免 D1 远端 GPU 触发 `fixture_prediction_gpu_unqualified`。
  2. 解除资产审批、GPU 调度与控制面的误阻断：
     - `packages/python-sdk/src/yoao_inference_contract/plugin_assets.py` 与 `apps/inference/src/yoao_inference/owner_transport.py:498-515`：当 `execution_mode == "direct"` 且 `asset_manifest_sha256 is None` 时放行外部托管权重（`plugin_asset_reason()` 返回 `None`、`check_approval = False`），同时保持 `container` 档 fail-closed。
     - `apps/inference/src/yoao_inference/gpu/backend_dispatch.py:252-260,359-374`、`gpu/diagnostics.py:170-184` 与 `backend_control_manager.py:234-241`：按 `gpu_resource_id IS NOT NULL` 区分本机受管 GPU，D1 远端 GPU 仅在 `gpu_info.device_uuid` 不命中本机任何受管物理卡 UUID 时放行。
     - `apps/api/app/api/v1/admin_plugin_installations.py:273,326,444` 与 `PluginQuiesceResponse.launch: PluginLaunchReceipt | None`：按 `gpu_resource_id is not None` 分派本机 GPU 控制与 CPU/D1 终端排空。
  3. 落地 Inference 自有异步出网守卫与 `managed_evaluation` 端到端闭环：
     - 在 `apps/inference/src/yoao_inference/` 新增 `host_guard.py` + `httpcore.AsyncNetworkBackend` 连接时 IP 固定传输层并接入 `BackendTransport`/`request_tracking.py`，硬阻断链路本地/元数据/重定向，按显式白名单分别放行私网（`INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST`）与裸机本地回环（`INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST`）。
     - 在 `apps/inference/src/yoao_inference/capabilities.py:422-448` 提取 `managed_evaluation` 写入 `health_meta.capabilities`，并在 `apps/api/app/services/training_adapters.py:94-127` 强制插件后端在管理员批准的 `plugin.manifest.capabilities` 中声明 `"managed_evaluation"`。
  4. 新增超管 REST 接口 `POST /api/v1/admin/ml-integrations/plugins/direct`（预留 -> 探活 -> 失败自动 `cancel_prepared` 清理；支持管理员补充 `publisher`/`license`/展示字段及显式批准 `approved_managed_model_compatibility` / `approved_managed_evaluation`）与只读 `POST /api/v1/admin/ml-integrations/plugins/installations/{id}/probe`（绝不改写 `extra_params->'plugin'`），扩展 `GET /api/v1/admin/ml-integrations/plugins/installations` 返回统一只读投影。
  5. 同步更新 `apps/api/openapi.snapshot.json`、`packages/python-sdk/src/yoao/_operation_catalog.generated.json` 与 `yoao plugins register-direct` CLI 命令。
- **验收标准**：
  - 一次性隔离 PostgreSQL 上通过 `alembic upgrade head -> downgrade bo0512 -> upgrade head`、`scripts/validate_migrations.py`、全量推理插件与角色权限回归测试（含 `test_grant_namespace_security.py`、`test_module_data_roles.py`、`scripts/test_grant_sql.py`）。
  - 覆盖以下场景：D1 CPU 与远端 GPU 注册 -> 内置模型推理与模型目录不被资产审批误拦 -> `managed_model_compatibility` 与 `managed_evaluation` 交集审批 -> 活跃引用阻断卸载 -> 排空并安全退役；D1 若伪装本机受管 GPU UUID 必被拒绝；容器档仍严格拒绝非法 `container_id`、缺失资产审批与自报 `source_tier`；SSRF 阻断元数据地址与重定向，且显式白名单下本地回环直连可通。

### I2：清除全部 LibreYOLO 硬编码与共享窄运行器抽象

- **目标仓**：`YOAO-Labs/ai-annotation-platform`（及按需同步 `YOAO-Labs/Plugin-SDK`）。
- **关键改动**：
  1. 清理 `apps/api/app/services/plugin_installation_observation.py`：
     - 删除 `LIBRE_CUDA_RUNTIME` / `LIBRE_RUNTIME` 导入与 `206-215`、`223-242`、`405-409`、`432-441` 的 `framework == "libreyolo"` 分支，保留 `216-222` 与 `410-431` 通用校验。
     - 对所有本地受管 CUDA 与声明 `managed_model_compatibility` 的插件统一要求 `cpu_fallback_supported is False`；当历史镜像缺失嵌套 `runtime_info.runtime_versions` 时以框架无关顶层字符串子集回退比对保证存量镜像零破坏；同步更新仅测旧安装期硬编码的 `test_libreyolo_cpu_admission_keeps_runtime_and_fallback_pins`。
  2. 清理 `packages/python-sdk/src/yoao/plugin_deployment.py`（`:561,636,651,656,718-727`）与 `_plugin_docker.py`（`:38,778-817`）：
     - 移除 `plugin_id != "libreyolo"`、`_LIBREYOLO_ENTRYPOINT = "yoao-libreyolo"`、`LIBRE_CUDA_RUNTIME` 硬编码与写死的 `prepare-assets --size t`，改为通用 OCI 标签 `io.yoao.plugin.prepare-assets-command`（经非 shell 校验）。
     - 抽离极窄 `ContainerRuntimePort`（负责 `inspect_image`、`run_detached`、`run_oneshot`、`inspect_container`、`stop_and_remove`，默认提供本地 Docker CLI 实现并预留 Podman/OCI 接入点），供后续训练器 T2 `training-supervisor` 直接复用。
  3. 新增 AST 守卫测试，禁止 `plugin_installation_observation.py`、`plugin_deployment.py` 与 `_plugin_docker.py` 重新引入具体框架硬编码（`libreyolo`、`ultralytics`、`yoao-libreyolo`）。
- **验收标准**：
  - 既有 LibreYOLO 推理插件（含历史 CUDA 镜像兼容样例）与模型部署退役回归测试全绿（同步更新的 1 个旧安装期特判测试按新边界验收）。
  - 新增通用非 LibreYOLO（如自定义 `segmentation` / `ocr` 插件）的观察、资格验证、资产准备标签与 `ContainerRuntimePort` 单元测试全绿。

### I3：Web 管理页直连插件支持与真实双轨验收

- **目标仓**：`YOAO-Labs/ai-annotation-platform`（及按需同步 `YOAO-Labs/YOAO-Docs`）。
- **关键改动**：
  1. 在 `apps/web` 模型市场 / 推理集成管理页中补齐直连推理插件（`direct`）注册弹窗（URL、Token、来源层级、能力探测预览与受控模型兼容性/受控评估勾选确认）、插件列表展示（展示 `display_name`、`source_tier`、`execution_mode`、`status`）、只读探活、启用/停用与安全退役操作；既有容器档记录保持完整展示与生命周期操作兼容。
  2. 在一次性测试库与真实本机进程环境下，用不依赖 Docker 的本地 `venv` 推理插件（基于 `yoao-plugin-sdk`，通过显式本地回环白名单）+ 容器推理插件跑通完整端到端验收（注册 -> 探活 -> 项目启用 -> 推理调用 -> 停用 -> 退役），使用 `agent-browser` 核验前端交互与持久状态。
  3. 同步更新 `CHANGELOG.md`、`YOAO-Labs/YOAO-Docs` 管理与开发指南、更新 [ADR-0190](../adr/archive/0190-inference-plugin-generalization-and-direct-tier.md) 状态，并在本计划末尾补写 `## Outcome`。
  4. 按 ADR-0190 已验证的双轨口径修订 [ADR-0189](../adr/archive/0189-trainer-extension-point.md)（支持 `execution_mode = "local_process" | "container"` 与共享 `ContainerRuntimePort`），无缝转入训练器插件化 **T2**。

### I4（可选独立切片）：D2 本机受管 CUDA GPU 直连档

> 仅在维护者明确需要“无 Docker 裸机 `venv`/`conda` 推理插件与平台其他服务共享同一张物理 GPU 仲裁”时排期（可在 I3 后立即实施，也可排在训练器 T2 之后）：

- 在 `YOAO-Labs/Plugin-SDK` 落地受管生命周期资格运行器（如 `yoao-plugin qualify-managed`，替代仅输出 `platform_managed_qualified: False` 的 `--qualify-local`）并解除 `inference/runtime/plugin_service.py:45,146-160` 对 OCI 环境变量与 `cuda:0` 的硬编码。
- 在主仓更新 `yoao_lifecycle_contract`、`yoao_inference_contract/gpu.py`、`apps/api/alembic/sql/inference.contracts.json`、GPU 控制/排空服务与前向 SQL 迁移 `bo0514`（`gpu_authority_step` 将 `_string(..., 64)` 扩至 `71` 并在加载 `v_registry` 后按 `execution_mode` 严格区分 `^[0-9a-f]{64}$` 与 `^direct:[0-9a-f]{64}$`，同步更新 `authorize_plugin_routing_replacement`、`command_plugin_installation` 与 `set_plugin_asset_approval`）。

## 4. 风险与缓解

| 风险                                                              | 缓解措施                                                                                                  |
| ----------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| D1 远端 GPU 绕过本机 GPU Arbiter 或触发 `0434` 误阻断             | 明确按 `gpu_resource_id IS NOT NULL` 分支；调度与注册双处核验 `gpu_info.device_uuid` 不命中本机受管卡     |
| `plugin_asset_reason` 误拦截 D1 无资产清单插件                    | 仅对 `execution_mode == "direct" and asset_manifest_sha256 is None` 放行，保持 `container` 档 fail-closed |
| 放开裸机本地回环直连引入 SSRF 漏洞                                | 在 `apps/inference` 实现异步 `httpcore.AsyncNetworkBackend` IP 固定守卫，仅显式白名单放行私网/回环        |
| 清理 `plugin_installation_observation.py` 破坏历史 LibreYOLO 镜像 | 缺失嵌套 `runtime_versions` 时保留框架无关的顶层字符串子集回退校验；Learning 侧继续守卫模型回流版本       |
| 主仓与 `Plugin-SDK` 清单再次漂移                                  | 在 I0 阶段即加入跨仓 JSON Schema 对等校验测试，成为本地与 CI 必跑检查                                     |

## 5. 当前状态与待决事项

- **当前状态**：**I0–I3 已全部完成并通过真实双轨与 `agent-browser` 端到端验收**。[ADR-0190](../adr/archive/0190-inference-plugin-generalization-and-direct-tier.md) 已转正并归档至 `docs/adr/archive/`，[ADR-0189](../adr/archive/0189-trainer-extension-point.md) 已按双轨与共享 `ContainerRuntimePort` 口径完成对齐。
- **后续可选切片**：I4（D2 本机受管 CUDA GPU 非容器直连档）保留为按需排期的独立切片；当前可直接进入训练器插件化 **T2**。

## 6. Outcome

### 已落地切片与提交

- **I0（双仓统一推理清单契约）**：主仓 `10fec5961` + `YOAO-Labs/Plugin-SDK` `253d7fc`，双向冻结 `yoao-extension-manifest/v1`（`extension_point = "inference"`）与完整推理能力词表，移除非法 `"train"`，零破坏兼容旧扁平清单。
- **I1（前向迁移 `bo0513` 与 D1 非容器直连档闭环）**：主仓 `a7cce1110`，落地 `0513_inference_plugin_direct_mode.sql`、Inference 自有 IP 固定异步 SSRF 守卫、`INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST` / `INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST`、`managed_evaluation` 审批闭环、`POST /api/v1/admin/ml-integrations/plugins/direct` 与只读 `probe` 端点以及 `yoao plugins register-direct` / `probe` CLI。
- **I2（清除 LibreYOLO 硬编码与 `ContainerRuntimePort` 抽象）**：主仓 `c9f9287f1` + `cee2c5697`，彻底移除安装观察器与部署器中的 LibreYOLO 特例分支，引入通用 OCI 标签 `io.yoao.plugin.prepare-assets-command` 与可插拔 `ContainerRuntimePort`（默认 `LocalDockerRuntime`），并附 AST 防回流测试。
- **I3（Web 直连管理 UI、Compose 迁仓与双轨实测收口）**：主仓 `f1f405cb6` + `cee2c5697` + 本次收口提交，`YOAO-Labs/YOAO-Plugins` `aa8eb42`（将主仓根目录 6 个 `docker-compose.ml*.yml` 迁移收口至各官方插件目录 `plugins/inference/<plugin>/docker-compose.yml`，同步更新 `apps/api/app/assembly/modules/inference.py`、`scripts/ml-backend-runner.py --plugins-root` 与 `.env.example`）。

### 真实双轨与浏览器验收留痕（2026-11-26）

- **一次性隔离栈**：独立 `postgres:16-alpine`（全量 `alembic upgrade head` 含 `bo0513` 与 17 个最小权限 `yoao_inference_*_client` 函数角色）、`redis:7-alpine`、`minio/minio:latest`（开启桶版本化）、`registry:2.8.3`、本检出 `yoao_inference` + FastAPI API + Vite SPA，全程不触碰日常开发库与生产库。
- **轨道 A（零 Docker 独立 Python 3.11 `venv` 直连推理插件）**：独立 `venv` 安装 `YOAO-Labs/Plugin-SDK` 并经 `validate_manifest_dict` 自检 `execution_mode = "direct"`，通过 `INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST=127.0.0.1` 完成注册 -> 只读探活（`protocol_version = "2.2"`、`boot_id = "venv-a-boot-001"`、`terminal_drain_supported = true`）-> 项目启用 -> 健康与能力查询 -> 真实上传 1x1 PNG 原图后调用 `predict-test` 返回真实检测结果（无需伪造容器资产审批）-> `disable` -> `quiesce`（调用真实 `/lifecycle/terminal-drain`）-> `retire` -> 退役后探活返回 `409`。
- **轨道 B（真实 Docker OCI 容器推理插件）**：构建携带 `io.yoao.plugin.manifest`（`yoao-extension-manifest/v1`）与 `io.yoao.plugin.prepare-assets-command` 的真实 OCI 镜像并推送至一次性本地仓库取得不可变 `@sha256:...` 摘要；经 `LocalPluginDeployment` + `LocalDockerRuntime`（`ContainerRuntimePort`）完成 `inspect_image` -> `prepare` -> `prepare_assets` -> `install`（独立桥接网 `i3-net` 经 `INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST` 联通）-> 资产审批前 `predict-test` 失败关闭（`503 ml_backend_pool_unavailable`）-> 实测容器内文件并审批资产 -> `predict-test` 返回真实检测结果 -> `uninstall` 停止并删除真实容器且置为 `retired`。
- **真实浏览器 `agent-browser` 验收**：在独立命名会话中分别以 `super_admin` 与 `project_admin` 实测真实页面控件，验证双轨同屏摘要与筛选、通过 `DirectInferencePluginModal` 实时注册第二个 `venv` 直连插件并经刷新持久化、详情弹窗只读探活与 `Escape` 关闭、完整直连生命周期（`停用 -> 启用 -> 停用 -> 排空 -> 退役` 与刷新持久化）、容器停用后禁用的 CLI 提示、以及非超管深链 `?tab=registry&registry_view=plugins` 自动回退且零插件接口请求、零未捕获控制台错误。
- **资源退役**：双浏览器会话已关闭，全部 `org.aap.acceptance=i3-dual-track` 标记容器、网络、临时镜像、临时 `venv` 与宿主进程已完整清理并复核归零。
