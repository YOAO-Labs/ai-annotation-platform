---
title: 后端分层
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-26
---

# 后端分层

## 分层规则

```text
HTTP → 路由 (api/v1) → 服务 (services) → 模型 (db/models)
                ↓
              Schemas (pydantic) — 请求/响应契约边界
```

**单向依赖原则**：路由层调用服务层，服务层读写 ORM 模型；严禁反向调用或跨层循环导入。

## 路由层（`api/v1`）

- **薄壳职责**：解析请求参数、执行权限守卫（`resolve_project_access` / `get_current_user`）、调用服务层、序列化响应。
- **零业务推导**：路由层不直接拼装复杂业务 SQL 或状态迁移分支。
- **OpenAPI 契约完备**：每个端点必须提供清晰的 docstring，并为所有非 200 状态码显式声明 `responses={}` 类型。

## 服务层（`services`）

服务层承载核心状态机、业务规则、跨表事务与外部 HTTP 通信（如调用 ML Backend）。规模较大的业务按领域拆分为独立子包（Domain Packages）：

| 领域子包                   | 核心职责                                                         | 内部模块分层与边界                                                                                                                                                   |
| :------------------------- | :--------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`gpu_arbitration/`**     | GPU 显存契约、租约账本、Redis Fence、静默卸载与抢占编排          | `contracts` / `policy` 为无环叶模块；`fences` / `proofs` / `reconciliation` / `retirement` 居中；`dispatch` / `membership_activation` / `rollout_control` 为高层编排 |
| **`ml_routing/`**          | 服务池能力指纹、加权轮询（SWRR）、路由租约账本与排空安全校验     | `contracts` / `capability` / `policy` 为纯函数叶模块；`ledger` 维护 Redis 原子租约与被动熔断；`router` / `safety` / `diagnostics` 编排拓扑与快照                     |
| **`video_tracking/`**      | 视频 AI 追踪适配器、分窗身份与候选暂存审阅                       | `adapter` / `job` / `runner` 单向协作，不反向耦合 WebSocket 路由                                                                                                     |
| **`exporting/`**           | 项目/批次异步导出缓存、单飞锁与多格式归档打包                    | `cache` / `service` / 格式导出器                                                                                                                                     |
| **`data_management/`**     | Data Manager 多维过滤、聚合统计与批量任务操作                    | `schemas` / 查询原语 / 高层批量服务                                                                                                                                  |
| **`mask_formats/`**        | 光栅掩码（Raster Mask）导入导出格式注册表、预检与流式校验        | 统一管理 COCO RLE / PNG 等掩码格式转换契约                                                                                                                           |
| **`mask_qc/`**             | 提交期与抽样光栅掩码质检（几何连通性、空洞与边界核验）           | 异步质检计算与质检报告持久化                                                                                                                                         |
| **`point_cloud_quality/`** | 3D 点云场景与立体框质量度量                                      | 点云稀疏度、框体尺寸与跨帧一致性检查                                                                                                                                 |
| **`sources/`**             | 外部对象存储（S3/MinIO）与 SFTP 适配器（复制导入与严格持续引用） | SSRF 防护、DNS 重绑定校验、固定主机公钥校验与条件读取                                                                                                                |

此外：

- **数据集与受控存储服务群**（`storage_location*.py`、`storage_access.py`、`storage_maintenance*.py`、`dataset_sources.py`、`dataset_scans.py`、`dataset_registration.py`、`dataset_source_sync.py`、`dataset_workbench.py`、`dataset_quality*.py`、`original_sources.py`、`managed_original_*.py`、`original_cache.py`、`site_agents.py`、`agent_transport.py`、`storage_migration_*.py`、`storage_usage*.py`）围绕共享只读原语包 `apps/_shared/filesystem_read` 与站点协议包 `apps/_shared/site_agent_contract` 构建，并与独立守护进程 `apps/storage-agent` 协作（详见[存储位置、连接器与用途绑定](./storage-connections)）。
- **在线学习与训练相关服务**（`training_datasets.py`、`training_runs.py`、`training_studies.py`、`training_observability.py`、`trained_models.py`、`sample_evaluations.py`、`active_learning.py`、`learning_policies.py`、`model_lineage.py`）围绕共享契约包 `apps/_shared/task_contract` 与 `apps/_shared/training_contract` 构建，并与独立拉取式执行器 `apps/training-executor` 协作。

### 领域子包架构守卫

1. **单向导入**：稳定契约（`contracts`）与底层原语不反向导入高层编排模块；子包 `__init__.py` 保持轻量，避免 eager-import 整个依赖图。
2. **禁止反向依赖**：`app.services.*` 不得导入 `app.api` 或 `app.workers`；派发 Celery 任务时使用稳定任务注册名或由路由层触发。
3. **服务端角色裁剪**：如 `ml_routing.diagnostics` 构建的 `TopologyResponse` 与 `RuntimeSnapshotResponse` 按角色在服务端完成字段裁剪（非超管隐藏物理权重与底层实例状态），并携带 freshness 信封（`observed_at` / `partial` / `sources[]`），前端只做展示合并而不猜测业务真值。
4. **移除模块防回流**：历史平铺兼容层已彻底物理删除，由 `tests/test_domain_package_architecture.py`、`tests/test_compat_facades.py` 与 `scripts/check_removed_service_modules.mjs` 在 CI 中强制拦截。

## 模型层（`db/models`）

- 仅包含 SQLAlchemy 2.0 表结构定义、索引约束与轻量级 `@hybrid_property`。
- 不在 ORM 模型内编写跨表查询或业务状态变更方法。

## 契约层（`schemas`）

- 请求模型（`*In` / `*Create` / `*Update`）与响应模型（`*Out` / `*Response`）严格分离。
- 禁止直接将 SQLAlchemy ORM 实例原样暴露为未声明 schema 的字典。
- 嵌套结构必须显式定义为强类型 Pydantic 子模型（例如使用 `ProjectBatchSummary` 而非裸 `dict`），确保 `@hey-api/openapi-ts` 生成精确的前端 TypeScript 类型。

## 中间件与异步任务（`middleware` & `workers`）

- **Middleware**：负责 `slowapi` 速率限制、请求级 `X-Request-ID` 注入与结构化访问日志。
- **Workers（Celery）**：定义于 `app/workers/`，以 Redis 为 broker 与 result backend，按 7 类专职队列隔离消费（详见[后端基础设施](./backend-infrastructure)）。

## 测试映射

| 层级         | 测试位置与方式                                                                            |
| :----------- | :---------------------------------------------------------------------------------------- |
| **路由层**   | `apps/api/tests/test_<feature>.py`，使用异步 `httpx.AsyncClient` 测试完整 HTTP 契约与权限 |
| **服务层**   | `apps/api/tests/test_<service>_service.py`，直接验证业务状态机、锁顺序与边界条件          |
| **领域架构** | `apps/api/tests/test_domain_package_architecture.py`，静态校验子包依赖方向与无环约束      |
| **Workers**  | `apps/api/tests/test_<task>.py`，验证任务幂等性、失败回滚与终态通知                       |
