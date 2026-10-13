---
audience: [ops]
type: reference
since: v0.1.0
status: stable
last_reviewed: 2026-09-19
---

# 安全模型

> 适用读者：负责评估平台安全姿态的工程师 / 合规人员；想了解某个能力背后的访问控制规则的开发者。
>
> 这份文档描述「平台内置」的安全机制。HTTPS / WAF / 网络隔离等基础设施层属于部署侧，见 [`deploy.md`](/ops/deploy/docker-compose)。

---

## 1. 威胁模型摘要

| 威胁                    | 缓解                                                                           | 实现位置                                                                                             |
| ----------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| 凭证泄露（撞库 / 钓鱼） | 密码强度 8+ 大小写数字 + 失败登录限流 + JWT 黑名单                             | `apps/api/app/core/password.py`、`auth.py:48`（5/min）、`apps/api/app/core/token_blacklist.py`       |
| 越权访问                | 平台角色 + 项目成员角色双层授权，项目能力集统一解析                            | `apps/api/app/services/project_access.py`、`apps/api/app/core/permissions.py`、各路由 `Depends(...)` |
| 邀请滥用 / 注册刷号     | `MAX_INVITATIONS_PER_DAY` + 开放注册 3/min 限流 + viewer 默认零权限            | `apps/api/app/services/invitation.py`、`auth.py:173`                                                 |
| 审计日志篡改            | PG `BEFORE UPDATE/DELETE` 触发器拒写                                           | `alembic/versions/0032_audit_log_immutability.py`                                                    |
| 数据泄露（导出滥用）    | 导出端点写审计 + 计划中下载者签名水印                                          | `audit.py:AuditAction.PROJECT_EXPORT/BATCH_EXPORT`                                                   |
| CSRF                    | JWT 走 `Authorization: Bearer` + CORS 白名单 + production methods/headers 收紧 | `main.py:71-83`                                                                                      |
| XSS                     | React 默认转义 + 不允许 `dangerouslySetInnerHTML` 用户输入                     | （前端约定）                                                                                         |
| 拒绝服务                | 请求级 SlowAPI 限流 + ML 调用超时 + Redis ConnectionPool 上限                  | `core/ratelimit.py`、`config.py:54-55`、`api/v1/ws.py:26`                                            |
| 敏感字段进日志          | Sentry `before_send` 屏蔽 Authorization                                        | `main.py:28-36`                                                                                      |

威胁模型不包含：物理访问 PG、root SSH 入侵 API 主机——这些走部署侧的访问控制。

---

## 2. 平台角色、项目角色与权限

平台把**账号身份**与**项目职责**分开。账号级 `PlatformRole` 4 级（`apps/api/app/db/enums.py:22`）：

```
super_admin > project_admin > employee > viewer
```

项目级 `ProjectRole`（存储在 `project_members.role`）：`annotator` / `reviewer` / `viewer`。

项目访问与写权限来自**资源所属项目的有效成员关系与项目角色**，集中解析在 `apps/api/app/services/project_access.py::resolve_project_access`。历史全局 `annotator` / `reviewer`（`UserRole`，`db/enums.py:4`）只用于迁移适配与历史读取，**不**参与新授权，也不再作为 fallback。

### 2.1 平台能力

| 能力                      |    super_admin     |         project_admin          | employee | viewer |
| ------------------------- | :----------------: | :----------------------------: | :------: | :----: |
| 创建项目                  |         ✅         |               ✅               |    ❌    |   ❌   |
| 删除项目                  |         ✅         |            仅 owner            |    ❌    |   ❌   |
| 邀请用户                  | ✅（全部平台角色） | employee / viewer（≤ MAX/day） |    ❌    |   ❌   |
| 平台角色变更（含预览）    |         ✅         |               ❌               |    ❌    |   ❌   |
| 项目成员角色变更          |         ✅         |   ✅（仅自己 owner 的项目）    |    ❌    |   ❌   |
| 查看审计日志              |        全部        |            项目相关            |    ❌    |   ❌   |
| 系统设置（`/settings/*`） |       读+写        |              仅读              |    ❌    |   ❌   |
| 项目绩效明细 / CSV        |         ✅         |            仅 owner            |    ❌    |   ❌   |

