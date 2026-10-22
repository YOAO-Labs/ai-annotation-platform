# 0169 — Task／Training 的 Workspace 项目快照

- **Status:** Proposed，选定消费者已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；复用既有无锁项目事实和引用锁

## Context

Task 访问、最终写入检查和 Training 权限／调度需要完整 Project 配置，但原先直接
`Session.get(Project, ...)`。普通 get 在非完全过期的 identity-map 命中时不会查询或
autoflush，部分字段过期也仍返回缓存；populate_existing 则强制读取和刷新。这些差异
影响错误顺序、待提交数据和配置快照，不能直接替换成每次都读取 owner 的调用。

训练权限围栏还有前置 Project.id KEY SHARE 查询。完整行接口会引入原 ID 查询没有
的时间字段解码，也不能用虚构 actor 替代可为空的账号输入。

## Decision

API 的 `get_workspace_project` 复用既有 `read_project_row` 事实阶段，始终使用调用方
实际 actor UUID，不引入 admitted role、提供方选择或新连接。普通缓存命中直接返回；
需要查询时才沿用完整行编解码与现有合并助手。部分过期缓存保持原样，显式刷新保持
原字段历史清除。完全过期且脏的数据在 autoflush 前恢复未修改的过期列，再执行原
flush 和后续读取，避免 flush 为获取 PK 再 SELECT 私表。

完全过期记录消失时，保持当前 SQLAlchemy 的删除状态转换和第二次 get 查询；调用
同一个 `_remove_newly_deleted` 维护事务快照，后续回滚仍由调用方持有。这是 API ORM
兼容绑定，Native owner 不依赖 ORM。旧权限 by-id 入口继续使用相同原有时序。

迁移八处真实调用：Task access 两处、最终阶段写检查一处、Training authority 一处、
Study coordinator 一处和 Training claim predicates 三处。其实际 actor 来自请求参数
或已保存的 creator_id；原权限检查、撤销、错误映射、执行锁与提交顺序保持。

新增现有引用锁的固定 NOWAIT companion `lock_project_reference_nowait(uuid)`，与
原 blocking `lock_project_reference(uuid)` 都只返回 UUID 或 null。账号 SHARE 仍先
获取；Project KEY SHARE 不读取其他配置字段，不把未知／空账号变成一次授权，也不
因异常日期而失败。保留原 autoflush、project_busy 映射和非竞争 SQL 错误。返回值非
null 时校验 UUID 与目标身份，允许驱动的 UUID 子类；错配失败由调用方回滚。

新函数只加入既有应用授权并由 Workspace owner 持有，PUBLIC、Assets／Inference
owner、append 和专用删除账号不因此扩权。没有新 JSON 协议、角色、服务、DSN、
后台写入或自主提交点。

## Consequences

选定 Task／Training 消费者不再直接 get Project，训练的前置引用锁也不 SELECT 私表。
实际受限 API LOGIN 可在 READ ONLY 中解析 Task 访问，并在训练权限检查中保持账号／
项目锁。Node 直接使用两种引用锁和现有快照阶段，不经 Python 代理。

其他业务快照和成员集合读取继续按
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。
本项不代表全部 Project 消费者或完整 Workspace 工件／凭据／恢复验收完成。

## Notes

旧源 a6ebe369b 的 40 项 getter 和 16 项引用锁真实基线先通过。冻结 getter 样本记录
查询／flush 顺序、缓存字段、删除／过期状态和待提交 Task；捕获拒绝覆盖已有案例。
新实现逐项重放通过，旧源执行不计入本轮数量。

本轮 329 项不同定向案例通过，无有效失败或跳过，包含 40 项冻结 getter、16 项引用
语义、受限实际消费者、Node、精确 ACL、错误范围回滚，以及完整选定训练／Task／
标注／预测消费者回归。首轮三个适配检查失败来自两个 mock 漏记 actor 事件和夹具
Task 清理顺序；修正后只复验这些失败并补齐未执行案例，原始报告保留。

汇总位于 `.worktree/workspace-project-get-qualification.json`。Native 工件往返及 head
0291 的限定迁移通过；没有新增完整 cold install、镜像、fresh migration 或备份恢复
资格。日常主环境、生产、远程 CI 和 Go 实验均未切换或扩展。
