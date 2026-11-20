/**
 * 视频 Konva 交互状态机纯函数测试。
 *
 * advanceDrag(拖拽推进)/ resolveDragCommit(松手提交)是栈无关纯函数,与旧 SVG 栈
 * VideoStage 的 onPointerMove/finishDrag 语义对齐。真实 Konva 事件/命中由 konva-mock
 * 与 Playwright 兜底(决策 C)。
 */
import { describe, expect, it } from "vitest";
import type { AnnotationResponse, VideoBboxGeometry, VideoTrackGeometry } from "@/types";
import {
  advanceDrag,
  isSamCandidateNavTool,
  isSamProbeTool,
  resolveDragCommit,
  samProbeMode,
  type ResolveDragCommitCtx,
} from "./videoKonvaInteraction";
import type { VideoDragState } from "./videoStageTypes";

function bbox(id: string, frameIndex = 0): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_bbox",
      frame_index: frameIndex,
      x: 0.1,
      y: 0.1,
      w: 0.2,
      h: 0.2,
    } satisfies VideoBboxGeometry,
  } as unknown as AnnotationResponse;
}

function track(id: string, trackId = "t1"): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_track_bbox",
      track_id: trackId,
      keyframes: [
        {
          frame_index: 0,
          bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
          source: "manual",
          occluded: false,
        },
      ],
    } satisfies VideoTrackGeometry,
  } as unknown as AnnotationResponse;
}

function polygon(id: string, points: [number, number][], frameIndex = 0): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: { type: "video_polygon", frame_index: frameIndex, points },
  } as unknown as AnnotationResponse;
}

function polygonTrack(id: string, points: [number, number][], frameIndex = 0): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_track_polygon",
      track_id: "tp1",
      keyframes: [{ frame_index: frameIndex, points, source: "manual", occluded: false }],
    },
  } as unknown as AnnotationResponse;
}

function obb(id: string): AnnotationResponse {
  return {
    id,
    class_name: "car",
    geometry: {
      type: "video_rotated_bbox",
      frame_index: 0,
      cx: 0.5,
      cy: 0.5,
      w: 0.4,
      h: 0.2,
      angle: 0,
    },
  } as unknown as AnnotationResponse;
}

function keypoints(id: string): AnnotationResponse {
  return {
    id,
    class_name: "person",
    geometry: {
      type: "video_keypoint",
      frame_index: 0,
      points: [
        { x: 0.2, y: 0.2, v: 2 },
        { x: 0.4, y: 0.4, v: 1 },
      ],
    },
  } as unknown as AnnotationResponse;
}

const baseCtx: ResolveDragCommitCtx = {
  annotations: [],
  videoTool: "box",
  selectedTrack: null,
  lockedTrackIds: new Set(),
  // 默认帧 10:与 track() 唯一关键帧(frame 0)不同帧, 使「延展」用例走跨帧路径。
  frameIndex: 10,
};

