---
title: Data Manager 查询与聚合
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# Data Manager 查询与聚合

Data Manager 的数据查询使用项目范围内的只读 read model，提供 task、object 和 logical track 三种 grain。查询、保存视图计数、facet、详情与定位必须从同一个 visible-task scope 派生：项目负责人和超级管理员可见整个项目；其他成员只可见其批次权限允许的任务。页面中的任务操作走独立命令入口，不通过 read model 改写 annotation、prediction 或 tracker candidate。

前端字段编辑、URL 状态、草稿校验及数量口径见[筛选状态与结果范围](./filtering.md)。

## 端点

| 端点                                                               | 用途                                                                             |
| ------------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| `GET /projects/{id}/data-manager/schema?entity_scope=...`          | 返回当前 grain 可用的项目能力、字段、操作符、列、排序和指标定义                  |
| `POST /projects/{id}/tasks/query`                                  | 按 task grain 过滤、排序和分页                                                   |
| `POST /projects/{id}/data-manager/summary`                         | 对同一过滤范围做项目聚合，不受分页 offset 影响                                   |
| `POST /projects/{id}/tasks/{task_id}/data-manager/matches`         | 返回命中的 annotation、prediction shape 或 tracker job 摘要，不返回 raw geometry |
| `POST /projects/{id}/data-manager/objects/query`                   | 按 active annotation grain 过滤、facet、排序和 keyset 分页                       |
| `GET /projects/{id}/data-manager/objects/{annotation_id}/detail`   | 返回对象来源、属性、溯源、反馈与稳定定位，不返回 raw geometry                    |
| `GET /projects/{id}/data-manager/objects/{annotation_id}/location` | 只返回工作台定位信息                                                             |
| `POST /projects/{id}/data-manager/tracks/query`                    | 按 compact annotation 或 Scene logical track grain 聚合和 keyset 分页            |
| `GET /projects/{id}/data-manager/tracks/{track_ref}/detail`        | 返回轨迹摘要、可见成员与逐帧定位                                                 |
| `/projects/{id}/task-views`                                        | 保存和共享 filter/sort/columns 配置                                              |

`schema` 是前端字段和操作符的唯一真值，并返回 `available_entity_scopes`。项目 profile 由 `data_type × scene_mode × enabled tool_bindings × attribute_schema` 组成；`type_key` 只提供 preset，不应作为唯一分流依据。对象对所有项目可用；轨迹只在视频轨迹能力或 Scene 模式成立时可用。

## 选定任务分派边界

`POST /projects/{id}/data-manager/tasks/assignment-preview` 和 `assignment-apply` 调用标注所有者 `services/labeling_assignment.py::LabelingTaskAssignmentService`，不再通过 `data_management` 写 Task。请求/响应模型由 `schemas/labeling_assignment.py` 持有，保留既有 `DataManagerTaskAssignment*` 公开 schema 名称；查询与保存视图仍属于 read model 及其配置，不取得任务写入权。

- 请求数组上限 200，UUID 去重并稳定排序。角色字段缺失表示保留，显式 null 表示清除任务覆盖并恢复批次默认；未分批任务恢复为未指派。raw 与 effective 前后值、预览摘要和逐项数量保持原合同。
- 预览读取当前管理权限；确认在接收人校验前取得当前账号/项目管理围栏，复用 `BatchService.lock_and_validate_assignment_targets` 校验并锁定启用账号及对应成员职责。旧主体、旧 owner 或旧预览不能提供写权限。
- 确认按批次→任务锁定并刷新当前事实，核对等待后的批次归属，再比较预览摘要；归属、状态、锁定或分派变化返回既有 `data_manager_assignment_preview_stale`。锁定行交给同一计划继续写入，不重新装载另一组目标。
- 最终编辑检查复用非阻塞 `TaskLockService.assert_final_write_available(allow_own=False)`；包括本人编辑锁在内的不可用目标保留 `task_locked` 逐项失败。只转换已知编辑繁忙冲突，其他数据库/权限错误不伪装为成功。
- 仅审核态可指定非空审核员；显式审核员变更保留领取时间重置。所有者只 flush，由 HTTP 在同一事务记录审计并提交，保留成功、跳过和失败的部分结果，不强制改派同批其他任务。

这些仍是本地 Session/ORM 持久化适配器，不是已实现的跨语言命令协议。

