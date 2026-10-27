---
title: API 文档
pageClass: docs-hub-page
aside: false
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# API 文档

后端基于 FastAPI，遵循 OpenAPI 3.1。先确认 URL、认证和错误格式，再按任务进入指南；需要逐个查看所有路由时，使用下面的交互式参考。

<div class="doc-card-grid cols-3">
  <DocLinkCard title="认证与第一条请求" desc="获取 Token，配置认证头并开始调用 API" href="/api/guides/auth" />
  <DocLinkCard title="操作任务与标注" desc="读取任务，写入标注并处理候选结果" href="/api/guides/tasks-and-annotations" />
  <DocLinkCard title="查询异步任务" desc="发起、查询、取消和重试后台作业" href="/api/guides/async-jobs" />
</div>

## 概览

- **基础 URL（本地）**：`http://localhost:8000`
- **路由前缀**：`/api/v1`
- **认证**：JWT Bearer Token（`POST /api/v1/auth/login`）
- **错误格式**：`{"detail": "<message>"}` 或 Pydantic 校验数组
- **限流**：用户级，超出返回 `429 Too Many Requests`

超管可通过 `GET /api/v1/admin/ml-integrations/managed-auth` 查看五个内置服务的私有凭据状态与已注册实例，通过 `POST /api/v1/admin/ml-integrations/managed-auth/initialize` 初始化并同步指定服务。请求可传 `providers`（`gsam2`、`sam3`、`yolo`、`onnxtools`、`rapidocr`）；省略表示全部部署配置中的服务。响应不包含凭据内容或文件路径，`credential_state=ready` 仅表示本地凭据已保存。已有手动认证保留，未注册服务不会被自动注册或启用到项目。服务认证仍由额外受认证握手确认。

本机服务维护使用 `GET /api/v1/admin/ml-backend-operations/status` 获取可用性和目标身份，再以 `POST /api/v1/admin/ml-backend-operations/jobs` 提交 `provider`、`expected_deployment` 和 `expected_runner_id`，通过 `GET /api/v1/admin/ml-backend-operations/jobs/{id}` 查看阶段。接口仅限超管；目标变化返回 `409`，不可自动重试。执行前须安装[宿主机维护程序](/ops/deploy/ml-backend-maintenance)。API 不接受命令、Compose 文件或容器名，响应不返回原始构建日志。

### 第一次接入

1. 调用 `POST /api/v1/auth/login` 获取 JWT Bearer Token。
2. 在后续请求的 `Authorization` 头中携带 `Bearer <token>`。
3. 从[项目](./guides/projects)、[任务与标注](./guides/tasks-and-annotations)开始读写资源，再按需接入异步任务或 AI 能力。

> API 指南说明稳定的调用顺序和业务约束；字段、状态码和完整路由以仓库中的 OpenAPI snapshot 与下方参考为准。

## 按分区进入

### 开始接入

- [认证](./guides/auth) — 登录、Token 刷新、API Key 与鉴权

### 资源与标注

- [项目](./guides/projects) — 项目的创建、配置与[项目可用模型目录](./guides/projects#项目可用模型目录)
- [任务与标注](./guides/tasks-and-annotations) — 读写任务、候选与正式标注
- [预测与作业](./guides/predictions) — 预测与任务对象模型

### 数据集与存储

- [数据集上传与扫描](./guides/datasets) — 生命周期归档、直传/ZIP/连接器导入、扫描、工作台视图、质量检测与引用样本备用来源
- [目录来源与观测扫描](./guides/dataset-sources) — 只读目录来源、断点恢复、预检确认、周期同步与外部保护声明
- [受控存储位置与迁移](./guides/storage-locations) — 位置登记、节点探测、维护门禁、九用途受控切换、容量盘点、原件保留回收与对象存储迁移
- [存储连接器](./guides/storage-connections) — S3 / OSS / SFTP 连接器管理与连通性校验
- [内容核验与严格读取](./guides/verified-content) — 内容摘要、请求内快照、同源缓存读取与当前任务授权
- [只读站点 Agent](./guides/site-agents) — 站点与 Agent 身份管理、一次性配对、长轮询与有界分块传输
- [视频派生媒体](./guides/video-derivatives) — 视频元数据、时间表、块/帧派生生成及故障重试

### 异步任务与导出

- [异步任务](./guides/async-jobs) — 触发、查询、取消或重试后台作业
- [视频追踪作业](./guides/video-tracker-jobs) — 发起、预览、接受、丢弃或取消视频追踪与纠错
- [导出](./guides/export) — 导出标注结果与独立媒体包
- [预测导入](./guides/import) — 导入外部预测与 Mask 格式预检执行

### 模型、训练与治理

- [在线学习与模型训练](./guides/training-datasets) — 训练数据版本、主动选样、逐样本评估、训练实验、参数搜索、增强预览、模型回流与自动策略
- [ML Backend](./guides/ml-backend) — 接入 ML Backend、查询能力或配置项目启用
- [WebSocket](./guides/websocket) — 实时事件与推送
- [系统设置](./guides/system-settings) — 超管运营配置、来源、重置与并发校验
- [审计归档](./guides/audit-archives) — 已发布归属、完整核验与分页读取
- [BUG 反馈与截图附件](./guides/bug-reports) — 认证字节上传、一次性绑定与核验下载

### 完整参考

- [全部路由索引（自动生成）](./guides/_routes.generated) — 按后端模块分组列出全部 `@router.*` 路由

## 静态契约

仓库中的真值源头：

```
apps/api/openapi.snapshot.json
```

每次后端路由 / Pydantic schema 变化都会刷新这份 snapshot；CI 校验运行时与 snapshot 一致，前端 `pnpm codegen` 也读它。

下载：[openapi.json](../openapi.json)；也可以[全屏打开 OpenAPI Reference](/api-reference.html)查看交互式路由说明。

<ApiReferenceFrame />

## 本地实时文档

启动后端后：

- [Swagger UI](http://localhost:8000/docs)
- [ReDoc](http://localhost:8000/redoc)
- [openapi.json (live)](http://localhost:8000/openapi.json)

## 前端类型生成

`pnpm codegen` 根据 snapshot 重新生成 `apps/web/src/api/generated/`。
