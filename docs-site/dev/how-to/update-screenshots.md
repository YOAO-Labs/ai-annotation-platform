---
title: 更新文档截图
description: 如何新增场景、更新现有截图、维护视觉回归基线
audience: [dev]
type: how-to
status: stable
last_reviewed: 2026-10-22
---

# 更新文档截图

本平台采用 Playwright 自动化产出文档图片，支持矩阵化截图（多视口 / 多主题）、
元素级裁切、SVG 注释叠加、网络状态 mock 以及流程录制（GIF/WebM）。

截图与标准录制使用软件 WebGL 支持 3D 场景，同时禁用加速 2D Canvas，避免调整画布尺寸后残留旧标注像素。
修改浏览器启动参数后，可运行不依赖 API 或截图数据库的绘制检查：

```bash
pnpm --filter @anno/web exec playwright test --config=playwright.screenshot-canvas.config.ts
```

## 高清母版统一采集与衍生

正式视频素材统一先采集合格的高清营销母版，再从同一母版衍生文档视频、GIF、首页视频和封面。
ML 与非 ML 流程遵循相同规则，不再单独录制低规格文档源。静态截图仍按截图矩阵独立采集。

录制机器不必也是 AI 推理机器。入口将场景所需的模型能力与采集规格分开：

```bash
# 仓库根目录：查看已登记场景、只预览依赖（不连接服务或写库）
pnpm --filter @anno/web screenshots:record -- --list
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --plan

# 准备好下文的隔离环境和素材缓存后采集高清母版（默认 marketing）
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw
pnpm --filter @anno/web screenshots:record -- --flow ocr-inference
pnpm --filter @anno/web screenshots:record -- --flow sam-tool-smart-point

# 流程预演：不生成视频或正式源归档
pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --profile docs --validate-only
```

默认 `marketing` 规格在 Mac 选择原生 ScreenCaptureKit，在 Linux 使用 X11/NVIDIA。
母版归档在 `.artifacts/marketing/<run-id>/`，保留来源提交、实际采集尺寸、4K60 输出参数、
重采样方式、帧率校准和哈希。4K60 是输出规格；现有 Linux / Mac 采集内容区分别为
2592×1458 / 2880×1620，按既有质量契约重采样，不能声称原生像素 4K。
录制不会自动替换已发布素材；后续统一使用 `pnpm docs:media:derive` 的默认 `marketing` 等级。

`docs` 只用于显式 `--validate-only` 预演，不生成正式录像；旧 `screenshots:flows` 命令也只预演。
历史 `.artifacts/recordings/` 标准源及其来源记录继续保留，只有明确要求复用旧素材时才使用
`--quality standard`，不得将其改标为高清母版。硬件或模型不可用时应解决阻碍，不自动降级采集。

Mac 的 `marketing` 规格仅接受**不依赖 ML 能力且不执行推理**的已注册流程；即使后端能力范围为空，
执行真实推理的 `ai-preannotate`、`pipeline-apply-project` 也会被拒绝。`--plan` 会显示选中的
`captureDriver: screencapturekit`，并在连接服务、造数前检查流程范围。

Mac 需要 **macOS 14 或更新系统**、Xcode Command Line Tools（`xcrun swiftc`）、
带 `libx264` 的 ffmpeg 和 ffprobe，以及能够提供 2880×1620 内容区的 Retina 窗口。
为运行命令的终端或 Agent 在「系统设置 → 隐私与安全性 → 屏幕录制」授权，按系统提示重启相应应用。
首次启动会编译原生 helper 并缓存在被 Git 忽略的 `.artifacts/marketing/.native/`，之后按源码和工具链复用。
录制时保持目标 Chromium 窗口在前台，不切入台前调度缩略状态；原生窗口须通过进程、标题和校准色块核对。
不依赖 ML 的 Mac 录制校验隔离 API / 数据库，不要求额外启动 ML worker。

定时息屏也会让采集窗口失去有效可见边界。需要保持显示器开启时，让临时防休眠覆盖整个命令（含预检），
而不是等开始写帧后才启用；命令退出即释放，不修改永久系统设置：

```bash
caffeinate -dimsu pnpm --filter @anno/web screenshots:record -- --flow bbox-draw --profile marketing
```

入口必须设置 `SCREENSHOT_DATABASE_URL`，以及完全相同、显式选择非零 Redis DB 的
`REDIS_URL` 和 `CELERY_BROKER_URL`。同时核对 API、Worker 的实际目标；入口只固定自己子进程的
`DATABASE_URL` / `MIGRATION_DATABASE_URL`，不会改已运行服务。真实推理检查隔离 broker 上只存在
`screenshots@` worker。每次运行及场景间会修复自己管理的截图夹具并重读目录，不能与其他录制共享该库并发运行。

手工场景使用 `--backend-requirements none`，不探测或启动模型；OCR 只要求 `ocr`，SAM 工具使用
`image_interactive` 能力组（包含 exemplar），多个 `--flow` 合并依赖。入口只使用真实后端，缺失时失败，不自动回退 stub。
`candidate-keyboard-review` 同样要求 `image_interactive`，并额外核验 `sam3-segmentation`。它通过正式预标注 API 和隔离 worker 生成车辆轮廓，保留模型原始置信度、候选索引及源清单中的 `inference_evidence`，不注入固定候选。录制会检查逐项采纳／拒绝请求和标注保存结果；模型未返回足够候选时直接失败。

视频范围刷选、跨帧多正点、正负点、框种子和文本发现录制使用 `video_tracker` 能力组，分别对应 `video-tracker-range`、`video-tracker-cross-frame-points`、`video-tracker-positive-negative`、`video-tracker-box-seed`、`video-tracker-text-discovery`。这些流程运行真实追踪作业，并保存请求、候选、采纳及重载验证证据；文本发现按返回几何匹配目标，不依赖固定实例编号。失败时也会按已登记 ID 清理作业和新建标注。

