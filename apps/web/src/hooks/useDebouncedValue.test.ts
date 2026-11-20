import { act, renderHook } from "@testing-library/react";
import { createElement, StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useDebouncedValue } from "./useDebouncedValue";

describe("useDebouncedValue", () => {
  afterEach(() => vi.useRealTimers());

  it("updates only after the requested delay", () => {
    vi.useFakeTimers();
    const { result, rerender } = renderHook(({ value }) => useDebouncedValue(value, 250), {
      initialProps: { value: "" },
    });
    rerender({ value: "car" });
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(249));
    expect(result.current).toBe("");
    act(() => vi.advanceTimersByTime(1));
    expect(result.current).toBe("car");
  });

  it("never exposes the retired value during StrictMode navigation renders", () => {
    const seen: string[] = [];
    const { rerender } = renderHook(
      ({ value, owner }) => {
        const current = useDebouncedValue(value, 250, owner);
        seen.push(current);
        return current;
      },
      {
        initialProps: { value: "old", owner: 1 },
        wrapper: ({ children }) => createElement(StrictMode, null, children),
      },
    );
    seen.length = 0;
    rerender({ value: "new", owner: 2 });
    expect(seen.length).toBeGreaterThan(0);
    expect(new Set(seen)).toEqual(new Set(["new"]));
  });
});
