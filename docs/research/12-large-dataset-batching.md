# 大数据集分包需求调研（10,000+ 量级）

> 编写时间：v0.5.3 阶段 / 2026-04-30
> 关注问题：当单个 dataset 到 1 万 / 10 万量级时，「分包（batch/partition/job）」到底是不是必须做的能力，怎么做。

---

## 落地状态（更新于 2026-09-18）

- **方案 A（`task.batch_label` 字符串）**：未按原样实施，已被方案 B 取代。
- **方案 B（`task_batches` 表 + `BatchService`）✅ 已落地**（v0.6.1 起持续演进）：`task_batches` 模型与 `Task.batch_id`、批次创建 / 拆分 / 状态流转 / 批量分配 / 优先级与 deadline、scheduler 按批次过滤、进度聚合（`batch_summary`）、按批次导出，以及前端批次管理与看板均已上线。
- **仍缺（未随 B 一起落地）**：批次级 IAA / 共识合并、按难度 / 不确定性智能切批、批次 approved 触发的主动学习闭环。
- **§6.4 独立工程项**：`link_project` 异步 / 分块建任务、dataset items 分页缩略图、task 列表虚拟滚动均已落地。

---

## 1. 摘要（结论先行）

- **结论：v0.6 内不做完整分包；只做「最轻方案」（task 加 `batch_label` 字符串字段 + 列表过滤）解燃眉之急；中量方案（独立 `task_batch` 表）放到 v0.7、与「审核流」「主动学习」绑定一起做才划算。**
- 当前项目 0 处出现 batch / job / partition 概念，`DatasetService.link_project()` 一次性把 dataset 全部 items 循环 `INSERT` 成 task（[apps/api/app/services/dataset.py:285-322](../../apps/api/app/services/dataset.py)），1 万张数据集会在一次 HTTP 请求里建 1 万行——这是分包问题之外的另一道独立伤口（先用「分批 commit + 后台任务」止血，不需要业务层分包）。
- 业务层「分包」真正解决的是**任务分配 + 审核节奏 + 主动学习闭环 + 按批导出/快照**这四件事，**不是性能问题**。性能问题用分页 + 索引就够。把分包当性能方案是误诊。
- 友商对照：CVAT 做得最重（Project → Task → Segment → Job 四层，job 是工序流头等公民）；Label Studio OSS 完全没分包概念（用 Data Manager 的 view 做轻量过滤）；Encord/V7/Scale 这些商业产品都把 Batch 作为工作流和 SLA 的最小颗粒。
- 推荐节奏：**轻量方案立刻做（半天工时）→ 等用户出现「想给批次1独立 deadline / 想按批次审核 / 想先标 batch1 训 v1 再回标 batch2」中任意一个真实诉求时，再升级到中量方案。**

---

## 2. 当前项目数据模型快照

### 2.1 关键事实

| 维度                | 现状                                                                                 | 文件:行号                                                                               |
| ------------------- | ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------- |
| Dataset 表          | 无 batch 字段                                                                        | [apps/api/app/db/models/dataset.py:9-20](../../apps/api/app/db/models/dataset.py)       |
| DatasetItem 表      | 无 batch / split 字段                                                                | [apps/api/app/db/models/dataset.py:23-39](../../apps/api/app/db/models/dataset.py)      |
| Project 表          | 无 batch 计数；只有 `total_tasks / completed_tasks / review_tasks` 三个 project 级数 | [apps/api/app/db/models/project.py:29-31](../../apps/api/app/db/models/project.py)      |
| Task 表             | 无 `batch_id`；只有 `status / assignee_id / sequence_order` 三种轴                   | [apps/api/app/db/models/task.py:9-31](../../apps/api/app/db/models/task.py)             |
| Annotation 表       | 无批次维度                                                                           | [apps/api/app/db/models/annotation.py:9-28](../../apps/api/app/db/models/annotation.py) |
| ProjectDataset 关联 | 简单 N:M，无 batch 范围字段                                                          | [apps/api/app/db/models/dataset.py:42-48](../../apps/api/app/db/models/dataset.py)      |

### 2.2 任务生成路径（最关键）

`DatasetService.link_project(dataset_id, project_id)` 在 [apps/api/app/services/dataset.py:285-322](../../apps/api/app/services/dataset.py)：

