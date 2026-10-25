---
audience: [dev]
type: explanation
since: v0.9.0
status: stable
last_reviewed: 2026-07-20
---

# AI 模型集成

协议契约见 [`ml-backend-protocol.md`](../reference/ml-backend-protocol)。本页说明当前模型服务的部署拓扑、显存预算、缓存、并发控制和能力协商。

<!-- history: the original v0.9/v0.10 release-slice notes are folded into the current model integration guide. -->

---

## 0. Backend 名录

平台当前自维护 **5 个 ML backend**(各为独立 FastAPI 微服务、独立 docker-compose profile,按显存预算自由组合启动)。所有 backend 走同一套[能力声明协议](../reference/ml-backend-protocol),由平台经 `/setup` 探能力 + `/predict` 调推理。

| Backend                 | 端口 | profile         | 模型族                                         | 主用途                                                                                     | composition                  |
| ----------------------- | ---- | --------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------ | ---------------------------- |
| `grounded-sam2-backend` | 8001 | `gpu`           | GroundingDINO + SAM 2.1                        | 文本/点/框交互式实例分割;暴露 `box-seg` 几何原子(多阶段下游)                               | `composite` + `atom`         |
| `sam3-backend`          | 8002 | `gpu-sam3`      | SAM 3                                          | 交互式分割(下一代 SAM)                                                                     | `composite`                  |
| `yolo-backend`          | 8003 | `gpu-yolo`      | ultralytics(v8/v11/v12 × det/seg/pose/obb/cls) | 纯批量预标(`supported_prompts=["none"]`),不挤占交互式工具栏                                | `composite`                  |
| `onnxtools-backend`     | 8004 | `gpu-onnxtools` | rtdetr + va                                    | 二阶段车辆属性预标注;多阶段编排原子组合(上游纯检测 + 下游纯分类)                           | `atom` × 2 + `composite` × 1 |
| `rapidocr-backend`      | 8005 | `gpu-rapidocr`  | RapidOCR(ONNX)PP-OCRv5/v6                      | OCR 文本检测 / 识别;`det → cls → rec` 拆原子 + 端到端,落点 `text`/`orientation`/`language` | `atom` × 2 + `composite` × 1 |

SAM 系列特化(image embedding 缓存、prompt 路由)集中在 §1–§5。`yolo-backend` / `onnxtools-backend` / `rapidocr-backend` 的差异化说明放在 §6。

---

## 1. 部署拓扑

```
apps/api (FastAPI 3.11) ──HTTP /predict──▶ grounded-sam2-backend
                                            FastAPI + PyTorch 2.3 + CUDA 12.1
                                            GroundingDINO + SAM 2.1
                                            + LRU model / embedding cache
                       └──HTTP /predict──▶ sam3-backend
                                            FastAPI + PyTorch 2.7 + CUDA 12.6
                                            SAM 3
```

**SAM 系列必须独立服务进程**：grounded-sam2-backend 与 sam3-backend 使用不同 Python、torch、CUDA 与权重依赖；GroundingDINO Deformable Attention 算子还需要 nvcc 现场编译。共用进程会触发 ABI 冲突（TORCH_CUDA_ARCH_LIST、cudnn 版本）。

### 1.1 docker-compose profile + nvidia 资源预留

每个 backend service 必备四项：① 独立 service + 独立端口；② `profiles: ["gpu"]` opt-in（dev 默认 CPU mock 不启 GPU profile，避免开发机被占用）；③ `deploy.resources.reservations.devices` 申请 nvidia GPU；④ `healthcheck start_period=120s`（首次冷启 ~900MB checkpoint 下载）。

```yaml
# docker-compose.yml 节选
services:
  grounded-sam2-backend:
    profiles: ["gpu"] # 默认不启, --profile gpu opt-in
    build:
      context: ./apps/grounded-sam2-backend
      dockerfile: Dockerfile
    image: ai-annotation/grounded-sam2-backend:0.9
    ports:
      - "8001:8000" # host:container
    environment:
      SAM_VARIANT: tiny # tiny|small|base_plus|large; 显存预算见 §1.2
      DINO_VARIANT: T # T|B
      CHECKPOINT_DIR: /app/checkpoints
    volumes:
      - ./checkpoints:/app/checkpoints # 持久化, 避免 image 重建拉权重
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      start_period: 120s # 首次冷启 + checkpoint 加载
      interval: 30s
      timeout: 10s
      retries: 3
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1 # 每 backend 1 张卡; 多变体并存看 §1.2
              capabilities: [gpu]
```

