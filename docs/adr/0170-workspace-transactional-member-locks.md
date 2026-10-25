# 0170 — Workspace 同事务成员锁

- **Status:** Proposed，选定消费者已通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；保留现有最终写入和批量分派策略

## Context

最终写入围栏仍直接查询 ProjectMember.id 并取得 SHARE 锁；批量分派则在账号锁之后，
一次按 user_id 排序读取完整成员集合并等待 SHARE 锁。前者不能因无关时间字段解码
新增失败条件；后者需要保留完整九列、缓存刷新和原 targets 顺序的错误判定。

既有访问阶段固定 NOWAIT 且返回完整行，无法原样替代这两种读取。真实旧消费者基线
还显示，完全过期且已修改的成员对象需要先无锁恢复过期列，再 autoflush，最后读取
锁定快照；提前取得成员锁会改变事务中的锁顺序。

## Decision

新增两项 Workspace Native 能力，均借用调用方连接与事务：

- `lock_project_member_reference(uuid,uuid,boolean)` 只返回匹配的 user_id 或 null，
  按参数等待或 NOWAIT 取得成员 SHARE 锁，不物化其他成员字段。
- `project_member_rows(uuid,uuid[],boolean)` 返回固定完整九列的文本 JSON。true 在
  单次 MATERIALIZED 集合查询中按 user_id 排序取得阻塞 SHARE 锁；false 只提供所需
  的无锁刷新前读取。输入集合不生成逐用户 RPC，空集合返回空数组。

API 保留账号 → 项目 → 单成员的最终围栏，以及排序账号 → 排序成员集合的分派顺序。
原 membership_busy 映射、非竞争数据库错误、账号／职责校验、外层提交与回滚均不变。
完整快照使用既有 member_values 解码器与会话合并助手；额外校验项目、请求用户集、
重复身份与返回顺序。标量非空结果校验 UUID 类型与目标用户，接受驱动 UUID 子类。

完全过期且已修改的缓存行无法从已卸载字段确定请求范围，因此只在出现该状态时作
一次请求集限定的无锁读取；按 ORM identity PK 交集恢复 unloaded 列，不覆盖脏字段，
随后执行原 flush 与锁定集合读取。未匹配缓存对象不被修改。本项只承诺选定消费者和
冻结案例的兼容，不扩张为对任意外部脏 ORM 对象的通用 autoflush 消除器。

函数只加入现有应用权限并由 Workspace owner 持有。PUBLIC、append、Assets／
Inference owner 与删除专用账号不因此扩权。不新增角色、服务、DSN 或自主提交。
移除三个无实际调用者的旧 task membership helper，避免为死代码扩展新能力。

## Consequences

生产路径的两类直接成员锁读取由 owner 执行，批量分派仍原子锁定整个目标集合。
Node 可直接使用这两项 SQL 能力；API ORM 兼容绑定不要求 owner 使用 Python。

成员集合过滤、投影及跨 owner joins 仍由
[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)跟踪。
本项不代替完整 Workspace 凭据、工件、独立运行与恢复验收。

## Notes

旧源 025b801e6 的 22 项真实基线先通过并冻结：十二项分派缓存／autoflush 组合和十项
标量缺失／空值／异常时间语义。捕获拒绝覆盖既有案例；原始执行不累计为新实现数量。
验收入口和原始报告分别见 DEV.md 与 `.worktree/workspace-member-locks-qualification.json`。

本轮 224 项不同定向检查通过，无有效失败或跳过；其中三十八项成员锁合同／真实
消费者检查、十九项 Identity 适配检查，以及一百六十七项分派、跨帧、视频、导出与
权限回归。22 项旧源基线单独保留，不累计。首轮三项异常时间夹具未将字符串先绑定
为 text，修正后仅复验失败项并补齐未执行案例。源码复核发现的早期 Identity flush
顺序及 schema 函数清单错配已在验收前修正；清单与真实函数权限一并核对。

受限实际 API、Node、排序集合锁等待、精确 ACL、异常回复回滚和完整缓存基线均通过。
七项 export cache 夹具产生的 AsyncMock 未等待警告保留在原报告，不计为失败或跳过。
Native 工件／限定迁移往返通过；完整冷安装、镜像、fresh migration 与备份恢复仍集中
在完整 Workspace 里程碑验收。日常主环境、生产、远程 CI 与 Go 实验均未扩展资格。
