---
audience: [dev]
type: explanation
since: v0.9.14
status: stable
last_reviewed: 2026-09-19
---

# 可见性与权限

本文讲 project / batch / task 三层的可见性和权限规则，重点是“用户为什么能看到这条数据”。

代码真值源：

- `apps/api/app/services/project_access.py`
- `apps/api/app/deps.py`
- `apps/api/app/services/scheduler.py`
- `apps/api/app/api/v1/tasks/_shared.py`
- `apps/api/app/api/v1/batches.py`

## 三层边界

可见性不是一个 if，而是三层叠加：

1. **项目层**：用户是否看得见这个 project
2. **批次层**：这个 batch 当前状态是否对该角色开放
3. **任务层**：任务是否满足批次状态与实际指派约束；未分批任务需要显式指派

## 项目层

项目层核心规则：

- `super_admin`：全部可见
- 项目 owner：当前项目越权可见
- 其他用户：必须命中 `ProjectMember(project_id, user_id)`

真值主要在 `deps.assert_project_visible()`。

### 平台角色与项目角色

项目访问与写权限来自**资源所属项目中的有效成员关系**，不来自账号的全局角色：

- 平台角色 `PlatformRole`：`super_admin` / `project_admin` / `employee` / `viewer`（`apps/api/app/db/enums.py:22`）。历史值 `annotator` / `reviewer` 仅用于迁移与历史读取，新授权模型**永不**从它们取权。
- 项目角色 `ProjectRole`：`annotator` / `reviewer` / `viewer`，存储在 `project_members.role`，每个账号在每个项目最多一个角色。
- 一个 `employee` 可以在 A 项目标注、B 项目质检、C 项目只读观察，无需切换全局身份；没有成员关系就没有项目访问，除非是合法管理者（超管，或该项目的 owner / 合法 `project_admin`）。

统一解析在 `apps/api/app/services/project_access.py`：`resolve_project_access()` 产出不可变上下文（平台角色、项目角色、membership ID/version、管理者分类）与固定能力集 `project.read` / `project.manage` / `member.read` / `member.manage` / `task.read` / `annotation.write` / `review.write` / `export.annotations` / `performance.read`。未知角色 fail closed，请求上下文与资源项目不一致直接拒绝。能力是**必要非充分**条件：任务级指派覆盖、批次默认、开放池、预留审核、管理员锁、乐观版本、Mask QC 与视频边界检查全部保留。API key scope 只做附加交集，`*` 不授予项目访问；Socket 与异步作业在各自边界重新解析当前权限。

项目访问还有两个**纯函数**真值，供已持有 ORM 行的调用方复用，避免各处自行拼装 owner / 平台角色判断：

- `platform_role_is_manager(platform_role)`：平台角色是否属于管理角色（`super_admin` / `project_admin`），只描述“需要配合所有权”。
- `is_privileged_for_project(user, project)`：活跃账号且是超管，或**该项目的 owner 且平台角色为管理角色**。它等价于 `resolve_project_access()` 的 `ACCESS_KIND_SUPER_ADMIN` / `ACCESS_KIND_OWNER`，被 scheduler、worker、批次状态机、成员变更与聚合读取共用；所有权本身不授权，`annotator` / `reviewer` 历史账号值也不是授权来源。

这两者与 `resolve_project_access()` 是同一套规则的不同入口：请求路径需要每次重读当前账号 / 项目状态（撤权、转移后立即失效），因此用 async 解析器；worker 与聚合已经锁定或加载了行，则用纯函数复用同一判定。

成员输出 `ProjectMemberOut` 是**扁平**的：`role` 是项目角色，另有独立的 `platform_role`；邀请 HTTP 字段仍叫 `project_member_role`，映射到数据库 `project_role`。项目列表 / 详情带 `my_project_role`，列表按 `project_role` 过滤。

### 导出收紧（有意行为变化）

全项目、批次与选定任务 / Data Manager 导出要求显式能力 `export.annotations`，仅项目 `reviewer` 与合法管理者；项目 `annotator` / `viewer` 被拒。这是相对“项目可见即可导出”的**故意收紧**，同样作用于导出创建、worker 执行、缓存命中与结果访问；选定任务导出仍保留逐任务可见性检查。项目绩效明细与 CSV 仍仅 owner / 超管，导出标注不等于可看他人绩效。

