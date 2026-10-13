# 0045 — 跨帧对象标识 track_id 提升为 annotation 表列

- **Status:** Accepted
- **Date:** 2026-07-03
- **Deciders:** core team
- **Supersedes:** —

## Context

「跨帧同一对象」历史上用**两套 id**，语义重叠、职责错位：

- `annotation.group_id`（本是 per-task 图像编组，Ctrl+G）被**高位段借用**做跨帧链——
  `propagate` / `interpolate_range` 从全局序列 `cross_frame_group_seq`（`START 1000000000`，
  见 `0097_project_prefer_scene_and_group_seq.py:46`）分配 `group_id >= 1e9` 来串同一对象的多帧。
- `track_id` 只存在于 `video_track_bbox` geometry JSON 内（交互式 VideoTrackerJob 与 v0.21.1 检测式
  追踪 `_remap_track_ids` 产出），**不是 annotation 级字段**。

后果：跨帧标识散在两处、按几何类型分裂——**静态 `box_3d` 跨帧链只有 `group_id`、没有 `track_id`**
（`export_lidar.py:234` 靠 `group_id` 造 `instance_token`），而视频 `video_track_bbox` 只有 geometry 内
`track_id`。同一个概念（「这几帧是同一个物体」）没有单一权威落点，导出/插值/前端各读各的。

| 选项                                                    | 主要卖点                                                                              | 主要劣势                                                           |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **A · track_id 升为 `annotation.track_id` 表列**        | 通用跨帧标识，所有几何类型可用；插值/传播/导出统一读表列；直接解掉 box_3d 无 track_id | 需数据迁移 + 回填；geometry 内 track_id 与表列需协调               |
| B · track_id 继续留 geometry 内、box_3d 也塞进 geometry | 无 schema 变更                                                                        | 每种几何各自维护、易漂移；查询/去重要拆 JSON；未解决「一字段两义」 |

## Decision

**采用方案 A：把 `track_id` 提升为 `annotation.track_id` 表列**（`VARCHAR(64)` nullable，带索引），
作为**与几何类型无关的通用跨帧对象标识**。`group_id` 回归纯图像编组语义（后续 v0.21.3 评估删除）。

- **格式统一**：全局单一工厂 `_new_track_id()`（`annotation_propagation.py:45`）产 `trk_<uuid.hex>`；
  检测式 ingestion（`_remap_track_ids`）、交互式传播、3D 存量回填共用它，表列内不混形态。
  （已在 v0.21.2 Phase 1a 落地。）
- **geometry 内 track_id 迁入表列**：读写以表列为权威；`video_track_bbox` geometry 内的 track_id
  与表列同步（迁移期回填 `track_id = geometry->>'track_id'`）。
- **分步迁移**（本 ADR 对应 v0.21.2 Phase 1–6）：
  1. 加列 + 索引；回填 geometry track_id → 列；为存量跨帧链（`group_id >= 1e9`）按
     `(project_id, group_id)` 每链生成一个 track_id 回填。
  2. `propagate` / `propagate_batch` 改分配 `track_id`，跨帧 `group_id` 归 NULL；停用
     `cross_frame_group_seq` 跨帧写入。
  3. `interpolate_range` 改按 `track_id` 查两端框。
  4. 导出：COCO `__group_id` → track_id attr；LiDAR `instance_token` 改 track_id；MOT/KITTI（已用
     track_id）不动。
  5. 前端 3D 工作台跨帧配对 / 关键帧插值改读 track_id（`perObjectAlign.ts` 等）。
  6. 迁移收尾，废弃 `cross_frame_group_seq`。

## Consequences

正向：

- 跨帧标识**单一权威落点**，几何类型无关；box_3d 跨帧链首次有一等 track_id。
- 插值/传播/导出/前端统一读 `annotation.track_id`，消除「一字段两义」的债。
- 为 v0.21.3 删除 `group_id` 持久化扫清前提（跨帧语义已迁走）。

负向：

- 需数据迁移 + 回填；生产存量跨帧链回填是有状态操作，须复核。
- 迁移期 geometry 内 track_id 与表列并存、需同步，存在短暂双写窗口。
- `annotation.track_id` 列在大表上加索引有一次性成本。

## Notes

- 实现代码位置：`apps/api/app/db/models/annotation.py`（列）、
  `apps/api/app/services/annotation_propagation.py:45`（工厂）、
  `apps/api/app/services/prediction.py`（检测式 ingestion）、
  `apps/api/app/services/annotation.py`（propagate / interpolate_range）、
  `apps/api/app/services/exporting/service.py` · `exporting/lidar.py`（导出）、
  `apps/web/src/pages/Workbench/stages/three-d/geometry/perObjectAlign.ts`（3D 前端）。
- 相关 alembic：`0113_*`（加列 + 回填），后续 drop `cross_frame_group_seq` 在 v0.21.3。
- 相关计划 / ROADMAP：`docs/plans/archive/2026-07-01-v0.21.2-crossframe-object-id-unification.md`、
  `docs/plans/archive/2026-07-01-v0.21.3-remove-annotation-group.md`。
- 上游：v0.21.1 检测式追踪先落 geometry 内 track_id，本 ADR 随即提升表列，避免迁两遍。
