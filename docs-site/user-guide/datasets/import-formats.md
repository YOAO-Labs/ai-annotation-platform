---
audience: [project_admin, super_admin]
type: how-to
status: stable
last_reviewed: 2026-10-18
---

# 点云 / 多模态数据集导入格式

本平台**只直接接受自家原生目录约定**;其他数据集格式(nuScenes / KITTI 等)请先用配套转换脚本离线转成原生结构,再上传或直接入库。这与 SUSTechPOINTS、xtreme1 等开源点云标注平台的做法一致——它们也不在上传端做格式适配,而是提供一次性转换脚本。

> 为什么不做"通用导入器":真实用户的点云数据来源高度异构(nuScenes / Waymo / KITTI / 自采),每种都有独立的标定坐标系与文件布局。"用 UI 导入任意格式"是伪需求——成熟平台都让用户先离线转换。平台聚焦把**原生格式**这条路走顺,再为常见数据源各配一个转换脚本。

## 平台原生目录约定

一个点云或多模态 Scene 的 ZIP 逻辑目录结构如下。下面的路径表示 ZIP 内的逻辑相对路径，不是对象存储 key。ZIP publication 使用独立的 UUID 对象 key；对象桶扫描只读取旧版 `<dataset_name>/` 前缀，不会自动发现这些 publication。

```
<dataset_name>/lidar/<frame>.pcd               # 点云帧(file_type=point_cloud)
<dataset_name>/camera/<cam>/<frame>.jpg         # 各相机同帧图像(file_type=image)
<dataset_name>/calib/camera/<cam>.json          # 每相机一份标定(对该相机所有帧通用)
```

- **帧 ID = 文件名 stem**（去掉扩展名），例如 `000970`。lidar 和各相机的同一帧必须使用**相同的文件名 stem**，平台按此将它们归为一帧。
- **相机名 = `camera/` 后那一段**,如 `front` / `left` / `CAM_FRONT`。
- `calib/camera/<cam>.json` 对该相机的所有帧通用。
- 角色目录还识别常见别名：`lidar_point_cloud_*`、`velodyne` 和 `points` 视为 lidar，`camera_image_*`、`image` 和 `cam` 视为 camera，`calibration` 视为 calib。别名只用于路径角色识别，不改变入库后的 Scene/Task 合同。

真实例子(SUSTechPOINTS 示例 scene):

```
pc-scene-dev/lidar/000970.pcd
pc-scene-dev/lidar/000971.pcd
pc-scene-dev/camera/front/000970.jpg
pc-scene-dev/camera/left/000970.jpg
pc-scene-dev/calib/camera/front.json
pc-scene-dev/calib/camera/left.json
```

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/pointcloud-dir-layout.png — 单 scene vs 多 scene 目录树并排对比(建议矢量示意图而非真截图);左:角色目录布局 lidar/camera/calib → 单 scene,右:顶层多场景名子目录 → 多 scene。[manual] -->

### 顶层多子目录 = 多 scene

当 ZIP / dataset 顶层**不是**角色目录(`lidar/` `camera/` `calib/` `image/` `video/`)、而是若干"场景名"子目录、每个子目录里再各含自己的 `lidar/ camera/ calib/` 时,平台判定为**多 scene**,每个顶层子目录建成一个独立 scene:

```
nu-mini-multi/                              # dataset(storage key 前缀)
├── scene-0061/                             # → scene "scene-0061"
│   ├── lidar/
│   │   ├── scene-0061_000000.pcd
│   │   └── scene-0061_000001.pcd
│   ├── camera/
│   │   ├── CAM_FRONT/scene-0061_000000.jpg
│   │   └── CAM_FRONT_LEFT/scene-0061_000000.jpg
│   └── calib/
│       └── camera/
│           ├── CAM_FRONT.json
│           └── CAM_FRONT_LEFT.json
└── scene-0103/                             # → scene "scene-0103"
    ├── lidar/
    │   └── scene-0103_000000.pcd
    ├── camera/
    │   └── CAM_FRONT/scene-0103_000000.jpg
    └── calib/
        └── camera/
            └── CAM_FRONT.json
```

> 帧 stem 使用 `<scene_name>_<6 位序号>`（例如 `scene-0061_000000`），不要只用序号。帧分组以**文件名 stem 作帧键**。多个 Scene 都使用 `000000` 时会发生跨 Scene 键冲突并漏建 Task，所以要把 Scene 名加到帧名前面。nuScenes 转换脚本也按这个规则命名。