**dev vs 生产差异**（下表 `--profile gpu` 命令省略了叠加文件前缀；GPU backend 在 `docker-compose.ml.yml`，实际须 `docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu ...`，或设 `COMPOSE_FILE` 固化）：

| 场景                  | 启动命令                                                | GPU 需求                     | 显存预算    | 备注                                                                 |
| --------------------- | ------------------------------------------------------- | ---------------------------- | ----------- | -------------------------------------------------------------------- |
| **dev (无 GPU 机)**   | `docker compose up`                                     | 0                            | 0           | GPU backend 不启动，注册表实例不可用，标注页前端走 disabled UI       |
| **dev (有 GPU)**      | `docker compose --profile gpu up`                       | 1 卡                         | ~3GB (tiny) | 默认启动 grounded-sam2 主变体，额外变体按 prefetch / model pool 配置 |
| **生产 (单租户)**     | `docker compose --profile gpu up -d`                    | ≥ 1 卡                       | 见 §1.2     | 按显存档位选 SAM_VARIANT                                             |
| **生产 (多变体并存)** | 拆 service: `gsam2-tiny` / `gsam2-large` (各自 profile) | ≥ 2 卡 (推荐) 或 1 张 ≥ 24GB | 累加 §1.2   | C → B 升级路径见 ROADMAP §A 注册 backend 选变体                      |

### 1.2 显存预算 + variant 选型

每个 backend 常驻 = SAM 模型权重 + GroundingDINO 权重 + 推理时 mask buffer + embedding cache buffer。`SAM_VARIANT` / `DINO_VARIANT` 是默认主变体；请求也可以通过 `/predict context` 携带 `sam_variant` / `dino_variant`，由 backend 内部 ModelPool 按 LRU 缓存和驱逐。

| SAM 变体         | 模型权重 | 推理时峰值 | 推荐显存 | 推荐卡              |
| ---------------- | -------- | ---------- | -------- | ------------------- |
| `tiny` (default) | ~155MB   | ~3GB       | 6GB+     | 4060 8GB / 3070 8GB |
| `small`          | ~185MB   | ~4GB       | 8GB+     | 3070 / 4070         |
| `base_plus`      | ~320MB   | ~5GB       | 10GB+    | 3080 / 4070 Ti      |
| `large`          | ~895MB   | ~7GB       | 12GB+    | 3090 24GB / A4000   |

GroundingDINO 额外占用：`T` ~700MB / `B` ~1.5GB（仅 mask + box 模式需要，box 模式跳过 SAM 仍占 DINO）。

embedding cache buffer：`EMBEDDING_CACHE_SIZE` 默认 16 entries；按 GPU 显存富余度调到 16~64，`large` 变体建议保守配置。

**显存预算表（典型 dev / 生产组合）**：

| GPU                 | 推荐 SAM          | 推荐 DINO | 单实例总显存 | 可同卡跑变体数      |
| ------------------- | ----------------- | --------- | ------------ | ------------------- |
| 4060 / 3060 (8GB)   | tiny              | T         | ~3.7GB       | 1 (单变体)          |
| 3070 / 4070 (12GB)  | small / base_plus | T         | ~4.7-5.7GB   | 1-2                 |
| 3090 / A4000 (24GB) | large             | T 或 B    | ~7.7-8.5GB   | 2-3 (多容器并存)    |
| A100 40GB / H100    | large             | B         | ~8.5GB       | 4+ (整租户多变体池) |

**多容器并存**（生产高负载）：把 `grounded-sam2-backend` 拆成 `gsam2-tiny` / `gsam2-large` 两个 service（独立 profile + 独立端口），按业务 tier 路由不同 batch（tier-A 高精度走 large，tier-B 快通走 tiny）。单容器内需要少量变体切换时，优先使用 ModelPool 并配置 `PREFETCH_SAM_VARIANTS` / `PREFETCH_DINO_VARIANTS`。

