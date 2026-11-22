import { describe, expect, it } from "vitest";
import { createSamCache, makeSamCacheKey } from "./useSamCache";
import type { PendingCandidate } from "./useInteractiveAI";

const sampleCandidate = (label: string): PendingCandidate => ({
  id: `c-${label}`,
  type: "polygonlabels",
  points: [
    [0.1, 0.1],
    [0.2, 0.1],
    [0.15, 0.2],
  ],
  label,
  score: 0.9,
  source: "point",
});

describe("makeSamCacheKey", () => {
  it("不同坐标顺位下 key 不同", () => {
    const a = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "point",
      ctx: { type: "point", points: [[0.5, 0.5]], labels: [1] },
    });
    const b = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "point",
      ctx: { type: "point", points: [[0.5, 0.6]], labels: [1] },
    });
    expect(a).not.toBe(b);
  });

  it("浮点抖动 (5 位小数) 不影响 key", () => {
    const a = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "point",
      ctx: { type: "point", points: [[0.50001, 0.50002]], labels: [1] },
    });
    const b = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "point",
      ctx: { type: "point", points: [[0.5, 0.5]], labels: [1] },
    });
    expect(a).toBe(b);
  });

  it("不同 ctx key 顺序 → key 一致", () => {
    const a = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "bbox",
      ctx: { type: "bbox", bbox: [0.1, 0.1, 0.5, 0.5] },
    });
    const b = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "b",
      ctxKind: "bbox",
      ctx: { bbox: [0.1, 0.1, 0.5, 0.5], type: "bbox" },
    });
    expect(a).toBe(b);
  });

  it("不同 mlBackend 不共用 key", () => {
    const a = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "sam3",
      ctxKind: "exemplar",
      ctx: { type: "exemplar", bbox: [0, 0, 1, 1] },
    });
    const b = makeSamCacheKey({
      taskId: "t",
      mlBackendId: "grounded-sam2",
      ctxKind: "exemplar",
      ctx: { type: "exemplar", bbox: [0, 0, 1, 1] },
    });
    expect(a).not.toBe(b);
  });
});

describe("createSamCache", () => {
  it("get on miss 返回 undefined 且 misses++", () => {
    const c = createSamCache(8);
    expect(c.get("k")).toBeUndefined();
    expect(c.stats).toEqual({ hits: 0, misses: 1 });
  });

  it("set 后 get 返回原数组", () => {
    const c = createSamCache(8);
    const v = [sampleCandidate("dog")];
    c.set("k", v);
    expect(c.get("k")?.candidates).toBe(v);
    expect(c.stats.hits).toBe(1);
  });

  it("cap 满 → 淘汰最早项", () => {
    const c = createSamCache(2);
    c.set("a", [sampleCandidate("a")]);
    c.set("b", [sampleCandidate("b")]);
    c.set("c", [sampleCandidate("c")]);
    expect(c.size).toBe(2);
    expect(c.get("a")).toBeUndefined();
    expect(c.get("b")).toBeDefined();
    expect(c.get("c")).toBeDefined();
  });

  it("get 触发 LRU 触摸 → 该项不再是最早", () => {
    const c = createSamCache(2);
    c.set("a", [sampleCandidate("a")]);
    c.set("b", [sampleCandidate("b")]);
    c.get("a");
    c.set("c", [sampleCandidate("c")]);
    expect(c.get("a")).toBeDefined();
    expect(c.get("b")).toBeUndefined();
    expect(c.get("c")).toBeDefined();
  });

  it("clearAll 清空", () => {
    const c = createSamCache();
    c.set("a", [sampleCandidate("a")]);
    expect(c.size).toBe(1);
    c.clearAll();
    expect(c.size).toBe(0);
    expect(c.get("a")).toBeUndefined();
  });

  it("set 重复 key → 覆盖且不增 size", () => {
    const c = createSamCache();
    c.set("a", [sampleCandidate("v1")]);
    c.set("a", [sampleCandidate("v2")]);
    expect(c.size).toBe(1);
    expect(c.get("a")?.candidates[0].label).toBe("v2");
  });

  it("到达固定 TTL 后 miss 并释放字节", () => {
    let now = 1_000;
    const c = createSamCache({ maxEntries: 8, ttlMs: 100, now: () => now });
    c.set("a", [sampleCandidate("a")]);
    const retained = c.byteSize;
    expect(retained).toBeGreaterThan(0);
    now = 1_099;
    expect(c.get("a")?.candidates).toHaveLength(1);
    now = 1_100;
    expect(c.get("a")).toBeUndefined();
    expect(c.byteSize).toBe(0);
  });

  it("字节预算先于条目上限驱逐 LRU", () => {
    const one = [sampleCandidate("a")];
    const probe = createSamCache({ maxEntries: 8, maxBytes: 10_000 });
    probe.set("a", one);
    const entryBytes = probe.byteSize;
    const c = createSamCache({ maxEntries: 8, maxBytes: entryBytes + 1 });
    c.set("a", one);
    c.set("b", [sampleCandidate("b")]);
    expect(c.size).toBe(1);
    expect(c.get("a")).toBeUndefined();
    expect(c.get("b")?.candidates).toHaveLength(1);
  });

  it("单项超过字节预算时不缓存", () => {
    const c = createSamCache({ maxBytes: 1 });
    expect(c.set("a", [sampleCandidate("a")])).toBeUndefined();
    expect(c.size).toBe(0);
    expect(c.byteSize).toBe(0);
  });
});
