import { describe, expect, it } from "vitest";
import type { AnnotationResponse, VideoTrackGeometry } from "@/types";
import {
  buildVideoCompositionCommands,
  buildVideoCreatePayload,
  buildVideoPointsCreatePayload,
  buildVideoPointsTrackCreatePayload,
  buildVideoMaskCreatePayload,
  buildVideoMaskTrackCreatePayload,
  buildVideoKeypointCreatePayload,
  upsertVideoMaskKeyframe,
  buildVideoUpdateCommand,
} from "./useVideoAnnotationActions";

const box = { x: 0.1, y: 0.2, w: 0.3, h: 0.4 };

function annotation(geometry: AnnotationResponse["geometry"]): AnnotationResponse {
  return {
    id: "ann-1",
    task_id: "task-1",
    project_id: "project-1",
    user_id: "user-1",
    source: "manual",
    annotation_type: geometry.type,
    class_name: "Car",
    geometry,
    confidence: 1,
    parent_prediction_id: null,
    parent_annotation_id: null,
    lead_time: null,
    is_active: true,
    ground_truth: false,
    attributes: {},
    created_at: "2026-05-11T00:00:00Z",
    updated_at: null,
  };
}

describe("video annotation actions helpers", () => {
  it("builds video_bbox create payload", () => {
    const payload = buildVideoCreatePayload("video_bbox", 7, box, "Car");

    expect(payload).toEqual({
      annotation_type: "video_bbox",
      class_name: "Car",
      geometry: { type: "video_bbox", frame_index: 7, ...box },
    });
  });

  it("builds lossless video OBB and keypoint payloads", () => {
    expect(buildVideoCreatePayload("video_rotated_bbox", 7, box, "Car")).toEqual({
      annotation_type: "video_rotated_bbox",
      class_name: "Car",
      tool_unit_id: "rotated_bbox",
      geometry: {
        type: "video_rotated_bbox",
        frame_index: 7,
        cx: 0.25,
        cy: 0.4,
        w: 0.3,
        h: 0.4,
        angle: 0,
      },
    });
    const points = [
      { x: 0.1, y: 0.2, v: 2 as const },
      { x: 0.3, y: 0.4, v: 1 as const },
      { x: 0.5, y: 0.6, v: 0 as const },
    ];
    expect(buildVideoKeypointCreatePayload(8, points, "Person")).toEqual({
      annotation_type: "video_keypoint",
      class_name: "Person",
      tool_unit_id: "keypoint",
      geometry: { type: "video_keypoint", frame_index: 8, points },
    });
  });

  it("builds video_track_polygon/polyline create payload from drawn points", () => {
    const pts: [number, number][] = [
      [0.1, 0.1],
      [0.3, 0.1],
      [0.3, 0.3],
    ];
    const poly = buildVideoPointsTrackCreatePayload("video_track_polygon", 4, pts, "Car");
    expect(poly.annotation_type).toBe("video_track_polygon");
    expect(poly.geometry.type).toBe("video_track_polygon");
    expect(poly.tool_unit_id).toBe("region");
    const g = poly.geometry as {
      track_id: string;
      keyframes: { frame_index: number; points: number[][]; source: string }[];
    };
    expect(g.track_id).toMatch(/^trk_/);
    expect(g.keyframes).toEqual([
      { frame_index: 4, points: pts, source: "manual", occluded: false },
    ]);

    const line = buildVideoPointsTrackCreatePayload(
      "video_track_polyline",
      2,
      [
        [0, 0],
        [0.5, 0.5],
      ],
      "",
    );
    expect(line.annotation_type).toBe("video_track_polyline");
    expect(line.tool_unit_id).toBe("polyline");
    expect(line.class_name).toBe("__unknown");
  });

  it("builds single-frame video_polygon/polyline create payload from drawn points", () => {
    const pts: [number, number][] = [
      [0.1, 0.1],
      [0.5, 0.1],
      [0.3, 0.6],
    ];
    const poly = buildVideoPointsCreatePayload("video_polygon", 3, pts, "Car");
    expect(poly).toEqual({
      annotation_type: "video_polygon",
      class_name: "Car",
      tool_unit_id: "region",
      geometry: { type: "video_polygon", frame_index: 3, points: pts },
    });

    const line = buildVideoPointsCreatePayload(
      "video_polyline",
      8,
      [
        [0.1, 0.1],
        [0.9, 0.9],
      ],
      "",
    );
    expect(line.annotation_type).toBe("video_polyline");
    expect(line.tool_unit_id).toBe("polyline");
    expect(line.class_name).toBe("__unknown");
    expect((line.geometry as { frame_index: number }).frame_index).toBe(8);
  });

  it("builds video_track create payload with one manual keyframe", () => {
    const payload = buildVideoCreatePayload("video_track_bbox", 9, box, "");

    expect(payload.annotation_type).toBe("video_track_bbox");
    expect(payload.class_name).toBe("__unknown");
    expect(payload.geometry.type).toBe("video_track_bbox");
    const geometry = payload.geometry as VideoTrackGeometry;
    expect(geometry.track_id).toMatch(/^trk_/);
    expect(geometry.keyframes).toEqual([
      {
        frame_index: 9,
        bbox: box,
        source: "manual",
        occluded: false,
      },
    ]);
  });

  it("builds separate single-frame Mask and Mask track payloads", () => {
    const sha = "a".repeat(64);
    const mask = {
      encoding: "coco_rle_ref" as const,
      size: [8, 12] as [number, number],
      object_key: `raster-masks/sha256/aa/aa/${sha}.json`,
      sha256: sha,
      runs: 3,
      bytes: 42,
    };
    expect(buildVideoMaskCreatePayload(4, mask, "Car")).toEqual({
      annotation_type: "video_mask",
      tool_unit_id: "region",
      class_name: "Car",
      geometry: { type: "video_mask", frame_index: 4, mask },
    });
    const payload = buildVideoMaskTrackCreatePayload(4, mask, "Car");
    expect(payload).toMatchObject({
      annotation_type: "video_track_mask",
      tool_unit_id: "region",
      geometry: {
        type: "video_track_mask",
        keyframes: [{ frame_index: 4, mask, source: "manual", occluded: false }],
      },
    });
    const geometry = {
      ...(payload.geometry as import("@/types").VideoTrackMaskGeometry),
      outside: [{ from: 5, to: 9, source: "manual" as const }],
    };
    const updated = upsertVideoMaskKeyframe(geometry, 7, { ...mask, sha256: "b".repeat(64) });
    expect(updated.keyframes.map((keyframe) => keyframe.frame_index)).toEqual([4, 7]);
    expect(updated.keyframes[1].source).toBe("manual");
    expect(updated.outside).toEqual([
      { from: 5, to: 6, source: "manual" },
      { from: 8, to: 9, source: "manual" },
    ]);
  });

  it("uses videoKeyframe history command for single-keyframe track edits", () => {
    const before: VideoTrackGeometry = {
      type: "video_track_bbox",
      track_id: "trk_1",
      keyframes: [{ frame_index: 0, bbox: box, source: "manual" }],
    };
    const after: VideoTrackGeometry = {
      ...before,
      keyframes: [
        ...before.keyframes,
        { frame_index: 5, bbox: { x: 0.2, y: 0.2, w: 0.3, h: 0.4 }, source: "manual" },
      ],
    };

    expect(buildVideoUpdateCommand(annotation(before), after)).toMatchObject({
      kind: "videoKeyframe",
      annotationId: "ann-1",
      frameIndex: 5,
    });
  });

  it("falls back to full geometry update for video_bbox edits", () => {
    const ann = annotation({ type: "video_bbox", frame_index: 1, ...box });
    const after = { type: "video_bbox" as const, frame_index: 1, x: 0.2, y: 0.2, w: 0.3, h: 0.4 };

    expect(buildVideoUpdateCommand(ann, after)).toEqual({
      kind: "update",
      annotationId: "ann-1",
      before: { geometry: ann.geometry },
      after: { geometry: after },
    });
  });

  it("builds batchable composition history commands", () => {
    const bbox = annotation({ type: "video_bbox", frame_index: 1, ...box });
    const track = annotation({
      type: "video_track_bbox",
      track_id: "trk_1",
      keyframes: [{ frame_index: 1, bbox: box, source: "manual" }],
    });
    const updated = {
      ...track,
      geometry: { ...track.geometry, keyframes: [] } as VideoTrackGeometry,
    };

    const commands = buildVideoCompositionCommands([bbox, track], {
      updated_annotations: [updated],
      created_annotations: [track],
      deleted_annotation_ids: [bbox.id],
    });

    expect(commands.map((cmd) => cmd.kind)).toEqual(["update", "delete", "create"]);
    expect(commands[0]).toMatchObject({ kind: "update", annotationId: track.id });
    expect(commands[1]).toMatchObject({ kind: "delete", annotation: { id: bbox.id } });
    expect(commands[2]).toMatchObject({ kind: "create", annotationId: track.id });
  });
});
