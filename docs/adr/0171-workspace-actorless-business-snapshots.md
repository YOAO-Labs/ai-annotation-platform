# 0171 — Workspace 无 actor 的业务快照

- **Status:** Proposed，选定消费者已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；复用现有完整项目快照与调用方会话绑定

## Context

剩余普通 Project getter 分布于 API、任务执行、标注、媒体、导入导出、训练策略和
模型路由。失败预测元数据、部分后台作业及服务方法没有 actor；普通 ORM get 也不
执行授权。为读取配置而伪造项目 owner 或 actor 会混淆事实读取与当前权限检查。

完整 AST 盘点发现 88 处生产 getter，其中两处随后写 Labeling 计数投影。其余
86 处只读 getter 跨 52 个生产文件，十处使用 populate_existing。另有三个测试
seed getter。集合／标量 SELECT、排序排他锁及计数写入需要各自边界，不能按普通
getter 一并转换成逐行读取。

## Decision

新增 Workspace Native `read_project_row(uuid)`，返回现有完整 43 列快照的文本
JSON 或 null。它复用已有完整行序列化和 Labeling 计数能力，只读取调用方事务的
当前事实。函数为 STRICT、SECURITY DEFINER，固定 search_path，PUBLIC 无执行权；
现有应用清单增加这一函数，append、Assets／Inference owner 与删除专用账号不扩权。

现有 `get_workspace_project` 接受可空 actor，缺省时使用这个真实的无 actor 能力。
原 actor-aware 调用继续使用既有事实阶段；缓存、autoflush、完整过期、脏列恢复、
删除身份转换及共享完整行解码保持同一实现。目标 ID 为空时也使用 STRICT scalar，
保留原 autoflush 后返回 None 的顺序，不进入拒绝空 ID 的 actor-aware phase。

迁移全部 86 处普通只读调用，保留原会话、目标表达式、十处刷新参数、条件分支和
异步作业的项目缓存。原管理权、成员、撤销、最终写入、任务／批次锁和提交仍在原
位置完成。读快照本身不成为授权凭据。没有新连接、服务、角色、HTTP selector 或
自主提交点；其他语言可直接调用相同 Native 能力。

## Consequences

普通生产单项目读取由 Workspace owner 执行。实际受限 API 可在 READ ONLY 中读取，
同一 backend／transaction 保持，其他事务仍能取得账号、项目和成员排他锁。真实无
actor 的失败预测元数据 relay 及 Node 直接调用均通过，不经 Python 代理。

两处 Labeling 计数写入 getter、三个测试 seed、Batch 排序项目排他锁和其余集合／
标量查询仍分别保留。它们及数据集解绑、视频退役安全缺口、完整模块凭据与工件
继续由[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)跟踪。
本项不代替完整 Workspace 独立部署、冷安装、镜像或恢复资格。

## Notes

沿用旧源 a6ebe369b 冻结的 40 个 ORM 行为场景，不重新捕获或覆盖；当前两条绑定
分别复验，合计 80 个绑定案例。四组空目标／actor／autoflush 对照还比较了实际安装
ORM 的 pending work 行为。AST 对照逐处核验原会话、作用域、目标和刷新参数。

本轮 957 个不同定向案例通过，无有效失败或跳过；覆盖 785 个选定消费者案例，另有
核心、受限 LOGIN、严格回复、Node、回滚和适配器检查。80 个绑定复验包含在此数量中；
旧源执行不重复累计。原失败报告和真实退出保留，仅复验失败／已改案例并继续未执行项。

夹具修正包括导出 mock 的实际查找位置、相机来源尺寸在注册后被 file_size 变更失效、
卸载 mock 缺少 topology 参数，以及旧的路由代次预期。最后一项已核对原创建／启用
写入路径和冻结路由触发器均未改变：池激活和项目绑定各推进围栏，实际代次为 3。
没有修改业务守卫或放宽权限。导出 cache 的 AsyncMock 警告保留在原日志。

Native 冻结工件与限定迁移往返通过；完整工件与恢复仍集中在一致的 Workspace
里程碑验收。汇总为 `.worktree/workspace-project-read-qualification.json`。
