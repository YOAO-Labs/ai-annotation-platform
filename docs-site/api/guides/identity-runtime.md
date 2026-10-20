---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-20
---

# Identity 私有运行协议

协议对象只接受声明的 wire 字段名：版本字段为 `schema`。内部 Python 字段名 `protocol` 无论单独使用还是与 `schema` 同时提交都被拒绝；opaque 偏好值中的同名键不受影响。

Identity 运行时提供认证准入、API key、核心会话、自助账户、全局分组目录与指派、完整邀请生命周期、四项用户目录读取、自助注销、注销到期处理、账号恢复和在线扫描命令，供受信任的服务调用。公开用户接口仍是 `/api/v1/auth/`、`/api/v1/me/api-keys` 与 `/api/v1/users`。私有接口要求独立控制 Bearer；需要用户身份的操作将凭据放在 JSON 的 `token` 中，不能代替控制凭据。注册/邀请证明凭据另置于 `payload`，与调用者身份 token 分开。账号恢复使用匿名请求或单次挑战证明，邮件与注销通知分别由独立执行进程投递。

| 接口                                         | 用途                                           |
| -------------------------------------------- | ---------------------------------------------- |
| `GET /health`                                | 进程存活                                       |
| `GET /ready`                                 | 原生策略和所配置能力的精确函数权限             |
| `POST /v1/identity/authenticate`             | JWT／API key 准入，返回 `aap-identity/v1` 主体 |
| `POST /v1/identity/api-keys/execute`         | API key 列表、创建、修改、轮换和撤销           |
| `POST /v1/identity/sessions/execute`         | 登录、刷新、登出和全端登出                     |
| `POST /v1/identity/accounts/execute`         | 当前资料、改名、偏好、心跳、改密和头像引用     |
| `POST /v1/identity/presence/mark-inactive`   | 按已固定的 cutoff 批量标记离线                 |
| `POST /v1/identity/groups/execute`           | 分组目录、查询、指派预览／执行                 |
| `POST /v1/identity/invitations/execute`      | 邀请管理、批量操作、注册、接受与耐久邮件发送   |
| `POST /v1/identity/directory/execute`        | 用户目录列表、查询、统计与导出                 |
| `POST /v1/identity/deactivation/execute`     | 自助注销申请与撤销                             |
| `POST /v1/identity/deactivation/process-due` | 分页处理已到期的自助注销申请                   |
| `POST /v1/identity/recovery/execute`         | 开放注册、重置请求／消费、验证邮件重发／消费   |

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

提供方的签发时长、邮箱验证、失败计数阈值／窗口和 Redis 实例必须与边缘及剩余签发端匹配。修改签发策略或 signing secret 后，旧操作回执冲突。原生策略固定 issuer、HS256 和七天宽限；含会话回执时拒绝降级。分组目录／指派与完整 invitation lifecycle 采用独立 Identity owners；当前 Directory slice 迁移四项用户只读操作，平台角色／lifecycle 写入及其他用户 hydration 仍留在 API，整体日常签名能力尚未撤除。

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

Invitation/MailV2 和 Directory read slice 均已有各自限定的本地资格；上文分别限定了 Directory 38 个选定数据库／进程案例，邀请的 68/375/324 数字只属于旧 slice。平台角色管理、其他用户 hydration、全部 offboarding、整个 Identity、另一语言 provider 和生产容量仍待完成。`v1` 恢复邮件保持原协议；没有新的 Notifications event、Jobs backfill、生产切换或 Go 扩展。

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

`RECOVERY` LOGIN 仅有 `recovery_policy`、`prepare_recovery_command`、`apply_recovery_command`；`MAIL` 仅有 `mail_policy`、`claim_mail`、`renew_mail`、`finish_mail`；`MAIL_SETTINGS` 仅有 Settings 的 `mail_delivery_policy`。客户端均无直接表、序列或 DDL 权限。内嵌装配与专用连接选择、Compose secret 文件和授权命令见 [DEV.md](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md#注册密码恢复与凭据邮件)。设计与剩余模块边界见 [ADR-0119](/dev/adr/0119-independent-identity-recovery-and-mail)及[模块台账](/dev/concepts/module-deployment)。
