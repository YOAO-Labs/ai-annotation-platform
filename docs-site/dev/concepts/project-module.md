---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-21
---

# 项目模块

本文是面向开发者的 project 手册，说明项目在系统中的职责、数据模型、配置面、成员边界，以及它如何约束 batch、task、工作台和 AI 能力。

如果你要改：

- 项目创建 / 更新 / 删除
- 项目成员与 owner
- 类目、属性 schema、采样配置
- ML backend 绑定
- 项目级统计、导出、预标注入口

先读这页。

## 模块定位

Project 是业务顶层容器。项目可以属于 organization；成员、批次、任务和私有编排直接挂在项目下，数据集与 ML 服务池则分别通过关联表建立关系。Dashboard stats 是读取项目缓存与下游聚合形成的读模型，不是项目拥有的独立实体。

<ExcalidrawDiagram
  src="/diagrams/dev/concepts/project-module-map.svg"
  alt="Project 模块关系图，区分 API 兼容字段、项目存储真值、成员与数据集关系、命名编排以及 ML 服务池绑定"
  caption="Project 模块全景：实线表示当前归属或调用，虚线表示套用、聚合等读取关系"
/>

一句话理解：

- `project` 决定“这批数据按什么规则工作”
- `batch` 决定“任务如何分组推进”
- `task` 决定“单条数据当前处于什么工作状态”

## 代码入口

| 位置                                                                           | 作用                                               |
| ------------------------------------------------------------------------------ | -------------------------------------------------- |
| `apps/api/app/db/models/project.py`                                            | Project 主模型                                     |
| `apps/api/app/db/models/project_member.py`                                     | 项目成员关系                                       |
| `apps/api/app/db/models/project_pipeline.py`                                   | 保存编排表映射（API 不直接读写 ORM）               |
| `apps/api/app/db/models/ml_backend_pool.py` · `ml_backend_registry.py`         | 服务池、物理实例与项目启用关系                     |
| `apps/api/app/schemas/project.py`                                              | Project 请求 / 响应 schema                         |
| `apps/api/app/api/v1/projects.py`                                              | 项目 HTTP 路由                                     |
| `apps/api/app/api/v1/ml_backends.py` · `project_pipelines.py`                  | 项目 ML 服务池与编排 API                           |
| `apps/inference/src/aap_inference/preannotate.py`                              | `PipelineStage` / `PreannotateRequest` 请求 schema |
| `apps/api/app/services/inference_pipelines.py`                                 | 选择命名编排 owner、提交边界和提交后投影           |
| `apps/api/app/services/inference_pipelines_data.py`                            | 借用 API Session 的固定 Native 数据适配            |
| `apps/inference/src/aap_inference/pipeline_manager.py` · `pipeline_policy.py`  | 命名编排便携命令、读取与纯策略                     |
| `apps/inference/src/aap_inference/pipeline_postgres.py` · `pipeline_server.py` | 独立 owner 的 Native 数据与 HTTP server            |
| `apps/inference/src/aap_inference/pipeline_client.py`                          | 受限 HTTP client 与状态探测                        |
| `apps/api/app/api/v1/dashboard/`(admin/reviewer/annotator 受众子 router)       | 项目级统计与聚合                                   |
| `apps/web/src/api/projects.ts`                                                 | 前端 project API wrapper                           |
| `apps/web/src/pages/Projects/`                                                 | 项目设置与管理 UI                                  |

## 数据模型

`Project` 当前承载的核心字段：

