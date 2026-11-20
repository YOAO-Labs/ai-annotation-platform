# 独立 Python 套件运行账本

证据目录为 `.artifacts/test-results/portable-junit/`。各 suite 使用本工作树的源码与各自的依赖边界，未使用阶段 F 的联合验收环境。

## 2026-11-21：按子模块移除不能区分对错的断言

以 11 月 20 日的工作树清单为基线，8 个只读代理筛查 12 个一级模块、126 个子模块；候选按入口、输入和可观察的失败逐案复核。26 个子模块删除 36 个旧定义，其中 3 个引用已移除的 Python helper，清理前就无法收集。新增 1 个真实 PostgreSQL 分页用例，净减 35。没有改产品代码或 CI 门禁。

- **保留能分辨对错的检查**：工作区行投影交换 `id` 和 `display_id` 后，旧的 25 列计数仍绿，完整有序列名断言变红。两个 SQL 形状用例改由结果保护：空过滤返回全部测试任务；按活动时间降序翻页时，同一时间的两条任务以 ID 顺序分布在两页，不重不漏。另在公开查询路由核对反馈后的真实活动时间。
- **运行边界**：受影响范围按最终测试 ID 去重，API 227、独立服务 41、Web 102、存储 Agent 4，共 374 个通过。存储 Agent 另有 3 个在 macOS 因 Linux `openat2` 不可用而初始化报错；没有替换文件安全检查。Playwright 仅收集，未跑浏览器。隔离 PostgreSQL 与 S3 服务已退役。
- **复核入口**：`.artifacts/test-results/module-prune-20261120-234723/` 内的 `removed-cases.csv` 逐条列出原位置、删除理由和保留用例；`screened-modules.csv` 含 126 个子模块，包括无删除的模块。JUnit、首次失败、单点复验及负对照均单独保存。前端角色筛选负对照见 [Web 账本](./web-governance-run-ledger.md)。

## 2026-11-20：用原始清单和临界输入检验训练契约

证据：`.artifacts/test-results/template-review-training/`。使用工作树内的 `training_contract`、`task_contract` 和 API 的 Python 环境，`test_input_membership.py` 与 `test_models.py` 清理前后均为 **60 通过，无跳过**（`training-baseline.xml`、`training-current.xml`）。不需要启动训练、数据库或 GPU。

- **成员身份**：把 `48×32` 样本的高度错写成宽度，旧成员套件 **23/23 通过**：冻结和回执共用同一个投影，错误摘要也能自洽。改为从原始清单用标准库计算期望 SHA-256，核对每个训练任务与摘要的配对，不固定加载顺序。调换样本顺序、内容相同但任务不同、回执绑定错误执行等场景仍保留。相同错改现在报出任务 ID 对应的实际与期望摘要。
- **整批预算**：旧夹具本来就大到单张图也超限。分别漏乘批大小、把 `>` 错写成 `>=`，旧模型套件都是 **37/37 通过**。新夹具用两张 `800×800` 图，50 类正好 **64,000,000** 个元素，51 类 **65,280,000**。前一种错改放过超限配置，后一种拒绝合法配置；现在各自命中不同的失败断言。
- 三个错误分别在独立 Python 进程中注入，未写入产品代码。`old-*.xml` 使用 `208eeff9b` 的历史用例，`new-*.xml` 使用现有用例；负对照不计入正常通过数。这是共享预检与成员身份的证据，不是原生训练或产物发布验收。

## 2026-11-20：从注册 CLI、实际 TUI 表格和备份文件验证结果

第二轮证据：`.artifacts/test-results/agent-guardrails-round2/`。五个 Python 文件的测试函数定义由 **103 降为 98**（`definitions.json`，不展开参数行）。

