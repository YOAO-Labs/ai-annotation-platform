import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useVideoBitmapCache } from "./useVideoBitmapCache";

/** close 带 spy 的假 ImageBitmap(width 用于模拟 close 后被中和)。 */
function makeBitmap(): ImageBitmap {
  return { width: 1920, height: 1080, close: vi.fn() } as unknown as ImageBitmap;
}

// readyState 4 / 有宽高 → 通过 capture 的就绪门槛。
const video = { readyState: 4, videoWidth: 1920, videoHeight: 1080 } as unknown as HTMLVideoElement;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("useVideoBitmapCache · capture 去重", () => {
  beforeEach(() => {
    // jsdom 默认无 createImageBitmap;每次返回一个新的假位图,便于断言"是否重抓"。
    (window as unknown as { createImageBitmap: unknown }).createImageBitmap = vi.fn(async () =>
      makeBitmap(),
    );
  });

  afterEach(() => {
    delete (window as unknown as { createImageBitmap?: unknown }).createImageBitmap;
    vi.restoreAllMocks();
  });

  it("同一帧重复 capture 复用缓存位图:不重抓、不 close 正在显示的位图", async () => {
    const { result } = renderHook(() => useVideoBitmapCache({ taskId: "task-1" }));
    const createBitmap = (window as unknown as { createImageBitmap: ReturnType<typeof vi.fn> })
      .createImageBitmap;

    let first: Awaited<ReturnType<typeof result.current.capture>> = null;
    await act(async () => {
      first = await result.current.capture(video, 0, { isCurrent: () => true });
    });
    expect(first).not.toBeNull();
    expect(createBitmap).toHaveBeenCalledTimes(1);

    let second: Awaited<ReturnType<typeof result.current.capture>> = null;
    await act(async () => {
      second = await result.current.capture(video, 0, { isCurrent: () => true });
    });

    // 复用同一缓存项,没有再次 createImageBitmap。
    expect(second).toBe(first);
    expect(createBitmap).toHaveBeenCalledTimes(1);
    // 关键回归点:正在显示的位图不能被 close(close 后 width=0 → Konva drawImage 黑屏/“image source is detached”)。
    expect(
      (first as unknown as { bitmap: { close: ReturnType<typeof vi.fn> } }).bitmap.close,
    ).not.toHaveBeenCalled();
    // 复用路径仍把该帧置为 active,保证立即显示。
    expect(result.current.activeBitmap).toBe(first);
  });

  it("不同帧分别 capture 各自解码一次", async () => {
    const { result } = renderHook(() => useVideoBitmapCache({ taskId: "task-1" }));
    const createBitmap = (window as unknown as { createImageBitmap: ReturnType<typeof vi.fn> })
      .createImageBitmap;

    await act(async () => {
      await result.current.capture(video, 0, { isCurrent: () => true });
    });
    await act(async () => {
      await result.current.capture(video, 1, { isCurrent: () => true });
    });

    expect(createBitmap).toHaveBeenCalledTimes(2);
  });

  it("requires an exact current source proof before starting a capture", async () => {
    const { result } = renderHook(() => useVideoBitmapCache({ taskId: "task-1" }));
    await act(async () => {
      expect(await result.current.capture(video, 17, { isCurrent: () => false })).toBeNull();
    });
    expect(window.createImageBitmap).not.toHaveBeenCalled();
    expect(result.current.cachedRanges).toEqual([]);
  });

  it("closes a capture whose source request became stale before bitmap creation finished", async () => {
    const gate = deferred<ImageBitmap>();
    vi.mocked(window.createImageBitmap).mockReturnValueOnce(gate.promise);
    const { result } = renderHook(() => useVideoBitmapCache({ taskId: "task-1" }));
    let current = true;
    let pending!: ReturnType<typeof result.current.capture>;
    act(() => {
      pending = result.current.capture(video, 17, { isCurrent: () => current });
    });
    current = false;
    const bitmap = makeBitmap();
    await act(async () => {
      gate.resolve(bitmap);
      await pending;
    });
    expect(await pending).toBeNull();
    expect(bitmap.close).toHaveBeenCalledOnce();
    expect(result.current.activeBitmap).toBeNull();
  });

  it("isolates task ABA captures and prevents an old finally from releasing a new in-flight key", async () => {
    const firstGate = deferred<ImageBitmap>();
    const secondGate = deferred<ImageBitmap>();
    vi.mocked(window.createImageBitmap)
      .mockReturnValueOnce(firstGate.promise)
      .mockReturnValueOnce(secondGate.promise);
    const { result, rerender } = renderHook(({ taskId }) => useVideoBitmapCache({ taskId }), {
      initialProps: { taskId: "A" },
    });
    let oldCapture!: ReturnType<typeof result.current.capture>;
    let currentCapture!: ReturnType<typeof result.current.capture>;
    act(() => {
      oldCapture = result.current.capture(video, 0, { isCurrent: () => true });
    });
    rerender({ taskId: "B" });
    rerender({ taskId: "A" });
    act(() => {
      currentCapture = result.current.capture(video, 0, { isCurrent: () => true });
    });
    const staleBitmap = makeBitmap();
    await act(async () => {
      firstGate.resolve(staleBitmap);
      await oldCapture;
    });
    expect(staleBitmap.close).toHaveBeenCalledOnce();
    await act(async () => {
      expect(await result.current.capture(video, 0, { isCurrent: () => true })).toBeNull();
    });
    expect(window.createImageBitmap).toHaveBeenCalledTimes(2);
    const currentBitmap = makeBitmap();
    await act(async () => {
      secondGate.resolve(currentBitmap);
      await currentCapture;
    });
    expect(result.current.activeBitmap?.bitmap).toBe(currentBitmap);
    expect(currentBitmap.close).not.toHaveBeenCalled();
  });

  it("retires a ready native bitmap when the same original's metadata or PTS ownership changes", async () => {
    const sourceKey = (generation: string, mode: string) =>
      JSON.stringify(["same-original.mp4", generation, mode]);
    const { result, rerender, unmount } = renderHook(
      ({ sourceKey }) => useVideoBitmapCache({ taskId: "A", sourceKey }),
      { initialProps: { sourceKey: sourceKey("G1", "pts") } },
    );
    await act(async () => {
      await result.current.capture(video, 5, { isCurrent: () => true });
    });
    const oldBitmap = result.current.activeBitmap?.bitmap;
    expect(oldBitmap).toBeTruthy();
    rerender({ sourceKey: sourceKey("G2", "fps") });
    expect(result.current.activeBitmap).toBeNull();
    expect(oldBitmap?.close).toHaveBeenCalledOnce();
    await act(async () => {
      await result.current.capture(video, 5, { isCurrent: () => true });
    });
    const estimatedBitmap = result.current.activeBitmap?.bitmap;
    rerender({ sourceKey: sourceKey("G2", "pts") });
    expect(result.current.activeBitmap).toBeNull();
    expect(estimatedBitmap?.close).toHaveBeenCalledOnce();
    await act(async () => {
      await result.current.capture(video, 5, { isCurrent: () => true });
    });
    const current = result.current.activeBitmap?.bitmap;
    expect(current).not.toBe(oldBitmap);
    expect(current).not.toBe(estimatedBitmap);
    unmount();
    expect(current?.close).toHaveBeenCalledOnce();
  });

  it.each(["source", "clear", "unmount"])("retires a pending capture on %s", async (action) => {
    const gate = deferred<ImageBitmap>();
    vi.mocked(window.createImageBitmap).mockReturnValueOnce(gate.promise);
    const { result, rerender, unmount } = renderHook(
      ({ sourceKey }) => useVideoBitmapCache({ taskId: "A", sourceKey }),
      { initialProps: { sourceKey: "old.mp4" } },
    );
    let pending!: ReturnType<typeof result.current.capture>;
    act(() => {
      pending = result.current.capture(video, 0, { isCurrent: () => true });
    });
    if (action === "source") rerender({ sourceKey: "new.mp4" });
    else if (action === "clear") act(() => result.current.clear());
    else unmount();
    const bitmap = makeBitmap();
    await act(async () => {
      gate.resolve(bitmap);
      await pending;
    });
    expect(await pending).toBeNull();
    expect(bitmap.close).toHaveBeenCalledOnce();
  });
});
