---
title: 点云联合标注数据模型
audience: [developer]
type: reference
status: stable
last_reviewed: 2026-08-27
---

# 点云联合标注 · 后端数据模型

> v0.13.0 落地的后端数据地基。本切片纯新增、无前端可见变化，为后续「LiDAR 点云 + 相机图像联合标注工作台」（Epic v0.13.x）打底。
>
> 设计依据：调研 `docs/research/14-point-cloud-image-fusion.md` §14.8.3（仓库源码文档，非本站页面）。

## 概览

支持点云 + 图像联合标注需要四个后端原语，v0.13.0 一次落地：

| 缺口                    | 落地物                                                      | 位置                                                |
| ----------------------- | ----------------------------------------------------------- | --------------------------------------------------- |
| G1 任务-数据项 1:N 关联 | `TaskDatasetItemLink` 中间表                                | `app/db/models/task_dataset_item_link.py`           |
| G2 标定存储（v0.13.1）  | `SensorCalibration` 进 `DatasetItem.metadata_`              | `_jsonb_types.py` / `services/pointcloud_import.py` |
| G3 3D 几何              | `Box3DGeometry` / `PointMaskGeometry`                       | `app/schemas/_jsonb_types.py`                       |
| G4 工具单位 / file_type | `lidar_box_3d`（启用）/ `point_mask_3d`（新增）；点云扩展名 | `_jsonb_types.py` / `services/dataset.py`           |
| G6 跨模态 ID 约定       | 复用 `Annotation.track_id`（不改模型）                      | 见下文「跨模态身份」                                |

> v0.13.0 落 G1/G3/G4/G6 静态地基；v0.13.1 补 **G2 标定存储** + **scene 导入管线**（见下文「scene 导入数据流」）。

## G1 · 多文件关联：`TaskDatasetItemLink`

一个 3D 标注任务（一帧 scene）同时关联多个数据项：一份主点云 + N 路相机图像。这与 2D 单文件任务的 1:1 关联（`task.dataset_item_id`）并存。

```
task_dataset_item_links
  id              uuid  PK
  task_id         uuid  FK→tasks.id            (ON DELETE CASCADE, indexed)
  dataset_item_id uuid  FK→dataset_items.id    (ON DELETE CASCADE, indexed)
  role            str   not null               # primary_lidar | camera_<name>
  sensor_name     str   nullable               # 传感器物理名（可选）
  created_at      timestamptz
  UNIQUE (task_id, role)                        # 一个 task 每个 role 槽位仅一个 item
```

- **`role` 约定值**：`primary_lidar`（主点云）或 `camera_<name>`（如 `camera_front`、`camera_rear_left`）。
- **校验在 service 层**（`services/task_dataset_link.py` 的 `_validate_role`）：`role == "primary_lidar"` 或 `role.startswith("camera_")`，否则 `raise ValueError`。**不加 DB CheckConstraint** —— `camera_<name>` 是开放后缀，枚举不可穷举，约束放应用层更自然。
- **共存策略**：2D 单文件 task 继续用 `task.dataset_item_id`（1:1）；3D 多文件 task 用 link 表。两条路径在 service 层按 `project.data_type` 分流，互不影响。`task.dataset_item_id` 1:1 路径完全保留。

service 接口（`app/services/task_dataset_link.py`）：

```python
await link_items(session, task_id, [(item_id, "primary_lidar", None),
                                     (item_id2, "camera_front", "cam0")])
links = await get_linked_items(session, task_id)
```

## G2 · 标定当前值与版本历史

相机标定按相机一份，当前读模型存进相机 `DatasetItem.metadata_` 的约定 key `"calibration"`。`sensor_calibration_revisions` 以 `(dataset_item_id, revision)` 保存 append-only 标定快照、SHA-256 digest、操作者和时间；所有标定更新都通过带 expected revision/digest 的服务串行化。只有 metadata 的存量标定按虚拟 revision 1 读取，首次修改前会先物化该基线。

```python
class SensorCalibration(BaseModel):
    extrinsic: list[float]   # len 16, row-major 4x4 外参
    intrinsic: list[float]   # len 9,  row-major 3x3 内参
    rect: list[float] | None # len 16, KITTI 可选矫正矩阵

class DatasetItemMetadata(BaseModel):   # extra="allow" 保留其它 metadata key
    calibration: SensorCalibration | None = None
```

