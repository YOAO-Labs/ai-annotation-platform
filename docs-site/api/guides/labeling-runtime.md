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

视频人工控制、导入／转换、多相机、Mask 质检／修复、点云／视频质检和无来源任务
维护已接入固定 Native 能力；通用读取使用八项公开关系与三项行锁。普通 API／worker
继续持有原业务事务与提交权，专用账号 profile 和验收选择器见 `DEV.md`。
章节读取经过 Assets Media 的五列公开投影，质检账号只得到命令执行权。
视频 worker 中间状态及 AI 路由溯源的 Inference 读取已接入固定能力；其余 API／看板、
查询聚合和共享工作流读取仍在迁移。这些源码边界不代表完整模块独立运行
或备份恢复已完成，详细状态见[唯一台账](/dev/concepts/module-deployment)。

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

## 人工标注与关联写入

人工标注创建、字段更新、软删除及创建回执使用
`aap-labeling-annotation-writes/v1`，由
`aap_labeling_v1.annotation_write_command(jsonb)` 承接。
关联的轨迹身份／存在区间、类名与相机成员版本使用
`aap-labeling-scene-track-domain/v1` 和 `scene_track_domain_command(jsonb)`；
任务标注数量、已标注标记及 pending／in_progress 翻转使用
`aap-labeling-annotation-statistics/v1` 和 `annotation_statistics_command(jsonb)`。
三个函数位于同一 `aap_labeling_v1` schema，请求与响应 schema 随包分发。

Annotation 业务拒绝使用 PostgreSQL `P0001`，消息为
`aap_annotation_write_error`，`DETAIL` 为含 `status_code` 与 `detail` 的 JSON；
调用方直接读取驱动异常字段，不解析格式化错误字符串。SceneTrack 拒绝保留原原因与消息，
版本及目标变化继续按原公共 API 状态映射。

它们继续使用调用方同一连接与事务，保留版本冲突、轨迹缺席／退役、相机及子标注级联
规则。API 保留当前账号／Workspace 权限、来源／Mask 预检、审计、心跳及最终提交；
Batch 自动流转也继续按原顺序组合。数据命令不是权限凭据，不能把来源快照当作授权。

Mask 修订触发器及快照函数保留原调用者权限和版本／快照规则，内部引用限定为公开 schema，
以适配 owner 的固定搜索路径。配套升级／回滚 SQL 同时进入 Labeling 工件和冻结迁移。
应用匹配迁移后重施 owner 及 LAN／worktree 授权；独立可信调用账号使用
`infra/docker/labeling-annotation-writes-grants.sql`，仅得到这三个函数，不得到原表／序列、
角色成员、CREATE、TEMP 或其他模块能力。原任务控制账号仍只有其两项函数。

