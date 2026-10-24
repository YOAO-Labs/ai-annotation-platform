---
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-24
---

# 导出

标注数据导出为下游训练可用格式。除 VOC 同步返回 blob 外，导出通过后台 job 执行：`POST` 创建 job 返回 `202 {job_id}`，产物ZIP生成后在任务铃按固定保存期限内的预签名URL下载。导出目标可多选，一个 job 产一个 ZIP。

图片项目、批次和 Data Manager 显式任务导出支持原地引用与托管图片混合。含引用的任务在排队时固定实际 Task 清单、任务版本及 Item/Asset/SHA；API Key 还须具备 `tasks:read`，后台持续验证同一个 Key 的身份与权限。原图经统一引用 reader 捕获严格快照，完整核对 SHA-256 后写入 ZIP；实际读取来源时要求真实 worker 的锚点。完整原图缓存的读取边界见[严格内容读取](verified-content.md#完整原图缓存的接入状态)；来源停用或内容绑定变化仍拒绝，不回退预览或按来源路径猜测平台桶。

引用条目在 `images_manifest.json` 中包含 `storage_mode="embedded"`、`archive_path`、`archive_paths`、`asset_id`、`sha256` 和 `size_bytes`，不包含来源 URL。其包内路径为 `images/<dataset UUID>/<item UUID>/<task UUID>/<安全文件名>`；YOLO 图片与标签在对应目标目录下配对。COCO `file_name`、AAP `file_path` 及掩码 manifest 使用同一包内图片路径。托管条目的预签名 URL 指向完整核验后的独立交付副本，清单保存真实 SHA、大小和共同截止。`fetch_images.py` 核对包内图片、已有文件及实际下载字节；托管 URL 过期不会跳过包内 SHA 校验。托管 URL 使用站内相对地址时，脚本需带 `--base-url https://平台地址`；显式平台 origin 不改写签名路径。

引用包不跨 job 复用缓存。上传后的最终短事务重新检查原发起人、项目权限、原 Key、固定内容、显式选择的批次可见性与来源，并与取消串行；通过后才一起提交产物索引、下载链接和完成通知。失败或取消等待真实I/O收尾后关闭快照、清理本任务临时ZIP；独立pending归属和未确认对象保留待核对，不物理删除或返回部分包。到期已发布Output由durable退休账本重试清理，pending不按年龄自动回收。已下载的包及已签发的七天 bearer URL 无法承诺随撤权立即回收。

`POST /async-jobs/{job_id}/cancel` 支持 pending/running 导出；已完成任务返回 409。取消在当前有界读图或上传实际收尾后清理临时资源并保留候选责任，不强杀其他任务。历史队列缺少原授权清单时，含引用的范围仍明确失败；同步 VOC 与预测入口尚未获得引用资格。严格读取合同见[已登记目录引用](verified-content.md#已登记目录引用)。

## 产物归属与固定期限

每个新上传候选以独立随机Output UUID形成对象键，重投递不复用旧候选。Worker写前提交保存端点/桶/精确键、独立ZIP SHA/大小及七天固定截止，再上传并完整回读；ready、缓存和完成结果在最终短事务一起发布。缓存命中也须从保存桶完整验证实际字节，随后重新校验同一Output、当前项目和期限，只签剩余秒数（不做时间对齐或延期）。缺失/损坏副本可重新生成独立候选，旧物理责任仍保留至原期限。

项目、批次、选中任务三个入口均固定原请求的账号/项目和Key证明，任务排队、生成和最终签名期间的撤销、轮换、scope或期限变化都会拒绝发布；不增加新的scope要求。私有output_authority不出现在Job DTO。引用输入仍须原有tasks:read与来源内容证明。旧未完成Job若没有输出授权证明，需要重新发起导出；旧NULL缓存索引不猜桶接管，重导建立新归属。已完成旧Job/通知保存的URL保持原值及原期限，GET不重新签名。

当前输出上限64GiB，完整读取以600秒协作预算和独立字节数约束，分块计算摘要。存储身份或端点不符明确失败，不回退当前桶；未确认关闭的读取permit会阻止退休清理。新内部归属不在公开result中返回，既有object_key/download_url字段继续使用。此能力不开放整个export用途切换。

## 图片独立媒体交付

项目、批次和 Data Manager 选中任务的异步图片导出，在入队事务固定准确范围、Task 顺序、类别／属性／工具、标注、AAP 预测与相关轨迹、尺寸、坐标约定和原件身份。后台序列化使用冻结视图；物理读取与发布前继续核对准确原请求、当前来源和完整输入。新托管图片使用 `images/tasks/<task UUID>/<安全文件名>`，YOLO 标签使用相同层级；同一原件可共享一个交付副本，但每个 Task 的绑定独立验证。

托管原件沿已登记 Dataset 或 TaskOriginal 位置完整读取，不猜当前桶；未知历史需先显式核验登记。媒体和 ZIP 同事务发布，共用 bundle 创建起168小时截止和一次3600秒租约。纯托管缓存核对完整输入摘要、保存位置的 ZIP 与全部媒体，复用首次截止；含引用时不跨 Job 缓存。纯引用与空项目同样固定输入并保留独立 ZIP／bundle 责任。

本合同最多20000个图片 Task（含引用），托管单件256MiB，唯一托管原件加各引用声明累计20GiB，冻结 JSON256MiB，相关标注和预测各100000行；Data Manager 选中200题限制不变。引用继续64MiB／3200万像素限制，已有 mask 和 ZIP 限制继续执行。超限在有界收集时明确拒绝，这些是交付预算，不是性能指标。旧未完成图片 Job 缺少 `image_media` 证明时需重新导出；私有证明不出现在公开 Job DTO，同步 VOC 保持原合同。

AAP JSON 中可移植 mask 对象累计最多400万条 RLE 游程和256MiB JSON；Indexed PNG 每图最多255个实例、累计400万条游程，实例数量在读取 mask 前检查。超限需拆分导出；Binary PNG 和 Brush 仍逐实例处理。

## 点云独立媒体交付

点云项目的 `kitti`、`pointmask`、`coco-multicamera` 通过项目或批次入口登记冻结输入；selected-task 仍不支持这些场景格式。同步预检只读取可信登记及格式配置，不下载原件。Worker 执行时重新核对原请求凭据和冻结输入，逐文件条件读取、核对完整 SHA／大小、保存并回读独立副本，最后复核输入和权限才发布 ZIP 与完成结果。冻结后的修改需要重新发起导出，不能混入旧作业。

媒体 manifest 保留原逻辑路径，包含独立副本的 `presigned_url` 和 `expires_at`。KITTI／Point Mask 使用 `sha256`、`size_bytes`；Multi-camera COCO 保留 `source_sha256`、`source_file_size`。链接使用部署配置的公共存储网关；相对路径需要给下载脚本传入 `--base-url https://平台地址`，既有绝对签名链接保持原值。KITTI 只复制所选相机与主 PCD；Point Mask 复制所有相机与主 PCD；Multi-camera COCO 复制所有相机且不强制 PCD。主 PCD 支持 ASCII、binary、binary_compressed；Point Mask 使用实际原始记录数，包含非有限记录的原序号，生成精确四字节标签。原件身份不明、同一路径冲突或超出预算均拒绝，不按当前桶猜测读取。

每次 HTTP 请求预分配独立 bundle、ZIP UUID 和媒体 UUID，Worker 一次 claim、固定 3600 秒，不续约或接管。ZIP 和媒体共用登记时起 168 小时的截止；缓存同时完整验证保存的 ZIP 与媒体，只签剩余窗口。损坏或缺失的缓存重新生成独立交付，不覆盖旧对象。已发布媒体保留到固定截止，即使 Job、项目或缓存索引消失；未发布的关联 ZIP／副本在明确尝试结束后才进入可重试回收。任何相关未知存储操作继续阻止删除。

私有输入、清单和 `lidar_media` 证明不出现在公开 Job DTO。项目预检及实际导出仍沿用既有 `export.annotations` 能力；批次预检保留项目可见性要求，不新增 `tasks:read`／`annotations:read`。旧未完成点云 Job 缺少媒体合同会失败，需要重新导出。恢复映射只改变保存桶和身份，保留 ZIP 内历史媒体 URL 与原期限，并失效准确关联的缓存；恢复后重新导出生成新的链接。NuScenes 的 raw／map 和全部相机同样进入此合同；单独与混合导出共用冻结输入、完整副本核验与固定截止。辅助文件缺少独立归属时明确拒绝，历史 JSON 路径和摘要不构成归属证明。

## 导出数据契约

新 ZIP 包含 `data_contract.json`，单文档 JSON 在自身保留等价元数据。每个目标分别记录 `task`、`media_type`、`format`、可用任务和来源；多目标 ZIP 的 `contracts[]` 不能简化成全局单一任务。普通标注导出标记 `training_qualified=false`，训练仍须经过专门的数据预检与严格几何桥接。

AAP 的格式标识为 `aap`；视觉任务使用 `detect`、`segment`、`classify`、`pose`、`obb`。混合 AAP 保留全部原标注，`task` 可为空、`available_tasks` 列出候选；存在尚未映射的工具时不能将整个包声明成单一检测任务。COCO 的原生 `segmentation`、`keypoints` 等字段不改名。

视频 JSON 的任务 `source`、AAP 的视频块和 ZIP manifest 保留原项目 `video_sampling`/`video_collaboration`、原始源 key、DatasetItem/内容身份、`resolved_step`、绝对 `source_frame_indices` 和逐帧 `source_sample_ids`。帧网格始终锚定源视频第 0 帧，范围导出只裁网格，不从范围起点重编号。标签展开方式与采样网格相互独立。

## 触发导出

```http
POST /api/v1/projects/{project_id}/export?targets=coco&targets=yolo-det&include_attributes=true
POST /api/v1/projects/{project_id}/batches/{batch_id}/export?targets=coco&include_attributes=true
```

视频项目可在可选 JSON 请求体中限定单个 task 的连续范围。帧号与 segment 两端都包含：

```json
{
  "scope": {
    "task_id": "...",
    "selection": { "kind": "frames", "from_frame": 120, "to_frame": 359 }
  }
}
```

Segment 模式把 `selection` 改为
`{"kind":"segments","start_segment_id":"...","end_segment_id":"..."}`，并自动包含两端之间按索引连续的全部 segment。task 必须属于当前项目；批次导出时还必须属于当前批次。图片或点云项目传 `scope`、范围越界或 segment 不连续均返回 422。省略请求体时保持完整项目 / 批次导出。

点云 KITTI 导出在请求体中显式选择相机 role：

```json
{
  "lidar": { "kitti_camera_role": "camera_front" }
}
```

创建任务前可调用 `POST /projects/{project_id}/exports/lidar:preflight`；批次使用 `POST /projects/{project_id}/batches/{batch_id}/exports/lidar:preflight`。请求体为 `{"targets":["kitti"],"lidar":{"kitti_camera_role":"camera_front"}}`；Multi-camera COCO 使用 `{"targets":["coco-multicamera"]}`，不需要选择单一相机。响应的 `camera_roles` 用于相机选择，`issues` 按 task / frame / camera 返回稳定 code；`ready=false` 时正式导出返回 409，且不会创建后台任务。

参数：

| 参数                   | 取值                                                 | 说明                                                                                                                  |
| ---------------------- | ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `targets`              | 可重复，多选                                         | 图片目标、`aap_json`、视频专属目标，以及点云 `coco-multicamera` / `kitti` / `nuscenes` / `pointmask`；`voc` 仅可单选  |
| `include_attributes`   | `true` / `false`                                     | 是否携带 `annotation.attributes` 与 `project.attribute_schema`                                                        |
| `video_frame_mode`     | `keyframes` / `all_frames`                           | 仅 `video-track` 生效；默认 `keyframes`                                                                               |
| `axis_frame`           | `iso` / `source`                                     | 仅影响导出中的 `box_3d` 几何；默认 `iso`（平台归一化 ISO 8855 PSR），`source` 反向映射回数据集 `axis_convention` 源系 |
| `video_overlap_policy` | `error` / `z_order` / `larger_area` / `smaller_area` | DAVIS / YouTube-VOS palette PNG 的实例重叠策略；默认阻止                                                              |
| `mots_frame_base`      | `0` / `1`                                            | MOTS 输出帧号基准；默认 0-based                                                                                       |

非 VOC 目标返回 `202 {job_id}`；勾选多个目标时产物 ZIP 内各目标落 `{target}/` 子目录，单目标落包根。`video-track` 项目只接受视频目标，选图片目标会返回 400。

导出会按项目当前类别 / 属性定义做兜底收敛：`class_name` 已不在当前类别集合内的标注不会进入任何导出格式；`annotation.attributes` 只保留当前 attribute schema 内的用户属性 key。这样即使尚未执行 cleanup，导出文件的 schema 与 data 也保持一致。

## 格式说明

| 目标                  | 适用                                                                                               |
| --------------------- | -------------------------------------------------------------------------------------------------- |
| **coco**              | COCO `annotations.json`：bbox + segmentation(polygon/mask) + keypoints(skeleton) + group_id        |
| **yolo-det**          | YOLO 检测 txt（矩形框）+ classes.txt，每图一文件                                                   |
| **yolo-obb**          | YOLO 旋转框 txt（rotated_bbox 四角）                                                               |
| **yolo-seg**          | YOLO 分割 txt（polygon / mask 归一化多边形）                                                       |
| **aap_json**          | 平台原生无损中间格式（双数组 annotations / predictions）                                           |
| **yolo-frames-det**   | `video-track` 专用逐帧 YOLO 检测集，按采样网格抽帧，合并 `video_bbox` 与 `video_track_bbox` 摊平框 |
| **yolo-frames-seg**   | 视频逐帧 polygon 分割标签；bbox / polyline / mask 跳过                                             |
| **coco-frames-seg**   | 视频标准 COCO instance segmentation；polygon 与 RLE mask                                           |
| **davis**             | 视频 Full-Resolution palette PNG；对象 ID 1–254、255 保留 void                                     |
| **youtube-vos**       | 稀疏 Mask 关键帧 palette PNG + `meta.json`                                                         |
| **mots**              | 六列 MOTS 文本，Mask 使用 compressed COCO RLE                                                      |
| **mot**               | MOT 16/17/20 tracking 评测格式，按采样网格重排帧号                                                 |
| **kitti**             | KITTI Tracking 2D label 文本                                                                       |
| **coco-multicamera**  | 全部相机持久化人工 bbox 的合并 COCO、可信媒体 manifest 与关系报告                                  |
| **kitti（点云）**     | 所选相机的 KITTI `label_2`、真实 calibration 与可见性跳过报告                                      |
| **nuscenes（点云）**  | 官方 13 表关键帧子集、原媒体 manifest 与回源脚本                                                   |
| **pointmask**         | 点云逐点 little-endian uint32 类别标签                                                             |
| **voc**               | Pascal VOC XML（仅同步单选）                                                                       |
| **video tracks json** | `video-track` 专用 JSON（`video_json` 目标）                                                       |

COCO / YOLO 会按各自能消费的几何映射。COCO 对图片 `raster_mask` 输出标准 RLE segmentation，并从像素内容计算 bbox 与 area；polygon / multi_polygon 继续输出多边形 segmentation。其余不匹配几何会跳过，COCO 跳过数记在 `info.skipped_annotations`。

## 视频轨迹导出

`video-track` 项目通过 `targets=video_json` 返回专用 Video Tracks JSON。响应顶层包含：

```json
{
  "export_type": "video_tracks",
  "frame_mode": "keyframes",
  "project": { "id": "...", "display_id": "P-1", "type_key": "video-track" },
  "categories": [{ "id": 0, "name": "car" }],
  "tasks": [{ "id": "...", "display_id": "T-1", "video_metadata": { "fps": 25 } }],
  "tracks": [
    {
      "annotation_id": "...",
      "task_id": "...",
      "track_id": "trk_car",
      "class_name": "car",
      "keyframes": [
        {
          "frame_index": 0,
          "bbox": { "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
          "source": "manual",
          "occluded": false
        }
      ]
    }
  ],
  "keyframes": [],
  "video_bbox": [],
  "video_metadata": {}
}
```

`video_frame_mode`：

- `keyframes`：只输出持久化关键帧，适合备份、质检和后续可编辑 ingest。
- `all_frames`：在每条 track 的 `frames[]` 中展开逐帧 bbox。后端按相邻有效关键帧线性插值，`outside` 段阻断跨段插值、不输出 bbox。缺少 `frame_count` 时用最大已标注帧兜底。

`include_attributes=false` 时，视频 JSON 不输出 `project.attribute_schema`，也不输出 track / legacy `video_bbox` 的 `attributes`。

部分导出只读取一个 task：单帧 geometry 按闭区间过滤，track 保留范围内原始关键帧与 outside，并在可见区间边界补齐可解析状态。Video JSON 顶层、AAP task 的 `video` 字段和 ZIP `manifest.json` 都写入规范化后的 `export_scope`。逐帧格式保持项目的全局采样相位，再把选中帧密集编号；`grid_source_frames` 保存输出帧到源帧的映射。

单帧 OBB 与关键点同样写入兼容字段 `video_bbox`：OBB 行保留 `type/frame_index/cx/cy/w/h/angle`，关键点行保留 `type/frame_index/points[{x,y,v}]`。AAP JSON 使用原始 geometry。专业视频目标没有这两种单帧几何的表示时，预检返回 `unsupported_geometry`，不会把 OBB 退化成外接框或丢弃关键点。

`targets=yolo-frames-det` 会生成逐帧 YOLO 检测集：

- 帧集来自项目采样网格 `derive_sampled_frames(frame_count, step)`，不是全帧，也不是仅关键帧。
- `video_bbox` 单帧框只在 `frame_index` 落网格时输出。
- `video_track_bbox` 先用 `resolved_track_frames(..., frame_mode="all_frames")` 展开，再筛采样网格；`outside` 区间不输出框。
- ZIP 内写 `labels/{sequence}/{frame:06d}.txt`、`classes.txt`、`data.yaml`、`manifest.json`、`fetch_videos.py`、`fetch_frames.py`。帧图不打包，`fetch_frames.py` 会抽到 `images/{sequence}`，与 label 路径对齐。

schema 语义见 [视频标注工作台](/dev/concepts/video-annotation-workbench)。

## 3D box 坐标系（`axis_frame`） {#export-axis-frame}

点云项目导出的 `box_3d` 几何默认按平台内部 **ISO 8855** 归一化 PSR 输出（`axis_frame=iso`）。当数据集声明了非 ISO 的 `axis_convention`（见 [lidar 坐标系约定](/user-guide/datasets/lidar-axis-convention)）且下游需要源系坐标时，加 `axis_frame=source`：

- 目前仅 `aap_json` 目标携带 `box_3d`，会对标注与预测的 box 几何调用 `unapply_to_psr` 反向映射回该数据集的源系约定；其它格式与非 box 几何不受影响。
- `source` 模式下每个被转换的几何额外带 `axis_frame: "source"` 与 `axis_convention: "<source>"`，便于消费方识别坐标系。
- `axis_frame` 计入导出缓存 key：`iso` 与 `source` 是两份独立缓存产物。

KITTI 不消费 `axis_frame`。它会先按每帧数据集的 `axis_convention` 把平台 ISO 框反变换回源 LiDAR 坐标，再应用 `lidar.kitti_camera_role` 对应的真实内外参，固定输出 KITTI camera 坐标。缺轴约定、相机帧、标定或图像宽高均由预检阻止；不生成 identity calibration、`.unverified` 文件或负数 bbox。完全不可见对象进入 `export_report.json`。

`targets=coco-multicamera` 不依赖主点云轴约定或完整 Scene。它为每个相机 link 生成一个 COCO image，只导出持久化人工 bbox，并保留无框图作为负样本；不会用 3D 投影补框。预检会核对 link、媒体类型/宽高/对象大小、归一化 bbox、类别及 2D 成员与活跃 3D 成员、SceneTrack 的身份闭合。标定 digest 过期不会阻断独立 2D 真值，而是在 annotation 扩展字段和报告中标记 `stale`。单次范围最多 20,000 张相机图、100,000 个人工框、20 GiB 媒体总量和单图 256 MiB。worker 流式计算媒体 SHA-256；`fetch_media.py` 只会在大小与 SHA-256 校验成功后原子落盘。

`targets=nuscenes` 只接受由 nuScenes 导入器以 ego / ISO 模式保全的完整 Scene。项目或批次预检会校验原始 scene / sample / sensor / pose / map 合同、标定指纹、原始资产指纹和完整帧范围；任一不符时返回 409 且不创建导出任务。单次范围最多包含 1000 帧和 30000 个有效 3D 框，单帧 PCD 不超过 256 MiB、总 PCD 不超过 4 GiB，精确框内点测试不超过 1 亿次；超限同样在预检阶段拒绝。产物是官方 devkit 可加载/查询的关键帧子集，不代表官方 benchmark 兼容。原始五通道 `.pcd.bin`、相机和地图逐字节复制到已保存的交付位置，`media_manifest.json` 保留原 `rel_path`、`kind`、`source_file_size`、`source_sha256`；平台 PCD 仅供点框统计。所有媒体共用 256MiB／件、21000 件／24GiB 总上限。删除 Dataset 不影响尚未到期的独立交付责任，辅助原件归属继续保留。

## 权限

| 角色          | 能否导出         |
| ------------- | ---------------- |
| viewer        | ❌               |
| annotator     | ❌               |
| reviewer      | ❌               |
| project_admin | ✅（自己的项目） |
| super_admin   | ✅（任何项目）   |
