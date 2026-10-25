# 0166 — Workspace 锁定权限读取与会话快照

- **Status:** Proposed，锁定读取边界已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；复用现有 `aap-workspace-project-access-step/v1`

## Context

当前写入路径按账号、Project、ProjectMember 的顺序锁定并重读权限。原 ORM 查询同时
使用 `populate_existing` 更新调用方的对象：训练冻结会读取同一 Project 的类型、工具
配置和状态，标注采纳及子标注校验也会继续读取该对象。只迁移权限事实而省略缓存刷新，
会改变这些实际消费者读到的业务状态。

Workspace 已有固定的 Native Project KEY SHARE 和成员 SHARE 读取阶段，可在调用方
同一事务内返回完整快照。另起连接或在锁后再次 SELECT Project 都不能保留这个边界。

## Decision

保留 `lock_current_project_rows` 和 `lock_current_project_access` 的原有元组接口及全部
调用方。账号仍通过 Identity 的 SHARE NOWAIT 能力获取完整 facts；随后调用现有
`project_access_step(uuid,uuid,text)` 的 Project 阶段，非 manager 才执行成员阶段。
所有步骤使用同一请求 Session、原 `no_autoflush`、锁序和外层事务。

严格校验回复 schema、actor、project、phase 与单一 facts 分支，UUID 大小写保持格式
兼容；错误回复不回退私表读取。Project 使用既有完整行编解码，成员使用既有完整
成员编解码。基础 Project materializer 从现有查询 materializer 提取，只排除查询附加
字段，继续保留 JSON、UUID、浮点、日期、时间戳及历史异常行为。

Project 的 43 项和成员的 9 项字段必须完整匹配 ORM 列。适配器用完整快照创建干净的
detached 对象，通过 `merge(load=False)` 同步到原 Session。它不再次 SELECT，也不
flush、提交、回滚或更换连接；已有对象保持身份并清除旧字段历史，未关联的 pending
work 保留。没有成员行时不移出调用方原有的过期成员对象，权限仍按当前事实拒绝。

Project 继续使用 KEY SHARE，允许普通计数的 NO KEY UPDATE，围栏负责人转移和删除；
成员使用 SHARE。锁竞争仍返回对应的 `account_busy/project_busy/membership_busy`，
其他 SQL 错误保留。manager 跳过成员读取，账号停用、不可见、职责不兼容的原有响应
不变。旧业务 Project 日期失败仍发生在成员阶段之前。

仅向既有 application role 的生产授权脚本和本地运行时函数清单加入此已有能力。
不扩展 `append_client` 或专用删除账号，也不增加角色、服务、运行时配置或迁移。
非锁定的 Project／成员读取继续保持原实现。

## Consequences

当前锁定读取不再要求 API 直接 SELECT Project、成员或 User 私表；Node 可以直接执行
同一组版本化 Native 能力。完整事务和 ORM 兼容性留在调用方绑定，owner 不依赖 API
的 Python 模型。原有调用方得到同一锁定快照，不必分散修改训练和标注业务。

本次不代表完整 Workspace 的最小凭据或独立工件已经闭合。应用兼容账号还承担其他
操作；非锁定读取、完整装配及删除安全状态见
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

## Notes

原 ORM 的独占测试库基线先确认了脏对象刷新、成员版本及 weekly target 同步、其他
pending Task 不被 flush；同一案例在新 Native 路径通过。定向结果共 163 个不同案例，
覆盖 9 项新真实数据库检查、共享及 API 编解码、原权限端口、标注范围、帧持久化、
pipeline 校验、反馈、训练冻结及三种绑定的冻结时间字段响应。

真实 API LOGIN 临时撤销 Project／成员 SELECT，既有 Identity fence 已拒绝 User
私读；检查后恢复原 ACL。账号／项目／成员锁保持到调用方结束，普通计数更新可继续，
错误范围回复会回滚调用方此前的写入。Node 进程使用同一受限 LOGIN，无 Python 代理。
HTTP 兼容检查使用独立的当前源码 Workspace 进程，未新增非 editable 工件资格。

首轮单元测试漏写账号锁事件、HTTP 用例缺少提供方解释器，分别最小复验通过；原始失败
保留，其余有效证据沿用。汇总位于 `.worktree/workspace-authority-locks-qualification.json`。
完整 Workspace 冷安装、容器、完整迁移和备份恢复继续在一致模块里程碑集中验收。