```python
items = (await db.execute(select(DatasetItem)...)).scalars().all()  # 全量 load
for item in items:
    task = Task(project_id=..., dataset_item_id=item.id, ...)
    db.add(task)
project.total_tasks += created_count
await db.flush()
```

**两个独立问题**：

1. **性能伤口**：1 万 items → 1 次 SELECT 全量 + 1 万次 ORM `db.add` + 1 次 flush。在线请求里跑会超时，且占连接。**这个跟分包无关，先用 bulk insert + 后台任务解决。**
2. **业务伤口**：所有 task 一开始就是 `pending` 状态、project 级一锅煮，没有「先放第一批 2000 条进入流程，剩下 8000 条 stash」的能力。**这个才是分包要解决的。**

### 2.3 调度路径

`scheduler.get_next_task` 在 [apps/api/app/services/scheduler.py:42-69](../../apps/api/app/services/scheduler.py) 按 `project_id + is_labeled=False + 未被自己标过` 选下一题，三种 sampling 都是 project 全集打分。**没有任何批次维度的过滤入口**。

### 2.4 前端

- `DatasetsPage.tsx`（[apps/web/src/pages/Datasets/DatasetsPage.tsx](../../apps/web/src/pages/Datasets/DatasetsPage.tsx)）：dataset 列表 + 展开看 items；items 列表用 `limit/offset` 分页 ([dataset.py:126-162](../../apps/api/app/services/dataset.py))；前端目前是 expand 一行展开看 items，**没有按批次分组的 UI**。
- `Projects/sections/`：只有 General/Members/Owner/Danger 四个 section，**没有批次管理 section**。

### 2.5 量级评估

代码里到处用 `limit=50` 做默认分页，task 列表没看到针对 1 万级的虚拟滚动，audit_log / annotation 历史查询也没显示按批次分页。从 `total_tasks / completed_tasks` 这种总数字段、以及 ProgressService 没拆批次的现状看，**当前产品的隐含设计上限是单 project 几百到几千 task 量级**。1 万+ 时已不仅仅是分包问题：dataset items 列表分页、task 列表前端、ProgressService 实时计算、export 全量打包都会陆续出问题（这些是工程性问题，与本报告主题分包是不同维度）。

---

## 3. 五角色痛点清单（1 万+ 量级）

### 3.1 业务方 / 数据采购方

| 痛点                                                           | 严重度 | 分包能解决吗                        |
| -------------------------------------------------------------- | ------ | ----------------------------------- |
| 一次交付 1 万张，希望按交付批次（如「2026Q2 第一批」）单独跟踪 | 高     | ✅ 直接命中                         |
| 不同来源/不同地区数据想分别看完成度（沿海 vs 内陆）            | 中     | ✅ 命中（如果允许按 metadata 切批） |
| 验收时希望一批一签字，而不是 1 万张一锅端                      | 高     | ✅ 命中                             |

### 3.2 项目经理 / 标注主管 PM

| 痛点                                                                     | 严重度 | 分包能解决吗                                            |
| ------------------------------------------------------------------------ | ------ | ------------------------------------------------------- |
| 「85% 完成」太模糊，剩 1500 张是分散在哪？卡在哪个标注员？卡在哪类难题？ | 高     | ✅ 部分命中（按批次看清得多，但更彻底要 metadata 维度） |
| 想给 50 个标注员公平派活：每人 200 张，下一批等做完再发                  | 高     | ✅ 命中（批次=分配最小单位）                            |
| 希望临时把「这 500 张高优先级」插队                                      | 中     | ✅ 命中（batch.priority + batch.deadline）              |
| 项目失败要回退：删第二批，留第一批已通过的                               | 中     | ✅ 命中（批次级状态/删除）                              |

### 3.3 标注员

| 痛点                                                 | 严重度 | 分包能解决吗                         |
| ---------------------------------------------------- | ------ | ------------------------------------ |
| 每天看自己「今天分到了什么」，而不是「项目还有多少」 | 中     | ✅ 命中（assigned batch）            |
| 不希望同一个项目的 9 千张永远刷不完（心理压力）      | 低-中  | ✅ 命中（batch 给清晰边界 + 完成感） |
| 想知道这一批的截止日期                               | 中     | ✅ 命中（batch.deadline）            |

### 3.4 审核员 / QA

