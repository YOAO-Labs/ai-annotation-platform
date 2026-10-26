---
title: 仓库地图：模块归属与调用链
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# 仓库地图：模块归属与调用链

> 本文回答「一个行为改哪里、沿着哪些真实符号走」。只列当前实现与允许依赖方向，
> 不复制目录树、不复制代码、不维护第二套角色矩阵（角色语义见
> [可见性与权限](./visibility-and-permissions)）。

## 1. 顶层结构与依赖方向

| 区域            | 位置                                                                                        | 允许依赖与职责边界                                                              |
| --------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 前端页面 / 装配 | `apps/web/src/pages/**`（含 `Workbench/`、`Training/`、`Projects/ProjectLearningPage.tsx`） | 领域 hooks → API client → 生成类型                                              |
| 前端领域状态    | `apps/web/src/pages/Workbench/state/**`、各页面 `*UrlState.ts`                              | 工作台状态模型、工作流 Hook 与无副作用纯函数转换器（`*UrlState` 不触网）        |
| 前端授权        | `apps/web/src/hooks/useProjectAccess.ts`、`src/components/routing/RequireProjectAccess.tsx` | 只消费后端返回的能力集，不在前端重算角色矩阵                                    |
| API 路由        | `apps/api/app/api/v1/**`                                                                    | 薄路由层：参数解析、权限守卫、调用服务层；不做业务规则推导                      |
| 服务层          | `apps/api/app/services/**`                                                                  | 授权真值在 `project_access`，聚合 SQL 在 `project_aggregates`                   |
| Worker          | `apps/api/app/workers/**`                                                                   | 复用服务层；作业终态统一走 `async_job_terminal`                                 |
| ML 共享运行时   | `apps/_shared/backend_runtime`                                                              | 无状态叶子函数 + `ManagedLruPool` 池内核                                        |
| 光栅掩码工具    | `apps/_shared/mask_utils`                                                                   | RLE 编解码、多边形与掩码转换等无状态图像/视频共享工具                           |
| ML 协议         | `apps/_shared/protocol_v2`                                                                  | 五个 backend 共用的 schema 与受控词表                                           |
| 任务契约        | `apps/_shared/task_contract`                                                                | 纯 Python 统一视觉操作名（`detect`/`segment`/`classify` 等）与别名映射          |
| 训练参数契约    | `apps/_shared/training_contract`                                                            | 纯 Python 原生框架字段快照、catalog 依赖校验、预算与回流契约                    |
| 只读文件原语    | `apps/_shared/filesystem_read`                                                              | 标准库 Linux `openat2` 只读目录/文件锚点校验与 `0600` 本地 `VerifiedStage`      |
| 站点 Agent 契约 | `apps/_shared/site_agent_contract`                                                          | 中心与跨网络只读站点 Agent 共用的命令、回执、摘要与协议模型                     |
| 站点存储 Agent  | `apps/storage-agent`                                                                        | 独立出站 HTTPS 只读目录扫描与分块传输守护进程（零入站监听、本地 SQLite 状态）   |
| 训练执行器      | `apps/training-executor`                                                                    | 独立拉取式执行器，OS `flock` 协调同一资源的执行器，管理原生训练子进程与脱敏日志 |
| ML backend      | `apps/{grounded-sam2,sam3,yolo,rapidocr,onnxtools}-backend`                                 | 各自的模型池包装、观测指标、GPU 生命周期与受控模型热加载                        |

## 2. 授权真值（唯一来源）

- **平台身份与项目能力解析**：`apps/api/app/services/project_access.py`
  - `resolve_project_access()` / `resolve_project_access_by_id()`：请求期真值，每次重读账号与项目，未知的平台或项目角色一律 fail closed。
  - `is_privileged_for_project()`：纯函数，管理型访问 = 活跃超管，或项目负责人且平台角色为管理型。
  - `membership_role_compatible()`：校验账号平台角色与项目职责的合法组合。
- **聚合 SQL 的同一契约**：`apps/api/app/services/project_aggregates.py`
  - `valid_membership_conditions()`：成员行仅在账号活跃、平台/项目角色均为现行值、且观察者仅持观察者职责时生效。
  - `project_scope_clause()`：聚合查询的范围谓词；对停用或未知角色直接返回空集。
- **前端消费**：`useProjectAccess` 与 `RequireProjectAccess` 直接消费后端下发的能力集，不自行推导权限。

