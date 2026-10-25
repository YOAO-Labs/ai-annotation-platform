# 0173 — Workspace actor 范围与列表读取

- **Status:** Proposed，选定读取通过限定本地验收
- **Date:** 2026-10-22
- **Deciders:** Pending maintainer review
- **Supersedes:** None

## Context

单项目读取与锁收口后，范围 helper、看板及作业／通知列表仍直接访问项目或成员表。
它们要求原 SQL 内的过滤、联接、计数和分页，不能改成逐行 RPC 或预取后 Python
过滤。已存在的项目搜索范围提供候选项目和元数据，但不校验活跃、已知角色及 viewer
配对；旧成员 helper 还支持只提供 actor ID，不能新增 role 依赖。

## Decision

新增 `aap_workspace_v1.actor_project_memberships(uuid)`，STABLE、STRICT、SECURITY
DEFINER、固定 search_path、PUBLIC 无执行权，由 Workspace owner 持有。只返回
原始 project_id／member_role，NULL actor 返回空集。不联接账号或项目、不加载日期、
不加锁、不写入或提交。未知职责保留为事实，调用方继续在同一 SQL 联接 Identity
account_profiles 并执行原活跃、已知角色、viewer 配对及 requested roles 规则。

所有者与看板元数据复用既有 project_search_scope。它是候选关系，不是授权凭据；
超级管理员的 None／全局分支保留，owner 分支保留原调用方状态判断。非超级管理员
作业的项目存在性子查询与后续 membership／owner／export 条件共同保持最终结果；
不声称存在性子查询单独等价。全局、未解析、retained 或已删除项目语义不变。

看板继续保持 batch／任务 claim、协作视频、项目管理员 owner override、历史个人
统计、排序和限制。通知只迁移 actor-list 的范围／存在性，任意接收人对仍保留原四次
查询。没有把可见性规则合并为一个新的授权结果，也不改变提交点或连接。

窄 ORM 列别名保持独立 Select 和嵌入查询的原 autoflush，只选择所需列，不物化成员
实体或日期。原 Core Select 版本在独立执行时漏掉 pending work 的 autoflush，已先
用真实受限账号复现后修复。读取工厂移至独立客户端模块，三个函数体 AST 保持；旧
query-data 导入路径继续导出同一实现。客户端可在禁止导入 Workspace 提供方运行时
时构造相同 SQL，避免为读事实引入新的提供方代码依赖。

应用 grant 增加既有搜索范围函数与新成员函数的执行权，worktree 兼容 grant 和 owner
清单同步更新；不向 API 开放私表 SELECT 或 Identity 认证／资料命令。受限 HTTP 夹具
显式使用既有 Identity AUTH、Briefs owner 连接，没有以权限放宽或认证 mock 绕过。

## Evidence and limits

在 a6ddf032 冻结 126 个旧场景，每个含 autoflush 开关观测；owner 与受限 API 两条
绑定分别复验。覆盖 caller／当前 profile 的角色和 active 差异、异常 employee owner、
viewer 配对、空／指定职责及 year-10000 成员日期；不重捕获或覆盖 golden。

本轮 415 个不同案例通过，无失败或跳过，含 SQL 适配器、受限 PG／Node、实际 HTTP
看板、作业／通知列表和相关业务消费者。新方法只有 actor ID、NULL actor、独立查询
的 pending work、无私表权限、只读／同事务／无锁及已提交撤权都有定向检查。真实
审核场景覆盖协作视频、其他用户 claim 的 owner override 和最近审核 DTO。

原失败与真实退出保留：夹具 display ID 长度、应用搜索范围执行权遗漏、Core
autoflush 差异，以及 HTTP 夹具未选择 AUTH／Briefs owner。ID-only 合同也通过旧源、
初版和当前代码的构造对照及既有数据库用例核对。修复最小用例
后再继续未执行项，或为新实现／绑定复验；不为计数重复整批。

汇总 `.worktree/workspace-actor-scope-qualification.json` 绑定源和证据。冻结 Native
安装／回滚及限定迁移往返通过；完整 Workspace 凭据、冷工件、恢复和容量仍集中
验收。其余成员集合、Project 投影／owner 子查询及视频删除安全缺口继续由
[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)跟踪。
本次不切换主环境、不推送、部署生产、发布或扩展 Go。
