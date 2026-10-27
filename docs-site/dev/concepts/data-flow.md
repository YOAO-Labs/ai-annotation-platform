---
title: 数据流
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 数据流

本页通过核心业务序列图与精确代码索引，展示请求在前端、API、数据库、对象存储、Celery 队列、跨网络站点 Agent 与独立执行器之间的流转路径。

## 标注一条任务的完整链路

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/annotation-task-flow.svg"
  alt="当前 React 工作台加载任务列表、申请与续期锁、分别读取媒体和预测、实时写标注、提交审核及条件 Mask 质检流程"
  caption="当前工作台完成一条标注任务的主链"
/>

当前 React 工作台先分页读取 `/tasks`，在本地选择记忆项、URL 指定项或首项，再单独申请 task lock；`GET /tasks/next` 是仍可用的调度旁路 API，但不是当前 UI 主链。托管任务不再在 `TaskOut.file_url` 中返回整段对象存储预签名 URL，而是暴露延迟身份端点（`/api/v1/files/tasks/{id}/original-identity`），由浏览器按账号/会话范围发起认证原件下载、受验图片快照读取或原生视频短租约准备；annotations 与 predictions 分开读取。

标注在绘制过程中通过 POST / PATCH / DELETE 实时落库，首个有效标注会使 `pending → in_progress`。提交接口只负责 `pending / in_progress → review`、释放锁、批次计数 / 自动流转与 `task.submit` 审计；它不批量创建标注、不通知 reviewer，也不在响应中返回下一题。若项目启用提交时 Mask QC 且任务包含有效 Mask，事务提交后才会异步派发质检。

代码索引：

- 当前工作台选题：`apps/web/src/pages/Workbench/state/useWorkbenchShellModel.tsx`
- 任务列表与调度旁路：`apps/api/app/api/v1/tasks/list.py:list_tasks / next_task`、`apps/api/app/services/scheduler.py:get_next_task`
- Task 身份与认证原件交付：`apps/api/app/api/v1/tasks/_shared.py:_task_with_url`、`apps/api/app/api/v1/originals.py`、`apps/api/app/services/original_binary.py`、`apps/web/src/api/originals.ts`
- 任务锁：`apps/web/src/hooks/useTaskLock.ts`、`apps/api/app/api/v1/tasks/locks.py`、`apps/api/app/services/task_lock.py`
- 标注写入：`apps/api/app/api/v1/tasks/annotations.py`、`apps/api/app/services/annotation.py`
- 提交：`apps/api/app/api/v1/tasks/lifecycle.py:submit_task`
- 审计：`apps/api/app/services/audit.py:AuditAction.TASK_SUBMIT`
- 条件 Mask QC：`app/services/mask_qc/service.py` 创建冻结 Run；Celery consumer 调用 `app/application/quality_execution.py` → `app/services/mask_qc/execution.py`，`app/workers/mask_qc.py` 仅保留原任务名/参数的 Session 适配。

---

## AI 预标注

<ExcalidrawDiagram
  src="/diagrams/shared/ai/ai-preannotation-handoff.svg"
  alt="AI 预标注在 Web、FastAPI、Redis、Celery worker、服务池路由、物理 ML 实例、Postgres 与人工工作台之间的数据流"
  caption="AI 预标注数据流：派发边界、持久化作业、路由推理与人工接管"
/>

API 先提交 pending `AsyncJob(kind=batch_predict)`，再以该 UUID 派发 Celery。成功派发返回 HTTP 200 与可立即查询的 `job_id`；worker 认领同一持久作业。worker 不是每 batch 一次把所有媒体交给 ML，而是按 task 处理：根阶段通常每次 `/predict` 一题，成功写 `Prediction + PredictionMeta`，失败写 `FailedPrediction`，然后单题提交。