scene 边界与 `frame_index` 的概念详见 [Scene + frame_index 跨 task 帧序列地基](/dev/concepts/scene-and-frame-index)。要点:

- ZIP 导入新增 Item 后，**只有在数据集没有 Scene 且没有既有 Scene 分配时才自动推断 Scene**（角色目录布局对应一个 Scene，名称取 Dataset 名）。推断会同时读取数据集的历史 Item 和本次 ZIP 新增 Item。
- **一个 dataset 可以装多个 scene**;scene 边界由 importer / 目录布局决定。
- scene 内 `frame_index` 决定跨帧导航(`Shift+→` 延续、邻帧框叠加)的顺序;跨 scene 不串(scene-A 末帧的"下一帧"为空,不会跳到 scene-B 首帧)。
- 对象桶扫描只补建 DatasetItem，不推断或断言 Scene。因此扫描不会让原本没有 Scene 的数据集自动变成时序数据集。
- 管理员也可通过认证 API 客户端调用 `PATCH /api/v1/scenes/{id}` 修正 scene 名称或来源字段；成功响应在事务提交后返回，后续 `GET` 可读取已保存结果。可附带 UUID `Idempotency-Key`，响应中断后用原键和原内容确认结果。同一 dataset 内重名返回 `409` 且不应用本次其他字段更改；省略字段或传 `null` 表示不修改，`source_metadata: {}` 则清空该字段。

> ⚠️ **不要**把 `lidar` / `camera` / `calib` / `image` / `video` 用作顶层 scene 目录名——它们是角色目录的保留名,会让多 scene 启发式误判为单 scene。

### 时序数据集声明与 scene 模式项目

入库后**含有 scene 的数据集**即"时序数据集",可在数据集列表用 `GET /api/v1/datasets?has_scenes=true` 筛出。该状态由 scene 行实时派生(`has_scenes`),不需要手动维护。

数据集导入向导的「基本信息」步骤提供「声明为时序数据集（Scene）」开关，也可通过导入脚本或 API 字段 `is_temporal` 设置。ZIP 导入会用这项声明做早失败检查：只有 ZIP 会自动推断 Scene；如果请求无法得到可接受的 Scene，数据库事务会回滚并提示检查目录结构。扫描只补建 Item，不推断或断言 Scene。nuScenes 转换脚本会自动设置 `is_temporal` 并创建已开启 Scene 模式的配套项目。

**与 scene 模式项目的关联规则**(对称硬门,API 与界面一致):

- scene 模式项目**只能**关联含 scene 的数据集;普通项目只能关联不含 scene 的数据集。
- 媒体类型必须匹配,且 lidar 与 point_cloud 视作同一媒体 kind(点云数据集存储类型为 `point_cloud`、点云项目媒体类型为 `lidar`,二者归一后匹配)。
- 不匹配时 `POST /api/v1/datasets/{id}/link` 返回 422;该校验只拦新关联,不追溯旧关联。

