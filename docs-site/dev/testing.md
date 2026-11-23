---
title: 测试指南
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# 测试指南

## 测试金字塔

```text
        ╱╲
       ╱E2E╲      少量、慢、覆盖关键用户路径
      ╱──────╲
     ╱集成测试╲    适量、按 API 端点为单元
    ╱──────────╲
   ╱  单元测试  ╲   大量、快、覆盖纯逻辑
  ╱──────────────╲
```

请按金字塔比例分配测试精力：**不要**为单一纯函数编写 E2E，也**不要**为跨页面工作流只写单元测试。

### 从审查意见到可执行规则

清理用例先问：**哪一种用户会遇到的错误，只有这条测试能发现？** 同一入口、输入边界、失败方式和可观测结果都被更强的用例覆盖，才删除重复项；原断言太弱但风险仍在，就改写。参数相似的权限拒绝与事务回滚不是同一个风险。删测时在治理记录或 PR 写明旧用例、替代用例与实际执行结果，不按删减比例验收。

下面的静态规则随 `pnpm lint` 在本地和 CI 的 `Repository quality` 中执行；也可只运行对应的 Ruff 或 ESLint。不能靠语法判定的规则，在真正消费该行为的入口验证：

- **用例身份与输入**：Web 的 `vitest/no-identical-title` 拦截同一测试组的重复静态标题；Python 的 Ruff `PT014` 拒绝同一 `pytest.mark.parametrize` 中重复的静态用例。标题冲突时先比较失败风险，风险不同就改名，不是机械删掉一个。这两条规则都不猜测跨套件的语义等价。
- **共享 Python 契约的拒绝路径**：`apps/_shared` 启用 Ruff `B017`，拦截用 `pytest.raises(Exception)` 等无约束捕获证明输入被拒绝。用契约规定的异常类型；输入可能因不同字段失败时再核对字段或机器码。lint 不能判断断言选对了业务原因。
- **构建产物与源码分层**：Web ESLint 拒绝产品 E2E、共享 helper 和 fixture 中以字面量或模板前缀直接 `import("/src/…")`。公开 DTO 的类型引用不受影响；需要 Vite 源码入口的真实 Worker 在 `e2e/browser-integration` 验证，不算产品验收。静态检查不证明页面执行过保存、刷新或恢复。
- **断言必须能失败**：Web 单测中的独立 `expect(value);` 不比较任何结果，`test-contracts/no-bare-expect` 会报错。要验选中状态就断言 `toBeChecked()`，而不是仅找到元素。正常链式断言、附说明的断言和异步断言不因此禁止。
- **先识别写入，再判断成败**：产品 Playwright 脚本、fixture 和 helper 使用 `waitForApiWrite(page, method, path)` 按 API 路径、资源 ID 和 HTTP 方法等待。`test-contracts/no-response-status-filter` 拦截内联或同文件具名匹配函数中对响应参数直接调用 `.ok()`／`.status()`；不追踪跨文件调用或任意别名。收到响应后再断言，另行验证持久结果。
- **业务含义不能靠 lint 判定**：按[推理后端协议](./reference/ml-backend-protocol)独立给出 `detect → bbox`、`pose → keypoint` 等期望，不能从被测映射生成答案。`bbox` 和 `keypoint` 都是合法值，把 `pose` 错接到 `bbox` 时，词表一致性检查本应继续通过，任务契约测试则必须失败。
- **读权限与页面分层**：模型市场拆分后，不能用旧卡片或被替换的子面板证明项目管理员能读目录。`ModelMarketReadAccess.test.tsx` 从受限角色的运行时深链进入真实页面，只提供模型目录和协议两个 GET；验证能看到目录、能切换协议，越界请求即失败。它不代替后端鉴权测试。
- **合法端点也可能接错**：[用户管理](../user-guide/superadmin/user-management#批量邀请与分组)中，预览、提交、换链接和发邮件都是合法操作。`management.test.ts` 通过真实 Web 客户端逐步核对 HTTP 方法、目标、解码后的查询和 JSON，而非单查 OpenAPI 路径是否存在。页面另验创建和复制都不发邮件、批量确认不含被拦下的成员、重试不重发成功项。只有服务端测试能证明回滚、令牌失效和授权。
- **测试时序要跨过真正的边界**：`accountScopedDownloads.test.ts` 等到旧账号 CSV 的响应体开始读取后才切换账号，最后放行字节，要求拒绝且不生成下载 URL。在发请求后立刻换账号，无法区分“收到响应头时检查”和“读完响应体后检查”。
- **目标要能选错**：要证明列表操作命中所选对象，放入两个合法但不同的对象，点击非首项；要证明跨页操作命中已选项，让当前页与已选集合不同。断言实际提交的完整 ID 集合和正文，不能只数调用次数。模板创建还要等收到成功响应，检查进入关联数据集步骤；按钮回调或成功提示都不能证明已经进入下一步。
- **数值要能区分错误算法**：指标按「标签 → 数值」检查，测试数据不要让通过数等于退回数。多因素上限取刚好允许的组合，再只改变一个因素使它越界；若去掉批大小后越界样本仍越界，就检不出漏乘。冻结记录与回执共用摘要实现时，从原始清单独立计算期望，不能用一次成功往返自证身份正确。

新增门禁时从一次可复现的错例开始：若能改 API 消除错误选择，优先改 API；否则在已有检查器中让错例失败、相邻的合法例通过，并给出正确入口。涉及运行状态的结论用独立的行为证据，不用搜索源码拼写代替。例如[决策弹窗](./reference/design-system#decision-dialogs)的 lint 能挡住原生弹窗调用，但不能证明用户取消删除后数据还在。

### 源码注释门禁

`pnpm lint:comments` 检查相对 `HEAD` 的工作区变化，包括未忽略的新文件。预提交读取真实暂存区；CI 的 `Repository quality` 对照 PR 目标或 push 前的提交。现行代码的解释性注释额度为零：即使目标分支原来有同样的文字，也不允许带回。

仅冻结迁移和 Web 中确实被 ESLint `no-empty` 消费的既有空 `catch` 说明保留逐文件、逐内容、逐次数的旧额度。删掉后不能在新的基线上补回；新文件不能借用，迁移改名到现行代码也不能继承历史特权。首次遇到还没有门禁的目标提交时使用一次性原始指纹，不维护可扩充的白名单。`pnpm lint:comments --base <提交>` 检查整个分支。

用 `pnpm lint:comments --inventory [目录]` 核算行数；`--jsonl` 列出每条注释的位置和保护原因，`--revision <提交>` 可复查历史快照。`tokens` 包含行尾注释；`comment_only_lines` 只统计拿掉解释性注释后没有代码的物理行。`frozen_lines` 是其中的迁移历史子集，不应通过改写已发布工件兑现。

范围是本仓维护的源码、测试和可执行配置，不包括 Markdown 正文、依赖、生成物、锁文件或外部子模块。解析模板内的相关脚本／样式，以及 GitHub Actions 中可确定为 Bash／sh 的 `run: |` 块；不把普通字符串、heredoc 或 PostgreSQL dollar-quoted 内容当成宿主语言注释。Python docstring 是程序数据；`.env.example` 是环境变量文档输入。许可、shebang、类型／lint 指令、SQL 分句标记等也不作解释性注释删除。不要为新注释伪造指令。

运行仓库技能 `/delete-comment [目录]` 逐条处置：重复或过时的删除；仍存在的权宜实现先登记可验收的 issue；非显而易见的约束归入对应 ADR。自动分类只用于分诊，不能代替删前核对。用 `pnpm lint:comments --test` 验证词法边界、暂存区隔离和删除后不能回填的行为。

### 分层归属与运行入口

| 层级             | 保护目标                                                   | 运行入口                                                                                                                                                                                                                                                         | 真值来源                       |
| ---------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| 后端纯规则       | 授权谓词、作业终态分派、schema 归一等纯函数                | `cd apps/api && uv run pytest <file>`                                                                                                                                                                                                                            | 无需数据库                     |
| 后端集成         | 事务、行锁、成员并发、通知顺序、在线学习控制面             | `pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python -m pytest'`                                                                                                                                                                  | worktree 自有 `yoao_wt_*_test` |
| 训练执行器       | 训练配方执行、Trial 调度、评估、权重回传与脱敏             | `cd apps/training-executor && uv run --with pytest --with pytest-asyncio pytest`                                                                                                                                                                                 | 沙盒临时目录与受控假数据       |
| 站点存储 Agent   | 只读 `openat2` 原语、站点协议契约、本地 TLS 配对与断网恢复 | `PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src apps/api/.venv/bin/python -m pytest apps/_shared/filesystem_read/tests apps/_shared/site_agent_contract/tests apps/storage-agent/tests -p no:cacheprovider` | 临时受控目录与本机 TLS 夹具    |
| 前端纯规则       | URL 编解码、几何换算、工作台与训练状态机                   | `pnpm --filter web test <file>`                                                                                                                                                                                                                                  | 无需后端                       |
| 前端页面集成     | 页面装配与 API 边界契约                                    | `pnpm --filter web test <page-dir>`                                                                                                                                                                                                                              | MSW                            |
| 浏览器 E2E       | 真实画布、渲染、跨页面长链路                               | `pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e`（见下文）                                                                                                                                                                                                | 工作树自有 `e2e` 资源          |
| 在线学习全栈冒烟 | 数据集冻结、训练执行、评估、模型回传与在线推理             | [COCO8 夹具与界面验收](./tutorials/local-dev#_2-准备-coco8-检测夹具)                                                                                                                                                                                             | 真实本地服务栈 + MinIO         |
| SDK / 示例       | 对外契约与文档示例                                         | `packages/python-sdk`、`docs-site/dev/examples/*`                                                                                                                                                                                                                | OpenAPI snapshot               |

当前测试基线的重要事实：

- **ML CPU 与共享契约套件**：平台工作流 `.github/workflows/ml-cpu-test.yml` 在独立虚拟环境运行协议与受控资产套件。官方插件源码测试在插件仓执行；CPU 测试不替代目标设备资格验收。
- **前端页面测试聚焦 MSW 边界**：页面集成测试统一通过 `src/test/renderWithProviders.tsx` 与 `src/test/dataManagerApi.ts` 在 MSW API 边界描述响应；已下沉到 `*UrlState.test.ts` 的纯规则不要在页面测试中重复断言。
- **异步掩码预览按可见完成状态验收**：等待加载状态结束及原图画布尺寸提交，再检查像素与图层切换；`createImageBitmap` 被调用不代表异步解码、记录发布与 React 绘制 effect 已完成。不把 mock 调用次数当作画布就绪，也不扩大等待预算掩盖竞态。

### G0：测试归属与入口清单

本清单根据当前目录、配置与 runner 静态登记，**不是实际 collection 或执行结果**。文件存在不等于被默认入口收集；同一文件也可能由不同矩阵重复选择。变更时以各入口的实际收集结果、跳过原因和报告核对，不用历史测试总数证明完整性。以下路径均相对仓库根目录。

机器可读归属集中在 `scripts/test-suites.json`：ML CPU runner 与 planner 共用 `ml_cpu`，CI owner 矩阵从 `owner_contracts` 动态派生。`node scripts/test-suite-inventory.mjs` 发现包含 Python 测试源码的独立平台测试根并拒绝未登记项；仅剩字节码的旧目录和 `plugins/` 下的独立仓库不属于平台选集。`other_test_roots` 明确其入口、资源与未接入常规 CI 的原因。登记与 `--collect-only` 均不等于执行通过。owner 入口分别上传实际 JUnit，不能合并成虚构的全仓成功计数。

协议文档的 echo／mock-v2、站点 Agent 组合和训练执行器各自产生并上传 JUnit，工件分别为 `echo-example-test-results`、`mock-v2-example-test-results`、`storage-agent-test-results`、`training-executor-test-results`。两个示例用各自的环境，即使前者测试失败也执行后者。训练 CI 校验官方预览权重失败时，任务失败，其余 CPU 用例仍运行并记录结果；缺少执行器依赖而未运行的用例不算通过。

结果消费者同时核对实际 case 记录与统计数，空报告不能凭通过计数变绿。Mask native／readonly 与独立源码 Worker 使用 `E2E_REQUIRE_ALL_TESTS=1`，选集内 skip、not-run、错误、未执行或计数不符均失败；JSON 状态与 Markdown 最终状态保持一致。模块边界验证还核对 collection 的 nodeid 与真实 JUnit，不能用另一个同文件 case 或旧报告冒充当前选择。

模块边界验证在精确销毁物理资源成功后，可能保留原资源根中不可变的
`fixture-original-receipt-*.json` 与已完成的 `fixture-store-owner-*.json` 作为证据；报告记录
准确路径、SHA256 及 `fixture_evidence` 原因。保留这些文件不表示环境仍在运行，也不是清理失败；
没有证据的空资源根则会移除。实际销毁与剩余责任仍以报告中的独立清理结果为准。

| 责任边界／实际测试根                                                                                                                                                                                                                      | 入口与资源                                                                                                                                                                                         | 范围和遗漏边界                                                                                                                                                            |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 平台 API 与跨 owner 消费者：`apps/api/tests`                                                                                                                                                                                              | `cd apps/api && uv run pytest`；集成需要隔离 PostgreSQL、Redis、MinIO，使用下文 worktree test 入口                                                                                                 | `pyproject.toml` 的默认 `testpaths` 只有 `tests`；CI 为该根按文件分四片。Learning／Insights 尚在现有 API 位置，不虚构独立包。纯规则与真实事务／独立进程验收不能互相替代。 |
| 平台 owner：`apps/{identity,workspace,inference,jobs,audit,notifications,labeling,labeling-mask-gc,preannotation-results,media-probe}/tests`、`apps/{assets-catalog,assets-cleanup,assets-source,assets-media,assets-mask-content}/tests` | 每个包在自己的项目环境中显式以 `tests` 为入口；按该套件 fixture 准备数据库、对象存储、受限角色或进程                                                                                               | 不是 API 默认 `testpaths` 的子目录；API 消费者测试不能证明这些独立根已收集。不要把源码相似的 owner 和消费者测试视为重复而删除。                                           |
| 便携契约与运行库：`apps/_shared/{audit_contract,inference_contract,lifecycle_contract,workspace_contract,mail_transport,training_contract,task_contract,protocol_v2,filesystem_read,site_agent_contract}/tests`                           | 各包独立环境；`bash scripts/run-ml-cpu-tests.sh run shared-protocol-v2` 包含 `task_contract` 与 `protocol_v2`，`controlled-assets` 验证平台权重入库工具；其他共享根按 owner 矩阵或下文站点入口执行 | 生命周期证据校验属于平台便携契约；插件运行库和 mask 工具随 Plugin-SDK 独立测试，不恢复旧共享源码。                                                                        |
| 官方模型插件与 Plugin-SDK：独立的 `YOAO-Plugins`、`Plugin-SDK` 仓库                                                                                                                                                                       | 各仓库自身的测试入口、依赖锁和 CI；本地 `plugins/YOAO-plugins/` 是忽略目录，不纳入平台 ML CPU runner                                                                                               | CPU 测试不证明真实权重、GPU 或发布工件资格；vendor 内测试不自动属于项目套件。                                                                                             |
| 平台 API SDK：`packages/python-sdk/tests`                                                                                                                                                                                                 | 从仓库根运行 `python3.11 scripts/test-sdk-distributions.py <新报告目录>`；使用锁定依赖、平台契约包和已校验的插件运行库 wheel                                                                       | 仓内文档／OpenAPI 与仓外安装包分开执行，逐项核对 JUnit；不执行独立 Plugin-SDK 或模型插件的全套测试。                                                                      |
| 执行器与站点 Agent：`apps/training-executor/tests`、`apps/storage-agent/tests`                                                                                                                                                            | 两个入口见下文的执行器和站点 Agent 专节；Agent 与两个共享根组合执行                                                                                                                                | 临时目录、本机 TLS 等组件夹具与跨网络 Docker 拓扑演练分开；后者使用 `scripts/verify_site_agent.py`，不能由组件测试代替。                                                  |
| Web 源码测试：`apps/web/src` 中的 `*.test.ts(x)`                                                                                                                                                                                          | `pnpm --filter web test`；页面集成使用 MSW 和 providers                                                                                                                                            | Vitest/jsdom 不证明真实浏览器 Worker、Canvas 或 GPU。                                                                                                                     |
| Web 浏览器：`apps/web/e2e/tests`                                                                                                                                                                                                          | 工作树 `e2e` 模式中运行 `pnpm test:e2e`；Mask、点云与压力入口见下文；需要浏览器及隔离服务                                                                                                          | 默认 `testMatch` 为 `tests/**/*.spec.ts`，排除 `@stress`，Chromium 排除点云 spec，点云项目单独选择。                                                                      |
| 仓库脚本与文档工具：`scripts/test_*.py`、`scripts/*.test.mjs`、`docs-site/scripts/*.test.mjs`、`docs-site/dev/examples/{echo-ml-backend,mock-v2-backend}/tests`                                                                           | 后端 CI 在 API 环境显式执行 `python -m pytest ../../scripts` 并上传 JUnit；Node 按 workflow 中列出的入口执行；文档 `pnpm --filter docs-site check:all`，示例各自 `uv run --extra test pytest -q`   | API 默认 pytest 不收集仓库 `scripts`；新增接线后的既有失败仍须报告，不用删测或 skip 变绿。文档检查与示例测试各有入口，手动执行不能记作远端 CI 通过。                      |
| 实验：`apps/api/experiments/tests`、`apps/audit-summary/*_test.go`                                                                                                                                                                        | 手动工作流 `.github/workflows/backend-go-experiment.yml`：API 项目下 `uv run pytest -q experiments/tests`；Go 项目下 `go test -race ./...`                                                         | 不在 API 默认 `tests` 根内；手动工作流不是常规 API 分片的通过证据。                                                                                                       |

Web 的包体积和精确帧基准脚本测试仍由 Vitest 执行。文档媒体不属于测试选集；需要演示或界面证据时按[按需截图与录屏](./how-to/update-screenshots)操作真实页面。

浏览器要按**用例**而非仅按 spec 归类：产品 E2E 以真实服务、用户行为和持久结果为证据；浏览器集成可使用受控替身或源码入口；渲染专项验证像素、renderer、资源与设备。Mask native 使用 `playwright.mask-product.config.ts` 及 `vite preview` 验证构建产物。1080p 真实 Worker 完成／取消／失败用例独立位于 `e2e/browser-integration/mask-worker.spec.ts`，其配置只启动 Vite 和静态测试页面，不需要 API、认证或数据库。两者分别生成报告，Worker 的 skip／not-run／空报告不能算通过，也不能计入构建产物黑盒成功。

工作台时间轴布局用 `pnpm --filter web test:e2e:layout video-playback-overlay.spec.ts`。夹具直接挂载真实组件与样式，在 Chromium 桌面亮色、暗色和窄屏项目中测量位置，通过指针拖选并核对提交的帧范围；不启动 API、解码器或数据库。`Frontend verification` 上传 `web-video-timeline-layout`，可与 Vitest 的问题弹窗 HTTP／状态回调报告区分。

已知条件边界须保留在报告中：Mask 用 `@mask-native`／`@mask-readonly` 标签按 `PLAYWRIGHT_RASTER_MASK_MATRIX` 静态选择，默认功能集不选择这两组标签，不把另一开关下的场景混入选集并记作 skip；AI inspector 的真实请求场景需 `PLAYWRIGHT_AI_REQUEST_WORKER=1` 和专用 API/Celery broker；WebCodecs 用例在未强制要求精确解码时可因能力不足跳过。原先只有显式跳过的视频 smoke 已移除，精确视频验收仍走现有专用场景。共享 runtime 的图片测试使用 `pytest.importorskip("PIL.Image")`。这些是源码中的例子，并非全部 skip 清单；缺依赖、collection error、取消或未选择均不可记为通过。

排查收集时，在**各自已准备依赖与安全环境**中分别运行下列只收集入口，并保留完整输出及退出状态；不要在仓库根混跑所有 Python 包，避免同名模块、不同 Python/Torch 版本与 `sys.modules` 桩互相污染：

```bash
cd apps/api && uv run pytest --collect-only -q
# 每个 owner / shared / SDK 项目单独执行，按其项目依赖准备环境：
cd <project-dir> && uv run pytest --collect-only -q tests
cd apps/web && pnpm exec vitest list
cd apps/web && pnpm exec playwright test --list --project=chromium
cd apps/web && pnpm exec playwright test --list --project=pointcloud
```

对照发现的文件、每个入口的 nodeid、条件与实际 CI 路由，记录未收集、重复选择、skip 和 collection error；仅列出即使成功也不证明运行通过。Mask、点云、压力及源码 Worker 必须使用各自配置／环境另行列出；真实性能、GPU、权重和工件验收须记录实际设备、资源及独立结果。

内部重构默认保持 E2E 的业务场景和 expected；仅可见文案变化可以同步相应 selector。删除或替换重复用例须指出相同风险的更强验收位置与实际执行证据，不能只证明 UI 出现了组件。源码 Worker、受控替身与真正产品持久化用例的移动要保留边界，不混算层级。

写入响应遵循上文的「先识别写入，再判断成败」。即使某次故障后会重试成功，也应观察第一次写入的 4xx／5xx，再单独验证重试及最终持久状态；调大超时不能修复错误的匹配器。

夹具创建／清理由 `e2e/helpers/seed-owner-transport.mjs` 在独立 Python 进程调用现有 seed router；夹具准备时仅该进程使用测试 owner 身份，校验启动器提供的规范资源根、identity、资源清单、数据库 owner 标记及匹配的受限 runtime LOGIN。真实浏览器业务请求仍走受限 HTTP API，不恢复私表授权。该夹具通道是进程内 ASGI，不是独立 HTTP 部署资格证据。

Identity／Workspace 原生场景使用 `identity_e2e_support.py` 的组合客户端，只有 `/api/v1/__test/seed/` 请求进入同一个受保护 owner 子进程；其他业务请求保持真实受限连接，失败不会切换到 owner 重试。报告将这些场景标为 owner 夹具加受限业务 HTTP，不再计作受限角色执行整个 seed 流程的资格。独立 Node 合同、私表拒绝和借用事务回滚仍分别验收。

已安装 API 镜像的普通原件消费者显式选择只读原件 profile：保持非 root 用户、受限数据库角色和只读源码夹具挂载，只传入专用原件端点／桶及只读凭据，不传 owner DSN、连接器解密密钥或存储管理凭据。该 profile 使用宿主网络到达已保存的准确端点，不改写存储身份，也不把普通消费者接入专用存储网络。scale 夹具命令使用另一条明确的 owner profile，不能混用两者。

原件存储引导实际检查 HEAD、GET、Range 和写入／删除／管理操作拒绝。`mc admin info --json` 可能以进程退出码零返回 `status: "error"`；管理权拒绝由结构化的 `Access Denied` 结果证明，不能只看退出码。失败的探针版本保留在 owner journal 中，只有核对准确版本及内容、结清物理操作后才能移除。

需要进程内 monkeypatch 的清理故障注入留在 owner 层，并使用仅挂载 seed 路由的独立测试应用；不能给普通业务应用临时切换 owner 凭据，也不能声称父进程的注入影响了子进程。

原生场景的普通 keeper 记录由当前 owner 会话按 ORM 插入事件登记准确 ID；插入回滚撤销登记，清理前回滚未提交事务。载入或更新既有记录不会认领其归属，其他会话新增的记录也不进入清理集合。该登记只覆盖 User、Project 和模型服务实例的 ORM 插入，不跟踪原始 SQL；seed 资源仍由 owner 子进程按既有范围清理。固定 seed namespace 的场景继续独占测试数据库串行运行，不将 keeper 的邻居保护扩大为整套测试并行资格。

跨测试模块复用的 SDK 构造器、客户端替身和 TUI 等待逻辑放在邻近的 `support_*.py`，不要从 `test_*.py` 导入。原生场景也应复用不参与 collection 的场景模块，而不是直接调用另一个 pytest 测试函数；原场景的业务断言和必要夹具依赖必须保留。

评论附件／指南图片的共享对象提供方与构造／上传 helper 位于 `seed_attachment_support.py`，各自的 context 与专属清理保持本地所有权。这两组是使用真实 PostgreSQL／HTTP 和受控对象提供方的 owner 层集成测试，不代表受限业务角色或真实 S3 的资格。

RLE 的共享对象提供方、`Harness`、`harness` 及图像任务／QC seed helper 位于 `raster_mask_support.py`；对象、GC、Annotations 消费者、publication、portability、QC 和 managed mask repair 测试直接导入该支持模块。它们使用真实 PostgreSQL 事务与受控对象提供方，不代表真实 S3 或渲染器资格。

使用默认 Mask 存储入口的消费者另外声明 `consumer_mask_storage`：它把已捕获的默认客户端接到同一受控对象提供方，并使用 `test_db_url` 的 owner 夹具连接；不会修改普通业务运行的凭据。publication 场景验证当前权限、来源变化、真实 NOWAIT 竞争及业务引用的提交／回滚，不再注入已移除的锁或 worker 内部符号。原生 owner 命令绕过 ORM 更新行后，先刷新 ORM 对象再断言持久状态，避免把 identity map 的旧值误判成生产失败。

`raster_mask_isolation.py` 按测试 engine 登记 ORM 对象／publication 插入及原生 `storage_access.acquire` 的单行 Core 许可写入。根事务或 savepoint 回滚撤销相应归属；实际 COMMIT 失败也撤销该次登记，但保留其他独立提交。预存或更新记录、另一 engine 的写入和冲突未插入的 ID 不被认领，调用者的 `RETURNING` 结果不被消费。Core 登记不覆盖批量、upsert 和原始 SQL；不将当前原生单行路径的验证扩大为任意 SQL 支持。

RLE 清理仍拒绝删除带有未归属 publication 的对象，拒绝时整次元数据清理回滚。独立提交后上传失败的分配仍可准确回收；这不是物理对象回收或整套同库并行的资格，既有独立资源与串行约束不变。

托管预测 worker 的 `managed_worker_world` 只登记当前 world 会话创建的 User、Project、Dataset 和模型服务实例。根事务／savepoint 回滚、未提交就关闭会话或实际 COMMIT 失败都会撤销对应归属，已成功提交的早先记录仍可清理；预存并更新的记录和其他会话复用回滚 ID 后提交的记录不被认领。该合同只覆盖 ORM 插入，调用者仍须关闭自己的会话；不代表任意 SQL 跟踪或整套同库并行资格。

托管原件的 `error`、`response`、`Body`、`MemoryS3`、`check_error` 位于 `managed_original_provider_support.py`，`Client`、`stage_client` 位于 `managed_original_client_support.py`。调用者直接导入支持模块，协议替身不依赖可收集的测试模块；历史迁移／创建场景夹具保留原所有权。受控提供方验证精确版本读回、当前版本漂移拒绝、条件写冲突及句柄关闭，不等于真实 S3 验收。

当前托管原件 publication 使用真实 `public` owner 及参与者、项目、素材事实；`current_original_db` 在有建库权限的一次性测试环境中按用例创建、迁移并精确回收数据库。它不复用为历史迁移准备的复制 schema，也不会把普通业务连接切换为 owner。历史迁移夹具继续归其既有消费者，选择迁移时使用确切文件名，避免编号相同但职责不同的迁移被误选。

预测提交与重试场景使用 `ApplyPredictionResultRequest`、持久 execution snapshot、当前重试 ledger 和分帧 dispatcher。任务来源经数据库触发器变更后先刷新再捕获写入范围；受控模型输出仍需通过训练模型类别／来源校验。写入事务中的原件收据不得深拷贝或保存到快照，结果载荷的隔离复制应在附加收据之前完成。worker 夹具通过 Jobs 的精确项目清理能力回收本 world 作业，不恢复旧私表写入或已移除的内部入口。

会提交真实审计事件的预标 API 场景使用独立数据库夹具，审计记录随该库整体退役；不关闭不可变审计触发器，也不删除公共审计以迁就普通 ORM 清理。提交边界测试从公开 `run_preannotation` 进入持久执行流程，不再直调缺少 execution owner 的内部执行函数；完成／部分失败注入放在实际结果 owner 的事务边界，保留 PostgreSQL 锁竞争和候选原子性断言。

提交边界夹具通过真实 owner 固定规范排序的选择值，捕获、准备并封存输入清单；只替换物理字节读写和模型响应，不替换来源权限、成员账本或候选写入证明。SQL 写入失败先独立观察旧候选与两类标注保留，再重放同一准备结果并断言不重新推理。多候选原子性在合法管线输出边界注入，不给单 Task 模型响应伪造重复 Task 记录。

稳定候选 ID／时间及失败 ID 由 Native writer 与提交／重试集成场景覆盖，不用假 Session 回显证明实际持久化。纯编码器测试保留轨迹身份与重映射行为；丢提交确认测试核对实际候选写入次数、提交尝试及回执恢复，不固定内部调用顺序或转发参数。已开始的持久执行应先经正常工作流结束，再由 fixture 精确回收；不能只修改 Job 状态来绕过结果租约或删除守卫。

合并 API 或 SDK 改动后，离线操作目录仍由 `packages/python-sdk/scripts/generate_operation_catalog.py` 从当前快照与真实消费者生成。目录过期时运行生成器并复验 discovery／OpenAPI 契约，不修改测试期望绕过漂移检查。

---

## 后端：pytest (`apps/api`)

### 常用命令

```bash
cd apps/api
uv run pytest                                # 运行全部测试
uv run pytest tests/test_smoke.py -v         # 运行单个测试文件
uv run pytest -k "batch_lifecycle"           # 按关键字过滤
uv run pytest --cov=app --cov-report=html    # 生成 HTML 覆盖率报告（输出至 htmlcov/index.html）
```

### CI 分片运行

CI 的 `Backend tests` 检查背后是 `.github/workflows/backend-test.yml` 的四个独立分片，每个分片在自己的 GitHub runner 上拥有独立的 Postgres / Redis / MinIO。分片只决定 pytest 收集哪些用例；依赖同步、worktree 运行时与 setup 测试、服务维护测试、存储隔离验收、`alembic upgrade head`、迁移校验、OpenAPI 快照漂移、`migration policy` 纯规则测试与覆盖率上传在每个分片上都完整执行，任何分片都不会因为分片而跳过检查。

后端分片固定使用 `ubuntu-24.04`，备份/还原显式选择 PostgreSQL 16 客户端，与本 Job 的 `postgres:16` 服务配套，避免浮动 runner 的新版 `pg_dump` 生成旧服务端不支持的配置。客户端缺失时安装同主版本包，不通过过滤备份语句、忽略 `pg_restore` 错误或跳过恢复场景放行迁移门禁。

原件回收的五个 Native 用例必须各自在空数据库执行，不能用普通 pytest 的条件跳过代替。后端分片 0 运行 `apps/api/.venv/bin/python scripts/verify_module_data_boundary.py --reclamation-only --output reclamation-results/acceptance.json`，核对收集清单与逐项 JUnit，随分片上传结果。HTTP 用例实际登录，并验证只读观察及指定 S3 版本的删除；不借用日常数据库或桶。

使用 `pg_stat_activity` 观测并发锁等待时，每次轮询先调用 `pg_stat_clear_snapshot()`，避免同一观测事务的统计快照遗漏稍后建立的连接。锁序测试仍需验证真实阻塞、取消落库和唯一终态通知，不靠增加等待时间或吞掉异常稳定用例。

真实媒体派生 HTTP 验收直接通过 API authority 请求服务返回的下载 URL，再核对输出字节数、完整 SHA-256 与代际身份。已登记媒体缓存返回 API capability URL，不能把它拼接到 MinIO endpoint；不绕开能力校验直读底层对象。

流式交付的独立数据库会话须注入当前测试的 function-scope 真实 session factory，避免跨 pytest 函数事件循环复用应用级连接池。保留真实数据库事务、存储读取与能力校验，不用模拟响应或扩大超时掩盖隔离问题。

Worker 准备过程中仅按既有接口明确的 busy 错误码等待，不把任意冲突或授权失败当成可重试状态；保持原轮询次数、总体期限与最终输出断言。

- **固定输入**：四个调用方分别传入 `--shard-index 0..3 --shard-count 4`。本地复现任一 CI 分片：
  ```bash
  cd apps/api
  uv run pytest --shard-index 2 --shard-count 4
  ```
- **划分规则**：分片键是 nodeid 中第一个 `::` 之前的测试文件路径（如 `tests/test_foo.py`），分桶为 `int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % count`；同一文件的所有用例落在同一分片，分片内部保持原有收集顺序。不传分片参数时保持原有的完整串行运行，本地与其它入口行为不变。
- **产物**：每个分片上传 `backend-test-results-<index>` JUnit 产物，并以相同 `backend` flag 上传覆盖率，Codecov 按同一 commit 合并。
- **聚合检查**：`ci.yml` 中的 `pytest` / `Backend tests` 是固定名称的聚合 job，四个分片任意一个 `failed`、`cancelled` 或 `skipped` 都会让该检查失败；只有四个真实分片全部 `success` 才会通过。

### 核心 Fixture

`tests/conftest.py` 提供开箱即用的测试夹具：

| Fixture                                                    | 用途                                          |
| ---------------------------------------------------------- | --------------------------------------------- |
| `db_session`                                               | 函数级作用域，基于 SAVEPOINT 隔离的数据库会话 |
| `httpx_client`                                             | 注入了 `db_session` 的异步 ASGI 客户端        |
| `super_admin` / `project_admin` / `annotator` / `reviewer` | 预置 4 种平台/项目角色的账号与 JWT Token      |

**测试库安全护栏**：测试库连接优先读取显式 `TEST_DATABASE_URL`，否则跟随当前环境的迁移连接并固定到 `annotation_test`；解析失败会直接报错，绝不回退到默认连接串。无论来源如何，目标数据库都必须是 PostgreSQL 且库名以 `_test` 结尾（如 worktree 启动器分配的 `yoao_wt_*_test`）；任何指向日常开发库或生产库的配置都会在迁移或写入前被拒绝。

会独立提交持久账目的存储窗口、历史核验及训练准备测试使用 `tests/storage_migration_isolation.py` 的 `migration_engine` 和配套会话／账号夹具：克隆已登记业务表、外键及命名空间内的守卫触发器，不复制共享数据；测试或准备失败后销毁整个私有 schema，不通过清空共享访问账目、恢复旧维护修订或删除未知责任来让测试通过。
原图缓存的真实 Celery 清理验收也使用此私有 schema：测试与独立 Worker 读取同一份隔离账目，Worker 的测试入口只指定真实数据库连接的 `search_path`，仍执行未修改的全局清理任务。发送端与结果观察使用自有 Celery 客户端，不借用全套测试共享的消费者状态；成功以 Redis 已保存的任务回执和真实对象删除为准，不依赖瞬时 Pub/Sub 消息，也不增加任务超时或生产任务参数。

Mask 切割的 GC 验收沿已保存 owner 读取准确字节，不按逻辑 RLE 键猜物理前缀。只在已验证的一次性 mode 中将 GC 观测时刻推进两天（仍在原操作的三十天恢复期内），分别执行退休与删除，复核恢复保护的像素未变、未引用对象已真实移除；不回写不可变所有权事实或缩短恢复期限。

导出回收的批次上限和操作保护在 `test_export_media_retirement.py` 用真实 PostgreSQL 验证：在可回收项之前放入仍有媒体或 ZIP 读写记录的项，以 `limit=1` 运行清理，核对留下的行和实际删除的对象键。保留独立连接的锁竞争、部分删除与重试；不要用测试自己拼出的 `SELECT` 或缓存里的 ORM 对象替代这些证据。对象存储使用受控替身，不是 S3 验收。

视频读取锁窗口用真实事务复现标注写入的 Task → Project 顺序：独立连接先确认读取确实阻塞，再交错项目计数更新，验证读取不会与写入形成反向等待；长期占用的排他锁仍在既有一秒预算内拒绝，不通过放宽锁或增加超时消除失败。

Data Manager 的筛选用例通过 HTTP 核对返回的 ID、总数和排序。在同一任务放入分别满足两个条件的不同标注，能抓到把它们误当成同一对象的查询；跨项目反馈不能改变本项目的筛选和最近活动时间。SQL 关系检查只负责守住已发布的读取边界，不验证 SQL 的字符串写法。

### 编写 API 测试示例

```python
async def test_create_project(httpx_client, project_admin):
    headers = {"Authorization": f"Bearer {project_admin['token']}"}
    res = await httpx_client.post(
        "/api/v1/projects",
        json={"name": "demo", "type_key": "bbox", "classes": ["car"]},
        headers=headers,
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "demo"
```

### OpenAPI 契约测试

每次修改 FastAPI 路由或 Pydantic Schema 后，必须刷新 OpenAPI 快照：

```bash
cd apps/api
uv run python ../../scripts/export_openapi.py
git add openapi.snapshot.json
```

CI 中的 `tests/test_openapi_contract.py` 会校验 `openapi.snapshot.json` 与运行时路由是否一致；前端 `pnpm codegen` 与 Python SDK 契约测试同样以该快照为唯一真值源。

### Python SDK：仓内契约和发行物

在仓库根目录安装 `uv`，按 CI 的 `Stage verified Plugin-SDK artifacts` 步骤将已校验的运行时 wheel 放入 `.artifacts/python/plugin-sdk/`，然后运行：

```bash
python3.11 scripts/test-sdk-distributions.py "$(mktemp -d)/sdk-reports"
```

脚本构建 SDK 与平台维护的五个共享契约包。先从 SDK 源码包安装核心依赖，检查离线发现和未安装 CLI extra 时的错误；再显式安装带测试与插件依赖的 wheel。临时环境必须在仓库外，脚本不从相邻 checkout 或 `PYTHONPATH` 借用实现。

SDK 文档片段、HTTP 调用点、覆盖清单和生成目录与 monorepo 输入一起测试，缺失快照直接失败。其余 SDK 测试以及推理、协议、训练三组便携向量从各自 sdist 取出，对实际安装包执行。两份 SDK JUnit 的用例身份必须合起来恰好覆盖一次仓内收集清单；缺项、重复、跳过或缺少应携带的夹具都会失败。CI 的 `python-sdk-test-results` 保留五份 JUnit、`sdk-collection.txt` 和 `distributions.sha256`，不代表独立插件仓、服务部署或 GPU 资格。

---

## 在线学习与训练执行器测试

在线学习子系统的验证横跨控制面（`apps/api`）、执行面（`apps/training-executor`）与 [YOLO 推理插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/yolo)：

1. **执行器单元测试（无需数据库）**：
   ```bash
   cd apps/training-executor
   uv run --with pytest --with pytest-asyncio pytest
   ```
   覆盖不可变快照校验、Ultralytics / LibreYOLO 适配器、检查点恢复、原生报告与脱敏日志。原生增强预览使用 [Ultralytics 官方检测权重 `yolo11n.pt`](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolo11n.pt)。本地下载后对照[发布资产的 SHA-256](https://api.github.com/repos/ultralytics/assets/releases/assets/340060970) 校验，将 `PREVIEW_TEST_BASE` 设为文件的绝对路径；未设置时读取 `/models/yolo11n.pt`。本地缺文件则跳过，不计作通过。平台的有预算串行搜索由 API 控制面测试覆盖。
2. **控制面 API 与状态机测试**：
   ```bash
   cd apps/api
   uv run pytest -k "training" -v
   ```
   覆盖训练数据集版本冻结、实验任务租约 CAS、心跳续租、尝试级观测指标、受控模型发布（`trained_models`）以及审核驱动的主动学习回流。
3. **真实链路验收**：按[本地开发](./tutorials/local-dev#在线学习与训练执行器联调-可选)准备独立环境、执行器和 COCO8 检测夹具，再通过界面完成训练、回流和人工发布。单元测试、夹具构建成功或模拟 API 页面测试均不能替代真实权重推理验收。

真实 HTTP 交接、认领重放与租约恢复是显式选择的本地验收，不由普通 API 或执行器 CI 测试启动。准备模块边界验证器的一次性 PostgreSQL、MinIO 和 Node peer 前置条件后，在同一工作树仓库根运行：

```bash
UV_PROJECT_ENVIRONMENT="$PWD/.artifacts/training-handoff-venv" \
  uv sync --locked --project apps/training-executor --only-group handoff
AAP_TRAINING_EXECUTOR_HANDOFF_ACCEPTANCE=1 \
  apps/api/.venv/bin/python scripts/verify_module_data_boundary.py \
  --workspace-consumer-profile default \
  --test-file tests/test_training_executor_handoff.py::test_live_executor_claim_replay_competition_cancel_and_finish_replay \
  --output .artifacts/test-results/training-handoff.json
```

API 使用自己的运行环境，交接 peer 使用独立的 `.artifacts/training-handoff-venv` 和当前执行器源码；不继承 API 模块路径或数据库凭据，也不安装不需要的 CUDA 依赖。受控 CPU 子进程验证 HTTP 与租约恢复，不替代训练容器、GPU 或真实权重验收。

---

## 数据集、存储迁移与只读站点 Agent 测试

数据集与受控存储领域的验证分为纯标准库/组件单测、隔离容器拓扑演练与资源基线测量三层：

1. **只读文件原语、站点协议与 Agent 组件单测（无需数据库或 Redis）**：
   ```bash
   PYTHONDONTWRITEBYTECODE=1 \
   PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src \
   apps/api/.venv/bin/python -m pytest --import-mode=importlib \
     apps/_shared/filesystem_read/tests \
     apps/_shared/site_agent_contract/tests \
     apps/storage-agent/tests -p no:cacheprovider
   ```
   覆盖 Linux `openat2` 防符号链接/越界、`0600 VerifiedStage`、一次性本机 TLS 配对、`renameat2(RENAME_NOREPLACE)` 原子落盘、256 KiB 分块限速以及进程重启后沿同一 `session_id` 重放 stage 恢复。
   TLS peer 由本目录 `conftest.py` 按需提供；测试模块只从同包 `support.py` 导入共享 helper，不需要先收集其他测试文件。
2. **跨网络隔离拓扑真实演练（`scripts/verify_site_agent.py`）**：
   在双 Docker bridge 隔离网络（中心无法向站点发起入站连接）中验证端到端链路，支持三种场景（`--scenario basic | recovery | replicas`）：
   ```bash
   apps/api/.venv/bin/python scripts/verify_site_agent.py \
     --state /tmp/aap-site-agent-verify \
     --scenario basic
   ```
   分别验证基础配对/扫描/预检登记/Range 读取（`basic`）、断网与重启恢复及撤销凭据关闭回执（`recovery`），以及主站点离线时显式同内容备用来源（`DatasetReferenceReplica`）的委托读取（`replicas`）。
3. **数据集与存储资源基线测量脚本**：
   在当前工作树专属且业务表为空的可丢弃 `e2e` / `test` 模式下运行，严禁指向非空开发库：
   - `scripts/benchmark_dataset_workbench.py`：[数据集工作台规模测量](./reference/dataset-workbench-benchmark)（1k / 10k 索引分页、组合筛选与项目统计）
   - `scripts/benchmark_dataset_scan.py`：[目录扫描资源基线](./reference/dataset-scan-benchmark)（真实 Worker 元信息扫描、取消与恢复）
   - `scripts/benchmark_dataset_read.py`：[目录原图读取资源基线](./reference/dataset-read-benchmark)（无缓存严格 SHA-256 核验与 HTTP 传输）
   - `scripts/benchmark_dataset_sync.py`：[本地增量同步资源基线](./reference/dataset-sync-benchmark)（Beat 调度、扫描、完整预检与新增登记闭环）
   - `scripts/benchmark_dataset_training.py`：[训练准备与重复传输资源基线](./reference/dataset-training-benchmark)（托管图片预检、物化打包与重复下载读取）

---

## 前端：Vitest + MSW (`apps/web`)

### 常用命令

```bash
cd apps/web
pnpm test                  # 一次性运行 Vitest
pnpm test:watch            # 监听模式
pnpm test:coverage         # 运行并检查覆盖率阈值
```

### MSW 接口模拟

Vitest 已自动挂载 MSW Server，默认处理器位于 `src/mocks/handlers.ts`。未命中处理器的 `/api/vN` 产品请求不会访问真实网络；即使应用捕获了请求错误，发起请求的用例仍会失败，并报告请求方法与路径。非产品路径不触发这项检查（查询参数里出现 `/api/vN` 也不算）。

为用例实际访问的路径和方法添加 `server.use(...)`，不要用宽泛处理器兜底。用例和套件各自持有处理器覆盖；异步操作即使延续到下一用例，也不会使用下一用例的处理器。使用 `vi.stubGlobal` 提供传输替身时，应只在用例内或 `beforeEach` 安装，并核对替身收到的请求。对于只读资源，也可添加对应的非 API MSW 处理器：

```ts
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";

it("渲染项目列表空态文案", async () => {
  server.use(
    http.get("*/api/v1/projects", () =>
      HttpResponse.json({ items: [], total: 0 }),
    ),
  );

  render(<ProjectList />);
  expect(await screen.findByText(/还没有项目/)).toBeInTheDocument();
});
```

### 组件测试边界

- **应当测试**：渲染分支切换、用户交互后的状态演进、与服务端请求/响应契约的对齐。
- **不要测试**：具体颜色类名、像素级静态布局、组件内部私有状态字段名。
- **存储绑定**：候选翻页、预览过期、陈旧响应和重复确认属于同一个对话框，只在 `StorageBindingChangeButton.test.tsx` 验证一次。各用途仍验证自己的历史数据、阻断条件和请求。`StorageBindingRecovery.test.tsx` 用 MSW 分别返回头像、报告附件和审计归档的目标缺失码，核对换目标后的预览与提交；共享的撤权路径用一次 `403` 验证旧预览消失且不能再确认。未处理的请求会让这组测试失败；它不替代服务端鉴权测试。

### 超大图金字塔与瓦片调度测试

纯逻辑与组件单测覆盖 manifest 校验、全分辨率半开视口矩形、DPR/LOD 迟滞切换、overlap 裁切、边缘瓦片、批量预签名签发、异步解码、过期提交丢弃与资源释放：

```bash
pnpm --filter web exec vitest run \
  src/pages/Workbench/stage/imagePyramid.test.ts \
  src/pages/Workbench/stage/imageTileScheduler.test.ts \
  src/pages/Workbench/stage/useWorkbenchImageSource.test.ts \
  src/pages/Workbench/stage/useImageTileScheduler.test.tsx \
  src/pages/Workbench/stage/useImageStageFit.test.ts \
  src/pages/Workbench/stages/image/ImageWorkbench.test.tsx
```

真实 50MP/200MP 浏览器回归使用 `P-LARGE-IMG` / `DS-LARGE-IMG` 开发夹具（参见 [超大图金字塔派生资产](/dev/concepts/image-pyramid-assets#可复现开发夹具)）：

- `required` 大图任务必须断言自动加载路径不发起 `original` 原图请求。
- 快速平移/缩放/切题后，检查诊断快照中的 stale commit、reserved/retained bytes、live bitmap/ObjectURL 与请求数在停止操作后进入平稳期（plateau），离开任务后全部归零。
- 不同长宽比的大图切题回归需额外确认：新图像源不得渲染已释放的旧瓦片，缩放比例按新图尺寸重新 fit，放大后可通过滚轮或缩放浮条回到全图适应比例，且 Minimap 与右下角缩放浮条互不遮挡。

---

## 前端：Playwright E2E

完整说明见 `apps/web/e2e/README.md`。

```bash
docker compose up -d postgres minio
pnpm dev:worktree -- exec --mode e2e -- pnpm test:e2e
```

启动器核验当前工作树独立资源及数据库 owner 标记，为 API、Web 分配保留端口；
Playwright 在该环境内启动受限 API 和 Web。不能仅靠 `PLAYWRIGHT_E2E_DATABASE_URL`
或 `_e2e`／`_test` 后缀证明资源归属，也不要复用日常开发库。

GitHub 的 Web E2E 安装锁定的 API 运行依赖（`uv sync --project apps/api --frozen --no-dev`），不依赖 Python 测试额外使用的私有 SDK wheel。每个 runner 从自己的 Compose 项目启动 `minio`，从同一服务提取 `mc`，结束后回收项目和卷。不要用无 Compose 归属的同名容器替代：夹具必须能核对它将要清理的服务。

- **何时编写 E2E**：跨页面业务流程、长链路状态收敛、涉及 WebSocket 实时推送或真实文件上传。
- **何时不写 E2E**：单组件局部交互、纯函数或 URL 规则校验。

需要复查外观或在真实界面复现问题时，按[按需操作真实界面](./how-to/update-screenshots#按需操作真实界面)只操作相关流程。不将临时证据写进测试选集；CI 不比较截图基线或保存成功路径截图。画布像素采样和布局测量仍作为可观察的行为断言。

### CI 测试分层与套件路由

下表命令描述 CI 套件选择；本地产品 E2E 需将对应命令放在
`pnpm dev:worktree -- exec --mode e2e --` 之后从仓库根目录执行（或在内部切换到
`apps/web`），不要在未核验资源的 shell 中直接执行。

| 测试集         | 套件命令（在 `apps/web` 下）                                                                                       | 执行范围                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| 功能回归       | `pnpm test:e2e --project=chromium --fully-parallel --shard=N/16`                                                   | 依用例均分 16 片；每片单 worker，覆盖图片、视频与布局                |
| Mask 矩阵      | `pnpm test:e2e:mask-readonly` / `test:e2e:mask-native` / `test:e2e:mask-native-editor` / `test:e2e:mask-ai-native` | 按套件计划选择，分别使用独立服务与隔离数据                           |
| 原生 AI 工具栏 | `pnpm test:e2e:mask-toolbar-native`                                                                                | 只执行 `workbench-ai-toolbar-layers.spec.ts` 的 6 个真实原生写入用例 |
| 异步 AI 作业   | `pnpm test:e2e --project=chromium e2e/tests/workbench-ai-inspector-phases.spec.ts`                                 | 只执行 4 个真实异步阶段用例；启动器必须加 `--with-worker`            |
| 点云专项       | `pnpm test:e2e --project=pointcloud --shard=N/4`                                                                   | 4 个分片覆盖全部点云交互与布局；保留专用渲染参数                     |
| 视频专项       | `pnpm test:e2e:video --project=chromium --fully-parallel e2e/tests/video- e2e/tests/workbench-video- --shard=N/4`  | 4 个分片使用构建产物和可写 Mask API，执行单帧与轨迹写入              |
| 布局压力       | `pnpm test:e2e:stress`                                                                                             | 相关变更、全量选集及手动 `extended`；覆盖六种上下文各 54 次重排      |

#### 布局与画布分片策略

- 普通功能配置默认排除 `@stress`；独立压力配置只选择该标签。两者都验证可操作性、状态流转与真实持久化。
- 布局矩阵按画布渲染栈拆分为两个 Playwright 项目：
  - `workbench-pointcloud-layout-matrix.spec.ts`：仅包含 3D 点云场景（运行在 `pointcloud` 项目，启用 SwiftShader 软渲染 WebGL）。
  - `workbench-layout-matrix.spec.ts`：包含图片与视频场景（运行在 `chromium` 项目，Konva 2D 画布无需 WebGL，避免软渲染在高负载下饿死主线程）。
  - 两份 spec 共用 `e2e/helpers/workbench-layout-matrix.ts` 注册用例，保持标题一致以稳定分片与套件选集。

完整功能选集与 `E2E_SELECTION_MODE=legacy` 都要求 16 个 Chromium 分片（`default-01` 至 `default-16`）、4 个点云分片（`pointcloud-1` 至 `pointcloud-4`）以及独立的 4 个视频报告（`video-pipeline-1` 至 `video-pipeline-4`）。Chromium 功能与视频按用例分片，每片仍只有一个 worker。每片须非空；同组分片的 Playwright 用例 ID 必须与未分片清单完全一致、不重复。单进程预算仍为十五分钟。

#### 套件路由与必需审计

- **智能路由入口（`scripts/plan-e2e-suites.mjs`）**：PR 默认运行有界核心 smoke 与变更触发的领域专项（Mask 五组、异步 AI 一组、视频四片、点云四片）。若触及共享依赖、运行时配置、未映射路径、空 diff、跨多域或文件重命名/删除，则保守放大为全量套件；纯文档变更（白名单 `.md`）显式跳过应用 E2E 并记录原因。设置仓库变量 `E2E_SELECTION_MODE=legacy` 可回退至固定功能分片和专项矩阵，仍要求两个独立 AI 专项。
- **规划报告传输**：完整的 legacy/planned 对照、变更路径分类和原因写入 `E2E_SHADOW_PATH`（默认 `e2e-shadow.json`），CLI 仅输出 `shadow_path` 文件指针。CI 从文件生成步骤摘要，并上传 `e2e-selection-report` 产物；不要将完整报告放入步骤输出或进程环境，以免大型 PR 超过进程参数上限。矩阵、必需 manifest 和 ML CPU 调度继续使用原有有界输出。
- **浏览器运行环境**：可复用 E2E 套件使用 `ubuntu-24.04`。默认功能和视频分片、原生 AI 工具栏及异步 AI 作业通过 Playwright `chrome` 通道启动 Google Chrome，并记录浏览器版本：这些用例需要真实解码 H.264 原件。其余套件保留锁文件指定的 Chromium；点云的 SwiftShader 参数不变。软件解码通过并不证明硬件加速。
- **CI 运行身份**：可复用 E2E 工作流通过现有 `dev:worktree exec --mode e2e` 准备专属数据库、受限运行账号、Redis 和存储桶，不以初始 owner 连接直接启动普通 API。迁移及私有夹具 owner 与 API 连接分离，API 子进程清空 owner 选择变量。已构建套件仍验证并使用同一指纹的 `dist`，不以开发服务器替代；这项入口约束不改变套件选集、重试策略或验收要求。
- **共享构建归档校验**：已构建套件只接受 `SHA256SUMS` 中一条有效记录，文件名必须字面等于 `web-e2e-dist-<指纹>.tar.gz`。先计算实际归档的 SHA-256，再从同一个已验证的文件描述符解包；路径前缀、名称后缀、重复记录、内容篡改及归档缺失都会在解包前失败。可用 `node --test scripts/unpack-e2e-build.test.mjs` 验证。
- **视频工具准备**：后端分片与 E2E runner 先复用已有的 `ffmpeg` / `ffprobe`，缺少时才以 `--no-install-recommends` 安装 FFmpeg，避免下载无关驱动及语音模型占满 Job 预算；安装前先把 runner 的 `/etc/apt/apt-mirrors.txt` 指回 HTTPS 的 `archive.ubuntu.com`，避免区域 HTTP 镜像拖慢同一个 `apt-get`；两者版本均写入步骤日志，测试选集和超时不变。
- **真实提供方验收**：版本、删除标记与净变化用例通过共用的 `live_provider_guard` 验证目标：本地仅允许工作树专属 `test` 桶；GitHub 托管 runner 可使用本次 Job 的临时 PostgreSQL、MinIO 与 Redis。远程存储、开发模式和非托管 runner 均拒绝，不用伪造工作树标记或跳过用例代替验收。
- **必需套件审计（`scripts/audit-e2e-requirements.mjs`）**：核心 smoke 以 `--retries=0` 且 `flakyPolicy=forbid` 运行（重试后通过仍会阻断审计）；专项套件保留一次诊断重试并单独标记 flaky。审计脚本按计划 manifest 严格核对每个选中套件的产物，任何缺失、取消、setup 失败、套件名不匹配或核心 flaky 都会令汇总步骤失败（不会把缺失报告当作通过）。
- **全量触发入口**：`push` 主分支与每日北京时间 03:00 的 `E2E extended` 定时工作流（`E2E_SCHEDULE_SCOPE=full`）执行全量套件；手动触发可通过 `scope` / `e2e_scope` 选择 `extended`（仅布局压力）或 `full`。修改分类或调度规则后，可运行 `node --test scripts/plan-e2e-suites.test.mjs` 自检。
- **矩阵并发**：全量矩阵包含 32 个套件 Job、33 份必需报告，最多并行 11 个。`mask-worker` 是 `mask-native` 的独立必需报告，不额外占用 Job；两个 AI 专项各用一个 Job、一份独立报告，失败、跳过或零执行均不能满足严格审计。
- **默认功能分片的条件跳过**：视频写入由 `test:e2e:video` 执行，原生 AI、原生 AI 工具栏及异步 AI 分别由 `mask-ai-native`、`mask-toolbar-native` 和 `ai-request-worker` 执行。以当前清单的用例身份逐项核对专项报告；默认分片中的条件跳过不能替代对应的实际执行。

#### 重试策略与画布落库断言

- **重试与超时护栏**：核心 smoke 不重试，完整功能与布局压力（`playwright.stress.config.ts`）最多重试 1 次，以诊断确定性损坏与高负载主线程饥饿。CI 中每个进程、测试步骤和 Job 分别限时 15 / 20 / 30 分钟，失败时自动保留 trace、截图与 `e2e-results.json`。
- **真实落库与像素级验证**：核心绘制流程必须验证实际提交请求、数据库落库内容与页面刷新恢复；测试辅助接口仅用于构造前置状态，严禁在 UI 保存失败后靠补写后端状态掩盖缺陷。图片与视频 Konva 的隐藏/恢复验收统一使用 `e2e/fixtures/annotation-canvas-pixels.ts`，仅统计媒体背景之上的标注图层像素。
- **审核退回**：先由标注员提交真实标注，再由审核员通过界面退回。在点击确认前监听该任务的真实退回请求，先断言 HTTP 响应，再以审核员身份读取任务并核对退回状态、结构化原因与补充说明；弹框消失不代表写入成功。
- **构建产物验收**：CI 的产物套件使用真实 `vite preview`，在 `apps/web` 下通过 `pnpm build --mode e2e` 构建；本地也须使用所属工作树的 `e2e` 环境。常规生产构建不包含点云交互探针，不能替代 E2E 产物。浏览器侧不得靠动态导入 `/src` 修改应用 store。身份续期应走应用的真实刷新流程；网络故障注入只控制时机，刷新请求、权限重验及旧媒体许可退场仍由真实后端完成。仅限开发服务器的专门验收必须明确限制运行环境，不能混入产物套件。
- **交互夹具的响应形状**：隔离页面交互时也要提供周边读取接口的真实结构。例如 `/projects/{id}/datasets` 返回数组，不能用通用分页对象兜底，否则页面会先进入错误边界，无法验收到目标控件。
- **媒体尺寸夹具**：视频 Mask 的 `[height, width]` 取自当前任务真实 manifest，RLE 计数总和必须覆盖完整源帧；不能沿用历史夹具尺寸或用浏览器视口代替源媒体。
- **原件夹具**：`seed.owned()` 已生成真实 PNG 并登记原件内容身份，图片裁剪与二次推理直接复用，不再改写 `Task.file_path` 或 `DatasetItem.file_path`。共享 fixture 统一清理命名空间，临时裁剪由托管推理输入 owner 清理，不猜测旧物理键。
- **推理输入存储**：真实二次推理要求输入桶启用版本控制。CI 在本 Job 的临时 MinIO 初始化并核验 `import` 桶的 `Versioning=Enabled`；本地验收只能在核验当前工作树 `e2e` 桶的配置与归属后设置，不能改日常环境或放宽服务端版本校验。
- **取消过期运行**：PR 被新提交替代时，昂贵 E2E Job 使用 `!cancelled()` 停止调度与执行，不使用会阻止取消的 `always()`。共享构建失败仍阻止全部产物套件；有界的状态汇总及已有失败证据上传保留取消审计。

### `_test_seed` 路由与 E2E Fixture

E2E 用例通过 `apps/web/e2e/fixtures/seed.ts` 的 owner 进程通道调用夹具专用应用中的 `/api/v1/__test/seed/*` 路由，快速构造测试数据；不要求普通业务 API 获得完整 seed 清理权限：

```ts
import { test, expect } from "../fixtures/seed";

test("正确凭证 → 跳转 dashboard", async ({ page, seed, owned }) => {
  await seed.loginViaUI(page, owned.admin_email, "Test1234");
  await expect(page).toHaveURL(/\/dashboard/);
});

test("注入 token 跳过 UI 登录", async ({ page, seed, owned }) => {
  await seed.injectToken(page, owned.annotator_email);
  await page.goto("/annotate");
});
```

- **夹具通道门禁**：owner 进程先核验启动器的规范资源根、清单和数据库所有权及匹配的受限运行身份。夹具路由默认关闭，仅在非 `production` 进程显式启用且当前数据库为 `_e2e`／`_test` 时可调用；库名后缀本身不授予普通 API 夹具权限。
- **工厂函数约定**：`seed.owned()` 返回私有命名空间内的 `admin` / `annotator` / `reviewer` 邮箱（统一密码 `Test1234`）、项目 ID 与 5 个任务 ID；teardown 精确清理该命名空间。自定义数据使用 `apps/api/tests/factory.py` 中的 `create_user`、`create_project`、`create_task`、`create_batch` 与 `create_membership`（平台身份与项目成员职责显式分离）。
- **图片与原生候选**：图片工作台种子使用真实 64×48 PNG，文件名、对象键、媒体类型及原件摘要对应同一份字节。原生图片候选沿已登记原件完整读回后签发回执，绑定真实内容身份、尺寸及事实摘要，不补造来源证据来绕过接纳检查。
- **训练基线归属**：训练验收种子通过与正式接口相同的分配流程，为新建检测/分割版本登记存储归属；重跑和失败重试保持原回执与保存桶。已有但未登记归属的历史版本不会自动认领，仍需按训练数据集的显式副本核验流程处理。
- **Labeling owner 命令**：E2E seed 写入 Batch、Task、Annotation、视频追踪作业和 TaskDatasetItemLink 时通过固定 owner 命令，视频反馈锚点也遵循同一协议。这些私有命令由已验证的 fixture owner 会话调用，并保留调用方原事务的提交／回滚；普通运行角色不因测试数据库后缀获得权限。
- **专项筛选夹具**：筛选验收使用 `seed.filtering()`（或 `e2e/fixtures/filtering.ts`），它先清理自己的命名空间，再返回类型化 manifest：同对象/跨对象属性、必填条件嵌套组、检测与追踪候选组合、101 项分页、Scene 逻辑轨迹和管理列表数据；需要进入视频工作台的用例传入 `{ videoReady: true }`，在种子阶段通过真实派生 worker 入口完成并核验元数据与分块，不依赖 CI 外部 Celery worker；普通筛选用例不额外生成视频。视频预测中的最小 shape 只验证指标；工作台几何交互通过产品预测导入 API 添加有效的带帧候选。点云 fixture 包含真实 PCD 字节和可解析的相机内外参。若需在本地手动体验并保留筛选数据，可运行 `pnpm dev:worktree -- up --mode e2e --scenario filtering`（参见 [多工作树并行开发环境](./how-to/worktree-environments#手动验收筛选功能)）。
- **Workspace 夹具**：`globalTeardown` 只清理本次 suite 已登记的子 run；规模夹具也使用独立 run 清单。对象收尾前先调用现有项目和 Jobs 退役守卫，已知未退役执行或忙锁返回冲突。未确认的存储写入、活跃读取及失败清理保留原始责任和收据；不能通过扫桶或强删 Jobs 绕过。协议与授权见 [Workspace 夹具边界](/api/guides/workspace-runtime#门控-e2e-夹具)。
- **真实作业与导出**：推理 scope 清单查询使用调用方明确提供的 owner 会话工厂，不回退到全局普通 runtime 连接。新建私有夹具导出由原生 owner 冻结 15 分钟期限，普通导出仍为 168 小时，已有期限不变。普通调度清理不处理这些私有交付；物理清理和删除回执只能由已验证的私有 owner 会话完成，普通角色不能直接写入回执或提前退休输出。
- **同步导入回收**：隔离库只认本次夹具的项目、操作者和作业。在同一事务内核对导入结果与关联该作业的成功审计，并确认没有异步派发，才回收已完成的标注或预测导入。只有终态、只有相近时间的审计，或缺少凭据的旧作业都不能据此删除。
- **离线 Raster 故障**：`pnpm --filter web test:e2e:mask-native` 先在新建的独立存储中准备准确版本的损坏夹具，再启动真实消费者。Playwright 只取得无秘密 manifest 和绑定 suite 的有限 relay；API／私有 owner／Vite 在其进程树之外受监督。产品预览和源码 Worker 的 Vite 都通过私有控制管道等待真实关闭，不以进程组信号或 CLI 的信号退出码代替关闭成功；正常结束的消费者也不再向尚在自行退出的后代发信号。seed 失败前已登记的准确意图仍可进入后端核验后的清理；未排空的请求、子进程或存储责任必须保留，不能因主 `pnpm` 进程退出就删除环境。这是进程配置与能力分离，不是对同 UID 宿主用户或 Docker 管理员的 OS 沙箱。
- **原生 Raster 编辑**：`pnpm --filter web test:e2e:mask-native-editor` 执行 8 个高级编辑用例和 2 个编辑器用例。它与仅执行 11 个 Raster 矩阵用例的 `mask-native` 使用同一个独立写入启动器、E2E 构建产物、真实 Images worker 和归属收据；编辑组不预分配故障夹具，也不执行源码 Worker。两组均不可通过跳过用例来满足审计。
- **AI 专项**：两组均使用 E2E 构建产物和专属工作树的 `e2e` 环境。工具栏验证真实原生 Mask 写入、采纳及像素读回；异步阶段使用 API 和 Celery 的真实任务、终态、重试、取消和持久化。仅异步启动命令设置 `PLAYWRIGHT_AI_REQUEST_WORKER=1 PREANNOTATE_GPU_QUEUE=ml.cpu PREANNOTATE_CPU_QUEUE=ml.cpu`，以便 API 和 worker 继承同一队列配置；由既有 Routing、健康观测、Registry 和协议管理测试提供方。不复用日常开发服务，也不据此声明真实模型质量、GPU 或硬件解码资格。完整运行及独立 `--list` 命令见仓库的 `apps/web/e2e/README.md`。
- **Raster 图像准备**：按 Images owner 返回的源尺寸和同一次冻结的 `ImagePolicy` 选择金字塔或缩略图，不根据旧预览路径推断。缩略图核对自身新鲜度；金字塔核对当前 profile 的 ready 代际和准确尺寸，不能要求它同时生成独立缩略图。任务、作业、源身份与代际检查对两种输出都必须成立；失败保留真实错误和原生责任，不放宽处理器对大图缩略图任务的拒绝。
- **Raster 冷准备与业务预算**：8K 数据、原生图像准备和账号准入在独立的既有 120 秒夹具预算内完成，不占用或增加 30 秒业务断言预算。夹具不提前执行编辑、保存、刷新或重载；这些行为仍由真实页面在原业务期限内验证。准备失败照常使场景失败，未知响应／ACK 与未结算原生责任继续保留。
- **Raster 进程身份**：Vite 必须在实际绑定端口后通过继承的私有管道完成就绪握手，单独 HTTP 200 不构成启动证明。原生／只读矩阵和源码 Worker 由本地 `BrowserServer` 持有准确浏览器进程，不连接外部浏览器；关闭 Promise 即使成功，真实信号退出、非零退出或请求关闭前已退出仍使验收失败。浏览器根进程结果不能取代启动器对其余后代、API 和原生 owner 的排空核验。
- **账号外部引用**：夹具账号仍被非夹具数据引用时，Identity 清理返回 409 并回滚最终数据库删除；先通过数据所属模块删除已确认归属的测试数据，再清理账号。清理不会扩大命名空间或删除无关数据，其他 Identity 清理错误也不会被吞掉。
- **并发与清理兜底**：`/seed/cleanup` 只接受明确的 suite ID。尚未完成并发资格的夹具族仍须串行，不能凭单个 owned 夹具推断整套测试并行安全。强制中断可能跳过 teardown；应保留运行清单并核对真实 owner 状态，绝不能在开发库或共享存储中运行。数据保留断言必须重新查询数据库，不能依赖未过期的 ORM identity map。
- **页面先退场**：测试结束并记录失败证据后，seed 先关闭经 `injectToken` 绑定的准确页面，再清理它构建的命名空间，避免仍在播放的页面持续创建预取；API-only 用例不会为此启动浏览器，测试中显式调用 `cleanupOwned` 不关闭页面。关闭与清理失败都保留为测试失败。
- **封存前的读帧请求**：关页后，夹具最多用 5 秒封住新请求并等待发布。如果已确认同一运行的 worker 正在处理准确的请求，该队列中已接单的剩余帧共用另外 10 秒。worker 提交输出租约和登记尝试之间，只有原执行仍持有该请求的数据库锁才可继续等待。这不是发布回执：每一帧都须有匹配的尝试和可核验的对象版本才能封存。队列消息、进程退出或缓存文件均不足以放行。
- **整套夹具收尾**先核实各子运行已受理的视频请求，再在套件锁内复查子运行集合。新增子运行或仍缺发布回执时保留开放状态，不先封存再重试；原因见 [ADR 0184](./adr/archive/0184-service-boundaries-and-contract-verification)。
- **视频晚准入收尾**：清理先退休加锁时发现的全部派生物归属、提交并释放样本锁，再排空首份快照之后准入的已登记读取。读取可完成最终核验并归还许可；等待预算不增加，超时保留归属与责任，不删除活动或未知操作。
- **清理范围栅栏**：账号、项目的最终范围锁可有界等候本夹具尚在收尾的请求，并恢复原本的锁超时；上传 owner、存储许可及未知操作仍保持即时拒绝，等待超时仍返回冲突并保留原责任。
- **评论附件清理**：运行级清理通过精确夹具项目找到独立上传责任，先退休已完成上传，再沿保存位置删除；所有存储 I/O 完成后才锁定并删除原夹具和已完成责任。未完成上传、活跃或未知收尾许可、保存位置不明、规范键残留和删除失败会明确失败并保留重试锚点。不要篡改 90 天期限或删访问账本来绕过；无法确认上传已结束时，核验归属后销毁整个可丢弃工作树 mode，参见[多工作树环境](./how-to/worktree-environments)。清理不按上传者反向删除其他项目的标注评论。
- **指南图片清理**：运行级清理只解除精确夹具项目的权威引用，并退休失去最后引用的已完成上传；其他项目的共享引用继续有效。该流程不立即删除对象，物理清理由 cleanup 队列后续执行，或在核验归属后销毁整个一次性 mode。仍属于精确夹具项目的 pending 会阻挡清理；原项目已删除且无法证明属于当前 scope 的 pending 保留 owner 与字节，不按 namespace 猜测或阻挡无关夹具。不要按年龄、上传者或 JSON 反向清桶，也不要删除独立责任来绕过未完成写入。

---

## 覆盖率门禁

CI 自动将测试覆盖率上传至 [Codecov](https://codecov.io)，并在 PR 评论中展示增量覆盖率（diff coverage）。

- **硬性阻断阈值**：`codecov.yml` 中后端（target 60%）与前端（target 10%）均设为 `informational: false`；`apps/web/vite.config.ts` 同步开启覆盖率门禁（lines/statements ≥ 10%），`pnpm test:coverage` 低于阈值会以非 0 状态码退出。
- **持续演进目标**：随着管理端弹窗、列表与工作台壳层单测的补充，前端覆盖率阈值将按阶段逐步上调至 ≥ 25%。

---

## Pre-commit 钩子

首次配置本地开发环境时安装钩子：

```bash
uv tool install "pre-commit==4.6.1" && pre-commit install
```

此后每次 `git commit` 会自动执行：

1. **Ruff check + format**：检查并格式化全部第一方 Python 代码（排除 vendor）。
2. **Prettier**：格式化本次提交涉及的受支持文本文件。
3. **ESLint & TypeScript 检查**：对 `apps/web` 执行静态检查与 `tsc --noEmit`。
4. **契约与文档生成物同步**：当 API Schema、配置或共享协议变更时自动刷新 OpenAPI 快照；当快捷键、工作台设置或 API 路由变更时自动刷新对应的 `*.generated.md`。

如果钩子报错，**切勿使用 `--no-verify` 强行跳过**，请先修复原因再提交。CI 除运行常规静态检查外，还会以 `manual` 阶段执行全仓 `pre-commit --all-files`，并由 OpenAPI 契约测试、能力词表测试与 `check:codegen` 只读校验兜底。
