---
title: ML Backend 接入教程
audience: [dev]
type: tutorial
status: stable
last_reviewed: 2026-10-19
---

# ML Backend 接入教程：从 echo 示例到真实模型

本文是接入 ML Backend 的入门路径：先把最小的 echo 示例跑起来，理解协议契约，再一步步把它改造成一个真实推理的 backend（以 OCR 为贯穿示例），最后注册到平台。

> **协议以 [ML Backend 协议契约](/dev/reference/ml-backend-protocol) 为准**，本文不重复抄协议；遇到字段语义疑问，一律以协议文档为权威。

完成本教程你将得到：

1. 一个在本机跑通的 echo backend，并通过平台「测试连接」；
2. 对 `/health`、`/setup`、`/predict` 三端点契约的最小心智模型；
3. 一个从 echo 改出来的 OCR backend 骨架（真实模型推理 + `attributes.text` 输出）；
4. 进阶方向：多模型目录 / variants / warmup 与 `aap_protocol_v2` 共享库。

## 前提

- Python >= 3.10，本地能 `pip install fastapi uvicorn`
- 平台本地环境已跑通（参见 [本地开发](/dev/tutorials/local-dev)），用于最后的注册验证

## Step 1：跑通 echo 示例

echo 示例位于仓库 `docs-site/dev/examples/echo-ml-backend/`，不到 100 行、零外部重依赖（仅 `fastapi` + `pydantic`），整目录拷贝即可作为 starter：

```bash
cd docs-site/dev/examples/echo-ml-backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

另起一个 shell 验证端点：

```bash
./test.sh
# 期望：/health /setup /predict 全部 200
```

它对任何输入都返回同一个固定 demo bbox——没有模型、没有 GPU，但协议形态是完整的 v2.1，足以让平台端到端链路（注册 → 测试连接 → AI 预标注 → 预测落库）整条走通。

## Step 2：理解三端点契约

打开 `main.py`（全文也镜像在协议文档 §8），backend 必需实现的只有三件事：

| 端点            | 一句话职责                                               | 协议章节  |
| --------------- | -------------------------------------------------------- | --------- |
| `GET /health`   | 返回 200 = 在线（平台只看状态码）                        | §1        |
| `GET /setup`    | 自描述能力：协议版本、模型目录、支持的 prompt / 几何输出 | §4 / §4.1 |
| `POST /predict` | 接 `tasks[]`，逐 task 回 `result[]` 标注数组             | §2 / §3   |

`/versions` 是可选的。三个关键认知：

- **`/setup` 是能力的唯一真相源**：前端哪些 AI 工具可用、参数面板长什么样，全部由 `/setup` 返回体决定。echo 声明了 `protocol_version: "2.1"`、一条 `models[]` 目录条目（`task: "detect"`、`supported_prompts: ["none"]` 表示纯批量）。
- **`/predict` 的 `result[]` 是 Label Studio 风格标注数组**：`type` + `value`（坐标兼容 `[0,1]` 与 `[0,100]` 百分比），见协议 §3。
- **平台不强校验 schema**：契约靠你自觉对齐，跑不通时优先对照协议文档逐字段排查。

## Step 3：把 echo 改造成 OCR backend

下面以「PaddleOCR 文本检测 + 识别」为例，展示从 echo 改出真实 backend 的三个关键改造点。完整思路同样适用于任何检测 / 分割模型。

### 3.1 模型加载

echo 没有模型；真实 backend 在模块级（或 lifespan 启动钩子）加载一次、全程复用，绝不在 `predict()` 里逐请求加载：

```python
from paddleocr import PaddleOCR

# 启动时加载一次; 大模型建议放 FastAPI lifespan 钩子或惰性加载
ocr = PaddleOCR(use_angle_cls=True, lang="ch")
```

`/predict` 收到的 `file_path` 通常是平台签发的 presigned URL，先下载再推理：

```python
import httpx

async def _fetch_image(file_path: str) -> bytes:
    if file_path.startswith("http"):
        async with httpx.AsyncClient() as client:
            resp = await client.get(file_path, timeout=30)
            resp.raise_for_status()
            return resp.content
    with open(file_path, "rb") as f:  # 本地路径调试用
        return f.read()
```

### 3.2 `/setup` 能力声明

把 echo 的 detection 条目换成 OCR 条目——`task: "ocr"` 与 `output_attribute_types: ["text"]` 告诉平台「这个模型输出几何 + 文本属性」（受控词表见协议 §4.1.3 / §4.1.8）：

```python
@app.get("/setup")
async def setup():
    return {
        "name": "my-ocr-backend",
        "version": "0.1.0",
        "protocol_version": "2.1",
        "compat_protocol_versions": ["2.0"],
        "model_version": "paddleocr-2.x",
        "is_interactive": False,
        "models": [
            {
                "id": "ppocr",
                "display_name": "PaddleOCR 文本检测+识别",
                "task": "ocr",
                "supported_prompts": ["none"],
                "supported_geometric_outputs": ["polygon"],
                "output_attribute_types": ["text"],
            }
        ],
    }