| 痛点                                             | 严重度 | 分包能解决吗                           |
| ------------------------------------------------ | ------ | -------------------------------------- |
| 边标边审还是攒一批审？1 万张一直审到底太累       | 高     | ✅ 命中（批次完成后整批进入审核）      |
| 抽检 IAA：拿哪 N 张算？随机抽对 1 万张统计意义弱 | 中     | ✅ 部分命中（按批次抽检 + 批次级 IAA） |
| 退回机制：不合格的能否整批退、不影响其他批       | 高     | ✅ 命中（batch.status=rejected）       |
| 想看「这批通过率多少」做合作方 KPI               | 中     | ✅ 命中                                |

### 3.5 ML 工程师 / 训练侧

| 痛点                                               | 严重度 | 分包能解决吗                        |
| -------------------------------------------------- | ------ | ----------------------------------- |
| 想拿前 2000 张先跑 v1 验证标签设计是否对           | 高     | ✅ 命中（batch 完成即触发训练）     |
| 主动学习闭环：v1 模型预标 batch 2，标注员只改不画  | 高     | ✅ 命中（这是分包最大的差异化价值） |
| 训练数据快照：v1 训练用了哪些 task？后期不可变追溯 | 高     | ✅ 命中（批次=快照单元）            |
| 数据集越加越大，希望追加而不是重训 → 增量训练      | 中     | ✅ 命中（每批一个训练 round）       |

### 3.6 运维 / 平台侧

| 痛点                                       | 严重度 | 分包能解决吗                                          |
| ------------------------------------------ | ------ | ----------------------------------------------------- |
| 1 万 task 一次拉列表前端卡死               | 高     | ❌ **这是分页 / 虚拟滚动问题，分包不解决**            |
| `link_project` 一次建 1 万 task 把请求卡死 | 高     | ❌ 用 bulk insert + Celery 异步建任务解决，与分包无关 |
| 整 dataset 导出耗时 / 内存爆               | 中     | ✅ 间接命中（按批次导出可分块）                       |
| Progress 实时计算扫全表                    | 中     | ✅ 间接命中（按批次预聚合）                           |

> **关键提醒**：运维痛点里前两条最常被错误归因到「需要分包」。它们其实是分页 + 异步任务的标准工程问题。把分包当性能方案做，会同时引入业务复杂度且不解决性能根因。

---

## 4. 友商怎么做的

### 4.1 Label Studio OSS：**没有 batch，只有 Data Manager View**

- LS OSS 没有 `Batch` 模型 ([01-label-studio.md §2.1.2](./01-label-studio.md))
- 用 `data_manager` app 的 **View**（保存的过滤器 + 排序）做轻量切分；用户在 UI 里圈选一组 task 然后批量分配
- 多人交叉用 `Task.overlap` 字段（每题需要 N 人独立标）
- LSE（企业版）才有 Workflow（接近批次的概念，闭源）
- **结论**：LS 不靠批次，靠 Data Manager 的灵活过滤 + Task 上多个状态字段（is_labeled / overlap / total_annotations）。这条路在数据规模 1 万+ 时也工作，但项目协调全靠 PM 手动操作 view。

### 4.2 CVAT：**Job 是头等公民**

参见 [03-cvat.md §2.3.1](./03-cvat.md)：

```
Project → Task → Segment（按帧切片 start~stop）→ Job（一个工单 → 一个标注员）
```

- `Task` 创建时强制问 `segment_size`（默认 N 帧一段），自动切 Job
- `Job.stage = annotation / validation / acceptance`（工序流）
- `Job.state = new / in_progress / completed`
- `Job.assignee` 是分配的最小单位（**不是 task 级**）
- `parent_job` 支持共识标注（同一段给 3 人独立标后合并）
- IAA / QualityReport 以 job 为单元生成（[03-cvat.md §2.3.4](./03-cvat.md)）

CVAT 的批次是**强制的**——你不能不切 job，因为视频几千帧一个人吃不下。这套设计天然适合视频，对图像数据集稍重。

### 4.3 商业产品

| 产品          | 批次概念               | 关键差异                                                           |
| ------------- | ---------------------- | ------------------------------------------------------------------ |
| **Encord**    | Workflow Stage + Batch | Batch 是 workflow 的最小流转单位；Encord Active 做按批次的难例分析 |
| **Scale AI**  | Batch / Project        | Batch 有独立 SLA、计费、QA 抽样比例；按批次结算                    |
| **V7 Darwin** | Workflow + Stage       | Stage 间通过条件流转；按 stage 分配人员                            |
| **Roboflow**  | Version / Batch        | Version 是数据快照（用于训练）；上传可以分 batch                   |
| **Labelbox**  | Batch + Workflow       | Batch 是分配 + SLA 单元，workflow 控审核流                         |