Mac 可用 `--profile docs --validate-only` 预演已注册的远程 Linux 模型服务；正式 ML 母版目前使用 Linux 采集：API / Worker 必须能访问模型，模型也必须能访问截图存储的签名媒体 URL。
`--ml-backend-url` 是 stub 配置，不是远程 live 后端地址。仅改 API URL 不会把本地 seed / 清理脚本搬到远端。

本次拆分的是 **AI 后端依赖**，完整截图素材缓存、MinIO、固定项目及 nuScenes 数据仍须准备；入口使用 offline seed。
`--list` 同时列出规格限制；点云 billboard、相机种框和跨帧链场景虽不依赖 ML，仍要求原有硬件 WebGL/60Hz 运行面，只接受 `marketing`，不静默跳过。
大图场景另需大图夹具。旧命令不传范围时仍完整校验。`--list` 外的复杂编排、追踪流程继续使用原入口；
新增场景须先在 `apps/web/e2e/screenshots/recording-plan.mjs` 声明依赖，不能把 mock 推理流程归类为真实 AI 证据。
`--validate-only` 不产视频，但仍准备和修改隔离夹具。

### 检测在线训练的私有演示

`detection-online-training` 展示数据冻结、YOLO11n 真实训练、训练／验证四张指标图、模型回流、
加载试用、默认模型预标、人工采纳与微调，以及用新数据版本和项目权重开始第二轮训练。
它需要专用数据库、队列、存储桶、训练执行器和 YOLO 推理实例；不要将执行器接到日常开发库。
专用 worker 必须消费 `export`（数据构建）、`ml`／`ml.cpu`（推理）、`default` 和 GPU 控制队列，
并运行同一隔离 Redis 上的 Celery beat，使推理实例的健康记录持续更新。
先完成 screenshots seed，再在同一隔离环境运行 `seed.py --profile online-training --offline`，
注册并为 COCO8 在线训练项目启用独立 YOLO 实例，准备对应训练目录和基础权重。
随后运行 `apps/api/scripts/screenshot_detection_training_fixture.py`，从截图目录解析真实 CC0 道路图片，
新增一张无标注、无预测的待标图片。该图片不参与首轮训练，由视频中的真实回流模型产生候选。
数据及容器由本次录制环境统一管理，录制完成后删除本次专用环境和存储桶，保留母版及来源记录。

显式选择此流程录制，避免全量预演意外启动训练：

```bash
pnpm --filter @anno/web screenshots:record -- --flow detection-online-training --headless-display

# 私有交付：同一合格母版派生 1080p MP4 和封面，不修改公共素材或其清单。
pnpm docs:media:derive -- --run <run-id> --asset detection-online-training \
  --format video --private-output .artifacts/demos/detection-online-training
```

COCO8 仅用于此处的私有开发演示，不自动发布到公共文档站。真实训练和推理成功只证明该演示流程，
不代表业务精度提升，也不代替目标 GPU 部署的容量、故障和生命周期验收。

审核退回录制使用正式的两步原因／补充说明对话框与真实退回 API。截图 seed 会为完整受管标注记录作者、
提交者和当前审核轮次的贡献者证据；清理退回结果时恢复该夹具的审核快照，不绕过「未知贡献者」或自审校验。
旧 seed 缺少证据时先运行 `--repair`，不要把角色分派当作标注历史。

S3 连接器演示从现有 `MINIO_ENDPOINT`、`MINIO_DATASETS_BUCKET` 读取隔离存储目标，使用受管
`coco8-dev/train/` 的 4 个样本和演示凭据。API 进程必须设置 `CONNECTOR_ENCRYPTION_KEY`；
通过超管连接器主机白名单显式批准实际可达的存储主机。Mac 宿主机采集不能假定 Linux Docker
网关可达，也不能把 loopback／link-local 加入白名单来绕过硬阻断。只在隔离库和进程内配置，
不要把真实密钥或机器路径写入素材、清单或版本库。

交互校验和录制期间不要编辑当前工作树或运行生成源码的命令：Vite 会对 E2E 流程脚本的变化
触发整页重载，破坏正在执行的导航和画面就绪检查。并行修改应留在代理的独立工作树，整合后再采集。

## 快速运行

截图自动化使用专用 `annotation_screenshots_test` 数据库和 `3001/8010`
端口，不复用日常开发的 `annotation` 数据库或 `3000/8000` 进程。

### 1. 准备、迁移并填充专用库

下例为全量截图 seed。仅更新非 ML 静态截图时，在准备截图库时显式指定
`--backend-requirements none`；这会跳过 backend 发现与绑定，但仍创建并校验 screenshots profile
中的用户、项目、数据集、任务、批次和真实媒体夹具。完整截图仍使用不带该参数的全量 seed。
两种模式都使用隔离数据库、Redis DB 和迁移配置：

```bash
docker compose up -d postgres redis minio

cd apps/api
export SCREENSHOT_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test
export MIGRATION_DATABASE_URL="$SCREENSHOT_DATABASE_URL"
export REDIS_URL=redis://localhost:6379/15
export CELERY_BROKER_URL="$REDIS_URL"
DATABASE_URL="$SCREENSHOT_DATABASE_URL" uv run python scripts/prepare_e2e_db.py
DATABASE_URL="$SCREENSHOT_DATABASE_URL" uv run alembic upgrade head

# 非 ML 静态矩阵：无需 ML Backend 或 ML worker
DATABASE_URL="$SCREENSHOT_DATABASE_URL" PYTHONPATH=. uv run python scripts/seed.py \
  --profile screenshots --repair --ml-backend-mode live --backend-requirements none

# 若要运行完整静态矩阵（含 ML 场景），使用全量 backend seed 并准备相应服务：
# DATABASE_URL="$SCREENSHOT_DATABASE_URL" PYTHONPATH=. uv run python scripts/seed.py \
#   --profile screenshots --repair --ml-backend-mode live
```

`prepare_e2e_db.py` 可重复执行，且会拒绝库名不以 `_e2e` 或 `_test` 结尾的目标。显式
`none` 不连接 ML backend；不带 `--backend-requirements` 仍按全量 profile 检查所有 backend。

