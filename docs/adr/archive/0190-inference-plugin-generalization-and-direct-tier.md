# 0190 — 推理插件机制升级：统一清单、非容器直连档与框架去硬编码

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** 维护者确认并验收通过 I0–I3 真实双轨（无 Docker 本机 `venv` 直连档 + OCI 容器档）与 `agent-browser` 端到端联调
- **Supersedes:** —（演进 [ADR-0180](../0180-installable-inference-plugins.md)，对齐 [ADR-0187](0187-notification-channel-extension-point.md) `yoao-extension-manifest/v1` 跨扩展清单分层、双轨接入与来源层级边界，并为 [ADR-0189](0189-trainer-extension-point.md) 共享运行器抽象铺路）

## Context

[ADR-0180](../0180-installable-inference-plugins.md) 以 LibreYOLO 检测插件为首个样本，建立了 Inference 领域 owner 的插件安装身份、GPU 仲裁与路由替换围栏。随后平台又按 [ADR-0187](0187-notification-channel-extension-point.md) 与 [ADR-0189](0189-trainer-extension-point.md) 落地了通知渠道、格式适配器与训练器契约，并把对外插件开发包统一至独立仓库 `YOAO-Labs/Plugin-SDK`。对照升级前的代码基线，推理插件机制暴露出四处制约生态扩展与日常使用的结构性耦合：

1. **托管安装强绑定 Docker 容器与 OCI 摘要，非容器推理服务无法获得一等插件治理**：
   - `packages/python-sdk/src/yoao_inference_contract/plugin.py` 将 `PluginManifest.artifact` 锁死为 `@sha256:...` OCI 引用，将 `PluginLaunchReceipt.container_id` 锁死为 64 位小写十六进制 Docker 容器 ID；`packages/python-sdk/src/yoao_inference_contract/gpu.py`、`plugin_assets.py` 以及 SQL 迁移 `apps/api/alembic/sql/0412_plugin_tracker_retirement_order.sql`、`0400_plugin_routing_replacement.sql`、`0399_plugin_terminal_hold.sql` 均按 `^[0-9a-f]{64}$` 校验 `container_id`。
   - 同时，6 处代码将 `compute.device == "cuda"` 与“受本机 GPU Arbiter 仲裁（`gpu_resource_id IS NOT NULL`）”混为一谈，且资产审批守卫对全部带 `extra_params->'plugin'` 的行无条件要求 `artifact`、`asset_manifest_sha256`、`container_id` 与 `image_id`。
   - 这导致运行在裸机 `venv`/`conda`、systemd 或远端 GPU 主机上的推理插件只能退回普通手动 `MLBackendRegistry` 注册，既无法纳入统一插件生命周期（`prepared`/`active`/`disabled`/`retired`）、管理员批准的 `managed_model_compatibility` 交集门禁、`source_tier` 标识与安全退役围栏，也无法进入后续统一插件市场视图。
2. **清单格式分裂且能力词表过窄，主仓与 `Plugin-SDK` 存在明显分叉**：
   - 主仓 `PluginImageManifest` 缺少 `manifest_schema`、`display_name`、`description`、`homepage`、`protocol_version`，强制填写 Python 专属字段 `sdk_compatibility`，且把 `capabilities` 锁死为 `Literal["inference", "detection", "managed_models"]`（无法声明分割、OBB、OCR、姿态、文档版面、交互式或视频跟踪插件）。
   - 同时独立仓 `YOAO-Labs/Plugin-SDK` 中 `extension_point = "inference"` 误把 `capabilities` 写为 `{"predict", "train", "track", "health"}`——其中 `"train"` 违反 [ADR-0180](../0180-installable-inference-plugins.md)（训练属于 `extension_point = "trainer"`），且缺少 `managed_models` / `managed_evaluation` 与平台任务能力词表。
