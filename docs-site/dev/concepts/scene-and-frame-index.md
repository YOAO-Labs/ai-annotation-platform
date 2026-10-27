---
title: Scene 与帧索引
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# Scene + frame_index（跨 task 帧序列地基）

> 把“一段录像被切成多个 task”型场景（3D 点云逐帧 / 2D 抽帧图像序列 / 多段 mp4 拼接长录像）统一到同一数据地基。跨帧交互建立在这套 scene 与帧索引之上。

## 背景：为什么要 scene

平台对“跨 task 时序”与“单 task 内时序”采用分层表示：

- **task 内的帧序列**：`VideoFrameIndex` 解决“单段 mp4 一个 task，内部跨帧”。
- **task 之间的帧序列**：通过 `scenes` 表与 `DatasetItem` 上的 `scene_id` + `frame_index` 显式建模。若仅靠 `file_name` 字符串排序推断连续帧，当一个 dataset 包含多个 scene（例如典型 nuScenes 数据集含多个 scene）时，会让 `scene-A` 末帧的“邻居”误连到 `scene-B` 首帧。

## 四种“时序录像”形态共用同一抽象

| 存法                   | 一个 task =   | “下一帧”在哪                          | 覆盖方式                      |
| ---------------------- | ------------- | ------------------------------------- | ----------------------------- |
| A. 整段 mp4 一个文件   | 一段视频      | task 内 `VideoFrameIndex.frame_index` | 单 task 视频工作台            |
| B. 抽帧图像序列        | 一张 jpg      | 跨 task                               | `scene_id + frame_index`      |
| C. 多段 mp4 拼成长录像 | 一段 mp4 片段 | 跨 task（段级）+ task 内（段内）      | 段级 `scene_id + frame_index` |
| D. 3D 点云逐帧         | 一帧 .pcd     | 跨 task                               | `scene_id + frame_index`      |

`scene_id + frame_index` 是面向“时序录像”的抽象，**与文件类型正交**——B / C / D 共用同一套邻居查询与跨帧交互。

## 数据模型

```
┌──────────┐  CASCADE  ┌──────────────┐   SET NULL   ┌──────────────┐
│ datasets │ ─────────→│   scenes     │ ←─────────── │ dataset_items│
└──────────┘           └──────────────┘              │  scene_id    │
                                                     │  frame_index │
                                                     └──────────────┘
                                                            ↑
                                                            │
                                          Task ─── task.dataset_item_id (2D)
                                          Task ─── TaskDatasetItemLink role=primary_lidar (3D)
```

### `scenes` 表

| 列                | 类型                     | 说明                                               |
| ----------------- | ------------------------ | -------------------------------------------------- |
| `id`              | UUID PK                  | `gen_random_uuid()`                                |
| `display_id`      | str unique               | `"SCN-N"`，走 `display_seq_scenes`                 |
| `dataset_id`      | UUID FK datasets CASCADE | 一个 scene 隶属一个 dataset                        |
| `name`            | str                      | 业务名（如 `"scene-0061"`）                        |
| `source_format`   | str?                     | `"sustechpoints"` / `"nuscenes"` / `"inferred"` 等 |
| `source_metadata` | JSONB                    | 自由格式，由 importer 自填                         |
| `created_by`      | UUID? FK users           | 创建者                                             |
| 唯一性            | `(dataset_id, name)`     | 允许跨 dataset 同名                                |

### `dataset_items` 时序字段

| 列            | 类型                     | 说明                                                                             |
| ------------- | ------------------------ | -------------------------------------------------------------------------------- |
| `scene_id`    | UUID? FK scenes SET NULL | 历史数据兼容 NULL                                                                |
| `frame_index` | int?                     | scene 内的有序位置；同 scene 同 `frame_index` **允许重复**（lidar + 多相机共享） |

**索引**：`idx_dataset_items_scene_frame` 建立在 `(scene_id, frame_index)` 上，服务于 neighbors 查询。**不加 UNIQUE**，因为多模态同帧的多路相机与 `frame_index=NULL` 的标定文件需要共存。

### 项目与数据集的 scene 声明

`Project.scene_mode` 是项目级声明，表示该项目按跨 task scene 标注来组织数据。它是项目创建时的显式选择，而非从数据集倒推的临时开关。

`Dataset.is_temporal` 是导入意图声明，只用于导入期早失败：如果数据集声明为时序数据集，但导入结束后没有产生任何 scene，导入会报错。项目关联不看 `is_temporal`，而是实时派生 `has_scenes`。

`has_scenes` 不落库，始终由 `EXISTS(scenes where scenes.dataset_id = datasets.id)` 派生。`GET /datasets?has_scenes=true|false` 使用同一判定过滤列表。