截图 seed 在夹具提交后同步初始化真实 DuckDB 分析视图，不依赖定时任务或 `cleanup` 队列。
连接 PostgreSQL `_test` / `_e2e` 库且未显式配置 `DUCKDB_PATH` 时，seed 与 API 按当前 checkout、
数据库主机、端口和库名派生同一个 `.artifacts/analytics/<hash>.duckdb` 私有文件。
请从同一 checkout 使用同一数据库启动两者；若显式配置 `DUCKDB_PATH`，则所有相关进程必须使用
同一个专属绝对路径。截图 seed 会在写入前拒绝开发默认分析文件。完整矩阵与非 ML 矩阵均检查
四个真实面板的健康状态；测试结束后仅移除本次私有 DuckDB 文件及其日志，不删除共享分析数据。

### 2. 启动专用 API、Worker 和 Web

在三个窗口分别运行。API 与 Worker 必须连接同一个截图数据库和独立 Redis DB；
不要让截图作业与日常开发 worker 竞争同一条队列。

```bash
# 窗口 A：数据库必须与建库、迁移、seed 完全一致；Redis DB 15 只供截图自动化使用
cd apps/api
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test \
  REDIS_URL=redis://localhost:6379/15 \
  E2E_SEED_ENABLED=true ENVIRONMENT=development \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8010

# 窗口 B：异步 AI 流程必须由连接同一截图数据库的专用 worker 执行
cd apps/api
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/annotation_screenshots_test \
  REDIS_URL=redis://localhost:6379/15 \
  E2E_SEED_ENABLED=true ENVIRONMENT=development \
  uv run celery -A app.workers.celery_app worker -l info \
    -Q ml,ml.cpu,gpu,default,media --concurrency=2 --hostname='screenshots@%h'

# 窗口 C：Web 只代理到上面的专用 API
cd apps/web
API_PROXY_TARGET=http://127.0.0.1:8010 PORT=3001 pnpm dev --host 127.0.0.1
```

`E2E_SEED_ENABLED` 仅对这个专用 API 进程临时开启。路由还会在数据库会话中
验证 `_test` 后缀，不要将 API 命令的 `DATABASE_URL` 改为开发库。
专用 Worker 故意不消费 `export` 队列；后台导出录制夹具会先从隔离的 Redis DB 15
确认并移除 API 投递的导出消息，再对截图数据库直接执行 worker 主体；清理步骤也会
排空该隔离队列，避免之后接入导出 Worker 时执行已删除作业的陈旧消息。
营销录制启动器还会检查 `/health`：只有截图 API 能看见 `screenshots@…` worker，
且看不见任何日常 worker 时才会开始录制。这个门禁可以在录制前发现数据库或 Redis
隔离错误，避免项目预标注被错误 worker 取走后一直停留在“运行中”。

### 3. 运行截图矩阵

```bash
cd apps/web
export PLAYWRIGHT_BASE_URL=http://127.0.0.1:3001
export PLAYWRIGHT_API_BASE=http://127.0.0.1:8010

pnpm screenshots                  # desktop-light 全量（最常用）
pnpm screenshots:dark             # desktop-dark 变体
pnpm screenshots:matrix           # desktop-light / dark / mobile
SCREENSHOT_STATIC_PROFILE=non-ml pnpm screenshots:matrix # desktop-light / dark / mobile，无 ML 服务
pnpm screenshots:flows            # 流程预演，不生成正式录像
pnpm screenshots:marketing        # Linux 原有全量母版入口；Mac 使用 screenshots:record 选择流程
pnpm screenshots:regression       # 比较高价值视觉回归基线
pnpm screenshots:regression:update # 有意改变 UI 后更新基线
pnpm screenshots:lint             # 快速检查静态引用与 manifest

# 开发/验证场景：执行真实导航和 locator 校验，但不写 PNG/manifest
SCREENSHOT_VALIDATE_ONLY=1 pnpm screenshots
SCREENSHOT_VALIDATE_ONLY=1 SCREENSHOT_STATIC_PROFILE=non-ml pnpm screenshots:matrix

# 首页 Hero 静态卡片源图更新后，按本轮范围生成轻量 WebP
pnpm --filter @anno/docs-site media:home-hero --asset video-track --asset pointcloud --asset review
```

静态矩阵的 `SCREENSHOT_STATIC_PROFILE=non-ml` 会把运行限制在 `desktop-light`、`desktop-dark`
和 `mobile`，并向只读 seed catalog 请求 `backend_requirements=none`。它不启动或调用 ML 服务，
不改成 mock；项目、任务、批次和真实图片 / 视频媒体检查仍 fail-closed。该 profile 只与截图矩阵
配合，不能和 `SCREENSHOT_BACKEND_REQUIREMENTS`、录制 flow/profile 混用；未知 profile 会直接失败。

非 ML 选择保留所有未声明 ML 依赖的静态场景。`fixture.backend` / `fixture.capabilities`
自动将场景归为 ML；只展示模型 / backend / 推理 UI、但不声明 fixture 依赖的场景必须设置
`requiresML: true`。每次运行会输出完整选中与排除的 scene ID；被排除场景不会被 mock 或作为
Playwright skipped 测试运行。当前排除：
`ai-pre/stepper`、`ai-pre/history-search`、`wizard/step4-backend`、
`projects/ml-backends/register-form`、`projects/ai-pre-config-panel`、
`projects/prediction-import-wizard`、`projects/prediction-purge-modal`、
`sam/smart-point-toolbar`、`sam/interactive-toolbar`、`sam/magic-box-toolbar`、
`sam/exemplar-output-mode`、`sam/ai-inspector-panel`、`workbench/video-real-scene`、
`workbench/video-ai-tracking-panel`、`workbench/ocr-real-scene`、
`workflows/failed-prediction-recovery-jobs-list`、`superadmin/model-market/list`、
`superadmin/ml-backend/register-form`。

