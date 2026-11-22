import { QueryClient } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnnotationResponse } from "@/types";
import type { AnnotationPayload } from "@/api/tasks";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";
import { useVideoAnnotationActions } from "./useVideoAnnotationActions";

const apiMocks = vi.hoisted(() => ({
  composeVideoTracks: vi.fn(),
  uploadTaskContent: vi.fn(),
  saveMaskKeyframe: vi.fn(),
  updateAnnotation: vi.fn(),
}));
const { enqueueDurably } = vi.hoisted(() => ({ enqueueDurably: vi.fn() }));

vi.mock("../../state/offlineQueue", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  enqueueDurably,
}));

vi.mock("@/api/tasks", () => ({
  tasksApi: apiMocks,
}));

vi.mock("@/api/rasterMasks", () => ({
  rasterMasksApi: { uploadTaskContent: apiMocks.uploadTaskContent },
}));

vi.mock("@/api/videoTracker", () => ({
  videoTrackerApi: { saveMaskKeyframe: apiMocks.saveMaskKeyframe },
}));

const trackAnn: AnnotationResponse = {
  id: "ann-a",
  task_id: "task-1",
  project_id: "project-1",
  user_id: "user-1",
  source: "manual",
  annotation_type: "video_track_bbox",
  class_name: "Car",
  geometry: {
    type: "video_track_bbox",
    track_id: "trk_a",
    keyframes: [{ frame_index: 0, bbox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 }, source: "manual" }],
  },
  confidence: 1,
  parent_prediction_id: null,
  parent_annotation_id: null,
  lead_time: null,
  is_active: true,
  ground_truth: false,
  attributes: {},
  created_at: "2026-05-21T00:00:00Z",
  updated_at: null,
};

function setup(context: { userId?: string; segmentId?: string } = {}) {
  const annotationsRef = { current: [trackAnn] as AnnotationResponse[] };
  const history = { push: vi.fn(), pushBatch: vi.fn() };
  const s = { setSelectedId: vi.fn() };
  const args = {
    taskId: "task-1",
    projectId: "project-1",
    queryClient: new QueryClient(),
    history: history as never,
    s: s as never,
    annotationsRef,
    pushToast: vi.fn(),
    recordRecentClass: vi.fn(),
    optimisticEnqueueCreate: vi.fn(),
    enqueueOnError: vi.fn(),
    mutations: {
      create: { mutate: vi.fn() },
      update: { mutate: vi.fn() },
      delete: { mutate: vi.fn() },
    },
  };
  const { result, rerender } = renderHook(
    ({ userId, segmentId }) =>
      useVideoAnnotationActions({
        ...args,
        meUserId: userId,
        videoSegmentId: segmentId,
        annotationQueryKey: segmentId ? ["annotations", args.taskId, segmentId] : undefined,
      } as never),
    { initialProps: context },
  );
  return { result, history, queryClient: args.queryClient, args, rerender };
}