## Filter DSL

过滤树由 rule 或 group 组成：

```json
{
  "op": "and",
  "rules": [
    { "field": "task.keyword", "op": "contains", "value": "frame-0042" },
    { "field": "annotation.source", "op": "eq", "value": "prediction_based" },
    { "field": "annotation.attribute.bbox.color", "op": "eq", "value": "red" }
  ]
}
```

动态属性路径格式为：

```text
annotation.attribute.<tool_unit_id>.<attribute_key>
annotation.attribute_origin.<tool_unit_id>.<attribute_key>
```

- **属性路径解析**：attribute key 可以包含点号；解析时只分离 tool unit，剩余字符串完整作为 key。字段必须存在于当前项目启用工具的 attribute schema 中。不同类型允许的操作符由 schema 返回，不开放任意 JSONB key 或 raw SQL。
- **同对象关联语义**：task grain 中，同一 `AND` group 的 annotation rule 编译为一个 correlated `EXISTS`，确保类别、来源、轨迹和属性由同一个 active、非 cancelled annotation 满足；不能为每个 rule 分别生成 `EXISTS`，否则会产生跨对象误命中。object / track grain 则把条件直接绑定到当前 annotation/member。
- **结构配额与类型校验**：结构校验在 SQL 编译前执行（根节点计为第 1 层和第 1 个节点，最多 32 层、4,096 个节点；`in` 最多 200 项）。非对象子节点、错误值类型、无效日期或 UUID、非有限数字统一返回 `422`。已有视图超出结构限制时保留记录并以 `invalid_fields: ["__filter__"]` 标记，不静默删除条件。

## 三种 grain 与分页

- **task**：一行一个任务，使用 offset 分页；summary 表示“匹配任务中的全部对象”。
- **object**：一行一个 active、非 cancelled、非 compact-track annotation。total 与 facet 都在 annotation grain 计算。
- **track**：普通视频以一条 compact track annotation 为实体；Scene 图片/点云按 `(project_id, track_id)` 聚合可见成员。Scene 聚合不会按关键帧或 task 重复成行。

object / track 查询使用版本化的不透明 cursor，cursor 绑定 sort field、direction、上一行 sort value 与稳定 tie-breaker。客户端不得解析或伪造 cursor；改变筛选或排序后必须从第一页重新查询。响应不携带 raw geometry，详情与工作台 deep link 使用 annotation UUID 或编码后的 `track_ref`。

## 聚合口径

summary 同时返回：

- `scope.visible_task_total`：当前用户的项目可见任务总量；
- `scope.matched_task_total`：当前过滤树匹配的任务量；
- 任务状态分布；
- active annotation 总量及 source / class / tool unit / geometry 分布；
- single-frame、tracked annotation 与 distinct track 计数；
- AI 检测候选待审、低置信候选待审、AI 追踪结果待审；
- 当前待审检测候选的模型版本与置信度区间分布；
- 未解决问题（有效、开放的问题主题，对齐工作台问题口径；兼容字段名 `unresolved_feedback`）；
- 项目 schema 中属性的 `eligible / present / missing` 与有限枚举值分布；
- capability 驱动的 image 分辨率、video 时长/帧/关键帧、lidar 相机/标定与 Scene 摘要。

### 属性与匹配对象解释规则

- **属性 `eligible` 判定**：服务同时应用字段的 `applies_to` 类别范围和 `visible_if` 依赖条件，再计算 `present / missing`。属性字段循环受项目 schema 数量约束，不随 task 或 annotation 行数增长为应用层 N+1。
- **Grain 聚合边界**：task-centric summary 聚合的是“匹配任务中的全部对象”；object / track query 的 `facets` 则只聚合该 grain 的完整匹配集合，不能用当前页 rows 在浏览器抽样。
- **“匹配对象”抽屉见证集（Matches）**：
  - 先确认完整筛选和任务权限，再按真实命中分支收集解释：`OR` 只贡献成立的分支；同一个 `AND` 内的直接 annotation 条件共享同一对象，不同嵌套组保留独立见证对象。
  - 空分组（`rules: []`）无论 `op` 为 `and` 还是 `or` 都编译为恒真，按任务上下文返回全部 active、非取消对象。
  - 命中项固定按 `annotation`、`prediction shape`、`tracker job` 排列并共用一个 `offset / limit` 窗口；`OR` 分支真值用单个布尔数组列返回，避免宽条件树触发 PostgreSQL 列数上限。