describe("advanceDrag", () => {
  it("draw → 更新 current 点", () => {
    const drag: VideoDragState = { kind: "draw", start: { x: 0, y: 0 }, current: { x: 0, y: 0 } };
    const next = advanceDrag(drag, { x: 0.5, y: 0.4 });
    expect(next).toEqual({ kind: "draw", start: { x: 0, y: 0 }, current: { x: 0.5, y: 0.4 } });
  });

  it("polyVertex → 只移动被拖顶点 (clamp [0,1])", () => {
    const origin: [number, number][] = [
      [0.1, 0.1],
      [0.4, 0.1],
      [0.25, 0.4],
    ];
    const drag: VideoDragState = {
      kind: "polyVertex",
      id: "p1",
      vidx: 1,
      start: { x: 0.4, y: 0.1 },
      origin,
      current: origin,
    };
    const next = advanceDrag(drag, { x: 1.5, y: 0.2 });
    if (next?.kind !== "polyVertex") throw new Error("expected polyVertex");
    expect(next.current).toEqual([
      [0.1, 0.1],
      [1, 0.2],
      [0.25, 0.4],
    ]); // 顶点 1 移动且 x clamp 到 1
  });

  it("polyMove → 整体平移所有顶点 (clamp [0,1])", () => {
    const origin: [number, number][] = [
      [0.1, 0.1],
      [0.4, 0.1],
      [0.25, 0.4],
    ];
    const drag: VideoDragState = {
      kind: "polyMove",
      id: "p1",
      start: { x: 0.2, y: 0.2 },
      origin,
      current: origin,
    };
    const next = advanceDrag(drag, { x: 0.3, y: 0.3 }); // dx=0.1, dy=0.1
    if (next?.kind !== "polyMove") throw new Error("expected polyMove");
    const rounded = next.current.map(([x, y]) => [
      Math.round(x * 100) / 100,
      Math.round(y * 100) / 100,
    ]);
    expect(rounded).toEqual([
      [0.2, 0.2],
      [0.5, 0.2],
      [0.35, 0.5],
    ]);
  });

  it("move → 平移 origin 并 clamp 到 [0,1]", () => {
    const drag: VideoDragState = {
      kind: "move",
      id: "a",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    };
    const next = advanceDrag(drag, { x: 0.3, y: 0.25 });
    expect(next?.kind).toBe("move");
    if (next?.kind === "move") {
      expect(next.current.x).toBeCloseTo(0.3, 5);
      expect(next.current.y).toBeCloseTo(0.25, 5);
      expect(next.current.w).toBeCloseTo(0.2, 5);
    }
  });

  it("resize(se 角)→ 复用 applyResize 扩展宽高", () => {
    const drag: VideoDragState = {
      kind: "resize",
      id: "a",
      dir: "se",
      start: { x: 0.3, y: 0.3 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    };
    const next = advanceDrag(drag, { x: 0.5, y: 0.4 });
    expect(next?.kind).toBe("resize");
    if (next?.kind === "resize") {
      expect(next.current.w).toBeCloseTo(0.4, 5);
      expect(next.current.h).toBeCloseTo(0.3, 5);
    }
  });

  it("OBB 按局部轴缩放、移动与旋转", () => {
    const origin = obb("obb").geometry;
    if (origin.type !== "video_rotated_bbox") throw new Error("expected obb");
    const resize = advanceDrag(
      {
        kind: "obbResize",
        id: "obb",
        dir: "e",
        start: { x: 0.7, y: 0.5 },
        origin,
        current: origin,
        imageSize: { w: 1000, h: 500 },
      },
      { x: 0.8, y: 0.5 },
    );
    expect(resize?.kind).toBe("obbResize");
    if (resize?.kind === "obbResize") expect(resize.current.w).toBeCloseTo(0.5);

    const move = advanceDrag(
      {
        kind: "obbMove",
        id: "obb",
        start: { x: 0.5, y: 0.5 },
        origin,
        current: origin,
        imageSize: { w: 1000, h: 500 },
      },
      { x: 0.6, y: 0.6 },
    );
    if (move?.kind !== "obbMove") throw new Error("expected obbMove");
    expect(move.current.cx).toBeCloseTo(0.6);
    expect(move.current.cy).toBeCloseTo(0.6);

    const rotate = advanceDrag(
      {
        kind: "obbRotate",
        id: "obb",
        origin,
        current: origin,
        startPointerAngle: -Math.PI / 2,
        imageSize: { w: 1000, h: 500 },
      },
      { x: 0.7, y: 0.5 },
    );
    if (rotate?.kind !== "obbRotate") throw new Error("expected obbRotate");
    expect(rotate.current.angle).toBeCloseTo(90);
  });

  it("关键点节点拖动保留可见性", () => {
    const origin = [
      { x: 0.2, y: 0.2, v: 2 as const },
      { x: 0.4, y: 0.4, v: 1 as const },
    ];
    const next = advanceDrag(
      { kind: "keypointNode", id: "kp", nodeIdx: 1, origin, current: origin },
      { x: 0.7, y: 0.8 },
    );
    if (next?.kind !== "keypointNode") throw new Error("expected keypointNode");
    expect(next.current[1]).toEqual({ x: 0.7, y: 0.8, v: 1 });
  });

  it("pan / null → 原样返回", () => {
    const pan: VideoDragState = { kind: "pan", sx: 1, sy: 2 };
    expect(advanceDrag(pan, { x: 0.5, y: 0.5 })).toBe(pan);
    expect(advanceDrag(null, { x: 0.5, y: 0.5 })).toBeNull();
  });
});

describe("resolveDragCommit", () => {
  const draw = (a: { x: number; y: number }, b: { x: number; y: number }): VideoDragState => ({
    kind: "draw",
    start: a,
    current: b,
  });

  it("draw 太小 → none", () => {
    const out = resolveDragCommit(
      draw({ x: 0.2, y: 0.2 }, { x: 0.201, y: 0.201 }),
      { x: 0.201, y: 0.201 },
      baseCtx,
    );
    expect(out.type).toBe("none");
  });

  it("box 工具 draw → 新 video_bbox", () => {
    const out = resolveDragCommit(
      draw({ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }),
      { x: 0.4, y: 0.4 },
      baseCtx,
    );
    expect(out).toMatchObject({ type: "draw", kind: "video_bbox" });
    if (out.type === "draw") {
      expect(out.geom.x).toBeCloseTo(0.1, 5);
      expect(out.geom.y).toBeCloseTo(0.1, 5);
      expect(out.geom.w).toBeCloseTo(0.3, 5);
      expect(out.geom.h).toBeCloseTo(0.3, 5);
    }
  });

  it("rotated-box 工具 draw → 新 video_rotated_bbox", () => {
    const out = resolveDragCommit(
      draw({ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }),
      { x: 0.4, y: 0.4 },
      { ...baseCtx, videoTool: "rotated-box" },
    );
    expect(out).toMatchObject({ type: "draw", kind: "video_rotated_bbox" });
  });

  it("OBB 与关键点编辑提交保留专用几何", () => {
    const obbAnn = obb("obb");
    if (obbAnn.geometry.type !== "video_rotated_bbox") throw new Error("expected obb");
    const moved = { ...obbAnn.geometry, cx: 0.6 };
    const obbCommitted = resolveDragCommit(
      {
        kind: "obbMove",
        id: "obb",
        start: { x: 0.5, y: 0.5 },
        origin: obbAnn.geometry,
        current: moved,
        imageSize: { w: 1000, h: 500 },
      },
      { x: 0.6, y: 0.5 },
      { ...baseCtx, annotations: [obbAnn] },
    );
    expect(obbCommitted).toMatchObject({ type: "rotated", geometry: { cx: 0.6 } });

    const kpAnn = keypoints("kp");
    if (kpAnn.geometry.type !== "video_keypoint") throw new Error("expected keypoint");
    const current = kpAnn.geometry.points.map((point, index) =>
      index === 0 ? { ...point, x: 0.3 } : point,
    );
    const keypointCommitted = resolveDragCommit(
      {
        kind: "keypointNode",
        id: "kp",
        nodeIdx: 0,
        origin: kpAnn.geometry.points,
        current,
      },
      { x: 0.3, y: 0.2 },
      { ...baseCtx, annotations: [kpAnn] },
    );
    expect(keypointCommitted.type).toBe("keypoint");
    if (keypointCommitted.type === "keypoint")
      expect(keypointCommitted.points[0]).toEqual({ x: 0.3, y: 0.2, v: 2 });
  });

  it("select 工具 draw → none", () => {
    const out = resolveDragCommit(
      draw({ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }),
      { x: 0.4, y: 0.4 },
      {
        ...baseCtx,
        videoTool: "select",
      },
    );
    expect(out.type).toBe("none");
  });

  it("track 工具 + 选中轨迹未锁 + 当前帧无关键帧 draw → 延展该轨迹(落新关键帧)", () => {
    const t = track("trk-1"); // 唯一关键帧在 frame 0;baseCtx.frameIndex 为 10 → 跨帧
    const out = resolveDragCommit(
      draw({ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }),
      { x: 0.4, y: 0.4 },
      {
        ...baseCtx,
        videoTool: "track",
        selectedTrack: t as AnnotationResponse & { geometry: VideoTrackGeometry },
      },
    );
    expect(out).toMatchObject({ type: "track" });
    if (out.type === "track") expect(out.ann.id).toBe("trk-1");
  });

  it("track 工具 + 选中轨迹未锁 + 当前帧已有关键帧 draw → 新建轨迹(不吞旧框)", () => {
    const t = track("trk-1"); // 关键帧在 frame 0
    const out = resolveDragCommit(
      draw({ x: 0.5, y: 0.5 }, { x: 0.7, y: 0.7 }),
      { x: 0.7, y: 0.7 },
      {
        ...baseCtx,
        videoTool: "track",
        selectedTrack: t as AnnotationResponse & { geometry: VideoTrackGeometry },
        frameIndex: 0, // 同帧:选中轨迹在 frame 0 已有关键帧 → 判为「标第二个物体」
      },
    );
    expect(out).toMatchObject({ type: "draw", kind: "video_track_bbox" });
  });

  it("track 工具 + 选中轨迹已锁 draw → 退化为新 video_track_bbox", () => {
    const t = track("trk-1", "t1");
    const out = resolveDragCommit(
      draw({ x: 0.1, y: 0.1 }, { x: 0.4, y: 0.4 }),
      { x: 0.4, y: 0.4 },
      {
        ...baseCtx,
        videoTool: "track",
        selectedTrack: t as AnnotationResponse & { geometry: VideoTrackGeometry },
        lockedTrackIds: new Set(["t1"]),
      },
    );
    expect(out).toMatchObject({ type: "draw", kind: "video_track_bbox" });
  });

  it("move bbox → 更新该 bbox 几何", () => {
    const ann = bbox("b1");
    const drag: VideoDragState = {
      kind: "move",
      id: "b1",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
    };
    const out = resolveDragCommit(drag, { x: 0.2, y: 0.2 }, { ...baseCtx, annotations: [ann] });
    expect(out).toMatchObject({ type: "bbox" });
    if (out.type === "bbox") expect(out.geom).toMatchObject({ x: 0.2, y: 0.2 });
  });

  it("move track → 走 track 提交", () => {
    const ann = track("trk-2", "t2");
    const drag: VideoDragState = {
      kind: "move",
      id: "trk-2",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
    };
    const out = resolveDragCommit(drag, { x: 0.2, y: 0.2 }, { ...baseCtx, annotations: [ann] });
    expect(out).toMatchObject({ type: "track" });
  });

  it("polyVertex/polyMove → poly 提交带新 points", () => {
    const ann = polygon("p1", [
      [0.1, 0.1],
      [0.4, 0.1],
      [0.25, 0.4],
    ]);
    const moved: [number, number][] = [
      [0.1, 0.1],
      [0.5, 0.15],
      [0.25, 0.4],
    ];
    const drag: VideoDragState = {
      kind: "polyVertex",
      id: "p1",
      vidx: 1,
      start: { x: 0.4, y: 0.1 },
      origin: ann.geometry.type === "video_polygon" ? ann.geometry.points : [],
      current: moved,
    };
    const out = resolveDragCommit(drag, { x: 0.5, y: 0.15 }, { ...baseCtx, annotations: [ann] });
    expect(out).toMatchObject({ type: "poly" });
    if (out.type === "poly") expect(out.points).toEqual(moved);
  });

  it("polyVertex 命中 polygon track → poly 提交 (commit 再据类型 upsert 关键帧)", () => {
    const ann = polygonTrack(
      "tp1",
      [
        [0.1, 0.1],
        [0.4, 0.1],
        [0.25, 0.4],
      ],
      5,
    );
    const moved: [number, number][] = [
      [0.15, 0.12],
      [0.4, 0.1],
      [0.25, 0.4],
    ];
    const drag: VideoDragState = {
      kind: "polyVertex",
      id: "tp1",
      vidx: 0,
      start: { x: 0.1, y: 0.1 },
      origin: [
        [0.1, 0.1],
        [0.4, 0.1],
        [0.25, 0.4],
      ],
      current: moved,
    };
    const out = resolveDragCommit(drag, { x: 0.15, y: 0.12 }, { ...baseCtx, annotations: [ann] });
    expect(out).toMatchObject({ type: "poly" });
    if (out.type === "poly") expect(out.points).toEqual(moved);
  });

  it("poly 拖拽找不到 ann / 非多边形 → none", () => {
    const drag: VideoDragState = {
      kind: "polyMove",
      id: "missing",
      start: { x: 0, y: 0 },
      origin: [],
      current: [[0.2, 0.2]],
    };
    expect(resolveDragCommit(drag, { x: 0, y: 0 }, baseCtx).type).toBe("none");
    const bboxAnn = bbox("b1");
    const drag2: VideoDragState = {
      kind: "polyMove",
      id: "b1",
      start: { x: 0, y: 0 },
      origin: [],
      current: [[0.2, 0.2]],
    };
    expect(
      resolveDragCommit(drag2, { x: 0, y: 0 }, { ...baseCtx, annotations: [bboxAnn] }).type,
    ).toBe("none");
  });

  it("resize 太小 → none;找不到 ann → none", () => {
    const ann = bbox("b1");
    const tiny: VideoDragState = {
      kind: "resize",
      id: "b1",
      dir: "se",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.1, y: 0.1, w: 0.001, h: 0.001 },
    };
    expect(
      resolveDragCommit(tiny, { x: 0.1, y: 0.1 }, { ...baseCtx, annotations: [ann] }).type,
    ).toBe("none");

    const missing: VideoDragState = {
      kind: "move",
      id: "ghost-x",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
    };
    expect(resolveDragCommit(missing, { x: 0.2, y: 0.2 }, baseCtx).type).toBe("none");
  });
});

