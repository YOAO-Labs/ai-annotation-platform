# 0189 — 训练器扩展点与隔离作业协议

- **Status:** Proposed
- **Date:** 2026-11-26
- **Deciders:** 维护者已确认架构方向并完成 T0/T1/T2/T3；已按已采纳的 [ADR-0190](archive/0190-inference-plugin-generalization-and-direct-tier.md) 对齐双轨运行模式（`local_process` / `container`）与共享 `ContainerRuntimePort`（保持 `Proposed` 直至 T4 真实单卡 GPU 验收完成）
- **Supersedes:** —（沿用 [ADR-0186](archive/0186-modular-monolith-and-on-demand-extraction.md) 单一写入 owner 原则、[ADR-0187](archive/0187-notification-channel-extension-point.md) `yoao-extension-manifest/v1` 通用清单与安全边界、[ADR-0190](archive/0190-inference-plugin-generalization-and-direct-tier.md) 双轨运行器与只读插件投影口径，以及 [ADR-0090](archive/0090-versioned-yolo-identities-and-content-assets.md) 发现不等于资格原则）

## Context

在线学习与原生训练子系统已经建立了完整的冻结数据集快照、单调序号流、GPU 资源互斥租约、单调时钟预算、脱敏观测、全量对象回读校验与受控模型回流门禁（[ADR-0076](archive/0076-controlled-trained-model-identity.md) 至 [ADR-0084](archive/0084-qualified-libreyolo-model-return.md)、[ADR-0085](0085-qualified-polygon-instance-training.md)、[ADR-0090](archive/0090-versioned-yolo-identities-and-content-assets.md)）。但当前实现在四个层级硬编码了 `ultralytics` 与 `libreyolo`，阻碍新训练框架接入，也迫使平台核心镜像承载重量级 ML 与 CUDA 依赖：

1. **共享契约层**：`packages/python-sdk/src/training_contract/{catalog,models,validation,dataset,native_architecture,model_return,inference_runtime,initialization,preview}.py` 内置具体框架参数字典、模型规格、源码哈希与预览策略。
2. **执行器层**：`apps/training-executor/Dockerfile` 与 `apps/training-executor/training_executor/{worker,adapters,train,ultralytics_adapter,libreyolo_adapter,instance_rle,evaluation,preview}.py` 在单一镜像内同时烘焙 `torch`、`ultralytics`、`libreyolo`，并通过静态 `if/elif` 分派与 `import torch` GPU 探测强绑定框架。
3. **API 与服务层**：`apps/api/app/schemas/training_*.py` 与 `apps/api/app/services/{training_runs,training_claims,trained_models,sample_evaluations,active_learning}.py` 使用 `Literal["ultralytics", "libreyolo"]` 字面量与 `framework == "ultralytics"` 条件分支，并存在少量跨模块 ORM/私有方法直连。
4. **数据库 SQL 层**：`apps/api/alembic/sql/0491_learning_dataset_boundary.sql` 与 `0492_learning_models.sql` 在视图和 `SECURITY DEFINER` 函数中硬编码 `v.framework = 'libreyolo'`、`'ultralytics-instance-rle-1'` 与 `'ultralytics-yolo'`，`0493_learning_selection.sql` 将学习任务约束限定在 `('detect', 'segment')`。

同时，平台已将内部 5 个便携契约包收敛为支持 Python `>=3.10` 的单一 `yoao-sdk`（`packages/python-sdk`），并将对外插件作者包收口至独立仓库 `YOAO-Labs/Plugin-SDK`（`yoao-plugin-sdk` 与 `@yoao/plugin-sdk`）。我们需要在进入 SQL 泛化（T1）与执行监督器改造（T2）前，冻结训练器扩展点的架构决策与线协议契约。

候选执行架构对比如下：

