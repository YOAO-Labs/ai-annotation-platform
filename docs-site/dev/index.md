---
title: 开发文档
pageClass: docs-hub-page
aside: false
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 开发文档

面向贡献者和团队工程师。先跑通本地环境，再按开发任务查找操作指南、系统架构和协议参考；已有运行问题可直接进入故障排查。

## 5 分钟跑通

从[本地开发](./tutorials/local-dev)开始，按其中的依赖、环境变量、基础设施和日常启动步骤准备工作树。多工作树并行开发请阅读[多工作树隔离环境](./how-to/worktree-environments)；第一次提交代码时接着阅读[第一个贡献](./tutorials/first-contribution)；如果改的是文档，直接看[编写文档](./how-to/write-documentation)。

<div class="doc-card-grid cols-3">
  <DocLinkCard title="本地开发" desc="准备依赖、启动基础设施并运行 API、Web 和 Worker" href="/dev/tutorials/local-dev" />
  <DocLinkCard title="第一个贡献" desc="从一次小改动开始，完成验证并提交 PR" href="/dev/tutorials/first-contribution" />
  <DocLinkCard title="编写文档" desc="按页面类型组织正文、媒体、锚点和真值来源" href="/dev/how-to/write-documentation" />
</div>

## 四个主入口

<div class="doc-card-grid">
  <DocLinkCard title="新增 API 或页面" desc="从端点、页面、迁移到测试和代码生成" href="/dev/how-to/add-api-endpoint" />
  <DocLinkCard title="理解架构" desc="从模块、状态机和数据流进入系统设计" href="/dev/concepts/" />
  <DocLinkCard title="使用 SDK 与 CLI" desc="Python SDK、CLI、TUI 监控面板和 Cookbook" href="/dev/sdk/quickstart" />
  <DocLinkCard title="排查运行时问题" desc="按症状查找容器、网络、任务和配置问题" href="/dev/troubleshooting/" />
</div>

## 我该改哪里？

