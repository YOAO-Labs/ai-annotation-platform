# ADR-0175：Workspace 项目集合与原生事实关系

- 状态：接受；限定本地读取边界验收通过
- 日期：2026-10-22

## 决定

新增 Workspace 持有的 `aap_workspace_v1.project_facts`，公开固定 38 列原生项目字段。
完整 ORM 读取在原 SQL 内按主键组合既有 Labeling `project_catalog_counters`，得到
`PROJECT_ROW_COLUMNS` 的 43 列。部分字段读取只用前者；不增加连接、提交点或逐行 RPC。
完整对象增加一次数据库内联接。两方来源都是同一项目主键，保持一对一与同语句快照。

旧 `project_catalog_configs` 返回完整 JSON 配置，缺少 pool binding，并改变部分
字段查询的日期解码边界，不能替代原生关系。独立审查找到既有计数视图；迁移与真实
数据库证明五个计数列全部 NOT NULL，COALESCE 对受支持数据恒等，因而移除重复的新
计数视图。共享关系描述复用既有列常量及原生类型，不另建过度约束原始值的 Project DTO。
两个阈值列按 PostgreSQL 实际的 real 类型声明，JSONB null、历史形状与非有限数保持。

## 消费者与行为

管理总览／绩效、预标摘要／队列、ML 总览、视频作业、失败预测、训练发现、审计范围、
通知／提及、导出标量、任务数据集回填及点云过期检查已迁移。保留原权限、范围过滤、
外联接、相关计数、排序和分页。Labeling 来源流仍只有原单条流式 SQL，只选择
tool_bindings，保持 populate_existing、yield_per 与关闭流程。

所有完整实体通过原 mapper 的 ORM 别名读取，保持 identity map、脏／过期字段和
延迟列。计数的 Core-only 初版在超级管理员无谓词分支丢失 autoflush，导致 total=1
但列表已有两项。补录不可变旧函数的对应基线、复现失败后，用 ORM 别名恢复原查询点
的自动刷新，不添加手动 flush 或全局钩子。

## 证据与边界

旧源码初始捕获 40 案例；审查补出的两项超级管理员场景从提交 22fba052 的完整函数
AST 执行，移除路由注册装饰器，保留原函数体和全局依赖。原 golden 不覆盖，最终同案
复验通过。受限 API 的原生列／owner／ACL、只读／同事务／无锁、已提交变更、缓存、
JSONB 与非有限数检查通过；真实 HTTP 和 Node 客户端不依赖提供方 Python 代理。

原失败日志与真实退出保留：schema 清单要求的空 defs、镜像描述不一致、real 类型
声明、超级管理员 autoflush，以及测试的 JSON SQL 参数／跨 owner 关系断言与 Node
计算列权限探针。先修复失败项最小用例，再继续未执行验收；计数不累计捕获或重复执行。
最终 125 项不同适用案例通过，无有效失败或跳过：42 项旧行为、15 项 Native／
HTTP／Node、4 项相关合同／客户端和 64 项实际消费者。
完整分类见 `.worktree/workspace-project-facts-qualification.json`。模拟导出通知夹具仍
产生既有 AsyncMock RuntimeWarning；触发处及发布函数 AST 与基线相同，实际受限
通知查询检查通过，原警告日志保留。

358 项适用架构检查及 20 项授权／选择单元通过，150 条架构规则按设计不适用。
Ruff／格式、显式 OpenAPI 核对、文档构建和差异检查通过；公开 API 合同保持。

三处 `labeling_tasks` 计数 UPDATE 仍是业务写入，未由本次读取迁移替代；测试 seed 和
运维脚本也不记为读取消费者闭合。应用仅新增公开关系 SELECT，默认宽凭据仍处于
兼容装配。完整 Workspace 凭据、冷工件、恢复及容量按
[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)集中验收。
本轮不切换主环境、不推送、发布、部署生产或扩展 Go。
