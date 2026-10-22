# 0177 — Workspace 数据集解绑的可移植事务绑定

- Status: Proposed（限定本地调用边界验收通过）
- Date: 2026-10-22
- Deciders: AAP maintainers
- Supersedes: None

## Context

数据集解绑已有冻结 Native 协调器，组合当前权限、关联代次、来源保留、任务／计数、
派生清理、Jobs、Audit 与不可变回执。但合同与调用绑定仍位于 API 内，独立调用方
不能直接复用这些值和约束。提取需保留原外层事务，不能增加远程提交点或改变删除规则。

## Decision

Workspace 包新增 `dataset_unlink_contract`、`DatasetUnlinkManager` 和
`DatasetUnlinkPostgresData`。前者迁移原有命令、预览与回执，API 保留兼容导出。
字段顺序、UUID、严格计数、soft=false、批次身份校验、开放错误码，以及省略 Audit
上下文时的 None 默认均与原合同保持。Manager 将成功回执绑定到原五项请求身份。

只复用两项既有 `aap_workspace_v1` 能力：

- `preview_dataset_unlink(uuid,uuid,uuid)`
- `unlink_dataset(uuid,uuid,uuid,uuid,text,jsonb)`

API 的 SQLAlchemy 适配器保留原 SQL 与参数；解绑仍显式 flush 后调用一次函数，
预览不增加显式 flush。应用的缓存失效、错误／HTTP 映射及成功后的 commit 保持。
不修改冻结 SQL、迁移 head、业务权限、锁顺序或回执重放条件。

独立 asyncpg 绑定要求借用已开启的调用方事务，在权限验证前后检查连接与事务。
`workspace_dataset_unlink_client` 仅能执行上述两项函数，没有私表／列、序列、DDL
或 TEMP 权限。沿用既有 function-only 验证器；意外增加执行权限也会在业务调用前
拒绝。绑定不连接、关闭、begin、commit、rollback 或重试。预览会获取当前权限锁，
不能将它放进 READ ONLY 事务；预览与解绑都保留数据库原来的只读拒绝行为。

原始 JSON 使用既有严格解析器拒绝重复键与非有限数；Python Mapping 直接交给原
Pydantic 模型。统一 JSON 往返初版拒绝了旧实现接受的 UUID／tuple 值，已通过不可变
基线 17992f64 的原解码函数 AST 复现并修复。新边界同时接受现有 Python 值及原始
JSON，未放宽计数、分支或字段约束。Assets 的 CleanupBucket 约束直接复用既有轻量
合同包，显式声明依赖；不复制规则或安装清理 worker 可选依赖。

## Consequences

正向：API 与独立调用方共享相同合同和管理器。受限 asyncpg 与 Node 直接调用同一
Native 协调器，预览锁、同事务、失败提交、保留检查、忙锁恢复与冻结回放有实际证据。
Node 原样转发回执 JSON，审计 ID 超过 JavaScript 安全整数范围时仍保持 PostgreSQL
精度。公开 API 响应与幂等键保持，真实 API 不再需要 Project 私表 SELECT／UPDATE。

负向：共享 PostgreSQL 与调用方事务仍是本边界前提。完整模块工件／凭据、另一语言
完整业务提供方及生产容量不由本操作资格代替。未退役视频执行被删除后留下输入 pin
与运行态摘要的既有安全缺口尚未修复，本轮不能关闭完整解绑安全能力。

## Notes

72 个不同适用案例通过：34 项包级合同／管理器／连接检查、23 项旧绑定／协调器／
HTTP 复验、15 项受限 Native／Node／实际 HTTP。无有效失败、错误或跳过；迁移前
23 项基线、失败与最小复验单独保存，不重复计数。测试夹具修正展示用 schema 标题、
随机 UUID 比较、ORM Session 断言上下文及权限验证器的实际异常类型；业务逻辑保持。
完整分类见 `.worktree/workspace-dataset-unlink-qualification.json`。

默认宽应用凭据继续兼容装配；日常主环境未切换。冷工件、恢复及完整模块验收继续
按[唯一台账](../../docs-site/dev/concepts/module-deployment.md#剩余工作台账)推进。
本轮不推送、发布、部署生产或扩展 Go。
