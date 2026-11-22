import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useScenePlayback,
  type ScenePlaybackRate,
  type ScenePlaybackTarget,
} from "./useScenePlayback";

const nextFrame: ScenePlaybackTarget = {
  taskId: "task-2",
  annotationId: "annotation-2",
  frameIndex: 101,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type Options = Parameters<typeof useScenePlayback>[0];

function setup(overrides: Partial<Options> = {}) {
  const onActiveChange = vi.fn();
  const resolveNext = vi.fn(async () => nextFrame);
  const navigate = vi.fn(async () => true);
  let props: Options = {
    active: true,
    onActiveChange,
    taskId: "task-1",
    sceneId: "scene-1",
    frameState: { taskId: "task-1", status: "ready" },
    rate: 2,
    visible: true,
    atEnd: false,
    resolveNext,
    navigate,
    ...overrides,
  };
  const hook = renderHook((value: Options) => useScenePlayback(value), { initialProps: props });
  const update = (changes: Partial<Options>) => {
    props = { ...props, ...changes };
    hook.rerender(props);
  };
  return { ...hook, update, onActiveChange, resolveNext, navigate };
}

async function advance(milliseconds: number) {
  await act(() => vi.advanceTimersByTimeAsync(milliseconds));
}

describe("useScenePlayback", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([1, 2, 4] as ScenePlaybackRate[])(
    "dwells at least 1000 / %s ms after ready",
    async (rate) => {
      const { resolveNext, navigate } = setup({ rate });
      await advance(1000 / rate - 1);
      expect(resolveNext).not.toHaveBeenCalled();
      await advance(1);
      expect(resolveNext).toHaveBeenCalledWith({
        taskId: "task-1",
        restart: false,
        signal: expect.any(AbortSignal),
      });
      expect(navigate).toHaveBeenCalledTimes(1);
      expect(navigate).toHaveBeenCalledWith(nextFrame);
    },
  );

  it("requires the target task and confirmed Scene before starting the next dwell", async () => {
    const { update, resolveNext, result } = setup({
      frameState: { taskId: "stale-task", status: "ready" },
    });
    await advance(2_000);
    expect(resolveNext).not.toHaveBeenCalled();
    update({ frameState: { taskId: "task-1", status: "ready" } });
    await advance(500);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    expect(result.current.waiting).toBe(true);
    update({ taskId: "task-2", sceneId: undefined });
    await advance(2_000);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    update({ frameState: { taskId: "task-2", status: "ready" } });
    await advance(2_000);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    update({ sceneId: "scene-1" });
    expect(result.current.waiting).toBe(false);
    await advance(499);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(resolveNext).toHaveBeenCalledTimes(2);
  });

  it("recalculates the current dwell when the rate changes", async () => {
    const { update, resolveNext } = setup({ rate: 1 });
    await advance(200);
    update({ rate: 4 });
    await advance(49);
    expect(resolveNext).not.toHaveBeenCalled();
    await advance(1);
    expect(resolveNext).toHaveBeenCalledTimes(1);
  });

  it("keeps a pending target through a temporary null task and waits for its confirmed ready state", async () => {
    const pending = deferred<boolean>();
    const { update, resolveNext, onActiveChange, result } = setup({
      navigate: () => pending.promise,
    });
    await advance(500);
    update({ taskId: null, sceneId: undefined, frameState: { taskId: null, status: "loading" } });
    await advance(2_000);
    expect(onActiveChange).not.toHaveBeenCalled();
    expect(resolveNext).toHaveBeenCalledTimes(1);
    expect(result.current.waiting).toBe(true);
    await act(async () => pending.resolve(true));
    update({ taskId: "task-2", frameState: { taskId: "task-2", status: "ready" } });
    await advance(2_000);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    update({ sceneId: "scene-1" });
    await advance(499);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(resolveNext).toHaveBeenCalledTimes(2);
  });

  it("aborts a pending summary on pause and ignores its late response", async () => {
    const pending = deferred<ScenePlaybackTarget | null>();
    const resolver = vi.fn(() => pending.promise);
    const { result, navigate, onActiveChange } = setup({ resolveNext: resolver });
    await advance(500);
    act(() => result.current.pause());
    expect(onActiveChange).toHaveBeenCalledTimes(1);
    expect(onActiveChange).toHaveBeenCalledWith(false);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => pending.resolve(nextFrame));
    expect(navigate).not.toHaveBeenCalled();
    expect(result.current.waiting).toBe(false);
  });

  it("lets one submitted navigation finish after pause without advancing again", async () => {
    const pending = deferred<boolean>();
    const navigator = vi.fn(() => pending.promise);
    const { result, update, resolveNext } = setup({ navigate: navigator });
    await advance(500);
    act(() => result.current.pause());
    update({ active: false });
    await act(async () => pending.resolve(true));
    update({ taskId: "task-2", frameState: { taskId: "task-2", status: "ready" } });
    await advance(20_000);
    expect(navigator).toHaveBeenCalledTimes(1);
    expect(resolveNext).toHaveBeenCalledTimes(1);
  });

  it("does not overlap navigation when play is pressed again before the old route completes", async () => {
    const pending = deferred<boolean>();
    const navigator = vi.fn(() => pending.promise);
    const { result, update, resolveNext } = setup({ navigate: navigator });
    await advance(500);
    act(() => result.current.pause());
    update({ active: false });
    update({ active: true });
    expect(result.current.error).toContain("仍在切换");
    await advance(5_000);
    expect(navigator).toHaveBeenCalledTimes(1);
    expect(resolveNext).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve(true));
    await advance(5_000);
    expect(navigator).toHaveBeenCalledTimes(1);
  });

  it.each([
    { taskId: "external-task" },
    { taskId: null },
    { sceneId: "different-scene" },
    { sceneId: null },
    { visible: false },
    { blocker: "请完成当前绘制" },
  ])("pauses on external navigation, hiding, or editing: %j", async (changes) => {
    const { update, resolveNext, onActiveChange } = setup();
    update(changes);
    await advance(2_000);
    expect(onActiveChange).toHaveBeenCalledTimes(1);
    expect(onActiveChange).toHaveBeenCalledWith(false);
    expect(resolveNext).not.toHaveBeenCalled();
  });

  it("pauses when the document goes into the background and does not resume on return", async () => {
    const { onActiveChange, resolveNext } = setup();
    const hidden = vi.spyOn(document, "hidden", "get");
    hidden.mockReturnValue(true);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    hidden.mockReturnValue(false);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await advance(2_000);
    expect(onActiveChange).toHaveBeenCalledTimes(1);
    expect(onActiveChange).toHaveBeenCalledWith(false);
    expect(resolveNext).not.toHaveBeenCalled();
  });

  it("times out one continuous wait across summary resolution, navigation, and resources", async () => {
    const summary = deferred<ScenePlaybackTarget | null>();
    const { result, navigate, update, onActiveChange } = setup({
      resolveNext: () => summary.promise,
    });
    await advance(500);
    await advance(10_000);
    await act(async () => summary.resolve(nextFrame));
    expect(navigate).toHaveBeenCalledTimes(1);
    update({ taskId: null, sceneId: undefined, frameState: { taskId: null, status: "loading" } });
    await advance(4_999);
    expect(onActiveChange).not.toHaveBeenCalled();
    await advance(1);
    expect(onActiveChange).toHaveBeenCalledTimes(1);
    expect(onActiveChange).toHaveBeenCalledWith(false);
    expect(result.current.error).toContain("15 秒");
    update({
      taskId: "task-2",
      sceneId: "scene-1",
      frameState: { taskId: "task-2", status: "ready" },
    });
    await advance(1_000);
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("times out a summary request and ignores its late response", async () => {
    const pending = deferred<ScenePlaybackTarget | null>();
    const { result, navigate } = setup({ resolveNext: () => pending.promise });
    await advance(500);
    await advance(15_000);
    expect(result.current.error).toContain("15 秒");
    await act(async () => pending.resolve(nextFrame));
    expect(navigate).not.toHaveBeenCalled();
  });

  it("pauses immediately on frame errors and on rejected summary requests", async () => {
    const { update, result, resolveNext } = setup();
    update({ frameState: { taskId: "task-1", status: "error", error: "点云解码失败" } });
    expect(result.current.error).toBe("点云解码失败");
    expect(resolveNext).not.toHaveBeenCalled();
    update({ active: false });
    update({
      active: true,
      frameState: { taskId: "task-1", status: "ready" },
      resolveNext: async () => {
        throw new Error("摘要请求失败");
      },
    });
    await advance(500);
    expect(result.current.error).toBe("摘要请求失败");
  });

  it("stops at the end and requests the first accessible frame on a new play session", async () => {
    const { update, onActiveChange } = setup({ resolveNext: async () => null });
    await advance(500);
    expect(onActiveChange).toHaveBeenCalledTimes(1);
    expect(onActiveChange).toHaveBeenCalledWith(false);
    const restart = vi.fn(async () => nextFrame);
    update({ active: false });
    update({ active: true, atEnd: false, resolveNext: restart });
    await advance(500);
    expect(restart).toHaveBeenCalledWith(expect.objectContaining({ restart: true }));
  });

  it("cancels timers and ignores pending responses after unmount", async () => {
    const pending = deferred<ScenePlaybackTarget | null>();
    const { navigate, unmount } = setup({ resolveNext: () => pending.promise });
    await advance(500);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => pending.resolve(nextFrame));
    expect(navigate).not.toHaveBeenCalled();
  });
});
