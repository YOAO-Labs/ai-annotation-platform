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

## 数据集与存储

- **独立数据集工作台与质量复核**：提供独立数据集详情页、分页表格/网格、样本检查器、扩展名/尺寸/Scene 服务端组合筛选与个人动态视图，展示关联项目进度、标签分布并支持一键冻结当前筛选创建训练数据版本；内置整份数据集图片解码、尺寸范围与精确 SHA-256 重复检测及固定范围批量人工复核（[数据集工作台](./docs-site/user-guide/datasets/workbench.md) · [质量检测与复核](./docs-site/user-guide/datasets/quality.md) · [数据集 API](./docs-site/api/guides/datasets.md)）。
- **受控目录、外部 S3 / SFTP 原地引用与增量同步**：统一挂载根内可动态登记并按节点探测只读目录，支持受控目录、外部 S3 与只读 SFTP 原地引用或托管复制接入；来源支持周期扫描预检、增量差异核对、新修订逐项接受、同内容重新核验、同来源移动重定位及扫描规则调整（[数据来源与增量同步](./docs-site/user-guide/datasets/sources.md) · [数据集来源 API](./docs-site/api/guides/dataset-sources.md) · [存储位置 API](./docs-site/api/guides/storage-locations.md)）。
- **跨网络只读站点 Agent 与显式同内容备用来源**：跨网络目录通过只读站点 Agent 主动出站轮询中心，复用既有扫描、预检登记与原图快照流程，支持完整核验后的单区间 Range 读取、中心带宽预算、断网续传与离线元数据/缓存保留；引用图片可显式关联同内容备用来源并调整读取优先级（[跨网络站点与 Agent](./docs-site/user-guide/datasets/site-agents.md) · [站点 Agent 部署](./docs-site/ops/deploy/site-agents.md) · [站点 Agent API](./docs-site/api/guides/site-agents.md)）。
- **统一认证原件读取、视频分片租约与按需原图缓存**：图片工作台、审核、数据管理预览、点云与相机统一采用按保存位置完整核对字节的认证原件读取，支持独立任务原件上传封存与最大 512 MiB 通用二进制下载；视频原件经完整核验后以短期同源租约处理 HEAD/Range，视频派生媒体与图片金字塔各自固定独立输出归属，外部位置支持配额与 TTL 受控的按需完整原图缓存（[认证内容读取 API](./docs-site/api/guides/verified-content.md) · [视频派生媒体 API](./docs-site/api/guides/video-derivatives.md) · [图片金字塔架构](./docs-site/dev/concepts/image-pyramid-assets.md)）。
- **存储管理控制台与九用途受控切换**：集中管理存储位置、远端连接器（含修订冲突保护与凭据列举验证）与九用途映射，展示读取/新写入策略、用途代次、位置修订、节点探测健康与去重后的已采纳源索引规模；头像、报告附件、审计归档、导出、导入暂存、数据集原件、媒体缓存、标注文件与训练文件均支持真实写入验证与五分钟预览确认后切换新分配位置，历史与在途对象保留原保存地址（[存储位置与连接器](./docs-site/user-guide/datasets/storage-connections.md) · [九用途映射与受控切换](./docs-site/user-guide/datasets/purpose-bindings.md)）。
- **新托管原件条件创建、保留账本与准确版本受控回收**：新托管上传、ZIP、连接器复制与目录托管复制采用暂存直传隔离、条件创建与完整回读核验；数据集支持预览归档与恢复，外部位置支持访问排空维护与维护影响盘点；删除托管数据集或样本时同事务保留原件位置证据，支持保留期限、备份/迁移要求登记、后台完整内容核验、周期引用核对与准确版本受控回收（[生命周期与保留回收](./docs-site/user-guide/datasets/lifecycle-and-retention.md) · [原件访问与保留架构](./docs-site/dev/concepts/original-access-and-retention.md)）。
- **对象存储迁移、原生写入封闭与九用途原子切换**：提供锁定构建的双候选对象存储本地对比验证、配置源只读迁移清单、27 类已保存业务定位目录、持久初次复制与后续增量批次、迁移维护窗口、受控本机 MinIO 原生写入封闭与最终增量封存；支持发布已封存迁移副本的九用途准确版本读取映射、同事务原子切换九用途与带真实新写入的受控回滚，以及切换后的正向观察与旧实例退役手册（[对象存储迁移准备](./docs-site/user-guide/datasets/storage-migration.md) · [迁移清单与切换运维](./docs-site/ops/deploy/storage-migration-inventory.md) · [候选存储本地验证](./docs-site/ops/deploy/storage-candidate-poc.md)）。
- **六分类容量盘点、净变化增量账目、故障观测与隔离恢复**：后台按实际桶去重盘点九用途对象占用并按六分类展示持久快照，配套 26 类生产者的写入/删除净变化增量账目与周期对账；位置来源诊断提供当日 UTC 窗口原图不可读率、预览失败率、持久故障合并恢复记录、外部原件保护责任声明与任务真实推进观测；配套离线一致备份与隔离恢复演练及紧急受控只读应用回退（[存储诊断与容量账目](./docs-site/user-guide/datasets/storage-operations.md) · [一致备份与恢复演练](./docs-site/ops/deploy/storage-recovery.md) · [受控只读应用回退](./docs-site/ops/upgrade-guide.md#数据集存储的受控只读应用回退)）。

## 在线学习与 AI 预标注闭环

- **训练数据版本、冻结来源血缘与数据洞察**：项目按检测或实例分割维护独立版本流（支持原生 RLE 掩码桥接），支持从数据集组合筛选或原地引用图片直接预检并冻结独立训练包；提供类别/划分统计、尺寸热力图与跨版本对比，单列托管原件身份与引用来源关系变化，且训练包、保留图片与掩码固定独立存储归属（[训练数据版本](./docs-site/user-guide/online-learning/data-versions.md) · [训练数据与实验 API](./docs-site/api/guides/training-datasets.md)）。
- **受控训练执行器、模型家族与实际加载器成员回执**：独立 GPU 训练执行器支持 Ultralytics YOLOv8 / YOLO11 / YOLO26（n/s/m/l/x 检测与分割）与 LibreYOLO9（t/s/m/c 检测），提供完整原生参数目录、不可变训练配方、同项目候选权重初始化、YOLO11 原生增强预览、多实验对比与脱敏实时监控；新建实验固定输入成员合同，由原生加载器核对实际 train/val 成员并在执行 `manifest.json` 保存回执（[配置与启动实验](./docs-site/user-guide/online-learning/experiments.md) · [Ultralytics 容器](./docs-site/user-guide/online-learning/ultralytics.md) · [LibreYOLO 容器](./docs-site/user-guide/online-learning/libreyolo.md) · [成员回执部署验收](./docs-site/ops/deploy/docker-compose.md#5-实际训练成员回执的部署验收)）。
- **模型回流、谱系追踪与逐样本评估**：成功训练权重可注册为项目候选模型，完成类别绑定、认证推理实例加载与固定样本试用后发布为项目默认预标模型，分别记录训练版本与实际推理版本；支持完整来源与使用谱系追踪，以及固定最多 32 个验证成员的逐样本 TP/FP/FN 叠加评估、双候选对比与当前任务原件核对（[模型回流与默认预标](./docs-site/user-guide/online-learning/models.md) · [逐样本评估](./docs-site/user-guide/online-learning/sample-evaluation.md)）。
- **自动训练策略、有界参数搜索与主动学习选样**：支持按审核后训练内容变化、检查间隔与冷却时间触发的自动训练策略，最多 20 个显式网格组合与原生计算时间预算的参数搜索，以及固定未标注候选池、启发式建议与等量随机对照的主动学习人工选样，且预览、评估与主动学习各自固定输出存储归属（[自动训练](./docs-site/user-guide/online-learning/auto-training.md) · [主动学习选样](./docs-site/user-guide/online-learning/active-learning.md)）。
- **固定核验原件输入的 AI 预标注、交互推理与异步导出**：托管与原地引用图片的后台预标、ROI 管线、整图原生 Mask 分割预标、失败重试、同步交互式 SAM、选中框二次推理、批量 Mask 修复，以及托管视频整段预标均沿已登记原件完整核验并使用固定版本临时输入；图片、视频、点云（KITTI / Point Mask / 多相机 COCO / NuScenes）与原地引用图片的异步导出均固定输入身份、交付核验后的独立媒体副本并共用首次七天有效期限（[预测 API](./docs-site/api/guides/predictions.md) · [ML 后端 API](./docs-site/api/guides/ml-backend.md) · [导出 API](./docs-site/api/guides/export.md) · [预标注管线架构](./docs-site/dev/concepts/prediction-pipeline.md)）。

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

- **平台身份与项目职责分离**：账号平台角色与项目角色解耦，同一员工可在不同项目分别承担标注、质检或只读职责；支持[项目邀请与成员管理](./docs-site/user-guide/superadmin/user-management.md)、职责交接、[批次分派负载预览](./docs-site/user-guide/projects/batch.md#分配人员)与[项目开工准备](./docs-site/user-guide/projects/index.md#开工准备与项目设置)（[ADR-0075](./docs/adr/0075-platform-identity-vs-project-authority.md) · [角色迁移与回滚 runbook](./docs-site/ops/runbooks/project-role-migration.md) · [项目 API](./docs-site/api/guides/projects.md)）。
- **多模态工作台、质检与协作**：支持图片 [Polygon](./docs-site/user-guide/workbench/polygon.md#切割为两个对象) 与 [Raster Mask](./docs-site/user-guide/workbench/mask-brush.md#直线切割为两个实例) 原子切割与撤销重做、持久 RLE 掩码独立归属、[3D 质量检查](./docs-site/user-guide/workbench/3d-box.md#3d-质量检查)、[标注指引与认证示例图](./docs-site/user-guide/projects/index.md#编写标注指引)、带认证附件与提及通知的[任务讨论与问题线程](./docs-site/api/guides/tasks-and-annotations.md#任务讨论与问题线程)，以及跨设备同步的[快捷键](./docs-site/user-guide/workbench/index.md#完整快捷键)与[命名布局预设](./docs-site/api/guides/auth.md#账号偏好与命名布局预设)。
- **系统运营、推理认证与审计归档**：全站业务列表采用统一服务端[筛选与分页](./docs-site/user-guide/reference/filtering.md#列表分页)；超级管理员可在[系统设置](./docs-site/user-guide/reference/settings.md)管理运行时运营参数（[系统设置 API](./docs-site/api/guides/system-settings.md)）、初始化[内置推理服务系统托管认证](./docs-site/user-guide/superadmin/ml-backend-registry.md)与[宿主机容器维护](./docs-site/ops/deploy/ml-backend-maintenance.md)，并通过认证接口管理 [BUG 反馈附件](./docs-site/api/guides/bug-reports.md)与[审计月归档](./docs-site/api/guides/audit-archives.md)。

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

`dev:worktree` 为每个工作树的 `dev/test/e2e` 模式管理独立数据库、Redis、九个 bucket 及本地文件；共享本机 PostgreSQL/MinIO 实例，不修改共享数据库。默认从 API `8100` 和 Web `3100` 寻找空闲端口，可用 `--with-worker` 启动本工作树的后台任务消费者。`doctor` 诊断、`stop` 停止、带精确确认值的 `reset/destroy` 重建或清理环境。首次启动不复制已有账号和项目。详见[独立工作树开发环境](./docs-site/dev/how-to/worktree-environments.md)。

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
# Celery 后台任务：通用队列、维护、GPU / CPU 预标、导出、视频帧、通知等
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

E2E 不复用开发环境的 `3000/8000` 或 `annotation` 数据库。测试路由默认关闭，仅对显式开启且名称以 `_e2e` / `_test` 结尾的数据库开放。详见 [E2E 运行说明](./apps/web/e2e/README.md)。

API 变更后同步跑 `pnpm openapi:export` 和 `pnpm codegen`；环境变量变更后同步更新 `.env.example` 并跑 `pnpm docs:gen-env-vars`。

## 文档地图

| 角色 / 任务                | 入口                                                                                                                                   |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 标注员、审核员、项目管理员 | [docs-site/user-guide/](./docs-site/user-guide/)                                                                                       |
| 数据集与存储用户指南       | [docs-site/user-guide/datasets/](./docs-site/user-guide/datasets/index.md)                                                             |
| 本地开发                   | [docs-site/dev/tutorials/local-dev.md](./docs-site/dev/tutorials/local-dev.md)                                                         |
| 测试策略                   | [docs-site/dev/testing.md](./docs-site/dev/testing.md)                                                                                 |
| API 文档                   | [docs-site/api/](./docs-site/api/)                                                                                                     |
| Python SDK / CLI           | [docs-site/dev/sdk/quickstart.md](./docs-site/dev/sdk/quickstart.md)                                                                   |
| ML Backend 协议            | [docs-site/dev/reference/ml-backend-protocol.md](./docs-site/dev/reference/ml-backend-protocol.md)                                     |
| Data Manager               | [用户指南](./docs-site/user-guide/projects/data-manager.md) · [查询与校验契约](./docs-site/dev/reference/data-manager-query.md)        |
| 视频帧服务                 | [docs-site/dev/reference/video-frame-service.md](./docs-site/dev/reference/video-frame-service.md)                                     |
| 导出格式                   | [docs-site/user-guide/reference/export-formats.md](./docs-site/user-guide/reference/export-formats.md)                                 |
| 存储迁移与恢复运维         | [迁移清单与切换](./docs-site/ops/deploy/storage-migration-inventory.md) · [一致备份与恢复](./docs-site/ops/deploy/storage-recovery.md) |
| 部署与运维                 | [docs-site/ops/](./docs-site/ops/)                                                                                                     |
| 架构概念                   | [docs-site/dev/concepts/](./docs-site/dev/concepts/)                                                                                   |
| ADR                        | [docs/adr/](./docs/adr/)                                                                                                               |
| 变更记录                   | [CHANGELOG.md](./CHANGELOG.md)                                                                                                         |

部署版文档站：[https://yyq19990828.github.io/ai-annotation-platform/](https://yyq19990828.github.io/ai-annotation-platform/)

## 仓库结构

```text
ai-annotation-platform/
├── apps/
│   ├── api/                       # FastAPI 后端、SQLAlchemy、Alembic、Celery workers
│   ├── web/                       # React 18 + TypeScript + Vite 标注前端
│   ├── storage-agent/             # 跨网络只读站点存储 Agent
│   ├── training-executor/         # 受控单 GPU 在线学习训练执行器
│   ├── grounded-sam2-backend/     # Grounded-SAM-2 GPU ML Backend
│   ├── sam3-backend/              # SAM 3 GPU ML Backend
│   ├── yolo-backend/              # YOLO 检测 / 分割 / Exemplar ML Backend
│   ├── onnxtools-backend/         # ONNX 检测与属性推理 ML Backend
│   ├── rapidocr-backend/          # OCR 检测 / 识别 / 端到端 ML Backend
│   └── _shared/                   # 7 个共享包（backend_runtime / filesystem_read / mask_utils / protocol_v2 / site_agent_contract / task_contract / training_contract）
├── packages/
│   └── python-sdk/                # Python SDK、CLI 与 TUI 客户端
├── docs-site/                     # VitePress 用户 / 开发 / API / 运维文档
├── docs/
│   ├── adr/                       # 架构决策记录
│   ├── changelogs/                # 历史版本变更
│   ├── plans/                     # 实施计划
│   └── research/                  # 调研报告
├── infra/                         # Docker、Prometheus、Grafana、Nginx 等基础设施配置
├── scripts/                       # OpenAPI、docs impact、seed、存储迁移与维护脚本
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
| 数据 | PostgreSQL 16、Redis 7、MinIO / S3 兼容对象存储、DuckDB 分析视图                 |
| AI   | Grounded-SAM-2、SAM 3、Ultralytics YOLO、LibreYOLO、ONNX Runtime、RapidOCR       |
| 文档 | VitePress、Mermaid、OpenAPI / Scalar、ADR                                        |
| CI   | GitHub Actions、docs impact、visual regression、OpenAPI snapshot check           |

## 贡献前检查

- 只改和当前目标直接相关的文件。
- 前端样式使用 `apps/web/src/styles/shadcn.css` 的 `--sc-*` 运行时 token 或 Tailwind 语义类，不再读取旧 `--color-*` 变量。
- API、环境变量、用户可见行为变更时，同步更新相关 docs。
- 提交前至少跑和改动范围匹配的 lint / test / OpenAPI / docs 校验。

## License

MIT
