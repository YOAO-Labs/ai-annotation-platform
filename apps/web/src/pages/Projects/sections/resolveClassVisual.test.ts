/**
 * resolveClassVisual 单测 — 前端 alias_to 解析 (须与后端
 * services/project.resolve_class_visual 行为一致)。
 */
import { describe, expect, it } from "vitest";
import { resolveClassVisual } from "./resolveClassVisual";

const ref = (u: string, c: string) => ({ tool_unit_id: u, class_name: c });

describe("resolveClassVisual", () => {
  const inheritBindings = {
    bbox: { classRows: [{ name: "person", color: "#ff0000", alias: "person" }] },
    region: { classRows: [{ name: "pedestrian", aliasTo: ref("bbox", "person") }] },
  };

  it("继承目标的 color 和 alias", () => {
    const out = resolveClassVisual(inheritBindings, {
      aliasTo: ref("bbox", "person"),
    });
    expect(out).toEqual({ color: "#ff0000", alias: "person" });
  });

  it("自身显式值覆盖继承, 缺失项仍继承", () => {
    const out = resolveClassVisual(inheritBindings, {
      color: "#0000ff",
      aliasTo: ref("bbox", "person"),
    });
    expect(out.color).toBe("#0000ff");
    expect(out.alias).toBe("person");
  });

  it("悬空引用降级到自身值", () => {
    const out = resolveClassVisual(inheritBindings, {
      color: "#123456",
      aliasTo: ref("bbox", "ghost"),
    });
    expect(out).toEqual({ color: "#123456", alias: undefined });
  });

  it("环引用不死循环, 降级到已知值", () => {
    const cyclic = {
      bbox: { classRows: [{ name: "a", color: "#aaaaaa", aliasTo: ref("region", "b") }] },
      region: { classRows: [{ name: "b", aliasTo: ref("bbox", "a") }] },
    };
    const out = resolveClassVisual(cyclic, cyclic.region.classRows[0]);
    expect(out.color).toBe("#aaaaaa");
    expect(out.alias).toBeUndefined();
  });

  it("多跳链继承到链尾的显式色", () => {
    const chain = {
      bbox: { classRows: [{ name: "a", color: "#0a0a0a" }] },
      region: { classRows: [{ name: "b", aliasTo: ref("bbox", "a") }] },
      polyline: { classRows: [{ name: "c", aliasTo: ref("region", "b") }] },
    };
    const out = resolveClassVisual(chain, chain.polyline.classRows[0]);
    expect(out.color).toBe("#0a0a0a");
  });

  it("无 aliasTo 时返回自身值", () => {
    expect(resolveClassVisual({}, { color: "#fff", alias: "x" })).toEqual({
      color: "#fff",
      alias: "x",
    });
  });
});
