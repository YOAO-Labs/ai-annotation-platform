# 0164 — Workspace 相邻项目写入的同事务能力

- **Status:** Proposed，源码与限定本地定向验收通过
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None；扩展 [ADR-0136](0136-workspace-configuration-command-owner.md)

## Context

普通项目、模板及成员命令已有 Workspace owner。仍有两处应用工作流直接修改
Workspace 项目字段：nuScenes 运维导入查找／创建项目，以及点云质量评估晋升更新配置。
前者要保留调用方给定的稳定 `P-NU-*` 标识，并与素材关联、建题和标定一起提交；通用
项目创建命令会分配新的 `P-<seq>`。后者要与评估晋升记录及 Audit 原子提交。把这两处
接到独立 HTTP／数据库事务，会拆开现有原子性。

## Decision

复用 `aap_workspace_v1`，以 `aap-workspace-project-maintenance/v1` 描述三项固定 SQL
能力。包内 JSON 与迁移冻结描述由同一个 `project_maintenance_schema()` 生成：

- `read_import_project(text)`：按原 display ID 精确查找，仅返回绑定该 ID 的可空项目 UUID。
- `create_import_project(uuid,text,text,uuid,jsonb)`：插入调用方指定 UUID、display ID、名称、
  owner 及工具绑定的点云导入项目，保留原 ORM 客户端默认值；仅返回绑定的项目引用。
- `write_point_cloud_quality_config(uuid,jsonb)`：更新精确项目的配置及原事务时间戳；相同
  JSONB 值不推进时间戳，返回绑定项目的带时区时间。

函数由 Workspace NOLOGIN owner 持有，封闭 search path 并撤销 PUBLIC 执行权限。
沿用现有授权脚本模式，`workspace_project_maintenance_client` 仅获得这三项业务 Native
能力，无表／列、序列、DDL 或 TEMP 权限；建题 `append_client` 与其他能力账号不扩权。
这里的“三项”限定业务 owner 能力，PostgreSQL 自带及历史 public invoker helper 的
继承权限不计入此数；它们不因此获得私有表权限。

应用适配器始终借用调用方连接和事务，不新增服务、DSN selector、提交、回滚、重试或
回执账本。查找保留 autoflush／no_autoflush；创建保留原来的显式全量 flush。导入工作流
只传递 UUID，不再水合或写入 Project ORM。已存在的 display ID 仍直接复用，不增加
owner／类型检查；并发查找后创建仍可产生原始唯一冲突，不改成 upsert。

质量晋升保留原 Project `FOR UPDATE`、ORM 读取语义、评估查找／gate／baseline 校验和
配置修订递增。当前管理权限仍由既有公开入口重验；Native setter 不另外增加授权、CAS
或锁模式。写入后只用 `set_committed_value` 同步当前 Project 的配置和时间戳，评估与
Audit 继续留在同一外层事务。严格解码拒绝额外字段、重复 JSON key、非有限值、错误
标识和无时区时间；失败不能触发 ORM 回退。

`workspace-project-maintenance-grants.sql` 校验目标库、迁移账号、owner 与 LOGIN，
并核对精确业务函数、私表与 DDL 权限。应用／工作树的显式函数清单一同更新；这些
授权源文件没有应用到日常主环境或生产环境。

## Consequences

这两类跨域工作流通过 Workspace 的语言无关数据能力修改项目，保留共享 PostgreSQL
上的原事务和当前授权顺序。Labeling 拥有的项目计数列继续由 Labeling 维护。

这些能力信任运维／管理工作流，不是新的用户 API，也不替代身份认证或项目权限围栏。
新回复是事务内结果，不是持久幂等回执。完整 Workspace 的删除／解绑装配、其余权限
消费者及模块资格只记录在[唯一模块台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)。

## Notes

修改前的点云评估与 nuScenes 测试共 28 项通过，保留为旧行为证据。当前定向结果为
26 项合同／适配器检查、1 项实际精确授权检查及 38 项 Native／真实消费者与回归检查，
共 65 个不同案例，无失败或跳过；旧 28 项不重复相加。

真实 PostgreSQL 检查覆盖完整导入默认值与旧 ORM 对照、稳定 ID、未提交不可见、同值
配置不写时间戳、目标隔离、原始唯一／长度／FK 错误及失败事务状态、锁等待与调用方
持锁。评估／项目写入在后续失败和错绑定回复时共同回滚；Node 直接调用三项 SQL 能力
并验证回滚，没有代理 Python 或 API。冻结 SQL 安装／回滚及隔离资源清理通过。

证据汇总为 `.worktree/workspace-project-maintenance-qualification.json`。本次是源码与
操作级定向资格，完整模块的冷安装、容器、完整迁移和备份恢复在 Workspace 一致
里程碑集中验收；保留既有有效资格。本次没有主环境切换、推送、发布或 Go 扩展。