### 2.2 项目能力（由项目角色决定）

`resolve_project_access` 产出固定能力集：`project.read` / `project.manage` / `member.read` / `member.manage` / `task.read` / `annotation.write` / `review.write` / `export.annotations` / `performance.read`。

| 能力                  |   annotator    |   reviewer   | viewer |     合法管理者      |
| --------------------- | :------------: | :----------: | :----: | :-----------------: |
| 项目 / 指引读取       |       ✅       |      ✅      |   ✅   |         ✅          |
| 标注写 / AI 接受      | ✅（任务检查） |      ❌      |   ❌   | 既有管理路径 + 检查 |
| 审核 / 通过-退回      |       ❌       | ✅（非自审） |   ❌   |  既有路径 + 非自审  |
| 全项目 / 选定任务导出 |       ❌       |      ✅      |   ❌   |         ✅          |
| 成员 / 配置管理       |       ❌       |      ❌      |   ❌   |   ✅（所管项目）    |

能力是**必要非充分**条件：任务级指派、批次默认、开放池、预留审核、管理员锁、乐观版本、Mask QC 与视频边界检查全部保留。API key scope 只做附加交集，`*` 不授予项目访问。JWT 里的 `role` 不直接授权；Socket 与异步作业在各自边界重新解析当前数据库权限。

> 邀请角色与编辑角色使用不同的服务端白名单；`project_admin` 不能邀请 `project_admin` / `super_admin`，平台角色变更只限超级管理员。
>
> `annotator_id` 单值绑定到 `batch.annotator_id`，同一 batch 内任务都派给该一人；reviewer 通过 `task.reviewer_id` 锁定。

### 2.3 项目访问解析

`project_members(project_id, user_id, role)` 是项目职责的唯一来源，保留 `UNIQUE(project_id, user_id)`。解析顺序（`services/project_access.py`）：

1. 有效 `super_admin` 放行；
2. 项目 owner / 合法 `project_admin` 按管理路径；
3. 命中 `ProjectMember`：按项目角色映射固定能力集；
4. 无成员关系即拒绝——**不再**回退到全局 `User.role`，未知 / 非法角色 fail closed。

