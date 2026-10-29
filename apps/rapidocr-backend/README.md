# rapidocr-backend

RapidOCR（ONNX）ML backend，平台首个真实 OCR backend。把 RapidOCR 的 `det → cls → rec`
三段拆为**原子能力 + 端到端编排**，对外自报三个 model：

| model_id  | 任务        | 输入 → 输出                                     | composition                 |
| --------- | ----------- | ----------------------------------------------- | --------------------------- |
| `ocr-det` | `detection` | full_image → polygon 文本框                     | atom                        |
| `ocr-rec` | `ocr`       | crop → `attributes.text`(+orientation/language) | atom（内部跑 cls 方向校正） |
| `ocr-e2e` | `ocr`       | full_image → polygon + text + orientation       | composite（det→cls→rec）    |

cls（文本行方向 0/180）语言/版本无关，内化进 rec 与 e2e、不单独暴露。平台 pipeline 可把
`ocr-det（源阶段）→ ocr-rec（下游吃 crop）` 串成编排。

## 变体（version × size × lang）

- **det**：v5 mobile/server · v6 tiny/small/medium（语言无关）
- **rec / e2e**：通用(中英) = v5 mobile/server + v6 tiny/small/medium；英文 = v5 mobile
- `cls` 按 size 选 mobile/server 档，rec/e2e 共享

经 `context.model_variants`（`{version,size,lang}`）选档，缺省取 v5/mobile/universal。

## 模型权重

13 个 onnx（det 5 + cls 2 + rec 6，共 ~369MB），来自 RapidOCR v3.9.0 的 ModelScope 仓库。
不入 git、bind-mount 注入。起栈前先下载：

```bash
python3 apps/rapidocr-backend/download_models.py   # SHA256 校验、幂等
```

## Python 依赖与镜像构建

`pyproject.toml` 声明 RapidOCR 3.9.0、ONNX Runtime GPU 1.23.2、NumPy 2.2.6、
headless OpenCV 和三个直接使用的共享包。ORT GPU wheel 也可在 CPU 上运行；CUDA 镜像额外使用
`cuda` extra 中的 cuDNN 9.10.2，CUDA/cuBLAS 由基础镜像提供。共享 runtime 还依赖
`aap-lifecycle-contract`。共享包仍是本仓库构建输入，这不是独立插件迁移或 GPU 验收声明。

`uv.lock` 固定完整依赖图，镜像安装的 `requirements-runtime.txt` 固定各 registry
artifact 的哈希；`requirements-build.txt` 固定构建工具 wheel。更新 lock 后，在本目录运行：

```bash
uv lock
uv export --locked --extra cuda --extra build --no-emit-local \
  --no-emit-package opencv-python --no-emit-package nvidia-cublas-cu12 \
  --output-file requirements-runtime.txt
```

镜像以 `--no-deps --require-hashes` 安装导出结果，再以 `--no-deps` 安装实际构建的共享包
和 backend。RapidOCR 的原始 metadata 要求 `opencv-python`，运行时刻意替换为唯一的
`opencv-python-headless`；因此 stock metadata 的 `pip check` 会报告缺少 GUI 包，
普通 `pip install` 或 `uv sync` 也不能保证此 headless 布局。不要补装 GUI 包，
两者会覆盖同一 `cv2` 文件。`nvidia-cublas-cu12` 同样不从 wheel 安装，以免覆盖基础镜像的 CUDA 库。

## 运行（dev）

```bash
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-rapidocr up -d rapidocr-backend
curl -s localhost:8005/health
curl -s localhost:8005/setup | python3 -m json.tool
```

端口 8005。base 与瘦身后的 onnxtools-backend 共享 `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04`，
RapidOCR 镜像会在其上固定 cuDNN 9.10.2，以保证 PP-OCRv6 非对称 padding 卷积在
RTX 3090 上不会从 CUDAExecutionProvider 回退到 CPUExecutionProvider。当前 `gpu-rapidocr`
compose profile 会无条件申请 NVIDIA 设备，因此只用于 GPU 部署。backend
也支持显式 CPU 模式；无 GPU reservation 时可直接运行同一镜像并设置
`RAPIDOCR_DEVICE=cpu`。环境变量：

