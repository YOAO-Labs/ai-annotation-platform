---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-25
---

# Labeling 运行协议

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

## 任务编辑锁与生命周期

任务编辑锁、送审／跳过／撤回／重开、审核认领／批准／驳回和贡献者证据使用两个封闭合同：
`aap-labeling-task-locks/v1` 与 `aap-labeling-task-workflow/v1`。
对应入口是 `aap_labeling_v1.task_edit_lock_command(jsonb)` 与
`aap_labeling_v1.task_workflow_command(jsonb)`；请求／响应 schema 随 Labeling 包分发。

调用方继续核验当前 Workspace 权限，owner 核对当前账号、Task 的项目／批次、状态及
审核轮次。编辑锁保留原 advisory 命名空间、续租／接管规则和批次完整集合的有序
NOWAIT 围栏。业务命令只承接对应状态和证据写入，计数、Audit、QC 与通知仍按原顺序
组合，不新增提交点。

这两项能力使用业务调用方的同一 PostgreSQL 连接与事务。API 适配器根据返回值同步
ORM 缓存；owner 不提交／回滚，也不把未提交数据和锁转交给独立 HTTP 事务。
其他语言可以在相同调用事务中使用同一 JSONB 命令边界。

先应用当前迁移和 `infra/docker/module-data-grants.sql`，再更新 LAN／worktree 的
既有运行授权。需要独立可信调用账号时，可应用
`infra/docker/labeling-task-control-grants.sql`：只授权这两个函数，拒绝直接表／列、
序列、角色成员、CREATE、TEMP 和其他模块函数权限。公开用户仍只能经过原 API 授权。
未迁移的批次与标注内容调用保留现有权限，不能据此宣称完整 Labeling 已独立。

## 视频 worker 的最终写入

视频 worker 成功、协作取消或失败时，API runner 将最终 Job 状态、待审候选、
Mask 根和不可变回执交给 Labeling 的私有 owner，在同一业务事务内提交。
公开 REST 路径与响应不变。owner 重新检查当前执行代次和 lease，并继续执行
跟踪 actor、原始来源、Task 与 segment 的现有检查；无需把数据库行或 Session
传给远程 Labeling owner。

调用可使用借用 API Session、专用 PostgreSQL DSN 或独立 HTTP 进程三种绑定。
默认借用 Session，保留 API 的提交权；DSN 与 HTTP 显式选择独立事务或进程，
两者不能同时配置，明确选择的提供方失败时不会回退。HTTP 私有协议为
`aap-labeling-video-writer/v1`，只接受内部 control bearer；入口是
`POST /v1/labeling/video-writer/execute` 和
`POST /v1/labeling/video-writer/status`。status 支持两种封闭请求：operation ID、
执行证明与请求摘要用于核对同一写入；仅含 execution proof 的 publication
lookup 调用独立 Native read_publication action，用于查询当前 job/run 是否已有
终态回执。一次命令最多 65 MiB，最终候选仍受 64 MiB 上限
约束。原始作业输入、执行证明和回执均不对公开 API 暴露。

已封存的视频窗口是完成候选的唯一来源。owner 按冻结计划逐窗读取原始
canonical payload 字节、manifest 与 attempt，验证摘要、前驱关系和计划后才
拼接 track 候选；discovery 数据不写入候选。普通追踪取消保留已封存前缀；
如命令带有未封存尾部，只接受当前代次已开始／捕获的下一窗口范围。
普通取消必须已有数据库取消标记；纠错取消绕过 candidate stage 授权并清空纠错候选。
失败命令只结算失败，不重新读取或授权原始来源。

操作 ID 按 run 固定。网络或提交确认不明确时，调用方以原 ID、同一执行证明和
同一请求摘要查询 status 或重试，不重新运行模型。新代次取消清理先按当前 proof 查询
job/run publication；命中时原样返回既有回执，不重新暂存旧候选。未命中才从账本恢复
封存前缀，且不带 worker 本地帧行。Mask 引用在写入事务内锁定，
再由 Assets Mask Content owner 校验字节；Mask 配置缺失或临时不可用时写入不完成。
bbox-only 结果不需要 Mask Content 凭据。

