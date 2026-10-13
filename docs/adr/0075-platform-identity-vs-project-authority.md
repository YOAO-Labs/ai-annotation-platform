# 0075 — 平台身份与项目职责分离：员工按项目成员角色授权

- **Status:** Proposed（本地分阶段实现中，见 `docs/plans/1789807315_project-scoped-employee-roles.md`；尚未部署）
- **Date:** 2026-09-19
- **Deciders:** core team
- **Supersedes:** —（修订 [ADR-0005](archive/0005-task-lock-and-review-matrix.md) 的角色矩阵口径，不推翻其锁与流转语义）

## Context

平台原本把“标注员 / 质检员”当作账号级全局角色（`users.role`）。同一个账号无法在项目 A 做标注、在项目 B 做质检，因为所有授权判断都读一个全局字符串。随着项目分裂、外部标注团队并行的场景增多，出现了三类问题：

- **授权来源错位**：任务可见性、批次流转、导出、工作台写入口、Socket 与异步作业结果各自散落 `role == "reviewer"` / `role == "annotator"` 判断，新增一个角色要改多处；任何一处遗漏就是越权。
- **跨项目串权**：项目成员表 `project_members` 其实已经支持“一个用户在一个项目一个角色”且带 `UNIQUE(project_id, user_id)`，但调用方仍要求全局角色与成员角色同时成立，导致“A 标注 / B 质检”不可表达，或依赖管理员手工改全局角色。
- **安全边界模糊**：全项目导出、自审（提交者审核自己的内容）、绩效可见性等高风险动作缺乏显式、可测试的能力判据；全项目导出此前只依赖“项目可见”，比前端权限表更宽松。

同时，`annotations` / 审核事实 / 审计 / 绩效归属都记录了当时的用户 ID，历史贡献不应随当前项目职责变化而重写。数据库中的角色列是字符串而非 PostgreSQL enum，迁移必须可审计、可回滚到明确的边界。

| 选项                                                    | 主要卖点                                     | 主要劣势                                                        |
| ------------------------------------------------------- | -------------------------------------------- | --------------------------------------------------------------- |
| **平台身份 + 项目成员角色（本决策）**                   | 复用既有成员模型；一处解析、多处复用；可解释 | 需要数据迁移与跨层协调切换（API / worker / 前端同版本）         |
| 保留全局 annotator / reviewer，用更多全局角色表达多项目 | 改动小                                       | 角色数随项目组合爆炸；无法表达“A 标注 / B 质检”；授权仍散落各处 |
| 引入通用 RBAC 策略引擎 / 权限目录                       | 长期灵活                                     | 引入新依赖与配置面；当前需求不需要策略语言；安全审计面反而更大  |
| 同时在成员表与全局角色维护“双授权”                      | 迁移期看似平滑                               | 永久双轨最危险：两个来源冲突时谁生效不可解释，等于没有单一真值  |

## Decision

引入一个独立的请求级授权解析器，把**平台身份**（账号级）与**项目职责**（成员级）分开，授权一律来自“资源所属项目中的有效成员关系 + 固定能力集”。不新增策略引擎、权限目录或通用 provider 抽象。

### 平台角色与项目角色

- 平台角色 `PlatformRole`：`super_admin` / `project_admin` / `employee` / `viewer`，继续存储在账号 DTO 与数据库的 `role` 字段（列名不变）。`db/enums.py:22`。
- 项目角色 `ProjectRole`：`annotator` / `reviewer` / `viewer`，存储在 `project_members.role`，保留 `UNIQUE(project_id, user_id)`。`db/enums.py:31`。
- 全局 `annotator` / `reviewer` 仅作为**历史值**保留在 `UserRole` 中供迁移适配器、审计与历史数据读取；新授权模型永不从它们取权。`db/enums.py:49`。

一个 `employee` 可以在 A 项目是 `annotator`、在 B 项目是 `reviewer`、在 C 项目是只读 `viewer`，无需登出或切换全局身份；无成员关系即无项目访问，除非账号本身是合法管理者。

