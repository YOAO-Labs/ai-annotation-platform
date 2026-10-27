---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-24
---

# 预标注流水线（Prediction Pipeline）

AI 预标注流水线以 `async_jobs(kind=batch_predict)` 作为用户可见任务真值，以 `predictions` / `failed_predictions` 保存可采纳结果和失败项。本页讲清 Celery 派发边界、持久化五态、写入时点与下游表的关系。

> 决策依据：[ADR 0014 — Prediction Jobs 历史表](../adr/archive/0014-prediction-jobs-table)

## 状态机

<ExcalidrawDiagram
  src="/diagrams/shared/ai/batch-predict-job-lifecycle.svg"
  alt="Celery 队列执行边界与 AsyncJob pending、running、completed、failed、cancelled 五态的真实关系"
  caption="作业五态；图中 worker 建档分支适用于旧单阶段请求，预建请求的排队期为持久化 pending"
/>

| 状态        | 何时进入                                                             | 关键字段                                                      |
| ----------- | -------------------------------------------------------------------- | ------------------------------------------------------------- |
| `pending`   | API 预建作业时进入；旧单阶段请求由 worker 建档后瞬时进入             | `created_at`, `payload.prompt`, `payload.ml_backend_id`       |
| `running`   | worker 接管预建作业或建档后提交，开始实际执行                        | `started_at`, `celery_task_id`, `progress_pct`                |
| `completed` | 无可处理 task，或至少一题成功；可同时有 `failed_count > 0`           | `completed_at`, `result.success_count`, `result.failed_count` |
| `failed`    | 全部子项失败，或 worker 根任务未捕获异常 / 失败 signal               | `completed_at`, `error_message`                               |
| `cancelled` | 运行中先记 `payload.cancel_requested`，worker 在下一处理边界协作收敛 | `completed_at`, `result.cancelled_at_index`                   |

`cancel_requested` 是 `running` 上的 payload 标记，不是第六个状态。调用 `revoke(terminate=False)` 也不会让已运行 worker 立即跳到 `cancelled`。单题 backend 5xx / timeout 通常会被记为 `FailedPrediction`；只有全部失败才让根 job 进入 `failed`。

`retry-failed` 不会让原 job 回到 `pending / running`，而是按失败项创建独立 `kind=prediction_retry` 作业。每日清理只删除以 `created_at` 为截止的过期 `completed / failed / cancelled`；`pending / running` 不会因超龄自动收敛。

## 端到端时序

<ExcalidrawDiagram
  src="/diagrams/shared/ai/ai-preannotation-handoff.svg"
  alt="owner 从 Web 触发预标注，FastAPI 入队，Celery worker 建立持久作业、经服务池选物理实例、写预测与进度，再由 owner 首次人工接管"
  caption="批量预标端到端主链，共享同一份 canonical 可编辑资产"
/>

图片请求均在 API 中预建 AsyncJob，冻结准确选择、配置及来源，返回该持久作业 ID 并用作 Celery task ID；其它模态保留各自的建档规则。前端 `ProjectDetailPanel` / `RunStatusPanel` 通过订阅 `project:{id}:preannotate` WebSocket 频道接收实时进度与阶段统计。

### 托管图片与混合输入

`ReferencePredictionJob` 同时编排引用与托管成员。服务端来源证明绑定原请求凭据、准确 Task 集合、执行快照与 canonical 原件事实；托管成员统一解析 TaskOriginal、直接 Item 或 primary 关联，按保存位置条件读取并完整核验，不调用当前桶 URL fallback。每题的整图和所有 ROI 共用同一 Snapshot，裁剪在纯计算后由异步执行层逐项上传，保留父阶段、原 box index 和多层几何谱系。

现有 `PredictionInputLease` 独立于业务删除，保存固定 import 位置和精确对象清单。对象责任和随机写入标记在 PUT 前独立提交；目标桶必须启用版本控制，条件创建后完整核对内容、非 null 的不可变 VersionId 及存储元信息中的 lease／写入标记，才可交给模型。签名 URL 固定已核验版本，相同 SHA 不足以证明对象属于本次写入。每次尝试的 UUID／SHA 路径避免后端按去 query 的 URL 缓存旧内容。原件和输入副本的许可覆盖真实读取／上传／关闭，任意中断都排空物理工作；未知关闭保留操作账本，不能靠 TTL 解除。应用不自动启用桶版本控制。

最终托管来源 receipt 绑定当前会话及事务，先于 overwrite 删除取得；`PredictionService` 只从该凭证写入受保护的 `original_input`，引用的普通与原生资格保持独立。失败保存原来源，重试刷新请求授权和 Task 版本但不重采来源；历史缺证明的图片消息明确拒绝。旧无固定位置或清单的临时租约保留为 `legacy_unpinned`，维护任务不猜测位置或盲删整个前缀。离线恢复只重映射操作位置，冻结来源、对象事实和首次期限保持。

## 模型目录与执行快照

协议词表、内置能力声明和项目训练版本分别有独立所有者。`model_catalog` 服务把它们投影为只读目录，包含具体规格、类别来源、部署和每种操作的可用原因；它不会注册、安装、发布或预热。项目成员使用项目范围接口，私有训练版本不会进入全局 `/ml-capabilities/instances`。

`prediction_execution_snapshots` 保存不可变的规范阶段配置、摘要及 schema 版本。`async_jobs.execution_snapshot_id` 和 `failed_predictions.execution_snapshot_id` 都引用该记录，失败记录不会因为原始作业过期而丢失管线。后台清理只删除无任何作业或失败引用的过期快照，已放弃但可恢复的失败也保留引用。

每个受控阶段冻结确切版本、部署、registry、权重／类别／项目绑定摘要及发布代次。检测别名 `project-release`、分割别名 `project-release:segment` 只在新提交时解析；相同幂等键在查到原作业后复用原快照。内置阶段冻结已声明的具体规格和参数默认值；已有权重核验时，`builtin_model_ref` 在实际借出的模型上检查权重、类别和加载器身份。未核验的旧声明仅保证配置冻结，不宣称物理权重已验证。

