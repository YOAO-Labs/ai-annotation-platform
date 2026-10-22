---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-22
---

# Workspace 私有运行与事务能力

项目成员创建、职责预览／变更与移除由 Workspace membership owner 组合。现有公开 API 路径、DTO、权限入口、错误映射和 SDK 不变；本页描述 API 到领域 owner 的内部边界，不是新的用户 API。

## 所有权与事务

可移植包 `aap-workspace` 使用封闭协议 `aap-workspace-membership/v1`，只承载 `add_member`、`preview_role_change`、`change_role` 和 `remove_member`。Portable manager 保留现有 Python policy，包括成员职责兼容、blocker 顺序与 preview token。Preview digest 沿用原 Python 快照算法 `sha256(json.dumps(snapshot, sort_keys=True, default=str).encode())`；字段、ASCII 转义、默认空格、`None` 与列表语义都属于兼容行为。

Labeling 只按项目返回成员职责变更所需的窄工作事实，并提供对应的 lock、handoff 与 release 能力；Workspace 不读取 Labeling 原表。历史 contributor evidence 保留原始 JSON：`NULL` 表示未知，空数组表示已知为空，不能相互替换；原 `assigned_user_ids` 列表也不能先去重再交给既有 policy。Reviewer blocker 仍按所有未完成的 effective-reviewer tasks 计算，不缩窄成只看当前 review phase 的任务。

一次命令在同一个 PostgreSQL 连接和事务中依次执行 `prepare_membership_command(jsonb)`、portable policy 和必要的 `apply_membership_command(jsonb)`。Preview 不提交；mutations 在 prepare/apply 间保留目标账号、Project、membership 和 Labeling 的现有锁序与 NOWAIT 语义。Apply 在同一事务完成所需的 Labeling 职责交接、Workspace membership 写入、固定 Audit 与成功 receipt。默认 caller Session 装配沿用旧服务的成功提交边界；业务拒绝不会额外提交，需 rollback 的失败仍回滚调用方事务。专用 DSN／HTTP owner 持有自己的事务。邀请接受继续使用既有 Identity invitation owner，不增加第二个 membership writer。

SQL 传输对 `change_role` 的 `preview_token` 和 `reason` 使用 `json-string-ascii/v1`：字段值承载原字符串的 ASCII JSON 字符串字面量，HTTP 仍传原始逻辑字符串。Native policy 必须声明相同的 `opaque_text_codec`，每次执行都在同一事务内核对；进程与迁移需要一起更新。请求指纹绑定编码后的原值，便携策略仍比较原 token；reason 只在 Audit detail 处转换为 JSONB。这样，含 NUL 的过期版本／快照仍先返回 409，有效请求的 NUL reason 则保留后续审计写入的原生错误和原子回滚。

命令只返回契约值，不返回 ORM `User` 或凭据。所需用户显示字段仍由 API 在 owner 命令完成后按现有路径组合。跨 owner 的 Project/Identity/Labeling/Audit 权限使用窄 Native 函数；Workspace 不获得这些 owner 的原始表权限。Labeling 的交接、职责锁定与任务锁释放 helper 只授权给 Workspace owner。专用 `workspace_membership_client` LOGIN 仅能执行 `membership_policy()`、`prepare_membership_command(jsonb)` 和 `apply_membership_command(jsonb)`，不获得 AUTH、credential FENCE 或 workflow append 权限。

成功 receipt 冻结原响应并绑定 operation UUID、actor、project、operation、请求与 audit context。同一 UUID 的匹配重放返回冻结成功结果、不重复写入或 Audit；绑定不同的请求返回冲突。重放会重新检查当前 actor 和 Project 管理权，但不要求已删除的目标成员外键仍存在。公开调用仍生成新 operation UUID，现有 version CAS 不变；没有新增公开幂等键，也不会把过期 preview 当成成功。

## 私有运行配置

API 默认使用本地 caller Session；可选 `WORKSPACE_MEMBERSHIP_DATABASE_URL` 或 `_FILE` 绑定本地专用数据 caller，也可通过 `WORKSPACE_MEMBERSHIP_URL` 和独立 control bearer 调用私有 runtime。API timeout 配置 `WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS` 默认为 30 秒。直接 DSN 与 `_FILE` 同时设置时视为配置错误；显式选择的远程服务不可用时返回 unavailable，不回退到 ORM 或另一条 owner 路径。

