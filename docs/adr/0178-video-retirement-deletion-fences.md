# 0178 — 视频执行退役后的项目删除与解绑围栏

- Status: Proposed（限定本地删除门禁验收通过）
- Date: 2026-10-22
- Deciders: AAP maintainers
- Supersedes: None

## Context

项目删除与数据集解绑会通过 Task／Annotation 外键级联删除 canonical Video Job。
旧协调器没有检查执行退役：真实 PostgreSQL 复现了 cancelled Job 仍绑定范围、journal
未退役时，两条命令均返回成功，Task／Job 消失而运行态日志与输入 pin 继续存在。
用户确认两条路径应返回 409，先取消并完成退役后再重试，不新增后台删除服务。

源码审查还确认，旧版和当前 runner 都可能先提交业务终态，再在 finally 中清理
路由，随后取得物理退役回执。completed、failed 或 cancelled 均不能替代退役证明。

## Decision

迁移 0300 新增三项固定 Native 能力，并只向原协调器插入视频门禁：

- Labeling `guard_project_video_deletion(uuid)` 在原 Project 排他锁下，按 UUID
  锁批次、Task 和将删除的 Annotation。
- Labeling `guard_dataset_video_deletion(uuid,uuid)` 复用原解绑 Task 快照和已有
  批次／Task 锁，再锁将删除的 Annotation。
- Jobs `guard_video_execution_deletion(uuid[],uuid[],uuid)` 对完整 Job 集，按 UUID
  批量锁 canonical Job、execution、journal。锁均为 NOWAIT，竞争沿用 project_busy。

Job 集包含目标 Task 的 Job、引用被删 Annotation 的 Job，以及输入来源 sidecar
对应的 Job；不能只看 Job.task_id。Annotation 锁同时阻止新外键／sidecar 引用插入。
保留的输入 scope 和 journal 也用于找回此前级联留下的孤儿执行，解绑保留原主来源
选择规则，不纳入无关当前 Job。两条协调器在旧保留检查之后、任何 owner 清理之前
调用门禁，保持原权限、错误优先级、回执重放、外层事务和提交点。冻结 SQL 不修改。

有 execution 时，只有当前 run 匹配且 execution_retired=true 的 journal，配合终态
或已不存在的 canonical Job，才能放行；缺 journal、错 run、未退役或活跃 Job 都拒绝。
证明成立后无需再等待 lease 到期。没有执行账本时，只有 canonical Job 尚在、
started_at 为空且状态明确未运行才可证明未启动；已启动的旧 Job 无论终态为何均拒绝。
缺 Job 且没有任何证明的 scope-only 孤儿同样拒绝。

为已证明从未启动、即将删除的 Job，Jobs 保存私有
`video_deletion_clearances(job_id,never_started_at)`，无 canonical Job 外键。整组检查
全部通过后才写入，随原删除提交或回滚，运行路径不更新／删除该证明。它记录未启动
事实，不伪造 execution 或物理退役。否则第一次合法解绑留下的输入 scope 会使下一次
解绑或项目删除永久误报 409；该问题已用两个失败场景复现，再以本表修复。当前存在
execution 时仍只看当前 run 的退役证明，旧 clearance 不得绕过它。

Jobs 仅增加 started_at 的窄列读取；新表由 Jobs 持有，内部执行权为 Workspace →
Labeling → Jobs，不向 API 或独立命令账号开放 helper／私表。公开两条路径返回
409 detail.code=video_execution_not_retired，提示取消并完成退役后重试。旧命令、响应
及幂等回执保持。降级在 clearance 非空时先以 55000 拒绝，避免销毁已提交证明；空表
降级恢复原协调器，再移除新增对象。

## Consequences

正向：删除前保留 canonical Job、Task、输入／Mask 运行根和检查点，当前 worker
可以完成既有退役；正常锁竞争不留下部分清理。Task／Annotation 外键和 Job 行锁
阻止新准入越过检查。真实取消请求返回后仍须等待 Native 退役，证明完成且 lease
尚未到期时可立即重试成功。独立 Python／Node 账号得到相同失败，并可提交失败
回执所在事务而不留下删除、审计或 outbox 的部分变化。

负向：已经启动但没有账本的旧 Job，以及缺证明的旧孤儿，会继续被 409 保护。
当前没有可靠的自动排空／修复入口；不能仅改业务状态来解除保护。这项恢复能力
仍在唯一台账中，完整 Workspace／Jobs 与历史数据恢复资格不因本门禁通过而闭合。
已使用 clearance 的数据库不能无损直接降级移除本表。

## Notes

226 个不同适用案例通过，无有效失败、错误或跳过：98 项 Native／并发／独立客户端、
8 项真实 HTTP、2 项错误映射、118 项既有删除／视频执行／流水线回归。原始两个
级联失败、两个未启动孤儿误阻断失败、最小修复复验及真实退出／JUnit 均保留；
捕获和重复运行不累计。公开取消 → 删除仍拒绝 → Native 退役 → 同请求重试成功，
API 私表 Project SELECT／UPDATE 被撤销，独立函数账号和 Node 无私表权限。
完整分类见 `.worktree/video-deletion-retirement-qualification.json`。

本轮仅本地整合与限定验收，未切换日常环境、推送、发布、部署生产或扩展 Go。
完整凭据、冷工件、备份恢复和容量继续按
[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。
