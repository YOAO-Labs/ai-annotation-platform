---
audience: [dev]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-07-23
---

# 测试指南

## 测试金字塔

```
        ╱╲
       ╱E2E╲      少量、慢、覆盖关键用户路径
      ╱──────╲
     ╱集成测试╲    适量、按 API 端点为单元
    ╱──────────╲
   ╱  单元测试  ╲   大量、快、覆盖纯逻辑
  ╱──────────────╲
```

按这个比例分配精力。**不要**为单一函数写 E2E、也不要为页面跳转写单元测试。

### 分层归属与运行入口

| 层           | 保护什么                                    | 运行入口                                                                                        | 真值来源                      |
| ------------ | ------------------------------------------- | ----------------------------------------------------------------------------------------------- | ----------------------------- |
| 后端纯规则   | 授权谓词、作业终态分派、schema 归一等纯函数 | `cd apps/api && uv run pytest <file>`                                                           | 无需数据库                    |
| 后端集成     | 事务、行锁、成员并发、通知顺序              | `pnpm dev:worktree -- exec --mode test -- bash -lc 'cd apps/api && .venv/bin/python -m pytest'` | worktree 自有 `aap_wt_*_test` |
| 前端纯规则   | URL 编解码、几何、状态机                    | `pnpm --filter @anno/web test <file>`                                                           | 无                            |
| 前端页面集成 | 页面装配与 API 边界契约                     | 同上（页面目录）                                                                                | MSW                           |
| 浏览器       | 真实画布、渲染、长链路                      | `pnpm test:e2e`（见下文）                                                                       | 隔离服务                      |
| SDK / 示例   | 对外契约与文档示例                          | `packages/python-sdk`、`docs-site/dev/examples/*`                                               | OpenAPI snapshot              |

两条当前事实：

- 全部 8 个 ML CPU 契约套件已接入 CI：PR 触及 `apps/_shared/**`（`backend_runtime` /
  `mask_utils` / `protocol_v2`）或任一 `apps/*-backend/` 路径时，`ML CPU contract tests`
  检查（`.github/workflows/ml-cpu-test.yml`，`suites: "all"`）会在每套件独立的一次性 venv 中
  运行对应 pytest（纯 CPU：`grounded-sam2` 与 `sam3` 需 CPU torch wheel，其余 torch-free；
  `onnxtools` 额外补装 `opencv-python-headless`，3 个上游 `importorskip` 跳过为预期）。
  各 backend 的 `pyproject.toml` 通过 `pythonpath` 注入 `../_shared/*/src`，共享包无需 editable
  安装；注意各 backend 可解析的共享包**并不相同**（图像/视频 backend 含 `mask_utils`）。
- 页面测试优先在 MSW API 边界描述响应（`src/test/renderWithProviders.tsx` 与
  `src/test/dataManagerApi.ts` 是样板）；同一纯规则不要在页面测试里重复断言，
  已下沉的规则写在对应 `*UrlState.test.ts`。

## 后端：pytest

### 跑

```bash
cd apps/api
uv run pytest                                # 全部
uv run pytest tests/test_smoke.py -v         # 单文件
uv run pytest -k "batch_lifecycle"           # 关键字过滤
uv run pytest --cov=app --cov-report=html    # 看覆盖率
```

报告：`htmlcov/index.html`。

### Fixture（已就绪）

`tests/conftest.py` 提供：

| Fixture                                                    | 用途                                      |
| ---------------------------------------------------------- | ----------------------------------------- |
| `db_session`                                               | function-scoped，SAVEPOINT 隔离的 DB 会话 |
| `httpx_client`                                             | ASGI 客户端，依赖注入了 db_session        |
| `super_admin` / `project_admin` / `annotator` / `reviewer` | 4 角色 fixture，带 JWT token              |

测试库连接解析规则：显式 `TEST_DATABASE_URL` 优先，否则跟随本环境迁移连接并固定到
`annotation_test`；解析失败会直接报错，不会回退到任何默认连接串。无论来源，目标都必须是
postgresql 且库名以 `_test` 结尾的一次性测试库（如 worktree 启动器分配的 `aap_wt_*_test`），
指向开发/生产库的配置会在迁移/写入前被拒绝。

### 写一个 API 测试

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

每次改路由 / Pydantic schema：

```bash
# 改完路由后
cd apps/api
uv run python ../../scripts/export_openapi.py
git add openapi.snapshot.json
```

CI 中 `tests/test_openapi_contract.py` 会校验 snapshot 与运行时一致；忘了刷就 fail。前端 `pnpm codegen` 也读这个 snapshot，所以 snapshot 是前后端契约的真值源头。

## 前端：vitest + MSW

### 跑

