import { describe, expect, it } from "vitest";
import type { AiBox } from "../state/transforms";
import {
  adjacentPredictedFrame,
  aiBoxOnFrame,
  collectPredictedFrames,
  dedupeAiBoxesById,
  resolveAiBoxAtFrame,
} from "./aiBoxFrames";

const bbox = { x: 0, y: 0, w: 0.2, h: 0.2 };

function bboxBox(id: string, frameIndex: number): AiBox {
  return {
    id,
    geometry: { type: "video_bbox", frame_index: frameIndex, bbox },
  } as unknown as AiBox;
}

function trackBox(id: string, frames: number[]): AiBox {
  return {
    id,
    geometry: {
      type: "video_track_bbox",
      track_id: id,
      keyframes: frames.map((frame_index) => ({ frame_index, bbox, source: "prediction" })),
    },
  } as unknown as AiBox;
}

function pointsTrackBox(id: string, type: "video_track_polygon" | "video_track_polyline"): AiBox {
  return {
    id,
    geometry: {
      type,
      track_id: id,
      keyframes: [
        {
          frame_index: 0,
          points: [[0.1, 0.1], [0.4, 0.1], ...(type === "video_track_polygon" ? [[0.3, 0.4]] : [])],
          source: "prediction",
        },
        {
          frame_index: 10,
          points: [[0.2, 0.2], [0.5, 0.2], ...(type === "video_track_polygon" ? [[0.4, 0.5]] : [])],
          source: "prediction",
        },
      ],
      outside: [],
    },
  } as unknown as AiBox;
}

function maskTrackBox(id: string): AiBox {
  const digest = "a".repeat(64);
  return {
    id,
    geometry: {
      type: "video_track_mask",
      track_id: id,
      keyframes: [
        {
          frame_index: 2,
          source: "prediction",
          mask: {
            encoding: "coco_rle_ref",
            size: [2, 3],
            object_key: `raster-masks/sha256/aa/aa/${digest}.json`,
            sha256: digest,
            runs: 3,
            bytes: 64,
          },
        },
      ],
      outside: [{ from: 6, to: 7, source: "prediction" }],
    },
  } as unknown as AiBox;
}

const videoSingles = [
  {
    type: "video_polygon",
    frame_index: 4,
    points: [
      [0.1, 0.1],
      [0.4, 0.1],
      [0.2, 0.4],
    ],
  },
  {
    type: "video_polyline",
    frame_index: 4,
    points: [
      [0.1, 0.1],
      [0.4, 0.4],
    ],
  },
  {
    type: "video_rotated_bbox",
    frame_index: 4,
    cx: 0.5,
    cy: 0.5,
    w: 0.2,
    h: 0.1,
    angle: 20,
  },
  {
    type: "video_keypoint",
    frame_index: 4,
    points: [{ x: 0.2, y: 0.3, v: 2 }],
  },
  {
    type: "video_mask",
    frame_index: 4,
    mask: {
      encoding: "coco_rle_ref",
      size: [2, 3],
      object_key: "mask.json",
      sha256: "a".repeat(64),
      runs: 3,
      bytes: 64,
    },
  },
] as const;