| 变量                                  | 默认          | 说明                                                                                                                                        |
| ------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `RAPIDOCR_MODEL_DIR`                  | `/app/models` | 权重根目录                                                                                                                                  |
| `RAPIDOCR_DEVICE`                     | `gpu`         | `gpu` 优先构造 CUDA session，明确设备错误时才尝试 CPU replacement；`cpu` 只构造 CPU session。实际 provider 以 `/health` 的业务 session 为准 |
| `RAPIDOCR_POOL_CAP`                   | `3`           | composite 引擎数上限；每个引擎固定持有 det/cls/rec 三个 ORT session                                                                         |
| `RAPIDOCR_BUILD_TIMEOUT`              | `30`          | 调用方等待冷启动的秒数；超时后 builder 仍由池跟踪                                                                                           |
| `RAPIDOCR_IDLE_UNLOAD_SECONDS`        | `600`         | 整池空闲卸载阈值；非正数关闭                                                                                                                |
| `RAPIDOCR_IDLE_CHECK_INTERVAL`        | `60`          | 空闲检查周期                                                                                                                                |
| `RAPIDOCR_MANAGED_LIFECYCLE_VERIFIED` | `0`           | 部署级验收门；只有当前镜像、权重和物理 GPU 的严格证据通过后才设为 `1`                                                                       |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON`      | 空            | Ed25519 验签公钥 keyring；空值保持 legacy gate                                                                                              |

## 引擎池与受管生命周期

六种合法权重三件套映射为六个动态 key。池在构造前预留 slot，同 key 冷启动
single-flight，容量满时只淘汰没有 borrower/waiter 的最旧引擎，并且先释放旧引擎的
三个 session 再构造新引擎。同引擎的阈值更新和推理由 use lock 串行；不同 key 可并行。

`/health.residency.pools.engines` 是用于仲裁的稳定聚合池 ID，动态权重 key 仍留在
`/health.pool.loaded_keys` 用于诊断。驻留判定读取每个引擎 det/cls/rec 三条完整
provider chain：任一 CUDA/TensorRT 为 true，全部明确为 CPU 才是 false，私有链缺失、
builder/清理中或清理失败为 unknown。

backend 实现 `POST /drain`、`/drain/cancel`、受管 `POST /unload`、
`/lifecycle/mode` 和 `/lifecycle/reset`。`/predict` 与 `/warmup` 在读取业务 body 前完成
admission。无 body 的 `/unload` 继续作为 legacy best-effort 兼容路径，不能单独作为
平台减账证据。

启动阶段不创建临时 ORT session。只有受 admission 保护的首个 builder 才构造业务
session。CUDA composite 构造异常只在确认为设备错误时才尝试 CPU 替代；即使替代成功，
由于无法直接观测部分构造的私有所有权链，residency 仍保持 unknown，直到一次成功的
全池清理。

默认 cap=3 的部署验收会同时构建三个 composite engine，并要求 9 条 det/cls/rec 业务
session 全部以 CUDA 为首 provider。独占目标卡后执行：

```bash
set -o pipefail
install -d -m 700 "$EVIDENCE_DIR"
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-rapidocr run --rm --no-deps --entrypoint python3 \
  -e VALIDATION_GIT_COMMIT -e VALIDATION_IMAGE_ID -e VALIDATION_GPU_UUID \
  -e VALIDATION_MODEL_APPROVAL_REF -e VALIDATION_FIXTURE_APPROVAL_REF \
  -e VALIDATION_WEIGHT_SHA256_JSON \
  rapidocr-backend /app/scripts/validate_managed_lifecycle.py \
  | tee "$EVIDENCE_DIR/rapidocr-managed-lifecycle.json"
jq -e '.passed == true and .runtime_ephemera_clean == true' \
  "$EVIDENCE_DIR/rapidocr-managed-lifecycle.json"
```

`VALIDATION_WEIGHT_SHA256_JSON` 的 key 是验收器选中权重相对 `RAPIDOCR_MODEL_DIR` 的路径，必须与该次
v5 mobile、v5 server 和 v6 medium 三引擎所需权重精确相等。验收会在真实推理后重新读取
9 条 session 的 provider chain，因此 ORT 运行期整 session CPU fallback 也会阻断声明。正式两轮
计量前会先加载并卸载一次三引擎全池，以成熟 ORT/CUDA/cuDNN context 作为稳定回收基线。
验收失败时不能开启声明。

## 接入平台

注册 backend URL 用 `172.17.0.1:8005`（宿主端口，非 compose 服务名；见 ml-backend-url-dev memo）。
`resource_profile.device`/`batchable` 决定 v0.19.5 设备感知队列路由（GPU→ml / CPU→ml.cpu）。
