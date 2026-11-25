# Echo ML Backend（最小协议参考实现）

> 这是 [ML Backend 协议](https://yoao-labs.github.io/ai-annotation-platform/dev/reference/ml-backend-protocol) v2.1 的最小可跑参考实现。文档构建比较 [Echo 教程片段](https://yoao-labs.github.io/ai-annotation-platform/dev/ml-backend/starter#step-1-跑通-echo-示例)与 `main.py`；CI 用 `tests/` 运行实际端点合同。

## 快速开始

### 方式 A · 直接 uvicorn

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 方式 B · Docker

```bash
docker build -t echo-ml-backend .
docker run --rm -p 8000:8000 echo-ml-backend
```

## 验证端点

另起一个 shell：

```bash
./test.sh
# 或显式指定 host：
HOST=http://host.docker.internal:8000 ./test.sh
```

期望输出：3 个端点全部 200，最后打印 `✓ echo-backend 全部端点 200。…`

## 接入平台

按[接入教程](https://yoao-labs.github.io/ai-annotation-platform/dev/ml-backend/starter)的直连步骤注册并启用样例，然后触发预测。使用平台进程可达的 base URL；平台在容器中时，按本地网络使用可访问的宿主地址。固定结果只证明协议链路，不证明真实模型。

## 跑 contract tests

```bash
uv run --extra test pytest -q
# 或 pip install -e ".[test]" && pytest -q
```

## 把 echo 改成真 backend

在 `main.py:predict()` 中把固定 demo bbox 替换为真实推理。完整改造路径见 [ML Backend 接入教程](https://yoao-labs.github.io/ai-annotation-platform/dev/ml-backend/starter)。

多模型目录、variants 与交互式 prompt 见 [ML Backend 协议](https://yoao-labs.github.io/ai-annotation-platform/dev/reference/ml-backend-protocol)和同级的 `mock-v2-backend/` 示例。