单容器内的图像与视频模型由两个独立容量的 LRU 池管理，但共享冷构建串行锁；图像 predictor 与 embedding cache 作为同一条目共同借出和释放。受管卸载会同时清空 image/video 两池，只有两池的 builder、borrower 与 GPU artifact 都可信归零时，`residency.gpu_loaded=false` 才能作为显存账本减账依据。每个部署仍须完成真实模型的加载、淘汰和全量卸载回落验收后，才可开启受管能力。

### 1.3 镜像基础 + checkpoint 同步

镜像基于 `pytorch/pytorch:2.3.1-cuda12.1-cudnn8-devel`（**devel** 必需，runtime 镜像缺 nvcc 触发 GroundingDINO 编译失败）。Dockerfile 末段 `pip install -e ../_shared/mask_utils` 把共享 mask 转换包链入容器，避免 grounded-sam2 与 sam3 各自维护 mask → polygon 转换逻辑。

checkpoint 同步：`apps/grounded-sam2-backend/scripts/download_checkpoints.py` 按 SAM_VARIANT / DINO_VARIANT env 拉 hf-mirror 镜像；首次冷启或换 variant 时跑一次。生产环境推荐挂 PV / EBS 把 `/app/checkpoints` 持久化（~900MB-2GB），避免 image rebuild 重新下载。

---

## 2. 三种 prompt 路由

```
context.type == "point"  ┐
context.type == "bbox"   ├─▶ SAM 2.1 image_predictor → mask → polygon
context.type == "text"   ┘   先 GroundingDINO(caption→boxes)，再 SAM
```

返回值统一是 `polygonlabels` 数组（归一化 [0,1] 顶点列表 + score）。`text` 一次可能返回 N 个 polygon（DINO 召回多目标）。

---

## 3. SAM 2 image embedding 缓存

### 3.1 为什么缓存

工作台 `S` 工具的典型操作是同一张图反复点击 / 拖框（先 positive point 再 negative point 修边、调 bbox 看效果）。每次 SAM 2 `set_image()` 计算 image embedding ≈ 1.5 s（4060 / tiny），是热点。

DINO 端不缓存：每条 caption 不同，命中率低，且 DINO 输出是 box 不是 embedding。

### 3.2 Cache key

```
cache_key = sha1(url_path + "|" + sam_variant)
```

- `url_path` 由 `urllib.parse.urlsplit()` 取 `scheme://netloc/path`，**剥掉 query string**。MinIO presigned URL 的 `X-Amz-Signature` / `X-Amz-Date` 每次都会变，但底层对象不变；剥掉签名后跨 TTL 仍然命中。
- 拼上 `sam_variant`（`tiny` / `small` / `base_plus` / `large`）确保切大模型不会读到老 embedding。
- 本地路径（dev 用）直接以原串作 key。

### 3.3 命中后做什么

SAM2ImagePredictor `set_image()` 之后状态写在 `_features` / `_orig_hw` / `_is_image_set` / `_is_batch` 几个实例属性。命中时把这些字段从 `CacheEntry` 写回，等价于 `set_image()` 但跳过 image encoder。

| prompt  | 命中能省                             | 命中不能省                                                  |
| ------- | ------------------------------------ | ----------------------------------------------------------- |
| `point` | `_fetch_image()` + SAM `set_image()` | SAM `predict()`（每次 prompt 不同）                         |
| `bbox`  | `_fetch_image()` + SAM `set_image()` | SAM `predict()`                                             |
| `text`  | SAM `set_image()`                    | DINO 推理（每次 caption 不同） + image fetch（DINO 要原图） |

> 工程注意：`features` 里的 tensor 在 GPU。我们存引用、不 deepcopy；GPU 内存上限由 LRU 容量物理保证。

### 3.4 容量与显存预算