3. **通用安装观察与部署器中残留 LibreYOLO 特例硬编码**：
   - `apps/api/app/services/plugin_installation_observation.py` 直接导入 `LIBRE_CUDA_RUNTIME` / `LIBRE_RUNTIME` 并按 `entry.framework == "libreyolo"` 分支校验版本与回退策略。
   - `packages/python-sdk/src/yoao/plugin_deployment.py` 与 `_plugin_docker.py` 写死了 `plugin_id != "libreyolo"`、`_LIBREYOLO_ENTRYPOINT = "yoao-libreyolo"` 与 `prepare-assets --size t`。
4. **运维与开发接入链路偏重，缺少跨推理/训练共享的运行器抽象**：
   - 仅支持通过 `yoao plugins` CLI 配合本地 Docker 命令安装推理插件，无法在 Web 管理页完成非容器直连插件注册与管理；Docker 调用逻辑与训练器 T2 所需的容器运行通道也尚未抽离为共享窄接口。

同时，Inference 领域已沉淀 19 个插件相关冻结 SQL 迁移（`0388`–`0413` 共 16 个安装围栏迁移，外加 `0404` 路由范围替换、`0433` GPU 成员标量守卫与 `0434` 预测范围路由）以及 `yoao_inference_gpu_v1`、`yoao_inference_routing_v1`、`public.sync_gpu_backend_membership()` 等关键安全不变量。本次升级必须在不原地修改任何冻结 SQL 文件、不破坏既有容器镜像与 GPU 仲裁安全性的前提下前向演进。

候选演进方案对比如下：

| 选项                                                                 | 主要卖点                                                                                | 主要劣势                                                                    |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| **A. 保留 Inference Owner，前向新增 `direct` 档 + 双轨清单（选用）** | 零改动 19 个冻结 SQL；复刻通知/格式适配器已验证的双轨模式；容器与非容器共守同一安全账本 | 过渡期同时支持旧扁平清单与 `yoao-extension-manifest/v1`，需维护规范化投影层 |
| B. 新建独立 `inference_plugins` 表并迁移存量注册行                   | 彻底甩开 `extra_params->'plugin'` 历史结构                                              | 必须重写 19 个冻结迁移与 GPU/路由外键，破坏 `sync_gpu_backend_membership`   |
| C. 继续要求所有推理插件容器化，非容器只走普通手动 MLBackend          | 无需改动安装 SQL 与观察器                                                               | 裸机 venv/conda 与远端 GPU 服务永久失去插件治理、兼容审批与市场统一投影     |

## Decision

### 1. 保持 Inference 单一写入 Owner，分离硬件类型与本机 GPU 仲裁，支持 `execution_mode = "direct" | "container"`

沿用 [ADR-0186](0186-modular-monolith-and-on-demand-extraction.md) 与 [ADR-0187](0187-notification-channel-extension-point.md)，Inference 继续是推理插件安装身份的唯一写入 owner，权威元数据仍落于 `public.ml_backend_registry.extra_params->'plugin'` 与既有伴生表，严禁新建第二套插件写入表，严禁原地修改冻结历史 SQL（`0388`–`0434`）。

#### 1.1 核心领域不变量：解耦 `compute.device` 与本机 GPU 仲裁

- `compute.device`（`"cpu" | "cuda"`）只表示插件运行的计算设备类型。
- `resource.gpu_resource_id IS NOT NULL` 表示该实例是否纳入**当前平台本机 GPU Arbiter 互斥仲裁**。
- 所有本地 GPU 仲裁分支（`PluginResourceRequirements`、`PluginInstallation.validate_binding`、`prepare_plugin_installation`、`command_plugin_installation`、`admin_plugin_installations.py`、`plugin_installation_observation.py`、`backend_control_manager.py` 与 `0434` `uses_gpu`）统一按 `gpu_resource_id IS NOT NULL`（或容器档 `cuda`）判断，不再把远端/外部独立 GPU 误判为本机受管 GPU。
- **防本机 GPU 绕过红线**：当 `gpu_arbiter_rollout_enabled` 开启且存在受管 GPU 边界时，非仲裁直连 GPU 插件（`execution_mode = "direct"` 且 `gpu_resource_id = null`、`device = "cuda"`）仅当其实测 `health.gpu_info.device_uuid` **不匹配**本机 `GPU_ARBITER_RESOURCES_JSON` 中任何已配置物理 GPU UUID 时才允许跳过本机仲裁；若命中本机受管物理卡 UUID，必须在注册观察与调度阶段均拒绝，防止本地 GPU 插件借直连档绕过仲裁。

