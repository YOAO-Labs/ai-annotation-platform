# 0047 — Data Manager 采用分 grain 实体 read model

- **Status:** Accepted
- **Date:** 2026-07-11
- **Deciders:** core team
- **Supersedes:** —（扩展 task-centric Data Manager，不改变 annotation 权威写模型）

## Context

Data Manager 最初以 task 为唯一结果 grain。它可以回答“哪些任务包含某类对象或轨迹”，但不能直接分页、排序和定位具体对象，也不能把跨 task 的 Scene track 稳定聚合成一行。若继续在 task 查询结果上前端展开，会出现三类问题：

1. total、facet 和分页仍是 task 数，却被误读成对象或轨迹数；
2. compact video track 会按关键帧重复，Scene track 会按 task 拆散；
3. 浏览器需要拉取大量 annotation，且容易绕过服务端 visible-task scope 泄露不可见批次的统计。

同时，annotation、prediction 与 tracker candidate 已有成熟写路径。为探索页面另建一套可写实体表会引入双写和一致性债。

| 选项                                                                  | 主要卖点                                 | 主要劣势                                       |
| --------------------------------------------------------------------- | ---------------------------------------- | ---------------------------------------------- |
| **A. 保留权威写模型，按 task/object/track 分 grain 构造只读查询服务** | total、facet、分页和权限语义准确；无双写 | 需要三套受控投影与 track 聚合器                |
| B. task 查询后在前端展开 annotation                                   | 实现快                                   | total/分页错误，大数据集不可用，权限边界脆弱   |
| C. 新建对象/轨迹投影表并同步写入                                      | 查询最快                                 | 引入写时同步、回填和漂移治理，当前规模收益不足 |

## Decision

采用方案 A：Data Manager 保持单一路由壳层，但以 `entity_scope = tasks | objects | tracks` 明确查询 grain。

### 查询边界

- task 查询继续使用 task grain 与 offset 分页。
- object 查询直接在 active、非 cancelled annotation grain 使用 keyset cursor，不先分页 task；compact track annotation 不进入 object 结果。
- compact video track 以 annotation UUID 作为稳定实体引用；Scene 图片/点云按 `(project_id, track_id)` 聚合可见成员。
- 所有查询、saved-view count、facet、detail 和 location 都先连接 `visible_tasks_stmt`。不可见实体统一表现为 404，不通过 total 或 facet 暴露存在性。
- read model 只返回筛选、统计、属性溯源和定位所需字段，不返回 raw geometry，不提供批量写操作。

### Saved view 与客户端状态

`project_task_views` 增加 `entity_scope`，名称唯一约束包含 scope。旧记录默认归入 tasks。schema 按项目 capability 和 scope 返回字段、操作符、列与排序白名单；不支持轨迹的项目拒绝 tracks scope。

前端 URL 记录 lens、view、搜索、filter、sort、columns 与 selected entity。object / track 结果使用服务端 cursor 与客户端虚拟化；统计图表读取完整匹配集合的服务端 summary/facets，不能从当前页抽样。

### 性能策略

优先为 annotation 一等列建立 active partial B-tree：项目 + 更新时间、类别、来源、工具/几何，以及既有项目 + track ID。只有 compact geometry 深层过滤在执行计划不达标时，才考虑受控派生 projection；不默认添加宽泛 JSONB GIN。

## Consequences

正向：

- 三种 total、facet 和分页单位与界面语义一致，能够直接搜索并定位对象或逻辑轨迹。
- 图片、视频、点云与 Scene 项目共用壳层和权限模型，同时允许 capability 驱动的不同列与轨迹聚合器。
- 权威写模型不变；新增能力是可删除、可重建的查询投影与 UI，不承担 annotation 一致性真值。
- keyset cursor、虚拟化和 partial index 使大结果集不依赖浏览器全量聚合。

负向：

- task、object 与 track 各自有字段白名单、默认列和 saved views，跨 scope 切换不能自动迁移任意过滤树。
- Scene track 聚合按项目内 track ID 合并；若上游错误复用 track ID，会显示 `multiple_scenes`、类别或属性不一致等质量信号，而不是猜测拆分。
- compact track 的关键帧统计仍需读取受控 geometry 字段；若真实规模基准退化，需要引入派生 projection 并新增 ADR，而不是在请求中继续堆 JSONB 展开。

## Alternatives Considered（详）

**方案 B（task 查询后前端展开）**：无法让 total、cursor 和 facet 变成实体 grain，并会把权限正确性依赖于客户端；与百万 annotation 的性能目标冲突。

**方案 C（持久化对象/轨迹投影表）**：能进一步压低复杂聚合延迟，但当前 partial index 基准已满足门槛。现在引入异步刷新、双写或 CDC 会增加远高于收益的运维成本；保留为执行计划持续不达标时的后续选项。

## Notes

### Amendment — 2026-09-14

Data Manager 扩展为概览、数据、成员三个区域。实体 read model 的只读边界继续有效；页面新增的任务分派、导出和预标注通过独立命令入口，分别复用 BatchService 的成员验证、TaskLockService、现有导出器及预标注 worker。

首批操作限定显式选择的 1–200 个 task ID，不把任务集合转换成 batch 集合，不提供全匹配选择、批量审核或删除。分派先预览，再在锁内验证版本并应用；异步作业持久化原始范围和幂等标识，执行时复核范围与权限。

成员统计使用独立的项目绩效服务和查询范围，仅项目负责人及超级管理员可读。首审事实与审核轮次在任务工作流事务内记录，已记录时长通过校验后的 TaskEvent 采集；这些事实不复制 annotation 权威写模型。技术口径见[项目成员绩效数据../../../docs-site/dev/concepts/project-performance.md)。

原有不带 `section` 的链接继续打开数据区域；任务/对象/轨迹仍是数据内部的 grain。新增操作不改变 Filter DSL、实体分页或 visible-task scope。

- 实现代码：`apps/api/app/services/data_management/entities.py`、`data_management/tracks.py`、`data_management/entity_filters.py`、`data_management/cursor.py`、`apps/web/src/pages/Projects/data-manager/`
- 迁移：`0119_project_task_view_entity_scope.py`、`0120_data_manager_entity_indexes.py`
- 基准：`apps/api/scripts/benchmark_data_manager_entities.sql`
- 相关 ADR：[ADR-0045](0045-track-id-as-annotation-column.md)
