/**
 * Pure transformation for tracker PVS seed collection: groups collected point
 * and box seeds into multi-frame prompt structures for the propagation/
 * correction payload.
 *
 * - Seeds are grouped by target (obj, 1-based) then by frame; each frame's
 *   prompt can carry points and a bbox simultaneously.
 * - Point polarity: 1 = positive, 0 = negative (Alt).
 * - Box seeds are normalized xyxy and converted to backend {x,y,w,h}.
 * - Both levels are sorted numerically so payloads are deterministic
 *   (idempotency keys must not depend on collection order).
 */

export type TrackerSeedPoint = {
  pt: [number, number];
  polarity: 1 | 0;
  obj: number;
  frame: number;
};
export type TrackerSeedBox = { bbox: [number, number, number, number]; obj: number; frame: number };

export interface TrackerSeedFramePrompt {
  frame_index: number;
  points?: [number, number, number][];
  bbox?: { x: number; w: number; y: number; h: number };
}

export interface TrackerSeedPromptGroup {
  obj_id: number;
  prompts: TrackerSeedFramePrompt[];
}

export function buildSeedPrompts(
  seeds: readonly TrackerSeedPoint[],
  boxes: readonly TrackerSeedBox[],
): TrackerSeedPromptGroup[] {
  type SeedEntry = {
    points: [number, number, number][];
    bbox?: { x: number; w: number; y: number; h: number };
  };
  const byObj = new Map<number, Map<number, SeedEntry>>();
  const ensureEntry = (obj: number, frame: number): SeedEntry => {
    const byFrame = byObj.get(obj) ?? new Map<number, SeedEntry>();
    const entry = byFrame.get(frame) ?? { points: [] };
    byFrame.set(frame, entry);
    byObj.set(obj, byFrame);
    return entry;
  };
  for (const { pt, polarity, obj, frame } of seeds) {
    ensureEntry(obj, frame).points.push([pt[0], pt[1], polarity]);
  }
  for (const { bbox, obj, frame } of boxes) {
    const [x1, y1, x2, y2] = bbox;
    ensureEntry(obj, frame).bbox = {
      x: Math.min(x1, x2),
      y: Math.min(y1, y2),
      w: Math.abs(x2 - x1),
      h: Math.abs(y2 - y1),
    };
  }
  return [...byObj.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([obj, byFrame]) => ({
      obj_id: obj,
      prompts: [...byFrame.entries()]
        .sort((a, b) => a[0] - b[0])
        .map(([frame, entry]) => ({
          frame_index: frame,
          ...(entry.points.length ? { points: entry.points } : {}),
          ...(entry.bbox ? { bbox: entry.bbox } : {}),
        })),
    }));
}

export function hasAnySeed(
  seeds: readonly TrackerSeedPoint[],
  boxes: readonly TrackerSeedBox[],
): boolean {
  return seeds.length > 0 || boxes.length > 0;
}