受控引用保留原训练 `framework_version`，并在重新加载试用后独立冻结实际 `inference_runtime`，后者参与引用及评估请求摘要。Backend 的结果必须匹配冻结的推理身份；根预测元数据单独保存该身份，阶段执行记录通过各自引用保留它。旧引用不新增字段或重算摘要，只允许原训练版本推理，不能因容器升级静默更换环境。软件身份与目标 GPU 的生命周期资格分别核验，见[受控模型协议](../reference/ml-backend-protocol#固定样本试用与发布)。

初跑与失败重试共用 `_run_task_pipeline`。重试只重跑失败任务的完整原始阶段树，沿用各阶段队列设备和受控部署；不重新解析当前发布。下游失败记录 `partial`、阶段错误和策略允许的候选，完整重试成功后将旧部分候选标为已被替代，保留其来源和人工采纳结果。无快照的历史失败维持旧单阶段重试。

WorkBench 请求要求项目 `annotation.write` 和当前题有效编辑／分派权限，批量请求仍要求项目管理权限。Worker 在推理前和最终写入锁内复校验账号、项目、任务、批次、分派和编辑锁；任务推理期间迁批次时拒绝最终写入。

预测元数据保留每阶段实际实例、模型引用和稳定 shape／直接父 shape 身份。`parent_box_idx` 属于直接父阶段；裁剪缓存按父阶段隔离，上传 key 包含内容摘要。受控框明确使用百分比坐标，ROI 回映尊重 `coordinate_units`，不把小于 1% 的框误判为归一化坐标。

受控实例分割仅作为图片整图源阶段运行；裁剪分割、交互与视频组合在统一校验边界拒绝。未知受控版本不冒充检测能力，解析确切版本后再次检查阶段范围。后端返回原尺寸逐实例 RLE，transport 验证类别、实例索引、模型及运行时并只映射一次项目类别；管线在富集或删框前完整核验 portable 对象，再从真实 RLE 取紧像素外接框供属性 crop 或全图 bbox prompt 使用。原 Mask 的洞、分离部分、实例索引和 SHA 保持不变；crop 与 prompt 都在下游派发前比对实际原图尺寸。

最终结果可以混合原生 Mask 与后续矢量几何，不能再次套用原生类别和连续实例索引校验。删框后逐结果包裁剪 `mask_objects`，相同 SHA 仍被其它 shape 引用时保留；不跨结果合并对象包。持久化在任何 PUT 前复核全部 native gate、任务尺寸、精确对象集合、摘要、非空及预算，再按 Task → 排序内容锁物化；回滚遗留的共享 SHA 对象由既有 GC 处理。预测正文与内联对象不复制到元数据。按 snapshot shape lineage 采纳时，每个 shape 使用自身工具单位及阶段模型的类别策略；旧无阶段来源记录保留行级合同。人工接受、修正及审核后才能进入下一轮训练，见[实例分割闭环](./instance-segmentation-learning)。

下游原生掩码仍缺少经过资格核验的像素回映与对象归属合同，不能通过丢 shape 或丢对象降级：运行时在属性合并、geometry 收集及 crop 回映前以 `NativeMaskChildOutputUnsupported` 记录明确阶段失败，按阶段策略保留或丢弃父对象。图片原生候选复用受预算管理的像素层、Worker 和缓存，按 Task/Prediction/原始 shape 下标读取并按真实像素拾取；来源、置信度或审阅状态变化后移除相应可见记录，缓存按预算回收，切题隔离迟到结果。引用后台原生生成使用下述独立资格，普通引用 receipt 继续拒绝原生载荷。

详细决策见 [模型目录与冻结管线](../adr/archive/0088-model-catalog-and-frozen-pipelines)。

### 引用图片的后台原生结果资格

批量与工作台后台执行、两个失败重试入口共用 `ReferencePredictionJob`。普通引用凭证继续只允许普通几何；受控实例分割整图根阶段另由 `reference_native_predictions` 为每条最终结果签发内部凭证。先校验整组载荷，任何一条模型、运行时、尺寸、类别、RLE 或阶段来源不合格，均不进入首个对象 PUT。凭证绑定 owner、当前会话与非嵌套事务、Task/Project 及完整写入内容，关闭或过期的原图快照不能继续写入。

最终检查锁定冻结模型部署与实际路由成员，复核当前项目类别和原图身份；不持锁等待模型或原件网络 I/O。原生实例索引保留运输层的身份，不因删父对象而要求重新连续编号。向量子结果必须来自实际执行的几何阶段，父链可以经过未持久化的 intermediate 或透传属性阶段；变换须满足整图坐标契约，不从缺失父身份猜测归属。已有 lineage 不保存已丢弃祖先的几何，因此这里不重新计算其完整裁剪映射。

服务端保留字段 `PredictionMeta.extra.reference_native` 保存生成时冻结配置、内容身份及最终候选摘要；普通写入剥离客户端同名字段。它是当前服务写入边界的记录，不是对任意数据库篡改的密码学证明。采纳先核验当前权限和严格原图，再比对最终候选、稳定模型/阶段元信息与保存的记录，并读取 RLE 复查当前 Mask gate。证明包含历史所需事实，不依赖已清理的 AsyncJob/执行快照或仍在线的模型；驳回和重试替代记录不改变生成时身份。交互与批量修复继续使用各自的资格，不能混用。

## 与 `predictions` 表的边界

| 用途                                  | 查哪张表                                                  |
| ------------------------------------- | --------------------------------------------------------- |
| 列出"现在能采纳的候选框"              | `predictions`（按 task 过滤）                             |
| 列出"AI 跑了哪几次、成功失败、谁触发" | `async_jobs`（`kind=batch_predict` / `prediction_retry`） |
| 重置批次后回看历史                    | **只能** `async_jobs`（`predictions` 已被清）             |
| 工作台读取候选 → 渲染紫框             | `predictions` 经 `to_internal_shape` adapter              |

详见 [API Schema 边界](./api-schema-boundary)。

## 工具单位（tool_unit）维度

`predictions.tool_unit_id String(30)` 是必填字段：

- 写入时由 `PredictionService.create_from_ml_result` 调 [`derive_tool_unit_from_result`](../../../apps/api/app/services/prediction.py) 按 `result[0].type` 派生：
  - `polygonlabels` / `brushlabels` / `multi_polygon` → `region`
  - `polylinelabels` → `polyline`
  - `rectanglelabels` → `bbox`；若 `value.rotation` 字段存在 → `rotated_bbox`
  - `keypointlabels` → `keypoint`；其它未知 LS type → `bbox` 占位
- `to_internal_shape()` 出参也带 `tool_unit_id`，供前端候选层 / AAP JSON 导出消费
- `accept_prediction()` 创建的 annotation 沿用 prediction 的 `tool_unit_id`（与项目 `tool_bindings[unit].classes` 软校验保一致）

详见 [annotation-module · 工具单位](./annotation-module#工具单位tool_unit维度) 与 [ADR-0026](../adr/archive/0026-tool-unit-class-and-attribute-binding)。

## WebSocket 通道

| 通道                         | 谁订阅        | 内容                                         |
| ---------------------------- | ------------- | -------------------------------------------- |
| `project:{id}:preannotate`   | 项目进度 hook | 单项目高频进度 / 错误；当前 WS 无鉴权        |
| `global:prediction-jobs`     | 任何 admin    | 兼容全局 in-flight 进度推送                  |
| `/api/v1/async-jobs` polling | 登录用户      | Topbar `JobsBell` 与 `/ai-pre/jobs` 任务历史 |

`JobsBell` 是当前用户可见任务的主入口；WebSocket 仍用于更快地刷新预标注进度。

## 失败兜底（B-1 教训）

`_BatchPredictTask.on_failure` 会尽力把未捕获异常推到 `project:{id}:preannotate`，并在已建档时通过 `celery_task_id` 反查、置 `async_jobs=failed`。如果异常发生在 worker 创建 AsyncJob 之前，WS 仍可能收到 error，但没有可反查的历史行；进程硬崩或 frame fan-out callback 缺口也可能遗留 `running`，当前没有 stale-job watchdog。

详见 [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart)。

## 能力协商与模态路由

### 能力快照落库

`check_health`（`apps/api/app/services/ml_backend.py`）在拉完 `/health` 后 best-effort 探一次 backend `/setup`，调 `extract_capabilities`（`apps/api/app/services/ml_capabilities.py`）把能力快照写进 `ml_backend_registry.health_meta["capabilities"]`：

| 字段                 | 来源                             | 含义                                                                            |
| -------------------- | -------------------------------- | ------------------------------------------------------------------------------- |
| `supported_prompts`  | `/setup` 直传                    | 支持的图像提示类型（text/point/interactive_box/exemplar 等）                    |
| `supported_inputs`   | `/setup.models[]` 或平台兼容合成 | 支持的投递形态（整图 / 裁剪图 / 框提示 / 点提示），多阶段父子可达性用它判断     |
| `supported_trackers` | `/setup` 直传                    | 支持的视频追踪器（如 `sam2_video`）                                             |
| `modalities`         | `derive_modalities()` 派生       | `supported_prompts` 非空 → `image`；`supported_trackers` 非空 → `video`         |
| `is_interactive`     | `/setup.is_interactive`          | 健康检查时回写，不再手填                                                        |
| `warnings`           | 平台校验派生                     | 受控词表越界诊断（task / infra / prompt / geometry），模型市场显示为 `⚠ 协议 N` |

`health_meta` 字段类型为 `HealthMeta(extra="allow")`，无需 alembic 迁移。探测失败时静默跳过，不影响健康检查结果（fail-open）。

### 绑定时模态校验

`PATCH /projects/{id}` 绑定 backend 时（`apps/api/app/api/v1/projects.py::_check_backend_modality_compat`），实时探 `/setup` 派生模态，与项目 `data_type` 不兼容返回 422。探测失败则 fail-open 放行，mismatch 留到 predict 时暴露。

### 前端按 data_type 模态分流

`/ai-pre` 执行页按项目 `data_type` 在前端分流：

| `data_type` | `/ai-pre` 行为                                                            |
| ----------- | ------------------------------------------------------------------------- |
| `image`     | 批量预标与多阶段流水线面板 → 走本页描述的 `batch_predict` 流水线          |
| `video`     | 编排画布选择整段视频追踪或逐帧图像检测，进入相应 `batch_predict` 执行分支 |
| `lidar`     | 显示占位提示，批量预标注入口暂不执行                                      |

视频项目也可从工作台发起交互 video tracker，运行态保留 `video_tracker_jobs` 专表；这类历史汇总在 `/ai-pre/jobs?tab=video`，由 `async_jobs(kind=video_tracker)` 提供。

**数据边界**：本页的批量预标流水线以 `Prediction` 为候选存储，按 shape 采纳；交互视频 tracker 则把逐帧、多目标结果暂存在 `VideoTrackerJob.staged_result`，按 job 接受 / 丢弃，接受后才写入轨迹 annotation。两者共享 ML Backend 能力协议，但不共享候选表、计数或状态机。视频链路详见[视频 AI 追踪架构](./video-ai-tracking)。

托管整段视频使用独立原件与输入 owner：三 canonical 来源的完整读取、同 FD probe、每 attempt 固定版本临时副本、实际 transport 前的授权／配置复核，以及源文件真实关闭后的最终短事务。输出先限制响应和几何预算，再适配聚合轨迹或逐帧框／多边形／Mask；私有结果资格绑定实际输出与 pipeline shape 身份，不能用仅有来源的 receipt 授予任意结果写入权限。普通采纳重新完整核验当前原件和实际 RLE，最终只在调用者事务核元信息与对象 owner；公开 Job／Prediction 不暴露私有授权和存储定位。逐帧与专用交互 Tracker 保持各自合同，见[固定输入 API](../../api/guides/predictions.md#托管视频的固定输入)。

Mask 内容 GET 使用独立只读资格：完整原件和 RLE 先读取并确认关闭，仅保留当前请求帧的有界内容，再在调用者事务中核对授权、来源、候选／标注及物理 owner。采纳时服务器在 `attributes_meta._video_prediction` 保存安全来源血缘，普通属性编辑保留该命名空间；HTTP 创建／更新不能提供它。已采纳标注读取不依赖父候选永久存在，不复用要求写权限的采纳 receipt。

## 按后端动态参数透传

`PreannotateRequest`（`apps/api/app/api/v1/projects.py`）新增 `params: dict | None` 字段，由前端按选中 backend 的 `/setup.params` 用 SchemaForm 渲染并按 backend 记忆（`User.preferences.ai.params_by_backend`）后带上。

Worker（`apps/api/app/workers/tasks.py::batch_predict`）构建 `/predict` context 时：

```python
if params:
    context.update({k: v for k, v in params.items() if v is not None})
```

即 `params` 的非 None 值覆盖项目级 `box_threshold` / `text_threshold` 兜底值。无 `params` 时使用项目级阈值兜底，状态机本身不变。

## 幂等预标模式（predict_mode）

`PreannotateRequest.predict_mode`（`apps/api/app/api/v1/projects.py`）是 `Literal["skip_predicted", "overwrite", "append"]`，默认 `skip_predicted`，由 worker（`apps/api/app/workers/tasks.py::batch_predict` / `_run_batch`）透传决定 task 选取与旧预测处理：

| 模式                     | task 选取                                                            | 旧预测处理                                                                             | 用途                                      |
| ------------------------ | -------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------------- |
| `skip_predicted`（默认） | task 选取追加 `.where(Task.total_predictions == 0)`，跳过已预标 task | 不动                                                                                   | 重复点"批量预标"时幂等，只补没跑过的 task |
| `overwrite`              | 不过滤已预标 task                                                    | 图片推理成功且最终守卫通过后，逐题清理旧预测并与新候选一起提交（**不重置 task 状态**） | 改了 prompt / 阈值想重跑                  |
| `append`                 | 不过滤                                                               | 不清，无脑追加                                                                         | 兼容旧行为，仅特殊场景                    |

注意：

- `skip_predicted` 模式下，进度条分母（`projects.py` 触发处）也排除已预标 task，避免"跳过的 task"虚增分母让进度显示卡住。
- `overwrite` 只清 AI 预标产物（复用 [`clean_task_predictions`](./batch-module#clean-task-predictions)），手工标注 `source="manual"` 不受影响，task 状态不回退；与删批次/`reset_to_draft` 的级联清理是同一套清理逻辑。
- 图片执行每题保留原子事务：全部 `Prediction` / `PredictionMeta`、覆盖清理及显式 partial 的 `FailedPrediction` 一起提交。异常先回滚，再单独保存失败记录；成功数和费用只统计已提交结果。清理中的原始 SQL 更新后，预测写入在 Task 锁下刷新物化计数，避免复用旧 ORM 计数。
- 图片 worker 在结果提交前锁定并刷新 `AsyncJob`；取消 API 使用同一行锁。取消先获得锁则丢弃当前推理结果，结果事务先获得锁则保留该题已提交结果，再跳过后续题目。下一题开始前也通过短作业锁观察已经排队的取消，随后提交释放锁，推理期间不持有作业锁。作业元信息合并前刷新取消标记，最终批次状态与作业终态在同一事务提交；取消 API 先提交数据库标记，再向 broker 发尽力撤销。
- `execution_unit="frame"` 在独立 owner 中执行，保留派发前清理的既有合同；其分布式逐帧写入不属于上述图片逐题原子替换合同。

## 多阶段预标注（pipeline_stages，路径 B）

`PreannotateRequest.pipeline_stages` 把单阶段批量预标泛化为**平台层跨 backend 编排**：源阶段（如 detect）产框 → 下游阶段（如 classify / box-seg）对每个父框按各自路由方式投递 → 结果合并回同一框。缺省（无 `pipeline_stages`）与单阶段 `batch_predict` 逐字等价，完全向后兼容。决策与边界见 [ADR 0043 — 多阶段预标注编排](../adr/archive/0043-staged-preannotation-pipeline)。

### 请求形态

```jsonc
// POST /api/v1/projects/{id}/preannotate 请求体节选
{
  "ml_backend_id": "<source-backend-uuid>", // 源阶段 backend（兼容字段，等价 stages[0].ml_backend_id）
  "model_id": "vehicle-detect", // 源阶段 model
  "pipeline_stages": [
    {
      "stage": 1, // 1-based 阶段序号
      "ml_backend_id": "<source-backend-uuid>",
      "model_id": "vehicle-detect",
    },
    {
      "stage": 2,
      "parent_stage": 1, // 父阶段（声明并行兄弟时同 parent_stage 即可挂多个 stage=2）
      "ml_backend_id": "<onnxtools-uuid>",
      "model_id": "vehicle-attr-classify",
      "parent_class_filter": ["car", "truck"], // 只对父框 class_name 命中时启动；其余降级保留纯检测
      "roi": { "pad": 0.05, "mode": "crop" }, // 仅 crop 模式有效，pad ∈ [0, 0.5]
      "on_failure": "keep_parent", // keep_parent（默认）/ drop_box
      "on_key_conflict": "reject", // reject（默认 422）/ last_wins（并行兄弟写同 key 时）
      "write": { "keys": ["vehicle_type", "color"] }, // 限定写回的属性 key 白名单（chip 多选）
    },
  ],
}
```

### 下游投递路由（`_resolve_input_mode`）

worker 解析投递模式：端点（`apps/api/app/api/v1/projects.py`）按下游 model 的 `supported_inputs` + `write.target` 烘焙 `input.mode`，worker（`apps/api/app/workers/tasks.py::_resolve_input_mode`）只读已烘焙的字段，缺省回落 `write.target` 启发式（产几何→`geometry`，产属性→`crop`）。

| `write.target` × 下游 `supported_inputs`                        | 投递模式               | 投递内容                                                                                  | 典型场景                                                         |
| --------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `attributes`（产属性，纯分类）                                  | `crop`                 | 平台按 `parent_class_filter` 裁父框 ROI（pad 按深度，root+1=5% / root+2=8% / root+3=12%） | yolo/onnxtools 纯分类，回属性合并进父框                          |
| `geometry` × box-prompt seg（`supported_inputs` 含 `geometry`） | `geometry`             | 全图 URL + 父框归一化坐标列表（`tasks[].prompts[]`）                                      | gsam2 `box-seg`：`set_image` 一次、N 框共享 embedding 出 polygon |
| `geometry` × 普通检测器（`supported_inputs` 含 `crop`）         | `crop`                 | 同上裁父框 ROI 喂下游 → 检出几何按 crop transform 回映回原图坐标                          | depth-3 crop-detect：父框 ROI 内检出新子物体，落库为新 polygon   |
| `intermediate`                                                  | `geometry` / `crop`    | 同上，但产出仅供下游消费，不落库                                                          | 中间几何（如 box-seg → 给孙子阶段做 ROI）                        |
| 缺省（旧 payload / 端点未烘焙）                                 | 按 `write.target` 推断 | 产几何 `geometry`，产属性 `crop`                                                          | 向后兼容回落                                                     |

### crop 投递通用化

crop 模式默认走 **presigned URL**：现有 HTTP 后端直接读取 import 桶内的 ROI，不需要修改 ML 镜像。批量预标与模型快照重试通过 `services/prediction_inputs.py` 创建独立 attempt，上传前在独立事务中提交 `prediction_input_leases`；对象使用 `prediction-inputs/<attempt UUID>/crops/<框索引>-<SHA>.jpg`，同题重试和同时执行的 job 不共用前缀。`_run_task_pipeline` 管理原图/PIL 句柄及输入 owner，成功、部分失败、普通异常或取消均在结束时主动清理本 attempt。纯函数调用仍可注入 `upload_crop`，或使用 `data:` 内联。

临时对象最多 10,000 个、总计 4 GiB，单个不超过 64 MiB；attempt 最长活动两小时，到期拒绝新上传。签名最长一小时且不超过剩余活动期，使用 `align=False`，通过 `audience="ml"` 在签名前选择模型可达端点，不替换已签链接的 Host；模型 Host 为空时使用内部端点，不继承浏览器公共地址。专用 S3 客户端连接/读超时为 3/15 秒，只尝试一次，避免 PUT 的自动重试掩盖仍可能迟到的旧请求。临时输入上传与签名错误只记录稳定代码，不将签名地址或 SDK 私有信息写入失败记录。

清理记录无业务级联外键，固定实际位置、精确对象清单和 endpoint 身份摘要；job、Task 或项目删除不能抹去回收意图。存储身份变化时拒绝清理，不跟随新配置删除其他后端。清理逐对象完整核对内容、写入标记、ETag 和不可变 VersionId，再删除准确版本；`IfMatch` 仅作额外条件，不能假设服务端一定执行该请求头。额外对象、未知版本或任意位置上同 purpose 的未收尾操作均阻止删除。正常清理后保留 `deleted` 墓碑，失败继续保留责任。过期或结果不确定的尝试保留七天恢复观察期，未收尾操作不因观察期结束解除；旧无固定位置的记录返回 `legacy_unpinned`，历史 null 版本回执返回 `immutable_version_required` 并保留。

`recover_prediction_inputs` 由 beat 每分钟派到 `cleanup`，每轮最多处理 50 条。托管原件许可在输入 owner 关闭之后结算，首次清理因此可能延后一轮；恢复延迟还取决于积压与存储可用性。应用启动移除自身创建的导入桶整桶七天过期规则，避免存储过期绕过责任账本；运维自设规则须独立核对。旧 `roi-crops/` 或其他未知导入对象不凭前缀猜测删除，需另行盘点。

原图快照由输入 owner 接管，通过同份快照产生 ROI，取消会等待已启动 SDK I/O 结束再关文件。该组件只负责资源所有权，引用消费的授权与最终写入由 `prediction_references.ReferencePredictionJob` 负责。

### 引用图片的执行资格

后台 `bulk` 和工作台单题图片预标在 API 冻结实际 Task 范围、Task.version、原请求凭据和来源事实，模型阶段写入不可变执行快照。引用成员另保存 Item/Asset/SHA/大小/尺寸及来源修订，并要求既有读取 scope；托管成员保留原入口权限要求。`skip_predicted` 先过滤，空集合不会重新扩展；pending 幂等重发始终复用首次范围、配置和来源。没有可信来源证明的旧图片队列明确拒绝并要求重新发起。

worker 通过统一引用 reader 捕获 strict snapshot，实际读源使用 `worker:<Celery hostname>` 的已核验锚点；按需完整缓存按同 Asset/SHA 和当前授权选择，并受位置额度/TTL约束，随后经 `PredictionInputs` 投递独占 `original/` 对象，geometry 复用同一 URL，ROI 从该快照解码，避免各阶段读到不同原件。逐阶段资格失败整题中止，不适用模型的 keep_parent/drop_box 软失败策略；引用在 frame 旧清理分支前拒绝。

最终短事务沿用作业取消锁、账号/项目/成员、原 Key、批次/Task，再持有来源/位置/连接和 Item/Asset 的生命周期锁，复核绑定与模型快照；节点探测仅用于实际读源资格。签发的专有 receipt 绑定当前 session、非嵌套事务、Task 和内容，提交或回滚即失效；`PredictionService` 仅据该 receipt 放行引用候选写入。持久元数据只存必要的内容及执行溯源，不能充当授权，也不保存 Key proof、原件定位配置或临时 URL。内容绑定与短锁 helper 由导出和预测复用，两者业务权限仍分别检查。

### 引用失败重试与采纳

两个 retry API 通过 `prediction_retries` 共用设备路由和准备：图片失败需有原模型快照及可信来源绑定，新提交固定当前 actor／凭据、Task.version 和来源状态，保留原图片内容与来源。新建持久 `prediction_retry`，FailedPrediction claim 绑定该 job UUID；原作业过期不改变快照身份，旧引用或托管图片失败缺少证明时均拒绝猜测恢复。CPU/GPU 路由在两个入口一致。

引用 worker 复用 `ReferencePredictionJob` 和同快照输入，不持 FailedPrediction 行锁等待原图或模型 I/O。最终按 job→账号/项目/成员→失败项→Key→批次/Task→内容生命周期锁复验本 attempt，再一起提交候选、同快照 partial 替代、失败删除和作业完成。异常先回滚业务，再用 job→失败项锁独立记录本次失败与计数；Celery failure/revoked 使用相同边界，重复或迟到回调只处理匹配的 attempt，不清除后续 claim。发布响应丢失时不撤销已运行的 attempt。

普通几何采纳由 `prediction_acceptance` 核验 URL Task 与候选归属、当前标注/读取权限及严格 API 原图，并与候选内容溯源比对。历史推理版本不作为永久门槛；当次操作固定当前版本与来源修订。最终持有账号/项目/成员、Key、批次/Task、Prediction/Meta 和内容生命周期锁，专有 receipt 绑定 session、非嵌套事务、actor、Task、候选及正文/溯源摘要，提交或回滚后失效。沿用类别、属性、贡献者、审核证据与统计流程。原生掩码使用各自专用资格，视频形态仍拒绝。同步交互和 predict-test 使用下述独立请求 owner；mask repair 和受控整图后台原生掩码分别核验生成记录，不全局放宽旧 URL resolver。

### 单层并行兄弟（声明式，非 Celery chord）

同一 `parent_stage` 可挂多个下游阶段（车辆框 → 颜色 + 车型 + 车牌 OCR 各写不同属性键），结果按 `write.keys` union 合并进同一框。当前实现为 **worker 内顺序串跑各兄弟阶段**，不是 Celery chord — chord 真并行作单列计划被搁置（详 `docs/plans/` 下 chord-parallelism plan，无实测 wall-clock 压力前不实施）。声明形态本身是「并行」语义（互不依赖），实施层是「顺序」——后续切 chord 不破协议。

并行兄弟写同一属性键时：

- `on_key_conflict=reject`（默认）→ worker 校验失败抛 422；前端阶段卡在**配置期**就用红字 + 红 chip 预警，不再跑完才 422。
- `on_key_conflict=last_wins` → 按阶段顺序末位覆盖。

### 上游几何可裁校验（`check_parent_geometry_roi`）

下游阶段按上游父框裁 crop 或转 `bbox_prompt` 作 ROI 时，**bbox / polygon / raster_mask** 可提供外接框（`roi._box_bbox_pct`）；原生 Mask 必须附经核验的 portable RLE，不能使用后端自报近似框。若上游模型自报的 `supported_geometric_outputs` 只输出其它形态（如仅 `keypoint` / `polyline`），该阶段的所有父框会在运行期被跳过、零富集。为把这种"跑完才发现是空"的场景提前到配置期暴露，`check_parent_geometry_roi`（`apps/api/app/services/pipeline_validation.py`）作为**上游输出侧的对称门**，与已有的「下游能否吃框」门（`supported_inputs` 含 `bbox_prompt` / `crop`）形成上下游对称：

| 上游 `supported_geometric_outputs`                 | 判决                   | 说明                                                            |
| -------------------------------------------------- | ---------------------- | --------------------------------------------------------------- |
| 未自报（老 backend）                               | 放过                   | 保零退化                                                        |
| 至少含一种可裁几何（bbox / polygon / raster_mask） | 放过                   | 部分不可裁交由运行期 `skipped_geometry` 兜底                    |
| 完全不含可裁几何（如仅 `keypoint`）                | 违例 `no_roi_geometry` | 保存编排时软提示 `capability_warnings`，触发预标时 **422 硬挡** |

两条通道复用同一纯函数：

- **保存编排** `PATCH /projects/{id}`：`_compute_pipeline_capability_warnings` 收集违例 detail 作为 `capability_warnings` 返回给前端阶段卡，红字预警但不拦保存（`apps/api/app/api/v1/projects.py`）。
- **触发预标** `POST /projects/{id}/preannotate`：dispatch-time 闸门 `_check_pipeline_capabilities` 命中直接 `HTTPException(status_code=422, detail=violations[0].detail)`，与「下游能否吃框」违例走同一 422 通道。

### trigger 响应的 `warnings` 字段

`POST /projects/{id}/preannotate` 成功入队时返回体新增 `warnings: string[]`，用于把**能跑但有代价**的选项（不硬挡的软提示）带回前端，与硬挡的 422 分开：

```json
{
  "job_id": "...",
  "status": "queued",
  "total_tasks": 12,
  "channel": "project:{id}:preannotate",
  "warnings": [
    "源阶段 output=both 会对同一实例产出「框 + 多边形」两条几何, 多阶段下游会各处理一次 (重复裁剪 / 推理 / 富集) 并落两条 region; 建议源阶段改用 box 或 mask。"
  ]
}
```

当前触发一条：源阶段 `output_mode=both` 且开启文本 prompt 又挂了下游阶段时，一条 both 结果会被两个下游各处理一次。空数组表示无软提示。硬校验违例不进这里，直接 422。

### ROI 模块（`roi.py`）

`apps/api/app/workers/roi.py` 集中所有 ROI 路由纯函数：

| 函数                          | 作用                                                                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `crop_inputs_from_boxes`      | 按父框裁 ROI crop（`pad` 加边、`parent_class_filter` 过滤），返回 `CropBatch(inputs, skipped_geometry)`            |
| `geometry_prompts_from_boxes` | 父框 → 归一化 prompts 列表（带 `parent_box_idx`），供 box-seg 出多边形与父框对齐                                   |
| `collect_geometry_shapes`     | 把 box-seg 返回的 polygon 按 `parent_box_idx` 还原到原图坐标，追加进父框预测                                       |
| `merge_classify_attributes`   | 把下游分类结果按 `write.keys` 白名单 union 进父框 `attributes`                                                     |
| `box_class_name`              | 从 LS shape 提取 `class_name`（`value.rectanglelabels[0]` / `value.labels[0]`），供 `parent_class_filter` 命中判定 |

旋转框 / 退化框命中阶段路由但几何不支持时计入 `stats[si].skipped_geometry`；多边形取外接框。原生 RLE 缺失、摘要不符、空前景或尺寸不符会明确失败，不计作普通几何跳过——见下文逐阶段统计。

### 逐阶段统计：实时快照 + 终态真值

worker 累加各阶段 stats（源阶段 `{detected}`、下游 `{targeted, ok, failed, skipped_geometry}`），有两条通道：

- **运行中实时快照**：按 5% 步长把当前累加 `pipeline_stages` 随进度推上 WS `project:{id}:preannotate`（复用现有通道，不新增）。前端阶段卡实时下放，显「待运行 / 运行中 / 已完成」徽标 + 计数 + `ProgressBar`。
- **终态真值**：`async_jobs.result.pipeline_stages` 落库（job 终态写一次）。WS 重连或运行后回看一律走终态字段，不丢。

若 GPU 仲裁在下游派发前拒绝，阶段仍按配置的 `keep_parent|drop_box` 处理，同时把完整根因记入 `PredictionMeta.extra.pipeline.gpu_arbiter_failures`。作业终态的 `async_jobs.result.gpu_arbiter_failures` 则按稳定错误码聚合 `status_code` / `retry_after_s` / `count`；`count` 表示被拒绝的派发次数，不替代 `pipeline_stages[].failed` 的受影响 ROI 计数。

### 拓扑落库与可追溯

`_pipeline_topology` 把 stages 配置派生为可审计拓扑落 `PredictionMeta.extra.pipeline`：

```json
{
  "stage_count": 2,
  "enriched_attr_keys": ["color", "vehicle_type"],
  "stages": [
    {
      "stage": 1,
      "ml_backend_id": "...",
      "model_id": "vehicle-detect",
      "parent_class_filter": null,
      "write_keys": null
    },
    {
      "stage": 2,
      "ml_backend_id": "...",
      "model_id": "vehicle-attr-classify",
      "parent_class_filter": ["car"],
      "write_keys": ["color", "vehicle_type"]
    }
  ]
}
```

这框的某属性来自哪个 backend / model 可逐条追溯——不改表（仍在 `PredictionMeta.extra` JSONB 内），`stage_count` / `enriched_attr_keys` 收口进同一 namespace。

### backend 来源、自训练模型与显存保护

- **全局注册 + 项目启用**：一个物理 backend 全局只注册一行，项目按需勾选启用，**没有项目级数量上限**（同一兼容实例也可执行多个检测阶段）。
- **项目自训练模型路由**：检测使用 `project-release`，分割使用 `project-release:segment`；新请求读取对应任务的当前发布并冻结为 `trained:{version_id}`，通过已核验的确切 YOLO 部署执行；原请求重试使用快照中的版本，与内置模型共享管线执行器（详见 [在线学习与训练体系](./training-datasets#_4-4-预标注动态回流与收据协议)）。
- **并发限速（`max_concurrency`）**：每个全局注册行的 `extra_params.max_concurrency` 始终限制单进程 / 事件循环的并行请求；`ML_BACKEND_ROUTER_MODE=off|observe` 时 API 与多个 Celery worker 仍会叠加，路由模式为 `enforce` 时再由 Redis route lease 收口为跨进程实例上限。
- **GPU 显存仲裁（`GPU_ARBITER_MODE`）**：
  - `observe` 模式：在所有可加载端点的 HTTP 发送前，按稳定 `gpu_resource_id` 对同卡预算和新鲜 residency 快照计算 `would-*` 决策，只写结构化日志且旁路短超时放行，不改变业务请求结果。
  - `enforce` 模式：使用 Redis lease 执行 Resident 快路与 cold admission；cold token 暴露后，完整 HTTP 响应触发新 challenge 探测，在逐卡持久锁内将 `Loading` 收口为 `Resident`、CPU fallback、`Unloaded` 或保守 `Unknown`。
  - **容量不足时的驱逐策略**：authority 在 target cold intent 之前按「优先级 + LRU」依次驱逐同一完整资源 ID 上的空闲 victim，仅在严格 drain ACK、新鲜 health proof 与终态 CAS 全部成功后释放预算；不确定结果保守收口为 `Unknown` 并停止后续驱逐。

### 引用图片的同步交互与原生掩码

`reference_inference.ReferenceImageOperation` 固定本次 Task.version、Item/Asset/SHA/大小/尺寸、来源/位置/连接修订与原发起 Key proof；来源物理读取另核对实际 API 节点锚点，完整缓存命中也需当前逻辑身份和权限。`interactive_predict` 和 `predict_test` 使用独立请求 UUID 及明确的输入账本 owner，不伪造业务 AsyncJob。初始权限与 Mask 提示授权后释放事务，再严格捕获原图并上传到独占临时输入；模型 setup 后、真正派发前再次校验。原图、掩码提示读取和模型 HTTP 均不跨持成员锁的事务。

响应边界取得账号/项目/成员、原 Key、Task 编辑 advisory、批次/Task 和内容生命周期锁，刷新批次状态及 fallback 分派，复核权限、绑定、版本与提示 Annotation 版本/digest；advisory 使用 try-lock，忙时返回 409。predict-test 保留管理者的可读测试语义，可以测试已完成任务；交互检查当前编辑权限、他人编辑锁与 AI 开关。请求取消时清理在受保护的取消范围内释放事务并回收输入；不确定 PUT 与硬中断仍由持久账本恢复。

交互候选的 HMAC receipt 和加密 logits 会话增加服务器生成的 `reference_input` 原图身份，续点同时核对内容、模型、参数和源 Mask。原生采纳在原有 atomic owner 之前重新严格核验原图及当前读取/写入权限，以独立私有 receipt 绑定 session、非嵌套事务、actor、Task、请求摘要和掩码 digest；Prediction writer 仅为这一专用 receipt 接受单个原生图片 Mask，普通后台 receipt 继续拒绝原生形态。Prediction、Annotation、贡献者、审计与 decision 仍同事务提交，精修保持源版本和 If-Match 合同。

decision 内保留非授权的原图溯源，15 分钟候选 receipt 到期后仍可在既有 24 小时 decision 期限内重放相同请求；重放同样复核当前权限和严格原图，不额外写候选。私有证据不在公开响应中输出，公开客户端仍只提交原有候选凭证。批量 mask repair 使用下述独立资格；后台原生掩码预标使用本页的逐结果资格。

### 托管图片的同步交互与原生掩码

`managed_image_inference.ManagedImageOperation` 由同步输入上下文分派，复用原有业务授权、`prediction_originals` 的凭据／canonical 原件核验与 `PredictionInputs` v2。短事务固定 Task.version、批次、完整来源事实和原 JWT／API Key 后提交，再分配输入并用同一 purpose 持有来源及目标许可，完整核验一次图片、解码真实尺寸并上传准确版本。Mask 提示先冻结 Annotation 元数据，原图准备后再读取 RLE；业务锁不跨原图／提示对象读取或模型 HTTP。setup 后复核，并在实际后端实例选中后通过 `before_dispatch` 再次核验、释放事务后发送原图 URL；响应末端沿调用者事务刷新 Batch／Task／来源与提示并调用 input `check_metadata`，不在最终锁下另开 guard session 或执行对象 I/O。

候选与 logits 使用独立 `original_input` 小型严格身份，与 `reference_input` 互斥；只含内容、实际尺寸、位置修订和 canonical 事实摘要，不含私有 locator 或凭据。原生接受重新完整读取并确认关闭源句柄，随后取得最终权限／来源锁，以独立私有 receipt 绑定同 session、顶层事务、actor／Task、请求、Mask 摘要、可信尺寸及原凭据身份和期限。Prediction／Annotation／像素尺寸校验和提交前的最后检查显式传递并验证该 receipt，不能凭普通尺寸字典放行；Mask 对象写入期间过期或 JWT 被注销都会回滚业务结果。凭据复核沿当前事务与授权缓存执行，不另开会话；证据写 original_input。Decision 的内部来源身份供精确重放并在公开响应剥离，旧无来源证据不回填。

物理工作保留真实 Future 并排空，未知关闭标记不会被后续 close 的 no-op 消除。同步模型输入关闭在事务释放后进行；来源仍持同 purpose 时，首次准确回收延期属于正常保护。接受仅重新读取原件，不生成模型输入或新后台任务。视频与引用的既有权限、身份和持久化合同保持独立。

### 托管图片的选中框二次推理

`managed_secondary_inference.ManagedSecondaryOperation` 复用同步原件适配器的凭据／canonical 绑定、完整 Snapshot 和取消收尾，以独立 `secondary_inference` 输入责任保存父 Annotation 的冻结事实。权限使用严格 annotation.write 判据及原 Key 的 annotations:write，不借交互工具开关或审核调整路径。纯参数与几何预检先于对象读取；deferred ROI 在线程中编码，取消时等待实际收尾，只将裁剪交给版本化输入 writer，不上传无用途整图或复用固定文件名。

实际路由选池后复核并释放短事务，再发送固定版本 URL。最终在调用者顶层事务锁定当前权限／Task／来源与父 Annotation，核对版本、几何、类别、属性／meta、结构和活动事实，再用刷新对象写属性或创建子框。父标注变化不能作为普通 AI 属性合并吞掉；整个子框集合、贡献者和 Audit 一起提交，提交前再次复核原凭据时钟与 JWT 注销缓存。正常退出先释放事务再关闭输入／来源，未知关闭保护延续输入账本的耐久责任。该路径直接写 Annotation，不伪造 Prediction、原生 Mask receipt 或异步 Job；引用、视频与批量修复保留各自合同。

### 引用图片的批量修复

`reference_mask_repair.ReferenceRepairUnit` 按分片/候选组管理固定引用内容、当前 Job 私有 Key proof、真实 Worker 节点严格快照及临时输入。确定性修复仅需审核写入与 native gate；SAM 另需 AI 开关，并在计划中固定模型声明、变体和参数。当前协议提供声明配置身份，不虚构 checkpoint 摘要。实际路由 transport 的 setup 在发送原图 URL 前核对冻结模型；最终短事务共享锁定 pool、项目启用关联、成员和 registry，保留实际 instance/pool 溯源。

Mutation 与 Prediction 分别消费绑定 session、非嵌套事务、actor/Task 及精确 payload 的内部 receipt，不全局放开引用写入。每个执行/恢复/回滚尝试取得新授权但保留原内容；回滚也严格读图。单元结束释放快照与输入账本，取消或失效时保持已有原子结果。

SAM Prediction、元信息、候选 ID 及生成时结果/元信息摘要与单元完成记录同事务提交。采纳核对完整计划与单元归属、源标注版本/几何摘要、当前原图和当前写权限；最终锁序为 Task/生命周期、Batch、RLE 内容、源 Annotation。Task 的历史版本不永久限制后续合法分派，父作业终态也不撤销已提交候选。重复/已驳回候选拒绝；驳回仅需要当前编辑权限，不读取原件。

### 托管图片的批量修复

`managed_mask_repair.ManagedRepairSource` 统一修复的 canonical 原件、请求凭据与真实尺寸，`ManagedRepairUnit` 负责每个确定性分片或 SAM 候选组。计划冻结完整原件关系、源 Annotation 事实与声明模型配置；Job 的托管授权与既有引用授权使用显式封套，旧引用计划保持原合同。公开计划从深拷贝中剥离内部证明，计划摘要仍对应完整持久内容。

原图完整读取与真实关闭在最终业务锁之外完成。SAM 输入有独立 `mask_repair` 责任与精确版本；实际 transport 的 setup、模型配置及当前授权在发送 URL 前核对。最终仅在调用者事务中复核元信息，原件或模型 I/O 不在此事务进行；既有 RLE 内容读取、所有权与代数校验继续执行。未知关闭保持耐久保护，不以本地 closed 标记或超时猜测已结束。

专属 `repair_original_receipt` 绑定 issuer、session、顶层事务、actor、Task、真实尺寸、精确 payload 及计划/单元/尝试。Prediction 仅允许单条修复候选，Mutation 在幂等返回前也验证 receipt；读 receipt 不能取得写资格。候选与 Meta 摘要保存在已提交单元内，确定性修复记录完整 after facts，后续分片、续做和严格回滚只从已提交检查点推进自身修改。

`managed_repair_acceptance` 对候选预览、采纳和已接受 Mask 读取复核当前原件及持久来源。采纳使用当前调用者的编辑权限，不永久依赖生成者 JWT 或 reviewer 身份；驳回无原件 I/O。普通直接 Item 手工 Mask 沿用原读取合同；独立 Task、primary 及已有来源证明的 Mask 通过受限读资格取得实际尺寸，不扩大手工写入许可。恢复只映射运行时定位，不改写计划、候选和初始授权事实，恢复后的旧证明不能自动续跑。

## 代码索引

- 模型：`apps/api/app/db/models/async_job.py`、`apps/api/app/db/models/prediction.py`
- Worker：`apps/api/app/workers/tasks.py::batch_predict` / `_run_batch` / `_run_task_pipeline` / `_resolve_input_mode`
- ROI 路由：`apps/api/app/workers/roi.py`（`crop_inputs_from_boxes` / `geometry_prompts_from_boxes` / `merge_classify_attributes`）
- async job service：`apps/api/app/services/async_job.py`
- 端点：`apps/api/app/api/v1/tasks/predictions.py`（结果查询 + `POST /tasks/{id}/predictions/{pid}/accept` `attribute_overrides`）、`apps/api/app/api/v1/projects.py::trigger_preannotation`（触发）
- 能力协商：`apps/api/app/services/ml_capabilities.py`（`extract_capabilities` / `derive_modalities`，含 `output_attribute_schema` / `composition` 字段）
- 前端：`apps/web/src/pages/AIPreAnnotate/`、`apps/web/src/hooks/useGlobalPreannotationJobs.ts`、`apps/web/src/pages/AIPreAnnotate/components/StageCard.tsx`（多阶段编排 UI）
- 迁移：`apps/api/alembic/versions/0085_drop_prediction_jobs.py`
