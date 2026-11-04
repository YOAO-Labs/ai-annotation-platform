---
audience: [dev]
type: reference
since: v0.10.15
status: stable
last_reviewed: 2026-08-12
---

# 外部预测导入端点

允许客户将**外部模型**（不通过平台模型服务运行的模型）生成的预测导入平台，供人工审阅，进入"AI 预标 → 人工修正 → 导出"工作流。

支持三种输入格式：**COCO Detection**、**YOLO zip** 与**原生 JSON**（YOAO 原生交换格式）。

## 端点

```http
POST /api/v1/projects/{project_id}/predictions/import?format=native_json&dry_run=false
Content-Type: multipart/form-data

file=<JSON file or YOLO zip>
model_version=<optional fallback string>
overwrite_existing=<true|false default true>
image_width=<optional COCO fallback width>
image_height=<optional COCO fallback height>
```

参数：

| 参数                           | 位置  | 类型                              | 必填 | 说明                                                                                                              |
| ------------------------------ | ----- | --------------------------------- | ---- | ----------------------------------------------------------------------------------------------------------------- |
| `format`                       | query | `native_json` \| `coco` \| `yolo` | 是   | input 格式. 默认 `native_json`.                                                                                   |
| `yolo_variant`                 | query | `det` \| `obb` \| `seg`           | 否   | 仅 `format=yolo` 生效,默认 `det`.                                                                                 |
| `dry_run`                      | query | bool                              | 否   | true 时走全部校验路径但**不入库**, 供前端 wizard 预览.                                                            |
| `file`                         | form  | File                              | 是   | 原生 JSON / COCO 为 JSON 文件,可重复提交多个 `file` 字段; YOLO 为 zip 包.                                         |
| `model_version`                | form  | string                            | 否   | 原生 JSON 内 `model_version` 缺失时的兜底值.                                                                      |
| `overwrite_existing`           | form  | bool                              | 否   | 默认 true。true 时按 task 维度删该 task 下 `source='external_import'` 的旧 prediction 后再写入;显式 false 时追加. |
| `image_width` / `image_height` | form  | int                               | 否   | 仅 `format=coco` 生效。COCO `images[]` 缺 `width/height` 时作为全局兜底；图片自带尺寸优先。两个字段必须同时提供。 |

权限：项目 owner 或 super_admin（与 ML backend 配置同位）。

## 响应

```json
{
  "imported": 42,
  "skipped": 3,
  "errors": [
    { "task_match": { "display_id": "T-999" }, "reason": "task not found in project" },
    { "task_match": { "file_path": "x.jpg" }, "reason": "unsupported geometry kind: 'polyline'" }
  ],
  "dry_run": false
}
```

OpenAPI 响应组件为 `ImportResult`，逐项错误为 `ImportErrorEntry`；生成客户端使用这两个类型名。界面中的“原生 JSON”对应 `format=native_json`，当前文件使用 `schema_version: "2.0"`、`format: "yoao"` 和 `exported_from.platform: "yoao"`；历史 `1.x` 文档仍可读取。

错误是逐 entry / shape 累计，不让整批挂。多文件上传时后端合并响应，并在 errors[].reason 前缀文件名。单文件 schema_version 不兼容 / JSON 不可解析会整体 422；多文件中的坏文件会作为一条错误累计，其他文件继续处理。

多文件导入是 best-effort 语义：整批在一个事务内提交，但**不具备原子性**——若某个文件失败被累计为错误，已成功文件写入的预测仍会保留，不会因后续文件失败而回滚。

## 原生 JSON 格式 {#aap-json-格式}