```

### 3.3 `/predict`：真实推理 + `attributes.text`

OCR 的输出约定（协议 §4.1.8）：几何走 `value.points`，识别文本写在 result 条目顶层的 `attributes.text`，采纳后由平台落到 annotation attributes：

```python
@app.post("/predict")
async def predict(req: PredictRequest):
    results = []
    for t in req.tasks:
        image = await _fetch_image(t.file_path)
        shapes = []
        for box, (text, conf) in run_ocr(ocr, image):  # 你的推理封装
            shapes.append(
                {
                    "type": "polygonlabels",
                    "value": {"points": box, "polygonlabels": ["text"]},
                    "score": conf,
                    "attributes": {"text": text},   # OCR 文本 → annotation.attributes
                }
            )
        results.append({"task": t.id, "result": shapes, "model_version": "paddleocr-2.x"})
    return {"results": results}
```

至此就是一个能在平台跑批量预标注的真实 OCR backend。固定 demo 输出的 mock 版本可对照 `docs-site/dev/examples/mock-v2-backend/` 的 `ppocr` 条目与 `_demo_shapes("ocr")`。

> 建议同步给自己的 backend 写 contract tests（fastapi `TestClient` 即可，无需起服务），两个示例目录下的 `tests/test_contract.py` 可直接当模板抄。

## Step 4：进阶——多模型目录、variants、warmup 与共享库

单模型骨架跑通后，按需取用 v2.1 的进阶能力，**完整形态参考 mock-v2 示例**：

- **多模型目录**：一个 backend 暴露 N 个 model（`/setup.models[]`），如 YOLO 的 det/seg/pose/obb 四条目（协议 §4.1）。
- **模型变体**：`supported_variants` 多轴声明 + `default_variants` 默认组合 + `variant_combinations` 非笛卡尔积过滤；`/predict` 经 `context.model_variants` 热切换（协议 §2.2 / §4.1.6）。
- **预热与运行时观测**：`/setup.warmup_endpoint: true` + `POST /warmup`；`/predict` 响应带 `cache_hit` / `model_load_ms`，前端据此区分「加载中」与「推理中」（协议 §4.2 / §4.4）。
- **标准错误形态**：非法 variant → 422 `variant_not_supported`；权重缺失 / 显存不可服务 → 503 `model_unavailable` + `Retry-After`（协议 §6）。

**生产建议**：直接依赖共享库 `apps/_shared/protocol_v2/`（`aap_protocol_v2`），它提供 `PredictionResult` / `BatchPredictResponse` / `PoolStatus` / `WarmupResponse` 等协议 Pydantic 模型、`VariantNotSupportedError` / `ModelUnavailableError` 标准错误类，以及旧 variant 字段的 normalize 工具——各第一方生产 backend（`yolo-backend` / `sam3-backend` / `grounded-sam2-backend`）均已依赖。真实推理 backend 的完整组织方式（ModelPool、observability、tests）首选参考 `apps/yolo-backend/`。

本教程的 OCR 主题，真实推理参考 `apps/rapidocr-backend/`：它把 RapidOCR 的 det→cls→rec 拆为**原子能力 + 端到端 composite** 三个 model（`ocr-det` / `ocr-rec` / `ocr-e2e`），示范一个 backend 如何既暴露可被 pipeline 编排的原子阶段、又提供一次跑完的便捷入口，并输出 `attributes.text` / `orientation` / `language` 富属性（协议 §4.1.8）。

## Step 5：全局注册并对项目启用

backend 跑起来后，按两层模型接入平台：

1. **全局注册（超级管理员）**：进入「模型市场 → 注册管理 → 实例」，添加平台可达的 Backend URL。手动 Token 或 None 方式可用「测试连接」检查公开健康端点；保存后执行实例健康检查，读取 `/setup` 更新能力快照；
2. **项目启用（项目管理员）**：进入目标项目的「项目设置 → ML 模型」，勾选启用该服务池并按需设置项目主模型；
3. **触发推理**：在批次详情页或预标注编排面板触发「AI 预标注」；若为交互式 backend（`/setup.is_interactive: true`），还会自动出现在工作台 AI 助手工具栏中。

操作细节见用户手册：[ML 后端绑定](/user-guide/projects/ml-backends)、[AI 预标注](/user-guide/projects/ai-preannotate)；超管全局视角见 [ML Backend 注册](/user-guide/superadmin/ml-backend-registry)。

本教程的 echo 示例不要求认证，开发调试可选 None。五个内置服务默认要求认证，可信地址选择对应的系统托管方式，保存时验证受认证的 `/managed-auth`；该方式不填写手动 Token，也不用普通「测试连接」替代认证校验。自建服务可实现并配置自己的手动 Bearer 认证，服务端与平台注册值需要一致。

一个外部 Backend 实现普通预测协议，不代表它已实现训练权重安装、共享逐样本评估或 GPU 仲裁。接入这些能力前分别核对[协议契约](/dev/reference/ml-backend-protocol)；服务池接流和凭据生命周期见[模型服务接入、路由与认证](../concepts/ml-backend-routing-and-auth)，已有训练模型的排查见[模型回流链路](../how-to/debug-model-return)。

## 下一步

- 对照协议 [接入 checklist](/dev/reference/ml-backend-protocol#_9-接入-checklist) 自查每一项
- 排障：[ML Backend 不可用 runbook](/ops/runbooks/ml-backend-down)
