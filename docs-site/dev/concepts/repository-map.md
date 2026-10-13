---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-09-21
---

# 仓库地图：模块归属与调用链

> 本文回答「一个行为改哪里、沿着哪些真实符号走」。只列当前实现与允许依赖方向，
> 不复制目录树、不复制代码、不维护第二套角色矩阵（角色语义见
> [可见性与权限](./visibility-and-permissions)）。

## 1. 顶层结构与依赖方向

| 区域            | 位置                                                                                        | 允许依赖                                                      |
| --------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| 前端页面 / 装配 | `apps/web/src/pages/**`                                                                     | 领域 hooks → API client → 生成类型                            |
| 前端领域状态    | `apps/web/src/pages/Workbench/state/**`、各页面 `*UrlState.ts`                              | 纯函数，不触网                                                |
| 前端授权        | `apps/web/src/hooks/useProjectAccess.ts`、`src/components/routing/RequireProjectAccess.tsx` | 只消费后端返回的能力集                                        |
| API 路由        | `apps/api/app/api/v1/**`                                                                    | 服务层；不做业务规则推导                                      |
| 服务层          | `apps/api/app/services/**`                                                                  | 授权真值在 `project_access`，聚合 SQL 在 `project_aggregates` |
| Worker          | `apps/api/app/workers/**`                                                                   | 复用服务层；作业终态走 `async_job_terminal`                   |
| ML 共享运行时   | `apps/_shared/backend_runtime`                                                              | 无状态叶子函数 + `ManagedLruPool` 池内核                      |
| ML 协议         | `apps/_shared/protocol_v2`                                                                  | 五个 backend 共用的 schema 与受控词表                         |
| ML backend      | `apps/{grounded-sam2,sam3,yolo,rapidocr,onnxtools}-backend`                                 | 各自的池包装 / 观测 / 生命周期                                |

## 2. 授权真值（唯一来源）

- 平台身份与项目能力解析：`app/services/project_access.py`
  - `resolve_project_access()` / `resolve_project_access_by_id()`：请求期真值，
    每次重读账号与项目，未知的平台/项目角色 fail closed。
  - `is_privileged_for_project()`：纯函数，管理型访问 = 活跃超管，或项目负责人且平台角色为管理型。
  - `membership_role_compatible()`：账号平台角色与项目职责的合法组合。
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
4. 编辑与提交：Workbench 装配在
   `apps/web/src/pages/Workbench/shell/WorkbenchShell.tsx` +
   `state/useWorkbenchShellModel.tsx`；任务切换准入在 `state/taskNavigation.ts`
   （latest-wins 调度、离开守卫、提交闸门、本地 URL 同步）；原生 Mask 变更工作流在
   `state/useMaskMutationWorkflows.ts`（错误策略纯模块 `state/maskMutationPolicy.ts`），
   视频 Mask 纠错在 `state/useVideoMaskCorrection.ts`，PVS 种子采集与传播在
   `state/useTrackerSeedCollection.ts`（种子分组纯模块 `state/trackerSeedPrompts.ts`），
   批量线后端选择在 `state/useBatchBackendSelection.ts`；后端 Mask 写入经
   `app/services/mask_mutation.py`（版本冲突与幂等键在此收口）。
5. 异步处理：Celery 信号兜底在 `app/workers/signals.py`
   （`task_failure` / `task_revoked` → 连接 → 查作业 → 通用终态 → 领域回填 → 通知 → 提交 → 释放）；
   领域终态差异（`partial` / `rollback_failed` / `cancelled`）由
   `app/services/async_job_terminal.py::DOMAIN_TERMINAL_RECONCILERS` 封闭分派，
   信号兜底不重塑这些值。
6. 通知：`app/services/notification.py::notify_job_terminal` 在调用方 `commit()`
   之前发布；列表/投递两条路径分别用 `_scope_conditions()` 与
   `_allowed_delivery_indices()` 复用同一成员契约
   （`valid_membership_conditions()`），投递路径额外要求账号活跃。

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

## 5. 测试层级与运行入口

分层口径、运行命令与接线状态见[测试指南](/dev/testing)。速记：

- 后端纯规则测试不落库；事务/锁/通知顺序测试用独立连接。
- 前端页面测试优先在 MSW API 边界描述响应（`src/test/` 提供最小 fixture）；
  纯 URL/几何规则下沉到 `*UrlState.test.ts` / 几何单测。
- ML backend 与 `apps/_shared` 三个共享包（`backend_runtime` / `mask_utils` / `protocol_v2`）
  的 `tests/` 执行入口是可复用工作流 `.github/workflows/ml-cpu-test.yml`（`workflow_call` /
  `workflow_dispatch`）与 `scripts/run-ml-cpu-tests.sh`（8 套件、每套件独立 venv；gs2/sam3
  装 CPU torch，yolo 不用 ultralytics/torch）。这些文件全部 CPU 可运行，不存在“硬件专属”
  测试；阶段接线状态见仓库优化台账。

## 6. 改动时的对齐参考

以下是现行实现的归属约定，不是新增门禁：

- 项目资源路由在 `resolve_project_access` 上做项目级授权；认证、健康检查与
  平台级管理入口各有自己的守卫（`get_current_user` / `require_roles` 等）。
- 带领域账本的异步作业在 `DOMAIN_TERMINAL_RECONCILERS` 登记；
  没有领域账本的作业（如 batch_predict）保持只有通用作业行。
- 页面 URL 规则放 `*UrlState.ts` 并配纯函数测试；页面保留代表性集成用例。
- 共享包新增能力前，先确认消费 backend 是否同契约；per-model 语义留在 backend 本地。
