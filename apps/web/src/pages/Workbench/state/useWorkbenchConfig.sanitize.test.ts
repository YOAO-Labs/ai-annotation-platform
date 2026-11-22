import { describe, it, expect } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import { sanitizeForPersist } from "./useWorkbenchConfig";

describe("sanitizeForPersist", () => {
  it("excludes workspace from every legacy writer without deleting its in-memory copy", () => {
    const wb = {
      ...DEFAULT_WORKBENCH_PREFERENCES,
      layout: {
        ...DEFAULT_WORKBENCH_PREFERENCES.layout,
        workspace: { engine: "dockview@8" as const, contexts: {} },
      },
    };
    expect(sanitizeForPersist(wb).layout).not.toHaveProperty("workspace");
    expect(wb.layout.workspace).toEqual({ engine: "dockview@8", contexts: {} });
  });

  it("把浮窗坐标小数取整为整数", () => {
    const wb = {
      ...DEFAULT_WORKBENCH_PREFERENCES,
      layout: {
        ...DEFAULT_WORKBENCH_PREFERENCES.layout,
        floatingSelection: { collapsed: false, x: 300.4, y: 200.6, w: 100.2, h: 200.8 },
        triViewFloat: { collapsed: false, x: 12.3, y: 8.7, w: null, h: null },
      },
    };
    const out = sanitizeForPersist(wb);
    expect(out.layout.floatingSelection).toEqual({
      collapsed: false,
      x: 300,
      y: 201,
      w: 100,
      h: 201,
    });
    expect(out.layout).not.toHaveProperty("triViewFloat");
  });

  it("整数坐标幂等；返回新对象不就地改原值", () => {
    const fs = { collapsed: false, x: 10, y: 20, w: 100, h: 200 };
    const wb = {
      ...DEFAULT_WORKBENCH_PREFERENCES,
      layout: { ...DEFAULT_WORKBENCH_PREFERENCES.layout, floatingSelection: fs },
    };
    const out = sanitizeForPersist(wb);
    expect(out.layout.floatingSelection).toEqual(fs);
    expect(out).not.toBe(wb);
    expect(out.layout).not.toBe(wb.layout);
    expect(out.layout.floatingSelection).not.toBe(fs);
  });
});