### 设计选择

- **为什么单独建表而不只在 `dataset_items` 加 `scene_id` 字符串**？scene 有自身元数据、唯一性约束与跨 item 一致性，单纯的字符串列无法承载。
- **为什么不复用既有 `metadata_` JSONB**？JSONB GIN 索引效率低于 BTree 外键、缺乏跨 item 约束，且跨 dataset 的 scene 列表会退化为 JSON 全表扫描。
- **为什么允许 nullable**？向后兼容历史非时序 dataset，历史数据迁移走显式 backfill 脚本。

## API

### `GET /api/v1/tasks/{task_id}/neighbors?k=1`

返回 task 在所属 scene 内前后 `k` 个邻居 task（`k ∈ [1, 20]`，超出返回 `400`）。

```json
{
  "scene_id": "uuid",
  "scene_name": "scene-0061",
  "frame_index": 42,
  "scene_total_frames": 80,
  "prev": [{ "task_id": "uuid", "frame_index": 41 }],
  "next": [{ "task_id": "uuid", "frame_index": 43 }]
}
```

- `prev` / `next` 均按“距离当前帧远近”排序——`cur-1` 在 `prev[0]`，`cur+1` 在 `next[0]`。
- 首帧或末帧对应方向数组为空，不报错。
- task 无 `scene_id`（非时序或未 backfill）返回 `200` + 空数组（与首末帧行为一致）。
- `scene_id` 非空但 `frame_index` 为 `NULL`（异常状态）返回 `409`。

### `GET /api/v1/scenes?dataset_id=<uuid>` / `GET /api/v1/scenes/{id}` / `PATCH /api/v1/scenes/{id}`

scene API 提供列表/单条读取及部分元数据 PATCH；创建由 importer 或 backfill 自动发起。PATCH 只接受 `name`、`source_format`、`source_metadata`；字段省略或 `null` 表示不变，`source_metadata: {}` 表示清空。同 dataset 名称冲突返回 `409` 且不应用其他字段，目标不存在返回 `404`。Scene service 负责加载、冲突检测、字段更新和 flush；API route 提交事务、refresh 并构造响应，因此成功返回后的新会话 GET 可见已保存值。

### `POST /api/v1/datasets/{id}/scenes/backfill?mode=auto&dry_run=false`

对 dataset 执行 `scene_inference`（`mode ∈ {single, per_subdirectory, auto}`，默认 `auto`）。

### `GET /api/v1/datasets?has_scenes=true|false`

按实时派生的 scene 存在性过滤数据集列表，可与 `data_type` 组合使用。点云数据集的存储类型是 `point_cloud`，项目媒体类型是 `lidar`；项目与数据集匹配时会归一到同一媒体 kind。

### `POST /api/v1/datasets/{id}/link`

关联项目时执行对称校验：

- 项目媒体类型必须匹配数据集媒体类型。
- `project.scene_mode` 必须等于数据集实时派生的 `has_scenes`。

不匹配返回 `422`。该规则只拦截新关联，不追溯修改旧关联。

### Manifest 透出

`GET /api/v1/tasks/{id}/point-cloud/manifest` 透出 `scene_id`、`scene_name`、`frame_index`、`scene_total_frames` 与本帧 `ego_pose`（全 `None` 表示非时序或未回填），供 3D 工作台 `SceneTimeline`、帧间导航与位姿对齐使用。

该清单由 `application/point_cloud_manifest.py` 组合 Labeling 链接引用与 Assets 的公开素材、标定、Scene 和位姿事实。HTTP 先核验当前任务可见性和项目类型，读取端口只接收 UUID；消费者不读取 Assets 私有 ORM。需要与标注写入串行化的标定事实通过同事务原生快照锁确认。缺失 Scene 或位姿继续返回空值，自由来源 JSON 以脱离持久化对象的值交付。

## `scene_inference` 算法

`aap_assets_catalog.spatial_workflow.SpatialWorkflow.backfill_scenes` 接收公开 `BackfillScenesRequest`，复用 Source 的纯 Scene planner。API 通过 `services/assets_spatial.py` 选择独立运行时或同一原生合同；需要整体回滚的导入显式使用调用方 SQL 连接。

### 三种模式

- **`single`**：整个 dataset 视为 1 个 scene，`scene.name` 取 `dataset.name`。
- **`per_subdirectory`**：按 `file_path` 中 `dataset_name` 之后的第一段目录分组，每组生成一个 scene。
- **`auto`**（默认）：若顶层目录全是已知角色名（`{lidar, camera, calib, image, video, images, videos}`），走 `single`；否则走 `per_subdirectory`。

### `frame_index` 赋值（点云 / 多模态）

