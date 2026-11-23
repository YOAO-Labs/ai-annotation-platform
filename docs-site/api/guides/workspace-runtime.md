---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-26
---

# Workspace 私有运行与事务能力

本页描述 API 到领域 owner 的内部调用，不是新的用户 API。公开路由、DTO、权限入口、错误映射和 SDK 不变。

## 默认部署与旧拆分安装 {#workspace-deployment}

API 在同一进程内调用六组 Workspace owner，每组都借用调用方的 `AsyncSession`。不启动 Workspace 容器，也不为这六组操作配置单独的 URL、命令／查询 DSN 或共享 HTTP bearer。普通 API 继续通过 `DATABASE_URL` 使用已有应用角色和[固定 SQL 能力](#普通消费者的-sql-凭据边界)，不获得 owner 或迁移凭据。成员和负责人转移仍受 `WORKSPACE_MEMBERSHIP_TIMEOUT_SECONDS`（默认 30 秒）约束。

| API 入口文件（`apps/api/app/services/`） | 调用的 Workspace owner  | 保留的事务边界                         |
| ---------------------------------------- | ----------------------- | -------------------------------------- |
| `workspace_membership.py`                | `MembershipManager`     | 职责交接、成员写入、Audit 和回执同事务 |
| `workspace_owner_transfer.py`            | `OwnerTransferManager`  | 当前账号／项目锁及原提交前、提交后投影 |
| `workspace_configuration.py`             | `ConfigurationManager`  | 配置、类别改名和指南操作沿用原事务     |
| `workspace_template_query.py`            | `TemplateQueryManager`  | 原 autoflush 及回执绑定的提交后投影    |
| `workspace_project_query.py`             | `ProjectQueryManager`   | 原权限读取阶段、缓存和响应投影         |
| `workspace_directory_query.py`           | `DirectoryQueryManager` | 成员／提及可见性及原调用方快照         |

跨 owner 的边界不变：配置需要后端能力时，通过 [Inference 的认证探测](/api/guides/inference-runtime#workspace-的后端能力探测)调用统一的 `inference` 凭据进程；不部署另一个探测容器。项目和目录仅在需要显示资料时调用 [Identity UserBrief](/api/guides/identity-runtime#identity-user-briefs)，沿用 API 自己的 Identity 装配，不为 Workspace 单独配置资料源。这两项依赖都不是 Workspace 自身的远程装配选项。

如果现有安装仍将 Workspace 六组操作发往独立服务或专用数据连接，**不要带着旧选择器、Compose 文件链或 secret 挂载进行滚动升级**。暂停升级，按[升级指南](/ops/upgrade-guide#workspace-split-upgrade)另行安排维护窗口。不要把旧专用 DSN、owner DSN 或迁移连接抄到 API 运行连接中。本次默认装配变更不会迁移日常环境、撤销既有账号或修改数据库授权。

版本化请求／回复、冻结 SQL、专用测试角色及独立 Python HTTP／Node 对照保留，用于可移植包一致性、错误与回执恢复验收，不是产品部署入口。退役前的[拆分拓扑验收](/dev/concepts/module-deployment#workspace-组合运行与恢复验收)和[本机采用记录](/dev/concepts/module-deployment#workspace-daily-adoption)仅供追溯，不是当前部署配方。

## 所有权与事务

可移植包 `yoao-workspace` 使用封闭协议 `aap-workspace-membership/v1`，只承载 `add_member`、`preview_role_change`、`change_role` 和 `remove_member`。Portable manager 保留现有 Python policy，包括成员职责兼容、blocker 顺序与 preview token。Preview digest 沿用原 Python 快照算法 `sha256(json.dumps(snapshot, sort_keys=True, default=str).encode())`；字段、ASCII 转义、默认空格、`None` 与列表语义都属于兼容行为。

Labeling 只按项目返回成员职责变更所需的窄工作事实，并提供对应的 lock、handoff 与 release 能力；Workspace 不读取 Labeling 原表。历史 contributor evidence 保留原始 JSON：`NULL` 表示未知，空数组表示已知为空，不能相互替换；原 `assigned_user_ids` 列表也不能先去重再交给既有 policy。Reviewer blocker 仍按所有未完成的 effective-reviewer tasks 计算，不缩窄成只看当前 review phase 的任务。

一次命令在同一个 PostgreSQL 连接和事务中依次执行 `prepare_membership_command(jsonb)`、portable policy 和必要的 `apply_membership_command(jsonb)`。Preview 不提交；mutations 在 prepare/apply 间保留目标账号、Project、membership 和 Labeling 的现有锁序与 NOWAIT 语义。Apply 在同一事务完成所需的 Labeling 职责交接、Workspace membership 写入、固定 Audit 与成功 receipt。API 沿用原成功提交边界；业务拒绝不会额外提交，需 rollback 的失败仍回滚调用方事务。邀请接受继续使用既有 Identity invitation owner，不增加第二个 membership writer。

SQL 传输对 `change_role` 的 `preview_token` 和 `reason` 使用 `json-string-ascii/v1`：字段值承载原字符串的 ASCII JSON 字符串字面量，HTTP 仍传原始逻辑字符串。Native policy 必须声明相同的 `opaque_text_codec`，每次执行都在同一事务内核对；进程与迁移需要一起更新。请求指纹绑定编码后的原值，便携策略仍比较原 token；reason 只在 Audit detail 处转换为 JSONB。这样，含 NUL 的过期版本／快照仍先返回 409，有效请求的 NUL reason 则保留后续审计写入的原生错误和原子回滚。

命令只返回契约值，不返回 ORM `User` 或凭据。所需用户显示字段仍由 API 在 owner 命令完成后按现有路径组合。跨 owner 的 Project/Identity/Labeling/Audit 权限使用窄 Native 函数；Workspace 不获得这些 owner 的原始表权限。用于独立一致性检查的 `yoao_workspace_membership_client` LOGIN 仅能执行 `yoao_workspace_membership_v1` 中的 `membership_policy()`、`prepare_membership_command(jsonb)` 和 `apply_membership_command(jsonb)`，不获得 AUTH、credential FENCE 或 workflow append 权限。

成功 receipt 冻结原响应并绑定 operation UUID、actor、project、operation、请求与 audit context。同一 UUID 的匹配重放返回冻结成功结果、不重复写入或 Audit；绑定不同的请求返回冲突。重放会重新检查当前 actor 和 Project 管理权，但不要求已删除的目标成员外键仍存在。公开调用仍生成新 operation UUID，现有 version CAS 不变；没有新增公开幂等键，也不会把过期 preview 当成成功。

来自迁移 `bo0467` 之前的数据库仍须满足原 namespace 切换门禁：核对备份、停止旧准入、将旧 `workspace_membership_client` 设为 `NOLOGIN`，并排空可调用 prepare／apply／policy 的连接。迁移拒绝未排空的旧入口、目标 namespace 冲突或未预期函数依赖，不主动停用账号或终止会话，也不提供旧 namespace 别名。wire schema、receipt、operation UUID、GUC 绑定的事务语义及历史 `0253` 工件不变。此历史 schema 门禁不是启用专用 API DSN 的步骤。

成员命令的独立安装包、受限 Native 权限、锁语义、重启重放和备份恢复证据见 [ADR-0133](/dev/adr/0133-workspace-membership-lifecycle-owner)。这些历史结果限定原候选和运行拓扑。

## 项目负责人转移

负责人转移使用封闭的 `aap-workspace-owner-transfer/v1`，请求仅含 schema、operation UUID、actor UUID、project UUID 与 new-owner UUID。原公开转移路由、super-admin 准入、ProjectOut 与 SDK 不变。

Native 先按 UUID 顺序锁当前操作人与接收账号，再取 Project 排他 NOWAIT 锁；目标账号启用检查在 Project 之前，目标 `project_admin` 角色检查在 Project 之后。只修改 owner；同一 owner 不执行 UPDATE，保留 updated_at。不额外写业务 Audit 或 membership。API 数据适配器借用调用方事务，成功不提交；账号锁异常回滚，项目锁冲突保留原失败事务状态。

成功回执与 owner 更新原子提交；匹配重放返回原成功内容，即使之后再次转移也不重做旧写入。每次重放仍检查当前 active super-admin 与 Project 存在并锁定，不要求旧接收者仍存在或仍有原角色。operation UUID 与 actor／project／target 不匹配返回 409。公开调用不增加幂等头，也不重发 Workspace 命令。

独立一致性检查继续使用受限的 `yoao_workspace_owner_transfer_client` LOGIN，仅能执行 `yoao_workspace_owner_transfer_v1` 中的 `owner_transfer_policy()` 和 `transfer_project_owner(jsonb)`。Node provider 用标准 PostgreSQL 协议调用相同两项函数，不代理 Python／API，业务规则仍由 Native 执行。

来自旧 namespace 的数据库仍受既有前向迁移门禁保护：先安排维护窗口、保留恢复点、停止旧写入并排空可调用转移函数的连接，显式关闭旧 `workspace_owner_transfer_client` LOGIN。旧 LOGIN 是集群级身份，须协调共享调用方；迁移不代替操作者停用账号或终止连接。旧 namespace 不留别名，也不改变 wire schema、回执身份或锁语义。

退役前命名试点通过 70 个去重案例：20 项双 namespace 安全识别／授权，50 项负责人转移；另有 34 项单元检查通过。覆盖有数据旧库的前向切换与拒绝路径、四条绑定与旧响应对照、受限 LOGIN、事务失败、丢响应／当前备份恢复，以及 Python／Node 受限镜像重启重放。结果由 66 项未变更的通过证据和四条最终兼容路径复验组成，不代表新装配资格或全部 namespace 切换。验证范围见 [ADR-0134](/dev/adr/0134-workspace-project-owner-transfer)。

## 配置命令实现与验收状态

项目创建／更新及模板创建／更新／删除／复制使用封闭的 `aap-workspace-configuration/v1`。
受限包对照角色 `yoao_workspace_configuration_client` 的核心命令仍使用
`configuration_policy()` 和 `configuration_step(jsonb,text,jsonb,text[],text[])`。

上述六种配置命令保存不可变成功回执，在同一事务中执行分阶段写入和当前权限围栏。
API 借用调用方事务，保留提交与响应。配置使用 API 的 `ML_HEALTH_TIMEOUT`、
`VIDEO_SEGMENT_SIZE_FRAMES`、`VIDEO_TRACKER_WINDOW_SIZE_FRAMES` 和
`VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES`；跨 owner 探测使用 Inference 自己的 URL、bearer 和调用期限。

退役前的三条绑定各对照了 106 项原公开响应，独立进程的丢响应重试、重启重放及不可用不回退也通过本地检查。失败事务、备份恢复、9 项关联恢复及 `0257` 完整迁移的证据保留；独立安装包、受限镜像与并发结果不代表当前拓扑已经验收。状态与事务取舍见
[ADR-0136](/dev/adr/0136-workspace-configuration-command-owner)。

### 类别重命名

`rename_project_class` 复用同一个配置 owner 和调用方事务。公开入口仍先检查
项目管理权，再校验非空名称；账户、项目和成员权限围栏保持原顺序。
实际改名在同一事务内先调用 Labeling 的窄标注类名能力，再写项目绑定。
公开响应继续在提交后读取项目投影；此时查询失败不会撤销已经提交的改名。

同名请求不写业务数据或操作回执，也不检查类别和工具单位是否存在。
API 在调用方仍持锁时读取投影。改名没有持久成功收据，不会自动重发。
退役前的三路径、独立安装包、受限镜像、重启、恢复和完整迁移已通过限定本地验收，范围见
[ADR-0143](/dev/adr/0143-workspace-project-class-rename-owner)。

## 模板列表、详情与响应投影

模板列表、详情和配置提交后的响应已由 `aap-workspace-template-query/v1` owner 提供。
API 保留参数与身份准入、写入提交和公开 DTO 校验；读取 owner 使用本次已认证的身份快照，
不增加第二次账号／角色检查。模板可见性、组织成员事实、过滤、排序及创建者显示的读取顺序保持不变。

模板读取借用调用方事务，保留原 autoflush。包对照中的专用 LOGIN 仍只有 policy／step 两项函数权限。

提交后投影绑定成功配置命令的私有回执，不增加公开参数，也不重新校验模板可见性。
此时查询或公开响应校验失败，不能回滚已经提交的配置；JSONB-null 的旧500行为保留。
原模板 ORM 读适配器已删除；命名流水线通过 Identity 窄能力读取组织范围，其他命名流水线写入仍由原入口持有。

退役前的三条路径各对照了 40 项实际旧 HTTP 观测，借用绑定另核对了调用方自动刷新和时区；
源码、受限数据账号与实际进程验证通过。独立安装包、受限 owner/API 镜像、
真实容器响应投影、故障恢复、备份恢复与0259完整迁移均已完成限定本地验收；
16次详情读取在并发4下全部成功，无 OOM，该本地样本不代表生产容量。
当前范围和信任边界见 [ADR-0137](/dev/adr/0137-workspace-template-query-owner)。

## 项目查询

项目列表、分页、详情、统计、就绪度、访问上下文、接受操作后的投影和项目搜索使用
`aap-workspace-project-query/v1`。API 借用调用方事务并保留原公开 DTO。

详情保留完整项目读取、当前账号、刷新项目、成员职责的原顺序；成员两个时间戳的
有限值超出 Python 范围时同样保留原内部错误，真实无穷值仍按原驱动处理。
需要显示资料时调用 Identity 的既有批量 UserBrief 适配器；空页、统计和就绪度不访问 Briefs。
限定本地验收与剩余边界见
[ADR-0139](/dev/adr/0139-workspace-project-query-owner)。

访问上下文的 `get_project_access` 复用同一个查询 owner，返回原有成员 ID、版本与能力字段。
第一轮保留初始项目、当前账号、刷新项目和成员读取，第二轮从当前账号开始再查项目与成员；
最终职责和版本来自同一次成员读取。该操作不需要 Briefs，不增加权限锁或更强事务快照。
原本地锁定解析器的 I/O 和锁序保留，共享纯权限策略。退役前的三路径响应、受限镜像、重启、
恢复和完整迁移已通过限定本地验收；历史证据见
[ADR-0141](/dev/adr/0141-workspace-project-access-query)。

供类别／属性统计使用的 `require_project_manager` 复用同一 Project Query owner，
只执行原来一轮当前权限检查，成功仅返回目标项目 ID。可见的普通成员在完整成员行解码后
返回原管理权限错误，不使用 `/access` 的第二轮读取，也不访问显示资料。
API 随后调用独立 Labeling 统计边界，配置见 [Labeling 运行协议](labeling-runtime)。

## 项目成员与提及候选

`GET /api/v1/projects/{project_id}/members` 与 `/mention-candidates` 使用
`aap-workspace-directory-query/v1`。成员先按 `assigned_at DESC` 读取完整成员记录，
再批量查询全局 UserBrief；空集不查资料，缺失资料继续省略。
提及候选按负责人、启用超管、成员三组分别读取，并按 UUID 保留第一个种类。
成员组不额外过滤停用账号，不能把三组统一改成仅启用账号。

读取保留调用方的 autoflush／`no_autoflush`，不提交或回滚。非空成员列表调用 Identity Briefs；
提及候选不读取 Briefs，只消费 Identity 的窄账号事实，不获得 User 原表权限。
包对照角色 `yoao_workspace_directory_query_client` 仍只有 policy 和 step 两项函数权限。
协议与验证范围见 [ADR-0140](/dev/adr/0140-workspace-directory-query-owner)。

该目录切片在退役前完成了三路径响应对照、独立安装包、受限镜像、故障重启、备份恢复及完整迁移的限定本地验收。1 CPU／256 MiB 下的 16 次并发读取不代表生产容量；
API 与数据库不在该容器预算内。完整模块当前状态见[模块台账](/dev/concepts/module-deployment#剩余工作台账)。

## 保存流水线结构

项目配置保存的预标注阶段通过共享 `yoao-inference-contract` 验证和规范化。公开 JSON Schema 位于 `apps/_shared/inference_contract/src/yoao_inference_contract/preannotate.schema.json`；阶段序号可非零、父阶段必须更早、依赖深度最多三层，写入目标、ROI 和属性键冲突规则保持。规范化复用任务名称契约；后端存在性、权限和可用性继续由原有入口检查。Workspace 核心与镜像无需安装 Inference 提供方，旧 Python 导入路径保留兼容别名；422 详情、校验时机和调用方事务不变。

## 同事务运维导入与质量配置

`aap-workspace-project-maintenance/v1` 是固定 SQL 数据合同，复用 `yoao_workspace_v1`，
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

冷运维验收从 API 镜像运行实际 nuScenes CLI，用既有可信运维账号的 Identity／Workspace
消费者围栏及两项 provisioning 能力验证同事务行为；普通 API 不增加运维权限。真实
对象存储的小夹具通过缺失能力、后置失败、容器中断后同名恢复、重复导入及来源漂移
检查。失败时 SQL 共同回滚，外部对象可能保留；重放也可能重写同键对象。中断只证明
连接释放后的最终回滚，不保证立即中断正在执行的长 SQL。可重复命令见[模块边界定向验收](/dev/testing#module-boundary-acceptance)，
限定证据见[冷运维导入验收](/dev/concepts/module-deployment#workspace-冷运维导入验收)。

质量晋升继续由原入口重验项目管理权，在既有 Project `FOR UPDATE` 下校验评估 gate、
baseline 与候选修订。Native setter 只写精确配置与事务时间戳，相同 JSONB 不推进时间；
调用方同步 Project 的配置／updated_at，不追加 ORM 写入。评估和 Audit 与配置一起提交
或回滚，错绑定回复不能确认整个工作流。能力不增加 CAS、授权检查、提交或回滚。

预建的 `yoao_workspace_project_maintenance_client` 通过
`infra/docker/workspace-project-maintenance-grants.sql` 获取六项固定业务 owner 执行权，
没有私表／列、序列、DDL 或 TEMP 权限；PostgreSQL/public invoker helpers 不属于这项
业务能力计数。先应用当前迁移和 `module-data-grants.sql`，再按专用脚本设置 `expected_database`、`migration_role`、`workspace_owner` 与 `workspace_project_maintenance_role`。严格回复及 Native／Node／
真实消费者的限定本地证据见 [ADR-0164](/dev/adr/0164-workspace-transactional-project-maintenance)。

开发点云种子还使用 `aap-workspace-seed-project/v1`，仍在同一 SQL Session 内调用：

| 能力                                                         | 返回值                                                         |
| ------------------------------------------------------------ | -------------------------------------------------------------- |
| `create_point_cloud_seed_project(uuid,text,text,uuid,jsonb)` | `schema/display_id/project_id`，严格绑定创建 UUID              |
| `write_nuscenes_seed_defaults(uuid,text,jsonb)`              | `schema/project_id/updated_at`；项目不存在时 timestamp 为 null |

`seed_pointcloud.py` 的目录种子按 display ID 跳过已有项目，缺失夹具检查在此之后；
新项目关闭两个 scene 标志。nuScenes 路径则开启 scene 模式，重跑时只重设名称、
工具绑定和两个 scene 标志，相同值不推进时间戳。读取旧种子会填充已过期字段，
保留仍已加载的本地修改；写入后同步缓存，供随后按 scene 建包使用。
目录建题保留内部提交，nuScenes 保留外层提交，旧 S3 对象仍只在外层提交后显式清理。
开启分包时，只有 Labeling 确认没有可分包任务才视为幂等重跑并报告 `batches=0`；
公开分包接口在同一情况下仍返回 `400 / No unassigned tasks to split`。其他分包错误
由种子调用方回滚，已写入的部分批次随之撤销，不执行旧对象清理；显式禁用分包时不调用批次 owner。

这两项固定运维写入不提供用户授权、通用字段补丁或提交能力。maintenance LOGIN
现有六项固定函数；其最初三项合同与快照保持不变。原开发兼容角色继续可运行脚本；
已应用普通消费者围栏的**可信运维账号**可显式追加
`infra/docker/workspace-seed-operator-grants.sql`。采用显式最小消费者配置的 API／worker
仍只有32项函数和两张事实视图；不能把运维追加配置用于这些独立账号。
这不代表开发种子的 Assets 私表操作或其他运维脚本已完成模块提取。
匹配冷 API 工件的实际种子入口已通过受限凭据、真实 S3、后置失败／回滚、提交后清理
及漂移重跑验收，范围见[开发点云种子](/dev/concepts/module-deployment#workspace-开发点云种子)。

主脚本 `seed.py` 的演示成员写入使用 `aap-workspace-seed-members/v1`，通过
`write_seed_members(jsonb)` 在调用方同一事务中按序插入或修改成员。更新仅写原有脏字段
`role`／`assigned_by` 及事务时间戳，保留 `version`、`weekly_target` 和 `assigned_at`。
同角色不修复分配人；缺失 persona／项目跳过。保留 autoflush、重复插入约束错误和
有变更才提交的行为；缺失更新目标或无效 owner 回复使本次 flush 回滚，调用方捕获异常
后再次提交也不会落下部分结果。读取使用既有公开事实关系。

可信运维追加配置现有六项 Workspace 写能力，显式最小 API／worker 配置仍是32项函数。
账号创建由独立的 Identity 种子能力持有。此范围只包含主脚本自身的账号和成员操作；
媒体子脚本及完整模块资格见[唯一台账](/dev/concepts/module-deployment#剩余工作台账)。

### 媒体种子 {#媒体及截图种子}

媒体 Project 使用 `aap-workspace-media-seed-project/v1`，成员固定时间使用
`aap-workspace-seed-member-assignment/v1`，均借用调用方事务：

| 能力                                                        | 语义与回复                                                                                          |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `create_media_seed_project(uuid,text,text,uuid,text,jsonb)` | kind 为 coco8／video／ocr／large_image；插入原默认值，严格绑定 kind、display ID 与创建 UUID         |
| `write_seed_project_projection(uuid,jsonb)`                 | 只写声明的种子元数据字段；返回 project_id、found 与 updated_at，缺行返回 false／null                |
| `pin_seed_member_assignment(uuid,timestamptz)`              | 只写成员 assigned_at 与事务 updated_at；返回 member_id、found 及两项时间，缺行返回 false／null 时间 |

Project 投影接受名称、类型、owner、状态、AI 标志、工具绑定、主后端／流水线及两项
时间戳，拒绝未知或空补丁；计数继续由 Labeling owner 持有。未指定 updated_at 时
使用事务时间；原外键、唯一约束和调用方提交／回滚保留。
成员时间能力只在原脏字段需要写入时调用，保留陈旧缓存下的原 UPDATE 语义；同一
调用方事务组合现有角色／分配人写入，保留 version 与 weekly_target。

授权使用上述可信种子追加配置；普通 Workspace consumer 配置不授予三项新能力。旧 development 兼容
角色继续可运行脚本，专用 maintenance LOGIN 仍保留原六项能力。实际调用边界和
历史本地验收范围见[模块台账](/dev/concepts/module-deployment#workspace-媒体及截图种子)，其中录制与 OCR 专用 CLI 已退役。

### 门控 E2E 夹具

`yoao_workspace_fixture_v1` 使用 `aap-workspace-e2e-fixtures/v1`，只用于既有测试
夹具。数据库名必须以 `_test` 或 `_e2e` 结尾；HTTP 入口仍要求非 production 和
`E2E_SEED_ENABLED=true`。普通 Workspace consumer 不获得这些能力。

| 能力                               | 调用方事务中的行为                                        |
| ---------------------------------- | --------------------------------------------------------- |
| `create_project(jsonb)`            | 接受固定的十字段记录，保留原默认值，返回调用方指定的 UUID |
| `create_templates(jsonb)`          | 接受固定模板记录数组，返回插入条数                        |
| `guard_project_cleanup(uuid[])`    | 排序锁定项目，在任何子记录删除前检查真实视频退役状态      |
| `delete_projects(uuid[])`          | 再次检查退役状态，只删除指定 UUID，返回条数               |
| `delete_templates(boolean,text[])` | 删除固定共享前缀或精确 display ID 集合，返回条数          |

创建字段由协议 schema 封闭定义；清理模板只读关系为 `templates(id,display_id)`。
项目和成员读取复用普通 Workspace 事实关系，成员创建与种子元数据更新复用既有
writer。原 fixture 的不兼容角色、历史属性及未来视频上下文保持原值。

先安装 owner grants 和普通 consumer 围栏，再为受信任测试操作账号追加
`workspace-seed-operator-grants.sql` 与 `workspace-e2e-fixture-grants.sql`。
完整 E2E 准备／清理由独立 owner 夹具进程执行，普通业务 HTTP 保持受限；上述固定
能力仍分别验证调用方事务和权限边界。物理对象收尾前先预检项目退役状态与 Jobs：
未退役视频、仍有关联或保留 Jobs 记录的项目、以及忙锁返回 409；终态本身不证明物理工作已停止。
预检 savepoint 回滚，避免提前持久化从未启动作业的清理许可。
最终删除再次检查，拒绝时回滚项目和子记录并保留重试锚点，但不撤销已完成的对象删除。
不得补造退役凭证。项目和模板残留均使清理失败；shared reset 前先显式清理各 owned namespace。

### 规模种子

规模种子使用 `aap-workspace-scale-seed/v1`，保留 SQL 默认值和显示编号序列。项目初始
`total_tasks` 由既有 Labeling projection 在同一事务中写入。每次调用必须提供唯一
`--run-id`；项目和数据集 UUID 在提交时登记到私有夹具清单，已存在的旧名称不会被接管。

| 能力                                                            | 语义                                                                                 |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `yoao_workspace_v1.create_scale_seed_project(uuid,text)`        | 插入固定类型的项目，保留 PostgreSQL 默认值，返回生成 UUID                            |
| `yoao_workspace_v1.ensure_scale_seed_member(uuid,uuid,uuid)`    | 插入标注职责；冲突时不更新，返回 0 或 1                                              |
| `yoao_workspace_v1.guard_scale_seed_cleanup(text)`              | 捕获并锁定精确名称范围，先检查 Learning 保留及视频退役                               |
| `yoao_workspace_v1.delete_scale_seed_projects(text,uuid[])`     | 只删除捕获且仍匹配名称的项目；再次检查保留条件                                       |
| `yoao_assets_v1.guard_scale_seed_dataset_cleanup(uuid,boolean)` | 复用 Assets 的 Dataset／Scene／Item 锁及来源保留检查；任务清理后再检查 Labeling 引用 |

最后一项使用 `aap-workspace-scale-source-guard/v1`，校验 Dataset UUID 和调用阶段，
不向客户端暴露私有锁 helper，不删除来源字节。清理先关闭指定运行的准入，并要求所有
准入已结清；随后只在 Workspace/Assets owner 返回范围与清单完全一致时继续。未登记
原件、未退役执行、学习历史、辅助引用或忙锁会阻止删除，保留锚点。

规模种子仅在 `_test` / `_e2e` 隔离库且显式启用 E2E seed 时运行，通过受信任的夹具
owner 通道访问私有台账和写入；Identity 账号选择使用单独的受限运行账号，权限缺失
时不会切回 owner。普通 API／worker 不获得夹具 owner 权限。生产环境仍在 CLI 创建
engine 前拒绝。旧 `scale-seed` 名称不会由新运行的清理隐式删除。
此消费者的 Workspace 边界不代表其余领域的批量 DML 已完成提取。

## 锁定权限与会话快照

API 写入权限入口保留原元组接口，通过现有
`yoao_workspace_v1.project_access_step(uuid,uuid,text)` 获取 Project KEY SHARE 和
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

## 普通业务项目快照与无 actor 读取

`read_project_row(uuid)` 通过同一调用方事务返回现有完整项目快照。普通 getter 在
缺省 actor 时使用该能力，有 actor 的既有入口继续使用原事实阶段；后台元数据不会
为读配置而伪造账号。空目标先遵循原 autoflush，再返回缺失。快照不是授权凭据，
原入口仍复核当前账号、项目职责和最终写入范围。

86 处普通只读 getter 保留会话、条件分支、项目缓存及十处强制刷新；受限 API 的
READ ONLY、Node、异常回复回滚和实际后台 relay 已通过限定本地检查。完整缓存
兼容继续使用已有行解码与会话绑定。安装当前迁移及 owner／应用授权，不新增运行
进程或凭据。范围见
[ADR-0171](/dev/adr/0171-workspace-actorless-business-snapshots)。其他集合查询及完整模块装配仍按唯一台账推进。

## 排序项目集合锁与计数投影

批次决策一次调用 `yoao_workspace_v1.lock_project_references_for_update(uuid[])`，
返回按 UUID 排序的现存项目 ID 集，锁保留在调用方事务内。重复 ID 去重，缺失 ID
不返回；NOWAIT 的 busy SQLSTATE 仍映射为 409 并回滚原事务。

两处 Labeling 重算读取现有项目快照，在原 ORM flush 内调用
`yoao_labeling_v1.write_project_counter_projection(uuid,jsonb)`。补丁仅包含实际变化
的四项任务计数、`batch_summary`，以及可选显式 `updated_at`；未提供时间戳时写
事务时间。回复为目标 UUID，缺失为 null；适配器对缺失或畸形回复在 UoW 内抛错，
保留原事务回滚。原无变化缓存、未 flush 的工作及外层提交顺序保持。

两项能力均为固定 search_path 的 SECURITY DEFINER，PUBLIC 无执行权；已有 owner、
应用与本地 worktree grant 清单同步更新。没有新增连接或自主提交点。范围和受限
API／Node 验收见 [ADR-0172](/dev/adr/0172-project-set-locks-and-labeling-counters)。
这仍是操作级调用边界，完整模块凭据与运行工件尚未集中验收。

撤回、重开与审核的原子增减分别使用 Labeling 的
`decrement_project_review_count(uuid)`、`decrement_project_completed_count(uuid)`、
`record_project_review_approval(uuid)`。它们直接更新数据库当前值，使用正常行锁等待，
保持原 autoflush、计数缓存和 Task／Audit 外层事务；不通过绝对补丁覆盖旧快照。
受限 API 撤销 Project 私表 SELECT／UPDATE 后，四条实际 HTTP 流程及 Node 调用通过，
范围见 [ADR-0176](/dev/adr/0176-labeling-lifecycle-counter-deltas)。

## 数据集解绑的调用方事务

预览和解绑使用 Workspace 包的 `DatasetUnlinkManager`，默认 API 通过原 SQLAlchemy
Session 执行既有 Native 函数。独立调用方可用
`DatasetUnlinkPostgresData(connection)` 借用已开启的 asyncpg 事务，账号为
`yoao_workspace_dataset_unlink_client`。它仅有 `preview_dataset_unlink` 和 `unlink_dataset`
执行权，没有私表／列、序列、DDL 或 TEMP 权限；沿用现有最小权限验证器。

绑定不打开、结束或重试事务。预览不删除业务数据，但会锁定当前权限；两项能力都
不能使用 READ ONLY 事务。原关联代次、保留检查、原子清理／审计／回执、缓存和
公开幂等行为保持。完整字段与作用域见[数据能力协议](./module-data#workspace-数据集解绑与清理回执)
和 [ADR-0177](/dev/adr/0177-workspace-caller-transaction-dataset-unlink)。

本地资格覆盖调用边界及[视频退役门禁](./module-data#视频退役与删除围栏)。未退役时
返回 video_execution_not_retired 409，锁竞争沿用 project_busy；两者均保留原事务。
旧作业无证明的排空／修复入口仍缺；组合凭据、冷工件和恢复的本地资格见[模块台账](/dev/concepts/module-deployment#workspace-组合运行与恢复验收)。

## 项目范围、看板与列表读取

`yoao_workspace_v1.actor_project_memberships(uuid)` 只返回 actor 的 `project_id` 与
`member_role` 原始事实，NULL actor 返回空集。它不读取日期，不授权或锁行；调用方
仍与 Identity 资料关系联接，过滤活跃账号、已知平台／项目角色、viewer 配对和所需
职责。原超级管理员全局范围及 manager owner 分支保持，角色不会互相代替。

审核／标注看板、异步作业列表与通知读取在原 SQL 中组合这些事实和既有项目搜索
范围，保留 count、分页、claim、导出职责、retained project 与伪造 payload 的处理。
读取工厂不依赖提供方运行时；ORM 列别名仅保持原 Select 的 autoflush，并不把事实
转换为可写实体或额外加载时间字段。API 在项目、成员和用户私表 SELECT 被拒绝时
可完成这些选定读取，认证与姓名资料仍走各自 Identity owner。

范围见 [ADR-0173](/dev/adr/0173-workspace-actor-scope-relations)。非锁定成员集合使用下述
成员事实关系；Project 集合／标量见下文原生项目事实。完整模块
凭据、冷工件与恢复继续按[唯一台账](/dev/concepts/module-deployment#剩余工作台账)集中验收。

## 非锁定成员集合与名册

`yoao_workspace_v1.project_member_facts` 由 Workspace 持有，以固定顺序公开成员的
九个原生列：`id/project_id/user_id/role/assigned_by/assigned_at/version/updated_at/weekly_target`。
它不联接、过滤、排序或授权；未知职责与可空值保持原样。便携描述复用共享
`ProjectMemberRow`，应用只增加此视图的 SELECT，PUBLIC 不获权限。

任意接收人通知、绩效名册、管理看板、批次内的相关成员判断和评论提及查询已接入。
各调用保留原规则：通知仍分四次查询；批次只看所需项目职责，不追加账号状态；
提及允许任意职责成员；个人绩效的团队分母仍只要求活跃账号和 annotator 成员关系。
完整 ORM 别名保留 identity map、脏字段、过期字段与 autoflush；局部字段查询不解码
未读取的日期。没有新增连接、锁或提交点。

受限 API LOGIN 在成员私表 SELECT 被撤销后完成选定 SQL 与真实 HTTP 读取，Node
直接读取同一关系。通知、提及和管理看板的 Project 字段也通过下文原生项目事实读取。默认
应用宽角色也仍是兼容装配，不能把本轮定向撤权试验视为默认凭据已收紧。边界见
[ADR-0174](/dev/adr/0174-workspace-nonlocking-member-facts)。

## 项目集合、投影与元数据

`yoao_workspace_v1.project_facts` 以原生 PostgreSQL 类型公开 Workspace 拥有的 38 个
Project 字段。完整对象通过一次主键联接组合既有
`yoao_labeling_v1.project_catalog_counters` 的五项计数，保留原 43 列 ORM 映射。
计数列均有 NOT NULL 约束，因此既有关系中的默认值不改变受支持数据；JSONB 自身的
null 与非对象值仍原样返回。合同复用 `PROJECT_ROW_COLUMNS`，不增加第二个 Project DTO。

管理总览、预标与视频列表、失败预测外联接、训练的相关计数／分页、审计范围、通知
与提及、导出元数据、回填和点云过期读取已接入。部分读取只选择需要的列；完整对象
保留缓存、脏字段及过期字段，训练列表保留原延迟列。总数语句也使用 ORM 别名，保持
超级管理员无范围谓词时的 autoflush。流式 Labeling 查询保留单语句、原刷新及分块参数。

应用只新增两项公开关系的 SELECT；默认宽角色仍属兼容装配。三处 Labeling 计数
原子写入和完整模块资格继续按台账推进。边界与定向验收见
[ADR-0175](/dev/adr/0175-workspace-project-fact-relations)。

## 最终写入与批量分派的成员锁

单成员围栏使用只返回 user_id／null 的 owner SHARE 能力，保留等待／NOWAIT、原
membership_busy 与账号 → 项目 → 成员锁序，不解码无关时间列。批量分派按已排序
账号取得 Identity 锁，再一次锁定排序成员集合，完整快照刷新原 ORM identity map；
当前账号、项目职责及原输入顺序的错误判定仍由调用方执行。

过期脏缓存的 preflush 读取使用同一 owner 的无锁分支，随后再 flush 和获取成员锁；
所有操作保留调用方 Session 与提交边界。返回行严格校验项目、请求用户集、重复与
顺序；异常范围失败由调用方回滚。只新增现有应用可执行的两项固定函数，没有新增
运行进程或凭据。语义与限定验收见
[ADR-0170](/dev/adr/0170-workspace-transactional-member-locks)；完整装配仍按模块台账推进。

## 项目删除的调用方事务

`DELETE /projects/{project_id}` 要求当前项目管理权，成功返回空体 204，项目不存在返回 404。
没有独立提交的私有 HTTP 命令，也没有公开幂等头。Workspace 通过
`yoao_workspace_v1.delete_project(uuid,uuid,uuid,jsonb)` 协调同库事务。输入只有
`operation_id`、`actor_id`、`project_id`、`audit_context`，无审计上下文时绑定 SQL NULL。

API 先 flush 原有待写入变更，在原事务中调用 Native 函数，再由原路由提交。
`ProjectDeletionPostgresData(connection)` 要求已开启的 asyncpg 事务，用
`yoao_workspace_project_deletion_client` 核验唯一协调器执行权限，不新建或结束事务。
`aap-project-delete/v1` 响应包含严格的 `ok`，成功回执带操作、actor、项目 UUID，
以及删除的 Task／Annotation 计数和审计 ID；失败返回稳定 `error.code`，在 API 边界映射为既有 HTTP 错误。

首次执行锁定当前活跃账号，按操作 ID 和项目串行化，核验当前项目管理权并以 NOWAIT 锁定项目行。
Learning 先检查主动学习引用和保留的训练、模型、评估、策略与数据版本，
Labeling 随后检查[视频退役围栏](./module-data#视频退役与删除围栏)。保留引用或未退役执行返回 409。
通过门禁后，各 owner 清理各自数据。Workspace 退役项目上传记录、清理成员和模板来源引用并删除项目，
Audit 获取当前锁定的身份事实并写入 `project.delete`。应用 LOGIN 不获私表权限或各 owner 的清理 helper。
源对象和导出文件不在该事务中物理删除；取消数据库中的活动建题作业也不等于撤销 Celery 消息。

锁冲突返回 `project_busy`；数据库异常或提交失败使各 owner 变更、审计和回执一起回滚。
事务内回执不是提交确认。内部调用方要处理不确定提交，须在首次调用前持久保存 operation ID。
已提交回执不依赖 Project 外键。账号仍活跃且操作 ID、actor 和项目完全一致时可重放；
同一 ID 改用其他 actor 或项目返回 `operation_conflict`。公开 HTTP 重试不会进入该回执重放路径。
Jobs 与通知保留[原项目范围](./module-data#jobs-保留的项目范围)，不会因项目删除而变为全局。

设计与验收范围见 [ADR-0165](/dev/adr/0165-workspace-caller-transaction-project-deletion)。

## 普通消费者的 SQL 凭据边界

显式最小 API／worker 消费者的 Workspace 本地调用使用既有32个固定函数及两张事实视图。
`infra/docker/workspace-consumer-grants.sql` 可在 owner 与兼容应用授权后显式应用，
撤销项目、成员、模板、删除／解绑回执和内部状态的表／列权限，以及项目序列、
私有 schema 创建和内部 helper 权限。固定视图仍为38列项目事实与9列成员事实；
项目计数继续从 Labeling 的公开关系读取。它不改变请求的连接、提交或权限校验。

配置脚本拒绝不匹配的数据库、owner、管理账号和普通账号向外的角色继承，并检查
有效的 PUBLIC 权限；失败整份回滚。迁移者可保持继承运行账号的既有维护方向。
未来 public 表／序列不再自动获得宽权限；迁移或重新应用兼容授权后应重新执行配置。
显式 seed 使用单独操作账号；普通账号不再直接读写这些私表。

兼容部署脚本保留六组借用调用所需的固定函数权限和成员搜索能力，无需为了补权限而改选连接。
此前 13 项本地定向案例通过，含真实 HTTP 生命周期与搜索、Node、worker 计数及回滚，
另有 8 项既有角色回归通过。兼容授权保持清理 worker 的八项函数，避免重新授权 API 时退回
两项 outbox 能力。完整模块的既往冷工件／装配／恢复结果见[历史模块审计](/dev/concepts/module-deployment#workspace-module-audit)；凭据切片的原始范围见
[ADR-0179](/dev/adr/0179-workspace-consumer-credential-closure)。

默认内嵌运行采用不同的权限组合：LAN 和 worktree 共用 Workspace 私有存储 ACL，
移除上述私表／列、回执、分区、相关序列及 schema CREATE，并清除宽默认授权；
只有两张公开事实视图可读，测试库可额外读取既有模板夹具视图。有效继承／所有权
旁路会使授权事务回滚。LAN 的围栏先于 cleanup 账号的有效权限检查执行。

共享开发账号保留既有固定导入、seed、scale、维护函数和仅 `_test`／`_e2e` 库启用的
夹具能力；不会为了采用存储围栏而收回受支持脚本的执行权，也不把它描述为32函数
最小账号。显式最小配置、专用操作配置及其 NOLOGIN owner 校验保持不变。应用匹配授权前仍须
核对并排空旧直接访问消费者；原默认存储围栏证据见[限定资格](/dev/concepts/module-deployment#workspace-default-private-storage)。
