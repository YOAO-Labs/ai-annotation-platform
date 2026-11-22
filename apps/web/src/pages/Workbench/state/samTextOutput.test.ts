import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  SAM_OUTPUT_STORAGE_PREFIX,
  defaultOutputMode,
  readStoredOutputMode,
  resolveInitialOutputMode,
  samOutputStorageKey,
  samOutputUserStorageKey,
  writeStoredOutputMode,
} from "./samTextOutput";

describe("samTextOutput", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });
  afterEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it("samOutputStorageKey 按 projectId 分桶", () => {
    expect(samOutputStorageKey("p1")).toBe(`${SAM_OUTPUT_STORAGE_PREFIX}p1`);
    expect(samOutputUserStorageKey("u1", "p1")).toBe("workbench.u1.sam.outputMode:p1");
  });

  it("defaultOutputMode: image-det → box，其它 → mask", () => {
    expect(defaultOutputMode("image-det")).toBe("box");
    expect(defaultOutputMode("image-seg")).toBe("mask");
    expect(defaultOutputMode(undefined)).toBe("mask");
    expect(defaultOutputMode(null)).toBe("mask");
  });

  it("read/writeStoredOutputMode 往返，非法值返回 null", () => {
    expect(readStoredOutputMode("p1")).toBeNull();
    writeStoredOutputMode("p1", "both", "u1");
    expect(readStoredOutputMode("p1")).toBe("both");
    expect(window.localStorage.getItem(samOutputUserStorageKey("u1", "p1"))).toBe("both");
    window.sessionStorage.setItem(samOutputStorageKey("p1"), "garbage");
    window.localStorage.setItem(samOutputUserStorageKey("u1", "p1"), "garbage");
    expect(readStoredOutputMode("p1", "u1")).toBeNull();
  });

  it("resolveInitialOutputMode 优先级: sessionStorage > localStorage > typeKey 默认", () => {
    window.sessionStorage.setItem(samOutputStorageKey("p1"), "both");
    window.localStorage.setItem(samOutputUserStorageKey("u1", "p1"), "box");
    expect(resolveInitialOutputMode("p1", "image-det", "u1")).toBe("both");
    window.sessionStorage.clear();
    expect(resolveInitialOutputMode("p1", "image-seg", "u1")).toBe("box");
    window.sessionStorage.clear();
    window.localStorage.clear();
    expect(resolveInitialOutputMode("p2", "image-det")).toBe("box");
    expect(resolveInitialOutputMode(undefined, "image-seg")).toBe("mask");
  });
});