请求中的 registry ID 先解析到项目已启用的逻辑 service pool，`RoutedMLBackendClient` 再选物理实例。物理 backend 使用签名 URL 向 MinIO 拉取原媒体；多阶段 crop 才额外由 worker 读取原图、上传 ROI，下游 backend 再拉取 crop。

代码索引：

- 触发端点：`apps/api/app/api/v1/projects.py:trigger_preannotation`
- 服务池路由与 ML client：`apps/api/app/services/ml_routing/client.py:RoutedMLBackendClient`
- ML 协议契约：[`docs-site/dev/reference/ml-backend-protocol.md`](../reference/ml-backend-protocol)
- Worker：`apps/api/app/workers/tasks.py:batch_predict` 与 `apps/api/app/application/preannotation_execution.py:run_preannotation`
- 对象存储签名 URL：`apps/api/app/services/storage.py:resolve_task_url`
- 项目 WS 中继：`apps/api/app/api/v1/ws.py:preannotate_progress`
- 全局 admin WS：`apps/api/app/api/v1/ws.py:prediction_jobs_socket`
- WS 协议：[`docs-site/dev/reference/ws-protocol.md`](../reference/ws-protocol)

---

## 数据导出

<ExcalidrawDiagram
  src="/diagrams/shared/workflow/async-export-delivery.svg"
  alt="项目和批次导出从可选 Mask 预检、持久化派发、Worker 缓存与 Redis single-flight，到临时 ZIP、固定归属工件、有期限下载和失败保留的完整边界"
  caption="异步导出交付链：先提交作业与审计，再派发；缓存未命中时单飞构建可下载工件"
/>

Mask export preflight 是项目范围的可选、只读计划步骤：adapter 统计对象和文件，返回 loss class、losses / warnings、容量估算与 plan digest。当前导出端点不接收该 digest，因此它帮助用户决策，但不锁定后续执行快照。

项目与批次导出先校验 targets / options。VOC 仅允许单选，在记录审计后同步返回 ZIP；其他目标创建 `AsyncJob(kind=export)` 并写导出审计，两者成功 `commit` 后才调用 `run_export.delay()`，然后返回 HTTP 202 与 `job_id`。因此 broker 不会拿到尚未提交的作业 ID。

Worker 将 active annotation 的 `max(updated_at) + count` 与 scope、targets、options、adapter contract 版本合成 cache key。命中时先核对缓存与独立ExportOutput的项目、键、大小及期限，再从保存桶完整验证SHA。最终授权/取消和维护围栏通过后，只签固定保存期限的剩余窗口，完成作业并发 `export.ready`；不会为旧对象重新延长七天。未命中时，Worker 用 Redis `SET NX EX 6h` 对 cache key 做 single-flight；竞争失败会每 5 秒重试，最多 120 次，持锁后还会重查一次缓存。

真正构建时按块读取任务与标注，ZIP写磁盘tempfile，先取得独立SHA/大小并提交ExportOutput pending归属、随机候选键和固定七天截止，再在durable permit中上传并完整回读。最终短事务复核原请求Key证明、账号/项目/任务/引用来源、取消、候选快照和维护代次，共同发布ready、缓存索引、Job结果与通知。异常/取消或commit应答未知保留pending及字节，不猜测删除；等真实线程收尾后才清tempfile并释放所属Redis锁。到期后无读取permit的ready对象先提交retired和缓存移除，再沿保存桶重试精确清理。项目或Job清理不会丢失独立Output责任。

代码索引：

- 项目导出端点：`apps/api/app/api/v1/projects.py:export_project`
- 批次导出端点：`apps/api/app/api/v1/batches.py:export_batch`
- 作业状态与下载 URL：`apps/api/app/api/v1/async_jobs.py:get_async_job`
- 审计：所有导出写 `AuditAction.PROJECT_EXPORT` / `BATCH_EXPORT`
- Worker：`apps/api/app/workers/export.py`（`app.workers.export.run_export` 任务）
- 工件缓存：`apps/api/app/services/exporting/cache.py`
- 格式 registry / plan / archive：`apps/api/app/services/mask_formats/`
- 导出打包：`apps/api/app/services/exporting/`

