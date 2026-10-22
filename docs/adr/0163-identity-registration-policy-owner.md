# 0163 — Identity 注册状态与部署默认值的统一读取

- **Status:** Proposed，源码集成与限定本地定向验收通过；完整 Identity 继续收口
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0119](0119-independent-identity-recovery-and-mail.md) 与 [ADR-0162](0162-identity-read-relations-and-consumer-grants.md)

## Context

注册执行已经通过 Settings owner 的 `identity_recovery_policy(jsonb)` 读取数据库
覆盖值，并在原事务中用 Settings SHARE 锁围栏注册；并非忽略数据库配置。剩余问题
在 API 状态读取：它使用自己的部署默认值和进程缓存，与独立 Identity 的 Recovery
默认值不同或缓存过期前，可能显示与实际执行相反的注册开关。

管理端的读取、元数据、CAS 版本和 reset 也使用 API 默认值。仅改响应中的布尔值会
造成版本与实际回退值不一致；导入预算还必须保留与管理快照同一设置版本的既有合同。
管理端刻意允许损坏历史覆盖值回退和修复，不能为了读取默认值而依赖严格验证当前行
的注册执行策略。邮箱验证开关本来由 Identity owner 的会话／恢复配置共同持有，本次
不把它迁成动态设置。

## Decision

复用现有 Recovery binding、控制 Bearer、超时与并发槽，新增私有只读合同
`aap-identity-registration-status/v1`。固定请求只有 `schema/operation`，操作为
`read_status` 或 `read_deployment_default`；成功回复精确为
`schema/operation/ok/data`，`ok` 严格为 true，data 只有 boolean
`allow_open_registration`。解码器拒绝多余字段、重复 JSON key、布尔强制转换和操作
错绑定；调用方不能提供策略或账号。包内与迁移冻结描述由同一个生成器维护。

`read_status` 经新 Recovery facade
`aap_identity_recovery_v1.read_registration_status(jsonb)` 复用现有 Settings helper。
没有复制策略、改原注册事务、写入默认值、回执或邮件。新函数由 Identity owner 持有，
撤销 PUBLIC，只加入既有 Recovery LOGIN 的第四项精确能力；其他客户端不扩权。
内嵌 API 沿用自己的 Session／autoflush 和提交边界；专用 DSN 使用短事务和有界连接，
验证回复后退出。Settings SHARE 锁随所属事务结束释放。

`read_deployment_default` 从 owner 已加载的配置返回默认值，不查询覆盖行或调用
Native policy。因此类型损坏的历史行仍能由管理员查看回退并修复。只启用认证的
Identity 也能返回默认值；缺少 Recovery 数据能力时，有效状态读取仍返回 unavailable。

API 的现有公开状态接口读取当前 owner 结果，保留原 JSON 形状，绕过 API 缓存。
管理端每请求只解析一次 owner 默认值，将这个单键值传入现有 Settings 快照、
元数据、版本、PATCH／reset 和冲突回读。数据库覆盖继续优先，false 不被当作缺失。
owner 默认值变化进入原 HMAC 版本，旧版本按现有 409 规则拒绝；没有新增 CAS 格式。
导入预算快照沿用同一默认值，保留预算与管理快照版本一致的既有约束。

读取 owner 失败时返回 503，不伪造 API 回退值；管理写入在默认值依赖失败前不执行。
损坏历史值的同值保存也会修复原行，避免新回退值与目标值相等时跳过修复。其他设置
字段和范围、原事务、授权、审计、注册回执及邮箱验证／CAPTCHA 语义保留。
没有新增 DSN、LOGIN、环境变量、部署模式、transport MAC 或 signer 抽象。

## Consequences

有效注册状态、管理默认值和设置并发版本由实际 owner 一致解释，同时保留 Settings
对动态覆盖值的所有权。匿名读取不具有注册执行权，也不会产生 User、Audit、receipt
或 outbox。管理端的修复路径不依赖损坏行的业务可执行性。

管理端和导入接受流程需要读取 owner 的部署默认值；无法获得该值时失败关闭，避免
记录错误版本。既有内嵌、专用数据库和远程选择路径保留。完整 Identity 的资格与
剩余阻塞只维护在[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

## Notes

定向检查覆盖严格合同、操作绑定、private Bearer／JSON／请求体边界、默认值查询不
访问损坏行、仅认证配置、无本地回退、API 旧缓存冲突、真实受限 Recovery LOGIN 的
四函数权限、私表拒绝、状态无持久副作用、Settings SHARE 围栏，以及 absent／NULL／
true／false 覆盖值与实际注册结果一致。

实际两个源码进程使用相反 API／Identity 注册默认值、互不相同的 Identity signer／
API edge secret／control token。API 角色经最终 consumer 权限清单撤销 Identity
私表权限，仍通过远程 owner 完成状态、管理 GET/PATCH/reset、损坏行读取／同值修复
与实际注册；owner 签发 token 不能用 API secret 验签。Node.js peer 实际读取两种
只读操作，校验闭合回复。owner 丢失后返回 503，数据库开关不被错误改写。

首次 CAS 连续冲突测试在预期回滚后丢失未提交的管理员夹具；种子数据建立提交点后
最小复验通过，生产回滚不变。签名拒绝测试改为捕获项目声明的 python-jose 错误类型，
先用最小复验再继续进程场景。新增第四项能力后的旧三函数断言已更新；原始失败报告
保留，仅由同一案例的最新有效结果替代。冻结的旧迁移／协议描述未被重写。

证据包括 `.worktree/identity-registration-read-port-integrated.*`、
`identity-registration-native-first.*`、`identity-registration-cas-reproducer.*`、
`identity-registration-live-second.*`、`identity-registration-import-and-roles-first.*`
与 `identity-registration-role-and-fault-reproducers.*`。完整 Identity 冷安装、容器和
备份恢复仍在一致模块里程碑集中验收；本次没有日常主环境切换、推送、发布或 Go 扩展。

当前汇总为 `.worktree/identity-registration-policy-qualification.json`：113 个不同的
选定 API／数据库／进程案例全部通过，无有效失败或跳过；658 项 Identity 包源码检查
全部通过，二者按案例去重分开记录。20 项运行授权／测试选择检查通过；架构通过
355 项，149 条规则不适用。重命名的旧三函数案例由四函数精确权限案例替代，原预算
单一上下文案例由 None／false／true 三个上下文变体替代，不重复计数。
