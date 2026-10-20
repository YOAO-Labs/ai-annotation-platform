---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-21
---

# Workspace 项目成员运行协议

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

独立 runtime 要求 `WORKSPACE_MEMBERSHIP_DATABASE_URL` 或 `_FILE` 和 `WORKSPACE_MEMBERSHIP_CONTROL_TOKEN` 或 `_FILE`。运行默认值为 `WORKSPACE_HOST=0.0.0.0`、`WORKSPACE_PORT=8172`、`WORKSPACE_TIMEOUT_SECONDS=30`、`WORKSPACE_MAX_CONCURRENCY=8`。可选 Compose 使用宿主机 `WORKSPACE_RUNTIME_MEMBERSHIP_DATABASE_URL_FILE` 与 `WORKSPACE_RUNTIME_CONTROL_TOKEN_FILE` 选择 secret 文件，再映射为 runtime 内部变量。API runtime URL、API data DSN 与 standalone runtime DSN 是三种明确的绑定方式；空 selector 不会阻止现有 API 启动，API 的默认 caller Session 仍可用。

## 当前范围

Workspace membership slice 的实现仍在本地 qualification 阶段，不能据此宣称独立部署或跨语言替换已通过。项目创建／转移、配置、模板、列表、mention 与其他 Workspace consumers 仍使用各自现有路径。其余模块、完整 Identity、十模块／七门槛目标、生产容量验证与 primary cutover 也仍未完成。Go 月报保持多语言架构实验，不构成该 owner 的语言提供方。
