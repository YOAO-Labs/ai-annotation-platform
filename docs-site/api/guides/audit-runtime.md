---
title: Audit 运行时
audience: [dev, ops]
type: reference
status: experimental
last_reviewed: 2026-10-25
---

# Audit 运行时

`apps/audit` 持有审计追加、查询、公开事件／日聚合关系，以及分区维护与归档。
公开审计 API 保留原有角色、项目范围、分页、导出和事务行为。数据交互使用固定
PostgreSQL 能力与 JSON 合同，维护工件不依赖 API、Celery 或通用 Assets 存储配置。

## 数据与事务边界

业务追加沿用 `aap-audit-append/v1` 与 `append_events(jsonb)`，查询沿用
`aap-audit-query/v1` 与 `query_events(jsonb)`。API 依据追加回执恢复原 ORM 身份，
保留调用方 flush／commit／rollback；项目范围读取使用公开 `audit_events`，
月报与 DuckDB 继续使用现有公开关系。HTTP 元数据审计仍按原有 best-effort 语义执行。

`aap-audit-maintenance/v1` 的固定入口是
`aap_audit_v1.maintenance_command(jsonb)`。请求包含 `schema`、`operation` 及该操作
声明的字段，拒绝未知字段。成功回复为 `schema/ok/data`。

| operation       | 作用                                                   |
| --------------- | ------------------------------------------------------ |
| `partitions`    | 列出固定月分区的名称、UTC 范围和 OID                   |
| `ensure`        | 按月首日期创建缺失分区，核对已存在对象的附着关系和范围 |
| `snapshot`      | 核验月分区身份并锁住已完成月份，读取公开事件快照       |
| `drop`          | 仅在同一事务已经持有快照锁时退役对应分区               |
| `archive_event` | 追加固定 `audit.archive` 事件，保持原有归档字段        |
| `refresh_daily` | 复用原日聚合并发刷新能力                               |

维护端核对月分区名称、OID 和边界，当前或未来月份不能归档退役。快照取得
`ACCESS EXCLUSIVE` 锁并持有到调用方事务结束，上传成功后才删除分区并记录归档事件。
上传失败保留分区；数据库步骤失败或取消时回滚。原对象键
`YYYY/MM.jsonl.gz`、JSONL 行格式与公开归档读取响应保持一致，归档数值需保留 bigint
精度。对象上传与数据库提交仍跨两个系统：提交失败可能留下对象，下次按原月份重试。

## 独立运行

`aap-audit-maintenance` 提供一次性 `ensure-partitions`、`archive-partitions` 与
`refresh-daily` 命令。默认分别补当前及未来三个月分区、归档十二个月保留期以外的
月份，以及刷新已完成 UTC 日聚合。每次操作使用一个数据库连接和事务；启动时核验
当前 LOGIN 只能执行固定维护能力，不持有私表、序列、DDL、owner 成员资格或其他模块
函数权限。独立 CLI 不需要 API 控制 bearer 或其他模块凭据。

数据库配置为 `AUDIT_MAINTENANCE_DATABASE_URL(_FILE)`，直接值与文件二选一。
仅归档命令读取 `AUDIT_MAINTENANCE_ARCHIVE_S3_ENDPOINT_URL`、`REGION`、`BUCKET` 和
`ACCESS_KEY_ID(_FILE)`／`SECRET_ACCESS_KEY(_FILE)`。归档桶须已配置；写身份只需该桶的
检查和对象上传权限，不需要建桶或删对象权限。密钥文件有界读取，配置失败不回显秘密。

可选 `docker-compose.audit.yml` 使用非 root 锁定工件、文件 secret 和明确的一次性
命令，不发布端口，不自动启动归档。与选定数据库加入同一网络后可运行：

```bash
docker compose --env-file .env.audit -f docker-compose.yml -f docker-compose.audit.yml \
  --profile maintenance run --rm --build audit-maintenance ensure-partitions
```

Compose 的数据库 `_FILE` 指向宿主 secret 文件；归档访问 key 的两个 `_FILE` 在运行
归档前也须设置。调度交接时只启用一个维护调度来源，原 Celery 入口继续作为兼容绑定。

## 管理员归档读取

原管理员 API 的列表和读取权限保持。`AUDIT_ARCHIVE_READ_S3_ENDPOINT_URL` 留空时
使用原内嵌存储绑定；设置后使用同组 `REGION`、`BUCKET`、
`ACCESS_KEY_ID(_FILE)`／`SECRET_ACCESS_KEY(_FILE)` 的独立只读身份。该身份只需归档桶
的 LIST／GET，不共享维护写入凭据。显式选择后的配置或访问失败不回退通用身份，
只关闭该次读取自行创建的客户端。

## 权限与采用

先应用当前代码的完整迁移，再安装 `module-data-grants.sql` 的 Audit NOLOGIN owner。
分区树与序列由 Audit owner 持有，创建未来固定月分区所需的 schema CREATE 和并发刷新
所需的 TEMPORARY 留在该 owner。普通 API／worker 通过固定能力调用，默认授权入口
撤销 Audit 私表及列写入、序列写入和分区所有权。Learning 暂缓消费者仍需要父表
SELECT，因此不能据此宣称全部私表读取已退出。

维护命令还承接原 E2E seed 路由的固定筛选夹具清理。该操作只允许 `_test`／`_e2e`
数据库和调用方已开启的不可变性维护标记，只匹配原 seed 路径与共享标记或精确 namespace。
它沿用调用方事务、回滚与错误传播，不提供普通审计历史删除能力，也不增加 CLI 子命令。

独立维护 LOGIN 使用 `infra/docker/audit-maintenance-grants.sql`，仅开放一个函数。
原追加、查询、读取专用授权继续保留。升级与回退须匹配代码和 owner 授权配置；不通过
修改 Alembic revision 伪装迁移完成。维护 LOGIN、快照锁与回滚和夹具范围已通过定向验证。
独立维护 CLI 的 ARM64 镜像、分离归档凭据、上传后进程丢失与回滚、孤儿对象接管及
PostgreSQL 完整恢复已通过本地组合验收，原 ASGI 管理员读取与普通用户拒绝也通过。
归档桶在数据库恢复期间保留；对象存储灾难恢复、冷 API 镜像与整个 Audit 模块资格未包含。
源码集成、主工作树采用、数据库升级及日常调度切换分别记录在
[模块台账](/dev/concepts/module-deployment#剩余工作台账)。
