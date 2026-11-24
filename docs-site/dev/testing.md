---
title: 测试指南
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-11-23
---

# 测试指南

选能发现本次回归的最窄入口。纯规则用单测；事务、权限和外部行为用实际消费者的集成测试；跨页面持久化、画布和真实设备用对应的浏览器或专项验收。

<span id="测试金字塔" aria-hidden="true"></span>
<span id="分层归属与运行入口" aria-hidden="true"></span>
<span id="g0-测试归属与入口清单" aria-hidden="true"></span>

## 1. 选测试

| 要证明的行为                       | 首选入口                                                                                                                                                         |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python 纯规则、便携契约或 owner 包 | 在所属 Python 项目中显式运行 `uv run pytest <文件或 nodeid>`。只有确认不使用服务的用例才脱离隔离环境运行。                                                       |
| API 事务、锁、权限、任务或真实存储 | `dev:worktree exec --mode test` 中定向运行 API pytest；独立 LOGIN、镜像或恢复另选模块验证器。                                                                    |
| Web 组件与客户端请求               | `pnpm --filter web test <文件或目录>`。MSW 核对业务请求，不代替后端权限或持久化验证。                                                                            |
| 浏览器业务、真实媒体或渲染         | `dev:worktree exec --mode e2e` 中运行目标 Playwright spec；Mask、点云、精确视频和源码 Worker 按专项入口执行。                                                    |
| ML 协议、SDK 工件或训练与存储      | 选 [ML CPU runner](/dev/reference/ml-backend-protocol#协议验证)或[模块与领域专项](./how-to/run-specialized-tests)，不从平台单测推断独立包、真实 GPU 或发布资格。 |

[测试归属清单](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/scripts/test-suites.json)是 Python 测试根与 CI owner／ML CPU 套件的来源。用 `node scripts/test-suite-inventory.mjs` 查未登记测试根，`bash scripts/run-ml-cpu-tests.sh list` 列 ML CPU 套件。各包分别收集，不在仓库根混跑所有 Python 测试。`--collect-only`、`vitest list`、`playwright test --list` 只证明当前选择，不证明执行通过。

`CI test planning` 按 PR 相对基线的完整差异选择检查。只改 `README.md`、`DEV.md`、路线图或文档目录内的普通 Markdown 时，默认保留文档构建、SDK 文档片段和变更文件检查，不启动应用测试。文档中的可执行示例、站点配置、依赖锁或任何未识别路径都不享受这个豁免。提交到 `dev` 后仍执行完整检查；定时工作流单独验证浏览器套件。

纯测试变更不等于纯文档。已识别的独立组件测试或 pytest 用例由原测试门禁执行，只免浏览器；共享测试助手、夹具、运行时配置和未识别路径不免。改产品页面时跑核心链路和已映射的功能场景。改视频、Mask、点云或工作台布局时选对应专项；共享或无法定位的改动跑全量。

## 2. 隔离并运行

按[独立工作树前置条件](./how-to/worktree-environments#前置条件)准备共享 PostgreSQL、MinIO 和此 checkout 的 API 环境。测试必须使用启动器核验归属的数据库、Redis、桶与角色；库名后缀不能证明归属。需要真实 worker 时显式加 `--with-worker`。不要把 owner DB 凭据交给普通 API 或浏览器。

### 后端：pytest (`apps/api`)

<span id="常用命令" aria-hidden="true"></span>
<span id="ci-分片运行" aria-hidden="true"></span>

```bash
pnpm dev:worktree -- exec --mode test -- \
  bash -lc 'cd apps/api && .venv/bin/python -m pytest -q tests/test_smoke.py'
```

替换为相应测试文件或 `文件::用例`。复验 CI 分片时，从该次 Job 读取 `--shard-index`、`--shard-count` 和其他选项，保留 JUnit。需要独立提交或并发锁时，使用独立连接及可退役测试资源，不能用外层 SAVEPOINT 冒充真实 commit。

`Plugin SDK test dependency` 从 Actions 变量 `YOAO_SDK_MANIFEST` 指定的 URL 读取清单，用 `YOAO_SDK_MANIFEST_SHA256` 验证清单，再验证并分发 `yoao-backend-runtime` wheel。四个 API 分片与 `Python SDK tests` 使用同一次运行的制品。准备失败表示测试未执行，不是用例失败；非纯文档 PR 缺少任何成功分片，`Backend tests` 都不会通过。设置分支保护前，先在产品 PR 确认四份 `backend-test-results-*` 和 `python-sdk-test-results` 包含实际用例结果。

工作树的运行、维护和资源隔离测试只运行一次，与四个 API 分片并行。工作树测试失败不会阻止 API 分片执行或上传 JUnit；在需要应用检查的 PR 上，`Backend tests` 要求工作树测试和四个分片都成功。

原视频播放租约的清理与配额由工作树 HTTP 集成用例验证，不需要 Chromium 或 Vite。它需要 Linux、Node 22、FFmpeg／ffprobe、系统 sRGB 配置文件和工作树的 PostgreSQL、MinIO、Redis。复验任务和数据集条目两个入口：

```bash
node scripts/dev-worktree.mjs exec --mode e2e -- node --test scripts/verify_original_playback_http.mjs
```

这里的 `e2e` 是真实隔离 API、存储和 fixture owner 的运行模式，不是浏览器。两个用例都必须实际完成；工作树 Job 的 `original-playback-http-results.xml` 单独记录结果。浏览器仍验证媒体加载、逐帧绘制和页面离开时释放租约。

pytest 先收集全部 API 测试模块，再按模块分片。如果四份 `pytest-results.xml` 只有相同的 `collection failure`，API 用例并未执行。验证覆盖时应核对每个分片的用例结果，并将收集错误、跳过和执行失败分开统计。

复验四个 API 分片时，保留 CI 的 `--shard-plan tests/ci-shard-plan.json`。计划只移动耗时长的整模块；未列出和新增的模块仍按路径哈希分配。单独运行文件不需要计划，也不会拆开同一模块的用例。

更新计划时，在被测提交上用 `pytest -o addopts= --collect-only -q` 保存逐用例清单，下载同一次完整运行的四份 `pytest-results.xml`。从 `apps/api` 运行 `.venv/bin/python -m tests.plan_shards --help` 查看生成参数；将标准输出交给 `pnpm exec prettier --parser json > tests/ci-shard-plan.json`。比较最慢分片的预计时间，优先选只需移动少数模块的方案。JUnit 耗时不含排队、依赖安装或覆盖率报告生成；缩短整轮 CI 需用下一次实际运行验证。

### 前端：Vitest + MSW (`apps/web`)

<span id="常用命令-1" aria-hidden="true"></span>
<span id="msw-接口模拟" aria-hidden="true"></span>
<span id="组件测试边界" aria-hidden="true"></span>

`pnpm --filter web test <文件或目录>` 定向运行。为真实调用的方法和路径设置 `server.use(...)`；不要用宽泛 MSW 处理器兜底。断言用户能观察到的状态或 HTTP 行为，不检查私有 state、具体样式类或 mock 调用次数。

### 前端：Playwright E2E

```bash
pnpm dev:worktree -- exec --mode e2e -- \
  pnpm --filter web test:e2e e2e/tests/auth.spec.ts
```

按 [E2E README](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/apps/web/e2e/README.md) 选择 spec、项目和专项启动器。独立构建产物验收不用开发服务器或浏览器注入状态代替。验证写入时先按方法、资源 ID 和路径捕获**实际**请求，再断言响应、持久状态和刷新恢复。

CI 只给已选中的浏览器套件排序。选中了 `smoke` 就先运行它，其余套件按 `scripts/e2e-job-durations.json` 中的历史耗时从长到短排队，并发上限为 11。耗时相同的套件保持原顺序；没有历史数据的套件按原顺序排在最后。更新这份数据时，使用所有分片均成功的完整矩阵，记录每个 job 从启动到结束的耗时，包含环境准备。不要用失败后提前终止的耗时。

`test:e2e:mask-native` 和 `test:e2e:mask-native-editor` 自行创建并监督隔离环境，直接从仓库根运行 `pnpm --filter web <脚本>`，不要套在 `dev:worktree exec` 内。

<span id="核心-fixture" aria-hidden="true"></span>
<span id="编写-api-测试示例" aria-hidden="true"></span>
<span id="test-seed-路由与-e2e-fixture" aria-hidden="true"></span>

### 夹具与清理

API 的 SAVEPOINT、ASGI 客户端和角色夹具由 `apps/api/tests/conftest.py` 提供。完整 E2E 数据只由独立 owner 通道准备和清理，真实业务仍走受限 API。契约与权限见 [Identity 夹具](/api/guides/identity-operator-data#门控-e2e-夹具)及 [Workspace 夹具](/api/guides/workspace-runtime#门控-e2e-夹具)。

条件写入、训练写入、导出媒体传输，以及对象和键删除的记账用例，需要从另一条连接读取已提交的流水，不能用同一事务内的 SAVEPOINT 代替。每个模块复用一个已迁移的私有数据库；每条用例使用独立连接工厂，结束时只清除自己创建的 `storage_usage_mutations` 行。删除用例仍在独立桶中验证真实 MinIO 版本和删除标记。用例失败、仍有连接或发现未登记的流水时，下一条用例使用新库。其他会写入持久数据的模块不能直接套用这个夹具。

`test_managed_mask_repair.py` 的请求和 worker 从不同连接提交。模块只复用迁移到 `0267` 的 PostgreSQL 模板库。该修订还没有按数据库身份生成的权限数据。每条用例从模板创建私有库，在新库里执行剩余迁移，并单独创建、验证和回收 Jobs 发布登录角色。不要把迁移到 `head` 或已经运行过用例的数据库当作模板。

夹具按本次运行已登记的资源与对象版本清理。忙锁、待写入和未知归属应保留责任及回执，不能扫桶、跳过清理或强删作业。运行级夹具清理失败与工作树资源销毁是不同结果。具体夹具实现以拥有它的测试和 runner 为准；旧案例与治理经过见[历史记录](https://github.com/YOAO-Labs/ai-annotation-platform/blob/5f2da55125d9ce647e07ee5351b5f6ee71153a7f/docs-site/dev/testing.md?plain=1)。

<span id="module-boundary-acceptance" aria-hidden="true"></span>

### 模块边界定向验收

用[模块验证器及报告说明](./how-to/run-specialized-tests#模块边界定向验收)选择真实受限角色、独立镜像、恢复或受保护的原件存储。不要用迁移账号或受控替身证明普通角色或真实存储可用。

- <span id="python-sdk-仓内契约和发行物" aria-hidden="true"></span>[Python SDK 仓内与发行物](./how-to/run-specialized-tests#python-sdk-仓内契约和发行物)
- <span id="在线学习与训练执行器测试" aria-hidden="true"></span>[在线学习与训练执行器](./how-to/run-specialized-tests#在线学习与训练执行器测试)
- <span id="本地执行器交接与负载观测" aria-hidden="true"></span>[本地执行器交接](./how-to/run-specialized-tests#本地执行器交接与负载观测)
- <span id="数据集、存储迁移与只读站点-agent-测试" aria-hidden="true"></span>[存储与站点 Agent](./how-to/run-specialized-tests#数据集、存储迁移与只读站点-agent-测试)
- <span id="超大图金字塔与瓦片调度测试" aria-hidden="true"></span>[超大图金字塔与瓦片](./how-to/run-specialized-tests#超大图金字塔与瓦片调度测试)

## 3. 判读结果

<span id="ci-测试分层与套件路由" aria-hidden="true"></span>
<span id="布局与画布分片策略" aria-hidden="true"></span>
<span id="套件路由与必需审计" aria-hidden="true"></span>
<span id="重试策略与画布落库断言" aria-hidden="true"></span>
<span id="覆盖率门禁" aria-hidden="true"></span>

- 对照实际 collection、逐用例报告和退出码。未收集、跳过、失败、空报告和资源未清理要分别记录；测试计划不能当成绩单。同一用例被多套件选中也不应重复计入。
- PR 的浏览器选集读 `e2e-selection-report`。用 `gates.effective` 对照必需报告，从 `gates.planned`、`gates.rollback` 和逐套件原因解释差异。`scripts/audit-e2e-requirements.mjs` 核对必需报告；核心用例重试转绿不能证明核心通过。区分浏览器 Job 的累计运行时间、首次开始到最后结束的窗口，以及整轮 CI 的墙钟耗时。
- 区分真 PostgreSQL、受控对象替身、真实 S3、独立进程和实际浏览器／GPU。狭窄验收不能证明整模块、真实权重、生产容量或日常切换。

### 从审查意见到可执行规则

新增或删减用例先说明它能发现哪一种用户可见故障。同入口、输入边界、失败方式和结果都已有更强证据时才删。用相邻合法例与真实错例验证新门禁。对授权、回滚和持久化选择真实拥有该行为的消费者；期望值独立于被测代码，异步测试等待真实保存、发布或绘制完成。

`pnpm lint` 执行仓库现有静态门禁，包括重复参数／标题、无断言的 `expect`、产品 E2E 导入 `/src/` 和等待写入时用成功状态过滤。静态门禁不代替业务断言。

### OpenAPI 契约测试

公开路由或 Pydantic Schema 变更后用 `pnpm openapi:export` 刷新快照，`pnpm openapi:check` 检查漂移，并运行 `pnpm codegen`。`tests/test_openapi_contract.py` 核对快照和实际路由，Web 类型和 SDK 都消费该快照。详见 [OpenAPI Codegen](./concepts/api-schema-boundary#openapi-codegen)。

### 源码注释门禁

`pnpm lint:comments` 检查工作树；`--base <提交>` 检查分支；`--inventory [目录] --jsonl` 查看位置与保护原因。已有冻结迁移和必要 lint 指令按门禁规则处理，不把解释性注释加入现行源码。规则自身用 `pnpm lint:comments --test` 验收。

### Pre-commit 钩子

安装与维护入口见[开发指南](https://github.com/YOAO-Labs/ai-annotation-platform/blob/dev/DEV.md)。钩子失败时修复实际原因，不跳过失败门禁。
