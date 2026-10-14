# 标注员绩效能力对标调研（Annotator Performance）

> 历史源码定位：外部路径来自当时的本地 CVAT / Label Studio 检出，本文未记录固定 commit，当前仓库也不包含这些检出。下文保留代码路径作历史索引，不将它们当成可用本地链接或已验证的当前上游源码。

> 调研日期:2026-06-03 · 调研者:Claude
>
> **目的**:为平台「绩效相关页面」的下一步深化做对标输入。源码级深读 CVAT(`../cvat`)、Label Studio(`../label-studio`),web 调研 6 个商业产品(Labelbox / Kili / Scale Rapid / Encord / Supervisely / Segments.ai / SuperAnnotate)。
>
> **使用方式**:本档是**研究输入**,不是 sprint backlog。落地节奏与版本切片见配套计划 [`docs/plans/archive/2026-06-03-annotator-performance-deepening.md`](../plans/archive/2026-06-03-annotator-performance-deepening.md)。

---

## 0. 摘要(给赶时间的人)

**我们的现状**(`/admin/people` v0.8.4 + `/me/performance` v0.12.3):per-user 吞吐、耗时 p50/p95、reject 率 + 原因类型(漏标/多标/类别错/位置错)、质量分(=1−reopen 率)、7 日趋势、活跃分钟、weekly_compare、alerts(high_rejected / drop_30)、个人 vs 团队均线。

**一句话结论**:**吞吐 / 工时 / 运营告警**三块我们已达甚至超过行业线(reject 分类型 + p50/p95 比多数商业产品更细;alerts 阈值化≈Labelbox Monitor 雏形)。真正的**结构性盲区**是质量度量的两大支柱:

1. **多人一致性 / Consensus / IAA** —— 我们的"质量分=1−reopen 率"是**单向 review 派生信号**,测不了"题目本身有歧义 / 多人分歧"。Labelbox、Kili、SuperAnnotate、Label Studio(企业)全有。
2. **GT / Honeypot / Benchmark 抽检** —— 衡量**绝对准确率**的唯一客观手段(reopen 率只是相对审核信号)。Kili、Labelbox、Scale、CVAT 全有。

外加两个**标配缺口**:**project_admin 项目级范围**、**报表导出**。

> 优先级研判:**Honeypot/GT 抽检 ＝ Consensus/IAA**(质量能力是否"合格"的行业硬门槛)> **project_admin 项目级范围** > **报表导出 / UI 深化**。前两者是结构性盲区,但体量大、需 ADR + 数据建模;后两者是快赢。

---

## 1. 跨产品指标 taxonomy(去重)

把 CVAT / LS / 6 商业产品的绩效指标去重归并成 5 类。✅=我们已有,⚠️=部分/可派生,❌=缺失。

### A. 吞吐类(Throughput)

| 指标                     | 定义                                            | 我们                    |
| ------------------------ | ----------------------------------------------- | ----------------------- |
| Done / completed count   | 进入完成态的计数(日/周)                         | ✅ throughput           |
| Labels / objects created | 创建标注对象总数                                | ✅ total_annotations    |
| Labeling **actions**     | 几何体 + 赋 tag 的动作数(比"标签数"细;多人各计) | ⚠️ 按 annotation 计     |
| Reviews count            | approve/reject 动作数                           | ⚠️ 有 review 事件未单列 |
| Labeling speed           | objects / active-time(对象/小时)                | ⚠️ 有耗时+计数可派生    |

### B. 工时类(Time / Effort)

| 指标                                | 定义                                                       | 我们                                |
| ----------------------------------- | ---------------------------------------------------------- | ----------------------------------- |
| Labeling / review / **rework** time | 分模式计时(返工独立计)                                     | ⚠️ task_events.duration_ms 不分模式 |
| Total / active time(剔除空闲)       | 合计,排除 >N 分钟空闲(LS/Supervisely 5min、CVAT 100s/事件) | ✅ active_minutes(口径待校)         |
| Avg / p50 / p95 time per task       | 单位耗时分位                                               | ✅ **p50/p95 比多数产品细**         |
| AHT(Average Handling Time)          | per created/submitted/done label 多状态均时                | ❌                                  |

### C. 质量类 — GT / Honeypot / Benchmark(单人对标准答案)

| 指标                           | 定义                                                                        | 我们              |
| ------------------------------ | --------------------------------------------------------------------------- | ----------------- |
| **Honeypot / Benchmark score** | 队列混入已知答案,比对 annotator;per-class/per-labeler;分类 0/100%、检测 IoU | ❌ **结构性缺失** |
| Accuracy / Precision / Recall  | 对 GT 的混淆矩阵派生                                                        | ❌                |
| **Confusion matrix**           | label×label 预测 vs 真实矩阵                                                | ❌                |
| Mean IoU / OKS                 | 检测/分割/关键点的空间精度                                                  | ❌                |
| Calibration score              | audit 拒绝 + 自评信心,>80% 才进生产(Scale)                                  | ❌(差异化)        |