- **CLI**：四条回填测试共用注册入口，但尺寸／缩略图、运行中／已失败或取消仍独立执行。逐项核对 JSON 回执、退出码、幂等键以及只发一次 POST，不把受理当完成。连接器导入保留响应丢失后的显式重放；取消数据集关联覆盖预览后确认、预览后拒绝、JSON 无确认零请求，未删响应丢失和重放场景。
- **TUI**：以前关联项目替身总返回空数组，只检查「零行」。现从数据集列表按 Enter，在真实 Textual 表中读出指定素材的文件名与尺寸，以及只出现在该数据集详情里的项目。数据集请求经过真实 SDK 和 respx，其余主页服务未接入。将 `Datasets.list_projects` 临时改为返回空列表后，新用例在读取项目行时失败（`tui-mutant.xml`）。
- **恢复与备份**：历史夹具两种 owner 的稳定 UUID 不再与同一个函数的当次返回值比较。临时换掉 UUID 命名空间，旧的一致性检查仍通过，新检查拒绝（`owner-mutant.log`）。备份只重新读取并校验一次落盘 manifest，仍核对摘要及无新增外部请求。首次运行被 macOS 临时目录的路径别名拒绝；仅将测试自有根目录解析成物理路径，未修改备份的安全检查，显式软链拒绝用例仍通过。
- **执行**：SDK／CLI／Textual **37 通过**（`sdk.xml`）。恢复固定 ID **1 通过**（`storage.xml` 同时记录了首次备份夹具失败），修正夹具后备份正例和两项拒绝 **3 通过**（`storage-physical.xml`）。五个文件通过仓库固定版本的 Ruff（`ruff-final.log`）。没有运行真实数据库、队列或对象存储。

## 2026-11-20：移除重复的 owner 测试，以独立协议期望保留拒绝路径

本轮证据目录：`.artifacts/test-results/agent-guardrails/`。相对 `208eeff9b`，13 个 Python 测试文件中的 `test_*` 函数定义由 **123 降为 108**，不按参数行展开。

- **API 与 Inference**：删掉 API 套件中直接调用同一个 Inference 函数的重复空输入、规范生命周期声明、归一和词表镜像用例。`test_capabilities.py` 仍检查精确词表／警告／生命周期；API 侧保留自己的 schema、注册表公开导出、快照及异常输入。两个快照的「文件存在」和「内容一致」合为同一断言链；在独立进程改指向不存在的路径，两项均明确失败而不是跳过（`missing-snapshots.json`）。
- **同层替代**：删除批次状态键存在性、删除记录器私有别名身份、过时的用户删除私有常量和仅检查审计 SQL 非空的用例。保留批次允许／禁止边、删除意图和回执丢失、身份 owner 事实向量、带筛选条件的列表／计数查询等不同风险；没有据此取消数据库或对象存储验收。
- **协议与 SDK**：拒绝路径断言 `ValidationError` 和有区分力的字段；合并重复默认值与 mask 编码场景。独立进程故意将 `pose` 从 `keypoint` 改为合法的 `bbox`：旧的一致性检查仍通过，新语义断言失败（`geometry-mutant.xml`，不计入通过数）。通知首选项从只看成功响应的 CLI 参数行移到注册入口；分别核对显式 `true`／`false`，未传渠道不会被附加到 PUT 正文。
- **执行**：API 普通选集 81 通过（`api-pure.xml`），另跑 OpenAPI 快照与启动 2 通过（`api-startup.xml`）；便携协议 41、Inference 16、Identity 向量 1、SDK 23 通过（`protocol.xml`、`inference.xml`、`offboarding.xml`、`sdk.xml`），均无跳过。API 初次收集缺少本地依赖与固定 TrackEval 子模块；成功结果使用本树的三个共享包源码和仓库固定子模块，不是独立 wheel 安装证明。未使用数据库、生产存储或真实 GPU。
- **静态门禁**：本轮 Python 文件与 `apps/_shared` 的 Ruff 检查通过。全仓 `pnpm lint:python` 仍在未改文件报 10 条非本轮规则的诊断（`python-lint.log`），不记为全仓通过。

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