| 选项                              | 主要卖点                                                                       | 主要劣势                                                                            |
| --------------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| **Supervisor 驱动一次性隔离作业** | 作业退出即彻底回收显存与进程树；默认断网沙箱无凭据暴露；复用既有冻结与租约模型 | 需要通过容器运行时对账与落盘记录替代子进程继承 `flock` / `os.killpg`                |
| 长驻 HTTP GPU 训练守护进程        | 与推理插件形态相似                                                             | 训练耗时数小时且易 OOM/挂死，长驻进程显存残留与强制回收复杂度高，且需暴露网络控制面 |
| 单镜像多 virtualenv 子进程        | 保留原有 `flock` 继承与 `os.killpg`                                            | 无法隔离 CUDA/系统库/GPL 许可边界，新框架接入仍需重建平台核心镜像                   |

## Decision

### 1. 扩展点归属、清单与资格门禁

1. **统一清单与双轨运行模式（对齐 [ADR-0190](archive/0190-inference-plugin-generalization-and-direct-tier.md)）**：训练器插件采用 `yoao-extension-manifest/v1`（见 [ADR-0187](archive/0187-notification-channel-extension-point.md)），固定 `extension_point = "trainer"`、`protocol_version = "yoao-trainer/v1"`，并在 `yoao_learning_v1.trainer_plugins` 上显式声明 `execution_mode = "container" | "local_process"`（生产隔离默认为 `"container"`；裸机独立 `venv`/`conda` 进程隔离为 `"local_process"`）：
   - `capabilities` 为 `{"train", "evaluate", "preview", "export_weights"}` 的非空无重复子集。
   - `container` 档必须提供 `entrypoint`（禁止 shell 解释器与 `python -c`）与 `resource.memory_mb`（16 至 65536），镜像内置清单禁止预填 `artifact`，必须由平台在安装时绑定不可变 OCI 摘要 `@sha256:...`；`local_process` 档由宿主机管理员在 `training-supervisor` 白名单注册可执行入口向量（同样禁止 shell 解释器与 `python -c`，`artifact` 为 `null`），无需强制容器化。
   - `extension` 禁止自报 `source_tier`；平台只读投影统一输出与 ADR-0190 同构的展示与治理字段（`plugin_id`、`version`、`extension_point`、`display_name`、`description`、`homepage`、`source_tier`、`execution_mode`、`status`），供后续统一插件市场（M5）直接聚合。
2. **领域 Owner 表归属**：Learning 域是训练器插件注册与资格状态的唯一写入 owner，在 `yoao_learning_v1` schema 下维护私有表 `yoao_learning_v1.trainer_plugins`（撤销 `PUBLIC` 权限，运行账号仅通过固定 `SECURITY DEFINER` 函数访问），记录平台权威 `source_tier`（`official` | `third_party`）、`execution_mode`（`container` | `local_process`）、展示元数据、不可变 `artifact`（`local_process` 为 `null`）、`capabilities_snapshot`、`qualification_report_sha256` 与状态机（`registered` -> `qualified` -> `enabled` <-> `disabled` -> `retired`）。
3. **发现不等于资格（[ADR-0090](archive/0090-versioned-yolo-identities-and-content-assets.md)）**：注册插件并执行 `describe` 仅产出能力发现快照；只有在平台核验镜像摘要（或 `local_process` 可执行入口摘要）、离线基座资产 SHA-256、能力收据与真实隔离冒烟报告摘要后，管理员才可批准该训练器进入 `qualified` / `enabled`。

### 2. 一次性隔离作业协议（`yoao-trainer/v1`）

每个训练器插件镜像通过 `entrypoint` 暴露 4 个确定性子命令（强类型模型同时冻结在 `yoao_plugin_sdk.trainer`、`@yoao/plugin-sdk` 与平台侧 `training_contract.trainer_plugin`）。所有带协议版本的线载荷均将 `protocol_version: "yoao-trainer/v1"` 设为必填字段并禁止未声明额外字段（`extra="forbid"`）：