默认全量截图仍会严格验证其声明的 backend / capability，原有 live / stub 使用规则不变。
静态非 ML profile 不会改写被排除场景的 backend 要求，也不会以协议 stub、
假推理结果或空态替代它们。

### 录制高清营销母版

`screenshots:record -- --flow <id> --profile marketing` 复用现有 flow 和隔离数据库，并使用
`marketing-master` project。两种平台保持相同的 1440×810 逻辑 viewport：Linux 使用 1.8 设备像素倍率，
内容区为 2592×1458；Mac 使用 2 倍率，内容区为 2880×1620。两者均以 Lanczos 生成
3840×2160 / 60fps 交付母版，保留真实源尺寸及重采样记录，不把交付尺寸称作原生 4K 采集。

录制前用校准色块测量 Chromium 内容区，不依赖固定的窗口装饰高度。Linux 使用 X11 / NVIDIA；
Mac 使用 ScreenCaptureKit 捕获已核对的单个窗口并写入 H.264，首帧时间取自原生帧时间戳，
不使用进程启动或回调到达时间估算。原生进程在正常停止、管道关闭和中断时结束编码；
调用端在正常结束或流程失败后清理自己的临时文件。录制期间窗口尺寸改变会中止采集。

按核心动作窗口裁切后的 4K MKV/H.264 采集源和 MP4/H.264 通用母版都归档到
`.artifacts/marketing/`，录制不直接覆盖已发布媒体；站点视频和海报继续通过现有派生命令生成。

每个营销目标必须在[高清营销资产目录](../reference/marketing-asset-catalog.md)
登记后才能归档。一个 `asset_id` 对应一个独立测试和一个明确主题；不要把无关功能混在
同一段录制中。包含登录、画框、提交和导入标注的 `e2e-quickstart` 是文档组合教程，营销 project
会显式跳过它。归档器会用 `ffprobe` 强制检查两份视频的 3840×2160、60fps，并检查两份母版的
最短/最长时长、编码和非全黑画面；任一门禁不符合时录制直接失败。MKV 与 MP4 使用相同的绝对动作时间窗口，
都会去掉动作窗口外的准备画面；`project-onboarding` 从登录页开始计入动作窗口，完整保留登录、创建项目和进入工作台。
未裁切缓存只在生成期间存在并在结束时删除。核心动作短于规格下限时，
不得向前回填登录或加载画面，也不得通过慢放、故意放慢操作或无意义重复动作满足时长门禁；应补齐与主题相关的必要镜头，
或修正该资产的最短时长。

两种平台都要求 Chromium 硬件合成；Linux 额外需要本机 X11 显示、ffmpeg 的 X11 采集与 `h264_nvenc`。录制器会在正式流程前
测量 120 个 `requestAnimationFrame`，并录制 1.1 秒 GPU 合成层校准动画后解码检查有效帧节奏：软件渲染、
GPU 合成未启用、p95 帧间隔超过 20ms、校准段少于 58 个采样帧、有效独立画面低于 55fps，
或独立帧占比低于 90% 都会直接失败。拖拽路径逐帧使用受信任的 Playwright 鼠标事件按 60Hz
节拍发送，避免浏览器指针状态与 Konva 拖拽状态脱节而在松手时跳到终点。
Linux 桌面至少需要 2700×1750，以容纳 2592×1458 内容区和浏览器边框；
`--resize-display` 会临时调整 X11 framebuffer，并在成功、失败或中断后恢复原尺寸；Mac 不使用该选项。

无显示器的 Linux NVIDIA 主机可使用同一录制入口的 `--headless-display`：

```bash
pnpm --filter @anno/web screenshots:record -- --flow sam-tool-smart-point --headless-display
```

该选项在数据库准备前启动独立 NVIDIA Xorg，使用未占用的 display、独立 seat、3840×2160 虚拟屏幕、
临时 Xauthority 和 Metacity 窗口管理器（关闭额外桌面合成）。需要当前用户已有 GPU 设备访问权，以及
`/usr/lib/xorg/Xorg`、`nvidia-smi`、`xauth`、`xdpyinfo`、Metacity 和支持 NVENC、libx264、x11grab 的 ffmpeg/ffprobe。
默认选择 `nvidia-smi` 报告的第一个 GPU；可通过 `MARKETING_HEADLESS_GPU` 指定其 PCI bus ID。
可用 `MARKETING_FFMPEG`、`MARKETING_FFPROBE` 指定编码工具；默认验证 PATH 中的工具，不符合时尝试 `/usr/bin`。
入口仅给 ffmpeg/ffprobe 添加临时 PATH 链接，保留 Node/pnpm 路径。
专用 Xorg 会话关闭屏保和 DPMS，避免长时间准备夹具时自动关闭采集显示输出。
它不修改系统 Xorg 配置、不重启登录管理器、不改设备权限，也不影响已有桌面；结束或中断时清理自己启动的进程组和临时文件。
`--list`、`--plan` 保持只读。该选项仍执行相同的硬件合成和有效帧率门禁；不能用 Xvfb 或软件渲染替代。

Linux 原有全量入口仍可定向选择测试；Mac 使用前述 `screenshots:record -- --flow ... --profile marketing`：

```bash
cd apps/web
MARKETING_CAPTURE_DISPLAY=:0 \
MARKETING_XAUTHORITY=/run/user/1000/gdm/Xauthority \
pnpm screenshots:marketing -- --resize-display --grep "bbox-draw"
```

每次命令使用独立的 `<UTC 时间>-<commit>` 目录。批次身份在启动 Playwright 前就已固定；
即使某项失败导致 worker 重启，后续成功资产仍写入同一份 manifest，不会被拆成多个伪批次：

```text
.artifacts/marketing/<run-id>/
├── manifest.json
├── raw/
│   └── <asset-id>/
│       └── <sha256>.mkv
└── masters/
    └── <asset-id>/
        └── <sha256>.mp4
```

