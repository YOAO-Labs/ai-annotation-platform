<div align="center">

<img src="./apps/web/public/ai-annotation-platform-icon.svg" alt="AI Annotation Platform" width="112">

<h1>AI Annotation Platform</h1>

<p><strong>A production-grade labeling control plane for image, video, point-cloud, and AI-assisted dataset operations.</strong></p>

<a href="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/ci.yml/badge.svg?branch=main"></a>
<a href="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/docs-deploy.yml"><img alt="Docs" src="https://github.com/yyq19990828/ai-annotation-platform/actions/workflows/docs-deploy.yml/badge.svg?branch=main"></a>
<img alt="React" src="https://img.shields.io/badge/React-18-61dafb?style=flat-square">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square">
<img alt="Python" src="https://img.shields.io/badge/Python-3.11+-3776ab?style=flat-square">
<img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-16-4169e1?style=flat-square">
<img alt="VitePress" src="https://img.shields.io/badge/docs-VitePress-42b883?style=flat-square">

<a href="https://yyq19990828.github.io/ai-annotation-platform/">Docs Site</a>
·
<a href="#快速开始">Quick Start</a>
·
<a href="#架构">Architecture</a>
·
<a href="./docs-site/api/">API</a>
·
<a href="./CHANGELOG.md">Changelog</a>

</div>

AI Annotation Platform 把项目管理、Data Manager、多模态标注工作台、AI 预标注、审核、导出、后台任务与可观测性放在同一条产品链路里。它不是一个只画框的 Demo，而是一个用于持续迭代标注生产系统的全栈仓库。