#### 1.2 通用持久化元数据与安全边界

在 `extra_params->'plugin'` 上新增向后兼容字段（同步更新 `PluginInstallation` 与 `PluginManifest`，同时保持 `extra="forbid"`）：

- `execution_mode`: `"container"`（历史缺省默认）或 `"direct"`。
- `source_tier`: `"official" | "third_party"`，仅由平台超管注册/安装入口写入，默认 `"third_party"`，严禁采信 OCI 清单或 `/setup` 自报值（历史存量记录只读投影按默认规则回填，不豁免任何隔离、鉴权或资格门禁）。
- `display_name`、`description`、`homepage`: 经纯文本与公开 `https://` 校验的展示字段，供统一只读投影与后续插件市场聚合。
- **资产审批策略（修复 `plugin_asset_reason` 与 `0403` 全量阻断）**：
  - `execution_mode = "container"`：维持 fail-closed，必须声明 64-hex `asset_manifest_sha256` 并完成资产审批后才允许执行内置模型推理。
  - `execution_mode = "direct"` 且 `asset_manifest_sha256 IS NULL`（权重由宿主机或远端服务外部管理）：`plugin_asset_reason()` 返回 `None`，`owner_transport.py` 置 `check_approval = False`，放行内置模型推理、模型目录与视频跟踪，不要求伪造容器资产审批行。
  - `execution_mode = "direct"` 且声明了 `asset_manifest_sha256`：前向扩展 `PluginAssetDescriptor`、`PluginAssetApprovalUpdate`、`plugin_asset_reason` 与 `yoao_inference_topology_v1.set_plugin_asset_approval`，按可空 `artifact` 与 `runtime_instance_id` / `runtime_fingerprint` 绑定审批。
- **Inference 自有异步出网守卫与裸机本地回环显式放行**：
  - `apps/inference` 保持 ADR-0186 模块独立性，不跨模块依赖 `yoao-assets-source`。在 `apps/inference/src/yoao_inference/host_guard.py` 实现自有连接时 IP 固定传输层（`httpcore.AsyncNetworkBackend`），接入 `BackendTransport` / `request_tracking.py` 统一守卫观察、执行与控制请求。
  - 始终硬阻断链路本地（含 `169.254.0.0/16` 元数据）、多播、未指定、保留段、IPv4 映射受禁段、重定向与环境代理（`follow_redirects=False, trust_env=False`）。
  - 私网（RFC1918）仅在命中管理员显式配置的 `INFERENCE_PLUGIN_PRIVATE_HOST_ALLOWLIST` 时放行；回环地址（`127.0.0.1` / `::1` / `localhost`，用于裸机本地 `venv`/`conda` 直连推理插件与本地验收）仅在命中管理员显式配置的 `INFERENCE_PLUGIN_LOOPBACK_HOST_ALLOWLIST` 时放行，直连注册入口同步遵循该白名单而非无条件拒绝回环。

#### 1.3 托管容器档（`execution_mode = "container"`）

