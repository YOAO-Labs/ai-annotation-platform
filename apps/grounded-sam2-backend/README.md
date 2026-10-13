# grounded-sam2-backend

> 把 [`IDEA-Research/Grounded-SAM-2`](https://github.com/IDEA-Research/Grounded-SAM-2) 打包成独立 GPU 服务，遵循平台 [ML Backend 协议契约](../../docs-site/dev/reference/ml-backend-protocol.md)。

---

## 能力盘点

| Prompt                         | 链路                                   | 用途                       |
| ------------------------------ | -------------------------------------- | -------------------------- |
| `context.type=point`           | SAM 2.1 image_predictor 直接出 mask    | 工作台单点/多点交互分割    |
| `context.type=interactive_box` | SAM 2.1 image_predictor 直接出 mask    | 工作台单框交互分割         |
| `context.type=text`            | GroundingDINO → boxes → SAM 2.1 → mask | 文本批量预标               |
| `context.type=video_tracker`   | SAM 2.1 video predictor 窗口传播       | 视频框、多边形或 mask 跟踪 |

返回数据均为 `polygonlabels`（归一化 [0,1] 顶点列表）+ score + model_version + inference_time_ms。

---

## 目录结构

```
apps/grounded-sam2-backend/
├── pyproject.toml          Python 3.10 依赖锁 (不含 torch/torchvision, 由 base image 锁定)
├── Dockerfile              基于 pytorch/pytorch:2.3.1-cuda12.1-cudnn8-devel (与官仓对齐)
├── .dockerignore
├── main.py                 FastAPI app + 推理、观测和受管生命周期端点
├── predictor.py            三种 prompt 推理 + mask→polygon 内联 + cache snapshot/restore
├── video_predictor.py      SAM 2 视频跟踪封装
├── model_pool.py           图像 predictor + embedding cache 池 (LRU 内核来自 _shared/backend_runtime)
├── video_pool.py           视频 tracker 池
├── pool_domain.py          图像/视频双池生命周期聚合
├── gpu_lifecycle.py        drain、generation、admission 与全量卸载状态机
├── embedding_cache.py      SAM 2 image embedding LRU 缓存
├── observability.py        Prometheus Counter/Histogram/Gauge
├── schemas.py              Pydantic schema (协议对齐)
├── tests/                  pytest 单测 (无 GPU 即可跑)
├── checkpoints/            权重落盘点 (启动时下载, 挂 volume)
├── scripts/
│   ├── download_checkpoints.py   幂等拉权重
│   └── sync_vendor.sh            同步上游到 vendor/
├── vendor/
│   └── grounded-sam-2/     vendored copy (须先跑 sync_vendor.sh)
└── README.md
```

---

## Vendor & 固定 commit

vendor 形态选 **vendored copy** 而非 git submodule（理由：上游 demo 脚本结构常变，同步脚本可控）。

**首次接入 / 升级**：

```bash
cd apps/grounded-sam2-backend
bash scripts/sync_vendor.sh <commit-sha>
git add vendor/grounded-sam-2 && git commit -m "vendor: bump grounded-sam-2 to <commit-sha>"
```

**当前固定 commit**：`b7a9c29f196edff0eb54dbe14588d7ae5e3dde28`（2026-05-07 选定，main HEAD；fix: add CUDA version check for <12.8 compatibility, #123）。

---

## 本地启动 (GPU 主机)

前置条件：

- NVIDIA driver ≥ **525.60.13**（CUDA 12.1 minimum；A100 集群通常已满足，老机房需先升驱动）
- `nvidia-container-toolkit` 已装好
- 主机 GPU 架构在 `TORCH_CUDA_ARCH_LIST="7.0;7.5;8.0;8.6;8.9;9.0"` 范围内（覆盖 V100 / T4 / A100 / RTX 30 / RTX 40 / H100）。

> Dockerfile 与对齐版本来自官仓 [`IDEA-Research/Grounded-SAM-2/Dockerfile`](https://github.com/IDEA-Research/Grounded-SAM-2/blob/main/Dockerfile)（Python 3.10 + torch 2.3.1 + torchvision 0.18.1 + CUDA 12.1 + gcc/g++-10）。我们额外加 `8.9 / 9.0` 以覆盖 Ada Lovelace 与 Hopper。

```bash
# 1. 同步 vendor (一次性)
bash apps/grounded-sam2-backend/scripts/sync_vendor.sh <commit-sha>

# 2. 通过 docker compose GPU profile 启动
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up --build grounded-sam2-backend

# 3. 端到端验证
curl http://localhost:8001/health
curl -X POST http://localhost:8001/predict \
  -H 'content-type: application/json' \
  -d '{"task":{"id":"t1","file_path":"https://example.com/sample.jpg"},
       "context":{"type":"text","text":"person"}}'
```

首次启动会下载 ~900MB checkpoints，冷启动 3-5 分钟。

---

## 模型变体配置

通过环境变量切换（默认值见 `.env.example`）：

| Env                                        | 默认   | 可选                                     | 备注                                           |
| ------------------------------------------ | ------ | ---------------------------------------- | ---------------------------------------------- |
| `SAM_VARIANT`                              | `tiny` | `tiny` / `small` / `base_plus` / `large` | 4060 8GB 推荐 tiny                             |
| `DINO_VARIANT`                             | `T`    | `T` / `B`                                | B 显存翻倍                                     |
| `BOX_THRESHOLD`                            | `0.35` | 0.20 ~ 0.50                              | 召回不足下调                                   |
| `TEXT_THRESHOLD`                           | `0.25` | 0.20 ~ 0.40                              | 短语 prompt 默认 0.25                          |
| `EMBEDDING_CACHE_SIZE`                     | `16`   | 8 ~ 64                                   | tiny 单条 ≈ 4 MB；large ≈ 24 MB；按 GPU 显存调 |
| `MODEL_POOL_CAP`                           | `1`    | ≥ 1                                      | 图像变体并存上限；满池仅驱逐无 borrower 的 LRU |
| `VIDEO_MODEL_POOL_CAP`                     | `1`    | ≥ 1                                      | 视频变体独立并存上限                           |
| `GROUNDED_SAM2_MANAGED_LIFECYCLE_VERIFIED` | `0`    | `0` / `1`                                | 实卡完成全量卸载验收后才发布受管能力           |

请求可通过 `context.model_variants` 在运行期选择变体。图像池与视频池的容量和 LRU 独立，但共享冷构建串行锁，避免两套重模型同时抢占显存。

受管能力必须用目标镜像、DINO/SAM 权重和物理卡重新验收。暂停同卡其他流量后执行：

```bash
set -o pipefail
install -d -m 700 "$EVIDENCE_DIR"
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu run --rm --no-deps --entrypoint python3 \
  -e VALIDATION_GIT_COMMIT -e VALIDATION_IMAGE_ID -e VALIDATION_GPU_UUID \
  -e VALIDATION_MODEL_APPROVAL_REF -e VALIDATION_FIXTURE_APPROVAL_REF \
  -e VALIDATION_WEIGHT_SHA256_JSON \
  grounded-sam2-backend /app/scripts/validate_managed_lifecycle.py \
  | tee "$EVIDENCE_DIR/grounded-sam2-managed-lifecycle.json"
jq -e '.passed == true and .runtime_ephemera_clean == true' \
  "$EVIDENCE_DIR/grounded-sam2-managed-lifecycle.json"
```

权重 manifest 必须精确包含 `sam2.1_hiera_tiny.pt` 和 `groundingdino_swint_ogc.pth`。验收器覆盖
image/video 双池、embedding cache、两轮 full cleanup 和故障矩阵；失败不会改动运行配置，并会清理
验收器创建的 fixture 与模型池。两轮计量前会先执行一次 image/video 真实推理与全池卸载，以成熟
CUDA/cuDNN context 为稳定基线；受管卸载同步清理 cuBLAS workspace 与 allocator cache。

---

## 端点速查

完整规范以 [`docs-site/dev/reference/ml-backend-protocol.md`](../../docs-site/dev/reference/ml-backend-protocol.md) 为准。本 backend 实现版本 `grounded-sam2-dino{T,B}-sam2.1{tiny,small,base_plus,large}`。

```
GET  /health        → {"ok": true, "gpu": true, "model_version": "...", "loaded": true}
GET  /setup         → {"name", "labels": [], "is_interactive": true, "params": {...}}
GET  /versions      → {"versions": ["grounded-sam2-dinoT-sam2.1tiny"]}
POST /predict       → 交互式 (task+context) 或 批量 (tasks[])
POST /warmup        → 预热图像或视频变体
POST /reload        → 兼容型变体预热
POST /unload        → 空 body 仅卸载图像池；generation body 受管卸载双池
POST /drain         → 停止接收当前 generation 的新 workload
POST /drain/cancel  → 取消 drain
POST /lifecycle/mode、/lifecycle/reset → 受管模式切换与可信全量复位
GET  /metrics       → Prometheus exposition (text/plain)
GET  /cache/stats   → 图像池各变体 embedding cache 的聚合统计
```

`POST /predict` 按 body shape 自动分流：

```jsonc
// 交互式 (单条 task + context, 返回 PredictionResult)
{
  "task":    {"id": 1, "file_path": "https://..."},
  "context": {"type": "bbox", "bbox": [0.2, 0.2, 0.5, 0.5]}
}

// 批量 (tasks 数组 + 顶层 context, 返回 {results: PredictionResult[]})
{
  "tasks":   [{"id": 1, "file_path": "..."}, {"id": 2, "file_path": "..."}],
  "context": {"type": "text", "text": "ripe apples"}
}
```

---

## 性能参考

| 硬件      | text 全链单图 | 缓存命中点击 (M1 后) |
| --------- | ------------- | -------------------- |
| 4060 8GB  | 200-500 ms    | < 50 ms              |
| 3090 24GB | 100-200 ms    | < 30 ms              |
| A100 40GB | 50-100 ms     | < 20 ms              |

LRU embedding 缓存的 key 为 `sha1(url_path|sam_variant)`（剥掉 MinIO presigned 的 query string，跨 TTL 滚动稳定）。同图二次 point/interactive_box 点击直接 restore SAM 内部 `_features`/`_orig_hw` 而不再 `set_image()`，并跳过图片下载；text 路径仍需 DINO 走原图，但 SAM 端同样命中。缓存与 predictor 属于同一个池条目，驱逐或卸载时一起释放。

---

## License

- GroundingDINO: Apache 2.0
- SAM 2: Apache 2.0
- Grounded-SAM-2: Apache 2.0
- 本 backend 代码：Apache 2.0（与平台一致）

---

## 排错

**vendor/grounded-sam-2 为空**：先跑 `bash scripts/sync_vendor.sh <commit>`。Dockerfile 在 build 期检测到空目录会 fail-fast。

**首次启动卡住 > 5 分钟**：`docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu logs -f grounded-sam2-backend` 查看 checkpoints 下载进度。HuggingFace 偶发限速，重试即可。

**CUDA OOM**：切到更小变体（`SAM_VARIANT=tiny`、`DINO_VARIANT=T`），或扩 swap 给 host；4060 8GB 仅能跑 tiny+T 主链。

**Deformable Attention 编译失败 / nvcc not found**：base image 必须是 `cuda12.1-cudnn8-devel`（不是 `runtime`）；如本地 build 缓存了 runtime 版本，先 `docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu build --no-cache grounded-sam2-backend`。

**driver too old**：`docker run` 报 `Failed to initialize NVML: Driver/library version mismatch` 或 `CUDA driver version is insufficient` → 升级主机驱动到 ≥ 525.60.13。

**ProjectSettings 测试连接红灯**：`http://grounded-sam2-backend:8001` 是 docker 内部地址；如平台 api 在 host 跑（dev 模式），改为 `http://localhost:8001`。

---
