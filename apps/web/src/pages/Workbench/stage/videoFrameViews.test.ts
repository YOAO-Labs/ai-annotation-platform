import { describe, expect, it } from "vitest";
import { deriveVideoFrameViews, visibleInReviewMode } from "./videoFrameViews";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import type { AnnotationResponse, VideoTrackOutsideRange } from "@/types";

function trackAnn(
  id: string,
  trackId: string,
  keyframes: {
    frame_index: number;
    bbox: { x: number; y: number; w: number; h: number };
    source?: string;
    occluded?: boolean;
  }[],
  className = "car",
): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_track_bbox", track_id: trackId, keyframes },
  } as unknown as AnnotationResponse;
}

function bboxAnn(id: string, frame: number, className = "car"): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_bbox", frame_index: frame, x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
  } as unknown as AnnotationResponse;
}

function polygonAnn(
  id: string,
  frame: number,
  points: [number, number][],
  className = "car",
): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_polygon", frame_index: frame, points },
  } as unknown as AnnotationResponse;
}

function polylineAnn(
  id: string,
  frame: number,
  points: [number, number][],
  className = "car",
): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_polyline", frame_index: frame, points },
  } as unknown as AnnotationResponse;
}

function polygonTrackAnn(
  id: string,
  trackId: string,
  keyframes: { frame_index: number; points: [number, number][]; source?: string }[],
  className = "car",
): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_track_polygon", track_id: trackId, keyframes },
  } as unknown as AnnotationResponse;
}

function polylineTrackAnn(
  id: string,
  trackId: string,
  keyframes: { frame_index: number; points: [number, number][]; source?: string }[],
  className = "car",
): AnnotationResponse {
  return {
    id,
    class_name: className,
    geometry: { type: "video_track_polyline", track_id: trackId, keyframes },
  } as unknown as AnnotationResponse;
}

function maskTrackAnn(id: string, trackId: string, frame = 0): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_track_mask",
      track_id: trackId,
      keyframes: [
        {
          frame_index: frame,
          source: "manual",
          mask: {
            encoding: "coco_rle_ref",
            size: [100, 100],
            object_key: "mask/test",
            sha256: "a".repeat(64),
            runs: 2,
            bytes: 4,
          },
        },
      ],
    },
  } as unknown as AnnotationResponse;
}

const base = { visual: DEFAULT_ANNOTATION_VISUAL, selectedId: null };