`manifest.json` 记录源 commit、工作树是否有未提交修改、seed revision、浏览器，
以及 `capture` 中的驱动、采集源尺寸、4K 交付尺寸、重采样方式、逻辑 viewport、设备像素倍率、60fps 合同，
以及校准段时长、采集帧数、独立帧数、有效独立帧率和独立帧占比；
`files.capture_source` 和 `files.universal_mp4` 记录各自的 codec / 分辨率 / 帧率 /
时长、字节数、SHA-256 和预留 `storage_key`。每个 entry 的 `content` 还会固化资产标题、主题、目标、时长策略、
分镜和剪辑提示，作为后期 Agent 的机器可读交接信息。初始 `review_status` 固定为
`pending`；对外上传或剪辑前必须人工检查敏感信息、个人数据、模型结果和画面构图。
`source_clip_seconds` 会同时记录 MKV 与 MP4 相对未裁切缓存的起点和请求时长；
两份可交付母版都只保留已稳定页面中的核心动作窗口。

工作台流程必须使用 seed catalog 中经过复核的 `normalized_media` 锚点，并根据图片或视频在 stage
中的实际渲染矩形换算坐标，不能用包含 letterbox 的整个容器边界。人工工具在结束前必须选择锚点声明的
精确类别，并等待 annotation 创建/更新接口成功；AI 工具还必须录全真实目标提示、候选、人工采纳和落库结果。

`.artifacts/marketing/` 整体被 Git 忽略，是待同步的本地私有母版区，不是备份。
在 Google Drive / R2 / S3 上传和摘要校验尚未完成前，不要删除该运行目录。录制命令不读取
也不保存对象存储凭证。

Playwright 在整次运行开始时只做一次严格 catalog 预检，并把同一逻辑键快照提供给
desktop-light、dark、mobile 和 regression project。运行期间不要 repair seed 或删除项目；
场景自己的正常读取不会触发跨 project 的第二次全库预检。

完整矩阵成功后，reporter 按原有完整清单重建 manifest；静态非 ML 等成功的部分矩阵只刷新本次
实际生成的 target 条目，并原样保留未选择的 ML / 手工条目及其旧哈希和 provenance。部分运行的根
清单标记 `partial-run`，根级 source commit / dirty 字段为空；新条目各自记录本次来源和工作树状态。
`SCREENSHOT_VALIDATE_ONLY=1`、失败或没有成功的运行不改写 manifest。不要把保留的旧条目描述为本次生成。

截图生成 provenance 不等于视觉 / 内容审核：自动化只证明场景执行和文件生成，不会更新
`docs-site/maintainers/media-reviews.json`、批准资产或制造人工 reviewer 记录。只有获授权的人工
逐张完成视觉、内容、隐私与敏感数据检查后，才可按文首媒体工作流登记审核；自动化结果或 Agent 抽查不能替代该审核。

不要在文档里手写 PNG、GIF 或视频数量。当前数量由静态 `manifest.json`、流程
`flow-manifest.json`、磁盘文件和文档引用实时计算；发布判断以这些来源的一致性为准。

## 目录结构

```
apps/web/e2e/screenshots/
├── scenes/                    # 场景声明（按功能分文件）
│   ├── _types.ts              # ScreenshotScene 接口定义
│   ├── auth.ts
│   ├── workbench-polygon.ts
│   ├── workbench-ai.ts
│   ├── workbench-media.ts
│   ├── projects.ts
│   ├── review.ts
│   ├── export.ts
│   ├── ai-pre.ts
│   └── index.ts               # 聚合导出
├── catalog.ts                     # fixture / backend capability 预检
├── catalog-runtime.ts             # 单次运行共享的 catalog 快照
├── environment.ts                 # 固定时钟、禁动画、资源 ready
├── global-setup.ts                # 整次 Playwright 运行的 fail-closed 预检
├── manifest-reporter.ts           # 按成功运行范围重建或局部刷新 manifest
├── _helpers/
│   ├── annotate.ts            # SVG overlay 注释
│   ├── mock-state.ts          # page.route 网络状态 mock
│   ├── portable-recorder.ts   # 标准源录像归档与 GIF 配置
│   ├── marketing-assets.ts    # 营销资产主题、分镜与时长规格
│   ├── marketing-external-recorder.ts # 4K60 外部采集与 GPU/刷新率门禁
│   └── marketing-recorder.ts  # 高清母版转码、归档与 manifest
├── flows/                     # 流程录制脚本
│   ├── e2e-quickstart.ts
│   ├── ai-prediction-import.ts
│   ├── ai-preannotate.ts
│   ├── ai-tracker-panel.ts
│   ├── sam-interactive.ts
│   ├── review-reject.ts
│   └── flows.spec.ts
├── regression/                # 视觉回归
│   ├── regression.spec.ts
│   └── __screenshots__/       # 基线截图（提交入 git）
├── outputs/
│   ├── manifest.json          # 提交入 git 的 v2 图片清单
│   ├── flow-manifest.json     # GIF、文档 MP4、首页视频与封面的生成来源清单
│   └── flows/                 # 旧临时目录，不再用于发布
└── screenshots.spec.ts        # 主 driver

.artifacts/marketing/             # 高清私有母版与运行 manifest（Git 忽略）
```

## 新增一个截图场景

### 1. 在对应类别文件中添加 scene

```typescript
// apps/web/e2e/screenshots/scenes/projects.ts
export const PROJECT_SCENES: ScreenshotScene[] = [
  // ... 已有场景

  {
    name: "projects/my-new-scene",
    role: "admin",
    fixture: {
      project: "image_demo",
      task: "annotating",
    },
    route: (catalog) => `/projects/${catalog.projects.image_demo.id}/settings`,

    prepare: async (page, catalog) => {
      await page.getByText(catalog.projects.image_demo.name, { exact: true }).waitFor();
      // 打开目标 tab / modal / 交互
    },

    // 截图模式（不填 = viewport）
    capture: { kind: "locator", selector: '[data-testid="my-panel"]', padding: 8 },

    // 自动注释（可选）
    annotate: [{ selector: '[data-testid="key-button"]', style: "rect-red", label: "点击这里" }],

    // 数据脱敏（可选，叠加到默认 mask 之上）
    mask: ["[data-testid='sensitive-info']"],

    // 矩阵（可选，不填只跑 desktop-light）
    matrix: { themes: ["light", "dark"] },

    target: "docs-site/user-guide/images/projects/my-new-scene.png",
  },
];
```

