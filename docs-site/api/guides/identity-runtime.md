---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-20
---

# Identity 私有运行协议

协议对象只接受声明的 wire 字段名：版本字段为 `schema`。内部 Python 字段名 `protocol` 无论单独使用还是与 `schema` 同时提交都被拒绝；opaque 偏好值中的同名键不受影响。

Identity 运行时提供认证准入、API key、核心会话、自助账户和在线扫描命令，供受信任的服务调用。公开用户接口仍是 `/api/v1/auth/` 和 `/api/v1/me/api-keys`。私有接口要求独立控制 Bearer；用户凭据放在 JSON 的 `token` 中，不能代替控制凭据。

| 接口                                       | 用途                                           |
| ------------------------------------------ | ---------------------------------------------- |
| `GET /health`                              | 进程存活                                       |
| `GET /ready`                               | 原生策略和所配置能力的精确函数权限             |
| `POST /v1/identity/authenticate`           | JWT／API key 准入，返回 `aap-identity/v1` 主体 |
| `POST /v1/identity/api-keys/execute`       | API key 列表、创建、修改、轮换和撤销           |
| `POST /v1/identity/sessions/execute`       | 登录、刷新、登出和全端登出                     |
| `POST /v1/identity/accounts/execute`       | 当前资料、改名、偏好、心跳、改密和头像引用     |
| `POST /v1/identity/presence/mark-inactive` | 按已固定的 cutoff 批量标记离线                 |

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

提供方的签发时长、邮箱验证、失败计数阈值／窗口和 Redis 实例必须与边缘及剩余签发端匹配。修改签发策略或 signing secret 后，旧操作回执冲突。原生策略固定 issuer、HS256 和七天宽限；含会话回执时拒绝降级。开放注册、邀请注册、密码／邮件及其他账号流程仍有待提取，API 整体尚未撤除签名能力。

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

账户偏好保留原有大小和基数集合，不套用会话的 64 KiB 请求或准入的 16 MiB 响应上限；并发、截止时间及部署资源仍是实际运行边界。字节处理不持远程 SQL 锁。确定拒绝时清理新头像暂存对象，提交未知时保留字节并先核对资料／回执；旧对象只在确认提交后尽力清理。注册、密码恢复／邮件验证、分组／管理员及多 owner 生命周期继续按[模块台账](/dev/concepts/module-deployment)推进。
