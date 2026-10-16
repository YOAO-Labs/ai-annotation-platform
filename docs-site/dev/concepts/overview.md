---
audience: [dev]
type: explanation
since: v0.1.0
status: stable
last_reviewed: 2026-07-29
---

# 系统全景 {#系统全景}

本页解释平台的主要运行关系：浏览器如何进入 API，API 何时把工作交给 Celery Worker，以及 PostgreSQL、Redis、对象存储和 ML 推理服务分别承担什么职责。先看物理边界，再沿一条请求路径阅读逻辑分层。

## 物理架构 {#物理架构}

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/system-overview.svg"
  alt="浏览器经 Nginx 访问 FastAPI，API 与 Celery Worker 连接 PostgreSQL、Redis、对象存储和 ML 推理服务"
  caption="平台物理架构与主要通信边界"
/>

## 关键数据流 {#关键数据流}

一条典型操作沿着“浏览器 → API → Worker → 数据与外部服务”流动：

1. 浏览器通过 Nginx 访问 FastAPI API，并携带 JWT；需要长时间处理的工作由 API 入队，不在浏览器请求中等待完成。
2. API 负责权限、参数校验和事务边界，将同步写入落到 PostgreSQL，将异步工作交给对应的 Celery Worker。
3. Worker 从 Redis 消费任务，调用对象存储或 ML 推理服务，完成后把结果写回 PostgreSQL，并通过通知让前端刷新状态。

平台内的主要路径如下：

- 用户登录 → JWT → 前端存内存 + refresh token cookie
- 标注提交 → API 写 `annotations` 表 → 触发 Celery 异步任务（IoU 计算 / 通知）
- AI 预标注 → API 入队 Celery → Worker 调外部 ML 服务 → 写回 `annotations`（source=ai）
- 数据导出 → API 入队 Celery → Worker 拼装 → 写 MinIO → 通知前端下载链接
- 在线学习闭环 → 审核标注冻结为规范任务数据版本（export worker 物化至 MinIO `training` 桶） → 独立执行器拉取训练实验或参数搜索（GPU 独占锁保护） → 产物校验发布与最佳权重提取 → 候选模型注册与样本试用 → 发布回流为当前预标注模型（详见[在线学习与训练体系](./training-datasets)）

详见 [数据流](./data-flow)。

## 逻辑分层 {#逻辑分层}

### 后端（apps/api） {#后端-apps-api}

```
app/
├── api/v1/         # HTTP 路由（薄）
├── services/       # 业务逻辑（核心）
│   ├── gpu_arbitration/ # GPU 契约、策略、ledger、proof、fence 与编排
│   ├── video_tracking/  # 视频追踪编排
│   ├── exporting/       # 导出与格式打包
│   └── data_management/ # Data Manager 查询与服务
├── db/
│   ├── models/     # SQLAlchemy
│   └── session.py  # 引擎与连接池
├── schemas/        # Pydantic（请求/响应模型）
├── core/           # 配置 / JWT / 权限
├── middleware/     # 限流 / 审计 / 请求 ID
├── workers/        # Celery 任务
├── utils/
└── main.py         # FastAPI 入口
```

已迁移完成的 Redis ledger、Video、Export 与 Data Manager 平铺旧模块仅作显式 re-export 兼容层；GPU orchestration 平铺模块仍是待归位的过渡实现。应用、worker 和测试的新调用点直接依赖已经落地的领域 package，避免纯兼容路径成为第二实现边界。

详见 [后端分层](./backend-layers)。

### 前端（apps/web） {#前端-apps-web}

```
src/
├── pages/          # 路由级页面
│   ├── Workbench/  # 标注工作台（含 modes/state/stage/stages/shell）
│   ├── Dashboard/
│   ├── Projects/
│   └── Users/
├── components/
│   ├── shell/      # TopBar / Sidebar
│   └── ui/         # 设计系统组件
├── api/
│   ├── generated/  # 由 openapi-ts 自动生成（不手改）
│   ├── users.ts    # 手写 wrapper
│   └── ...
├── stores/         # Zustand
├── styles/
└── main.tsx
```

详见 [前端分层](./frontend-layers) 与 [工作台 Shell 架构](./workbench-shell)。

## 不在主流程中的组件 {#不在主流程中的组件}

- **Sentry** — 前后端错误监控
- **Prometheus** — API 指标 `/metrics`
- **结构化日志** — `structlog`，输出 JSON 给 ELK

这些组件提供观测和排障能力，不改变浏览器、API、Worker 与数据服务之间的主流程。需要继续追踪某一层时，可从以下页面进入：

- [后端分层](./backend-layers)：API、服务、数据库和 Worker 的职责边界
- [前端分层](./frontend-layers)：页面、组件、API wrapper 与状态管理
- [工作台 Shell 架构](./workbench-shell)：工作台外壳与模式切换
- [部署拓扑](./deployment-topology)：进程、容器和外部依赖的部署关系
- [在线学习与训练体系](./training-datasets)：训练数据版本、独立执行器、模型谱系与回流闭环