| 变体        | 单条 embedding ≈ | 默认 capacity | 总占用 ≈ |
| ----------- | ---------------- | ------------- | -------- |
| `tiny`      | 4 MB             | 16            | 64 MB    |
| `small`     | 8 MB             | 16            | 128 MB   |
| `base_plus` | 16 MB            | 16            | 256 MB   |
| `large`     | 24 MB            | 8             | 192 MB   |

经验值，仅供参考。`EMBEDDING_CACHE_SIZE` 环境变量可调：

- 4060 8 GB → 16
- 3090 24 GB → 32
- A100 40 GB → 64

> ⚠️ `large` 变体下不要把 cache size 设到 64+：单次能吃 ~1.5 GB，叠加 SAM/DINO 模型本体 + 推理临时显存可能 OOM。

### 3.5 vendor 升级风险

`_features` / `_orig_hw` / `_is_image_set` / `_is_batch` 是 vendor `IDEA-Research/Grounded-SAM-2` 的内部 API（commit `b7a9c29`）。`scripts/sync_vendor.sh` 升级后必须人肉跑 5-clicks 集成验收（README §性能参考）。

---

## 4. 观测

### 4.1 端点

- `GET /metrics` — Prometheus exposition（`generate_latest()` 原始格式）。
- `GET /cache/stats` — 人类可读 JSON：`{size, capacity, hits, misses, hit_rate, variant}`。

### 4.2 指标

| metric                         | 类型      | labels                 | 含义                                              |
| ------------------------------ | --------- | ---------------------- | ------------------------------------------------- |
| `embedding_cache_hits_total`   | Counter   | `prompt_type`          | 命中次数（按 `point`/`bbox`/`text`/`unknown` 分） |
| `embedding_cache_misses_total` | Counter   | `prompt_type`          | 未命中次数                                        |
| `embedding_cache_size`         | Gauge     | —                      | 当前缓存条目数                                    |
| `inference_latency_seconds`    | Histogram | `prompt_type`, `cache` | 端到端 `/predict` 延迟，`cache ∈ {hit,miss}`      |

bucket：`[0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]`，专为「miss 长尾秒级 + hit 短尾毫秒级」打的两段。

### 4.3 Prometheus scrape

本 backend 默认监听 8001。在 monitoring profile 的 prometheus 配置里增加 job：

```yaml
scrape_configs:
  - job_name: grounded-sam2-backend
    metrics_path: /metrics
    static_configs:
      - targets: ["grounded-sam2-backend:8001"]
```

### 4.4 关键查询

```text
# embedding 缓存命中率（按 prompt 类型）
sum by (prompt_type) (rate(embedding_cache_hits_total[5m]))
  / (
    sum by (prompt_type) (rate(embedding_cache_hits_total[5m]))
    + sum by (prompt_type) (rate(embedding_cache_misses_total[5m]))
  )

# /predict 命中 vs 未命中 P95 延迟
histogram_quantile(0.95,
  sum by (le, cache) (rate(inference_latency_seconds_bucket[5m]))
)
```

期望：交互式工作台流量稳定后，`prompt_type=point` 的命中率应 ≥ 70%（同图多次点击的天然分布）。

---

## 4.5 per-backend max_concurrency 限速

`ml_backend_registry.extra_params.max_concurrency` (JSONB 字段, 默认 4) 控制平台到该 backend 的并发 `/predict` 上限. 实现:

- `apps/api/app/services/ml_client.py` 把 backend id → semaphore 的缓存挂在当前 event loop 自身；loop 退出后缓存与已绑定 semaphore 作为同一循环对象图一起回收，不会被模块级容器反向保活，也不会交给后续 `asyncio.run()`.
- `MLBackendClient.__init__` 读取 `extra_params.max_concurrency`；真正进入预测时，`_get_semaphore(backend_id, max_cc)` 在当前 event loop 中取得或创建信号量.
- `predict()` / `predict_interactive()` 在 `async with await self._acquire():` 内调 httpx, 信号量 acquire/release 自动绕在请求 IO 外.
- 同一 event loop 内的多个 `MLBackendClient` 实例共享同 backend id 的信号量，跨 task / 跨请求生效；API 与不同 Celery loop 之间仍不共享，本地闸不能替代 Redis 全局 lease.

