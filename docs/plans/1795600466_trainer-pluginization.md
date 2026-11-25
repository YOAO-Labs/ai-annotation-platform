# 训练器插件化架构与实施计划

> 状态：规划草案（待维护者确认后按 T0 至 T4 分阶段实施）。
>
> 创建日期：2026-11-25。
>
> 关联上下文：承接已收口的 [平台插件化计划（M0-M4）](1795366269_platform-pluginization.md) 与 [ADR-0186](../adr/archive/0186-modular-monolith-and-on-demand-extraction.md)、[ADR-0180](../adr/0180-installable-inference-plugins.md)、[ADR-0090（版本化 YOLO 身份与资产）](../adr/archive/0090-versioned-yolo-identities-and-content-assets.md) 与在线学习设计约束（[训练数据集与执行器架构](../../docs-site/dev/concepts/training-datasets.md)）。统一插件 SDK 落点为独立仓库 `YOAO-Labs/Plugin-SDK`（`yoao-plugin-sdk[trainer]` 与 `@yoao/plugin-sdk`）。

## 1. 目标与非目标

### 目标

| 编号 | 目标                                                                                                                                                                                                                                                                       |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| G1   | **训练框架彻底解耦**：把 `ultralytics`、`libreyolo` 等具体训练框架从平台核心镜像（`apps/training-executor`）、共享契约（`apps/_shared/training_contract`）、API 校验与 `yoao_learning_v1` SQL 中抽离为独立可安装的 `trainer` 扩展点。                                      |
| G2   | **保留在线学习全部硬不变量**：冻结数据集快照、单调序号流、GPU 互斥锁与孤儿作业回收、`lost` 状态保租约、单调时钟 `ProcessBudget`、预签名对象存储回读校验与受控模型回流资格不得因插件化而退化。                                                                              |
| G3   | **统一扩展点与多环境隔离**：训练器插件使用统一清单 `yoao-extension-manifest/v1`（`extension_point: "trainer"`、`protocol_version: "yoao-trainer/v1"`）与 `yoao-plugin-sdk[trainer]`，每个训练器镜像独立锁定 Python、PyTorch、CUDA 与框架依赖，无需修改或重建平台核心镜像。 |
| G4   | **数据驱动的训练到推理兼容匹配**：废除 `framework == "ultralytics"                                                                                                                                                                                                         | "libreyolo"`硬编码分派，改为由训练器产出声明式产物兼容性描述符，与推理插件的`managed_model_compatibility` 和能力声明进行结构化比对。 |

### 非目标

- 不引入长驻 HTTP GPU 训练守护进程来替代一次性隔离作业（见 §3.1 选型理由）。
- 不在本计划内实现多机多卡分布式训练调度（DDP/FSDP）或前端训练器市场 UI（前端市场仍归属后续 M5 独立计划）。
- 不修改已有历史 `TrainingDatasetVersion`、`TrainingRun`、`TrainingAttempt` 与 `TrainedModelVersion` 的冻结快照、序号或收据。
- 不将数据库凭据、对象存储凭据、平台管理员 Token 或 Docker socket 暴露给任何训练器插件。

## 2. 现状耦合基线（实测盘点）

训练子系统当前在四层硬编码了 `ultralytics`（`8.4.183` / 历史 `8.4.177`）与 `libreyolo`（`1.6.0`）：

