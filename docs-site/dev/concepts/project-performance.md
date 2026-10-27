---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# 项目成员绩效数据

Data Manager 成员区域由 `apps/api/app/services/project_performance.py` 提供统一的列表、详情、依据和 CSV 计算。它用于项目负责人了解成员贡献与工作瓶颈，不提供综合分数。旧的个人绩效字段保留原有契约，新页面使用明确命名的指标。

## 权限与范围

- **角色门禁**：团队接口（`/projects/{project_id}/performance/*`）仅允许项目负责人和超级管理员访问；普通项目成员继续通过 `/me/performance` 查看自己的数据，前端隐藏入口不能代替服务端鉴权。
- **成员名单范围**：
  - 默认包含负责人、零活动成员和停用账号。
  - `include_historical=true` 可加入存在项目活动但无当前成员关系的用户（标记 `is_current_member=false`）；区间内持久首审快照中的贡献者也可进入列表，即使原审计已归档。活动记录不能证明历史成员角色或加入日期。
- **时间区间与对比期**：
  - 所有接口解析相同的 `from`、`to`、IANA `timezone`、`work_type`、账号状态和成员搜索范围。
  - 区间为 `from <= timestamp < to`，最多 90 天（按完整本地日计算，兼容夏令时切换）；日期字符串按指定时区午夜解析，带时区的时间戳按实际瞬间解析。响应回显 UTC 边界、时区和 `as_of`。
  - 对比期与当前区间等长；当前待处理量始终是查询时刻的存量。
- **待处理量（存量）口径**：复用 scheduler 的 `effective_task_assignee_expr()` 和 `effective_task_reviewer_expr()`（任务非空指派覆盖批次默认，空值回退批次；未分批且未指派的任务只进入项目存量）。
- **项目总数去重**：成员筛选与 Data Manager 的任务/对象 DSL 分开；列表分页不改变项目总数，项目任务数按项目去重，不能把多人的贡献任务直接求和。

## 产出、归属与首审

### 核心指标与归属口径

| 指标维度                | 统计口径与归属规则                                                                                                                                                       |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **提交与重提**          | 按成功工作流事件的实际操作人归属（任务改派后当前 assignee 不覆盖历史提交人）；跳过任务保留为工作流依据，不计为提交或重提产出；重提单独计数                               |
| **审核通过与驳回**      | 标注视角按该轮审核记录的**贡献人快照**归属审核结果；审核视角按**实际决策人**归属；兼任两种工作的成员不会重复累计同一次驳回原因；多轮批准的同一任务在去重通过数中仅计一次 |
| **每日趋势**            | 沿用成员汇总的任务去重口径，将同一任务计入所选区间内首次发生的本地日期                                                                                                   |
| **首审通过率（FPY）**   | 首次送审或跳过时把贡献人快照复制到任务；首次完成审核时在任务锁同事务写入 `first_reviewed_at`、`first_review_result` 和快照（后续审核不覆盖）；待审任务不进入首审分母     |
| **保留标注（Objects）** | 按区间内创建、当前 `is_active=True` 且 `was_cancelled=False` 的记录计算，按创建人、类别、来源与几何类型展开；导入标记优先于普通 source，不把导入或 AI 内容当成人工工作量 |

### 历史覆盖度与数据库内聚合

- **缺失快照保守标记**：缺失或无有效贡献人的轮次快照不用于成员归属，会增加未归属审核决定数并将标注结果归属覆盖标记为 `incomplete`，绝不按当前负责人补造贡献。
- **首审资格（`first_review_eligible`）**：历史存量任务保持未知，新建任务才默认可记录完整首审事实；缺少明确历史的任务不自动补成已通过，也不使用 `reopened_count == 0` 作为通过证据。每个比例指标均携带 `numerator`、`denominator` 和 `coverage` 状态。
- **撤回与重开**：撤回或重开会清空当前提交时间；历史视频任务若在区间内送审但缺少对应审计，提交指标标记覆盖不完整。审核视角的总体覆盖仅受审核证据影响，不继承标注提交与首审历史的缺口。
- **全 SQL 分组聚合**：工作流与首审事实在 PostgreSQL 内按成员、日期和驳回原因分组并关联审核轮次；返回行数仅随聚合分组增长，Python 不加载全量工作流 ORM 或任务 ID 集合。空值指标以成员 UUID 稳定排序。