当 `ML_BACKEND_ROUTER_MODE=enforce` 时，服务池路由 ledger 会在选定物理实例前按同一 `max_concurrency` 发放 Redis route lease，因此 API 与多个 Celery worker 共享实例级上限；off/observe 仍只依赖各进程本地 semaphore。GPU 仲裁的逐资源 effective mode 负责显存准入与驱逐，不决定 route lease 是否生效。

**配置示例** (DB JSONB):

```json
// ml_backend_registry 行
{
  "id": "abc-...",
  "url": "http://172.17.0.1:8001/",
  "extra_params": {
    "max_concurrency": 2
  }
}
```

**调整后必须重启 worker**：每个存活 event loop 仍按 backend id 缓存既有信号量，改字段后须 `docker compose restart api celery-worker` 才能生效。若要运行期热更新，需要把 cache key 纳入 `max_cc` 或增加 invalidation 机制。

**注册表单 UI**：全局注册表单 `GlobalBackendFormModal`（模型市场 → 注册管理，仅超管）在「认证方式」下方提供「最大并发」number input（1-32，留空走默认 4），提交时合并到 `extra_params.max_concurrency`（覆盖 textarea JSON 同名键）。`RegisteredBackendsTab` 全局注册表行的类型列旁显示 `≤N 并发` chip，缺省值不渲染避免列表噪音——限速真正 per-物理-backend 生效，此 chip 即该物理 backend 的全局并发闸。不再需要直接手改 DB JSONB 字段。 <!-- since v0.19.0 · ADR-0044 限速上提全局 -->

**前端可见性**: `/admin/preannotate-summary` 透传 `ml_backend_max_concurrency` 给 `ProjectCardGrid` 卡片 + `ProjectDetailPanel` 头部展示「最多 N 并发」, 多 batch 并行预标时给 admin 心理预期.

**触发**: B-17 admin 反馈「多 batch 并行预标」需要并发护盾, 否则单 backend 被打爆 → grounded-sam2 GPU OOM 风险.

---

## 5. 协议契约引用

请求与响应字段以 [`ml-backend-protocol.md`](../reference/ml-backend-protocol) §2 为准。`/cache/stats` / `/metrics` **不进协议契约**——它们是 backend 内部端点，平台 API 不会消费。

---

## 6. YOLO / ONNX / OCR 通用推理 backend

`grounded-sam2-backend` / `sam3-backend` 之外平台还自维护三个**通用推理 backend**——`yolo-backend`、`onnxtools-backend` 与 `rapidocr-backend`。三者共性:不交互式(`supported_prompts=["none"]` 或仅 `bbox`),走纯批量预标;镜像与权重均按 backend 自治(`docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile <profile> up`),不影响 SAM 系列。

### 6.1 yolo-backend(8003 / `gpu-yolo`)

ultralytics 多任务多系列(`detection` / `segmentation` / `pose` / `obb` / `classification`)。协议 v2 多模型目录(详 [ADR-0036](../adr/archive/0036-ml-backend-capability-protocol-v2-multi-model))暴露所有上百权重,前端模型市场按 `task` × `series` × `size` 分组渲染。权重经 `yolo_checkpoints` 卷持久化。

### 6.2 onnxtools-backend(8004 / `gpu-onnxtools`)

