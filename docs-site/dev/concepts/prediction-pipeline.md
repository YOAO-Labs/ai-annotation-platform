---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-27
---

# 预标注流水线（Prediction Pipeline）

AI 预标注流水线以 `async_jobs(kind=batch_predict)` 作为用户可见任务真值，以 `predictions` / `failed_predictions` 保存可采纳结果和失败项。本页讲清应用用例、Celery 派发边界、持久化五态及写入时点。

`application/preannotation_contract.py::BatchPredictionRequest` 固定现有任务参数，`preannotation_execution.run_preannotation` 持有业务资源生命周期、Jobs／结果账本和最终写回。模型任务、阶段图、ROI、上下文及结果转换由 `aap_inference.execution_pipeline`、`execution_context`、`roi` 和 `secondary` 持有，通过同一不可变 `ModelExecutionPort` 执行；调用方不再使用旧模型客户端或直接打开后端凭据。

HTTP 经注入的 `BatchPredictionDispatcher` 派发，`workers/batch_prediction_dispatch.py` 保留原 Celery 参数、执行 ID、设备队列及 tracker 超时；`workers/tasks.py::batch_predict` 保留原注册名/签名与失败钩子。Assets 继续提供已校验输入与 crop 发布，Jobs／Labeling 持有原始／准备结果、最终候选和终态；Inference 不代替这些 owner 提交。

帧模式在应用的同一 Session 中逐任务读取元数据，仅把任务 ID/帧数、阶段配置与作业/执行 ID 交给帧派发端口。`workers/frame_preannotate.py` 负责段规划、Redis 进度和两阶段 chord；无帧时由应用完成空作业，派发后由既有帧任务完成预测与收尾。每次模型调用通过 [Inference 私有执行协议](/api/guides/inference-runtime)，保留精确 task IDs、实际实例、原响应上限与期限、结构化错误和 transport observation；不在不确定响应后重发模型请求。

> 决策依据：[ADR 0014 — Prediction Jobs 历史表](../adr/archive/0014-prediction-jobs-table)

<!-- history: prediction_jobs was the early dedicated table; current docs describe async_jobs as the single job source. -->

## 状态机

<ExcalidrawDiagram
  src="/diagrams/shared/ai/batch-predict-job-lifecycle.svg"
  alt="Celery 队列执行边界与 AsyncJob pending、running、completed、failed、cancelled 五态的真实关系"
  caption="批量预测作业生命周期：API 先持久化 pending，再派发给 worker"
/>

| 状态        | 何时进入                                                           | 关键字段                                                         |
| ----------- | ------------------------------------------------------------------ | ---------------------------------------------------------------- |
| `pending`   | HTTP 受理在派发前调用 `create_job()` 并提交                        | `created_at`, `payload.ml_backend_id`, `payload.execution_scope` |
| `running`   | worker 认领同一持久作业并提交执行状态                              | `started_at`, `celery_task_id`, `progress_pct`                   |
| `completed` | 无可处理 task，或至少一题成功；可同时有 `failed_count > 0`         | `completed_at`, `result.success_count`, `result.failed_count`    |
| `failed`    | 未认领时派发失败、全部子项失败，或 worker 未捕获异常 / 失败 signal | `completed_at`, `error_message`                                  |
| `cancelled` | pending 可直接取消；running 记录取消意图后由 worker 收敛           | `completed_at`, `result.cancelled_at_index`                      |

`cancel_requested` 是 `running` 上的 payload 标记，不是第六个状态。调用 `revoke(terminate=False)` 也不会让已运行 worker 立即跳到 `cancelled`。单题 backend 5xx / timeout 通常会被记为 `FailedPrediction`；只有全部失败才让根 job 进入 `failed`。

`retry-failed` 不会让原 job 回到 `pending / running`，而是按失败项创建独立 `kind=prediction_retry` 作业。每日清理只删除以 `created_at` 为截止的过期 `completed / failed / cancelled`；`pending / running` 不会因超龄自动收敛。

## 端到端时序

