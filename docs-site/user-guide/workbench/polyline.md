---
audience: [annotator]
type: how-to
status: stable
last_reviewed: 2026-06-10
---

# 折线标注（Polyline）

折线是**开放、不闭合**的顶点序列，与多边形的唯一区别是首尾不连、不填充。

## 适用场景

- 车道线 / 道路中心线
- 河流、管线等线性目标
- 任何"有走向但不围合区域"的标注

## 操作

<DocsVideo
  src="/media/polyline/draw.mp4"
  poster="/media/polyline/draw-poster.webp"
  alt="沿线性目标逐点绘制折线并按 Enter 结束"
  caption="保留类别和标注详情面板，沿目标逐点绘制；按 Enter 结束折线，再选择正确类别。"
/>

1. 在**无选中**状态下按 `L` 切到折线工具（选中状态下 `L` 是锁定）。
2. 沿目标依次单击落点，每点生成一个顶点（至少 2 点）。
3. **双击**或按 `Enter` 结束当前折线（需 ≥ 2 顶点）。
4. 在折线旁弹出的类别浮层中选择对应的目标类别。

绘制中的快捷键：

| 操作                 | 快捷键         |
| -------------------- | -------------- |
| 结束折线（≥ 2 顶点） | `Enter` 或双击 |
| 撤销最后一个落点     | `Backspace`    |
| 取消当前草稿         | `Esc`          |

绘制 polyline 时，会在 8px 屏幕距离内吸附到可见 polygon / multi_polygon 的顶点或边界；按住 `Alt` 可临时关闭吸附。详见 [Polygon 标注 · 编辑](./polygon#编辑)。

## 编辑已有折线

<!-- TODO IMAGE_CHECKLIST: images/polyline/vertex-edit.png — 折线选中态 Alt 插入/Shift 删除提示 -->

- 选中后**拖动顶点** → 修改形状。
- `Alt` + 单击边 → 在该段插入新顶点。
- `Shift` + 单击顶点 → 删除该顶点（少于 2 点时拒绝）。
- 拖动折线主体 → 整体平移。

## 与多边形的区别

|          | 折线 polyline  | 多边形 polygon |
| -------- | -------------- | -------------- |
| 闭合     | 否（首尾不连） | 是             |
| 填充     | 无             | 有             |
| 最少顶点 | 2              | 3              |
| 自交检测 | 无             | 有             |

## 数据语义

折线存 `points[]`（≥2 个归一化顶点），不带 `holes`。几何 JSON 示例：

```json
{
  "type": "polyline",
  "points": [
    [0.12, 0.45],
    [0.34, 0.6],
    [0.78, 0.55]
  ]
}
```

开发者细节见 [标注模块 · Geometry union](../../dev/concepts/annotation-module#geometry-union)。

## 导出说明

| 导出目标      | 说明                           |
| ------------- | ------------------------------ |
| `native_json` | 原始 `points[]` 归一化坐标保留 |
| `coco`        | 折线**不进 COCO**，导出时跳过  |
| `yolo`        | 折线不映射任何 YOLO 格式，跳过 |