工作台支持按账号保存图片、视频和通用命令的自定义快捷键；类别直选使用 `1–9` 和 `0`，更多类别可搜索。使用方式见[工作台快捷键](./docs-site/user-guide/workbench/index.md#完整快捷键)，偏好读写契约见[认证与账号 API](./docs-site/api/guides/auth.md)。

## 工作台 AI 案例

图片标注工作台支持对已确认标注做[二次推理](./docs-site/user-guide/ai/current-task-inference.md)，由标注员、项目管理员或超级管理员使用。审核工作台隐藏相关入口，接口也拒绝审核员调用；人工审核修改保留。

<table>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>智能点 · Smart Point</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/smart-point.mp4">
        <img src="./docs-site/public/media/sam/smart-point-poster.webp" alt="智能点单击生成对象轮廓" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>智能框 · Smart Box</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/smart-box.mp4">
        <img src="./docs-site/public/media/sam/smart-box-poster.webp" alt="智能框框选生成多边形候选" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>Magic Box</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/ai/assisted-annotation.mp4">
        <img src="./docs-site/public/media/ai/assisted-annotation-poster.webp" alt="Magic Box 粗框自动收紧目标框" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>Exemplar</strong><br>
      <a href="https://yyq19990828.github.io/ai-annotation-platform/media/sam/exemplar.mp4">
        <img src="./docs-site/public/media/sam/exemplar-poster.webp" alt="Exemplar 示例驱动相似目标发现" width="100%">
        <br>▶ 播放视频
      </a>
    </td>
  </tr>
</table>

## 控制台能力

<table>
  <tr>
    <td width="33%" valign="top">
      <strong>Project & Labeling Ops</strong><br>
      项目、数据集、批次、任务分配、审核回退、个人通知与后台任务历史。
    </td>
    <td width="33%" valign="top">
      <strong>Image & OCR Workbench</strong><br>
      bbox、rotated bbox、polygon、polyline、keypoint、mask 与 OCR 标注链路。
    </td>
    <td width="33%" valign="top">
      <strong>Video Workbench</strong><br>
      <code>video_bbox</code>、<code>video_track</code>、关键帧、插值、outside 段、帧缓存与 chunk 服务。
    </td>
  </tr>
  <tr>
    <td width="33%" valign="top">
      <strong>3D & Multimodal Workbench</strong><br>
      点云 3D 框、点级分割、scene 时序、LiDAR 坐标归一化与相机投影联动。
    </td>
    <td width="33%" valign="top">
      <strong>AI & Model Ops</strong><br>
      Grounded-SAM-2、SAM 3、YOLO、ONNXTools、RapidOCR，以及交互预测、批量预标和视频追踪。
    </td>
    <td width="33%" valign="top">
      <strong>Review & Data Delivery</strong><br>
      Data Manager 项目概览、任务 / 对象 / 轨迹浏览、选定任务操作与成员绩效看板，<a href="./docs-site/user-guide/review/index.md#在视频画面标记问题">视频问题标记与上下文恢复</a>（源帧范围、对象、画布和时间窗）、审核反馈，以及 COCO、YOLO、DAVIS、MOT、KITTI、Multi-camera COCO、Point Mask 等导出。
    </td>
  </tr>
</table>

管理员可用[项目邀请](./docs-site/user-guide/superadmin/user-management.md)为新老账号指定目标项目；超级管理员可在[系统设置](./docs-site/user-guide/reference/settings.md)管理常用运营参数、查看生效来源并恢复部署默认。接口与并发语义见[系统设置 API](./docs-site/api/guides/system-settings.md)。

成员管理支持按项目、用户组、角色和账号状态分页筛选，统计与导出采用相同范围；批量邀请与分组逐项报告结果，批次派发前可核对负责人变化和任务负载。

项目管理的列表与卡片支持服务端分页，默认每页 20 个，搜索、筛选和页码可通过链接恢复。分页接口为 [`GET /api/v1/projects/query`](./docs-site/api/guides/projects.md#列表与分页)，原项目数组接口继续供 SDK 与项目选择器使用。

项目成员可在工作台阅读[标注指引](./docs-site/user-guide/projects/index.md#编写标注指引)及示例图；指引编辑和图片上传、删除由项目负责人或超级管理员管理。

平台把**账号身份（平台角色）**与**项目职责（项目角色）**分开：同一个员工可以在 A 项目标注、B 项目质检、C 项目只读，无须切换账号。项目访问与写入由项目成员关系和固定能力集决定，全项目导出收紧到质检员 / 负责人、自审按持久贡献者证据拒绝。设计见 [ADR-0075](./docs/adr/0075-platform-identity-vs-project-authority.md)，迁移、回滚与已签发 URL 限制见[员工项目角色迁移与回滚 runbook](./docs-site/ops/runbooks/project-role-migration.md)。

## 架构

<table>
  <tr>
    <td width="50%" valign="top" align="center">
      <strong>数据航线</strong><br>
      <a href="./docs/assets/readme/data-route.excalidraw">
        <img src="./docs/assets/readme/data-route.svg" alt="数据航线 Excalidraw 图" width="100%">
      </a>
    </td>
    <td width="50%" valign="top" align="center">
      <strong>系统架构</strong><br>
      <a href="./docs/assets/readme/system-architecture.excalidraw">
        <img src="./docs/assets/readme/system-architecture.svg" alt="系统架构 Excalidraw 图" width="100%">
      </a>
    </td>
  </tr>
</table>

## 快速开始

### 前置依赖

| 依赖           | 版本                       |
| -------------- | -------------------------- |
| Node.js        | `>= 22`                    |
| pnpm           | `>= 10`                    |
| Python         | `>= 3.11`                  |
| uv             | Python 包管理              |
| Docker Compose | 本地数据库、队列、对象存储 |
| pre-commit     | 可选，但推荐               |

### 本地启动

```bash
# 1. 安装依赖
pnpm install
cd apps/api && uv sync --extra test && cd ../..
pnpm codegen

# 可选：已安装 pre-commit 时启用本地 hooks
pre-commit install

# 2. 启动基础设施
docker compose up -d postgres redis minio mailpit

# 3. 初始化数据库
cd apps/api && uv run alembic upgrade head && cd ../..

# 4. 启动后端和前端
pnpm dev:api        # http://localhost:8000
pnpm dev:web        # http://localhost:3000

# 多 worktree 并行时可用一条命令启动当前 checkout 的前后端
pnpm dev:worktree   # 独立数据库/Redis/bucket，自动选择端口并配置代理
```

`dev:worktree` 为每个工作树的 `dev/test/e2e` 模式管理独立数据库、Redis、八个 bucket
及本地文件；共享本机 PostgreSQL/MinIO 实例，不修改共享数据库。默认从 API `8100`
和 Web `3100` 寻找空闲端口，可用 `--with-worker` 启动本工作树的后台任务消费者。
`doctor` 诊断、`stop` 停止、带精确确认值的 `reset/destroy` 重建或清理环境。
首次启动不复制已有账号和项目。详见[独立工作树开发环境](docs-site/dev/how-to/worktree-environments.md)。

需要演示数据时：

```bash
cd apps/api
PYTHONPATH=. uv run python scripts/seed.py
```

### 常用入口

| 服务          | 地址                       | 说明                            |
| ------------- | -------------------------- | ------------------------------- |
| Web App       | http://localhost:3000      | 标注平台主界面                  |
| Swagger UI    | http://localhost:8000/docs | 运行时 API 文档                 |
| Docs Site     | http://localhost:5173      | `pnpm docs:dev` 后打开          |
| MinIO Console | http://localhost:9001      | `minioadmin / minioadmin`       |
| Mailpit       | http://localhost:8025      | 开发邮件收件箱                  |
| Grafana       | http://localhost:3001      | `monitoring` profile 启动后打开 |

### 局域网生产部署

已有开发环境时，可使用独立 `docker-compose.lan-prod.yml` 复用 PostgreSQL、MinIO 和模型服务，生产账号、项目、数据集、队列和存储桶单独管理。前端提供局域网 HTTPS 3030，API 提供 HTTPS 8080；客户端首次导入内部 CA 公共证书。配置、初始化与回退步骤见[局域网生产部署](./docs-site/ops/deploy/lan-production.md)。

## 可选服务

```bash
# Celery 后台任务：通用队列、GPU / CPU 预标、导出、视频帧、通知等
docker compose up -d celery-worker celery-worker-maintenance celery-worker-gpu celery-worker-cpu celery-worker-export celery-beat

# GPU ML Backend 在叠加文件 docker-compose.ml.yml，按显存预算选择独立 profile
# Grounded-SAM-2：适合图片 SAM / DINO 与视频 tracker
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu up -d grounded-sam2-backend

# SAM 3：独立 GPU profile，需要 HF_TOKEN 与更高显存
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-sam3 up -d sam3-backend

# 其它可选后端：YOLO、ONNXTools、RapidOCR
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-yolo up -d yolo-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-onnxtools up -d onnxtools-backend
docker compose -f docker-compose.yml -f docker-compose.ml.yml --profile gpu-rapidocr up -d rapidocr-backend

# Prometheus + Grafana
docker compose --profile monitoring up -d prometheus grafana
```

修改 `apps/api/**` 下的 worker 业务代码后，Celery 不会热重载，重启 worker 即可；修改依赖、Dockerfile 或 compose build 配置才需要 rebuild。开发 worker 不自动迁移数据库；更新包含迁移时，应先按上面的初始化命令显式迁移，再启动 worker。

## 开发工作流

```bash
# 全仓格式与静态检查
pnpm format:check
pnpm lint
pnpm typecheck

# 自动修复格式（提交前执行）
pnpm format

# 前端测试
pnpm test
pnpm test:e2e          # 需要 Postgres/Redis/MinIO；自启 3001/8010 与 annotation_e2e
pnpm --filter @anno/web test:e2e:visual  # 视觉基线（独立于默认功能集）
pnpm --filter @anno/web test:e2e:stress  # 完整布局压力矩阵

# 后端
cd apps/api && uv run pytest

# API 契约
pnpm openapi:export    # API 变更后刷新 snapshot
pnpm openapi:check     # CI 同款契约校验

# 文档站
pnpm docs:dev
pnpm docs:build
```

E2E 不复用开发环境的 `3000/8000` 或 `annotation` 数据库。测试路由
默认关闭，仅对显式开启且名称以 `_e2e` / `_test` 结尾的数据库开放。
详见 [E2E 运行说明](./apps/web/e2e/README.md)。

API 变更后同步跑 `pnpm openapi:export` 和 `pnpm codegen`；环境变量变更后同步更新 `.env.example` 并跑 `pnpm docs:gen-env-vars`。

Data Manager 将未解决问题与普通评论分别计数，兼容旧反馈筛选；成员绩效统计已保存、仍保留的图片与标注记录，不要求先提交任务。字段、统计口径与权限见[项目 API 指南](./docs-site/api/guides/projects.md)。

视频轨迹 API 保留旧关键帧缺失的来源字段，客户端应按「来源未知」处理；新建标注仍使用现有来源默认值，详见[任务与标注 API](./docs-site/api/guides/tasks-and-annotations.md)。

内部账号支持邮件恢复、按状态查询、逐项目职责交接、紧急停用与恢复。管理员确认交接时会重新核对接收资格及预览状态；恢复不会拿回已交接工作或恢复已撤销的凭据。接口与兼容字段见[认证与账号生命周期](./docs-site/api/guides/auth.md)。

工作台显示保存和本机待同步状态，并按账号保留离线操作；普通标注创建支持持久幂等键，避免重试生成重复对象。详见[任务与标注 API](./docs-site/api/guides/tasks-and-annotations.md#保存标注与提交任务)。

任务讨论 API 汇总原标注评论和原生任务留言，任务留言支持提及项目成员、项目负责人与启用的平台超级管理员（候选见 `GET /api/v1/projects/:id/mention-candidates`），图片任务留言还可附绘图；提供按标注 ID 批量统计评论数，图片和视频画布提示可通过默认开启的账号偏好关闭。聚合记录保留来源、附件与视频锚点；问题 API 提供根记录筛选、准确状态数量和历史回复分页，图片和视频像素问题均可关联对象，读写都检查项目与任务权限。详见[任务讨论与问题线程](./docs-site/api/guides/tasks-and-annotations.md#任务讨论与问题线程)。

问题回复、状态变化、任务留言和标注评论提及复用站内通知与按类型偏好，点击时重新核对权限并定位原对话。`GET/PUT /api/v1/notification-preferences` 按类型返回并更新「接收通知（in_app）」与「弹出提示（toast）」；PUT 至少提供其一，只原子合并请求出现的键，`toast` 读取为显式选择或类型默认计算出的有效值。讨论通知在业务提交成功后推送，网络推送失败不影响已保存内容，详见[讨论通知](./docs-site/api/guides/tasks-and-annotations.md#讨论通知)。

命名布局预设通过账号偏好 API 跨设备保存；`namedPresets` 采用带 revision 的整份 map 替换与省略删除语义，陈旧设备写入返回冲突，而 `contexts` 可在预设专用 PATCH 中缺席。安全更新方式与兼容规则见[账号偏好与命名布局预设](./docs-site/api/guides/auth.md#账号偏好与命名布局预设)。

工作台之外的全局界面偏好（主题、二次推理工具条显隐、登录后「本次更新」提醒的已确认版本）保存在账号偏好的 `ui` 子树并跨设备同步；`ui.changelog_seen_version` 为空表示从未确认，客户端仅在当前构建版本更高时提醒一次。契约见[账号偏好与命名布局预设](./docs-site/api/guides/auth.md#账号偏好与命名布局预设)。

图片 Polygon 和 Raster Mask 支持预览后原子切割与保留对象 ID 的撤销/重做。提交和恢复使用独立幂等请求，恢复受完整版本集与原切割后 30 天期限约束，Mask 支持保留全部像素的直线切割，撤销后的内容引用受资源清理保护。详见[Mask 切割](./docs-site/user-guide/workbench/mask-brush.md#直线切割为两个实例)、[Polygon 切割](./docs-site/user-guide/workbench/polygon.md#切割为两个对象)与[API 合同](./docs-site/api/guides/tasks-and-annotations.md#polygon-原子切割与恢复)。

SAM3 示例召回支持成对返回矩形框与原生 Mask，图片和视频当前帧均可分别采纳；客户端须保留 Mask 在完整候选数组中的索引。详见[交互式示例候选合同](./docs-site/api/guides/ml-backend.md#交互式示例候选)。

## 文档地图

| 角色 / 任务                | 入口                                                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| 标注员、审核员、项目管理员 | [docs-site/user-guide/](./docs-site/user-guide/)                                                                                |
| 本地开发                   | [docs-site/dev/tutorials/local-dev.md](./docs-site/dev/tutorials/local-dev.md)                                                  |
| 测试策略                   | [docs-site/dev/testing.md](./docs-site/dev/testing.md)                                                                          |
| API 文档                   | [docs-site/api/](./docs-site/api/)                                                                                              |
| Python SDK / CLI           | [docs-site/dev/sdk/quickstart.md](./docs-site/dev/sdk/quickstart.md)                                                            |
| ML Backend 协议            | [docs-site/dev/reference/ml-backend-protocol.md](./docs-site/dev/reference/ml-backend-protocol.md)                              |
| Data Manager               | [用户指南](./docs-site/user-guide/projects/data-manager.md) · [查询与校验契约](./docs-site/dev/reference/data-manager-query.md) |
| 视频帧服务                 | [docs-site/dev/reference/video-frame-service.md](./docs-site/dev/reference/video-frame-service.md)                              |
| 导出格式                   | [docs-site/user-guide/reference/export-formats.md](./docs-site/user-guide/reference/export-formats.md)                          |
| 部署与运维                 | [docs-site/ops/](./docs-site/ops/)                                                                                              |
| 架构概念                   | [docs-site/dev/concepts/](./docs-site/dev/concepts/)                                                                            |
| ADR                        | [docs/adr/](./docs/adr/)                                                                                                        |
| 变更记录                   | [CHANGELOG.md](./CHANGELOG.md)                                                                                                  |

部署版文档站：[https://yyq19990828.github.io/ai-annotation-platform/](https://yyq19990828.github.io/ai-annotation-platform/)

## 仓库结构

```text
ai-annotation-platform/
├── apps/
│   ├── api/                       # FastAPI 后端、SQLAlchemy、Alembic、Celery workers
│   ├── web/                       # React 18 + TypeScript + Vite 标注前端
│   ├── grounded-sam2-backend/     # Grounded-SAM-2 GPU ML Backend
│   ├── sam3-backend/              # SAM 3 GPU ML Backend
│   ├── yolo-backend/              # YOLO 检测 / 分割 / Exemplar ML Backend
│   ├── onnxtools-backend/         # ONNX 检测与属性推理 ML Backend
│   ├── rapidocr-backend/          # OCR 检测 / 识别 / 端到端 ML Backend
│   └── _shared/                   # Backend runtime、协议与 mask 工具共享包
├── docs-site/                     # VitePress 用户 / 开发 / API / 运维文档
├── docs/
│   ├── adr/                       # 架构决策记录
│   ├── changelogs/                # 历史版本变更
│   ├── plans/                     # 实施计划
│   └── research/                  # 调研报告
├── infra/                         # Docker、Prometheus、Grafana、Nginx 等基础设施配置
├── scripts/                       # OpenAPI、docs impact、seed、维护脚本
├── docker-compose.yml             # 基础栈（postgres/redis/minio/celery + 监控 profile）
├── docker-compose.ml.yml          # GPU ML Backend 与截图协议 stub 叠加
├── pnpm-workspace.yaml
└── package.json
```

## 技术栈

| 层   | 技术                                                                             |
| ---- | -------------------------------------------------------------------------------- |
| 前端 | React 18、TypeScript、Vite、TanStack Query、Zustand、Konva、Three.js、Playwright |
| 后端 | FastAPI、Pydantic、SQLAlchemy 2、Alembic、Celery、pytest                         |
| 数据 | PostgreSQL 16、Redis 7、MinIO / OSS、DuckDB 分析视图                             |
| AI   | Grounded-SAM-2、SAM 3、YOLO、ONNX Runtime、RapidOCR、开放 ML Backend 协议        |
| 文档 | VitePress、Mermaid、OpenAPI / Scalar、ADR                                        |
| CI   | GitHub Actions、docs impact、visual regression、OpenAPI snapshot check           |

项目开工检查和员工指南确认的 API 见[项目 API 指南](docs-site/api/guides/projects.md#开工状态)，操作说明见[快速开始](docs-site/user-guide/getting-started.md)与[项目设置](docs-site/user-guide/projects/index.md#开工准备与项目设置)。

[成员管理](docs-site/user-guide/superadmin/user-management.md)支持服务端分页筛选、同范围导出、批量邀请和分组，以及角色影响预览；[批次分派](docs-site/user-guide/projects/batch.md#分配人员)确认前展示负责人变化与任务负载。

## 贡献前检查

- 只改和当前目标直接相关的文件。
- 前端样式使用 `apps/web/src/styles/shadcn.css` 的 `--sc-*` 运行时 token 或 Tailwind 语义类，不再读取旧 `--color-*` 变量。
- API、环境变量、用户可见行为变更时，同步更新相关 docs。
- 提交前至少跑和改动范围匹配的 lint / test / OpenAPI / docs 校验。

## License

MIT