`DatasetItemOut.metadata` 用 `DatasetItemMetadata` 出强类型(codegen 流到前端)。投影(v0.13.4)按 `task → camera link → DatasetItem.metadata_["calibration"]` 取标定:`extrinsic·[x,y,z,1] → 取 xyz → intrinsic·xyz → 透视除法 → 像素`。

标定管理沿用 task visibility 与相机 role 解析边界：

```text
GET   /tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration
PATCH /tasks/{task_id}/point-cloud/cameras/{camera_role}/calibration
```

GET 返回当前 revision/digest 及降序完整快照。如果历史表尚无记录，响应仍包含从 metadata 读取的虚拟 revision 1，`created_at` 为 `null`。PATCH 只允许项目 owner 或 super admin，请求必须携带当前 expected revision/digest；跨项目复用同一相机 DatasetItem 时只允许 super admin 修改，避免一个项目的 owner 改变其他项目共享的数据集真值。冲突返回 409，不会自动重放。无变化更新返回当前快照且不新增历史行。

工作台管理面板始终读写未经 ISO 轴约定变换的原始标定；投影渲染仍在独立派生链中对 extrinsic 做坐标归一化，不得把渲染矩阵回写为数据集真值。从旧快照“恢复”时也通过同一 PATCH 追加新 revision，不存在原地回滚端点。

## scene 导入与任务创建数据流

素材入库仍由资产服务持有；`application/dataset_task_creation.py` 按 `project.data_type == "lidar"` 分流，标注所有者只消费不可变帧事实。

```mermaid
flowchart TD
    Entry["关联项目 / CREATE_TASKS worker"] --> Workflow["DatasetTaskCreationWorkflow"]
    Workflow --> Calibration["pointcloud_import.attach_calibration"]
    Calibration --> Commit["标定独立提交"]
    Commit --> Prepare["prepare_pointcloud_frames：缺场景时推断 single"]
    Prepare --> Group["dataset_frame_grouping：按 scene / stem 分组"]
    Group --> Labeling["LabelingTaskService.create_initial_pointcloud"]
    Labeling --> Writes["Task / 主点云与相机关联 / 项目任务数"]
    Writes --> Chunk["按原块大小提交与上报进度"]
```

角色匹配仍取路径中最后一次命中的 lidar/camera/calib 段；每个主点云帧建一个 Task，并关联实际存在的 `primary_lidar` 和 `camera_<cam>`。缺相机可只关联主点云；缺主点云的分组才跳过并警告。按已有主点云任务跳过已建帧，不新增并发唯一性约束或残缺关联补建。

入库后的追加仍按 DatasetItem 建题，不改成初始帧聚合。标定修改后重放即使没有新任务也会先提交标定；解绑/删除的跨域写入尚未迁移，不能把当前边界视为可直接拆成远程事务。

- **`task.dataset_item_id` 指向主点云 item**:让假设单 item 的现存消费方(导出 / 列表 / 缩略图)不炸,link-aware 消费方走 link 表取全部。
- **标定降级**:无 calib 文件 → 跳过写入,scene 退化为 3D-only,不阻断。

## G3 · 3D 几何类型

加入 `Geometry` discriminated union（`Field(discriminator="type")`），零迁移（几何存 `annotations.geometry` JSONB）：

| `type`          | 类                  | 字段                                    | 备注                                               |
| --------------- | ------------------- | --------------------------------------- | -------------------------------------------------- |
| `box_3d`        | `Box3DGeometry`     | `center[3]` / `size[3]` / `rotation[3]` | 米 / 长宽高 / 绕各轴弧度；`extra="allow"` 容纳扩展 |
| `point_mask_3d` | `PointMaskGeometry` | `point_indices: list[int]`              | 指向点云的非负整数索引；`extra="forbid"`           |

旧 2D 几何（bbox / polygon / …）不受影响。前端强类型由 OpenAPI codegen 落到 `apps/web/src/api/generated/`；手写业务 union（`apps/web/src/types/index.ts`）暂不并入，待 v0.13.2 前端引入 3D 工作台时再加（避免逼迫现有 2D 窄化逻辑处理 3D 分支）。

## G4 · 工具单位 + file_type