## Saved view 与 URL

- **视图作用域（`entity_scope`）**：`project_task_views.entity_scope` 为 `tasks | objects | tracks`（私有名称唯一键与项目共享名称唯一键均包含 scope，三个粒度可使用同名视图）。创建和更新必须使用对应 schema 的 filter/sort/column 白名单；不兼容字段以 `invalid_fields` 返回，不静默改写。
- **URL 参数与状态切换**：前端 URL 保存 `section/lens/view/q/filter/sort/columns/selected/layout/selected_tasks`（`section` 为 `overview | data | members`，`lens` 表示数据区域的 grain）。`filter`、`sort` 与 `columns` 使用带版本号的 JSON envelope；切换 grain 时清空不兼容状态，存在未保存修改时先提示确认。
- **表达式保结构原则**：三个 grain 共享值编辑控件和保留结构的表达式操作，分组绝不展平（避免破坏同对象 `EXISTS` 约束）。关键词仅从独立 `contains` 规则或顶层 `AND` 中的无歧义规则提取；`OR` 内关键词保留在原树中。

## AI 待审

检测候选按 prediction 内的 shape 计算：

```text
全部 shape
- rejected_shape_indexes
- active annotation 中已接受的 (parent_prediction_id, attributes._shape_index)
```

不能使用 `Task.total_predictions`，它只是 prediction 行数。

低置信待审使用相同集合，读取每个 shape 自身的 `score`，兼容 `confidence`；缺失或非数字按 `0` 处理。固定阈值为 `< 0.5`，任务列与 `ai.low_confidence_prediction_shape_count` 过滤器都返回候选数量。summary 的 `by_model_version` 和 `confidence_buckets` 也只聚合这个当前待审集合，不混入已接受、已拒绝或仅存在于历史运行中的候选。历史 `prediction.model_version` 仍可用于任务追溯筛选，但 Task 表不展示跨运行拼接的模型版本或 prediction 行级平均分。

追踪候选只统计带非空 `staged_result.results` 且状态为 `pending_review`、`partially_reviewed` 或可审阅 `cancelled` 的 job。非特权用户还需满足 tracker job 的 `created_by` 限制，避免列表显示其无法恢复审阅的候选。

## 轨迹标识

`Annotation.track_id` 是查询和聚合的权威列。compact video geometry 同时保留同值 `geometry.track_id`，所有创建、接受 prediction、导入、tracker 接受、拆分和合并路径必须双写。迁移会回填两种表示并创建 active project-track 索引。

- compact video：一条 track annotation 通常是一条逻辑轨迹；
- scene image/lidar：多条跨 task annotation 共享 track ID，项目轨迹数使用 `count(distinct track_id)`。

Scene track 的 `track_ref` 对 track ID 做 URL-safe 编码；compact track 的 `track_ref` 绑定 annotation UUID，避免只用业务 `track_id` 时出现歧义。详情仍重新连接 visible-task scope；隐藏批次中的成员不会进入 total、facet、质量统计或定位结果。

## 查询成本

显式 `columns_json` 控制任务行昂贵投影；未显示的来源、轨迹、候选、关键帧和点云标定列不会加入列表 SQL。实体列表不在应用层逐 row 发起查询，facet 查询数量固定，不随结果行数增长。summary / facets 独立于分页，并由前端查询缓存按 filter scope 复用。

active annotation 热路径使用以下 partial index：

- `(project_id, updated_at, id)`：对象默认排序；
- `(project_id, class_name, id)`：类别筛选与 facet；
- `(project_id, source, id)`：来源筛选与 facet；
- `(project_id, tool_unit_id, annotation_type, id)`：工具与几何筛选；
- `(project_id, track_id, task_id)`：logical track 聚合。

`scripts/benchmark_data_manager_entities.sql` 提供可重复的 1,000,000 行临时数据基准。当前基线中，对象第一页从 67.954 ms 降至 0.088 ms，类别/来源聚合从 68.023 ms 降至 29.638 ms，Scene track 第一页从 81.149 ms 降至 1.763 ms；所有常用首屏路径均低于 1 s 门槛。生产大表应在低峰期用 `CREATE INDEX CONCURRENTLY` 预建，再运行事务型迁移。