scene 模式项目的开启与用法见 [项目管理 · scene 模式项目](/user-guide/projects/#scene-模式项目)。

## 上传方式

### A. 浏览器 ZIP 上传(向导)

在数据集导入向导选 **3D 点云** 类型,上传一个 ZIP 包。**打包时必须保留 `lidar/ camera/ calib/` 三级子目录**(v0.14.2 起 ZIP 上传保留 ZIP 内子目录,不再拍平到 basename)。

<!-- TODO(v0.14.18) IMAGE_CHECKLIST: images/datasets/import-wizard-3d-type.png — 导入向导「基本信息」步选「3D 点云」+ 勾「声明为时序数据集」;红框:数据类型选择、时序开关、axis_convention 选择器。[manual] -->

- **默认一个 ZIP = 一个 scene**,除非顶层有多个非角色子目录(见上)。
- 直接调用 ZIP API 时，可以发送 UUID `Idempotency-Key`。遇到网络结果不确定时，使用同一 key 和同一 ZIP 重试，服务会回放第一次完成的结果。更换 ZIP 字节后必须生成新 key。响应会回传 `Idempotency-Key` 请求头。
- ZIP 上限为压缩和展开后的数据各 200 MiB，最多 5,000 个文件条目，单文件不超过 100 MiB，压缩比不超过 100:1，中央目录最多 10,000 条。nuScenes 单 Scene（约 80 MB）通常适合向导。更大的多 Scene 包请使用转换脚本。压缩/展开预算、文件数或压缩比超限会拒绝整包；单文件超限只记录该文件错误。
- 顶层目录如果混用了保留角色名和 scene 名,响应的 `scene_inference_notes` 会提示冲突原因。
- 系统自动跳过 macOS 元数据（`__MACOSX/`）和隐藏文件（例如 `.DS_Store`）。绝对路径、`..` 等不安全路径也计入 `skipped`，但不会出现在逐文件错误列表里。
- 跨子目录同名文件(如 `camera/front/000970.jpg` 与 `camera/left/000970.jpg`)按 **content_hash**(文件字节 MD5)去重,同名不冲突——同帧号跨相机是合法的;字节完全相同的重复文件计入 `deduped`。

`POST /api/v1/datasets/{id}/items/upload-zip` 的成功响应（`200`）形如：

```json
{
  "added": 18,
  "deduped": 2,
  "skipped": 1,
  "errors": [],
  "total_in_zip": 21,
  "linked_tasks": 6,
  "scene_inference_notes": ["[pc-scene-dev] 2 calib items left frame_index=NULL"]
}
```

- `added`：新入库的 DatasetItem 数；`deduped`：按 `content_hash` 命中已有内容而跳过的数量；`skipped`：因规则跳过的数量，例如隐藏文件或不安全路径。该字段只返回整数，不返回文件名列表。
- `total_in_zip` 统计安全解析出的非目录文件条目，包含隐藏条目，但不包含被安全规则拒绝的路径。因此，`skipped` 可能还会计入没有包含在 `total_in_zip` 中的不安全路径。
- `errors` 逐条返回 `{ "name": <ZIP 内路径>, "error": <原因> }`。例如单文件超过 100 MiB、解压失败或对象存储写入失败。文件级错误不会阻断其余合法文件入库。
- `linked_tasks`:为新 item 创建并关联到 scene 模式项目的 task 数。
- `scene_inference_notes`:scene 推断说明,逐条多以 `[scene_name]` 前缀标注属于哪个 scene。一切正常的单 scene 上传通常只会有 calib 项「`N calib items left frame_index=NULL`」这类提示(标定是 scene 级、不参与帧序),甚至为空。顶层同时含保留角色目录与 scene 目录时,这里会出现冲突提示,例如:

  ```
  ZIP 顶层同时包含保留角色目录 (lidar) 与 scene 目录 (scene-0061); 多 scene 顶层目录不要使用 lidar/camera/calib/image/video 等角色名。
  ```

> 警告：如果数据集已声明为时序（`is_temporal`），但 ZIP 完成后没有可接受的 Scene，整个 ZIP 操作会回滚并返回 `422`，提示检查目录结构。原始来源不会被删除；数据库回滚也不会撤销已经写入的新 publication 字节。

### B. 命令行脚本入库

不走向导、直接灌进当前栈(MinIO + DB)。范例见 [`apps/api/scripts/seed_pointcloud.py`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/apps/api/scripts/seed_pointcloud.py)(SUSTechPOINTS 示例)与下文 nuScenes 脚本。

nuScenes 脚本会把 `sample_data` 中的相机像素宽高一并持久化，并保留原始 `.pcd.bin`、相机图、map 以及官方表上下文的 size / SHA-256。对已入库的同名 Scene 重跑时，会幂等回填这些尺寸和可信导出合同，同时保留原有任务和标注。

## 标定 JSON schema

`calib/camera/<cam>.json` 经 `SensorCalibration` 校验,三个字段:

| 字段         | 类型                | 说明                                      |
| ------------ | ------------------- | ----------------------------------------- |
| `extrinsic`  | 16 float(row-major) | lidar → camera 的 4×4 外参齐次矩阵        |
| `intrinsic`  | 9 float(row-major)  | 相机内参 3×3 矩阵 K                       |
| `rect`(可选) | 16 float            | KITTI 风格 4×4 矫正矩阵;非 KITTI 数据不填 |

非 schema 字段(上游 / 厂商夹带的杂键)入库时自动剥除,不会让一份合法标定整体作废。素材准备流程：读 calib JSON 后先 `{k: v for k, v in raw if k in {extrinsic, intrinsic, rect}}` 把未建模键过滤掉,再过 `SensorCalibration` 校验(`extra="forbid"`,只接受三字段);若过滤后仍非法(如长度不对)则 `warning` 跳过该相机、不抛错。原始标定缺少 `rect` 时保持可读取。已有人工标定修订优先，导入不会覆盖它；读取期间素材来源发生变化时，本次迟到结果不会写入。

**前后对比**——以 SUSTechPOINTS 示例 `calib/camera/left.json` 为例,上游夹带了一个非 schema 的 `extrinsic_ok` 键(疑似占位 / 历史外参):

上游原始 JSON(截断):

```json
{
  "extrinsic_ok": [
    0.0008, -0.9995, 0.0321, -0.5, 0.0518, -0.032, -0.9981, 0.0014, 0.9987, 0.0025, 0.0518, -0.1094,
    0, 0, 0, 1
  ],
  "extrinsic": [
    -0.0394, -0.9972, 0.0634, 0.1248, 0.0502, -0.0653, -0.9966, -0.1844, 0.998, -0.0361, 0.0527,
    -0.3944, 0, 0, 0, 1
  ],
  "intrinsic": [1210.06, 0.0, 1022.43, 0.0, 1205.85, 792.54, 0.0, 0.0, 1.0]
}
```

入库后 `DatasetItem.metadata_.calibration`(仅保留三字段,`extrinsic_ok` 被剥除;此例无 `rect` 故不出现):

```json
{
  "extrinsic": [
    -0.0394, -0.9972, 0.0634, 0.1248, 0.0502, -0.0653, -0.9966, -0.1844, 0.998, -0.0361, 0.0527,
    -0.3944, 0, 0, 0, 1
  ],
  "intrinsic": [1210.06, 0.0, 1022.43, 0.0, 1205.85, 792.54, 0.0, 0.0, 1.0]
}
```

一份干净的最小标定(SUSTechPOINTS `front.json`,无杂键、截断):

```json
{
  "extrinsic": [
    -0.9994, 0.033, -0.0039, 0.2049, 0.0025, -0.0419, -0.9991, 0.0014, -0.0332, -0.9986, 0.0418,
    -0.1094, 0, 0, 0, 1
  ],
  "intrinsic": [1210.06, 0.0, 1022.43, 0.0, 1205.85, 792.54, 0.0, 0.0, 1.0]
}
```

## 从其他数据集转换

### SUSTechPOINTS / xtreme1 自家格式

目录布局已符合平台约定,**直接 ZIP 上传**(一个 ZIP 默认单 scene)。

### nuScenes

用配套脚本 [`apps/api/scripts/import_nuscenes_scene.py`](https://github.com/yyq19990828/ai-annotation-platform/blob/main/apps/api/scripts/import_nuscenes_scene.py) 离线转换 + 直接入库。脚本**为每个传入的 scene 各建一个 scene**,sample 顺序赋 `frame_index`:

> ⚠️ **`--scene-tokens` 的命名有误导**:它接受的是 nuScenes `scene.json` 的 **`name` 字段**(如 `scene-0061`),**不是** scene 的 UUID token(那种形如 `cc8c0bf57f984915a77078b10eb33198` 的 40 位十六进制串)。脚本内部用 `{s["name"]: s}` 按 name 查表,匹配不到就报错。落库时:`scenes.name` = 你传入的 name(如 `scene-0061`),而真正的 UUID token 另存到该 scene 的 `source_metadata.scene_token`。所以传 UUID token 会匹配失败——请填 `scene-XXXX` 形式的 name。

```bash
cd apps/api
# 单 scene(PYTHONPATH=. 才能 import app.*,与其他 seed 脚本一致)
PYTHONPATH=. uv run python scripts/import_nuscenes_scene.py \
  --nuscenes-root /data/nuscenes-mini --scene-tokens scene-0061 \
  --dataset-name nu-scene-0061 \
  --frame ego
# 多 scene 共用一个 dataset(--scene-tokens 填 name,逗号分隔)
PYTHONPATH=. uv run python scripts/import_nuscenes_scene.py \
  --nuscenes-root /data/nuscenes-mini --scene-tokens scene-0061,scene-0103,scene-0553 \
  --dataset-name nu-mini-multi
```

> 转换后的平台工作资产位于 `<dataset_uuid>/<scene_name>/{lidar,camera,calib}/...`；可信导出使用的原始资产位于 `<dataset_uuid>/_nuscenes/source/<原相对路径>`。对象前缀与展示名称解耦，同名 Dataset 不会互相覆盖。每个 scene 的平台投影标定取首帧作为工作台基准；导出时则使用逐传感器保留的原始 `calibrated_sensor` 和 `ego_pose`。

脚本只依赖 numpy + Pillow,不需要 `nuscenes-devkit`。数据集下载见 [nuscenes.org/nuscenes#download](https://www.nuscenes.org/nuscenes#download)(选 mini split)。如果 `--dataset-name` 较长,脚本会把内部 `DS-NU-...` / `P-NU-...` display_id 稳定截断并追加 hash,避免超过数据库长度限制;展示名称仍保留原始 `dataset_name`，对象存储前缀则使用不可冲突的 Dataset UUID。

脚本入库的数据集会自动声明为时序数据集(`is_temporal`),并配套创建一个已开启 scene 模式的项目,导入完成即可直接按 scene 分包标注(见上文「时序数据集声明与 scene 模式项目」)。

> **数据库与对象存储不是同一事务**：导入失败时 PostgreSQL 写入会回滚，但已上传或覆盖的 MinIO 对象不会回滚。`apps/api/scripts/reset_datasets.py` 只清除数据库中的数据集、素材、项目关联并解除任务素材引用，不删除 MinIO 对象；不要把它当作对象存储清理命令。

> **逐帧 ego pose 回填(v0.15.0)**:脚本会顺带把每帧的车体位姿(nuScenes `ego_pose.json` 的 ego→global translation/rotation)与 LIDAR_TOP 时间戳落到 `scene_frame_poses` 表,作为跨帧自动化(运动补偿 / 插值)的数据地基。v0.15.0 之前导入的 nuScenes 数据集没有这些行,用回填脚本补:
>
> ```bash
> cd apps/api
> PYTHONPATH=. uv run python scripts/backfill_frame_poses.py \
>   --dataset-id DS-NU-nu-scene-0061 \
>   --nuscenes-root /data/nuscenes-mini
> ```
>
> `--dataset-id` 接受 UUID 或 `DS-NU-*` display_id;脚本按 scene 的 `source_metadata.scene_token` 反查同一份 nuScenes 元数据,幂等可重跑。非 nuScenes 来源(如 SUSTechPOINTS 示例)没有 ego pose 数据,跨帧自动化对这类 scene 自动降级,这是预期行为。

> **坐标系**:v0.14.3 起脚本默认 `--frame ego`,逐点乘 `T_ego_from_lidar` 把 LIDAR_TOP 原始点落到 nuScenes ego(车体)系,并写 `axis_convention=iso_8855`;相机标定同步写为 `cam_from_ego`,投影仍与点云自洽。若需要保留 v0.14.2 的原始 LIDAR_TOP 传感器系点,可显式传 `--frame sensor`,此时脚本写 `axis_convention=apollo` 和 `cam_from_lidar`。
>
> ⚠️ 同一个 dataset 不能混用 `--frame ego` 与 `--frame sensor`;脚本发现已存在 dataset 的 `axis_convention` 与本次模式不一致时会拒绝继续导入。多相机装置的 `sniff-axis-convention` 响应会透出 `per_camera` 和 `agreement`,可用于判断侧/后相机是否与正前相机有分歧。
>
> nuScenes 可信导出只接受 `--frame ego`。历史 Scene 必须对同一来源重跑本导入器，补齐 log / map / sample / sensor 上下文和原始资产后才能通过导出预检；不会用平台 PCD 或默认地图伪造缺失值。

### KITTI

本版本暂未提供 KITTI 转换脚本。社区参考:SUSTechPOINTS 的 `tools/trans_kitti_labels.py`([第三方源码](https://github.com/naurril/SUSTechPOINTS/blob/master/tools/trans_kitti_labels.py))——目录树与本平台不完全一致,作为转换思路参考,不直接照搬。

## axis_convention(坐标系约定)怎么选

不同数据源的 lidar/world 坐标系约定不同,选错会导致 BEV 俯视下车头不朝上。详见 [点云数据集的 lidar 坐标系约定](/user-guide/datasets/lidar-axis-convention)。可用 `POST /api/v1/datasets/{id}/sniff-axis-convention` 端点对样本帧自动嗅探。
