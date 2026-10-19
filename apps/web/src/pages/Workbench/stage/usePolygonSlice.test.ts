import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Annotation } from "@/types";
import { usePolygonSlice } from "./usePolygonSlice";

const source = {
  id: "saved",
  version: 1,
  geometry: {
    type: "polygon",
    points: [
      [0.2, 0.2],
      [0.8, 0.2],
      [0.8, 0.8],
      [0.2, 0.8],
    ],
  },
} as Annotation;
afterEach(() => vi.unstubAllGlobals());
function setup(commit = vi.fn(async () => {})) {
  const { result, rerender, unmount } = renderHook((props) => usePolygonSlice(props), {
    initialProps: { enabled: true, owner: "task-a", annotations: [source], commit },
  });
  const draw = () =>
    act(() => {
      result.current.begin(source);
      result.current.addPoint([0.5, 0]);
      result.current.addPoint([0.5, 1]);
      result.current.preview();
    });
  return { result, rerender, unmount, commit, draw };
}
describe("Polygon Slice preview ownership", () => {
  it("cancel and editing the cut never persist, while confirmation is single-flight", async () => {
    let resolve!: () => void;
    const h = setup(
      vi.fn(
        () =>
          new Promise<void>((done) => {
            resolve = done;
          }),
      ),
    );
    h.draw();
    act(() => h.result.current.cancel());
    expect(h.commit).not.toHaveBeenCalled();
    h.draw();
    act(() => h.result.current.back());
    expect(h.result.current.session?.preview).toBeUndefined();
    expect(h.result.current.session?.points).toHaveLength(2);
    act(() => h.result.current.preview());
    let request!: Promise<void>;
    act(() => {
      request = h.result.current.confirm();
    });
    await act(async () => h.result.current.confirm());
    act(() => h.result.current.cancel());
    expect(h.result.current.session?.busy).toBe(true);
    expect(h.commit).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolve();
      await request;
    });
    expect(h.result.current.session).toBeNull();
  });
  it("retains the exact request and preview after response loss, even after a source refetch", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const commit = vi
      .fn()
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValue(undefined);
    const h = setup(commit);
    h.draw();
    await act(async () => h.result.current.confirm());
    expect(h.result.current.session?.error).toBe("response lost");
    expect(commit.mock.calls[0][0].idempotency_key).toMatch(/^[0-9a-f]{32}$/);
    h.rerender({
      enabled: true,
      owner: "task-a",
      annotations: [{ ...source, version: 2 }],
      commit,
    });
    expect(h.result.current.session?.preview).toHaveLength(2);
    act(() => h.result.current.back());
    await act(async () => h.result.current.confirm());
    expect(commit.mock.calls[0]).toEqual(commit.mock.calls[1]);
  });
  it("invalidates changed source geometry before sending anything", () => {
    const h = setup();
    h.draw();
    h.rerender({
      enabled: true,
      owner: "task-a",
      annotations: [{ ...source, version: 2 }],
      commit: h.commit,
    });
    expect(h.result.current.session?.invalid).toBe(true);
    expect(h.result.current.session?.preview).toBeUndefined();
    expect(h.commit).not.toHaveBeenCalled();
  });
  it("retired requests cannot clear the new task's preview", async () => {
    let resolve!: () => void;
    const h = setup(
      vi.fn(
        () =>
          new Promise<void>((done) => {
            resolve = done;
          }),
      ),
    );
    h.draw();
    let request!: Promise<void>;
    act(() => {
      request = h.result.current.confirm();
    });
    h.rerender({ enabled: true, owner: "task-b", annotations: [source], commit: h.commit });
    expect(h.result.current.session).toBeNull();
    h.draw();
    const next = h.result.current.session;
    await act(async () => {
      resolve();
      await request;
    });
    expect(h.result.current.session).toBe(next);
  });
  it("Esc cancels and Backspace removes one point before any API request", () => {
    const h = setup();
    act(() => {
      h.result.current.begin(source);
      h.result.current.addPoint([0, 0.5]);
    });
    act(() => h.result.current.keyDown(new KeyboardEvent("keydown", { key: "Backspace" })));
    expect(h.result.current.session?.points).toHaveLength(0);
    act(() => h.result.current.keyDown(new KeyboardEvent("keydown", { key: "Escape" })));
    expect(h.result.current.session).toBeNull();
    expect(h.commit).not.toHaveBeenCalled();
  });
});
