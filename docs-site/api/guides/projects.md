---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-17
---

# 项目

批次 `exports/lidar:preflight` 与 `export` 同样要求 `export.annotations`，标注员／观察者的项目可见性不授予导出权。批次审计会核对现存批次的父项目；删除历史须有可靠的项目 detail 或精确项目／批次路径。随机切批份数大于当前可切任务数时返回 400，不创建空批次。

`GET /search` 的任务结果在 limit 之前按成员职责、分派和批次范围筛选，保留启用协作视频的读取规则。项目人员看板的计数、导出和活动流按选定项目过滤；超级管理员未指定项目时可读全局。具体 SDK 请求与恢复语义见 [项目、批次与数据运维](../../dev/sdk/project-workflows)。

项目 ML warmup 要求目标 backend 当前已在该项目启用，关系不匹配时在上游操作之前返回 404。共享 backend 的 reload/unload 仍是超级管理员全局生命周期操作，project_id 用于审计上下文。完整 SDK/CLI 控制与诊断语义见 [账户、管理与运维](../../dev/sdk/account-and-operations)。

## 列表与分页

```http
GET /api/v1/projects/query?page=1&page_size=20
```

返回 `{ items, total, page, page_size, pages }`，`items` 中每项与项目详情的字段一致。`page` 从 1 开始，`page_size` 默认 20，范围为 1–100。空结果的 `total`、`pages` 为 0；超出末页时保留请求页码、返回空 `items` 和实际总数，调用方可据此回到有效页。

支持 `status`、`search`、可重复的 `type_key` / `data_type`、`member_id`、`project_role`、`created_from` / `created_to`。`project_role` 按成员的项目角色过滤；不要把旧的账号 `role=reviewer` 解释为“任意带 reviewer 成员关系的员工”。总数和项目行均先应用相同的权限与筛选：超级管理员可见所有项目，项目管理员可见自己负责的项目，其他成员可见自己加入的项目。项目行带 `my_project_role`（当前账号在该项目的项目角色，管理者 / 无成员关系为 null）。按创建时间倒序排列，同一时间以项目 ID 倒序稳定排序。

创建时间条件接受 ISO 日期或时间；无时区值按 UTC 解释，仅填写日期时结束日期包含全天。非法日期、倒置日期范围及非法分页参数返回 422。

`GET /api/v1/projects` 继续返回全部匹配项目的数组，供现有 SDK、CLI 和项目选择器使用；筛选与排序规则和分页接口一致。

## 成员与提及候选

`GET /api/v1/projects/{project_id}/access` 返回当前平台角色、项目职责、成员 ID／版本和能力列表。
管理者的成员字段可以为 null；普通成员的职责和版本使用最终同一次权限读取。
能力列表按名称排序，既有字段和 SDK 用法保持不变。