VOC 保留同步 ZIP 响应；其余目标走上述异步作业与缓存链路。

### 格式导入执行边界

格式导入先通过服务端生成的预签名 URL 上传到 import bucket，预检只接受当前项目与用户前缀下的 object key
和由调用方提交的 SHA-256。服务端流式复核字节配额与摘要，再将 adapter / manifest 版本、mapping / options digest、
逐任务计划和 15 分钟 receipt 写入 `mask_format_imports`。

执行时再次复核 object SHA-256 和全部 digest；任何一项变化都拒绝执行。Worker 逐 task 开启独立事务，并在
`result_json.items` 中记录 `committed / failed / skipped`；续跑只处理未提交项。本地临时目录在成功、失败和取消时都由
context manager 清理，staged object 由 import bucket 的 7 天 lifecycle 回收。项目标注导入 UI 在至少一个 Mask adapter 完成
consumer 闭环验证前保持关闭；capability 响应不会把未验证 adapter 暴露给前端。

---

## 在线学习闭环

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/online-learning-loop.svg"
  alt="在线学习端到端闭环架构：从审核标注到数据版本快照、独立执行器训练、逐样本评估、模型注册发布、动态回流预标注，再到主动选样与自动触发策略"
  caption="在线学习端到端闭环数据流全景"
/>

在线学习实现了从高质量标注产出到预标注模型迭代的双向自增益循环：

1. **快照物化**：单语句查询冻结已审核标注，Celery export worker 下载原图、校验 SHA-256 并物化至 MinIO `training` 存储桶。
2. **独立调度**：独立拉取式执行器通过共享状态目录的 OS `flock` 文件锁排他独占物理 GPU，守护原生训练子进程，实时向平台推送观测指标与脱敏日志。
3. **评估决策**：训练完成后对最佳权重执行逐样本贪心评估，将不可变权重登记入模型谱系，由管理员追加处置决策并发布。
4. **预标注回流**：YOLO backend 通过 `/managed-models` 端点动态挂载发布权重，双向核验收据协议后直接服务于工作台预标注与批跑流水线。
5. **主动选样**：通过启发式难例挖掘（`max(1 - score)`）结合随机探索圈选新批次，驱动下一轮数据沉淀。

代码索引：

- 数据集版本化与物化：`apps/api/app/services/training_datasets.py`、`apps/api/app/workers/training_dataset.py`
- 算力租约与调度：`apps/api/app/services/training_runs.py`、`apps/api/app/services/training_studies.py`
- 独立执行器：`apps/training-executor/training_executor/`
- 模型谱系、决策与发布：`apps/api/app/services/model_lineage.py`、`apps/api/app/api/v1/model_lineage.py`、`apps/api/app/api/v1/trained_models.py`
- 模型回流与受控评估端点：`apps/yolo-backend/managed_models.py`、`apps/yolo-backend/sample_evaluations.py`
- 架构总览：[在线学习与训练体系](./training-datasets)

---

## 实时通知

<ExcalidrawDiagram
  src="/diagrams/shared/realtime/durable-notification-delivery.svg"
  alt="普通通知默认由调用方提交后再尝试发布；终态 job 保留提交前兼容路径，WebSocket 先核验已提交行和权限再转发"
  caption="普通通知提交后发布、终态 job 提交前兼容路径及 WebSocket 可见性门禁"
/>

通知的持久化真值是 Postgres，Redis Pub/Sub 只是在线刷新提示。`NotificationService.notify()` 先通过偏好 owner 检查静音，再调用 `aap_notifications_v1.append_notification(jsonb)`，把返回行快照合并进原 Session identity map，最后执行调用方原有的完整 flush；事务仍由调用方提交或回滚。默认 `defer_publish=True`，普通调用方在 commit 成功后再 best-effort 发布。选定的通知工作流使用 owner durable outbox / lease，但不覆盖所有通知。