详见 [用户文档 · 原生 JSON](../../user-guide/reference/export-formats#aap-json-1-5-无损) + [ADR-0024](../../dev/adr/archive/0024-aap-json-format)。预测导入消费 `predictions[]`；标注导入接口消费 `annotations[]`。图片 `raster_mask` 和视频 `video_track_mask` 都通过 `mask_objects` 恢复内容寻址正文；导入会先校验引用、摘要与尺寸，再写对象并挂载 Prediction 或 Annotation。

COCO 导入同时识别 bbox、polygon segmentation 与 uncompressed RLE segmentation。RLE 会先写入共享内容层，再形成 `raster_mask` 候选；非法、空前景或尺寸不匹配的 segmentation 作为逐 annotation 错误返回，不会静默降级成 bbox。

最小可导入示例：

```json
{
  "schema_version": "2.0",
  "format": "yoao",
  "exported_from": { "platform": "yoao" },
  "tasks": [
    {
      "task_match": { "display_id": "T-101" },
      "predictions": [
        {
          "geometry": { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
          "class_name": "stop_sign",
          "confidence": 0.92,
          "model_version": "ext-yolov8-v1"
        }
      ]
    }
  ]
}
```

`predictions[i]` 也可以用 `shapes[]` 把多个几何合并成同一条 Prediction：

```json
{
  "geometry": { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
  "class_name": "stop_sign",
  "confidence": 0.92,
  "score": 0.88,
  "shapes": [
    { "type": "bbox", "x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4 },
    { "type": "bbox", "x": 0.5, "y": 0.2, "w": 0.2, "h": 0.3 }
  ]
}
```

`shapes[]` 与 `geometry` 同时存在时，以 `shapes[]` 为准；其中某个 shape 不支持时只记录该 shape 的 errors[]，其余 shape 仍合并入库。同一 entry 中通过校验的 shapes 必须属于同一工具单位；bbox、region（polygon / mask）和 polyline 不能混在一条 Prediction 中。

支持的图片 geometry kind：`bbox` / `polygon` / `multi_polygon` / `polyline` / `rotated_bbox` / `keypoint`。`rotated_bbox` 使用平台内部中心点格式 `{cx, cy, w, h, angle}`，导入时写成 Label Studio `rectanglelabels.rotation`，读回时再还原中心点。`keypoint` 使用 `{points:[{x,y,v}]}`，`v` 保留 COCO 可见性 0/1/2。

视频 task block 必须声明 `media_type: "video"`，支持 `video_bbox`、`video_track_bbox`、`video_track_polygon`、`video_track_polyline` 与 `video_track_mask`。导入会按源视频 `frame_count` 校验单帧、关键帧和 outside 范围，要求轨迹关键帧严格递增，并把外部 keyframe / outside 的 `source` 归一为 `prediction`。缺失源帧数、命中非视频任务或越界时，该 entry 进入 `errors[]`。

## COCO Detection 格式

最小子集：`images[]` + `annotations[]` + `categories[]`。bbox 是 COCO 标准 `[x, y, w, h]` 像素坐标，用 `images[].width/height` 归一化；若 image 缺尺寸，可通过 form `image_width` / `image_height` 提供全局兜底。

匹配规则：用 `images[].file_name` 当 `task.file_path`（调用方应保证 dataset 命名一致）。

`annotations[].score` 字段被读为 confidence；缺失则 confidence=None。

## YOLO zip 格式

`format=yolo` 接受一个 zip 包。包内需要：

- `classes.txt` 或 `data.yaml`：类别索引映射。缺失时回退项目当前类别顺序。
- 一个或多个 label `.txt`：空文件表示该图无预测，导入时 no-op。

支持的 label 行由 `yolo_variant` 决定：

| `yolo_variant` | 行格式                          | 写入 geometry                                  |
| -------------- | ------------------------------- | ---------------------------------------------- |
| `det`          | `<cls> <cx> <cy> <w> <h>`       | `bbox`                                         |
| `obb`          | `<cls> <x1> <y1> ... <x4> <y4>` | 矩形四角写 `rotated_bbox`,否则降级为 `polygon` |
| `seg`          | `<cls> <x1> <y1> <x2> <y2> ...` | `polygon`                                      |

所有坐标都是归一化 `[0,1]`。`obb` 需要匹配 task 的 `DatasetItem.width/height`，用于在像素空间还原角度与宽高；不接受用户手填尺寸。

task 匹配按 label 文件 stem 进行：`labels/animals/cat/001.txt` 会匹配项目内 `animals/cat/001.<任意图片扩展名>`。纯叶子 stem（如 `labels/001.txt`）如果命中多个目录或扩展名，进入 `errors[]`，不会猜测。

## 写入语义

- 写入的 `predictions` 行：`source='external_import'`, `ml_backend_id=NULL`, `model_version=<entry 内值或 form 兜底>`, `result=<内部 LS shape 数组>`.
- 原生 JSON 每个 `predictions[i]` 对应**一条** Prediction 行；普通 `geometry` 写 1 个 shape，`shapes[]` 写多个 shape 到同一行的 `result[]`。COCO 每个 `annotations[i]` 对应一条。YOLO 每个非空 label 文件对应一条，文件内多行合并到同一条 `result[]`。
- `overwrite_existing` 默认 true，只清 `source='external_import'`；不会删除 ML Backend 生成的 `source='ml_backend'` 预测。多文件同一次上传共享 purge 作用域，同一 task 只清一次旧导入，随后各文件的新预测都会保留。
- 写入路径复用 `PredictionService.create_from_ml_result`，确保与 ML backend 写入路径同源。

## task_match 匹配规则

原生 JSON `task_match` 是 oneof：

1. **`display_id` 优先**：全局唯一，最稳。命中后校验 `project_id` 一致；跨项目命中视为不匹配（防偷换项目）。
2. **`file_path` fallback**：项目内查；命中第一条即返。
3. 都没给或都不命中 → entry 进 errors[]，整批继续。

YOLO 不使用 `task_match` 字段，而是按 label 文件 stem 走相同的项目内防歧义匹配规则。

## dry-run 工作流（推荐）

```bash
# Step 1: dry-run 预览
curl -X POST "https://platform/api/v1/projects/$PID/predictions/import?format=native_json&dry_run=true" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@external_preds.json"

# 检查 errors[] 是否符合预期，调整 input 后...

# Step 2: 正式导入
curl -X POST "https://platform/api/v1/projects/$PID/predictions/import?format=native_json&dry_run=false" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@external_preds.json" \
  -F "overwrite_existing=true"
```

多文件 JSON 可重复传 `file` 字段：

```bash
curl -X POST "https://platform/api/v1/projects/$PID/predictions/import?format=native_json" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@part-a.json" \
  -F "file=@part-b.json"
```

## 审计

所有非 dry-run 导入在 `audit_logs` 写一条 `predictions.import` 记录，`detail_json` 含 `format` / `imported` / `skipped` / `error_count` / `overwrite_existing` / `model_version_fallback` / `image_size_hint` / `yolo_variant` / `file_count`。
