# onnxtools-backend

二阶段车辆属性预标注 ML backend：RT-DETR 检测后，对机动车 ROI 执行车型与颜色分类，并把属性写回检测框。实现基于 [onnxtools](https://github.com/yyq19990828/onnxtools)。

服务暴露三个按需加载的固定句柄：

- `vehicle-detect`：独立检测器，一个 ORT session。
- `vehicle-attr-classify`：独立属性分类器，一个 ORT session。
- `vehicle-attr`：检测与分类复合管道，两个 ORT session。

句柄池为同 key 冷启动提供 single-flight，推理期间持有 borrower 与逐句柄 use lock。手动卸载、空闲卸载和 shutdown 都经过同一个全池清理边界，不会卸载仍在构建或使用的 session。

## 端点

| 端点                    | 说明                                                       |
| ----------------------- | ---------------------------------------------------------- |
| `GET /health`           | 健康、实际 ORT provider、句柄池与受管 residency            |
| `GET /setup`            | 协议模型目录；通过部署验证后同时声明 managed lifecycle     |
| `GET /versions`         | backend 与模型版本                                         |
| `POST /predict`         | 批量预测，按 `context.model_id` 路由三个句柄               |
| `POST /warmup`          | 可空 body；按 `model_id` 预加载指定句柄                    |
| `POST /unload`          | 空 body 保留 legacy 响应；generation body 执行受管全池卸载 |
| `POST /drain`           | 进入指定 generation 的 draining 状态                       |
| `POST /drain/cancel`    | 用更新 generation 取消 drain                               |
| `POST /lifecycle/mode`  | 切换 legacy/enforce gate                                   |
| `POST /lifecycle/reset` | 受签名的全池清理与状态恢复                                 |

`/predict` 与 `/warmup` 在读取业务 body 前完成 admission。协程取消时，底层 builder 或 executor 未真正结束之前，active、builder 和 borrower 不会提前归零。

## Residency 与部署验证

`/health.residency.pools` 始终包含 `pipeline`、`detector` 和 `va`。驻留判定读取实际业务 session 的完整 `get_providers()`：

- 任一 session provider chain 包含 CUDA 或 TensorRT，`gpu_loaded=true`。
- 所有已加载 session 都可确认只使用 CPU，`gpu_loaded=false`。
- 私有 session 路径不可读、构建/清理状态不确定或清理失败时返回 `null`。

Dockerfile 固定到已审计的 onnxtools commit，避免 `_onnx_session` ownership 契约随上游 `main` 漂移。新部署完成“空池基线 → 四个业务 session 全部加载 → 全池卸载 → 回到稳定基线”的真实 GPU 验证前，必须保持 `ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED=0`。此时 `/setup` 不宣告 managed lifecycle，`/lifecycle/mode` 拒绝切入 enforce，residency 也不会成为自动驱逐依据。

验收器会在加载 CUDA 前校验两个模型的批准 SHA-256 与审批引用，不允许用“结构相似”的未批准模型代替业务模型关闭门禁。在独占验收卡上执行：

```bash
export VALIDATION_IMAGE_PATH=/absolute/path/to/representative-vehicle.jpg
export VALIDATION_DET_SHA256=<approved-rtdetr-sha256>
export VALIDATION_VA_SHA256=<approved-va-sha256>
export VALIDATION_MODEL_APPROVAL_REF=<approval-record-or-ticket>
export VALIDATION_FIXTURE_APPROVAL_REF=<approved-fixture-record-or-ticket>
export VALIDATION_GPU_UUID=GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
export VALIDATION_GIT_COMMIT=$(git rev-parse HEAD)
export EVIDENCE_DIR=/tmp/aap-gpu-acceptance
install -d -m 700 "$EVIDENCE_DIR"
set -o pipefail

docker run --rm --gpus '"device=0"' --entrypoint python3 \
  -e VALIDATION_GPU_UUID \
  -e VALIDATION_DET_SHA256 \
  -e VALIDATION_VA_SHA256 \
  -e VALIDATION_MODEL_APPROVAL_REF \
  -e VALIDATION_FIXTURE_APPROVAL_REF \
  -e VALIDATION_GIT_COMMIT \
  -e VALIDATION_IMAGE_PATH=/validation/vehicle.jpg \
  -e VALIDATION_IMAGE_ID="$(docker image inspect \
    ai-annotation-platform-onnxtools-backend:latest --format '{{.Id}}')" \
  -v "$PWD/apps/onnxtools-backend/models:/app/models:ro" \
  --mount "type=bind,src=$PWD/data/inference-auth/onnxtools,dst=/run/aap-inference-auth,readonly" \
  -v "$VALIDATION_IMAGE_PATH:/validation/vehicle.jpg:ro" \
  ai-annotation-platform-onnxtools-backend:latest \
  /app/scripts/validate_managed_lifecycle.py \
  | tee "$EVIDENCE_DIR/onnxtools-managed-lifecycle.json"
jq -e '.passed == true and .runtime_ephemera_clean == true' \
  "$EVIDENCE_DIR/onnxtools-managed-lifecycle.json"
```

运行前先由平台初始化内置服务默认凭据。独立验收容器只读挂载 ONNXTools 自己的凭据目录；缺少或损坏的凭据会明确拒绝验证。已有手动认证的部署可显式传入相同的 `AAP_BACKEND_AUTH_TOKEN`，脚本不会生成临时绕过凭据。

代表图必须让 `vehicle-attr` 复合管道实际产生检测与分类结果。验收器会执行两轮三句柄/四 session 加载与受签全池卸载，拒绝任一 session 退回 CPU，并要求每轮卸载后显存稳定、回收至上下文基线且至少回收 90% 的模型工作集。严格证据只保留路径无关的制品指纹、物理 GPU 身份、显存样本与最终 residency；仅在 `passed=true` 并完成复核后，才能在目标部署中把 `ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED` 设为 `1`。

## 环境变量

| 变量                                   | 默认                     | 说明                                                  |
| -------------------------------------- | ------------------------ | ----------------------------------------------------- |
| `ONNXTOOLS_MODEL_DIR`                  | `/app/models`            | 模型目录                                              |
| `ONNXTOOLS_DET_MODEL`                  | `rtdetr-2024080100.onnx` | 检测模型文件名                                        |
| `ONNXTOOLS_VA_MODEL`                   | `va_260612.onnx`         | 属性分类模型文件名                                    |
| `ONNXTOOLS_CONF_THRES`                 | `0.5`                    | 检测置信度阈值                                        |
| `ONNXTOOLS_BUILD_TIMEOUT`              | `30`                     | 调用方等待冷启动的秒数；超时后真实 builder 继续受跟踪 |
| `ONNXTOOLS_IDLE_UNLOAD_SECONDS`        | `600`                    | 全池空闲卸载阈值；非正数关闭                          |
| `ONNXTOOLS_IDLE_CHECK_INTERVAL`        | `60`                     | 空闲检查周期                                          |
| `ONNXTOOLS_MANAGED_LIFECYCLE_VERIFIED` | `0`                      | 真实 GPU 全池卸载验证门槛                             |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON`       | 空                       | Ed25519 公钥 keyring；空值保持 legacy gate            |
| `ONNXTOOLS_LOG_LEVEL`                  | `INFO`                   | 日志级别                                              |

## 模型放置与启动

模型不打进镜像。把两个 ONNX 文件放到 `apps/onnxtools-backend/models/`，然后启动独立 profile：

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-onnxtools build onnxtools-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-onnxtools up -d onnxtools-backend
curl -s localhost:8004/health | python -m json.tool
```

ONNX Runtime 缺少可用 GPU provider 时会退回 CPU；`compute` 报告实际 primary provider，`residency` 独立表达 GPU session 是否仍可能驻留。

## 测试

`onnxtools[inference]` 与 OpenCV 由 Dockerfile 按审计提交安装，未写入本地项目依赖；干净的 `uv sync` 环境不足以运行测试。优先在刚构建的真实后端镜像中验证，并只读挂载测试目录：

```bash
docker run --rm --entrypoint sh \
  -v "$PWD/apps/onnxtools-backend/tests:/tests:ro" \
  -w /app ai-annotation-platform-onnxtools-backend:latest \
  -c 'python3 -m pip install -q "pytest>=8" "pytest-asyncio>=0.23" && \
      PYTHONPATH=/app python3 -m pytest -q -p no:cacheprovider /tests'
```

若使用本地虚拟环境，需先安装 Dockerfile 中固定提交的 `onnxtools[inference]`，再执行 `uv sync --extra dev` 与测试。