<ExcalidrawDiagram
  src="/diagrams/shared/ai/ai-preannotation-handoff.svg"
  alt="FastAPI 先提交持久预标注作业再入队，worker 认领同一作业、经服务池推理并保存候选，再由 owner 人工接管"
  caption="批量预标端到端主链，共享同一份 canonical 可编辑资产"
/>

HTTP 入口对普通批量、Data Manager、工作台和受控模型请求统一先提交 `AsyncJob`，再把该 UUID 作为 Celery 执行 ID。返回的 `job_id` 可在 worker 启动前直接查询；派发异常仅将仍为 pending 的行原子地转为 failed。broker 可能已接收消息但丢失确认，已认领或已结束作业的状态因此由执行侧保留。幂等重放沿用原作业，没有幂等键的新请求创建新作业。内部旧 Celery 调用仍可省略预建 ID，由应用建立作业；它不是当前 HTTP 受理合同。

项目级与批次级受理都会记录当时待处理数量提示，`skip_predicted` 排除已有预测。pending 取消使用该提示统计跳过数；该数量并不冻结任务成员，执行时仍按既有范围规则选择任务。

## 与 `predictions` 表的边界

| 用途                                  | 查哪张表                                                  |
| ------------------------------------- | --------------------------------------------------------- |
| 列出"现在能采纳的候选框"              | `predictions`（按 task 过滤）                             |
| 列出"AI 跑了哪几次、成功失败、谁触发" | `async_jobs`（`kind=batch_predict` / `prediction_retry`） |
| 重置批次后回看历史                    | **只能** `async_jobs`（`predictions` 已被清）             |
| 工作台读取候选 → 渲染紫框             | `predictions` 经 `to_internal_shape` adapter              |

详见 [API Schema 边界](./api-schema-boundary)。

## 最终候选写入所有权

`labeling_write_scope` 定义不可变任务/批次事实：实际 actor、操作、执行范围、可用的 Task 版本、状态、分派、批次、审核轮次和作业身份；二次推理与 Mask 修复另冻结源 Annotation ID/版本。`known=false` 与明确的 `null` 不互换，作用域对象本身不提供权限，也不构成远程授权令牌。

`labeling_prediction_contract.ApplyPredictionResultPort` 接收类型化请求及该作用域，返回预测 ID、创建时间与派生工具单位；不传 Session 或返回 ORM。`PredictionService.apply_prediction_result()` 在调用方原事务内实现写入，在任何候选、Meta 和任务预测数写入前调用 `labeling_write_guard`。准入复用 `project_access.lock_current_project_access()`，按账号→项目→成员锁定并读取当前权威，再锁定批次/任务并复核冻结前置条件、当前编辑锁和实际作业的主体、项目、范围与终态。项目行共享锁覆盖所有权迁移，编辑 advisory 锁覆盖写入至提交期间的编辑锁抢占；调用方保留原提交边界。端口用稳定 code 和 retryable 区分忙锁与失效作用域，入口仍投影原 HTTP 状态/detail，不新增远程重试或独立提交。

- 批量仍要求 pending task 与 active / 未分派 draft 批次；工作台仍用同一 `WORKBENCH_AI_EDITABLE_TASK_STATUSES` 白名单并允许本人编辑锁。批次仅在仍为 active、未被管理员锁定且当前任务/编辑集合符合该执行范围时推进；draft 不自动推进。
- 外部导入保留项目管理准入与原任务阶段范围，不强行套用批量 pending 规则。覆盖清理先持有当前管理权限；失败重试和候选写入同样携带真实 actor，不以项目所有者补造身份。
- 实际终态作业不能接收迟到候选。运行中 `cancel_requested` 仍只是协作取消意图，保留原单项完成与下一处理边界收敛语义；不将请求取消等同于立即终止。
- 普通采纳/拒绝同时绑定实际 Prediction 的 task/project 与 URL 作用域，错题或跨项目 ID 返回 404。原生交互接受快照仍不能经普通采纳接口重复创建标注。
- 专用 `create_test_seed_prediction()` 是唯一明确无主体的夹具命令：再次核对非 production、显式 E2E 开关及数据库实际名称的 `_e2e` / `_test` 后缀，保留不创建 Meta/不增加预测数的夹具语义；这不能授权普通候选命令。

