# 0168 — Workspace 独占 Project 快照

- **Status:** Proposed，两项消费者已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；补充 ADR-0166／0167 的独占读取边界

## Context

孤儿标注清理在当前管理权围栏之后，使用 `FOR UPDATE NOWAIT`、`no_autoflush` 和
`populate_existing` 取得 Project 配置。点云评估晋升则等待 `FOR UPDATE`，保留原
autoflush，且不强制覆盖缓存中的已加载字段；它还须先读取 evaluation，再判断项目
或评估是否缺失。两条路径都依赖调用方持有整个业务事务。

现有 class-rename 私有助手可以序列化完整行并获取 NOWAIT 锁，但公开配置动作要求
实际 rename command、actor 和操作状态。用虚构的 rename／actor 请求代替一般行锁，
或公开私有助手的全部模式，都不能形成正确边界。

## Decision

在既有 Workspace schema 增加 `lock_project_row(uuid,boolean)`，使用
`aap-workspace-project-lock/v1` 的严格回复：schema、project_id、nowait、row_json。
它只返回完整 Project 行文本或 null，不检查 actor、不决定权限、不写业务状态，也
不创建连接或结束事务。两种等待行为都有本次实际消费者，不增加任意 lock mode。

NOWAIT 模式复用既有私有序列化器的 update 分支。等待模式先取得 Project UPDATE
锁；缺行直接返回 null，成功后用既有 read 模式生成完整快照。取得锁后的新语句快照
避免等待前的 STABLE Labeling 计数与等待后的 Project 字段混合。锁持续到调用方结束。
旧 key_share、update、read 分支和冻结 SQL／schema 保留。降级删除新函数，并恢复
冻结 0289 的私有助手；原 SQLSTATE 继续由各调用方处理。

API 绑定使用原 Session，按原 `db.autoflush` 显式 flush 文本 SQL 之前的 pending work。
完整行编解码在缓存命中时也执行，保持历史时间字段失败。需要 populate_existing 时
复用完整 `merge(load=False)`；普通查询只恢复 expired／unloaded 字段，保留已加载
值和未 flush 的字段历史。合并助手集中到已有 Workspace API 绑定，旧权限入口保留
原别名，不复制实现。

原 `lock_workspace_project` 签名、无 autoflush、缺行 404 和竞争 409 project_busy
保持。晋升继续使用阻塞锁、不强制刷新、先读 evaluation，以及原配置写入、评估、
Audit 和提交顺序。Shared contract 严格绑定 UUID、boolean 和行文本；错误回复返回
503，并由调用方回滚原业务。SQL 锁或事务错误不被合同错误替换。

新函数由现有 Workspace owner 持有，只加入既有 application role 的生产／本地授权。
PUBLIC、append、删除、maintenance、configuration 专用账号均不因本项扩权。
不新增角色、DSN、服务或独立提交的 HTTP 锁请求。

## Consequences

这两项消费者不再直接 SELECT Workspace 私有 Project 行。真实 API 使用独立 AUTH
凭据完成身份准入，业务会话使用受限 API LOGIN；原管理权锁仍由既有 owner 能力持有。
另一个语言的调用方可以直接使用同一 Native 合同及原事务。

完整模块状态仍只在
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)维护。
本次没有新增完整 Workspace 冷安装、镜像或备份恢复资格，也未切换日常主环境。

## Notes

修改前九项真实基线分别覆盖清理的脏缓存刷新和晋升的 clean／dirty／expired／partial
cache × autoflush。它们在新实现重放通过，旧源执行不计入本轮资格数量。

本轮 245 项不同定向案例通过，无有效失败或跳过：24 项新 Native／实际 API／Node
案例、7 项 API 绑定单元、12 项合同检查、20 项原锁定／无锁数据库回归及 182 项业务
消费者回归。受限 LOGIN 的私读拒绝、调用方事务、两种竞争结果、缺行、RR／Serializable
的 40001、错误回复回滚、实际清理／晋升与 Audit 提交均有直接证据。

等待计数的首轮探针揭示了真实混合快照问题；修复后同案通过。首轮还保留了单元测试
漏字段、AUTH 凭据和 immutable Audit 夹具退场的失败。夹具最终保留审计及其 actor
到自有数据库销毁，不关闭审计保护；其余有效通过结果沿用，未为恢复数量重跑。
汇总位于 `.worktree/workspace-exclusive-qualification.json`。

Native 工件安装／回滚和 0189 到 head 0290 的往返属于该独占验收器的限定资格，不
代表完整 fresh migration。时间字段 HTTP 对照使用独立当前源码进程，未声称新安装
工件、另一语言完整 provider、生产容量、远程 CI 或 Go 上线。
