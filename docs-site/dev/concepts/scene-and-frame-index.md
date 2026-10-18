---
title: Scene 与帧索引
audience: [developer]
type: explanation
status: stable
last_reviewed: 2026-10-18
---

# Scene + frame_index（跨 task 帧序列地基）

> 把“一段录像被切成多个 task”型场景（3D 点云逐帧 / 2D 抽帧图像序列 / 多段 mp4 拼接长录像）统一到同一数据地基。跨帧交互建立在这套 scene 与帧索引之上。

## 背景:为什么要 scene

平台之前对"跨 task 时序"无表示:

- **task 内的帧序列已有**:`VideoFrameIndex` 解决"单段 mp4 一个 task,内部跨帧"。
- **task 之间缺失**:`DatasetItem` 没有 `scene_id` / `frame_index` 列;"哪些 task 是同一段录像的连续帧"靠 `file_name` 字符串排序的弱约定推断,跨多 scene 直接破。

结果:跨帧 propagate / track / 邻帧框叠加三类 UX 没有合法实现路径;一个 dataset 跨多 scene(典型 nuScenes 一 dataset 含 10 个 scene)时,`file_name` 排序会让 scene-A 末帧"邻居"是 scene-B 首帧,语义错。

## 四种"时序录像"形态共用同一抽象

| 存法                   | 一个 task =   | "下一帧"在哪                          | v0.14.0 覆盖   |
| ---------------------- | ------------- | ------------------------------------- | -------------- |
| A. 整段 mp4 一个文件   | 一段视频      | task 内 `VideoFrameIndex.frame_index` | 保留现状,不动  |
| B. 抽帧图像序列        | 一张 jpg      | 跨 task                               | ✅             |
| C. 多段 mp4 拼成长录像 | 一段 mp4 片段 | 跨 task(段级)+ task 内(段内)          | ✅ 段级跨 task |
| D. 3D 点云逐帧         | 一帧 .pcd     | 跨 task                               | ✅             |

`scene_id + frame_index` 是面向"时序录像"的抽象,**与文件类型正交**——一立起来 B/C/D 共用,前端跨帧 UX 写一份就够。

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

| 列                | 类型                     | 说明                                                    |
| ----------------- | ------------------------ | ------------------------------------------------------- |
| `id`              | UUID PK                  | gen_random_uuid()                                       |
| `display_id`      | str unique               | "SCN-N",走 `display_seq_scenes`                         |
| `dataset_id`      | UUID FK datasets CASCADE | 一 scene 隶属一 dataset(本期不跨 dataset)               |
| `name`            | str                      | 业务名(如 "scene-0061")                                 |
| `source_format`   | str?                     | "sustechpoints" / "nuscenes" / "inferred" / "manual" 等 |
| `source_metadata` | JSONB                    | 自由格式;importer 自填                                  |
| `created_by`      | UUID? FK users           | 创建者                                                  |
| 唯一性            | `(dataset_id, name)`     | 允许跨 dataset 同名                                     |

### `dataset_items` 新增两列

| 列            | 类型                     | 说明                                                                         |
| ------------- | ------------------------ | ---------------------------------------------------------------------------- |
| `scene_id`    | UUID? FK scenes SET NULL | 历史数据兼容 NULL                                                            |
| `frame_index` | int?                     | scene 内的有序位置;同 scene 同 frame_index **允许重复**(lidar + 多 cam 共享) |

**索引**:`idx_dataset_items_scene_frame` on `(scene_id, frame_index)`,给 neighbors 查询。**不**加 UNIQUE——多模态同帧 3 行 + calib NULL 多行需共存。

### 项目与数据集的 scene 声明

`Project.scene_mode` 是项目级声明，表示这个项目要按跨 task scene 标注来组织数据。它不是从数据集倒推出来的临时开关，而是项目创建时的显式选择。

`Dataset.is_temporal` 是导入意图声明，只用于导入期早失败：如果数据集声明为时序数据集，但导入结束后没有产生任何 scene，导入会报错。项目关联不看 `is_temporal`，而是实时派生 `has_scenes`。

`has_scenes` 不落库，始终由 `EXISTS(scenes where scenes.dataset_id = datasets.id)` 派生。`GET /datasets?has_scenes=true|false` 使用同一判定过滤列表。

### 设计选择

