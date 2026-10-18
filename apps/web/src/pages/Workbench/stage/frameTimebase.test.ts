import { describe, expect, it } from "vitest";
import {
  buildFrameTimebase,
  frameTimebaseDuration,
  frameToSeekTime,
  frameToTime,
  timeToFrame,
} from "./frameTimebase";
import type { TaskVideoFrameTimetableResponse, VideoMetadata } from "@/types";

const metadata: VideoMetadata = {
  duration_ms: 1250,
  fps: 10,
  frame_count: 10,
  width: 100,
  height: 50,
  codec: "h264",
  playback_path: null,
  playback_codec: null,
  playback_error: null,
  poster_frame_path: null,
  probe_error: null,
  poster_error: null,
  frame_timetable_frame_count: null,
  frame_timetable_error: null,
};

describe("frameTimebase", () => {
  it("maps frames by fps when no timetable is available", () => {
    const timebase = buildFrameTimebase(metadata);

    expect(frameToTime(3, timebase)).toBe(0.3);
    expect(frameTimebaseDuration(timebase)).toBe(1.25);
    expect(frameToSeekTime(3, timebase)).toBe(0.325);
    expect(timeToFrame(frameToSeekTime(3, timebase), timebase)).toBe(3);
    expect(timeToFrame(0.32, timebase)).toBe(3);
    expect(timeToFrame(99, timebase)).toBe(9);
  });

  it("falls back to frame count and fps when media duration is unavailable", () => {
    const timebase = buildFrameTimebase({ ...metadata, duration_ms: null });

    expect(frameTimebaseDuration(timebase)).toBe(1);
  });

  it("uses ffprobe pts values and nearest-frame lookup", () => {
    const timetable: TaskVideoFrameTimetableResponse = {
      task_id: "task-1",
      fps: 10,
      frame_count: 4,
      source: "ffprobe",
      frames: [
        { frame_index: 0, pts_ms: 0, is_keyframe: true, pict_type: "I", byte_offset: 1 },
        { frame_index: 1, pts_ms: 41, is_keyframe: false, pict_type: "P", byte_offset: 2 },
        { frame_index: 2, pts_ms: 83, is_keyframe: false, pict_type: "P", byte_offset: 3 },
        { frame_index: 3, pts_ms: 125, is_keyframe: false, pict_type: "P", byte_offset: 4 },
      ],
    };
    const timebase = buildFrameTimebase(metadata, timetable);

    expect(timebase.source).toBe("ffprobe");
    expect(frameToTime(2, timebase)).toBe(0.083);
    expect(frameToSeekTime(2, timebase)).toBe(0.0935);
    expect(timeToFrame(frameToSeekTime(2, timebase), timebase)).toBe(2);
    expect(timeToFrame(0.05, timebase)).toBe(1);
    expect(timeToFrame(0.12, timebase)).toBe(3);
  });

  it("interpolates sparse VFR anchors into a searchable frame timebase", () => {
    const timetable: TaskVideoFrameTimetableResponse = {
      task_id: "task-1",
      fps: 10,
      frame_count: 10,
      source: "ffprobe",
      frames: [
        { frame_index: 0, pts_ms: 0, is_keyframe: true, pict_type: "I", byte_offset: 1 },
        { frame_index: 3, pts_ms: 120, is_keyframe: false, pict_type: "P", byte_offset: 2 },
        { frame_index: 9, pts_ms: 960, is_keyframe: false, pict_type: "P", byte_offset: 3 },
      ],
    };
    const timebase = buildFrameTimebase(metadata, timetable);

    expect(timebase.ptsMs).toEqual([0, 40, 80, 120, 260, 400, 540, 680, 820, 960]);
    expect(timebase.ptsMs?.every(Number.isFinite)).toBe(true);
    expect(frameToTime(4, timebase)).toBe(0.26);
    expect(timeToFrame(0.15, timebase)).toBe(3);
    expect(timeToFrame(frameToSeekTime(4, timebase), timebase)).toBe(4);
  });
});
