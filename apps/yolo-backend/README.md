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

### 受控分割权重的部署资格

普通 `segment` 验收不代替受控 `trained:<model_version_id>` 路径。部署项目训练得到的
`yolo11n-seg` 时，额外提供同一份真实 checkpoint、代表性原图及以下严格 JSON 夹具。
三个环境变量必须同时设置；未设置时保留原有普通模型验收。目标 GPU、运行镜像、pool cap
和业务权重仍须与待部署环境一致，目标卡必须独占。

```json
{
  "schema": 1,
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

将这三个文件放在独立的 `FIXTURE_DIR`，挂载为只读；在前述普通验收命令中增加：

```bash
  -v "$FIXTURE_DIR:/fixtures:ro" \
  -e VALIDATION_CONTROLLED_SEGMENT_WEIGHTS=/fixtures/best.pt \
  -e VALIDATION_CONTROLLED_SEGMENT_FIXTURE=/fixtures/controlled.json \
  -e VALIDATION_CONTROLLED_SEGMENT_IMAGE=/fixtures/representative.jpg \
```

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

重建当前镜像时一并安装共享 `aap-training-contract`，保留 checkpoints 卷的 `managed/` 权重与收据。平台完成固定验证图试用后，仍须负责人明确发布或回滚。共享逐样本评估及主动选样目前只接受 Ultralytics；原生推理成功不授予共享评估资格。平台流程见[在线学习](../../docs-site/user-guide/projects/online-learning.md)。