- 100% 保持既有不可变 OCI `@sha256:...` 摘要绑定、64 位十六进制 `container_id`、本地容器启动收据、受控重启替换围栏与 GPU 资格报告校验。
- 新增支持 `yoao-extension-manifest/v1`（`extension_point = "inference"`）清单，并与旧扁平 `PluginManifest` 双轨兼容解析。
- 管理员 CLI（`yoao plugins`）底层容器操作收口到共享窄接口 `ContainerRuntimePort`（`packages/python-sdk/src/yoao/container_runtime.py`，默认 `LocalDockerRuntime` 实现，接口不锁死 Podman/OCI），供训练器 T2 `training-supervisor` 复用。
- 官方推理插件的独立 Docker Compose 叠加配置统一迁出平台主仓库根目录，随插件维护于 `YOAO-Labs/YOAO-Plugins` 的 `plugins/inference/<plugin>/docker-compose.yml`；宿主机维护脚本 `scripts/ml-backend-runner.py` 显式通过 `--plugins-root` 绑定官方插件仓库路径。

#### 1.4 非容器直连档（`execution_mode = "direct"`，分两期落位）

1. **D1：非本机仲裁直连档（`execution_mode = "direct"`, `gpu_resource_id = null`，I1 落地）**
   - 适用于裸机 `venv`/`conda` CPU 插件、无 Docker 本机独立卡（非平台仲裁）或远端 GPU/CPU 推理服务。
   - **原子注册与可审计投影**：超管通过 `POST /api/v1/admin/ml-integrations/plugins/direct` 提交 `name`、`url`、`auth_token`（>=32 字符）、`source_tier`、可选 `display_name`/`description`/`homepage`/`publisher`/`license`、以及显式批准的 `approved_managed_model_compatibility`（默认 `[]`，发现不等于资格）与 `approved_managed_evaluation: bool`（默认 `false`）。
   - 平台先在 Inference owner 内预留 `prepared` 行（凭据加密存管于 Inference owner，API 不落地明文），随即经 `HealthObservationClient`（含自有 IP 固定守卫）主动拉取带挑战的 `GET /health` 与鉴权 `GET /setup`：
     - 校验 `protocol_version == PROTOCOL_VERSION`（当前为 `"2.2"`）、`plugin_identity`（`plugin_id`、`version`）、`models`（全量 `yoao-protocol-v2` 词表）、`/setup` 与 `/health` 一致的 `pool_topology`、非空 `residency.boot_id`（`<= 128` 字符）、以及若 `health.gpu_info` 非空则其 `device_uuid` 不命中本机任何受管物理 GPU。
     - 核验批准的 `approved_managed_model_compatibility` 是实时 `/setup` 的子集，若 `approved_managed_evaluation = true` 则实时 `/setup` 必须声明 `managed_evaluation: true` 且至少批准一项兼容框架。
     - `/setup` 缺失的清单外围字段（`publisher`、`license`）按管理员提交值或确定性默认值（`"direct"` / `"unspecified"`）补齐，`capabilities` 由观测到的 `models` 任务集合与已批准的 `managed_models`/`managed_evaluation` 派生并冻结写入 `extra_params->'plugin'->'manifest'`；若探活或校验失败，立即在返回前以 savepoint 回滚或 `cancel_prepared` 清理预留行，不遗留脏记录。
   - **只读复查与安全退役**：
     - `POST /api/v1/admin/ml-integrations/plugins/installations/{id}/probe` 仅做只读探活与刷新健康观测，**绝不改写 `extra_params->'plugin'`**（尊重 `0433` `sync_gpu_backend_membership` 不变量）。
     - `disable -> quiesce -> retire`：若端点在 `/setup` 声明了 `terminal_lifecycle.terminal_drain_endpoint`，`quiesce` 照常调用 `terminal_drain` 取得终端排空证明；若无该端点，则在成员排空、活动路由范围与跟踪范围物理排空、`active_releases == 0` 且 `frozen_executions == 0` 后允许 `quiesce`（`PluginQuiesceResponse.launch` 设为 `PluginLaunchReceipt | None`）并安全 `retire`（同时不可逆清除运行态凭据）。
