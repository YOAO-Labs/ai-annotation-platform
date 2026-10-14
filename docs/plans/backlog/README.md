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

## Other unscheduled drafts

| Draft                                                                                                | Retained context and promotion requirement                                                                              |
| ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| [Semantic/panoptic mask workflows](2026-07-21-semantic-panoptic-mask-workflows.md)                   | Deferred raster-mask Epic scope; reassess concrete project demand, current instance-mask contracts and ADR-0055         |
| [New capability marketing masters](1789230200_new-capability-marketing-masters.md)                   | Twelve unregistered stories; native Mac recording integration is completed, but capture/publication needs its own scope |
| [AI documentation layering and governance](2026-07-11-ai-documentation-layering-governance-draft.md) | Initial layering is implemented; reassess only the remaining governance decisions against current documentation tooling |
| [Internal identity governance](1788952434_internal-identity-governance.md)                           | Unscheduled enterprise identity and advanced policy directions; recheck the implemented project-scoped role model       |

## 当前 3D 工作台草案集

### 可在精修计划后优先转定稿

[持续创建与持续自动拟合](../archive/2026-08-24-v0.24.3-3d-continuous-creation.md) 已转为 v0.24.3 实施计划，不再属于 backlog。

[跨帧任务中心](../archive/2026-08-25-v0.24.8-3d-cross-frame-job-center-mvp.md) 已拆出 registration 并转为实施计划，不再属于 backlog；registration 仍需单独通过准确率门。

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