| 层级         | 关键文件与位置                                                                                                                                                                                                                                                                                                                                                                                                            | 耦合表现                                                                                                                                                                                                                                                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 共享契约     | `apps/_shared/training_contract/training_contract/{catalog,models,validation,dataset,native_architecture,model_return,inference_runtime,initialization,preview}.py` 与 `native_inventory*.json`                                                                                                                                                                                                                           | 硬编码 `ADAPTERS`、34 个 `MODEL_SPECS`、`ULTRA`/`LIBRE` 参数字典、4 套 YOLO/RLE 数据集契约、`model_return.py` 中的 `LIBRE_RUNTIME`/`SEGMENTATION_RUNTIME` 源码哈希、`inference_runtime.py` 中的 `LIBRE_CUDA_RUNTIME`、`ultralytics` 专属初始化与预览策略。                                                               |
| 训练执行器   | `apps/training-executor/{Dockerfile,pyproject.toml}` 与 `training_executor/{worker,adapters,train,ultralytics_adapter,libreyolo_adapter,instance_rle,native_identity,initialization,preview,preview_worker,evaluation}.py`                                                                                                                                                                                                | 单一镜像同时烘焙 `ultralytics`、`libreyolo`、`torch` 与字体；`adapters.py:194-201` 静态 `if/elif` 分派；`worker.py:70,101-125` 直接 `import torch` 并强制反射 `ultralytics` 版本；`instance_rle.py` 顶层导入 `torch`/`ultralytics`；`evaluation.py:57-163` 硬编码两套评估器源码哈希。                                    |
| API 与服务   | `apps/api/app/config.py:223-228`、`apps/api/app/schemas/{training_run,training_hub,training_study,learning_policy,training_recipe,training_preview,training_initialization,training_dataset,sample_evaluation}.py`、`apps/api/app/services/{training_adapters,training_runs,training_claims,training_evaluations,trained_models,managed_inference,sample_evaluations,active_learning,plugin_installation_observation}.py` | 全局单执行器配置（单 `training_gpu_uuid` 与单预览槽位）；Pydantic 与路由查询字面量限定 `Literal["ultralytics", "libreyolo"]`；冻结样本评估与主动学习拒绝 `framework != "ultralytics"`；`trained_models.py` 直接实例化 `MLBackendService`、`JobsDomainQueryPostgresData` 并调用私有 `MLBackendRouter._build_candidates`。 |
| 数据库与 SQL | `apps/api/alembic/sql/0491_learning_dataset_boundary.sql:1-55`、`apps/api/alembic/sql/0492_learning_models.sql:331,974,982`、`apps/api/alembic/sql/0493_learning_selection.sql:78-94`、`apps/api/app/db/models/trained_model.py:103,148`                                                                                                                                                                                  | `advance_model_deployment_retirement()` 写死 `AND v.framework = 'libreyolo'`；`pending_dataset_mask_*` 按 `v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'` 过滤；任务约束限定 `('detect', 'segment')`；数据集默认格式写死 `'ultralytics-yolo'`。                                                                   |

## 3. 目标架构与职责边界

### 3.1 为什么训练器采用 Supervisor 驱动的一次性隔离作业，而不是长驻 HTTP 守护进程

推理插件采用长驻 HTTP 服务是因为在线预标注需要毫秒级响应与 LRU 显存复用。训练任务的约束恰好相反：

1. **作业退出即显存释放**：训练作业持续数十分钟至数小时，终止或超时必须彻底回收容器内全部进程与 CUDA 上下文。一次性隔离容器退出后由内核彻底回收显存与文件描述符，不需要重造推理侧复杂的签名排空与分配器缓存证明链路。
2. **容器边界的互斥锁与崩溃恢复语义（不退化既有硬门禁）**：原单镜像子进程通过继承 `/state/gpu-<sha24>.lock` 文件描述符和 `os.killpg` 防止 supervisor 崩溃后提前释放 GPU；当训练器拆出为独立 Docker/OCI 容器时，由守护进程（`dockerd`）管理的容器**不会继承** supervisor 的文件描述符，也无法由 `os.killpg` 停止。因此 `training-supervisor` 必须按三项硬规则保持同等互斥保证：
   - **先落盘后启动**：在调用容器运行时 `create/start` 之前，把确定性容器名/标签（绑定 `gpu_resource_id`、`attempt_id`）与 `spawn_intent` 原子写入 `/state/assignment.json`。
   - **启动/恢复期先对账容器再申领**：supervisor 重启持有 `/state/gpu-<sha24>.lock` 后，在调用 `POST /claim` 之前先向容器运行时查询该 GPU 资源的存量受管容器；凡存在运行中容器，必须先执行运行时停机并确认退出后才能提交恢复或申领新任务。
   - **以运行时退出状态作为租约释放前提**：停止、取消或预算到期时，通过容器运行时 API 执行 `stop`/`kill`、`wait` 并复核 `Running == false`，只有确认容器完全退出才向平台上报 `process_exited: True` 并释放 `TrainingResourceLease`。（本地开发隔离进程模式仍保留子进程 `flock` 继承与 `os.killpg`。）
3. **默认断网沙箱**：训练器插件作业阶段完全运行在 `--network=none`、`cap_drop: [ALL]`、`no-new-privileges:true` 的只读输入沙箱中，既无平台 Token，也无 S3 凭据，杜绝训练期联网下载权重或外泄标注数据。