## 3. 调用链：进入标注 → 编辑提交 → 异步处理 → 通知

1. **路由进入**：`apps/web/src/App.tsx` → 页面路由；项目内入口经 `RequireProjectAccess` 调用 `useProjectAccess` 取得能力集。
2. **后端鉴权**：`app/deps.py::get_current_user`（停用账号返回 `401`）→ 路由内调用 `resolve_project_access()` 建立不可变 `ProjectAccess`。
3. **派题与锁**：`app/services/scheduler.py` 派题；任务锁续期与抢占语义位于 `app/services/task_lock.py`。
4. **工作台装配与编辑提交**：
   - **外壳与导航**：`pages/Workbench/shell/WorkbenchShell.tsx` + `state/useWorkbenchShellModel.tsx`；任务切换准入在 `state/taskNavigation.ts`（latest-wins 调度、离开守卫、提交闸门、本地 URL 同步）。
   - **光栅与视频交互**：原生 Mask 变更工作流位于 `state/useMaskMutationWorkflows.ts`（错误策略纯模块 `state/maskMutationPolicy.ts`），后端经 `app/services/mask_mutation.py` 收口版本冲突与幂等键；视频 Mask 纠错位于 `state/useVideoMaskCorrection.ts`；PVS 种子采集与传播位于 `state/useTrackerSeedCollection.ts`（种子分组纯模块 `state/trackerSeedPrompts.ts`）。
   - **批量预标后端选择**：`state/useBatchBackendSelection.ts`。
5. **异步终态收敛**：Celery 信号兜底位于 `app/workers/signals.py`（`task_failure` / `task_revoked` → 查作业 → 通用终态 → 领域回填 → 通知 → 提交）；领域终态差异（`partial` / `rollback_failed` / `cancelled`）由 `app/services/async_job_terminal.py::DOMAIN_TERMINAL_RECONCILERS` 封闭分派。
6. **实时通知**：`app/services/notification.py::notify_job_terminal` 在调用方 `commit()` 之前写入并发布；列表与投递路径分别通过 `_scope_conditions()` 与 `_allowed_delivery_indices()` 复用 `valid_membership_conditions()`。

## 4. ML Backend 调用链

1. **协议契约**：`apps/_shared/protocol_v2` 定义请求/响应 schema 与受控词表；前端生成类型与 API 快照由 `pnpm codegen` 与 CI 同步校验。
2. **共享池内核**：`apps/_shared/backend_runtime/src/aap_backend_runtime/managed_pool.py` 提供 builder 单飞、borrower 串行、租约、quarantine 清理与强制回收。
3. **Backend 本地实现**：各推理服务的 `model_pool.py` / `video_pool.py` / `pool_domain.py` 负责模型级包装，`gpu_lifecycle.py` 维护 admission / generation / drain 状态机。
4. **平台侧路由与调用**：`apps/api/app/services/ml_routing/` 执行服务池能力匹配与加权路由，选中物理实例后交由 `app/services/ml_client.py` 发起 HTTP 请求。

## 5. 在线学习与模型训练调用链

1. **数据版本化**：`apps/api/app/services/training_datasets.py` 单语句快照项目已审核标注；Celery `export` 队列（`app/workers/training_dataset.py`）校验原图 SHA-256 并物化为 YOLO/COCO 归档包，上传至 MinIO `training` 桶。
2. **执行调度与超参搜索**：`app/services/training_runs.py` 与 `training_studies.py` 管理训练实验及参数网格搜索（由 Beat `check_studies` 推进），通过数据库资源租约排队。
3. **独立执行器通信**：`apps/training-executor/training_executor/` 轮询 `/api/v1/internal/training/*` 认领实验，以 OS `flock` 协调同一资源的执行器，守护原生训练子进程并通过 `/observations` 上报采样指标与脱敏日志。
4. **模型评估、回流与主动选样**：
   - 负责人注册 `TrainedModelVersion` 后，由 `yolo-backend`（`/managed-models`）校验权重收据并完成样本试运行，发布为 `ModelRelease`，通过 `model_id="project-release"` 接入预标注流水线与工作台。
   - 冻结逐样本评估（`app/services/sample_evaluations.py`）与主动学习选样（`app/services/active_learning.py`）在 `ml.cpu` 队列执行，自动重训策略（`app/services/learning_policies.py`）由 Beat `check_policies` 周期巡检。