### D. 一致性类 — Consensus / IAA(多人一致性)

| 指标                    | 定义                                                        | 我们                        |
| ----------------------- | ----------------------------------------------------------- | --------------------------- |
| **Consensus score**     | 同一资产多人标注的相互一致度;可配覆盖率% 与人数             | ❌ **结构性缺失**           |
| 按任务类型的一致性算法  | 分类=一致率、检测=IoU、转写=Dice、NER/OCR=Levenshtein(Kili) | ❌                          |
| Control-tag 加权一致性  | 按标注类型权重聚合(LS control_weights)                      | ❌                          |
| `precomputed_agreement` | task 级一致性分数存储字段                                   | ⚠️ **DB 列已存在,完全没接** |

### E. 运营 / 管理类(Ops)

| 能力                                    | 说明                          | 我们                                           |
| --------------------------------------- | ----------------------------- | ---------------------------------------------- |
| Per-member 绩效表                       | 逐人 一张表                   | ✅ /admin/people                               |
| **Team activity heatmap**               | 逐日/小时活跃热力图           | ❌(AnnotatorDashboard 有 hour_buckets 雏形)    |
| Class / tag balance                     | 类别分布占比                  | ❌(§4.1 点名"类别覆盖")                        |
| **Outlier detection**                   | 按 N-sigma 自动圈异常 labeler | ⚠️ 有固定阈值 alerts,无 sigma                  |
| Issues 追踪                             | issue 创建/解决/重开趋势      | ⚠️ 有 annotation_feedbacks 未做趋势            |
| **Trends over time**                    | 日/周/月趋势                  | ✅ 7 日 + 4 周趋势                             |
| **Export 报表**                         | CSV/PDF/Excel/JSON/API        | ❌ **标配缺失**                                |
| Org / 跨项目聚合 + **per-project 范围** | 两层视图                      | ⚠️ 仅 super_admin 全局,无 project_admin 项目级 |

---

## 2. CVAT:质量驱动(GT 对比)

> 源码:`../cvat`。CVAT 的绩效是**质量驱动**(围绕 GT job 对比),工时走**事件流 + ClickHouse**。

### 2.1 工时:客户端事件流 + 服务端聚合

- 客户端收集 `draw:object` / `change:frame` / ... 事件,3 触发器(90s 定时 / Save / 开关视图)批量推送。
- 服务端 `compute_working_time_per_ids`(events/utils.py:84-121（历史路径 `cvat/apps/events/handlers.py`）):事件间隔 **<100s** 才计入工时(空闲剔除),`change:frame` 这类带 duration 的累加。落 `send:working_time` 事件。
- 存 ClickHouse `events` 表(scope/obj_id/duration/count/user_id/project/task/job + payload JSON),按月分区。Grafana 看板 `sum(working_time)` + `count() as Activity` GROUP BY user×project×task×job。
- **借鉴**:工时"空闲剔除"规则要透明(我们 active_minutes 口径需明确);ClickHouse 路线我们已有 DuckDB 离线档对应,暂不上。

### 2.2 质量:GT job + 8 类冲突 + 混淆矩阵

- `quality_control` app:把某 job 标为 `GROUND_TRUTH`,`DatasetComparator` 用 IoU/OKS 匹配标注员标注 vs GT,产出 **Job→Task→Project 三级报告**(quality_control/models.py:91-246（历史路径 `cvat/apps/quality_control/models.py`）)。
- **8 类冲突**(比我们 4 类 reject 原因更细):`MISSING_ANNOTATION`/`EXTRA_ANNOTATION`/`MISMATCHING_LABEL`(error)、`LOW_OVERLAP`/`MISMATCHING_ATTRIBUTES`/`MISMATCHING_DIRECTION`/`MISMATCHING_GROUPS`/`COVERED_ANNOTATION`(warning)。
- 指标:accuracy = valid/(ds∪gt)、precision = valid/ds_count、recall = valid/gt_count、mean_iou、**confusion_matrix**(label×label,带 jaccard_index)。
- 可配阈值:`iou_threshold`(0.4)、`low_overlap_threshold`(0.8)、`oks_sigma`(0.09)、`target_metric`(accuracy)+ `target_metric_threshold`(0.7)。
- 归因:`Job.assignee` → 标注员;`AnnotationConflict.annotation_ids`(obj_id, job_id, type, shape_type)→ 定位到具体出错标注。
- **借鉴**:① reject 原因从"4 类"可扩到"冲突分类"颗粒;② **有 GT 时直接出 accuracy/precision/recall + confusion_matrix**,无需 reopen;③ 三级钻取报告结构契合 RBAC。

