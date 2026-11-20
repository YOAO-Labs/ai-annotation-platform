---
title: ML Backend 接入教程
audience: [dev]
type: tutorial
status: stable
last_reviewed: 2026-10-30
---

# ML Backend 接入教程：自定义插件与 HTTP 契约

简单检测服务可以使用独立 Plugin-SDK 实现自己的 provider，无需安装官方模型；复杂服务或其他语言实现可直接遵守完整 HTTP 契约。下文另保留 echo 协议教学，它不执行真实推理。

## 使用自定义推理 SDK

作者 SDK 是独立的 `yoao-inference-plugin-sdk`（Python 导入 `yoao_inference_plugin`，命令 `yoao-plugin`），不是调用平台 API 的 `yoao-sdk`。开发过程不依赖平台或官方插件 checkout、数据库、管理员凭据、Torch 或模型权重。

先从可信渠道取得经核验的候选 SDK、平台便携契约及完整依赖 wheelhouse，包括 `build` 和生成项目的构建后端。指定真实 manifest 路径或 URL 与可信的 manifest SHA256，按[制品获取接口](/dev/concepts/module-deployment#plugin-candidate-artifacts)核验来源；不要假定新发行名已公开发布。第三方依赖和权重的许可分别核验。在 CPython 3.11 新环境安装：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
export PIP_NO_INDEX=1
export PIP_FIND_LINKS=/path/to/wheelhouse
python -m pip install 'yoao-inference-plugin-sdk[dev]' 'setuptools>=68' wheel
yoao-plugin init my-detector
yoao-plugin build my-detector
python -m pip install my-detector/dist/*.whl
umask 077
python -c 'import secrets; from pathlib import Path; Path("token").write_text(secrets.token_urlsafe(40))'
yoao-plugin dev --entrypoint my-color-region-plugin --device cpu --token-file ./token
```

SDK 的 `dev` extra 提供 `build`；生成的项目不使用构建隔离，`setuptools` 和 `wheel` 也必须安装在当前环境，不能只放在 wheelhouse 里。编辑 provider 后，重新构建、安装并重启服务即可，不依赖 editable 路径。`yoao-plugin dev` 仅监听 loopback，调用已安装的作者 console script，预热默认检测变体并检查显式请求设备上的驻留；它移除继承的宿主路由／插件权限配置，不需要 Registry、管理员或 GPU keyring。本地 CUDA 成功不等于平台托管资格，也不自动回退 CPU。模型专属 serve 参数放在 `--` 之后，不能覆盖开发工具控制的 host、port、token 或 device。

生成的 `my_color_region_plugin.py` 是可运行的 CPU 颜色区域算法，**不是预训练模型**：对每个 RGB 通道，选取本通道至少 200、其他通道至多 80 的像素，分别返回 `red-region`／`green-region`／`blue-region` 的一个包围框，分数为 1；同色不连通区域合并到同一框，没有匹配像素则返回空结果。作者替换 `Provider.load`、`ColorRegionModel.predict` 与 `close` 实现自己的模型；发布前修改 distribution、模块和入口名，避免使用示例身份。

具体模型的资产准备由插件提供：显式核验来源、许可、大小和摘要，不在推理中隐式下载。官方 LibreYOLO 的模型加载器、资产工具和 manifest 生成器位于 [YOAO-Plugins](https://github.com/YOAO-Labs/YOAO-Plugins#readme)，不是通用 SDK 的默认依赖。

为默认示例生成独立可确定期望的图片：

```python
from PIL import Image, ImageDraw

image = Image.new("RGB", (100, 100), "black")
draw = ImageDraw.Draw(image)
draw.rectangle((10, 10, 29, 39), fill="red")
draw.rectangle((50, 40, 79, 89), fill=(0, 255, 0))
image.save("multi.png")
Image.new("RGB", (100, 100), "black").save("empty.png")
```

将下面内容保存为 `fixtures.json`。`box` 是图片中的 `[x, y, width, height]` 百分比，框右／下边界按像素排他边界计算。替换为真实模型后，期望值必须独立绑定权重、输入、参数和执行设备；不要从待测 HTTP 响应反向生成期望，也不要在失败后放宽容差。CPU 检查不等于 CUDA 数值或托管资格验证。

```json
{
  "multi": {
    "image": "multi.png",
    "box_tolerance": 0.00001,
    "score_tolerance": 0,
    "detections": [
      { "native_class_index": 0, "label": "red-region", "box": [10, 10, 20, 30], "score": 1 },
      { "native_class_index": 1, "label": "green-region", "box": [50, 40, 30, 50], "score": 1 }
    ]
  },
  "empty": { "image": "empty.png", "box_tolerance": 0, "score_tolerance": 0, "detections": [] }
}
```

图片路径相对于 fixture 文件解析；工具将本地图片作为有界 data URL 提交，不让服务读取任意本地路径。另开终端，用启动服务时的同一份 token 文件检查 HTTP。`AAP_PLUGIN_TOKEN` 是当前候选工具实际读取的变量，不是平台 API key；只把它传给本次检查：

```bash
AAP_PLUGIN_TOKEN="$(cat ./token)" yoao-plugin check --url http://127.0.0.1:8000 --fixtures ./fixtures.json
```

检查核对多检测与空检测的数量、类别、坐标、分数、认证、参数和本地路径拒绝，只报告 HTTP 事实。CPU 示例不实现 GPU 检查钩子。具备 `probe`／`qualification-server` 钩子的 GPU provider 才能进行下面的可选检查；先确保指定设备获准独占，用于取消场景的图片不大于 2 MiB：

```bash
yoao-plugin check --qualify-local --entrypoint your-gpu-provider --fixtures ./fixtures.json \
  --device cuda:0 --gpu-uuid GPU-实际设备UUID --asset-dir ./assets \
  --exclusive-gpu --report ./local-gpu-evidence.json
```

修改模型实现后，重新运行 `yoao-plugin build my-detector`，安装新 wheel 并重启开发服务；记录命令输出的 SHA-256。镜像使用已检查的作者 wheel，不依赖平台源码或 editable 安装。

本地检查执行真实的缺失资产、错误设备、驻留、两次推理释放和签名排空／取消验证；它启动独立 loopback 服务和临时签名密钥，不是平台控制器的签名权，也不证明平台托管资格。看不到必要事实时失败而不虚报成功。平台图片 URL 主机须由部署管理员明确配置。

检测 SDK 的窄 Python 合同由 `Detection`、`DetectionModel` 和 `DetectionProvider` 组成：provider 的 `load(model_id, device, config)` 返回持有 `classes` 的 model，model 的 `predict(image, parameters)` 返回原图像素坐标的检测，`close()` 释放模型。SDK 负责协议响应、类别索引和坐标转换；裁剪图坐标相对传入的裁剪图，父图映射仍由平台处理。通用 SDK 不依赖 ORM、Celery 或原生模型框架；具体模型运行时由 provider 提供，可选训练产物通过共享受控收据安装，不直接访问平台数据库。

构建生产镜像时，先将作者 wheel 和全部依赖放入 wheelhouse，并准备包含每个固定版本及 SHA-256 的完整 requirements lock。基础镜像必须以 digest 固定，`--entrypoint` 必须由 lock 中的发行包提供；镜像的 pip 安装步骤禁网并使用 `--require-hashes`，不加载工作目录。系统包安装步骤仍需要可用的软件包源。以下命令使用已安装的 CLI，不要求当前目录位于平台仓库：

```bash
yoao-plugin build --base-image image@sha256:<真实摘要> \
  --wheelhouse /path/to/complete-wheelhouse --requirements-lock /path/to/requirements.lock \
  --entrypoint my-color-region-plugin --tag registry.example/my-detector:release --push
```

`--push` 可省略。作者按公开推理 manifest schema 描述自己的能力、资源、启动参数和数据目录，绑定镜像可检查的实际 RepoDigest；SDK 生成镜像的可写数据目录为 `/var/lib/aap-inference`。不能用 LibreYOLO 的专属 manifest 工具为不相关的自定义插件生成身份。模型来源与许可证分别审核，清单展示不授予下载、启用或再分发权限。管理员按[插件安装流程](/user-guide/superadmin/ml-backend-registry#可安装推理插件)审核制品和资格，安装不会自动为项目启用。

安装实例退役时，CPU 实例的不可逆终止排空使用认证的 `/lifecycle/terminal-drain`，并绑定注册实例和当前 boot；GPU 终止排空使用 `/lifecycle/terminal-drain-signed`，除业务 token 外还要求平台签发的 GPU DRAIN 许可。两种路径都等待真实模型借用和构建退出后卸载；业务 token 不能代替 GPU 许可，也不能重启已终止的实例。

## 协议教学：echo 示例

下文的 echo 是协议教学，不是真实模型或推荐的模型实现。OCR 片段仅解释协议映射，省略具体推理封装。

> **协议以 [ML Backend 协议契约](/dev/reference/ml-backend-protocol) 为准**，本文不重复抄协议；遇到字段语义疑问，一律以协议文档为权威。

完成本教程你将得到：

1. 一个在本机跑通的 echo backend，并通过平台「测试连接」；
2. 对 `/health`、`/setup`、`/predict` 三端点契约的最小心智模型；
3. 一个从 echo 改出来的 OCR backend 骨架（真实模型推理 + `attributes.text` 输出）；
4. 进阶方向：多模型目录 / variants / warmup 与 `yoao_protocol_v2` 共享库。

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

完成自己的图像读取、模型推理封装和安全检查后，才能形成可用的 OCR backend；上面的片段本身不是完整实现。固定 demo 输出可对照 `docs-site/dev/examples/mock-v2-backend/` 的 `ppocr` 条目与 `_demo_shapes("ocr")`。

> 建议同步给自己的 backend 写 contract tests（fastapi `TestClient` 即可，无需起服务），两个示例目录下的 `tests/test_contract.py` 可直接当模板抄。

## Step 4：进阶——多模型目录、variants、warmup 与共享库

单模型骨架跑通后，按需取用 v2.1 的进阶能力，**完整形态参考 mock-v2 示例**：

- **多模型目录**：一个 backend 暴露 N 个 model（`/setup.models[]`），如 YOLO 的 det/seg/pose/obb 四条目（协议 §4.1）。
- **模型变体**：`supported_variants` 多轴声明 + `default_variants` 默认组合 + `variant_combinations` 非笛卡尔积过滤；`/predict` 经 `context.model_variants` 热切换（协议 §2.2 / §4.1.6）。
- **预热与运行时观测**：`/setup.warmup_endpoint: true` + `POST /warmup`；`/predict` 响应带 `cache_hit` / `model_load_ms`，前端据此区分「加载中」与「推理中」（协议 §4.2 / §4.4）。
- **标准错误形态**：非法 variant → 422 `variant_not_supported`；权重缺失 / 显存不可服务 → 503 `model_unavailable` + `Retry-After`（协议 §6）。

**生产建议**：按[制品获取说明](/dev/concepts/module-deployment#plugin-candidate-artifacts)核验与宿主匹配的 `yoao-protocol-v2`，从 `yoao_protocol_v2` 导入请求、响应、标准错误与词表。可参考[官方 YOLO 插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/yolo)的服务组织方式；安装与构建请使用候选修订自带的说明，而非其他版本的 README。

本教程的 OCR 主题，真实推理参考 [RapidOCR 插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/rapidocr)：它把 RapidOCR 的 det→cls→rec 拆为**原子能力 + 端到端 composite** 三个 model（`ocr-det` / `ocr-rec` / `ocr-e2e`），并输出 `attributes.text` / `orientation` / `language` 富属性（协议 §4.1.8）。

## Step 5：全局注册并对项目启用

backend 跑起来后，按两层模型接入平台：

1. **全局注册（超级管理员）**：进入「模型市场 → 注册管理 → 实例」，添加平台可达的 Backend URL。手动 Token 或 None 方式可用「测试连接」检查公开健康端点；保存后执行实例健康检查，读取 `/setup` 更新能力快照；
2. **项目启用（项目管理员）**：进入目标项目的「项目设置 → ML 模型」，勾选启用该服务池并按需设置项目主模型；
3. **触发推理**：在批次详情页或预标注编排面板触发「AI 预标注」；若为交互式 backend（`/setup.is_interactive: true`），还会自动出现在工作台 AI 助手工具栏中。

操作细节见用户手册：[ML 后端绑定](/user-guide/projects/ml-backends)、[AI 预标注](/user-guide/projects/ai-preannotate)；超管全局视角见 [ML Backend 注册](/user-guide/superadmin/ml-backend-registry)。

本教程的 echo 示例不要求认证，开发调试可选 None。系统托管仅适用于部署配置的可信地址：保存时平台验证该服务的 `/managed-auth`，不需要填写手动 Token，普通「测试连接」也不能替代这一步。自建服务可实现自己的 Bearer 认证，手动注册时服务端与平台的令牌必须一致。受控插件使用各安装实例自己的凭据，不继承同名服务的托管身份。

一个外部 Backend 实现普通预测协议，不代表它已实现训练权重安装、共享逐样本评估或 GPU 仲裁。接入这些能力前分别核对[协议契约](/dev/reference/ml-backend-protocol)；服务池接流和凭据生命周期见[模型服务接入、路由与认证](../concepts/ml-backend-routing-and-auth)，已有训练模型的排查见[模型回流链路](../how-to/debug-model-return)。

## 下一步

- 对照协议 [接入 checklist](/dev/reference/ml-backend-protocol#_9-接入-checklist) 自查每一项
- 排障：[ML Backend 不可用 runbook](/ops/runbooks/ml-backend-down)