describe("deriveVideoFrameViews", () => {
  it("单帧 polygon: entry 带 points + 外接盒 geom, 实线, 只在所属帧显示", () => {
    const ann = polygonAnn("sp1", 3, [
      [0.1, 0.1],
      [0.5, 0.1],
      [0.3, 0.6],
    ]);
    const atFrame = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 3 });
    expect(atFrame.entries).toHaveLength(1);
    expect(atFrame.entries[0].points).toEqual([
      [0.1, 0.1],
      [0.5, 0.1],
      [0.3, 0.6],
    ]);
    expect(atFrame.entries[0].open).toBeUndefined();
    expect(atFrame.entries[0].geom).toEqual({ x: 0.1, y: 0.1, w: 0.4, h: 0.5 });
    expect(atFrame.entries[0].dashed).toBe(false);
    const otherFrame = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 4 });
    expect(otherFrame.entries).toHaveLength(0);
  });

  it("单帧 polyline: entry 带 points + open=true, 只在所属帧显示", () => {
    const ann = polylineAnn("sl1", 7, [
      [0.1, 0.1],
      [0.9, 0.9],
    ]);
    const atFrame = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 7 });
    expect(atFrame.entries).toHaveLength(1);
    expect(atFrame.entries[0].open).toBe(true);
    expect(atFrame.entries[0].points).toEqual([
      [0.1, 0.1],
      [0.9, 0.9],
    ]);
    expect(
      deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 6 }).entries,
    ).toHaveLength(0);
  });

  it("单帧 OBB: entry 四角旋转顶点 (angle=0 时轴对齐), 只在所属帧显示", () => {
    const ann = {
      id: "obb1",
      class_name: "car",
      geometry: {
        type: "video_rotated_bbox",
        frame_index: 2,
        cx: 0.5,
        cy: 0.5,
        w: 0.4,
        h: 0.2,
        angle: 0,
      },
    } as unknown as AnnotationResponse;
    const atFrame = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 2 });
    expect(atFrame.entries).toHaveLength(1);
    expect(atFrame.entries[0].points).toEqual([
      [0.3, 0.4],
      [0.7, 0.4],
      [0.7, 0.6],
      [0.3, 0.6],
    ]);
    expect(atFrame.entries[0].open).toBeUndefined();
    expect(atFrame.entries[0].rotatedBbox).toMatchObject({ angle: 0, cx: 0.5, cy: 0.5 });
    expect(
      deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 3 }).entries,
    ).toHaveLength(0);
  });

  it("单帧关键点只在所属帧显示并保留 v", () => {
    const points = [
      { x: 0.2, y: 0.3, v: 2 as const },
      { x: 0.5, y: 0.6, v: 0 as const },
    ];
    const ann = {
      id: "kp1",
      class_name: "person",
      geometry: {
        type: "video_keypoint",
        frame_index: 4,
        points,
      },
    } as unknown as AnnotationResponse;
    const atFrame = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 4 });
    expect(atFrame.entries[0].keypoints).toEqual(points);
    expect(
      deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 3 }).entries,
    ).toHaveLength(0);
  });

  it("polyline track: entry 带 points + open=true, 插值帧虚线", () => {
    const ann = polylineTrackAnn("l1", "line1", [
      {
        frame_index: 0,
        points: [
          [0, 0],
          [0.2, 0],
          [0.4, 0],
        ],
        source: "manual",
      },
      {
        frame_index: 10,
        points: [
          [0, 0.2],
          [0.2, 0.2],
          [0.4, 0.2],
        ],
        source: "manual",
      },
    ]);
    const atKf = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });
    expect(atKf.entries).toHaveLength(1);
    expect(atKf.entries[0].open).toBe(true);
    expect(atKf.entries[0].points).toEqual([
      [0, 0],
      [0.2, 0],
      [0.4, 0],
    ]);
    const atInterp = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 5 });
    expect(atInterp.entries[0].dashed).toBe(true);
    expect(atInterp.entries[0].points).toEqual([
      [0, 0.1],
      [0.2, 0.1],
      [0.4, 0.1],
    ]);
  });

  it("polygon track: entry 带 points + 外接盒 geom, 插值帧虚线", () => {
    const ann = polygonTrackAnn("p1", "poly1", [
      {
        frame_index: 0,
        points: [
          [0, 0],
          [0.2, 0],
          [0.2, 0.2],
          [0, 0.2],
        ],
        source: "manual",
      },
      {
        frame_index: 10,
        points: [
          [0.4, 0],
          [0.6, 0],
          [0.6, 0.2],
          [0.4, 0.2],
        ],
        source: "manual",
      },
    ]);
    const atKf = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });
    expect(atKf.entries).toHaveLength(1);
    expect(atKf.entries[0].points).toEqual([
      [0, 0],
      [0.2, 0],
      [0.2, 0.2],
      [0, 0.2],
    ]);
    expect(atKf.entries[0].geom).toEqual({ x: 0, y: 0, w: 0.2, h: 0.2 });
    expect(atKf.entries[0].dashed).toBe(false);

    const atInterp = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 5 });
    expect(atInterp.entries[0].dashed).toBe(true);
    expect(atInterp.entries[0].points).toEqual([
      [0.2, 0],
      [0.4, 0],
      [0.4, 0.2],
      [0.2, 0.2],
    ]);
  });

  it("精确关键帧:实线、非遮挡;插值帧:虚线", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    const atKf = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });
    expect(atKf.entries).toHaveLength(1);
    expect(atKf.entries[0].dashed).toBe(false);

    const atInterp = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 5 });
    expect(atInterp.entries[0].dashed).toBe(true);
    expect(atInterp.entries[0].labelText).toContain("插值");
  });

  it("Mask 轨迹与其它视频轨迹共享同一编号序列", () => {
    const mask = maskTrackAnn("mask-track", "a-mask");
    const bbox = trackAnn("bbox-track", "b-box", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    const view = deriveVideoFrameViews({ ...base, annotations: [bbox, mask], frameIndex: 0 });

    expect(view.entries[0].labelText).toContain("#2");
  });

  it("entry / preview key 使用 render_key,避免 tmp id 确认后重挂", () => {
    const ann = {
      ...trackAnn("real-1", "trk1", [
        { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      ]),
      render_key: "tmp_abc",
    };

    const v = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });

    expect(v.entries[0].key).toBe("tmp_abc-trk1");
    expect(v.previews[0].key).toBe("tmp_abc");
  });

  it("遮挡关键帧:虚线 + 标签含遮挡", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual", occluded: true },
    ]);
    const v = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });
    expect(v.entries[0].dashed).toBe(true);
    expect(v.entries[0].occluded).toBe(true);
    expect(v.entries[0].labelText).toContain("遮挡");
  });

  it("hidden track 不渲染", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 0,
      hiddenTrackIds: new Set(["trk1"]),
    });
    expect(v.entries).toHaveLength(0);
    expect(v.previews).toHaveLength(0);
  });

  it.each(["diff", "raw", "final"] as const)("persisted hidden annotation 不渲染 (%s)", (mode) => {
    const ann = { ...bboxAnn("hidden", 0), is_hidden: true };
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 0,
      reviewDisplayMode: mode,
    });
    expect(v.entries).toHaveLength(0);
  });

  it("persisted hidden track excludes entries, previews, ghost, and carry-over", () => {
    const ann = {
      ...trackAnn("hidden-track", "hidden-track", [
        { frame_index: 0, bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 } },
      ]),
      is_hidden: true,
    };
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 1,
      selectedId: ann.id,
    });
    expect(v.entries).toHaveLength(0);
    expect(v.previews).toHaveLength(0);
    expect(v.ghost).toBeNull();
    expect(v.carryOverGhosts).toHaveLength(0);
  });

  it("shares raw/diff/final source visibility with the roster", () => {
    expect(visibleInReviewMode("prediction", "raw")).toBe(true);
    expect(visibleInReviewMode("manual", "raw")).toBe(false);
    expect(visibleInReviewMode("prediction", "final")).toBe(false);
    expect(visibleInReviewMode("manual", "final")).toBe(true);
    expect(visibleInReviewMode("interpolated", "diff")).toBe(true);
  });

  it("选中态标记 + 预览线只在选中时给关键帧圆点", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    const unsel = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 0 });
    expect(unsel.entries[0].selected).toBe(false);
    expect(unsel.previews[0].selected).toBe(false);
    expect(unsel.previews[0].points).toHaveLength(2);

    const sel = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 0,
      selectedId: "t1",
    });
    expect(sel.entries[0].selected).toBe(true);
    expect(sel.previews[0].selected).toBe(true);
    expect(sel.previews[0].points[0]).toMatchObject({ frame: 0, x: 0.1, y: 0.1 });
  });

  it("outside 区间内的插值帧 → 无 entry", () => {
    const keyframes = [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 20, bbox: { x: 0.6, y: 0.6, w: 0.2, h: 0.2 }, source: "manual" },
    ];
    const without = trackAnn("t1", "trk1", keyframes);
    expect(
      deriveVideoFrameViews({ ...base, annotations: [without], frameIndex: 10 }).entries,
    ).toHaveLength(1);

    const withOutside = trackAnn("t1", "trk1", keyframes);
    (withOutside.geometry as { outside?: VideoTrackOutsideRange[] }).outside = [
      { from: 5, to: 15 },
    ];
    expect(
      deriveVideoFrameViews({ ...base, annotations: [withOutside], frameIndex: 10 }).entries,
    ).toHaveLength(0);
  });

  it("选中轨迹当前帧无框 → ghost 取最近关键帧", () => {
    const ann = trackAnn(
      "t1",
      "trk1",
      [
        { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
        { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
        { frame_index: 20, bbox: { x: 0.6, y: 0.6, w: 0.2, h: 0.2 }, source: "manual" },
      ],
      "car",
    );
    (ann.geometry as { outside?: VideoTrackOutsideRange[] }).outside = [{ from: 21, to: 100 }];
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 50,
      selectedId: "t1",
    });
    expect(v.entries).toHaveLength(0);
    expect(v.ghost).not.toBeNull();
    expect(v.ghost!.labelText).toContain("参考 F20");
  });

  it("锁定轨迹 → 不显示 ghost 参考框", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 20, bbox: { x: 0.6, y: 0.6, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    (ann.geometry as { outside?: VideoTrackOutsideRange[] }).outside = [{ from: 21, to: 100 }];
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 50,
      selectedId: "t1",
      lockedTrackIds: new Set(["trk1"]),
    });
    expect(v.ghost).toBeNull();
  });

  it("referenceConfig=linear → ghost 改用恒速外推 + 预测标签", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 20, bbox: { x: 0.6, y: 0.6, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    (ann.geometry as { outside?: VideoTrackOutsideRange[] }).outside = [{ from: 21, to: 100 }];
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [ann],
      frameIndex: 25,
      selectedId: "t1",
      referenceConfig: { mode: "linear", preset: "stable" },
    });
    expect(v.ghost).not.toBeNull();
    expect(v.ghost!.labelText).toContain("预测 F20");
    expect(v.ghost!.geom.x).toBeCloseTo(0.7);
  });

  it("标签门控:none 全隐,selected 仅选中", () => {
    const ann = trackAnn("t1", "trk1", [
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
    ]);
    const none = deriveVideoFrameViews({
      annotations: [ann],
      frameIndex: 0,
      selectedId: "t1",
      visual: { ...DEFAULT_ANNOTATION_VISUAL, labelVisibility: "none" },
    });
    expect(none.labels).toHaveLength(0);

    const onlySel = deriveVideoFrameViews({
      annotations: [ann, bboxAnn("b1", 0)],
      frameIndex: 0,
      selectedId: "t1",
      visual: { ...DEFAULT_ANNOTATION_VISUAL, labelVisibility: "selected" },
    });
    expect(onlySel.labels).toHaveLength(1);
    expect(onlySel.labels[0].key).toContain("t1");
  });

  it("pending draft 进入标签", () => {
    const v = deriveVideoFrameViews({
      ...base,
      annotations: [],
      frameIndex: 0,
      pendingDraft: { geom: { x: 0.2, y: 0.2, w: 0.1, h: 0.1 }, className: "person" },
    });
    expect(v.labels.find((l) => l.key === "pending-draft")?.text).toBe("person");
  });

  describe("carryOverGhosts (跨网格帧续写参考框)", () => {
    const kf = (frame_index: number, x: number) => ({
      frame_index,
      bbox: { x, y: 0.1, w: 0.2, h: 0.2 },
      source: "manual",
    });

    it("恰好上一网格帧有关键帧、当前帧未画 → 出现 carryOver ghost(取参考框)", () => {
      const ann = trackAnn("t1", "trk1", [kf(0, 0.1)]);
      const v = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 1 });
      expect(v.entries).toHaveLength(0);
      expect(v.carryOverGhosts).toHaveLength(1);
      expect(v.carryOverGhosts[0].id).toBe("t1");
      expect(v.carryOverGhosts[0].geom).toMatchObject({ x: 0.1 });
    });

    it("网格 step>1:上一网格帧按 gridPrev 判定", () => {
      const ann = trackAnn("t1", "trk1", [kf(0, 0.1)]);
      const hit = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 10,
        samplingStep: 10,
      });
      expect(hit.carryOverGhosts).toHaveLength(1);
    });

    it("跳格(最近关键帧非上一网格帧)→ 不进 S", () => {
      const ann = trackAnn("t1", "trk1", [kf(0, 0.1)]);
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 20,
        samplingStep: 10,
      });
      expect(v.carryOverGhosts).toHaveLength(0);
    });

    it("锁定 / 隐藏 / 当前帧已画 → 排除", () => {
      const ann = trackAnn("t1", "trk1", [kf(0, 0.1)]);
      expect(
        deriveVideoFrameViews({
          ...base,
          annotations: [ann],
          frameIndex: 1,
          lockedTrackIds: new Set(["trk1"]),
        }).carryOverGhosts,
      ).toHaveLength(0);
      expect(
        deriveVideoFrameViews({
          ...base,
          annotations: [ann],
          frameIndex: 1,
          hiddenTrackIds: new Set(["trk1"]),
        }).carryOverGhosts,
      ).toHaveLength(0);
      const drawn = trackAnn("t1", "trk1", [kf(0, 0.1), kf(1, 0.3)]);
      expect(
        deriveVideoFrameViews({ ...base, annotations: [drawn], frameIndex: 1 }).carryOverGhosts,
      ).toHaveLength(0);
    });

    it("选中那条不进 carryOver(由 ghost 承),其余进", () => {
      const a = trackAnn("t1", "trk1", [kf(0, 0.1)]);
      const b = trackAnn("t2", "trk2", [kf(0, 0.5)]);
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [a, b],
        frameIndex: 1,
        selectedId: "t1",
      });
      expect(v.ghost?.id).toBe("t1");
      expect(v.carryOverGhosts.map((g) => g.id)).toEqual(["t2"]);
    });
  });

  describe("多选高亮 (selectedIds)", () => {
    const kf = (f: number) => ({ frame_index: f, bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 } });
    const t1 = trackAnn("a1", "trk1", [kf(0), kf(10)]);
    const t2 = trackAnn("a2", "trk2", [kf(0), kf(10)]);
    const t3 = trackAnn("a3", "trk3", [kf(0), kf(10)]);

    it("多选 → 选中的轨迹都高亮, 未选中的不高亮", () => {
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [t1, t2, t3],
        frameIndex: 5,
        selectedId: "a1",
        selectedIds: ["a1", "a2"],
      });
      const sel = Object.fromEntries(v.entries.map((e) => [e.id, e.selected]));
      expect(sel).toEqual({ a1: true, a2: true, a3: false });
    });

    it("多选 → 轨迹路径线也一并高亮 (与 entry 同源)", () => {
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [t1, t2, t3],
        frameIndex: 5,
        selectedId: "a1",
        selectedIds: ["a1", "a2"],
      });
      const sel = Object.fromEntries(v.previews.map((p) => [p.id, p.selected]));
      expect(sel).toEqual({ a1: true, a2: true, a3: false });
    });

    it("不传 selectedIds → 回落 primary 单选 (老调用方语义不变)", () => {
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [t1, t2],
        frameIndex: 5,
        selectedId: "a2",
      });
      expect(Object.fromEntries(v.entries.map((e) => [e.id, e.selected]))).toEqual({
        a1: false,
        a2: true,
      });
    });

    it("ghost 参考框只跟 primary 走, 不随多选扩散", () => {
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [t1, t2],
        frameIndex: 20,
        selectedId: "a2",
        selectedIds: ["a1", "a2"],
      });
      expect(v.ghost?.id ?? null).toBe("a2");
    });
  });

  describe("点集轨迹 ghost / carry-over (issue #54③)", () => {
    const OUTSIDE_20 = [{ from: 11, to: 29 }];
    const polyKfs = [
      {
        frame_index: 10,
        points: [
          [0.1, 0.1],
          [0.3, 0.1],
          [0.3, 0.3],
          [0.1, 0.3],
        ] as [number, number][],
        source: "manual",
      },
      {
        frame_index: 30,
        points: [
          [0.5, 0.5],
          [0.7, 0.5],
          [0.7, 0.7],
          [0.5, 0.7],
        ] as [number, number][],
        source: "manual",
      },
    ];
    const lineKfs = [
      {
        frame_index: 10,
        points: [
          [0.1, 0.1],
          [0.5, 0.5],
        ] as [number, number][],
        source: "manual",
      },
      {
        frame_index: 30,
        points: [
          [0.2, 0.2],
          [0.6, 0.6],
        ] as [number, number][],
        source: "manual",
      },
    ];

    it("选中 polygon 轨迹当前帧无实框 → ghost 带 points (就近取 F10), open=false, geom 为外接框", () => {
      const ann = polygonTrackAnn("pt1", "ptrk1", polyKfs);
      (ann.geometry as { outside?: unknown[] }).outside = OUTSIDE_20;
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 20,
        selectedId: "pt1",
      });
      expect(v.entries).toHaveLength(0);
      expect(v.ghost).not.toBeNull();
      expect(v.ghost!.points).toEqual([
        [0.1, 0.1],
        [0.3, 0.1],
        [0.3, 0.3],
        [0.1, 0.3],
      ]);
      expect(v.ghost!.open).toBe(false);
      expect(v.ghost!.geom.x).toBeCloseTo(0.1);
      expect(v.ghost!.geom.y).toBeCloseTo(0.1);
      expect(v.ghost!.geom.w).toBeCloseTo(0.2);
      expect(v.ghost!.geom.h).toBeCloseTo(0.2);
      expect(v.ghost!.labelText).toContain("参考 F10");
    });

    it("选中 polyline 轨迹 → ghost.open=true, points 就近取 F10", () => {
      const ann = polylineTrackAnn("lt1", "ltrk1", lineKfs);
      (ann.geometry as { outside?: unknown[] }).outside = OUTSIDE_20;
      const v = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 20,
        selectedId: "lt1",
      });
      expect(v.ghost).not.toBeNull();
      expect(v.ghost!.open).toBe(true);
      expect(v.ghost!.points).toEqual([
        [0.1, 0.1],
        [0.5, 0.5],
      ]);
    });

    it("点集轨迹被 hidden / locked → 不出 ghost", () => {
      const ann = polygonTrackAnn("pt1", "ptrk1", polyKfs);
      (ann.geometry as { outside?: unknown[] }).outside = OUTSIDE_20;
      const hidden = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 20,
        selectedId: "pt1",
        hiddenTrackIds: new Set(["ptrk1"]),
      });
      expect(hidden.ghost).toBeNull();
      const locked = deriveVideoFrameViews({
        ...base,
        annotations: [ann],
        frameIndex: 20,
        selectedId: "pt1",
        lockedTrackIds: new Set(["ptrk1"]),
      });
      expect(locked.ghost).toBeNull();
    });

    it("非选中 polygon 轨迹也纳入 carry-over ghost (回归 #54③: 此前只遍历 bbox 轨迹, 点集轨迹在此静默缺席)", () => {
      const ann = polygonTrackAnn("pt1", "ptrk1", [
        {
          frame_index: 0,
          points: [
            [0.1, 0.1],
            [0.3, 0.1],
            [0.3, 0.3],
            [0.1, 0.3],
          ],
          source: "manual",
        },
      ]);
      const v = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 1 });
      expect(v.entries).toHaveLength(0);
      expect(v.carryOverGhosts).toHaveLength(1);
      expect(v.carryOverGhosts[0].id).toBe("pt1");
      expect(v.carryOverGhosts[0].points).toEqual([
        [0.1, 0.1],
        [0.3, 0.1],
        [0.3, 0.3],
        [0.1, 0.3],
      ]);
      expect(v.carryOverGhosts[0].open).toBe(false);
      expect(v.carryOverGhosts[0].geom.x).toBeCloseTo(0.1);
      expect(v.carryOverGhosts[0].geom.y).toBeCloseTo(0.1);
      expect(v.carryOverGhosts[0].geom.w).toBeCloseTo(0.2);
      expect(v.carryOverGhosts[0].geom.h).toBeCloseTo(0.2);
    });

    it("非选中 polyline 轨迹的 carry-over ghost: open=true", () => {
      const ann = polylineTrackAnn("lt1", "ltrk1", [
        {
          frame_index: 0,
          points: [
            [0.1, 0.1],
            [0.5, 0.5],
          ],
          source: "manual",
        },
      ]);
      const v = deriveVideoFrameViews({ ...base, annotations: [ann], frameIndex: 1 });
      expect(v.carryOverGhosts).toHaveLength(1);
      expect(v.carryOverGhosts[0].open).toBe(true);
      expect(v.carryOverGhosts[0].points).toEqual([
        [0.1, 0.1],
        [0.5, 0.5],
      ]);
    });

    it("点集 carry-over: hidden / locked → 排除", () => {
      const ann = polygonTrackAnn("pt1", "ptrk1", [
        {
          frame_index: 0,
          points: [
            [0.1, 0.1],
            [0.3, 0.1],
            [0.3, 0.3],
            [0.1, 0.3],
          ],
          source: "manual",
        },
      ]);
      expect(
        deriveVideoFrameViews({
          ...base,
          annotations: [ann],
          frameIndex: 1,
          lockedTrackIds: new Set(["ptrk1"]),
        }).carryOverGhosts,
      ).toHaveLength(0);
      expect(
        deriveVideoFrameViews({
          ...base,
          annotations: [ann],
          frameIndex: 1,
          hiddenTrackIds: new Set(["ptrk1"]),
        }).carryOverGhosts,
      ).toHaveLength(0);
    });
  });
});