5. **模型谱系与决策**：`app/services/model_lineage.py`（路由 `app/api/v1/model_lineage.py`）提供单跳谱系投影与 `model_decisions` 追加式处置记录。详见[在线学习与训练体系](./training-datasets)。

## 5.5 数据集、目录来源与受控存储调用链

1. **存储位置、连接器与九用途绑定**：`app/services/storage_connection.py`（Fernet 加密 + `connector_guard.py` SSRF 白名单）与 `storage_location.py` 管理外部连接及四类存储位置（`filesystem` / `s3` / `sftp` / `agent`）；`dataset_binding_change.py` 与各用途 `*_storage.py` 管理九用途绑定切换与 27 家族保存目标（`storage_ref`）隔离。位置级访问准入与排空由 `storage_access.py` 和 `storage_maintenance.py` 维护。
2. **目录扫描、预检登记与增量同步**：`app/services/dataset_scans.py`、`dataset_registration.py`、`dataset_source_changes.py`、`dataset_source_revisions.py` 与 `dataset_source_sync.py` 编排目录枚举（底层调用 `apps/_shared/filesystem_read`、`sources/s3_reference.py`、`sources/sftp_reference.py` 或 `agent_storage.py`）、六类增量差异预检、内容修订与 Beat 周期同步。
3. **跨网络只读站点 Agent**：`apps/storage-agent` 轮询 `app/api/v1/site_agent_transport.py`（`app/services/agent_transport.py`），借由 `apps/_shared/site_agent_contract` 校验命令并上传 `256 KiB` 分块；显式同内容备用来源由 `dataset_reference_replicas.py` 调度。
4. **原件验证读取、缓存与保留回收**：`app/services/original_sources.py`、`managed_original_reader.py`、`original_binary.py` 与 `original_playback.py` 提供严格内容核验读取、认证下载与原生视频分片舞台；`original_cache.py` 管理按需完整原图缓存；`managed_original_creation.py`、`managed_original_retirement.py`、`managed_original_retention.py`、`managed_original_verification.py` 与 `managed_original_reclaim.py` 管理条件创建、删除保留账本、声明保留要求与准确版本回收。
5. **数据集工作台、质量检测与存储迁移**：`app/services/dataset_workbench.py` 提供六态观测聚合、组合筛选与关联项目可见任务统计；`dataset_quality.py` 与 `dataset_quality_review.py` 执行可恢复图片质量检测与固定范围批量复核；`storage_migration_*.py` 与 `storage_usage*.py` 提供配置源迁移清单、复制批次、维护窗口、已封存读取映射（`PhysicalReadPlan`）、九用途原子切换与六分类容量盘点。详见[存储位置、连接器与用途绑定](./storage-connections)。

## 6. 测试层级与运行入口

分层口径、运行命令与接线状态见[测试指南](/dev/testing)：

- **后端测试**：纯规则测试不落库；事务、锁顺序与通知测试使用独立测试数据库连接。
- **前端测试**：页面测试优先在 MSW API 边界描述响应（`src/test/` 提供最小 fixture）；纯 URL 与几何规则下沉至 `*UrlState.test.ts` 与几何单测。
- **ML、存储 Agent 与共享契约包测试**：5 个 ML backend、`apps/_shared` 下 7 个共享包（`backend_runtime` / `mask_utils` / `protocol_v2` / `task_contract` / `training_contract` / `filesystem_read` / `site_agent_contract`）、`apps/training-executor` 与 `apps/storage-agent` 均可在纯 CPU 环境下独立运行 pytest（其中 ML 与相关共享包通过 `scripts/run-ml-cpu-tests.sh` 在 CI `.github/workflows/ml-cpu-test.yml` 中执行）。

## 7. 改动时的对齐约定

- **权限入口**：项目资源路由统一通过 `resolve_project_access` 做项目级授权；认证、健康检查与平台级管理入口使用各自的守卫（`get_current_user` / `require_roles`）。
- **异步作业终态**：带领域账本的异步作业必须在 `DOMAIN_TERMINAL_RECONCILERS` 登记；无领域账本的作业（如 `batch_predict`）仅维护通用 `AsyncJob` 行。
- **URL 状态解耦**：页面筛选与 URL 同步规则放在 `*UrlState.ts` 并配纯函数测试。
- **共享包边界**：向 `apps/_shared/*` 新增能力前先确认多消费方共用同一契约；单模型专有语义留在具体 backend 内部。
