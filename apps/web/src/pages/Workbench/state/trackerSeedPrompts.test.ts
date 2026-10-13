import { describe, it, expect } from "vitest";
import { buildSeedPrompts, hasAnySeed } from "./trackerSeedPrompts";

describe("buildSeedPrompts", () => {
  it("groups seeds by obj then frame, deterministically sorted", () => {
    const seeds = [
      { pt: [0.2, 0.3] as [number, number], polarity: 1 as const, obj: 2, frame: 4 },
      { pt: [0.1, 0.1] as [number, number], polarity: 1 as const, obj: 1, frame: 7 },
      { pt: [0.5, 0.5] as [number, number], polarity: 0 as const, obj: 1, frame: 3 },
    ];
    const boxes = [
      { bbox: [0.9, 0.9, 0.2, 0.2] as [number, number, number, number], obj: 2, frame: 3 },
    ];

    expect(buildSeedPrompts(seeds, boxes)).toEqual([
      {
        obj_id: 1,
        prompts: [
          { frame_index: 3, points: [[0.5, 0.5, 0]] },
          { frame_index: 7, points: [[0.1, 0.1, 1]] },
        ],
      },
      {
        obj_id: 2,
        prompts: [
          { frame_index: 3, bbox: { x: 0.2, y: 0.2, w: 0.7, h: 0.7 } },
          { frame_index: 4, points: [[0.2, 0.3, 1]] },
        ],
      },
    ]);
  });

  it("carries points and bbox on the same frame prompt", () => {
    const prompts = buildSeedPrompts(
      [{ pt: [0.25, 0.75], polarity: 1, obj: 1, frame: 2 }],
      [{ bbox: [0, 1, 0.5, 0.5], obj: 1, frame: 2 }],
    );
    expect(prompts).toEqual([
      {
        obj_id: 1,
        prompts: [
          {
            frame_index: 2,
            points: [[0.25, 0.75, 1]],
            bbox: { x: 0, y: 0.5, w: 0.5, h: 0.5 },
          },
        ],
      },
    ]);
  });

  it("omits empty point lists (bbox-only corrections)", () => {
    const prompts = buildSeedPrompts([], [{ bbox: [0.4, 0.4, 0.1, 0.1], obj: 3, frame: 1 }]);
    expect(prompts).toEqual([
      {
        obj_id: 3,
        prompts: [{ frame_index: 1, bbox: { x: 0.1, y: 0.1, w: 0.4 - 0.1, h: 0.4 - 0.1 } }],
      },
    ]);
  });

  it("returns an empty payload when nothing was collected", () => {
    expect(buildSeedPrompts([], [])).toEqual([]);
    expect(hasAnySeed([], [])).toBe(false);
    expect(hasAnySeed([{ pt: [0, 0], polarity: 1, obj: 1, frame: 0 }], [])).toBe(true);
  });
});
