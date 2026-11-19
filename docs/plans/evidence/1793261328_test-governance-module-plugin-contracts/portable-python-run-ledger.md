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
- 该轮没有提供 `PREVIEW_TEST_BASE`，六项预览保留为跳过；后面的定向结果不回写这份报告。

### 用官方 YOLO11n 权重补验原六项

- 从 [Ultralytics 官方 release](https://github.com/ultralytics/assets/releases/tag/v8.4.0) 取得检测权重 `yolo11n.pt`（不是分割权重）。[GitHub 资产 340060970](https://api.github.com/repos/ultralytics/assets/releases/assets/340060970) 公布的大小为 **5,613,764 字节**，SHA-256 为 `0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1`；下载后先核对大小和摘要，再传给 `PREVIEW_TEST_BASE`。发布元数据见 `official-yolo11n-release.json`，权重不入 Git。
- 从原 JUnit 提取六个跳过的 nodeid，在同一执行器依赖锁、CPython 3.12.11、CPU 环境定向执行：**6 通过、0 跳过／失败／错误**。逐项身份与原报告一致，包括增强前后像素和框的对照、CutMix 来源与几何、rect／epoch 策略和损坏输入拒绝。见 `official-yolo11n-selected.txt`、`official-yolo11n-preview.xml`、`official-yolo11n-reconciliation.json`。这是前次 231 项通过加本次六项定向通过，不是一次全量运行，也不是 GPU 或部署验收。
- 单独执行训练 workflow 的取件步骤，传给 pytest 的文件与官方摘要相符。保持相同大小、只翻转一个字节后，同一校验命令退出 1，没有把未通过校验的文件交给 pytest（`official-yolo11n-ci-step.json`）。未运行远端 CI；本地测试及生产执行器未增加联网行为。

## 2026-11-19：在仓库外安装 SDK 并执行便携契约

证据目录：`.artifacts/test-results/sdk-distribution/`。CPython 3.11.13、uv 0.11.29；只使用本树构建的 SDK 和五个共享契约包，以及经 SHA-256 校验的 `yoao-backend-runtime` wheel，没有外部仓库 editable 安装。

- 修正前的 SDK sdist 包含两份仓内测试却不含四份冻结响应 JSON；在仓外对已安装 wheel 运行受影响用例为 **11 失败、2 跳过**（`before-archive.json`、`before-outside.xml`）。Protocol 缺少 `native_mask_interaction.json` 导致收集错误，Training 缺少 `legacy_task_identities.json` 导致历史身份向量失败；Inference 的 legacy 输入原已随包分发。未改任何期望向量的字节。
- 把文档、调用点与目录生成对账留给仓内强制执行；在一次性副本移去 OpenAPI 快照，三项选定的仓内检查均 **失败，无跳过**（`missing-checkout.xml`）。还原快照后，生成检查发现模型注册表创建与修改端点的两处 422 描述已过期；同步后目录与生成器输出逐字节一致（`catalog-drift.json`）。
- 即使两边各有 1,264 项，最初仍有 44 项身份含收集时生成的 UUID。仅命名这些参数用例，不固定输入 UUID，也不在对账时屏蔽参数值。用一项重复通过替换另一项、保持总数不变，对账仍拒绝，并列出准确缺失和多余项（`equal-count-negative.json`）。
- 最终同一入口先直接从 sdist 安装核心 SDK，验证随包指南、离线能力发现和不装 CLI extra 的依赖错误；再替换成带测试依赖的非 editable wheel 并运行真实 `yoao --version`。仓内 **49 通过**，仓外 **1,215 通过**；两份 JUnit 合计恰好覆盖 **1,264 个唯一 SDK 用例**。Inference／Protocol／Training 另为 **30／30／32 通过**，五份报告全部零跳过、零失败、零错误。见 `installed.log`、`installed-reconciliation.json`、`installed/` 下的 JUnit、collection 及十二个构建制品摘要。
- 这是本地执行 GitHub Actions 使用的命令，不是远端 CI、SDK 发布、独立 Plugin-SDK 资格或 T1／T2 联合验收。
