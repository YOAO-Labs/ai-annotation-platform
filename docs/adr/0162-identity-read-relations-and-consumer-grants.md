# 0162 — Identity 集合读取关系与独立 API 消费者权限

- **Status:** Proposed，源码与限定本地定向验收通过；完整 Identity 继续收口
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0161](0161-identity-current-authority-consumers.md)

## Context

单账号权威读取和锁已有 Identity capability，但集合成员、通知、搜索、管理统计、
绩效与作者展示仍连接 User 原表，审计还依赖邀请的项目目标。改成逐行 RPC 会拆开
同语句过滤／排序／count 和调用方快照，且不能保留 inner／outer join 的缺失行语义。
仅换成 view 而保留消费者原表权限，也不足以建立独立模块的数据边界。

## Decision

沿用现有 owner 公开关系模式，安装两个 Identity-owned security-barrier view：

- `account_profiles` 只发布 `id/name/email/role/is_active/status/avatar_ref`；
  `avatar_ref` 保留原 nullable varchar 字符串。
- `invitation_audit_targets` 只发布 `id/project_id`，包括 nullable project target。

便携描述为 `aap-identity-read-relations/v1`，生成函数位于独立 Identity 包，包内
与迁移冻结 JSON 保持一致。关系没有隐藏过滤、排序、类型转换或凭据字段；每行仍
对应一个 owner 记录。PUBLIC 无权限；当前实际读取关系的 API consumer 获得 SELECT，其他 owner 和普通
AUTH、key-management 和 workflow LOGIN 不扩权。公开关系可枚举其全部行，因此
业务 scope、active 和角色配对条件仍在原消费者语句内。

剩余生产集合／展示／邀请审计查询迁移到这些关系，保留筛选、连接类型、排序、
count、limit、cursor 及 inactive／历史参与者行为。Core 完整行读取先转为普通字典
再由闭合 DTO 校验，显式沿用 Session 的 autoflush／no-autoflush。事务、提交和回滚
仍属于原调用方。已无调用者的旧 `management.user_scope_clause` 一并移除；不复制
其死代码到新边界。受守卫的隔离 seed 操作保留其独立权限需求。

已有 API consumer 的全部 Identity binding 选择远程 owner 后，在 owner／宽授权
之后应用 `identity-consumer-grants.sql`。脚本核对部署身份和固定合同，撤销 Identity
私表／列、私有 schema 和 Identity 命令函数权限，仅保留两个 SELECT 关系与五个
调用方事务能力：当前账号事实、三种账号锁及已接受 API-key 最终检查。它不新增
LOGIN、DSN、runtime 或部署模式，不改内嵌 Identity 与隔离 seed 的旧授权配置。

该清单针对当前既有对象。旧 LAN 授权及默认 ACL 继续服务兼容绑定，不能在 consumer
清单之后重新应用；每次授权刷新和新增 Identity 对象都需重新核对最终权限。
本文限定了所选远程 consumer 的数据库能力，不声称宽权限主 API 已完成切换。

## Consequences

同一物理 PostgreSQL 下，集合读取、锁与调用方业务写入仍保持原事务语义；另一语言
实现只需继续提供固定 owner 关系和能力，不需要导入 API ORM。关系中的 email 等
资料对获授权后端可枚举，因此不能作为对外目录或用户可直接查询的视图。

只读限制由 consumer ACL 强制，Identity owner 仍持有自己的写入能力。部署必须
按最终权限清单排序，避免宽授权重新打开边界。完整模块的资格只在唯一
[模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)记录。

## Notes

已通过的 12 项 Native／adapter 案例覆盖真实 SQLAlchemy RowMapping、原始字段与
可空邀请目标、autoflush／no-autoflush、调用方 rollback、READ COMMITTED 与
REPEATABLE READ、真实受限 LOGIN、五能力 allowlist、私表与视图写入拒绝，以及
成员／通知的 active、历史角色、viewer 配对和 export 范围。另四项部署 guard
验证错误数据库／操作者／owner／consumer 不改变原 ACL。

修复前报告保留：严格 DTO 不接受 RowMapping，适配器现显式转 dict；授权 guard 的
CASE 条件曾有 PL/pgSQL 语法错误，已先在最小隔离 PostgreSQL 中复现并修复，再重跑
完整受限角色案例。旧 SQL 编译测试的表名断言已更新，15 项最小复验通过。

旧 HEAD 读取 helper 下也能复现历史角色返回 401 与 User fixture 缓存未被刷新的
断言失败。修正测试后继续核对 401/Bearer、响应与持久记录的当前作者名及精确通知
payload；不改变生产提交或权限语义。早期 baseline harness 的参数数目错误及混合
旧新 scope 警告保留在原报告，后续最小复现移除该干扰。

原始证据位于 `.worktree/identity-read-relations-native-third.*`、
`identity-read-relations-consumers-first.*`、`identity-read-relations-scope-reproducer.xml`
及 `identity-read-relations-assertion-reproducers.*`。最新有效结果去重后为 172 项选定案例，零失败／错误／跳过，汇总于
`identity-read-relations-qualification.json`；包含六项真实提交后的投递、Redis 失败及
数据库回滚检查。专用第二测试库在同一验证器拥有的临时 PostgreSQL 内建立，核对实际
库名与 ownership marker、关闭客户端后删除，未复用日常数据库。最初生成的 64-byte
库名被 PostgreSQL 截断，连接目标守卫拒绝执行；缩短名称并预核对物理库名后补验通过。

运行授权／测试选择单元检查 20 项通过；架构检查 355 项通过、149 条规则不适用，
不与 172 个选定案例叠加。架构清单撤销 annotation_comment 对 User 模型的许可，
只允许新的公开关系 adapter，并明确限制该 adapter 不能依赖 app 模块。
完整 Identity 冷安装／容器／备份恢复仍按唯一台账在一致里程碑集中验收；本次未切换
主运行环境、推送或部署生产。

最终权限复核只向当前实际消费者授权：保留 Identity 对 view 的所有权、现有 API
application role 和远程 consumer 的 SELECT，不向尚无关系调用的其他 owner 提前
授予资料枚举权限。单独的真实 ACL 检查验证这些 owner 与函数客户端均不可读取关系。

源码与文档集成提交为 `935aeca26af2cf31077f8c78a9208cfc068182ad`，正常提交钩子（含 OpenAPI 与路由文档生成）通过，
公开接口工件保持。汇总清单中的 32 个源码／测试／权限文件指纹与该提交一致；
172 项选定案例、20 项运行授权／选择检查、355 项架构检查及文档构建通过，
149 条不适用架构规则另计。三个源码工作树已按已核对的分支和提交清理，分支保留；
只移除本次本地虚拟环境、字节码和构建缓存。主工作区及四个共享容器保持原状态。
