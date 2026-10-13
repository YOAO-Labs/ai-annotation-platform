# E2E 测试（Playwright）

完整跨服务的端到端测试，验证「前端 + FastAPI + Postgres + Redis + MinIO」整条链路。

## 何时写 E2E

- 用户关键路径：登录、创建项目、提交标注、批次审核、导出
- 跨页面流程：项目创建 → 任务分配 → 标注 → 审核
- 涉及 WebSocket / 文件上传 / 长流程的功能

**不要**用 E2E 测试组件细节或纯逻辑——这些用 vitest 单测覆盖。

## 本地运行

```bash
# 只需先起共享基础设施
docker compose up -d postgres redis minio

# Playwright 自动准备 annotation_e2e、迁移数据库，并启动专用 Web/API
cd apps/web && pnpm test:e2e            # 全部功能回归（含布局短流程）
pnpm test:e2e e2e/tests/auth.spec.ts    # 单文件
pnpm test:e2e --headed                   # 看着浏览器跑
pnpm test:e2e --ui                       # 交互式 UI 模式
pnpm test:e2e:visual                     # 独立截图比较
pnpm test:e2e:stress                     # 完整布局压力矩阵
```

首次运行需要 `pnpm exec playwright install chromium` 装浏览器。

CI 使用 `line` reporter 显示正在执行的用例及重试，同时保留 GitHub annotations、HTML 和 `e2e-results.json` 报告。Actions 摘要分别列出每个套件通过、失败、重试后通过（flaky）、跳过数量及执行时间。每次失败尝试都保留截图和 trace，包括零重试的视觉基线套件；报告缺失会明确提示检查启动或超时日志。分片内按单 worker 串行运行；排查长时间执行时先查看当前用例和超时信息，不能只凭 WebSocket 断连日志判断测试卡死。

CI 功能用例最多重试一次；视觉基线不重试，压力用例最多重试一次。首个用例最终失败后终止当前分片，其他分片继续完成各自诊断。每个 Playwright 测试进程总限时 15 分钟，测试步骤限时 20 分钟，整个 E2E job（含安装和构建）限时 30 分钟。分层限时为清理和报告上传保留余量，并在测试进程无法自行退出时由 Actions 兜底。本地运行不启用这些 CI 早退限制。

## 功能、视觉与压力测试

默认 `playwright.config.ts` 排除 `@visual`、`@stress`；扩展配置拆成两个入口，均复用相同项目、服务和数据隔离配置：`playwright.extended.config.ts` 只选择 `@visual`（零重试），`playwright.stress.config.ts` 选择 `@stress`（最多重试一次，用于区分确定性损坏与 runner 资源饥饿，重试通过仍标记 flaky）。不要在功能测试中混入整页或整个工作区的截图断言。给视觉测试添加 `{ tag: "@visual" }`，保持其标题、文件位置和项目名以复用既有基线；有意的外观变化经人工核对差异后，用 `pnpm test:e2e:visual --update-snapshots` 更新并提交基线，不能自动接受差异。

布局矩阵在图片/视频/点云 × 标注/审核六种上下文中都保留真实拖动、画布身份、上下文隔离、紧凑模式、保存与刷新恢复断言。功能集执行 14 次重排，`@stress` 执行完整 54 次；新增长循环必须放入压力集，并在功能集中保留覆盖关键状态转换的短流程。

CI 调度规则由根目录 `scripts/plan-e2e-suites.mjs` 维护：

- 每个 PR 始终执行四个功能分片和三个 Mask 套件。
- `apps/web/`、`apps/api/`、`packages/`、共享依赖/容器配置、E2E workflow 或路由脚本变更时，追加视觉和压力套件。分类保守覆盖共享依赖；布局或样式 PR 也会跑完整压力矩阵，只是与功能检查独立执行和报告。
- 主分支执行全部九个套件。`E2E extended` 每天 UTC 19:00（北京时间次日 03:00）及手动运行两个扩展套件。文档截图的每周 `Browser checks` 保持独立。
- PR 路径读取失败会使计划 job 失败；`Frontend E2E` 汇总同时要求计划和所有已选套件成功，不会把漏跑当作通过。