需要创建持久化示例时，在 `prepare(page, catalog, registerCleanup)` 中通过正式 API 创建受管夹具，
并在后续界面断言前注册清理函数。截图与视觉回归驱动均在 `finally` 中执行清理；数据组示例恢复原成员分组，
模板示例只删除本次创建且未被应用的记录。不要从历史审计日志推测待恢复状态，也不要删除同名但不属于本次采集的记录。

邀请加入录制会核对本次唯一邮箱与用户基线，结束或失败时移除新成员关系并停用本次注册的账号，随后回读停用状态；已有用户不受影响。讨论草稿录制在获得新建反馈 ID 后即登记 `finally` 清理，后续保存读回或界面断言失败也会删除该条反馈。

### 2. 定向验证场景

```bash
cd apps/web
# 先只验证，不写正式资产
SCREENSHOT_VALIDATE_ONLY=1 pnpm screenshots --grep "my-new-scene"
# 成功后只刷新本次生成的条目；未选择条目及旧 provenance 保留，根清单标为 partial-run
pnpm screenshots --grep "my-new-scene"
```

### 3. 在文档中引用

推荐使用 `<AutoImage>` 组件（自动显示「自动产出」badge）：

```md
<AutoImage src="projects/my-new-scene.png" alt="我的新场景" />
```

或普通 Markdown 图片：

```md
![我的新场景](../images/projects/my-new-scene.png)
```

### 4. 发布完整矩阵和 manifest

图片覆盖与孤儿检查同时识别 Markdown 图片、`<AutoImage>` 和文档主题 Vue / TypeScript
组件中的静态图片导入。仅供首页使用的截图仍需保留 manifest 与生成来源；没有任何已发布
页面或主题组件引用的图片会被孤儿检查拦截。

```bash
cd apps/web
pnpm screenshots:matrix
pnpm screenshots:lint

cd ../..
node docs-site/scripts/check-image-manifest.mjs --release
node docs-site/scripts/check-orphan-images.mjs --strict
pnpm docs:media:audit -- --release
```

只有 desktop-light、desktop-dark、mobile 三个 project 全部成功时，reporter 才会按完整矩阵
重建 manifest，并执行 legacy schema migration。成功的定向 / 部分运行会只刷新本次生成 target
的条目 provenance，保留所有未选择的资产、旧哈希和 provenance；根清单标为 `partial-run`，
不能当作完整矩阵新鲜度或 legacy migration 完成。失败、`--list` 和 `SCREENSHOT_VALIDATE_ONLY=1`
不会更新 manifest。每个自动条目记录 scene、源码文件、capture/fixture、seed revision、commit、
浏览器、视口/主题/语言、生成时间、SHA-256 和像素尺寸。`--strict` 检查现有资产四方一致性；
全量重拍交付前再用 `--release` 要求所有当前 scene 和 seed revision 都已更新。

人工审阅时至少检查每张 PNG 的主体内容、加载状态和敏感信息；GIF / MP4 / WebM 除首帧外
还要抽查核心动作和最终结果，并确认动效完整、体积合理。首页 WebM 与 MP4 fallback 还需检查对应 WebP 海报能独立说明
场景，主图不被浮动面板遮挡，且移动端与 `prefers-reduced-motion` 下不自动播放。Hero 源图更新后还要
重新生成派生 WebP。生成器先核对源图与截图清单的哈希，再把派生关系写入流程媒体清单；不传 `--asset` 时处理全部四张卡片。媒体审计也会读取主题组件中的静态图片导入，覆盖这些首页配图。源录像保留在 `.artifacts/recordings/` 或 `.artifacts/marketing/`；备份确认前不要删除。

若本机 ffmpeg 缺少 `libwebp` 编码器，首页静态图生成器会尝试已安装的 `cwebp`，沿用相同质量和像素尺寸；两者都不可用时保留已有成品并报错。

## 生成来源与人工复核版本

生成来源和人工复核是两个独立事实：

- `manifest.json` 与 `flow-manifest.json` 记录素材在哪个 commit、seed 和录制流程上生成。
- `docs-site/maintainers/media-reviews.json` 记录素材最后一次完成视觉和内容复核的 commit、时间、哈希、复核人和关联路径。

先运行 `pnpm docs:media:audit` 查看 `broken / stale / review-due / current`。只有工作树干净、素材已经提交且人工检查完成后，才能执行：

```bash
pnpm docs:media:approve -- --asset docs-site/public/media/video/tracker-box-seed.mp4
pnpm docs:media:audit -- --release
```

批准命令拒绝在脏工作树上运行，避免把视觉结论绑定到不完整的 SHA。

文档检查按日常开发和阶段验收分开：

| 检查               | 触发时机                           | 检查范围                                             |
| ------------------ | ---------------------------------- | ---------------------------------------------------- |
| `Docs build`       | 每次面向 main 的 PR                | 文档构建                                             |
| `Docs validation`  | 相关文件变更的 PR，以及 main 推送  | 链接、生成文件同步、元数据、图片清单与媒体文件完整性 |
| `Docs media audit` | 每周一北京时间 11:17，也可手动运行 | 输出媒体待办报告，不因审核过期而失败                 |
| `Docs acceptance`  | 功能稳定、文档准备发布时手动运行   | 完整文档检查、全量截图清单、媒体来源及人工复核记录   |

`media:audit` 默认只生成报告；`--integrity` 只阻断引用文件缺失和文件与生成清单的哈希不一致，
允许人工复核缺失、到期或关联源码变化。`--strict` 还阻断已批准文件哈希变化和关联路径变化；
`--release` 进一步要求所有引用素材有生成来源、使用当前 seed、生成时工作树干净，并且人工复核未超过 30 天。
日常 PR 使用 `--integrity`，因此界面迭代不要求每次重新拍摄、批准所有旧素材。