本组不替代切片／Mask、其余导入及视频人工候选的后续迁移。
源码、消费者和集中验收分别记录在[唯一台账](/dev/concepts/module-deployment#剩余工作台账)。

## 批量编辑、视频框与批次控制 {#bulk-video-and-batch-control}

批量标注使用 `aap-labeling-annotation-bulk/v1` 和
`aap_labeling_v1.annotation_bulk_command(jsonb)`，保留完整轨迹改类检查、相机成员
更新、属性来源及版本规则；项目配置复用 Workspace 的公开读取能力。

视频框转换与组合使用 `aap-labeling-video-bbox-editing/v1` 和
`video_bbox_editing_command(jsonb)`，覆盖轨迹转框、框聚合、切分、合并及连接。
当前 Task／来源围栏与原有几何规则保留，API 按原顺序记录一次贡献者并更新一次统计。

批次创建、维护、分派、状态、管理锁、计数与重置使用
`aap-labeling-batch-control/v1` 和 `batch_control_command(jsonb)`。
当前账号与项目管理权、分派预览版本和业务协调继续由原调用方持有。清理预测时，停用
AI 标注与解除父预测引用保持两次更新，以保留 Mask 版本和历史；清理统计仍包含相机
成员，不能替换成人工标注统计的口径。预测行删除本身仍是单独的迁移边界。

这些命令借用同一业务连接与事务，不新增产品环境变量或独立提交点。迁移与 owner／
runtime 授权必须匹配；专用可信账号分别由 `infra/docker/labeling-editing-grants.sql`
开放两项编辑函数，以及 `infra/docker/labeling-batch-control-grants.sql` 开放一项批次
函数。原任务控制与人工写入账号的执行范围保持。命令不能代替用户授权。

新增原生回归已接入自有数据库验收器；源码基础检查不等同于真实角色、并发、回滚、冷
装配或恢复通过。完整模块的剩余范围与验收状态由唯一台账记录。

## 预测、场景轨迹与跨帧写入 {#prediction-tracks-and-cross-frame}

`prediction_record_command(jsonb)` 持有预测与 Meta 写入、任务预测计数及更新时间、失败
记录、忽略／恢复、候选驳回、任务／批次清理和按来源清除导入预测。导入清除不删除失败
记录，普通清理仍先删 Meta 再删预测／失败行。`annotation_prediction_command(jsonb)`
持有采纳标注、二次推理属性与子标注来源写入；原 Mask 预检、当前写入权限、贡献者、
输入引用、心跳、审计和提交顺序保留。子标注创建与来源更新保持两次写入，保留原 Mask
历史行为。

`scene_track_command(jsonb)` 提供锁定、重放、执行、查看、历史和撤销，写入轨迹／区间／
成员及操作快照。Task、轨迹修订及成员状态会在同事务内重验；帧归属复用 Assets 公开
投影，保留主素材优先及 primary-LiDAR 回退。预览、详情和其他领域读取仍由原 API
适配器处理。

`cross_frame_write_command(jsonb)` 持有传播与插值写入，目标帧继续逐一检查当前可见性、
可编辑性及锁。运动补偿使用既有几何规则与 Assets 公开位姿能力。四项命令都借用原连接
和事务，不新增产品部署方式或提交点；命令载荷不是授权凭据。

专用可信账号分别使用 `infra/docker/labeling-prediction-grants.sql`（两个预测函数）、
`infra/docker/labeling-scene-track-commands-grants.sql` 与
`infra/docker/labeling-cross-frame-grants.sql`（各一个函数）。既有账号不扩大执行权，
原生回归使用验收器创建的独占数据库及角色。生产入口之外的旧测试夹具清理、其他
Labeling 内容写入与读取仍待迁移，完整模块原生／冷装配／恢复资格继续按唯一台账验收。

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

## 草稿与配置孤儿清理 {#annotation-maintenance}

`aap-labeling-annotation-maintenance/v1` 的固定 `annotation_maintenance_command(jsonb)`
持有现有草稿读写、清理候选／任务读取及最终孤儿清理。清理先固定候选身份，再按批次、
任务、标注排序取得 NOWAIT 锁并重新校验，忙锁及目标改向仍返回 409。系统属性、来源、
几何和既有 Mask 修订规则保留；当前管理权／配置锁、贡献证据、统计、审计及提交仍由原工作流组合。

草稿继续是未接入生产入口的内部服务，本次不新增产品入口。可信调用账号使用
`infra/docker/labeling-annotation-maintenance-grants.sql`，仅获得这一函数的执行权。
本组源码实现不替代整个 Labeling 的原生权限、并发、独立运行及恢复验收。

## 讨论、Mask 编辑及场景读取 {#discussion-mask-and-scene}

讨论／反馈与任务事件分别由两个固定命令持有，公开评论、反馈和事件投影供原 SQL 组合读取。
调用方继续执行原 Identity／Workspace 权限检查及通知、审计和提交顺序。专用 discussion
账号只有这两个命令及四个只读投影（含既有统一对账 view）的权限，不获得底表写入权。

切片／恢复、Mask 最终编辑、operation／lineage、revision 保留及 AI Mask 采纳记录使用三个
固定命令。内容验证、像素运算与原 Task／来源围栏保持；内部 revision 保留 helper 显式传入
Task UUID，未增加产品参数。场景读取使用 `scene_read_query`，严格核对请求字段和返回范围；
其三个标量 JSON helper 仅由 owner 使用，场景来源仍读 Assets 公开空间投影。

三组可信账号分别使用 `infra/docker/labeling-discussion-grants.sql`、
`labeling-mask-edit-grants.sql`、`labeling-scene-read-grants.sql`。源码及调用接线已集成，
原生权限、并发、冷安装及恢复仍按完整 Labeling 模块集中验收。
