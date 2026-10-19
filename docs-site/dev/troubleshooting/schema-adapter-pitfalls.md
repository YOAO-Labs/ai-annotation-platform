---
audience: [dev]
type: explanation
status: stable
last_reviewed: 2026-10-19
---

# Prediction Schema 适配器陷阱

## 症状

AI 预标注任务后端跑成功（`prediction_jobs` 表里 `succeeded`，`predictions` 表里有行），但前端工作台打开任务、看不到任何候选框；或前端报错 `Cannot read property 'class_name' of undefined`。

早期 SAM/DINO 推理接通并端到端跑通时发现历史 32 条 predictions 在前端读路径全部丢失。

## 根因

DB 里存的是 **LabelStudio 标准格式**（保导出/CVAT 兼容性）：

```json
{
  "type": "rectanglelabels",
  "value": {
    "x": 12,
    "y": 34,
    "width": 56,
    "height": 78,
    "rectanglelabels": ["dog"]
  },
  "score": 0.91
}
```

但前端 `predictionsToBoxes` 期望**内部 schema**：

```json
{
  "type": "rectanglelabels",
  "class_name": "dog",
  "geometry": { "x": 12, "y": 34, "width": 56, "height": 78 },
  "confidence": 0.91
}
```

两端各自演化，没人在中间做适配，前端自然渲染不出来。

## 修复

不动 DB 写路径（保 LabelStudio 标准），在**读路径**加 `to_internal_shape` 适配器：

- `apps/api/app/api/v1/tasks/predictions.py::get_predictions`
- `apps/api/app/api/v1/annotation.py::accept_prediction`

适配器三层 fallback（容忍历史数据）：

1. LabelStudio 标准：`value.{type}[0]` self-referential（`type=rectanglelabels` 取 `value.rectanglelabels[0]`）
2. 老格式：`value.labels[0]`
3. 老格式：`value.class`

`score` ↔ `confidence` 同样 fallback。已是内部 schema 的输入直接 pass-through（idempotent）。

10 单测覆盖：LabelStudio / `value.labels` / `value.class` / pass-through / `score`↔`confidence` / 非法输入。

## 防御性收口

同步引入 **schema 边界文档与类型校验**：

- `docs-site/dev/concepts/api-schema-boundary.md` — 明确「写路径用 LabelStudio 标准、读路径过 adapter 出内部 schema」
- `apps/web/src/api/transforms.ts` — 5 黄金样本 + 3 idempotent/边界 case，走 codegen 派生类型

## 教训

跨语言（Python ↔ TS）schema 没有单一来源时，**要么强制走 OpenAPI codegen**，要么显式声明 adapter 层。手写两端 dataclass 注定漂移。

## 相关

- commit: `0a99cc6` 含 adapter 落地
- commit: `d41236b` schema 边界文档化 + `transforms.ts` 单测
- 文档：[API Schema 边界](../concepts/api-schema-boundary)
- 代码：`apps/api/app/services/predictions/adapter.py`
