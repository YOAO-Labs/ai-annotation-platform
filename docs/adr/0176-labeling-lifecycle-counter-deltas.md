# 0176 — Labeling 生命周期与审核原子计数

- Status: Proposed（限定本地调用边界验收通过）
- Date: 2026-10-22
- Deciders: AAP maintainers
- Supersedes: None

## Context

Project 的只读查询与两处绝对计数重算已有 owner 边界，但撤回、重开和审核仍通过
`LabelingTaskService` 直接 UPDATE Workspace 私表中的 Labeling 计数字段。用读取后
计算的绝对值替换这些增减会丢失并发更新；分离提交也会破坏 Task、计数和 Audit 的
共同回滚。因此这组写入需要独立的固定数据能力，继续借用原调用方事务。

## Decision

迁移 0299 新增 Labeling 持有的三项 SQL 函数：

| 函数                                      | 单次原子写入                                | 返回                                  |
| ----------------------------------------- | ------------------------------------------- | ------------------------------------- |
| `decrement_project_review_count(uuid)`    | review 饱和减一、事务时间戳                 | review 整数或 null                    |
| `decrement_project_completed_count(uuid)` | completed 饱和减一、事务时间戳              | completed 整数或 null                 |
| `record_project_review_approval(uuid)`    | completed 加一、review 饱和减一、事务时间戳 | 按 completed、review 顺序的零行或一行 |

函数位于 `aap_labeling_v1`，每项只有一条 UPDATE。原 COALESCE、GREATEST、int32
溢出和正常行锁等待保持；不增加账号检查、额外锁、连接或提交点。权限、Task／Batch
变更、证据、Audit、通知与外层提交仍由原工作流组合。函数固定 search_path、撤销
PUBLIC 执行权；沿用 Labeling owner 的窄列权限，三个运行授权清单同步登记执行权。

调用方以 ORM `select(...).from_statement(...)` 执行原生函数，保留旧 UPDATE 的
autoflush，不添加手动 flush。只将原来返回的计数同步为 committed cache；不刷新
其他字段或时间戳。数据库时间戳显式使用 now()，等价于旧 ORM 的 onupdate。

函数使用 CALLED ON NULL INPUT。初版 STRICT 在空 ID 时跳过 UPDATE，使 READ ONLY
不再报旧实现的 25006；不可变基线提交 af9c604f 中的原方法 AST 与候选直接对照复现了
差异。取消短路后，空 ID 仍尝试匹配零行的写入：可写事务的标量返回 null，审批返回
零行并由原 `.one()` 抛 NoResultFound；只读事务仍拒绝。缺失 UUID 同样保留原行为。

## Consequences

正向：三处跨 owner 私表写入已迁移，受限 API LOGIN 撤掉 Project SELECT／UPDATE
后，撤回、重开、批准和驳回仍能提交 Task、计数及 Audit。两个持有旧快照的连接通过
正常行锁等待累计增减，不丢失更新。Node 直接通过 PostgreSQL 协议调用三项函数，
证明数据合同不依赖 Python 提供方代理。

负向：仍依赖共享 PostgreSQL 的调用方事务和已登记能力。完整 Labeling／Workspace
工件、最小凭据及语言提供方替换不由本操作资格代替；默认宽应用角色继续兼容装配。

## Notes

139 项不同适用案例通过，无有效失败、错误或跳过：69 项旧缓存／autoflush／数字
边界复验、30 项 Native／受限连接／Node、4 项真实 HTTP、36 项现有消费者回归。
覆盖缺失／空 ID、READ ONLY、时间戳、并发等待、溢出与 pending Task 共同回滚，
以及审核证据、当前权限撤销、QC 和真实并发批准。旧 golden 只重放，不覆盖或重复计数。

失败日志、JUnit 和真实退出保留。HTTP 夹具修正了送审后审核员分配、实际审核路由与
中间件审计连接；旧认证计数包装器补传真实 Identity gateway 依赖，未改业务认证。
最小失败复验先通过，再执行剩余案例。完整分类见
`.worktree/labeling-counter-deltas-qualification.json`。

完整迁移升降级在独占测试资源内通过。冷工件、备份恢复与容量留到完整模块里程碑；
本轮不切换日常运行环境、不推送、发布、部署生产或扩展 Go。其余工作仅在
[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)继续跟踪。