2. **D2：本机受管 CUDA GPU 直连档（`execution_mode = "direct"`, `gpu_resource_id != null`，作为独立后续切片 I4 / 按需排期）**
   - 面向裸机 `venv`/`conda` 上与平台其他服务共享同一块物理 GPU 仲裁的推理插件（零 Docker）。
   - **硬前提（绝不降级安全标准）**：当前 `Plugin-SDK` 的 `yoao-plugin check --qualify-local` 仅输出 `"platform_managed_qualified": False` 的开发冒烟报告，不满足 `yoao_lifecycle_contract` v1 闭集校验。因此 D2 必须配套独立的受管生命周期资格运行器（如 `yoao-plugin qualify-managed`），产出完整 `yoao_lifecycle_contract` 报告并绑定 `runtime_fingerprint`（`^sha256:[0-9a-f]{64}$`，替代 `image_id`），同步移除 `Plugin-SDK` `inference/runtime/plugin_service.py` 对 OCI 环境变量与 `cuda:0` 的硬编码。
   - **身份与 SQL 前向放宽规则**：保持内部 JSON/SQL 键名 `container_id` 以兼容 `0391`/`0395`/`0399` 精确键约束，由前向迁移（`bo0514`）与 `yoao_inference_contract/gpu.py`、`inference.contracts.json` 同步放宽：在 `gpu_authority_step` 将 `_string(..., 64)` 提升至 `71`，预读仅接受 `^([0-9a-f]{64}|direct:[0-9a-f]{64})$`，在读取 `v_registry` 后严格按 `execution_mode` 校验（`container` 仍必须 `^[0-9a-f]{64}$`，`direct` 必须 `^direct:[0-9a-f]{64}$`），并同步更新 `authorize_plugin_routing_replacement` 与 `command_plugin_installation`。

### 2. 统一 `yoao-extension-manifest/v1`（`extension_point = "inference"`）并双向对齐 `Plugin-SDK`

主仓 `packages/python-sdk/src/yoao_inference_contract/plugin.py` 与独立仓 `YOAO-Labs/Plugin-SDK`（`yoao_plugin_sdk.manifest`、`yoao_plugin_sdk.inference`、`yoao_plugin_sdk.cli` 与 `@yoao/plugin-sdk`）必须在同一规范下保持 JSON Schema 对等：

1. **通用核心字段**（对齐 [ADR-0187](0187-notification-channel-extension-point.md) §2.1）：
   - `manifest_schema`: `"yoao-extension-manifest/v1"`
   - `extension_point`: `"inference"`
   - `plugin_id`、`version`、`display_name?`、`description?`、`homepage?`、`publisher`、`license`
   - `protocol_version`: `"yoao-inference/v2"`（映射到推理 HTTP 线协议 `GET /setup` 的 `protocol_version` 常量 `yoao_protocol_v2.version.PROTOCOL_VERSION`，当前为 `"2.2"`）
   - `entrypoint?`: `container` 档必填（1..64 个非 shell、非 `python -c` 参数向量）；`direct` 档可省略
   - `resource?`: `{ "memory_mb": int (16..65536) }`（`container` 档必填；`direct` 档可选）
   - `artifact?`: 安装期绑定的 OCI `@sha256:...`（镜像标签内严禁预填；`direct` 档为 `null`）
2. **统一推理 `capabilities` 词表（彻底移除非法 `"train"`，对齐完整 `yoao_task_contract.Task`）**：
   - 合法词表为 `{"inference", "detection", "segmentation", "interactive", "video_tracking", "ocr", "classification", "obb", "pose", "doc_layout", "managed_models", "managed_evaluation"}`，要求非空、无重复且必须包含 `"inference"`。
   - 联动约束：声明 `"managed_models"` 当且仅当 `extension.managed_model_compatibility` 非空；声明 `"managed_evaluation"` 必须同时声明 `"managed_models"`。
   - **`managed_evaluation` 端到端闭环**：在 `apps/inference/src/yoao_inference/capabilities.py`（`extract_capabilities`）中提取 `/setup` 声明的 `managed_evaluation` 写入 `health_meta.capabilities`，在 `plugin_installation_observation.py` 校验 `manifest.capabilities` 与实时 `/setup` 一致，并在 `apps/api/app/services/training_adapters.py` 强制插件后端同时在管理员批准的 `plugin.manifest.capabilities` 中包含 `"managed_evaluation"`（守住“发现不等于资格”）。