// 交互式 SAM 提示 (smart-point / smart-box)
describe("samProbe · 交互式 SAM 提示", () => {
  const probe = (
    mode: "point" | "bbox",
    start: { x: number; y: number },
    current: { x: number; y: number },
    alt = false,
  ): VideoDragState => ({ kind: "samProbe", mode, start, current, alt });

  it("advanceDrag 推进 current（bbox 拖框实时预览）", () => {
    const next = advanceDrag(probe("bbox", { x: 0.1, y: 0.1 }, { x: 0.1, y: 0.1 }), {
      x: 0.4,
      y: 0.5,
    });
    expect(next).toMatchObject({ kind: "samProbe", current: { x: 0.4, y: 0.5 } });
  });

  it("point 模式提交起点坐标，与松手位置无关（零位移点击）", () => {
    const commit = resolveDragCommit(
      probe("point", { x: 0.3, y: 0.6 }, { x: 0.3, y: 0.6 }),
      { x: 0.31, y: 0.61 },
      { ...baseCtx, videoTool: "smart-point" },
    );
    expect(commit).toEqual({ type: "samProbe", mode: "point", pt: [0.3, 0.6], alt: false });
  });

  it("point 模式带 alt → 负点", () => {
    const commit = resolveDragCommit(
      probe("point", { x: 0.2, y: 0.2 }, { x: 0.2, y: 0.2 }, true),
      { x: 0.2, y: 0.2 },
      { ...baseCtx, videoTool: "smart-point" },
    );
    expect(commit).toMatchObject({ mode: "point", alt: true });
  });

  it("bbox 模式归一化为 [x1,y1,x2,y2]，反向拖拽也正序", () => {
    const commit = resolveDragCommit(
      probe("bbox", { x: 0.6, y: 0.7 }, { x: 0.6, y: 0.7 }),
      { x: 0.2, y: 0.3 },
      { ...baseCtx, videoTool: "smart-box" },
    );
    expect(commit).toEqual({
      type: "samProbe",
      mode: "bbox",
      bbox: [0.2, 0.3, 0.6, 0.7],
      alt: false,
    });
  });

  it("bbox 拖拽小于最小阈值 → 不提交（误点不该喂退化框给后端）", () => {
    const commit = resolveDragCommit(
      probe("bbox", { x: 0.5, y: 0.5 }, { x: 0.5, y: 0.5 }),
      { x: 0.502, y: 0.502 }, // 0.002 < SAM_MIN_DRAG(0.005)
      { ...baseCtx, videoTool: "smart-box" },
    );
    expect(commit).toEqual({ type: "none" });
  });

  it("samProbe 不产生任何几何创建 / 更新提交", () => {
    const commit = resolveDragCommit(
      probe("bbox", { x: 0.1, y: 0.1 }, { x: 0.1, y: 0.1 }),
      { x: 0.5, y: 0.5 },
      { ...baseCtx, videoTool: "smart-box", annotations: [bbox("a1")] },
    );
    expect(commit.type).toBe("samProbe");
    expect(commit).not.toHaveProperty("ann");
    expect(commit).not.toHaveProperty("geom");
  });
});