```bash
cd apps/web
pnpm test                  # 一次性跑
pnpm test:watch            # watch
pnpm test:coverage         # 带覆盖率
```

### MSW 用法

`vitest.setup.ts` 已挂上 MSW server，默认 handlers 在 `src/mocks/handlers.ts`。

单测里临时覆盖某个 endpoint：

```ts
import { http, HttpResponse } from "msw";
import { server } from "@/mocks/server";

it("空态文案", async () => {
  server.use(
    http.get("*/api/v1/projects", () =>
      HttpResponse.json({ items: [], total: 0 }),
    ),
  );

  render(<ProjectList />);
  expect(await screen.findByText(/还没有项目/)).toBeInTheDocument();
});
```

### 写组件测试的边界

✅ 写：渲染分支、用户交互后的状态变化、与服务端契约的校验
❌ 不写：颜色样式、像素级布局、内部状态字段名

### 超大图 Tile

纯逻辑测试覆盖 manifest 校验、full-resolution 半开视口 rect、DPR/LOD hysteresis、overlap crop、
edge tile、批量签发、decode、stale commit 和 dispose：

```bash
pnpm --filter @anno/web exec vitest run \
  src/pages/Workbench/stage/imagePyramid.test.ts \
  src/pages/Workbench/stage/imageTileScheduler.test.ts \
  src/pages/Workbench/stage/useWorkbenchImageSource.test.ts \
  src/pages/Workbench/stage/useImageTileScheduler.test.tsx \
  src/pages/Workbench/stage/useImageStageFit.test.ts \
  src/pages/Workbench/stages/image/ImageWorkbench.test.tsx
```