1. **`<entrypoint> describe`**（无网络、无 GPU）：向 `stdout` 输出 `TrainerCapabilitiesV1`，声明 `protocol_version`、`framework_id`、`framework_version`、`runtime_versions`、`supported_tasks`、`supported_dataset_contracts`、`architectures`（含离线基座权重文件名与可选 `base_asset_sha256`、`end2end` 标记）、`parameter_schema`、`evaluator_descriptors`、`policy_digests` 与 `serving_compatibility`。
2. **`<entrypoint> validate-config --spec /work/input/spec.json`**（无网络、无 GPU）：输出 `ValidateConfigResultV1`（含 `protocol_version`、`valid`、`schema_id`、`schema_digest`、`resolved_config`、`effective_values`、`origins`、`inactive` 与结构化 `issues`）。当 `valid == true` 时，`issues` 必须为空且 `schema_id` / `schema_digest` 必须完整；当 `valid == false` 时，`issues` 必须非空。
3. **`<entrypoint> train --spec /work/input/spec.json --output /work/output`**（`--network=none`，单卡 GPU 或显式 CPU）：
   - 只读输入 `/work/input`：`spec.json`（`TrainSpecV1`，包含必填的 `protocol_version` 与 64 位小写十六进制 `dataset_snapshot_digest`、`config_digest`、`classes_digest`，成对出现的 `schema_id`/`schema_digest`，并强制 `resume_checkpoint_path` 与 `init_weights_path` 互斥）、校验解压后的 `dataset/` 以及节点只读预置 `assets/`。
   - 可写输出 `/work/output`：
     - `events.jsonl`：按序追加 `TrainEventV1` 并逐行 `fsync`，吞吐与显存统计统一置于 `metrics` 或 `detail`；`progress` 为 `[0.0, 1.0]` 有限数值（`-0.0` 归一为 `0.0`，拒绝布尔值与 `NaN`/`Infinity`）。
     - 检查点文件与原子替换并 `fsync` 目录项的 `checkpoint.json`（仅在新元数据落盘后清理旧检查点）。
     - 终态产物与统一收据 `result.json`（`TrainReceiptV1`）：以 `protocol_version`、`status: "succeeded" | "failed" | "cancelled"`、`artifacts`（安全单层文件名、64 位小写 SHA-256 与非负字节数）、`serving_compatibility`、`failure_code` 与 `failure_message` 统一表达终态，不设立第二套 `failure.json`。规则约束：`status == "failed"` 必须给出非空 `failure_code`；`status == "succeeded"` 禁止携带 `failure_code` 或 `failure_message`。
4. **`<entrypoint> preview --spec /work/input/preview-spec.json --output /work/output`**（可选，仅声明 `preview` 能力时调用）：
   - 输入 `PreviewSpecV1`：携带平台侧按内容寻址规则（不含单次 `preview_id`，对应 `training_contract/preview.py` `INPUT_KEYS`）预先计算的 `input_digest`，以及 `preview_id`、`framework_id`、`task`、`architecture_id`、`config_digest`、`dataset_snapshot_digest`、`policy_digest`、`seed`、`epoch`、`count`、`sources`（含可选成对 `label_path` / `label_sha256`）、`parameters`、`native_values` 与 `pool_summary`。线契约直接守卫 `training_contract/preview.py` `LIMITS` 硬上限：单源 `size_bytes <= 8 MiB`、`width * height <= 16_000_000`、`box_count <= 256`；源池总计 `sources <= 16`、`pool_bytes <= 64 MiB`、`pool_pixels <= 32_000_000`、`pool_boxes <= 4096`。
   - 输出 `preview-*.png` 与 `report.json`（`PreviewReportV1`：`protocol_version`、`status: "succeeded" | "failed"`、`input_digest`、`policy_digest`、`cuda_initialized`、`outputs`、`warnings`、`runtime`、`details`、`failure_code`、`failure_message`）。当 `status == "succeeded"` 时，`cuda_initialized` 必须为 `false`、`failure_code` 与 `failure_message` 必须为 `null` 且 `outputs` 非空；当 `status == "failed"` 时，`failure_code` 必须非空。
   - **监督器投影层**：具体框架的预览追踪证据（例如 `boxes`、`source_draws`、`sources_read`、`operations`、`mosaic_closed`、`transform_parameters`、`native_parameters`）通过结构化的 `PreviewOutputV1.metadata` 与 `PreviewReportV1.details` / `runtime` 回传，由 `PreviewSupervisor` 校验并投影到平台既有 `TrainingPreviewReport`（`apps/api/app/schemas/training_preview.py`），不在通用线契约中写死单一框架的检测框或操作步骤模型。
