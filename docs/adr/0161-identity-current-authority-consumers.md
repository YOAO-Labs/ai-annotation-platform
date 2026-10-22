# 0161 — 同事务 Identity 账号读取与已接受 API-key 最终授权

- **Status:** Proposed，源码与限定本地定向验收通过；完整 Identity 继续收口
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0131](0131-identity-transactional-account-locks.md) 与 [ADR-0132](0132-identity-operational-provisioning-and-authority-reads.md)

## Context

Identity 已有独立运行工件，但执行期消费者仍通过 User ORM 读取当前账号；这些查询会装载密码及凭据代次等实际不需要的字段。训练数据最终授权还在调用方直接锁定 ApiKey，检查 owner、撤销、过期和 scope。已接受的持久操作只保存 key ID，没有原 secret/hash，不能用需要凭据持有证明的准入函数替代这道最终写入围栏。

这些检查处在既有调用方事务内。账号、项目、成员和 key 的锁序、NOWAIT 与等待差异、取消状态、错误和提交范围均须保持。普通 HTTP RPC 不能替代持续到调用方提交的行锁。

## Decision

复用 `IdentityAccountLockData.read_actor_status` 与 `lock_accounts`，从已有 Identity SQL 能力获取最小当前事实。`lock_actor_project_authority` 返回已经持有的账号事实，训练最终授权不再额外读取 User。原地替换预测／重试、调度、预标注、视频、Mask、导出、WebSocket、作业列表／取消、上传、批量清理、事件恢复以及讨论／评论／通知入口的单账号查询。查询顺序和后续 Workspace／Task 权限判断保持。

新增固定能力 `aap_identity_v1.check_accepted_api_key(uuid,uuid,text[]) RETURNS boolean`，只确认已接受操作的当前 key 资格。它等待并持有 key `FOR SHARE`，随后检查 owner、撤销、真实当前时间的过期点以及 wildcard／所需 scopes。它不重新认证明文，不检查或返回 key hash，不读取 User，不提交、回滚或创建回执；hash 轮换的既有接受语义保持。

调用方仍先取得账号 SHARE 与项目 KEY SHARE，再调用此 key 能力。过期检查使用取得锁后的 `clock_timestamp()`，不能使用等待前已固定的事务时间。参数缺失、数组维数错误及 null scope 成员以 SQLSTATE `22023` 拒绝；空 scope 集合法，不新增数量上限。数据库锁／事务错误原样传播，调用方决定何时回滚。

函数由 Identity owner 持有，普通 AUTH／key-management／建题 LOGIN 没有执行权；Learning owner 获得固定能力，过渡 API 通过原有运行权限清单调用。没有新增 DSN、LOGIN、HTTP endpoint 或部署模式。便携请求与 SQL 结果描述由 `account_lock_contract.accepted_api_key_schema()` 生成，包内与迁移的冻结 JSON 描述保持一致。

特殊语义逐条保留：失败预测的超级管理员分支仍二次读取；视频最终账号锁仍 SHARE NOWAIT，并保留原 busy 映射；Mask Repair、批量清理无项目时及通知列表只检查原有的存在条件，不增加 active 拒绝；导出状态检查仍在原范围锁之前；长连接不额外比较历史 admission role。取消的停用／不存在账号维持 401 与 Bearer 错误，不再从陈旧 User ORM 缓存误判为 403。

适配器沿用 Session 的 autoflush／no-autoflush 设置，事务和提交位置由原调用方持有。读取不会重新获得业务写入资格，也不会恢复失效执行证明。

## Verification

最新定向结果合并为 406 个不同的选定案例，无有效失败或跳过，包括 Native 权限／锁、训练与评估、预测、调度、视频、Mask、导出、WebSocket、作业列表、上传、批量清理、事件、讨论和通知。47 项适配器／描述检查与 18 项运行授权脚本检查另行保留，不与上述案例重复相加。并发 key 撤销、收紧 scopes、等待期间过期、原锁序、调用方 commit／rollback 及 SQLSTATE／事务失败状态均有真实数据库检查。

取消错误通过最小复现补验；导出测试的旧 fake Session 更新为明确的 Identity 读取端口。旧上传回滚夹具在原 User 读取实现下同样会丢失未提交的 Task；独立复现确认后，为操作前种子数据建立 Session 提交点，生产事务代码不变。旧失败报告继续保留，只用对应最新通过案例替换有效结果。

汇总为 `.worktree/identity-closure-current-authority-qualification.json`。受限 Learning owner 的额外检查在同一事务中实际调用账号 SHARE、当前状态及最终 key 确认，并验证原表读取仍被拒绝；补齐的只是 `read_actor_facts` 执行授权。其报告为 `identity-closure-learning-owner-role.*`。

原始证据包括 `identity-closure-key-native-second.*`、`identity-closure-learning-regression-first.*`、`identity-closure-authority-consumers-first.*`、`identity-closure-current-read-tail-first.*`、`identity-closure-discussion-current-first.*` 及定向 reproducer 报告。

独立冷安装、容器、完整迁移与备份恢复会在完整 Identity 的一致里程碑集中验证；当前结果不替代这些门槛。集合成员／通知、搜索／统计、作者展示与邀请审计范围仍存在原表消费者，完整 Identity 尚未完成。实际剩余项只维护在[模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)。未切日常主环境、推送或部署生产。

## Identity signer boundary

源码复核更正了“独立 Identity 签名 secret 必须等于 API SECRET_KEY”的旧文档说法：远程准入、actor-context 和命令通过 `IDENTITY_TOKEN` 鉴权，API 的远程响应解码器不使用自己的 secret 验签或重新派生 Identity 输出。独立 Identity 可以私有保管签发／派生密钥；使用不同密钥的完整真实消费者验收仍待补齐，不引入第二套命令签名协议。

已有部署的 Identity secret 仍须稳定。它同时参与 JWT、持久操作指纹、派生凭据及部分邮件密钥；轮换可能令旧 JWT 失效、旧回执冲突，不能因新验收采用不同密钥而改动已有环境。当前只在隔离新环境验证边界。
