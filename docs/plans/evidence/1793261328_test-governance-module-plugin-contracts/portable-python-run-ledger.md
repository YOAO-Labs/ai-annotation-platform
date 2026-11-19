# 独立 Python 套件运行账本

证据目录为 `.artifacts/test-results/portable-junit/`。各 suite 使用本工作树的源码与各自的依赖边界，未使用阶段 F 的联合验收环境。

## 2026-11-19：文档示例与站点 Agent

- CPython 3.11.13：echo 与 mock-v2 各自用锁文件和独立环境执行 `uv run --extra test pytest -q --junitxml=…`。`echo-test-results.xml` 为 **5 通过**，`mock-v2-test-results.xml` 为 **15 通过**，没有跳过；各有一条 Starlette TestClient 弃用告警。这是示例 ASGI 合同，不是真模型 HTTP 服务验收。
- 重构前，在不向 `sys.path` 注入测试目录的模式下单独收集 `apps/storage-agent/tests/test_pairing.py`，因直接导入 `conftest` 退出 2，零项收集。将共享值与 HTTP 替身移入本地 `tests.support`、TLS peer 移入 pytest fixture 后，同一文件可独立收集 **40 项**。前后原始结果为 `pairing-import-before.log`、`pairing-import-after.log`。
- Agent 加两个便携契约根：原默认模式与新 `--import-mode=importlib` 真实执行各 **552 通过**，无 skip、失败或错误；`storage-agent-test-results.xml`、`storage-agent-isolated.xml` 分别保留。实际覆盖本机临时目录、TLS、断连重放与新进程，不是跨网络部署验收。
- 对账暴露一处测试 ID 来自收集时的随机 UUID。保留每次生成真实命令 ID，仅给该 chunks 负例命名 `other-command-chunks`。551 项原身份不动；该新 nodeid 定向通过，当前 552 个收集项都有对应通过记录（`storage-agent-collection.txt`、`stable-agent-node.xml`、`storage-agent-reconciliation.json`），不把定向复验加到套件总数。

## 2026-11-19：训练执行器 CPU 测试入口

- 使用本工作树的 `apps/training-executor/uv.lock`，CPython 3.12.11；实际安装 PyTorch `2.14.1+cu132`、torchvision `0.29.1+cu132`、Ultralytics `8.4.183`、LibreYOLO `1.6.0`。从新缓存填充依赖的首次联机同步超时，不是 pytest 结果；随后使用本机另一个 uv 的缓存按同一锁文件完成同步，未改锁、替换为 CPU wheel 或移除原生依赖。
- 首轮收集 **237 项／15 个文件**，与 JUnit 的逐项身份一致：**230 通过、1 失败、6 跳过**（`training-collection.txt`、`training-executor-before.xml`）。取消续训场景的真实 journal 记为 `preparation_failed`、`KeyError: 'run_id'`（`resume-cancel-before.json`）；恢复输入已落盘，夹具却未给执行器提供 Run 身份，尚未进入取消分支。只补齐派工夹具的 Run／Attempt UUID，不改生产代码、检查点字节、epoch 或不启动子进程的断言。
- 修正后在同一锁定环境、禁用 CUDA 设备，重跑完整选集：**231 通过、6 跳过、0 失败／错误**。前后 237 个唯一 case 与 collection 逐项相等，唯一状态变化是上述取消场景。最终 journal 为 `cancelled`、无子进程记录，保留 epoch 2 的原检查点字节。见 `training-executor-test-results.xml`、`training-reconciliation.json`；临时环境已退役。
- 六项原生预览缺少受控 `PREVIEW_TEST_BASE` 权重，未运行；不以任意下载的权重代替。此处没有真实 GPU／部署验收，也没有远端 CI 上传结果。