### 权威解析与能力集

新增 `apps/api/app/services/project_access.py`：`resolve_project_access()` 依据“资源 ID → 实际项目 → 负责人 / 成员关系 + 项目角色”产出不可变请求上下文（`user_id`、`project_id`、平台角色、项目角色、membership ID/version、管理者分类与固定 `ProjectCapability` 集合）。解析器拒绝上下文与资源项目不一致；未知角色 fail closed。`deps.py` 只做薄接线。

能力集（`project_access.py:51`）：`project.read` / `project.manage` / `member.read` / `member.manage` / `task.read` / `annotation.write` / `review.write` / `export.annotations` / `performance.read`。

- 能力是必要条件而非充分条件：任务级指派覆盖、批次默认、开放池、审核预留、管理员锁、乐观版本、Mask QC、视频边界检查全部保留。
- SQL 谓词与对象级检查必须一致，复用 `effective_task_assignee_id` / `effective_task_reviewer_id` 及其 SQL 对应实现，不新增第二套指派解释。
- API key scope 仍作为附加交集；`*` 不授予项目访问。Socket / 异步作业 / 导出缓存在各自边界重新解析当前权限，不信任旧 JWT 角色。

### HTTP 契约

- `GET /api/v1/projects/{project_id}/access`（新增 `ProjectAccessOut`）：返回 `project_id` / `user_id` / `platform_role` / `project_role` / `membership_id` / `membership_version` / `access_kind`（`super_admin`|`owner`|`member`）/ `is_manager` / `capabilities`。管理者 membership 字段可为 null。
- 成员输出 `ProjectMemberOut` **扁平**：`role` 是项目角色，新增扁平 `platform_role` 是平台角色；不引入嵌套 user 或角色别名。`apps/api/app/schemas/project.py:372`。
- `POST /projects/{project_id}/members/{member_id}/role/preview`（新增，只读）：目标角色、阻塞项、受影响资源快照与 `preview_token`。
- `PATCH /projects/{project_id}/members/{member_id}/role`（新增）：要求 `project_role` + `expected_version` + `preview_token` + `reason`，可带显式接替人；锁内重校验，陈旧版本 / 快照失效 / 资源占用返回 409。
- 邀请 HTTP 入参 / 出参继续使用既有名称 `project_member_role`，显式映射到数据库 `project_role`，与账号 `role` 分离；`project_members.role` 仍叫 `role`。
- 项目列表 / 详情新增 `my_project_role`；列表 `project_role` 过滤使用成员角色，不再把 `role=reviewer` 解释为“任意带 reviewer 成员关系的员工”。
- 标注员 / 质检员仪表盘复用既有端点：接受 `employee`，按各自的成员角色过滤负载，缺失角色返回空工作集，不新增员工仪表盘 API。

### 有意收紧的行为

- **全项目导出**：显式能力 `export.annotations`，仅质检员与合法管理者；标注员 / 观察者被拒。这是相对“项目可见即可导出”的**故意**收紧，同样作用于导出创建、worker 执行、缓存命中与结果访问。
- **自审**：拒绝提交者对自己提交内容的领取 / 编辑 / 通过 / 退回（含管理者动作），不做隐式超管豁免。依据不是 `users.role`，而是冻结的贡献者证据。
- **绩效**：项目绩效明细与 CSV 仍仅负责人 / 超管；导出标注不等于可看他人绩效。

### 审阅贡献者证据（自审的持久依据）

在任务上新增 `annotation_contributor_ids`（可空 JSONB，标注阶段参与者的去重并集）、`review_contributor_ids`（冻结在 `review_round_id`）、`review_submitter_id`。所有标注阶段变更（含批量、撤销 / 恢复、转换、导入、AI 接受、视频 / 场景 / 多相机）在任务锁事务内累积**实际操作者**，而不只是原始作者。