- **工具单位**（`ToolUnitId` / `TOOL_UNIT_IDS`）：`lidar_box_3d` 从「留位」转为后端可用；新增 `point_mask_3d`。
- **file_type**：`services/dataset.py` 的 `_infer_file_type_from_ext` 放开点云扩展名 `.pcd` / `.bin` / `.las` / `.ply` → `file_type = "point_cloud"`（对应 `DatasetDataType.POINT_CLOUD`）。

## G6 · SceneTrack 多模态成员

<!-- since v0.21.2 · ADR-0045：跨模态身份从 group_id 迁到独立 track_id 列，group_id 列 / group 端点已下线 -->

同一物理物体的「3D 框 + 各相机人工 2D 框」共享 `Annotation.scene_track_id + track_id`，聚为一个逻辑对象。3D 框是该帧的主成员；每个相机 role 最多有一个活跃 bbox 成员。

约定：

- 2D 成员仍是一等 `Annotation`，`geometry.type=bbox`，并保存 `sensor_dataset_item_id`、`sensor_role`、`sensor_visibility`、`calibration_revision` 和 `calibration_digest`。
- 传感器上下文字段必须全有或全无；部分唯一索引约束 `(task_id, scene_track_id, sensor_role)` 的活跃成员。
- SceneTrack revision 是跨模态成员集合的并发边界。成员创建、更新、删除或恢复同时校验 annotation version、track revision 和当前 calibration revision/digest。
- 3D→2D 投影仍是可重建的派生参考，不写入数据库；人工 2D 框不会因 3D 框或标定变化被自动覆盖。标定 digest 不一致时关系状态为 `stale`。
- SceneTrack 拆分、合并、缺席、恢复与终止会同时处理全部模态成员，主标注列表和任务统计只计算非传感器成员。

AAP JSON 以 camera role 而不是实例内 DatasetItem UUID 迁移相机成员；导入时把 role 重新解析到目标任务的数据项，并恢复原 SceneTrack 关系和标定版本证据。

## 点云查看器 manifest API + 前端模块（v0.13.2）

前端 3D 舞台经一个 manifest 端点拿渲染所需的一切：

```
GET /tasks/{id}/point-cloud/manifest   (project.data_type=="lidar"，否则 409)
  → { point_cloud_url,                 # 主点云 presigned URL（datasets bucket）
      point_cloud_format: "pcd",
      cameras: [{ name, role, image_url, calibration: SensorCalibration | null }],
      expires_in }
```

实现：`api/v1/tasks/video.py` 用 `get_linked_items` 取 link → 主点云（无 `primary_lidar` link 时回退 `task.file_path`）+ 各相机 presign + `metadata_["calibration"]`（非法降级 None）。

### 可信 KITTI 导出合同

点云 KITTI 不直接把平台 ISO 框当成 camera frame。导出按每个 task 的主点云 Dataset 读取 `axis_convention`，先用 `R_normᵀ` 把 ISO 角点映射回数据源 LiDAR 轴，再依次应用用户显式选择的 camera role 对应 `extrinsic`、可选 `rect` 与 `intrinsic`。`label_2` 的二维框来自近裁剪面裁剪后的 8 角点 / 12 边投影；`location` 使用相机坐标下的底面中心，完全不可见对象进入 `export_report.json`。

项目和批次导出在创建 `AsyncJob` 前共用严格预检，worker 在查缓存和打包前重复检查。缺主点云、未声明或不可信的轴约定、缺所选相机帧、非法标定、缺图像宽高都会返回稳定 issue code；不得用 identity matrix、`.unverified` 文件或负数 bbox 代替失败。Multi-camera COCO 不要求主点云轴约定，但会严格校验相机 link、媒体对象、归一化人工 bbox、类别以及 2D 成员与活跃 3D 成员、SceneTrack 的身份闭合；标定变化只将关系标为 stale，不改写独立 2D 真值。nuScenes 还会校验真实 scene / log / map、完整 sample 链、逐传感器位姿与时钟、原媒体指纹和完整 Scene 范围，任一缺口都拒绝。