私有 HTTP endpoint 为 `POST /v1/workspace/membership/execute`。内部 Bearer 是控制凭据，不是最终用户身份；当前操作人由已认证的 API 绑定到请求。业务失败使用 HTTP 200 返回版本化失败封套，保留原 `status_code/detail/headers` 供 public API 映射；畸形请求为 400，缺少或错误的内部 Bearer 为 401，未配置能力、连接故障或超时为 503。原生数据错误保留 500 路径，专用 SQL caller 保留原生 SQLSTATE。`GET /health` 只报告存活；`GET /ready` 检查精确的 Native policy、文本 codec 与函数权限。

成员能力要求 `WORKSPACE_MEMBERSHIP_DATABASE_URL` 或 `_FILE`；runtime 还要求共享的 `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN` 或 `_FILE`。运行默认值为 `WORKSPACE_HOST=0.0.0.0`、`WORKSPACE_PORT=8172`、`WORKSPACE_TIMEOUT_SECONDS=30`、`WORKSPACE_MAX_CONCURRENCY=8`。可选 Compose 使用宿主机 `WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE` 与 `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` 选择 secret 文件，再映射为 runtime 内部变量。API runtime URL、API data DSN 与 standalone runtime DSN 是三种明确的绑定方式；空 selector 不会阻止现有 API 启动，API 的默认 caller Session 仍可用。

## 当前范围

成员命令切片已通过独立安装包、受限 Native 权限、进程／镜像、真实 API、并发交接、重启重放和备份恢复的本地限定验收；计数与边界见 [ADR-0133](/dev/adr/0133-workspace-membership-lifecycle-owner)。命令 owner 只持有三项 RPC 权限，API 验收仍使用过渡期 `legacy_runtime` 装配。

