---
audience: [project_admin, super_admin]
type: how-to
status: stable
last_reviewed: 2026-08-15
---

# 外部预测导入 / 导出

将外部模型（客户自训模型、第三方推理服务、离线脚本）的预测结果导入项目，供人工审阅，作为**待采纳预测**出现在工作台 AI 候选里；以及反向把项目里的标注 / 预测导出成 COCO / YOLO / 原生 JSON 等格式。

本页只讲「外部预测进出本平台」这条链路。如果你要的是把**原始数据**导入数据集，图像见 [图像数据集导入](./import-images)、点云 / 多模态见 [点云 / 多模态数据集导入格式](./import-formats)；模型服务自动执行预标的流程见 [AI 预标注](../projects/ai-preannotate)。

## 导入外部预测

### 入口

两个入口都打开同一个「导入预测」向导：

- **AI 预标注页**：进入某个项目详情后，头部右侧的 **「导入预测」** 按钮。
- **项目总览 / 项目管理**：在项目列表或卡片的 **「更多操作」** 菜单中选择 **「导入预测」**。项目管理员只能操作自己负责的项目，超级管理员可操作所有项目。

项目总览 / 项目管理中的入口**不要求项目已绑定模型服务**；AI 预标注页只列出已启用模型服务的项目。外部预测通过上传文件导入，不经过平台推理。导入的预测行 `source='external_import'`、`ml_backend_id` 为空，与模型服务跑出来的预标在来源上区分开。

![导入预测弹窗（原生 JSON / COCO / YOLO 格式选择 + 文件上传 + 替换开关）](../images/projects/prediction-import-wizard.png)

<DocsVideo
  src="/media/ai/prediction-import.mp4"
  poster="/media/ai/prediction-import-poster.webp"
  alt="导入与真实车辆对齐的预测结果，在工作台检查候选并采纳为正式标注"
  caption="导入预测不会直接覆盖人工标注；候选需在工作台复核并采纳后才成为 Annotation。"
/>

### 向导流程

向导分三步：

1. **选择文件**：选格式、上传文件、（可选）填写「默认模型版本」、设置「替换已有外部导入预测」开关。
2. **预览**：以 `dry_run` 跑一遍校验路径但不入库，显示将写入 / 跳过 / 错误的数量与错误明细。
3. **完成**：确认后真正写入，再次显示汇总。

只有当预览结果「将写入」大于 0 时，「确认导入」按钮才可点。

「默认模型版本」对应请求中的 `model_version`。原生 JSON 中，每条预测自己的 `model_version` 非空时优先使用该值；缺失或为空时使用向导填写的默认版本。COCO 和 YOLO 的预测使用向导填写的默认版本；不填写时版本可为空。

### 三种格式

向导支持三种输入格式，对应 `format=native_json|coco|yolo`。

#### 原生 JSON {#aap-json-平台无损}

同一份 YOAO 原生 JSON 可以包含待审预测（`predictions[]`）和正式标注（`annotations[]`）。这个向导只导入待审预测。如果有效文件只有正式标注，预览显示将写入 **0** 条预测，不表示文件为空；要导入这些标注，请用 `yoao annotations import`。最小预测文件示例：

```json
{
  "schema_version": "2.0",
  "format": "yoao",
  "exported_from": { "platform": "yoao" },
  "tasks": [
    {
      "task_match": { "file_path": "animals/cat/001.jpg" },
      "predictions": [
        {
          "class_name": "cat",
          "confidence": 0.92,
          "geometry": { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.25 }
        }
      ]
    }
  ]
}
```

要点：

