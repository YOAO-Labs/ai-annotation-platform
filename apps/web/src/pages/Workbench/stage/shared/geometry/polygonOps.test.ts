import { describe, expect, it } from "vitest";
import type { AnnotationResponse, Geometry } from "@/types";
import {
  buildPolygonJoinPayload,
  canJoinPolygonAnnotation,
  cropPolygonGeometry,
  joinPolygonGeometries,
} from "./polygonOps";

function ann(
  id: string,
  geometry: Geometry,
  overrides: Partial<AnnotationResponse> = {},
): AnnotationResponse {
  return {
    id,
    task_id: "task",
    project_id: "project",
    user_id: "user",
    source: "manual",
    annotation_type: geometry.type,
    class_name: "road",
    geometry,
    confidence: 1,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    attributes: { lane: "main" },
    created_at: "2026-06-07T00:00:00Z",
    updated_at: null,
    tool_unit_id: "region",
    ...overrides,
  };
}

const left: Geometry = {
  type: "polygon",
  points: [
    [0, 0],
    [0.5, 0],
    [0.5, 1],
    [0, 1],
  ],
};
const right: Geometry = {
  type: "polygon",
  points: [
    [0.5, 0],
    [1, 0],
    [1, 1],
    [0.5, 1],
  ],
};

describe("polygon join helpers", () => {
  it("joins adjacent polygons into a single polygon", () => {
    const out = joinPolygonGeometries([left, right]);

    expect(out?.type).toBe("polygon");
    expect(out && "points" in out ? out.points.length : 0).toBeGreaterThanOrEqual(4);
  });

  it("returns multi_polygon for disjoint polygon union", () => {
    const out = joinPolygonGeometries([
      left,
      {
        type: "polygon",
        points: [
          [0.7, 0],
          [0.9, 0],
          [0.9, 0.2],
          [0.7, 0.2],
        ],
      },
    ]);

    expect(out?.type).toBe("multi_polygon");
    expect(out && "polygons" in out ? out.polygons : []).toHaveLength(2);
  });

  it("builds a create payload and preserves identical attributes", () => {
    const result = buildPolygonJoinPayload([ann("a", left), ann("b", right)]);

    expect(result?.payload).toMatchObject({
      annotation_type: "polygon",
      class_name: "road",
      tool_unit_id: "region",
      attributes: { lane: "main" },
    });
    expect(result?.sourceAnnotations.map((source) => source.id)).toEqual(["a", "b"]);
  });

  it("preserves attributes that match regardless of key order", () => {
    const result = buildPolygonJoinPayload([
      ann("a", left, { attributes: { lane: "main", dir: "n" } }),
      ann("b", right, { attributes: { dir: "n", lane: "main" } }),
    ]);

    expect(result?.payload.attributes).toEqual({ lane: "main", dir: "n" });
  });

  it("clears attributes when selected polygons disagree", () => {
    const result = buildPolygonJoinPayload([
      ann("a", left),
      ann("b", right, { attributes: { lane: "side" } }),
    ]);

    expect(result?.payload.attributes).toEqual({});
  });

  it("rejects mixed classes and non-polygon inputs", () => {
    expect(
      buildPolygonJoinPayload([ann("a", left), ann("b", right, { class_name: "sidewalk" })]),
    ).toBeNull();
    expect(
      buildPolygonJoinPayload([
        ann("a", left),
        ann("b", { type: "bbox", x: 0, y: 0, w: 0.1, h: 0.1 }),
      ]),
    ).toBeNull();
  });

  it("blocks locked annotations from join eligibility", () => {
    expect(canJoinPolygonAnnotation(ann("a", left))).toBe(true);
    expect(canJoinPolygonAnnotation(ann("a", left, { is_locked: true }))).toBe(false);
  });
});

const fullSquare: Geometry = {
  type: "polygon",
  points: [
    [0, 0],
    [1, 0],
    [1, 1],
    [0, 1],
  ],
};

describe("polygon crop helpers", () => {
  it("punches a hole when the cutter sits fully inside the base", () => {
    const inner: Geometry = {
      type: "polygon",
      points: [
        [0.3, 0.3],
        [0.6, 0.3],
        [0.6, 0.6],
        [0.3, 0.6],
      ],
    };
    const out = cropPolygonGeometry(fullSquare, [inner]);

    expect(out?.type).toBe("polygon");
    expect(out && out.type === "polygon" ? out.holes?.length : 0).toBe(1);
  });

  it("clips an overlapping edge into a smaller polygon", () => {
    const out = cropPolygonGeometry(fullSquare, [right]);

    expect(out?.type).toBe("polygon");
    const xs = out && out.type === "polygon" ? out.points.map(([x]) => x) : [];
    expect(Math.max(...xs)).toBeCloseTo(0.5, 5);
  });

  it("subtracts multiple cutters at once", () => {
    const out = cropPolygonGeometry(fullSquare, [left, right]);
    expect(out).toBeNull();
  });

  it("returns null when the cutter fully covers the base", () => {
    expect(cropPolygonGeometry(left, [fullSquare])).toBeNull();
  });

  it("returns null when there is no valid cutter", () => {
    expect(cropPolygonGeometry(fullSquare, [])).toBeNull();
    expect(
      cropPolygonGeometry(fullSquare, [{ type: "bbox", x: 0, y: 0, w: 0.1, h: 0.1 }]),
    ).toBeNull();
  });
});