5. **规范摘要、跨语言数字边界与原子序列化**：
   - `canonical_json_digest(payload)` 对完整 JSON 投影执行 Unicode 码点序 `sort_keys=True`、`separators=(",", ":")`、`ensure_ascii=False`、`allow_nan=False` 后计算 SHA-256，保留显式 `null`（严禁 `exclude_none=True` 丢弃 `format: null`），与既有 `training_contract.catalog.digest` 字节级一致。
   - **跨语言权威摘要边界**：`config_digest`、`dataset_snapshot_digest`、`classes_digest`、`policy_digest` 与 `input_digest` 均由平台/监督器在写入规范载荷时计算并随 `spec.json` / `preview-spec.json` 下发。非 Python 语言插件（如 JS/Go/Rust，其浮点文本格式与 Python `json.dumps` 存在 `0.0`/`0` 或科学计数法差异）在回填 `PreviewReportV1.input_digest` / `policy_digest` 时必须原样回显输入规格中的摘要或对原始规范字节流取哈希，不得对经由本语言 IEEE-754 解析后的浮点对象重新计算平台摘要。
   - `atomic_write_json` 必须在创建父目录前完成规范序列化（禁止 `NaN`/`Infinity`），写入同目录临时文件、设置跨容器 UID 可读的非敏感文件权限（默认 `0o644`，敏感状态文件可显式指定 `mode=0o600`）并 `fsync`，经 `os.replace` 原子替换后 `fsync` 父目录，并在任何 `BaseException` 下清理残留临时文件。

### 3. 双轨执行边界的 GPU 互斥、孤儿回收与租约释放语义

`training-supervisor` 原生支持受管容器档（`execution_mode = "container"`）与本地独立进程档（`execution_mode = "local_process"`），容器档统一复用已在 `yoao-sdk` 抽离并验收通过的共享窄接口 `ContainerRuntimePort`（`packages/python-sdk/src/yoao/container_runtime.py`，默认 `LocalDockerRuntime` 实现，接口保持 Podman/OCI 可插拔），API 进程永不接触容器 socket。

原单镜像子进程通过继承 `/state/gpu-<sha24>.lock` 文件描述符和 `os.killpg` 防止 supervisor 崩溃后提前释放 GPU。容器运行时拉起的容器不会继承 supervisor 的文件描述符，也无法由 `os.killpg` 终止。为保证双轨同等强度的互斥与崩溃恢复，无 ML 依赖的 `training-supervisor` 必须执行四条硬规则：

1. **先落盘后启动**：在调用 `ContainerRuntimePort` 启动容器或拉起 `local_process` 子进程组之前，必须将确定性作业身份（绑定 `gpu_resource_id` 与 `attempt_id` 的容器名/标签或进程组记录）和 `spawn_intent` 原子写入并 `fsync` 到 `/state/assignment.json`。
2. **启动/恢复期先对账再申领**：supervisor 重启持有 `/state/gpu-<sha24>.lock` 后，在调用 `POST /claim` 之前必须通过 `ContainerRuntimePort.list_containers` 按标签枚举绑定该 `gpu_resource_id` 的受管容器（或在 `local_process` 下确认继承锁释放与残留进程组清理）；凡发现运行中容器，必须先执行强停并确认退出，再执行既有恢复或申领新作业。
3. **以真实退出状态作为租约释放前提**：停止、取消、撤权或 `ProcessBudget` 单调时钟预算到期时，`container` 档必须通过 `ContainerRuntimePort` 执行强停并复核 `inspect_container` 确认退出，只有确认完全退出后才向平台上报 `process_exited: True` 以释放 `TrainingResourceLease`；`local_process` 档继续使用子进程 `flock` 继承与 `os.killpg` 并确认子进程组全部退出。
4. **保租约与撤权不退化**：心跳超时置 `lost` 仍保留 `TrainingResourceLease`；项目管理员权限或 API Key 作用域撤销在 admission、claim、heartbeat、finish、resume 五个节点持续复核。