前端(双画布架构,ADR-0031):`project.type_key === "lidar"` → `WorkbenchStageHost` 的 `3d` 分支 → lazy `ThreeDWorkbench`(独立 `vendor-three` chunk,不进主 bundle)。裸 Three.js 封装 `PointCloudScene`，由持久 Worker 解析 PCD、归一化轴向、抽稀并生成高度色；主透视视图与 Top / Side / Front 三正交视图使用同一个 renderer、canvas 和图形 context，通过 viewport / scissor 分 pass 绘制，并由事件驱动 scheduler 在状态稳定后停止提交。四视图共享点云 geometry 的 GPU attribute、backend、相机纹理和 device-lost 生命周期；不同相机仍各自执行一次 render pass。默认使用 Legacy WebGL2，设置中的本地实验开关可启用异步 WebGPU renderer、实例化点精灵和相机纹理直采样；切帧时旧实例立即归零，场景级实例缓冲与固定六路相机采样 TSL 拓扑继续复用，只更新点属性、纹理和标定 uniform。点云 geometry 就绪后会用固定六面裁剪拓扑异步准备三视图管线；准备期间若三视图被展开，scheduler 会在完成后补画，避免把首次管线编译留到交互路径。实验路径只生成 GPU 需要的 depth-only 遮挡栅格，并在 8 MiB / 8-key LRU 中与相邻帧预取合并，Legacy 和 WebGL2 fallback 不触发该预取。初始化失败或 device lost 会回退 Legacy。模块在 `apps/web/src/pages/Workbench/stages/three-d/`,与 Konva `stage/` 隔离。

### 会话态测量 Overlay

点云测量是前端辅助层，不是 `Annotation` 几何类型。`ThreeDWorkbench` 持有当前 task 的测量草稿与已完成路径；锚点由 `PointCloudScene.pickPoint` 严格射线命中当前渲染点后取得源点索引和 ISO 世界坐标，不回落到 `groundZ`、地面平面或自由空间。读数由纯函数累计各段三维距离与水平距离，首尾高差取末点 `z` 减首点 `z`。

React 状态只经 `usePointCloudScene` 单向同步到 `PointCloudScene` 的 measurement layer。路径更新、隐藏、删除和 scene dispose 都会释放该层自有的 geometry/material，并仅触发主视图的事件驱动重绘。测量不调用 Annotation API，不进入 history、导出、审计或账号偏好；切换 task 会清空全部测量。只读任务仍可使用，因为该能力不产生持久化写入。

开发 seed 的 nuScenes 导入器把每点五个 float 的 `.pcd.bin` 转为只保留 XYZ 的 little-endian binary PCD，并在 dataset source metadata 中记录 `pcd_encoding=binary_xyz_f32`。同时保留原始 `.pcd.bin`、相机图和 map，冻结 size / SHA-256 及 scene、sample、sample_data、sensor、calibrated_sensor、ego_pose、log 和 map 表上下文。对既有 Scene 重跑导入器时，只会在来源表上下文与原始字节指纹全部未漂移时幂等回填可信导出合同，并保留任务和标注；同名 Scene 指向不同源时直接拒绝。新数据集使用受保留的 Dataset UUID 对象前缀，普通上传无法写入该命名空间。nuScenes 预检在完整加载前对项目或批次限制 1000 帧、30000 个有效 3D 框，以及单帧/总 PCD 字节数和框内点计算预算。Scene 时间轴点击目标帧后会立即开始任务导航，PCD、相机位图及实验路径深度资源的预取在后台并行执行并由资产缓存去重；慢预取或预取失败不会阻塞导航。

渲染链在 geometry 与相机颜色跨过实际绘制边界后写入 `aap:pointcloud:geometry-ready` 和
`aap:pointcloud:camera-color-ready` Performance mark，便于在 DevTools trace 中区分 PCD/geometry
等待与相机上色等待。开发环境可用 `pointcloud:renderer-bench` 对同一 nuScenes Scene 分别刷新
Legacy 与实验 renderer；必须同时提供 `POINTCLOUD_BENCH_PROJECT_ID` 和
`POINTCLOUD_BENCH_TASK_ID`。报告中的 `runValidity` 只说明样本可信，只有 `promotionGate.passed`
才表示本机样本达到推广门；实验开关仍需跨 OS/GPU 资格后才能转为默认功能。

> 只读;3D 框标注(v0.13.3)与标定驱动投影联动(v0.13.4)后续。

## 3D 框标注链路(v0.13.3)

把只读查看器升级为**可标注**:在 3D 工作台画 / 选 / 编辑 `box_3d` 框,经现有标注 CRUD 持久化。**后端零改动、零新端点、零迁移**(链路在 v0.13.0 已备好),全部是前端 + 一处设置解禁。交互形态决策见 ADR-0032。

