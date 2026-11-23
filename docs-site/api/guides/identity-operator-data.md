---
audience: [dev, ops]
type: reference
status: stable
last_reviewed: 2026-11-23
---

# Identity 运维数据能力

本页记录测试夹具、角色审计和账号运维 CLI 使用的 Identity 数据能力。这些不是普通 API 能力，不授予普通应用 LOGIN。私有 HTTP、账号锁和部署边界见 [Identity 私有运行协议](./identity-runtime)。

## 门控 E2E 夹具

`aap-identity-e2e-fixtures/v1` 是可信测试运维的固定 PostgreSQL 契约，独立于普通
API 的 consumer 配置。`yoao_identity_fixture_v1.account_profiles` 只提供原
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
实际资格见[模块台账](/dev/concepts/module-deployment#identity-门控-e2e-夹具)。

完整 E2E 数据准备／清理使用独立 owner 夹具进程，而非给普通 API 追加这些权限。Identity／Workspace 原生场景将 seed 请求与受限业务 HTTP 显式分流；受限固定能力、私表拒绝和借用事务的检查仍独立保留。owner 通道成功不计作受限角色执行完整 seed 的资格，见[测试边界](/dev/testing)。

## 历史角色审计关系

`aap-identity-role-audit/v1` 是可信运维只读关系契约，独立于普通 HTTP 凭据配置。

| 关系                                         | 字段与用途                                                                          |
| -------------------------------------------- | ----------------------------------------------------------------------------------- |
| `yoao_identity_audit_v1.account_roles`       | id、原始 role、is_active；保留 NULL、未知角色与停用行                               |
| `yoao_identity_audit_v1.invitation_facts`    | id、project_id、role、project_role、expires_at、accepted_at、revoked_at、invited_by |
| `yoao_identity_audit_v1.schema_capabilities` | 单行布尔值 invitation_project_role，表示源库是否存在该准备列                        |

关系没有筛选、排序、权限决策或提交，调用方在原 SQL 中保留作用域、连接和计数。
邀请的项目角色通过整行 JSON 的单字段表达式读取：旧库缺列时为 NULL，列加入后
自动读到新值。整行 JSON 和 email／token 不返回给消费者。

维护者显式运行 `infra/docker/identity-role-audit-grants.sql`，验证目标库及迁移角色、
既有非 LOGIN Identity owner 和受限审计 LOGIN。该步骤兼容普通 Identity schema
出现之前的数据库，不属于审计 CLI 的只读事务，也不修改 Alembic revision。普通
Identity API 仍只有两视图／五函数；审计 LOGIN 仅追加三个审计关系的 SELECT，并
被撤销 User／邀请私表及列权限。其他领域的读取与 Alembic 元数据由原 owner 提供。

审计 CLI 继续使用 `REPEATABLE READ, READ ONLY`，缺少关系或权限时返回退出码 2，
不自动降级为私表读取。安装与调用见[项目角色迁移手册](/ops/runbooks/project-role-migration)；资格和剩余项见
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
第二项能力。`seed_scale.py` 在其受限运行账号上执行这两次选择，夹具 owner 通道
仍负责私有台账和写入；选择权限缺失时不回退 owner。可信运行账号的追加配置是
`identity-scale-seed-grants.sql`；普通消费者仍是既有两视图／五函数，无法执行选择能力。
不能拆成跨连接的先读候选、再选账号。

图片导入只复用既有公开账号事实，无序选择任意首账号，不增加角色或启用条件。
定向与冷运行范围见[运维账号选择](/dev/concepts/module-deployment#identity-运维账号选择)。

## 运维账号 provisioning

开发主种子另使用 `aap-identity-seed-persona/v1` 和固定函数
`create_seed_persona_account(jsonb)`。`seed.py` 先精确查询 email；已有账号（包括停用、
角色或资料漂移的账号）完整复用，缺失时才计算密码哈希并创建，保留中文 `group_name`
和 nullable 值，不创建 Group。函数只插入账号，不隐式授权、审计、提交或重试；并发
重复仍以原始约束错误中止。主脚本继续拒绝 `production`，账号循环后统一提交一次。

迁移新增的函数独立于原 provisioning 合同。可信 `yoao_identity_provisioning_client` 当前
具有三项固定 Identity 函数；普通 Identity API 消费者仍只有两张视图与五项函数。
已应用普通围栏的可信种子运维连接可通过
`infra/docker/identity-seed-operator-grants.sql` 追加精确查询与 persona 创建能力；
该配置拒绝普通 API 账号、角色继承和 User 私表／列权限。
主种子的限定验收见[模块台账](/dev/concepts/module-deployment#主种子账号与演示成员)。

COCO8 和视频示例复用固定 persona 能力，新账号保留中文管理员名称，已有账号原样复用；
大图保留按精确邮箱选择 owner 的行为。这些消费者不新增协议、SQL 函数、角色或公开 REST。
历史定向和冷运行资格见
[模块台账](/dev/concepts/module-deployment#媒体种子-identity-边界)。

Identity operational provisioning 是给 bootstrap/import CLI 使用的内部 SQL owner，不是 HTTP/SDK 账号创建接口。Portable protocol `aap-identity-provisioning/v1` 只有 exact-email lookup 与 `bootstrap_admin`／`import_admin` create 两种模式；`AccountProvisioningManager` 通过 `create_bootstrap_super_admin(...)` 返回 UUID 或 `None`，通过 `ensure_import_admin(environment=...)` 返回 UUID。API caller adapter 不返回可变 User 或 password hash。

Bootstrap 按单个 email 判重，不要求全局用户表为空。已有同 email 行无论 role、active 状态、姓名或 hash 都原样复用，不额外 hash、不写 Audit；不同 email 的 bootstrap admin 可共存。CLI 继续 lowercase/trim email、trim name，owner 不增加 normalization、长度或 password-strength policy。新用户保留 role `super_admin`、active、`status=online`、null group、`email_verified_at=caller now`、generation 0、`preferences={}` 和其余 nullable lifecycle fields null；`created_at` 使用 DB transaction time。Identity 与 Audit owners 在同一 caller transaction 原子写固定 `system.bootstrap_admin`，actor 为新用户，target 为 user UUID，detail 只含 `{email}`；method/path/status/IP 为 null，request ID 来自既有 context 或 null，Audit 时间使用 DB 默认值。应用 wrapper 不重复写 Audit。

Import 只查精确 email `admin`。已有任意 row 都原样复用，包括 inactive、历史或非管理员账号；不存在时环境字符串严格为 `production` 才在 hash/create 前抛原 RuntimeError：

```text
生产环境下未找到 admin 用户,拒绝自动创建弱口令 admin/123456。请先手动创建管理员账号再运行本导入脚本。
```

其它环境（包括 staging）沿用 `admin`／`超级管理员`／bcrypt(`123456`)／`super_admin`／active／`offline`／null `email_verified_at`，不写 Audit。Importer 对 Assets、Workspace、Labeling 的 SQL 提交/回滚与账号 provisioning 保持一个事务，失败回滚全部业务行。

Migration 0252 只增加只读 `read_provisioning_account(text)` 和 mutable `create_provisioning_account(jsonb)` 两个 Identity 函数。当前运维账号权限以 `identity-provisioning-grants.sql` 与 `identity-seed-operator-grants.sql` 为准。Audit 由固定 `yoao_audit_v1.record_identity_bootstrap_admin(uuid,text,text)` capability 写入，Identity 没有 Audit table 权限。Native SQL constraint／duplicate 错误原样抛出；相同 email 并发创建仍以 `23505` 中止事务，无 conflict-ignore、重试或 adapter rollback。读路径遵循 `autoflush/no_autoflush`；create 即使在 `no_autoflush` 下也显式 flush caller pending work。没有新 runtime DSN、setting 或独立服务；verifier-only `AAP_MODULE_DATA_IDENTITY_PROVISIONING_URL` 不是应用配置。

非锁定 Project authority 查询复用 `read_actor_facts`；写入复用[账号事实锁](./identity-runtime#调用方事务内的账号事实锁)。既有锁序、等待和 HTTP 错误映射不变。需要刷新 Project ORM 的调用方仍须刷新；仅检查 active 的守卫不增加 role 检查。证据与精确默认见 [ADR-0132](/dev/adr/0132-identity-operational-provisioning-and-authority-reads)。