### 自审与审阅贡献者证据

自审拒绝（提交者不能领取 / 编辑 / 通过 / 退回自己提交的内容，含管理者动作）依据的是持久化的**贡献者证据**，不是全局角色：

- 任务上新增 `annotation_contributor_ids`（标注阶段实际操作者的去重并集）、`review_contributor_ids`（冻结在当前 `review_round_id`）与 `review_submitter_id`。
- 所有标注阶段变更（批量、撤销 / 恢复、转换、导入、AI 接受、视频 / 场景 / 多相机）在任务锁事务内累积**实际操作者**，而不仅是原始作者；改派不清空已冻结贡献者。
- “已知为空”与“未知”不同：`annotation_contributor_ids = []` 表示尚无已记录的贡献者，但它单独不能证明审核轮次完整；`NULL`（旧二进制或迁移前创建）、结构畸形，或审核轮次缺少有效 `review_round_id` + `review_submitter_id` + 完整集合，才是“未知”。“未知”是黏性的：`NULL` 不会因新增一条标注变成“已知集合”；未知证据的审核写入返回 `409 review_contributors_unknown` 并阻断，**不**按全局角色回退。**平台不附带自动回填工具**，证据修复与不可用任务的恢复是单独批准的运维闸门，见 [迁移与回滚 runbook](/ops/runbooks/project-role-migration)。

### 成员角色变更与交接

角色变更走“预检 → 写入”两阶段：`GET .../access` 之外，项目 owner / 超管可调用 `POST /projects/{project_id}/members/{member_id}/role/preview`（只读，返回阻塞项、受影响资源快照与 `preview_token`）与 `PATCH .../role`（要求 `expected_version` + `preview_token` + 原因，可带显式接替人）。写入在锁内重读、按 `current_version` 做 CAS，并在**一个事务内**完成交接、释放受影响项目的锁 / 认领与角色 / version 更新；陈旧版本 / 快照失效 / 资源占用返回 409，不做部分交接。删除 / 重新加入产生新 member ID，重放旧变更不会成功。

## 批次层

批次层当前最重要的规则在 `scheduler.batch_visibility_clause()`：

- reviewer：可见 `active / annotating / reviewing`
- annotator：
  - `active / annotating` 且 batch 未分派或分配给自己
  - `rejected` 且分配给自己

这就是为什么某些 task 明明存在，但用户仍然拿不到、也查不到。

## 任务层

任务查询使用 `scheduler.task_visibility_clause()`；单题读取与上锁沿用相同的实际指派口径。批次列表仍使用批次自身的可见性，不能用整批权限代替单题判定。

关键点：

- `Task.assignee_id` 非空时优先于 `TaskBatch.annotator_id`；空值恢复批次默认。Data Manager 单题改派只影响选中任务，同批兄弟任务保留原范围。内部 `assignee_is_override` / `reviewer_is_override` 区分显式任务指派与批次回填值，批次改派只更新继承指派；恢复默认会清除对应覆盖标记。
- 无 batch 的任务只对显式指定的标注员或审核员开放；未指派任务仍仅特权用户可见。
- reviewer 的批次读取不受 annotator 约束；领取审核使用 `Task.reviewer_id`，为空时回退批次 `reviewer_id`。有预留审核员时其他审核员不能领取。
- annotator 对 `rejected` 批次，以及审核中批次内可返工的任务，只向实际被指派者开放。
- 实际指派只决定当前访问与待办；历史提交、审核绩效归属来自当时的事件，不随改派重写。
- 移除项目成员后，旧任务指派不能继续授权：单题读取、批量可见性、工作流操作及作业结果都重新检查当前成员关系。导出 worker 在读取缓存或生成文件前复核项目权限；历史贡献记录仍可由负责人查看。

## 操作权限不等于可见性

即使看得见，也不一定能操作。

例如：

- annotator 可能看见 `rejected` task，但不能做 reviewer approve
- reviewer 能看见 reviewing task，但不能激活 batch
- owner 越权可见，不代表绕过所有 task 状态机约束

### 项目作用域 ML Backend

`/projects/{project_id}/ml-backends/*` 不能只依赖 `require_roles`。当前端点按动作分三组：

