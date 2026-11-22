import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useDirtyTracker } from "./useDirtyTracker";

describe("useDirtyTracker", () => {
  it("markDirty + getDirtyFields 累积且去重", () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
      result.current.markDirty("a", "class_name");
      result.current.markDirty("a", "attributes");
    });
    expect(result.current.getDirtyFields("a").sort()).toEqual(["attributes", "class_name"]);
    expect(result.current.getDirtyFields("b")).toEqual([]);
  });

  it("clear / clearAll", () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
      result.current.markDirty("b", "geometry");
    });
    act(() => {
      result.current.clear("a");
    });
    expect(result.current.getDirtyFields("a")).toEqual([]);
    expect(result.current.getDirtyFields("b")).toEqual(["geometry"]);
    act(() => {
      result.current.clearAll();
    });
    expect(result.current.getDirtyFields("b")).toEqual([]);
  });

  it("flush 同步 commit：清空 dirty 并把字段交给 commit", () => {
    const { result } = renderHook(() => useDirtyTracker());
    const commit = vi.fn();
    act(() => {
      result.current.markDirty("a", "attributes");
      result.current.markDirty("a", "class_name");
    });
    let returned: string[] = [];
    act(() => {
      returned = result.current.flush("a", commit) as string[];
    });
    expect(returned.sort()).toEqual(["attributes", "class_name"]);
    expect(commit).toHaveBeenCalledTimes(1);
    expect((commit.mock.calls[0][0] as string[]).sort()).toEqual(["attributes", "class_name"]);
    expect(result.current.getDirtyFields("a")).toEqual([]);
  });

  it("flush 无 commit：仅清空", () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
    });
    act(() => {
      result.current.flush("a");
    });
    expect(result.current.getDirtyFields("a")).toEqual([]);
  });

  it("flush 空 dirty：返回空数组、不调 commit", () => {
    const { result } = renderHook(() => useDirtyTracker());
    const commit = vi.fn();
    let returned: string[] = [];
    act(() => {
      returned = result.current.flush("nope", commit) as string[];
    });
    expect(returned).toEqual([]);
    expect(commit).not.toHaveBeenCalled();
  });

  it("flush commit 同步抛出 → dirty 回滚", () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
      result.current.markDirty("a", "geometry");
    });
    act(() => {
      result.current.flush("a", () => {
        throw new Error("boom");
      });
    });
    expect(result.current.getDirtyFields("a").sort()).toEqual(["attributes", "geometry"]);
  });

  it("flush commit Promise reject → dirty 回滚", async () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
    });
    await act(async () => {
      result.current.flush("a", () => Promise.reject(new Error("net")));
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.getDirtyFields("a")).toEqual(["attributes"]);
  });

  it("subscribe 在 markDirty / clear / flush 时触发", () => {
    const { result } = renderHook(() => useDirtyTracker());
    const listener = vi.fn();
    act(() => {
      result.current.subscribe(listener);
    });
    act(() => {
      result.current.markDirty("a", "attributes");
    });
    act(() => {
      result.current.markDirty("a", "geometry");
    });
    act(() => {
      result.current.flush("a");
    });
    expect(listener.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("切 annotation 后 dirty 不互相污染", () => {
    const { result } = renderHook(() => useDirtyTracker());
    act(() => {
      result.current.markDirty("a", "attributes");
      result.current.markDirty("b", "class_name");
    });
    let af: string[] = [];
    let bf: string[] = [];
    act(() => {
      af = result.current.flush("a") as string[];
      bf = result.current.flush("b") as string[];
    });
    expect(af).toEqual(["attributes"]);
    expect(bf).toEqual(["class_name"]);
  });
});