| 字段                                                                     | 含义                                                                                                                                                                                                                                               |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `display_id`                                                             | 人类可读项目 ID                                                                                                                                                                                                                                    |
| `name`                                                                   | 项目名                                                                                                                                                                                                                                             |
| `organization_id`                                                        | 可空的组织归属                                                                                                                                                                                                                                     |
| `type_key` / `type_label` / `data_type`                                  | 任务类型与媒体类型，例如 `image-det` / `image`                                                                                                                                                                                                     |
| `owner_id`                                                               | 项目 owner，决定写权限上限                                                                                                                                                                                                                         |
| `status`                                                                 | 项目生命周期状态                                                                                                                                                                                                                                   |
| `tool_bindings`                                                          | 工具维度类别 / 属性绑定 JSONB, `{ tool_unit_id: { enabled, classes: [...], attribute_schema: {...} } }` 嵌套结构，**唯一存储真值**；旧扁平 `classes` / `classes_config` / `attribute_schema` 仅作为响应兼容投影                                    |
| `sampling`                                                               | 工作台派题策略                                                                                                                                                                                                                                     |
| `maximum_annotations`                                                    | 多人重叠标注上限                                                                                                                                                                                                                                   |
| `show_overlap_first`                                                     | 是否优先展示重叠任务                                                                                                                                                                                                                               |
| `task_lock_ttl_seconds`                                                  | task 锁 TTL                                                                                                                                                                                                                                        |
| `ml_backend_pool_id`                                                     | ORM 中的项目主服务池指针；交互 prompt 与视频 tracker 仍可在其它已启用服务池中按能力路由                                                                                                                                                            |
| `ml_backend_id`（API 兼容投影）                                          | `ProjectOut` / 写入 schema 暂时暴露的物理 registry ID；路由层会与 singleton / legacy service pool 互相解析，不是数据库字段                                                                                                                         |
| `ai_interactive_enabled`                                                 | 工作台交互式 AI 工具的项目级总开关；不属于 `tool_bindings` 几何单位                                                                                                                                                                                |
| `model_version`                                                          | 展示提示，不是能力或路由真值                                                                                                                                                                                                                       |
| `box_threshold` / `text_threshold`                                       | 项目级 AI 推理默认参数                                                                                                                                                                                                                             |
| `preannotate_pipeline`                                                   | 旧项目内嵌编排的兼容兜底；当前主路径是可命名、可复用并支持 private / organization / public scope 的 `ProjectPipeline`，项目可选择一条 private default。详见 [预标注流水线 · 多阶段预标注](./prediction-pipeline#多阶段预标注pipeline_stages路径-b) |
| `scene_mode`                                                             | 是否为 scene 模式项目（默认 `false`）；仅 image / lidar 项目可开启，且需绑定 `has_scenes=true` 的数据集（已建 task 后不可切换）                                                                                                                    |
| `prefer_same_scene_continuation`                                         | scene 模式连续派题开关（默认 `false`）：打开后 `get_next_task` 优先返回用户上次提交 task 的同 scene 下一帧                                                                                                                                         |
| `scene_continuation_window_min`                                          | 连续 session 估计窗口（分钟，默认 `30`，约束 1~480）                                                                                                                                                                                               |
| `total_tasks` / `completed_tasks` / `review_tasks` / `in_progress_tasks` | 项目级聚合统计                                                                                                                                                                                                                                     |
| `due_date`                                                               | 截止日期                                                                                                                                                                                                                                           |

设计要点：

- Project 不只是“容器名”，它同时保存工作台策略、AI 行为默认值和统计缓存
- `model_version` 只是展示提示；实际能力由项目已启用服务池的 `/setup` 快照、具体 prompt / tracker 路由与主服务池优先级共同决定

## 项目状态

项目状态枚举在 `apps/api/app/db/enums.py`：

```text
in_progress
completed
pending_review
archived
```

这些状态更多用于项目列表与总览展示，不像 task / batch 那样承载细粒度工作流。

经验上：

- 日常开发里更常碰的是 batch / task 状态机
- project.status 更接近“管理看板状态”，而不是驱动工作台细节的唯一真值

## Project 负责哪些配置

### 1. 标注 schema（按工具单位拆分）

项目定义:

- 启用哪些**工具单位** (tool_unit) 与各 unit 持有的类别 / 属性 schema: `tool_bindings` (唯一存储真值)
- 响应 / 导出按需从 `tool_bindings` **读时派生**的扁平投影: `classes` / `classes_config` / `attribute_schema`

<!-- history: tool_bindings replaced the older flat project schema across the v0.10 tool-unit slices. -->

`tool_bindings` 结构示例:

```json
{
  "bbox": {
    "enabled": true,
    "classes": [{ "name": "person", "color": "#ff0000", "order": 0 }],
    "attribute_schema": { "fields": [] }
  },
  "region": { ... },
  "polyline": { ... }
}
```

工具单位枚举与 `app/schemas/_jsonb_types.ToolUnitId` Literal 对齐：`bbox` / `polyline` / `region` / `lidar_box_3d` / `rotated_bbox` / `keypoint` / `point_mask_3d`。交互式 AI 不是几何工具单位：smart-point / smart-box / exemplar 的多边形归 `region`，Magic Box 的矩形框归 `bbox`；项目级 `ai_interactive_enabled` 只控制能力是否开放。**强隔离决策**: 不同工具的同名类是两条独立记录, 详见 [ADR-0026](../adr/archive/0026-tool-unit-class-and-attribute-binding)。

如果你改的是「标注长什么样」, 十有八九要从 project.tool_bindings 入手, 而不是 task。

写入路径由 `projects.py` 保留 HTTP 请求、提交与响应；六项项目／模板配置命令通过 `services/workspace_configuration.py` 进入同一个 Workspace owner。`apps/workspace/src/aap_workspace` 持有配置编排、值、旧扁平类别/属性规范化、kind、克隆与模板策略；Native 执行当前权限围栏、跨 owner 类型化调用及固定列写入。默认适配器借用调用方 Session，不自行提交或回滚；独立 DSN／HTTP 只拥有各自的业务事务，显式失败不回退。原配置 API 工作流与 ORM 行命令已删除；模板列表／详情和提交后的响应投影通过 `services/workspace_template_query.py` 进入读取 owner；API 附带成功命令回执读取当前行，再校验公开响应。原模板 ORM 读取适配器已删除；命名流水线复用 Identity 的窄组织事实能力。项目内嵌与命名流水线共用 `aap_inference.pipeline.validate_saved_pipeline`。`ProjectOut` 继续消费共享 Workspace 纯投影。六项命令已完成限定本地包、进程、镜像与恢复验收；项目普通读取已由查询 owner 提供，成员／mention／access 也已接入各自查询 owner；其余相邻写入继续迁移，见 [配置命令 ADR](/dev/adr/0136-workspace-configuration-command-owner)。

前端项目设置的归属：`基本信息` 只维护名称、状态、截止日期和类型展示；`类别与属性` 是 `tool_bindings` 的唯一编辑入口，同一个工具单位 tab 下同时维护类别、关键点骨骼模板与属性 schema。

### 2. 工作台派题策略

`sampling` 决定 `scheduler.get_next_task()` 的排序行为：

- `sequence`
- `uniform`
- `uncertainty`

同时还会受这些项目级配置影响：

- `maximum_annotations`
- `show_overlap_first`
- `task_lock_ttl_seconds`

也就是说，工作台“下一题给谁、按什么顺序给”本质上是 project 级策略。

scene 模式项目额外多一层连续派题逻辑：打开 `prefer_same_scene_continuation` 后，`get_next_task` 会优先把用户上次提交 task 的同 scene 下一帧派给同一人（连续标注同一段序列），其中 `scene_continuation_window_min` 是判定“是否仍属于同一连续 session”的时间窗口（分钟）。两者默认 OFF / 30，既有非 scene 项目零回归。详见 [scheduler-and-task-dispatch](scheduler-and-task-dispatch) 与 [scene-and-frame-index](scene-and-frame-index)。

### 3. AI / 预标注配置

项目还决定：

- 是否启用 AI：`ai_enabled` 是独立持久化字段；当前前端在选择或清空主后端时同步写入它，但数据库没有“只要绑定后端就自动派生”的约束
- 主服务池：ORM 字段是 `Project.ml_backend_pool_id`，配置 owner 通过固定 Native binding 步骤写入。`application/ml_registry_deletion.py` 先完成删除准入、路由静默及 registry 删除，仅成功后才调用 `services/project_workspace.py::clear_primary_backend_binding_for_pool()` 清空指向该池的全部主绑定；清理与 registry 删除同属一个调用者事务，启用关系不变。公共 API 暂以 `ml_backend_id` 接收 / 返回 registry ID，并在配置入口解析到服务池。
- 文本预标注默认阈值：`box_threshold` / `text_threshold`

AI 能力是 project 级开关，不是 batch 或 task 私有配置。

前端项目设置的归属：这些 AI 字段统一在 `ML 模型` 页维护。该页上方是项目级 AI 预标注设置（选主服务池时同步启用、去重阈值），下方是**对全局服务池的启用 / 禁用清单**；物理 registry 由服务池成员承载，项目只管理哪些池可用以及哪个池是主池。per-backend 阈值覆盖已退役：推理参数运行时按 `/setup.params` 通用渲染，项目级 `box_threshold` 仅作兜底。物理注册与健康检查归超管在模型市场维护。

## 成员与权限边界

项目权限边界由两层组成：

1. 平台角色 `PlatformRole`（账号级，`users.role`，列名不变）：`super_admin` / `project_admin` / `employee` / `viewer`
2. 项目内成员关系：`ProjectMember(project_id, user_id, role)`，`role` 是项目角色 `annotator` / `reviewer` / `viewer`

`owner_id` 是项目级管理权限兜底。

当前常见判断模式：

- `super_admin`：越权可见 / 可写
- `project owner` / 合法 `project_admin`：所管项目的管理路径（管理权限还需项目所有权，见 [可见性与权限](./visibility-and-permissions#项目层)）
- 其他账号：必须命中 `ProjectMember(project_id, user_id)`；无成员关系即拒绝，**不再**回退全局 `users.role`
- 历史全局 `annotator` / `reviewer` 只用于迁移适配与历史读取，不提供任何项目授权

统一解析在 `apps/api/app/services/project_access.py::resolve_project_access`，输出固定能力集。所以“用户能不能在这个项目标注 / 质检”取决于该项目内的成员角色，而不是账号全局角色。

前端项目设置的 `成员管理` 选择的是**项目角色**（标注员 / 质检员 / 观察者）；同一个员工可以在不同项目承担不同角色。角色变更走“预检 → 按版本与 token 原子交接”，见 [可见性与权限](./visibility-and-permissions#成员角色变更与交接)。后端仍以单条 `ProjectMember` 关系为写入单位。

## 和 Batch / Task 的关系

### Project → Batch

一个项目下面有多个 batch。

项目层提供：

- batch 的业务边界
- batch 默认排序参考（`priority` 仍在 batch 上）
- batch 汇总统计（见 `ProjectOut.batch_summary`）

### Project → Task

所有 task 都属于某个 project。

Project 决定 task 的这些上层语义：

- 数据类型和标注 schema
- 重叠标注上限
- 派题策略
- task 锁 TTL

这也是为什么 task 虽然有独立状态机，但很多行为仍要回读 project 配置。

## 主要 API 面

`apps/api/app/api/v1/projects.py` 当前覆盖项目 CRUD、成员、导出、预标注触发和数据关联；ML 启用 / 服务池由 `ml_backends.py` 管理。`/project-pipelines` 的五个 CRUD 路由与项目 apply 路由共六个公开路由仍保持原请求 / 响应合同，读写由 `aap_inference.pipeline_manager.PipelineManager` 执行，API 经 `services/inference_pipelines.py` 选择默认借用事务、专用 DSN 或 HTTP owner。命名编排数据不经 `ProjectPipeline` ORM 读写；请求与响应 schema 仍位于 API。项目内嵌旧配置和保存编排结构继续共用 `aap_inference.pipeline.validate_saved_pipeline`，canonical `PipelineStage` 与 `PreannotateRequest` 位于 `aap_inference.preannotate`。

项目 apply 保留 `require_project_owner` 在源编排查询之前的入口检查；manager 读取源和 backend 启用事实后，再经 Workspace 固定能力重读当前账号、项目与成员锁。借用事务由 API 提交成功命令，再以完整命令回执读取新投影，并通过 Identity UserBrief 补充创建者名称；apply 响应沿用 `created_by_name: null`。

项目主路由覆盖的主要能力：

- 列表 / 详情 / 创建 / 更新 / 删除
- 项目克隆与模板套用（`source_project_id` 与 `template_id`）
- 类目重命名
- 项目 owner 转移
- 成员列表 / 新增 / 删除
- 项目导出
- 项目级 AI 预标注触发
- orphan tasks 预览与清理
- 关联 datasets 查询

### 项目克隆与模板继承

创建项目（`POST /api/v1/projects`）支持通过 `source_project_id`（从已有可见项目复制配置）或 `template_id`（从模板库创建）快速初始化（二者互斥）：

- **类型继承与一致性推导**：服务层通过 `resolve_project_kind` 解析。调用方未显式提供 `type_key` 或 `data_type` 时，服务端自动从源项目或模板中继承；若显式指定了任意一项，另一项由系统自动派生并严格断言二者一致。
- **标注指引与素材**：`copy_annotation_guide=True` 仅当 `source_project_id` 给定时有效，会将源项目的标注指引文本深拷贝并复用 `guide_assets` 存储 key（不重复上传原图）；模板套用只继承模板中的指引文本，不复制素材资源。
- **覆盖规则**：调用方显式传入的配置优先于继承值。任务、标注、数据集关联、项目成员与批次不会被复制。
- **模板库管理**：由 `/api/v1/project-templates` 路由支持，提供私有／公开模板的 CRUD、基于源项目的模板生成，以及无正文一键复制 `duplicate`。

如果你新增 project 能力，优先判断它属于：

- “项目配置面”
- “项目成员面”
- “项目级运营动作”

不要把 project 路由写成 batch / task 杂项收纳箱。

项目删除经 `application/project_deletion.py`，先按原顺序锁项目、检查主动学习和训练/模型/评估/策略保留，再组合标注、反馈、缺陷引用、管理关联和项目行所有者。该用例不提交，HTTP 和开发种子脚本保留事务控制；缺陷记录保留但解除项目/任务引用，素材和全局模型后端不随项目删除。

无源任务经 `application/project_task_cleanup.py`，保留数据库反连接、排序任务锁、引用保护和清理后的计数同步。初始目标 ID 只用于原锁及保留检查，批量删除继续使用子查询；没有新增按任务循环读取或把所有 ID 复制进每条清理语句。

## 统计与缓存

Project 模型本身保留了多种聚合字段：

- `total_tasks`
- `completed_tasks`
- `review_tasks`
- `in_progress_tasks`

这些字段不是纯展示装饰，它们被：

- Dashboard
- 项目列表
- 批次 / 工作台周边摘要

直接消费。

因此改 task / batch 状态时，要留意是否需要同步 project counters。

## 常见修改落点

| 你想改什么                                                                                                    | 先看哪里                                                                                                                         |
| ------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| 新增项目配置字段                                                                                              | `db/models/project.py` + `schemas/project.py` + `api/v1/projects.py`                                                             |
| 改项目权限                                                                                                    | `deps.py` + `api/v1/projects.py`                                                                                                 |
| 改派题策略                                                                                                    | `db/models/project.py` + `services/scheduler.py`                                                                                 |
| 改 scene 模式 / 连续派题（`scene_mode` / `prefer_same_scene_continuation` / `scene_continuation_window_min`） | `db/models/project.py` + `schemas/project.py` + `services/project_kind.py`（`scene_mode_allowed` 门禁）+ `services/scheduler.py` |
| 改 AI 默认参数                                                                                                | `schemas/project.py` + `projects.py` + 相关前端表单                                                                              |
| 改项目统计                                                                                                    | `dashboard/`(admin/reviewer/annotator)+ 相关 service / counter 回写                                                              |

## 测试与联动点

改 project 相关逻辑时，至少检查：

- `apps/api/tests` 下 project、dashboard、preannotate 相关测试
- OpenAPI snapshot
- `apps/web/src/api/projects.ts`
- 项目设置页与 Dashboard 组件

高频联动风险：

- 后端 schema 加了字段，前端 settings 页没接
- project 配置改了，但 `scheduler` 还在读旧字段
- owner / member 权限判断只改了一半

## 项目读取的所有权基础

权限上下文、能力与角色兼容规则已由独立 Workspace 包的
`aap_workspace_contract.access` 持有，Workspace／API／服务／worker 调用方直接导入该所有者，
原 API 值模块已删除。当前 SQL 权限解析、锁与最终写入检查仍由原适配器执行；
这些值本身不是跨进程授权凭据。

项目普通读取由独立 Workspace 查询 owner 提供，0260提供了15项固定 PostgreSQL 读取能力：
Labeling 持有物化计数、AI／时间序列及任务／批次事实，Assets 持有数据集和素材事实，
Jobs 持有建题作业计数与最新状态，Inference 持有主池的 legacy instance 映射。
Workspace 只获显式函数权限，不直接读取这些所有者的原表。

实际旧行为已冻结27项 HTTP 观测、两项调用方事务探针，以及日期编码与43个物理字段；
原 SQL 读取、授权时点和配置提交后的响应失败行为仍保留。补充采集已冻结有限极端日期、
搜索、重命名和移交／配置提交后降权的读回行为。独立包内已实现 Project 查询协议、
原始值编码与分阶段策略；API 已选择借用事务、专用只读连接或 HTTP 运行时，显式失败不回退。
50 项冻结 HTTP 观测在三路径通过，独立包、受限镜像、故障／重启、真实备份恢复和完整迁移均已限定本地验证。
负责人转移保留提交前解码和提交后重新读行两个阶段；公开 DTO 仍在 API 验证。
成员／mention／access、相邻写入和其他跨域消费者仍未完成。
详见 [Project 查询 owner ADR](/dev/adr/0139-workspace-project-query-owner)。
