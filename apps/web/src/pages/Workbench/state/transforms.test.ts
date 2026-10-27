/**
 * v0.8.3 · transforms 单测：bbox / polygon 几何转换 + AnnotationResponse / Prediction 映射。
 */
import { describe, it, expect } from "vitest";
import {
  bboxGeom,
  polygonGeom,
  polygonBounds,
  geometryToShape,
  annotationToBox,
  collectOccludedKeys,
  predictionsToBoxes,
} from "./transforms";

describe("bboxGeom / polygonGeom", () => {
  it("bboxGeom 包装", () => {
    const g = bboxGeom({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
    expect(g).toEqual({ type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
  });
  it("polygonGeom 包装", () => {
    const g = polygonGeom([
      [0, 0],
      [1, 0],
      [0.5, 1],
    ]);
    expect(g.type).toBe("polygon");
    expect(g.points).toHaveLength(3);
  });
});

describe("polygonBounds", () => {
  it("空数组 → 全 0", () => {
    expect(polygonBounds([])).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });
  it("三角形包围盒", () => {
    const b = polygonBounds([
      [0.1, 0.2],
      [0.9, 0.3],
      [0.5, 0.8],
    ]);
    expect(b.x).toBeCloseTo(0.1);
    expect(b.y).toBeCloseTo(0.2);
    expect(b.w).toBeCloseTo(0.8);
    expect(b.h).toBeCloseTo(0.6);
  });
});

describe("geometryToShape", () => {
  it("polygon → 含 polygon 字段 + bounds", () => {
    const s = geometryToShape({
      type: "polygon",
      points: [
        [0, 0],
        [1, 0],
        [0.5, 1],
      ],
    });
    expect(s.x).toBe(0);
    expect(s.w).toBe(1);
    expect(s.polygon).toHaveLength(3);
  });
  it("bbox → 不含 polygon 字段", () => {
    const s = geometryToShape({ type: "bbox", x: 0, y: 0, w: 1, h: 1 });
    expect(s).toEqual({ x: 0, y: 0, w: 1, h: 1 });
    expect((s as { polygon?: unknown }).polygon).toBeUndefined();
  });

  it("video_bbox → 忽略 frame_index 并返回当前帧 bbox 几何", () => {
    const s = geometryToShape({
      type: "video_bbox",
      frame_index: 12,
      x: 0.1,
      y: 0.2,
      w: 0.3,
      h: 0.4,
    });
    expect(s).toEqual({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
  });

  it("video_track → 返回第一条非消失关键帧 bbox", () => {
    const s = geometryToShape({
      type: "video_track_bbox",
      track_id: "trk_1",
      keyframes: [
        { frame_index: 0, bbox: { x: 0, y: 0, w: 0.1, h: 0.1 }, source: "manual" },
        { frame_index: 12, bbox: { x: 0.2, y: 0.3, w: 0.4, h: 0.5 }, source: "manual" },
      ],
      outside: [{ from: 0, to: 0 }],
    });
    expect(s).toEqual({ x: 0.2, y: 0.3, w: 0.4, h: 0.5 });
  });
});

describe("annotationToBox", () => {
  it("映射常规字段", () => {
    const ann = {
      id: "a1",
      geometry: { type: "bbox", x: 0, y: 0, w: 0.5, h: 0.5 },
      class_name: "car",
      confidence: 0.9,
      source: "manual",
    } as any;
    const box = annotationToBox(ann);
    expect(box.id).toBe("a1");
    expect(box.cls).toBe("car");
    expect(box.conf).toBe(0.9);
    expect(box.w).toBe(0.5);
  });

  it("透传前端 render_key,避免乐观创建确认后画布重挂", () => {
    const ann = {
      id: "real-1",
      render_key: "tmp_abc",
      geometry: { type: "bbox", x: 0, y: 0, w: 0.5, h: 0.5 },
      class_name: "car",
      confidence: 0.9,
      source: "manual",
    } as any;

    expect(annotationToBox(ann).render_key).toBe("tmp_abc");
  });

  it("无 confidence → 默认 1", () => {
    const ann = {
      id: "a2",
      geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
      class_name: "person",
      source: "manual",
    } as any;
    expect(annotationToBox(ann).conf).toBe(1);
  });

  // v0.11.27 · 遮挡为渲染派生：style_occluded 属性为 true 时 occluded=true。
  it("不传 occludedKeys → occluded 默认 false", () => {
    const ann = {
      id: "a3",
      geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
      class_name: "car",
      source: "manual",
      attributes: { occ: true },
    } as any;
    expect(annotationToBox(ann).occluded).toBe(false);
  });

  it("属性命中 occludedKeys 且为 true → occluded=true", () => {
    const keys = new Set(["occ"]);
    const truthy = {
      id: "a4",
      geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
      class_name: "car",
      source: "manual",
      attributes: { occ: true },
    } as any;
    const falsy = { ...truthy, id: "a5", attributes: { occ: false } } as any;
    const missing = { ...truthy, id: "a6", attributes: {} } as any;
    expect(annotationToBox(truthy, keys).occluded).toBe(true);
    expect(annotationToBox(falsy, keys).occluded).toBe(false);
    expect(annotationToBox(missing, keys).occluded).toBe(false);
  });
});

describe("collectOccludedKeys", () => {
  it("仅收集 boolean 且 style_occluded 的字段 key", () => {
    const fields = [
      { key: "occ", label: "遮挡", type: "boolean", style_occluded: true },
      { key: "blur", label: "模糊", type: "boolean" },
      { key: "note", label: "备注", type: "text", style_occluded: true },
    ] as any;
    const keys = collectOccludedKeys(fields);
    expect([...keys]).toEqual(["occ"]);
  });
});

describe("predictionsToBoxes", () => {
  it("keeps each stage's tool unit and already-bound native class", () => {
    const boxes = predictionsToBoxes(
      [
        {
          id: "mixed",
          tool_unit_id: "region",
          result: [
            {
              tool_unit_id: "region",
              class_name: "car",
              confidence: 0.9,
              geometry: { type: "raster_mask", mask: {} },
            },
            {
              tool_unit_id: "bbox",
              class_name: "Car",
              confidence: 0.8,
              geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
            },
          ],
        },
      ] as any,
      {
        region: { enabled: true, classes: [{ name: "car" }, { name: "wrong", alias: "car" }] },
        bbox: { enabled: true, classes: [{ name: "车辆", alias: "Car" }] },
      } as any,
    );
    expect(boxes.map((box) => [box.tool_unit_id, box.cls])).toEqual([
      ["region", "car"],
      ["bbox", "车辆"],
    ]);
  });

  it("flatten predictions.result 到 AiBox 列表", () => {
    const preds = [
      {
        id: "p1",
        source: "external_import",
        result: [
          {
            geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
            class_name: "car",
            confidence: 0.8,
          },
          {
            geometry: { type: "bbox", x: 1, y: 1, w: 1, h: 1 },
            class_name: "person",
            confidence: 0.7,
          },
        ],
      },
    ] as any;
    const boxes = predictionsToBoxes(preds);
    expect(boxes).toHaveLength(2);
    expect(boxes[0].id).toBe("pred-p1-0");
    expect(boxes[0].predictionId).toBe("p1");
    expect(boxes[0].source).toBe("prediction_based");
    expect(boxes[0].predictionSource).toBe("external_import");
  });

  it("uses original shape_index for ids and accept payloads when predictions are filtered", () => {
    const preds = [
      {
        id: "p1",
        result: [
          {
            shape_index: 2,
            geometry: { type: "bbox", x: 0.5, y: 0.5, w: 0.2, h: 0.2 },
            class_name: "person",
            confidence: 0.9,
          },
        ],
      },
    ] as any;
    const [box] = predictionsToBoxes(preds);
    expect(box.id).toBe("pred-p1-2");
    expect(box.shapeIndex).toBe(2);
    expect(box.cls).toBe("person");
  });

  it("把 DINO 写入的英文 alias 归一回原类别名 (按 prediction 的 tool_unit_id 隔离)", () => {
    const toolBindings = {
      bbox: {
        enabled: true,
        classes: [{ name: "汽车", alias: "Car", order: 0 }],
      },
    } as any;
    const preds = [
      {
        id: "p1",
        tool_unit_id: "bbox",
        result: [
          {
            geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
            class_name: "car",
            confidence: 0.8,
          },
          {
            geometry: { type: "bbox", x: 1, y: 1, w: 1, h: 1 },
            class_name: "tree",
            confidence: 0.7,
          },
        ],
      },
    ] as any;
    const boxes = predictionsToBoxes(preds, toolBindings);
    expect(boxes[0].cls).toBe("汽车"); // alias 大小写不敏感命中 → 原名
    expect(boxes[1].cls).toBe("tree"); // 无 alias 配置 → 原样保留
  });

  it("无 toolBindings 时保持 class_name 原样 (向后兼容)", () => {
    const preds = [
      {
        id: "p1",
        result: [
          {
            geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 },
            class_name: "car",
            confidence: 0.8,
          },
        ],
      },
    ] as any;
    expect(predictionsToBoxes(preds)[0].cls).toBe("car");
  });
});

/**
 * v0.9.8 · predictionsToBoxes 黄金样本 — 锁定后端 to_internal_shape 写入 DB 后
 * 前端消费契约. 后端 PredictionService 负责 LabelStudio 标准 → 内部 shape 的
 * 转换 (apps/api/app/services/prediction.py to_internal_shape), 这里假设输入
 * 已是内部 shape, 覆盖空 / 多 / polygon / 缺字段 4 类边界.
 */
describe("predictionsToBoxes 黄金样本 (v0.9.8 schema 边界)", () => {
  it("空 result 数组 → 空 box 列表", () => {
    const preds = [{ id: "empty", result: [] }] as any;
    expect(predictionsToBoxes(preds)).toEqual([]);
  });

  it("多 prediction 行 + 多 shape, id 含 prediction id 与索引", () => {
    const preds = [
      {
        id: "p-a",
        result: [
          {
            geometry: { type: "bbox", x: 0, y: 0, w: 0.1, h: 0.1 },
            class_name: "car",
            confidence: 0.9,
          },
          {
            geometry: { type: "bbox", x: 0.5, y: 0.5, w: 0.2, h: 0.2 },
            class_name: "person",
            confidence: 0.8,
          },
        ],
      },
      {
        id: "p-b",
        result: [
          {
            geometry: { type: "bbox", x: 0.3, y: 0.3, w: 0.1, h: 0.1 },
            class_name: "dog",
            confidence: 0.7,
          },
        ],
      },
    ] as any;
    const boxes = predictionsToBoxes(preds);
    expect(boxes).toHaveLength(3);
    const ids = boxes.map((b) => b.id);
    expect(ids).toEqual(["pred-p-a-0", "pred-p-a-1", "pred-p-b-0"]);
    expect(boxes.every((b) => b.source === "prediction_based")).toBe(true);
  });

  it("polygon 几何 → polygon 字段保留, x/w 来自 bounds", () => {
    const preds = [
      {
        id: "p-poly",
        result: [
          {
            geometry: {
              type: "polygon",
              points: [
                [0.2, 0.2],
                [0.8, 0.3],
                [0.5, 0.9],
              ],
            },
            class_name: "leaf",
            confidence: 0.85,
          },
        ],
      },
    ] as any;
    const boxes = predictionsToBoxes(preds);
    expect(boxes).toHaveLength(1);
    expect(boxes[0].polygon).toHaveLength(3);
    expect(boxes[0].x).toBeCloseTo(0.2);
    expect(boxes[0].w).toBeCloseTo(0.6);
  });

  it("confidence=0 不被视为缺失 (pass-through)", () => {
    const preds = [
      {
        id: "p-zero",
        result: [
          { geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 }, class_name: "x", confidence: 0 },
        ],
      },
    ] as any;
    const [b] = predictionsToBoxes(preds);
    expect(b.conf).toBe(0);
  });

  it("class_name 空字符串 (alias 缺失场景) 不抛错, 落到 cls=''", () => {
    const preds = [
      {
        id: "p-empty-cls",
        result: [
          { geometry: { type: "bbox", x: 0, y: 0, w: 1, h: 1 }, class_name: "", confidence: 0.5 },
        ],
      },
    ] as any;
    const [b] = predictionsToBoxes(preds);
    expect(b.cls).toBe("");
  });
});

/**
 * v0.9.14 · multi_polygon / hole 几何映射 — mask→polygon 协议升级后, 后端
 * to_internal_shape (apps/api/app/services/prediction.py) 在 mask 多连通或带 hole
 * 时分别输出 polygon+holes / multi_polygon 两类, 前端 transforms 必须正确解析.
 * 当前编辑路径只识别单环 polygon, 多连通时降级取主外环, 完整 polygons 透传到
 * multiPolygon 字段供 v0.10.x 镂空渲染升级.
 */
describe("v0.9.14 mask 多连通域 / 空洞", () => {
  it("polygon + holes → polygon 字段是外环, holes 字段透传", () => {
    const s = geometryToShape({
      type: "polygon",
      points: [
        [0, 0],
        [10, 0],
        [10, 10],
        [0, 10],
      ],
      holes: [
        [
          [3, 3],
          [7, 3],
          [7, 7],
          [3, 7],
        ],
      ],
    });
    expect(s.polygon).toHaveLength(4);
    expect(s.holes).toHaveLength(1);
    expect(s.holes![0]).toHaveLength(4);
    // bounding rect 跟外环一致
    expect(s.x).toBe(0);
    expect(s.w).toBe(10);
  });

  it("multi_polygon → 主外环走 polygon 字段, 全部 polygons 留在 multiPolygon", () => {
    const s = geometryToShape({
      type: "multi_polygon",
      polygons: [
        // 小三角
        {
          type: "polygon",
          points: [
            [0, 0],
            [1, 0],
            [0.5, 1],
          ],
        },
        // 大五边形 (顶点最多, 选作主环)
        {
          type: "polygon",
          points: [
            [10, 10],
            [12, 10],
            [13, 12],
            [11, 14],
            [9, 12],
          ],
          holes: [
            [
              [10.5, 11],
              [11.5, 11],
              [11.5, 12],
              [10.5, 12],
            ],
          ],
        },
      ],
    });
    expect(s.polygon).toHaveLength(5);
    expect(s.holes).toHaveLength(1); // 来自主环的 hole
    expect(s.multiPolygon).toHaveLength(2);
    // bounding rect 应覆盖所有 polygon
    expect(s.x).toBe(0);
    expect(s.w).toBeCloseTo(13);
    expect(s.h).toBeCloseTo(14);
  });

  it("annotationToBox 透传 holes / multiPolygon", () => {
    const ann = {
      id: "a-multi",
      geometry: {
        type: "multi_polygon",
        polygons: [
          {
            type: "polygon",
            points: [
              [0, 0],
              [1, 0],
              [1, 1],
            ],
          },
          {
            type: "polygon",
            points: [
              [2, 2],
              [3, 2],
              [3, 3],
              [2, 3],
            ],
          },
        ],
      },
      class_name: "donut",
      confidence: 0.91,
      source: "prediction_based",
    } as any;
    const box = annotationToBox(ann);
    expect(box.cls).toBe("donut");
    expect(box.multiPolygon).toHaveLength(2);
    expect(box.polygon).toBeDefined();
  });

  it("predictionsToBoxes 处理 polygon + holes (单连通带空洞)", () => {
    const preds = [
      {
        id: "p-donut",
        result: [
          {
            geometry: {
              type: "polygon",
              points: [
                [0.1, 0.1],
                [0.9, 0.1],
                [0.9, 0.9],
                [0.1, 0.9],
              ],
              holes: [
                [
                  [0.4, 0.4],
                  [0.6, 0.4],
                  [0.6, 0.6],
                  [0.4, 0.6],
                ],
              ],
            },
            class_name: "donut",
            confidence: 0.88,
          },
        ],
      },
    ] as any;
    const [b] = predictionsToBoxes(preds);
    expect(b.polygon).toHaveLength(4);
    expect(b.holes).toHaveLength(1);
    expect(b.cls).toBe("donut");
  });
});

describe("v0.23.6 raster_mask", () => {
  it("同步转换不猜测外接框", () => {
    expect(
      geometryToShape({
        type: "raster_mask",
        mask: {
          encoding: "coco_rle_ref",
          size: [10, 20],
          object_key: "raster-masks/sha256/aa/bb/digest.json",
          sha256: "a".repeat(64),
          runs: 4,
          bytes: 32,
        },
      }),
    ).toMatchObject({ x: 0, y: 0, w: 0, h: 0 });
  });
});
