---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-16
---

# 仓库地图：模块归属与调用链

> 本文回答「一个行为改哪里、沿着哪些真实符号走」。只列当前实现与允许依赖方向，
> 不复制目录树、不复制代码、不维护第二套角色矩阵（角色语义见
> [可见性与权限](./visibility-and-permissions)）。

## 1. 顶层结构与依赖方向

| 区域            | 位置                                                                                        | 允许依赖                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| 前端页面 / 装配 | `apps/web/src/pages/**`                                                                     | 领域 hooks → API client → 生成类型                                                               |
| 前端领域状态    | `apps/web/src/pages/Workbench/state/**`、各页面 `*UrlState.ts`                              | 纯函数，不触网                                                                                   |
| 前端授权        | `apps/web/src/hooks/useProjectAccess.ts`、`src/components/routing/RequireProjectAccess.tsx` | 只消费后端返回的能力集                                                                           |
| API 路由        | `apps/api/app/api/v1/**`                                                                    | 应用工作流或服务层；不做业务规则推导                                                             |
| 应用工作流      | `apps/api/app/application/**`                                                               | 显式装配领域服务、协调用例事务与派发顺序；不持有领域状态机                                       |
| 服务层          | `apps/api/app/services/**`                                                                  | 项目授权在 `project_access`，任务访问/写入准入在 `task_access`，聚合 SQL 在 `project_aggregates` |
| Worker          | `apps/api/app/workers/**`                                                                   | 复用工作流与服务；终态信号仅适配事件、连接及调用方提交                                           |
| ML 共享运行时   | `apps/_shared/backend_runtime`                                                              | 无状态叶子函数 + `ManagedLruPool` 池内核                                                         |
| ML 协议         | `apps/_shared/protocol_v2`                                                                  | 五个 backend 共用的 schema 与受控词表                                                            |
| 任务契约        | `apps/_shared/task_contract`                                                                | 纯 Python 统一视觉操作名（detect/segment/classify 等）与别名映射                                 |
| 训练参数契约    | `apps/_shared/training_contract`                                                            | 纯 Python 原生框架字段快照、catalog 依赖校验、预算与回流契约                                     |
| 训练执行器      | `apps/training-executor`                                                                    | 独立拉取式执行器，文件锁独占 GPU，管理原生训练子进程与脱敏日志                                   |
| ML backend      | `apps/{grounded-sam2,sam3,yolo,rapidocr,onnxtools}-backend`                                 | 各自的池包装 / 观测 / 生命周期                                                                   |

## 2. 授权真值（唯一来源）

- 平台身份与项目能力解析：`app/services/project_access.py`
  - `resolve_project_access()` / `resolve_project_access_by_id()`：请求期真值，
    每次重读账号与项目，未知的平台/项目角色 fail closed。
  - `is_privileged_for_project()`：纯函数，管理型访问 = 活跃超管，或项目负责人且平台角色为管理型。
  - `membership_role_compatible()`：账号平台角色与项目职责的合法组合。
- 任务资源访问与标注写入准入：`app/services/task_access.py`
  - `assert_task_visible()` / `visible_task_ids()`：单题与批量资源可见性。
  - `assert_task_editable()` / `assert_annotation_write_allowed()`：阶段锁与项目写能力。
  - `resolve_effective_task_assignee_id()` / `assert_effective_task_assignee()`：有效分派人。
  - `assert_review_adjustment_evidence()`：审核调整前的冻结非本人证据。
  - API 依赖、WebSocket 与写入所有者直接调用，不通过路由私有函数重新导出。
- 异步最终写入组合：`app/services/project_write_guard.py::assert_phase_write_allowed`；
  复用上述服务与当前项目授权，保留调用方的锁顺序与事务归属。
- 管理者写命令：`app/services/project_write_guard.py::lock_project_manager`；
  与 `resolve_project_access(lock_membership=True)` 复用账号→项目→成员当前权威锁，
  训练/模型/评估不再依赖训练数据集路由辅助函数。
- 推理候选最终写入：`app/services/labeling_write_scope.py` 冻结主体/目标/源版本事实，
  `labeling_write_guard.py` 消费 `project_access.lock_current_project_access()` 的
  账号→项目→成员当前权威；候选/Meta/计数、标注采纳及批次推进由各自标注所有者写入，
  不把历史作业或 Scope DTO 当授权。