- 可见成员读 / 推理：list、get、setup、capabilities 和交互推理使用 `require_project_visible`；指定 backend 还必须已对该项目启用。
- owner 管理：创建、编辑、停用、enablement、health、capabilities refresh、warmup、predict-test 使用 `require_project_owner`。
- 平台运维（全局共享态）：reload、unload 使用 `require_roles(super_admin)`。二者改写的是「全局 backend 显存驻留 / 常驻变体」——同一物理 backend 被多个项目共用，若允许项目 owner 触发就会驱逐 / 换掉其他项目正在用的权重。故这类破坏性驻留操作收口到超级管理员，且不叠加 `require_project_owner`（全局操作按 backend 定位、与 path 里的 project 无归属关系）。构造性的 warmup 是项目自身预标 / 交互推理前置，仍留在 owner 组。

所有带 `task_id` 的推理入口还要校验 `task.project_id == project_id`。不存在与跨项目 task 都返回 404，不允许 A 项目成员拿 A 的 backend 处理 B 项目 task。该边界与“用户角色是 project_admin”不同：角色只决定动作类别，project dependency 决定具体资源范围。

## 项目级范围收敛（成员绩效端点）

除了「看不看得见某条数据」，还有一类是「聚合数字按哪个项目口径切分」。
成员绩效端点 `GET /dashboard/admin/people` 及其详情 / 导出遵循统一的范围解析（`dashboard._resolve_people_scope`）：

- `super_admin`：`project` 可选；给定则走 `assert_project_visible` 校验存在（对 super_admin 恒可见），缺省则全局聚合。
- `project_admin`：`project` **必填**，且必须是其 **owner** 的项目；
  - 严格校验 `project.owner_id == current_user.id`，**不复用** `assert_project_visible`；越权或项目不存在均返回 `404`（隐藏存在性，不泄露「项目存在但你无权」）；
  - 缺省 `project` → `403`。

为什么不复用 `assert_project_visible`：后者对 project_admin 的 owner 校验失败后会 **fallback 到 `ProjectMember` 查询**（见 [`apps/api/app/deps.py`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/apps/api/app/deps.py) `assert_project_visible`），这是项目级数据访问的通用宽松策略，但对成员绩效语义过宽——会让「身为他人项目 member 的 project_admin」读到他人产能 / reject 率 / 类别分布。`_resolve_people_scope` 自行校验 `owner_id` 以收死边界，与「能不能看见 task」用同一把锁是错的口径。

这把「越权读他人项目绩效」收成 IDOR 安全边界：role 门只放行 `super_admin + project_admin`，
而 `project_admin` 被严格锁定在 **owner 自有项目** 范围，所有产能 / 质量 / 活跃聚合都按该 `project_id` 过滤
（此前 `project` 仅过滤「返回哪些用户」，聚合仍是跨项目全局数字 → 误导且越权）。

同一原则也适用于视频追踪任务聚合：`GET /video-tracker-jobs` 的 role 门只决定谁能进入管理列表；`project_admin` 的 items、counts 和显式 `project_id` 过滤还必须与 `Project.owner_id == current_user.id` 求交集。工作台恢复候选使用任务级 reviewable 端点：先校验 task 可见性，普通用户再按 `created_by` 收窄，项目 owner / 超级管理员才可恢复该 task 的全部候选。

## 现阶段最该注意的坑

项目内 `GET /projects/{project_id}/performance/*` 的列表、详情、依据和 CSV 仅对项目负责人及超级管理员开放，所有指标固定在该项目。普通成员保留 `/me/performance` 自查入口。历史贡献者可在负责人明确启用后显示，但活动记录不代表当前成员资格。

- 不要把 task lock 当成权限系统
- 不要把 reviewer 和 project owner 的权限混为一谈
- 改 batch 状态集合时，要同步审视 task 可见性是否跟着变
- 新授权判断不要读账号全局 `role`：项目访问来自 `resolve_project_access()`；遗留 `annotator` / `reviewer` 只用于历史读取
- 改导出、自审、异步 / Socket 边界时，要同步覆盖创建、worker 执行、缓存命中与结果交付四个时点

## 相关文档

- [项目模块](./project-module)
- [任务模块](./task-module)
- [批次模块](./batch-module)
- [Scheduler 与派题](./scheduler-and-task-dispatch)
