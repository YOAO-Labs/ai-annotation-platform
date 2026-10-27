import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  useTrackerSeedCollection,
  type UseTrackerSeedCollectionParams,
} from "./useTrackerSeedCollection";

function setup(unavailableReason?: string) {
  const show = vi.fn();
  const onUnavailable = vi.fn();
  const params: UseTrackerSeedCollectionParams = {
    taskId: "task-a",
    isVideoTask: true,
    videoTool: "select",
    isVideoToolEnabled: () => true,
    setVideoTool: vi.fn(),
    setVideoToolSelection: vi.fn(),
    requestTemporaryToolRef: { current: vi.fn() },
    panelCommandsRef: {
      current: { show, hide: vi.fn() } as unknown as NonNullable<
        UseTrackerSeedCollectionParams["panelCommandsRef"]["current"]
      >,
    },
    disarmChapterDraft: vi.fn(),
    clearPropagateBrush: vi.fn(),
    trackerJobs: { propagate: vi.fn(), track: vi.fn(), jobs: {}, candidates: {} },
    onUnavailable,
  };
  const hook = renderHook(
    ({ reason }) => useTrackerSeedCollection({ ...params, unavailableReason: reason }),
    {
      initialProps: { reason: unavailableReason },
    },
  );
  return { ...hook, params, show, onUnavailable };
}

describe("tracker entry admission", () => {
  it("blocks all new dialog commands without a usable backend and restores them after reconnection", () => {
    const { result, rerender, show, onUnavailable, params } = setup("未接入追踪后端");
    act(() => result.current.openDialog(null));
    act(() => result.current.toggleDialog());
    expect(result.current.dialog).toBeNull();
    expect(show).not.toHaveBeenCalled();
    expect(params.clearPropagateBrush).not.toHaveBeenCalled();
    expect(onUnavailable).toHaveBeenCalledWith("未接入追踪后端");
    rerender({ reason: undefined });
    act(() => result.current.toggleDialog());
    expect(result.current.dialog).not.toBeNull();
    expect(show).toHaveBeenCalledWith("video-tracker");
  });

  it("preserves an existing session and seeds on disconnect, while blocking new submissions", async () => {
    const { result, rerender, show, params } = setup();
    act(() => result.current.openDialog(null));
    act(() => result.current.collectPoint([0.2, 0.3], 1, 4));
    const dialog = result.current.dialog;
    rerender({ reason: "追踪后端离线" });
    act(() => result.current.openDialog(null));
    act(() => result.current.toggleDialog());
    expect(result.current.dialog).toBe(dialog);
    expect(result.current.seeds).toHaveLength(1);
    expect(show).toHaveBeenCalledTimes(3);
    await act(() => result.current.submit({} as Parameters<typeof result.current.submit>[0]));
    expect(params.trackerJobs.track).not.toHaveBeenCalled();
    expect(params.trackerJobs.propagate).not.toHaveBeenCalled();
    expect(result.current.dialog?.submitting).toBe(false);
  });
});