### 持久化 payload(复用既有标注 API)

```jsonc
POST /tasks/{id}/annotations            // 创建; PATCH .../{aid} 更新, DELETE 软删
{ "annotation_type": "box_3d",
  "tool_unit_id": "lidar_box_3d",        // 类别 / 属性绑定挂在此 unit 下
  "class_name": "car",
  "geometry": { "type": "box_3d",
    "center": [x, y, z],                 // 米, 点云 Z-up
    "size":   [长, 宽, 高],
    "rotation": [0, 0, yaw] } }          // 7-DoF: 仅 rotation[2]=yaw(绕 Z)可编辑
```

- **类别校验**:service 层 `_validate_class_name` 按 `lookup_classes_for_tool_unit(tool_bindings, "lidar_box_3d")` 校验,非集合内类别 422;空 `tool_bindings` 放行(向后兼容)。与 2D 同一条路径,无 3D 专用分支。测试:`apps/api/tests/test_pointcloud_box3d_annotation.py`(创建命中 / 422 / 空放行),几何判别联合分发由 `test_jsonb_strong_types.py::test_geometry_union_dispatches_3d_types` 锁。
- **设置解禁**:`lidar` 项目在建项目向导 / 项目设置可启用 `lidar_box_3d` 并配类别(`toolUnits.ts` 映射 `data_type==="lidar" → ["lidar_box_3d"]`,向导按 `available` 过滤,v0.13.3 起 `available=true`)。

### 编辑交互(主视图 gizmo + 数值面板,ADR-0032 方案 A)

- **渲染 + 选中**:`PointCloudScene` 框图层每框一个 Group(线框 + 半透拾取 Mesh,`depthTest:false` 始终画在点云之上),`Raycaster` 拾取选中。PSR 用 Group 的 position/quaternion/scale 表示(让 `TransformControls` 直接驱动)。
- **编辑**:选中后官方 `TransformControls`(W 平移 / E 绕 Z 转 / R 缩放)+ 右上 PSR 数值面板,两者经选中框 PSR 单一真值双向同步;缩放 gizmo 翻转出的负尺寸取绝对值兜底(下限 0.05m),避免框翻转 / 卡住面板 `size>0` 提交校验。
- **放置**:「放置框 (B)」切换 → 点地面射线打 `z=groundZ` 水平面(`groundZ` 由 z 直方图低分位估计,避免默认框悬浮)→ 默认尺寸框 → 持久化 → 自动选中精修。透视拖拽不准,故落点 + 默认尺寸 + 数值面板精修,不做拖画足迹。
- **只读**:锁定 task / viewer(`readOnly`,壳层透传)不放置 / 不挂 gizmo / 面板禁用,仅可选中查看数值。

### PSR↔角点纯函数(为 v0.13.4 投影预留)

`three-d/geometry/box3d.ts`(移植 SUSTechPOINTS 矩阵,无框架依赖,带单测):`boxToMatrix4` / `psrToCorners` 算 8 角点与朝向。v0.13.4 标定驱动 3D→2D 投影复用同一套约定(欧拉角顺序 `XYZ`、yaw=`rotation[2]`),约定一旦漂移投影必偏。

## 投影联动:3D 框 → 相机视图(v0.13.4)

把 3D 框经相机标定**实时**投影到各相机图上画线框,标注员对照图像确认 / 校正 3D 框。**后端零改动、零新端点、零迁移、不预存投影**:标定已在 `DatasetItem.metadata_`(G2)、manifest 的 `cameras[].calibration` 直出;前端每次按标定现算。前端为主,详见 ADR-0033。

### 投影链(纯函数 `three-d/geometry/projection.ts`)

世界点 `p=[x,y,z]`(lidar 系、米)→ 相机像素,与 SUSTechPOINTS `image.js#points3d_homo_to_image2d` + `util.js#matmul` **逐字对齐**:

```
p_cam   = extrinsic · [x, y, z, 1]ᵀ      // extrinsic: 行主序 4x4 lidar→camera 外参
p_cam   = rect · p_cam   (KITTI 有 rect)  // 可选矫正, 行主序 4x4
[u,v,w] = intrinsic · p_cam.xyz          // intrinsic: 行主序 3x3 内参
pixel   = [u/w, v/w]                       // 透视除法; 像素原点左上(u 右、v 下)
visible = w > 0                            // 相机前方; w<=0(后方)剔除该角点
```