- “已知为空”与“未知”不同：`annotation_contributor_ids = []` 表示尚无已记录的贡献者，但它单独不能证明审核轮次完整；列为 `NULL`（旧二进制或迁移前创建）、结构畸形，或审核轮次缺少有效 `review_round_id` + `review_submitter_id` + 完整集合，才是“未知”。未知是黏性的：不得因为新增一条标注就把 `NULL` 当成“已知集合”；未知证据的审核写入返回 `409 review_contributors_unknown` 并阻断，不按全局角色回退。**平台不附带自动的贡献者回填工具**，修复需单独批准。
- 领取 / 编辑 / 通过 / 退回（含批处理与改变已审内容的 QC 接受）都校验当前冻结证据；改派不得移除已冻结贡献者。
- 保留既有首审绩效事实，不改历史归属。

### 成员变更、CAS 与原子交接

成员角色变更走“预检 → 写入”两阶段：预检产出阻塞项、受影响任务 / 批次 / 锁 / 认领 / 可用质检员快照与 token；写入在锁内重读、重校验，按 `current_version` 做 CAS，并**在一个事务内**完成交接、释放受影响项目的锁 / 认领、更新角色 / version 与审计。`expected_version` 只保护成员编辑本身，不能检测新指派，因此快照与事务内校验同时必需；不依赖 SQLAlchemy mapper versioning 保护批量 UPDATE。

- 并发沿用仓库既有模型：授权写入用 `FOR SHARE`，成员变更用 `FOR UPDATE NOWAIT`，资源获取失败即回滚 409，不形成等待环、不部分交接。
- 删除 / 重新加入成员产生新 member ID，重放旧变更不会用复用版本成功。
- 已授权且持锁的写入可在成员变更提交前完成；提交后的新写入必须失败。长推理不得持 DB 锁计算，最终写事务内重新授权。

### 账号生命周期与平台角色变更

- 全局停用 / 离职继续走既有跨项目管理边界与凭据清退；修正旧的“全局角色必须等于成员角色”假设，合法的 A 标注 / B 质检不再被判为 `historic_mixed_role` 阻塞。
- 平台角色预览 / 变更仅超级管理员；降级时阻止不兼容成员关系、所有权或未完成工作，且**不**级联改其他项目。保留自我变更与最后一位超管保护。

### 迁移与回滚边界

- `0173_project_role_preparation.py`：加性、向前 / 向后兼容的字段（可空列，或带默认值的非空列，如 `project_members.version`）与只读审计，旧二进制可继续运行。**Increment A 的准备层可独立兼容部署且不改变现有授权**；只有完整的 Increment B 是单次协调切换，其内部工作包（B1…）不可分别部署。`0174_project_role_conversion.py`：只把全局 `annotator` / `reviewer` 转为 `employee`，回填**待处理**邀请的项目角色，保留 ID、停用状态与全部历史；它不改写成员关系行 / 角色；**不**从全局角色创建成员关系，缺失成员关系保持缺失并 fail closed。
- 0174 的 downgrade **在任何窗口都始终抛错**，即使是在重开写入之前的冻结窗口；不存在“旧二进制 + 自动降级”的旧模型回退。前置恢复只有一种：恢复**精确的迁移前快照**并回退旧二进制。重开写入之后，旧全局角色模型无法无损表达跨项目职责，只能向前修复（支持新 schema / 模型的新二进制）或把备份恢复视为一次显式业务数据回滚。相关 runbook 见 `docs-site/ops/runbooks/project-role-migration.md`。
- 贡献者证据只有在**所有** API / worker 写入生产者都已部署（旧写入经过排空边界）后才可信；混合新旧写入者无法建立完整来源。旧二进制回滚后加性列不再维护，重开写入前必须归档已收集证据并把受影响可变任务的完整性标记为未知。
- 最终 CHECK 约束属于兼容性清理阶段；在此之前未知 / 意外遗留值由运维按审计闸门逐行核对，禁止自动猜测或补建成员关系。