- 作业结果可见性：`app/services/async_job_access.py::can_access_job`；
  明细与取消重查当前项目、导出能力及审核任务范围，不把历史作业归属当作当前授权。
- 账号与责任交接：`app/application/{user_lifecycle,legacy_user_lifecycle,project_membership,project_workspace}.py`
  组合身份、workspace 与 labeling 所有者；账号写入在
  `services/{user_lifecycle,identity_commands,account_groups}.py`，
  成员行在 `services/project_membership.py`，任务/批次交接在 `BatchService`，
  GDPR 审计历史脱敏在 `AuditService`。预览/CAS、传统删除与离职、SQL/Redis 时序不混同。
- 失败预测管理：`app/services/prediction.py::assert_can_manage_failed_prediction`；
  单项重试、放弃和恢复共享当前项目管理准入，不从 API 路由导入规则。
- 贡献与审核事实：`app/services/annotation_evidence.py` 持有轮次、贡献者快照、
  当前轮冻结证据与首次审核事实；提交审计缺失时只按既有规则回退保留行。
  API 和片段服务直接消费，不再经路由私有快照函数。
- 聚合 SQL 的同一契约：`app/services/project_aggregates.py`
  - `valid_membership_conditions()`：成员行只在账号活跃、平台/项目角色均为现行值、
    且观察者只持观察者职责时生效。
  - `project_scope_clause()`：聚合查询的范围谓词；对停用或未知角色直接返回空集。
- 前端：`useProjectAccess` / `RequireProjectAccess` 只消费后端下发的能力集，
  不在前端重算角色矩阵。

## 3. 调用链：进入标注 → 提交 → 异步处理 → 通知

1. 路由进入：`apps/web/src/App.tsx` → 页面路由；项目内入口经
   `RequireProjectAccess` 调 `useProjectAccess` 取得能力集。
2. 后端鉴权：`app/deps.py::get_current_user`（停用账号 401）→
   路由内 `resolve_project_access()` 建立不可变 `ProjectAccess`。
3. 取任务：`app/services/scheduler.py` 派题；任务锁语义在 `app/services/task_lock.py`。
   视频共享上下文和采样在 `app/services/video_context.py`；帧服务负责构建，
   片段、协作、导出、追踪与质检单向依赖该下层所有者。
4. 编辑与提交：Workbench 装配在
   `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` +
   `state/useWorkbenchShellModel.tsx`；任务切换准入在 `state/taskNavigation.ts`
   （latest-wins 调度、离开守卫、提交闸门、本地 URL 同步）；原生 Mask 变更工作流在
   `state/useMaskMutationWorkflows.ts`（错误策略纯模块 `state/maskMutationPolicy.ts`），
   视频 Mask 纠错在 `state/useVideoMaskCorrection.ts`，PVS 种子采集与传播在
   `state/useTrackerSeedCollection.ts`（种子分组纯模块 `state/trackerSeedPrompts.ts`），
   批量线后端选择在 `state/useBatchBackendSelection.ts`；后端 Mask 写入经
   `app/services/mask_mutation.py`（版本冲突与幂等键在此收口）。
   素材关联/初始建题和入库后的追加建题经 `app/application/dataset_task_creation.py`；
   `services/dataset` 持有素材事实，`services/labeling_tasks` 持有建题、媒体关联与项目任务数增量，
   素材/帧传递使用 `services/dataset_task_facts`，共享分组在 `services/dataset_frame_grouping`。
   分块提交、追加 flush-only 和标定前置提交不同，不能合并成一种事务。
   解绑/素材删除、项目删除和无源任务清理由 `application/dataset_deletion.py`、
   `project_deletion.py`、`project_task_cleanup.py` 组合领域所有者；
   保留原本地原子事务、不同清理子集、计数口径和提交后派生存储清理，不是远程删除协议。
   生命周期、审核及单题/整批送审经 `application/task_lifecycle.py`、`task_review.py`、
   `task_submission.py`；状态/部分项目计数写入归 `LabelingTaskService`，证据归
   `annotation_evidence`，应用组合反馈、审计和通知。整批默认 200 题分块提交，
   不与单题事务或最终批次投影提交合并。
