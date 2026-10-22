# 0179 — Workspace 普通消费者的凭据边界

- Status: Proposed（限定本地权限边界验收通过）
- Date: 2026-10-22
- Deciders: AAP maintainers
- Supersedes: None

## Context

Workspace 的项目、成员、模板与相邻写入消费者已经使用 owner 函数或固定事实关系，
普通应用的兼容授权仍提供 public 表的广泛 CRUD。只有在真实调用账号失去私表权限后，
才能验证这些调用不依赖旧绑定。种子与截图准备仍需要直接写入，不能与普通账号混用。

源码审查发现旧部署脚本还漏授配置、模板及目录的默认同事务能力，以及成员搜索
TVF。临时数据库已复现普通账号访问配置 schema 得到42501；这些路径不应因为
没有配置可选独立 URL／DSN 而失败。

## Decision

沿用既有迁移者、NOLOGIN Workspace owner、普通 API／worker LOGIN 和借用事务：

- `workspace-consumer-grants.sql` 在 owner 与旧应用授权之后显式执行，只保留
  32个当前消费者使用的 Workspace 固定函数、38列 project_facts 和9列
  project_member_facts 两张安全屏障视图。函数 owner／SECURITY DEFINER 和视图
  布局必须与固定合同匹配；内部 helper、旧 catalog view 不开放。
- 撤销 projects、project*members、project_templates、删除／解绑回执及 workspace*
  前缀持久表的关系和列权限，同时关闭项目序列、私有 schema 创建及其他函数权限。
  撤销迁移者对该账号未来 public 表／序列的宽默认授权。最终检查有效权限，包括
  PUBLIC 与对象所有权；失败将整份配置原子回滚。
- 保留其他领域权限及 Labeling 的 project_catalog_counters SELECT。普通账号不能
  继承迁移者或其他角色；迁移者继承普通运行账号的既有维护方向仍允许。该配置不把
  普通账号等同于单一切片的函数专用 LOGIN。
- 旧部署授权同时补齐三个 schema、六个 policy／step 函数及成员搜索函数，保证
  默认同事务绑定继续可用。迁移或重新执行宽授权后，需再次应用消费者权限配置。
- 不增加业务提交点、连接、进程或环境变量。公开业务权限、Identity 校验与最终
  写入锁保持；显式种子／截图操作保留独立的操作账号配置。

旧视频恢复的源审计另确认：有 Jobs 执行账本时可复用现有 proof-bound 恢复；只有
旧 tracker scope 时仍缺完整枚举与旧 worker 的可验证停止证明；更早或 off/observe
路径可能根本没有持久执行身份。空表、终态、租约到期和操作者声明均不能证明物理
排空。全局 aap-video-restore 是有外部围栏前提的数据库恢复流程，不能冒充逐作业
修复。这些无证明旧记录继续409；完整恢复资格仍开放。

## Consequences

普通 API 和共享 worker 服务可以在同一数据库事务中通过固定能力工作，同时失去
Workspace 私表访问。语言替换客户端仍使用原 SQL 协议；Node 已实际读取／锁定并
验证同一事务，未获得私表权限。错误 PUBLIC 列授权会拒绝整份配置，防止部分收紧。

本地13个不同适用案例通过：11项权限／事务／共享 worker／旧授权检查、1项实际
HTTP生命周期与搜索、1项Node。覆盖模板与项目创建／修改／读取／删除、成员增删、
普通项目管理员的成员搜索、共同回滚、未来关系默认权限、错误部署身份与继承方向。
另有8项既有模块角色回归通过。交叉检查复现了旧应用授权把清理账号缩回两函数，
与现有清理 worker 的八项合同不符；脚本现保持这八项固定清理能力。夹具恢复维护
对象 owner，使用独立临时清理账号并撤销临时账号／ACL，保留审计数据到数据库清理。
原始失败、最小修复复验、JUnit与退出记录保留在
`.worktree/workspace-consumer-profile-qualification.json`。

本结论仅关闭普通消费者的本地 Workspace SQL 凭据边界。完整 Workspace 的冷工件、
统一运行装配、备份恢复与容量仍需集中验收，日常环境未切换，Go继续仅为实验。
状态仅在[模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)维护。