### 3.2 三方职责边界

| 职责             | 平台核心（Learning + Assets Owner）                                                                                  | 平台执行监督器（`training-supervisor`，无 ML 依赖）                                                                                                               | 训练器插件（`extension_point: "trainer"`）                                                    |
| ---------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 数据集冻结与血缘 | 唯一权威：`TrainingDatasetVersion` 两阶段冻结、单调序号、防泄漏切分、`source_lineage_contract`、不可变 `storage_ref` | 下载并校验 `dataset.zip` SHA-256，安全解压到只读 `/work/input/dataset`                                                                                            | 只读消费声明支持的规范数据集契约，产出 `input-membership.json` 证明真实加载样本集             |
| 租约与预算控制   | 唯一权威：`TrainingAttempt`、`TrainingResourceLease`、`lost` 保租约、撤权复核、Study 预算账本                        | 持有 `/state/gpu-<sha24>.lock`、`assignment.json`（含容器身份与 `spawn_intent`）、启动容器对账、心跳停机、`ProcessBudget` 单调时钟看门狗、容器 API 强停与退出复核 | 响应 `SIGTERM`/`SIGINT` 做优雅收尾并落盘最后检查点，不参与租约决策                            |
| 对象存储与日志   | 唯一权威：`TrainingRun.storage_ref`、`TrainingSavedIO`、签发短效预签名 PUT、全量回读校验 SHA-256 与 `attempt-id`     | 捕获容器/子进程 `stdout`/`stderr`，按 8 KiB 行截断并脱敏后写入 8 MiB `ObservationSpool`；统一上传产物并提交 fenced `finish`                                       | 仅向本地 `/work/output` 写入权重、检查点、指标与收据，不接触任何对象存储凭据                  |
| 训练算法与环境   | 不含任何 PyTorch / CUDA / 框架代码；仅按注册资格与能力清单校验参数与产物收据                                         | 按已批准的 `@sha256:...` 镜像摘要启动隔离作业（GPU 训练或容器边界强制 CPU-only 预览）                                                                             | 拥有独立 Python/PyTorch/CUDA 环境、参数 Schema、模型结构与原生训练/评估/增强预览逻辑          |
| 模型回流与发布   | 唯一权威：`TrainedModelVersion`、兼容性比对、推理侧安装回执核验、固定样本试跑、人工 CAS 发布与退役                   | 不参与推理部署或发布决策                                                                                                                                          | 仅在 `result.json` 中声明产物格式、训练运行时事实与兼容推理运行时约束；发现不等于具备上线资格 |

### 3.3 统一清单与 `yoao-trainer/v1` 协议契约

#### 清单（`yoao-extension-manifest/v1`）

```json
{
  "manifest_schema": "yoao-extension-manifest/v1",
  "extension_point": "trainer",
  "plugin_id": "yoao.trainer.ultralytics",
  "version": "1.0.0",
  "display_name": "Ultralytics YOLO Trainer",
  "description": "Official Ultralytics detection and instance segmentation trainer plugin.",
  "publisher": "YOAO Official",
  "license": "AGPL-3.0-only",
  "protocol_version": "yoao-trainer/v1",
  "entrypoint": ["yoao-trainer-ultralytics"],
  "capabilities": ["train", "evaluate", "preview", "export_weights"],
  "resource": { "memory_mb": 8192 },
  "extension": {
    "framework_id": "ultralytics",
    "framework_version": "8.4.183",
    "supported_tasks": ["detect", "segment"],
    "supported_dataset_contracts": [
      "ultralytics-bbox-1",
      "ultralytics-simple-polygon-1",
      "ultralytics-instance-rle-1"
    ],
    "requires_gpu": true,
    "preview_cpu_only": true,
    "allowed_egress_hosts": []
  }
}
```

> 关键约束：manifest 禁止自报 `source_tier`；`entrypoint` 禁止 shell 解释器与 `python -c`；安装时必须由平台绑定不可变 OCI `@sha256:...` 摘要，并在执行资格核验后由管理员显式启用。

#### 命令与文件协议（`yoao-trainer/v1`）

每个训练器插件镜像通过 `entrypoint` 暴露 4 个确定性子命令（由 `yoao-plugin-sdk[trainer]` 提供强类型模型与原子写入原语）：