`seed` 只用于准备前置数据。核心创建用例必须验证真实 UI 操作、保存响应、API 读回和刷新恢复；不能在 UI 保存失败后调用 `seed.advanceTask` 等接口伪造成功。Canvas 指针坐标应依据当前媒体位置和尺寸计算，并确认没有被浮层遮挡。连续执行相互独立的边缘绘制时，通过真实 UI 收起前一个标注的浮窗，并检查拖拽起点命中画布。截图检查不能替代保存检查，存储状态检查也不能替代真实渲染检查。

Mask 设置统一通过 `fixtures/mask-toolbar.ts` 打开：等待胶囊展开及有限动画完成，再点击更多工具，防止展开期间滚动位置变化使按下和抬起命中不同位置。虚拟标注列表进入编辑时，优先使用选中信息卡上的真实按钮；不要依赖滚动后可能失去 hover 的行内操作，也不要用强制点击绕过命中检查。

修改测试分类后先执行各命令的 `--list --reporter=line`，确认标签不会造成漏选；修改 CI 路由时执行 `node --test scripts/plan-e2e-suites.test.mjs`。本地验证结束后清理当前测试生成的 `test-results/`、`playwright-report/`、`e2e-results.json` 及专用环境数据，保留人工确认需要提交的基线图片。

本地 E2E 固定使用 `annotation_e2e` 逻辑库、Web `127.0.0.1:3001`、API
`127.0.0.1:8010`。Playwright 不会复用开发端口 `3000/8000`；专用端口被占用时
直接失败，防止测试误写开发服务。如需使用其它隔离测试库，用
`PLAYWRIGHT_E2E_DATABASE_URL` 覆盖；目标库名仍必须以 `_e2e` 或 `_test` 结尾。

本地并行运行多组测试时，每组须使用独立测试数据库、服务端口和 `MINIO_DATASETS_BUCKET`。WebCodecs 夹具清理会删除桶内整个测试前缀；共享同一个桶的测试应串行执行，避免另一组 reset 删除正在读取的视频对象。CI 分片使用各自 runner 上的独立 MinIO 服务。

## 数据准备

避免每个 spec 重复造数据：`e2e/fixtures/seed.ts` 通过
`/api/v1/__test/seed/*` 创建 fixture。这组路由需要同时满足：

- `E2E_SEED_ENABLED=true`；
- 当前数据库名以 `_e2e` 或 `_test` 结尾；
- `production` 环境永不挂载路由。

**默认使用私有命名空间 fixture**：`await seed.owned()` 按 spec 文件 + 测试标题
确定性派生命名空间（`^[a-z0-9]{4,12}$`，重试稳定、跨测试唯一），由 `/seed/owned`
只创建该命名空间的标准工作台 fixture，测试结束后 seed fixture teardown 调用
`/seed/owned-cleanup` 精确清理（含存储对象与 mock backend 注册项）；清理失败会让
测试失败。`/seed/lidar`、`/seed/project-roles`、`/seed/filtering` 的命名空间参数
同样只触碰各自命名空间；filtering manifest 暴露 `display_names`、`invitation_emails`、
`search_keys`、`audit_scopes`，spec 必须用 manifest 值做全局列表搜索/文案/导出断言。

`seed.reset()` 只保留给确属全局操作的套件：它破坏性重建共享命名空间并会收敛所有
E2E 数据（含 owned 命名空间），是已记录的串行例外；不要再在普通用例中调用。需要
跨命名空间存活/清理证据时，可用 `fixtures/storage-object-probe.py` 经真实存储客户端
读取对象字节。

正常结束时 `globalTeardown` 会调用 `/api/v1/__test/seed/cleanup` 清除固定 E2E
数据。这只是卫生性兜底：强制中断或进程被终止时 teardown 可能来不及执行，
所以数据库隔离才是不污染开发库的根本保障。下次运行会通过 reset/cleanup
重新收敛专用库的状态。

## 当前题 AI 的真实异步作业 E2E

`workbench-ai-inspector-phases.spec.ts` 需要与隔离 API 使用同一测试数据库和专用
Redis broker 的 Celery worker；确认后设置 `PLAYWRIGHT_AI_REQUEST_WORKER=1` 才会执行。
不能把 worker 连到开发数据库或复用开发任务队列。API 和 worker 同时覆盖
`DATABASE_URL`、`MIGRATION_DATABASE_URL`、`REDIS_URL`、`CELERY_BROKER_URL`，并设置相同的
`PREANNOTATE_GPU_QUEUE` / `PREANNOTATE_CPU_QUEUE`。worker 订阅这两个队列及 `default,audit`，
使用当前 checkout 的虚拟环境，不启动 beat。关闭 GPU arbiter 和 backend router，防止
继承开发环境调度：`GPU_ARBITER_MODE=off`、`GPU_ARBITER_ROLLOUT_ENABLED=false`、
`GPU_ARBITER_RESOURCES_JSON={}`、`ML_BACKEND_ROUTER_MODE=off`。