- **为什么单独建表而不只在 `dataset_items` 加 `scene_id` 字符串**?scene 有自身元数据 + 唯一性约束 + 跨 item 一致性;字符串列做不到。
- **为什么不复用既有 `metadata_` JSONB**?(a) 索引差(JSONB GIN < BTree FK)、(b) 缺乏跨 item 约束、(c) 跨 dataset 的 scene 列表会变成 JSON 扫表。
- **为什么 nullable**?向后兼容。历史 dataset 不动,backfill 走显式脚本。

## API

### `GET /api/v1/tasks/{task_id}/neighbors?k=1`

返回 task 在所属 scene 内前后 k 个邻居 task。`k ∈ [1, 20]`(超出 400)。

```json
{
  "scene_id": "uuid",
  "scene_name": "scene-0061",
  "frame_index": 42,
  "scene_total_frames": 80,
  "prev": [{"task_id": "uuid", "frame_index": 41}, ...],
  "next": [{"task_id": "uuid", "frame_index": 43}, ...]
}
```

- prev / next 都按"距离 cur 远近"排序——cur-1 在 `prev[0]`,cur+1 在 `next[0]`。
- 首/末帧 → 对应方向数组为空(不报错)。
- task 无 scene_id(历史未 backfill)→ 200 + 全空响应(与首末帧一致,前端不区分)。
- scene_id 非空但 frame_index NULL(异常状态)→ 409。

### `GET /api/v1/scenes?dataset_id=<uuid>` / `GET /api/v1/scenes/{id}` / `PATCH /api/v1/scenes/{id}`

scene API 提供列表/单条读取及部分元数据 PATCH；create 由 importer / backfill 自动发起,API 不暴露。PATCH 只接受 `name`、`source_format`、`source_metadata`;字段省略或 `null` 表示不变,`source_metadata: {}` 表示清空。同 dataset 名称冲突返回 `409` 且不应用其他字段,目标不存在返回 `404`。Scene service 负责加载、冲突检测、字段更新和 flush；API route 提交事务、refresh 并构造响应,因此成功返回后的新会话 GET 可见已保存值。

### `POST /api/v1/datasets/{id}/scenes/backfill?mode=auto&dry_run=false`

对 dataset 跑 `scene_inference`。`mode ∈ {single, per_subdirectory, auto}`,默认 `auto`。

### `GET /api/v1/datasets?has_scenes=true|false`

按实时派生的 scene 存在性过滤数据集列表。可与 `data_type` 一起使用。点云数据集的存储类型仍是 `point_cloud`，项目媒体类型是 `lidar`；项目-数据集匹配时会归一到同一媒体 kind。

### `POST /api/v1/datasets/{id}/link`

关联项目时执行对称 kind 校验：

- 项目媒体类型必须匹配数据集媒体类型。
- `project.scene_mode` 必须等于数据集实时派生的 `has_scenes`。

不匹配返回 422。该规则只拦新的关联，不追溯修改旧关联。

### Manifest 透出

`GET /api/v1/tasks/{id}/point-cloud/manifest` 返回 `scene_id` / `scene_name` / `frame_index` / `scene_total_frames` 四字段；未回填 Scene 时均为空。

该清单由 `application/point_cloud_manifest.py` 组合 Labeling 链接引用与 Assets 的素材/标定/Scene/位姿事实。HTTP 先核验当前任务可见性和项目类型，读取端口只接收 UUID，在同一 Session 内保留原读取及签名顺序；完整素材 ORM 留在 Assets SQL 适配器内。缺失 Scene 或位姿继续返回空值，位姿的自由来源 JSON 复制后交付，避免消费者修改 ORM 缓存。

## scene_inference 算法

`services/scene_inference.py` 暴露 `infer_and_apply(db, *, dataset_id, mode, dry_run)`。

### 三种 mode

- **`single`**:整 dataset = 1 scene,`scene.name` 取 `dataset.name`。
- **`per_subdirectory`**:按 `file_path` 中 dataset_name 之后第一段分组,每组一 scene。
- **`auto`**(默认):顶层目录全是已知角色名 → single;否则 per_subdirectory。

`ROLE_DIR_NAMES = {lidar, camera, calib, image, video, images, videos}`。

### frame_index 赋值(点云 / 多模态)