- **保留文件原有的版本和身份。** 新文件使用 `schema_version: "2.0"` 和 `yoao` 身份；历史 `1.x` 文件按原样读取，不要手动改成 `yoao`。若文件被拒绝，先按提示的行、列或字段位置修正，再预览。见[导入接口的错误说明](../../api/guides/import#响应)。
- **`task_match` 必须给 `display_id` 或 `file_path` 至少一个**，两者全省略则该 task 块的预测全部跳过。`display_id` 全局唯一最稳；`file_path` 在项目内匹配，带子目录时会自动吸收 dataset 名前缀（库内 `task.file_path` 形如 `{dataset}/animals/cat/001.jpg`，外部相对路径 `animals/cat/001.jpg` 也能命中）。
- 图片预测支持 `bbox` / `polygon` / `multi_polygon` / `polyline` / `rotated_bbox` / `keypoint`；视频预测支持 `video_bbox` / `video_track_bbox` / `video_track_polygon` / `video_track_polyline` / `video_track_mask`。视频 task block 需声明 `media_type: "video"`，任务必须有可校验的源视频帧数；越界帧、重复关键帧或非法 outside 范围会显示在预览错误中。
- 视频帧数以**当前原视频已完成的解析结果**为准，不使用旧汇总值。原视频变化或解析尚未就绪时，先完成原件核验与视频处理，再导入预测。
- 视频 Mask 正文放在 `mask_objects`，向导会在预览时校验引用、尺寸和内容；`dry_run` 不会写 Prediction 或对象存储。正式导入后，Mask 仍是待审候选，不会直接成为标注。
- 一条 `predictions[i]` 可以用 `shapes[]` 把多个 shape 合并写入同一条预测；`shapes` 与单 `geometry` 同时存在时 `shapes` 优先。同一 entry 的 shapes 必须同属 bbox、region（polygon / mask）或 polyline 中的一个工具单位，不能混合。

坐标用归一化 `[0, 1]`。完整格式规范（含 `tool_bindings` / `attribute_schema` / `mask_objects` / `scene_tracks` / 各几何字段）见 [导出格式 · 原生 JSON](../reference/export-formats#aap-json-1-5-无损)，本页不重复展开。

#### COCO Detection

标准 COCO Detection 子集（`images[]` + `annotations[]` + `categories[]`），`bbox` 为 `[x, y, w, h]` 像素坐标。最小结构：

```json
{
  "images": [{ "id": 1, "file_name": "animals/cat/001.jpg", "width": 1280, "height": 720 }],
  "categories": [{ "id": 1, "name": "cat" }],
  "annotations": [{ "image_id": 1, "category_id": 1, "bbox": [128, 144, 384, 180], "score": 0.9 }]
}
```

用 `image.file_name` 匹配 `task.file_path`。`images[i]` **缺 `width/height` 时**，可在向导里填**全局默认宽高**用于归一化；文件内已有尺寸时优先用文件内尺寸。向导里宽、高必须**同时**填写且为正整数，否则不放行。

#### YOLO（zip）

选择一个 zip 包，包内需含：

- `classes.txt`（每行一个类名）**或** `data.yaml`（`names:` 字段），用于把类别索引映射成类名；都缺且项目也没有可回退的类别顺序时整包报错。
- 每图一个 label `.txt`（空 label 文件视为该图无预测，对平台导出的 YOLO 包可往返）。

label 路径按**文件 stem** 匹配任务：归一化时会剥掉直到最后一个 `labels/` 目录段、再去掉扩展名，例如 `labels/animals/cat/001.txt` 匹配项目内 `animals/cat/001.jpg`（或 `.png` 等）。同名跨目录或跨扩展名出现歧义时不自动猜测，会在预览 errors 里提示。

向导里用 **YOLO 变体** 选择 `det`（检测）/ `obb`（旋转框）/ `seg`（分割）：

| 变体  | label 行格式                         | 落地几何                         |
| ----- | ------------------------------------ | -------------------------------- |
| `det` | `cls cx cy w h`（归一化）            | `bbox`                           |
| `seg` | `cls x1 y1 x2 y2 …`（≥3 点，归一化） | `polygon`                        |
| `obb` | `cls x1 y1 … x4 y4`（四角，归一化）  | `rotated_bbox`，退化时 `polygon` |

##### YOLO OBB 专项

`obb` 变体的四角是归一化坐标，需要**图像真实像素尺寸**才能还原成平台的旋转框（中心 + 宽高 + 角度）。因此被匹配到的任务对应的 `dataset_item` 必须有有效的 `width/height`，否则该图整体跳过并提示 `dataset item width/height required for YOLO OBB`。还原时若四角不构成矩形（边长不相等或不正交，容差约 3%），会**降级为 `polygon`** 原样保留四个角点。`det` / `seg` 用归一化坐标直接落地，不需要图像尺寸。

### 替换已有外部导入预测

「替换已有外部导入预测」开关**默认开启**。开启时，导入会清除本次涉及任务中已有的 `source='external_import'` 预测，再写入新预测；其他任务的预测不受影响。清理只按任务和来源区分，不按预测是否已采纳区分；模型服务预测和正式标注不会被删除。需要在旧导入基础上**追加**而非替换时，取消勾选即可。

原生 JSON 和 COCO 在任务有可写入的预测时才清理该任务的旧外部预测。YOLO 的非空标签文件匹配到任务后，会在逐行校验前清理；即使该文件的预测行最终都无效，旧外部预测也可能被清除。请先检查预览中的错误，再确认导入。

命令行与向导的默认行为不同：`yoao predictions import` 追加待审预测，不替换旧候选。先加 `--dry-run` 查看预计写入数量；要从同一原生 JSON 导入正式标注，用 `yoao annotations import`。示例和部分失败的处理见 [CLI 导入与清理](../../dev/sdk/cli#yoao-predictions)。

### 导入结果

预览与完成都给出三项汇总：**写入 / 跳过 / 错误**。错误明细列出每条的 `task_match` 与原因（如 `task not found in project`、`unsupported geometry kind`、`ambiguous task file stem`），最多展示前若干条。

向导支持**一次选多个 JSON 文件**（YOLO 限单个 zip），后端作为同一批处理：「替换」的清理在整批内按 task 去重，多文件命中同一 task 只清一次，各文件结果合并后再汇总。

Mask 存储冲突或暂不可用（409／503），以及未预期的执行异常，会中止整批导入并回滚本批预测更改：已有外部预测不会因失败的替换而丢失，前面文件已处理的预测也不会部分提交。解决错误原因后再重新导入。普通文件格式错误仍会在多文件结果中记录并跳过；不要把这类结果明细与整批执行失败混为一谈。

## 清理预测

需要撤销导入、或在重新整理文件前清空旧候选时，用「清理预测」按来源批量删除当前项目的预测。

### 入口

项目总览 / 项目管理中，打开项目的 **「更多操作」** 菜单，选择 **「清理预测」**。项目管理员只能操作自己负责的项目，超级管理员可操作所有项目。

![清理预测弹窗三种来源范围 + 模型服务风险确认复选框](../images/projects/prediction-purge-modal.png)

### 来源范围

弹窗按 `source_scope` 提供三个选项：

| 来源范围                              | 含义                                                   |
| ------------------------------------- | ------------------------------------------------------ |
| **外部导入预测**（`external_import`） | 默认项，只删本页导入的预测                             |
| **模型服务预标**（`ml_backend`）      | 删平台模型跑出的预标候选，清理后需重新运行模型才能恢复 |
| **全部预测**（`all`）                 | 同时删除外部导入与模型服务预标                         |

弹窗打开时会先以 `dry_run` **统计将删除的数量**（按来源拆分显示模型服务 / 外部导入 / 其他来源），数量为 0 时无法提交。

选 **模型服务预标** 或 **全部预测** 属于高风险操作，需**额外勾选确认复选框**才能点「确认清理」；选「外部导入预测」无需额外确认。任何来源范围下，**已采纳的人工标注都不会被删除**——清理只动 `predictions` 表里的候选行。

## 导出标注 / 预测

反向链路：把项目里的标注与预测导出成外部格式（COCO / YOLO / 原生 JSON 等）。

- 入口在 Dashboard 项目卡片 / 列表行右下角 `⋮` → **「导出标注数据」**，打开导出弹窗，可选导出范围（整个项目 / 单个批次）与一个或多个目标格式。
- 原地引用图片经完整摘要核验后随 ZIP 交付，混合包中的托管图片继续回源；运行中的导出支持取消。
- 点击「开始导出」后任务进入后台队列；可在顶栏「后台任务」查看进度。完成行会列出项目、目标格式、ZIP 文件数与大小，并提供「下载」入口。
- 接收请求不等于导出完成；只在任务显示「已完成」且出现「下载」后取包。重新导入原生 JSON 时，使用 ZIP 中的 `annotations.json`，先核对预览数量与错误，再确认写入待审预测；替换开关只影响命中任务已有的外部导入预测，不删除模型服务产生的预测。
- 原生 JSON 是 YOAO 原生交换格式，能把 `predictions[]` 连同 `confidence` / `model_version` 带出，也能用 `annotations[]` + `mask_objects` 迁移视频栅格 mask track，并用 `scene_tracks[]` 保留 3D 对象身份、存在模式、存在区间和时间角色，常用于跨实例迁移与离线回填闭环。

<DocsVideo
  src="/media/datasets/background-export-download.mp4"
  poster="/media/datasets/background-export-download-poster.webp"
  alt="同时配置 COCO 与 原生 JSON 导出，在后台任务中查看进度并下载完成的 ZIP 产物"
  caption="导出在后台执行；完成后可核对格式、文件数和大小，再下载 ZIP 产物。"
/>

各格式的字段映射、目录结构、回源脚本等细节见 [导出格式参考](../reference/export-formats)。导出后按 task / scene 维度核对任务时，可用 [Data Manager](../projects/data-manager) 的「AI 待审」视图快速定位尚未处理的检测 shape 或视频追踪候选。
