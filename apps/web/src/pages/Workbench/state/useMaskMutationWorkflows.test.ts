import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import type { AnnotationResponse } from "@/types";
import type { UseMaskEditorSessionReturn } from "./useMaskEditorSession";
import {
  useMaskMutationWorkflows,
  type UseMaskMutationWorkflowsParams,
} from "./useMaskMutationWorkflows";

vi.mock("@/api/rasterMasks", () => ({
  rasterMasksApi: {
    uploadTaskContent: vi.fn(async () => ({ sha256: "upload" })),
    annotationRasterMaskContent: vi.fn(),
    annotationVideoMaskContent: vi.fn(),
  },
}));

const commitMock = vi.fn(async () => {
  throw new ApiError(409, "conflict", { reason: "version_mismatch" });
});

vi.mock("@/api/maskMutations", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/maskMutations")>();
  return {
    ...actual,
    maskMutationsApi: {
      ...actual.maskMutationsApi,
      commit: (...args: unknown[]) => commitMock(...(args as [])),
    },
  };
});

const primary = {
  id: "a1",
  task_id: "t1",
  version: 3,
  class_name: "road",
  is_locked: false,
  is_active: true,
  geometry: { type: "raster_mask" },
} as unknown as AnnotationResponse;

function makeEditor() {
  return {
    dirty: false,
    buffer: { data: { length: 9 }, height: 3, width: 3 },
    connectivity: 4,
    phase: "ready",
    sessionId: "s1",
    generation: 1,
    acceptedSessionId: "s1",
    instanceOperationPreview: null,
    commitToRle: vi.fn(() => new Uint8Array([1, 2, 3])),
    runInstanceOperation: vi.fn(function (this: unknown) {
      (this as { instanceOperationPreview: unknown }).instanceOperationPreview = {
        id: 7,
        plan: {
          kind: "split_components",
          sourceCount: 1,
          resultCount: 2,
          sourceAreas: [4],
          resultAreas: [2, 2],
          primary: new Uint8Array(9),
          created: [new Uint8Array(9)],
          focusAlpha: new Uint8Array(9),
          cutPath: null,
        },
      };
      return true;
    }),
    previewInstanceOperation: vi.fn(() => true),
    save: vi.fn(async (updater: () => Promise<unknown>) => updater()),
    cancel: vi.fn(),
    cancelOperation: vi.fn(),
    rebaseSession: vi.fn(),
    initFromRle: vi.fn(),
  } as unknown as UseMaskEditorSessionReturn;
}

function makeS(overrides: Record<string, unknown> = {}): UseMaskMutationWorkflowsParams["s"] {
  return {
    selectedId: "a1",
    selectedIds: [],
    lockedVideoTrackIds: new Set<string>(),
    videoFrameIndex: 5,
    videoTool: "select",
    setSelectedId: vi.fn(),
    setTool: vi.fn(),
    setVideoTool: vi.fn(),
    ...overrides,
  } as UseMaskMutationWorkflowsParams["s"];
}

function renderWorkflows(
  editor: UseMaskEditorSessionReturn,
  s = makeS(),
  overrides: Partial<UseMaskMutationWorkflowsParams> = {},
) {
  const params: UseMaskMutationWorkflowsParams = {
    taskId: "t1",
    isVideoTask: false,
    s,
    currentVideoSegment: null,
    maskEditor: editor,
    maskSessionContextRef: {
      current: {
        key: {
          taskId: "t1",
          frameIndex: 5,
          toolKey: "box",
          routeKey: "r",
          selectionKey: "a1",
          annotationVersion: 3,
        },
        generation: 1,
      },
    },
    currentTaskIdRef: { current: "t1" },
    annotationsRef: { current: [primary] },
    refetchAnnotations: vi.fn(async () => ({ data: [primary], isError: false, error: null })),
    annotationQueryKey: ["annotations", "t1"],
    history: { push: vi.fn() },
    pushToast: vi.fn(),
    queryClient: new QueryClient(),
    sliceWriteOwner: { current: { taskId: "t1", canWrite: true } },
    maskEditorSize: { width: 3, height: 3 },
    transitionInFlightRef: { current: false },
    ...overrides,
  };
  const rendered = renderHook(() => useMaskMutationWorkflows(params));
  return { ...rendered, params };
}

describe("useMaskMutationWorkflows", () => {
  it("runMaskInstanceOperation publishes a fresh preview and keeps failure state clean", async () => {
    const editor = makeEditor();
    const { result } = renderWorkflows(editor);

    let previewed = false;
    await act(async () => {
      previewed = await result.current.runMaskInstanceOperation("split_components", {
        type: "split_components",
        keep: "largest",
        connectivity: 4,
      } as never);
    });

    expect(previewed).toBe(true);
    expect(editor.runInstanceOperation).toHaveBeenCalledOnce();
    expect(result.current.commitError).toBeNull();
  });

  it("a version-conflict commit failure keeps the draft and only offers refresh", async () => {
    const editor = makeEditor();
    const { result } = renderWorkflows(editor);
    await act(async () => {
      await result.current.runMaskInstanceOperation("split_components", {
        type: "split_components",
        keep: "largest",
        connectivity: 4,
      } as never);
    });

    let committed = false;
    await act(async () => {
      committed = await result.current.requestCommitMaskInstanceOperation();
    });

    expect(committed).toBe(false);
    expect(commitMock).toHaveBeenCalledOnce();
    expect(result.current.commitError).toBe("来源 Mask 已变更，草稿已保留");
    expect(result.current.recovery).toEqual({ retry: false, refresh: true });
    // 草稿保留：失败的提交不得撤销/清空编辑会话
    expect(editor.cancel).not.toHaveBeenCalled();
  });

  it("refresh without a draft is a guarded no-op and never rebases a switched session", async () => {
    const editor = makeEditor();
    const { result, params, rerender } = renderWorkflows(editor);

    await act(async () => {
      await result.current.refreshMaskInstanceOperation();
    });
    expect(editor.cancel).not.toHaveBeenCalled();
    expect(editor.rebaseSession).not.toHaveBeenCalled();

    // 会话已切换到另一帧 + 新 generation：迟到的刷新不得触碰新会话。
    (params.maskSessionContextRef as { current: unknown }).current = {
      key: {
        taskId: "t1",
        frameIndex: 9,
        toolKey: "box",
        routeKey: "r",
        selectionKey: "a1",
        annotationVersion: 4,
      },
      generation: 5,
    };
    rerender();
    await act(async () => {
      await result.current.refreshMaskInstanceOperation();
    });
    expect(editor.initFromRle).not.toHaveBeenCalled();
    expect(params.refetchAnnotations).not.toHaveBeenCalled();
  });

  it("nativeMaskTrackLocallyLocked only locks video track masks of locally locked tracks", () => {
    const editor = makeEditor();
    const s = makeS({ lockedVideoTrackIds: new Set<string>(["trk_1"]) });
    const { result } = renderWorkflows(editor, s, { isVideoTask: true });

    const videoMask = {
      id: "v1",
      geometry: { type: "video_track_mask", track_id: "trk_1" },
    } as unknown as AnnotationResponse;
    const unlockedVideoMask = {
      id: "v2",
      geometry: { type: "video_track_mask", track_id: "trk_2" },
    } as unknown as AnnotationResponse;

    expect(result.current.nativeMaskTrackLocallyLocked(videoMask)).toBe(true);
    expect(result.current.nativeMaskTrackLocallyLocked(unlockedVideoMask)).toBe(false);
    expect(result.current.nativeMaskTrackLocallyLocked(primary)).toBe(false);
  });
});