---

## 3. Label Studio:一致性 + 项目级隔离

> 源码:`../label-studio`。LS 是**项目级隔离**,质量靠 **overlap + agreement**。

### 3.1 lead_time / 成员统计(OSS 已有)

- `Annotation.lead_time`(秒,tasks/models.py:723（历史路径 `label_studio/tasks/models.py`）),FSM `annotation_created` transition 捕获;Data Manager `Avg('annotations__lead_time')` 出 per-task `avg_lead_time`。
- `annotators` 列 = `ArrayAgg('annotations__completed_by')` 任务级标注员聚合;`total_annotations`/`cancelled_annotations` per-task 计数。
- **借鉴**:per-task-annotator 对的聚合(我们按 user 聚合,缺"某题谁标的"维度)。

### 3.2 Agreement / overlap(OSS 存字段、企业算)

- `Task.overlap`(重复标注数)+ `Task.precomputed_agreement`(task 级平均一致性);`agreement_threshold` 判 task 完成(`_agreement >= threshold`)。
- `control_weights`(JSON,按 bbox/label 等标注类型设权)→ 加权一致性。OSS 存字段,**计算逻辑由企业版 `GET_TASKS_AGREEMENT_QUERYSET` 钩子提供**(可插拔)。
- **借鉴**:① 我们 `tasks.precomputed_agreement` 列也存在且**完全没接**(和 LS OSS 同状态)——这是 IAA 的天然落点;② overlap(覆盖率)→ 多人标同题 → agreement 的设计范式;③ 一致性算法做成**可插拔接口**,不写死。

### 3.3 项目级 scope(我们正缺的)

- `ProjectMember` 强隔离;Data Manager `annotators` 列下拉 = `project.all_members`;`TaskQuerySet` 过滤 `project__in`。`ProjectSummary` 存项目级聚合(类别分布 `created_labels`、`created_annotations`)。
- **借鉴**:project_admin 项目级范围直接参考"每个聚合 + project 过滤 + 成员限定";`ProjectSummary` 式的类别分布预聚合可喂"类别覆盖"面板。

---

## 4. 商业产品:绩效亮点