1. 将素材转换为不可变 `PreparedDatasetItem`，由 `aap_assets_source.frame_layout.group_frames(items)` 取得 `{frame_stem: GroupedFrame}` 与相机标定事实；`aap_assets_source.scene_layout.plan_scenes` 纯函数负责场景和帧索引规划，不依赖 API、ORM 或 SQLAlchemy。
2. frame_stem 自然排序("000001" < "000010")后 lidar 取 0..N-1。
3. 同帧 cam item 共享 lidar 的 frame_index。
4. calib_items 仅写 scene_id,frame_index=NULL。

### frame_index 赋值(非点云)

`file_name` 自然排序,所有 items 取 0..N-1。

ZIP 使用 `metadata.source_ingest.relpath` 作为逻辑路径，因此不可变 `dataset-sources/{dataset}/{item}/{publication}/{logical_relpath}` publication 不会把 UUID 段误认成 Scene 名。ZIP 仅在没有任何 Scene 且没有既有 Scene 分配时，对全部历史与新增 Item 一起规划。旧前缀桶扫描只登记 Item，不运行 Scene 推断。

### 边界

- dataset 已有 scene → 跳过(幂等)。
- 部分 items 已有 scene_id → 跳过,notes 报"partial migration"。
- 推断出 > 100 scene → ValueError(防误识别巨型 jpg 序列为伪 scene)。

### 几种 ZIP 布局命中

| ZIP 顶层                                  | mode=auto 推断                              | scene.name           |
| ----------------------------------------- | ------------------------------------------- | -------------------- |
| `lidar/ camera/ calib/`(SUSTech 单 scene) | single                                      | dataset.name         |
| `scene_a/lidar/ scene_b/lidar/ ...`       | per_subdirectory(2 scene)                   | `scene_a`, `scene_b` |
| `nu-scene-0061/lidar/ nu-scene-0103/...`  | per_subdirectory(N scene)                   | `nu-scene-0061`, ... |
| `lidar/ camera/ scene_extra/`(混搭)       | per_subdirectory(`_single` + `scene_extra`) | 见 notes 警告        |

## 导入端口

两处自动挂上:

1. **`pointcloud_import.prepare_pointcloud_frames`**：应用建题工作流在标定提交后调用；缺少场景时先跑 `single`-mode inference，再按 scene/stem 准备帧事实，交给标注所有者建题。
2. **`POST /datasets/{id}/items/upload-zip`**:上传完跑 `auto`-mode inference;notes 透回响应字段 `scene_inference_notes`。

`nuScenes` 转换脚本走显式 `scene_svc.create_scene` + `assign_items_to_scene` 路径，不依赖启发式；建题统一调用应用工作流。

## Backfill 历史数据

```bash
cd apps/api

# 单 dataset
PYTHONPATH=. uv run python scripts/backfill_scenes.py --dataset-id <uuid>

# 全部缺 scene
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing

# 预览
PYTHONPATH=. uv run python scripts/backfill_scenes.py --all-missing --dry-run
```

**不**在 docker 启动时自动跑——管理员人工 review 后执行。脚本默认 `mode=auto`;对文件名编码 scene 信息(如 `<ds>/scene_a_000001.pcd` 平铺)会误判为单 scene,需显式 `--mode=per_subdirectory` 或人工 PATCH。

## 逐帧 ego pose / 时间戳(v0.15.0)

scene 只给了"帧的相对顺序";`scene_frame_poses` 表补上"帧的时空"——每帧车体在世界系的位姿 + 时间戳,是 nuScenes `sample_data.ego_pose` / `timestamp` 的平台等价物,也是跨帧自动化(运动补偿 propagate / 插值 / Kalman)的硬前置。

### `scene_frame_poses` 表(迁移 0102)

| 列                | 类型                      | 说明                                                            |
| ----------------- | ------------------------- | --------------------------------------------------------------- |
| `id`              | UUID PK                   | 非用户实体,无 display_id                                        |
| `scene_id`        | UUID FK scenes CASCADE    | 随 scene 级联删除                                               |
| `frame_index`     | int                       | 与 `dataset_items.frame_index` 同语义                           |
| `timestamp_us`    | bigint?                   | 主帧时钟(nuScenes 取 LIDAR_TOP 的 `sample_data.timestamp`,微秒) |
| `ego_translation` | JSONB `[x,y,z]`           | ego→global(世界系)平移                                          |
| `ego_rotation`    | JSONB `[w,x,y,z]`         | ego→global 四元数(nuScenes 原样)                                |
| `source_metadata` | JSONB                     | 自由格式(如 `ego_pose_token`)                                   |
| 唯一性            | `(scene_id, frame_index)` | 一帧一行,兼做轨迹查询索引                                       |

