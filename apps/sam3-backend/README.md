# sam3-backend

> v0.10.x AI 基座的 ML Backend — 把 [`facebookresearch/sam3`](https://github.com/facebookresearch/sam3) (848M, 2025-11) 打包成独立 GPU 服务, 遵循平台 [ML Backend 协议契约](../../docs-site/dev/reference/ml-backend-protocol.md). 图像 PCS + inst 交互与 PVS 视频追踪使用 `facebook/sam3` 的 `sam3.pt`；multiplex 文本视频追踪使用 `facebook/sam3.1` 的 `sam3.1_multiplex.pt`.
>
> 当前版本: **v0.10.0 (M0 — 容器化 + exemplar 协议落地)**. 后续 v0.10.1 ~ v0.10.3 在 [`ROADMAP/archive/0.10.x.md`](../../ROADMAP/archive/0.10.x.md) 切片.

---

## 与 grounded-sam2-backend 的关系

两个 backend **并存** — sam3-backend 是高精度首选, grounded-sam2-backend 兜底 (笔记本能跑). 路由策略见路线图 §3.3, v0.10.1 在 apps/api 落地.

| 能力                                   | grounded-sam2-backend | sam3-backend                                               |
| -------------------------------------- | --------------------- | ---------------------------------------------------------- |
| `point` prompt (单实例点交互)          | ✅ SAM 2.1 直接       | ✅ inst predictor (`model.predict_inst`, v0.18.17 开 inst) |
| `interactive_box` prompt (单框单 mask) | ✅ SAM 2.1 直接       | ✅ inst predictor (v0.18.17)                               |
| `text` prompt                          | ✅ DINO → SAM 复合链  | ✅ SAM 3 PCS 单模型一步出                                  |
| **`exemplar` prompt** (全图相似)       | ❌                    | ✅ SAM 3 PCS 视觉示例 → 全图相似实例                       |
| 推荐 GPU                               | 4060 / 3090 / A100    | **3090 / A100** (不部署 4060)                              |
| Python / CUDA                          | 3.10 / 12.1           | 3.12 / 12.6                                                |

---

## 能力盘点

> v0.18.17 选项 B: 启用 `enable_inst_interactivity`, 解锁 SAM-style 单实例点/框交互 (`model.predict_inst`). multiplex 权重自带 tracker.\*, 无需额外下载; 代价是常驻显存 +2-3GB.

| Prompt                                                           | 链路                                                                               | 用途                                                                                                                                                 |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `context.type=point`                                             | `model.predict_inst(point_coords, point_labels)` → 单实例 mask                     | 单点交互, 正/负点累加精修 (前端重发全量点); `multimask_output` 出 3 候选                                                                             |
| `context.type=interactive_box`                                   | `model.predict_inst(box)` → 单框单 mask                                            | SAM 2 式「框内出一个 mask」; ≠ exemplar 的全图相似                                                                                                   |
| `context.type=text`                                              | `set_text_prompt(prompt)` → PCS 一步出全图匹配概念                                 | 文本批量预标 / `/ai-pre`                                                                                                                             |
| `context.type=exemplar`                                          | `(可选)set_text_prompt → 顺序多次 add_geometric_prompt(box, label)` → 全图相似实例 | PCS 视觉示例迭代 refine: 多正负框累加 (`exemplars[]`, 正框扩召回 / 负框排误检) + 可选 text 概念组合 + per-request 阈值重过滤; 缺省退化单 `bbox` 正框 |
| `context.type=video_tracker`, `model_key=sam3_video`             | multiplex 文本发现与跨帧传播                                                       | 按文本发现多目标并输出逐帧 bbox / polygon / mask                                                                                                     |
| `context.type=video_tracker`, `model_key=sam3_video_interactive` | PVS 点/框 seed 与 memory 传播                                                      | 按稳定 `obj_id` 逐对象跨帧追踪                                                                                                                       |
| ~~`context.type=bbox`~~                                          | —                                                                                  | v0.18.17 退役 (仅作几何形状); 旧请求落 422                                                                                                           |

point / interactive_box 与 PCS 共用同一 `backbone_out` 缓存 (开 inst 后 `set_image` 一次同产两路特征). 返回数据均为 `polygonlabels` / `rectanglelabels` (归一化 [0,1]) + score + model_version + inference_time_ms.

---

## 目录结构

```
apps/sam3-backend/
├── pyproject.toml          Python 3.12 依赖锁 (不含 torch/torchvision, 由 base image 锁定)
├── Dockerfile              基于 pytorch/pytorch:2.7.0-cuda12.6-cudnn-devel
├── .dockerignore
├── main.py                 FastAPI app + 推理、预热与生命周期端点
├── predictor.py            SAM3Predictor: 四种 prompt 推理 + mask→polygon + cache snapshot/restore
├── video_predictor.py      multiplex 文本视频追踪
├── pvs_video_predictor.py  PVS 点/框视频追踪
├── embedding_cache.py      SAM 3 image embedding LRU 缓存 (cap 默认 32)
├── pool_domain.py          image / multiplex / PVS 三池驻留聚合 (LRU 内核来自 _shared/backend_runtime)
├── gpu_lifecycle.py        admission、generation fencing 与 drain/unload 状态机
├── observability.py        Prometheus Counter/Histogram/Gauge (sam3_* 前缀)
├── schemas.py              Pydantic schema (协议对齐, 含 exemplar)
├── tests/                  pytest 单测 (无 GPU 即可跑)
├── checkpoints/            权重落盘点 (启动时下载, 挂 volume)
├── scripts/
│   ├── download_checkpoints.py   幂等拉 sam3.pt + sam3.1_multiplex 权重 (gated, 需 HF_TOKEN)
│   ├── validate_managed_lifecycle.py  真实 GPU 三池部署验收
│   └── sync_vendor.sh            同步上游到 vendor/
├── vendor/
│   └── sam3/               vendored copy (须先跑 sync_vendor.sh)
└── README.md
```

---

## Vendor & 固定 commit

vendor 形态选 **vendored copy** 而非 git submodule (与 grounded-sam2-backend 一致, 同步脚本可控).

**首次接入 / 升级**:

```bash
cd apps/sam3-backend
bash scripts/sync_vendor.sh <commit-sha>
git add vendor/sam3 && git commit -m "vendor: bump sam3 to <commit-sha>"
```

**当前固定 commit**: `4cbac146c1b5a1e3a7f5c6a894901090b4dfd65b` (2026-05-13 拉取, main HEAD: "Fix PYRE_MISSING_ANNOTATIONS issues in fbcode/deeplearning/projects/sam3_release/sam3/model/io_utils.py").

> ✅ **2026-05-13 状态**: vendor 已就位 + `predictor.py` / `embedding_cache.py` / `tests/` 已按真实 API 重写 (45 单测全绿). v0.10.0 选项 A: 不启用 `enable_inst_interactivity`, 放弃 point prompt, 让 grounded-sam2-backend 兜底单点交互. 等首位 GPU 部署者跑端到端验收 (HF_TOKEN + `--profile gpu-sam3`).

升级 commit 时务必跑 5-clicks 集成验收, 复核以下签名是否仍然存在:

- `Sam3Processor` 公共方法 (`set_image` / `set_text_prompt` / `add_geometric_prompt` / `reset_all_prompts`) 与 `state` dict 字段 (`backbone_out` / `geometric_prompt` / `masks` / `boxes` / `scores`)。
- v0.18.17 inst 路径: `model.predict_inst(inference_state, point_coords=, point_labels=, box=, multimask_output=)` 返回 `(masks CxHxW, iou C, low_res Cx256x256)`; `SAM3InteractiveImagePredictor.predict(...)` 签名; `state["backbone_out"]["sam2_backbone_out"]` 字段 (开 inst 后 `set_image` 产出)。
- v0.18.19 exemplar refine 依赖: `geometric_prompt.append_boxes(boxes, labels)` 是**累加**语义 (concat 非覆盖, 见 `geometry_encoders.py`), 多次 `add_geometric_prompt` 顺序叠框; `add_geometric_prompt(box, label, state)` 的 `label: bool` 约定 `True`=正框 / `False`=负框; `set_text_prompt` 后再叠几何框可组合 (后者在 `language_features` 已存在时不补 dummy "visual")。任一约定变更需同步 `predict_exemplars`。

---

## HF_TOKEN 配置

`facebook/sam3` 与 `facebook/sam3.1` 均为 **gated repo**, 必须:

1. 分别在 [https://huggingface.co/facebook/sam3](https://huggingface.co/facebook/sam3) 与 [https://huggingface.co/facebook/sam3.1](https://huggingface.co/facebook/sam3.1) 接受 license.
2. 创建一个 read-only access token: [https://huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
3. 写到根 `.env`:

   ```
   HF_TOKEN=hf_xxxxxxxxxxxx
   ```

4. docker-compose 通过 `environment: HF_TOKEN: ${HF_TOKEN}` 注入容器.

`download_checkpoints.py` 启动时检查 `HF_TOKEN`; 未设置则 fail-fast 让容器启动失败.

---

## 本地启动 (GPU 主机)

前置条件:

- NVIDIA driver ≥ **570** (CUDA 12.8 minimum, 对齐 vendor README §70); 老机房需先升驱动.
- `nvidia-container-toolkit` 已装好.
- 主机 GPU 架构在 `TORCH_CUDA_ARCH_LIST="8.0;8.6;8.9;9.0"` 范围内 (A100 / RTX 30 / RTX 40 / H100 / H200).
- 显存建议 ≥ 16 GB (FP16 下 sam3 单图 ~6-7 GB, 留余量做 batch).
- `.env` 已配 `HF_TOKEN` 并接受 sam3 / sam3.1 license.

**镜像 mirror 注意**: Dockerfile 用 `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel` (torch + CUDA + cudnn 一站式, 不需要 pip 重装 torch). 若 mirror 无该 tag, 备选 `pytorch/pytorch:2.7.1-cuda12.6-cudnn9-devel` (CUDA 12.6 下限) 或 `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel` (更新, 不一定存在). vendor README §82 推荐的是 `torch==2.10.0+cu128`, 我们用 2.7.1 已满足 vendor §70 的「torch >= 2.7」下限, 换取镜像可用性.

```bash
# 1. 同步 vendor (一次性)
bash apps/sam3-backend/scripts/sync_vendor.sh <commit-sha>

# 2. 通过 docker compose GPU profile 启动
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-sam3 up --build sam3-backend

# 3. 端到端验证 (四种 prompt 都试一遍)
curl http://localhost:8002/health
curl -X POST http://localhost:8002/predict \
  -H 'content-type: application/json' \
  -d '{"task":{"id":"t1","file_path":"https://example.com/sample.jpg"},
       "context":{"type":"text","text":"person"}}'
curl -X POST http://localhost:8002/predict \
  -H 'content-type: application/json' \
  -d '{"task":{"id":"t1","file_path":"https://example.com/sample.jpg"},
       "context":{"type":"exemplar","bbox":[0.2,0.2,0.45,0.55]}}'
```

首次启动会下载 sam3.pt + sam3.1_multiplex 权重 (~6.6 GB 合计), 冷启动数分钟取决于网速.

---

## 端点速查

完整规范以 [`docs-site/dev/reference/ml-backend-protocol.md`](../../docs-site/dev/reference/ml-backend-protocol.md) 为准. 本 backend 图像模型变体 `sam3`.

```
GET  /health        → {"ok": true, "gpu": true, "model_version": "sam3", "loaded": true, ...}
GET  /setup         → {"name":"sam3", "supported_prompts":["point","interactive_box","text","exemplar"], ...}
GET  /versions      → {"versions": ["sam3"]}
POST /predict       → 交互式 (task+context) 或 批量 (tasks[])
GET  /metrics       → Prometheus exposition (sam3_* 指标)
GET  /cache/stats   → {"size": N, "capacity": 32, "hits":..., "misses":..., "hit_rate": 0.85, "variant": "sam3"}
POST /warmup       → 空 body 预热 image；`task=tracker|interactive` 预热对应视频池
POST /unload       → 空 body 保留 legacy 响应；带签名 generation body 执行受管三池卸载
POST /drain        → 受管停流；`/drain/cancel` 取消尚未完成的迁移
```

`POST /predict` 按 body shape 自动分流, 与 grounded-sam2-backend 完全一致:

```jsonc
// 交互式 point
{"task": {"id":1, "file_path":"https://..."}, "context": {"type":"point", "points":[[0.5,0.5]], "labels":[1]}}

// 交互式 exemplar (单框, 兼容旧路径)
{"task": {"id":1, "file_path":"https://..."}, "context": {"type":"exemplar", "bbox":[0.2,0.2,0.45,0.55]}}

// 交互式 exemplar refine (多正负框 + text 组合 + 阈值)
{"task": {"id":1, "file_path":"https://..."}, "context": {"type":"exemplar",
  "exemplars":[{"bbox":[0.2,0.2,0.45,0.55],"label":true},{"bbox":[0.6,0.6,0.7,0.7],"label":false}],
  "text":"car", "score_threshold":0.5, "output":"mask"}}

// 批量 text
{"tasks": [{"id":1,"file_path":"..."}, {"id":2,"file_path":"..."}], "context": {"type":"text", "text":"ripe apples"}}
```

---

## 环境变量

| Env                               | 默认                  | 说明                                               |
| --------------------------------- | --------------------- | -------------------------------------------------- |
| `HF_TOKEN`                        | —                     | **必填**; sam3 / sam3.1 均为 gated repo.           |
| `CHECKPOINT_DIR`                  | `/app/checkpoints`    | 权重落盘点 (volume 挂载).                          |
| `SAM3_IMAGE_HF_REPO_ID`           | `facebook/sam3`       | 图像模型 (PCS + inst) repo.                        |
| `SAM3_IMAGE_CHECKPOINT_FILE`      | `sam3.pt`             | 图像模型权重文件名.                                |
| `SAM3_HF_REPO_ID`                 | `facebook/sam3.1`     | 视频 multiplex repo (预留).                        |
| `SAM3_CHECKPOINT_FILE`            | `sam3.1_multiplex.pt` | 视频追踪权重文件名 (预留).                         |
| `SAM3_EMBEDDING_CACHE_SIZE`       | `32`                  | LRU 容量; A100 充裕可调到 64.                      |
| `SAM3_SCORE_THRESHOLD`            | `0.5`                 | text / exemplar 路径 PCS score 过滤阈值.           |
| `LOG_LEVEL`                       | `INFO`                | DEBUG / INFO / WARNING.                            |
| `IMAGE_DOWNLOAD_TIMEOUT`          | `30`                  | 拉远端图片超时 (秒).                               |
| `SAM3_IDLE_UNLOAD_SECONDS`        | `600`                 | 空闲多少秒后自动卸载模型释放显存; ≤0 关闭定时卸载. |
| `SAM3_IDLE_CHECK_INTERVAL`        | `60`                  | idle 检查器轮询间隔 (秒).                          |
| `SAM3_MODEL_POOL_BUILD_TIMEOUT`   | `120`                 | 冷构建等待超时；超时后真实 builder 仍跟踪到结束.   |
| `GPU_LIFECYCLE_VERIFY_KEYS_JSON`  | 空                    | 平台 Ed25519 公钥 keyring；非空时必须可完整解析.   |
| `SAM3_MANAGED_LIFECYCLE_VERIFIED` | `0`                   | 仅在当前制品/权重/硬件完成三池实卡验收后设为 `1`.  |

---

## 受管模型池与 Idle Unload

SAM3 把 image、multiplex video、PVS video 作为三个独立 `cap=1` 池。三池共用冷构建串行锁，避免大模型同时冷启；已加载池可同时常驻。每个池用 single-flight、borrower 和 use lock 保护真实 owner，HTTP 取消或 build timeout 不会提前把仍在运行的 executor 报成空池。

1. **自动卸载**：`_idle_watcher` 逐池判断空闲时间；有 builder、waiter 或 borrower 的池不会被越过 owner 卸载。
2. **懒重载**：下一个 predict / warmup / reload 通过对应 pool 恢复所需模型；同 key 并发冷启只执行一次构建。
3. **手动兼容**：bodyless `POST /unload` 保留 `{ok, unloaded, loaded, video_loaded}` 响应并清理三池；`POST /reload` 仍只预热 image。legacy 调用没有 generation fencing，不能作为仲裁账本减记证据。
4. **受管卸载**：签名 reset / mode / drain / unload 覆盖三池和 image embedding cache。`/health.residency` 仅在所有池、builder、borrower 都可信为空时报 `gpu_loaded=false`。

仓库默认保持 `SAM3_MANAGED_LIFECYCLE_VERIFIED=0`，此时 `/setup` 不发布 `managed_lifecycle`、`/lifecycle/mode` 拒绝 enforce。只有与已验收制品匹配，或在当前部署重新运行 `scripts/validate_managed_lifecycle.py` 通过后，才能显式开启。

独占目标卡并确认 image 与 multiplex 权重审批摘要后执行：

```bash
set -o pipefail
install -d -m 700 "$EVIDENCE_DIR"
docker compose -f docker-compose.yml -f docker-compose.ml.yml \
  --profile gpu-sam3 run --rm --no-deps --entrypoint python3 \
  -e VALIDATION_GIT_COMMIT -e VALIDATION_IMAGE_ID -e VALIDATION_GPU_UUID \
  -e VALIDATION_MODEL_APPROVAL_REF -e VALIDATION_FIXTURE_APPROVAL_REF \
  -e VALIDATION_SAM3_SHA256 -e VALIDATION_MULTIPLEX_SHA256 \
  sam3-backend /app/scripts/validate_managed_lifecycle.py \
  | tee "$EVIDENCE_DIR/sam3-managed-lifecycle.json"
jq -e '.passed == true and .runtime_ephemera_clean == true' \
  "$EVIDENCE_DIR/sam3-managed-lifecycle.json"
```

验收器真实运行 image、multiplex video 和 PVS video 三池，检查 BF16/CUDA 路径、两轮整池卸载、
90% 工作集回收门和共享故障矩阵。运行前必须从目标镜像和当前权重计算环境变量，不得复用其他机器的
结论。

`/health` 返回字段:

```json
{
  "loaded": true,
  "residency": {
    "state": "resident",
    "gpu_loaded": true,
    "pools": { "image": { "resident": true } }
  },
  "idle_unload_seconds": 600,
  "last_request_age_seconds": 123.45
}
```

关闭机制 (常驻显存): `SAM3_IDLE_UNLOAD_SECONDS=0` 或 `SAM3_IDLE_UNLOAD_SECONDS=-1`. 仍可通过 `/unload` 手动卸载.

---

## 性能参考 (FP16 实测目标; 实际数据待 v0.10.0 落地后回填)

| 硬件       | text 全链单图       | 缓存命中点击 |
| ---------- | ------------------- | ------------ |
| 3090 24 GB | 200-400 ms          | < 50 ms      |
| A100 40 GB | 100-200 ms          | < 30 ms      |
| H200       | 30-60 ms / 100+ obj | < 20 ms      |

---

## License

- facebookresearch/sam3 code: 见上游 `LICENSE` (商用前逐条核对; 不与 SAM 2 自动等价).
- 本 backend 代码: Apache 2.0 (与平台一致).

---

## 排错

**vendor/sam3 为空**: 先跑 `bash scripts/sync_vendor.sh <commit>`. Dockerfile 在 build 期检测到空目录会 fail-fast.

**HF_TOKEN 未设置**: 容器启动后 `download_checkpoints.py` 立即 fail-fast 退出. 检查 `.env` 与 docker-compose 的 environment 注入是否一致.

**license not accepted**: HuggingFace API 返回 401/403. 浏览器登 [https://huggingface.co/facebook/sam3](https://huggingface.co/facebook/sam3) 与 [https://huggingface.co/facebook/sam3.1](https://huggingface.co/facebook/sam3.1) 接受 license 后重试.

**首次启动卡住 > 8 分钟**: `docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-sam3 logs -f sam3-backend` 查看下载进度. HuggingFace 偶发限速, 重试即可.

**CUDA OOM**: 减小 `SAM3_EMBEDDING_CACHE_SIZE` (默认 32 → 16); 4060 笔记本不要部署 sam3, 走 grounded-sam2.

**driver too old**: 升级主机驱动到 ≥ 555 (CUDA 12.6 minimum).

**ProjectSettings 测试连接红灯**: `http://sam3-backend:8002` 是 docker 内部地址; 如平台 api 在 host 跑 (dev 模式), 改为 `http://localhost:8002` 或 `http://172.17.0.1:8002` (Linux bridge gateway).

---

## 后续切片 (v0.10.x 路线图)

- v0.10.0 M0: 容器化 + exemplar 协议落地 ✅ (本文档)
- v0.10.1 M1: 工作台 Shift+拖框 = exemplar 入口 + apps/api 路由策略
- v0.10.2 M2: AB 对比工具 (同任务跑两 backend, 平台显示对比)
- v0.10.3 M3: ADR-0012 双 backend 永久共存 + deploy.md 双拓扑