模型夹具以 Node HTTP server 提供确定性的 setup / predict，在真实 API 注册路径中临时
替换 seed backend 并于每例结束恢复。它只模拟模型输出、延迟和失败，预标派发、作业查询、
取消、预测落库和候选采纳均使用真实服务。`PLAYWRIGHT_ML_FIXTURE_HOST` 可指定 API/worker
能访问的非 loopback IPv4；缺省选择本机接口地址。该检查不验证模型质量。

```bash
PLAYWRIGHT_AI_REQUEST_WORKER=1 pnpm test:e2e \
  e2e/tests/workbench-ai-inspector-phases.spec.ts --project=chromium
```

每例在输出目录保存不含凭据的 `ai-request-metadata.json`，记录项目与模型收到的任务 ID。
结束时先释放模型等待并等作业终态，再恢复 seed backend、清理测试数据；按记录中的任务 ID
从 import 桶删除精确的 `frame-predict/{task_id}/` 临时帧前缀，停止自有 worker 及 broker，
清理报告与临时端口配置。服务异常中断时仍需执行相同清理。

## WebCodecs 精确帧 E2E

视频 Issue 的 180 帧夹具按测试 API 的 `VIDEO_CHUNK_SIZE_FRAMES` 生成完整真实分片，默认三块，每块 60 帧。测试开始前核对正式 manifest 与分片范围、就绪状态；不能依赖未启动的媒体 worker 补齐缺失分片。像素身份仍从实际编码视频验证。请求失败分类集中在 `helpers/request-errors.ts`：每条允许项都是「方法 + 精确路径 + 原因」的有类型规则，图片与视频套件按共享规则组组合；只允许明确端点的 `net::ERR_ABORTED` 生命周期取消（心跳、会话统计、bootstrap/任务上下文读、帧预览及分片元数据/样本），标注与 Issue 写入失败、其他网络错误与 HTTP 错误仍须报告。`pnpm --filter @anno/web test scripts/video-request-errors.test.ts` 同时验证这些边界的允许侧与禁止侧，CI 的前端单元测试同步执行。

刷新前的布局保存等待必须匹配当前交互产生的 workspace 快照，并等响应接收完成。初始化、切换预设和激活讨论面板会分别发送偏好 PATCH，首条成功响应不能代表后续保存完成。任务级 Issue 用例延迟最终讨论布局响应，验证刷新不会抢先取消偏好写入；偏好 PATCH 的取消仍按错误处理。

`e2e/tests/video-webcodecs-precise-frame.spec.ts` 用 `seed/video-webcodecs`
造确定性 H.264 fixture（baseline / 主 profile B 帧 / 短 GOP / VFR），验证精确帧
pipeline 的开关边界、精确解码或安全回退、pending→ready 切换。视频舞台容器暴露
`data-video-frame-source` / `data-video-precise-state` / `data-video-frame-index`
三个可观察属性供 spec 读取；`data-video-painted-frame-index`（精确绘制完成的帧号，
非 webcodecs 源恒为 `-1`）用于等待绘制就绪。导航触发的异步取帧在负载下可能
迟到：应用层已在播放控制器的帧号写入处加单调栅栏，迟到的旧帧回报不会再覆盖
更新后的帧号（Issue #114，组件级回归见
`useVideoPlaybackController.stale-seek.test.ts`），视频 Issue 套件的
`expectPaintSettled` 保留为冗余防护——等它与 `data-video-frame-index` 一致后再
继续或断言仍最稳妥。

### 时间轴定位契约与真实指针回归

