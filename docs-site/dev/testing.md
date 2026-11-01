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

### 分层归属与运行入口

| 层级             | 保护目标                                                   | 运行入口                                                                                                                                                                                                                                                         | 真值来源                      |
| ---------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| 后端纯规则       | 授权谓词、作业终态分派、schema 归一等纯函数                | `cd apps/api && uv run pytest <file>`                                                                                                                                                                                                                            | 无需数据库                    |
| 后端集成         | 事务、行锁、成员并发、通知顺序、在线学习控制面             | `pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python -m pytest'`                                                                                                                                                                  | worktree 自有 `aap_wt_*_test` |
| 训练执行器       | 训练配方执行、Trial 调度、评估、权重回传与脱敏             | `cd apps/training-executor && uv run pytest`                                                                                                                                                                                                                     | 沙盒临时目录与受控假数据      |
| 站点存储 Agent   | 只读 `openat2` 原语、站点协议契约、本地 TLS 配对与断网恢复 | `PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src apps/api/.venv/bin/python -m pytest apps/_shared/filesystem_read/tests apps/_shared/site_agent_contract/tests apps/storage-agent/tests -p no:cacheprovider` | 临时受控目录与本机 TLS 夹具   |
| 前端纯规则       | URL 编解码、几何换算、工作台与训练状态机                   | `pnpm --filter web test <file>`                                                                                                                                                                                                                                  | 无需后端                      |
| 前端页面集成     | 页面装配与 API 边界契约                                    | `pnpm --filter web test <page-dir>`                                                                                                                                                                                                                              | MSW                           |
| 浏览器 E2E       | 真实画布、渲染、跨页面长链路                               | `pnpm test:e2e`（见下文）                                                                                                                                                                                                                                        | 隔离服务与 `annotation_e2e`   |
| 在线学习全栈冒烟 | 数据集冻结、训练执行、评估、模型回传与在线推理             | [COCO8 夹具与界面验收](./tutorials/local-dev#_2-准备-coco8-检测夹具)                                                                                                                                                                                             | 真实本地服务栈 + MinIO        |
| SDK / 示例       | 对外契约与文档示例                                         | `packages/python-sdk`、`docs-site/dev/examples/*`                                                                                                                                                                                                                | OpenAPI snapshot              |

当前测试基线的重要事实：

- **ML CPU 与共享契约套件**：平台工作流 `.github/workflows/ml-cpu-test.yml` 在独立虚拟环境运行协议与受控资产套件。官方插件源码测试在插件仓执行；CPU 测试不替代目标设备资格验收。
- **前端页面测试聚焦 MSW 边界**：页面集成测试统一通过 `src/test/renderWithProviders.tsx` 与 `src/test/dataManagerApi.ts` 在 MSW API 边界描述响应；已下沉到 `*UrlState.test.ts` 的纯规则不要在页面测试中重复断言。
- **异步掩码预览按可见完成状态验收**：等待加载状态结束及原图画布尺寸提交，再检查像素与图层切换；`createImageBitmap` 被调用不代表异步解码、记录发布与 React 绘制 effect 已完成。不把 mock 调用次数当作画布就绪，也不扩大等待预算掩盖竞态。

### G0：测试归属与入口清单

本清单根据当前目录、配置与 runner 静态登记，**不是实际 collection 或执行结果**。文件存在不等于被默认入口收集；同一文件也可能由不同矩阵重复选择。变更时以各入口的实际收集结果、跳过原因和报告核对，不用历史测试总数证明完整性。以下路径均相对仓库根目录。

机器可读归属集中在 `scripts/test-suites.json`：ML CPU runner 与 planner 共用 `ml_cpu`，CI owner 矩阵从 `owner_contracts` 动态派生。`node scripts/test-suite-inventory.mjs` 发现包含 Python 测试源码的独立平台测试根并拒绝未登记项；仅剩字节码的旧目录和 `plugins/` 下的独立仓库不属于平台选集。`other_test_roots` 明确其入口、资源与未接入常规 CI 的原因。登记与 `--collect-only` 均不等于执行通过。owner 入口分别上传实际 JUnit，不能合并成虚构的全仓成功计数。

结果消费者同时核对实际 case 记录与统计数，空报告不能凭通过计数变绿。Mask native／readonly 与独立源码 Worker 使用 `E2E_REQUIRE_ALL_TESTS=1`，选集内 skip、not-run、错误、未执行或计数不符均失败；JSON 状态与 Markdown 最终状态保持一致。模块边界验证还核对 collection 的 nodeid 与真实 JUnit，不能用另一个同文件 case 或旧报告冒充当前选择。

| 责任边界／实际测试根                                                                                                                                                                                                                      | 入口与资源                                                                                                                                                                                         | 范围和遗漏边界                                                                                                                                                                               |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 平台 API 与跨 owner 消费者：`apps/api/tests`                                                                                                                                                                                              | `cd apps/api && uv run pytest`；集成需要隔离 PostgreSQL、Redis、MinIO，使用下文 worktree test 入口                                                                                                 | `pyproject.toml` 的默认 `testpaths` 只有 `tests`；CI 为该根按文件分四片。Learning／Insights 尚在现有 API 位置，不虚构独立包。纯规则与真实事务／独立进程验收不能互相替代。                    |
| 平台 owner：`apps/{identity,workspace,inference,jobs,audit,notifications,labeling,labeling-mask-gc,preannotation-results,media-probe}/tests`、`apps/{assets-catalog,assets-cleanup,assets-source,assets-media,assets-mask-content}/tests` | 每个包在自己的项目环境中显式以 `tests` 为入口；按该套件 fixture 准备数据库、对象存储、受限角色或进程                                                                                               | 不是 API 默认 `testpaths` 的子目录；API 消费者测试不能证明这些独立根已收集。不要把源码相似的 owner 和消费者测试视为重复而删除。                                                              |
| 便携契约与运行库：`apps/_shared/{audit_contract,inference_contract,lifecycle_contract,workspace_contract,mail_transport,training_contract,task_contract,protocol_v2,filesystem_read,site_agent_contract}/tests`                           | 各包独立环境；`bash scripts/run-ml-cpu-tests.sh run shared-protocol-v2` 包含 `task_contract` 与 `protocol_v2`，`controlled-assets` 验证平台权重入库工具；其他共享根按 owner 矩阵或下文站点入口执行 | 生命周期证据校验属于平台便携契约；插件运行库和 mask 工具随 Plugin-SDK 独立测试，不恢复旧共享源码。                                                                                           |
| 官方模型插件与 Plugin-SDK：独立的 `YOAO-Plugins`、`Plugin-SDK` 仓库                                                                                                                                                                       | 各仓库自身的测试入口、依赖锁和 CI；本地 `plugins/YOAO-plugins/` 是忽略目录，不纳入平台 ML CPU runner                                                                                               | CPU 测试不证明真实权重、GPU 或发布工件资格；vendor 内测试不自动属于项目套件。                                                                                                                |
| 平台 API SDK：`packages/python-sdk/tests`                                                                                                                                                                                                 | 在包目录执行 CI 中的 SDK 命令；共享契约由 `tool.uv.sources` 解析，插件部署夹具使用 `apps/_shared/requirements-plugin-runtime.txt` 固定的外部运行库及显式协议依赖                                   | API SDK 和 Plugin-SDK 的测试、发布边界独立；消费插件部署契约不等于执行插件 SDK 或模型插件的全套测试。                                                                                        |
| 执行器与站点 Agent：`apps/training-executor/tests`、`apps/storage-agent/tests`                                                                                                                                                            | 执行器 `cd apps/training-executor && uv run pytest`；Agent 和两个共享根的组合命令见下文                                                                                                            | 临时目录、本机 TLS 等组件夹具与跨网络 Docker 拓扑演练分开；后者使用 `scripts/verify_site_agent.py`，不能由组件测试代替。                                                                     |
| Web 源码测试：`apps/web/src` 中的 `*.test.ts(x)`                                                                                                                                                                                          | `pnpm --filter web test`；页面集成使用 MSW 和 providers                                                                                                                                            | Vitest/jsdom 不证明真实浏览器 Worker、Canvas 或 GPU。                                                                                                                                        |
| Web 浏览器：`apps/web/e2e/tests`                                                                                                                                                                                                          | `pnpm test:e2e`；Mask、点云、视觉与压力入口见下文；需要浏览器及隔离服务                                                                                                                            | 默认 `testMatch` 为 `tests/**/*.spec.ts`，排除 `@visual`／`@stress`，Chromium 排除点云 spec，点云项目单独选择。截图采集在 `apps/web/e2e/screenshots`，使用独立截图配置，不属于默认产品选集。 |
| 仓库脚本与文档工具：`scripts/test_*.py`、`scripts/*.test.mjs`、`docs-site/scripts/*.test.mjs`、`docs-site/dev/examples/{echo-ml-backend,mock-v2-backend}/tests`                                                                           | 后端 CI 在 API 环境显式执行 `python -m pytest ../../scripts` 并上传 JUnit；Node 按 workflow 中列出的入口执行；文档 `pnpm --filter docs-site check:all`，示例各自 `uv run --extra test pytest -q`   | API 默认 pytest 不收集仓库 `scripts`；新增接线后的既有失败仍须报告，不用删测或 skip 变绿。文档检查与示例测试各有入口，手动执行不能记作远端 CI 通过。                                         |
| 实验：`apps/api/experiments/tests`、`apps/audit-summary/*_test.go`                                                                                                                                                                        | 手动工作流 `.github/workflows/backend-go-experiment.yml`：API 项目下 `uv run pytest -q experiments/tests`；Go 项目下 `go test -race ./...`                                                         | 不在 API 默认 `tests` 根内；手动工作流不是常规 API 分片的通过证据。                                                                                                                          |

浏览器要按**用例**而非仅按 spec 归类：产品 E2E 以真实服务、用户行为和持久结果为证据；浏览器集成可使用受控替身或源码入口；视觉／渲染资格另负责像素、renderer、资源与设备。Mask native 使用 `playwright.mask-product.config.ts` 及 `vite preview` 验证构建产物。1080p 真实 Worker 完成／取消／失败用例独立位于 `e2e/browser-integration/mask-worker.spec.ts`，其配置只启动 Vite 和静态测试页面，不需要 API、认证或数据库。两者分别生成报告，Worker 的 skip／not-run／空报告不能算通过，也不能计入构建产物黑盒成功。

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

对照发现的文件、每个入口的 nodeid、条件与实际 CI 路由，记录未收集、重复选择、skip 和 collection error；仅列出即使成功也不证明运行通过。Mask、视觉、压力及源码 Worker 必须使用各自配置／环境另行列出；真实性能、GPU、权重和工件验收须记录实际设备、资源及独立结果。

内部重构默认保持 E2E 的业务场景和 expected；仅可见文案变化可以同步相应 selector。删除或替换重复用例须指出相同风险的更强验收位置与实际执行证据，不能只证明 UI 出现了组件。源码 Worker、受控替身与真正产品持久化用例的移动要保留边界，不混算层级。

写入响应按端点与方法匹配，随后独立断言成功状态和持久结果；不要在等待条件中先过滤掉错误响应，使真实的 4xx／5xx 只表现成超时。

夹具创建／清理由 `e2e/helpers/seed-owner-transport.ts` 在独立 Python 进程调用现有 seed router；夹具准备时仅该进程以测试 owner 身份连接数据库，并校验与 runtime 指向同一个显式 `_test`／`_e2e` 数据库。真实浏览器业务请求仍走受限 HTTP API，不恢复私表授权。该夹具通道是进程内 ASGI，不是独立 HTTP 部署资格证据。截图配置使用明确的 `SCREENSHOT_DATABASE_URL` 为夹具选库，不能指向日常开发或生产数据库。

Identity／Workspace 原生场景使用 `identity_e2e_support.py` 的组合客户端，只有 `/api/v1/__test/seed/` 请求进入同一个受保护 owner 子进程；其他业务请求保持真实受限连接，失败不会切换到 owner 重试。报告将这些场景标为 owner 夹具加受限业务 HTTP，不再计作受限角色执行整个 seed 流程的资格。独立 Node 合同、私表拒绝和借用事务回滚仍分别验收。

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

**测试库安全护栏**：测试库连接优先读取显式 `TEST_DATABASE_URL`，否则跟随当前环境的迁移连接并固定到 `annotation_test`；解析失败会直接报错，绝不回退到默认连接串。无论来源如何，目标数据库都必须是 PostgreSQL 且库名以 `_test` 结尾（如 worktree 启动器分配的 `aap_wt_*_test`）；任何指向日常开发库或生产库的配置都会在迁移或写入前被拒绝。

会独立提交持久账目的存储窗口、历史核验及训练准备测试使用 `tests/storage_migration_isolation.py` 的 `migration_engine` 和配套会话／账号夹具：克隆已登记业务表、外键及命名空间内的守卫触发器，不复制共享数据；测试或准备失败后销毁整个私有 schema，不通过清空共享访问账目、恢复旧维护修订或删除未知责任来让测试通过。
原图缓存的真实 Celery 清理验收也使用此私有 schema：测试与独立 Worker 读取同一份隔离账目，Worker 的测试入口只指定真实数据库连接的 `search_path`，仍执行未修改的全局清理任务。发送端与结果观察使用自有 Celery 客户端，不借用全套测试共享的消费者状态；成功以 Redis 已保存的任务回执和真实对象删除为准，不依赖瞬时 Pub/Sub 消息，也不增加任务超时或生产任务参数。

Mask 切割的 GC 验收沿已保存 owner 读取准确字节，不按逻辑 RLE 键猜物理前缀。只在已验证的一次性 mode 中将 GC 观测时刻推进两天（仍在原操作的三十天恢复期内），分别执行退休与删除，复核恢复保护的像素未变、未引用对象已真实移除；不回写不可变所有权事实或缩短恢复期限。

视频读取锁窗口用真实事务复现标注写入的 Task → Project 顺序：独立连接先确认读取确实阻塞，再交错项目计数更新，验证读取不会与写入形成反向等待；长期占用的排他锁仍在既有一秒预算内拒绝，不通过放宽锁或增加超时消除失败。

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

---

## 在线学习与训练执行器测试

在线学习子系统的验证横跨控制面（`apps/api`）、执行面（`apps/training-executor`）与 [YOLO 推理插件](https://github.com/YOAO-Labs/YOAO-Plugins/tree/main/plugins/inference/yolo)：

1. **执行器单元测试（无需数据库）**：
   ```bash
   cd apps/training-executor
   uv run pytest
   ```
   覆盖不可变快照校验、Ultralytics / LibreYOLO 适配器、增强预览、检查点恢复、原生报告与脱敏日志。平台的有预算串行搜索由 API 控制面测试覆盖。
2. **控制面 API 与状态机测试**：
   ```bash
   cd apps/api
   uv run pytest -k "training" -v
   ```
   覆盖训练数据集版本冻结、实验任务租约 CAS、心跳续租、尝试级观测指标、受控模型发布（`trained_models`）以及审核驱动的主动学习回流。
3. **真实链路验收**：按[本地开发](./tutorials/local-dev#在线学习与训练执行器联调-可选)准备独立环境、执行器和 COCO8 检测夹具，再通过界面完成训练、回流和人工发布。单元测试、夹具构建成功或模拟 API 页面测试均不能替代真实权重推理验收。

---

## 数据集、存储迁移与只读站点 Agent 测试

数据集与受控存储领域的验证分为纯标准库/组件单测、隔离容器拓扑演练与资源基线测量三层：

1. **只读文件原语、站点协议与 Agent 组件单测（无需数据库或 Redis）**：
   ```bash
   PYTHONDONTWRITEBYTECODE=1 \
   PYTHONPATH=apps/storage-agent:apps/_shared/filesystem_read/src:apps/_shared/site_agent_contract/src \
   apps/api/.venv/bin/python -m pytest \
     apps/_shared/filesystem_read/tests \
     apps/_shared/site_agent_contract/tests \
     apps/storage-agent/tests -p no:cacheprovider
   ```
   覆盖 Linux `openat2` 防符号链接/越界、`0600 VerifiedStage`、一次性本机 TLS 配对、`renameat2(RENAME_NOREPLACE)` 原子落盘、256 KiB 分块限速以及进程重启后沿同一 `session_id` 重放 stage 恢复。
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
pnpm test                  # 一次性运行全部单测
pnpm test:watch            # 监听模式
pnpm test:coverage         # 运行并检查覆盖率阈值
```

### MSW 接口模拟

`vitest.setup.ts` 已自动挂载 MSW Server，默认处理器位于 `src/mocks/handlers.ts`。在单测中可按需覆盖特定端点：

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
docker compose up -d postgres redis minio
pnpm test:e2e
```

Playwright 会自动准备专用逻辑数据库 `annotation_e2e`、执行迁移，并拉起独立的 Web（`127.0.0.1:3001`）与 API（`127.0.0.1:8010`）进程。它绝不复用开发环境的 `3000/8000` 端口；若专用端口被占用会直接报错退出。如需自定义隔离库，可设置 `PLAYWRIGHT_E2E_DATABASE_URL`（库名必须以 `_e2e` 或 `_test` 结尾）。

- **何时编写 E2E**：跨页面业务流程、长链路状态收敛、涉及 WebSocket 实时推送或真实文件上传。
- **何时不写 E2E**：单组件局部交互、纯函数或 URL 规则校验。

### CI 测试分层与套件路由

| 测试集    | 命令（在 `apps/web` 下）                                                           | 执行范围                                                         |
| --------- | ---------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| 功能回归  | `pnpm test:e2e --project=chromium --shard=N/4`                                     | 完整选集以四个分片执行图片、视频及其布局上下文                   |
| Mask 矩阵 | `pnpm test:e2e:mask-readonly` / `test:e2e:mask-native` / `test:e2e:mask-ai-native` | 每个 PR，分别使用独立服务与隔离数据                              |
| 点云专项  | `pnpm test:e2e --project=pointcloud`                                               | 完整选集单独执行全部 3D 点云与布局上下文，不再与四个普通分片重复 |
| 视觉基线  | `pnpm test:e2e:visual`                                                             | 应用与共享依赖相关 PR、主分支、夜间定时任务和手动触发            |
| 布局压力  | `pnpm test:e2e:stress`                                                             | 同上触发范围；覆盖六种上下文各 54 次重排                         |

#### 布局与画布分片策略

- 普通功能配置默认排除 `@visual` 与 `@stress` 标签。视觉测试基于截图基线与容差比对（预期外观变更需审阅后更新基线）；功能测试则验证可操作性、状态流转与真实落库。
- 布局矩阵按画布渲染栈拆分为两个 Playwright 项目：
  - `workbench-pointcloud-layout-matrix.spec.ts`：仅包含 3D 点云场景（运行在 `pointcloud` 项目，启用 SwiftShader 软渲染 WebGL）。
  - `workbench-layout-matrix.spec.ts`：包含图片与视频场景（运行在 `chromium` 项目，Konva 2D 画布无需 WebGL，避免软渲染在高负载下饿死主线程）。
  - 两份 spec 共用 `e2e/helpers/workbench-layout-matrix.ts` 注册用例，保持标题一致以稳定分片与套件选集。

完整功能选集，以及 `E2E_SELECTION_MODE=legacy` 模式的 PR 与主分支推送，都要求四个 Chromium 分片与点云专项；两者合并覆盖原有全部 Playwright 项目，不省略用例，也不增加十五分钟进程预算。按真实 Playwright 收集集合核对覆盖等价，不用手工维护的测试数代替。

#### 套件路由与必需审计

- **智能路由入口（`scripts/plan-e2e-suites.mjs`）**：PR 默认运行“有界核心 smoke + 按变更路径触发的领域专项（Mask×3、`video-pipeline`、`pointcloud`）”。若触及共享依赖、运行时配置、未映射路径、空 diff、跨多域或文件重命名/删除，则保守放大为全量套件；纯文档变更（白名单 `.md`）显式跳过应用 E2E 并记录原因。设置仓库变量 `E2E_SELECTION_MODE=legacy` 可随时无代码回退至固定全量矩阵。
- **规划报告传输**：完整的 legacy/planned 对照、变更路径分类和原因写入 `E2E_SHADOW_PATH`（默认 `e2e-shadow.json`），CLI 仅输出 `shadow_path` 文件指针。CI 从文件生成步骤摘要，并上传 `e2e-selection-report` 产物；不要将完整报告放入步骤输出或进程环境，以免大型 PR 超过进程参数上限。矩阵、必需 manifest 和 ML CPU 调度继续使用原有有界输出。
- **浏览器运行环境**：可复用 E2E 套件与独立视觉回归 Job 固定使用 `ubuntu-24.04`，避免 `ubuntu-latest` 切换到 Playwright 尚不支持的发行版。升级 runner 前须核对 Playwright 支持范围，并通过 Chromium 安装及实际套件运行；不跳过浏览器依赖或缩小测试选集。
- **视频工具准备**：后端分片与 E2E runner 先复用已有的 `ffmpeg` / `ffprobe`，缺少时才以 `--no-install-recommends` 安装 FFmpeg，避免下载无关驱动及语音模型占满 Job 预算；安装前先把 runner 的 `/etc/apt/apt-mirrors.txt` 指回 HTTPS 的 `archive.ubuntu.com`，避免区域 HTTP 镜像拖慢同一个 `apt-get`；两者版本均写入步骤日志，测试选集和超时不变。
- **真实提供方验收**：版本、删除标记与净变化用例通过共用的 `live_provider_guard` 验证目标：本地仅允许工作树专属 `test` 桶；GitHub 托管 runner 可使用本次 Job 的临时 PostgreSQL、MinIO 与 Redis。远程存储、开发模式和非托管 runner 均拒绝，不用伪造工作树标记或跳过用例代替验收。
- **必需套件审计（`scripts/audit-e2e-requirements.mjs`）**：核心 smoke 以 `--retries=0` 且 `flakyPolicy=forbid` 运行（重试后通过仍会阻断审计）；专项套件保留一次诊断重试并单独标记 flaky。审计脚本按计划 manifest 严格核对每个选中套件的产物，任何缺失、取消、setup 失败、套件名不匹配或核心 flaky 都会令汇总步骤失败（不会把缺失报告当作通过）。
- **全量触发入口**：`push` 主分支与每日北京时间 03:00 的 `E2E extended` 定时工作流（`E2E_SCHEDULE_SCOPE=full`）执行全量套件；手动触发可通过 `scope` / `e2e_scope` 选择 `extended` 或 `full`。修改分类或调度规则后，可运行 `node --test scripts/plan-e2e-suites.test.mjs` 自检。

#### 重试策略与画布落库断言

- **重试与超时护栏**：功能用例最多重试 1 次；视觉基线（`playwright.extended.config.ts`）不重试；布局压力（`playwright.stress.config.ts`）最多重试 1 次以区分确定性损坏与高负载主线程饥饿。CI 中每个进程、测试步骤和 Job 分别限时 15 / 20 / 30 分钟，失败时自动保留 trace、截图与 `e2e-results.json`。
- **真实落库与像素级验证**：核心绘制流程必须验证实际提交请求、数据库落库内容与页面刷新恢复；测试辅助接口仅用于构造前置状态，严禁在 UI 保存失败后靠补写后端状态掩盖缺陷。图片与视频 Konva 的隐藏/恢复验收统一使用 `e2e/fixtures/annotation-canvas-pixels.ts`，仅统计媒体背景之上的标注图层像素。
- **构建产物验收**：CI 的产物套件使用真实 `vite preview`，在 `apps/web` 下通过 `pnpm build --mode e2e` 构建；本地也须使用所属工作树的 `e2e` 环境。常规生产构建不包含点云交互探针，不能替代 E2E 产物。浏览器侧不得靠动态导入 `/src` 修改应用 store。身份续期应走应用的真实刷新流程；网络故障注入只控制时机，刷新请求、权限重验及旧媒体许可退场仍由真实后端完成。仅限开发服务器的专门验收必须明确限制运行环境，不能混入产物套件。
- **交互夹具的响应形状**：隔离页面交互时也要提供周边读取接口的真实结构。例如 `/projects/{id}/datasets` 返回数组，不能用通用分页对象兜底，否则页面会先进入错误边界，无法验收到目标控件。
- **媒体尺寸夹具**：视频 Mask 的 `[height, width]` 取自当前任务真实 manifest，RLE 计数总和必须覆盖完整源帧；不能沿用历史夹具尺寸或用浏览器视口代替源媒体。
- **原件夹具**：`seed.owned()` 已生成真实 PNG 并登记原件内容身份，图片裁剪与二次推理直接复用，不再改写 `Task.file_path` 或 `DatasetItem.file_path`。共享 fixture 统一清理命名空间，临时裁剪由托管推理输入 owner 清理，不猜测旧物理键。
- **推理输入存储**：真实二次推理要求输入桶启用版本控制。CI 在本 Job 的临时 MinIO 初始化并核验 `import` 桶的 `Versioning=Enabled`；本地验收只能在核验当前工作树 `e2e` 桶的配置与归属后设置，不能改日常环境或放宽服务端版本校验。
- **取消过期运行**：PR 被新提交替代时，昂贵 E2E Job 使用 `!cancelled()` 停止调度与执行，不使用会阻止取消的 `always()`。共享构建失败仍阻止全部产物套件；有界的状态汇总及已有失败证据上传保留取消审计。

### `_test_seed` 路由与 E2E Fixture

E2E 用例通过 `apps/web/e2e/fixtures/seed.ts` 的 owner 进程通道调用夹具专用应用中的 `/api/v1/__test/seed/*` 路由，快速构造测试数据；不要求普通业务 API 获得完整 seed 清理权限：

```ts
// apps/web/e2e/tests/auth.spec.ts
import { test, expect } from "../fixtures/seed";

test("正确凭证 → 跳转 dashboard", async ({ page, seed }) => {
  const data = await seed.reset(); // 清理并重建固定 fixture
  await seed.loginViaUI(page, data.admin_email, "Test1234");
  await expect(page).toHaveURL(/\/dashboard/);
});

test("注入 token 跳过 UI 登录", async ({ page, seed }) => {
  const data = await seed.reset();
  await seed.injectToken(page, data.annotator_email); // 直接向 localStorage 注入凭据
  await page.goto("/annotate");
});
```

- **三重安全门禁**：该路由默认关闭，仅当非 `production` 进程显式设置 `E2E_SEED_ENABLED=true` 且当前会话 `current_database()` 以 `_e2e` 或 `_test` 结尾时才允许执行。
- **工厂函数约定**：`seed.reset()` 返回预置的 `admin` / `annotator` / `reviewer` 邮箱（统一密码 `Test1234`）、项目 ID 与 5 个任务 ID。自定义数据使用 `apps/api/tests/factory.py` 中的 `create_user`、`create_project`、`create_task`、`create_batch` 与 `create_membership`（平台身份与项目成员职责显式分离）。
- **图片与原生候选**：图片工作台种子使用真实 64×48 PNG，文件名、对象键、媒体类型及原件摘要对应同一份字节。原生图片候选沿已登记原件完整读回后签发回执，绑定真实内容身份、尺寸及事实摘要，不补造来源证据来绕过接纳检查。
- **训练基线归属**：训练验收种子通过与正式接口相同的分配流程，为新建检测/分割版本登记存储归属；重跑和失败重试保持原回执与保存桶。已有但未登记归属的历史版本不会自动认领，仍需按训练数据集的显式副本核验流程处理。
- **Labeling owner 命令**：E2E seed 写入 Labeling 的 Batch、Task、Annotation、视频追踪作业和 TaskDatasetItemLink 时通过固定 Labeling owner 命令；视频反馈锚点更新也走同一测试协议。命令只在数据库名以 `_e2e` 或 `_test` 结尾时可由运行角色调用，并在调用方原事务中执行；seed 路由仍负责原有提交或回滚时机。
- **专项筛选夹具**：筛选验收使用 `seed.filtering()`（或 `e2e/fixtures/filtering.ts`），它先重置基础 fixture，再返回类型化 manifest：同对象/跨对象属性、必填条件嵌套组、检测与追踪候选组合、101 项分页、Scene 逻辑轨迹和管理列表数据；需要进入视频工作台的用例传入 `{ videoReady: true }`，在种子阶段通过真实派生 worker 入口完成并核验元数据与分块，不依赖 CI 外部 Celery worker；普通筛选用例不额外生成视频。视频预测中的最小 shape 只验证指标；工作台几何交互通过产品预测导入 API 添加有效的带帧候选。点云 fixture 包含真实 PCD 字节和可解析的相机内外参。若需在本地手动体验并保留筛选数据，可运行 `pnpm dev:worktree -- up --mode e2e --scenario filtering`（参见 [多工作树并行开发环境](./how-to/worktree-environments#手动验收筛选功能)）。
- **共享 reset 与 Workspace 夹具**：共享 reset 前先调用各 namespace 的 owned-cleanup；共享选择器不能代替 owned 项目、筛选模板的精确清理。对象收尾前先调用现有项目退役守卫，已知未退役视频执行或忙锁返回 409。预检 savepoint 回滚，不提前提交从未启动作业的清理许可；最终删除仍重新检查。物理收尾完成后才删除夹具记录，最终数据库拒绝会回滚项目及子记录并保留重试锚点，但不撤销已完成的对象删除；解决阻塞后可重试，不伪造退役历史。协议与授权见 [Workspace 夹具边界](/api/guides/workspace-runtime#门控-e2e-夹具)。
- **并发与清理兜底**：同一测试库同一时刻只能运行一个 seed/reset 流程；多进程并行测试需同时隔离数据库与 MinIO 存储桶。Playwright 结束时的 `globalTeardown` 会调用 `/seed/cleanup`，但强制中断可能跳过 teardown，因此必须始终依赖 `annotation_e2e` 数据库隔离，不能把 cleanup 当成可在开发库运行 E2E 的理由。新增清理条件应使用 fixture 的项目、任务或显式标记；数据保留断言必须重新查询数据库，不能依赖未过期的 ORM identity map。
- **页面先退场**：测试结束并记录失败证据后，seed 先关闭经 `injectToken` 绑定的准确页面，再清理它构建的命名空间，避免仍在播放的页面持续创建预取；API-only 用例不会为此启动浏览器，测试中显式调用 `cleanupOwned` 不关闭页面。关闭与清理失败都保留为测试失败。
- **视频晚准入收尾**：清理先退休加锁时发现的全部派生物归属、提交并释放样本锁，再排空首份快照之后准入的已登记读取。读取可完成最终核验并归还许可；等待预算不增加，超时保留归属与责任，不删除活动或未知操作。
- **清理范围栅栏**：账号、项目的最终范围锁可有界等候本夹具尚在收尾的请求，并恢复原本的锁超时；上传 owner、存储许可及未知操作仍保持即时拒绝，等待超时仍返回冲突并保留原责任。
- **评论附件清理**：owned/shared 清理通过精确夹具项目找到独立上传责任，先退休已完成上传，再沿保存位置删除；所有存储 IO 完成后才锁定并删除原夹具和已完成责任。未完成上传、活跃或未知收尾许可、保存位置不明、规范键残留和删除失败会明确失败并保留重试锚点。不要篡改90天期限或删访问账本来绕过；无法确认上传已结束时，核验归属后销毁整个可丢弃工作树 mode，参见[多工作树环境](./how-to/worktree-environments)。清理不按上传者反向删除其他项目的标注评论。
- **指南图片清理**：owned/shared 清理只解除精确夹具项目的权威引用，并退休失去最后引用的已完成上传；其他项目的共享引用继续有效。该流程不立即删除对象，物理清理由cleanup队列后续执行，或在核验归属后销毁整个一次性mode。仍属于精确夹具项目的pending会阻挡清理；原项目已删除且无法证明属于当前scope的pending保留owner与字节，不按namespace猜测或阻挡无关夹具。不要按年龄、上传者或JSON反向清桶，也不要删除独立责任来绕过未完成写入。

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