白名单后台 job 终态仍由 `notify_job_terminal` 在外层事务提交前写入并尝试发布，以保留既有 worker 调用顺序。通知 WS 对真实业务消息逐帧重新验证 JWT，确认通知行属于当前接收者并重验项目权限；row 尚不可见时最多查询 4 次，3 次未命中后分别等待 150ms（累计 sleep 450ms，另加查询耗时）。第四次仍未见行或事务回滚时，gate 丢弃消息，不会转发幽灵提示。若发布失败、或 gate 检查结束后事务才提交，Postgres 记录仍可由 REST 读取。

`useNotificationSocket` 为通知频道维护独立的 1s → 30s 无限重连循环，并在 `1008 / 4001` 鉴权关闭码下刷新 token。收到业务消息后它只让查询缓存失效，展示数据仍以 REST 已提交结果为准；连接打开或重开时也会失效通知查询，默认 30s REST 轮询、回到前台和网络重连继续补齐状态。

服务端 push 主要事件：

- 任务通过、驳回或重开（`type = "task.approved" | "task.rejected" | "task.reopened"`）
- 导出完成或失败（`type = "export.ready" | "export.failed"`）
- 用户发起的白名单后台作业终态（`type = "job.completed" | "job.failed" | "job.cancelled"`，涵盖预标注、导出、训练数据集版本构建 `training_dataset`、训练实验 `training_run`、逐样本评估 `sample_evaluation` 与主动学习轮次 `active_learning_round`，并自动解析附带对应资源名称）

代码索引：

- WS 端点：`apps/api/app/api/v1/ws.py:notifications_socket`
- 通知服务：`apps/api/app/services/notification.py:NotificationService.notify`、`apps/api/app/services/async_job.py`
- 通知模型：`apps/api/app/db/models/notification.py`
- 前端 hook：`apps/web/src/hooks/useNotificationSocket.ts`
- WS 协议详细：[`docs-site/dev/reference/ws-protocol.md`](../reference/ws-protocol)

---

## 目录来源扫描、预检差异与原地引用读取

```text
DatasetSource (filesystem / s3 / sftp / agent)
  ├─ 1. 目录扫描 (DatasetScanRun / DatasetScanEntry, 60s 租约分块提交)
  ├─ 2. 批量预检 (DatasetRegistrationRun / Entry, 固定已接纳路径基线并比对 6 类差异)
  ├─ 3. 确认采纳 (创建 DatasetItem + DatasetAsset + DatasetReference / DatasetSourceRevision)
  └─ 4. 原件验证读取 (original_sources / reference_snapshot + Location Gate 准入 + SHA-256 核验)
```

1. **目录扫描（Scan）**：`DatasetSource` 绑定数据集与受控存储位置后，按 `Location → Source → Job → Run` 短锁领取 `DatasetScanRun`，在事务外通过 `aap_filesystem_read`（`openat2`）、严格外部 S3/SFTP 客户端或站点 Agent 分块枚举目录，写入 `DatasetScanEntry`。`stat` 观测不直接提升内容为 `verified`。
2. **批量预检与增量差异（Preflight）**：扫描完成后创建 `DatasetRegistrationRun`，在来源锁内以已接纳路径头（`DatasetSourceItem`）为基线，逐项完整读取字节并核验 SHA-256 与图片尺寸，生成新增、相同、内容变化、观测变化、未见和异常六类差异。
3. **采纳与修订推进（Adoption）**：管理员确认仅新增（或周期计划命中 `pure_additions`）以及显式接纳同摘要重验、跨路径移动或新内容修订时，在同一事务内创建/更新 `DatasetItem`、`DatasetAsset`、`DatasetReference`（原地引用不复制对象存储副本）与不可变 `DatasetSourceRevision`，并为已关联项目补建任务。
4. **验证读取与按需缓存（Verified Read）**：工作台或后台消费者读取原地引用样本时，先取得逻辑位置的维护 Gate 共享许可，核对当前任务/数据集权限与 API Key 证明；若位置与来源均开启 `on_demand` 且命中有效 `OriginalCacheEntry`，则从 `media-cache` 读取完整核验副本，否则回源捕获有界快照并复核完整 SHA-256。