功能稳定后，集中完成内容阅读与视觉审核、提交素材和真实复核记录，再在 GitHub Actions 中选择
**Release readiness → Run workflow → 待验收分支**。手动入口需要工作流先进入默认分支。
验收使用所选分支的提交，媒体报告记录其 SHA；修正问题后重新验收，发布前确认报告对应拟发布提交。
依赖安装成功后，各项验收独立执行：某项失败不跳过后续检查，整体仍判定失败；内部链接检查仅依赖生成链接目标成功。
媒体检查失败也会上传 `docs-acceptance` 报告，保留 30 天。工作流检查证据，不能代替人工阅读或视觉审核，
也不会自动重录、批准素材或发布版本。

定期巡检检查默认分支，报告为 `docs-media-audit`，保留 30 天。把报告中的问题纳入阶段待办即可，
不要求每周立即重拍全部素材。`Docs acceptance` 应作为阶段验收依据，不设为每个 PR 都必须执行的检查。

所有视频、GIF、视频封面都从私有源归档派生，静态截图保持独立：

```bash
pnpm docs:media:derive
# 指定高清批次和单个资产（支持只含部分资产的批次）
pnpm docs:media:derive -- --run <run-id> --asset video-chapter
# 兼容历史标准源：仅在明确要求复用时使用，先审阅并指定起点:时长（秒）
pnpm docs:media:derive -- --quality standard --run <run-id> --asset video-track --format video --clip 2:8
# 从高清母版派生已记录的 GIF 子片段
pnpm docs:media:derive -- --run <run-id> --asset bbox-draw --format gif
# 宣传文章复用相同的源校验、编码和来源记录
pnpm docs:media:derive -- --run /absolute/path/to/run --article 06-video-track --asset video-draw
```

`--quality` 默认 `marketing`，仍要求 4K60 文件参数与硬件采集／独立帧证据；历史标准源兼容必须显式使用 `standard`；新录制的正式素材统一使用高清母版。
`--run` 可以是所选等级下的批次名，也可以是包含 `manifest.json` 的绝对目录。
默认只处理该批次中实际存在、且支持所选格式的已登记资产，不要求整套资产齐全；GIF 批处理跳过没有 GIF 配置的条目。`--asset` 可重复指定，显式指定不支持的资产仍报错，未选文件和清单条目保留。

统一实现位于 `apps/web/scripts/media-derivation.mjs`，先检查源哈希、质量、路径和裁剪范围，再暂存全部编码结果，编码成功后才发布。
`--article` 选择文章的成对 GIF/PNG 配置，不能与 `--format`、`--gif-target` 混用；旧文章脚本保留为兼容入口，也使用同一实现。
文档视频最大 1280×720、30fps，标准源不放大、不补帧；首页可同时派生 VP9 WebM、H.264 MP4 与 WebP 封面。
默认 `--format all`；`video` 包含视频及封面，`gif` 使用归档保存的独立子片段配置。
标准源的所有输出（包括 GIF）都需要单资产的 `--clip start:duration`，显式窗口覆盖录制时的估算。
同一资产有多个 GIF 时，先用 `--format video` 生成主视频，再用 `--format gif --gif-target <已登记的仓库相对路径>` 分别指定各自的裁剪窗口。
例如视频章节的“创建”和“调整”不能被同一个覆盖窗口合并。营销源不指定覆盖窗口时使用归档内的 GIF 配置；录制器已将窗口换算到归档时间轴，并按实际结尾收敛尾部留白，派生时不能再次减去原始录制偏移。
旧营销清单没有 GIF 配置时仍可派生视频；要生成对应 GIF，须先重新录制，不能猜测窗口。

每个成品在 `flow-manifest.json` 中记录来源批次、资产 ID、源 SHA、质量等级、实际裁剪范围及派生哈希。
成品的推理声明沿用母版清单中的真实证据；历史未记录证据的源继续标为 `unverified`，不补造推理事实。
特定资产可在配方声明归一化隐私矩形和源时间区间；遮挡先在源帧坐标上应用于视频、GIF 和海报，再缩放，只在对应表单或列表阶段生效。海报与裁剪片段会换算到原始源时间，`source_asset.normalized_privacy_masks` 保留精确配方。连接器配方绑定已复核母版的 SHA，换源后必须重新检查坐标与时间，不能沿用遮挡假定。
该流程不生成或更新人工审核结论，也不自动重做已有发布资产。
文档正文继续使用 `<DocsVideo>`；组件先加载封面，进入可视区域后才挂载视频。短微交互才使用 GIF。

## `ScreenshotScene` 完整字段说明

| 字段        | 类型                                         | 说明                                                       |
| ----------- | -------------------------------------------- | ---------------------------------------------------------- |
| `name`      | `string`                                     | 唯一标识，用于 `--grep` 过滤                               |
| `role`      | `Role \| Role[]`                             | 登录角色（admin / annotator / reviewer）                   |
| `fixture`   | 可选对象                                     | 场景依赖的 catalog 项目、任务、批次、backend 和 capability |
| `route`     | `(catalog: ScreenshotSeedCatalog) => string` | 用 catalog 逻辑键构造页面路由                              |
| `prepare`   | 可选函数                                     | 截图前交互（打开 modal / 切 tab 等）                       |
| `capture`   | 可选                                         | 截图模式：fullPage / locator / clip                        |
| `annotate`  | 可选数组                                     | 自动红框 / 编号注释                                        |
| `mask`      | 可选 `string[]`                              | 额外脱敏选择器                                             |
| `mockState` | 可选                                         | 网络状态：empty / error / loading / rate-limited           |
| `matrix`    | 可选                                         | 矩阵维度：viewports / themes / locales                     |
| `target`    | `string`                                     | 输出 PNG 路径（相对仓库根）                                |

## 视觉回归基线更新