| 任务                            | 入口                                                                                                                                                                                                                                           |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 加一个后端 API                  | [How-to: 新增 API 端点](./how-to/add-api-endpoint) · [生成物归属](./reference/generated-artifacts)                                                                                                                                             |
| 加一个前端页面或改样式          | [How-to: 新增前端页面](./how-to/add-page) · [设计系统](./reference/design-system) · [图标约定](./reference/icon-conventions)                                                                                                                   |
| 改数据库结构                    | [How-to: Alembic 迁移](./how-to/add-migration)                                                                                                                                                                                                 |
| 写 / 调试后台任务               | [How-to: 调试 Celery](./how-to/debug-celery) · [后端基础设施](./concepts/backend-infrastructure)                                                                                                                                               |
| 理解项目、批次、任务与审核      | [项目模块](./concepts/project-module) · [批次模块](./concepts/batch-module) · [任务模块](./concepts/task-module) · [审核模块](./concepts/review-module)                                                                                        |
| 理解工作台布局、快捷键与光栅    | [工作台 Shell 架构](./concepts/workbench-shell) · [标注模块](./concepts/annotation-module) · [栅格资源协调](./concepts/raster-resource-coordination)                                                                                           |
| 理解 3D 点云与跨帧轨迹          | [Scene 与帧索引](./concepts/scene-and-frame-index) · [Scene Track 生命周期](./concepts/scene-track-lifecycle) · [点云数据模型](./reference/point-cloud-data-model)                                                                             |
| 理解派题、锁与权限边界          | [Scheduler 与派题](./concepts/scheduler-and-task-dispatch) · [Task Lock](./concepts/task-locking) · [可见性与权限](./concepts/visibility-and-permissions)                                                                                      |
| 理解状态流与横切机制            | [状态机总览](./concepts/state-machines) · [计数与派生字段](./concepts/counters-and-derived-fields) · [审计与通知](./concepts/audit-and-notifications)                                                                                          |
| 理解数据集、目录来源与存储治理  | [存储位置与用途绑定](./concepts/storage-connections) · [数据集与增量同步](./concepts/dataset-sources-and-sync) · [原件读取与保留回收](./concepts/original-access-and-retention) · [存储迁移与站点 Agent](./concepts/storage-migration-and-ops) |
| 修改批量或多阶段 AI 预标        | [预标注流水线](./concepts/prediction-pipeline) · [AI 预标注接管](./concepts/ai-preannotate-handoff) · [异步任务 API](/api/guides/async-jobs)                                                                                                   |
| 在线学习与模型训练闭环          | [在线学习与训练体系](./concepts/training-datasets) · [状态机总览](./concepts/state-machines#在线学习与训练状态机)                                                                                                                              |
| 修改视频工作台或 AI 追踪        | [视频标注工作台](./concepts/video-annotation-workbench) · [视频 AI 追踪](./concepts/video-ai-tracking) · [视频帧服务](./reference/video-frame-service)                                                                                         |
| 修改 backend 能力声明或项目启用 | [AI 模型集成](./concepts/ai-models) · [ML Backend 接入教程](/dev/ml-backend/starter) · [ML Backend 协议](./reference/ml-backend-protocol)                                                                                                      |
| 编写或重排文档                  | [编写文档](./how-to/write-documentation) · [更新截图](./how-to/update-screenshots) · [更新 Excalidraw 图表](./how-to/update-excalidraw-diagrams)                                                                                               |
| 理解整体架构与代码归属          | [概念：架构地图](./concepts/) · [系统全景](./concepts/overview) · [仓库地图](./concepts/repository-map)                                                                                                                                        |
| 跑测试或准备版本发布            | [测试指南](./testing) · [发布流程](./release)                                                                                                                                                                                                  |
| 排查运行时问题                  | [故障排查总览](./troubleshooting/)                                                                                                                                                                                                             |
| 查协议与底层规范                | [ML Backend 协议](./reference/ml-backend-protocol) · [WebSocket 协议](./reference/ws-protocol) · [存储桶布局](./reference/storage-buckets) · [内部 API 端点](./reference/internal-api-endpoints)                                               |

模型服务相关问题可先阅读[服务接入、路由与认证](./concepts/ml-backend-routing-and-auth)，再按[模型回流链路排查](./how-to/debug-model-return)定位请求、状态与代码入口。

## 项目仓库结构

仓库采用 Monorepo 结构，包含核心前后端、独立训练执行器、第一方推理后端、共享协议包与 Python SDK：

| 目录                                                                                                                        | 语言         | 技术栈与职责                                                                                                                      | 入口 / 核心包                 |
| --------------------------------------------------------------------------------------------------------------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `apps/api`                                                                                                                  | Python 3.11+ | FastAPI + SQLAlchemy 2.0 + Alembic + Celery（平台核心 API 与异步任务队列）                                                        | `app/main.py`                 |
| `apps/web`                                                                                                                  | TypeScript   | React 18 + Vite + Zustand + TanStack Query + Konva + Three.js（前端 SPA）                                                         | `src/main.tsx`                |
| `apps/training-executor`                                                                                                    | Python 3.11+ | 独立拉取式在线学习训练守护进程（OS `flock` 协调同一资源的执行器，支持 YOLO11 / LibreYOLO）                                        | `training_executor/worker.py` |
| `apps/storage-agent`                                                                                                        | Python 3.11+ | 跨网络只读站点存储 Agent（零入站端口、一次性配对、出站 HTTPS 命令轮询与分块上传）                                                 | `storage_agent/cli.py`        |
| `apps/{yolo,sam3,grounded-sam2,onnxtools,rapidocr}-backend`                                                                 | Python 3.11+ | 5 个第一方 ML Backend 推理服务（遵循统一 v2 协议，支持受管 GPU 仲裁与模型热加载）                                                 | 各目录下 `main.py`            |
| `apps/_shared/{backend_runtime,mask_utils,protocol_v2,task_contract,training_contract,filesystem_read,site_agent_contract}` | Python 3.11+ | 7 个跨服务共享包：池内核、光栅掩码工具、ML v2 协议、视觉任务契约、训练参数契约、Linux `openat2` 只读文件原语与站点 Agent 协议契约 | 各子包 `src/`                 |
| `packages/python-sdk`                                                                                                       | Python 3.10+ | 官方 Python SDK（`ai-annotation-sdk`）、`aap` CLI 与 Textual TUI 监控面板                                                         | `src/ai_annotation/`          |
| `docs-site`                                                                                                                 | TypeScript   | Vue 3 + VitePress 文档站与自动化契约/图表/媒体校验脚本                                                                            | `.vitepress/config.ts`        |

## 关键参考文件

- 协作与开发准则：`/AGENTS.md`（`/CLAUDE.md` 为指向同一规范的软链接）
- 本地开发与环境速查：`/DEV.md`
- 版本历史：`/CHANGELOG.md`
- 架构决策：`/docs/adr/` · [ADR 列表](./adr/)（侧边栏）
- 文档规范：[编写文档](./how-to/write-documentation)
- 部署 / 运维：[部署与运维](/ops/)
- 面向 AI / Coding Agent：[llms.txt](https://yyq19990828.github.io/ai-annotation-platform/llms.txt)（文档索引）· [llms-full.txt](https://yyq19990828.github.io/ai-annotation-platform/llms-full.txt)（全文语料）· [openapi.json](../openapi.json)（API 契约）