**共同模式**：商业产品里 Batch 几乎都同时承担三件事——**分配单元、SLA/截止单元、训练快照单元**。

### 4.4 现有调研报告对比

[08-comparison-matrix.md](./08-comparison-matrix.md) §5.3 表里我们已经有的：

- Task Lock ✅、overlap 字段 ✅、ground_truth 字段 🟡
- 但 IAA / 共识 / 审核 stage 都标的 ❌ 或 🟡

如果做中量分包方案，等于一次性把这几个 ❌ 升一档（因为它们都希望以批次为单位）。

---

## 5. 三档方案

### 5.1 对比总览

| 维度               | 轻量（A）      | 中量（B）                | 重量（C）                   |
| ------------------ | -------------- | ------------------------ | --------------------------- |
| 工时估算           | 0.5-1 天       | 5-8 天                   | 15-25 天                    |
| Schema 改动        | task 加 1 字段 | 新表 + Task FK + Service | + workflow stage + 训练触发 |
| 解决业务方/PM 痛点 | 30%            | 80%                      | 95%                         |
| 解决审核员痛点     | 10%            | 60%                      | 90%                         |
| 解决 ML 痛点       | 0%             | 30%（按批导出）          | 95%（含主动学习闭环）       |
| 解决运维性能       | 0%             | 0%                       | 0%                          |
| 可逆性（做错能改） | 高             | 中                       | 低                          |
| 适用项目阶段       | v0.5-v0.6      | v0.7-v0.8                | v1.0+                       |

### 5.2 方案 A：轻量（推荐立刻做）— 已被方案 B 取代，未单独实施 ✅ 由 B 覆盖

**核心**：不引入新表，task 加一个 `batch_label: String` 字符串字段，前端在任务列表加 batch 筛选器。

#### Schema

```python
# task.py 新增
batch_label: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

#### 关键路径改动

- `DatasetService.link_project()`：可选参数 `batch_label`（默认 None），传入则给本次创建的所有 task 标这个值
- `scheduler.get_next_task()`：可选 batch_label 过滤
- `ProgressService`：增加按 batch_label group by 聚合
- 前端：dataset 列表"关联到项目"时让用户填一个批次名（可空）；任务队列上方加 batch dropdown 筛选

#### 能解决

- 业务方"按交付批次跟踪"（粗粒度）
- PM 看「批次 1 / 批次 2 各完成多少」
- 标注员能筛选自己今天做哪一批

#### 不能解决

- 审核流转（status 仍然是 task 级）
- 批次级 deadline / 优先级 / IAA
- 主动学习闭环
- 不能给批次设状态（pending/active/locked/archived）

#### 风险

- **业务复杂度倒灌**：用户慢慢会要求 batch 加属性（deadline、负责人），届时不得不升级到 B 方案，老数据迁移成本不大但前端需要重写筛选器
- **数据治理弱**：批次只是 label，没有"这批被退回"的概念

#### 成本

- 后端：~150 行（field + service 参数 + service 聚合）
- 前端：~200 行（DatasetsPage 加输入框 + 任务列表加 dropdown）
- 数据迁移：1 个 alembic migration（加列，可空）

### 5.3 方案 B：中量（v0.7 候选）✅ 已落地

**核心**：新建 `task_batch` 表，作为分配 + 审核 + 导出 + 进度展示的最小单位。

#### Schema

```python
class TaskBatch(Base):
    __tablename__ = "task_batches"
    id: UUID (pk)
    project_id: UUID (fk projects, index)
    dataset_id: UUID | None (fk datasets, nullable)  # 可来自多 dataset
    display_id: str(20)
    name: str(100)
    description: text
    status: str(20)  # draft / active / annotating / reviewing / approved / rejected / archived
    priority: int  # 0-100
    deadline: datetime | None
    assigned_user_ids: JSONB  # 可分配多人，或 null = 项目所有标注员
    total_tasks: int
    completed_tasks: int
    review_tasks: int
    approved_tasks: int
    rejected_tasks: int
    created_by: UUID (fk users)
    created_at, updated_at