设计要点(沿用 v0.14.0「表优于 JSONB」论证):

- **grain = (scene_id, frame_index)**,与 neighbors 查询对齐;不塞 `dataset_items.metadata_`(轨迹查询会变 JSONB 扫表,且位姿是 lidar 专属语义)。
- **存原始 ego→global**:跨帧相对位移 = `inv(pose_i) @ pose_j`,由消费方算,不预存。
- **nullable 友好**:历史 scene / 非 nuScenes 来源无行 → 消费方按"无轨迹"降级,不报错。
- **世界系只在 scene 内可比**:nuScenes ego_pose 跨 log 世界系不可比,本表只服务 scene 内跨帧,不跨 scene 比绝对坐标。
- **逐相机 timestamp 偏差不在本表**:同 sample 跨相机 ~50ms 偏差留 v0.15.1+ 处理;本表只存 frame 级主时钟。

### API 与透出

- `GET /api/v1/scenes/{id}/trajectory`:按 `frame_index` 升序返回 `{scene_id, poses: [{frame_index, timestamp_us, ego_translation, ego_rotation, source_metadata}]}`;无位姿 scene → 200 + `poses: []`。
- manifest(`GET /tasks/{id}/point-cloud/manifest`)新增 `ego_pose` 字段(本帧位姿,无则 null);v0.15.0 前端仅调试可见,不消费。

### 数据来源与回填

- `import_nuscenes_scene.py` 落 scene 后逐帧 upsert(`services/scene_pose.py::upsert_frame_poses`,按唯一键 `ON CONFLICT DO UPDATE`,幂等)。
- 历史 dataset 用 `scripts/backfill_frame_poses.py --dataset-id <uuid|display_id> --nuscenes-root <root>` 补;按 `scene.source_metadata.scene_token` 反查原元数据。

### ego 补偿与插值如何消费 trajectory(v0.15.1)

几何核心在 `services/ego_transform.py`(纯函数,euler XYZ 与 `axis_convention.py` / 前端 three.js 同约定),所有变换经世界系:

- **运动补偿 propagate**:`propagate` 对 box_3d 且源/目标帧均有 pose 时,`world = ego_to_world(psr_src, pose_src)` → `psr_dst = world_to_ego(world, pose_dst)`——静止物世界位置不变,目标帧 PSR 自动"追平"ego 运动。响应带 `motion_compensated` 标记;任一帧缺 pose 恒等降级(= v0.14.1 原样复制,byte 级零回归,`test_annotation_propagate.py` 既有用例守)。`override_psr` 显式给定时跳过补偿。
- **批量**:`POST /tasks/{id}/annotations/propagate-batch` body `{target_task_id, annotation_ids|null}`(null=源帧全部 active box_3d),逐条复用单条 propagate 校验,整批一个事务。
- **区间插值**:`POST /tasks/{id}/annotations/interpolate-range` body `{track_id, to_task_id}`(路径 task=起点帧)。两端框各 `ego_to_world` → 世界系线性插值中心 + **slerp** 插值朝向 + 线性插值尺寸,按 `t=(m-i)/(k-i)` 投回各中间帧 ego 系。生成框 `source="interpolated"`;已有同 `track_id` 标注的中间帧幂等跳过(`skipped_frames` 透出);任一中间帧锁态 → 整批拒。中间帧 task 经 `scene.get_scene_frame_task_map`(与 neighbors 共用)反查。
- **前端 overlay ego 对齐**:`useSceneTrajectory(sceneId)` 拉 trajectory → `stages/three-d/geometry/egoAlign.ts` 用 `inv(T_cur) @ T_nbr` 把邻帧参考框变换到当前帧 ego 系再 `setReferenceBoxes`,静止物参考框与当前帧重合。无轨迹 → 退回 v0.14.1 原样叠加。
- **审核口径**:`source="interpolated"` 与 `manual` 可区分,审核侧可按来源过滤/批量删插值框(本期不新建审核 UI,只保证标记可查)。

## 跨帧 UX 如何消费 neighbors API(v0.14.1)