配置、项目查询和成员目录的当前接入见本文后续说明。其余 Workspace consumers 继续迁移。完整 Workspace、其余模块和十模块／七门槛目标仍未完成；Identity 的本地私网资格见[模块台账](/dev/concepts/module-deployment#identity-整模块独立装配)。完整模块的另一语言提供方、生产容量与 primary cutover 也未获资格。Go 月报保持多语言架构实验，不构成该 owner 的语言提供方。

## 项目负责人转移

负责人转移使用封闭的 `aap-workspace-owner-transfer/v1`，请求仅含 schema、operation UUID、actor UUID、project UUID 与 new-owner UUID。私有入口为 `POST /v1/workspace/owner-transfer/execute`。原公开转移路由、super-admin 准入、ProjectOut 与 SDK 不变。

Native 先按 UUID 顺序锁当前操作人与接收账号，再取 Project 排他 NOWAIT 锁；目标账号启用检查在 Project 之前，目标 `project_admin` 角色检查在 Project 之后。只修改 owner；同一 owner 不执行 UPDATE，保留 updated_at。不额外写业务 Audit 或 membership。默认 API 数据适配器始终借用调用方事务，成功不提交；账号锁异常回滚，项目锁冲突保留原失败事务状态。专用 DSN／HTTP owner 提交或回滚自己的事务。

受限 `workspace_owner_transfer_client` 仅能执行 `owner_transfer_policy()` 和 `transfer_project_owner(jsonb)`。成功回执与 owner 更新原子提交；匹配重放返回原成功内容，即使之后再次转移也不重做旧写入。每次重放仍检查当前 active super-admin 与 Project 存在并锁定，不要求旧接收者仍存在或仍有原角色。operation UUID 与 actor／project／target 不匹配返回 409。公开调用不增加幂等头；API 仅在传输失败时，用同一私有 UUID 最多重试一次。

API 可选 `WORKSPACE_OWNER_TRANSFER_URL`，或二选一的 `WORKSPACE_OWNER_TRANSFER_DATABASE_URL(_FILE)`；未配置时使用 caller Session，显式失败不回退。共享成员命令的 control token 与 timeout。独立 runtime 支持只启用成员、只启用转移或同时启用，`/ready` 检查每个已配置能力和受限权限。可选 `docker-compose.workspace-owner-transfer.yml` 使用宿主端口 8173，通过 `WORKSPACE_RUNTIME_OWNER_TRANSFER_DATABASE_URL_FILE` 与 `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` 挂载 secret。

Node conformance provider 以标准 PostgreSQL 协议调用相同的两项函数，不代理 Python/API；其业务规则仍由 Native 执行。该提供方仅服务于本地单命令替换实验。完整 Workspace、独立数据库、生产容量与其他模块的剩余门槛继续推进。

负责人转移切片的 66 个去重选定案例已完成本地限定验收，包含四条绑定路径与旧响应对照、受限数据库权限、事务错误、丢响应恢复、关联备份恢复和 Python／Node 镜像重启。独立 Node 提供方只覆盖这个命令的传输与事务，完整 Workspace 的语言替换仍待后续工作。验证范围见 [ADR-0134](/dev/adr/0134-workspace-project-owner-transfer)。

## 配置命令实现与验收状态

项目创建／更新及模板创建／更新／删除／复制已有封闭私有协议
`aap-workspace-configuration/v1`，入口为
`POST /v1/workspace/configuration/execute`。受限
`workspace_configuration_client` 只有 `configuration_policy()` 和
`configuration_step(jsonb,text,jsonb,text[],text[])` 两项能力。配置独立运行使用
`WORKSPACE_CONFIGURATION_DATABASE_URL(_FILE)`，复用 control token；本地 Compose
文件为 `docker-compose.workspace-configuration.yml`，端口8174。

上述六种配置命令保存不可变成功回执，在同一事务中执行分阶段写入和当前权限围栏。
默认 API 数据端口借用调用方事务；独立连接只持有自己的业务事务，不提交 API
连接上的无关 pending work。后端探测超时及视频参数需要与 API 保持一致，具体变量见
[环境变量参考](/dev/reference/env-vars)。

公开配置路由已接入同一个配置 owner，默认借用调用方事务，HTTP 保留提交与响应。
专用 DSN／HTTP 通过 `WORKSPACE_CONFIGURATION_DATABASE_URL(_FILE)`／
`WORKSPACE_CONFIGURATION_URL` 选择，不能同时配置；显式失败不回退。
三条绑定路径各已对照106项原公开响应；独立进程的丢响应重试、重启重放及不可用不回退也已通过本地检查。失败事务、备份恢复、9项关联恢复及0257完整迁移验证均已通过。独立安装包、受限镜像与本地并发执行也已完成限定验收；项目与模板读取的接入状态见后文。这一步不代表完整
Workspace 已独立部署。状态与事务取舍见
[ADR-0136](/dev/adr/0136-workspace-configuration-command-owner)。

### 类别重命名

`rename_project_class` 复用同一个配置端点、数据账号和进程。公开入口仍先检查
项目管理权，再校验非空名称；账户、项目和成员权限围栏保持原顺序。
实际改名在同一事务内先调用 Labeling 的窄标注类名能力，再写项目绑定。
公开响应继续在提交后读取项目投影；此时查询失败不会撤销已经提交的改名。

同名请求不写业务数据或操作回执，也不检查类别和工具单位是否存在。
借用事务时，API 在调用方仍持锁时读取投影。独立连接／HTTP 提供方在自己的
事务内完成投影，随后回滚这个无写入事务，并把绑定的投影结果交回 API。
独立提供方因此需要 Project Query 与 Identity Briefs 能力；缺失时返回不可用。
改名没有持久成功收据，传输失败不会自动重发；原有六种配置命令的重试保持不变。

独立 Configuration 进程可在原 Compose 文件上叠加
`docker-compose.workspace-class-rename.yml`，为同一服务补充项目查询与身份资料的
两份只读 secret。四份 secret 分别由
`WORKSPACE_RUNTIME_CONFIGURATION_DATABASE_URL_FILE`、
`WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`、
`WORKSPACE_RUNTIME_PROJECT_QUERY_DATABASE_URL_FILE` 和
`WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE` 指定。
不增加服务或对外端口；原六种配置操作仍可使用原来的两份 secret 部署。
此切片的三路径、独立安装包、受限镜像、重启、恢复和完整迁移已通过限定本地验收，范围见
[ADR-0143](/dev/adr/0143-workspace-project-class-rename-owner)。

## 模板列表、详情与响应投影

模板列表、详情和配置提交后的响应已由 `aap-workspace-template-query/v1` owner 提供。
API 保留参数与身份准入、写入提交和公开 DTO 校验；读取 owner 使用本次已认证的身份快照，
不增加第二次账号／角色检查。模板可见性、组织成员事实、过滤、排序及创建者显示的读取顺序保持不变。

默认绑定借用调用方事务。独立连接使用 `WORKSPACE_TEMPLATE_QUERY_DATABASE_URL(_FILE)`，
HTTP 使用 `WORKSPACE_TEMPLATE_QUERY_URL`，二者互斥，显式失败不回退；远程 timeout
由 `WORKSPACE_TEMPLATE_QUERY_TIMEOUT_SECONDS` 控制。私有服务端点为
`POST /v1/workspace/template-queries/execute`，复用 Workspace control bearer。
独立服务可只配置模板读取能力，专用 LOGIN 只有 policy／step 两项函数权限；
本地 Compose 使用8175和两份只读 secret 文件。

提交后投影绑定成功配置命令的私有回执，不增加公开参数，也不重新校验模板可见性。
此时查询或公开响应校验失败，不能回滚已经提交的配置；JSONB-null 的旧500行为保留。
原模板 ORM 读适配器已删除；命名流水线通过 Identity 窄能力读取组织范围，其他命名流水线写入仍由原入口持有。

三条路径各已对照40项实际旧 HTTP 观测，默认绑定另核对调用方自动刷新和时区；
源码、受限数据账号与实际进程验证通过。独立安装包、受限 owner/API 镜像、
真实容器响应投影、故障恢复、备份恢复与0259完整迁移均已完成限定本地验收；
16次详情读取在并发4下全部成功，无 OOM，该本地样本不代表生产容量。
当前范围和信任边界见 [ADR-0137](/dev/adr/0137-workspace-template-query-owner)。

## 项目查询

项目列表、分页、详情、统计、就绪度、访问上下文、接受操作后的投影和项目搜索使用
`aap-workspace-project-query/v1`。API 保留原公开 DTO；默认借用调用方事务，
可用 `WORKSPACE_PROJECT_QUERY_DATABASE_URL(_FILE)` 或 `WORKSPACE_PROJECT_QUERY_URL`
选择独立 owner。两种选择互斥，显式失败不回退。
私有入口是 `POST /v1/workspace/project-queries/execute`。

详情保留完整项目读取、当前账号、刷新项目、成员职责的原顺序；成员两个时间戳的
有限值超出 Python 范围时同样保留原内部错误，真实无穷值仍按原驱动处理。
非空响应的显示资料使用显式 `WORKSPACE_PROJECT_QUERY_IDENTITY_*` 配置；
空页、统计和就绪度不访问 Briefs。限定本地验收与剩余边界见
[ADR-0139](/dev/adr/0139-workspace-project-query-owner)。

访问上下文的 `get_project_access` 复用同一端点和凭据，返回原有成员 ID、版本与能力字段。
第一轮保留初始项目、当前账号、刷新项目和成员读取，第二轮从当前账号开始再查项目与成员；
最终职责和版本来自同一次成员读取。该操作不需要 Briefs，不增加权限锁或更强事务快照。
原本地锁定解析器的 I/O 和锁序保留，共享纯权限策略。三路径响应、受限镜像、重启、
恢复和完整迁移已通过限定本地验收；完整 Workspace 与生产容量仍未完成，证据见
[ADR-0141](/dev/adr/0141-workspace-project-access-query)。

供类别／属性统计使用的 `require_project_manager` 复用同一 Project Query 配置，
只执行原来一轮当前权限检查，成功仅返回目标项目 ID。可见的普通成员在完整成员行解码后
返回原管理权限错误，不使用 `/access` 的第二轮读取，也不访问显示资料。
API 随后调用独立 Labeling 统计边界，配置见 [Labeling 运行协议](labeling-runtime)。

## 项目成员与提及候选

`GET /api/v1/projects/{project_id}/members` 与 `/mention-candidates` 使用
`aap-workspace-directory-query/v1`。成员先按 `assigned_at DESC` 读取完整成员记录，
再批量查询全局 UserBrief；空集不查资料，缺失资料继续省略。
提及候选按负责人、启用超管、成员三组分别读取，并按 UUID 保留第一个种类。
成员组不额外过滤停用账号，不能把三组统一改成仅启用账号。

默认绑定保留 caller autoflush／no*autoflush，不提交或回滚；专用连接与 HTTP 使用
自己的只读事务，不能读取其他连接未提交的修改。API 可选
`WORKSPACE_DIRECTORY_QUERY_DATABASE_URL(_FILE)` 或 `WORKSPACE_DIRECTORY_QUERY_URL`，
两者互斥，独立调用使用 `WORKSPACE_DIRECTORY_QUERY_TIMEOUT_SECONDS`，失败不回退。
私有 `POST /v1/workspace/directory-queries/execute` 复用 Workspace control bearer。
非空成员列表复用 `WORKSPACE_PROJECT_QUERY_IDENTITY*\*` Briefs 配置；提及候选不依赖 Briefs。

独立进程可只启用目录能力。`workspace_directory_query_client` 只有 policy 与 step
两项函数权限；Identity 通过窄账号函数提供提及资料，不向 Workspace 授予 User 原表。
可选 Compose 文件为 `docker-compose.workspace-directory-query.yml`，本机端口8177，
三份只读 secret 的宿主选择器为 `WORKSPACE_RUNTIME_DIRECTORY_QUERY_DATABASE_URL_FILE`、
`WORKSPACE_RUNTIME_PROJECT_QUERY_BRIEFS_DATABASE_URL_FILE` 和 `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE`。
协议与验证范围见 [ADR-0140](/dev/adr/0140-workspace-directory-query-owner)。

此目录切片已通过三路径公开响应对照、独立安装包、受限 owner/API 镜像、故障与重启、
备份恢复及完整迁移的限定本地验收。1 CPU／256 MiB 下的16次并发读取不代表生产容量；
API 与数据库不在该容器预算内。完整模块当前状态见[模块台账](/dev/concepts/module-deployment#剩余工作台账)。

## 同事务运维导入与质量配置

`aap-workspace-project-maintenance/v1` 是固定 SQL 数据合同，复用 `aap_workspace_v1`，
没有新 HTTP endpoint 或 runtime DSN selector：

| 能力                                               | 返回值                                                |
| -------------------------------------------------- | ----------------------------------------------------- |
| `read_import_project(text)`                        | `schema/display_id/project_id`；未找到时 UUID 为 null |
| `create_import_project(uuid,text,text,uuid,jsonb)` | 同上；UUID 必须与调用方指定值一致                     |
| `write_point_cloud_quality_config(uuid,jsonb)`     | `schema/project_id/updated_at`；时间必须带时区        |

导入按稳定 display ID 精确复用项目，不增加 owner／类型判断。创建保持旧 ORM 默认值，
不分配普通项目序号，不执行 upsert 或冲突重试；长度、外键、唯一错误保留原 SQLSTATE。
查找保留 caller autoflush／no_autoflush；创建仍显式 flush 全部 pending work。后续素材
关联、建题、标定和计数保留同一外层事务。导入标识来自受信任运维入口，不是用户授权。

质量晋升继续由原入口重验项目管理权，在既有 Project `FOR UPDATE` 下校验评估 gate、
baseline 与候选修订。Native setter 只写精确配置与事务时间戳，相同 JSONB 不推进时间；
调用方同步 Project 的配置／updated_at，不追加 ORM 写入。评估和 Audit 与配置一起提交
或回滚，错绑定回复不能确认整个工作流。能力不增加 CAS、授权检查、提交或回滚。

预建的 `workspace_project_maintenance_client` 通过
`infra/docker/workspace-project-maintenance-grants.sql` 获取三项业务 owner 执行权，
没有私表／列、序列、DDL 或 TEMP 权限；PostgreSQL/public invoker helpers 不属于这项
业务能力计数。迁移、owner grants 与精确授权入口见 `DEV.md`。严格回复及 Native／Node／
真实消费者的限定本地证据见 [ADR-0164](/dev/adr/0164-workspace-transactional-project-maintenance)。

## 锁定权限与会话快照

API 写入权限入口保留原元组接口，通过现有
`aap_workspace_v1.project_access_step(uuid,uuid,text)` 获取 Project KEY SHARE 和
成员 SHARE 快照。Identity 的完整账号 SHARE facts、NOWAIT 错误、manager 跳过成员
及当前权限策略不变。它们属于调用方同一事务，不是可跨事务重放的授权 token。

完整 Project／成员数据通过既有编解码恢复到原 Session，保留对象身份、配置／状态
刷新及无 autoflush 行为；不在锁后再次读取 Project，不新开连接。调用方仍提交或回滚
整个业务。应用授权脚本和本地 runtime allowlist 包含已有 Native 函数，无新 DSN、
服务或客户端账号；无锁读取见下一节。

真实 API LOGIN 在撤销私表 SELECT 后仍可读取权限并保持锁；Node 直接消费同一组
能力。共享编解码与实际标注／训练等定向结果见
[ADR-0166](/dev/adr/0166-workspace-locked-authority-snapshots)。完整模块凭据与冷工件
仍待集中验收，本项局部资格不替代完整独立部署。

## 无锁权限与调用方快照

当前权限 helper 使用同一请求 Session 读取 owner facts：by-id 先用
`project_access_step` 的 `read_project_row` 获取完整 Project，再检查当前账号，随后
复用 Project Query 的 `get_row` 和非 manager 的 `access_member_rows`。初始缺失先
返回 404；直接 resolve 仍先查账号。Query 请求只使用真实 admitted actor role。

完整 Project／成员编解码与缓存刷新保持原对象身份，保留原 autoflush 和外层
`no_autoflush`，不增加行锁或独立连接。调用方继续持有整个事务；锁定写入入口及公开
GET access 的第二轮权威检查保持。应用授权清单补齐既有 Project Query schema、
policy 和 step，不新增角色或服务。

真实受限 API LOGIN、READ ONLY、Node、时序和缓存验收见
[ADR-0167](/dev/adr/0167-workspace-unlocked-authority-snapshots)。本项局部资格仍不
替代完整 Workspace 装配与恢复验收。

## 独占 Project 快照

清理孤儿标注和点云评估晋升通过 `aap-workspace-project-lock/v1` 借用同一请求事务，
分别保留 NOWAIT 与等待锁。原管理权检查、清理的缓存刷新／无 autoflush、晋升的
普通缓存／autoflush，以及 evaluation、Audit 和提交顺序不变。锁在调用方结束时释放。

Native `lock_project_row(uuid,boolean)` 不承接权限策略；只返回严格绑定的完整行或
缺失。等待后在持锁期间生成新快照，避免计数来自等待前的状态。应用授权清单加入
此单一函数，没有新增角色、服务、连接或 HTTP 提交边界。

受限真实 API、Node、锁、缓存和隔离级别资格见
[ADR-0168](/dev/adr/0168-workspace-exclusive-project-snapshots)。完整模块装配仍见
[唯一台账](/dev/concepts/module-deployment#剩余工作台账)。

## Task／Training 业务快照

选定 Task access、最终阶段写检查和 Training authority／study／claim 通过现有无锁
Project 事实读取配置。调用方的缓存命中、autoflush、强制刷新与过期删除处理保留；
实际 actor 来自请求或保存的 creator_id，owner 事实不代替业务权限判断。

训练的前置引用锁仍先持有账号 SHARE，再持有 Project KEY SHARE。原等待函数和
新增 NOWAIT companion 只返回 UUID／null，不额外解码配置日期，不新开连接或提交。
应用授权、实际受限 API 和 Node 证据见
[ADR-0169](/dev/adr/0169-workspace-task-training-project-snapshots)。其余消费者和
完整模块资格仍按[唯一台账](/dev/concepts/module-deployment#剩余工作台账)推进。

## 项目删除的调用方事务

项目删除使用已有 `aap-project-delete/v1` 和
`aap_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)`，不提供独立提交的 HTTP 命令。
命令只有 `operation_id/actor_id/project_id/audit_context` 四字段；Audit context 为 null
时绑定 SQL NULL。回复保留 schema、严格 ok、成功回执或稳定业务错误。API 的兼容导入
仍可用，实际验证和 UUID 绑定由 Workspace 包持有。

调用方须保持同一外层事务：API 先 flush 原 pending work，Native 执行当前权威、保留
检查、各 owner 清理、Audit 和回执，原路由最后提交。连接式
`ProjectDeletionPostgresData(connection)` 要求已开启的 asyncpg 事务，并在同一事务中
检查 `workspace_project_deletion_client` 及唯一协调器权限；不会新建或结束事务。
提交失败必须由调用方处理，不把事务内回执当成已完成提交。

公开删除仍要求当前项目管理权，成功为空体 204，后续删除不存在项目返回 404；没有新增
幂等头。持有原 operation ID 的内部调用方可重放冻结回执，仍受 Native 当前 active-actor
检查约束。受限 Node、Python 和实际公开 API 定向检查通过；未退役视频执行的删除规则
与完整资格仍见[唯一模块台账](/dev/concepts/module-deployment#剩余工作台账)。