3. **推理专属 `extension` 块（`extra="forbid"`，禁止 `source_tier`）**：
   - `execution_mode`: `"container" | "direct"`（默认 `"container"`）
   - `models`: `list[PluginModelCapability]`（1..128 项，放开至完整 `yoao-protocol-v2` 词表与 `Task`）
   - `pools`: `dict[str, PluginPoolTopology]`（1..128 个池）
   - `compute`: `{ "device": "cpu" | "cuda", "gpu_memory_mb": int }`（`container` CUDA 与受管仲裁 CUDA 要求 `gpu_memory_mb > 0`，`cpu` 与非仲裁直连档允许 `0`）
   - `sdk_compatibility?`: 可选字符串（非 Python 推理插件无需填写）
   - `asset_manifest_sha256?`: 可选 64 位小写 hex（`container` 档必填；`direct` 档可选）
   - `allowed_asset_sources`: `list[str]`（默认 `[]`）
   - `managed_model_compatibility`: `list[ManagedModelCompatibility]`（默认 `[]`）
4. **双轨兼容投影**：
   - 解析入口在检测到 `manifest_schema == "yoao-extension-manifest/v1"` 时按新分层规范校验，并将其规范投影至兼容既有 SQL 读路径（`manifest.resource.device`、`manifest.resource.gpu_memory_mb`、`manifest.models`、`manifest.pools`、`manifest.managed_model_compatibility`）的持久化结构；缺少 `manifest_schema` 时继续走旧版扁平 `PluginManifest` 解析，确保已验收历史镜像零破坏。

### 3. 清除推理插件链路中的全部 LibreYOLO 特例硬编码

1. **`apps/api/app/services/plugin_installation_observation.py`**：
   - 移除 `LIBRE_CUDA_RUNTIME`、`LIBRE_RUNTIME` 导入及全部 `framework == "libreyolo"` 条件分支，保留通用兼容性与版本核验。
   - 收敛为框架无关通用规则：
     - 任意声明 `managed_model_compatibility` 或本地受管 CUDA 的插件必须报告 `compute.cpu_fallback_supported is False`。
     - 受管 CUDA 资格验证统一比对实时 `runtime_info.runtime_versions` 与资格报告 `deployment.runtime_versions`（含 `cuda_build` 与 `tf32=false` 严格核验）；为向后兼容已构建的历史 LibreYOLO CUDA 镜像（其版本字段位于 `runtime_info` 顶层而非嵌套 `runtime_versions`），当 `runtime_info.runtime_versions` 缺失时采用框架无关的顶层全键校验回退，不按框架名特判。
     - LibreYOLO 受控模型回流的精确框架版本与精度约束由 Learning 侧 `trained_models.py` 与 `training_contract.inference_runtime.validate_inference_runtime` 持续守卫，不损失安全等价性。
2. **`packages/python-sdk/src/yoao/plugin_deployment.py` 与 `_plugin_docker.py`**：
   - 移除 `plugin_id != "libreyolo"`、`_LIBREYOLO_ENTRYPOINT = "yoao-libreyolo"`、`LIBRE_CUDA_RUNTIME` 硬编码与写死的 `prepare-assets --size t`，改为通用 OCI 标签 `io.yoao.plugin.prepare-assets-command`（经非 shell 校验），并由 AST 守卫测试长期禁止回流框架硬编码。

### 4. 与训练器扩展点（ADR-0189）拉齐跨扩展口径

推理插件（ADR-0190）与训练器插件（[ADR-0189](0189-trainer-extension-point.md)）在训练器 T2 前已对齐三项跨扩展约定：