| 产品              | 绩效亮点(差异化)                                                                                                                                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Labelbox**      | 工时拆 labeling/review/**rework** 三态 + 全套 **AHT**;**Monitor** 工作区按 **N-sigma outlier detection** 自动圈异常 labeler;质量两把尺 Benchmark(对 GT)+ Consensus(对他人)。                                              |
| **Kili**          | 质量算法**最完整**:Consensus + Honeypot + Review-score 三套,每种任务类型给精确公式(分类一致率/检测 IoU/转写 Dice/NER·OCR Levenshtein),多 job 加权·几何平均;per-class × per-labeler 双维。强调"统计 actions 而非 assets"。 |
| **Scale Rapid**   | 把质量分回灌**资格/路由**:Calibration(audit 拒绝+自评)>80% 才进生产;Evaluation tasks 带 concept+difficulty 均衡采样,差者降级/踢出。                                                                                       |
| **Encord**        | Analytics 四视图 Tasks/Labels/**Collaborators**/Issues;Collaborators 分 annotator/reviewer 两套(avg time、label rejection rate、task rejection rate、active time);**Org Analytics** 跨项目可比;CSV 列选择器导出。         |
| **Supervisely**   | 绩效表、团队热力图、标注速度、通过率、class/tag 统计、空闲剔除和 PDF/Excel 导出;当前已有 Consensus,公开材料中未见 GT/honeypot 闭环。                                                                                      |
| **Segments.ai**   | 指标干净:per-user/per-sample/org 三层,各拆 label/review/**edit** 三类 count+total+avg;导出最灵活 CSV/JSON/Excel + API + Python SDK。无 consensus/honeypot。                                                               |
| **SuperAnnotate** | 实时 dashboard + **predictive analytics**(估完工时间/资源);hourly precision。                                                                                                                                             |

来源(择要):[Labelbox Performance dashboard](https://docs.labelbox.com/docs/performance-dashboard) · [Labelbox Monitor](https://docs.labelbox.com/docs/monitor) · [Kili Consensus](https://docs.kili-technology.com/docs/consensus-overview) · [Kili Honeypot](https://docs.kili-technology.com/docs/honeypot-overview) · [Kili 计算规则](https://docs.kili-technology.com/docs/calculation-rules-for-quality-metrics) · [Scale Rapid Calibration](https://scale.com/docs/rapid-or-calibration) · [Encord Org Analytics](https://encord.com/blog/organization-analytics/) · [Supervisely Labeling Performance](https://docs.supervisely.com/labeling/labeling-performance) · [Supervisely Labeling Consensus](https://docs.supervisely.com/labeling/jobs/labeling-consensus) · [Segments.ai metrics](https://docs.segments.ai/background/labeling-metrics) · [SuperAnnotate Quality](https://www.superannotate.com/quality-management)

---

## 5. Gap 总表 + 落地优先级

| 能力                                     | 行业普及度             | 我们                   | 体量                             | 优先级        | 数据基础                                            |
| ---------------------------------------- | ---------------------- | ---------------------- | -------------------------------- | ------------- | --------------------------------------------------- |
| **GT/Honeypot 抽检**(accuracy/per-class) | 高(Kili/LB/Scale/CVAT) | ❌                     | 大(需建 GT/抽检管线 + ADR)       | **P1 结构性** | 需新表 + 调度;`ground_truth` 列已存在于 annotations |
| **Consensus / IAA**(多人一致性)          | 高(LB/Kili/SA/LS)      | ❌                     | 大(需 overlap 分发 + 一致性算法) | **P1 结构性** | `tasks.precomputed_agreement` 列已存在未接          |
| **project_admin 项目级范围**             | 标配(全部)             | ❌                     | 中(每聚合加 project 过滤 + RBAC) | **P2 标配**   | 数据齐,纯查询改造                                   |
| **报表导出 CSV**                         | 标配                   | ❌                     | 小                               | **P2 快赢**   | 数据齐                                              |
| **Reject 原因细分(per-person)**          | —                      | ⚠️ 有类型未 per-person | 小                               | **P2 快赢**   | reject_reason_type 现成                             |
| **类别覆盖 / 分布**                      | 中                     | ❌                     | 小                               | **P2 快赢**   | annotations.class_name 现成                         |
| **指标下钻到任务**                       | 中                     | ❌                     | 小-中                            | **P2**        | 数据齐                                              |
| **/admin/analytics 升级 recharts**       | —                      | ⚠️ CSS 条              | 小                               | **P3 收尾**   | recharts 已进                                       |
| **工时热力图(小时×星期)**                | 中(Supervisely)        | ❌                     | 小-中                            | **P3**        | task_events.started_at 现成                         |
| **首过率 first-pass yield**              | —                      | ❌                     | 小                               | **P3**        | tasks.reopened_count 现成                           |
| **Outlier N-sigma 告警**                 | 中(Labelbox)           | ⚠️ 固定阈值            | 小                               | **P3 升级**   | 现有 alerts 升级                                    |
| **Calibration 资格门禁路由**             | 低(Scale 独有)         | ❌                     | 大                               | **P4 差异化** | 依赖 Honeypot 先落                                  |
| AHT / rework 分模式工时                  | 中                     | ❌                     | 中(需 task_events 加 mode)       | **P4**        | 需扩事件维度                                        |

---

## 6. 决策底线 / 避坑(沿用平台一贯选择)

| 主题       | 反模式(来源)                                | 我们应保持的选择                                                                                                                       |
| ---------- | ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| 质量度量   | 只靠 reopen/reject 率当质量(我们现状的盲区) | reopen 率保留为"相对审核信号"一档,但补 **GT honeypot(绝对)+ consensus(分歧)** 两支柱,三档分开不混算                                    |
| 一致性算法 | 写死单一算法                                | 学 LS `GET_TASKS_AGREEMENT_QUERYSET` 做**可插拔**:按任务类型(分类一致率/检测 IoU/...)分派,不写死                                       |
| 工时口径   | 不剔空闲直接 sum(误高)                      | 沿用 CVAT/LS 的空闲剔除阈值(明确我们 active_minutes 口径)                                                                              |
| 质量阈值   | 硬编码                                      | 学 CVAT `target_metric` + `target_metric_threshold` 做项目级可配                                                                       |
| 统计单位   | 多人标同题只算一次                          | 学 Kili"统计 actions 而非 assets",多人各计                                                                                             |
| 范围隔离   | super_admin 全局糊一把                      | project_admin **每个聚合都加 project 过滤**,不靠最后过滤用户列表(当前 `/admin/people` 的 `project` 参数正是只过滤用户、不切聚合的反例) |
| 报表格式   | 自造格式                                    | CSV 优先 + 列选择器(对齐 Encord),不自造                                                                                                |

---

## 7. 一句话路线

**先快赢稳住体验(reject 细分 / 类别覆盖 / 导出 / 下钻 / analytics 升级 / 热力图),同窗口补 project_admin 范围;并行启动 Honeypot/GT + Consensus/IAA 两个结构性 epic(各需 ADR + 数据建模),它们才是把我们从"效率看板"升级到"质量平台"的关键。** 详见配套版本计划。
