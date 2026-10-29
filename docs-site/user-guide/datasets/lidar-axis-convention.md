---
audience: [admin, superadmin]
type: how-to
since: v0.13.11
status: stable
last_reviewed: 2026-06-10
---

# 点云数据集的 lidar 坐标系约定

如果你打开 3D 工作台后发现 **BEV 俯视下车头不朝屏幕上方**、或者**画框出来的 yaw=0 朝向与车身长轴完全错位**,绝大多数情况下不是平台或标定的 bug,而是数据集的 **lidar/world 坐标系约定** 与平台内部约定不一致。

## 平台内部约定

平台所有 3D 几何代码均假设 **ISO 8855 / SAE J670** 标准:

| 轴  | 方向     |
| --- | -------- |
| +X  | 车头(前) |
| +Y  | 车左     |
| +Z  | 天       |

右手坐标系。Autoware / nuScenes / Waymo / ROS REP-103 默认即此。

## 业界常见的非 ISO 约定

| 来源                   | 约定                      |
| ---------------------- | ------------------------- |
| KITTI camera-as-world  | +X 右 / +Y 下 / +Z 前     |
| Apollo                 | +X 右 / +Y 前 / +Z 上     |
| Velodyne SDK 原始系    | +X 右 / +Y 前 / +Z 上     |
| SUSTechPOINTS 示例数据 | +X 车左 / +Y 车后 / +Z 天 |
| OpenCV 相机系          | +X 右 / +Y 下 / +Z 前     |

只要数据集不是 ISO 8855,直接上传后 3D 工作台的 BEV 一定会"歪"。

## 处理方式

平台采用**加载侧归一化**:你声明数据集来自哪种约定,前端在打开 3D 工作台时**自动**把点云与相机外参旋转到 ISO 8855。原始文件(PCD / calib JSON)不会被改写。

### 创建数据集时声明

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/lidar-axis-wizard-step2.png — 向导第 2 步选「3D 点云」后出现 AxisConventionPicker + 自动检测按钮 [manual] -->

创建点云数据集时,上传向导**第 2 步**（填写数据集名称与类型）选择数据类型为「3D 点云」后会显示「LiDAR 坐标系约定」下拉（`AxisConventionPicker`）。选定后会随 `POST /datasets` 的 `axis_convention` 字段一起落库:

```json
{
  "name": "scene-001",
  "data_type": "lidar",
  "axis_convention": "sustechpoints_demo"
}
```

可选值:

- `iso_8855` (默认) / `ros_rep103` — 标准约定,identity
- `kitti_camera` / `opencv_camera` — KITTI / OpenCV 相机系
- `apollo` / `y_forward` — +Y 朝前
- `sustechpoints_demo` — SUSTechPOINTS 自带示例数据
- `raw` — 不归一化,平台不为该数据集承诺 ISO(用于无法归一化的特殊情况)

### 已上传数据集的修改

数据集详情页的设置面板也可修改点云坐标系。点「保存」后会通过 `PUT /datasets/{id}` 更新 `axis_convention`；如果该数据集已经关联项目,页面会先提示历史 3D 标注可能与新约定不一致。

API 仍支持直接修改:

```sql
UPDATE datasets
SET metadata = jsonb_set(metadata, '{axis_convention}', '"sustechpoints_demo"')
WHERE name = 'scene-001';
```

修改后重新打开或刷新 3D 工作台即可看到正确朝向的 BEV。

### 如何判断该选哪个约定

#### 自动检测

数据集详情页提供「自动检测」按钮,会调用 `POST /datasets/{id}/sniff-axis-convention`。平台优先读取当前数据集中 front 相机的外参,把相机光轴方向与已知约定做相似度比对,返回最匹配的结果。响应的关键字段如下:

| 字段         | 说明                         |
| ------------ | ---------------------------- |
| `best`       | 最匹配的 convention 字符串   |
| `score`      | 匹配分数（0–1），越高越可信  |
| `candidates` | 所有候选约定及各自分数的列表 |
| `per_camera` | 每个相机的独立推断结果列表   |
| `agreement`  | 各相机推断一致的比例（0–1）  |

`agreement` 低或 `score` 低时建议人工核查。无 front 相机时平台会对所有相机投票取众数，置信度打 0.75 折。

#### 手动核查

如果需要手算,且数据集里有 front 相机（named `"front"` 或类似）,先确认标定 JSON 的格式：

```jsonc
// calib/camera/front.json（示例）
{
  "extrinsic": [
    e0,  e1,  e2,  e3,
    e4,  e5,  e6,  e7,
    e8,  e9,  e10, e11,   // ← (e8, e9) = front 相机光轴在 world 系的水平投影
    0,   0,   0,   1
  ],
  "intrinsic": { "fx": 1210.06, "fy": 1205.85, "cx": 1022.43, "cy": 792.54 }
}
```

然后看 `(e8, e9)`（第 3 行前两个数）代表"车头方向"在 lidar 世界系下的指向:

- `(1, 0)` → 车头朝 +X → **`iso_8855`** ✓
- `(0, 1)` → 车头朝 +Y → **`apollo`** / `y_forward`
- `(0, -1)` → 车头朝 -Y → **`sustechpoints_demo`**
- 其它 → 看上述「业界常见」表对照

## 影响范围

声明 `axis_convention` 后:

- ✓ BEV 俯视(`俯视` 按钮)车头朝屏幕正上方
- ✓ 框选画框生成的初始 PSR(`yaw=0`) 沿车身长轴对齐
- ✓ 三视图 (Top / Side / Front) 视角与车身真实长 / 宽 / 高对齐
- ✓ 相机投影联动方向正确
- ✓ 新建 3D 框和点云分割标注会记录当时的 `convention_at_create`
- ✓ 原生 JSON 导出可通过 `axis_frame=source` 把 3D 框反向映射回数据源坐标系;默认 `iso` 保持平台内部坐标。参数语义与适用格式见 [导出 · 3D box 坐标系](../../api/guides/export.md#export-axis-frame)

### 历史标注

v0.13.11 之前已经标注的 3D 框,**当时是按 ISO 假设画的**(几何代码一直锁死 ISO),所以它们存的 PSR 对应的就是"用户当时屏幕上看到的"。给数据集设定新 convention 后,旧框会被按新 convention 重投影显示——若你确认旧框已经匹配点云(说明数据集本来就是 ISO),保持 `axis_convention=iso_8855` 即可,旧框不动。若新 convention 把旧框带歪,需要重新审标或在数据集设置回 `iso_8855`。

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/lidar-axis-mismatch-banner.png — 3D 工作台顶部坐标系不一致 banner + 重投影按钮 [manual] -->

v0.13.12 起,新建的 `box_3d` / `point_mask_3d` 几何会记录 `convention_at_create`。打开 3D 任务时,如果某个 3D 框的创建约定与当前数据集约定不同,工作台顶部会显示警告;选中单个框后可执行「按当前约定重投影选中框」。该动作只处理选中框,不做批量改写。

## 相关

- 架构决策: [ADR-0034 lidar 坐标系约定](../../dev/adr/0034-lidar-axis-convention)
- 实现计划: [lidar 坐标系约定实现计划](../../../docs/plans/archive/2026-06-05-v0.13.11-lidar-axis-convention.md)
- 收尾计划: [3D 打磨与 point mask 收尾计划](../../../docs/plans/archive/2026-06-05-v0.13.12-3d-polish-and-pointmask.md)
