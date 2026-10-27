# Research drafts and promotion conventions

`docs/plans/backlog/` holds drafts with a recommended direction that are not yet scheduled and do not authorize implementation. They preserve research findings, scope boundaries, dependency order, and acceptance direction. They do not describe the current implementation or reserve any version number.

## Status semantics

- `research-draft`: the recommended direction is clear, but it must be reassessed against the repository on the day implementation starts.
- `trigger-gated research-draft`: in addition to reassessment, the draft's data, performance, or business triggers must be satisfied first.
- Only root-level plans marked `draft / pending approval / approved` may enter implementation review. Backlog files cannot authorize work. Maintainers determine release milestones and version assignments.

Drafts must not hide decisions behind `TODO`, `TBD`, or blank placeholders. Decisions that cannot yet be safely finalized must become verifiable triggers or promotion checklist items.

## Promotion gate

Before implementing any draft, complete the following steps in order. If any step reveals changed assumptions, use the current code, schema, ADRs, and official documentation as the source of truth and rewrite the draft accordingly.

1. Check the current `CHANGELOG.md`, active Epics, and implemented plans to reassess scope and dependencies. Record only maintainer-confirmed release milestones; do not assign versions or infer them from draft dates or timestamps.
2. Check `git status`, distinguish uncommitted user changes from the plan's scope, and record worktree constraints that must be preserved.
3. Use `rg` to relocate the draft's code anchors. Check whether APIs, database models, migration heads, frontend state machines, test fixtures, and official documentation have evolved.
4. Assess the draft's triggers, scope, and exclusions, recording whether each still holds, is already covered by the current system, needs splitting, or no longer holds.
5. Replace the candidate change areas with an exact file list, dependency graph, migration and compatibility strategies, test commands by layer, manual acceptance matrix, and documentation and `CHANGELOG.md` updates.
6. Specify acceptance gates and rollback methods for each mergeable slice. If the plan touches more than 8 files, explicitly acknowledge the large change scope in the finalized plan; if it touches more than 3 components, retain an ASCII data-flow diagram.
7. Move the file to the `docs/plans/` root, following the [naming conventions](../README.md#naming-conventions). Keep existing `<unix-seconds>_<topic>.md` filenames. For legacy drafts with date or version prefixes, use the 10-digit Unix timestamp in seconds at promotion, remove version numbers, and update all Markdown references. Set the status to pending approval or approved; implementation still requires authorization.

Promotion requires a fresh decision based on the actual repository, not a few notes appended to the original text. Research sources and rejected directions may remain, but the file scope, contracts, and test criteria must be regenerated.

## 数据集与存储升级草案集

[总览与推荐顺序](1792661016_dataset-storage-roadmap.md)将整套外部材料转为本项目 17 份研究草案：5 份总纲与 12 个 Epic，保留需求、故事和验收编号，补入代码落点、复用边界、独立切片、晋升条件及回退方法。E01-A、E02-A、E04-A、E05-A 与完整 E03 已完成本地验收，E03 已归档。完整 E04 已完成并归档，涵盖手动/周期增量、内容新修订、同内容重新核验、移动重定位、规则变更与历史导航。E10-A 已完成项目级引用训练冻结及实际浏览器验收，E08-A/E09-A 已完成独立数据集工作台、唯一存储配置、双向深链及实际浏览器验收；E08-B1 已完成服务端组合筛选、个人动态视图及实际浏览器验收（实现 `5bb8346cd`）；E08-B2 已完成（`233577c94`）固定成员质量检测、可信精确重复和人工复核；E08-B3 已完成固定范围批量复核、影响预览、逐项冲突与取消恢复（实现 `944ebbf08`）；E12-A1/E09-B1 已完成（`0e85ce496`）平台九用途异步盘点、成功快照与过期/失败显示，真实盘点与取消保留已验收；E12-A2/E09-B2 已完成位置内原地引用原件/预览/扫描/预检诊断，包含历史身份、旧轮恢复和上下文导航；59 个不同本地用例及真实明暗/窄屏验收通过。E08-B4 已完成 Scene 组合筛选、轻量选项搜索/分页与个人视图恢复，74个不同本地用例和独立真实明暗/窄屏验收通过；E08-B5/E10-B1 已完成筛选视图固定训练成员，181个不同用例与真实ready/ZIP不变性验收通过；E02-B1/B2 已完成连接并发保护与候选验证；E05-C1 已完成所有异步图片格式的引用原图打包、最终授权与取消，160个不同用例和真实 worker/浏览器验收通过；E05-C2-A 已完成共用输入回收基座并接入现有多阶段 ROI，221 个不同用例及真实 MinIO/Celery 恢复验收通过；C2-B 结果事务前置完成 177 个不同用例和真实 Celery/MinIO/HTTP 三作业验证，后续已接通引用预标、两个失败重试入口、普通几何与同步原生采纳、引用图片批量修复及候选审阅；修复资格累计 303 个不同 API 用例与真实 HTTP/MinIO 验证通过。后台原生掩码、完整原图缓存与E10-B2-A引用血缘现已交付，E12-B1完成离线隔离恢复；E11已完成位置影响、归档／恢复、准确版本独立回收和周期引用核对，按原始矩阵完整归档。当前完成证据和下一行动以[整套实施队列](../1792752668_dataset-storage-epic-execution.md)及各Epic Outcome为准。其余范围按实际缺口或触发条件独立评审，不分配发布版本。

共同约束：[架构与决策](1792661016_dataset-storage-architecture.md) · [信息架构](1792661016_dataset-storage-information-architecture.md) · [交付与 33 项跨 Epic 验收](1792661016_dataset-storage-delivery-acceptance.md) · [基线、来源映射与参考](1792661016_dataset-storage-baseline.md)。

| 草案                                                                                               | 独立结果                              | 依赖或触发条件                         |
| -------------------------------------------------------------------------------------------------- | ------------------------------------- | -------------------------------------- |
| [E01 领域与存储抽象](../archive/1792661016_dataset-storage-domain-abstraction.md)                  | 稳定样本/内容/位置、兼容历史真实键    | 完整验收归档；保留回退限制             |
| [E02 位置、连接与用途（已完成）](../archive/1792661016_dataset-storage-locations-connections.md)   | 只读根、节点能力、九用途映射          | E01；授权矩阵与真实挂载                |
| [E03 已有目录原地注册（已完成）](../archive/1792661016_dataset-in-place-directory-registration.md) | 不先复制原图即可浏览、关联和标注      | E02、E04-A、E05-A；最小权限            |
| [E04 增量同步与任务](../archive/1792661016_dataset-incremental-sync-jobs.md)                       | 完整 E04 已验收归档；保留后续范围边界 | 手动/周期/裁决/导航完成                |
| [E05 文件访问与缓存](../archive/1792661016_dataset-asset-access-cache.md)                          | 同内容读取、分离原图/预览和缓存       | E01/E02；读取/提交授权                 |
| [E06 多站点 Agent](../archive/1792661016_dataset-multi-site-agent.md)                              | 隔离站点出站扫描/读取和断线恢复       | 完整E06验收归档；现场部署条件另行核验  |
| [E07 S3 选型与迁移](../1792661016_dataset-s3-selection-migration.md)                               | 候选契约、全用途迁移及回滚            | 试验独立；切换另过恢复门               |
| [E08 数据集工作台与质量](../archive/1792661016_dataset-workbench-quality.md)                       | 完整 E08 已验收归档；含项目上下文统计 | 共享／近似重复按原计划后置             |
| [E09 存储控制台与去重](../archive/1792661016_dataset-storage-console-boundaries.md)                | 唯一配置、准确指标和上下文深链        | E02；与 E08 迁旧入口                   |
| [E10 版本与训练消费](../1792661016_dataset-versions-training-consumption.md)                       | 目录进入已有冻结体系、血缘及缓存      | 现有任务版本流与 E05；不重建           |
| [E11 权限与生命周期](../archive/1792661016_dataset-permissions-lifecycle.md)                       | 只读/修订保护、影响预览、恢复/回收    | 原始矩阵与准确版本回收已收口，完整归档 |
| [E12 容量、运维与恢复](../archive/1792661016_dataset-storage-operations-recovery.md)               | 异步统计、定位故障、一致恢复          | 本地已完成并归档；生产目标另验         |

实施前逐项通过本页 promotion gate，不能直接从 backlog 开始编码。E07 生产迁移和 E06 生产部署仍需现场条件；E06 已获持续实施授权，按原 AC01 的本地隔离拓扑实施，不等待生产目标。

## 在线学习闭环

| 草案                                                                | 独立结果                                                                         | 阶段依赖                                              |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------- |
| [在线学习闭环多阶段计划](1791971285_online-learning-closed-loop.md) | 首批 A–E 已验收：固定数据、平台训练、Ultralytics 回流、自动策略与 LibreYOLO 下载 | LibreYOLO 回流已由 T13 验收；其余任务按独立资格门复核 |

[图像实例分割在线学习链路](../archive/1792480513_segmentation-online-learning.md) 的 S1–S7 已完成本地实施与验收，现已归档，覆盖任务隔离发布、受控分割回流、原生栅格掩码训练、掩码评估、初始化、自动重训与主动选样。目标 GPU、业务质量和其他任务仍以计划记录的资格边界为准。

### 已归档的训练体验计划集

[总览与依赖图](../archive/1792035920_online-learning-experience-roadmap.md)及 T01–T14 均已逐项完成实施与验收，并移入 archive。下表保留各节点的交付结果、原始依赖与已验收边界，供后续工作追溯。新增任务或推理能力需独立通过本页的 promotion gate。

| 归档计划                                                                               | 独立结果                                                     | 原始依赖 / 已验收边界                   |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------ | --------------------------------------- |
| [T01 完整训练参数目录](../archive/1792035920_training-parameter-catalog.md)            | 按框架、版本、模型家族与任务列全参数，统一校验并冻结有效配置 | 现有训练适配器                          |
| [T02 训练配置工作区](../archive/1792035920_training-configuration-workspace.md)        | 分组、搜索、默认值来源、草稿恢复和 YAML 导入导出             | T01                                     |
| [T03 模板与实验克隆](../archive/1792035920_training-recipes-and-cloning.md)            | 项目模板修订、配置差异及自动策略显式采用                     | T01、T02                                |
| [T04 数据版本洞察](../archive/1792035920_training-data-insights.md)                    | 冻结数据差异、类别分布、质量解释与样本查看                   | 现有数据版本                            |
| [T05 跨实验比较](../archive/1792036036_training-experiment-comparison.md)              | 参数差异、曲线及具有明确评估身份的最终报告                   | 现有实验与已校验产物                    |
| [T06 原生增强预览](../archive/1792036036_training-augmentation-preview.md)             | 展示真实原生增强后的图片、框及变换信息                       | T01、T02、T04；受限 CPU 原生执行验证    |
| [T07 训练遥测与故障指导](../archive/1792036036_training-observability-and-recovery.md) | 实际资源数据、有界日志、错误解释和恢复操作                   | 现有执行器与尝试隔离                    |
| [T08 样本评估与错误分析](../archive/1792036036_training-sample-evaluation.md)          | 固定样本误检漏检分析、候选对照及工作台回链                   | 已完成；固定阈值、原生推理与回链已验收  |
| [T09 模型血缘与候选决策](../archive/1792036036_training-model-lineage.md)              | 查询来源链、发布历史及候选决策记录                           | 已完成；有界来源导航与追加决策已验收    |
| [T10 自训练模型初始化](../archive/1792036132_training-controlled-warm-start.md)        | 从兼容项目模型开始新实验，与断点续训区分                     | 已完成；原生初始化与自身续训已验收      |
| [T11 有预算的参数搜索](../archive/1792036132_training-bounded-parameter-sweeps.md)     | 有界网格实验、预算执行与兼容结果排名                         | 已完成；原生截止、串行结算与取消已验收  |
| [T12 主动学习选样](../archive/1792036132_training-active-learning.md)                  | 可解释的下一批人工标注建议及选样审计                         | 已完成；固定选样、人工接纳与反馈已验收  |
| [T13 LibreYOLO 模型回流](../archive/1792036132_training-libreyolo-model-return.md)     | 原生模型注册、推理、人工发布与回退                           | 已完成；原生 CPU 试用、发布与回滚已验收 |
| [T14 其他任务类型资格验证](../archive/1792036132_training-task-qualification.md)       | 已交付多边形实例训练、完整续训、框与掩码报告及下载           | 已完成；推理回流和其他任务需独立验收    |

各归档计划保留实际代码范围、兼容边界、验收证据与回退方式；未通过独立资格验证的其他任务和能力继续保持关闭。本次归档复用已有验收记录，未重新运行历史应用或原生推理测试。

## 当前 3D 工作台草案集

### 可在精修计划后优先转定稿

[持续创建与持续自动拟合](../archive/2026-08-24-v0.24.3-3d-continuous-creation.md) 已转为 v0.24.3 实施计划，不再属于 backlog。

[跨帧任务中心](../archive/2026-08-25-v0.24.8-3d-cross-frame-job-center-mvp.md) 已拆出 registration 并转为实施计划，不再属于 backlog；registration 仍需单独通过准确率门。

| 草案 | 独立结果 | 关键依赖 |
| ---- | -------- | -------- |

[3D 专属质量流程](../archive/2026-08-26-v0.24.13-3d-quality-workflow.md) 已完成转定稿并进入实施，不再属于 backlog。

[3D 测量 Overlay MVP](../archive/2026-08-27-v0.24.19-3d-measurement-overlay.md) 已从“地面、测量与几何辅助层”拆出并转为实施计划；原草案不再属于 backlog，地面预览与几何特征只有重新满足各自触发门后才能另立草案。

可信 LiDAR 导出已收缩为 [LiDAR 可信导出 MVP](../archive/2026-08-25-v0.24.9-lidar-trusted-export-mvp.md) 并转入实施；完整 nuScenes 与多相机 COCO 派生 2D 不在当前版本范围。

[持久化多模态对象](2026-08-24-persistent-multimodal-object-draft.md) 的相机 bbox 主路径已由
[`v0.24.15`](../archive/2026-08-26-v0.24.15-persistent-multicamera-members.md) 实施；原草案标记为部分替代，
只保留相机 polygon / mask 等未排期方向。

### 由合同或基线触发后再转定稿

| 草案                                                                          | 触发后交付                           | 触发门                                    |
| ----------------------------------------------------------------------------- | ------------------------------------ | ----------------------------------------- |
| [点云空间流式加载](2026-08-24-pointcloud-spatial-streaming-draft.md)          | 视口 LOD、分块解码和稳定全局点 ID    | 三档固定数据集基准证明现链路不达标        |
| [3D AI 候选助手](2026-08-24-3d-ai-candidate-assistant-draft.md)               | 预览、调整、接受、拒绝的统一候选合同 | 本地算法验证后再开放远程模型协议          |
| [Point Cloud Pen](2026-08-24-pointcloud-pen-draft.md)                         | 点选与笔刷共用一个点掩码工具         | 点索引稳定；若启用 tiling 则先完成全局 ID |
| [3D Ground Truth 与 Consensus](2026-08-24-3d-ground-truth-consensus-draft.md) | 可解释的对拍、即时反馈与多人一致性   | 3D 指标合同和质量定位流程稳定             |

## 共同研究依据

- [`v0.24.4 · 3D Scene 时间轴 MVP`](../archive/2026-08-24-v0.24.4-3d-scene-timeline.md) 已转为实施计划，不再属于 backlog。
- [`v0.24.11 · 3D 时序对象与轨迹生命周期基础`](../archive/2026-08-25-v0.24.11-3d-track-domain-foundation.md) 已转为实施计划，不再属于 backlog。

- [`docs/research/22-supervisely-cvat-workbench.md`](../../research/22-supervisely-cvat-workbench.md)：Supervisely / CVAT 工作台、教程、图片、GIF、视频与固定源码快照的深度调研。
- [`docs/research/24-3d-temporal-object-lifecycle.md`](../../research/24-3d-temporal-object-lifecycle.md)：商业平台、开源工具与 OpenLABEL 的 3D 时序对象、存在区间和轨迹修复合同。
- [`docs/research/14-point-cloud-image-fusion.md`](../../research/14-point-cloud-image-fusion.md)：点云与图像联合标注、真值边界与平台差距。
- [`docs/plans/archive/2026-08-14-v0.24.x-3d-workbench-optimization-epic.md`](../archive/2026-08-14-v0.24.x-3d-workbench-optimization-epic.md)：已实施历史、延期方向和版本边界。

具身 Episode Profile、2D 类别行工具、Center Out、Slice、通用 Layers / Filters 和视频双层时间轴不属于这组 3D 工作台草案；它们需要各自的产品域计划，不能借本目录顺带实施。