1. **`<entrypoint> describe`**（无网络、无 GPU）
   - 向 `stdout` 输出规范化 JSON `TrainerCapabilitiesV1`：
     - `protocol_version`: `"yoao-trainer/v1"`
     - `framework_id` / `framework_version` / `runtime_versions`（`python`、`torch`、`cuda` 等）
     - `supported_tasks`、`supported_dataset_contracts`
     - `architectures`（模型规格、支持任务、离线基座权重期望 `base_asset_sha256`、`end2end` 标记）
     - `parameter_schema`（JSON Schema + 托管/非托管参数分类与跨字段约束规则版本）
     - `evaluator_descriptors`（评估器实现标识与源码 `.py` SHA-256）
     - `policy_digests`（预算策略、输入成员校验策略、权重初始化策略摘要）
     - `serving_compatibility`（显式声明产物格式 `format` 与目标推理插件兼容矩阵；`format: null` 仅表示原生权重而非通配符，禁止空串或省略）
2. **`<entrypoint> validate-config --spec /work/input/spec.json`**（无网络、无 GPU）
   - 校验冻结训练配置、架构 YAML 与可选初始化权重兼容性，退出码 `0` 时输出规范化响应模型（`ValidateConfigResultV1`，含 `resolved_config` 与 `schema_digest`），失败返回结构化错误 JSON。
3. **`<entrypoint> train --spec /work/input/spec.json --output /work/output`**（`--network=none`，单卡 GPU 或显式 CPU）
   - 只读输入目录 `/work/input`：
     - `spec.json`（`TrainSpecV1`：`run_id`、`attempt_id`、`framework_id`、`task`、`architecture_id`、`dataset_contract`、`dataset_dir`、`parameters`、`base_weights_path`、`resume_checkpoint_path`、`init_weights_path`、`native_budget`；`resume_checkpoint_path` 与 `init_weights_path` 互斥）
     - `dataset/`（校验通过后的冻结数据集目录）
     - `assets/`（节点本地只读预置基座权重与离线字体）
   - 可写输出目录 `/work/output`：
     - `events.jsonl`（每条 `TrainEventV1` 追加后 `fsync`；顶层字段为 `seq`、`kind`、`phase`、`epoch`、`total_epochs`、`progress`、`metrics`、`detail`，其中批量吞吐 `batch_throughput` 与显存统计 `cuda_memory_mb` 统一置于 `metrics` 或 `detail` 内）
     - `checkpoint-NNNN.<ext>` + 原子替换 `checkpoint.json`（仅在新 `checkpoint.json` 提交并 `fsync` 后删除旧检查点）
     - 终态产物：`best.<ext>`、`metrics.json`（含 `comparison` 评估器源码摘要）、`native-config.json`（含实际原生配置与初始化指纹）、`input-membership.json`（证明真实加载样本与冻结集完全一致）、以及统一终态收据 `result.json`（`TrainReceiptV1`，通过 `status: "succeeded" | "failed" | "cancelled"`、`artifacts`、`serving_compatibility`、`failure_code`、`failure_message` 统一表达成功、失败与取消，不另设第二套 `failure.json` 协议）。
4. **`<entrypoint> preview --spec /work/input/preview-spec.json --output /work/output`**（可选，仅当声明 `preview` 能力时生效）
   - **资源与 CPU-only 边界由 `PreviewSupervisor` 在容器/OS 层强制执行**：不挂载 `--gpus`，注入 `NVIDIA_VISIBLE_DEVICES=void` 与 `CUDA_VISIBLE_DEVICES=""`，施加 `--cpus=1`、`--memory`（上限与现有 16 GiB 内存预算对齐）、`--network=none` 与 45s 墙钟超时强杀；官方 PyTorch 插件内部的 `torch.cuda.is_initialized() == False` 检查仅作为纵深防御，绝不依赖第三方插件自报遵守资源约束。
   - 输入 `preview-spec.json`（`PreviewSpecV1`）与输出 `preview-*.png` + `report.json`（`PreviewReportV1`）的强类型模型在 T0 阶段并入 `yoao_plugin_sdk.trainer`。

### 3.4 必须保留的安全与 Owner 不变量清单

任何阶段实施时都必须以自动化测试持续守卫以下不变量：