5. 领域执行派发：
   - 质量：`app/services/quality_job_dispatch.py::QualityJobDispatcher` →
     `app/deps.py::get_quality_job_dispatcher` 装配
     `app/workers/quality_dispatch.py::CeleryQualityJobDispatcher`；
     覆盖三类质检、Mask 修复/回滚和格式导入。
   - 媒体：`app/services/media_job_dispatch.py::MediaJobDispatcher` →
     `app/deps.py::get_media_job_dispatcher` 装配 `app/workers/media_dispatch.py`；
     数据集入库、视频缓存和资产重试共用该端口，连接器导入 worker 自行装配。
   - 训练数据集：`app/services/training_dataset_dispatch.py::TrainingDatasetDispatcher` →
     HTTP 的 `app/deps.py::get_training_dataset_dispatcher` 或周期策略 worker →
     `app/workers/training_dataset_dispatch.py::CeleryTrainingDatasetDispatcher`；
     领域保留 version/job/attempt 与派发失败竞态处理，适配器保留线程卸载。
   - 执行取消：`app/services/job_execution_control.py::JobExecutionControl` →
     `app/deps.py::get_job_execution_control` 装配 `app/workers/job_execution_control.py`；
     只请求非强制撤销，不把请求回执当作已停止证明。
   - 失败预测重试：`app/services/prediction_retry.py::PredictionRetryDispatcher` →
     `app/deps.py::get_prediction_retry_dispatcher` 装配 `app/workers/prediction_retry_dispatch.py`；
     单项接收预选执行队列，批量失败项保留原默认路由，不用任务注册名充当接口。
   - 视频追踪：`app/services/video_tracking/dispatch.py::VideoTrackerJobDispatcher` →
     HTTP 的 `app/deps.py::get_video_tracker_job_dispatcher` 或 Mask 修复 worker 入口装配
     `app/workers/video_tracker_dispatch.py`；领域只收作业 UUID，不携带 Celery 注册名和队列。
6. 异步处理：`app/workers/signals.py` 将 `task_failure` / `task_revoked` 交给
   `app/application/job_terminal.py`，由工作流组合通用状态、领域回填及通知，
   信号适配器仍控制连接和提交。六类领域回填由各自服务的 `reconcile_job_terminal` 持有，
   经不可变 `app/services/async_job_terminal.py::JobTerminal` 传递事实；
   评估与主动学习走各自中断规则，不抹平 `partial`、`rollback_failed` 或非终态取消意图。
   HTTP 取消经 `app/application/job_cancellation.py` 组合准入和领域 `request_job_cancel`，
   保留 pending 立即取消、running 协作取消、评估取消意图与数据版本当前尝试隔离；
   事务仍由路由提交，列表/明细/取消的外部协议未变。
   失败项重试经 `app/application/prediction_retry.py`，先提交占用再派发；
   发布异常仅释放派发调用尚未返回成功的项，保留已返回成功项的排队标记。该多阶段事务不与取消用例混为一谈。
7. 通知：`application/task_review.py` / `task_lifecycle.py` 保存审核/重开持久通知并 `commit()` 后调用
   `NotificationService.publish_committed()`；通用作业终态仍按
   `app/services/async_job_notify.py::notify_job_terminal` 的既有合同在调用方提交前写入和发布。
   列表/投递分别用 `_scope_conditions()` 与 `_allowed_delivery_indices()` 复用
   `valid_membership_conditions()`，投递路径额外要求账号活跃；不要混淆两种提交时序。
   讨论流的 `services/task_discussion.py` 与 `services/discussion_notifications.py` 直接调用
   `project_access.resolve_project_access_by_id` 和任务可见性所有者，不依赖 HTTP 装配；
   对实际接收人复核当前项目/任务资格，移除成员后不再成为受限讨论通知目的地。

## 4. ML backend 调用链

1. 协议：`apps/_shared/protocol_v2` 定义请求/响应 schema 与受控词表；
   `apps/web` 的生成类型与 API 快照由 CI 再生成，不手改。
2. 池内核：`apps/_shared/backend_runtime/src/aap_backend_runtime/managed_pool.py`
   提供 builder 单飞、borrower 串行、租约、quarantine 清理与强制回收；
   各 backend 通过 `logger_name` 保留自己的日志身份。
