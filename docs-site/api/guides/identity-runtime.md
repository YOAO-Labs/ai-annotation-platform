---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-23
---

# Identity 私有运行协议

协议对象只接受声明的 wire 字段名：版本字段为 `schema`。内部 Python 字段名 `protocol` 无论单独使用还是与 `schema` 同时提交都被拒绝；opaque 偏好值中的同名键不受影响。

Identity 运行时提供认证准入、API key、核心会话、自助账户、全局分组目录与指派、完整邀请生命周期、四项用户目录读取、自助注销、注销到期处理、账号恢复和在线扫描命令；平台角色预览／变更、管理员暂停／恢复与 UserBrief 批量 reads 均通过各自限定的本地资格。公开用户接口仍是 `/api/v1/auth/`、`/api/v1/me/api-keys` 与 `/api/v1/users`。私有接口要求独立控制 Bearer；需要用户身份的操作将凭据放在 JSON 的 `token` 中，不能代替控制凭据。注册/邀请证明凭据另置于 `payload`，与调用者身份 token 分开。账号恢复使用匿名请求或单次挑战证明，邮件与注销通知分别由独立执行进程投递。

已配置能力的装配通过本地 Compose 私网功能验收：独立安装／冷镜像、全部配置能力、受限远程 API、不同签名密钥、邮件／通知 worker、真实消费者及完整数据库恢复。运维与门控测试操作已按 owner 收口；默认兼容运行角色的私表权限仍按台账验收。旧宿主机数据库转发路径仍未修复；日常主环境尚未切换。精确证据、网络边界和其他模块剩余工作见[模块台账](/dev/concepts/module-deployment#identity-整模块独立装配)。本页各操作的案例数只证明各自合同，不能相加作为整模块覆盖率。

| 接口                                             | 用途                                           |
| ------------------------------------------------ | ---------------------------------------------- |
| `GET /health`                                    | 进程存活                                       |
| `GET /ready`                                     | 原生策略和所配置能力的精确函数权限             |
| `POST /v1/identity/authenticate`                 | JWT／API key 准入，返回 `aap-identity/v1` 主体 |
| `POST /v1/identity/actor-context`                | 准入后读取当前主体的基础资料事实               |
| `POST /v1/identity/actor-status`                 | 已准入账号的最小活动状态读取                   |
| `POST /v1/identity/api-keys/execute`             | API key 列表、创建、修改、轮换和撤销           |
| `POST /v1/identity/sessions/execute`             | 登录、刷新、登出和全端登出                     |
| `POST /v1/identity/accounts/execute`             | 当前资料、改名、偏好、心跳、改密和头像引用     |
| `POST /v1/identity/presence/mark-inactive`       | 按已固定的 cutoff 批量标记离线                 |
| `POST /v1/identity/groups/execute`               | 分组目录、查询、指派预览／执行                 |
| `POST /v1/identity/invitations/execute`          | 邀请管理、批量操作、注册、接受与耐久邮件发送   |
| `POST /v1/identity/directory/execute`            | 用户目录列表、查询、统计与导出                 |
| `POST /v1/identity/briefs/execute`               | 已限定本地资格的内部批量 UserBrief 富化        |
| `POST /v1/identity/admin-passwords/execute`      | 管理员临时密码重置                             |
| `POST /v1/identity/platform-roles/execute`       | 平台角色预览与变更                             |
| `POST /v1/identity/suspensions/execute`          | 管理员停用与恢复账号                           |
| `POST /v1/identity/deletions/execute`            | 管理员删除账号与可选任务转交                   |
| `POST /v1/identity/offboarding/execute`          | 离职预览与提交、任务职责交接                   |
| `POST /v1/identity/deactivation/execute`         | 自助注销申请与撤销                             |
| `POST /v1/identity/deactivation/process-due`     | 分页处理已到期的自助注销申请                   |
| `POST /v1/identity/recovery/execute`             | 开放注册、重置请求／消费、验证邮件重发／消费   |
| `POST /v1/identity/recovery/registration-status` | 有效注册开关或 owner 部署默认值的只读查询      |

## 限权准入与账号活动状态

可选 `IDENTITY_ADMISSION_TOKEN(_FILE)` 必须与通用控制 token 不同，只能调用
`/authenticate` 与 `/actor-status`；其他私有接口仍仅接受通用控制 bearer。
Notifications 不持有通用控制凭据、签名密钥或 API 数据库凭据。共享
`aap-identity-admission` 包保留原 Principal／异常类型的类身份；旧 Python 导入路径
继续导出相同对象，远程客户端支持 API 已有的 HTTP(S) base path。

`POST /v1/identity/actor-status` 请求精确为
`{"schema":"aap-identity-actor-status/v1","actor_id":"<uuid>"}`。成功回复精确为
`schema/ok/data`，`data.actor` 是 null 或 `{actor_id,is_active}`，`is_active` 必须是
JSON boolean，返回的 UUID 必须匹配请求。它不验证 JWT、比较先前 role、加锁或写入；
握手先认证再单独读取活动状态，保留“角色变化但账号仍活动”的原行为。

仅此 endpoint 使用可选 `IDENTITY_ACTOR_STATUS_DATABASE_URL(_FILE)` 和既有
`read_actor_facts(uuid)`。专用 LOGIN 由 `identity-actor-status-grants.sql` 授予一项
EXECUTE，无表／列权限或其他 owner 能力，不扩大原 AUTH 五函数。
未配置或不可用返回 HTTP 503；非法 JSON／闭合字段返回 HTTP 400 的
`invalid_request` 失败封套；错误 bearer 返回 401。配置该数据源后 `/ready` 也检查
它，未配置时不影响原能力就绪。原整模块资格覆盖原先配置的能力，本次新增接口的
独立运行资格单独列于模块台账，不计入旧案例或能力数量。

## 旧 Workspace 绑定的组织读取

普通 API 的固定 allowlist 还包含 `aap_identity_v1.workspace_template_query_org_ids(uuid)`，供尚未迁移的模板与编排消费者读取当前账号的有效组织 ID。此能力复用已有 `aap-identity-workspace-template-query/v1` 回复；调用方校验 actor UUID 和完整回复，保留原 `autoflush` 与外层事务，不获取组织成员私表权限。普通 API 共保留两张公开视图和六项函数；独立 Identity AUTH／命令账号的职责保持分离。实际受限 SQL、HTTP 可见性和 Node 调用均有定向验收，完整运行装配及主环境采用按模块台账分别记录。

## 认证后的主体上下文

`POST /v1/identity/actor-context` 是既有 AUTH 准入后的私有只读调用，使用同一个 control Bearer，为已准入账号读取当前主体事实；它不会再次认证凭据。请求使用 `aap-identity-actor/v1`，字段精确为 `schema`、`actor_id` 和 `admitted_role`，不携带 JWT、API key、scope、audit context 或调用方提供的资料。

成功 envelope 为 `schema/ok/data`，其中 `ok: true`，`data` 严格包含 `id`、`name`、`email`、`role`、`is_active: true` 与 nullable int32 `weekly_target_default`。name/email 保留数据库原值；响应不含偏好、头像、密码或凭据。API 将返回的 `id`、`role` 和 active 状态绑定到本次准入结果。失败 envelope 的 `error.code` 只能是 `invalid_request` 或 `credential_invalid`。私有接口对 `invalid_request` 返回 HTTP 400，对 `credential_invalid` 返回 HTTP 200 的失败封套；账号缺失、停用或精确 role 不匹配由 API 映射到现有 401 和 Bearer challenge。owner 未配置／不可用、超时、响应损坏或绑定不符映射到现有 503，不回退 API ORM。

Identity migration 0250 新增只读 `aap_identity_v1.read_actor_context(uuid,text)`，并把该函数加入既有 `identity_auth_client` 的第五项 allowlist。函数不取锁、不写 Audit/receipt，也不新增 LOGIN、DSN、设置或依赖。该 endpoint 沿用 AUTH 的既有 timeout、并发槽和有界请求体限制。内嵌 reader 留在调用方事务并遵循 `autoflush`／`no_autoflush`，不 commit/rollback；专用 AUTH binding 自行持有有界只读事务。API-key 准入原提交点不变，本次读取不引入新提交。凭据过期、generation/revocation、API-key scope 和 last-used 仍由原准入负责；写入时的账号锁、目标 User 查询和 ORM mutation synchronization 保留在现有路径。本切片已通过限定本地验收，覆盖实际受限权限、事务保留、双向语言 peer、真实 API 和恢复；缺少第五项 AUTH 权限时 readiness 和读取均失败，深层非法 JSON 返回闭合的 400 失败封套。结果不代表完整 Identity 或生产切换。详见 [ADR-0130](/dev/adr/0130-identity-post-admission-actor-context)。

## 调用方事务内的账号事实锁

Account-lock 是内部 PostgreSQL capability，不提供 HTTP lock RPC，也不使用独立 Identity 连接：每个 caller 必须在同一 connection／transaction 中取得账号锁并完成随后属于自己的业务写入。Portable request `aap-identity-account-locks/v1` 固定 `account_ids`、`mode: share|update|key_share` 与严格 boolean `nowait`；重复输入可接受，Native 锁定排序去重后的 UUID。响应只包含现有账号的 `id/name/email/role/is_active` 五项事实。缺失账号省略，停用账号与历史／未知 role 不过滤，role 和 profile 字符串保持原值；没有 ORM、凭据或其他资料字段。

Migration 0251 在 `aap_identity_v1` 提供三项安全函数：`lock_accounts_share(uuid[],boolean)`、`lock_accounts_update(uuid[],boolean)`、`lock_accounts_key_share(uuid[],boolean)`。权限只加给已有 owner/workflow LOGIN 的必要操作，不扩展 AUTH 或 credential FENCE LOGIN，不授予 User 表权限，不新增角色、DSN 或环境设置。适配器校验闭合回复；畸形或错绑定结果按 unavailable 503 失败，不回退 ORM。适配器不 commit/rollback、不捕获 SQLSTATE，也不重试或转成 JSON；PostgreSQL 锁错误会使当前事务保持 aborted。`nowait=false` 等待后返回当前锁定行版本。

账号事实锁必须遵守账号 → Project → membership → resource 的既有顺序，保留每个调用方的 SHARE/UPDATE/KEY SHARE 和 NOWAIT/wait 选择。API 依旧由 caller 映射错误：单账号竞争保持 `account_busy`，账号／项目联合 guard 保持 `project_busy`，Project access 分别区分账号、项目、成员争用。`UserLifecycleService.lock_accounts` 对所有 DBAPIError 显式回滚并保留 `账号正在更新，请刷新后重试` 的旧 409 文案；其他调用方由原事务所有者决定回滚，不能在共享数据端口统一处理。

跨帧 worker 每个待写帧都重新锁定并核对当前账号及项目／成员／任务权限，不使用跨帧快照或 `db.refresh(User)` 替代。忙碌仍映射到既有 `permission_changed`，无新重试；账号删除、停用、基础设施失败与阶段拒绝的分类也保持旧区分。非锁定 Workspace authority 查询与 bootstrap/import 写入由下文的 provisioning 切片接管。最新本地 qualification 去重后 231 项全部通过，包括受限 Node 调用、转移／角色变更竞态、恢复与新 Celery 进程。源码及非 editable 安装各通过 595 项，119 个包文件与独立 owner／API 容器指纹一致。旧行为 baseline 的一个测试覆盖 15 个场景，不能叠加到新资格数量。细节见 [ADR-0131](/dev/adr/0131-identity-transactional-account-locks)。

## 门控 E2E 夹具

`aap-identity-e2e-fixtures/v1` 是可信测试运维的固定 PostgreSQL 契约，独立于普通
API 的六函数／两视图配置。`aap_identity_fixture_v1.account_profiles` 只提供原
`UserOut` 的 21 个资料字段；不返回密码哈希、凭据代际、注销原因或周目标默认值。
关系使用 security barrier，并仅在实际数据库名转小写后以 `_test`／`_e2e` 结尾时
返回行。登录仍可返回停用账号，精确邮箱、缓存资料及 peek 优先级保持。

七项固定能力分别为 `create_account`、`mark_manual_inactive`、`create_invitations`、
`delete_accounts`、`delete_reset_tokens`、`delete_invitations` 与
`delete_organization_members`。它们保留原工厂默认值、历史角色、邀请状态和清理谓词，
不提供任意 SQL、表名或动态谓词。每项写入都检查实际数据库后缀，调用方持有事务；
原始约束错误和行锁等待保持。SQL 层不新增账号授权、审计、回执、重试或提交。

路由挂载及每请求的环境／开关／数据库守卫保持。可信 operator 在普通 Identity 围栏
后通过 `identity-e2e-fixture-grants.sql` 追加这些能力，仍无 Identity 私表及列权限；
普通 API 不能追加此配置。其他领域的可信测试写入及对象权限需分别提供。配置与
实际资格见 `DEV.md` 和[模块台账](/dev/concepts/module-deployment#identity-门控-e2e-夹具)。

## 截图夹具的账号与偏好

恢复／拒绝夹具按精确 email 从公开 `account_profiles` 关系读取 UUID，不增加角色或
启用状态条件。单账号查询保持原 Session 的缓存行为，不用 email 映射重新查找已缓存行。

`aap-identity-fixture-preferences/v1` 描述一项固定 Native 函数：
`aap_identity_v1.replace_fixture_preferences(uuid,jsonb)` 返回整数 0 或 1。它完整替换
指定账号的 JSON object；同值更新仍返回 1，缺失或 NULL ID 返回 0。未知嵌套键、空值及
停用账号保持原行为，不走公开偏好接口的深合并。调用方持有事务，正常等待行锁；没有
隐式提交、重试、审计或账号授权。SQL NULL／非 object 值返回 SQLSTATE 22023。

函数在实际数据库名严格以 `_test` 或 `_e2e` 结尾时才执行，否则返回 42501；大小写不被
规范化。普通 API 仍是两视图／六函数，不能执行此能力。可信录制 LOGIN 先应用普通
Identity 围栏，再由 `identity-recording-fixture-grants.sql` 追加一项 EXECUTE；它仍无
User 私表或列权限。原始脚本的生产拒绝、来源／任务守卫及偏好工具的 URL 检查保持。
此能力不授予其他领域的夹具写入权限；命令和限定资格见仓库 `DEV.md` 及
[模块台账](/dev/concepts/module-deployment#identity-截图夹具)。

## 历史角色审计关系

`aap-identity-role-audit/v1` 是可信运维只读关系契约，独立于普通 HTTP 凭据配置。

| 关系                                        | 字段与用途                                                                          |
| ------------------------------------------- | ----------------------------------------------------------------------------------- |
| `aap_identity_audit_v1.account_roles`       | id、原始 role、is_active；保留 NULL、未知角色与停用行                               |
| `aap_identity_audit_v1.invitation_facts`    | id、project_id、role、project_role、expires_at、accepted_at、revoked_at、invited_by |
| `aap_identity_audit_v1.schema_capabilities` | 单行布尔值 invitation_project_role，表示源库是否存在该准备列                        |

关系没有筛选、排序、权限决策或提交，调用方在原 SQL 中保留作用域、连接和计数。
邀请的项目角色通过整行 JSON 的单字段表达式读取：旧库缺列时为 NULL，列加入后
自动读到新值。整行 JSON 和 email／token 不返回给消费者。

维护者显式运行 `infra/docker/identity-role-audit-grants.sql`，验证目标库及迁移角色、
既有非 LOGIN Identity owner 和受限审计 LOGIN。该步骤兼容普通 Identity schema
出现之前的数据库，不属于审计 CLI 的只读事务，也不修改 Alembic revision。普通
Identity API 仍只有两视图／六函数；审计 LOGIN 仅追加三个审计关系的 SELECT，并
被撤销 User／邀请私表及列权限。其他领域的读取与 Alembic 元数据由原 owner 提供。

审计 CLI 继续使用 `REPEATABLE READ, READ ONLY`，缺少关系或权限时返回退出码 2，
不自动降级为私表读取。安装命令见仓库 `DEV.md`；资格和剩余项见
[历史角色审计](/dev/concepts/module-deployment#identity-历史角色审计)。

## 运维账号选择

`aap-identity-scale-seed-actors/v1` 提供两项固定只读 Native 能力，回复只有
`schema`／`kind`／`account_id`；缺失账号为 null。创建时间保留在 Identity 内部。

| 能力                                | 选择规则                                                        |
| ----------------------------------- | --------------------------------------------------------------- |
| `read_scale_seed_super_admin()`     | 最早创建的 super_admin，保留停用账号，kind 为 super_admin       |
| `read_scale_seed_annotator(uuid[])` | 输入候选中最早创建的启用账号，不检查平台角色，kind 为 annotator |

没有额外的 UUID 排序、账号锁、授权或提交。这些 `STABLE` 函数只对调用语句的快照
负责；规模种子把 Workspace annotator 成员事实组成 UUID 数组，在同一 SQL 语句中传给
第二项能力。可信账号的追加配置是 `identity-scale-seed-grants.sql`；普通消费者仍是
既有两视图／六函数，无法执行选择能力。不能拆成跨连接的先读候选、再选账号。

图片导入只复用既有公开账号事实，无序选择任意首账号，不增加角色或启用条件。
定向与冷运行范围见[运维账号选择](/dev/concepts/module-deployment#identity-运维账号选择)。

## 运维账号 provisioning

开发主种子另使用 `aap-identity-seed-persona/v1` 和固定函数
`create_seed_persona_account(jsonb)`。`seed.py` 先精确查询 email；已有账号（包括停用、
角色或资料漂移的账号）完整复用，缺失时才计算密码哈希并创建，保留中文 `group_name`
和 nullable 值，不创建 Group。函数只插入账号，不隐式授权、审计、提交或重试；并发
重复仍以原始约束错误中止。主脚本继续拒绝 `production`，账号循环后统一提交一次。

迁移新增的函数独立于原 provisioning 合同。可信 `identity_provisioning_client` 当前
具有三项固定 Identity 函数；普通 Identity API 消费者仍只有两张视图与六项函数。
已应用普通围栏的可信种子运维连接可通过
`infra/docker/identity-seed-operator-grants.sql` 追加精确查询与 persona 创建能力；
该配置拒绝普通 API 账号、角色继承和 User 私表／列权限。
主种子的限定验收见[模块台账](/dev/concepts/module-deployment#主种子账号与演示成员)。

媒体子种子也复用这项固定 persona 能力。COCO8／视频保留中文管理员名称，OCR 保留
`Admin`，已有账号仍完整复用。截图准备与目录按原顺序读取公开账号事实，分别保留
角色校验和角色＋启用状态校验；后续只传递 UUID。大图保留原精确邮箱 owner 选择。
这几项消费者的迁移不增加协议、SQL 函数、角色或公开 REST；其定向与冷运行资格见
[模块台账](/dev/concepts/module-deployment#媒体种子-identity-边界)。

Identity operational provisioning 是给 bootstrap/import CLI 使用的内部 SQL owner，不是 HTTP/SDK 账号创建接口。Portable protocol `aap-identity-provisioning/v1` 只有 exact-email lookup 与 `bootstrap_admin`／`import_admin` create 两种模式；`AccountProvisioningManager` 通过 `create_bootstrap_super_admin(...)` 返回 UUID 或 `None`，通过 `ensure_import_admin(environment=...)` 返回 UUID。API caller adapter 不返回可变 User 或 password hash。

Bootstrap 按单个 email 判重，不要求全局用户表为空。已有同 email 行无论 role、active 状态、姓名或 hash 都原样复用，不额外 hash、不写 Audit；不同 email 的 bootstrap admin 可共存。CLI 继续 lowercase/trim email、trim name，owner 不增加 normalization、长度或 password-strength policy。新用户保留 role `super_admin`、active、`status=online`、null group、`email_verified_at=caller now`、generation 0、`preferences={}` 和其余 nullable lifecycle fields null；`created_at` 使用 DB transaction time。Identity 与 Audit owners 在同一 caller transaction 原子写固定 `system.bootstrap_admin`，actor 为新用户，target 为 user UUID，detail 只含 `{email}`；method/path/status/IP 为 null，request ID 来自既有 context 或 null，Audit 时间使用 DB 默认值。应用 wrapper 不重复写 Audit。

Import 只查精确 email `admin`。已有任意 row 都原样复用，包括 inactive、历史或非管理员账号；不存在时环境字符串严格为 `production` 才在 hash/create 前抛原 RuntimeError：

```text
生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。请先手动创建管理员账号再运行本导入脚本。
```

其它环境（包括 staging）沿用 `admin`／`超级管理员`／bcrypt(`123456`)／`super_admin`／active／`offline`／null `email_verified_at`，不写 Audit。Importer 对 Assets、Workspace、Labeling 的 SQL 提交/回滚与账号 provisioning 保持一个事务，失败回滚全部业务行。

Migration 0252 只增加只读 `read_provisioning_account(text)` 和 mutable `create_provisioning_account(jsonb)` 两个 Identity 函数，并新增仅有这两项 function grants 的 `identity_provisioning_client`。Audit 由固定 `aap_audit_v1.record_identity_bootstrap_admin(uuid,text,text)` capability 写入，Identity 没有 Audit table 权限。Native SQL constraint／duplicate 错误原样抛出；相同 email 并发创建仍以 `23505` 中止事务，无 conflict-ignore、重试或 adapter rollback。读路径遵循 `autoflush/no_autoflush`；create 即使在 `no_autoflush` 下也显式 flush caller pending work。没有新 runtime DSN、setting 或独立服务；verifier-only `AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL` 不是应用配置。

Project authority 的非锁定 actor/status 查询与任务、批次等已拥有者复用现有 `read_actor_facts` 和事务内 account-lock functions，不新增 SQL/http 协议，不改变锁序、mode、等待行为或 401/Bearer/403/404 映射；仍需要 Project ORM refresh 的调用方继续 refresh，active-only guard 不新增 role check。Provisioning 与 authority consumers 已通过限定本地资格：236 个去重选定案例全部通过、无有效跳过，包括受限 Node client、实际 CLI、新 Celery worker、八项恢复及事务／锁语义。源码与非 editable 安装各通过 619 项检查，122 个包文件在独立 owner 及禁网只读 API 镜像中一致。此前 account locks 的 231-case 验收独立，不计入本切片；整模块资格与完整多模块目标见[模块台账](/dev/concepts/module-deployment)。精确默认、Audit 与权限边界见 [ADR-0132](/dev/adr/0132-identity-operational-provisioning-and-authority-reads)。

## API key 请求

请求使用 `aap-identity-keys/v1`，具有 `schema`、`operation`、`operation_id`、`token`、`payload` 五个字段。`operation_id` 是调用方预先保存的 UUID；用户身份从有效凭据解析，不能通过请求指定其他 owner。

| operation        | payload                                        |
| ---------------- | ---------------------------------------------- |
| `list_api_keys`  | `{}`                                           |
| `create_api_key` | `name`、`scopes`，以及可空的 `expires_in_days` |
| `update_api_key` | `key_id` 和 `patch`                            |
| `rotate_api_key` | `key_id`                                       |
| `revoke_api_key` | `key_id`                                       |

名称为 1–60 个字符，scope 为字符串数组。有效期天数为 1–3650，空值表示永久有效。PATCH 的字段省略表示不变，`expires_in_days: null` 清除有效期；名称和 scope 不能显式为 null。已过期但未撤销的 key 仍可管理，已撤销或其他 owner 的目标返回 `key_not_found`。

成功响应包含 `schema`、`ok: true`、匹配的 `operation`／`operation_id`、经过当前围栏确认的 `principal` 和 `data`：

| 操作       | data                                                 |
| ---------- | ---------------------------------------------------- |
| 列表       | `items` 数组，包含已撤销 key，按创建时间和 UUID 倒序 |
| 创建／轮换 | `key` 元数据和 `plaintext`                           |
| 修改       | `key` 元数据                                         |
| 撤销       | `key_id` 和 `revoked: true`                          |

key 元数据只有 `id`、`name`、`key_prefix`、`scopes`、`expires_at`、`last_used_at`、`revoked_at`、`created_at`；没有哈希。时间带时区。主体代次是精确的有符号 64 位整数，其他语言不能用会舍入该值的 JSON 数字解码器。

失败响应是同一 schema 下的 `ok: false, error: {code}`。认证失败沿用准入错误；命令另有 `key_not_found`、`key_busy`、`operation_conflict`。未知字段、错版本或错请求范围响应均不能用于确认操作。服务不可用返回 503，不切回其他写入者。请求体有界，错误不会回显用户 token。

## 重放与当前权限

调用方在第一次发送前固定 operation UUID 和 payload。写入和冻结回执在同一事务中提交，相同请求可在响应丢失或进程重启后重放；换输入复用 UUID 会冲突。列表始终读取当前状态。

每次重放仍需当前有效凭据和活跃账号。用 key 轮换自身后，旧明文已经失效，须使用该账号的其他有效凭据重放。冻结结果不会重做修改，也不会恢复已撤销或再次轮换的 key；返回历史签发明文不意味着它当前还能认证。

明文由身份服务按冻结算法重新派生，只有 bcrypt 哈希进入 key 行，回执不存明文。更换身份签名 secret 后，既有 key 仍可按原哈希认证；旧签发操作的派生标识不符时冲突，不返回另一把明文。公开用户 POST 保持原有单次请求行为，调用方不能盲目重试。

## 部署与数据权限

准入使用原有四函数 LOGIN；命令另用只有 `key_policy` 和 `execute_key_command` 的 LOGIN。两个连接使用同一 Identity 数据库中的所有者能力，不能赋予直接表权限或角色成员关系。缺少命令 DSN 的运行时只提供准入，命令返回 503。

会话另用三函数 LOGIN：`session_policy`、`read_login_candidate` 和 `execute_session_command`。它与准入、key LOGIN 分开，不持有身份表或 Audit 表权限。独立 Redis 身份只允许 `login_failed:*` 的 GET／INCRBY／EXPIRE／DEL 及必要连接命令，不能访问业务缓存、broker 或 Pub/Sub。

变量、精确授权、独立镜像和本地验收入口见仓库 `DEV.md`。设计及 secret 轮换限制见 [ADR-0116](/dev/adr/0116-independent-identity-key-commands) 和 [ADR-0117](/dev/adr/0117-independent-identity-session-commands)。这些协议覆盖准入、API key 和核心会话；完整 Identity 和其他模块的部署状态见[模块台账](/dev/concepts/module-deployment)。

## 核心会话请求

会话使用 `aap-identity-sessions/v1`，请求字段固定为 `schema`、`operation`、`operation_id`、`token`、`payload`、`audit_context`、`user_agent`。审计上下文为 `method`、`path`、`ip`、`request_id` 四个可空字段；它用于归因，不代替操作 UUID。

| operation    | token                        | payload                                              |
| ------------ | ---------------------------- | ---------------------------------------------------- |
| `login`      | null                         | `email`、`password`、`captcha_verified`、`client_ip` |
| `refresh`    | 当前 JWT，可在七天宽限内过期 | `{}`                                                 |
| `logout`     | 当前有效 JWT                 | `{}`                                                 |
| `logout_all` | 当前有效 JWT 或 API key      | `{}`                                                 |

API 保留通用限流和 Turnstile 验证，提供方重新检查失败计数阈值。只有受信任边缘能提交 `captcha_verified`；用户不能直接调用此私有接口。密码按原输入验证，先判断密码，再检查账号启用和邮箱验证 gate。失败计数保留首次创建时设置窗口、成功清零及 Redis 故障时放行的既有行为；计数与 SQL 事务分别提交。

成功响应含 `schema`、`ok`、匹配的 `operation`／`operation_id`、`principal` 和 `data`。`data` 只有 `claims` 与 `access_token`，登出时两者均为 null。签发声明固定为 `sub`、`role`、整数 `exp`、`jti`、精确 64 位 `gen` 和 `iss`；JTI 等于操作 UUID。原始 JWT 只返回给调用方，不进入操作回执。刷新不撤销旧 JWT；全端登出只推进 JWT 代次，API key 保持有效。

失败响应固定为 `ok: false, error: {code, failed_count}`。错误密码与验证码挑战包含非负失败次数，其他错误次数为 null。除准入错误外，还包括 `invalid_credentials`、`captcha_required`、`email_not_verified`、`invalid_token`、`malformed_token`、`account_busy` 和 `operation_conflict`。公开适配器保留原状态、错误代码和计数头；账号更新竞争 `account_busy` 返回 409，凭据／操作锁的 `credential_busy` 返回 503。刷新仅在无法解码 JWT 的 `invalid_token` 情况携带 Bearer challenge；已解码的畸形声明、宽限到期、撤销和账号状态失败保持原错误语义。

成功命令的状态、JTI／代次、业务审计和冻结声明回执共同提交。错误密码、停用或未验证的登录也会提交其经过验证的业务审计；格式错误或绑定错误的提供方响应不用于确认事务。响应丢失后重放要求当前认证，另一个有效凭据可用于恢复同一账号的冻结结果。重放不会再次登出新凭据、推进代次、记录业务审计或刷新旧声明；历史 token 可能已经失效。登录重放重新检查当前密码和 gate，账号角色取当前资料，签发声明保持冻结。

提供方的签发时长、邮箱验证、失败计数阈值／窗口和 Redis 实例必须与边缘及剩余签发端匹配。修改签发策略或 signing secret 后，旧操作回执冲突。原生策略固定 issuer、HS256 和七天宽限；含会话回执时拒绝降级。分组目录／指派与完整 invitation lifecycle 采用独立 Identity owners；Directory 已迁移四项用户只读操作，平台角色预览／变更也已通过本地限定资格；这两类切片都不覆盖其他用户 hydration 或 cross-owner offboarding。整体日常签名能力尚未撤除。

为保证重放字节稳定，JWT header 使用紧凑 JSON `alg`、`typ`，payload 字段顺序固定为 `sub`、`role`、`exp`、`jti`、`gen`、`iss`。UUID 为小写规范形式，代次和时间为十进制整数，Base64URL 不带填充。HS256 key 是 `HMAC-SHA256(secret UTF-8, issuer UTF-8)` 的小写 hex 字符串，再按 UTF-8 作为 HMAC key。会话派生 master 使用相同 secret 与独立域 `aap-identity-sessions/v1`；其 SHA-256 是派生标识，登录指纹是该 master 对按键排序、紧凑且非 ASCII 以 Unicode 转义表示的 `{email,password}` JSON 的 HMAC-SHA256。仓库的 `apps/identity/tests/fixtures/session_vectors.json` 提供公开测试向量，不含部署凭据；Python 和 Node 都核对相同结果。这是跨语言原语／序列化证据，尚非另一语言的完整提供方验收。

## 自助账户命令

请求使用 `aap-identity-accounts/v1`，字段固定为 `schema`、`operation`、`operation_id`、`token`、`audit_context`、`payload`。审计上下文与会话相同。用户身份从当前 JWT 或 API key 解析，payload 不能指定另一个用户。既有 API key 自助访问策略保持不变。

| operation           | payload                                                          |
| ------------------- | ---------------------------------------------------------------- |
| `read_self_account` | `{}`，返回公开 `UserOut` 资料投影，包含原始偏好和头像引用        |
| `update_name`       | `name`，1–100 字符；去除首尾空白后不能为空                       |
| `get_preferences`   | `{}`，返回归一化偏好，保留 opaque 工作区／快捷键                 |
| `patch_preferences` | `patch` 对象，保留深合并、原子 map、revision CAS 和版本保护      |
| `heartbeat`         | `{}`，以数据库时间更新在线状态，不创建轮询回执                   |
| `change_password`   | `old_password`、`new_password`，保持既有长度／强度与当前会话策略 |
| `set_avatar_ref`    | `avatar_ref`、`kind`、`bytes`；上传引用只能由受信任字节入口生成  |

成功响应含 `schema`、`ok: true`、匹配的 `operation`／`operation_id`、当前围栏主体和 `data`。data 固定为 `account`、`preferences`、`previous_avatar_ref`、`changed` 四字段：资料读取／改名／头像返回 account，偏好操作返回 preferences，心跳／改密均为 null。改名和头像的 changed 为布尔值；头像只有实际替换才有旧引用，no-op 的旧引用为 null，不能误删当前对象。资料没有密码 hash 或额外凭据字段。

失败使用 `ok: false, error: {code,status_code,detail}`。普通准入错误、`account_busy` 和 `operation_conflict` 的后两字段为 null；`policy_rejected` 保留原有 400／409／422 与安全详情。校验诊断只含 `type`、`loc`、`msg`，没有 input／ctx；错误消息不会回显密码。账户竞争仍为公开 409，能力不可用为 503。形状错误、主体／操作错配或非有限数字不能确认提交。

所有者在一条连接上先准备并锁定 User，再检查当前 JTI／代次／有效期或 API key hash，运行完整偏好策略或 bcrypt，最后重新检查并提交。偏好等待最新行后合并；其他写入保留非等待竞争。名称／偏好／密码／头像修改的成功回执与状态、业务审计共同提交。名称／头像 no-op 不重复业务审计，自助改密清除管理员重置标记，仍不推进 JWT 代次或撤销 API key。

私有调用方固定操作 UUID 与输入后，可在丢响应、重启或恢复数据库后取回冻结结果。重放需要当前有效凭据，不能用已过期／撤销的凭据读取回执；可用同一账号的其他有效凭据。它不重新检查已失效的旧密码、不重复修改或审计、不回退当前偏好和头像。冻结 account 的名称／角色／邮箱可能早于当前 principal。HMAC 指纹、epoch 和保留回执的降级规则见 [ADR-0118](/dev/adr/0118-independent-identity-account-commands)。公开接口没有新增自动重试或幂等 header。

账户角色只有 `account_policy`、`prepare_account_command`、`apply_account_command` 三函数；扫描角色只有 `mark_inactive_accounts_offline`。扫描请求是 `{schema:"aap-identity-presence/v1",cutoff:"带时区时间"}`，成功响应为相同 schema 和严格非负 64 位 affected。它保留 `online AND (last_seen_at IS NULL OR last_seen_at < cutoff)`，不新增 active 过滤。就绪检查只查询权限，不执行扫描。

账户偏好保留原有大小和基数集合，不套用会话的 64 KiB 请求或准入的 16 MiB 响应上限；并发、截止时间及部署资源仍是实际运行边界。字节处理不持远程 SQL 锁。确定拒绝时清理新头像暂存对象，提交未知时保留字节并先核对资料／回执；旧对象只在确认提交后尽力清理。平台角色／管理员写入、其他用户 hydration 及多 owner lifecycle 继续按[模块台账](/dev/concepts/module-deployment)推进。

## 分组目录与指派

`POST /v1/identity/groups/execute` 使用 `aap-identity-groups/v1`，字段固定为 `schema`、`operation`、`operation_id`、`token`、`payload`。`operation_id` 是预先固定的 UUID；`token` 是非空且最多 16,384 字符的当前 JWT 或 API key，并作为 secret 处理。身份与平台角色由 Identity 重新解析，不能由请求传入权限布尔值。Native 协议用当前 `actor`、仅对 API key 出现的 `expected_key_hash`、请求指纹与派生 epoch 替代原 bearer，不把原 token 放入数据库命令。

| operation                   | payload                                                             | data                                       |
| --------------------------- | ------------------------------------------------------------------- | ------------------------------------------ |
| `list_groups`               | empty object                                                        | `{items: GroupRecord[]}`                   |
| `query_groups`              | `page` ≥ 1; `page_size` 1–500; nullable `search`                    | `{items,total,page,page_size,pages}`       |
| `create_group`              | `name` 1–100; nullable `description` up to 500                      | `{group: GroupRecord}`                     |
| `update_group`              | `group_id`; nullable `name` 1–100; nullable `description` up to 500 | `{group: GroupRecord}`                     |
| `delete_group`              | `group_id`                                                          | `{group_id,deleted:true}`                  |
| `preview_group_assignments` | 1–500 `user_ids`; nullable `group_id`                               | Existing `BulkGroupAssignmentPreview` DTO  |
| `apply_group_assignments`   | 1–500 `user_ids`; nullable `group_id`                               | Existing `BulkGroupAssignmentResponse` DTO |
| `assign_user_group`         | `user_id`; nullable `group_id`                                      | `{account: AccountRecord}`                 |

`GroupRecord` 固定为 `{id,name,description,member_count,created_at}`；`created_at` 必须带时区，`member_count` 只计算启用账号。目录按 name、UUID 顺序返回；既有未分页 `list_groups` 不增加新的固定结果大小上限。查询页码从 1 开始，page size 为 1–500；`search` 以 Python `str.strip()` 规则修剪后，在 name 和 description 上执行 ILIKE，保留现有无长度上限的查询行为。总页数为 `ceil(total/page_size)`，空结果为 0。

`create_group` 的 ID 固定为 `operation_id`。Group name 限 1–100 个字符并使用同一 Python Unicode trim；description 可为 null，最长 500 个字符。`update_group` 中 name 或 description 省略与 null 都表示不改；非 null description 会写入并审计，空字符串清除描述。只传入未变化的 name 不生成 Audit；创建的空白 name 返回 400 `名称不能为空`，重复名返回 409 `已存在同名数据组: ` 加组名（create 使用 trim 后名称，update 保留旧 API 的原始名称），更新去重和所有权拒绝不泄漏 SQL 文本。

一次批量指派接受 1–500 个 user UUID；重复值与输入顺序保留，不新增统一 response byte ceiling。超级管理员可管理全局目录并指派任意目标。项目管理员也可管理全局目录；其指派范围是自己、当前任一所属项目成员，或任意启用 employee。批量操作还兼容其所属项目中的停用成员；单用户指派要求目标启用。范围外目标与不存在目标保持相同隐私响应。批量结果使用逐项状态，允许合法项目成员部分成功：请求的 group 不存在时，apply 返回顶层 404，preview 则为对应合格项返回错误；单用户指派的 AccountRecord.id 必须等于请求的 user_id。

成功 envelope 包含 `schema`、`ok:true`、匹配的 `operation`／`operation_id`、当前 `principal` 和 `data`。失败 envelope 固定为 `{schema,ok:false,error:{code,status_code,detail}}`；native 非策略错误的后两项为 null，API 只映射已知安全错误，不传 SQL 诊断。新增错误为 `group_not_found`、`target_not_found`、`group_busy`、`account_busy`、`operation_conflict` 和 `policy_rejected`；找不到分组映射 HTTP 404「数据组不存在」，不可见或不存在的用户统一为 404「用户不存在」，Group/User 锁冲突映射 409 `{reason:group_busy|account_busy}`，操作范围冲突映射 409 `{reason:operation_conflict}`。逐项失败带安全旧消息和 `retryable:true`。策略错误只允许管理权撤销 403「当前账号已无管理权限」、空白名称 400「名称不能为空」及重名 409。重放使用 operation UUID 和请求 HMAC 取回冻结结果，但每次都要求当前 manager 身份；单用户指派重放还会重新检查当前目标可见范围。

Create、update、delete、single assignment 与 bulk apply 都保存成功操作回执；bulk apply 即使每项失败仍冻结其已接受结果。List、query 和 preview 不建回执。回执与 Group/User 写入及 typed Audit 一起提交；同一操作 ID 不能用于不同 actor、操作或 payload。Catalog rename/delete 会按 UUID 顺序预锁所有受影响 User；用户撤权或 Workspace 归属变化不能越过持有的范围锁。

`identity_group_operation_receipts` 保存 operation UUID、actor、operation、request HMAC epoch 与 frozen result，不带 live-User 外键。尝试再次使用同一个 ID 的不同用户／操作／请求会冲突；失败策略不落回执。HMAC 以 Identity secret 的 `aap-identity-groups/v1` 专用 domain 派生。Native Audit 写入使用 `record_identity_group_event(text,jsonb,jsonb,jsonb)`：仅接受 `group.create`、`group.update`、`group.delete`、`user.group_change`、`user.bulk_group_assign`，输入包含显式 `target_type`、`target_id` 和封闭 `detail`，只保存协议允许的业务 detail。无 Audit 表或泛化写函数授权。

Identity 的 GROUP LOGIN 只有 `group_policy()` 与 `execute_group_command(jsonb)`，无直接 Group/User/Project/ProjectMember 表权限。Project assignment scope 由 Workspace 的 `lock_group_assignment_scope(uuid,uuid[],boolean)` 投影；Workspace 按 Project、ProjectMember 顺序读取／锁定，写入围栏持有至调用方提交。Group 修改先锁目标 Group，再锁 actor/target User；改名/删除按 UUID 顺序预锁受影响 User。Identity 不复制 Project scope，也不从 API 布尔值授权。

API 可设置 `IDENTITY_GROUP_DATABASE_URL` 或 `IDENTITY_GROUP_DATABASE_URL_FILE`；独立 Identity runtime 读取同名的 `IDENTITY_GROUP_DATABASE_URL(_FILE)` 配置。省略 DSN 时使用内嵌调用方事务；显式配置的 DSN 失败时返回不可用，不切换到其他写入者。Compose 用宿主机 `IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE` 选择文件，并在容器内挂载为 `IDENTITY_GROUP_DATABASE_URL_FILE`。专用 DSN 必须指向同一个 PostgreSQL 数据库，由它承载 Workspace scope function 和 Identity group schema。

此前 group slice 只通过私有 `resolve_invitation_group(uuid,text)` helper 兼容旧邀请 coordinator；该 helper 仍由 Identity 内部调用，不公开为操作，也没有授予 GROUP LOGIN。现在完整邀请 lifecycle 使用独立的 `aap-identity-invitations/v1` owner；它保留 Public HTTP 与 workspace membership 边界，详见下一节及 [ADR-0122](/dev/adr/0122-independent-identity-invitations-and-mail-v2)。分组的查询/指派协议与当前 scope 仍见 [ADR-0120](/dev/adr/0120-independent-identity-group-catalog-and-assignment)。

## 邀请生命周期

现有创建、批量预览／执行、查询、统计、CSV/JSON 导出、撤销、换新链接、邮件发送、链接解析、首次注册和已有账号接受共 13 项操作统一使用私有 `POST /v1/identity/invitations/execute` 与 `aap-identity-invitations/v1`。Envelope 固定为 `schema`、`operation`、`operation_id`、`token`、`audit_context`、`payload`。`token` 是 manager 或 accept 调用者的当前 JWT/API key；匿名 resolve/register 时必须为 null。邀请链接中的证明使用 `payload.invitation_token`，不能与调用者 token 混淆。密码、token、明文链接和密文在模型 repr／日志中隐藏；只在既有成功响应里保留允许的 token/link 字段，失败永不回显凭据或数据库诊断。

| operation                  | 用途与重要保真规则                                                                                                     |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `create_invitation`        | 单人创建；按现有 Python validator 修剪并小写 email，trim group name，空值转 null。                                     |
| `preview_invitation_batch` | 最多 500 项，执行完整外层 rollback；不返回邀请 token/link，不留下配额、Audit、替换行或回执。                           |
| `apply_invitation_batch`   | 保留有序逐项结果与成功／失败计数；成功行用 savepoint 提交，逐项身份稳定可重放。                                        |
| `list_invitations`         | 兼容旧数组读取，项目管理员仅自己的邀请且不能请求 `scope=all`；超级管理员可读全局。                                     |
| `query_invitations`        | 最多 500 行分页，按 `created_at/id` 倒序；保留包含式日期过滤和现有 ILIKE wildcard 行为。                               |
| `invitation_stats`         | 使用同一权限范围和过滤条件，总计状态按 accepted、revoked、expired、pending 顺序协调。                                  |
| `export_invitations`       | owner 选择记录、计数并记 `invitation.export` Audit；CSV/JSON 编码由 HTTP 表示层负责，无新行数或字节上限。              |
| `revoke_invitation`        | 已 revoked 时幂等成功；accepted invitation 不可撤销。                                                                  |
| `resend_invitation`        | 明确轮换 token，恢复过期／已撤销记录；accepted invitation 不可重发，且此操作本身不发邮件。                             |
| `send_invitation_email`    | 为当前链接建立 durable send intent；不轮换 token、更新有效期或改变邀请状态。                                           |
| `resolve_invitation`       | 匿名 proof 解析公开注册链接；无当前 manager token，不泄漏 SQL/SMTP 诊断。                                              |
| `register_invitation`      | 使用邀请 proof 新建 active/online、已验证账号；User/Group/Member、proof 消费、Audit 同事务提交，JWT 由 Identity 签发。 |
| `accept_invitation`        | 已有账号使用同邮箱、当前有效 user credential 加入项目；不修改平台角色，不静默合并账号。                                |

平台角色与项目角色独立校验。项目管理员只能邀请 `employee`／`viewer`，且只能管理自己创建的邀请行；不能使用 `scope=all`。超级管理员可管理所有行，只有当前超级管理员能签发 `project_admin`／`super_admin` 邀请。新项目邀请只允许 `annotator`、`reviewer`、`viewer`；employee 可持有三种项目角色，platform viewer 仅可持 viewer。管理员账号不能通过邀请获得项目成员行，已有账号接受时不改平台角色。

Create/resolve/accept/send 均重新确认 inviter 是当前启用管理者；有权限级别的受邀角色要求签发人当前为超级管理员。目标项目仍须存在并有启用的 manager owner；project_admin 还必须是当前 owner。保持无效链接 404 与已用／撤销／过期／issuer/project 不再有效的 410 区别。注册和接受都要求匹配当前登录邮箱及兼容项目角色；account-only invitation 和重复成员接受继续返回 409。

单人 email/group 规则保留现有 validator；批量单项长度校验仍较宽（email/group 最多 1000、role/project role 最多 100），多项上限 500。预览时逐项拒绝错误记录和重复成功 email，不把单行错误提升为整批失败。Apply 在一笔外层事务里提交每个成功 savepoint，返回原 `index/email/ok/retryable/error` 结果及成功／失败数。列表不加总行 cap；query 和 stats/export 使用相同当前权限与筛选，不加固定响应字节或 profile URL 限制。

Per-inviter daily quota 通过锁住 issuer 后数滚动 24 小时窗口；默认 `max_invitations_per_day=30`、`invitation_ttl_days=7`，policy 允许 quota 1–1000、TTL 1–90。非法 deployment policy fail closed，TTL 沿用已记录的默认 fallback。create 只令同 email 的未接受前序邀请过期：project_admin 只更新自己创建的，super_admin 可全局更新。Group value trim 使用 Python Unicode 规则，长度不超过 100。

Native schema `aap_identity_invitations_v1` 在 migration 0242 安装。`identity_invitation_client` LOGIN 精确只有 `invitation_policy()`、`prepare_invitation_command(jsonb)`、`apply_invitation_command(jsonb)`，没有 AUTH/GROUP/MAIL、foreign helper schema、表／列／序列或 DDL 权限。Prepare 收集当前 proof/facts 后释放 SQL 锁；注册 bcrypt、JWT 签名和加密都在 SQL 锁外做；Apply 在提交前重新校验全部用户／邀请／项目／凭据围栏，并验证 native 与投影 wire reply。

参与的 User authority 先按 UUID 排序锁定，再取 email advisory key `hashtextextended(email,0)`，随后才锁 Project，最后 Invitation/ProjectMember；Group helper 在相同 Identity owner 内继续执行。先由 Workspace 捕获 project id/name/owner，再取相关 User 锁并重读/锁定捕获的项目 owner；迁移 owner 会导致安全可重试 409，不能使用过期授权布尔值。Project membership 与 User/Group/invitation/Audit 状态在一个 PostgreSQL transaction 原子提交；Labeling 只提供当前活动批次数作为 work hint，不创建或分配任务。

成功变更回执以 actor（匿名 operation 可为 null）、operation、UUID、HMAC fingerprint 和 derivation epoch 绑定，无 live User/Invitation FK，不复制第二份明文 proof。Replay 重查当前凭据和行管理范围，不修改新行、不重复 Audit；注册 replay 在 generation 撤销后不能签出可用凭据。邀请 Audit 固定事件为 `user.invite`、`user.bulk_invite`、`user.invite_revoke`、`user.invite_resend`、`user.register`、`user.invite_accept_existing`、`invitation.export`、`invitation.send_email`，维持旧 target/status/detail 形状，不包含密码、token、链接或 Fernet ciphertext。唯一的组解析 helper `resolve_invitation_group(uuid,text)` 仍是 Identity 私有调用，不授予 GROUP LOGIN。

## 邀请邮件与 MailV2

`POST /api/v1/invitations/{id}/send-email` 保持现有 200 成功和 502 失败，发送当前已存在的链接；错误不轮换、撤销或作废邀请，不改为公开 202。Invitation owner 先 durable-accept intent，再由 HTTP gateway 对同一个 operation UUID 短轮询 private `mail_status=pending|sent|failed`。默认 bounded wait 60 秒，最多 120 秒；达到时限仍 pending/unknown 时返回兼容 502，但 intent 仍可恢复并可能稍后发出。用户仍可重复点击当前邀请的邮件操作；SMTP outcome 未知时 at-least-once 允许同一 Message-ID 的重复邮件。永久 SMTP rejection 只终止这条 delivery intent，邀请 token 和链接仍有效。

不要把 MailV2 加进恢复邮件 v1 的 closed schema。V1 继续接受 recovery 的两种 purpose，`claim_mail` 必须过滤 invitation rows，避免未知 purpose 阻塞旧 worker。V2 使用 `aap-identity-mail-native/v2`、policy `aap-identity-mail-policy/v2` 和 wire `aap-identity-mail/v2`；`identity_mail_v2_client` 精确拥有 `mail_policy()`、`claim_mail(jsonb)`、`renew_mail(jsonb)`、`finish_mail(jsonb)` 四个函数。V2 MailExecutor 处理 reset、verification 和 invitation 三个 purpose；同一 `identity_credential_mail_outbox` 延展 `invitation_id` 及封闭 invitation context。邀请行的 `user_id` 是 issuer 而非 recipient User，typed `InvitationMailV2Lease` 同时显式给 `issuer_id`、`invitation_id`、recipient/project values，不能当作收件人身份授权。

V2 claim／renew／finish 在发送前后重新校验当前 invitation token hash、有效期、接受／撤销／superseded 状态以及 inviter/project scope。SMTP 网络期间不持 SQL 锁；claim 先提交 lease，current scope 在 claim/renew boundary 围住发送。已经在 SMTP 中的消息无法撤回，因此重发、接受或撤销与正在发送邮件交错时仍按至少一次语义处理。所有目的共用现有 Fernet epoch/key，V2 不存 plaintext token/link 副本，也不回退到当前 key 或新 token；旧 V1 recovery outbox、roles、schemas 与 worker 保持可用。

独立 sender 运行 `python -m aap_identity_runtime.mail_worker_v2`，同一 `MailExecutor` 重用已提取的 SMTP transport；该进程只持 MAILV2 DSN、MAIL_SETTINGS DSN、Fernet key 与 SMTP fallback，不带 Identity/AUTH/control/signing secret，不使用 User/Invitation SQL 事务来发送。`mail_policy/claim/renew/finish` 的短事务在 SMTP 网络调用前后提交，不持 SQL lock。Claims 保存同一 message ID；unknown timeout 允许重复邮件而不损坏邀请链接。邮件 Settings DB snapshot 覆盖 env fallback。

Invitation API 使用可选 `IDENTITY_INVITATION_DATABASE_URL(_FILE)` 和 `IDENTITY_MAIL_V2_DATABASE_URL(_FILE)`；独立 Identity runtime 选择 `IDENTITY_INVITATION_DATABASE_URL(_FILE)`，并复用既有 recovery Fernet key。Invitation DSN 缺失／显式失败分别按 capability 503／fail closed。API embedded MailV2 kick 可选 MAILV2 DSN；选择邀请 owner 但未选择 mail kick 时允许独立 mail-v2 服务消费，绝不 fallback 到 caller SQL。API mail wait 配置 `IDENTITY_INVITATION_MAIL_TIMEOUT_SECONDS` 默认60、必须 >0 且 ≤120。Runtime invitation link fallback 为独立 `IDENTITY_INVITATION_FRONTEND_BASE_URL` 默认 `http://localhost:5173`; Settings 的 system `frontend_base_url` 若设置则覆盖，Recovery Runtime 原 `http://localhost:3000` fallback 不变。

可选 Compose `--profile mail-v2` 启动 `identity-mail-v2`：它使用 `IDENTITY_RUNTIME_MAIL_V2_DATABASE_URL_FILE` 新文件；既有 `IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE` 和 `IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE` 分别映射到 V2 `IDENTITY_MAIL_V2_SETTINGS_DATABASE_URL_FILE`、`IDENTITY_MAIL_V2_ENCRYPTION_KEY_FILE`；原宿主机 `IDENTITY_MAIL_SMTP_*` fallback 映射到 `IDENTITY_MAIL_V2_SMTP_*`。身份、Settings、key 和 SMTP 只授予 sender；`identity-admission` 没有 SMTP fallback credentials。V1 `identity-mail` 服务/LOGIN 仍有效并只领取 recovery intents。迁移 0242 先加载 `0242_identity_invitations_support.sql`，再 Mail 的 `0242_invitation_mail.sql`，最后 `0242_identity_invitations.sql`；grant 入口见 [DEV.md](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md) 和 [ADR-0122](/dev/adr/0122-independent-identity-invitations-and-mail-v2)。

Invitation/MailV2、Directory reads 和 batched UserBrief reads 均有各自限定的本地资格；上文分别限定了 Directory 38 个选定数据库／进程案例与 UserBrief 64 个 selected cases，邀请的 68/375/324 数字只属于旧 slice。平台角色管理和管理员 offboarding preview/commit 也通过各自限定的本地资格；其他用户 hydration、其余 Identity lifecycle、整个 Identity、另一语言 provider 和生产容量仍待完成。`v1` 恢复邮件保持原协议；没有新的 Notifications event、Jobs backfill、生产切换或 Go 扩展。

## 用户目录读取

本目录 slice 覆盖四个既有只读 HTTP operation：`GET /api/v1/users`、`/users/query`、`/users/stats` 和 `/users/export`。公开 paths、filters、defaults、DTO、status、CSV/JSON 和 SDK REST 调用保持不变。Identity 私有 `POST /v1/identity/directory/execute` 使用 `aap-identity-directory/v1`，body 精确包含 `schema`、`operation`、`operation_id`、`token`、`audit_context`、`payload` 六项。`token` 是当前非空 JWT/API-key credential，不是 actor 或 public User assertion；请求追踪继续使用 Session 的 method/path/ip/request_id context。

| operation      | payload 与 defaults                                                               | data                                                                    |
| -------------- | --------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| `list_users`   | `role=null`、`status=active`、`project_id=null`                                   | `{users: DirectoryUserRecord[]}`；公开 API 继续返回数组。               |
| `query_users`  | `page=1`、`page_size=50`、`status=active`、`project_id/group_id/role/search=null` | `{items,total,page,page_size,pages}`；每项加两项管理 flags。            |
| `user_stats`   | `status=active`、`project_id/group_id/role/search=null`，与 query 共用 filters    | `{total,online,weekly_active}`。                                        |
| `export_users` | 同一组 filters，另有 `format=csv`（或 `json`）                                    | `{users: DirectoryExportRecord[],exported_by,exported_at,request_id}`。 |

Wire success 精确是 `{schema,ok:true,operation,operation_id,principal,data}`；failure 使用闭合 `error={code,status_code,detail}`。Native success 在同一 envelope 上另带 strict boolean `replayed`。Query 的 count/page/page_size/pages 是精确 signed int64，`pages=ceil(total/page_size)` 且仅在 total=0 时为 0；页内 flags 是严格 boolean。

不要把 legacy list 和 query/stats/export 合并成同一角色范围。Super-admin 的 `list_users` 返回匹配 status/role 的全部用户；传 `project_id` 时和所选项目成员取交集。Project-admin 只有在 `role=employee AND status=active` 时可看全局 active employee 候选，否则只看 self 与其自有项目成员，且 legacy `project_id` 输入被忽略。Legacy list 仅按 `created_at DESC` 排序，不新增 tie-order 保证。

Query、stats、export 无 `project_id` 时，super-admin 看全部匹配用户；project-admin 看 self、自有项目成员、所有 active employee 和所有 active super-admin。额外的 active super-admin 只是只读联系人，除非该用户也满足现有 own-member 条件。带 `project_id` 时，super-admin 与选中项目成员取交集；project-admin 将 self／自有项目成员集合与选定项目成员取交集，不带 global employee 或 super-admin arm。保留当前字面范围：这个交集不额外要求所选项目归该 project-admin 所有，所以同时是其 self/own-member、且加入被筛外国项目的用户仍可能出现。

`status` 允许 `active/inactive/all`，默认 `active`；`role` 不做 trim 或小写转换，也不增加任意短长度上限。非法或旧平台角色返回业务 400，detail 仍是 `非法平台角色: {submitted role}`，而不是输入 schema 422；该拒绝绑定实际 role 值。`search` 最长 255 字符，按 Python `str.strip()` 修剪，保留 email/name 的 ILIKE wildcard 行为；`group_id` 精确匹配。Query `page` 是正 signed int64，默认 1；`page_size` 是 1–500，默认 50，native offset 必须避免溢出。Query/export 按 `created_at DESC, id DESC` 排序。List 与 export 没有新增总行或响应字节上限。统计为 `total/online/weekly_active` 三个非负 int64，其中后两项不得超过 total；7 天 cutoff 含等号，并与读取使用同一 snapshot。

每个用户 record 精确保留现有 `UserOut`／`AccountRecord` 的 21 个字段：`id/email/name/role/group_name/group_id/status/is_active/disabled_kind/disabled_at/disabled_by/disabled_reason/email_verified_at/last_login_at/last_seen_at/password_admin_reset_at/deactivation_requested_at/deactivation_scheduled_at/preferences/avatar_ref/created_at`。role/status 是原始 string，preferences 是 opaque JSON，不筛未知 keys；跨语言 Node consumer 必须保留输入数值 token 与宽整数，不能转成 JavaScript `Number` 后丢精度。后端沿用原 Python/JSON canonicalization，例如 20 位十进制 fixture 仍按既有行为规范为 `0.12345678901234568`；这不是新的后端十进制精度保证。UUID、datetime、boolean 保持类型；密码、credential hash、generation 和 API key 不进入输出。Query item 只新增 `is_managed`、`is_lifecycle_managed` 两个严格 boolean。Super-admin 两者为 true；project-admin 的 `is_managed` 表示 self、自有项目成员或 active employee，`is_lifecycle_managed` 还要求用户的派生项目均由当前 actor 管理。Derived projects 包括目标拥有项目、所有 memberships、TaskBatch annotator/reviewer、Task assignee/reviewer 和 TaskLock→Task；历史、停用、archived rows 仍参与，孤儿引用保留旧 inner-join Project 语义。

Scope flags 来自窄批量 owner projections：Workspace 的 `identity_directory_owned_member_ids(uuid)`、`identity_directory_project_member_ids(uuid)` 与 `identity_directory_lifecycle_blocked(uuid,uuid[])`，以及 Labeling 的 `identity_directory_project_refs(uuid[])`（最多 500 个用户）。Workspace 只获得这项 Labeling capability；Identity 不读 Workspace/Labeling 原始表。Selected rows、total 与 flags 使用同一 SQL snapshot，不能改为逐用户 RPC。Audit 只提供 `record_identity_directory_export(...)` 这一封闭 typed user-export capability。

Export row 精确包含 `id/email/name/role/group_name/group_id/status/created_at`。CSV 保留 8 列、UTF-8 BOM、formula escaping、ISO timestamp、media type 和原 attachment filename；JSON `_export_meta` 保留 `exported_by`、带时区 `exported_at`、`request_id` 和 count。新 export 以 status 200 写唯一 `user.export` Audit，`target_type=user`、`target_id=null`，detail 保留 `format/count/actor_email/ip/request_id`。Identity successful export receipt freezes 8 个字段、metadata 和 Audit ID；重放仍需当前凭据与 manager role，并按当前 export scope 和原 `project_id` 重新核验所有冻结用户 ID。旧搜索、group、status filters 不重新应用，profile/context 变化单独不阻止返回原冻结行与 metadata；若用户已删除或不再可见，则返回 `409 operation_conflict`。receipt 没有 live User FK，也不复制整个 User/preferences。其它三个 read operation 不建 receipt。

Native protocol 是 `aap-identity-directory-native/v1`，policy schema/protocol 分别是 `aap-identity-directory-policy/v1` 和 `aap-identity-directory/v1`。Native request 精确包含 `schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload`；`actor` 来自已认证 Principal，`expected_key_hash` 仅 API-key 凭据时存在，原 token 不进入 native args 或 receipts。`identity_directory_client` LOGIN 精确只有 `directory_policy()` 和 `execute_directory_command(jsonb)`，没有 table/column/sequence/DDL 或 foreign-schema 权限；readiness 拒绝额外 grants。Native success 另带的 `replayed` 由 manager 去除，wire success 保持 `schema/ok/operation/operation_id/principal/data` 六项。失败只有闭合 `code/status_code/detail`；已知附加 code 为 `invalid_request` 400、`manager_required` 403、`invalid_platform_role` 400、`current_scope_busy` 与 `operation_conflict` 409、`unavailable` 503。未知、畸形或 actor/op/UUID 不匹配均 rollback，不泄漏服务器诊断或 fallback。

Migration 0243 创建目标 schema `aap_identity_directory_v1`。API 与 Identity runtime 通过 `IDENTITY_DIRECTORY_DATABASE_URL` 或 `IDENTITY_DIRECTORY_DATABASE_URL_FILE` 选择此函数 owner。可选 Compose host selector 是 `IDENTITY_RUNTIME_DIRECTORY_DATABASE_URL_FILE`，容器内映射为 `IDENTITY_DIRECTORY_DATABASE_URL_FILE`。selector 留空会关闭 Directory，私有路由返回 503，既有 Identity Compose service 仍能启动；只有启用 Directory 时才配置该 secret file。显式 DSN 连接失败不回退 API ORM。平台角色变更、管理员重置密码、deactivate/delete/offboarding/reactivation、bootstrap/import 和 dashboard analytics 不属于这四项读取。

Directory read slice 已通过限定的本地资格。Committed source `eeeaa16e48459a1357389723a6d99ec74e3c71d3` 对应 32 个 native/role cases、3 个 restore cases 与 3 个 live/API cases，均 0 skip；合计 38 个选定数据库／进程用例。Restore/live suite 包含邀请与 legacy challenge 兼容用例，因此不能将 38 项都称为新 Directory 测试。

安装包 398 项检查通过，82 个 installed source 文件与 hash 一致；noneditable Identity 安装不依赖 app、SQLAlchemy、Celery、Boto3 或 Torch。Node consumers 22 项自检、SDK 31 项检查通过；API assembly 348 项通过，另有 125 项 lazy architecture rules 明确延期跳过。Identity 镜像 SHA-256 `04cc403370bdccce6d84d89b6bc222997b9e4d77b976c0a547f0aff28805704e` 含这 82 个文件，实际 runtime 与 API proof 通过。

API image SHA-256 `12eadf471566c1487a5830961928232e9abf4b01728912b03fb18fa70fc60479` 在无网络、只读、512 MiB cold container 核对 82 个 package files、70 个 API/native inputs、Directory 四路由和既有 13 Inv/5 Recovery/8 Group/2 Deactivation 路由、OpenAPI 与 0243 head。Public paths/fields/statuses 不变；OpenAPI 仅 users/stats description 更新。环境文档生成核对 447 个 variables／48 sections，codegen、schema export 通过。所用 API cold-proof 容器已移除。

Live 用 540 个 synthetic users 覆盖超过 500 行与 64 KiB 的 list/export；int64 大页、生命周期引用类别、stale role 403、profile/context frozen export、no-repeat Audit、当前可见性 409、credential-generation fence 与 restart 也通过。受限 remote Identity container 的 1 CPU／256 MiB 边界只适用于该容器。另一个 12 次读取、并发 4 的 host-process sample 为 elapsed 0.232s、p50 0.077s、p95 0.081s，包含 Node process overhead；它不对应上述容器预算，也不是生产容量或 SLO。Node fixture 保留输入 JSON 数值 token 与 wide integers；20 位小数 `0.12345678901234568` 只验证既有后端 canonical form，不主张新的后端十进制精度。此前 Invitation/MailV2 的 68/375/324 counts 仍只属于该 slice。

## 内部批量 UserBrief 读取（已通过限定本地资格）

此 owner 抽取的是现有 API enrichment helper `app.services.user_brief.resolve_briefs` 与 `resolve_briefs_with_project_role`，不是可供用户任意查询的 public profile endpoint。所有调用方仍先授权自己的 task/project/result rows，再 enrichment 相关 user IDs；helper 没有 actor 参数，不使用 Directory 的 manager gate，也不改变公开 API／SDK schemas、paths、pagination 或 errors。

Wire schema 为 `aap-identity-briefs/v1`，只接受 `schema/operation/payload`。`get_user_briefs` 的 payload 为 `{user_ids:[UUID,...]}`；`get_project_user_briefs` 为 `{project_id:UUID,user_ids:[UUID,...]}`。没有 token、actor、nonce、operation ID 或 audit context；调用只由内部 control Bearer 授权。UUID string 规范化，重复与空数组合法；`None` 输入不发送，空输入本地返回 `{}`。不加 ID 数量或 JSON body 大小上限。拒绝 `protocol` alias、未知/重复 JSON keys、NaN/Infinity 以及 number/boolean/null ID。坏 control Bearer 返回 401；无效请求返回闭合的 400 envelope；disabled capability、timeout 或 owner failure 返回 503 `Identity brief reads unavailable`，不做 ORM fallback。

Wire success 的 data 是 `{project_id: UUID|null,users:[UserBrief,...]}`：全局读取的 `project_id` 必为 null，项目读取必须等于请求值；记录唯一、按 canonical UUID 字符串排序并且是请求 ID 子集，缺失用户不合成。`UserBrief` 精确为 `id/name/email/role/avatar_initial/avatar_ref`。输出包括 inactive/offline、pending retirement 与历史 role 的现存 User，不加状态筛选。项目角色取 Workspace 返回的 truthy membership role；空/缺失成员角色回退 User.role，owner/历史 assignee 无成员行时也保留 User brief。`avatar_initial` 由 Python 计算：`source=(name or email or "?").strip()`，有内容时取 `source[:1].upper()`，否则为 `"?"`。空白 name 不回退 email，不做 Unicode normalization，例如 `ß` 仍可变成多字符 `SS`。

Native schema `aap-identity-briefs-native/v1` 使用同操作与 payload，只返回 `id/name/email/role/avatar_ref` 五个 source 字段；Identity Python owner 计算 initial 并在嵌入式或远程路径都验证 owner envelope、scope、ID 子集与输出形状。Migration 0249 计划提供 `aap_identity_briefs_v1` 下的 `briefs_policy()` 与 `execute_briefs_command(jsonb)`，dedicated LOGIN `identity_briefs_client` 只获这两项函数权限。Workspace 仅暴露 `identity_brief_project_roles(project_id,user_ids)`，不授权读取 ProjectMember 原表。

API 可选 `IDENTITY_BRIEFS_DATABASE_URL(_FILE)`；两项互斥，留空时沿用现有 caller Session。已有 `IDENTITY_URL` 优先并调用 private HTTP。独立 runtime 的 DSN 也可留空，此时 briefs capability disabled；Compose host selector 为 `IDENTITY_RUNTIME_BRIEFS_DATABASE_URL_FILE`。嵌入式 SQLAlchemy adapter 永不提交或回滚调用方事务；text SQL 前只有 `autoflush=true` 且不在 `no_autoflush` 时才 flush。专用 asyncpg 绑定则拥有只读 transaction 并在每次调用后关闭连接。Root 的最新 union 通过 64 个 distinct selected cases（59 DB/process、5 pure/static），零跳过；source/installed suites 各 549 项、114 个 package hash 精确匹配，API assembly 625 项通过并延期 132 条规则，Node 21 与 SDK 43 项通过。14 个 live/restore cases 均通过，覆盖安装 owner、普通 JWT caller、task/project role projection、404 no-PII、远程 DSN 无回退与 owner restore。Migration 0249/frozen SQL/grants 及资源清理通过；Identity owner 和禁网、只读冷 API images 均已通过，Compose disabled/enabled selectors 的 configuration-only checks 通过且没有启动 deployment。冷 API image 的 41.875 秒是 build 加 offline verification，不是启动时延。准确的镜像 hashes、schema hashes、latest-case union 和 sample caveats 见 [ADR-0129](/dev/adr/0129-identity-batched-user-brief-reads)。这些数字只限定内部 UserBrief slice；整模块当前资格见[模块台账](/dev/concepts/module-deployment#identity-整模块独立装配)。生产容量、十模块／七门槛、另一语言完整 provider 与 primary cutover 仍未完成。

Projects 成员列表是该 UserBrief owner 的一个已验证消费者：`projects.list_members` 先由 Workspace 完成当前成员授权和行选择，再将全部用户 ID 一次交给全局 `resolve_briefs`。输出仍使用原十字段 DTO；UserBrief 的全局 `role` 填入必填四值 `platform_role`，membership 的 `role` 仍表示项目职责，不采用项目角色 overlay，也不输出 `avatar_initial`。空列表不会调用资料 owner；缺失用户在列表中省略，以保持旧 inner join 行为。若 `_member_out` 收到缺失目标，或历史全局角色不属于四值枚举，既有 `ValidationError` 保持不变。此调用只增加一次批量读取，没有逐用户 RPC；远程组合不保证 Workspace 与 Identity 共用数据库快照。21 个选定消费者案例（18 DB/process、3 pure/static）和 SDK 序列化 44 项通过，详情见 [ADR-0129](/dev/adr/0129-identity-batched-user-brief-reads#project-member-list-consumer)。

Project owner 展示是另一条独立的只读消费者，不参与授权判断。`projects._serialize_project` 与 `_serialize_project_list` 复用全局 `resolve_briefs`，只填充原 `ProjectOut.owner_name`／`owner_avatar_ref`；`owner_id` 仍为必需 UUID。资料缺失继续得到两个 `null`，inactive/history owner 仍展示，name/avatar 原样返回。不走项目角色 overlay，也不改公开 schema。列表和查询先按原规则授权、过滤、排序、计数及分页，再对返回页的 owner IDs 一次批量读取；共享 owner 复用结果，空页不调用 Identity，传入 `{}` 不会使单项 serializer 再查一次。创建、详情、patch、改名与转移仅对非空 owner ID 在旧授权和提交/refresh 位置查询一次。Dataset-linked `/datasets/{id}/projects` 仍保留 null owner 显示字段。旧响应基线和 59 个选定消费者案例（53 DB/process、6 pure/static）见 [ADR-0129](/dev/adr/0129-identity-batched-user-brief-reads#project-owner-display-consumer)；更新后的 API cold image、OpenAPI/codegen/catalog 核对均通过，范围仍限定于这条消费者。

## 管理员临时密码重置

本 slice 只迁移 `POST /api/v1/users/{user_id}/admin-reset-password`。公开路径、UUID 参数、每分钟 3 次限流和 `AdminResetPasswordResponse` 保持不变；成功响应为 HTTP 200，精确有 `temp_password`、`message`、`target_email` 三字段，message 固定为 `请通过安全渠道告知用户首次登录后立即修改密码`。临时密码 16 个 ASCII 字符，字符集仅 `A-Z`、`a-z`、`0-9`、`!@#$%^&*?`，并至少含一个大写、小写、数字和符号。明文按 secret 处理，不进入 repr、日志、错误或 Audit。

私有 `POST /v1/identity/admin-passwords/execute` 使用 `aap-identity-admin-passwords/v1` 的 `admin_reset_password` operation。Wire request 精确是 `schema/operation/operation_id/token/audit_context/payload`，payload 只有 `{target_user_id}`；token 是真实当前 Bearer JWT／API key，`audit_context` 保留 method/path/ip/request_id 四项 session provenance。Native request 精确有 `schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload/new_password_hash` 十项，`expected_key_hash` 是仅 API key 凭据时出现的 1–255 字符 opaque 值。Native bcrypt-12 hash 和 wire 明文均须隐藏；Native success 只返回用户 ID/email/reset timestamp 和额外的 `replayed` 标记，manager 在返回前验证两层 envelope。

| code                  | HTTP | detail                                   |
| --------------------- | ---: | ---------------------------------------- |
| `invalid_request`     |  400 | `null`                                   |
| `manager_required`    |  403 | `当前账号已无管理权限`                   |
| `target_not_found`    |  404 | `用户不存在`                             |
| `self_reset`          |  400 | `不能给自己重置密码（请用「修改密码」）` |
| `role_forbidden`      |  403 | `只能重置等级低于你的用户的密码`         |
| `target_out_of_scope` |  403 | `该用户不在你管理的项目内`               |
| `account_busy`        |  409 | `账号正在更新，请刷新后重试`             |
| `operation_conflict`  |  409 | `null`                                   |
| `unavailable`         |  503 | `null`                                   |

Malformed／unknown／duplicate／extra fields、alias 字段、secret echo 或主体/operation 绑定错误都回滚为安全的 unavailable；不接受内部 Native schema、不回显输入密码或数据库诊断，也不回退到另一 writer。

只允许 active super-admin 与 project-admin。角色等级按 super-admin=0、project-admin=1、reviewer=2、employee/历史 annotator=3、viewer=4、未知历史 target role=99 比较；target 等级必须严格大于 actor。Project-admin 对启用 employee 有全局例外（包含未分配或 foreign-project employee），其他低等级 target 必须属于其管理项目。保留历史 Project／ProjectMember 状态参与范围判断，不增加 active/archive 条件。缺失或停用 target 为 404；self 为 400。

先按 UUID 顺序 `FOR UPDATE NOWAIT` 锁 actor 与 target User，再复核 actor 当前启用状态、manager role 和 credential。Project-admin scope 随后经 Workspace 窄布尔 capability 锁定 Project（UUID 顺序）后锁 membership 并重查 owner/scope，锁持有到调用事务结束；争用 409 `account_busy`。Identity 不读 Workspace 原表，也不调用 Labeling。变更的 password hash、`password_admin_reset_at`、typed Audit 与 receipt 原子提交。Audit 只记录 `user.password_admin_reset`（status 200、target_type user、target_id UUID），detail 精确为 `target_email/target_role`；不记录密码。重置不改变 JWT generation 或 API key，self password change 仍会清 marker。

密码按 signing secret 的独立 HMAC 域 `aap-identity-admin-passwords/v1` 确定性派生，以复用私有 operation UUID 支持响应丢失后确认；每次 public invocation 都生成新 UUID，不增加公开幂等 header，也不自动 transport retry。Bcrypt-12 计算在 SQL 锁外进行。Migration 0244 的 immutable receipt 不存明文或可用 bcrypt hash，也不新增 User 列或触发器；Native 以 operation UUID 绑定的 `password_state_fingerprint` 加 `password_admin_reset_at` 共同验证当前密码状态。自改密、恢复流程或后续重置会使旧结果返回 409，避免重放已失效的临时密码；同 signing secret 的 backup/restore 可重放，epoch 变化冲突。完整派生 stream、HMAC fingerprint 与 receipt fence 见 [ADR-0124](/dev/adr/0124-independent-identity-admin-password-reset)。

Migration 0244 创建 `aap_identity_admin_passwords_v1`。`identity_admin_password_client` LOGIN 仅可执行 `admin_password_policy()` 和 `execute_admin_password_command(jsonb)`，没有 table/column/sequence/DDL 或 foreign-schema 权限。API/runtime 可选 `IDENTITY_ADMIN_PASSWORD_DATABASE_URL` 或 `IDENTITY_ADMIN_PASSWORD_DATABASE_URL_FILE`；Compose host selector 是 `IDENTITY_RUNTIME_ADMIN_PASSWORD_DATABASE_URL_FILE`。selector 留空只关闭该 capability、私有路由返回 503，不影响既有 Identity service 启动；显式 DSN 故障不回退 ORM。

管理员重置 slice 已通过本地限定验收：42 个选定数据库／进程案例全部通过且零跳过，其中 36 个 DB/restore 案例与 6 个 live 案例包含 Directory、Invitation、legacy challenge 等兼容路径，不能视为 42 个全新的 admin-reset 案例。Identity portable source 与 installed package 各通过 420 项检查；API/architecture 467 项通过、126 条 lazy rule 明确延期；SDK user/CLI/OpenAPI 35 项通过，公开 OpenAPI 保持不变。非 root、只读 Identity 镜像及禁网、只读、512 MiB API cold image 均通过，镜像哈希、输入清单和精确分类见 [ADR-0124](/dev/adr/0124-independent-identity-admin-password-reset)。一次 12 目标、并发 4 的 host-process sample 含 Node/HTTP/bcrypt 开销，不对应 Identity 容器预算，也不构成生产容量或 SLO。该 qualification 仅覆盖本地；不包括平台角色修改、deactivate/delete、offboarding 或其他账号写入，也不表示生产部署或主认证切换完成。

## 平台角色预览与变更

本 owner slice 只迁移两个超级管理员路由：`GET /api/v1/users/{user_id}/role/preview?role=...` 与 `PATCH /api/v1/users/{user_id}/role`。方法、路径、既有 `RoleImpactPreview`／用户响应、errors、SDK／CLI 行为保持不变，没有 public CAS 或幂等 header。Preview 的 `user_id` 保持 UUID、`role` 是必填且 `min_length=1` 的字符串；Mutation 的 path 参数继续是字符串，body 是 `RoleChangePayload`，extra fields 继续被忽略，非法 mutation UUID 仍返回 404。请求角色只接受 `super_admin`、`project_admin`、`employee`、`viewer`；不 trim／lowercase，也不增加任意短长度上限。非法值返回 400 `非法平台角色: {role}`，缺失／停用目标 404 `用户不存在`，修改自己 400 `不能修改自己的角色`。Current actor 必须仍是 active super-admin，否则 403 `当前账号已无管理权限`。

私有 endpoint 是 `POST /v1/identity/platform-roles/execute`，协议 `aap-identity-platform-roles/v1`，operations 为 `preview_role_change` 与 `change_platform_role`。Wire request 精确为 `schema/operation/operation_id/token/audit_context/payload`，payload 是 `{target_user_id,role}`；raw token 隐藏，actor 只能由当前凭据解析。Native request 精确有 `schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload`；API-key `expected_key_hash` 保持 opaque 1–255 字符。Native success 多一个严格 boolean `replayed`。完整 schema、错误与 receipt 约束见 [ADR-0125](/dev/adr/0125-independent-identity-platform-role-commands)。

Preview data 精确有 12 个字段：`user_id/email/current_role/requested_role/can_change/blockers/projects/assigned_batch_count/assigned_task_count/review_task_count/other_project_count/warnings`。项目行精确包含 `project_id/project_name/membership_role/annotator_batch_count/reviewer_batch_count/assigned_task_count/review_task_count`，其中 membership role 可为 null。项目是 target 的成员项目，按 project name、UUID 排序，不加 status/archive 筛选；所有状态都计入展示 counts，且只按 batch annotator/reviewer 与 Task assignee/reviewer 的直接 assignment 计数，不套用 blocker 的 COALESCE fallback。同一 batch 同时占两个角色槽位会计两次；总数只汇总成员项目，不与 viewer blocker 计数混用。`other_project_count` 对当前 super-admin-only surface 固定为 0。Warnings 固定为：

- `平台角色对所有项目生效；已有项目成员身份和批次负责人不会自动改变。`
- `若新平台角色与现有项目身份不匹配，成员可能无法继续标注或审核；请检查并重新分派。`

Work blockers 按顺序判断：目标已有非 viewer ProjectMember 且请求角色为 manager 或 viewer；目标仍拥有 Project 且请求角色为 employee/viewer；只对 viewer，未完成 task 的 effective assignee/reviewer 仍指向目标。后者仅查询 pending、in_progress、rejected、review；explicit Task assignment 覆盖 batch fallback，unbatched task 也参与，TaskBatch 用 outer join；一个 task 即使命中两个 assignment 也只阻止一次，TaskLock 不参与。Preview blocker 顺序是 self、same role、last super-admin、上述三类工作职责；same role 不计算 work blockers，active target 不会出现 `用户已停用` blocker。Preview 是 advisory，无 last-SA reservation fence；`can_change` 等于 blocker 列表为空。

三类工作 blocker 的精确文案依次为 `账号仍持有标注/质检项目职责，请先在项目中变更职责`、`账号仍是项目负责人，请先转移项目后变更平台角色`、`账号仍有未完成的标注/质检工作，请先交接`。Mutation 的 `platform_role_change_blocked` 409 按此顺序返回 blocker list。

Mutation 用共享 last-super-admin fence 先于 actor/target User 的 UUID 排序 `FOR UPDATE NOWAIT` 锁。锁后 re-entrant 检查当前 JWT 代次／有效期或 API-key 状态／hash／scope，再确认 actor 仍为 active super-admin。同角色 mutation 会立即冻结成功 no-op receipt，不取 Project 锁、不计算 blockers/last-SA，也不写 Audit。真实变更先由 Workspace 按 UUID 顺序 NOWAIT 锁定 target-owned Projects；竞争返回 409 `{reason:project_busy}`，账号／global fence 竞争返回 409 `账号正在更新，请刷新后重试`。锁定后才计算 work blockers；若有 blocker 返回 409。然后，若 target 原角色是 super-admin 且新角色不是，再检查至少有另一名 active 且未登记注销请求／计划的 super-admin；若两类条件同时触发，work-blocker 409 优先于 last-SA 的 400。ProjectMember、batch owner 与 Task 均不改写。

| code                           | HTTP | detail / error body                                    |
| ------------------------------ | ---: | ------------------------------------------------------ |
| `invalid_request`              |  400 | `null`                                                 |
| `super_admin_required`         |  403 | `当前账号已无管理权限`                                 |
| `target_not_found`             |  404 | `用户不存在`                                           |
| `invalid_platform_role`        |  400 | `非法平台角色: {rawrole}`                              |
| `self_change`                  |  400 | `不能修改自己的角色`                                   |
| `last_super_admin`             |  400 | `不能降级最后一名超级管理员`                           |
| `account_busy`                 |  409 | `账号正在更新，请刷新后重试`                           |
| `project_busy`                 |  409 | `{reason:project_busy}`                                |
| `platform_role_change_blocked` |  409 | `{reason:platform_role_change_blocked,blockers:[...]}` |
| `operation_conflict`           |  409 | `null`                                                 |
| `unavailable`                  |  503 | `null`                                                 |

Identity authentication errors keep their established codes with null status/detail. Unknown or malformed fields, duplicate/alias keys, secret echo and operation/principal mismatch roll back as `unavailable` without diagnostic leakage or fallback.

成功 role change 在同一事务写入 role、一个 `user.role_change` Audit 和 receipt；Audit detail 精确为 email/old/new。No-op receipt 不产生 Audit。Mutation response 使用 21 字段 Directory-user 兼容投影，不包含凭据、hash 或 generation；raw status、nullable metadata 与 opaque preferences 保留。Role change 不推进 JWT generation，也不撤销 API keys。receipt 冻结原本已接受的用户快照。Replay 总是重新验证 active super-admin actor 和 active target，但不重算 blockers 或 last-SA mutation 条件、不重放写入，也不重复 Audit。若 target 角色经历 A→B→A，原 B 操作重放仍返回冻结 B 快照，数据库维持 A；same-role no-op 也不能在重试时变为新写入。不存在 User version 字段、supersession guard 或 Audit scan。Actor、payload 或 epoch 错配返回 409 `operation_conflict`；Public PATCH 每次使用新 operation UUID，没有公开 replay key。

Workspace 的 `identity_platform_role_facts(target UUID)` 返回封闭 `schema/target_user_id/owned_project_count/non_viewer_membership_count/membership_projects`，每个项目只有 `project_id/project_name/membership_role`，按 project name/UUID 排序；raw membership count 不改成 join 后数量。`lock_identity_platform_role_owned_projects(target UUID)` 仅在真实角色变更前锁 target-owned projects 并返回 strict boolean；同角色 no-op 与 preview 不取此锁。Labeling 的 `identity_platform_role_facts(target UUID, ordered_membership_project_ids UUID[])` 一次返回 `schema/target_user_id/unfinished_effective_work_count/membership_projects`，每项只有 `project_id/annotator_batch_count/reviewer_batch_count/assigned_task_count/review_task_count`，同序且包含零计数。封闭 projection schema 分别为 `platform_role_workspace_facts.schema.json`（`aap-workspace-identity-platform-role-facts/v1`）与 `platform_role_labeling_facts.schema.json`（`aap-labeling-identity-platform-role-facts/v1`）；Identity 不读取 Workspace／Labeling 原表，Labeling 也不反向调用 Workspace，Audit 只授予当前角色事件的 typed write。Project facts、blockers、counts 与 last-SA 读取符合各自的 snapshot/lock 顺序要求。

Migration 0245 创建 `aap_identity_platform_roles_v1`。`identity_platform_role_client` LOGIN 仅能执行 `platform_role_policy()` 与 `execute_platform_role_command(jsonb)`。API/runtime 可选 `IDENTITY_PLATFORM_ROLE_DATABASE_URL(_FILE)`，Compose host selector 为 `IDENTITY_RUNTIME_PLATFORM_ROLE_DATABASE_URL_FILE`；留空只关闭此 capability、route 返回 503，既有 Identity service 仍可启动。API 直接 DSN 必须先调用 policy/readiness；显式 DSN 故障不回退 ORM。56 个选定 DB/process cases（48 Native/旧 API、5 restore、3 live）全部通过、零跳过；installed package 94 个 `.py`/`.json` 文件 hash 匹配，source 与 installed suites 各 447 项通过。API/architecture 499 项通过、127 条明确延期；Node 22 self-check 与 SDK 35 项通过。禁网、只读、1 CPU/512 MiB API cold image `sha256:a49450a0b203a3f05f68c6de058cd61d7d84d66cfd2b9384265ce3d8adbb6ca9` 用时 38.894 秒，核验 94 个 package 文件、89 个 API/native inputs、两条 role routes、既有 owner 路由数量、OpenAPI、migration head 0245 和已退役 helper absence。12 个 target、并发 4 的 host-process execute/replay sample 包含 Node/HTTP 开销，不构成生产容量或 SLO；细节见 [ADR-0125](/dev/adr/0125-independent-identity-platform-role-commands)。这些 evidence 只限定本 owner slice，不与 admin-reset 或 Directory qualification 合并，也不代表全 Identity／生产切换完成。

## 管理员停用与恢复账号

此 owner slice 仅迁移超级管理员／项目管理员的 `POST /api/v1/users/{user_id}/deactivate` 与 `POST /api/v1/users/{user_id}/reactivate`，保留现有方法、路径、body、errors、`UserOut` 和 SDK/Web 调用。Suspend 的 `user_id` 保持字符串 path、无 body 和 malformed UUID 404；reactivate 保持 UUID path 的 422、可空 `ReactivateRequest`、可选/null `reason`（最多 500 字符）和忽略 extra fields 的行为。有效 U+0000 原因仍被接受，unpaired Unicode surrogate 继续由现有模型拒绝。两路都不增加 public CAS/idempotency header；每个 public invocation 生成新私有 UUID。

Private endpoint 是 `POST /v1/identity/suspensions/execute`，wire `aap-identity-suspensions/v1`，operations 为 `suspend_account` 与 `reactivate_account`。Wire request 精确为 `schema/operation/operation_id/token/audit_context/payload`；suspend payload 只有 `{target_user_id}`，reactivate payload 是 `{target_user_id,reason}`。Portable manager 按 Python Unicode `str.strip()` 计算是否填写，Native reactivate payload 仅为 `{target_user_id,reason_present:boolean}`；原因原文不进入 PostgreSQL JSONB，保留 U+0000 等既有合法输入。Native `aap-identity-suspensions-native/v1` request 精确有 `schema/operation/operation_id/actor/expected_key_hash/request_fingerprint/derivation_id/audit_context/payload`。Actor 从真实凭据解析；隐藏 token 和 reason，不回显诊断。Success data 是 `{user: ...}`，使用既有 21-field Directory-user record；非敏感 User 字段、历史 role/status、nullables 和 opaque preferences 保持不变。

两操作先取得 shared last-super-admin fence，再按 UUID 顺序 `FOR UPDATE NOWAIT` 锁 actor/target User，随后重入 Account credential helper 并检查当前 manager/JWT generation/JTI/expiry 或 API-key hash/state/scopes。Current Principal role/name/email/scopes 改变本身不使有效 Principal 畸形。首次准入发现凭据已停用仍返回 401；准入完成后账号被停用，公开适配层保持原 403 `当前账号已无管理权限`，私有 `user_inactive` code 不变。Inactive suspend target 的既有提前返回位于 actor manager、target missing、self 检查之后，但在 Project scope／target role 检查之前：返回完整 User，不 Audit、不退休凭据；项目管理员因此仍可得到历史／deleted inactive target 的 no-op 响应。

真实 suspend 对 target 的所有 derived Projects 先持 `KEY SHARE NOWAIT`。Project-admin 只能停 employee；derived set 可以为空，非空时必须全部由 actor 管理。此分支的既有 active-employee manages-target helper 始终为 true，不新增 ProjectMember 要求。Project scope 后，target 为 super-admin 时需保留至少一个 active 且尚未预留注销的 super-admin。通过后写 `is_active=false/status=offline/disabled_kind=suspended` 及原有 disabled metadata，并退休凭据。

Reactivate 的现有顺序不同：target missing 404 早于 fresh actor manager 检查，self 仍 400。Project-admin 仅能恢复 employee，且必须有非空 derived-project set、全部归 actor 管理；其查询只锁 actor-owned subset，使用 `FOR SHARE NOWAIT` 后比较完整覆盖。Super-admin 跳过 Project scope 与锁。Scope 检查发生在 target active/kind 检查之前；已启用返回 `账号当前已启用`，只有 `suspended` 或 `emergency_suspended` 可恢复。通过后先退休凭据，再设 `is_active=true/status=offline` 并清空 disabled metadata。

Derived project scope 精确保留现有来源：目标拥有的 Project、目标 ProjectMember、TaskBatch annotator/reviewer、Task 直接 assignee/reviewer 和 TaskLock 用户经 Task 关联；全部状态参与，无 TaskBatch fallback；孤儿／null refs 按既有 Project join 消失。Workspace 使用一个 typed helper，并可在内部复用 Labeling 现有 `identity_directory_project_refs(uuid[])`；不新增 Labeling protocol/grant，不加 per-project RPC、ProjectMember/Batch/Task/TaskLock 锁或通用 scope framework。清空旧 batch assignment 时没有锁旧 User 的既有 Labeling race 不在本 slice 内。

两类真实写入都使用现有 credential-retirement helper：generation 加一、撤销当前有效 API keys、消费 unused reset links；旧凭据不能复活。已接受的 self-deactivation reservation fields 原样保留，Admin suspend/reactivate 不取消已受理申请，也不转交成员、批次或任务。Audit 只写一条：`user.deactivate` detail `{email}`；`user.reactivate` detail `{reason_present:boolean}`。Reason presence 遵循 Python Unicode `str.strip()` 语义，原文不进 User/Audit/receipt。Reactivate replay 不退休成功受理后新签发的凭据。回放先核对收据绑定及 target generation/active flag，再检查目标 scope/state；后续生命周期变化返回 `operation_conflict`，状态匹配时仍检查 PA-reactivate 当前 scope。

| code                         | HTTP | detail                                 |
| ---------------------------- | ---: | -------------------------------------- |
| `invalid_request`            |  400 | `null`                                 |
| `manager_required`           |  403 | `当前账号已无管理权限`                 |
| `target_not_found`           |  404 | `用户不存在`                           |
| `self_suspend`               |  400 | `不能停用自己`                         |
| `self_reactivate`            |  400 | `不能恢复自己`                         |
| `suspend_role_forbidden`     |  403 | `项目管理员仅能停用标注员/审核员账号`  |
| `reactivate_role_forbidden`  |  403 | `项目管理员不能恢复该角色`             |
| `reactivate_not_managed`     |  403 | `该用户不在你管理的项目内`             |
| `suspend_scope_forbidden`    |  403 | `该用户跨多个项目，须由超级管理员处理` |
| `reactivate_scope_forbidden` |  403 | `该用户存在不在你管理范围内的项目`     |
| `last_super_admin`           |  400 | `不能停用最后一名超级管理员`           |
| `already_active`             |  409 | `账号当前已启用`                       |
| `not_reactivatable`          |  409 | `该停用状态不可恢复`                   |
| `account_busy`               |  409 | `账号正在更新，请刷新后重试`           |
| `project_busy`               |  409 | `{reason:project_busy}` (suspend only) |
| `operation_conflict`         |  409 | `null`                                 |
| `unavailable`                |  503 | `null`                                 |

Reactivate Project-lock contention maps to `account_busy`, preserving the old route; only suspend returns `project_busy`. For a project-admin, an empty derived-project set maps to `reactivate_not_managed`, while a nonempty set with incomplete project ownership maps to `reactivate_scope_forbidden`. Existing Identity authentication errors retain their current codes with null status/detail.

Native schema `aap-identity-suspensions-native/v1` uses the distinct HMAC domain `aap-identity-suspensions/v1`; the canonical fingerprint covers normalized UUIDs and the raw wire operation payload but excludes audit context. Wire replies are closed; Native adds strict `replayed:boolean`. Malformed/unknown/duplicate/extra/alias fields, secret echoes and op/principal misbinding roll back as `unavailable` with no writer fallback.

Successful suspend—including inactive-target no-op—and successful reactivate have immutable receipts storing the accepted `{user: ...}`, nullable Audit ID and target generation/active flag, never raw reason/token/hash/credential/Principal/live User FK. Replay authenticates the current manager and rechecks the original target authority branch. Inactive-suspend early return remains before project-admin role/scope checks, including on replay; project-admin reactivation still rechecks its current project scope. A changed target generation or active flag returns 409 `operation_conflict`; otherwise return the frozen record without new retirement/write/Audit. Populated receipts block downgrade.

Migration 0246 creates `aap_identity_suspensions_v1` and `public.identity_suspension_operation_receipts`. `identity_suspension_client` LOGIN has only `suspension_policy()` and `execute_suspension_command(jsonb)`; API direct DSN must pass policy/readiness first. API/runtime optional variables are `IDENTITY_SUSPENSION_DATABASE_URL(_FILE)`; Compose host selector is `IDENTITY_RUNTIME_SUSPENSION_DATABASE_URL_FILE`. A blank selector disables just this capability and returns 503 without blocking existing Identity startup; explicit failures do not fall back. The owner has local qualification: 77 distinct selected database/process cases passed with zero skips (73 database/restore and four live), source and installed package suites each passed 468 tests, and 99 installed package hashes match. API architecture passed 543 checks with 128 explicit deferrals; Node passed 46 checks and SDK passed 35. The cold API image passed; exact image hashes, route/input coverage and host-sample limits are in [ADR-0126](/dev/adr/0126-independent-identity-suspension-commands). This does not change the separate self-deactivation workflow or qualify full Identity, production capacity or cutover.

## 管理员删除账号

此 owner slice 只覆盖管理员 `DELETE /api/v1/users/{user_id}`。保留公开字符串 path、malformed UUID 404、可空 `DeleteUserPayload` 与 `transfer_to_user_id`、忽略 extra fields、现有 manager 权限、errors 和 21 字段 `UserOut`。每次公开调用仍使用新的 private operation UUID，不增加 replay header。

私有 endpoint 为 `POST /v1/identity/deletions/execute`，operation 是 `delete_account`，wire schema 为 `aap-identity-deletions/v1`。Wire request 精确含 `schema/operation/operation_id/token/audit_context/payload`；payload 是 `{target_user_id,transfer_to_user_id}`，公开请求省略 receiver 时也会规范化为 null。Native schema 为 `aap-identity-deletions-native/v1`，由认证 actor 与 credential binding 替代 token。HMAC 使用独立 `aap-identity-deletions/v1` domain，不纳入 Audit context。冻结字段 schema、closed errors 与 replay 顺序见 [ADR-0127](/dev/adr/0127-independent-identity-deletion-commands)。

Owner 在共享 last-super-admin fence 后，按 UUID 顺序锁 actor、target 与可选 receiver User，并在锁内重新验证当前 active manager 和 credential。Current manager check 先于 target lookup；target missing 返回 404，self-delete 返回 400。之后 inactive target 返回完整当前 record no-op，不执行 role/project/receiver/work 检查、Audit 或修改。Active target 锁全部 derived Projects；PA 只可删除 employee，空集合允许，非空时每个 Project 都须由 actor 管理；旧 active-employee manages-target helper 在此分支不增加成员关系要求。完成该 scope 检查后再保留 last-unreserved-super-admin 围栏。首次准入凭据已停用仍返回 401；准入后才失活的 actor 保持旧 public 403。

删除阻塞只统计 target 的直接 assignee Task，状态为 `pending`、`in_progress`、`review` 或 `rejected`；reviewer work、TaskBatch fallback 和完成态不计入。所有 target TaskLock 都计数，包括过期锁。没有 receiver 且任一计数为正时返回 `has_pending_tasks`，sample 最多五个任务 UUID。传入 receiver 时即使无工作也会校验：必须启用、不能是 target、角色为 employee 或 project_admin。PA 还须覆盖每个待转交直接任务所在的 distinct nonnull Project；receiver 是 actor 时保留既有例外。转交只更新 eligible Task assignee，并将 `assigned_at` 与 `updated_at` 设为当前时间，保留旧 ORM `onupdate` 效果；不改变 reviewer、TaskBatch 或其他状态的 Task，也不推进 excluded Task timestamps。之后释放所有 target TaskLock。

DELETE 保留与 suspend/reactivate 不同的凭据行为：写 inactive/offline/deleted metadata，但不推进 generation、不改 API-key revoke state、reset-link use state 或 JWT/JTI。已接受的 self-deactivation fields 保持不变。Typed Audit helper 先清除 target 历史行的 `actor_email`／`actor_role` 并捕获数量，再 INSERT 固定 `USER_DELETE`；仅数量非零时在新 Audit detail 嵌入 `redacted_audit_rows`，不增加 `detail_json` UPDATE 权限。既有行的 actor ID、detail、IP 和 target 保留；该历史 actor redaction 不是完整擦除。成功删除和 inactive no-op 都冻结 receipt；重放重新认证当前 manager、核对 target generation/active state，匹配时返回冻结响应，不重复转交、释放 TaskLock、Audit 或脱敏；target 后续变化返回 `operation_conflict`。

Migration 0247 与 `identity_deletion_client` 只开放 `deletion_policy()` 和 `execute_deletion_command(jsonb)`。API/runtime 可选 `IDENTITY_DELETION_DATABASE_URL(_FILE)`；Compose host selector 为 `IDENTITY_RUNTIME_DELETION_DATABASE_URL_FILE`。Selector 留空只关闭本 capability，private route 返回 503；显式 DSN 必须通过 readiness，失败不 fallback。Root 的最终最新用例并集通过 72 个 selected acceptance cases（71 DB/process、1 static artifact），零跳过；restore 与 dedicated-API 的最新通过结果覆盖早期夹具失败，重跑不重复计数。Source 与 installed package 各通过 502 项，104 个 installed 文件 hash 精确匹配；API assembly 576 项通过并明确延期 129 条规则，Node 21、SDK 35 项通过。Migration 0247 downgrade/restore 与 grants、codegen/catalog/schema、459 个环境变量和 48 个 sections 均通过。OpenAPI 只改变 DELETE description，public shapes 保持不变。受限 owner image 与禁网只读 API cold image 均通过；host sample 只用于有限并发联通，不是容器 CPU 时延、生产容量或 SLO。完整 hashes、路由覆盖、端到端样本和 12 receipts/12 Audit replay 检查见 [ADR-0127](/dev/adr/0127-independent-identity-deletion-commands)。offboarding、registration-status settings 与 dashboard hydration 仍不在此 slice。

## 管理员离职预览与提交

此 owner slice 只抽取 `GET /api/v1/users/{user_id}/offboarding-preview` 与 `POST /api/v1/users/{user_id}/offboarding`。Public 路径、UUID 422、原 DTO／默认值、忽略 extra fields、响应、错误和调用方保持；Preview 无 body。Commit 保留 `preview_version` required unbounded string、`reason` 默认为空且最多 500 字符、`mode=handoff|emergency_suspend`（默认 handoff）、`projects=[]` 和 nullable `owner_receiver_id/annotator_receiver_id/reviewer_receiver_id`。没有 project-count cap，remote Identity JSON reader 不能加 64 KiB body limit。模型不得提前拒绝重复 project ID；仍在原 scope、CAS 与 target-state 顺序后返回 422。

Preview 返回固定七个顶层字段 `user/preview_version/generated_at/projects/api_keys/blockers/can_commit`。每个 project row 使用 `project_id/project_name/roles/tasks/locked_task_count/blockers`；batch row 使用 `batch_id/batch_name`。`preview_version` 继续使用精确的 Python `json.dumps(resource_state, sort_keys=True, default=str)` UTF-8 字节的 SHA-256；保留 `ensure_ascii`、默认分隔符空格、列表顺序、aware datetime 的 Python 字符串格式与 UUID 小写格式。它是快照版本，不是身份凭据或 HMAC。资源字段清单、null project ID 的旧 `None` 摘要语义、包含／排除字段、option 顺序和 blocker 顺序见 [ADR-0128](/dev/adr/0128-independent-identity-offboarding-commands)。

Preview 是 advisory。Target missing 为 404；当前身份必须是 active super-admin/project-admin；self 为 400。Active super-admin target 的 last-unreserved 检查先于 PA role/scope 检查。PA 只能处理 employee；active target 的空项目集允许，inactive 且无 project 的用户返回 `inactive_unmanaged`，非空集合必须全部为 actor-owned。SA 跳过项目范围限制。`can_commit` 不是 commit gate，commit 必须在持锁后重算 scope、facts 与快照。

Commit 在共享 last-super-admin fence 后，按 UUID 锁 actor/target/receivers，再依序锁 Project、相关 ProjectMember、target batches（含 archived）、相关 Tasks、每个 Task 的 edit advisory lock 和 target TaskLocks。Locked facts 更新后先检查 current scope，再比较 raw `preview_version`；因此新撤权优先于 stale-preview。Target role/state、duplicate project mapping、receiver 与 blockers 后续按旧顺序检查。Emergency suspend 仍检查 scope、CAS、state 和重复映射，但不检查接收人/工作阻塞，也不转交任务。

Handoff 按原 project 与 role 顺序 `owner→annotator→reviewer` 执行。Owner 只转移 Project owner；annotator/reviewer 只转移未归档 target batch 和四个 active status 的 direct role Tasks。Task／Batch 的隐式 `updated_at` 变化和 annotator-only `assigned_at` 更新保持原语义；Batch `assigned_user_ids` 按 annotator UUID、reviewer UUID 重建，不去重。Emergency mode 对每个存在角色返回固定 unresolved reason `emergency_suspension_requires_later_handoff`，不写职责转移。两种模式都将用户置为 inactive/offline 并使用当前停用 metadata；normalized reason 写入 User，但原始 reason 不入 Audit。

两种模式均在同一 PostgreSQL transaction 中更新用户、职责、凭据、TaskLocks、Audit 和 frozen receipt。更新目标状态后再 retire generation、active API keys 和未使用的 reset links，并在最后释放 target TaskLocks；保留已接受的 self-deactivation reservation。Typed `user.offboard` Audit 只写 `mode/reason_present/transfers/unresolved/revoked_api_key_count`。Private prepare/apply 由 `OffboardingPostgresData.transaction()` 持有同一连接和事务；不在 HTTP 间暴露事务，也不引入 saga、prepare ledger 或第二个 service。重放用 accepted `scope_basis`，当前 PA 仍须拥有/可见全部受理项目；新 target 引用不重新计算旧工作。

Private protocol 是 `POST /v1/identity/offboarding/execute`，wire `aap-identity-offboarding/v1`、native `aap-identity-offboarding-native/v1`、policy `aap-identity-offboarding-policy/v1` / `aap-identity-offboarding/v1`。API HTTP composition 为 `apps/api/app/api/v1/offboarding_commands.py` 的 `require_offboarding_manager`、`get_offboarding_commands` 与 `execute_offboarding`；`IdentityOffboardingCommands` 装配 `OffboardingCommandsPort`，`IdentityOffboardingCommandData` 在同一 caller transaction 内执行 prepare/apply。API/runtime 可选 `IDENTITY_OFFBOARDING_DATABASE_URL(_FILE)`，Compose host selector 为 `IDENTITY_RUNTIME_OFFBOARDING_DATABASE_URL_FILE`。Migration 0248 的 `identity_offboarding_client` 仅有三个 typed functions。空 selector 关闭 standalone Identity runtime capability；嵌入式 API 在无专用 DSN 时继续使用 caller transaction。显式 DSN 必须 readiness pass，失败不 fallback。Root 的限定本地验收通过 77 个 distinct selected cases（76 DB/process、1 static artifact），零跳过；exact checks、hashes、image bounds 与样本见 [ADR-0128](/dev/adr/0128-independent-identity-offboarding-commands)。这不代表完整 Identity、生产容量或切换完成；Offboarding 也不是完整 GDPR 擦除。

## 自助注销与最后一名超级管理员围栏

公开 `POST /api/v1/auth/me/deactivation-request` 与 `DELETE /api/v1/auth/me/deactivation-request` 保持现有路径和 `UserOut` 响应。API 以 `aap-identity-deactivation/v1` 调用 `POST /v1/identity/deactivation/execute`；该协议只接受 `schema`、`operation`、`operation_id`、`token`、`audit_context` 与 `payload`。`operation_id` 为 UUID，`token` 是 1–16,384 字符的有效 JWT／API key，且按 secret 处理。

| operation                   | payload                                           | success data               |
| --------------------------- | ------------------------------------------------- | -------------------------- |
| `request_self_deactivation` | `{reason: string or null}`；最多 500 Unicode 字符 | `{account: AccountRecord}` |
| `cancel_self_deactivation`  | `{}`                                              | `{account: AccountRecord}` |

当前身份只能操作自己；四种启用平台角色都可申请。reason 按 Python `str.strip()` 修剪并限制 500 字符，不进入响应、Audit detail 或通知 payload。成功请求沿用七天 UTC 冷静期；重复申请与无待处理申请分别保留既有 400 错误。返回的 `account.id` 必须等于当前 actor。

Identity 在变更或回放前重新验证当前 JWT／API key，并在同一事务内获取用户锁、核验凭据代际／API key hash、状态、请求回执和业务策略。成功 account、Audit、通知 outbox 与冻结操作结果共同提交；重放要求当前有效凭据，返回冻结 account，但不会恢复较新的注销字段或重复副作用。Native wire `aap-identity-deactivation-native/v1` 使用 actor、仅在 API key 时出现的 `expected_key_hash`、请求 HMAC 与派生 ID，不传原 bearer 或审计上下文进入幂等指纹。HMAC 域为 `aap-identity-deactivation/v1`，指纹只覆盖规范化的 `{operation,payload}`。

用户命令失败使用公共失败 envelope，附加 `account_busy`、`invariant_busy`、`operation_conflict` 与 `policy_rejected`。策略失败在接受前即与 actor／operation／请求范围绑定，不产生成功回执。无效／停用身份沿用公共 401；用户锁或超管 fence 忙返回安全 409；请求范围冲突用 `{reason:operation_conflict}`；未配置选定 Identity capability 或显式连接故障返回 503，不回退到本地 User ORM。非策略错误不透出 PostgreSQL 诊断。

自助超级管理员申请要求至少有一名**其他启用**超级管理员；若没有，返回 `400 您是最后一名超级管理员，请先指定继任者再申请注销`。如果还有其他启用超级管理员，但他们都已有未完成的注销申请，则返回 `400 其余超级管理员均已申请注销，请先让其他管理员撤销申请或指定继任者`。`unreserved` 的定义是启用且 `deactivation_requested_at`、`deactivation_scheduled_at` 都为空。Role preview 使用同一状态快照；会减少启用超级管理员的管理、停用和 offboarding 写入也要求保留至少一名其他未预留的超级管理员。

这个约束由单个版本化 advisory try-lock 围栏串行化；所有减少超级管理员或改变其生命周期的路径都先取围栏，再按 UUID 顺序锁用户。锁竞争返回 `409`，重试必须沿用对应请求／页的操作 UUID。预留注销申请已被接受后，管理员 suspend 或 reactivate 不会撤销它；只有用户显式取消或到期处理成功才清除这三个请求字段。管理停用和 offboarding 原有的凭据副作用仍单独保留。

到期执行由私有 `POST /v1/identity/deactivation/process-due` 提供，只接受 control Bearer，不接收用户 token、调用方指定的用户 ID 或时钟。请求精确为 `{schema:"aap-identity-deactivation-due/v1",operation_id:UUID,limit:int}`；limit 必须是严格整数 1–500，默认 256，是单页事务上限而不是总处理上限。响应是 `{schema,ok:true,operation_id,processed,examined,blocked_last_super_admin,has_more}`，其中 `processed + blocked_last_super_admin = examined <= limit`；不返回用户资料或通知值。错误只允许 `invalid_request`、`invariant_busy` 与 `operation_conflict`。

调度器继续使用 `app.workers.deactivation.process_deactivation_requests` 和每日 04:00 UTC 节奏。Celery request UUID 固定为一次运行的 ID，各页用 `uuid5(run_id,"batch:N")` 固定；任务回放先读回已提交页，再继续领取。每页完成后 worker 持续读取，直到没有进展或 `has_more=false`，因此不存在总量截断。当前被锁定或因最后超级管理员保护而阻塞的页不会计作永久失败或丢弃；无进展就结束本轮，下一次日调度使用新 ID。

Celery task 的兼容结果保持 `{processed: n}`，其中 `n` 是本次 run 成功注销的用户数；blocked-last-admin 与 examined page 数只存在于私有 operator page，不进入旧 task 响应。

每名实际到期用户在停用前再次确认仍有其他启用超级管理员。通过后只做软删除：设为 inactive/offline、`disabled_kind=deleted`、`disabled_by=self`，reason 用原申请原因或 `自助注销申请到期`，然后清除三个请求字段。若该用户是最后一名实际启用超级管理员，保留所有请求字段与待处理账本，不写批准 Audit，也不发完成通知，并增加 `blocked_last_super_admin`。此自助到期流程不推进 JWT generation、不撤 API key，也不交接任务或项目。

Audit 只允许 `user.deactivation_request`、`user.deactivation_cancel`、`user.deactivation_approve` 三种固定事件，调用 `aap_audit_v1.record_identity_deactivation_event(text,jsonb,jsonb,jsonb)`；请求 detail 只有 `scheduled_at` 和 `reason_present`，原因正文不保存。批准流程先插入批准事件，再对该 actor 的历史仅将现存 `actor_email` 和 `actor_role` 两列置空。历史 `detail_json`、通知 payload 中的既有邮箱／角色字段都保留；这不是完整 GDPR 或通用字段擦除保证。

## 注销通知的 durable publisher

注销申请和成功完成会在业务事务中调用 Notifications owner 的 `create_identity_deactivation_notifications(uuid,text,jsonb,uuid[])`：request 通知 `user.deactivation_requested` 发给当前启用的超级管理员（申请人是超管时也包含本人）；completion 通知 `user.deactivation_completed` 发给其他当前启用超级管理员，不包含被注销者。request payload 固定为 `{email,name,role,scheduled_at}`，completion 为 `{email,name,auto:true}`；reason 永不写入通知。创建时应用原有 in-app 偏好 truthiness（缺项默认为 true；null、false、0、空字符串／列表／对象视为静音）。业务 Notification 行和 `notification_delivery_outbox` 同事务提交，按 operation／recipient／type 去重；若用户在领取前删除通知，外键级联会取消待投递。

独立 publisher 使用 `aap-notifications-delivery/v1` 和仅有三个函数权限的 `notification_publish_client`：`identity_delivery_policy()`、`claim_identity_deliveries(jsonb)`、`finish_identity_delivery(jsonb)`。领取必须先提交 lease，再向 `notify:{user_id}` 发布；只有 Redis 接受后才能确认。过期或旧 lease 不能确认，worker 失联后可由下一次 claim 接管；same Notification ID 会跨 retry 保持，因此语义为至少一次、允许重复 broker 消息。领取量 1–100（默认 16）、lease 60 秒、进程并发 4、Redis I/O timeout 不超过 5 秒，不需要 lease renewal。只有 `delivered` 和 `retry` 两种安全 finish 状态；publisher 不持有 Identity、AUTH、JWT signing secret 或其他 SQL 能力。

同一安装工件的独立进程命令为 `python -m aap_identity_runtime.notification_worker`，由 `aap_identity_runtime.notification_config.NotificationPublisherSettings` 读取 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)` 与 `NOTIFICATION_PUBLISH_REDIS_URL(_FILE)` 两组必需 secret。默认模式持续 poll，`--once` 只领取并处理一个 batch；它不是 HTTP server，也没有 HTTP health endpoint。API/Celery 可复用相同 executor、显式设置 `notification_publish_database_url` 并使用现有 task Redis 配置；default queue 每 10 秒的 relay 与独立 publisher 处理同一 leased outbox，不再并行做 ephemeral best-effort publish。Notifications owner、Compose secret 和运行限制见 [开发部署说明](/ops/deploy/development) 与 [ADR-0121](/dev/adr/0121-identity-self-deactivation-and-notification-delivery)。

API／独立 Identity runtime 的自助 DSN 分别使用 `IDENTITY_DEACTIVATION_DATABASE_URL(_FILE)` 和 `IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL(_FILE)`；选择器可留空用于调用方事务，明确选择后连接失败不回退。API/Celery 的可选 Notifications SQL binding 为 `NOTIFICATION_PUBLISH_DATABASE_URL(_FILE)`，Redis 继续使用现有 task settings。可选 Compose 的 host selectors 是 `IDENTITY_RUNTIME_DEACTIVATION_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_DEACTIVATION_OPERATOR_DATABASE_URL_FILE`、`IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE` 和 `IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE`。独立 publisher secret 只映射到它自己的两项 `NOTIFICATION_PUBLISH_*_FILE`；这些 owner 能力须在共享 PostgreSQL transaction owner 中组合。

## 开放注册与账号恢复

`POST /v1/identity/recovery/execute` 接受 `aap-identity-recovery/v1`，请求字段固定为 `schema`、`operation`、`operation_id`、`audit_context`、`payload`。操作 UUID 在首次发送前固定；审计上下文仍是四个可空字段。API 保留公开限流、密码输入校验和 Turnstile 验证；只有受信任的 API 边缘可以提交 `captcha_verified`。

| operation                    | payload                                         |
| ---------------------------- | ----------------------------------------------- |
| `register_open`              | `email`、`name`、`password`、`captcha_verified` |
| `request_password_reset`     | `email`、`captcha_verified`                     |
| `resend_email_verification`  | `email`                                         |
| `consume_password_reset`     | `token`、`new_password`                         |
| `consume_email_verification` | `token`                                         |

成功响应是匹配操作和 UUID 的 `{schema,ok:true,operation,operation_id,data}`。data 固定包含 `account`、`claims`、`access_token`、`email_verification_required`、`completed` 五字段；没有原始挑战 token 或链接。

- 注册创建无数据组的 `viewer`、代次 0，返回账户与验证标记。需要验证时状态为 offline，claims／access_token 为 null；否则为 online，返回冻结声明及 JWT。两种情况 `last_login_at` 都为 null。
- 请求／重发的五字段全部为 null，未知邮箱和无资格目标也写入不可重新定向的空结果回执。
- 消费只返回布尔 completed，其余字段为 null。停用账户的有效重置挑战被单次消费并返回 false；邮箱验证不会重新启用账户。

失败响应为 `{schema,ok:false,error:{code,status_code,detail}}`。封闭错误包括 `registration_disabled` 403、`email_exists` 409、`captcha_required` 400、`policy_rejected` 422、`operation_conflict` 409 和无效链接 400。服务或能力不可用为 503；形状、摘要、操作或 UUID 绑定错误不能确认提交，也不会回退其他写入者。密码诊断不含 input／ctx，私有请求体上限为 64 KiB。

所有者在同一连接内持有 User 和挑战行锁，运行 bcrypt，再重新检查并提交账号、挑战、邮件意图、业务审计与回执。重置会清除管理员重置标记并推进 JWT 代次，API key 保持有效。相同操作回放只返回冻结结果，不重复变更或审计。注册回放要求当前同 ID／邮箱且启用；曾签发 JWT 的回放还需当前密码验证。冻结账户角色、验证策略与声明不随当前资料或部署策略变化，历史 JWT 仍可能已失效。公开接口没有新增幂等 header 或自动重试。

挑战表以 SHA-256 摘要和签发邮箱保存单次证明，迁移在原位摘要旧 token，因此现有有效链接仍可消费。邮件 outbox 只保存 Fernet 密文与 key epoch，恢复回执不保存姓名、邮箱、密码、原始 token 或 JWT。消费条件为当前邮箱仍匹配且 `now < expires_at`；保留任一挑战、回执或邮件意图时拒绝降级。

## 独立账号邮件执行

同一个安装包或镜像以 `python -m aap_identity_runtime.mail_worker` 启动邮件进程，默认健康端口 8171。它只有 `MAIL` 四函数连接、独立的 `MAIL_SETTINGS` 单函数连接及 Fernet key，不需要认证控制 token、AUTH 数据连接或 JWT signing secret。`/health` 检查进程，`/ready` 检查所需权限、策略和 key 格式；就绪不保证 SMTP 可送达。

执行器先提交有期限的领取租约，再读取密文并发信，网络等待期间续租。User → 挑战 → outbox 的行锁不跨 SMTP。领取、续租和完成在锁等待结束后按数据库当前时间检查；过期或旧租约不能确认投递。进程失联后，另一执行器可以接管过期租约。

已投递和确定拒绝为终态；临时故障、DATA 后断线等结果不明状态继续使用同一挑战、密文与 Message-ID 重试。投递属于至少一次语义，用户可能收到多封相同有效链接。错误 key epoch 保留待投递项，不破坏仍有效的挑战；轮换前应排空旧邮件或保留对应旧 key 的执行器。缺少 SMTP 配置或确定拒绝只使对应重置挑战作废，验证挑战保持有效。

`RECOVERY` LOGIN 仅有 `recovery_policy`、`prepare_recovery_command`、`apply_recovery_command`、`read_registration_status`；`MAIL` 仅有 `mail_policy`、`claim_mail`、`renew_mail`、`finish_mail`；`MAIL_SETTINGS` 仅有 Settings 的 `mail_delivery_policy`。客户端均无直接表、序列或 DDL 权限。内嵌装配与专用连接选择、Compose secret 文件和授权命令见 [DEV.md](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md#注册密码恢复与凭据邮件)。设计与剩余模块边界见 [ADR-0119](/dev/adr/0119-independent-identity-recovery-and-mail)及[模块台账](/dev/concepts/module-deployment)。

## 已接受 API-key 的最终授权与当前账号读取

已受理的训练数据、评估及相关持久操作只保留 key ID。最终写入前由 Identity 的
`aap_identity_v1.check_accepted_api_key(uuid,uuid,text[])` 检查当前 owner、撤销、
过期和所需 scopes。它不重新证明凭据持有、不读取或返回 key hash；hash 轮换的原有
接受语义保持。请求／SQL 结果描述使用 `aap-identity-accepted-api-key/v1`，包内
`accepted_api_key_protocol.schema.json` 与 Native 冻结描述由同一 owner 生成。

调用方先持有账号 SHARE 与项目 KEY SHARE，然后等待 key SHARE。所有锁保持到调用方
commit／rollback，函数不改事务范围；等待后以 `clock_timestamp()` 检查过期。
空 scope 集合法，无新增数量上限；非法参数为 SQLSTATE `22023`，锁错误与失败事务状态
原样保留。Learning owner 和过渡应用连接得到固定执行权限，普通 AUTH／key-management／
建题客户端没有该权限；不新增 DSN 或 HTTP 路由。

单账号读取复用已有的 Identity 最小状态与锁能力，保留当前权限、原查询／锁序、取消和
错误语义。Mask Repair 等原来仅检查账号存在的入口不增加 active 拒绝；作业取消的停用
账号保持 401 与 Bearer 错误。集合查询和展示投影通过下文公开关系读取，当前定向结果
不代表完整 Identity 独立部署完成；实际剩余项见[模块台账](/dev/concepts/module-deployment#剩余工作台账)。详见
[ADR-0161](/dev/adr/0161-identity-current-authority-consumers)。

独立 HTTP Identity 保管自己的签发／派生 secret；API edge 的 `SECRET_KEY` 可以不同，
RPC 使用独立的 `IDENTITY_TOKEN`。API 的远程响应解码不重新派生 Identity 输出。
不同密钥已通过整模块真实 REST／Socket 和恢复验收。已有 Identity secret 影响 JWT、
派生凭据和持久回执，不能为此改动已有部署或静默恢复旧结果。

## 集合与展示的同事务读取关系

Identity 的 `aap-identity-read-relations/v1` 固定两个 PostgreSQL 只读关系：

| 关系                                       | 发布字段                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------- |
| `aap_identity_v1.account_profiles`         | `id/name/email/role/is_active/status/avatar_ref`，其中 `avatar_ref` 是可空字符串 |
| `aap_identity_v1.invitation_audit_targets` | `id/project_id`，其中 `project_id` 可空                                          |

它们由 Identity owner 持有，设置 `security_barrier`，不向 PUBLIC 或浏览器开放。
关系保留原值及禁用／历史角色行，不发布密码、API-key hash、凭据代次、生命周期详情
或邀请 token。受信任后端可以枚举这些最小资料；每个消费者仍须在原 SQL 中保留其
权限范围、成员配对、筛选、inner／outer join、count、排序和游标。关系本身不作
行级授权，不代替提交前的账号事实锁。

成员／通知集合、搜索、管理统计、绩效 roster、作者／创建者展示和邀请审计范围已
迁到这两个关系。读取处在调用方 connection／transaction 中，遵循原快照与
autoflush／no-autoflush，不引入逐行 HTTP 调用或额外提交。仅供隔离测试的 seed
读写保留在有显式开关、非生产环境及测试库名守卫的操作入口。

全部 Identity binding 使用远程 owner 的既有 API 角色，可在迁移和 owner／旧运行
授权脚本之后应用 `infra/docker/identity-consumer-grants.sql`：

```bash
psql "$MIGRATION_DATABASE_URL" \
  -v expected_database="$DATABASE_NAME" \
  -v migration_role="$MIGRATION_ROLE" \
  -v identity_owner="$IDENTITY_OWNER_ROLE" \
  -v consumer_role="$API_RUNTIME_ROLE" \
  -f infra/docker/identity-consumer-grants.sql
```

脚本核对数据库、操作者、NOLOGIN owner、无角色成员资格的普通 consumer LOGIN、
五个能力的 owner 及关系形状。随后撤销 Identity 私表和列级权限、Identity 命令函数及
私有 schema 权限，只授予两个 SELECT 关系，以及 `read_actor_facts`、三项账号锁和
`check_accepted_api_key`。它不创建新角色或部署模式；内嵌 Identity 和隔离 seed 的
旧配置继续可用。旧宽授权及其默认权限不因此改写；每次刷新宽授权后必须最后重应用
consumer 清单，新 Identity 关系也需复核。当前脚本和定向证据不代表主 API 已切换
到这份凭据。设计与验证见 [ADR-0162](/dev/adr/0162-identity-read-relations-and-consumer-grants)。

## 内嵌运行角色的私表权限

Identity URL 和专用数据库选择器留空时，API 沿用既有 caller Session 调用 Identity
固定 Native 能力。`lan-production-grants.sql` 和工作区 `grant_runtime` 在各自授权
事务内撤销应用角色对 Identity 私表的全部表级及列级权限：八项账号、凭据、组织和
邀请表、`public.identity_*` 关系，以及 Identity 协议 schema 内的底层存储。
固定公开视图与已有命令能力继续保留；不将内嵌角色缩减为远程消费者的五函数清单。
工作区授权同时补齐目录、管理员重置密码、平台角色、暂停／恢复、删除及离职的能力。

授权要求普通 LOGIN 与迁移操作者分离，应用角色不能持有任何角色成员资格；
`NOINHERIT` 仍允许 `SET ROLE`，不足以构成隔离。撤权后再次检查有效表／列权限，
PUBLIC 授权、所有权或其他遗留权限仍可绕过时，整笔授权回滚，需先由迁移操作者
解除冲突后重试。脚本不擅自删除共享 PUBLIC 授权或角色关系。每次迁移、创建新关系
或刷新宽授权后重新执行相应授权入口；数据库默认权限本身不在本项中改写。

工作区基础配置共用同一账号时，由既有资源生命周期创建本模式的普通 LOGIN，
随机密码以 `0600` 文件保存在 `.worktree/<mode>`，应用子进程改用该账号；原 owner
只供迁移、测试夹具及专用维护 worker。需要迁移账号具备 `CREATEROLE`，否则先配置
已有的分离账号；不会回退 owner。所有 Identity 专用数据库／文件选择器在工作区
子进程中清空，避免绕回共享配置。角色和凭据按精确归属恢复／删除，停止不改密。
此路径已通过 20 项去重 Native 检查、真实普通／维护 worker、完整备份恢复及匹配
冷镜像的实际 HTTP 流程；迁移 head 0306、冻结 SQL 回滚与自有资源清理通过。完整
资格范围与剩余阻断见[模块台账](/dev/concepts/module-deployment#identity-内嵌运行权限)。
主开发环境和生产配置的切换必须单独验收；直接启动 API 的其他入口不在本流程中。独立部署仍在完整 owner 授权之后最后应用前述 consumer
清单；可信录制及门控 E2E 操作继续使用各自固定能力和数据库守卫。

## 注册策略的 owner 读取

`POST /v1/identity/recovery/registration-status` 使用既有控制 Bearer、并发槽与超时，
JSON 请求上限为 65,536 bytes。合同为 `aap-identity-registration-status/v1`，请求只有
`schema` 和 `operation`。操作可选 `read_status` 或 `read_deployment_default`，回复
精确包含 `schema/operation/ok/data`，`ok` 为 `true`，data 只有严格 boolean
`allow_open_registration`；不接受调用方提供默认值、账号、凭据或写入参数。

`read_status` 通过新增 Recovery facade `read_registration_status(jsonb)`，复用现有
Settings owner 的 `identity_recovery_policy`，数据库覆盖优先，缺失／NULL 时使用
实际 Recovery owner 默认值。原 Settings SHARE 围栏与注册写入、回执及邮箱验证策略
保持；该读取没有 User、Audit、回执或邮件写入。专用连接在有界读事务结束时释放锁，
内嵌路径沿用调用方事务和 autoflush／no-autoflush，不自行 commit／rollback。

`read_deployment_default` 只读取 owner 运行配置，不访问覆盖行。管理端将这个单键
默认值传入现有快照、元数据、版本、PATCH／reset 和冲突回读，因此损坏的历史覆盖值
仍可显示回退并修复。owner 默认值改变后，旧管理版本仍按现有 409 规则拒绝；导入预算
快照使用同一默认值，保持与管理端的设置版本一致。只启用认证的运行时也能读取该
默认值，未配置 Recovery 数据能力时不会宣称有效注册状态可用。

公开 `/auth/registration-status` 的返回形状保持，只读取 owner 当前有效状态，绕过
API 的旧进程缓存。已有远程、专用 Recovery DSN 与内嵌选择路径保留；缺少 owner、
损坏响应或连接错误返回 503，不回退 API 默认值或原表读取。没有新增 DSN、环境变量、
LOGIN 或部署模式。源码／定向进程证据见 [ADR-0163](/dev/adr/0163-identity-registration-policy-owner)。