# task.py 新增
batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID, ForeignKey("task_batches.id"), nullable=True, index=True)
```

#### 关键路径改动

- `BatchService`：CRUD + status 流转 + bulk 分配
- `DatasetService.link_project` 改造：默认创建 1 个隐式 batch（向后兼容）；可选传入 `batches: [{name, item_ids 或 item_range, deadline, ...}]` 一次创建多批
- `scheduler.get_next_task`：增加 `batch_id` 入参；按批次过滤候选
- `ProgressService`：按 batch 聚合
- `ExportService`：支持按 batch 导出
- 前端：项目设置加 Batches section；任务队列按 batch 分组；ReviewPage 按 batch 过滤

#### 能解决

- 上述五角色 80% 痛点
- 批次 deadline / 优先级 / 状态流转
- 按批导出（→ 训练快照雏形）
- 退回/重做颗粒度

#### 不能解决

- 主动学习闭环（要 C 方案的训练触发联动）
- 智能分批（按难度/类别/metadata 自动切，仍要人配置）
- 批次级 IAA / 共识合并算法（只是有了字段还没算）

#### 风险

- **隐式 batch 兼容**：老数据迁移要给每个 project 建一个"默认批次"把现存 task 挂上去，否则 nullable 字段会让前端各种判空
- **PM UI 学习成本**：项目设置变复杂；要花时间设计"创建 batch 时怎么选范围"的交互（按 metadata 切？按 item id 范围？随机均分？）
- **status 流转语义膨胀**：需要明确什么时候批次自动变 reviewing，会不会跟 task.status 冲突

#### 成本

- 后端：~800-1200 行（model + service + 6-8 个 endpoint + scheduler 改 + progress 改 + export 改）
- 前端：~1500 行（项目下 Batches Section + 创建/编辑表单 + 任务队列分组视图）
- 数据迁移：建表 + 给现有 project 各建一个 default batch + 给老 task 填 batch_id
- QA：现有 e2e/E1-E12 中至少 E5/E6/E11 需要复测

### 5.4 方案 C：重量（v1.0+ 差异化）

**核心**：在 B 基础上引入 Workflow Stage（pending → annotating → reviewing → approved → training_ready）+ 主动学习闭环（每批训练完，反哺下一批的 pre-annotation）。

#### 增量 Schema

```python
class WorkflowStage(Base):
    id, batch_id (fk), stage_name, status,
    triggered_by_user_id, triggered_at, completed_at, metadata (JSONB)

# task_batch 加：
training_run_id: UUID | None  # 关联到 ML 训练任务
pre_annotated_by_run_id: UUID | None  # 是被哪一轮训练出的模型预标的
```

#### 关键能力

- 批次 approved → 自动触发 ML Backend 训练
- 训练完成 → 模型版本 → 给下一个 batch 跑 prediction（已有 PredictionService）
- 标注员看到的是「修改预标注」而非「从零画」
- 数据快照：每个 approved batch 生成 immutable snapshot，记录 task ids + annotation versions

#### 风险

- 主动学习链条上任何一环 flake（训练失败 / 模型变差 / 预标置信度低）都会 cascade 影响下批的标注效率
- 状态机复杂度高，bug 难调（建议引入 FSM 库如 LS 那个 `fsm/`）
- 训练触发要跟 ML Backend service 深度耦合，目前 [ml_backend.py](../../apps/api/app/services/ml_backend.py) 的 train 接口是否能稳定工作还需评估
- 一旦上线，回退成本极高（数据快照已经被外部训练用了）

#### 成本

- 在 B 之上再 +15-20 天

---

## 6. 推荐与开工顺序

### 6.1 推荐：**立刻做 A，B 留到 v0.7-v0.8 触发条件满足后再做**

**触发条件（任一满足即升 B）**：

- 真有用户提了批次级 deadline / 退回需求（不是猜的）
- 单 project task 数超过 5000 且活跃（不是单一历史项目）
- 审核流要做成"批次级流转"（产品决策）

### 6.2 为什么不直接上 B

1. **当前还没真实用户**：v0.5.3 是单项目单 dataset 流转才稳定（看 commit 历史）。在没真实用户的阶段铺大架构 = 高概率方向错。
2. **B 方案有 1500+ 行前端**：要先把现有任务列表 / 进度 / 审核 UI 摸清楚再动，仓促做会跟下一阶段产品决策（如多 dataset 合一项目、workflow stage）打架。
3. **A 方案数据是 B 方案子集**：A 的 `batch_label` 字符串可以无损迁移成 B 的 `task_batch.name` + `batch_id`，没有架构反向锁。

### 6.3 A 方案开工顺序（半天到一天）

```
1. alembic: 给 tasks 加 batch_label 列（nullable, index）
   → verify: 老 task 仍然查得出来（batch_label IS NULL）

