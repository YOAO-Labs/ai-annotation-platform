import { describe, it, expect } from "vitest";
import { resolveCrossFrameNavigation, resolveCrossFrameTarget } from "./crossFrameTarget";
import type { NeighborsResponse } from "@/types";

function nb(over: Partial<NeighborsResponse>): NeighborsResponse {
  return {
    scene_id: "s",
    scene_name: "sc",
    frame_index: 2,
    scene_total_frames: 5,
    prev: [{ task_id: "t1", frame_index: 1 }],
    next: [{ task_id: "t3", frame_index: 3 }],
    ...over,
  };
}

describe("resolveCrossFrameTarget", () => {
  it("null / scene_id=null → no-scene", () => {
    expect(resolveCrossFrameTarget(null, "next").kind).toBe("no-scene");
    expect(
      resolveCrossFrameTarget(nb({ scene_id: null, scene_name: null, frame_index: null }), "next")
        .kind,
    ).toBe("no-scene");
  });

  it("next 正常解析到 next[0]", () => {
    const r = resolveCrossFrameTarget(nb({}), "next");
    expect(r).toEqual({ kind: "ok", taskId: "t3", frameIndex: 3 });
  });

  it("prev 正常解析到 prev[0]", () => {
    const r = resolveCrossFrameTarget(nb({}), "prev");
    expect(r).toEqual({ kind: "ok", taskId: "t1", frameIndex: 1 });
  });

  it("末帧 next 为空 → boundary next", () => {
    const r = resolveCrossFrameTarget(nb({ next: [] }), "next");
    expect(r).toEqual({ kind: "boundary", direction: "next" });
  });

  it("首帧 prev 为空 → boundary prev", () => {
    const r = resolveCrossFrameTarget(nb({ prev: [] }), "prev");
    expect(r).toEqual({ kind: "boundary", direction: "prev" });
  });

  it("目标 task 已加载 → loaded 导航", () => {
    expect(resolveCrossFrameNavigation(["t1", "t2"], "t2")).toEqual({
      kind: "loaded",
      taskId: "t2",
    });
  });

  it("目标 task 未加载 → direct taskId 导航", () => {
    expect(resolveCrossFrameNavigation(["t1", "t2"], "t3")).toEqual({
      kind: "direct",
      taskId: "t3",
    });
  });
});
