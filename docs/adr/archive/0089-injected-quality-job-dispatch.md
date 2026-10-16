# 0089 — 质量执行通过领域端口注入 Celery 派发适配器

- **Status**: Accepted
- **Date**: 2026-10-16
- **Deciders**: 项目维护者（委托实施）
- **Supersedes**: None

## Context

Mask QC、点云质检、视频轨迹质检、Mask 修复和格式导入的业务服务直接导入 worker task。即使使用延迟导入，业务模块仍依赖 Celery 的任务对象和调用方式，不利于独立测试或按模块替换执行实现。

三类质检在提交领域记录前预分配执行 ID；修复及回滚指定 `media` 队列；格式导入沿用 task 的默认派发设置。各领域对派发失败的状态、通知和 HTTP 错误映射不同，不能在抽取传输边界时统一成一个异常处理策略。

## Decision

1. 由 `app/services/quality_job_dispatch.py::QualityJobDispatcher` 定义领域侧执行端口。输入仅包含受限任务种类、资源 UUID、执行 ID 与回滚标志，不依赖 ORM、HTTP 或 Celery。
2. 由 `app/workers/quality_dispatch.py::CeleryQualityJobDispatcher` 实现具体传输；保留现有 task 对象的 `apply_async` / `delay`、指定执行 ID、队列及默认选项。不替换为通用 `send_task`，避免丢失 task 自身配置。
3. `app/deps.py::get_quality_job_dispatcher` 是 HTTP 装配入口。五个领域派发函数要求显式传入执行端口；手动创建、任务提交、视频片段提交和批量提交等调用方全部迁移，不保留默认适配器或兼容路径。
4. 路由继续先提交再派发；领域服务继续负责准入、作业身份及派发失败语义。适配器不拥有数据库事务、领域终态或通知。
5. 复用现有 AST 架构守卫，覆盖端口及五个领域服务文件，禁止 API/worker 反向依赖和服务环；守卫支持领域 package 内的单文件所有者。

## Consequences

- 业务服务不再选择 Celery task 对象、注册名或队列。未来替换传输或执行语言时，可以在装配入口提供另一实现，而不把 broker 依赖重新引入领域服务。
- 当前仍是进程内接口，没有新增 HTTP、JSON 编解码或网络往返；既有 worker、数据库和对外 API 保持原状。
- 数据库提交与 broker 派发之间的失败窗口不变。本决策没有引入 outbox，不承诺 exactly-once，也没有把数据库中的现有执行 ID 字段迁移为新的存储协议。
- 其他业务域的派发和通用作业终态装配不在本切片范围，仍需逐域迁移；本端口不是全后端的通用任务总线。

## Alternatives Considered

- **只改为稳定任务注册名**：避免导入 worker，但业务服务仍依赖 Celery 和任务路由细节，不能形成传输无关边界。
- **服务内默认构造适配器**：调用方便，但隐式依赖继续存在，遗漏调用方迁移也不能及时暴露。
- **通用字符串任务总线**：把领域操作及参数错误推迟到运行时，并混淆不同任务的身份和队列合同。
- **立即增加远程执行服务或 outbox**：会改变部署、事务和恢复合同；不作为本次依赖方向迁移的隐含范围。

## Notes

- [后端分层](../../../docs-site/dev/concepts/backend-layers.md)
- [仓库地图](../../../docs-site/dev/concepts/repository-map.md)
- [实施计划与验收证据](../../plans/1792160873_backend-module-decoupling.md)