1. **发现不等于资格（[ADR-0090](../adr/archive/0090-versioned-yolo-identities-and-content-assets.md)）**：注册训练器镜像并运行 `describe` 仅完成能力发现；只有经平台核验镜像摘要、离线基座资产 SHA-256、能力收据与真实隔离冒烟报告（`qualification_report_sha256`）后，管理员才可批准该训练器参与正式训练。
2. **输入与收据防篡改**：API 在 `finish` 时继续执行全量对象流回读（核对大小、`x-amz-meta-attempt-id`、SHA-256），继续校验 `native-config.json`、`input-membership.json`、`observability.json` 与必选产物角色；不信任训练器插件自报的成功状态或跳过样本成员核对。
3. **`TrainingSavedIO` 路径语法不破坏**：所有训练器产物均由 `training-supervisor` 按既有 `attempts/{attempt_id}/{name}` 路径规范上传，保持与 `yoao_assets_source` 和 `apps/api/alembic/sql/0507_training_saved_io.sql` 完全兼容。
4. **租约与撤权串行化**：心跳超时置 `lost` 仍保留 `TrainingResourceLease`；项目管理员权限或 API Key 作用域撤销时，在 admission、claim、heartbeat、finish、resume 五个节点持续复核并触发停机。
5. **模型回流独立门禁**：训练器产出的 `serving_compatibility` 仅声明“该权重属于何种格式与训练运行时”；推理插件安装仍需经过 `POST /managed-models` 回执核对、固定验证图 `/predict` 试跑、以及人工 `(project_id, task)` CAS 发布。

## 4. 分阶段实施路线（T0 至 T4）

每个阶段均按“契约冻结 -> 核心解耦 -> 实现切换 -> 删除旧硬编码 -> 实测验收”组织，阶段结束均可独立验证。

### T0：ADR 与 `yoao-trainer/v1` 契约冻结

- 产出架构决策文档 `docs/adr/0189-trainer-extension-point.md`（正式冻结一次性作业协议、容器化互斥与崩溃恢复语义、离线安全边界、资格门禁与数据驱动的模型回流兼容规则）。
- 在 `Plugin-SDK`（`yoao_plugin_sdk.trainer` 与 `@yoao/plugin-sdk`）补齐并冻结 `yoao-trainer/v1` 全量模型：既有 `TrainerCapabilitiesV1`、`TrainSpecV1`、`TrainEventV1`、`TrainReceiptV1`、`ServingCompatibilityV1`、`write_event_jsonl`、`atomic_write_json`，以及本阶段补齐的 `ValidateConfigResultV1`、`PreviewSpecV1`、`PreviewReportV1` 和冻结数据集/配置摘要字段。
- 明确与主仓 `yoao-sdk` 单包收敛的衔接：平台侧框架无关契约保留在 `training_contract`（后续并入 `yoao-sdk`），不再新增任何具体框架常量。
- **验收**：`Plugin-SDK` Python 3.10/3.11/3.12 与 Node 20 契约测试全部通过；ADR 获维护者确认。

### T1：Learning Owner 边界收口与 SQL 泛化（前置清理切片）

在不改变现有 `ultralytics` / `libreyolo` 行为的前提下，先解除阻塞多训练器的数据库、SQL 与服务耦合：

- **只通过新增前向迁移更新 SQL（严禁原地修改已冻结历史 SQL 文件）**：
  - 保持 `apps/api/alembic/sql/0491_learning_dataset_boundary.sql`、`0492_learning_models.sql`、`0493_learning_selection.sql` 冻结不变；新增前向迁移（如 `0512_trainer_extension_generalization.py`）使用 `CREATE OR REPLACE FUNCTION` / `CREATE OR REPLACE VIEW`，完整保留 `SECURITY DEFINER`、`SET search_path = pg_catalog, pg_temp`、`REVOKE ALL ... FROM PUBLIC` 与受限 LOGIN 权限守卫测试：
    - 更新 `yoao_learning_v1.advance_model_deployment_retirement()`：移除 `AND v.framework = 'libreyolo'`，改为按部署记录的显式退役能力标记判断是否进入 `draining -> retired`。
    - 更新 `yoao_learning_v1.pending_dataset_mask_*` 与数据集边界视图：将 `v.snapshot->>'bridge' = 'ultralytics-instance-rle-1'` 泛化为规范数据集契约中的 `mask_storage_kind = 'instance_rle'` 属性（向后兼容历史快照），并放开非 `ultralytics-yolo` 数据集契约识别。