## 已标注图片与独立项目总数

- **`annotated_images` 口径**：
  - 统计“区间内创建且仍保留有效标注的不同任务数”，并严格限制任务实际 `file_type == image`（不按 `Project.data_type` 圈定，视频与点云任务只进入全模态 `contributed_tasks` 和 `retained_objects`）。
  - Scene 图片帧任务按任务计一次；同一物理媒体关联为不同任务时按任务计数。
  - 保存内容指标回答“这段时间保存了什么”，不要求任务已提交或已审核；同一图片上的多个对象计为 `1` 张图片和 `N` 条对象记录，未保存草稿（`AnnotationDraft`）和空图片不计入。
- **时间与归属边界**：归属跟随记录的创建者/接受者/导入者（`Annotation.user_id`），任务改派或他人编辑已有对象不转移原计数；把昨天的框移动到今天不产生今天的产出。
- **项目级去重聚合（`PerformanceTotals`）**：`PerformanceTotals.annotated_images` / `retained_objects` 在 SQL 内（`_annotation_totals()`）按相同资格谓词独立聚合成 distinct 任务/记录数，不直接对成员行求和（例如两名成员在同一图片上各保存一个对象时，各自 `annotated_images=1`，项目总计 `annotated_images=1`、`retained_objects=2`）。

## 计时采集

### 客户端采样与队列（`useSessionStats`）

- 同时服务工作台 ETA 和项目已记录时长，捕获任务、项目、账号及标注/审核类型。
- 切题或离开时关闭当前区间，隐藏页面暂停，超过 5 分钟无输入停止累计；连续区间每 30 分钟拆分（仅影响上传粒度，ETA 仍按单题累计时长采样）。
- 上传队列每批最多 200 条、暂存最多 1,000 个区间并做有限重试；队列溢出会发送 `collection_coverage=partial` 标记（落库保留为 `session / unverified_collection`，不计入合格时长）。

### 服务端校验与幂等入库（`services/task_event_ingestion.py`）

API 和 Celery 持久化任务共用同一套校验与入库逻辑：

- **身份与范围**：账号来自认证上下文，项目来自已授权任务；客户端项目冲突、不可见任务、错误工作类型和非法区间直接拒绝。
- **时间与幂等锁**：校验时间顺序、时区、未来时间、时长与区间长度一致性；按客户端事件 UUID 有序获取 PostgreSQL 事务锁，同内容重试保持幂等，冲突内容拒绝改写（同步单事件返回 `409`，混合批次丢弃冲突项并在 `discarded` 中返回索引、事件 ID 和原因）。
- **采集等级（`qualified` vs `unverified_collection`）**：
  - 新采集器使用 `collector_version=session-v2`，记录 `collection_source=session` 与 `collection_coverage=qualified`（表示通过归属、时间区间与去重校验，不作为考勤或防作弊证明）。
  - 旧事件保留 `legacy / unverified_collection`，新团队接口不从其生成成员身份或活动依据。
- **工作流时间窗裁剪**：任务提交或完成审核后，最后区间保留 5 分钟收尾容差；更早的缓冲区间必须落在该操作人的服务端分派/认领起始时间（含 5 分钟入场容差）与本次状态转换之间。
- **区间并集聚合**：仅计算 `qualified` 区间并与请求边界求交；同一成员、同一工作类型的重叠区间在 PostgreSQL 内取并集，避免多标签页重复累计。无合格采集证据时返回 `null` 和未知覆盖。

## 接口与客户端状态

- **API 端点**：位于 `/projects/{project_id}/performance`，包含 `members`、`members/{user_id}`、`members/{user_id}/events` 和 `export`。指标返回 `value`、`unit`（`tasks` / `objects` / `images` / `decisions` / `minutes` / `percent`）、可选分子分母及 `coverage`；CSV 导出自动转义可能被电子表格执行的公式前缀。
- **前端状态同步**：URL 使用 `members_*` 参数保存成员筛选，切换账号/项目或浏览器历史时丢弃过期请求结果；详情趋势、依据分页和导出继承已应用范围，无效日期草稿不触发新查询。
- **代码入口**：`apps/api/app/api/v1/project_performance.py`、`apps/api/app/services/project_performance.py`、`apps/web/src/pages/Projects/data-manager/ProjectMembersPerformance.tsx`。