describe("aiBoxFrames", () => {
  it("collects predicted frames from video_bbox frame_index", () => {
    const frames = collectPredictedFrames([bboxBox("pred-1-0", 3), bboxBox("pred-2-0", 1)]);
    expect(frames).toEqual([1, 3]);
  });

  it("collects predicted frames from video_track_bbox keyframes", () => {
    const frames = collectPredictedFrames([trackBox("pred-9-0", [0, 10, 5])]);
    expect(frames).toEqual([0, 5, 10]);
  });

  it("dedupes by id before collecting (offset-pagination overlap)", () => {
    const dup = bboxBox("pred-1-0", 3);
    const frames = collectPredictedFrames([dup, dup, bboxBox("pred-1-0", 3)]);
    expect(frames).toEqual([3]);
    expect(dedupeAiBoxesById([dup, dup])).toHaveLength(1);
  });

  it("finds adjacent predicted frame in both directions", () => {
    const frames = [1, 3, 7];
    expect(adjacentPredictedFrame(frames, 3, 1)).toBe(7);
    expect(adjacentPredictedFrame(frames, 3, -1)).toBe(1);
    expect(adjacentPredictedFrame(frames, 7, 1)).toBeNull();
    expect(adjacentPredictedFrame(frames, 1, -1)).toBeNull();
  });

  it("aiBoxOnFrame matches video_bbox by exact frame", () => {
    const box = bboxBox("pred-1-0", 4);
    expect(aiBoxOnFrame(box, 4)).toBe(true);
    expect(aiBoxOnFrame(box, 5)).toBe(false);
  });

  it.each(videoSingles)("matches $type by exact frame", (geometry) => {
    const box = { id: `pred-${geometry.type}`, geometry } as unknown as AiBox;
    expect(aiBoxOnFrame(box, 4)).toBe(true);
    expect(aiBoxOnFrame(box, 5)).toBe(false);
    expect(resolveAiBoxAtFrame(box, 4)).toBe(box);
    expect(resolveAiBoxAtFrame(box, 5)).toBeNull();
  });

  it("aiBoxOnFrame matches video_track_bbox on interpolated frames", () => {
    const box = trackBox("pred-9-0", [0, 10]);
    expect(aiBoxOnFrame(box, 0)).toBe(true);
    expect(aiBoxOnFrame(box, 5)).toBe(true);
    expect(aiBoxOnFrame(box, 20)).toBe(false);
  });

  it("resolveAiBoxAtFrame returns video_bbox as-is on its frame, null otherwise", () => {
    const box = bboxBox("pred-1-0", 4);
    expect(resolveAiBoxAtFrame(box, 4)).toBe(box);
    expect(resolveAiBoxAtFrame(box, 5)).toBeNull();
  });

  it("resolveAiBoxAtFrame overrides track x/y/w/h with the current-frame geometry", () => {
    const box = trackBox("pred-9-0", [0, 10]);
    const resolved = resolveAiBoxAtFrame(box, 0);
    expect(resolved).not.toBeNull();
    expect({ x: resolved!.x, y: resolved!.y, w: resolved!.w, h: resolved!.h }).toEqual(bbox);
    expect(resolveAiBoxAtFrame(box, 20)).toBeNull();
  });

  it("resolves polygon/polyline tracks with current-frame points and outside gaps", () => {
    const polygon = pointsTrackBox("pred-polygon", "video_track_polygon");
    const polyline = pointsTrackBox("pred-polyline", "video_track_polyline");
    const resolvedPolygon = resolveAiBoxAtFrame(polygon, 5);
    const resolvedPolyline = resolveAiBoxAtFrame(polyline, 5);

    expect(resolvedPolygon?.polygon).toHaveLength(3);
    expect(resolvedPolygon?.w).toBeGreaterThan(0);
    expect(resolvedPolyline?.polyline).toHaveLength(2);
    expect(resolvedPolyline?.w).toBeGreaterThan(0);
    const outsidePolygon = {
      ...polygon,
      geometry: {
        ...polygon.geometry,
        outside: [{ from: 6, to: 7, source: "prediction" }],
      },
    } as AiBox;
    expect(resolveAiBoxAtFrame(outsidePolygon, 6)).toBeNull();
    expect(aiBoxOnFrame(outsidePolygon, 7)).toBe(false);
  });

  it("includes mask tracks in frame visibility and prediction density", () => {
    const mask = maskTrackBox("pred-mask");
    expect(aiBoxOnFrame(mask, 5)).toBe(true);
    expect(aiBoxOnFrame(mask, 6)).toBe(false);
    expect(resolveAiBoxAtFrame(mask, 5)).toBe(mask);
    expect(
      collectPredictedFrames([mask, pointsTrackBox("pred-polygon", "video_track_polygon")]),
    ).toEqual([0, 2, 10]);
  });
});