独立进程从相同 `apps/labeling` 工件启动，默认监听 `127.0.0.1:8180`，最大并发
默认 8，单请求超时默认 90 秒。API 选择远程 owner 使用
`LABELING_VIDEO_WRITER_URL`、`LABELING_VIDEO_WRITER_CONTROL_TOKEN(_FILE)`；
专用数据库连接使用 `LABELING_VIDEO_WRITER_DATABASE_URL(_FILE)`；
超时使用 `LABELING_VIDEO_WRITER_TIMEOUT_SECONDS`。进程自身使用相同
`LABELING_VIDEO_WRITER_DATABASE_URL(_FILE)` 与 control token，并可设置
`LABELING_VIDEO_WRITER_HOST`、`PORT`、`MAX_CONCURRENCY`。Mask 字节读取另用
`ASSETS_MASK_CONTENT_URL` 与 `ASSETS_MASK_CONTENT_TOKEN(_FILE)`。

数据边界由迁移、`infra/docker/module-data-grants.sql` 与
`infra/docker/labeling-video-writer-grants.sql` 提供。受限
`labeling_video_writer_client` LOGIN 只有一个 Labeling 函数的执行权；
Jobs 的固定内部 capability 只授予 Labeling owner，LOGIN 不获得 Jobs 表、
Labeling 表或任意 Jobs RPC 权限。

限定本地验收已覆盖 HTTP／DSN／借用事务的 bbox 与 Mask 真实消费者、独立 HTTP
进程重启，以及进程中断后取消时的封存候选恢复与重复投递。独立 owner 容器以非 root、
只读根文件系统、1 CPU／768 MiB 运行，完成和重启后均能查询同一收据；只挂载函数 LOGIN、
control token 和按需 Mask token 文件。API 冷镜像另以禁网、只读、1 CPU／512 MiB 检查。

每个容器场景的 16 次收据读取、并发上限 4 均返回同一结果，真实锁争用按原命令重试；
该采样只覆盖小收据读取。另一组有限 bbox 写入采样对 direct-Postgres 与 HTTP 各执行
16 个独立作业，每个作业从 3 个封存窗口发布 6 条帧结果，并发上限与观测并发均为 4。
直接绑定共调用 96 次 Native 函数，HTTP 绑定发送 16 次请求；两侧均无忙重试，所有收据、
规范帧行与来源指纹一致。单作业中位数分别为 169.48／178.93 ms；这是一次本地小负载，
不含模型、队列、最大缓冲区或大 Mask，不代表生产容量，也没有与旧 writer 比较。
数据库在容器外，HTTP 调用端在宿主机，直接绑定额外启动一个容器内 Python 进程；
记录的 cgroup 当前内存为 87.08／60.30 MiB，不能作为峰值或等预算比较。

续租明确返回 `job_not_active` 时，worker 停止续租，
仍可在原租约有效期内完成物理清理；Native 拒绝该状态下的业务写入，过期或被替代的证明仍失效。

同一受控 PostgreSQL 实例中的非空备份恢复保留冻结计划、窗口原始字节、收据和权限。
副本拒绝源环境的旧执行证明。[离线视频恢复 operator](/api/guides/inference-runtime#离线视频恢复-operator)
已通过限定本地验收：同 incarnation 的 CPU 后台退休旧请求后，实际 runner 以同 run、
新代次复用封存窗口并发布结果。验收复用同一 S3 原始版本；Mask／原片对象存储备份恢复
及完整容量门槛仍需独立验证。
人工审核及中间 start/progress 仍由 API 持有，不能据此认定整个 Labeling
已独立部署或可替换语言。Go 月报服务继续作为架构实验。范围和限制见
[ADR-0159](/dev/adr/0159-independent-labeling-video-writer)。