### 4. 离线断网沙箱与容器/OS 强制预览边界

1. **训练作业沙箱**：所有训练器容器强制 `--network=none`、`cap_drop: [ALL]`、`no-new-privileges:true`、`pids_limit`、有界 `/dev/shm`、只读 `/work/input` 与 `/models:ro`。绝不向训练器容器注入数据库 DSN、对象存储凭据、平台管理员 Token、用户 JWT 或 Docker socket。
2. **容器/OS 层强制 CPU-only 预览**：`PreviewSupervisor` 不信任第三方插件自报遵守 CPU 与内存上限，必须在容器/OS 边界强制不挂载 `--gpus`、注入 `NVIDIA_VISIBLE_DEVICES=void` 与 `CUDA_VISIBLE_DEVICES=""`、施加 `--cpus=1`、`--memory`（对齐既有 16 GiB 内存上限）、`--network=none` 与 45s 墙钟超时强杀。插件内部上报的 `cuda_initialized == false` 仅作纵深防御校验。

### 5. 既有存储不变量、数据驱动模型回流与前向 SQL 纪律

1. **`TrainingSavedIO` 与全量回读不变量**：所有训练器产物继续由 `training-supervisor` 按既有 `attempts/{attempt_id}/{name}` 路径规范上传，保持与 `apps/api/alembic/sql/0507_training_saved_io.sql` 兼容。API 在 `finish` 时继续全量流式回读校验大小、`x-amz-meta-attempt-id` 与 SHA-256，并核对 `native-config.json`、`input-membership.json` 与 `observability.json`。
2. **数据驱动的 Serving 兼容性**：`ServingCompatibilityV1` 要求显式给出 `framework`（非空去首尾空白字符串）与 `format`（`null` 显式表示框架原生权重，而非通配符，禁止省略或空字符串），并附带 `training_runtime` 与 `compatible_serving_runtimes`。模型回流仍须通过推理插件 `POST /managed-models` 回执核验、固定验证样本 `/predict` 试跑与人工 `(project_id, task)` CAS 发布。
3. **前向 SQL 迁移纪律**：严禁原地修改已冻结历史 SQL 文件（`apps/api/alembic/sql/0491_learning_dataset_boundary.sql`、`0492_learning_models.sql`、`0493_learning_selection.sql`）。T1 阶段只能通过新增前向 Alembic 迁移以 `CREATE OR REPLACE FUNCTION` / `CREATE OR REPLACE VIEW` 泛化退役条件、实例掩码契约识别与学习任务约束，完整保留 `SECURITY DEFINER`、`SET search_path = pg_catalog, pg_temp` 与 `REVOKE ALL ... FROM PUBLIC`。

### 6. 双 SDK 边界

- **插件作者侧（`YOAO-Labs/Plugin-SDK`）**：`yoao-plugin-sdk[trainer]`（Python `>=3.10`，除 `pydantic>=2.5` 与标准库外零额外运行时依赖，不绑定 PyTorch/CUDA）与 `@yoao/plugin-sdk`（Node `>=20`，零运行时依赖，提供协议常量、TypeScript 类型与运行时校验函数）。
- **平台与监督器侧（`YOAO-Labs/ai-annotation-platform`）**：统一 `yoao-sdk` 下的 `training_contract.trainer_plugin`（Python `>=3.10`，零框架硬编码，JSON Schema 与 `yoao_plugin_sdk.trainer` 保持逐字段一致）。