3. backend 本地实现：`model_pool.py` / `video_pool.py` / `pool_domain.py`
   是 per-model 包装，`gpu_lifecycle.py` 是 admission / generation / drain 状态机；
   这些**不**共享，因为池拓扑与 vendor 语义不同。
4. 平台侧消费：`apps/api/app/services/ml_client.py` 与
   `app/api/v1/ml_backends.py` 调 backend 的 HTTP 协议端点。
5. 共享预测准备：`app/services/prediction_context.py` 构建上下文与读取原图，
   `prediction_roi.py` 负责裁剪和回映，`prediction_progress.py` 发布现有项目/全局进度。
   HTTP、同步二次推理、批量及逐帧 worker 直接导入下层所有者；
   原执行编排与结果写入事务不变，没有增加语言替换所需的远程协议。

## 5. 在线学习与模型训练调用链

1. 数据版本化：`apps/api/app/services/training_datasets.py` 快照项目已审核标注；Celery export worker (`app/workers/training_dataset.py`) 下载校验原图素材并物化为 Ultralytics YOLO 格式数据包，上传至 MinIO `training` 存储桶。
2. 执行调度与排队：`app/services/training_runs.py` / `training_studies.py` 管理训练实验与参数搜索计划，通过数据库资源锁和租约在多框架、多实验间排队。共同下层由 `training_adapters.py` 持有资源标识、配置目录与参数解析，`training_claims.py` 持有项目事务锁及策略/搜索的领取准入，不反向依赖编排服务。
3. 执行器通信：`apps/training-executor` 通过 `/api/v1/internal/training` 拉取实验与任务凭据，以 OS flock 独占 GPU 文件锁，拉取不可变训练包并守护训练子进程；阶段耗时、资源采样与脱敏日志通过 `/observations` 上报。
4. 产物与模型回流：`training_artifacts.py` 统一流式产物校验、观测摘要校验与有界 JSON 读取，执行终态、初始化和模型注册直接消费该所有者。训练完成后由执行器上传不可变权重与完整检查点；负责人核验最终评估后注册为 `TrainedModelVersion`；在已认证的 YOLO backend (`/managed-models`) 完成 CPU 加载与样本试用后发布（`ModelRelease`），最终接入 Workbench 与预标注流水线 (`model_id="project-release"`)。
5. 谱系与决策：`app/services/model_lineage.py` 提供单跳追溯，`model_decisions` 记录负责人处置（发布、归档、说明追加）。详见[在线学习与训练体系](./training-datasets)。
6. 评估与主动学习的共同下层：`sample_evaluation_sources.py` 负责当前标注切面与原图条件读取，直接消费纯引擎限制和存储；`active_learning_references.py` 持有版本/评估引用及删除保护。主动学习编排、评估清理和项目/素材/任务删除入口使用同一引用所有者，保留原 409 与事务顺序，不通过高层编排互相导入。

## 6. 测试层级与运行入口

分层口径、运行命令与接线状态见[测试指南](/dev/testing)。速记：

- 后端纯规则测试不落库；事务/锁/通知顺序测试用独立连接。
- 前端页面测试优先在 MSW API 边界描述响应（`src/test/` 提供最小 fixture）；
  纯 URL/几何规则下沉到 `*UrlState.test.ts` / 几何单测。
- ML backend 与 `apps/_shared` 五个共享包（`backend_runtime` / `mask_utils` / `protocol_v2` / `task_contract` / `training_contract`）
  的 `tests/` 执行入口是可复用工作流 `.github/workflows/ml-cpu-test.yml` 与 `scripts/run-ml-cpu-tests.sh`。这些测试全部 CPU 可运行，不存在“硬件专属”测试。

## 7. 改动时的对齐参考

以下是现行实现的归属约定，不是新增门禁：

- 项目资源路由在 `resolve_project_access` 上做项目级授权；认证、健康检查与
  平台级管理入口各有自己的守卫（`get_current_user` / `require_roles` 等）。
- 带领域账本的异步作业在 `application/job_terminal.py` 显式绑定领域处理器；
  没有领域账本的作业（如 batch_predict）保持只有通用作业行，正常 worker 完成路径不变。
- 页面 URL 规则放 `*UrlState.ts` 并配纯函数测试；页面保留代表性集成用例。
- 共享包新增能力前，先确认消费 backend 是否同契约；per-model 语义留在 backend 本地。
