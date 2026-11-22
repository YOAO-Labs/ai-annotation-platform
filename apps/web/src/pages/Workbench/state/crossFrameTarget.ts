import type { NeighborsResponse } from "@/types";

export type CrossFrameResolution =
  | { kind: "ok"; taskId: string; frameIndex: number }
  | { kind: "no-scene" }
  | { kind: "boundary"; direction: "next" | "prev" };

export type CrossFrameNavigation =
  | { kind: "loaded"; taskId: string }
  | { kind: "direct"; taskId: string };

export function resolveCrossFrameTarget(
  neighbors: NeighborsResponse | null,
  direction: "next" | "prev",
): CrossFrameResolution {
  if (!neighbors || neighbors.scene_id == null) {
    return { kind: "no-scene" };
  }
  const target = direction === "next" ? neighbors.next?.[0] : neighbors.prev?.[0];
  if (!target) {
    return { kind: "boundary", direction };
  }
  return { kind: "ok", taskId: target.task_id, frameIndex: target.frame_index };
}

export function resolveCrossFrameNavigation(
  loadedTaskIds: readonly string[],
  targetTaskId: string,
): CrossFrameNavigation {
  return loadedTaskIds.includes(targetTaskId)
    ? { kind: "loaded", taskId: targetTaskId }
    : { kind: "direct", taskId: targetTaskId };
}
