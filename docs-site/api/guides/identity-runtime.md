---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-20
---

# Identity 私有运行协议

协议对象只接受声明的 wire 字段名：版本字段为 `schema`。内部 Python 字段名 `protocol` 无论单独使用还是与 `schema` 同时提交都被拒绝；opaque 偏好值中的同名键不受影响。

Identity 运行时提供认证准入、API key、核心会话、自助账户、全局分组目录与指派、账号恢复和在线扫描命令，供受信任的服务调用。公开用户接口仍是 `/api/v1/auth/` 和 `/api/v1/me/api-keys`。私有接口要求独立控制 Bearer；需要用户身份的操作将凭据放在 JSON 的 `token` 中，不能代替控制凭据。账号恢复使用匿名请求或单次挑战证明，邮件由同一工件的独立执行进程投递。

| 接口                                       | 用途                                           |
| ------------------------------------------ | ---------------------------------------------- |
| `GET /health`                              | 进程存活                                       |
| `GET /ready`                               | 原生策略和所配置能力的精确函数权限             |
| `POST /v1/identity/authenticate`           | JWT／API key 准入，返回 `aap-identity/v1` 主体 |
| `POST /v1/identity/api-keys/execute`       | API key 列表、创建、修改、轮换和撤销           |
| `POST /v1/identity/sessions/execute`       | 登录、刷新、登出和全端登出                     |
| `POST /v1/identity/accounts/execute`       | 当前资料、改名、偏好、心跳、改密和头像引用     |
| `POST /v1/identity/presence/mark-inactive` | 按已固定的 cutoff 批量标记离线                 |
| `POST /v1/identity/groups/execute`         | 分组目录、查询、指派预览／执行                 |
| `POST /v1/identity/recovery/execute`       | 开放注册、重置请求／消费、验证邮件重发／消费   |

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

提供方的签发时长、邮箱验证、失败计数阈值／窗口和 Redis 实例必须与边缘及剩余签发端匹配。修改签发策略或 signing secret 后，旧操作回执冲突。原生策略固定 issuer、HS256 和七天宽限；含会话回执时拒绝降级。分组目录／指派已有独立 Python owner 切片；邀请接受协调、其他注册签发、用户目录／平台管理员流程仍有待提取，API 整体尚未撤除签名能力。

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

账户偏好保留原有大小和基数集合，不套用会话的 64 KiB 请求或准入的 16 MiB 响应上限；并发、截止时间及部署资源仍是实际运行边界。字节处理不持远程 SQL 锁。确定拒绝时清理新头像暂存对象，提交未知时保留字节并先核对资料／回执；旧对象只在确认提交后尽力清理。邀请接受协调、平台管理员及多 owner 生命周期继续按[模块台账](/dev/concepts/module-deployment)推进。

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

Invitation 接受仍走原 coordinator。它可在既有邀请事务中调用私有 `resolve_invitation_group(uuid,text)` 读取/创建组值；该 helper 不是公开 wire 操作，也没有授予 GROUP LOGIN。邀请、目录搜索、其他用户资料、平台角色和多-owner lifecycle 仍未整体迁移。部署 DSN、精确 grant、Audit 与 receipt 规则见 [DEV.md](https://github.com/yyq19990828/ai-annotation-platform/blob/main/DEV.md) 和 [ADR-0120](/dev/adr/0120-independent-identity-group-catalog-and-assignment)。

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