// PR2 · exemplar (视觉示例框): 同为拖框, 但派发到 runExemplar 而非 runBbox。
describe("samProbe · exemplar", () => {
  const probe = (
    mode: "point" | "bbox" | "exemplar",
    start: { x: number; y: number },
    alt = false,
  ): VideoDragState => ({ kind: "samProbe", mode, start, current: start, alt });

  it("exemplar 提交 mode=exemplar, 不与 bbox 混淆", () => {
    const commit = resolveDragCommit(
      probe("exemplar", { x: 0.2, y: 0.2 }),
      { x: 0.5, y: 0.5 },
      { ...baseCtx, videoTool: "exemplar" },
    );
    expect(commit).toEqual({
      type: "samProbe",
      mode: "exemplar",
      bbox: [0.2, 0.2, 0.5, 0.5],
      alt: false,
    });
  });

  it("exemplar 的 alt = 负框（排误检）", () => {
    const commit = resolveDragCommit(
      probe("exemplar", { x: 0.2, y: 0.2 }, true),
      { x: 0.5, y: 0.5 },
      { ...baseCtx, videoTool: "exemplar" },
    );
    expect(commit).toMatchObject({ mode: "exemplar", alt: true });
  });

  it("exemplar 同样受最小拖拽阈值约束", () => {
    const commit = resolveDragCommit(
      probe("exemplar", { x: 0.5, y: 0.5 }),
      { x: 0.502, y: 0.502 },
      { ...baseCtx, videoTool: "exemplar" },
    );
    expect(commit).toEqual({ type: "none" });
  });

  it("isSamProbeTool / samProbeMode 覆盖三个 AI 工具, 几何工具不误伤", () => {
    expect(isSamProbeTool("smart-point")).toBe(true);
    expect(isSamProbeTool("smart-box")).toBe(true);
    expect(isSamProbeTool("exemplar")).toBe(true);
    expect(isSamProbeTool("box")).toBe(false);
    expect(isSamProbeTool("polygon")).toBe(false);
    expect(isSamProbeTool("select")).toBe(false);

    expect(samProbeMode("smart-point")).toBe("point");
    expect(samProbeMode("smart-box")).toBe("bbox");
    expect(samProbeMode("exemplar")).toBe("exemplar");
  });

  it("magic-box 骑 interactive_box prompt（提示形态与 smart-box 相同）", () => {
    expect(isSamProbeTool("magic-box")).toBe(true);
    expect(samProbeMode("magic-box")).toBe("bbox");
  });

  it("magic-box 不参与候选导航（单候选，自动弹 popover）", () => {
    expect(isSamCandidateNavTool("smart-point")).toBe(true);
    expect(isSamCandidateNavTool("smart-box")).toBe(true);
    expect(isSamCandidateNavTool("exemplar")).toBe(true);
    expect(isSamCandidateNavTool("magic-box")).toBe(false);
    expect(isSamCandidateNavTool("box")).toBe(false);
  });
});
