# 0165 — Workspace 项目删除的调用方事务边界

- **Status:** Proposed，可移植调用与定向验收通过；视频删除前置仍未闭合
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；复用既有 `aap-project-delete/v1` 与 Workspace 原子删除协调器

## Context

项目删除已由 Workspace Native 协调器在同一 PostgreSQL 事务中执行当前权威检查、
保留检查、各 owner 清理、Audit 和持久回执。API 先 flush 调用方全部 pending work，
执行协调器，再由原路由提交。现有测试明确要求外层回滚或最终提交失败恢复整个图、
Audit 和回执。

因此，这项操作不能直接套用自有事务的 HTTP／专用 DSN 命令模式：提供方先提交后，
无法再随 API 的最终提交失败回滚。完整模块的独立交付需要包含其 Native 能力和
语言无关调用契约，而不是为每项事务操作增加独立提交点。

## Decision

将原 API 内的值契约、回执绑定和命令校验移入 `aap-workspace`。命令沿用既有四字段
`operation_id/actor_id/project_id/audit_context`，不新增请求版本字段；回复继续使用
`schema: aap-project-delete/v1`、严格 boolean `ok` 及单一成功／失败分支。回执字段、
严格非负计数、正 Audit ID 和 UUID 行为保留；原 API 模块保留兼容重导出。

Portable manager 调用一个数据端口，校验三个 UUID 绑定。API 适配器使用原请求 Session
无条件 flush，再调用同一四参数 Native 函数。它不提交、回滚、创建 savepoint、切换
连接或重试。成功后 API 继续移出缓存 Project，原权限入口、Audit 来源整理、错误映射、
新操作 UUID 生成及最终 HTTP 204 提交点保留。

连接式 PostgreSQL 适配器只借用调用方已开启的 asyncpg 事务，拒绝自动提交模式。
在同一事务内先用既有 owner-data helper 验证精确账号与函数能力，再执行协调器。
因为本能力复用 `aap_workspace_v1`，账号名显式指定为
`workspace_project_deletion_client`，不能按 schema 推导成通用 Workspace 账号。
空 Audit context 绑定 SQL NULL，不转换为 JSON null。

独立 LOGIN 只有 `aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)` 这一项业务
owner 能力，无私表／列、序列、DDL、TEMP 或子清理能力。标准 PostgreSQL/public invoker
helper 不计入业务能力数。原 Native SQL 和冻结迁移契约不改写，没有新服务、DSN
selector、HTTP endpoint、策略 RPC、事务协调框架或依赖库。

Native 保留 active-actor、操作锁、Learning advisory lock、当前项目管理权和项目／
成员锁序。匹配回执在再次检查项目管理权之前重放，但仍要求 actor 当前启用；公开 API
仍先按原入口检查项目，删除后再次请求返回 404，不增加公开幂等键。原始 SQLSTATE、
保留失败与业务错误映射保持。Shared Audit contract 成为包的显式内部依赖。

## Consequences

其他语言可以通过标准 PostgreSQL 调用 Workspace owner，保留调用方的完整事务；
可移植包和 API 不再分别持有回执语义。Node 验收直接执行同一协调器，并保持原始
JSON 文本中的整数精度。返回事务内回执不等于最终提交成功，调用方仍需提交自己的事务。

当前发现的视频执行清理缺口不会因调用包迁移而消失。删除会移除 canonical Job，
写围栏随后返回 `job_missing`，但运行态摘要和未退役输入 pin 可能继续存在。删除规则
仍待决定，整项项目删除安全验收尚未完成；剩余状态只维护在
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

## Notes

定向结果为 76 个不同案例，无有效失败或跳过，覆盖合同／适配器、实际精确 LOGIN、
自动提交拒绝、原始协调器回归、外层回滚／延迟失败提交、错绑定回滚、断线重连后的
冻结回执重放、Node 三种事务结果及大于 JavaScript 安全整数范围的 Audit ID。实际公开
API 验证成员拒绝、负责人空体 204、Audit 来源与后续 404。

修改前的 30 项检查有一项旧夹具失败：它直接插入跨项目标注父子关系，现有图片及视频
输入约束均拒绝该状态。夹具现在仅在已验证独占测试库中以 savepoint 临时关闭两项
指定 guard 来建立历史数据，再恢复并验证新写入仍以 23514 拒绝；生产 guard 不变。
第一次最小复验发现第二项视频 guard，补齐后单案通过。权限调用的首次失败来自按
schema 推导账号名；显式账号后先通过单案，再通过其余事务检查。原始报告保留。

另一个独立诊断案例确认视频执行残留，不能计入 76 项成功资格，也不能记作已修复。
证据汇总为 `.worktree/workspace-project-deletion-qualification.json`。完整 Workspace
冷安装、容器、完整迁移与恢复资格集中在一致模块里程碑；本次没有主环境切换、推送、
发布或 Go 扩展。