- `projectPoints(points, calib) → { pixels, visible }`:接受 `THREE.Vector3[]` 或 `[x,y,z][]`。**手写行主序矩阵·向量**(`THREE.Matrix4.elements` 是列主序,直接喂行主序标定会被转置而出错)。
- `BOX_EDGES`:12 条边索引表(底面环 / 顶面环 / 4 竖棱),基于 `box3d.ts` 角点顺序;overlay 据此连线。
- **坐标约定锁死(漂移即偏)**:extrinsic 方向 lidar→cam、`rect` 在 extrinsic 之后、像素原点左上、欧拉顺序 `XYZ`。
- **欧拉顺序差异**:SUSTech 默认 `ZYX` 与 box3d.ts 的 `XYZ` 仅在 pitch/roll 非零时不同;7-DoF 只编辑 yaw(rx=ry=0)时退化为同一 `Rz`,角点一致。投影链本身与欧拉顺序无关。
- **对拍验证**:`projection.test.ts` 用 `third-party/SUSTechPOINTS/data/example/calib/camera/front.json` 真实标定 + 同 scene yaw-only 框,`psrToCorners`→`projectPoints` 与移植的 SUSTech oracle 逐角点像素一致(epic 验证策略)。

### overlay 渲染 + 缩放(`CameraProjectionView.tsx`)

- 相机图 `<img>` 上叠等尺寸 `<canvas>`,消费**同一份** `annotations`(经 `boxes`)+ `highlightedIds`;3D 框改 PSR / 改类 / 选中变化即重绘(`useUpdateAnnotation` **乐观更新**会即时写缓存,故面板 / gizmo / 列表改框后 overlay 立即跟随)。
- **缩放**:`intrinsic` 基于图像**原始分辨率**,投影像素是原图坐标;overlay 按 `clientWidth/naturalWidth` 比例缩放绘制,`ResizeObserver` + `onLoad` 重算。`devicePixelRatio` 适配高清屏。
- **可见性**:全角点在相机后方 / 全不可见 → 该相机不画此框;部分可见 → 只连两端都可见的边(MVP 不做画面裁剪)。无 `calibration` 的相机降级不画、不报错(承 ADR-0030)。

### 选中联动 + 跨模态身份

- **3D→2D**:选中 3D 框 → 各相机投影框高亮(白描边加粗 + 淡填充),承共享 `selectedId`。
- **2D→3D 反选**:点相机里的投影框 → 命中测试(投影包围盒含点、取最小面积框)→ `onSelectBox` 选中对应 3D 框。
- **最佳相机提示**:选中框按可见角点数统计被几个相机看到,状态条显示「投影可见于 N 相机 · 正对 X」,最正对相机 figcaption 标「· 正对」。
- **跨模态成员联动**：overlay 高亮集合 = 选中框 + 同 `track_id` 成员。放大相机图可独立创建 / 编辑人工 bbox；API 按 `scene_track_id` 读取成员并返回当前 track revision、标定关系和投影残差。新建 3D 框由 `_new_track_id()` 分配外部稳定键，权威成员关系由 `scene_track_id` 维护。<!-- since v0.21.2 · ADR-0045：原按 group_id + /annotations/group 端点，编组下线后统一到 track_id -->

## Scene 跨帧传播任务

长区间的 `box_3d` 传播使用统一 `AsyncJob` 持久化，`kind=point_cloud_cross_frame`。作业 payload 保存 Scene、源 task、源 annotation 的 `id + version` 快照、显式 scope / direction / 闭区间和目标帧快照；result 按帧记录 success / skipped / failed / stale / cancelled 及新建标注数。

每个目标 task 是独立提交边界。写入前会重新检查用户权限、task 可编辑性与源标注版本；已有同 `track_id` 活跃框的目标帧使用 `skip_existing` 跳过。源版本发生外部变化时，当前与后续未执行帧进入 stale，不再继续读取新几何。取消是协作式的：已提交帧保留，剩余帧终结为 cancelled。

`point_mask_3d` 不能复用跨帧点索引，因此不进入该传播合同；registration 和轨迹拆分 / 合并也保持为独立后续能力。

## 3D 质量闭环

3D 质量检查使用独立的 Run / Issue 领域，不把三维证据塞进二维 Mask QC：