describe("offline video edits", () => {
  beforeEach(() => {
    enqueueDurably.mockReset();
    useAuthStore.getState().setAuth("owner-token", { id: "user-1" } as MeResponse);
  });
  afterEach(() => useAuthStore.getState().logout());

  it.each(["class", "attributes"] as const)(
    "accepts %s only after a matching segment's durable write",
    async (edit) => {
      const { result, history, queryClient, args } = setup({ userId: "user-1", segmentId: "a" });
      const original = { ...trackAnn, video_segment_id: "a" };
      const neighbor = { ...trackAnn, video_segment_id: "b" };
      queryClient.setQueryData(["annotations", "task-1", "a"], [original]);
      queryClient.setQueryData(["annotations", "task-1", "b"], [neighbor]);
      let settle!: () => void;
      enqueueDurably.mockReturnValueOnce(new Promise<void>((resolve) => (settle = resolve)));
      act(() => {
        if (edit === "class") result.current.handleVideoRename(original, "Truck");
        else result.current.handleUpdateTrackAttributes(original, { checked: true });
      });
      const submitted = args.mutations.update.mutate.mock.calls[0];
      expect(submitted[1].optimistic).toBe(false);
      let pending!: Promise<void>;
      act(() => {
        pending = submitted[1].onError(new TypeError("offline"));
      });
      expect(history.push).not.toHaveBeenCalled();
      expect(queryClient.getQueryData(["annotations", "task-1", "a"])).toEqual([original]);
      await act(async () => {
        settle();
        await pending;
      });
      const expected =
        edit === "class" ? { class_name: "Truck" } : { attributes: { checked: true } };
      expect(enqueueDurably).toHaveBeenCalledWith(
        expect.objectContaining({ videoSegmentId: "a", taskId: "task-1", payload: expected }),
        { userId: "user-1", taskId: "task-1" },
      );
      expect(history.push).toHaveBeenCalledOnce();
      expect(queryClient.getQueryData(["annotations", "task-1", "a"])).toMatchObject([expected]);
      expect(queryClient.getQueryData(["annotations", "task-1", "b"])).toEqual([neighbor]);
      expect(queryClient.getQueryData(["annotations", "task-1"])).toBeUndefined();
    },
  );

  it("commits a late queue write to the original owner without repainting the next account", async () => {
    const { result, history, args, rerender, queryClient } = setup({
      userId: "user-1",
      segmentId: "a",
    });
    let settle!: () => void;
    enqueueDurably.mockReturnValueOnce(new Promise<void>((resolve) => (settle = resolve)));
    act(() => result.current.handleVideoRename(trackAnn, "Truck"));
    const onError = args.mutations.update.mutate.mock.calls[0][1].onError;
    let pending!: Promise<void>;
    act(() => {
      pending = onError(new TypeError("offline"));
    });
    const neighbor = { ...trackAnn, class_name: "Next account" };
    act(() => useAuthStore.getState().setAuth("next-token", { id: "user-2" } as MeResponse));
    rerender({ userId: "user-2", segmentId: "a" });
    queryClient.setQueryData(["annotations", "task-1", "a"], [neighbor]);
    await act(async () => {
      settle();
      await pending;
    });
    expect(history.push).toHaveBeenCalledOnce();
    expect(queryClient.getQueryData(["annotations", "task-1", "a"])).toEqual([neighbor]);
    expect(args.pushToast).not.toHaveBeenCalled();
  });

  it("retries an aborted edit only after returning to its original segment", async () => {
    const { result, history, args, rerender, queryClient } = setup({
      userId: "user-1",
      segmentId: "a",
    });
    const original = { ...trackAnn, video_segment_id: "a" };
    queryClient.setQueryData(["annotations", "task-1", "a"], [original]);
    enqueueDurably
      .mockRejectedValueOnce(new Error("quota exceeded"))
      .mockResolvedValueOnce(undefined);
    act(() => result.current.handleVideoRename(original, "Truck"));
    const update = args.mutations.update.mutate;
    await act(async () => update.mock.calls[0][1].onError(new TypeError("offline")));
    expect(history.push).not.toHaveBeenCalled();
    const [{ retry }] = args.pushToast.mock.calls[0];
    rerender({ userId: "user-1", segmentId: "b" });
    expect(retry()).toBe(false);
    expect(update).toHaveBeenCalledTimes(1);
    rerender({ userId: "user-1", segmentId: "a" });
    act(() => {
      expect(retry()).toBe(true);
      expect(retry()).toBe(false);
    });
    await act(async () => update.mock.calls[1][1].onError(new TypeError("offline")));
    expect(enqueueDurably.mock.calls[0][0].id).toBe(enqueueDurably.mock.calls[1][0].id);
    expect(queryClient.getQueryData(["annotations", "task-1", "a"])).toMatchObject([
      { class_name: "Truck" },
    ]);
    expect(history.push).toHaveBeenCalledOnce();
  });
});

describe("handleVideoComposeTracks gap_mode passthrough", () => {
  beforeEach(() => {
    apiMocks.composeVideoTracks.mockReset();
    apiMocks.composeVideoTracks.mockResolvedValue({
      operation: "join_tracks",
      updated_annotations: [],
      created_annotations: [],
      deleted_annotation_ids: [],
    });
  });

  it("forwards gapMode as gap_mode for join_tracks", async () => {
    const { result } = setup();
    await result.current.handleVideoComposeTracks({
      operation: "join_tracks",
      annotationIds: ["ann-a"],
      gapMode: "outside",
    });
    expect(apiMocks.composeVideoTracks).toHaveBeenCalledWith("task-1", {
      operation: "join_tracks",
      annotation_ids: ["ann-a"],
      frame_index: undefined,
      delete_sources: undefined,
      gap_mode: "outside",
    });
  });

  it("leaves gap_mode undefined for merge_tracks", async () => {
    const { result } = setup();
    await result.current.handleVideoComposeTracks({
      operation: "merge_tracks",
      annotationIds: ["ann-a"],
    });
    expect(apiMocks.composeVideoTracks).toHaveBeenCalledWith("task-1", {
      operation: "merge_tracks",
      annotation_ids: ["ann-a"],
      frame_index: undefined,
      delete_sources: undefined,
      gap_mode: undefined,
    });
  });
});