进入推理前的 `labeling_write_scope` 还记录 Task 来源代次、素材关联、路径和类型；最终写入持有 Task 锁后复核，换源以 `prediction_source_changed` 拒绝，即使 Task 版本未变或路径已经改回原值。该检查约束 Task 绑定；对象同键覆盖和素材自身代次仍待[媒体输入合同](./accepted-media-inputs)补齐。

逐帧执行从持久作业恢复 actor、batch 和 `execution_scope`，冻结实际任务的批次；缺失范围不猜成默认 bulk。最终写入遇到账号、项目、成员、批次、任务或作业的短暂锁竞争时，回滚后以带随机抖动的指数退避重试，含首次写入每轮最多 5 次。持续繁忙时由原 Celery 段任务进入 `RETRY`，按最长 30 秒的退避延迟重新派发，没有锁竞争重试次数上限；该帧不计失败，chord 保持等待。

重试消息携带当前帧的推理结果、后端／池与计费来源、原始 `labeling_write_scope`、剩余帧和累计段统计，采用现有 Kombu JSON 序列化保留 UUID。恢复先保存已有结果，不重复推理，也不以新任务状态重建写入授权；每次保存仍执行完整准入检查。段结束才累计一次 Redis 进度，重试不重复计数；延迟派发前续期 Redis chord 的已完成兄弟段结果与帧进度键。权限、范围或编辑锁拒绝仍按真实失败记录；终态作业不再接收迟到结果。恢复入口观察协作取消请求并跳过剩余帧，最终回调锁定作业后写入 `cancelled`，不覆盖已有终态。队列、注册名、模型冻结身份、分区 Meta 的时间键和原逐帧提交保持。

`Task.version` 不是所有 Annotation 编辑的全局修订号；ROI 结果必须另外核对实际源 Annotation 版本。二次推理引擎仅返回 `secondary_inference_result.SecondaryInferenceResult` 不可变事实，`SecondaryInferenceWritePort` 将原作用域和产物交给同事务 SQL 实现；私有 `AnnotationService._apply_secondary_inference()` 在最终准入后写属性/子框、贡献证据及实际属性变更的 Annotation 版本。端口返回原 `SecondaryInferenceResponse`，不外传 ORM；人工属性优先级、来源、原心跳/审计/提交与刷新顺序不变，审计模型池查询仍在写应用之后。引擎的媒体/ML I/O 和长推理前的只读提交保持原组合。

## 工具单位（tool_unit）维度

`predictions.tool_unit_id String(30)` 是必填字段：

- 写入时由 `PredictionService.apply_prediction_result` 调 [`derive_tool_unit_from_result`](../../../apps/api/app/services/prediction.py) 按 `result[0].type` 派生:
  - `polygonlabels` / `brushlabels` / `multi_polygon` → `region`
  - `polylinelabels` → `polyline`
  - `rectanglelabels` → `bbox`; 若 `value.rotation` 字段存在 → `rotated_bbox`
  - `keypointlabels` → `keypoint`; 其它未知 LS type → `bbox` 占位
- `to_internal_shape()` 出参也带 `tool_unit_id`, 供前端候选层 / AAP JSON 导出消费
- `CandidateDecisionPort.accept()` 创建的 annotation 沿用 prediction 的 `tool_unit_id` (与项目 `tool_bindings[unit].classes` 软校验保一致)

