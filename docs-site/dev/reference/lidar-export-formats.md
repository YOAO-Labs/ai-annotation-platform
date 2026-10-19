---
title: 点云（LiDAR）导出格式
audience: [dev, ml_engineer]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# 点云（LiDAR）导出格式

点云（LiDAR）项目支持五种受信导出目标格式。其中 `nuscenes` 执行比其余格式更严格的源数据契约校验，仅接受从真实 nuScenes 表结构与媒体文件完整导入的场景。

| 导出目标（Target） | 用途说明                             | 坐标系约定                   |
| ------------------ | ------------------------------------ | ---------------------------- |
| `aap_json`         | 平台原生无损 JSON 格式               | 由 API `axis_frame` 参数指定 |
| `coco-multicamera` | 合并的人工相机 2D 边界框 COCO 数据集 | 图像像素坐标系               |
| `kitti`            | KITTI 3D 目标检测标签格式            | 所选 KITTI 相机坐标系        |
| `nuscenes`         | nuScenes 关键帧关系表集合            | 全局 nuScenes 坐标系         |
| `pointmask`        | 逐点语义分割标签                     | 点云点索引顺序               |

上述导出目标均为纯粹的序列化器。`aap_json` 会完整保留 `SceneTrack` 下的各相机传感器成员（Camera Members）、可见性属性以及按相机角色关联的标定修订（Calibration Revision）关系。

<span id="common-package-files" aria-hidden="true"></span>

## 通用导出包文件结构

标准 LiDAR 导出包仅写入标注文件与媒体清单（Manifest），不直接内嵌原始点云或图像字节流。这一设计与图像/视频导出策略保持一致，能够显著减小大型多模态场景导出包的体积。

| 文件路径                          | 内容说明                                    |
| --------------------------------- | ------------------------------------------- |
| `classes.txt`                     | 项目类别名称列表                            |
| `attribute_schema.json`           | 项目属性 Schema（当勾选导出属性时生成）     |
| `images_manifest.json`            | 关联相机图像的预签名下载 URL 清单           |
| `pointclouds_manifest.json`       | 主激光雷达点云的预签名下载 URL 清单         |
| `fetch_images.py`                 | 将相机图像批量下载至本地 `images/` 的脚本   |
| `fetch_pointclouds.py`            | 将点云文件批量下载至本地 `velodyne/` 的脚本 |
| `calib_raw/<camera>/<frame>.json` | 原始 `SensorCalibration` 传感器标定载荷     |

执行多目标联合导出时，每个导出目标的文件会分别放置在各自的 `{target}/` 子目录下。

<span id="kitti-3d" aria-hidden="true"></span>

## KITTI 3D 格式（`kitti`）

KITTI 3D 导出包生成以下文件：

```text
label_2/<frame>.txt
calib/<frame>.txt
calib_raw/<camera>/<frame>.json
export_report.json
```

`label_2/<frame>.txt` 中的每一行包含标准 KITTI 的 15 个字段：

```text
type truncated occluded alpha x1 y1 x2 y2 h w l x y z ry
```

### 预检与坐标变换规则

- **必填请求参数与预检（Preflight）**：请求体必须指定 `{"lidar":{"kitti_camera_role":"camera_front"}}`。API 在创建 `AsyncJob` 前会逐任务检查主 LiDAR 数据项、可信的数据集 `axis_convention`、所选相机数据项、有限的行主序（row-major）标定矩阵以及合法的正数图像宽高；任一检查未通过直接返回 `HTTP 409`，Worker 在查缓存与打包前也会重复同一校验。
- **ISO 到相机坐标系的映射**：平台内部以 ISO 8855 坐标系（`+X` 向前、`+Y` 向左、`+Z` 向上）存储 `box_3d` PSR。序列化器对每一帧先将 ISO 八角点经数据集的 `R_norm^T` 还原，再依次应用所选相机的外参（`extrinsic`）、可选矫正矩阵（`rect`）与内参（`intrinsic`）。2D 边界框由八个角点及与近裁剪面的交点共同投影并裁剪至真实图像边界，`truncated` 即为裁剪损失的投影面积比例；`location` 为矫正后相机坐标系下的 3D 框底面中心，`rotation_y` 由变换后的局部 `+X` 方向求得，`alpha = rotation_y - atan2(x, z)`。
- **人工 2D 框优先与审计报告**：若所选相机在同一 `SceneTrack` 下存在有效的人工 2D 边界框成员，该人工框优先于 3D 投影派生框（3D 尺寸、位置与朝向仍取自 3D 成员）。`export_report.json` 会分别统计 `manual_bbox_count` 与 `derived_bbox_count` 供下游审计。人工成员的可见性映射为 KITTI 遮挡等级：`visible=0`、`occluded=1`、`truncated=0`、`unknown=3`。
- **背面剔除与真实标定输出**：完全位于相机背面、视野外或投影退化的目标会从 `label_2` 中省略，并在 `export_report.json` 中记录稳定的剔除原因。`calib/<frame>.txt` 始终输出所选相机真实的 `P2`、`R0_rect` 与 `Tr_velo_to_cam`，不存在单位阵回退或 `.unverified` 文件名。