v0.14.1 在这套地基上落了用户可用的跨帧能力,消费路径:

1. **`useFrameNeighbors(taskId, k)`**(`apps/web/src/hooks/`):薄包 `GET /tasks/{id}/neighbors?k=K`,纯透传 `NeighborsResponse`,不感知几何类型,3D / 2D 共用。`refresh()` 在 propagate 前强刷避免缓存陈旧。
2. **propagate**:`POST /tasks/{task_id}/annotations/{annotation_id}/propagate-to-task` body `{ target_task_id }`。`services/annotation.py::propagate` 复制 geometry / class / attributes,共享 `track_id`(源无则 `_new_track_id()` 分配一个 `trk_<uuid.hex>` 并写回源),`box_3d.convention_at_create` 取**目标** dataset 的 `axis_convention`。
   - **track_id 作用域**:跨帧同一对象共享一个独立的 `track_id` 列(几何类型无关,`_new_track_id()` 产)——同 scene 跨帧 overlay 按 `track_id` 精确匹配不误命中无关对象。<!-- since v0.21.2 · ADR-0045：编组下线，原 per-task group 序列 / cross_frame_group_seq 高位段双命名空间已废，group_id 列删 -->
3. **邻帧框叠加**:`useNeighborAnnotations(taskIds, trackId)` 用 `useQueries` 批量拉前后 K 帧 task 的标注(复用 `["annotations",taskId]` 缓存键),client 端按 `track_id` 过滤 → `PointCloudScene.setReferenceBoxes` 渲染只读参考框。`trackId=null` 时整 hook 短路不发请求。
4. **键位**:3D `Shift+→/←`(ThreeDWorkbench 本地 keydown,3D 无 arrow-nudge 冲突);2D `Alt+→/←`(中央 hotkey,2D 的 `Shift+方向` 已被 10px nudge 占用)。两者共用壳层 `useWorkbenchShellModel.crossFramePropagate`(几何无关)+ `resolveCrossFrameTarget` 纯函数判 scene 边界。

## scheduler scene 连续标注(`prefer_same_scene_continuation`,v0.14.1)

`Project.prefer_same_scene_continuation`(默认 `false`)打开后,`get_next_task` 在套用既有 sampling 策略**前**插一步:找用户在 `scene_continuation_window_min`(默认 30)分钟内最近创建的 active annotation → 其 task 的 `scene_id + frame_index` → 该 scene 内 `frame_index` 更大的、按帧升序第一个可分配(未锁未标可见)task,锁定返回。找不到回退既有策略。

- **不**强制独占 scene(其它帧仍可分配给他人),只是"同一人继续要 task 时优先连续"。
- 普通项目默认关闭；scene 模式项目创建时默认开启。关闭时整段不进入,既有 sampling 测试 byte-for-byte 不变(`tests/test_scheduler_scene_preference.py` 守此)。

## scene 感知分包

`BatchService.split(strategy="by_scene")` 用 task 的主 dataset item 反查 `scene_id + frame_index`，按 scene 分组建批次。

- 普通图片 task 走 `Task.dataset_item_id`。
- 点云 task 走 `TaskDatasetItemLink(role="primary_lidar")`。
- 每个 scene 生成一个 `draft` 批次。
- 批次内 task 按帧号排序，并写 `Task.sequence_order = frame_index`。
- 没有 scene 的 task 不丢弃，会进入“无 scene”兜底批次。
- 反查 `scene_id + frame_index` 的 `resolve_task_scene_frames` 按固定 chunk(5000)分批查询，避免大数据集 scene 项目一次性把全量待分包 task id 灌进 `IN(...)` 撞 asyncpg 绑定参数上限。

这让 scene、批次 owner 和审核粒度自然对齐，避免跨帧 propagate 或连续调度被 batch 可见性打断。

## 不在本期(留后)

- 跨 scene 段内段间无感导航(case C 视频多段)→ v0.14.2+
- 视频段 `Alt+→` 分流到 `video_tracker_runner`(段内)→ 后续
- Kalman / 非线性运动模型(急转弯/加减速时线性插值会偏)→ 后续按需
- `point_mask_3d` 跨帧(点索引跨帧无意义,需"按世界系框重新分割"另一套机制)→ 明确不做,留更后
- scene 跨多 dataset(一 scene 横跨 lidar + image dataset)→ v0.15.2+
