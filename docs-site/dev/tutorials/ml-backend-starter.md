---
title: ML Backend 接入教程
audience: [dev]
type: tutorial
status: stable
last_reviewed: 2026-11-23
---

# ML Backend 接入教程：自定义插件与 HTTP 契约

已有符合[推理 HTTP 契约](/dev/reference/ml-backend-protocol)的服务，可直接从[直连注册](#direct-registration)开始，不需要安装插件 SDK。没有现成服务时，下面的 CPU 示例只需 Python 和经核验的 wheel：先在本机检查算法，再把同一个作者入口部署到平台私网。需要平台管理镜像、启停和退役时，才走[安装档](#managed-installation)。

## 使用自定义推理 SDK

作者 SDK 是独立的 `yoao-inference-plugin-sdk`（Python 导入 `yoao_inference_plugin`，命令 `yoao-plugin`），不是调用平台 API 的 `yoao-sdk`。开发过程不依赖平台或官方插件 checkout、数据库、管理员凭据、Torch 或模型权重。

先从可信渠道取得经核验的候选 SDK、平台便携契约及匹配 Python 解释器和平台的完整依赖 wheelhouse，包括 `build` 和生成项目的构建后端。指定真实 manifest 路径或 URL 与可信的 manifest SHA256，按[制品获取接口](/dev/concepts/module-deployment#plugin-candidate-artifacts)核验来源；不要假定新发行名已公开发布。第三方依赖和权重的许可分别核验。以下以 CPython 3.11 为例：

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

检查核对多检测与空检测的数量、类别、坐标、分数、认证、参数和本地路径拒绝。它只证明开发机上的模型可用，不证明平台能连接或读取项目图片。

## 直连档：注册 HTTP 服务并预测 {#direct-registration}

1. **作者部署服务。** `yoao-plugin dev` 固定监听 `127.0.0.1`，注册表拒绝该地址。仅把表单 URL 换成宿主机 IP 或 Docker 网关，并不会改变监听地址。停止 `dev`，改用已安装的 **provider `serve`**，绑定平台 API、Inference owner 和执行 worker 可达的受控接口；同一私有容器网络也可注册服务 DNS，无需发布到公网。
2. **允许服务读取平台图片。** 示例的本地 `check` 使用 data URL，项目任务使用有时限的签名 URL。默认 `DetectionConfig.allowed_image_hosts` 为空。请向管理员取得**签名时使用且从模型端可达的对象存储主机名**；不要改写已签名 URL，不给模型 S3 密钥或通配许可。对于生成的示例，在 `my_color_region_plugin.py` 的 `args = parser.parse_args()` 之前添加 `serve.add_argument("--image-host", action="append", default=[])`，并给现有 `DetectionConfig(...)` 加上 `allowed_image_hosts=tuple(args.image_host)`。这是示例自定义参数，不是 `yoao-plugin dev` 的选项。

   ```bash
   yoao-plugin build my-detector
   python -m pip install --no-deps --force-reinstall my-detector/dist/*.whl
   my-color-region-plugin serve --device cpu --token-file ./token \
     --host "$PRIVATE_BIND_IP" --port 8000 --image-host "$STORAGE_HOSTNAME"
   ```

   `PRIVATE_BIND_IP` 是服务所在主机的私网接口地址，`STORAGE_HOSTNAME` 只填主机，不含协议、端口和路径。其他语言的服务直接部署其受认证的 `/setup` 和 `/predict`；跨不可信网络用 HTTPS。

3. **超管注册并启用全局池。** 「模型市场 → 注册管理 → 实例」添加服务，用调用方可达的 URL 和**手动 Token**（即服务端 token 文件的值，不是平台 API Key）。「测试连接」只探测 `/health`；保存后执行实例健康检查，确认 `/setup` 的模型目录包含 `colors`。在「服务池」核对同名单成员池、成员和能力；若池已停用，选择「启用服务池」。能力漂移须先审核，不能用启用代替。仅有健康实例、没有启用全局池时，项目绑定会返回 `409 ml_backend_pool_not_enabled`。普通 URL 注册不创建插件安装身份。

   部署若使用服务池 `enforce` 路由，健康与池启用还不够。确认作者服务使用与平台同代的 `yoao-protocol-v2` wheel（核对 manifest 摘要，不能只比版本号）。注册取得实例 ID 后，服务运营者以 `AAP_ROUTE_REGISTRY_ID` 指定该 ID、`ROUTE_CONTROL_TOKEN_FILE` 指向登记的服务 token 文件，启动同一个 `serve`。平台运维方须完成[路由连续性绑定](/api/guides/inference-runtime#route-continuity)再预测。仅支持普通预测、或只实现旧控制路径的 HTTP 服务不能靠 URL 加入强制路由；不要为了接入而更改平台策略。此绑定不创建安装身份，也不授予 GPU 管理权限。

4. **项目负责人启用并验证。** 在「项目设置 → 模型服务 → 管理模型服务」显式启用该服务池。选择项目内的 `multi.png` 图像任务；可用项目管理凭据调用 `POST /api/v1/projects/{project_id}/ml-backends/{backend_id}/predict-test?task_id={task_id}`。这里传**实例 ID** 而非池 ID。核对响应属于该任务，红、绿框分别为 `[10,10,20,30]`、`[50,40,30,50]`，分数均为 1。此接口只返回预测；需要保存候选时，在[项目预标注](/user-guide/projects/ai-preannotate)中选择已启用的服务和模型，执行后在任务中检查候选。批量型 `colors` 不会因此成为交互工具。

**直连的能力边界。** 手动 Token 只授权平台访问服务，不授予平台启停外部进程的权力，也不产生镜像来源、GPU 驻留、签名准入或安全停机证明。没有 GPU 声明不等于已确认 CPU；受管资源仍须通过实际健康、资源与生命周期门禁。受控训练权重也不是按「直连／安装」一刀切：还需项目启用、符合要求的认证、精确实例路由，以及兼容的权重安装、加载试用和回执。普通示例没有这些能力。详见[模型服务接入、路由与认证](../concepts/ml-backend-routing-and-auth)。

## 可选：本地 GPU 资格检查

CPU 示例不实现 GPU 检查钩子。具备 `probe`／`qualification-server` 钩子的 GPU provider 才能进行下面的可选检查；先确保指定设备获准独占，用于取消场景的图片不大于 2 MiB：

```bash
yoao-plugin check --qualify-local --entrypoint your-gpu-provider --fixtures ./fixtures.json \
  --device cuda:0 --gpu-uuid GPU-实际设备UUID --asset-dir ./assets \
  --exclusive-gpu --report ./local-gpu-evidence.json
```

修改模型实现后，重新运行 `yoao-plugin build my-detector`，安装新 wheel 并重启开发服务；记录命令输出的 SHA-256。镜像使用已检查的作者 wheel，不依赖平台源码或 editable 安装。

本地检查执行真实的缺失资产、错误设备、驻留、两次推理释放和签名排空／取消验证；它启动独立 loopback 服务和临时签名密钥，不是平台控制器的签名权，也不证明平台托管资格。看不到必要事实时失败而不虚报成功。平台图片 URL 主机须由部署管理员明确配置。

检测 SDK 的窄 Python 合同由 `Detection`、`DetectionModel` 和 `DetectionProvider` 组成：provider 的 `load(model_id, device, config)` 返回持有 `classes` 的 model，model 的 `predict(image, parameters)` 返回原图像素坐标的检测，`close()` 释放模型。SDK 负责协议响应、类别索引和坐标转换；裁剪图坐标相对传入的裁剪图，父图映射仍由平台处理。通用 SDK 不依赖 ORM、Celery 或原生模型框架；具体模型运行时由 provider 提供，可选训练产物通过共享受控收据安装，不直接访问平台数据库。

## 安装档：交付供平台管理的制品 {#managed-installation}

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

echo 示例位于仓库 `examples/ml-backend/echo-ml-backend/`，不到 100 行、零外部重依赖（仅 `fastapi` + `pydantic`），整目录拷贝即可作为 starter：

```bash
cd examples/ml-backend/echo-ml-backend
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

另起一个 shell 验证端点：

```bash
./test.sh
# 期望：/health /setup /predict 全部 200
```

它对任何输入都返回同一个固定 demo bbox——没有模型、没有 GPU，但协议形态是完整的 v2.1，足以让平台端到端链路（注册 → 测试连接 → AI 预标注 → 预测落库）整条走通。

::: details Echo backend 完整源码

<!-- snippet:examples/ml-backend/echo-ml-backend/main.py -->

```python
"""Echo ML backend — 协议 v2.1 参考实现（最小可跑版）。

满足 ml-backend-protocol 的 4 个端点：/health、/setup、/versions、/predict。
所有 /predict 输出固定的 demo bbox，让平台端到端链路可以直接走通。
真实 backend 的 inference 替换到 predict() 内部即可。

零外部依赖（仅 fastapi + pydantic），整目录可直接拷走当 starter。
字段语义以 docs-site/dev/reference/ml-backend-protocol.md 为准。
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

MODEL_VERSION = "echo-v1"


class TaskItem(BaseModel):
    id: str
    file_path: str


class PredictRequest(BaseModel):
    tasks: list[TaskItem]


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/setup")
async def setup():
    return {
        "name": "echo-backend",
        "version": "0.1.0",
        "protocol_version": "2.1",
        "task_contract_revision": 1,
        "compat_protocol_versions": ["2.0"],
        "model_version": MODEL_VERSION,
        "is_interactive": False,
        "labels": ["demo"],
        "models": [
            {
                "id": "echo-detect",
                "display_name": "Echo 固定框 demo",
                "task": "detect",
                "supported_prompts": ["none"],
                "supported_geometric_outputs": ["bbox"],
            }
        ],
    }


@app.get("/versions")
async def versions():
    return {"versions": [MODEL_VERSION]}


@app.post("/predict")
async def predict(req: PredictRequest):
    results = []
    for t in req.tasks:
        results.append(
            {
                "task": t.id,
                "result": [
                    {
                        "type": "rectanglelabels",
                        "value": {
                            "x": 0.1,
                            "y": 0.1,
                            "width": 0.2,
                            "height": 0.2,
                            "rectanglelabels": ["demo"],
                        },
                        "score": 0.5,
                    }
                ],
                "score": 0.5,
                "model_version": MODEL_VERSION,
                "inference_time_ms": 1,
                "cache_hit": True,
                "model_load_ms": 0,
            }
        )
    return {"results": results}
```

<!-- /snippet -->

:::

## Step 2：理解三端点契约

打开 `main.py`。这里的 echo 实现三个接入端点；各端点的必需条件与字段以协议为准。

| 端点            | 一句话职责                                               | 协议章节  |
| --------------- | -------------------------------------------------------- | --------- |
| `GET /health`   | 返回 200 = 在线（平台只看状态码）                        | §1        |
| `GET /setup`    | 自描述能力：协议版本、模型目录、支持的 prompt / 几何输出 | §4 / §4.1 |
| `POST /predict` | 接 `tasks[]`，逐 task 回 `result[]` 标注数组             | §2 / §3   |

`/versions` 是可选的。三个关键认知：

- **`/setup` 是能力的唯一真相源**：前端哪些 AI 工具可用、参数面板长什么样，全部由 `/setup` 返回体决定。echo 声明了 `protocol_version: "2.1"`、一条 `models[]` 目录条目（`task: "detect"`、`supported_prompts: ["none"]` 表示纯批量）。
- **`/predict` 的 `result[]` 是 Label Studio 风格标注数组**：`type` + `value`（坐标兼容 `[0,1]` 与 `[0,100]` 百分比），见协议 §3。
- **用契约测试核对字段**：先检查后端实际请求与响应，再在平台测试连接和预测。仅健康检查通过不能证明结果格式正确。

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

完成自己的图像读取、模型推理封装和安全检查后，才能形成可用的 OCR backend；上面的片段本身不是完整实现。固定 demo 输出可对照 `examples/ml-backend/mock-v2-backend/` 的 `ppocr` 条目与 `_demo_shapes("ocr")`。

> 建议同步给自己的 backend 写 contract tests（fastapi `TestClient` 即可，无需起服务），两个示例目录下的 `tests/test_contract.py` 可直接当模板抄。

## Step 4：进阶——多模型目录、variants、warmup 与共享库

单模型骨架跑通后，按需取用 v2.1 的进阶能力，**完整形态参考 mock-v2 示例**：

- **多模型目录**：一个 backend 暴露 N 个 model（`/setup.models[]`），如 YOLO 的 det/seg/pose/obb 四条目（协议 §4.1）。
- **模型变体**：`supported_variants` 多轴声明 + `default_variants` 默认组合 + `variant_combinations` 非笛卡尔积过滤；`/predict` 经 `context.model_variants` 热切换（协议 §2.2 / §4.1.6）。
- **预热与运行时观测**：`/setup.warmup_endpoint: true` + `POST /warmup`；`/predict` 响应带 `cache_hit` / `model_load_ms`，前端据此区分「加载中」与「推理中」（协议 §4.2 / §4.4）。
- **标准错误形态**：非法 variant → 422 `variant_not_supported`；权重缺失 / 显存不可服务 → 503 `model_unavailable` + `Retry-After`（协议 §6）。

**生产建议**：按[制品获取说明](/dev/concepts/module-deployment#plugin-candidate-artifacts)核验与宿主匹配的 `yoao-protocol-v2`，从 `yoao_protocol_v2` 导入请求、响应、标准错误与词表。可参考[官方 YOLO 插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/yolo)的服务组织方式；安装与构建请使用候选修订自带的说明，而非其他版本的 README。

本教程的 OCR 主题，真实推理参考 [RapidOCR 插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/rapidocr)：它把 RapidOCR 的 det→cls→rec 拆为**原子能力 + 端到端 composite** 三个 model（`ocr-det` / `ocr-rec` / `ocr-e2e`），并输出 `attributes.text` / `orientation` / `language` 富属性（协议 §4.1.8）。

## Step 5：连接平台

按[直连注册](#direct-registration)部署、注册、启用并触发推理。echo 示例不要求认证，**仅限隔离的协议教学环境**可选 None；固定 demo 框不能用于验证真实模型。项目绑定细节见[启用模型服务](/user-guide/projects/ml-backends)，超管操作见[模型服务注册](/user-guide/superadmin/ml-backend-registry)。

## 接入 checklist {#integration-checklist}

- [ ] `/health` 返回 200
- [ ] `/setup` 声明 `protocol_version: "2.1"` 与 `compat_protocol_versions: ["2.0"]`
- [ ] `/predict` 批量 schema 与[批量预测合同](/dev/reference/ml-backend-protocol#_2-1-批量预测-同步)对齐，至少回填 `task` + `result`
- [ ] 如声明 `is_interactive=True`，`/predict` 也接受[交互请求](/dev/reference/ml-backend-protocol#_2-2-交互式预测)
- [ ] `/predict.context.model_variants` 支持通用 axis dict；旧字段仅作为兼容期 normalize
- [ ] `/setup.params` 给阈值/数量/简化容差等字段标 `x-platform-role`
- [ ] 每条 result 的 `type` 与项目类型匹配（image-det 项目至少要有 `rectanglelabels`）
- [ ] 非法 variant 返回 422；模型暂不可用返回 503 + `Retry-After`（见[错误响应](/dev/reference/ml-backend-protocol#_6-错误响应约定)）
- [ ] 在当前部署的推理请求时限内返回结果；先运行[协议验证](/dev/reference/ml-backend-protocol#协议验证)，再用实际平台注册和预测核对端到端结果。

## 下一步

- 排障：[ML Backend 不可用 runbook](/ops/runbooks/ml-backend-down)