视频 Issue/track 套件建立前置帧一律走 `e2e/helpers/video-timeline-seek.ts` 的
确定性通路：先等可见窗口包含目标（需要时展开时间轴并 zoom-reset，等窗口状态
实际更新），再把能精确表达目标帧的 range 归一化值（0..10000）写入「视频帧时间轴」
输入，经应用既有 `onChange → onSeek` 生效。映射与组件 `timelineCoords.pctToFrame`
同款，`rangeValueForFrame` 反解不精确即显式失败——本套件 180 帧素材在任意合法
窗口下逐帧可表达（`scripts/video-timeline-seek.test.ts` 逐帧验证）；更长素材出现
不可表达帧时应缩窗或扩粒度，不允许静默近似。导航失败时 `seek` 会在
`seek-navigation-state` 附件写入目标/选择/绘制帧、显示源、precise state 与时间轴
窗口的只读快照，帮助区分「目标未写入 / 呈现未完成」，且不覆盖原始断言错误。

像素采样（`fixtures/video-frame-pixels.ts`）对「属性断言已过、采样时文档恰好被
真实导航替换」的竞态做了有界重试：仅针对 `Execution context was destroyed`，
重试在当前文档上重新采样。帧身份断言逐帧特异，采错帧会照常失败，因此重试
不会把导航竞态放大成假通过。

不要回退成「裸坐标点击时间轴 + 有限键盘补偿」：播放浮层在指针离开画布约 2s 后
进入 `pointer-events: none`，裸 `mouse.click` 绕过 actionability 检查会被静默吞掉
（`default-two` shard 2/4 曾因此偶发失败：点击后停留网格吸附帧，补偿十次后断言
失败）。fill 不做命中检查，不依赖浮层可见。

真实鼠标路径由 `e2e/tests/video-timeline-seek.spec.ts` 单独覆盖：折叠态、展开态、
关闭 Issue 表单后、缩放窗口与首末帧端点。它以 `video-timeline-shell` 为定位器，
用 `locator.click({ position })`（带 hit-target 检查，被吞的点击会显式失败），
几何换算与组件 `frameFromPointer` 一致；点击前通过真实指针移动唤出浮层并断言
shell `pointerEvents` 恢复 `auto`，不用 `force: true`、不关动画、不用键盘补救。
端点点击内缩 2px 并容忍 ±1 帧的取整漂移（输入量化到整数像素的固有不确定
性，内部帧仍逐帧精确断言）；不要把端点断言绑定在个别边界像素上。

**能力门**：WebCodecs `VideoDecoder` 需 secure context。localhost 下 Chromium 暴露
构造器，但 headless 软解下 `isConfigSupported` / 实际 decode 可能不通过，精确帧会
安全回退。spec 据此 **capability-aware**：先观测 pipeline 实际解析到的
`data-video-frame-source`，精确帧成功才跑 corner_bits 像素断言，回退则验证 fallback
合同并通过 annotation 记录原因 —— 绝不把「回退」伪装成「像素已验证」，也不把
「能力不足」判成失败。

spec 已实现 Konva media canvas 的区域像素采样：按背景亮度与四角 bit 验证 key / P /
B / GOP / VFR 目标帧，并同步核对诊断中的目标 PTS。默认冒烟环境若明确缺少 H.264
解码能力，会把像素和 pending→precise 用例标为 capability skip。浏览器资格测试必须
显式开启严格能力门，此时缺少 WebCodecs 原语、codec 不支持或 `decode_failed` 都会失败，
不能用回退或 `ready` 属性代替像素证据：

```bash
cd apps/web
DISPLAY=:0 XAUTHORITY=/run/user/1000/gdm/Xauthority \
PLAYWRIGHT_CHROMIUM_CHANNEL=chrome PLAYWRIGHT_REQUIRE_WEBCODECS=1 \
pnpm exec playwright test \
  e2e/tests/video-webcodecs-precise-frame.spec.ts --project=chromium --headed
```

GPU runner 应显式选择系统 Google Chrome；Playwright bundled Chromium 的 codec
发行配置可能与用户 Chrome 不同。`DISPLAY` / `XAUTHORITY` 按 runner 的本地 X11
会话调整，不能把不可用的 SSH 转发显示误记为浏览器能力失败。

## 文件组织

```
e2e/
├── fixtures/          # 共享 fixture（seed、authedPage 等）
├── global-teardown.ts # 正常结束时的 cleanup 兜底
├── tests/             # 实际 spec
│   ├── auth.spec.ts
│   ├── annotation.spec.ts
│   └── batch-flow.spec.ts
└── utils/             # 辅助函数
```