- **统一双轨运行**：均原生支持非容器运行（推理 `direct`、训练器 `local_process`）与受管容器运行（`container`），API 进程永不接触 Docker socket、永不 `import` 第三方插件代码。
- **共享窄容器抽象**：在 `yoao-sdk` 内部沉淀极窄 `ContainerRuntimePort`（`packages/python-sdk/src/yoao/container_runtime.py`，默认 `LocalDockerRuntime` 实现，预留 Podman/OCI 接入点），供推理 `yoao plugins` CLI 与训练器 `training-supervisor` 复用。
- **统一只读插件投影**：`GET /api/v1/admin/ml-integrations/plugins/installations` 与训练器/通知/格式适配器列表输出同构的展示与治理摘要字段（`plugin_id`、`version`、`extension_point`、`display_name`、`description`、`homepage`、`source_tier`、`execution_mode`、`status`），供后续插件市场页面（M5）直接聚合，前端已沉淀无私有状态耦合的独立管理组件。

## Consequences

正向：

- 裸机 `venv`/`conda`、无 Docker 环境与远端 GPU 服务器都能以一等公民身份接入推理插件，完整享有能力核验、受控模型兼容性与评估能力审批、来源层级标识与安全退役围栏。
- 推理插件不再被锁死在 `detection` 和 LibreYOLO 特例中，分割、OBB、OCR、姿态、文档版面、交互式与视频跟踪及非 Python 推理服务均可走同一套统一清单与安装协议。
- `YOAO-Labs/Plugin-SDK` 与主仓 `yoao-sdk` 彻底消除 `yoao-extension-manifest/v1` 契约分叉，官方插件 Compose 配置收口至 `YOAO-Labs/YOAO-Plugins`，同时为训练器 T2 沉淀已实测验证的 `ContainerRuntimePort` 与双轨治理范式。
- 19 个冻结历史 SQL 迁移与已验收历史容器镜像保持 100% 兼容，不产生迁移断层。

负向：

- 过渡期同时维护旧扁平 `PluginManifest` 与新版 `yoao-extension-manifest/v1` 双轨解析器，需要跨格式规范投影测试防止语义漂移。
- 非本机仲裁直连档（D1）的进程生命周期由外部运维或远端主机管理，平台无法像容器档那样保证进程已物理退出，必须在 UI 与审计日志中明确区分隔离级别。
- 本机受管 CUDA 的非容器直连档（D2）涉及受管生命周期资格运行器、SQL 键长放宽与多处 GPU 围栏更新，留作独立可选切片 I4 按需排期。

## Alternatives Considered

- **仅在普通手动 `MLBackendRegistry` 上打补丁支持插件展示**：无法提供 `prepared -> active -> disabled -> retired` 状态机、`managed_model_compatibility` 管理员批准交集守卫与历史引用阻断卸载，会造成“半套插件”。
- **重写 `0388`–`0434` 冻结迁移并重构 `ml_backend_registry`**：违反不可变历史迁移原则，且极易破坏 `sync_gpu_backend_membership` 与路由代次围栏。
- **在 I1 中同时强推 D1 与 D2 并复借 `yoao-plugin check --qualify-local`**：`--qualify-local` 仅输出开发冒烟收据（`platform_managed_qualified: False`），强行接入会破坏受管 GPU 资格安全底线；因此先在 I1 闭合 D1，将 D2 拆为配套真实资格运行器的独立切片 I4。

## Notes

- 实施计划与完整验收留痕见 [推理插件机制升级实施计划](../../plans/1795617883_inference-plugin-upgrade.md)（I0–I3 均已实施并通过真实双轨端到端与 `agent-browser` 验收；I4 作为可选后续切片保留）。
- 已按本 ADR 的双轨与共享运行器口径同步修订 [ADR-0189](0189-trainer-extension-point.md)，准备转入训练器插件化 **T2**。