- **API Schema 与服务去字面量**：
  - 移除 `apps/api/app/schemas/*.py` 与路由中的 `Literal["ultralytics", "libreyolo"]`，改为通过已注册训练器能力表动态校验 `framework`、`task`、`base_model` 与 `dataset_contract`。
  - 解除 `sample_evaluations.py`、`managed_inference.py`、`active_learning.py` 对 `framework == "ultralytics"` 的硬编码限制，改为检查推理后端能力 `managed_evaluation` 与模型兼容性描述符。
- **收口 Learning 跨模块直连泄漏**：
  - 清理 `training_runs.py`、`training_studies.py`、`learning_policies.py`、`trained_model_backend.py`、`trained_models.py` 对 `User`/`Project` ORM 模型及 `MLBackendRouter._build_candidates` 私有方法的直接依赖，改走既有只读关系与公开端口（新增 AST 边界守卫，与 M4c 格式适配器守卫口径一致）。
- **验收**：既有在线学习、模型回流、冻结样本评估、迁移升级（含存量库 `alembic upgrade head`）与边界守卫测试全绿；零历史数据语义漂移。

### T2：平台训练器注册表、资格门禁与轻量化 `training-supervisor`

- **领域 Owner 注册表（`yoao_learning_v1.trainer_plugins`）**：
  - 沿用 `notification_channels` 与 `format_adapter_plugins` 的领域 owner 归属模式，在 `yoao_learning_v1` 新增训练器插件安装表（含不可变 `artifact` `@sha256:...`、平台权威 `source_tier: official | third_party`、核验后的 `capabilities_snapshot`、`qualification_report_sha256`、`status: registered | qualified | enabled | disabled | retired`）。
  - 提供超管 REST 接口与 `yoao plugins` CLI 支持（注册、查看能力、导入资格报告、启用、停用、退役）；同步扩展资源槽位模型，解除 `config.py` 全局单一 `training_gpu_uuid` 对多训练器共存的限制（单张物理 GPU UUID 同一时刻仍严格受单一 `TrainingResourceLease` 互斥保护）。
- **轻量化 `training-supervisor`**：
  - 剥离 `apps/training-executor` 中所有 ML 框架依赖（`torch`、`torchvision`、`ultralytics`、`libreyolo`、`pycocotools`），并将 `worker.py:70` 的 `import torch` GPU 探测替换为无框架依赖的 `nvidia-smi` / NVML 探测；保留纯净监督层（`/state/gpu-<sha24>.lock`、`assignment.json` 容器身份与启动对账、`ProcessBudget`、`LogCapture`、`ObservationSpool`、预签名传输、以及在容器边界强制 CPU-only 与资源上限的 `PreviewSupervisor`）。
  - 通过隔离执行后端（受管 OCI 容器运行器，强制 `--network=none`、`cap_drop: [ALL]`、`no-new-privileges`、`pids_limit`、`shm_size`、只读 `/work/input` 与 `/models:ro`，停止时走容器 API `kill`+`wait`+`Running == false` 确认；同时保留本地开发隔离进程模式用于单元测试与作者调试）驱动 `yoao-trainer/v1` 协议。
- **验收**：用基于 `yoao-plugin-sdk[trainer]` 编写的极简 CPU 哑训练器插件跑通注册 -> 资格校验 -> 训练 -> 心跳/日志脱敏 -> 取消与容器退出确认 -> 模拟 supervisor 崩溃后重启孤儿容器回收 -> 预算超时中止 -> 产物回读校验 -> 停用/退役全流程，证明平台核心完全不感知具体训练框架。

### T3：官方参考训练器拆仓至 `YOAO-Plugins` 并删除旧静态实现

- 在 `YOAO-Plugins` 新增 `plugins/trainer/ultralytics/` 与 `plugins/trainer/libreyolo/`，各自独立维护 `manifest.json`、Dockerfile、哈希锁定 `requirements.lock` 与 CI 校验，统一依赖 `yoao-plugin-sdk[trainer]`：
  - `plugins/trainer/ultralytics/` 完整承接 `UltralyticsAttemptTrainer`（离线字体拦截、首 epoch 禁 OOM 自动减半 batch）、`InstanceRLETrainer`、`native_identity`、权重初始化张量指纹校验、`input-membership.json`、评估器源码哈希与增强预览逻辑。
  - `plugins/trainer/libreyolo/` 完整承接 `weights_only=True` 检查点校验、`StrictResumeLogging`、`ObservedTrainer`/`ObservedValidator` 与 `libreyolo-native-state-dict-1.0` 回流描述符。