describe("handleVideoMaskCommit", () => {
  const digest = "a".repeat(64);
  const rle = {
    encoding: "coco_rle" as const,
    size: [2, 3] as [number, number],
    counts: [1, 2, 3],
  };
  const reference = {
    encoding: "coco_rle_ref" as const,
    size: [2, 3] as [number, number],
    object_key: `raster-masks/sha256/aa/aa/${digest}.json`,
    sha256: digest,
    runs: 3,
    bytes: 64,
  };
  const maskTrack: AnnotationResponse = {
    ...trackAnn,
    id: "mask-1",
    annotation_type: "video_track_mask",
    tool_unit_id: "region",
    version: 3,
    geometry: {
      type: "video_track_mask",
      track_id: "mask-track-1",
      keyframes: [{ frame_index: 1, mask: reference, source: "manual" }],
      outside: [],
    },
  };

  beforeEach(() => {
    apiMocks.uploadTaskContent.mockReset();
    apiMocks.saveMaskKeyframe.mockReset();
    apiMocks.updateAnnotation.mockReset();
    apiMocks.uploadTaskContent.mockResolvedValue(reference);
    apiMocks.saveMaskKeyframe.mockResolvedValue({ ...maskTrack, version: 4 });
  });

  it("已有 Mask 轨迹走专用单帧 PUT 并采信服务端新版本", async () => {
    const { result, history, queryClient } = setup();
    queryClient.setQueryData(["annotations", "task-1"], [maskTrack]);

    const saved = await result.current.handleVideoMaskCommit(rle, 5, maskTrack);

    expect(apiMocks.uploadTaskContent).toHaveBeenCalledWith("task-1", rle);
    expect(apiMocks.saveMaskKeyframe).toHaveBeenCalledWith("task-1", "mask-1", 5, reference, 3);
    expect(saved).toMatchObject({
      annotation: { id: "mask-1", version: 4 },
      mask: reference,
      frameIndex: 5,
    });
    expect(history.push).toHaveBeenCalledTimes(1);
    expect(
      queryClient.getQueryData<AnnotationResponse[]>(["annotations", "task-1"])?.[0],
    ).toMatchObject({ id: "mask-1", version: 4 });
  });

  it("上传完成前 owner 失效不写旧关键帧或当前历史", async () => {
    const { result, history } = setup();
    let resolveUpload!: (value: typeof reference) => void;
    apiMocks.uploadTaskContent.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpload = resolve;
        }),
    );
    let current = true;
    const saved = result.current.handleVideoMaskCommit(rle, 5, maskTrack, "track", () => current);
    current = false;
    resolveUpload(reference);
    expect(await saved).toBeNull();
    expect(apiMocks.saveMaskKeyframe).not.toHaveBeenCalled();
    expect(history.push).not.toHaveBeenCalled();
  });

  it("旧关键帧迟到成功仅更新原查询缓存，不进入当前历史", async () => {
    const { result, history, queryClient } = setup();
    queryClient.setQueryData(["annotations", "task-1"], [maskTrack]);
    let resolveSave!: (value: AnnotationResponse) => void;
    apiMocks.saveMaskKeyframe.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    );
    let current = true;
    const saved = result.current.handleVideoMaskCommit(rle, 5, maskTrack, "track", () => current);
    await vi.waitFor(() => expect(apiMocks.saveMaskKeyframe).toHaveBeenCalledTimes(1));
    current = false;
    resolveSave({ ...maskTrack, version: 4 });
    expect(await saved).toBeNull();
    expect(history.push).not.toHaveBeenCalled();
    expect(
      queryClient.getQueryData<AnnotationResponse[]>(["annotations", "task-1"])?.[0].version,
    ).toBe(4);
  });

  it("版本缺失时在上传前稳定失败", async () => {
    const { result } = setup();

    await expect(
      result.current.handleVideoMaskCommit(rle, 5, { ...maskTrack, version: undefined }),
    ).rejects.toThrow("version is missing");
    expect(apiMocks.uploadTaskContent).not.toHaveBeenCalled();
    expect(apiMocks.saveMaskKeyframe).not.toHaveBeenCalled();
  });

  it("新建单帧 Mask 先选 region 类别，再创建 video_mask", async () => {
    const state = {
      activeClass: "Recommended",
      pendingDrawing: null as import("../../state/useWorkbenchState").PendingDrawing,
      setPendingDrawing: vi.fn(
        (pending: import("../../state/useWorkbenchState").PendingDrawing) => {
          state.pendingDrawing = pending;
        },
      ),
      setSelectedId: vi.fn(),
    };
    const create = vi.fn(
      (
        payload: AnnotationPayload,
        options?: { onSuccess?: (created: AnnotationResponse) => void },
      ) => {
        options?.onSuccess?.({
          ...maskTrack,
          id: "mask-new",
          class_name: payload.class_name,
          geometry: payload.geometry,
        });
      },
    );
    const { result } = renderHook(() =>
      useVideoAnnotationActions({
        taskId: "task-1",
        queryClient: new QueryClient(),
        history: { push: vi.fn(), pushBatch: vi.fn() } as never,
        s: state as never,
        annotationsRef: { current: [] },
        pushToast: vi.fn(),
        recordRecentClass: vi.fn(),
        optimisticEnqueueCreate: vi.fn(),
        enqueueOnError: vi.fn(),
        activeToolHasOwnClasses: true,
        mutations: {
          create: { mutate: create },
          update: { mutate: vi.fn() },
          delete: { mutate: vi.fn() },
        },
      }),
    );

    const savePromise = result.current.handleVideoMaskCommit(rle, 5, null);
    expect(state.pendingDrawing).toMatchObject({ kind: "video_mask", frameIndex: 5 });
    expect(apiMocks.uploadTaskContent).not.toHaveBeenCalled();

    let saved;
    await act(async () => {
      expect(result.current.handlePickVideoPendingClass("Road")).toBe(true);
      saved = await savePromise;
    });

    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        annotation_type: "video_mask",
        tool_unit_id: "region",
        class_name: "Road",
        geometry: expect.objectContaining({
          type: "video_mask",
          frame_index: 5,
        }),
      }),
      expect.anything(),
    );
    expect(create).toHaveBeenCalledTimes(1);
    expect(saved).toMatchObject({ annotation: { id: "mask-new", class_name: "Road" } });
  });

  it("Mask 轨迹工具显式创建 video_track_mask", async () => {
    const state = {
      pendingDrawing: null as import("../../state/useWorkbenchState").PendingDrawing,
      setPendingDrawing: vi.fn(
        (pending: import("../../state/useWorkbenchState").PendingDrawing) => {
          state.pendingDrawing = pending;
        },
      ),
      setSelectedId: vi.fn(),
    };
    const create = vi.fn(
      (
        payload: AnnotationPayload,
        options?: { onSuccess?: (created: AnnotationResponse) => void },
      ) => {
        options?.onSuccess?.({
          ...maskTrack,
          id: "mask-track-new",
          class_name: payload.class_name,
          geometry: payload.geometry,
        });
      },
    );
    const { result } = renderHook(() =>
      useVideoAnnotationActions({
        taskId: "task-1",
        queryClient: new QueryClient(),
        history: { push: vi.fn(), pushBatch: vi.fn() } as never,
        s: state as never,
        annotationsRef: { current: [] },
        pushToast: vi.fn(),
        recordRecentClass: vi.fn(),
        optimisticEnqueueCreate: vi.fn(),
        enqueueOnError: vi.fn(),
        activeToolHasOwnClasses: true,
        mutations: {
          create: { mutate: create },
          update: { mutate: vi.fn() },
          delete: { mutate: vi.fn() },
        },
      }),
    );

    const savePromise = result.current.handleVideoMaskCommit(rle, 5, null, "track");
    await act(async () => {
      result.current.handlePickVideoPendingClass("Road");
      await savePromise;
    });

    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        annotation_type: "video_track_mask",
        geometry: expect.objectContaining({
          type: "video_track_mask",
          keyframes: [expect.objectContaining({ frame_index: 5 })],
        }),
      }),
      expect.anything(),
    );
  });

  it("编辑已有单帧 Mask 使用 If-Match 更新 video_mask", async () => {
    const singleMask: AnnotationResponse = {
      ...maskTrack,
      id: "mask-frame-1",
      annotation_type: "video_mask",
      version: 7,
      geometry: { type: "video_mask", frame_index: 5, mask: reference },
    };
    apiMocks.updateAnnotation.mockResolvedValue({ ...singleMask, version: 8 });
    const { result, queryClient, history } = setup();
    queryClient.setQueryData(["annotations", "task-1"], [singleMask]);

    const saved = await result.current.handleVideoMaskCommit(rle, 5, singleMask);

    expect(apiMocks.updateAnnotation).toHaveBeenCalledWith(
      "task-1",
      "mask-frame-1",
      {
        geometry: {
          type: "video_mask",
          frame_index: 5,
          mask: reference,
        },
      },
      'W/"7"',
    );
    expect(saved?.annotation.version).toBe(8);
    expect(history.push).toHaveBeenCalledTimes(1);
  });

  it("取消新建 Mask 的类别选择会结束等待且不上传内容", async () => {
    const state = {
      pendingDrawing: null as import("../../state/useWorkbenchState").PendingDrawing,
      setPendingDrawing: vi.fn(
        (pending: import("../../state/useWorkbenchState").PendingDrawing) => {
          state.pendingDrawing = pending;
        },
      ),
    };
    const { result } = renderHook(() =>
      useVideoAnnotationActions({
        taskId: "task-1",
        queryClient: new QueryClient(),
        history: { push: vi.fn(), pushBatch: vi.fn() } as never,
        s: state as never,
        annotationsRef: { current: [] },
        pushToast: vi.fn(),
        recordRecentClass: vi.fn(),
        optimisticEnqueueCreate: vi.fn(),
        enqueueOnError: vi.fn(),
        activeToolHasOwnClasses: true,
        mutations: {
          create: { mutate: vi.fn() },
          update: { mutate: vi.fn() },
          delete: { mutate: vi.fn() },
        },
      }),
    );

    const savePromise = result.current.handleVideoMaskCommit(rle, 5, null);
    expect(state.pendingDrawing).toMatchObject({ kind: "video_mask" });

    let saved;
    await act(async () => {
      expect(result.current.handleCancelVideoMaskPendingClass()).toBe(true);
      saved = await savePromise;
    });

    expect(saved).toBeNull();
    expect(apiMocks.uploadTaskContent).not.toHaveBeenCalled();
  });
});