2. DatasetService.link_project 加可选 batch_label 参数
   → verify: 不传 = 老行为；传 "batch_2026Q2_a" = 所有新 task 带这个 label

3. ProgressService 加 by_batch_label 聚合方法
   → verify: 同一 project 两次 link 用不同 label，能分开看到完成数

4. 任务列表 API 加 batch_label query 参数
   → verify: ?batch_label=foo 只返回该批

5. 前端 ImportDatasetWizard / DatasetsPage 关联项目时填 batch 名
   → verify: e2e 走通

6. 前端 WorkbenchPage 任务队列上方 batch dropdown
   → verify: 切换 dropdown 队列正确过滤
```

### 6.4 同时需要单独做的事（与分包无关但被同样场景触发）

这些**不要**跟 A 方案打包：

- **`link_project` 性能修复**：`bulk_insert_mappings` 替代循环 `db.add`，1 万行从 ~30s 降到 ~1s。或者改成 Celery 任务异步建。这是独立 bug。
- **dataset items 列表分页 + 缩略图懒加载**：1 万 items 前端不要一把拉。
- **task 列表虚拟滚动**：1 万 task 前端列表用 react-window 之类。

---

## 7. 附录：术语表

不同产品/语境下这些词指什么不一样，避免讨论时打架。

| 术语                   | 在不同产品中的含义                                                                                                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Batch**              | Encord/Scale/Labelbox：分配 + SLA + 训练快照单位（接近本报告 B 方案的 task_batch）<br>Roboflow：上传时分批（语义弱，几乎=upload session）<br>本项目 A 方案：Task 上的字符串 label |
| **Job**                | CVAT：分配最小单位（一个标注员一个 job），有 stage 工序流<br>LS：完全没有这个词<br>本项目：暂无                                                                                   |
| **Segment**            | CVAT：视频分帧切片（start_frame ~ stop_frame），一个 segment 对应一个 job                                                                                                         |
| **Partition**          | 数据集划分（train/val/test），偏 ML 含义；Roboflow Version 接近此意                                                                                                               |
| **Split**              | 同上，多用于 ML 工程语境                                                                                                                                                          |
| **Round**              | 主动学习/迭代训练的轮次（如 round 1 标 2k → 训 v1 → round 2 用 v1 预标 2k）                                                                                                       |
| **Stage**              | CVAT Job.stage：annotation / validation / acceptance（工序流）<br>V7 Workflow Stage：可配置的工作流节点                                                                           |
| **Workflow**           | LS LSE / V7 / Encord：状态机驱动的整条标注链（含审核、QA、训练）                                                                                                                  |
| **Snapshot / Version** | Roboflow Version、Encord Snapshot：用于训练的数据集不可变快照                                                                                                                     |
| **View**               | LS Data Manager View：保存的过滤器 + 排序，**不是**批次（不会真切分数据）                                                                                                         |

---

## 附：主要参考代码引用

- [apps/api/app/db/models/task.py](../../apps/api/app/db/models/task.py)
- [apps/api/app/db/models/dataset.py](../../apps/api/app/db/models/dataset.py)
- [apps/api/app/db/models/project.py](../../apps/api/app/db/models/project.py)
- [apps/api/app/db/models/annotation.py](../../apps/api/app/db/models/annotation.py)
- [apps/api/app/services/dataset.py](../../apps/api/app/services/dataset.py)（重点：`link_project` 285-322）
- [apps/api/app/services/scheduler.py](../../apps/api/app/services/scheduler.py)
- [apps/api/app/services/exporting/service.py](../../apps/api/app/services/exporting/service.py)
- [docs/research/01-label-studio.md](./01-label-studio.md)
- [docs/research/03-cvat.md](./03-cvat.md)
- [docs/research/05-commercial.md](./05-commercial.md)
- [docs/research/08-comparison-matrix.md](./08-comparison-matrix.md)
