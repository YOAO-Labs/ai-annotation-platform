# 0167 — Workspace 无锁权限读取与调用方快照

- **Status:** Proposed，无锁读取边界已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；补充 ADR-0166 的无锁读取边界

## Context

既有 `resolve_project_access_by_id` 先读取完整 Project，再检查账号状态，然后刷新
Project，非 manager 才读取完整成员行。Project 不存在时必须先返回 404；不能为了
构造包含 admitted actor role 的查询请求，提前读取 Identity 或虚构一个角色。

这些读取还会通过 `populate_existing` 刷新调用方的缓存，并遵循调用方原有 autoflush。
更换连接、增加锁、只返回权限布尔值，都会改变这个接口对现有消费者的行为。

## Decision

在既有 `aap_workspace_v1.project_access_step(uuid,uuid,text)` 增加唯一无锁阶段
`read_project_row`，复用既有 Project 行序列化器的新增 read 模式。初始读取不查账号、
不决定权限、不持有行锁或 advisory lock；回复仍严格绑定 actor、project、phase 和
完整行文本。冻结的旧 SQL／schema 不修改；新迁移可恢复两个函数的原有定义。

API 的 by-id 入口先执行该阶段；直接 resolve 入口继续先查当前 Identity 状态。
拿到真实 active actor 后，使用同一 Session 的 `WorkspaceProjectQueryData` 执行
`get_row`，manager 判断之后才执行 `access_member_rows`。两步共享一次 policy 检查。
请求使用实际平台角色，不调用可能切换连接或增加 Identity 读取的 provider selector。

完整 Project 43 列和成员 9 列沿用现有编解码与 `merge(load=False)`，刷新原对象身份
及字段历史。文本 SQL 前显式保留原 autoflush；外层 `no_autoflush` 仍生效，其他 pending
work 保留。绑定不创建连接，不开始、提交或回滚 SQL 事务。错误回复不回退私表读取。
缺失、停用、不可见、不兼容职责及历史时间字段失败保持原顺序。

应用账号仅补齐既有 Project Query schema、policy 和 step 的授权；原 access-step
签名及专用角色边界不变。没有新服务、DSN、配置项或永久角色。公开 GET access 的
第二轮完整权限读取继续保留；锁定入口仍使用 Identity SHARE、Project KEY SHARE 和
成员 SHARE 及原 NOWAIT 映射。

## Consequences

当前权限 helper 的 Project／成员读取不再要求私表 SELECT。另一个语言的调用方可以
直接消费这些 owner facts，调用方仍负责阶段顺序、事实解码、权限策略与外层事务。
这没有为多步权限检查引入跨事务授权令牌，也没有改变现有写入最终围栏。

这是当前权限读取的源码／Native 边界资格；完整模块状态只在
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)维护。

## Notes

修改前的独占测试库先验证默认 autoflush 与 `no_autoflush` 两种真实脏缓存场景，之后
在新实现重放同案。受限 API LOGIN 在 User／Project／成员私读被拒绝时完成无锁读取，
保持同一 backend／transaction，READ ONLY 事务可用，另一事务可获取三行的 UPDATE
锁。还验证阶段间已提交变化、错误优先级、异常回复导致调用方原写入回滚，以及独立
Node 直接调用。原锁定阶段作为回归保留。

本轮 180 项不同定向案例通过，无有效失败或跳过，包含首轮 24 项及消费者回归 156 项；
修改前两项缓存基线单独保留，不计入上述数量。公开 GET 的冻结时序、时间字段的
borrowed／DSN／独立源码 HTTP 对照，以及标注、帧持久化、pipeline、反馈和训练消费者
均通过。Native 冻结工件安装／回滚及 head 0289 往返通过，不视为完整迁移资格。
汇总及原始报告保留于 `.worktree/workspace-access-unlocked-qualification.json`。
本地定向结果不新增完整 Workspace 冷安装、镜像、备份恢复或生产容量资格，日常主环境
没有切换，Go 实验没有扩展。
