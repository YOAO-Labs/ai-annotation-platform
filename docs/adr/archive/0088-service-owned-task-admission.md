# 0088 — 任务可见性与写入准入由服务层统一持有

- **Status**: Accepted
- **Date**: 2026-10-16
- **Deciders**: 项目维护者（委托实施）
- **Supersedes**: None

## Context

后端需要逐步建立可独立演进、后续可更换实现语言的业务边界。任务可见性、编辑阶段、有效标注员和审核调整证据原先位于 API 的私有共享文件，却被 Mask 写入、切割、讨论、事件接收和异步最终写入服务反向导入。后台写入因此依赖路由装配图，HTTP 与非 HTTP 路径也容易出现第二套准入规则。

这些规则涉及项目能力上下文绑定、任务分派、成员撤销和冻结审核证据。结构迁移不能改变错误合同、锁顺序、事务范围、幂等重放准入或批量可见性语义，也不能直接把本地原子操作换成远程调用。

## Decision

以 `apps/api/app/services/task_access.py` 作为任务资源可见性与标注写入准入的唯一实现所有者，公开项目上下文绑定、任务编辑性、有效标注员解析/检查、项目访问解析、标注写能力、审核调整证据和单题/批量可见性操作。

API 的 `tasks/_shared.py` 保留 FastAPI 依赖适配和尚未迁移的 HTTP 装配辅助；依赖适配器调用服务层公开操作，不再实现或重新导出上述规则。API、WebSocket、领域写入服务和跨帧 worker 一次性迁移到服务所有者，删除旧私有函数和任务 package 中的对应导出，不增加兼容 facade。

`project_write_guard.py::assert_phase_write_allowed` 组合该服务的编辑/分派规则与既有项目授权、审核证据，不反向导入 API。账号、成员和资源锁的顺序、最终写入检查时点及提交仍由现有调用方持有；本片不增加 commit、网络调用或持久化副本。

现有 AST 架构门禁同时覆盖领域 package 与明确声明的单文件服务，并保护 `task_access`、`project_write_guard` 不反向导入 API/worker、不参与服务导入环。一个职责清晰的服务不为满足目录模板而拆成空层次。

本片保留既有 `ProjectAccess`、ORM 参数和 HTTPException 错误映射。它建立服务所有权与单向依赖，**不代表已完成语言无关 DTO、独立存储或远程服务提取**；这些变更必须在后续具体边界中单独设计和验证。

## Consequences

正向：

- 任务访问与写入规则可在不装配 API/worker 的情况下加载并验证；HTTP 与异步最终写入消费同一实现。
- 外部 HTTP/OpenAPI、WebSocket 和作业合同不变；内部私有导入不成为永久兼容负担。
- 权限撤销、管理者上下文错配、完成态锁定和审核证据继续失败关闭。

负向：

- 仓内调用方和测试中的私有 patch 路径必须在同片迁移。
- 任务访问仍依赖现有数据库模型、项目授权及调度规则；整个后端尚未解耦，不能仅更换 transport 就跨语言替换这一事务范围。

## Alternatives Considered

- **只在最终写入守卫复制几条规则**：形成多份权限真值，HTTP 与后台路径后续可能漂移。
- **只移动函数，保留 API 旧名重新导出**：保留反向依赖与装配耦合，不能证明调用方已迁移。
- **立即新增权限微服务**：现有锁和最终写入事务无法由远程权限快照等价替代，还会引入网络往返和新的故障语义。

## Notes

- 规则所有者：`apps/api/app/services/task_access.py`。
- 异步最终写入组合：`apps/api/app/services/project_write_guard.py::assert_phase_write_allowed`。
- HTTP 依赖适配：`apps/api/app/api/v1/tasks/_shared.py::require_task_annotation_write` 及相邻依赖。
- 验证：`apps/api/tests/test_task_access_binding.py`、`test_tracker_final_write_guard.py`、`test_review_evidence_guard.py`、`test_domain_package_architecture.py` 及相关路由/写入服务用例。
- 实施记录：[后端模块解耦计划](../../plans/1792160873_backend-module-decoupling.md)。
