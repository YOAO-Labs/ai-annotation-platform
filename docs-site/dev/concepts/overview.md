---
title: 系统全景
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 系统全景 {#系统全景}

本页解释平台的主要运行关系：浏览器如何进入 API，API 何时把工作交给 Celery Worker 或独立训练执行器，以及 PostgreSQL、Redis、对象存储和 ML 推理服务分别承担什么职责。先看物理边界，再沿一条请求路径阅读逻辑分层。

## 物理架构 {#物理架构}

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/system-overview.svg"
  alt="浏览器经 Nginx 访问 FastAPI，API 与 Celery Worker 连接 PostgreSQL、Redis、对象存储和 ML 推理服务"
  caption="平台物理架构与主要通信边界"
/>

## 关键数据流 {#关键数据流}

一条典型操作沿着“浏览器 → API → Worker / 执行器 → 数据与外部服务”流动：

1. **同步写入与鉴权**：浏览器通过 Nginx 访问 FastAPI API（携带 JWT）；API 负责权限校验、参数验证和数据库事务边界，将同步变更写入 PostgreSQL。
2. **异步派发**：耗时任务（预标注、导出、金字塔构建、样本评估等）由 API 入队至 Redis，交由专职 Celery Worker 消费；长耗时模型训练则由独立拉取式执行器（`training-executor`）通过内部 HTTP 端点认领。
3. **落盘与通知**：Worker 或执行器读写 MinIO 对象存储、调用 ML 推理服务或启动原生训练子进程，完成后将结果写回 PostgreSQL，并通过 Redis Pub/Sub + WebSocket 通知前端刷新状态。

平台内的核心业务流如下（详见[数据流](./data-flow)）：

- **认证与会话**：用户登录 → 签发短期内存 JWT + HttpOnly refresh token cookie
- **人工标注与审核**：延迟身份查询与认证原件/原生视频短租约读取 → 实时写 `annotations` 表 → 提交送审释放锁 → 触发审计与可选 Mask 质检
- **数据集与目录来源同步**：受控本地目录（`openat2`）、外部 S3/SFTP 或跨网络只读站点 Agent（`apps/storage-agent`）扫描 → 批量预检六类差异 → 原地引用或托管复制采纳 → 按需缓存与验证读取（详见[数据集、目录来源与增量同步架构](./dataset-sources-and-sync)）
- **AI 预标注**：API 入队 Celery → 服务池路由选择物理 ML 实例 → 写回 `predictions` 或自动转 `annotations`
- **数据导出**：API 创建 `AsyncJob` 并入队 → Worker 单飞构建 ZIP 与独立媒体包写入保存目标 → 推送有期限下载链接
- **在线学习闭环**：已审核标注冻结为规范任务数据版本（`export` 队列物化至 `training` 用途保存目标） → `training-executor` 通过 OS `flock` 协调同一资源的执行器执行训练 → 产物校验并注册为 `TrainedModelVersion` → `yolo-backend` 受控热加载并完成样本试运行 → 发布为项目当前模型反哺预标注与主动选样（详见[在线学习与训练体系](./training-datasets)）

## 逻辑分层 {#逻辑分层}

### 后端（apps/api） {#后端-apps-api}

```text
app/
├── api/v1/                  # HTTP 路由（薄壳：参数解析、权限守卫、调用服务）
├── services/                # 业务逻辑核心（状态机、跨表事务与领域子包）
│   ├── gpu_arbitration/     # GPU 显存契约、策略、账本、fence 与编排
│   ├── ml_routing/          # 服务池能力指纹、加权轮询与路由租约账本
│   ├── video_tracking/      # 视频追踪适配器、分窗与候选暂存
│   ├── exporting/           # 异步导出缓存、打包与多格式转换
│   ├── data_management/     # Data Manager 多维筛选查询与批量操作
│   ├── mask_formats/        # 光栅掩码导入导出格式注册表与预检
│   ├── mask_qc/             # 提交期与抽样光栅掩码质检
│   ├── point_cloud_quality/ # 3D 点云质量度量与校验
│   └── sources/             # 外部存储连接器与数据源适配
├── db/
│   ├── models/              # SQLAlchemy 2.0 ORM 模型
│   └── session.py           # 异步引擎与连接池
├── schemas/                 # Pydantic 请求/响应契约模型
├── core/                    # 配置、安全、JWT 与权限原语
├── middleware/              # 限流、审计与 Request ID 注入
├── workers/                 # 7 类 Celery 队列任务与 Beat 定时调度
└── main.py                  # FastAPI 应用入口
```

规模较大的领域逻辑均收敛于 `app/services/` 下的领域子包，平铺历史兼容模块已彻底移除并由架构测试守卫拦截。详见[后端分层](./backend-layers)与[仓库地图](./repository-map)。

### 前端（apps/web） {#前端-apps-web}

```text
src/
├── pages/
│   ├── Workbench/           # 标注/审核工作台（含 shell/stages/state/modes/components）
│   ├── Projects/            # 项目工作区（含 DataManager / 在线学习 ProjectLearningPage）
│   ├── Training/            # 全局训练中心（开始训练 / 队列与历史 / 训练仓库）
│   ├── AIPreAnnotate/       # 全局预标注流水线库与 DAG 画布
│   ├── ModelMarket/         # 模型市场、物理实例注册表与服务池拓扑
│   ├── Review/              # 审核工作台与任务队列
│   ├── Dashboard/           # 角色工作台看板
│   ├── Datasets/            # 数据集工作台、目录来源同步、个人视图与图片质量复核
│   ├── Storage/             # 受控存储位置、连接器、九用途映射、站点 Agent 与迁移准备
│   └── Users/               # 用户与超管系统治理
│   ├── shell/               # TopBar / Sidebar 应用外壳
│   ├── shadcn/ui/           # 本地拥有的 shadcn/Radix 基础原语
│   └── ui/                  # 业务通用 UI 适配组件
├── api/
│   ├── generated/           # 由 @hey-api/openapi-ts 与能力词表脚本自动生成（gitignored）
│   └── *.ts                 # 按领域封装的 API client 函数
├── stores/                  # 全局壳层 Zustand store（认证、主题、布局偏好）
├── styles/                  # shadcn.css 语义主题令牌（--sc-*）
└── main.tsx                 # React 18 路由与应用入口
```

详见[前端分层](./frontend-layers)与[工作台 Shell 架构](./workbench-shell)。

## 不在主流程中的组件 {#不在主流程中的组件}

- **Sentry** — 前后端异常捕获与错误追踪
- **Prometheus & Grafana** — API `/metrics` 与 ML Backend 指标采集及可视化看板
- **结构化日志** — `structlog`，输出 JSON 结构化日志供集中检索

这些组件提供观测和排障能力，不改变浏览器、API、Worker 与数据服务之间的主流程。需要继续追踪某一层时，可从以下页面进入：

- [后端分层](./backend-layers)：API、服务、数据库和 Worker 的职责边界
- [前端分层](./frontend-layers)：页面、组件、API wrapper 与状态管理
- [工作台 Shell 架构](./workbench-shell)：工作台外壳与模式切换
- [部署拓扑](./deployment-topology)：进程、容器和外部依赖的部署关系
- [在线学习与训练体系](./training-datasets)：训练数据版本、独立执行器、模型谱系与回流闭环
- [存储位置、连接器与用途绑定](./storage-connections)：存储位置注册、九用途保存目标隔离、原件验证读取与对象存储受控迁移
