# 0092 — 同库事务中的版本化模块数据接口

- **Status**: Accepted
- **Date**: 2026-10-18
- **Deciders**: 项目维护者授权继续实现所有模块独立部署与语言替换
- **Supersedes**: None

## Context

新增 DatasetItem 后，为其当前关联的项目创建 Task，并维护项目任务计数，原本通过
多个 Python 服务共享同一个 SQL Session 完成。直接改成多次 HTTP 请求会丢失调用方
回滚素材、任务与计数的原子性。让独立进程继续直接读写其他 owner 的表，也无法建立
数据权限边界。本计划允许共用 PostgreSQL，但要求跨 owner 访问只通过公开合同。

## Decision

1. 强事务数据操作采用 owner 发布的版本化 PostgreSQL 函数。客户端通过 PostgreSQL
   协议传递 UUID、UUID 数组和 JSON，拥有外层 `BEGIN`、`COMMIT` 与 `ROLLBACK`。
   函数不提交事务，不接收任意 SQL、表名或存储凭据。普通远程服务交互继续采用 HTTP。
2. Workspace 的 `aap_workspace_v1.lock_project_reference` 只保证项目引用存续；
   它不是用户认证或当前项目授权接口，不能替代现有撤权及写入准入校验。
   Assets 的 `aap_assets_v1` 发布 dataset 引用、当前关联和追加建题来源的最小快照。
   Labeling 的 `aap_labeling_v1.append_dataset_items` 独占 Task、追加操作回执和
   `projects.total_tasks` 历史物理列上的派生投影写入。Assets 函数通过 Workspace
   合同引用项目，不读取其私有配置。
3. 每个操作接收唯一 `operation_id`，将 dataset 与排序去重后的 item ID 数组作为
   规范请求保存。同一请求和操作 ID 返回原冻结结果；同 ID 的不同请求以 SQLSTATE
   `22023` 和 `operation_conflict` 拒绝。回执与业务写入同事务保存，不另建消息系统。
   原有上传、扫描和连接器每次调用分配新操作 ID，保持重复追加返回零的旧语义。
4. 调用顺序是 dataset `NO KEY UPDATE`、操作回执、按 UUID 排序的项目引用与关联、素材引用、
   按 UUID 排序的项目计数写锁。关联创建、解绑和素材删除采用兼容的父行优先顺序。
   Dataset 的锁不能升级成 `FOR UPDATE`，以免和新素材外键已取得的 `KEY SHARE`
   相互等待。素材引用使用 `KEY SHARE`，允许缩略图、尺寸和内容观测等非键更新。
   锁保留至外层事务结束；该切片以现有 `READ COMMITTED` 隔离级别验收。
5. 安装工件是冻结 SQL，Alembic 与 `psql --single-transaction` 使用同一份实现。
   语义不兼容时增加新 schema 版本，不能原地改变既有合同。函数使用固定
   `search_path = pg_catalog, pg_temp`、限定对象名的 `SECURITY DEFINER`，创建和
   撤销 `PUBLIC` 权限在同一事务内完成。配置遵循 PostgreSQL 的
   [安全函数说明](https://www.postgresql.org/docs/16/sql-createfunction.html#SQL-CREATEFUNCTION-SECURITY)
   和[行锁兼容规则](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS)。
   JSON Schema 由数据接口 owner 持有，Python 响应模型是客户端绑定；实际函数结果与
   冻结 schema 对照验收，不将主 API 的 OpenAPI 作为这个内部 SQL 合同的真值。
6. 独立部署时，函数由各模块独有的 `NOLOGIN` 角色持有，仅授予实现必需的列权限。
   专用调用角色只有公开函数执行权，无外部表的直接列或表权限、owner 继承或建表权。
   `infra/docker/module-data-grants.sql` 验证显式数据库、角色属性与有效权限后原子安装。
   原主 API 的宽权限角色仅作为迁移期装配；它不能被当成已隔离的模块凭据。

## Consequences

- 语言替换不再要求共享 Python ORM、Session 对象或异常类型，但这条强事务接口仍
  依赖同一 PostgreSQL 实例、连接和事务。它不提供独立数据库引擎或跨数据库原子提交。
- 回执中的项目计数是操作完成时的快照；重放不能覆盖当前计数。Python 适配器会失效
  已缓存的计数属性，再由需要它的旧调用方刷新。序列号在回滚后可能留空号，与原行为一致。
- 回执没有素材或项目外键，删除源数据不会改写已返回结果。当前不自动清理回执，部署
  仍需为其容量和保留策略作后续决定；不能在未经定义的重试期限内删除幂等证据。
- 本片只覆盖已有数据集上的追加建题及其冲突路径。初次关联的分块建题、队列在途作业
  的关联代次、完整授权协议、项目级删除和其余模块提取仍须单独迁移与验收。
  模块整体状态继续见[部署台账](../../docs-site/dev/concepts/module-deployment.md)。