详见 [annotation-module · 工具单位](./annotation-module#工具单位tool_unit维度) 与 [ADR-0026](../adr/archive/0026-tool-unit-class-and-attribute-binding)。

## 普通逐题批量的结果恢复

普通 `batch_predict` 在开始推理前冻结完整处理成员和原始 `LabelingWriteScope`。Jobs 持有工作进程、代次、令牌和到期时间，以及覆盖清理、游标、费用与阶段统计；Labeling 持有分块原始结果、规范化后的准备结果和稳定 Prediction ID／创建时间。原始结果字节完整后，Mask 字节生成、引用登记和最终写入均可继续重试，不能因短暂竞争把它改记为失败。

最终写入继续经过当前账号／项目／Task／来源／编辑锁／Job 准入。Prediction、Meta、精确回执、快照和根释放及成员进度共用一次提交；提交确认丢失时读取回执，不再创建候选或失败行。正常写入每轮三次快速重试，仍忙则延后同一 Celery 消息；late ack 和 worker-loss redelivery 从已存原始或准备结果恢复，不重新选择任务、清理覆盖或调用模型。尚未完整存下原始字节的计算不提供一次执行保证。

原始／准备结果按单个模型响应保存，512 KiB 分块和 2 MiB 请求／响应只限制传输，不限制整题结果总量。待保存的 Mask 引用同时被当前内嵌 collector 和 native 根表保护。协作取消阻止下一成员，当前已开始成员可先完成；真实终态拒绝新写入。失败预测重试复用这份结果账本，并增加独立准入和派发协议；逐帧和场景路径继续持有各自协议。完整契约与受限执行角色见[同库数据接口](/api/guides/module-data#普通批量结果检查点)。

## WebSocket 通道

| 通道                         | 谁订阅        | 内容                                         |
| ---------------------------- | ------------- | -------------------------------------------- |
| `project:{id}:preannotate`   | 项目进度 hook | 单项目高频进度 / 错误；当前 WS 无鉴权        |
| `global:prediction-jobs`     | 任何 admin    | 兼容全局 in-flight 进度推送                  |
| `/api/v1/async-jobs` polling | 登录用户      | Topbar `JobsBell` 与 `/ai-pre/jobs` 任务历史 |

`JobsBell` 是当前用户可见任务的主入口；WebSocket 仍用于更快地刷新预标注进度。

## 失败兜底（B-1 教训）

`_BatchPredictTask.on_failure` 尽力推送未捕获异常；只有尚未绑定持久执行摘要的旧调用才按 `celery_task_id` 反查并置失败。普通批量的终态由持有当前原生租约的工作流写入，broker task ID 不能代替执行权证明。存储或回执不确定时保留检查点并延后；当前没有覆盖全部作业类型的 stale-job watchdog，逐帧 callback 等独立路径仍按自身恢复协议处理。

详见 [Docker rebuild vs restart](../troubleshooting/docker-rebuild-vs-restart)。

## 能力协商与模态路由

### 能力快照落库

`check_health`（`apps/api/app/services/ml_backend.py`）在拉完 `/health` 后 best-effort 探一次 backend `/setup`，调 `extract_capabilities`（`apps/inference/src/aap_inference/capabilities.py`）把能力快照写进 `ml_backend_registry.health_meta["capabilities"]`：

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

`/ai-pre` 执行页按项目 `data_type` 在前端分流，不再统一进入批量预标流水线：

| `data_type` | `/ai-pre` 行为                                                                                      |
| ----------- | --------------------------------------------------------------------------------------------------- |
| `image`     | 文本批量预标面板 → 走本页描述的 `batch_predict` 流水线                                              |
| `video`     | 引导卡片（`VideoPreannotateGuide`）→ 跳工作台逐轨迹 Shift+T 发起追踪，**不进入批量 predict 流水线** |
| `lidar`     | 显示占位提示，批量预标注入口暂不执行                                                                |

视频项目的 AI 标注通过工作台 video tracker 发起，运行态保留 `video_tracker_jobs` 专表；历史汇总在 `/ai-pre/jobs?tab=video`，由 `async_jobs(kind=video_tracker)` 提供。

**数据边界**：本页的批量预标流水线以 `Prediction` 为候选存储，按 shape 采纳；交互视频 tracker 则把逐帧、多目标结果暂存在 `VideoTrackerJob.staged_result`，按 job 接受 / 丢弃，接受后才写入轨迹 annotation。两者共享 ML Backend 能力协议，但不共享候选表、计数或状态机。视频链路详见[视频 AI 追踪架构](./video-ai-tracking)。

## 按后端动态参数透传

`PreannotateRequest`（`apps/inference/src/aap_inference/preannotate.py`）新增 `params: dict | None` 字段，由前端按选中 backend 的 `/setup.params` 用 SchemaForm 渲染并按 backend 记忆（`User.preferences.ai.params_by_backend`）后带上。

应用命令（`apps/api/app/application/preannotation_execution.py::run_preannotation`）构建 `/predict` context 时：

```python
if params:
    context.update({k: v for k, v in params.items() if v is not None})
```

即 `params` 的非 None 值覆盖项目级 `box_threshold` / `text_threshold` 兜底值。无 `params` 时使用项目级阈值兜底，状态机本身不变。

## 幂等预标模式（predict_mode）

`PreannotateRequest.predict_mode`（`apps/inference/src/aap_inference/preannotate.py`）是 `Literal["skip_predicted", "overwrite", "append"]`，默认 `skip_predicted`，由应用命令（`apps/api/app/application/preannotation_execution.py::run_preannotation`）决定 task 选取与旧预测处理：

| 模式                     | task 选取                                                            | 旧预测处理                                                                                          | 用途                                      |
| ------------------------ | -------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `skip_predicted`（默认） | task 选取追加 `.where(Task.total_predictions == 0)`，跳过已预标 task | 不动                                                                                                | 重复点"批量预标"时幂等，只补没跑过的 task |
| `overwrite`              | 不过滤已预标 task                                                    | 预标前先 `BatchService(db).clean_task_predictions([t.id])` + flush 清旧预测（**不重置 task 状态**） | 改了 prompt / 阈值想重跑                  |
| `append`                 | 不过滤                                                               | 不清，无脑追加                                                                                      | 兼容旧行为，仅特殊场景                    |

注意：

- `skip_predicted` 模式下，进度条分母（`projects.py` 触发处）也排除已预标 task，避免"跳过的 task"虚增分母让进度显示卡住。
- `overwrite` 只清 AI 预标产物（复用 [`clean_task_predictions`](./batch-module#clean-task-predictions)），手工标注 `source="manual"` 不受影响，task 状态不回退；与删批次/`reset_to_draft` 的级联清理是同一套清理逻辑。

## 多阶段预标注（pipeline_stages，路径 B）

`PreannotateRequest.pipeline_stages`（定义于 `apps/inference/src/aap_inference/preannotate.py`）把单阶段批量预标泛化为**平台层跨 backend 编排**：源阶段（如 detect）产框 → 下游阶段（如 classify / box-seg）对每个父框按各自路由方式投递 → 结果合并回同一框。缺省（无 `pipeline_stages`）与单阶段 `batch_predict` 逐字等价，完全向后兼容。决策与边界见 [ADR 0043 — 多阶段预标注编排](../adr/archive/0043-staged-preannotation-pipeline)。

命名预标编排的保存、读取、apply 与本页描述的执行流水线属于相邻层次：结构仍共用 `aap_inference.pipeline.validate_saved_pipeline`，便携命令由 `aap_inference.pipeline_manager.PipelineManager` 持有，API 路由经 `services/inference_pipelines.py` 选择事务或独立 owner。六个公开路由的 DTO 不变；这些命令不会改变本页所述 `PreannotateRequest` 或 worker 执行合同。

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

API 端点（`apps/api/app/api/v1/projects.py`）按下游 model 的 `supported_inputs` + `write.target` 烘焙 `input.mode`，`aap_inference.execution_pipeline.resolve_input_mode` 只读已烘焙的字段，缺省回落 `write.target` 启发式（产几何→`geometry`，产属性→`crop`）。

| `write.target` × 下游 `supported_inputs`                        | 投递模式               | 投递内容                                                                                  | 典型场景                                                         |
| --------------------------------------------------------------- | ---------------------- | ----------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `attributes`（产属性，纯分类）                                  | `crop`                 | 平台按 `parent_class_filter` 裁父框 ROI（pad 按深度，root+1=5% / root+2=8% / root+3=12%） | yolo/onnxtools 纯分类，回属性合并进父框                          |
| `geometry` × box-prompt seg（`supported_inputs` 含 `geometry`） | `geometry`             | 全图 URL + 父框归一化坐标列表（`tasks[].prompts[]`）                                      | gsam2 `box-seg`：`set_image` 一次、N 框共享 embedding 出 polygon |
| `geometry` × 普通检测器（`supported_inputs` 含 `crop`）         | `crop`                 | 同上裁父框 ROI 喂下游 → 检出几何按 crop transform 回映回原图坐标                          | depth-3 crop-detect：父框 ROI 内检出新子物体，落库为新 polygon   |
| `intermediate`                                                  | `geometry` / `crop`    | 同上，但产出仅供下游消费，不落库                                                          | 中间几何（如 box-seg → 给孙子阶段做 ROI）                        |
| 缺省（旧 payload / 端点未烘焙）                                 | 按 `write.target` 推断 | 产几何 `geometry`，产属性 `crop`                                                          | 向后兼容回落                                                     |

### crop 投递通用化

crop 模式默认走 **presigned URL** 而非 `data:` base64：平台把裁好的 ROI 上传 import 桶（key=`roi-crops/{job_id}/{task_id}/{box_idx}.jpg`），挂 7 天 lifecycle 自动清，URL 经 [`StorageService.rewrite_host_for_ml_backend`](../../../apps/api/app/services/storage.py) 重写到 backend 可拉取的 host。所有走 `httpx.get` 的下游 backend（gsam2 / sam3）零改造可作分类阶段。`data:` 内联保留为纯函数快路径（单测 / 已知支持 `data:` 的 backend：onnxtools / yolo），由 `_make_crop_uploader` 是否注入 `upload_crop` 选择路径。

### 单层并行兄弟（声明式，非 Celery chord）

同一 `parent_stage` 可挂多个下游阶段（车辆框 → 颜色 + 车型 + 车牌 OCR 各写不同属性键），结果按 `write.keys` union 合并进同一框。当前实现为 **worker 内顺序串跑各兄弟阶段**，不是 Celery chord — chord 真并行作单列计划被搁置（详 `docs/plans/` 下 chord-parallelism plan，无实测 wall-clock 压力前不实施）。声明形态本身是「并行」语义（互不依赖），实施层是「顺序」——后续切 chord 不破协议。

并行兄弟写同一属性键时：

- `on_key_conflict=reject`（默认）→ worker 校验失败抛 422；前端阶段卡在**配置期**就用红字 + 红 chip 预警，不再跑完才 422。
- `on_key_conflict=last_wins` → 按阶段顺序末位覆盖。

### 上游几何可裁校验（`check_parent_geometry_roi`）

下游阶段按上游父框裁 crop 或转 `bbox_prompt` 作 ROI 时，只有 **bbox / polygon** 是可裁几何（`roi._box_bbox_pct`）。若上游模型自报的 `supported_geometric_outputs` 只输出其它形态（如仅 `keypoint` / `polyline`），该阶段的所有父框会在运行期被跳过、零富集。为把这种"跑完才发现是空"的场景提前到配置期暴露，`check_parent_geometry_roi`（`apps/api/app/services/pipeline_validation.py`）作为**上游输出侧的对称门**，与已有的「下游能否吃框」门（`supported_inputs` 含 `bbox_prompt` / `crop`）形成上下游对称：

| 上游 `supported_geometric_outputs`   | 判决                   | 说明                                                            |
| ------------------------------------ | ---------------------- | --------------------------------------------------------------- |
| 未自报（老 backend）                 | 放过                   | 保零退化                                                        |
| 至少含一种可裁几何（bbox / polygon） | 放过                   | 部分不可裁交由运行期 `skipped_geometry` 兜底                    |
| 完全不含可裁几何（如仅 `keypoint`）  | 违例 `no_roi_geometry` | 保存编排时软提示 `capability_warnings`，触发预标时 **422 硬挡** |

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

### ROI 模块（`prediction_roi.py`）

`apps/api/app/services/prediction_roi.py` 集中 ROI 数据与几何处理，批量 worker 和同步二次推理直接消费，不经 worker 转导出。原图 RGB 读取和规范化上下文由同层 `prediction_context.py` 持有；裁剪上传回调及每个父阶段独立的缓存仍由原调用方控制。

| 函数                          | 作用                                                                                                               |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `crop_inputs_from_boxes`      | 按父框裁 ROI crop（`pad` 加边、`parent_class_filter` 过滤），返回 `CropBatch(inputs, skipped_geometry)`            |
| `geometry_prompts_from_boxes` | 父框 → 归一化 prompts 列表（带 `parent_box_idx`），供 box-seg 出多边形与父框对齐                                   |
| `collect_geometry_shapes`     | 按有效 `parent_box_idx` 收集已在原图坐标中的 box-seg 结果，追加进父框预测                                          |
| `merge_classify_attributes`   | 把下游分类结果按 `write.keys` 白名单 union 进父框 `attributes`                                                     |
| `box_class_name`              | 从 LS shape 提取 `class_name`（`value.rectanglelabels[0]` / `value.labels[0]`），供 `parent_class_filter` 命中判定 |
| `remap_geometry_to_image`     | 将 crop 中 `[0,1]` 或 `[0,100]` 几何回映到原图，保留多连通多边形和洞                                               |

旋转框 / 退化框等命中阶段路由但几何不支持时计入 `stats[si].skipped_geometry`，不再静默；多边形父框按外接框裁剪。几何、索引、JPEG 编码和缓存语义不因所有者迁移改变。

### 逐阶段统计：实时快照 + 终态真值

worker 累加各阶段 stats（源阶段 `{detected}`、下游 `{targeted, ok, failed, skipped_geometry}`），有两条通道:

- **运行中实时快照**：按 5% 步长把当前累加 `pipeline_stages` 随进度推上 WS `project:{id}:preannotate`（复用现有通道，不新增）。前端阶段卡实时下放，显「待运行 / 运行中 / 已完成」徽标 + 计数 +`ProgressBar`。
- **终态真值**：`async_jobs.result.pipeline_stages` 落库（job 终态写一次）。WS 重连或运行后回看一律走终态字段，不丢。

发布由 `services/prediction_progress.py::publish_progress` 持有：普通进度只推项目频道，调用方提供 `job_meta` 时才另推 `global:prediction-jobs`；项目消息不混入该元数据。批量与逐帧执行直接调用共享发布者，仍沿用原节流、阶段统计和终态事务，不新建频道或作业状态机。

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

### backend 来源与显存保护

backend 走**全局注册 + 项目启用**：一个物理 backend 全局只注册一行，项目按需勾选启用，**没有项目级数量上限**（跨 backend 编排天然需 detect + classify ≥ 2，多阶段 DAG 还会更多）。每个全局注册行的 `max_concurrency`（`extra_params.max_concurrency`）始终限制单进程 / 事件循环的并行请求；`ML_BACKEND_ROUTER_MODE=off|observe` 时 API 与多个 Celery worker 仍会叠加，路由模式为 `enforce` 时再由 Redis route lease 收口为跨进程实例上限。GPU 仲裁 effective mode 只负责显存准入与驱逐。

设置 `GPU_ARBITER_MODE=observe` 后，平台会在所有可加载端点的 HTTP 发送前，按稳定
`gpu_resource_id` 对同卡预算和新鲜 residency 快照计算 `would-*` 决策。该阶段只写结构化日志，
旁路查询有严格短超时并在失败时放行业务请求，不会改变请求结果。所有生产派发口已注入惰性
authority，effective `enforce` 下可使用 Redis lease 执行 Resident 快路与 cold admission。cold token
暴露后，完整 HTTP 响应会触发新 challenge 探测，并在逐卡持久锁内立即把 Loading 收口为
Resident、CPU fallback、Unloaded 或保守 Unknown。容量不足时，authority 会在 target cold intent
之前按优先级 + LRU 依次驱逐同一完整资源 ID 上的空闲 victim，只在严格 drain ACK、
新鲜 health proof 与终态 CAS 都成功后释放预算；不确定结果保守收口 Unknown 并停止后续驱逐。
空闲和 busy victim 的等待、驱逐与取消路径均已接通；effective `enforce` 只有在运维显式开启 release latch、
且逐资源 rollout 成功收敛为 `enforcing` 后才会生效。legacy unload 仍不能作为显存已释放或预算已减账的证据。

## 失败预测重试边界

单项重试与按作业重试由 `app/application/prediction_retry.py` 受理，保留公开路由与角色入口。Labeling 原生准入在当前管理权围栏内固定原失败项、Task 作用域和推理参数，Jobs 同事务创建 pending Job、执行成员与 outbox；Job UUID 同时作为 Celery task ID。单项队列仍由原设备规则选择，按作业重试仍使用默认路由。

`aap-prediction-retries/v1` 提供准入、读取、成功／失败结算及派发领取／确认六项能力。执行器复用 `aap-preannotation-results/v1` 的租约、分块结果、稳定 Prediction 身份与 Mask 根；模型必须恰好返回一个原 Task 的结果。原始输出先保存，再校验来源与生产 Mask。完整 raw 之后的重投递直接恢复结果，临时输入等待、三次短写入重试耗尽和提交确认丢失均不增加失败次数。

成功时，实际 Prediction／Meta 证明、原失败行和 Meta 删除、回执、成员计数及 Job 终态共同提交。模型或 Mask 数据的永久失败只增加一次 `retry_count`，最多三次；账号、来源、dismiss 或作业状态失效则放弃当前 attempt，释放对应占用及临时根，不计模型失败。当前 Labeling guard 仍是最终写入条件。

发布成功但确认丢失时保留 outbox，定时 relay 使用函数专用连接重投相同 UUID。Celery 迟确认并在子进程丢失后重新投递；通用异常信号不能代替当前租约终结已受理重试。显式终态仍停止写入。迁移释放没有绑定 Job 的旧 pending marker，保留失败次数；迟到旧消息须重新通过同一准入。协议见[同库数据接口](/api/guides/module-data#失败预测重试)。

## 代码索引

- 模型：`apps/api/app/db/models/async_job.py`、`apps/api/app/db/models/prediction.py`
- 请求与派发：`apps/api/app/application/preannotation_contract.py`、`apps/api/app/workers/batch_prediction_dispatch.py`
- 应用编排：`apps/api/app/application/preannotation_execution.py::run_preannotation`、`_run_task_pipeline`、`_resolve_input_mode`
- Celery 适配：`apps/api/app/workers/tasks.py::batch_predict` 与失败钩子、`apps/api/app/workers/frame_preannotate.py` 的帧 chord
- ROI 路由：`apps/api/app/services/prediction_roi.py`（`crop_inputs_from_boxes` / `geometry_prompts_from_boxes` / `merge_classify_attributes` / `remap_geometry_to_image`）
- 上下文与原图：`apps/api/app/services/prediction_context.py`（`build_predict_context` / `model_label` / `load_task_image`）
- 进度发布：`apps/api/app/services/prediction_progress.py::publish_progress`
- async job service：`apps/api/app/services/async_job.py`
- 端点：`apps/api/app/api/v1/predictions.py`（结果查询 + `POST /tasks/{id}/predictions/{pid}/accept` `attribute_overrides`）、`apps/api/app/api/v1/projects.py::trigger_preannotation`（触发）
- 能力协商：`apps/inference/src/aap_inference/capabilities.py`（`extract_capabilities` / `derive_modalities`，含 `output_attribute_schema` / `composition` 字段）
- 前端：`apps/web/src/pages/AIPreAnnotate/`、`hooks/useGlobalPreannotationJobs.ts`、`pages/AIPreAnnotate/components/StageCard.tsx`（多阶段编排 UI）
- 迁移：`apps/api/alembic/versions/0085_drop_prediction_jobs.py`
