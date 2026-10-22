# 0172 — 项目集合锁与 Labeling 计数持久化

- **Status:** Proposed，选定操作已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None

## Context

普通项目 getter 迁移后，仍有两处 Labeling 重算写入 Project 计数，以及批次决策的
排序排他锁。直接改为即时 UPDATE 会改变原 autoflush、延迟 flush、缓存与时间戳；
逐项目锁调用也会拆开原集合语句。

## Decision

Workspace 提供 `lock_project_references_for_update(uuid[])`，在单个 MATERIALIZED
有序查询内取得 NOWAIT 排他锁，只返回现存 UUID 集。重复／缺失 ID 保持原集合语义，
原账号、管理权、成员、批次与任务检查及 SQLSTATE → 409 回滚位置不变。

Labeling 提供 `write_project_counter_projection(uuid,jsonb)`。补丁只允许四项任务计数、
`batch_summary` 和可选 `updated_at`，且必须至少包含一项计数。动态 UPDATE 仅使用
固定白名单列，参数绑定值；未提供显式时间戳时用事务 now()。返回目标 UUID 或缺失
null，不重算、不自行授权或提交。两项 SQL 均固定 search_path、SECURITY DEFINER、
撤销 PUBLIC 执行权，由既有 owner 持有；应用及 worktree 兼容 grant 同步更新。

API 的两处旧 getter 改用现有 Workspace 快照。定向 Project mapper 适配器只处理
已登记项目的五项计数 history，在原 UoW 的 before_update 内调用 Labeling Native，
清除这些列的 ORM 写 history；隐式 updated_at 在 after_update 过期。其他脏字段及
显式时间戳沿用原 ORM 行为。每个可能消费标记的 autoflush 后，在下一批计数赋值前
重新登记。事务结束或回滚清理私有标记，Session.close 后复用不会泄漏到其他写入。
这不是通用 ORM 代理，也没有全局 flush hook、额外连接、角色或运行服务。

缺失行抛原 StaleDataError；错误类型或错误 UUID 回复为 503。异常发生在 UoW 内，
真实 Native 已执行的写入也随调用方事务回滚。未变化缓存不写数据库；延迟计数仍待
外层 flush。混合其他脏 metadata 时可产生分开的 Native／ORM UPDATE，保留原最终值、
事务、flush 和 timestamp，不声称物理语句数与任意通用 ORM 写入完全一致。

## Evidence and limits

修改前在 9e37fe5b 冻结 32 项真实 ORM 场景，覆盖两处方法、autoflush 开关、缓存命中／
过期、其他脏字段、显式时间戳和并发修改后的旧缓存。golden 不覆盖或重捕获。旧批次
查询没有 ORDER BY，最后待 flush 行受随机 UUID 顺序影响；复验比较已落库与待写入
投影全集及待写数量，保持该无序语义。

本轮 243 项去重适用案例通过：32 项旧行为复验、23 项受限 API LOGIN／PG／Node、15 项
适配器和 173 项实际批次／权限／清理消费者；另 1 项既有权限参数分支按设计跳过，其放行边由专门正向用例覆盖。真实 API 在 Project SELECT 与 UPDATE
被拒绝时完成选定操作；同一 backend／transaction、外部不可见性、回滚、NOWAIT 竞争、
过期删除、只读拒绝及故障后 Session 复用通过。关闭后复用标记泄漏和兼容 runtime
执行权遗漏均先保留失败，再最小复验；其他夹具失败保留原报告。

汇总 `.worktree/project-owner-persistence-qualification.json` 绑定源与证据。限定 Native
迁移往返通过；完整 Workspace 冷工件与恢复仍集中验收。Project／成员集合、目录、
运维分页和视频删除安全缺口继续由[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)跟踪。
没有日常环境切换、生产部署、推送、发布或 Go 扩展。
