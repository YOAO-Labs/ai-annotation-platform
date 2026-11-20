import { describe, expect, it } from "vitest";
import {
  interpolatePolygon,
  nearestTrackKeyframe,
  resampleClosedPolygon,
  resampleOpenPolyline,
  resolveTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
  resolveVideoMaskTrackAtFrame,
  sortedKeyframes,
  trackReferenceAtFrame,
  upsertKeyframe,
  upsertPointsKeyframe,
} from "./videoStageGeometry";
import type {
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackPolygonGeometry,
  VideoTrackPolylineGeometry,
} from "@/types";

function track(
  keyframes: VideoTrackGeometry["keyframes"],
  patch?: Partial<VideoTrackGeometry>,
): VideoTrackGeometry {
  return {
    type: "video_track_bbox",
    track_id: "trk_1",
    keyframes,
    ...patch,
  };
}

describe("videoStageGeometry", () => {
  it("resolves exact and interpolated frames from sorted keyframe indexes", () => {
    const geometry = track([
      { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
    ]);

    expect(sortedKeyframes(geometry).map((kf) => kf.frame_index)).toEqual([0, 10]);
    expect(resolveTrackAtFrame(geometry, 0)?.source).toBe("manual");
    expect(resolveTrackAtFrame(geometry, 5)?.geom).toEqual({
      x: 0.2,
      y: 0.2,
      w: 0.2,
      h: 0.2,
    });
  });

  it("does not interpolate across an outside keyframe", () => {
    const geometry = track(
      [
        { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
        { frame_index: 5, bbox: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 }, source: "manual" },
        { frame_index: 10, bbox: { x: 0.4, y: 0.4, w: 0.2, h: 0.2 }, source: "manual" },
      ],
      {
        outside: [{ from: 5, to: 5 }],
      },
    );

    expect(resolveTrackAtFrame(geometry, 5)).toBeNull();
    expect(resolveTrackAtFrame(geometry, 7)).toBeNull();
    expect(nearestTrackKeyframe(geometry, 6)?.frame_index).toBe(10);
  });

  it("treats outside ranges as higher-priority absence", () => {
    const geometry = track(
      [
        { frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
        { frame_index: 6, bbox: { x: 0.6, y: 0, w: 0.2, h: 0.2 }, source: "manual" },
      ],
      {
        outside: [{ from: 3, to: 4 }],
      },
    );

    expect(resolveTrackAtFrame(geometry, 3)).toBeNull();
    expect(resolveTrackAtFrame(geometry, 5)).toBeNull();
  });

  describe("trackReferenceAtFrame", () => {
    const geometry = track([
      { frame_index: 0, bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, source: "manual" },
      { frame_index: 10, bbox: { x: 0.3, y: 0.1, w: 0.2, h: 0.2 }, source: "manual" },
    ]);

    it("mode=off → 取最近关键帧 bbox", () => {
      const ref = trackReferenceAtFrame(geometry, 15, "off");
      expect(ref?.predicted).toBe(false);
      expect(ref?.originFrame).toBe(10);
      expect(ref?.bbox.x).toBeCloseTo(0.3);
    });

    it("mode=linear → 按前两关键帧恒速外推到当前帧", () => {
      // vx = (0.3-0.1)/10 = 0.02/帧;F15 = 0.3 + 0.02*5 = 0.4。
      const ref = trackReferenceAtFrame(geometry, 15, "linear");
      expect(ref?.predicted).toBe(true);
      expect(ref?.predictedKind).toBe("linear");
      expect(ref?.originFrame).toBe(10);
      expect(ref?.bbox.x).toBeCloseTo(0.4);
      expect(ref?.bbox.w).toBeCloseTo(0.2);
    });

    it("mode=kalman → 标记 kalman 且参考帧为末关键帧", () => {
      const ref = trackReferenceAtFrame(geometry, 15, "kalman", "agile");
      expect(ref?.predicted).toBe(true);
      expect(ref?.predictedKind).toBe("kalman");
      expect(ref?.originFrame).toBe(10);
    });

    it("预测档但先行关键帧不足两个 → 回退最近关键帧", () => {
      const single = track([
        { frame_index: 0, bbox: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 }, source: "manual" },
      ]);
      expect(trackReferenceAtFrame(single, 8, "linear")?.predicted).toBe(false);
      const k = trackReferenceAtFrame(single, 8, "kalman");
      expect(k?.predicted).toBe(false);
      expect(k?.bbox.x).toBeCloseTo(0.1);
    });
  });

  it("clears explicit outside coverage when upserting a visible keyframe", () => {
    const geometry = track(
      [{ frame_index: 0, bbox: { x: 0, y: 0, w: 0.2, h: 0.2 }, source: "manual" }],
      {
        outside: [{ from: 2, to: 4 }],
      },
    );

    const next = upsertKeyframe(geometry, 3, { x: 0.3, y: 0, w: 0.2, h: 0.2 });
    const updated = resolveTrackAtFrame(next, 3);

    expect(next.outside).toEqual([
      { from: 2, to: 2, source: "manual" },
      { from: 4, to: 4, source: "manual" },
    ]);
    expect(updated?.geom.x).toBe(0.3);
  });
});

// ── polygon track 弧长参数化插值 (前端, 镜像后端 lerp_polygon) ──

const SQUARE_A: [number, number][] = [
  [0, 0],
  [0.2, 0],
  [0.2, 0.2],
  [0, 0.2],
];
const SQUARE_B: [number, number][] = [
  [0.4, 0],
  [0.6, 0],
  [0.6, 0.2],
  [0.4, 0.2],
];

function polygonTrack(
  keyframes: VideoTrackPolygonGeometry["keyframes"],
  patch?: Partial<VideoTrackPolygonGeometry>,
): VideoTrackPolygonGeometry {
  return { type: "video_track_polygon", track_id: "poly_1", keyframes, ...patch };
}

describe("videoStageGeometry · polygon track", () => {
  it("resampleClosedPolygon 方块→4 点返回原顶点", () => {
    expect(resampleClosedPolygon(SQUARE_A, 4)).toEqual(SQUARE_A);
  });

  it("resampleClosedPolygon 退化输入安全回退", () => {
    expect(resampleClosedPolygon([[0.1, 0.1]], 4)).toEqual([[0.1, 0.1]]);
  });

  it("interpolatePolygon 顶点数不等时重采样到公共 n, 不抛异常", () => {
    const tri = {
      frame_index: 0,
      points: [
        [0, 0],
        [0.4, 0],
        [0.2, 0.4],
      ] as [number, number][],
      source: "manual" as const,
    };
    const sq = { frame_index: 10, points: SQUARE_B, source: "manual" as const };
    expect(interpolatePolygon(tri, sq, 5)).toHaveLength(4);
  });

  it("resolveVideoPolygonTrackAtFrame: 精确关键帧 / 插值 / outside→null", () => {
    const geom = polygonTrack([
      { frame_index: 0, points: SQUARE_A, source: "manual" },
      { frame_index: 10, points: SQUARE_B, source: "manual" },
    ]);
    expect(resolveVideoPolygonTrackAtFrame(geom, 0)?.points).toEqual(SQUARE_A);
    const mid = resolveVideoPolygonTrackAtFrame(geom, 5);
    expect(mid?.source).toBe("interpolated");
    expect(mid?.points).toEqual([
      [0.2, 0],
      [0.4, 0],
      [0.4, 0.2],
      [0.2, 0.2],
    ]);

    const withOutside = polygonTrack(
      [
        { frame_index: 0, points: SQUARE_A, source: "manual" },
        { frame_index: 10, points: SQUARE_B, source: "manual" },
      ],
      { outside: [{ from: 4, to: 6, source: "manual" }] },
    );
    expect(resolveVideoPolygonTrackAtFrame(withOutside, 5)).toBeNull();
  });

  it("upsertPointsKeyframe: 精确帧替换 points, 保持排序", () => {
    const geom = polygonTrack([
      { frame_index: 0, points: SQUARE_A, source: "manual" },
      { frame_index: 10, points: SQUARE_B, source: "manual" },
    ]);
    const edited: [number, number][] = [
      [0.05, 0.05],
      [0.2, 0],
      [0.2, 0.2],
      [0, 0.2],
    ];
    const next = upsertPointsKeyframe(geom, 0, edited);
    expect(next.keyframes).toHaveLength(2);
    expect(next.keyframes[0]).toMatchObject({ frame_index: 0, points: edited, source: "manual" });
    expect(next.keyframes[1].frame_index).toBe(10);
  });

  it("upsertPointsKeyframe: 插值帧物化为新 manual 关键帧 + 清该帧 outside", () => {
    const geom = polygonTrack(
      [
        { frame_index: 0, points: SQUARE_A, source: "manual" },
        { frame_index: 10, points: SQUARE_B, source: "manual" },
      ],
      { outside: [{ from: 4, to: 6, source: "manual" }] },
    );
    const materialized: [number, number][] = [
      [0.25, 0],
      [0.45, 0],
      [0.45, 0.2],
      [0.25, 0.2],
    ];
    const next = upsertPointsKeyframe(geom, 5, materialized);
    expect(next.keyframes).toHaveLength(3);
    expect(next.keyframes.find((kf) => kf.frame_index === 5)).toMatchObject({
      points: materialized,
      source: "manual",
    });
    // 落新可见关键帧 → outside [4,6] 被拆成 [4,4] 与 [6,6]。
    expect(next.outside).toEqual([
      { from: 4, to: 4, source: "manual" },
      { from: 6, to: 6, source: "manual" },
    ]);
  });
});

// ── polyline (开路径) track 插值 (前端, 镜像后端 lerp_polyline) ──

const LINE_A: [number, number][] = [
  [0, 0],
  [0.2, 0],
  [0.4, 0],
];
const LINE_B: [number, number][] = [
  [0, 0.2],
  [0.2, 0.2],
  [0.4, 0.2],
];

function polylineTrack(
  keyframes: VideoTrackPolylineGeometry["keyframes"],
  patch?: Partial<VideoTrackPolylineGeometry>,
): VideoTrackPolylineGeometry {
  return { type: "video_track_polyline", track_id: "line_1", keyframes, ...patch };
}

describe("videoStageGeometry · polyline track", () => {
  it("resampleOpenPolyline 保端点 + 等距三点线不变", () => {
    expect(resampleOpenPolyline(LINE_A, 3)).toEqual(LINE_A);
    const out = resampleOpenPolyline(
      [
        [0, 0],
        [0.4, 0],
      ],
      5,
    );
    expect(out[0]).toEqual([0, 0]);
    expect(out[4]).toEqual([0.4, 0]);
    expect(out).toHaveLength(5);
  });

  it("resolveVideoPolylineTrackAtFrame: 精确关键帧与中点插值", () => {
    const geom = polylineTrack([
      { frame_index: 0, points: LINE_A, source: "manual" },
      { frame_index: 10, points: LINE_B, source: "manual" },
    ]);
    expect(resolveVideoPolylineTrackAtFrame(geom, 0)?.points).toEqual(LINE_A);
    expect(resolveVideoPolylineTrackAtFrame(geom, 5)?.source).toBe("interpolated");
    expect(resolveVideoPolylineTrackAtFrame(geom, 5)?.points).toEqual([
      [0, 0.1],
      [0.2, 0.1],
      [0.4, 0.1],
    ]);
  });
});

describe("videoStageGeometry · raster mask nearest hold", () => {
  const ref = (seed: string) => ({
    encoding: "coco_rle_ref" as const,
    size: [2, 3] as [number, number],
    object_key: `raster-masks/sha256/${seed}${seed}/${seed}${seed}/${seed.repeat(64)}.json`,
    sha256: seed.repeat(64),
    runs: 4,
    bytes: 64,
  });
  const geometry: VideoTrackMaskGeometry = {
    type: "video_track_mask",
    track_id: "mask-1",
    keyframes: [
      { frame_index: 4, mask: ref("a"), source: "manual" },
      { frame_index: 8, mask: ref("b"), source: "prediction", occluded: true },
    ],
    outside: [{ from: 5, to: 5 }],
  };

  it("holds endpoints, chooses earlier on ties, and lets outside win", () => {
    expect(resolveVideoMaskTrackAtFrame(geometry, 0)?.keyframeFrame).toBe(4);
    expect(resolveVideoMaskTrackAtFrame(geometry, 5)).toBeNull();
    expect(resolveVideoMaskTrackAtFrame(geometry, 6)?.keyframeFrame).toBe(4);
    expect(resolveVideoMaskTrackAtFrame(geometry, 20)?.keyframeFrame).toBe(8);
  });
});
