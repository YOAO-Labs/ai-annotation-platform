# yolo-backend

ai-annotation-platform 的第三个 ML backend（v0.14.12）—— 基于 [ultralytics](https://github.com/ultralytics/ultralytics) 8.4.x 提供 **detection / segmentation(实例) / keypoint / obb** 四任务批量预标。

与 `apps/grounded-sam2-backend` / `apps/sam3-backend` 平级，使用同一份 ml-backend 协议 v2 接入平台。

## 范围

| series  | det | seg | pose | obb | sizes       |
| ------- | :-: | :-: | :--: | :-: | :---------- |
| YOLOv8  | ✅  | ✅  |  ✅  | ✅  | n/s/m/l/x   |
| YOLOv9  | ✅  | c/e |  –   |  –  | t/s/m/c/e   |
| YOLOv10 | ✅  |  –  |  –   |  –  | n/s/m/b/l/x |
| YOLO11  | ✅  | ✅  |  ✅  | ✅  | n/s/m/l/x   |
| YOLO12  | ✅  |  –  |  –   |  –  | n/s/m/l/x   |
| YOLO26  | ✅  | ✅  |  ✅  | ✅  | n/s/m/l/x   |
| RT-DETR | ✅  |  –  |  –   |  –  | l/x         |

矩阵基于 [`ultralytics/assets` release v8.3.0 + v8.4.0](https://github.com/ultralytics/assets/releases) 实际预训练权重核对（2026-06-08）；不在矩阵内的组合，`/setup` 不暴露 + `/predict` 返回 422 `variant_not_supported`。

**不做**：classification、YOLO26 的 sem/depth/reid/s3d/objv1 头、tracker、训练（`/fit`）、TensorRT/ONNX 加速、batch 推理优化。具体见 v0.14.12 计划文件 §2.2。

## 端点

| 端点                                        | 用途                                                            |
| ------------------------------------------- | --------------------------------------------------------------- |
| `GET /health`                               | 健康检查 + 池状态 + GPU/容器 PerfHud                            |
| `GET /setup`                                | 协议 v2 多模型目录（4 model × series × size variants）          |
| `GET /versions`                             | backend / ultralytics 版本                                      |
| `POST /predict`                             | 批量预测；入参 `context.{type, variants, params}` + `tasks[]`   |
| `POST /warmup`                              | 受 borrower 保护的模型预热                                      |
| `POST /drain` / `/drain/cancel`             | 受签名 generation fencing 控制的排空 / 恢复                     |
| `POST /unload`                              | 无 body 保留 legacy 兼容；带 generation body 时执行受管全池卸载 |
| `POST /lifecycle/mode` / `/lifecycle/reset` | gate 握手与 unmanaged 驻留可信清场                              |
| `GET /metrics`                              | Prometheus                                                      |

详见 [`docs-site/dev/reference/ml-backend-protocol.md`](../../docs-site/dev/reference/ml-backend-protocol.md) §4.1。

## 环境变量

| 名                                | 默认               | 作用                                                  |
| --------------------------------- | ------------------ | ----------------------------------------------------- |
| `YOLO_DEVICE`                     | `cuda:0`           | torch device                                          |
| `YOLO_MODEL_POOL_CAP`             | `2`                | LRU 池容量                                            |
| `YOLO_MANAGED_MODELS_TOKEN`       | 空                 | 至少 32 个随机字符；受控模型安装和推理的 Bearer 凭据  |
| `YOLO_BUILD_TIMEOUT`              | `30`               | 单次 build 超时(秒)                                   |
| `YOLO_IDLE_UNLOAD_SECONDS`        | `600`              | 空闲卸载触发阈值；`<=0` 关闭                          |
| `YOLO_IDLE_CHECK_INTERVAL`        | `60`               | 空闲检查周期(秒)                                      |
| `YOLO_STRICT_OFFLINE`             | `0`                | 1 时缺权重报 400, 不去 GH download                    |
| `YOLO_CHECKPOINTS_DIR`            | `/app/checkpoints` | 权重落盘位置                                          |
| `YOLO_MANAGED_LIFECYCLE_VERIFIED` | `0`                | 当前部署通过真实 GPU 全池回落验收后设为 `1`           |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON`  | 空                 | Ed25519 验签公钥 keyring JSON；空值只启用 legacy gate |
| `LOG_LEVEL`                       | `INFO`             | python logging                                        |

## 部署

```bash
# 容器启动
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-yolo up yolo-backend

# 健康
curl http://localhost:8003/health

# 能力探测
curl http://localhost:8003/setup | jq .

# 离线场景: 先在有网环境预下载
python scripts/download_weights.py --series yolo11
```

### 受控基础模型与同机缓存

训练器与 YOLO Backend 使用同一个环境的 `MINIO_TRAINING_BUCKET` 保存权威基础权重，
对象键固定为 `model-assets/sha256/<sha256>/weights.pt`。训练产物保持原有项目与执行路径，
不因共用存储桶而变成公共资产。桶名相同还需核对存储服务与对象键；同机文件去重则需核对实际挂载源。

管理员准备经过审核的 JSON 清单：`version` 为 `1`，`assets` 为数组，每项填写
`filename`、64 位小写 `sha256`、实际 `size_bytes` 及上述固定 `object_key`。
仓库的 `scripts/controlled-model-assets.json` 提供 34 个目标组合的确切大小、摘要和固定官方下载来源，
可以筛选成当前部署要开放的子集。清单不包含权重本体，也不会自动启用训练目录或授予设备资格。
清单须描述受控目录所采用的确切文件；下载同名文件不能代替校验。已有预下载脚本提供受控模式，
该模式不导入模型框架、不申请 GPU，也不会在缺失时切换到其他上游文件：

```bash
# 在具备现有 MINIO_* 配置与 boto3 的管理员环境中执行，例如平台 API 虚拟环境。
# 本地导入、校验并上传权威对象；不会覆盖已有不同内容的对象。
apps/api/.venv/bin/python apps/yolo-backend/scripts/download_weights.py \
  --asset-manifest /secure/reviewed-model-assets.json \
  --source-dir /secure/reviewed-weights \
  --target-dir /srv/aap/model-assets --publish

# 其他机器独立执行；每台机器的缓存目录可以不同。
apps/api/.venv/bin/python apps/yolo-backend/scripts/download_weights.py \
  --asset-manifest /secure/reviewed-model-assets.json \
  --from-bucket --target-dir /srv/aap/model-assets
```

命令默认采用现有 `MINIO_TRAINING_BUCKET`（未设置时为 `training`）和 `MINIO_ENDPOINT`，
可通过 `--bucket`、`--endpoint` 指定部署已有配置。管理员使用现有存储凭据，
运行中的 Backend 与执行器不需要 MinIO 管理凭据。各节点必须实际访问得到配置的端点；
跨机不能使用另一台主机的 `localhost` 或 Docker 内部主机名。

缓存以 `<sha256>.pt` 保存物理文件，`assets.json` 保存名称映射。写入使用节点内进程锁、
临时文件和原子发布，大小及 SHA256 均通过后才对消费者可见。同一节点的并发准备复用同一份文件；
中断、超限或校验失败不会发布部分文件。已有受控映射缺失或损坏时加载失败，不能退回同名在线下载。
没有受控映射的历史内置模型仍遵守原有离线设置，应通过同一流程逐步迁移。

同机部署把两个 Compose 项目的 `TRAINING_MODELS_PATH` 设置为相同的**绝对路径**，
训练器只读挂载为 `/models`，Backend 只读挂载为 `/model-assets`。
不同机器各自准备本地缓存，无需 NFS；Docker 卷也不会自动跨机器共享。
Backend 的检查点状态、模型安装收据和评估日志继续保存在单独的可写卷中。
受控安装在该保护目录的 `.blobs/sha256` 复用相同摘要的权重，版本目录保留独立收据；
每次新安装仍检查任务、类别与原生运行时。取消或修复损坏缓存不会改写其他保留版本的文件。
执行器启动时只报告该节点已校验的资产，新增资产准备完成后需刷新执行器的启动快照。
桶内存在对象、另一节点可用或已有本地缓存，都不能代替本节点的镜像、设备和操作资格验收。

迁移顺序是复制、核验、切换消费者，最后由管理员在确认无活动读取且保留回滚副本后清理旧文件。
此命令不删除旧权重、训练产物或其他 Backend 缓存。存储暂时不可达时，已保留所需字节的实例
仍可加载本地模型；冷节点准备失败，数据包、续训检查点和评估样本仍各自依赖其下载链路。

受管生命周期的代码实现不等于当前部署已经通过验收。默认
`YOLO_MANAGED_LIFECYCLE_VERIFIED=0` 时，`/setup` 不发布 `managed_lifecycle`，Backend
拒绝进入 `enforce` 且不会报告可驱逐；只有当前镜像、权重和 GPU 完成多模型池加载、受管全池卸载与
显存回落基线验证后，才能把该开关设为 `1`。

独占目标卡并准备好 detect / segment / pose / OBB 四份业务权重后，在目标镜像内执行：

```bash
set -o pipefail
install -d -m 700 "$EVIDENCE_DIR"
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-yolo run --rm --no-deps --entrypoint python3 \
  -e VALIDATION_GIT_COMMIT -e VALIDATION_IMAGE_ID -e VALIDATION_GPU_UUID \
  -e VALIDATION_MODEL_APPROVAL_REF -e VALIDATION_FIXTURE_APPROVAL_REF \
  -e VALIDATION_WEIGHT_SHA256_JSON \
  yolo-backend /app/scripts/validate_managed_lifecycle.py \
  | tee "$EVIDENCE_DIR/yolo-managed-lifecycle.json"
jq -e '.passed == true and .runtime_ephemera_clean == true' \
  "$EVIDENCE_DIR/yolo-managed-lifecycle.json"
```

`VALIDATION_WEIGHT_SHA256_JSON` 必须精确列出本次验证的四份权重文件名与小写 SHA-256。
验收器会跑图像四任务、视频 tracker、两轮整池加载/卸载和共享故障矩阵；只有严格证据中
`passed=true` 才可以开启声明。完整的隔离、审批引用和回滚要求见 GPU 仲裁验收 runbook。
镜像已固定安装 tracker 所需的 `lap`，运行时不会联网 AutoUpdate；全池卸载还会清理 PyTorch
进程级 cuBLAS workspace，再释放 allocator cache，避免池对象已空但显存仍被工作区占用。验收器会先做
一次真实卷积 priming 与全卸载，以成熟 CUDA/cuDNN context 作为随后两轮 90% 工作集回收的稳定基线。

### 受控权重的部署资格

普通四任务验收不代替受控 `trained:<model_version_id>` 路径。部署项目训练得到的
检测或分割模型时，额外提供同一份真实 checkpoint、代表性原图及以下严格 JSON 夹具。
三个环境变量必须同时设置；未设置时保留原有普通模型验收。目标 GPU、运行镜像、pool cap
和业务权重仍须与待部署环境一致，目标卡必须独占。

```json
{
  "schema": 3,
  "task": "segment",
  "base_model": "yolo26n-seg",
  "framework_version": "8.4.183",
  "frozen_model_ref": {
    "model_version_id": "77123456-1234-4234-8234-123456789abc",
    "sha256": "<best.pt 的小写 SHA-256>",
    "classes_digest": "<有序原生类别数组的规范 JSON SHA-256>"
  },
  "classes": ["shape"],
  "confidence": 0.01,
  "max_det": 20,
  "min_instances": 1
}
```

`classes` 必须精确等于 checkpoint 的原生类别顺序；`classes_digest` 使用
`sha256(json.dumps(classes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())`。
`model_version_id` 可用正式候选身份，或批准的离线验收夹具 UUID；不得因此声称已完成平台注册。
`min_instances` 至少为 1：选择确实能产出实例的代表图和已批准阈值，空结果不能关闭原生 RLE 门禁。
夹具、原图和 checkpoint 均记录审批引用及内容摘要，不能用结构相似的其他权重代替。
`task`、`base_model`、`framework_version` 必须与权重的原生身份完全相符；更换家族、规格或运行时要分别验收。
旧 schema 1 仅保留 `yolo11n-seg`／Ultralytics `8.4.177` 身份，schema 2 则显式指定分割基础模型和版本。
旧夹具不能拿来证明新模型组合通过。

将这三个文件放在独立的 `FIXTURE_DIR`，挂载为只读；在前述普通验收命令中增加：

```bash
  -v "$FIXTURE_DIR:/fixtures:ro" \
  -e VALIDATION_CONTROLLED_MODEL_WEIGHTS=/fixtures/best.pt \
  -e VALIDATION_CONTROLLED_MODEL_FIXTURE=/fixtures/controlled.json \
  -e VALIDATION_CONTROLLED_MODEL_IMAGE=/fixtures/representative.jpg \
```

原有 `VALIDATION_CONTROLLED_SEGMENT_*` 三项仍兼容分割夹具，但不能与新输入组混用。

验收器先冻结私有输入副本，通过临时 loopback HTTP 服务调用真实 `/managed-models` 安装流程，
核验权重、原生架构、runtime 和持久 receipt，再核对 GET receipt。安装目录位于本次临时目录，
不会覆盖部署现有受控缓存。相同版本的冲突 receipt，以及 SHA／类别摘要错配的冻结引用都必须拒绝。
两轮各自冷加载确切的 `trained:` 权重与 receipt 池键，经真实 `/predict` 核验原图尺寸 RLE、
逐实例类别／置信度和对象摘要，确认 CUDA 驻留后执行同池卸载。第二轮共享故障矩阵使用该受控
`/predict` 请求，保留 partial header、token replay、旧 generation、取消计费和 busy unload 检查。

除既有严格证据外，必须同时检查以下附加结果：

```bash
jq -e '
  .passed == true and .runtime_ephemera_clean == true and
  .contract_checks.controlled_segment_install == true and
  .contract_checks.controlled_segment_reference_mismatch_rejected == true and
  .contract_checks.controlled_segment_receipt_mismatch_rejected == true and
  .contract_checks.controlled_segment_native_rle == true and
  .contract_checks.controlled_segment_cold_reload == true and
  (.deployment.pool_topology.controlled_segment.cycles | length) == 2 and
  (.deployment.pool_topology.controlled_segment.cycles |
    all(.cold_load == true and .unloaded == true and .instances >= 1))
' "$EVIDENCE_DIR/yolo-managed-lifecycle.json"
```

`deployment.pool_topology.controlled_segment` 保留冻结引用、receipt／runtime 资格摘要、原图／夹具
摘要、尺寸和每轮实例数。通用的两轮显存回收阈值及完整空池要求继续适用。此验收验证推理后端
生命周期；训练执行器的 GPU checkpoint 恢复、宿主／驱动故障和跨后端仲裁仍分别验收。

检测夹具使用相同 schema 3，改为 `task="detect"` 及实际基础模型（例如 `yolo26n`）。工具核验原生
检测框、原图尺寸、类别、置信度和非空结果，记录到 `deployment.pool_topology.controlled_detect`；
对应检查为 `controlled_detect_install`、`controlled_detect_reference_mismatch_rejected`、
`controlled_detect_receipt_mismatch_rejected`、`controlled_detect_native_boxes` 和
`controlled_detect_cold_reload`。两轮冷加载、卸载、故障检查和显存门槛与分割相同；两种受控任务各自运行、各自留证。

普通四任务权重从 `MODEL_ASSETS_DIR` 的 SHA256 缓存读取时，审批清单仍用逻辑文件名绑定摘要，
报告同时记录逻辑名与实际 SHA 文件。坏映射、缺失或损坏的缓存会阻断验收，不回退到同名旧文件。

## 结果映射

| task         | result.type                          | apps/api Geometry        |
| ------------ | ------------------------------------ | ------------------------ |
| detection    | `rectanglelabels`                    | `bbox`                   |
| segmentation | `polygonlabels`                      | `polygon`                |
| keypoint     | `keypointlabels`                     | `keypoint`（17 点 COCO） |
| obb          | `rectanglelabels` + `value.rotation` | `rotated_bbox`           |

零 adapter——四种 result type 命中 `apps/api/app/services/prediction.py::to_internal_shape` 现有分支（v0.10.28 已就位）。

## 类名

backend 默认输出 ultralytics 内置类名：

- detection / segmentation / keypoint → **COCO-80**（`person`, `car`, ...）
- keypoint → COCO 17 keypoint（hardcoded 顺序见 `predictor.py::COCO_KEYPOINT_NAMES`）
- obb → **DOTA-15**（`plane`, `ship`, `vehicle`, ...）

平台侧 v0.14.11 已经处理 backend 类名与项目 LabelConfig 不匹配的"未匹配分桶"，本 backend 不在协议层做映射。

## 测试

```bash
# 单测（按锁文件安装依赖，不需 GPU 或模型权重）
cd apps/yolo-backend
uv run pytest tests/ -q --ignore=tests/test_libreyolo_adapter.py
uv run pytest tests/test_libreyolo_adapter.py -q

# 端到端冒烟（需 GPU + ultralytics 权重）
# 见 v0.14.12 计划 §6.2
```

CI 使用 `bash scripts/run-ml-cpu-tests.sh run yolo` 创建临时虚拟环境，按后端锁文件安装完整的固定依赖。普通契约测试与原生 LibreYOLO 测试分别启动 Python 进程，避免 Torch 桩污染真实张量测试。原生测试在 CPU 上验证固定运行时的加载、类别顺序和坐标，无需 GPU、下载权重或启动训练；结束后自动删除虚拟环境和测试临时文件。

## LibreYOLO 受控模型

认证的 `POST /managed-models` 支持独立通过资格检查的 LibreYOLO YOLO9 t/s/m/c 原生检测 state-dict。它使用专属原生适配器，服务端固定 LibreYOLO、Torch、torchvision 与 CPU 运行时身份；E、其他任务、量化、P2/E2E 和外部格式不在该资格范围。内置模型目录继续使用原有 Ultralytics 组合。

安装先核对精确 SHA256、字节数、原始类别顺序和 native metadata，再检查全部主模型张量确实加载；仅原生辅助训练头可不进入推理结构。原生单图 Results 的所有检测均保留，按原图尺寸验证坐标、类别和置信度后转换一次百分比。收据固定框架、版本、家族、规格、格式、字节数和运行时资格；模型池以 UUID、权重 SHA 和收据摘要隔离实例与类别缓存，仍遵守原有借用和取消保护。

重建当前镜像时一并安装共享 `aap-training-contract`，保留 checkpoints 卷的 `managed/` 权重与收据。平台完成固定验证图试用后，仍须负责人明确发布或回滚。共享逐样本评估及主动选样目前只接受 Ultralytics；原生推理成功不授予共享评估资格。平台流程见[在线学习](../../docs-site/user-guide/online-learning/index.md)。