1. 将素材转换为不可变 `PreparedDatasetItem`，由 `aap_assets_source.frame_layout.group_frames(items)` 取得 `{frame_stem: GroupedFrame}` 与相机标定事实；`aap_assets_source.scene_layout.plan_scenes` 纯函数负责场景和帧索引规划，不依赖 API、ORM 或 SQLAlchemy。
2. `frame_stem` 自然排序（`"000001" < "000010"`）后，lidar 依次取 `0..N-1`。
3. 同帧相机 item 共享 lidar 的 `frame_index`。
4. `calib_items` 仅写 `scene_id`，`frame_index=NULL`。

### `frame_index` 赋值（非点云）

按 `file_name` 自然排序，所有 items 依次取 `0..N-1`。

ZIP 使用 `metadata.source_ingest.relpath` 作为逻辑路径，因此不可变 `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` publication 不会把 UUID 段误认成 Scene 名。ZIP 仅在没有任何 Scene 且没有既有 Scene 分配时，对全部历史与新增 Item 一起规划。旧前缀桶扫描只登记 Item，不运行 Scene 推断。

### 边界保护

- dataset 已有 scene → 跳过（幂等）。
- 部分 items 已有 `scene_id` → 跳过并在 notes 报告 `"partial migration"`。
- 推断出超过 100 个 scene → 抛出 `ValueError`（防止把平铺大图像序列误判为大量伪 scene）。

### 常见 ZIP 布局命中表

| ZIP 顶层                                    | `mode=auto` 推断                                | `scene.name`         |
| ------------------------------------------- | ----------------------------------------------- | -------------------- |
| `lidar/ camera/ calib/`（SUSTech 单 scene） | `single`                                        | `dataset.name`       |
| `scene_a/lidar/ scene_b/lidar/ ...`         | `per_subdirectory`（2 个 scene）                | `scene_a`, `scene_b` |
| `nu-scene-0061/lidar/ nu-scene-0103/...`    | `per_subdirectory`（N 个 scene）                | `nu-scene-0061`, ... |
| `lidar/ camera/ scene_extra/`（混搭）       | `per_subdirectory`（`_single` + `scene_extra`） | 见 notes 警告        |

## 导入入口

1. **`pointcloud_import.prepare_pointcloud_frames`**：应用建题工作流在标定提交后调用；缺少场景时先跑 `single` 模式推断，再按 scene/stem 准备帧事实，交给标注所有者建题。
2. **`POST /datasets/{id}/items/upload-zip`**：上传完成后运行 `auto` 模式推断，并将 `scene_inference_notes` 透传回响应。
3. **`nuScenes` 转换脚本**：直接走显式 `scene_svc.create_scene` + `assign_items_to_scene` 路径，不依赖目录启发式；建题统一调用应用工作流。

## Backfill 历史数据

```bash
cd apps/api

# 单 dataset
PYTHONPATH=. uv run python scripts/backfill_scenes.py --dataset-id <uuid>

# 全部缺 scene 的数据集
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing

# 预览
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing --dry-run
```

脚本不在容器启动时自动执行，由管理员审阅后运行。对文件名编码 scene 信息的平铺目录（如 `<ds>/scene_a_000001.pcd`），需显式指定模式或人工调整。

## 逐帧 Ego Pose 与时间戳（`scene_frame_poses`）

`scenes` 给出了帧的相对顺序，`scene_frame_poses` 表则补充每帧车体在世界系的位姿与时间戳（对应 nuScenes `sample_data.ego_pose` / `timestamp`），支撑运动补偿传播（`propagate`）、区间插值（`interpolate-range`）与邻帧参考框世界系对齐。

### `scene_frame_poses` 表结构

| 列                | 类型                      | 说明                                                                 |
| ----------------- | ------------------------- | -------------------------------------------------------------------- |
| `id`              | UUID PK                   | 内部实体，无 `display_id`                                            |
| `scene_id`        | UUID FK scenes CASCADE    | 随 scene 级联删除                                                    |
| `frame_index`     | int                       | 与 `dataset_items.frame_index` 同语义                                |
| `timestamp_us`    | bigint?                   | 主帧时钟（nuScenes 取 `LIDAR_TOP` 的 `sample_data.timestamp`，微秒） |
| `ego_translation` | JSONB `[x,y,z]`           | `ego → global`（世界系）平移                                         |
| `ego_rotation`    | JSONB `[w,x,y,z]`         | `ego → global` 四元数                                                |
| `source_metadata` | JSONB                     | 自由格式（如 `ego_pose_token`）                                      |
| 唯一性            | `(scene_id, frame_index)` | 一帧一行，兼做轨迹查询索引                                           |

设计要点：