`GET /api/v1/projects/{project_id}/members` 按成员加入时间倒序返回项目职责和全局用户资料；
空项目返回空数组，缺失用户资料的成员继续省略。`GET /api/v1/projects/{project_id}/mention-candidates`
按负责人、启用超级管理员、成员的顺序返回，重复用户保留首次出现的种类。
成员组保留停用账号的现有展示规则。两条读取都按当前账号和项目可见性授权，
没有可见权限时返回404。内部独立部署配置见 [Workspace 运行协议](workspace-runtime#项目成员与提及候选)。

## 类别与属性用量

`GET /api/v1/projects/{project_id}/class-usage` 由当前项目负责人或超级管理员读取，返回
`classes` 与 `attributes` 两个计数映射。统计项目内所有启用标注，包括仍启用的已取消和
AI 标注；空类别名省略，属性统计对象的顶层键。类别和属性来自顺序的两次读取，
不承诺共同快照或映射键顺序。内部提供方配置见 [Labeling 运行协议](labeling-runtime)。

## 创建项目

```http
POST /api/v1/projects
{
  "name": "<unique>",
  "type_label": "图像检测",
  "description": "...",
  "data_type": "image",
  "tool_bindings": {
    "bbox": {
      "enabled": true,
      "classes": [{ "name": "dog", "color": "#ff0000", "aliases": ["puppy"] }],
      "attribute_schema": { "fields": [] }
    }
  },
  "ai_enabled": false,
  "ml_backend_source_id": null
}
```

`tool_bindings` 是类别与属性的存储真值。`classes_config` / `attribute_schema` 仍会在响应中作为兼容视图派生出来，但新代码应优先写 `tool_bindings`。aliases 用于 AI 预标的 prompt 召回（DINO 对自然语言敏感）。

`ml_backend_source_id` 指向全局注册表里的一个 backend：新建项目时填它即为新项目**启用**该全局注册项（**引用同一全局 id，不复制**），并把它设为项目主后端。留空则新项目不预先启用任何 backend。

`template_id` 与 `source_project_id` 互斥。使用可见模板／源项目时可省略 type_label 和类型字段，由服务器继承；明确给出 type_key 或 data_type 时，另一项按明确类型推导，配置明确字段优先。普通创建仍要求 type_label。copy_annotation_guide 仅用于源项目；模板继承指引文本但不复制 guide-assets。任务、标注、数据集、成员和批次不复制。

模板库提供 `GET/POST /project-templates`、`GET/PATCH/DELETE /project-templates/{template_id}` 和无正文的 `POST /project-templates/{template_id}/duplicate`。创建模板可通过 source_project_id 省略 name/type_label/type_key，普通创建必须提供这些字段；明确 null 不能代替省略。列表是未分页数组，支持 scope、重复 type_key 和 search；PATCH 没有 revision 条件，duplicate 产生当前用户的 private 副本。权限与 SDK 用法见 [视频、模板与存储运维](../../dev/sdk/video-and-templates)。

## 配置

```http
PATCH /api/v1/projects/:id
```

支持字段（部分更新）：

- `name` / `description`
- `tool_bindings`（整体替换）
- `classes_config` / `attribute_schema`（兼容输入，会被归并到对应工具单位）
- `ai_enabled` / `ml_backend_id`
- `review_required`
- `annotation_guide` / `video_sampling` / `rendering_config`

类别**重命名**走专用端点（原子迁移 annotations）：

```http
POST /api/v1/projects/:id/classes/rename
{ "old_name": "dog", "new_name": "canine" }
```

可选 `tool_unit_id` 只限定要改的项目类别配置；省略时遍历全部已存工具绑定，包括禁用工具。存量标注按**项目和旧名称**整体迁移，不按工具或活跃状态过滤；标注版本、预测结果、类别 alias 和颜色保持不变，也不自动重指向其他类别的 `alias_to`。非空旧名与新名相同时为成功无操作。

命令在当前管理权限下刷新配置并取得项目排他围栏，与已持有项目共享围栏的标注写入争用时返回 409。配置与标注迁移仍是同一个本地事务；这不表示普通项目 PATCH 已成为同一个命令。

直接 PATCH `classes_config` 改名会让历史 annotation 的 `class_name` 失联。

删除类别 / 属性定义不会删除已有标注；旧 `class_name` 或属性 key 会按当前配置实时判定为孤儿。提供两个治理端点：

```http
GET /api/v1/projects/:id/class-usage
POST /api/v1/projects/:id/cleanup-orphans
```

- `class-usage` 返回 `{ classes: {name: count}, attributes: {key: count} }`，用于删除确认。
- `cleanup-orphans` 默认 `dry_run=true`，只返回 `{ orphan_annotations, orphan_attribute_keys }`；预览不修改数据、不写审计记录，也不提交事务。
- 由项目负责人或超管执行 `dry_run=false` 时，会在当前管理权及项目配置锁内复核目标，再按批次 → 任务 → 标注顺序锁定和刷新待处理记录；锁忙或目标已改向时返回 `409`。
- 执行会软删类别已不存在的标注，并仅从类别仍有效的标注移除 schema 外用户属性 key；保留 `_` 系统元数据、标注几何与来源，不删除对象存储内容。标注修改、贡献证据、任务计数与审计在同一事务提交。

## 标注指引图片

```http
GET    /api/v1/projects/:id/guide-assets/sign-url?key=<resource-key>
POST   /api/v1/projects/:id/guide-assets/upload-init
POST   /api/v1/projects/:id/guide-assets/upload-complete
DELETE /api/v1/projects/:id/guide-assets?key=<resource-key>
```

图片读取沿用项目可见性规则：超级管理员、项目负责人及项目成员可申请短期签名链接，响应的 `expires_in` 为 3600 秒，实际签名到期时间会向下一个 10 分钟缓存窗口边界对齐。非成员或已移出的成员返回 404。资源 key 必须属于当前项目且已登记在该项目的 `guide_assets` 中，未登记或跨项目的 key 返回 404。已签发链接在到期前仍可使用。

上传与删除仅限项目负责人或超级管理员；普通项目成员写入返回 403。正文通过项目的 `annotation_guide` 字段读取和保存。

初始化接受 PNG、JPEG、WebP、GIF 和 SVG 的 MIME 类型，文件声明大小为 1 字节至 5 MiB。完成登记以实际对象 HEAD 为准；校验期间不持有权限/项目行锁，随后重新核对当前账号和管理权限，再锁定、刷新项目登记。期间失权的旧请求不得登记或执行无效文件清理；当前账号停用返回 401，转移后不可见返回 404，资源争用返回 409。

重复完成同一个 key 仍追加登记，不改成去重或幂等更新；删除移除该 key 的全部登记。正常的零字节或超限对象会先删除再返回 400，清理失败仍传播存储异常。显式删除则沿用原策略：对象服务失败仍移除项目登记，因此 API 成功不等于物理对象删除已确认，SQL 与对象存储不是一个原子事务。

## 当前账号的项目权限

```http
GET /api/v1/projects/:id/access
```

返回当前账号在该项目上的解析后权限：`project_id`、`user_id`、`platform_role`（账号级平台角色）、`project_role`（项目角色，管理者可为 null）、`membership_id`、`membership_version`、`access_kind`（`super_admin` / `owner` / `member`）、`is_manager` 与固定 `capabilities`。管理者可能没有成员关系；普通成员给出 membership 身份 / 版本与自身能力。能力集为 `project.read` / `project.manage` / `member.read` / `member.manage` / `task.read` / `annotation.write` / `review.write` / `export.annotations` / `performance.read`，服务端是权威，客户端本地判断只作展示。

授权来源是资源所属项目的**成员关系 + 项目角色**，不是账号全局角色：一个 `employee` 可以在 A 项目 `annotator`、B 项目 `reviewer`，无成员关系即无项目访问（合法管理者除外）。历史全局 `annotator` / `reviewer` 只用于迁移与历史读取。

## 成员管理

```http
GET    /api/v1/projects/:id/members                        # 列成员
POST   /api/v1/projects/:id/members                        # 加成员
DELETE /api/v1/projects/:id/members/:member_id             # 移除
POST   /api/v1/projects/:id/members/:member_id/role/preview # 角色变更预检（只读）
PATCH  /api/v1/projects/:id/members/:member_id/role         # 角色变更
```

成员输出是扁平结构：`role` 是**项目角色**（`annotator` / `reviewer` / `viewer`），`platform_role` 是账号的**平台角色**（`super_admin` / `project_admin` / `employee` / `viewer`），另有 `version`（CAS 版本）与 `assigned_at` / `updated_at`。项目成员角色不含 `project_admin`；平台 `viewer` 账号只能获得 `viewer` 成员身份。

加成员会校验账号启用状态与平台 / 项目角色兼容性。角色变更分两步：

1. `POST .../role/preview` 传 `project_role`（可带 `replacement_annotator_id` / `replacement_reviewer_id`），返回当前角色 / 版本、阻塞项、受影响资源快照与一次性 `preview_token`，**不写库**。
2. `PATCH .../role` 要求 `project_role` + `expected_version` + `preview_token` + `reason`（1–500 字），可带显式接替人。服务端在锁内重校验并按 `expected_version` 做 CAS；版本陈旧、预览快照失效或资源被占用返回 409，需要重新预检。删除成员对未完成工作 / 活动锁 / 认领返回 409，需先交接或改派，不会静默清空指派。

角色的 `role` / `platform_role` 命名与账号 / 邀请的区分见[可见性与权限](../../dev/concepts/visibility-and-permissions.md)。完整迁移、回滚与已签发 URL 限制见[员工项目角色迁移与回滚 runbook](../../ops/runbooks/project-role-migration.md)。

### @ 提及候选

```http
GET /api/v1/projects/:id/mention-candidates
```

返回讨论区输入 `@` 时可选择的用户，仅要求项目可见权限（成员 / 负责人 / 超管都可调用，不需要管理权限）。列表按 `user_id` 去重，顺序为项目负责人（`kind=owner`）→ 启用的平台超级管理员（`kind=super_admin`）→ 项目成员（`kind=member`）。提及校验本就放行 owner / project_admin / super_admin，本端点只补齐候选发现，避免普通成员依赖受管理员限制的 `GET /users`。

## Task Views / Data Manager

```http
GET    /api/v1/projects/:id/task-views
POST   /api/v1/projects/:id/task-views
GET    /api/v1/projects/:id/task-views/:view_id
PATCH  /api/v1/projects/:id/task-views/:view_id
DELETE /api/v1/projects/:id/task-views/:view_id
POST   /api/v1/projects/:id/task-views/:view_id/copy

POST   /api/v1/projects/:id/tasks/query
GET    /api/v1/projects/:id/task-views/:view_id/tasks
GET    /api/v1/projects/:id/data-manager/schema?entity_scope=objects
POST   /api/v1/projects/:id/data-manager/objects/query
GET    /api/v1/projects/:id/data-manager/objects/:annotation_id/detail
GET    /api/v1/projects/:id/data-manager/objects/:annotation_id/location
POST   /api/v1/projects/:id/data-manager/tracks/query
GET    /api/v1/projects/:id/data-manager/tracks/:track_ref/detail
```

`task-views` 保存项目内 Data Manager 视图，包含 `entity_scope`（`tasks | objects | tracks`）、`filter_json`、`sort_json`、`columns_json` 和 `visibility`。不同 scope 的名称和字段白名单相互隔离。`private` 视图只有创建者可见；`project` 视图对项目成员可见，但只有项目负责人或超级管理员可编辑。

`tasks/query` 接受临时过滤条件，不保存视图：

```json
{
  "filter_json": {
    "op": "and",
    "rules": [
      { "field": "issue.unresolved_count", "op": "gt", "value": 0 },
      { "field": "prediction.model_version", "op": "eq", "value": "sam3-v1" }
    ]
  },
  "sort_json": [{ "field": "last_activity_at", "direction": "desc" }],
  "columns_json": ["display_id", "status", "unresolved_issue_count", "comment_count"],
  "limit": 50,
  "offset": 0
}
```

任务行包含 `unresolved_issue_count`（有效、开放的问题主题数）和 `comment_count`（完整评论源总数：原生任务评论 + 标注评论）。旧列 `unresolved_feedback_count`、排序键与 `feedback.unresolved_count` 筛选保留为兼容别名，语义已对齐未解决问题计数；数值筛选 `issue.unresolved_count` 和 `discussion.comment_count` 使用相同谓词。

对象查询使用 annotation grain 的 keyset cursor；轨迹查询按 compact annotation 或 Scene 共享 track ID 的逻辑 grain 返回。两者的 total、facet、详情和定位都先与当前用户的 visible-task scope 连接，不返回 raw geometry。过滤字段是白名单，未知字段或不允许的操作符返回 422。Data Manager 的查询 read model 保持只读，任务操作使用独立命令入口。

任务查询另返回 `effective_assignee` / `effective_reviewer`，供表格和画廊展示实际指派：任务覆盖值优先，空值回退批次默认。`task.assignee` / `task.reviewer` 筛选、排序使用同一口径；原有 `assignee_id`、`assignee` 和 `reviewer` 保留任务行自身的值。

过滤树最多 32 层、4096 个节点（根计为 1），`in` 最多 200 项。子节点结构、数字、ISO 日期和 UUID 在查询前校验，错误返回 422；无时区日期按 UTC 解释。可空任务字段仍支持 `eq` / `ne` 与 JSON `null`。保存的无效条件保留在视图中并通过 `invalid_fields` 报告，结构错误使用 `__filter__` 标记。

## Data Manager 任务操作

```http
POST /api/v1/projects/:id/data-manager/tasks/assignment-preview
POST /api/v1/projects/:id/data-manager/tasks/assignment-apply
POST /api/v1/projects/:id/data-manager/tasks/export
POST /api/v1/projects/:id/preannotate
```

分派和选定任务导出使用 `task_ids`（1–200 个 UUID），服务端去重并稳定排序。分派须项目负责人权限；`annotator_id`、`reviewer_id` 未传表示保留，null 清除任务单独指派并恢复批次默认（未分批则为未分派），至少传其中一个。非空审核员仅可分派到 review 状态任务。预览返回原始前后字段与 `effective_before_*` / `effective_after_*` 实际生效字段、状态、可更新/跳过/失败数量及 `preview_version`；apply 必须携带此版本，状态或指派已变化返回 409。

导出按调用人的可见任务范围校验，接受 `targets` 和已有导出选项，返回 202 与持久化 `job_id`。暂不支持 `voc`、`coco-multicamera`、`kitti`、`nuscenes`、`pointmask` 的局部任务范围，也不能混用视频局部范围参数。

预标注保留原项目/批次入口；显式 `task_ids` 必须非空且最多 200 项，不能用空数组表示整个项目。默认（`execution_scope: "bulk"`，数据管理批量路径）所选任务必须属于项目、满足给定批次、处于 pending，批次为 active 或尚未分派标注员及审核员的 draft，且未被管理锁定，任务没有编辑锁。工作台「当前题 AI」传 `execution_scope: "workbench"` 与单个 `task_ids`，按当前用户对该题的编辑语义校验：允许 pending / in_progress / rejected 任务与 draft 批次，但批次管理员锁、任一他人编辑锁以及 uploading / review / completed 任务仍被拒绝（不接受 `batch_id`），拒绝原因返回中文提示。所有预标注请求都在派发前提交持久作业，包括不带幂等键的普通项目／批次请求。返回的 `job_id` 是 `async_jobs.id`，工作进程未启动时也可查询；派发异常仅将仍为 pending 的作业转为 failed，已认领或已结束的作业保留其状态。

普通图片项目的批量请求（含 Scene 模式中的单图任务）与持久作业在同一事务冻结任务成员及逻辑来源；提交后新增任务不进入原作业。未显式指定任务时仍无总任务数上限，服务端按最多 100 项读取内部清单，不把全量 ID 放进队列消息。`skip_predicted` 在冻结时排除已有预测，`total_tasks` 使用实际选中数量。执行前等待所选原图准备完成；同一幂等请求复用原清单，来源变化不会换成当前图片。首次尚无对象身份的来源，以准备时首次成功读取对象身份为字节受理时点。显式场景执行单位、视频、逐帧和文字／跟踪流程保留各自执行路径。

普通逐题批量消费者还会保存执行租约、原始模型结果及最终写入回执。写入锁竞争和存储不确定性不会消耗为失败项；重投递先恢复已保存结果，已提交题目、覆盖清理、费用与阶段统计只推进一次。结果尚未完整保存前的进程中断可能重新执行该题推理。每次最终写入仍检查当前权限、原始 Task 作用域和来源，终态作业拒绝新结果。此检查点不改变单独的失败预测重试与逐帧／场景协议。

逐帧视频请求通过源阶段的 `source.execution_unit: "frame"` 声明，只接受一个检测阶段，多阶段请求返回 422。Job 与视频清单在同一事务内固定完整成员、来源及服务器采样规则；重放待执行请求使用已保存的采样配置，不因后续帧数上限变化重选。缺少原视频清单或输入类型不匹配的旧待执行作业返回 409，需重新发起请求。内部清单身份由服务生成，不新增公开请求参数。

导出与预标注可携带 `Idempotency-Key`（1–128 字符）。相同调用人、项目、动作及 key 复用作业；相同 key 配不同请求返回 409。工作进程再次检查作业、项目、任务范围与调用人权限；按 task ID 加载，不将局部选择扩大为整批。

任务范围导出的缓存仅在同一持久化作业的重试间复用；新建导出作业重新生成文件，包含更新后的预测、媒体和轨迹内容。

## 项目成员绩效

```http
GET /api/v1/projects/:id/performance/members
GET /api/v1/projects/:id/performance/members/:user_id
GET /api/v1/projects/:id/performance/members/:user_id/events
GET /api/v1/projects/:id/performance/export
```

仅项目负责人和超级管理员可调用。共同查询参数为 `from`、`to`、`timezone`、`work_type=annotation|review`、`account_status=all|active|inactive`、`include_historical`、`q`、`sort`、`cursor` 和 `limit`（默认 50、最多 100）。日期按指定 IANA 时区解析，带时区时间戳按实际瞬间解析；区间左闭右开，最多 90 天。`sort` 使用允许字段和 `+`/`-` 前缀，URL 中的 `+` 应编码为 `%2B`。

列表返回 scope、coverage、project_totals、items 和 next_cursor。指标包含 value、unit、coverage，比例同时携带 numerator/denominator；未知值为 null。成员指标包含 `annotated_images`（区间内创建且仍保留标注的不同图片任务数，单位 images），项目总数额外包含 `annotated_images` 与 `retained_objects`，二者按相同资格谓词独立聚合，不求和成员行。`sort` 允许 `annotated_images` 与 `retained_objects`。详情包括成员、趋势、类别/来源/几何分布、驳回原因和依据；依据独立分页。CSV 复用同一范围，不受列表当前页限制。具体归属和采集规则见[项目成员绩效数据](../../dev/concepts/project-performance.md)。

## Alias 频率

```http
GET /api/v1/admin/projects/:id/alias-frequency
```

返回每个 alias 在该项目历史 prediction 中的出现次数，前端 chip 按 desc 排序。

## 列出 / 详情

```http
GET /api/v1/projects                       # 当前用户可见
GET /api/v1/projects/:id
GET /api/v1/dashboard?view=projects        # 超管看全部
```

## 归档 / 删除

归档：`PATCH /projects/:id` 设 `status='archived'`。归档项目对标注员不可见但数据保留。

`DELETE /api/v1/projects/{project_id}` 由项目负责人或超级管理员按当前管理权限执行，成功返回 204。项目任务、标注和各 owner 清理、审计与回执共同提交，原数据集和素材对象保留；学习证据等保留规则仍会阻止删除。

存在尚未退役的视频执行时返回 409，`detail.code` 为 `video_execution_not_retired`。先取消执行并完成退役后重试；业务终态不能替代退役证明。拒绝时不会删除 Task、Job、输入或检查点，也不留下部分 outbox 或删除审计。旧作业缺少证明时仍拒绝，详见[数据能力协议](./module-data#视频退役与删除围栏)及模块台账。

## 相关

- [批次与任务](./tasks-and-annotations)
- [ML Backend](./ml-backend)

## 开工状态

- `GET /api/v1/projects/{project_id}/readiness`：项目负责人或超管读取真实数据、任务创建作业、非空批次及有效成员和分派状态。空批次不算可执行；停用账号或角色不匹配的成员不算有效接收人。
- `GET /api/v1/dashboard/annotator/projects/{project_id}/onboarding`：当前用户在可见项目内的分派、访问、有效标注和审核结果计数，`reviewed_task_id` 指向最近产生审核结果的本人任务；reviewed_task_display_id、reviewed_task_status 和 reviewed_task_reason 可直接展示审核结论。分派计数只包含当前可进入的任务，历史结果不依赖任务仍可编辑。等待审核的任务不计入审核结果。

清单的跳过与指南确认继续通过个人偏好接口持久化，按项目和指南版本隔离。