<span id="multi-camera-coco" aria-hidden="true"></span>

## 多相机 COCO 格式（`coco-multicamera`）

`coco-multicamera` 为所选项目或批次中的每个关联相机角色生成一份统一的 COCO Instances `annotations.json`。该格式**仅导出有效的人工持久化相机 2D 边界框成员**，绝不从 3D 框自动派生缺失的 2D 标签；不含人工 2D 框的关联图像仍保留在 `images[]` 中作为负样本。

- **文件名与扩展溯源字段**：每张图像的 `file_name` 格式为 `images/{sensor_role}/{task_uuid}/{source_basename}`。标准 COCO 消费方可直接忽略额外附带的任务、数据项、传感器角色、Scene、帧号、SceneTrack、轨迹、可见性及标定关联状态字段。所有整数 ID 均由稳定实体标识确定性派生（处于 JavaScript 安全整数范围内并带冲突检测）。若保存人工框后相机标定发生过变更，该标注作为 2D 真值依然有效，并标记 `relation_status=stale`。
- **媒体清单与安全拉取脚本**：导出包同时包含 `media_manifest.json`、`fetch_media.py`、`export_report.json` 与 `README.md`。Worker 会流式读取每张图像以计算真实字节大小与 SHA-256；`fetch_media.py` 会拒绝不安全或重复的路径，校验本地已有及新下载的文件，先写入临时文件并在哈希核验通过后原子替换目标路径。
- **预检与容量配额**：预检要求任务范围非空、每任务至少关联一个相机、任务内数据项与传感器角色唯一绑定、图像尺寸与对象大小合法、边界框几何有限且为正、类别已知，且满足“人工 2D 成员 → 有效 3D 成员 → SceneTrack”身份闭环。单次导出上限为 **20,000 张关联相机图像、100,000 个人工框、总媒体体积 20 GiB、单张图像 256 MiB**。

<span id="nuscenes-json" aria-hidden="true"></span>

## nuScenes JSON 格式（`nuscenes`）

`nuscenes` 在 `v1.0-aap/` 目录下输出 13 张核心关系表，并附带 `media_manifest.json`、`fetch_nuscenes_media.py` 与 `README.md`。媒体清单按原始路径引用真实的 `.pcd.bin`、相机图像与地图文件，平台内部转换生成的标准 PCD 绝不会冒充原始 nuScenes 媒体输出。

- **严格源契约预检**：预检要求源数据必须为真实 nuScenes 导入、导入时使用 `--frame ego`（ISO 8855）、包含完整的 `0..nbr_samples-1` 场景序列、源 sample 双向链表闭合、逐传感器 sample_data / calibrated_sensor / ego_pose 引用完整、标定摘要未发生篡改，且源资产在对象存储中的实际大小与 SHA-256 与元数据一致。若项目或批次导出仅包含残缺场景，API 会在创建异步任务前直接拒绝。
- **全局坐标还原与 Devkit 兼容**：序列化器会重建确定性 token 与 `prev` / `next` 链表，沿用源 sample 时间戳，将自车（ego）坐标系下的 3D 框变换回全局坐标系，将平台的 `[length, width, height]` 映射为 nuScenes 约定的 `[width, length, height]`，并基于当前 ego PCD 实时统计 `num_lidar_pts`。运行拉取脚本后，目录结构可直接由官方 `nuscenes-devkit` 加载和查询（注：导出结果为关键帧子集，项目自定义类别不会自动映射为官方评测本体）。

<span id="point-mask" aria-hidden="true"></span>

## 逐点语义掩码格式（`pointmask`）

`pointmask` 导出包生成以下文件：

```text
segmentation/<frame>.label
category_map.json
```

`segmentation/<frame>.label` 是小端序（little-endian）`uint32` 二进制数组，索引 `i` 对应所拉取点云中第 `i` 个点的类别 ID（类别 ID 从 `1` 开始编号，`0` 表示未标注/背景）。

::: warning 点序严格绑定
切勿将 `.label` 文件与非同源的点云文件混用。点索引顺序仅保证与同包 `pointclouds_manifest.json` 引用的点云文件精确对齐。
:::

<span id="target-validation" aria-hidden="true"></span>

## 导出目标校验与同名区分

导出注册表识别以下五种点云导出标识符：

```text
aap_json, coco-multicamera, kitti, nuscenes, pointmask
```

其中 `kitti` 标识符在视频项目与点云项目间共享名称，但底层由各自独立的序列化器实现：

| 项目数据类型（`data_type`） | `kitti` 导出目标的实际含义 |
| --------------------------- | -------------------------- |
| `video`                     | KITTI 2D 多目标追踪标签    |
| `lidar`                     | KITTI 3D 目标检测标签      |
