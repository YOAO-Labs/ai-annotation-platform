import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import type { AnnotationResponse } from "@/types";
import type { VideoDrawingDraft, VideoStageControls } from "../stage/videoStageControls";
import { useWorkbenchState, type VideoTool } from "./useWorkbenchState";
import { useVideoToolCommands } from "./useVideoToolCommands";

vi.mock("./useWorkbenchConfig", () => ({
  useWorkbenchConfig: () => ({
    config: DEFAULT_WORKBENCH_PREFERENCES,
    layout: DEFAULT_WORKBENCH_PREFERENCES.layout,
    loaded: true,
    update: vi.fn(),
    setFields: vi.fn(),
    setLayout: vi.fn(),
  }),
}));

type Options = Omit<Parameters<typeof useVideoToolCommands>[0], "state">;
const tracks = [
  ["video_track_bbox", "track"],
  ["video_track_polygon", "polygon-track"],
  ["video_track_polyline", "polyline-track"],
  ["video_track_mask", "mask-track"],
] as const;
function annotation(id: string, type: string): AnnotationResponse {
  return { id, geometry: { type } } as AnnotationResponse;
}
function setup(overrides: Partial<Options> = {}) {
  const options: Options = {
    enabled: true,
    ownerKey: "task-a/segment-a/F0",
    controlsRef: { current: null },
    annotationsRef: { current: tracks.map(([type]) => annotation(type, type)) },
    isToolEnabled: () => true,
    toolDisabledReason: () => undefined,
    needsMaskGuard: false,
    guardMask: vi.fn(async () => true),
    explain: vi.fn(),
    ...overrides,
  };
  const view = renderHook(
    (props: Options) => {
      const state = useWorkbenchState();
      const commands = useVideoToolCommands({ ...props, state });
      return { state, commands };
    },
    { initialProps: options },
  );
  return { ...view, options };
}
function deferred() {
  let resolve!: (value: boolean) => void;
  const promise = new Promise<boolean>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("explicit video tool commands", () => {
  it("awaits checked selection without seeking or activating an editing tool", async () => {
    const { result } = setup();
    act(() => result.current.state.setVideoTool("polygon"));
    let accepted;
    await act(async () => {
      accepted = await result.current.commands.requestSelectionReady(
        "video_track_bbox",
        () => true,
      );
    });
    expect(accepted).toBe(true);
    expect(result.current.state.selectedId).toBe("video_track_bbox");
    expect(result.current.state.videoTool).toBe("polygon");
    expect(result.current.state.videoFrameIndex).toBe(0);
  });

  it("does not apply a checked selection when navigation retires during a mask save", async () => {
    const save = deferred();
    let relevant = true;
    const { result } = setup({ needsMaskGuard: true, guardMask: () => save.promise });
    act(() => result.current.state.setSelectedId("original"));
    let accepted!: Promise<boolean>;
    act(() => {
      accepted = result.current.commands.requestSelectionReady("video_track_bbox", () => relevant);
    });
    relevant = false;
    await act(async () => save.resolve(true));
    expect(await accepted).toBe(false);
    expect(result.current.state.selectedId).toBe("original");
  });

  it("reports a refused checked selection and preserves the live drawing", async () => {
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon", frameIndex: 0 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.state.setSelectedId("original"));
    let accepted!: Promise<boolean>;
    act(() => {
      accepted = result.current.commands.requestSelectionReady("video_track_bbox", () => true);
    });
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(await accepted).toBe(false);
    expect(discardDrawingDraft).not.toHaveBeenCalled();
    expect(result.current.state.selectedId).toBe("original");
  });

  it("clears a task reset without retiring navigation, while a blank user selection interrupts it", () => {
    const onUserIntent = vi.fn();
    const { result } = setup({ onUserIntent });
    act(() => result.current.state.setSelectedId("old-task-object"));
    act(() => result.current.commands.requestSelection(null, { source: "task-reset" }));
    expect(result.current.state.selectedIds).toEqual([]);
    expect(onUserIntent).not.toHaveBeenCalled();
    act(() => result.current.commands.requestSelection(null));
    expect(onUserIntent).toHaveBeenCalledOnce();
  });

  it("checks cross-task leave against a drawing without changing frame, selection or tool", async () => {
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon", frameIndex: 0 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => {
      result.current.state.setVideoTool("polygon");
      result.current.state.setSelectedId("existing");
    });
    let leave!: Promise<boolean>;
    act(() => {
      leave = result.current.commands.requestLeave(() => true);
    });
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(await leave).toBe(false);
    expect(discardDrawingDraft).not.toHaveBeenCalled();
    act(() => {
      leave = result.current.commands.requestLeave(() => true);
    });
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(await leave).toBe(true);
    expect(discardDrawingDraft).toHaveBeenCalledOnce();
    expect(result.current.state.selectedId).toBe("existing");
    expect(result.current.state.videoTool).toBe("polygon");
    expect(result.current.state.videoFrameIndex).toBe(0);
  });

  it("rejects a superseded leave after the Mask owner finishes saving", async () => {
    const save = deferred();
    let relevant = true;
    const { result } = setup({ needsMaskGuard: true, guardMask: () => save.promise });
    let leave!: Promise<boolean>;
    act(() => {
      leave = result.current.commands.requestLeave(() => relevant);
    });
    relevant = false;
    await act(async () => save.resolve(true));
    expect(await leave).toBe(false);
  });

  it.each(["ready", "cancelled", "timeout", "unavailable"] as const)(
    "checked frame navigation preserves the Stage's %s result",
    async (status) => {
      const expected = { status, frameIndex: 16, source: null };
      const seekToFrameReady = vi.fn(async () => expected);
      const seekToFrame = vi.fn();
      const { result } = setup({
        controlsRef: {
          current: { seekToFrameReady, seekToFrame } as unknown as VideoStageControls,
        },
      });
      let actual;
      await act(async () => {
        actual = await result.current.commands.requestFrameReady(16, () => true);
      });
      expect(actual).toEqual(expected);
      expect(seekToFrameReady).toHaveBeenCalledOnce();
      expect(seekToFrameReady).toHaveBeenCalledWith(16, { recordHistory: true });
      expect(seekToFrame).not.toHaveBeenCalled();
    },
  );

  it("checked navigation without Stage controls is unavailable and does not set an optimistic frame", async () => {
    const { result } = setup();
    let actual;
    await act(async () => {
      actual = await result.current.commands.requestFrameReady(16, () => true);
    });
    expect(actual).toEqual({ status: "unavailable", frameIndex: 16, source: null });
    expect(result.current.state.videoFrameIndex).toBe(0);
  });

  it("checked navigation refused by a draft confirmation leaves the draft and media untouched", async () => {
    const seekToFrameReady = vi.fn();
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon", frameIndex: 0 }),
          discardDrawingDraft,
          seekToFrameReady,
        } as unknown as VideoStageControls,
      },
    });
    let navigation: ReturnType<typeof result.current.commands.requestFrameReady>;
    act(() => {
      navigation = result.current.commands.requestFrameReady(16, () => true);
    });
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(await navigation!).toEqual({ status: "cancelled", frameIndex: 16, source: null });
    expect(seekToFrameReady).not.toHaveBeenCalled();
    expect(discardDrawingDraft).not.toHaveBeenCalled();
  });

  it("a retired Issue intent cannot accept a late successful presentation", async () => {
    let finish!: (value: { status: "ready"; frameIndex: number; source: null }) => void;
    const ready = new Promise<{ status: "ready"; frameIndex: number; source: null }>((resolve) => {
      finish = resolve;
    });
    let relevant = true;
    const { result } = setup({
      controlsRef: { current: { seekToFrameReady: () => ready } as unknown as VideoStageControls },
    });
    let navigation: ReturnType<typeof result.current.commands.requestFrameReady>;
    act(() => {
      navigation = result.current.commands.requestFrameReady(16, () => relevant);
    });
    relevant = false;
    await act(async () => finish({ status: "ready", frameIndex: 16, source: null }));
    expect(await navigation!).toEqual({ status: "cancelled", frameIndex: 16, source: null });
  });

  it("review navigation preserves the reference selection and explicit tool scope", () => {
    const { result } = setup();
    act(() => result.current.commands.requestSelection("video_track_bbox"));
    act(() => result.current.commands.requestSelection("video_track_polygon", { shift: true }));
    act(() => result.current.commands.requestTool("box"));
    act(() => result.current.commands.requestFrame(16, () => true));
    expect(result.current.state.videoFrameIndex).toBe(16);
    expect(result.current.state.selectedIds).toEqual(["video_track_bbox", "video_track_polygon"]);
    expect(result.current.state.videoTool).toBe("box");
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it("review navigation uses stage seeking only after the drawing guard admits it", async () => {
    const seekToFrame = vi.fn();
    const pausePlayback = vi.fn();
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon", frameIndex: 0 }),
          discardDrawingDraft,
          pausePlayback,
          seekToFrame,
        } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.commands.requestFrame(16, () => true));
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(seekToFrame).not.toHaveBeenCalled();
    expect(discardDrawingDraft).not.toHaveBeenCalled();
    act(() => result.current.commands.requestFrame(16, () => true));
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(discardDrawingDraft).toHaveBeenCalledOnce();
    expect(pausePlayback).toHaveBeenCalledWith({ snapToGrid: false });
    expect(seekToFrame).toHaveBeenCalledWith(16, { recordHistory: true });
  });

  it("a changed review intent retires navigation waiting for a Mask save", async () => {
    const save = deferred();
    let currentReview = true;
    const { result } = setup({ needsMaskGuard: true, guardMask: () => save.promise });
    act(() => result.current.commands.requestFrame(16, () => currentReview));
    currentReview = false;
    await act(async () => save.resolve(true));
    expect(result.current.state.videoFrameIndex).toBe(0);
  });

  it.each(tracks)(
    "%s selection maps once and explicit frame commands survive refetch",
    (type, tool) => {
      const { result, rerender, options } = setup();
      act(() => result.current.commands.requestSelection(type));
      expect(result.current.state.videoTool).toBe(tool);
      expect(result.current.state.videoToolScope).toBe("track");
      for (const explicit of ["box", "polygon", "mask"] as const) {
        act(() => result.current.commands.requestTool(explicit));
        rerender({ ...options, annotationsRef: { current: [annotation(type, type)] } });
        expect(result.current.state.selectedId).toBe(type);
        expect(result.current.state.videoTool).toBe(explicit);
        expect(result.current.state.videoToolScope).toBe("frame");
      }
      act(() => result.current.state.setSelectedId(type));
      expect(result.current.state.videoTool).toBe("mask");
      act(() => result.current.commands.requestSelection(type));
      expect(result.current.state.videoTool).toBe(tool);
    },
  );

  it("neutral select and blank selection keep the chosen scope", () => {
    const { result } = setup();
    act(() => result.current.commands.requestScope("track"));
    expect(result.current.state.videoTool).toBe("select");
    act(() => result.current.commands.requestSelection("video_track_bbox"));
    act(() => result.current.commands.requestTool("select"));
    act(() => result.current.commands.requestSelection(null));
    expect(result.current.state.videoTool).toBe("select");
    expect(result.current.state.videoToolScope).toBe("track");
    expect(result.current.state.selectedId).toBeNull();
    act(() => result.current.commands.requestScope("frame"));
    act(() => result.current.commands.requestTool("select"));
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it("Shift selection preserves the set and removal does not reapply an older track's tool", () => {
    const { result } = setup();
    act(() => result.current.commands.requestSelection("video_track_bbox"));
    act(() => result.current.commands.requestSelection("video_track_polygon", { shift: true }));
    expect(result.current.state.selectedIds).toEqual(["video_track_bbox", "video_track_polygon"]);
    expect(result.current.state.videoTool).toBe("polygon-track");
    act(() => result.current.commands.requestTool("box"));
    act(() => result.current.commands.requestSelection("video_track_polygon", { shift: true }));
    expect(result.current.state.selectedId).toBe("video_track_bbox");
    expect(result.current.state.selectedIds).toEqual(["video_track_bbox"]);
    expect(result.current.state.videoTool).toBe("box");
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it("keeps selected identity and neutral track scope when its tool is unavailable", () => {
    const { result, options } = setup({ isToolEnabled: (tool) => tool !== "polygon-track" });
    act(() => result.current.commands.requestSelection("video_track_polygon"));
    expect(result.current.state.selectedId).toBe("video_track_polygon");
    expect(result.current.state.videoTool).toBe("select");
    expect(result.current.state.videoToolScope).toBe("track");
    expect(options.explain).toHaveBeenCalledWith(expect.stringContaining("未启用"));
  });

  it("rejects unavailable AI/keypoint commands through the same admission callback", () => {
    const { result, options } = setup({
      toolDisabledReason: (tool) => (tool === "keypoint" ? "未配置骨骼" : undefined),
    });
    act(() => result.current.commands.requestTool("keypoint"));
    expect(result.current.state.videoTool).toBe("select");
    expect(options.explain).toHaveBeenCalledWith("未配置骨骼");
  });

  it.each(["points", "keypoint", "box"] as const)(
    "%s draft is untouched on continue and discarded only after approval",
    async (kind) => {
      const discardDrawingDraft = vi.fn();
      const getDrawingDraft = vi.fn(() => ({ kind, tool: "polygon" as VideoTool, frameIndex: 0 }));
      const { result } = setup({
        controlsRef: {
          current: { getDrawingDraft, discardDrawingDraft } as unknown as VideoStageControls,
        },
      });
      act(() => result.current.state.setVideoTool("polygon"));
      act(() => result.current.commands.requestScope("track"));
      expect(result.current.commands.confirmationOpen).toBe(true);
      expect(result.current.state.videoTool).toBe("polygon");
      await act(async () => result.current.commands.settleConfirmation(false));
      expect(discardDrawingDraft).not.toHaveBeenCalled();
      expect(result.current.state.videoToolScope).toBe("frame");
      act(() => result.current.commands.requestScope("track"));
      await act(async () => result.current.commands.settleConfirmation(true));
      expect(discardDrawingDraft).toHaveBeenCalledOnce();
      expect(result.current.state.videoTool).toBe("polygon-track");
      expect(result.current.state.videoToolScope).toBe("track");
    },
  );

  it("retains the completed geometry and its captured frame while class selection is pending", async () => {
    const { result } = setup();
    const pending = {
      kind: "video_polygon" as const,
      frameIndex: 8,
      points: [
        [0.1, 0.1],
        [0.3, 0.1],
        [0.3, 0.3],
      ] as [number, number][],
      geom: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      anchor: { left: 300, top: 200 },
    };
    act(() => {
      result.current.state.setVideoTool("polygon");
      result.current.state.setPendingDrawing(pending);
    });
    act(() => result.current.commands.requestScope("track"));
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(result.current.state.pendingDrawing).toBe(pending);
    expect(result.current.state.videoTool).toBe("polygon");
    act(() => result.current.commands.requestScope("track"));
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(result.current.state.pendingDrawing).toBeNull();
    expect(result.current.state.videoTool).toBe("polygon-track");
  });

  it.each([false, true])(
    "a box released during confirmation keeps or discards the migrated class draft (discard=%s)",
    async (discard) => {
      let draft: VideoDrawingDraft | null = { kind: "box", tool: "box", frameIndex: 8 };
      const discardDrawingDraft = vi.fn();
      const { result } = setup({
        controlsRef: {
          current: {
            getDrawingDraft: () => draft,
            discardDrawingDraft,
          } as unknown as VideoStageControls,
        },
      });
      act(() => result.current.state.setVideoTool("box"));
      act(() => result.current.commands.requestTool("track"));
      expect(result.current.commands.confirmationOpen).toBe(true);
      const pending = {
        kind: "video_bbox" as const,
        frameIndex: 8,
        geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
        anchor: { left: 300, top: 200 },
      };
      draft = null;
      act(() => result.current.state.setPendingDrawing(pending));
      await act(async () => result.current.commands.settleConfirmation(discard));
      expect(result.current.state.pendingDrawing).toBe(discard ? null : pending);
      expect(result.current.state.videoTool).toBe(discard ? "track" : "box");
      expect(discardDrawingDraft).toHaveBeenCalledTimes(discard ? 1 : 0);
    },
  );

  it.each([
    { kind: "video_bbox" as const, frameIndex: 9 },
    { kind: "video_polygon" as const, frameIndex: 8 },
  ])("does not discard a foreign pending draft: %s", async (owner) => {
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "box", tool: "box", frameIndex: 8 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.state.setVideoTool("box"));
    act(() => result.current.commands.requestTool("track"));
    const pending = {
      ...owner,
      geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      anchor: { left: 300, top: 200 },
    };
    act(() => result.current.state.setPendingDrawing(pending));
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(result.current.state.pendingDrawing).toBe(pending);
    expect(result.current.state.videoTool).toBe("box");
    expect(discardDrawingDraft).not.toHaveBeenCalled();
  });

  it("rechecks the pending draft owner after awaiting the Mask writer", async () => {
    const save = deferred();
    const { result } = setup({ needsMaskGuard: true, guardMask: () => save.promise });
    const pending = {
      kind: "video_bbox" as const,
      frameIndex: 8,
      geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      anchor: { left: 300, top: 200 },
    };
    act(() => {
      result.current.state.setVideoTool("box");
      result.current.state.setPendingDrawing(pending);
    });
    act(() => result.current.commands.requestTool("track"));
    await act(async () => result.current.commands.settleConfirmation(true));
    const replacement = { ...pending, frameIndex: 9 };
    act(() => result.current.state.setPendingDrawing(replacement));
    await act(async () => save.resolve(true));
    expect(result.current.state.pendingDrawing).toBe(replacement);
    expect(result.current.state.videoTool).toBe("box");
  });

  it.each(["tool", "scope", "selection", "seed"] as const)(
    "%s waits for an existing track continuation to finish before switching",
    (command) => {
      let drawing = true;
      const discardDrawingDraft = vi.fn();
      const seedAdmitted = vi.fn();
      const { result, options } = setup({
        controlsRef: {
          current: {
            getDrawingDraft: () =>
              drawing
                ? {
                    kind: "box",
                    tool: "track",
                    frameIndex: 8,
                    continuingTrack: true,
                  }
                : null,
            discardDrawingDraft,
          } as unknown as VideoStageControls,
        },
      });
      act(() => {
        result.current.state.setVideoTool("track");
        result.current.state.setVideoFrameIndex(8);
        result.current.state.replaceSelected(["video_track_bbox"]);
      });
      act(() => {
        if (command === "tool") result.current.commands.requestTool("box");
        if (command === "scope") result.current.commands.requestScope("frame");
        if (command === "selection")
          result.current.commands.requestSelection("video_track_polygon", { frameIndex: 0 });
        if (command === "seed")
          result.current.commands.requestTemporaryTool("smart-point", seedAdmitted, () => true);
      });
      expect(result.current.commands.confirmationOpen).toBe(false);
      expect(result.current.state.videoTool).toBe("track");
      expect(result.current.state.videoToolScope).toBe("track");
      expect(result.current.state.videoFrameIndex).toBe(8);
      expect(result.current.state.selectedId).toBe("video_track_bbox");
      expect(discardDrawingDraft).not.toHaveBeenCalled();
      expect(seedAdmitted).not.toHaveBeenCalled();
      expect(options.explain).toHaveBeenCalledWith("正在续画轨迹，请先松手完成本帧后再切换");
      drawing = false;
      act(() => result.current.commands.requestTool("box"));
      expect(result.current.state.videoTool).toBe("box");
      expect(result.current.state.videoToolScope).toBe("frame");
    },
  );

  it.each(["task-b/segment-a/F0", "task-a/segment-b/F0", "task-a/segment-a/F1"])(
    "late Mask approval cannot change %s",
    async (ownerKey) => {
      const save = deferred();
      const { result, rerender, options } = setup({
        needsMaskGuard: true,
        guardMask: () => save.promise,
      });
      act(() => result.current.state.setVideoTool("mask"));
      act(() => result.current.commands.requestScope("track"));
      rerender({ ...options, ownerKey });
      await act(async () => save.resolve(true));
      expect(result.current.state.videoTool).toBe("mask");
      expect(result.current.state.videoToolScope).toBe("frame");
    },
  );

  it("a newer explicit command supersedes a pending Mask save", async () => {
    const save = deferred();
    const { result, rerender, options } = setup({
      needsMaskGuard: true,
      guardMask: () => save.promise,
    });
    act(() => result.current.state.setVideoTool("mask"));
    act(() => result.current.commands.requestScope("track"));
    rerender({ ...options, needsMaskGuard: false });
    act(() => result.current.commands.requestTool("polygon"));
    await act(async () => save.resolve(true));
    expect(result.current.state.videoTool).toBe("polygon");
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it.each([false, true])(
    "Mask save failure/continue retains the original selection (reject=%s)",
    async (reject) => {
      const guardMask = vi.fn(async () => {
        if (reject) throw new Error("save failed");
        return false;
      });
      const { result, options } = setup({ needsMaskGuard: true, guardMask });
      act(() => result.current.state.setVideoTool("mask"));
      await act(async () => result.current.commands.requestSelection("video_track_bbox"));
      expect(result.current.state.videoTool).toBe("mask");
      expect(result.current.state.selectedId).toBeNull();
      expect(guardMask).toHaveBeenCalledOnce();
      if (reject) expect(options.explain).toHaveBeenCalledWith(expect.stringContaining("保存失败"));
    },
  );

  it("rechecks capability after a pending draft confirmation", async () => {
    const getDrawingDraft = () => ({
      kind: "points" as const,
      tool: "polygon" as const,
      frameIndex: 0,
    });
    const { result, rerender, options } = setup({
      controlsRef: {
        current: { getDrawingDraft, discardDrawingDraft: vi.fn() } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.state.setVideoTool("polygon"));
    act(() => result.current.commands.requestScope("track"));
    rerender({ ...options, isToolEnabled: (tool) => tool !== "polygon-track" });
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(result.current.state.videoTool).toBe("select");
    expect(result.current.state.videoToolScope).toBe("track");
    expect(options.explain).toHaveBeenCalledWith(expect.stringContaining("未启用"));
  });

  it("context changes close pending confirmation without discarding its owner", async () => {
    const discardDrawingDraft = vi.fn();
    const { result, rerender, options } = setup({
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "box", tool: "box", frameIndex: 0 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.state.setVideoTool("box"));
    act(() => result.current.commands.requestScope("track"));
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => rerender({ ...options, ownerKey: "new-task" }));
    expect(result.current.commands.confirmationOpen).toBe(false);
    expect(discardDrawingDraft).not.toHaveBeenCalled();
  });

  it("a native Mask class save stays owned by its writer while tool commands are blocked", () => {
    const { result, options } = setup({ blockedReason: "请先为 Mask 选择类别或取消保存" });
    const pending = {
      kind: "video_mask" as const,
      frameIndex: 0,
      geom: { x: 0.1, y: 0.2, w: 0.2, h: 0.2 },
      anchor: { left: 300, top: 200 },
    };
    act(() => {
      result.current.state.setVideoTool("mask");
      result.current.state.setPendingDrawing(pending);
    });
    act(() => result.current.commands.requestScope("track"));
    expect(result.current.commands.confirmationOpen).toBe(false);
    expect(result.current.state.pendingDrawing).toBe(pending);
    expect(result.current.state.videoTool).toBe("mask");
    expect(options.guardMask).not.toHaveBeenCalled();
    expect(options.explain).toHaveBeenCalledWith("请先为 Mask 选择类别或取消保存");
  });

  it("a list selection waits before seeking or changing the local selection set", async () => {
    const discardDrawingDraft = vi.fn();
    const onAdmitted = vi.fn();
    const { result } = setup({
      ownerKey: "task-a/segment-a/F8",
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon", frameIndex: 8 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => {
      result.current.state.setVideoFrameIndex(8);
      result.current.state.setVideoTool("polygon");
    });
    act(() =>
      result.current.commands.requestSelection("video_track_bbox", { frameIndex: 0, onAdmitted }),
    );
    expect(result.current.state.videoFrameIndex).toBe(8);
    expect(onAdmitted).not.toHaveBeenCalled();
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(result.current.state.videoTool).toBe("polygon");
    expect(result.current.state.videoFrameIndex).toBe(8);
    expect(result.current.state.selectedId).toBeNull();
    act(() =>
      result.current.commands.requestSelection("video_track_bbox", { frameIndex: 0, onAdmitted }),
    );
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(result.current.state.videoTool).toBe("track");
    expect(result.current.state.videoToolScope).toBe("track");
    expect(result.current.state.videoFrameIndex).toBe(0);
    expect(result.current.state.selectedId).toBe("video_track_bbox");
    expect(discardDrawingDraft).toHaveBeenCalledOnce();
    expect(onAdmitted).toHaveBeenCalledOnce();
  });

  it("removing a roster member may restore its old primary without reactivating that track tool", () => {
    const { result } = setup();
    act(() => result.current.state.setVideoTool("polygon"));
    act(() =>
      result.current.commands.requestSelection("video_track_bbox", { activateTrackTool: false }),
    );
    expect(result.current.state.selectedId).toBe("video_track_bbox");
    expect(result.current.state.videoTool).toBe("polygon");
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it("tracker seed collection admits its draft before borrowing a tool with separate capabilities", async () => {
    const onAdmitted = vi.fn();
    const discardDrawingDraft = vi.fn();
    const { result } = setup({
      isToolEnabled: (tool) => tool !== "smart-point",
      controlsRef: {
        current: {
          getDrawingDraft: () => ({ kind: "points", tool: "polygon-track", frameIndex: 0 }),
          discardDrawingDraft,
        } as unknown as VideoStageControls,
      },
    });
    act(() => result.current.state.setVideoTool("polygon-track"));
    act(() => result.current.commands.requestTemporaryTool("smart-point", onAdmitted, () => true));
    expect(result.current.commands.confirmationOpen).toBe(true);
    await act(async () => result.current.commands.settleConfirmation(false));
    expect(onAdmitted).not.toHaveBeenCalled();
    expect(result.current.state.videoTool).toBe("polygon-track");
    act(() => result.current.commands.requestTemporaryTool("smart-point", onAdmitted, () => true));
    await act(async () => result.current.commands.settleConfirmation(true));
    expect(onAdmitted).toHaveBeenCalledWith({ tool: "polygon-track", scope: "track" });
    expect(discardDrawingDraft).toHaveBeenCalledOnce();
    expect(result.current.state.videoTool).toBe("smart-point");
    expect(result.current.state.videoToolScope).toBe("frame");
  });

  it("closing the tracker panel invalidates a delayed seed admission", async () => {
    const save = deferred();
    const onAdmitted = vi.fn();
    let panelOpen = true;
    const { result } = setup({ needsMaskGuard: true, guardMask: () => save.promise });
    act(() => result.current.state.setVideoTool("mask-track"));
    act(() =>
      result.current.commands.requestTemporaryTool("smart-point", onAdmitted, () => panelOpen),
    );
    panelOpen = false;
    await act(async () => save.resolve(true));
    expect(onAdmitted).not.toHaveBeenCalled();
    expect(result.current.state.videoTool).toBe("mask-track");
    expect(result.current.state.videoToolScope).toBe("track");
  });

  it("does not apply a late save after unmount", async () => {
    const save = deferred();
    const { result, unmount, options } = setup({
      needsMaskGuard: true,
      guardMask: () => save.promise,
      toolDisabledReason: () => "disabled",
    });
    act(() => result.current.state.setVideoTool("mask-track"));
    act(() => result.current.commands.requestTool("box"));
    unmount();
    await act(async () => save.resolve(true));
    expect(options.explain).not.toHaveBeenCalled();
  });
});