```text
Project / Scene / Task / Annotation scope
  -> freeze config + annotation versions + SceneTrack revisions + point-cloud hashes
  -> PointCloudQualityRun + AsyncJob
  -> deterministic rule kernel
  -> PointCloudQualityIssue
  -> timeline marker / workbench locator / point_cloud feedback anchor
```

`PointCloudQualityRun` 保存 scope、配置快照与 digest、源快照与 digest、singleflight key、进度、跳过摘要和终态。相同输入与配置的 pending / running / completed 运行会复用；worker 开始前再次计算源 digest，防止在过期几何上生成新事实。

Project / Scene scope 冻结完整 Scene 成员，可以执行逐框和轨迹规则。Task / Annotation scope 只执行所选标注可独立判定的逐框规则；轨迹规则记录 `track_rules:scope_incomplete` skip，避免用成员片段制造整轨缺口、跳变或身份漂移。worker 每个标注/轨迹边界直接读取最新取消状态，并只缓存当前帧的解析点云。

`app/services/point_cloud_quality/execution.py::execute_run` 持有逐框/轨迹规则、skip/issue 写入和 Run 终态；`app/application/quality_execution.py` 将服务端 `QualityJobLifecycle` 绑定到通用作业状态及终态通知。原 Celery worker 的注册名、单个 Run ID 参数与 `media` 路由不变；领域 `Run`、`Issue` 和 `AsyncJob` 的本地事务检查点也不因此转成远程接口。

`PointCloudQualityIssue` 持久化规则 code/version、class、severity/status、frame 区间、metric/threshold/evidence、标注版本、SceneTrack revision、稳定 dedupe key 和可恢复 locator。locator 可同时指向 Scene、帧、任务、标注、SceneTrack、相机与辅助层。人工决策由 `services/point_cloud_quality/service.py::decide_issue` 校验当前 Task 绑定与源新鲜度，再更新状态、trim 后的 reason/note、resolver 和 verdict/reviewer 字段；重开清除 resolution 与 review 字段。API 保留当前范围/角色检查、锁定获取、审计、提交与响应 refresh 顺序。Issue 和源身份仍归点云质检，不由反馈锚接管；处置不执行 `suggested_command`。

规则内核将 PCD 源坐标按 Dataset `axis_convention` 归一到 ISO 平台坐标，再执行框内点数、局部地面、尺寸稳健异常和轨迹时序检查。无法解析的 PCD 或不足的地面样本进入 run skip，不产生猜测 issue，也不会批量将同 Scene 的既有问题标为 stale。

问题的 `open / resolved / wont_fix / stale` 状态与通用 `AnnotationFeedback` 评论分开；人工评估结论也以 `confirmed / false_positive / accepted_exception / uncertain` 独立存储，不再从工作流状态猜测。评论通过 `anchor_type=point_cloud` 保存结构化定位器，并继续服从对应 Task 的可见性边界。标注版本、轨迹 revision、主点云 item / content hash / path 或项目规则 digest 任一变化都会在问题读取时使旧证据 stale；页内问题使用批量版本、轨迹与点云校验，不按 issue 执行 N+1 读取。

### 评估快照与配置晋级

Project 质量配置支持全局阈值和按 class 的稀疏 override；旧 schema 在服务边界补齐治理字段后规范化为当前结构。扫描 Run 仍冻结当时的完整 config snapshot/digest，同一 Run 不会在运行中读取新阈值。

`PointCloudQualityEvaluation` 将已有明确人工判定且非 stale 的 issue 冻结为最多 20,000 条的样本快照，保存 baseline/candidate 配置与 digest、cutoff、按规则/类别摘要、gate 理由和晋级记录。原始样本仅在服务端保留，API 不返回样本中的 issue id。数值候选只能沿不会制造新问题的方向重放；收紧阈值、改变样本构造阈值、启停规则或修改 severity 都要先执行新扫描。

观察精度与误报率只使用 `confirmed + false_positive` 作为可判定分母；`accepted_exception` 和 `uncertain` 单独计数。候选“已确认问题保留率”是 baseline 已发现问题上的代理指标，不是 recall。晋级同时要求受影响规则/类别的可判定样本数、候选误报率和确认保留率达标，并在 project row lock 下复核 baseline revision/digest 后才将 config revision 增加一次。