- **粒度 `(scene_id, frame_index)`**：与 neighbors 查询对齐，不塞入 `dataset_items.metadata_`。
- **保存原始 `ego → global`**：跨帧相对位移 `inv(pose_i) @ pose_j` 由消费方实时计算。
- **缺省降级友好**：历史 scene 或非 nuScenes 来源无位姿行时，消费方自动降级为纯本车系（ego）叠加或原样复制，不报错。
- **世界系仅在同一 scene 内可比**：不跨 scene 比较绝对世界坐标。

### API 与数据来源

- **`GET /api/v1/scenes/{id}/trajectory`**：按 `frame_index` 升序返回 `{scene_id, poses: [...]}`；无位姿时返回 `200` + `poses: []`。
- **数据写入**：`import_nuscenes_scene.py` 落库后调用 `AssetsSpatialGateway.upsert_poses`（按唯一键幂等 upsert）；存量数据集可通过 `scripts/backfill_frame_poses.py` 补齐。

### Ego 运动补偿、区间插值与前端叠加

几何核心位于 `apps/api/app/services/ego_transform.py`（Euler XYZ 约定与 `axis_convention.py` 及前端 Three.js 保持一致）：

- **单条 / 批量运动补偿传播**：`propagate` 与 `propagate-batch` 在源帧与目标帧均有 ego pose 时，通过 `world = ego_to_world(psr_src, pose_src) → psr_dst = world_to_ego(world, pose_dst)` 让静止目标在下一帧自动套准；任一帧缺位姿则退化为原样复制。响应返回 `motion_compensated` 标记。
- **跨帧区间插值（`POST /tasks/{id}/annotations/interpolate-range`）**：在同 `track_id` 链两端关键帧之间，经世界系线性插值中心 + `slerp` 插值朝向 + 线性插值尺寸，生成 `source="interpolated"`、`temporal_role="derived"` 的中间帧标注。
- **前端邻帧参考框对齐**：`useSceneTrajectory(sceneId)` 拉取位姿序列，`stages/three-d/geometry/egoAlign.ts` 使用 `inv(T_cur) @ T_nbr` 将邻帧参考框变换到当前帧本车系后交由 `PointCloudScene.setReferenceBoxes` 渲染。

## 跨帧交互如何消费 Neighbors API

1. **`useFrameNeighbors(taskId, k)`**（`apps/web/src/hooks/`）：封装 `GET /tasks/{id}/neighbors?k=K`，3D 与 2D 共用。
2. **跨帧延续（`propagate`）**：复制 geometry、类别与属性，绑定同一 `track_id` 与 3D `SceneTrack`（详见 [Scene Track 与轨迹生命周期](./scene-track-lifecycle)）。
3. **邻帧参考框叠加**：`useNeighborAnnotations(taskIds, trackId)` 使用 `useQueries` 批量拉取前后 `K` 帧标注，按 `track_id` 过滤后渲染只读参考框。
4. **快捷键分流**：3D 工作台使用 `Shift+→/←`（`ThreeDWorkbench` 本地监听），2D 工作台使用 `Alt+→/←`（全局快捷键），共用 `useWorkbenchShellModel.crossFramePropagate` 与 `resolveCrossFrameTarget`。

## 调度器 Scene 连续派题（`prefer_same_scene_continuation`）

`Project.prefer_same_scene_continuation`（普通项目默认 `false`，scene 模式项目创建时默认开启）打开后，`get_next_task` 在常规采样策略前增加连续帧优先逻辑：查找当前用户在 `scene_continuation_window_min`（默认 30 分钟）内最近创建的有效标注所属 `scene_id + frame_index`，优先派发同一 scene 内 `frame_index` 更大且按帧升序首个可分配的 task；若无匹配则回退至常规采样策略。

## Scene 感知分包（`strategy="by_scene"`）

`BatchService.split(strategy="by_scene")` 按 task 的主 `dataset_item`（2D 走 `Task.dataset_item_id`，3D 走 `TaskDatasetItemLink(role="primary_lidar")`）反查 `scene_id + frame_index`，按 scene 分组建立 `draft` 批次，并将批次内 task 的 `sequence_order` 写为 `frame_index`。`resolve_task_scene_frames` 按固定 5000 条分块查询，避免巨型数据集触发 `asyncpg` 参数上限。

## 边界与相关文档

- `point_mask_3d` 不支持跨帧直接复制（点云索引跨帧不具备对应语义）。
- [Scene Track 与轨迹生命周期](./scene-track-lifecycle)
- [标注模块 · 跨帧 propagate 与插值](./annotation-module#跨帧-propagate-与插值3d-时序)
- [任务模块](./task-module)
- [调度与任务派发](./scheduler-and-task-dispatch)