describe("视频非矩形工具选类", () => {
  it("单帧多边形完成后先打开类别候选，再按所选类别创建", () => {
    const create = vi.fn();
    const state = {
      activeClass: "Recommended",
      pendingDrawing: null as import("../../state/useWorkbenchState").PendingDrawing,
      setPendingDrawing: vi.fn(
        (pending: import("../../state/useWorkbenchState").PendingDrawing) => {
          state.pendingDrawing = pending;
        },
      ),
      setActiveClass: vi.fn(),
      setSelectedId: vi.fn(),
    };
    const { result } = renderHook(() =>
      useVideoAnnotationActions({
        taskId: "task-1",
        queryClient: new QueryClient(),
        history: { push: vi.fn(), pushBatch: vi.fn() } as never,
        s: state as never,
        annotationsRef: { current: [] },
        pushToast: vi.fn(),
        recordRecentClass: vi.fn(),
        optimisticEnqueueCreate: vi.fn(),
        enqueueOnError: vi.fn(),
        activeToolHasOwnClasses: true,
        mutations: {
          create: { mutate: create },
          update: { mutate: vi.fn() },
          delete: { mutate: vi.fn() },
        },
      }),
    );
    const points: [number, number][] = [
      [0.1, 0.1],
      [0.5, 0.1],
      [0.3, 0.6],
    ];

    act(() => result.current.handleVideoPointsCreate("video_polygon", 7, points));
    expect(create).not.toHaveBeenCalled();
    expect(state.pendingDrawing).toMatchObject({
      kind: "video_polygon",
      frameIndex: 7,
      points,
    });

    act(() => result.current.handlePickVideoPendingClass("Road"));
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        annotation_type: "video_polygon",
        tool_unit_id: "region",
        class_name: "Road",
      }),
      expect.anything(),
    );
  });
});
