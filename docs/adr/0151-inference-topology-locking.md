# 0151 — Inference 拓扑写入锁序与观察重验

- Status: Accepted
- Date: 2026-10-21
- Deciders: AAP maintainers
- Supersedes: None; follows ADR-0150

## Context

健康对账先锁成员再锁池，成员变更先锁池再锁成员，已在真实 PostgreSQL 中重现死锁。成员写入还可能通过外键隐式等待 Registry；能力漂移接受的联表锁会锁住同池其他 Registry。删除在 Redis 检查后重新读取成员，但没有验证检查时与删除时是否为同一拓扑。

Workspace 配置先锁 Project 再锁 Pool。删除持 Pool 后清理 Project 主绑定，形成跨 owner 锁环；配置在等待 Pool 前读取的成员归属也可能失效。这些问题必须在进一步提取独立控制写服务前消除。

## Decision

需要 Registry 锁的写入先取得该锁，再按 Pool → Member 顺序持锁；多个 Pool 按 UUID 排序。成员 upsert 与能力接受使用 Registry SHARE，删除使用 UPDATE，成员移除使用 KEY SHARE 稳定外键引用。drain/resume 不在持 Pool 后再等待 Registry。健康仍在外部 I/O 后锁 Registry，然后先读取成员池 ID，锁 Pool，再锁 Member 并重验池归属；锁定 ORM 读取刷新 identity map。能力接受只锁联表查询中的 Member，避免反向锁同池其他 Registry。

删除和成员移除接受一份早先数据库拓扑观察，包含 registry、member、pool、routing generation 与 traffic state，在写锁内与当前值比较。变化报 `ml_backend_topology_changed`。该值是进程内不可变观察，不能作为远程调用方提交的停流授权；没有新增 wire 或签发能力。删除使用最终锁定的池 ID 清理 Workspace 绑定。

API 的物理实例启用在 Registry 或 Pool 已消失时同样返回拓扑409；逻辑池入口保留池不存在时404的既有映射。项目后端关联写入复用 Workspace 既有 `lock_project_reference(uuid)`，在 Registry／Pool 前取得 Project KEY SHARE；Inference 仅增加该固定函数 EXECUTE，不获得 Project 表权限。按物理实例启用时锁后重读成员池归属，按逻辑池启用时锁后重新校验池仍启用且有 legacy 实例；池删除也在物理 FK 清理前使用 NOWAIT 主绑定清理，避免等待 Project 的反向边。

Native 0275 替换两个既有固定函数，函数签名和调用方事务不变：绑定清理按 UUID 对当前匹配 Project 行使用 NOWAIT，成功后只更新已锁 ID；55P03 映射为409 `project_busy` 并由调用方完整回滚。后端启用在 Pool 锁后重新读取同一 registry→pool 关系，成员失效或等待期间池已被删除，以40001及既有配置冲突 hint 拒绝，阻止等待期间删除或迁移成员后启用旧池。降级恢复两个原函数体，无新表、角色或部署 selector。

## Consequences

### 正向

健康与既有成员写入消除已重现的锁环；删除不会消费另一个成员身份或路由代次的旧观察。项目保存与删除争用有可重试的明确冲突，不会提交部分删除或按失效的物理实例选择绑定旧池。外部健康与 Redis I/O 不移入行锁区间。

### 负向

Registry SHARE 会让同一实例的配置、健康和成员变更串行等待；项目绑定争用要求调用者刷新后重试。该修复限定普通应用写路径与当前拓扑，不阻止绕过合同的高权限直接写表。

Redis PING 和一次 exact inflight=0 不是持久停流证明。路由候选暂停后恢复的代次围栏、账本丢失或重启的连续性、过期租约与长请求心跳仍未完成；卸载路径也尚未使用此拓扑重验。不能据此宣称完整控制面、独立健康服务或整个 Inference 模块完成。

## Notes

本地验收使用隔离 PostgreSQL，强制真实锁等待并通过 `pg_blocking_pids` 确认交叠；后端 I/O 为受控替身。旧健康、Registry 和 Pool 观测文件保持原字节，仅健康重放显式容许 member-read → pool-lock → member-lock 的锁序变化。此前单独声明的移除响应修正继续保留。完整迁移、恢复、工件和本地结果记录于 `.worktree/inference-topology-qualification.json`。未切换主开发或生产环境，Go 实验范围不变。

## Scoped local qualification

本轮选择151项去重通过检查，包含25项真实 PostgreSQL 并发与拓扑边界；受控后端 I/O 为替身，并非真实 GPU 或生产负载。原实现先重现三条死锁。24组健康、58组 Registry 和38组 Pool 旧观测回放匹配既定差异规则；六个冻结文件与修复前提交逐字节相同。五阶段完整迁移和非空 Workspace 配置／绑定恢复通过。

独立非 editable 安装的 Inference／Workspace 分别通过132／449项，50／272个包文件与源码一致；源码 Inference／共享合同另通过132项。源提交9ca449f09的三项镜像在无网络、只读、降权容器中完成冷检查，API 的1,246项输入、OpenAPI 和 Native head275相符。镜像冷检查不代表独立健康服务执行资格。

最终34项组合批次的测试退出码为0；外层脚本首次资源销毁报 WorktreeError，因此原回执保留failed。单独重新核验容器标签、数据库 marker、完整资源拓扑及精确确认值后，清理重试成功并记录恢复回执，没有强制断开未知连接。另一次旧行为回放因未传基线摘要参数而拒绝执行，补齐固定摘要后通过；失败尝试不计入通过总数。日常开发与生产未迁移或刷新。