## Consequences

正向：

- 单一授权真值：项目角色 + 固定能力集，新增 / 审计点收敛到 `project_access.py`；跨项目职责无需换号。
- 安全边界显式化：导出收紧、自审按持久证据拒绝、异步 / Socket 边界重新授权，均有可测试判据。
- 历史无损：账号 ID、标注、审核事实、审计与绩效归属不随职责变化重写；迁移只做确定性转换。
- 复用既有并发与成员模型，不引入新依赖、配置或 RBAC 引擎。

负向：

- 必须是 API / worker / 前端**同版本**协调切换；前端发 `employee` 而后端未匹配、或后端放开员工而兄弟端点仍保留全局质检旁路，都会破坏一致性。
- 遗留任务中“未知审阅证据”在单独批准的修复完成前会**阻断**对应审核写入（平台不附带自动回填工具）；运维需识别受影响任务并约定恢复方式（批准的数据修复或用单独归属的新任务替换）。
- 已有签发的直接存储 URL 在撤销后仍有到期前的可用窗口（当前下载默认 1 小时、最多 10 分钟对齐；评论附件 5 分钟无对齐；导出 7 天且调用方另受对齐约束）。立即失效需单独规划存储 / 代理改造。
- 旧二进制在 0173 之后仍可运行但不再维护加性列；回滚重开写入前必须归档已收集的授权证据并把受影响可变任务标记为“完整性未知”。

## Alternatives Considered

**保留全局 annotator / reviewer 并扩展全局角色**：无法表达“一个账号在不同项目承担不同职责”，且授权判断继续散落在任意读取 `users.role` 的位置，安全面随时间单调变差。

**通用 RBAC 策略引擎 / 权限目录**：为当前固定能力集引入策略语言、持久化权限目录与运行时评估，扩大配置面与审计面，收益不足以抵消复杂度；能力集用枚举 + 显式函数即可。

**成员表 + 全局角色双授权**：迁移期看似平滑，但两个来源冲突时“谁生效”不可解释；本决策明确：新授权一律来自成员关系，全局角色不提供任何项目授权，遗留值只作历史读取。

**自审检测仅复制当前“活跃作者”快照**：无法覆盖被删除 / 撤销 / 非原作者编辑、导入、AI 接受与视频写入，无法支撑自审保证；因此采用独立持久化贡献者证据并对未知 fail closed。

## Notes

- 授权解析：`apps/api/app/services/project_access.py`；接线：`apps/api/app/deps.py`。
- 成员变更 / 交接：`apps/api/app/services/project_membership.py`；邀请 / 生命周期：`apps/api/app/services/{management,invitation,user_lifecycle,batch}.py`。
- 契约：`apps/api/app/api/v1/projects.py`（access / preview / change role）、`apps/api/app/schemas/project.py`、`apps/api/app/schemas/invitation.py`。
- 任务与审阅证据：`apps/api/app/api/v1/tasks/_shared.py`；`db/models/task.py`。
- 迁移：`apps/api/alembic/versions/0173_project_role_preparation.py`、`0174_project_role_conversion.py`。
- 前端（同功能增量，另行落地）：`apps/web/src/hooks/useProjectAccess.ts` 等；Python SDK：`packages/python-sdk/src/ai_annotation/models.py`、`client.py`、`cli/members.py`。
- 相关文档：[可见性与权限](../../docs-site/dev/concepts/visibility-and-permissions.md)、[迁移与回滚 runbook](../../docs-site/ops/runbooks/project-role-migration.md)、[ADR-0005 任务锁与审核矩阵](archive/0005-task-lock-and-review-matrix.md)。完整计划位于仓库 `docs/plans/1789807315_project-scoped-employee-roles.md`，不属于文档站页面。
- 已知边界 / 后续：撤销后已签发 URL 的立即失效、兼容性清理阶段的最终 CHECK 约束、遗留审阅证据的单独批准修复方式（无自动回填工具）。
