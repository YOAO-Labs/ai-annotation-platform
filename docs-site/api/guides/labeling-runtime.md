---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# Labeling 类别与属性统计运行协议

公开 `GET /api/v1/projects/{project_id}/class-usage` 保持原路径、响应与管理权限。
API 先由 Workspace 核验当前项目管理权，再调用 Labeling 读取统计。
这里只描述内部提供方配置；完整 Labeling 模块仍在迁移。

## 读取与事务

类别统计与属性读取保持为两条顺序 SQL，均只按标注的项目 ID 和启用状态筛选。
仍启用的已取消标注、AI 标注及未配置的工具类别参与统计；不会改按任务所属项目筛选。
空类别名省略，属性只统计完整 JSON 解码后对象的顶层键。两条语句之间提交的新属性可被
第二条读到；属性读取或解码失败时整个请求失败，不返回部分统计。

默认借用 API 调用方 Session，保留待提交数据、autoflush／no_autoflush 和提交权。
独立 DSN／HTTP 提供方使用只读连接，不共享调用方的未提交数据；远程 Workspace 与
Labeling 也没有共同快照。授权只执行原来的一轮当前权限读取，不增加业务锁或缓存。

## 提供方配置

| 配置                                        | 用途                           |
| ------------------------------------------- | ------------------------------ |
| `LABELING_CLASS_QUERY_URL`                  | 私有 HTTP owner 地址           |
| `LABELING_CLASS_QUERY_DATABASE_URL(_FILE)`  | 专用三函数 DSN，与 URL 二选一  |
| `LABELING_CLASS_QUERY_CONTROL_TOKEN(_FILE)` | HTTP owner 独立控制凭据        |
| `LABELING_CLASS_QUERY_TIMEOUT_SECONDS`      | 显式独立调用的超时，默认 30 秒 |

全部留空时使用调用方事务。显式文件不可读或为空、连接失败、凭据失效及冲突配置均失败，
不会切回本地读取。提供方内部解码失败继续走原有 HTTP 500；服务不可用返回 503。

独立进程使用相同 DSN／control 配置，可通过 `python -m aap_labeling` 启动。
其 `HOST`、`PORT`、`MAX_CONCURRENCY` 也使用 `LABELING_CLASS_QUERY_` 前缀，
默认分别为 `0.0.0.0`、`8178`、`8`。它不需要 Identity Briefs、Workspace 连接、
对象存储或 Celery。公开账号凭据只进入 API，不发送给这个私有统计进程。

私有入口是 `POST /v1/labeling/class-queries/execute`，使用封闭的
`aap-labeling-class-query/v1` 请求与绑定回包。调用者是可信 API peer，必须先完成项目授权。
`data_json` 只含 `classes`、`attributes` 两个精确整数映射，键顺序和空白不属于合同。

## 数据权限与本地部署

先应用包含两个 owner 扩展的当前迁移，再应用 `infra/docker/module-data-grants.sql`
和 `infra/docker/labeling-class-query-grants.sql`。`labeling_class_query_client`
只得到 `class_query_policy()`、`class_counts(uuid)`、`attribute_rows(uuid)` 三个函数，
没有原表、列、角色成员、CREATE 或 TEMP 权限。Workspace 管理权检查沿用现有
Project Query 配置；部署时同步更新匹配的 Workspace／Labeling 工件与 Native 函数。

独立 Compose 文件为 `docker-compose.labeling-class-query.yml`，只发布本机 8178。
宿主文件选择器是 `LABELING_RUNTIME_CLASS_QUERY_DATABASE_URL_FILE` 与
`LABELING_RUNTIME_CLASS_QUERY_CONTROL_TOKEN_FILE`；容器内以只读 secrets 提供。
使用单独的 Compose project 名运行，日常主环境不会自动切换。

三路径响应、真实语句间提交、调用方回滚、完整迁移与当前库备份恢复已通过本地检查；
受限镜像、独立容器、重启和有限并发检查也已通过。容器仅配置两个只读凭据，
使用 1 CPU／256 MiB；16 次请求、并发 4 的结果一致。数据库和 API 不在这个资源预算内，
这些结果不代表生产容量。范围、冻结样本与剩余门槛见
[ADR-0142](/dev/adr/0142-labeling-class-usage-query-owner)。