## Consequences

正向：

- 训练框架依赖（Python 版本、PyTorch、CUDA、AGPL/MIT 许可与原生算法）完全隔离到可独立构建与升级的 OCI 镜像，`apps/training-executor` 退化为轻量化无 ML 依赖的 `training-supervisor`。
- 在线学习核心安全门禁（冻结快照、单调序号、GPU 互斥、`lost` 保租约、单调时钟预算、脱敏日志、预签名全量回读与受控模型回流）全部保留在平台核心，不向插件下放信任。
- 训练到推理的模型回流摆脱 `ultralytics` / `libreyolo` 字面量耦合，为第三方训练器接入提供可验证的数据驱动匹配路径。

负向：

- `training-supervisor` 需要维护容器运行时对账与强停逻辑，比单一进程组 `os.killpg` 增加一层运行时状态检查；本地开发仍需并行维护轻量隔离进程模式以支持单元测试与作者调试。
- 平台侧 `training_contract.trainer_plugin` 与独立仓 `yoao_plugin_sdk.trainer` 需要在契约变更时保持跨仓 JSON Schema 对等校验以防止漂移。

## Alternatives Considered（详）

**方案 B（长驻 HTTP GPU 训练守护进程）**：复用推理插件的长驻 HTTP 架构。未采用原因在于训练作业生命周期长达数小时且极易触发 CUDA OOM 或原生 C++ 扩展死锁；若采用长驻进程，取消或超时后难以在进程内彻底回收显存与全部工作线程，且迫使训练器镜像开放网络监听端口，削弱 `--network=none` 离线沙箱边界。

**方案 C（平台执行器单镜像内维护多套 virtualenv 子进程）**：沿用现有子进程 `flock` 继承与 `os.killpg`。未采用原因在于无法隔离系统级 CUDA/cuDNN 驱动依赖与不同框架的开源许可边界，且每新增或升级一个训练框架都必须重新打包并发布平台核心镜像，违背插件可独立安装与退役的目标。

## Notes

- 实施路线：见 [训练器插件化架构与实施计划](../plans/1795600466_trainer-pluginization.md)（T0 契约冻结 -> T1 Learning Owner 收口与 SQL 泛化 -> T2 注册表与 `training-supervisor` -> T3 官方训练器拆仓至 `YOAO-Plugins` 并删除旧实现 -> T4 单卡 GPU 实测与回流验证）。
- T0 契约实现位置：
  - 平台侧：`packages/python-sdk/src/training_contract/trainer_plugin.py`、`packages/python-sdk/tests/contracts/training_contract/test_trainer_plugin.py`
  - 独立插件 SDK：`YOAO-Labs/Plugin-SDK` 仓内 `packages/python/src/yoao_plugin_sdk/trainer/__init__.py`、`packages/js/src/trainer.mjs`、`packages/js/src/index.d.ts`
- T0–T3 与 [ADR-0190](archive/0190-inference-plugin-generalization-and-direct-tier.md) 推理插件升级均已完成验收：
  - `yoao_learning_v1.trainer_plugins`（迁移 `bo0514`）、超管 API/SDK/CLI、多槽位资源模型、共享 `ContainerRuntimePort` 生命周期扩展及无 ML 依赖双轨 `training-supervisor` 已在隔离环境下通过真实 CPU 哑训练器端到端验收；
  - T3 已将官方参考训练器提取至独立插件仓库 `YOAO-Plugins`（`plugins/trainer/ultralytics/` 与 `plugins/trainer/libreyolo/`，非 root `USER 10001:10001`），并从主仓库彻底删除静态 ML 训练器实现、`TRAINING_CATALOG_JSON` 与框架专属契约字典/库存 JSON，同时新增主仓库零 ML 框架导入 AST 边界守卫与跨仓契约对等验证，当前具备启动 **T4** 真实单卡 GPU 验收的全部前提。
