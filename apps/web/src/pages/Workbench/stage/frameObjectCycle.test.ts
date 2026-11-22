import { describe, expect, it } from "vitest";
import {
  buildFrameCategories,
  collectFrameCategories,
  nextInCategory,
  nextCategory,
} from "./frameObjectCycle";

describe("buildFrameCategories", () => {
  it("sorts each category by y↑ then x↑ then id", () => {
    const cats = buildFrameCategories(
      [
        { id: "a2", x: 0.5, y: 0.5 },
        { id: "a1", x: 0.1, y: 0.1 },
      ],
      [
        { id: "u2", x: 0.9, y: 0.2 },
        { id: "u1", x: 0.1, y: 0.2 },
      ],
      [],
    );
    expect(cats.ai).toEqual(["a1", "a2"]);
    expect(cats.user).toEqual(["u1", "u2"]);
    expect(cats.track).toEqual([]);
  });

  it("tie-breaks equal x/y deterministically by id", () => {
    const cats = buildFrameCategories(
      [
        { id: "b", x: 0.3, y: 0.3 },
        { id: "a", x: 0.3, y: 0.3 },
      ],
      [],
      [],
    );
    expect(cats.ai).toEqual(["a", "b"]);
  });
});

describe("collectFrameCategories (来源归并)", () => {
  it("entries 按 isTrack 分流到 track / user, carryOverGhosts 并入 track", () => {
    const cats = collectFrameCategories({
      ai: [{ id: "a1", x: 0.2, y: 0.1 }],
      entries: [
        { id: "u1", x: 0.1, y: 0.5, isTrack: false },
        { id: "t1", x: 0.1, y: 0.1, isTrack: true },
      ],
      carryOverGhosts: [{ id: "g1", x: 0.3, y: 0.3 }],
      selectedTrackGhost: null,
    });
    expect(cats.ai).toEqual(["a1"]);
    expect(cats.user).toEqual(["u1"]);
    expect(cats.track).toEqual(["t1", "g1"]);
  });

  it("回归: 全部轨迹以 ghost 显示时, 选中 ghost 并入 track, Tab 遍历全部四条而非只在两条间弹", () => {
    const sources = (selectedId: string) => {
      const all = [
        { id: "t1", x: 0.1, y: 0.1 },
        { id: "t2", x: 0.2, y: 0.2 },
        { id: "t3", x: 0.3, y: 0.3 },
        { id: "t4", x: 0.4, y: 0.4 },
      ];
      const sel = all.find((t) => t.id === selectedId)!;
      return collectFrameCategories({
        ai: [],
        entries: [],
        carryOverGhosts: all.filter((t) => t.id !== selectedId),
        selectedTrackGhost: sel,
      });
    };
    let selected = "t1";
    const visited: string[] = [selected];
    for (let i = 0; i < 4; i++) {
      selected = nextInCategory(sources(selected), selected, 1)!;
      visited.push(selected);
    }
    expect(visited).toEqual(["t1", "t2", "t3", "t4", "t1"]);
    expect(new Set(visited).size).toBe(4);
  });
});

const CATS = {
  ai: ["a1", "a2", "a3"],
  user: ["u1", "u2"],
  track: ["t1"],
};

describe("nextInCategory (同类流转)", () => {
  it("cycles within the selected object's category (ring)", () => {
    expect(nextInCategory(CATS, "a1", 1)).toBe("a2");
    expect(nextInCategory(CATS, "a3", 1)).toBe("a1");
    expect(nextInCategory(CATS, "a1", -1)).toBe("a3");
    expect(nextInCategory(CATS, "u1", 1)).toBe("u2");
    expect(nextInCategory(CATS, "u2", 1)).toBe("u1");
  });

  it("stays put for a single-item category", () => {
    expect(nextInCategory(CATS, "t1", 1)).toBe("t1");
  });

  it("falls to first non-empty category's head when nothing selected", () => {
    expect(nextInCategory(CATS, null, 1)).toBe("a1");
    expect(nextInCategory({ ai: [], user: ["u1"], track: [] }, null, 1)).toBe("u1");
  });

  it("returns null when all categories empty", () => {
    expect(nextInCategory({ ai: [], user: [], track: [] }, "x", 1)).toBeNull();
  });
});

describe("nextCategory (跨类跳转)", () => {
  it("jumps to next non-empty category's head (AI→user→track→wrap)", () => {
    expect(nextCategory(CATS, "a2", 1)).toBe("u1");
    expect(nextCategory(CATS, "u1", 1)).toBe("t1");
    expect(nextCategory(CATS, "t1", 1)).toBe("a1");
    expect(nextCategory(CATS, "u1", -1)).toBe("a1");
  });

  it("skips empty categories", () => {
    const cats = { ai: ["a1"], user: [], track: ["t1"] };
    expect(nextCategory(cats, "a1", 1)).toBe("t1");
    expect(nextCategory(cats, "t1", 1)).toBe("a1");
  });

  it("lands on first / last non-empty category when nothing selected", () => {
    expect(nextCategory(CATS, null, 1)).toBe("a1");
    expect(nextCategory(CATS, null, -1)).toBe("t1");
  });

  it("returns null when all categories empty", () => {
    expect(nextCategory({ ai: [], user: [], track: [] }, null, 1)).toBeNull();
  });
});