`Browser checks` 工作流（`.github/workflows/visual-regression.yml`）会在截图 seed 前安装 `ffmpeg`，用于准备视频素材，并在 MinIO 就绪后调用 `storage_service.ensure_all_buckets()` 初始化存储桶。seed 会先于 API 启动上传媒体，因此必须提前创建存储桶；MinIO 健康检查通过只表示服务可用。

该环境使用 ML protocol stub，不启动 Celery worker。API 启动后通过 `/health/db` 检查就绪；聚合 `/health` 还会检查 Celery，在这个环境中会因没有 worker 返回 503。

截图 profile 会同步准备它所管理的图片缩略图、视频海报和视频元数据，并校验存储对象，失败时终止 seed。普通 demo profile 仍使用异步媒体任务。截图任务使用稳定展示编号，项目、数据集和任务时间戳在所有种子写入完成后统一固定；保留的种子预测也固定创建时间，并保持导入元数据关联，避免数据库自增序列、更新时间和“最近活动”影响比较。

前端构建完成后使用 `pnpm preview --host 127.0.0.1 --port 3000 --strictPort`，保留 Vite 的 API、WebSocket 和 MinIO 代理。图片、视频场景等待画布可见与媒体就绪状态，避免开发服务器冷编译或固定休眠造成超时、空画布基线。

工作台租约使用真实浏览器时间；回归仅遮罩任务锁与分段租约的倒计时数值，保留锁状态和错误信息。预标配置回归显式选择 catalog 中协议 stub 的 `yolo-detect` 模型，缺失时失败。文档正式截图继续使用各自的真实 backend，因此不要混用文档图与 protocol stub 回归基线。

定时运行失败时，工作流使用 job 级别的 `issues: write` 权限创建通知 Issue，区分视觉测试失败、未完成，以及测试通过后的检查或清理失败。先检查失败步骤和日志；依赖或数据初始化失败不代表截图差异，不应通过更新基线处理。

当 UI 有意改变导致 regression 失败时：

```bash
cd apps/web
# 先在 protocol stub + screenshots seed 环境更新基线
pnpm screenshots:regression:update

# 无更新模式复跑，确认基线可稳定比较
pnpm screenshots:regression

# 查看 diff（在 test-results/ 目录）
# 确认无误后提交基线变更
git add e2e/screenshots/regression/__screenshots__/
git commit -m "chore(screenshots): 更新视觉回归基线 — <变更原因>"
```

也可手动运行工作流并设置 `update_snapshots=true`，下载 `regression-baseline-<run-id>` artifact，逐张确认 9 个场景的媒体、布局和控件完整后提交到上述目录。随后以 `update_snapshots=false` 再运行一次；更新模式通过本身不代表基线比较通过。保持既有 1% 像素差异阈值，不用扩大遮罩或放宽阈值处理实际 UI 变化。

## 手动维护的图

手工合成且没有可复现场景的图片使用 `auto: false`。自动 driver 在写文件前会拒绝覆盖
这类 target；完整矩阵和部分矩阵都保留手工条目，部分矩阵也不会把未选择的 ML 条目标为新鲜。
手工图片改变后必须同步更新哈希、尺寸和时间，再运行严格检查：

```json
{
  "schema_version": 2,
  "entries": {
    "docs-site/user-guide/images/<page>/<figure>.png": {
      "auto": false,
      "target": "docs-site/user-guide/images/<page>/<figure>.png",
      "note": "示意图需人工合成，自动化无法复现",
      "provenance": "manual",
      "sha256": "<64 位摘要>",
      "width": 1440,
      "height": 900
    }
  }
}
```

## 常见问题

**Q: seed/catalog 报项目、任务或 backend 缺失**
确认已启动 live backend 或截图协议 stub，然后重跑 `scripts/seed.py --profile screenshots
--repair --ml-backend-mode <live|stub>`。catalog 会在导航前报出具体逻辑键和缺失能力，
不会退化为任意项目。

**Q: locator 截图失败（元素找不到）**
检查 `data-testid` 是否正确，或在 `prepare` 里等待具体业务 ready selector。
locator capture 不会退化为整页图；元素缺失必须修复场景或产品问题。

**Q: 远程打开工作台一直显示「重连中」**
DEV 远程页面应连当前 `:3000` 同源的 `/ws`，由 Vite 升级并转发到 API。
不要把 Docker 默认 IP 或服务器的 `localhost:8000` 当成远程浏览器可达地址。
同理，媒体应走同源 `/minio`。详见 [WebSocket 调试](./debug-websocket.md)。

**Q: ffmpeg 不可用，GIF 无法生成**
安装 ffmpeg：`sudo apt install ffmpeg` 或 `brew install ffmpeg`，
或设置 `FFMPEG_PATH=/path/to/ffmpeg`。
营销母版还会调用同一安装包提供的 `ffprobe` 校验实际媒体参数。
统一派生同样需要 `ffprobe`；生成 WebP 时使用 ffmpeg 的 `libwebp` 编码器，缺少时可使用已安装的 `cwebp`。

**Q: 视觉回归误报（字体渲染差异）**
先确认 CI 与本机都使用 Playwright Chromium、固定时区/语言/DPR 和 protocol stub。
确有平台级渲染差异时，再在 `regression.spec.ts` 里对单个场景调整阈值，避免放宽全部基线。

正式录制中的推理耗时可能变化；逐项检查封面，不使用仍在等待的画面。可对单个资产传入 `pnpm docs:media:derive -- --run <run-id> --asset <asset-id> --format video --poster-at <seconds>` 选择已检查的画面。时间相对所选片段，必须位于片段内部，清单保存源视频中的实际取帧时间。

显式选定录制的整条测试预算至少为 15 分钟，覆盖隔离夹具重建、真实模型交互及母版编码；流程中的较短预算不会覆盖已经分配的准备预算。普通 E2E 时限、产品请求断言和母版质量门槛保持原有约束。Exemplar 的内容窗口包含真实保存后的重载与框选结果展示。