- 在主仓删除旧 `apps/training-executor/training_executor/{ultralytics_adapter,libreyolo_adapter,instance_rle,evaluation,train,preview,native_identity,initialization}.py`、移除 `adapters.py` 中的框架静态分派，以及删除 `apps/_shared/training_contract` 中所有框架专属字典、版本常量与 `native_inventory*.json`（按“迁移调用方后立即删除旧实现”原则，不在核心保留双轨兼容层）。
- **验收**：主仓代码不再出现 `import ultralytics`、`import libreyolo` 或执行器侧 `import torch`；官方两款训练器在 `YOAO-Plugins` 独立 CI 下通过契约校验与离线构建。

### T4：真实单卡 GPU 端到端验收与模型回流资格验证

在一次性测试数据库、隔离对象存储与真实本机单卡 GPU 环境下完成端到端实测：

1. 使用提取后的 `ultralytics` 训练器插件完成真实检测与实例分割（RLE）训练、容器边界 CPU-only 增强预览、运行中取消与容器退出确认、`ProcessBudget` 超时中止、心跳失联置 `lost` 保租约与 fenced 恢复、以及 supervisor 崩溃重启回收孤儿容器验证。
2. 使用提取后的 `libreyolo` 训练器插件完成真实检测训练、`weights_only=True` 断点续训与产物回读验证。
3. 将两款训练器产出的 `TrainedModelVersion` 分别部署至 `YOAO-Plugins` 中对应的推理插件，验证 `POST /managed-models` 回执核对、固定验证图 `/predict` 试跑、人类显式 CAS 发布、推理预标注调用与安全退役。
4. 在 `docs/plans/evidence/1795600466_trainer-pluginization/` 记录脱敏实测台账，并同步更新 `docs-site/user-guide/`、`docs-site/dev/concepts/training-datasets.md`、`CHANGELOG.md` 与本计划 `## Outcome` 章节。

## 5. 与双包生态的衔接

| 仓库                               | 包名                                                                         | 在训练器插件化中的角色                                                                                                                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `YOAO-Labs/Plugin-SDK`             | `yoao-plugin-sdk`（`[trainer]` extra，无额外运行时依赖）、`@yoao/plugin-sdk` | 训练器作者唯一依赖的公开开发包：提供 `yoao-extension-manifest/v1`（`extension_point: "trainer"`）校验、`yoao-trainer/v1` 协议数据模型、原子事件/检查点写入原语与 `yoao-plugin init --type trainer` / `validate` CLI。支持 Python `>=3.10`，不强绑定任何 PyTorch 或 CUDA 版本。 |
| `YOAO-Labs/ai-annotation-platform` | 现有 5 个便携契约包（后续收敛为单一 `yoao-sdk`）                             | 平台核心与 `training-supervisor` 消费的平台侧契约与控制面客户端（含管理员 `yoao plugins` 安装与资格管理 CLI）。训练器插件作者无需安装主仓内部契约包即可开发兼容 `yoao-trainer/v1` 的训练器。                                                                                   |
| `YOAO-Labs/YOAO-Plugins`           | `plugins/trainer/ultralytics`、`plugins/trainer/libreyolo`                   | 官方训练器参考实现与发布镜像源，与现有 `plugins/inference/*`、`plugins/notification-channel/*`、`plugins/format-adapter/*` 并列组织。                                                                                                                                          |

## 6. 当前状态与待决事项

- **当前状态**：已完成训练子系统全链路耦合盘点与 `Plugin-SDK` `yoao-trainer/v1` 核心模型落地（待进入 T0 ADR 编写并补齐 `ValidateConfigResultV1`/`PreviewSpecV1`/`PreviewReportV1`）。
- **待维护者确认**：
  1. `T0` 启动时机：是否立即按 T0 -> T1 -> T2 -> T3 -> T4 顺序推进，还是先完成 `ai-annotation-platform` 内部 5 个契约包合并为 `yoao-sdk`。
  2. 训练器镜像执行方式：自托管默认 `training-supervisor` 通过受限容器运行通道调用本地 Docker Engine 启动训练器容器（宿主 API 仍绝不接触 Docker socket），还是同时保留 Podman/OCI runtime 抽象接口。
