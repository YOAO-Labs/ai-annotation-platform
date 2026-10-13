---
audience: [developer]
type: reference
status: stable
last_reviewed: 2026-09-14
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

attribute key 可以包含点号；解析时只分离 tool unit，剩余字符串完整作为 key。字段必须存在于当前项目启用工具的 attribute schema 中。不同类型允许的操作符由 schema 返回，不能开放任意 JSONB key 或 raw SQL。

task grain 中，同一 AND group 的 annotation rule 编译为一个 correlated `EXISTS`，确保类别、来源、轨迹和属性由同一个 active、非 cancelled annotation 满足；不能为每个 rule 分别生成 `EXISTS`，否则会产生跨对象误命中。object / track grain 则把条件直接绑定到当前 annotation/member；不能先分页 tasks 再展开实体。

结构校验在 SQL 编译前执行：根节点计为第 1 层和第 1 个节点，最多 32 层、4096 个节点；`in` 最多 200 项。非对象子节点、错误的值类型、无效日期或 UUID、非有限数字统一返回 422。日期按 ISO 格式解析，无时区值按 UTC 处理；可空任务字段的 `eq: null` / `ne: null` 保留 `IS NULL` / `IS NOT NULL` 语义。已有视图超出结构限制时保留记录，并以 `invalid_fields: ["__filter__"]` 标记，不能静默删除条件。

## 三种 grain 与分页

- **task**：一行一个任务，使用 offset 分页；summary 表示“匹配任务中的全部对象”。
- **object**：一行一个 active、非 cancelled、非 compact-track annotation。total 与 facet 都在 annotation grain 计算。
- **track**：普通视频以一条 compact track annotation 为实体；Scene 图片/点云按 `(project_id, track_id)` 聚合可见成员。Scene 聚合不会按关键帧或 task 重复成行。

object / track 查询使用版本化的不透明 cursor，cursor 绑定 sort field、direction、上一行 sort value 与稳定 tie-breaker。客户端不得解析或伪造 cursor；改变筛选或排序后必须从第一页重新查询。响应不携带 raw geometry，详情与工作台 deep link 使用 annotation UUID 或编码后的 `track_ref`。

## 聚合口径

summary 同时返回：

- `scope.visible_task_total`：当前用户的项目可见任务总量；
- `scope.matched_task_total`：当前过滤树匹配的任务量；
- 任务状态；
- active annotation 总量及 source/class/tool unit/geometry 分布；
- single-frame、tracked annotation 与 distinct track；
- AI 检测候选待审、低置信候选待审、AI 追踪结果待审；
- 当前待审检测候选的模型版本与置信度区间分布；
- 未解决问题（有效、开放的问题主题，对齐工作台问题口径；兼容字段名 `unresolved_feedback`）；
- 项目 schema 中属性的 eligible/present/missing 与有限枚举值分布。
- capability 驱动的 image 分辨率、video 时长/帧/关键帧、lidar 相机/标定与 Scene 摘要。

属性 `eligible` 不等同于同工具单位的全部对象：服务会同时应用字段的 `applies_to` 类别范围和 `visible_if` 依赖条件，再计算 present/missing。属性字段循环受项目 schema 数量约束，不随 task 或 annotation 行数增长为应用层 N+1。

task-centric summary 聚合的是“匹配任务中的全部对象”。object / track query 的 `facets` 则只聚合该 grain 的完整匹配集合，并为可视化图表提供 class、source、tool/type 与 track quality 分布；不能用当前页 rows 在浏览器抽样。

任务的“匹配对象”抽屉先确认完整筛选和任务权限，再按真实命中分支收集解释：OR 只贡献成立的分支；一个 AND 内的直接 annotation 条件共享同一对象，不同嵌套组保留独立见证对象。单纯任务条件保留 active、非取消对象作为上下文；它在已经由实体解释的 AND 中不会扩大对象范围。空分组（`rules: []`）无论 `op` 为 and 还是 or 都与编译器一致地编译为恒真：命中按任务上下文解释，返回全部 active、非取消对象而不是空见证集，位于已由实体解释的 AND 中时同样不扩大对象范围。历史模型条件属于任务追溯，不会冒充当前待审候选。

命中项固定按 annotation、prediction shape、tracker job 排列，三个来源共用一个 offset/limit 窗口。总数和候选页都从 SQL 的完整匹配范围派生，仅转换当前页的预测几何；低置信条件使用与指标相同的剩余 shape 和 `< 0.5` 阈值。零候选计数可以命中任务而没有候选明细。OR 分支真值用单个布尔数组列返回，宽条件树不会因逐节点增加结果列而触发 PostgreSQL 列数上限。

## Saved view 与 URL

`project_task_views.entity_scope` 为 `tasks | objects | tracks`，旧记录迁移为 `tasks`。私有名称唯一键与项目共享名称唯一键都包含 scope，因此三个粒度可以使用同名视图。创建和更新必须使用对应 schema 的 filter/sort/column 白名单；不兼容字段在列表中以 `invalid_fields` 返回，不静默改写。

前端 URL 保存 `section/lens/view/q/filter/sort/columns/selected/layout/selected_tasks`。`section` 为 `overview | data | members`，缺省仍打开 `data`；`lens` 仅表示数据区域的 grain。filter、sort 与 columns 使用带版本号的 JSON envelope；解析失败时回退当前视图，不执行未校验输入。切换 grain 时清空不兼容状态，存在未保存修改时先要求确认。成员筛选使用独立的 `members_*` 参数，不继承 Data Manager 的对象条件。

三个 grain 共享值编辑控件和保留结构的表达式操作。分组不能展平：即使普通布尔代数等价，也可能改变同对象约束。关键词只从独立 `contains` 规则或直接 AND 中的无歧义规则提取；OR 内关键词保留在树中。外部 q 作为额外 AND 条件，数字和范围草稿不进入查询。页面读取实时 URL，首次请求等待视图和字段恢复；后续条件变化使过期页码和实体 cursor 失效。

一级区域为概览、数据、成员，grain tabs 位于数据区域内。数据区域使用单视口布局，结果表和详情抽屉独立滚动；概览和成员内容允许纵向滚动。桌面端保存视图使用可收起的分组侧栏，窄屏使用下拉。任务、对象与轨迹共用可搜索字段选择器和条件芯片，字段分组及编辑控件完全由各自 `schema.filter_fields` 驱动。这些布局差异不改变 Filter DSL、URL envelope 或保存视图契约。

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