**二阶段车辆属性预标注**:rtdetr 检测出框 → 对机动车框跑 va 分类出**车型(13 类)+ 颜色(11 类)**→ 写入框 `attributes`。基于 [onnxtools `VehicleAttributePipeline`](https://github.com/yyq19990828/onnxtools) 封装。

**镜像基础**:`pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel`(cuda12.8 + cudnn9 满足 onnxruntime-gpu 1.22;cudnn8 base 会静默退 CPU)。Dockerfile entrypoint 把 torch 自带的 `nvidia/*/lib` 加进 `LD_LIBRARY_PATH`,否则 CUDAExecutionProvider 找不到 cudnn 静默退 CPU(实测 GPU ~35ms/图 vs CPU ~940ms)。缺 GPU 时自动 fallback CPU,功能仍可用。

**模型注入**:不打进镜像,经 bind mount(`./apps/onnxtools-backend/models:/app/models:ro`)。起栈前需手动复制(rtdetr-2024080100 + va_260612)到该目录。

**三 model 暴露(原子化范式)**:`/setup` 广播三个 model,各架在自己的单模型推理类上、按需懒加载——detect-only 部署只加载 `RtdetrORT`、classify-only 只加载 `VehicleAttributeORT`。

| `model_id`              | `task`           | `composition` | 推理类                                               | 编排定位                                                   |
| ----------------------- | ---------------- | ------------- | ---------------------------------------------------- | ---------------------------------------------------------- |
| `vehicle-detect`        | `detection`      | `atom`        | 独立 `RtdetrORT`                                     | 多阶段编排**上游**(只出 bbox,属性留空交下游)               |
| `vehicle-attr-classify` | `classification` | `atom`        | 独立 `VehicleAttributeORT`                           | 多阶段编排**下游**(整图当一辆车,跳过 rtdetr,写车型 / 颜色) |
| `vehicle-attr`          | `detection`      | `composite`   | `VehicleAttributePipeline`(内部串 detect + classify) | 单阶段一锅端(开箱即用,内部编排复合)                        |

`composition` 由能力声明协议(详 [ADR 0043 — 多阶段预标注编排](../adr/archive/0043-staged-preannotation-pipeline))引入:`atom` 才能进**编排下游 stage** 选择器(只组合 atom,避免重复编排);`composite` 在单阶段配置可直接选用。模型市场目录 ModelCard / 列表视图均补「原子 / 内置流程」徽标。

**`/unload` + idle-unload**:`POST /unload` 释放全部已加载句柄(模型市场卸载按钮直接生效,UI 零改动);末次推理后空闲 `ONNXTOOLS_IDLE_UNLOAD_SECONDS`(默认 600s)自动卸载。按 model 句柄粒度释放显存——原子化让 detect-only 工作流不再背 va 分类器显存。与 yolo 体验对齐。

**协议 `output_attribute_schema`**:`vehicle-attr` / `vehicle-attr-classify` 在 `/setup` 自报输出属性 schema(含每个 select 字段的 `options`,value + 中文 label),沿 `ml_capabilities` 透传到前端,供「从 ML Backend 导入属性」一键合并进项目工具单位的 `attribute_schema`。`vehicle-detect` 纯检测不写 `attributes`,不声明 `output_attribute_schema`。

### 6.3 rapidocr-backend(8005 / `gpu-rapidocr`)

**平台首个真实 OCR backend**:基于 RapidOCR(ONNX),把 `det → cls → rec` 三段拆为「原子能力 + 端到端编排」,对外自报三个 model,激活协议早留好的 `ocr` 任务族,并成为 `attributes.text` / `orientation` / `language` 落点校验的首个真实 producer。

| `model_id` | `task`      | `composition` | `supported_inputs` | 编排定位                                                                  |
| ---------- | ----------- | ------------- | ------------------ | ------------------------------------------------------------------------- |
| `ocr-det`  | `detection` | `atom`        | `full_image`       | 整图文本检测,出文本 polygon 框,不写 `attributes`                          |
| `ocr-rec`  | `ocr`       | `atom`        | `crop`             | 吃裁剪图,内部跑 cls 做 180° 校正,写回 `text` / `orientation` / `language` |
| `ocr-e2e`  | `ocr`       | `composite`   | `full_image`       | 单阶段一锅端 det + cls + rec,出 polygon + 文本属性                        |

cls(文本行方向 0/180)**语言/版本无关**,内化进 rec 与 e2e,不单独暴露 model 条目。

**下游识别原子可作跨 backend 编排**:`ocr-rec` 吃 crop,可以做任何上游检测器(YOLO / gsam2 / onnxtools 等)的下游识别阶段——「上游任意 backend 出框 → 裁 crop → 下游 rec 认字写回 `text`」是平台支持的跨 backend 流水线形态。 <!-- since v0.20.5 -->

**变体轴 PP-OCRv5/v6 × 尺寸档 × 通用(中英)/英文**:`ocr-det` / `ocr-rec` / `ocr-e2e` 自报 `supported_variants`(version / size / lang),`/predict context.model_variants` 选档落到 RapidOCR 的 `Det.ocr_version` / `Rec.ocr_version` / `Rec.model_path`。

**引擎池 + 阈值显式下发**:`det` / `rec` / `e2e` 同 variant 共享池化 `RapidOCR` 实例(`pool_key = det+cls+rec` 三件套路径);`update_params` 对 `None` 是跳过不重置,缺参传 `None` 会让上一次请求的 `text_score` / `box_thresh` / `unclip_ratio` 粘在引擎上污染后续请求(含跨原子类型、跨项目)——预测器缺参显式回落 catalog 默认值并每次写定,跨请求泄漏从结构上消除。

**镜像基础**:`nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04`(与瘦身后的 onnxtools 共享 nvidia/cuda runtime base);GPU 可选,缺 GPU 自动 fallback CPU 仍可用。

---

## 7. 能力协商 + 模态派生

平台对 backend 的「能力 / 模态」有持久化感知。实现集中在两个文件：

- `apps/inference/src/aap_inference/capabilities.py`（`extract_capabilities` + `derive_modalities`）
- `apps/api/app/services/ml_backend.py`（`check_health`）

### 7.1 健康检查时能力快照落库

`check_health` 拉完 `/health`（得到 `healthy + meta`）后，best-effort 再探一次 `/setup`，把能力快照写入 `health_meta["capabilities"]`：

```python
# apps/api/app/services/ml_backend.py  check_health()
caps = extract_capabilities(await client.setup())
if caps is not None:
    meta = {**meta, "capabilities": caps}
    backend.is_interactive = caps["is_interactive"]   # 改派生对账
backend.health_meta = meta
```

探测失败（网络抖动 / backend 尚未实现 `/setup`）捕获后静默跳过，不影响 `check_health` 的 bool 返回值。`HealthMeta` schema 用 `extra="allow"`，无需 alembic 迁移。

### 7.2 能力快照字段

`health_meta["capabilities"]` 由 `extract_capabilities(setup_resp)` 填充，字段来自 backend `/setup` 响应：

| 字段                          | 类型        | 含义                                                 |
| ----------------------------- | ----------- | ---------------------------------------------------- |
| `is_interactive`              | `bool`      | backend 是否为交互式（点/框/文本 prompt 模式）       |
| `supported_prompts`           | `list[str]` | 支持的 prompt 类型，如 `["point","bbox","text"]`     |
| `supported_trackers`          | `list[str]` | 支持的 tracker，如 `["sam2_video"]`                  |
| `supported_text_outputs`      | `list[str]` | 文本类输出，如 `["caption"]`                         |
| `supported_geometric_outputs` | `list[str]` | 几何类输出，如 `["polygonlabels","rectanglelabels"]` |
| `modalities`                  | `list[str]` | 派生字段，见 §7.3                                    |

### 7.3 模态派生规则

`derive_modalities(caps)` 从能力快照推断支持的标注模态（不入库、读时算，同步写入快照中）：

```python
# apps/inference/src/aap_inference/capabilities.py
if caps.get("supported_prompts"):   # 非空 ⇒ image
    modalities.append("image")
if caps.get("supported_trackers"):  # 非空 ⇒ video
    modalities.append("video")
```

`grounded-sam2-backend` 只有 `supported_prompts` 时 `modalities=["image"]`；上报 `supported_trackers: ["sam2_video"]` 时 `modalities=["image","video"]`。

### 7.4 绑定校验（PATCH /projects/{id}）

项目绑定 backend（`PATCH /projects/{id}`）时平台实时探 `/setup` 派生模态，与项目 `data_type` 不兼容则返回 422；探测失败 fail-open 放行（避免 backend 瞬时宕机卡住绑定，真正的模态不匹配留到 `/predict` 时暴露）。

### 7.5 `is_interactive` 改派生对账

`is_interactive` 由 backend `/setup.is_interactive` 自报并在每次 `check_health` 回写；注册/编辑表单不再手填 checkbox（只显示「健康检查时自动探测」提示），create/update payload 不带 `is_interactive`。

---