成员角色变更走“预检 → CAS 写入 + 原子交接”，见 [可见性与权限](../../dev/concepts/visibility-and-permissions#成员角色变更与交接)。完整迁移、回滚与已签发 URL 限制见[员工项目角色迁移与回滚 runbook](../runbooks/project-role-migration.md)。

---

## 3. 鉴权链路

### 3.1 JWT 生命周期

平台使用对称 JWT（HS256），密钥从 `SECRET_KEY` env 读，默认值在 production 启动**会触发 RuntimeError**（`apps/api/app/main.py:50-57`）。

Token claims：

```json
{
  "sub": "<user_uuid>",
  "role": "<UserRole value>",
  "jti": "<token_uuid>", // 用于黑名单
  "gen": 0, // 用户代际号；改变即旧 token 全失效
  "exp": 1745020800,
  "iat": 1744934400
}
```

默认 TTL = 24 小时（`ACCESS_TOKEN_EXPIRE_MINUTES`）。

### 3.2 注销机制

<ExcalidrawDiagram
  src="/diagrams/ops/security/logout-lifecycle.svg"
  alt="单设备登出将 JWT jti 加入 Redis 黑名单，全设备登出递增用户 token generation 使旧 token 失效"
  caption="JWT 单设备与全设备注销链路"
/>

实现：

- `apps/api/app/core/token_blacklist.py` — `blacklist_token` / `is_blacklisted` / `increment_user_generation` / `get_user_generation`
- `apps/api/app/core/security.py:decode_access_token` 在解析后查 jti 黑名单 + gen 比对
- 前端 hook：`apps/web/src/api/auth.ts` 调 `/auth/logout` 后清 localStorage + 跳登录页

`logout` 黑名单 TTL = token 剩余有效期，自动随 `exp` 过期淘汰，不会无限增长。

### 3.3 密码

`apps/api/app/core/password.py:validate_password_strength`：

- 长度 ≥ 8（`auth.py:36`）
- 至少包含一个大写字母、一个小写字母、一个数字
- 不限符号（兼容性优先）

前端 RegisterPage / InvitationAcceptPage 用同一规则做实时强度提示。

### 3.4 失败登录限流

`auth.py:48` 的 `@limiter.limit("5/minute")` 装饰器（slowapi，按 IP）。失败时审计行 `auth.login` 状态码 401 + `detail.user_agent`（截前 256 字符），便于事后分析。

> 这是请求级限流，不防分布式拨号。如果开放注册放量后看到刷号迹象，下一步上 hCaptcha / Turnstile（ROADMAP §A 已列）。

---

## 4. 邀请流程

<ExcalidrawDiagram
  src="/diagrams/ops/security/invitation-lifecycle.svg"
  alt="管理员创建并复制邀请链接、被邀请人公开解析 token、注册后保留邀请接受记录并签发访问令牌的流程"
  caption="邀请链接的创建、解析与接受生命周期"
/>

要点：

- `project_admin` 与 `super_admin` 调用 `POST /api/v1/users/invite`。项目管理员只能邀请 reviewer、annotator 或 viewer，超级管理员可指定全部角色；角色范围由服务端强制执行。每个发起人的新建邀请按过去 24 小时滚动统计，默认上限 30；已激活邮箱会被拒绝。项目管理员新建邀请时只会使自己对同邮箱的未接受旧邀请过期，超级管理员则会使该邮箱的所有未接受邀请过期。
- TTL 从运行时 `invitation_ttl_days` 系统设置读取，默认 7 天。API 返回 `invite_url / token / expires_at`，管理端复制链接并通过外部安全渠道分享；平台不发送邀请邮件。
- 被邀请人先调用 `GET /api/v1/auth/invitations/{token}`。不存在的 token 返回 404，已接受、已撤销或过期返回 410，只有有效 token 才返回 email、角色、数据组、过期时间与邀请人。
- 接受邀请使用 `POST /api/v1/auth/register {token,name,password}`。用户以邀请角色创建并视为邮箱已验证；指定数据组时会复用或创建正式 `groups` 记录并写入用户的 `group_id`。邀请行不会删除，而是写入 `accepted_at / accepted_user_id`，同时记录 `user.register` 审计，再返回 access token 与用户。
- 项目管理员的邀请列表、撤销和重发都限定为自己创建的记录；超级管理员可查看和管理全部邀请。被撤销的邀请只有原创建者或超级管理员重发后才会生成新 token 并重新生效，项目管理员不能重发历史高权限邀请；升级时会撤销由不具备有效超级管理员权限的签发者创建、尚未接受的存量高权限邀请。

::: warning 历史高权限账号复核
系统不会仅根据签发者的当前角色自动停用已注册账号，以免误伤由后续合法降级的超级管理员签发的邀请。运维人员应在审计日志中复核 `user.invite` 事件：当目标角色为 `project_admin` 或 `super_admin`、且 `actor_role` 不是 `super_admin` 时，确认对应账号合法性并停用未授权账号。
:::

::: warning 邀请链接是 bearer credential
任何持有尚未失效邀请链接的人都能以链接中分配的角色完成注册。明文 token 会存入邀请表并返回给管理端，应只通过受控渠道分享，避免写入公开聊天、工单或日志。
:::

### 4.1 开放注册

`POST /auth/register-open`，`ALLOW_OPEN_REGISTRATION=true` 时启用。新用户角色固定 `viewer`（最低权限），3/min 限流。

**邮箱验证**：由 `REQUIRE_EMAIL_VERIFICATION` 控制，留空时按环境派生（production 默认开、dev/staging 默认关）。开关打开时，开放注册后 `email_verified_at` 为空、登录被 `400 {code: "email_not_verified"}` 拦截，须点验证邮件链接（`POST /auth/verify-email`，24h 一次性 token）后方可登录；`POST /auth/send-verification-email` 可重发（防枚举恒 202）。邀请注册与管理员建号恒视为已验证。验证邮件复用 SMTP 配置；SMTP 未配置时仅把验证链接写入日志（dev 友好）。

CAPTCHA 使用 Turnstile（见 §3）。

---

## 5. 审计日志

### 5.1 字段

`audit_logs` 表（`apps/api/app/db/models/audit_log.py`）：

| 字段                                  | 类型        | 说明                                                                                |
| ------------------------------------- | ----------- | ----------------------------------------------------------------------------------- |
| `id`                                  | bigint      | 主键                                                                                |
| `actor_id` `actor_email` `actor_role` | —           | 行为发起人三元组（actor 删除后仍保留 email/role 快照）                              |
| `action`                              | str         | `AuditAction` 枚举值，见 `services/audit.py:14-66`                                  |
| `target_type` `target_id`             | str         | 受影响实体（`task` / `user` / `project` / `batch` / ...）                           |
| `method` `path` `status_code`         | —           | HTTP 请求三元组（来自 AuditMiddleware 或显式打点）                                  |
| `ip`                                  | str         | `X-Forwarded-For` 头第一个值，否则 `request.client.host`                            |
| `detail_json`                         | jsonb       | 自由结构；常见键：`user_agent`、`result`、`new_generation`、`from_role` / `to_role` |
| `request_id`                          | str         | 关联同请求其它日志 / Sentry                                                         |
| `created_at`                          | timestamptz | 默认 `now()`                                                                        |

### 5.2 不可变性

```sql
-- alembic/versions/0032_audit_log_immutability.py
CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_logs ...
CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_logs ...
```

任何 `UPDATE` / `DELETE` 触发 `RAISE EXCEPTION 'audit_logs rows are immutable'`。

**唯一豁免路径**：GDPR 用户数据清除任务在 session 中执行 `SET LOCAL app.allow_audit_update = 'true'` 后才能删行（`apps/api/app/services/gdpr_*` 类）。pg_restore / pg_dump --data-only 走 COPY 不走 UPDATE，备份恢复不受影响。

### 5.3 已打点的 action 一览

参见 `apps/api/app/services/audit.py:AuditAction` 枚举。涵盖：

- 认证：`auth.login` / `auth.logout` / `auth.logout_all`
- 用户：`user.invite` / `user.register` / `user.role_change` / `user.password_change` / `user.deactivate`
- 项目：`project.create/update/delete/transfer/member_add/member_remove`
- 批次：`batch.created/status_changed/rejected/reset_to_draft/deleted/distribute_even`、`batch.bulk_*`
- 任务：`task.submit/withdraw/review_claim/approve/reject/reopen`
- 标注：`annotation.create/update/delete/attribute_change/comment_add/comment_delete`
- 数据集：`dataset.create/delete/link/unlink`
- 导出：`project.export/batch.export`
- Mask 格式导入：`mask_format.import_preflight/import_execute/import_resume`
- bug 反馈：`bug_report.created/status_changed`
- 系统：`system.bootstrap_admin`

新加 action 时务必在枚举里登记——`AuditService.log` 接受 `str | AuditAction`，运行期不强校验，但 `AuditService` 路径是查 IDE 引用唯一可靠的入口。

---

## 6. HTTP 响应头与 CORS

### 6.1 Production 安全响应头

`apps/api/app/middleware/security_headers.py` 在 `environment == "production"` 时由 `main.py` 注册（详见 [ADR-0010](/dev/adr/archive/0010-security-headers-middleware)）。dev / staging 不启用，避免本地热更新被 inline script 打挂。

| Header                      | Value                                 |
| --------------------------- | ------------------------------------- |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` |
| `X-Content-Type-Options`    | `nosniff`                             |
| `X-Frame-Options`           | `DENY`                                |
| `Referrer-Policy`           | `strict-origin-when-cross-origin`     |
| `Content-Security-Policy`   | 见下文                                |

**CSP 当前为 nonce 收紧版**：

```
default-src 'self';
img-src 'self' data: blob: https:;
style-src 'self' 'nonce-$request_id';
script-src 'self' 'nonce-$request_id' https://challenges.cloudflare.com;
frame-src https://challenges.cloudflare.com;
connect-src 'self' https: wss: ws:;
font-src 'self' data:;
object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

生产 HTML 由 Nginx 注入 CSP header，并通过 `sub_filter` 把 vite build 时写入的 `__CSP_NONCE__` 占位符替换成同一个 `$request_id`；API 响应路径不含 HTML，由 FastAPI middleware 使用无 nonce 的 `style-src 'self'` / `script-src 'self' https://challenges.cloudflare.com`。`https://challenges.cloudflare.com` 是 Turnstile widget 的固定来源。

**新增第三方依赖时的 checklist**：

- 加 Sentry CDN？追加 `script-src https://*.sentry-cdn.com`
- 加 Google Fonts？追加 `font-src https://fonts.gstatic.com` + `style-src https://fonts.googleapis.com`
- 加第三方 ML backend iframe？追加 `frame-src` 对应域

部署前先用 `max-age=300`（5 min）灰度 24h 确认 https 稳定，再切换到默认 1 年值——deploy.md 有 SOP。

`/metrics` 由独立 ASGI 子应用挂载，不经过 SecurityHeadersMiddleware，避免内网 scrape 被 HSTS 影响。

### 6.2 CORS

由 `apps/api/app/main.py` 注册：

| 维度                 | development / staging           | production                            |
| -------------------- | ------------------------------- | ------------------------------------- |
| `allow_origins`      | 默认 `localhost:3000/3001/5173` | 必填 `CORS_ALLOW_ORIGINS`（启动断言） |
| `allow_origin_regex` | `http://localhost:\d+`          | 自动失效（`config.py:36-41`）         |
| `allow_methods`      | `*`                             | 显式白名单 `cors_allow_methods`       |
| `allow_headers`      | `*`                             | 显式白名单 `cors_allow_headers`       |
| `allow_credentials`  | `True`                          | `True`                                |

production 收紧的目的：避免误把 dev regex 上线放任何 localhost 端口；避免接受未声明的 method 让某些奇怪 path 被探到。

---

## 7. 数据泄露面

### 7.1 导出端点

项目 / 批次与选定任务导出均要求显式 `export.annotations` 能力：仅项目 `reviewer` 与合法管理者，项目 `annotator` / `viewer` 被拒。这是相对“项目可见即可导出”的**故意收紧**，并在导出创建、worker 执行、缓存命中与结果访问四个时点都校验（缓存命中不等于已授权）。`GET /api/v1/projects/{id}/export?format=...` 和 `GET /api/v1/projects/{id}/batches/{bid}/export` 都会写 `audit_logs.action = project.export / batch.export`，detail 含 `format` + `task_count`。审计页可按 `action ILIKE '%.export'` 筛查异常导出。

下载者签名水印（PDF/zip 内嵌发起人邮箱）仍在路线图中，当前导出审计依赖 audit log 和对象存储访问日志。

### 7.2 文件存取

Mask 格式 staged upload 只能使用服务端为当前项目和用户生成的 object key 前缀。预检与执行分别复核 SHA-256，
15 分钟 receipt 只持久化 token hash，并绑定 adapter / manifest 版本、mapping、options 与 plan digest。archive reader
拒绝路径穿越、绝对路径、重复或大小写折叠路径、symlink、压缩炸弹和悬空 manifest 引用，不使用 `extractall`。

MinIO 的 presigned **GET / download URL** 默认 1 小时 TTL（`apps/api/app/services/storage.py`）。下载签名会把过期时刻对齐到 10 分钟网格，让同一对象在一个窗口内签出完全相同的 URL——浏览器据此缓存缩略图与原图，避免列表刷新后整批重下。代价是实际有效期在 1 小时到 1 小时 10 分之间浮动。presigned PUT / upload URL 仍使用调用方传入的原始 `expires_in`，不做网格对齐。每个 URL 只授权单文件，不含目录列表能力。

**已签发 URL 是撤销的已知边界**：撤销成员 / 角色后不再签发新 URL，也不允许新的结果交付，但**已签发的直接存储 URL 在到期前仍可使用**。当前默认有效期保持不变——下载 1 小时（含 10 分钟对齐）、评论附件 5 分钟（无对齐）、导出 7 天（同样受调用方的 10 分钟对齐约束）。立即失效旧 URL 需要单独的存储 / 代理改造，不在本次角色迁移范围内；运维侧的完整边界与演练见[员工项目角色迁移与回滚 runbook](../runbooks/project-role-migration.md)。

### 7.3 Sentry 数据脱敏

`apps/api/app/main.py:28-36` 的 `_sentry_before_send` 钩子把 `Authorization` header 改为 `[REDACTED]`。同时 `send_default_pii=False`，Sentry 不会上传 cookies / 用户 IP。

---

## 8. 安全测试与监控

- **自动测试**：`apps/api/tests/test_auth.py` 覆盖密码策略、限流、JWT 过期。`test_audit_immutability.py` 覆盖触发器。
- **CI 静态扫描**：未启用（ROADMAP P3 待评估 bandit / semgrep）。
- **依赖扫描**：未配置（dependabot / renovate 待加）。
- **入侵检测**：依赖部署侧（CloudFlare WAF / fail2ban）。
- **日志告警**：建议在 Loki / Datadog 上加规则：
  - 单 IP 5min 内 `auth.login` 401 ≥ 20 → 告警
  - `system.bootstrap_admin` 写入 → 直接 PagerDuty
  - `audit_logs rows are immutable` 抛错 → P0（说明有人尝试改审计）

---

## 9. 关键文件索引

| 主题                       | 路径                                                       |
| -------------------------- | ---------------------------------------------------------- |
| 角色枚举                   | `apps/api/app/db/enums.py`                                 |
| 项目授权解析               | `apps/api/app/services/project_access.py`                  |
| 成员角色变更 / 交接        | `apps/api/app/services/project_membership.py`              |
| 路由权限装饰器             | `apps/api/app/core/permissions.py`                         |
| 密码策略                   | `apps/api/app/core/password.py`                            |
| JWT 编解码                 | `apps/api/app/core/security.py`                            |
| Token 黑名单               | `apps/api/app/core/token_blacklist.py`                     |
| 限流                       | `apps/api/app/core/ratelimit.py`                           |
| 审计服务                   | `apps/api/app/services/audit.py`                           |
| 审计中间件                 | `apps/api/app/middleware/audit.py`                         |
| 审计不可变 trigger         | `apps/api/alembic/versions/0032_audit_log_immutability.py` |
| 邀请服务                   | `apps/api/app/services/invitation.py`                      |
| Bootstrap super_admin      | `apps/api/scripts/bootstrap_admin.py`                      |
| 认证路由                   | `apps/api/app/api/v1/auth.py`                              |
| CORS / production 启动断言 | `apps/api/app/main.py:50-83`                               |
