---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# Workspace 成员与负责人转移运行协议

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

配置命令的当前接入与验收见本文后续说明。项目列表、mention 与其他 Workspace consumers 继续迁移。完整 Workspace、其余模块、完整 Identity、十模块／七门槛目标、完整模块的另一语言提供方、生产容量验证与 primary cutover 也仍未完成。Go 月报保持多语言架构实验，不构成该 owner 的语言提供方。

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

配置服务保存不可变成功回执，在同一事务中执行分阶段写入和当前权限围栏。
默认 API 数据端口借用调用方事务；独立连接只持有自己的业务事务，不提交 API
连接上的无关 pending work。后端探测超时及视频参数需要与 API 保持一致，具体变量见
[环境变量参考](/dev/reference/env-vars)。

公开配置路由已接入同一个配置 owner，默认借用调用方事务，HTTP 保留提交与响应。
专用 DSN／HTTP 通过 `WORKSPACE_CONFIGURATION_DATABASE_URL(_FILE)`／
`WORKSPACE_CONFIGURATION_URL` 选择，不能同时配置；显式失败不回退。
三条绑定路径各已对照106项原公开响应；独立进程的丢响应重试、重启重放及不可用不回退也已通过本地检查。失败事务、备份恢复、9项关联恢复及0257完整迁移验证均已通过。独立安装包、受限镜像与本地并发执行也已完成限定验收；项目普通读取仍在 API，模板读取与响应投影的接入状态见后文。这一步不代表完整
Workspace 已独立部署。状态与事务取舍见
[ADR-0136](/dev/adr/0136-workspace-configuration-command-owner)。

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
