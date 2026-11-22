import { describe, expect, it } from "vitest";
import { boxIsOnFrame, firstTrackFrame } from "./annotationFrameScope";
describe("boxIsOnFrame / firstTrackFrame · 视频几何", () => {
  const ann = (geometry: unknown) => ({ id: "a", geometry }) as never;

  const SINGLE = [
    { type: "video_bbox", frame_index: 7, x: 0, y: 0, w: 0.1, h: 0.1 },
    {
      type: "video_polygon",
      frame_index: 7,
      points: [
        [0, 0],
        [0.1, 0],
        [0.1, 0.1],
      ],
    },
    {
      type: "video_polyline",
      frame_index: 7,
      points: [
        [0, 0],
        [0.1, 0.1],
      ],
    },
    {
      type: "video_mask",
      frame_index: 7,
      mask: {
        encoding: "coco_rle_ref",
        size: [2, 3],
        object_key: "mask.json",
        sha256: "a".repeat(64),
        runs: 3,
        bytes: 64,
      },
    },
    {
      type: "video_rotated_bbox",
      frame_index: 7,
      cx: 0.5,
      cy: 0.5,
      w: 0.2,
      h: 0.1,
      angle: 30,
    },
    {
      type: "video_keypoint",
      frame_index: 7,
      points: [
        { x: 0.2, y: 0.3, v: 2 },
        { x: 0.4, y: 0.5, v: 1 },
      ],
    },
  ];

  it.each(SINGLE)("单帧几何 $type: 只在所属帧显示, 跳转到该帧", (geometry) => {
    expect(boxIsOnFrame(ann(geometry), 7)).toBe(true);
    expect(boxIsOnFrame(ann(geometry), 8)).toBe(false);
    expect(firstTrackFrame(ann(geometry))).toBe(7);
  });

  const TRACKS = [
    {
      type: "video_track_bbox",
      track_id: "t",
      keyframes: [
        { frame_index: 2, bbox: { x: 0, y: 0, w: 0.1, h: 0.1 }, source: "manual" },
        { frame_index: 6, bbox: { x: 0.2, y: 0.2, w: 0.1, h: 0.1 }, source: "manual" },
      ],
    },
    {
      type: "video_track_polygon",
      track_id: "t",
      keyframes: [
        {
          frame_index: 2,
          points: [
            [0, 0],
            [0.1, 0],
            [0.1, 0.1],
          ],
          source: "manual",
        },
        {
          frame_index: 6,
          points: [
            [0.2, 0.2],
            [0.3, 0.2],
            [0.3, 0.3],
          ],
          source: "manual",
        },
      ],
    },
    {
      type: "video_track_polyline",
      track_id: "t",
      keyframes: [
        {
          frame_index: 2,
          points: [
            [0, 0],
            [0.1, 0.1],
          ],
          source: "manual",
        },
        {
          frame_index: 6,
          points: [
            [0.2, 0.2],
            [0.3, 0.3],
          ],
          source: "manual",
        },
      ],
    },
  ];

  it.each(TRACKS)("轨迹几何 $type: 关键帧区间内(含插值帧)显示, 跳转到首个关键帧", (geometry) => {
    expect(boxIsOnFrame(ann(geometry), 2)).toBe(true);
    expect(boxIsOnFrame(ann(geometry), 4)).toBe(true);
    expect(boxIsOnFrame(ann(geometry), 9)).toBe(false);
    expect(firstTrackFrame(ann(geometry))).toBe(2);
  });

  it.each(TRACKS)("轨迹几何 $type: outside 区间内不显示", (geometry) => {
    const withOutside = { ...geometry, outside: [{ from: 4, to: 4 }] };
    expect(boxIsOnFrame(ann(withOutside), 4)).toBe(false);
    expect(boxIsOnFrame(ann(withOutside), 2)).toBe(true);
    expect(boxIsOnFrame(ann(withOutside), 6)).toBe(true);
  });

  it("Mask 轨迹按保持语义显示，outside 帧隐藏", () => {
    const geometry = {
      type: "video_track_mask",
      track_id: "mask-track",
      keyframes: [
        {
          frame_index: 2,
          mask: {
            encoding: "coco_rle_ref",
            size: [2, 3],
            object_key: "mask.json",
            sha256: "a".repeat(64),
            runs: 3,
            bytes: 64,
          },
          source: "manual",
        },
      ],
      outside: [{ from: 4, to: 4 }],
    };
    expect(boxIsOnFrame(ann(geometry), 2)).toBe(true);
    expect(boxIsOnFrame(ann(geometry), 9)).toBe(true);
    expect(boxIsOnFrame(ann(geometry), 4)).toBe(false);
    expect(firstTrackFrame(ann(geometry))).toBe(2);
  });

  it("非视频几何(图片框)不受帧筛选影响", () => {
    const rect = { type: "rectangle", x: 0, y: 0, width: 0.1, height: 0.1 };
    expect(boxIsOnFrame(ann(rect), 3)).toBe(true);
    expect(firstTrackFrame(ann(rect))).toBeNull();
  });
});