代码索引：

- 目录扫描与恢复：`apps/api/app/services/dataset_scans.py`、`apps/_shared/filesystem_read/`
- 预检差异与批量登记：`apps/api/app/services/dataset_registration.py`、`apps/api/app/services/dataset_source_changes.py`
- 周期同步协调与修订：`apps/api/app/services/dataset_source_sync.py`、`apps/api/app/services/dataset_source_revisions.py`
- 严格读取与按需缓存：`apps/api/app/services/dataset_content.py`、`apps/api/app/services/original_cache.py`
- 架构总览：[数据集、目录来源与增量同步架构](./dataset-sources-and-sync)

---

## 跨网络站点 Agent 与迁移封存读取路由

```text
[跨网络站点 Agent 出站传输]
apps/storage-agent ──HTTPS POST /site-agents/commands/poll──► 中心 API (派发租约命令)
  ├─ 本地 openat2 + SHA-256 生成 0600 VerifiedStage
  ├─ POST /commands/{id}/chunks (256 KiB 分块中继至有界 Redis)
  └─ POST /commands/{id}/result (持久化关闭证据与业务 ACK)

[迁移已封存读取映射 (StorageMigrationReadPublication)]
调用方请求逻辑源 A ──► OriginalSource + immutable read_route ──► PhysicalReadPlan (目标 B + Gate 许可)
```

1. **站点 Agent 出站拉取与分块回传**：独立只读组件 `apps/storage-agent` 不监听入站端口，使用一次性配对取得凭据后，携带持久 `session_id` 轮询 `/api/v1/site-agents/commands/poll`。收到 `probe`、`browse`、`scan` 或 `read` 命令后，在本地受控根内完成完整 SHA-256 校验与 `VerifiedStage` 暂存；当 `/result` 返回 `409 agent_content_incomplete` 时，按 `256 KiB` 分块上传至 `/chunks`，由中心重组并二次校验 SHA-256 后交付上层 Snapshot。若主来源 Agent 离线，已登记的同数据集同 SHA-256 显式副本（`DatasetReferenceReplica`）可受控接管物理读取。
2. **已封存迁移读取路由（`PhysicalReadPlan`）**：对象存储迁移完成最终增量核验并发布 `StorageMigrationReadPublication` 后，业务记录（`OriginalSource` 及 27 类保存目标）保持原逻辑源 A 地址不变，由 `PhysicalReadPlan` 与 `storage_migration_artifact_reads` 在读取时透明路由至目标 B 的准确已核验 `VersionId` 与 `ETag`，同时在目标 B 登记 `migration_read_<publication_id>` 与原 owner 读取许可，支持随时安全撤回（`withdrawing → withdrawn`）或九用途原子切换与回滚。

代码索引：

- 站点 Agent 控制面与传输：`apps/api/app/api/v1/site_agents.py`、`apps/api/app/api/v1/site_agent_transport.py`、`apps/api/app/services/agent_transport.py`、`apps/storage-agent/`
- 显式同内容引用副本：`apps/api/app/services/dataset_reference_replicas.py`
- 迁移读取发布与路由：`apps/api/app/services/storage_migration_read_publications.py`、`apps/api/app/services/original_read_plan.py`、`apps/api/app/services/storage_migration_artifact_reads.py`
- 架构总览：[对象存储迁移、站点 Agent 与容量观测架构](./storage-migration-and-ops)
