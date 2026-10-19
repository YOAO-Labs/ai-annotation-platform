---
title: YOLO 导入适配
audience: [dev]
type: reference
status: stable
last_reviewed: 2026-10-19
---

# YOLO 导入适配

YOLO 预测导入走 `POST /api/v1/projects/{project_id}/predictions/import?format=yolo&yolo_variant=det|obb|seg`。请求体仍是 multipart，`file` 必须是 zip 包；正式导入写入 `predictions.source='external_import'`。

## 包结构

导入器不要求固定根目录，但会读取：

- `classes.txt`：每行一个类别，行号就是 YOLO class index。
- `data.yaml` / `data.yml`：支持 `names: [car, truck]`、`names: {0: car, 1: truck}`、缩进列表和缩进 map。
- `*.txt` label 文件：跳过 `classes.txt`、隐藏文件和 `*.attrs.json`。

类别文件缺失时，导入器回退项目 `tool_bindings` 派生出的类别顺序。若类别名不属于项目类别，当前行进入 `errors[]`。

## Task 匹配

YOLO label 文件没有 task id，所以按 label 文件 stem 匹配 task 的图片路径 stem：

- `labels/img_001.txt` → `img_001.jpg/png/...`
- `labels/animals/cat/001.txt` → `animals/cat/001.jpg/png/...`
- `<project>/<dataset>/labels/animals/cat/001.txt` 会先剥掉最后一个 `labels/` 前缀，再匹配 `animals/cat/001`

纯叶子 stem 可能跨目录或跨扩展名命中多条 task。此时导入器返回 `ambiguous task file stem`，不会猜测。

## 几何转换

| Variant | YOLO 行               | 内部 geometry               |
| ------- | --------------------- | --------------------------- |
| `det`   | `cls cx cy w h`       | `bbox`                      |
| `seg`   | `cls x1 y1 x2 y2 ...` | `polygon`                   |
| `obb`   | `cls x1 y1 ... x4 y4` | `rotated_bbox` 或 `polygon` |

所有坐标必须是归一化 `[0,1]`。`obb` 会先把四角乘以 `DatasetItem.width/height` 进入像素空间，再按导出端同一角点顺序还原 `{cx, cy, w, h, angle}`；四角不满足矩形约束时保留为 polygon，避免错误角度污染训练数据。

## 写入粒度

每个非空 label 文件写一条 Prediction，文件内多行合并到该行的 `result[]`。空 label 文件是 no-op，用于兼容平台导出的空标签文件。