真实 50MP/200MP 浏览器回归使用 `P-LARGE-IMG` / `DS-LARGE-IMG` 开发夹具；先按
[超大图金字塔派生资产](/dev/concepts/image-pyramid-assets#可复现开发夹具)下载、入库并等待 ready。
required 大图用例必须断言自动路径没有 original 请求；快速 pan/zoom/切题后检查 BUG 诊断中的
stale commit、reserved/retained bytes、live bitmap/ObjectURL 与 request 数在停止后进入 plateau，
离开任务后全部归零。临时下载和浏览器 trace 只写入 gitignored `test-results/`，测试结束后删除。
不同长宽比的大图切题回归还必须检查：新 source 不得渲染已释放的旧瓦片，缩放比例按新图尺寸重新 fit，先放大再连续缩小可通过滚轮和浮条回到当前全图适应比例，且放大后 Minimap 与右下角缩放浮条的边界不相交。

## 前端：Playwright E2E

详见 `apps/web/e2e/README.md`。

启动：

```bash
docker compose up -d postgres redis minio
pnpm test:e2e
```

Playwright 会自动准备专用逻辑库 `annotation_e2e`、执行迁移，并启动 Web
`127.0.0.1:3001` 与 API `127.0.0.1:8010`。它不会复用开发环境的
`3000/8000`；专用端口被占用时会直接失败。如需替换隔离测试库，设置
`PLAYWRIGHT_E2E_DATABASE_URL`，并保持库名以 `_e2e` 或 `_test` 结尾。

**何时写 E2E**：跨页面流程、长链路、涉及 WebSocket / 文件上传。

**何时不写 E2E**：单组件交互、纯逻辑校验。

### CI 测试分层

| 测试集    | 命令（在 `apps/web`）                                                              | 执行范围                                                     |
| --------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 功能回归  | `pnpm test:e2e`                                                                    | 每个 PR 的四个分片；包含六种布局上下文的 14 次重排和恢复检查 |
| Mask 矩阵 | `pnpm test:e2e:mask-readonly` / `test:e2e:mask-native` / `test:e2e:mask-ai-native` | 每个 PR，分别使用独立服务与数据                              |
| 视觉基线  | `pnpm test:e2e:visual`                                                             | 应用与共享依赖相关 PR、主分支、夜间和手动                    |
| 布局压力  | `pnpm test:e2e:stress`                                                             | 相同触发范围；保留六种上下文各 54 次重排                     |

普通功能配置排除 `@visual` / `@stress`。外观比较使用既有截图基线与容差，预期视觉变更必须审阅差异后更新；功能用例检查可操作性、状态与真实保存。新增压力测试时同步保留覆盖关键状态转换的短流程，不能只在夜间检查画布丢失、草稿丢失或任务切换写入问题。

布局矩阵按画布类型拆分 Playwright 项目：`workbench-pointcloud-layout-matrix.spec.ts` 只保留 3D 场景（pointcloud 项目，SwiftShader 软渲染 WebGL）；图片 / 视频场景在 `workbench-layout-matrix.spec.ts`，走 chromium 项目——它们的 Konva 画布不需要 WebGL，软渲染只会让 CI 压力重载时主线程更易被饿死。两份 spec 的用例注册共用 `e2e/helpers/workbench-layout-matrix.ts`，标题保持一致以稳定分片与扩展套件选集。

路由脚本 `scripts/plan-e2e-suites.mjs` 是套件选择的单一入口，当前执行 **P9 计划选择**（plan §6.2/§6.3）：
PR 运行有界核心 smoke + 按路径触发的领域专项（Mask×3、video-pipeline、pointcloud），共享依赖/运行时/未映射路径/空 diff/多域/重命名删除等无法分类的输入保守放大到全量；纯文档（白名单 `.md`）显式跳过应用 E2E 并记录原因。每次运行都会同时输出冻结的旧全量选集作为对照（`legacy=`），便于比较与回退。

有界核心 smoke 以 `--retries=0` 运行，且被标记为 `flakyPolicy=forbid`：重试后通过的核心会阻塞必需套件审计（非核心专项保留一次诊断重试，重试后通过仍单独计为 flaky）。必需套件审计（`scripts/audit-e2e-requirements.mjs`）按计划 manifest 校验每个选中套件的结果产物，缺失、取消、setup 失败、suite 名不匹配、格式错误或核心 flaky 都会使汇总失败。

准备失败导致 Playwright 未执行时，汇总仍写出 `setup-failure` 状态产物；运行失败或取消但无报告也保留对应失败证据。声称成功却没有报告会使汇总步骤失败，并写出失败状态，不会把缺失报告或未执行当作通过。

顶层入口与范围（§6.7-5 显式区分，不用事件名隐含）：`push` 与夜间 `E2E extended` 工作流（每天北京时间 03:00，`E2E_SCHEDULE_SCOPE=full`）执行全量（smoke + 全部分片 + 全部专项 + visual/layout-stress）；该工作流手动触发时用 `scope` 输入显式选择 `extended` 或 `full`；`ci.yml` 的 `Frontend E2E` 手动触发用 `e2e_scope` 输入做同样选择。所有入口都运行同一必需套件审计。

**回退**：设置仓库变量 `E2E_SELECTION_MODE=legacy`（未设置或为空即计划模式）即可恢复 P8 前冻结的旧全量矩阵与对应必需清单，无需改代码；回退模式仍执行审计，但只要求它实际执行的 9 个旧套件。

**最终验收证据**：门禁切换与影子对照的逐项证据见 `docs/research/43-repository-optimization-p9-shadow-comparison.md`，P10 最终验收记录见 `docs/research/45-repository-optimization-final-acceptance.md`。冻结 `898505469` 的 full-12 campaign 是**失败历史**（default-four 13P/1F/53 not-run，实跑审计 exit 1）；集成根 `13d274326` 的 composed 审计才退出 0（`layout-stress`/`default-four` 在修正构建重跑替换，其余 10 套件按**变更影响范围**复用，不是全应用逐字节等价）。

功能用例最多重试一次；扩展用例中视觉基线（`playwright.extended.config.ts`）不重试，压力用例（`playwright.stress.config.ts`）最多重试一次——压力重载在高负载 runner 上可能把渲染主线程冻结数十秒，重试用于区分确定性损坏（两次都失败仍红）与资源饥饿，重试后通过仍标记 flaky，不掩盖问题。CI 首个最终失败终止当前分片，每个进程/测试步骤/job 分别限时 15/20/30 分钟。每次失败保留 trace 与截图，Actions 摘要区分通过、失败、flaky 与跳过，避免把重试后通过误认为已消除不稳定性。HTML、原始测试产物及 `e2e-results.json` 随套件上传。

核心绘制流程必须验证实际提交请求、落库内容与刷新恢复。测试接口只能造前置状态，不能在 UI 保存失败后补写状态来使测试通过。Canvas 坐标按实际媒体尺寸计算并验证命中；组件细节、纯状态组合优先使用 Vitest。

图片与视频 Konva 的隐藏/恢复验收使用 `e2e/fixtures/annotation-canvas-pixels.ts`，只统计媒体背景之后的标注图层，不把跨域背景画布读成标注证据。必须先确认有真实绘制像素，再验证隐藏减少、刷新后仍隐藏、恢复后像素与几何一致；图层未就绪或标注层不可读应失败，不能吞掉异常返回零。跨域画布安全限制需要真实浏览器回归，不能靠关闭浏览器安全策略或仅断言 DOM 数量代替。

修改分类后通过 `--list --reporter=line` 核对选集；修改调度后运行根目录的 `node --test scripts/plan-e2e-suites.test.mjs`。本地结束后清理当前验证生成的报告、临时数据和自有服务；详见 `apps/web/e2e/README.md`。

### `_test_seed` router + E2E fixture

E2E spec 通过 `apps/web/e2e/fixtures/seed.ts` 调后端 `/api/v1/__test/seed/*` 端点造数：

```ts
// apps/web/e2e/tests/auth.spec.ts
import { test, expect } from "../fixtures/seed";

test("正确凭证 → 跳 dashboard", async ({ page, seed }) => {
  const data = await seed.reset(); // 清理并重建固定 fixture
  await seed.loginViaUI(page, data.admin_email, "Test1234");
  await expect(page).toHaveURL(/\/dashboard/);
});

test("注入 token 跳 UI 登录", async ({ page, seed }) => {
  const data = await seed.reset();
  await seed.injectToken(page, data.annotator_email); // 直接 localStorage 注入
  await page.goto("/annotate");
});
```

**安全约束**：路由默认关闭，只有非 production 进程显式设置
`E2E_SEED_ENABLED=true` 时才会挂载。路由的统一守卫还会在当前会话查询
`current_database()`，数据库名不以 `_e2e` 或 `_test` 结尾时拒绝所有
seed/login/cleanup 请求。production 即使设置开关也不挂载路由。

**fixture 用法**：`reset()` 返回固定结构（admin/annotator/reviewer 三个邮箱 + 项目 id + 5 个任务 id）；密码统一 `Test1234`。新增数据用 `apps/api/tests/factory.py` 的 `create_user / create_project / create_task / create_batch / create_membership`。用户工厂只创建平台身份；`create_membership` 显式创建项目成员与职责，两者分开，工厂不会从平台角色推断 membership。批量 `add_all`、需要跨会话并发 seed 或固定 `id` / `version` 的场景仍按需内联构造。

筛选验收使用 `seed.filtering()`，它先重置基础 fixture，再返回类型化 manifest：同对象/跨对象属性、必填条件嵌套组、检测与追踪候选组合、101 项分页、Scene 逻辑轨迹和管理列表数据。`e2e/fixtures/filtering.ts` 提供同一入口。视频预测中的最小 shape 只验证指标；工作台几何交互通过产品预测导入 API 添加有效的带帧候选。点云 fixture 包含真实 PCD 字节和可解析的相机内外参。

需要手动体验并保留数据时，使用 `pnpm dev:worktree -- up --mode e2e --scenario filtering`；见[手动验收筛选功能](./how-to/worktree-environments#手动验收筛选功能)。该入口与自动化测试复用基础夹具，重启不重置，结束后显式清理。

同一测试库一次只运行一个 seed/reset 流程。并行测试还需给各进程配置独立 MinIO buckets：基础清理使用固定媒体前缀，仅隔离数据库无法保护另一套测试的媒体。新增清理条件应使用 fixture 的项目、任务或显式标记；数据保留断言必须重新查询数据库，不能依赖未过期的 ORM identity map。

Playwright 正常结束时由 `globalTeardown` 调用 `/seed/cleanup`。它只是兜底：
强制中断可能跳过 teardown，因此必须始终依赖 `annotation_e2e` 的数据库隔离，
不能把 cleanup 当成可在开发库运行 E2E 的理由。

## 覆盖率

CI 上传到 [Codecov](https://codecov.io)，PR 评论显示 diff coverage。

**v0.8.3 切硬阻断**：`codecov.yml` backend `informational: false`（target 60%）+ frontend `informational: false`（target 10%，实测 10.88% 留 0.88pp 容差）。`apps/web/vite.config.ts` coverage thresholds 同步生效（lines/statements ≥ 10）；`pnpm test:coverage` 低于阈值非 0 退出。

ROADMAP 列出的 ≥ 25% 目标继续推：补 InviteUserModal / RegisterPage / Dashboard / ProjectList / WorkbenchShell 等页面级单测，达标后上调阈值。

## Pre-commit

先执行 `uv tool install "pre-commit==4.6.1" && pre-commit install`。之后每次
`git commit` 自动跑：

- Ruff check + format（全部第一方 Python，排除 vendor）
- Prettier（本次提交涉及的受支持文本文件）
- eslint（apps/web）
- tsc --noEmit（apps/web）
- API/schema/config 或共享协议变更时刷新 OpenAPI 快照；快捷键、工作台设置或 API
  路由变更时刷新对应的受跟踪文档生成页

如果 hook 失败，**不要** `--no-verify`，先把问题修了。

CI 在全仓 `format:check`、Ruff、ESLint 和类型检查之外，还会以 `manual` 阶段执行一次
`pre-commit --all-files`。该阶段跳过会写入并暂存 OpenAPI、能力词表和文档生成物的三个
本地提交 hook；这些生成物继续由 OpenAPI 契约测试、能力词表契约测试，以及覆盖快捷键、
工作台设置和 API 路由索引的文档 codegen 只读检查兜底。
