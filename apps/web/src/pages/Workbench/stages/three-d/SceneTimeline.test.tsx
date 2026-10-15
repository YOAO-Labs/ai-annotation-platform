import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ComponentProps } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SceneTimelineFrameSummary, SceneTimelineResponse } from "@/api/generated";

const useSceneTimelineMock = vi.hoisted(() => vi.fn());
const getSceneTimelineMock = vi.hoisted(() => vi.fn());
const getPointCloudManifestMock = vi.hoisted(() => vi.fn());
const prefetchPointCloudFrameAssetsMock = vi.hoisted(() => vi.fn());
const isPointCloudFramePrefetchReadyMock = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useSceneTimeline", () => ({
  useSceneTimeline: useSceneTimelineMock,
  SCENE_TIMELINE_QUERY_KEY: "scene-timeline",
}));

vi.mock("@/api/tasks", () => ({
  tasksApi: {
    getPointCloudManifest: getPointCloudManifestMock,
    getSceneTimeline: getSceneTimelineMock,
  },
}));

vi.mock("./pointCloudAssetCache", () => ({
  prefetchPointCloudFrameAssets: prefetchPointCloudFrameAssetsMock,
  isPointCloudFramePrefetchReady: isPointCloudFramePrefetchReadyMock,
}));

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 3) }, (_, index) => ({
        index,
        key: index,
        start: index * 40,
        end: (index + 1) * 40,
        size: 40,
        lane: 0,
      })),
    getTotalSize: () => count * 40,
    scrollToIndex: vi.fn(),
    scrollToOffset: vi.fn(),
    measure: vi.fn(),
  }),
}));

import { SceneTimeline } from "./SceneTimeline";

function renderTimeline(props: ComponentProps<typeof SceneTimeline>, expand = true) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <SceneTimeline {...props} />
    </QueryClientProvider>,
  );
  if (expand && screen.queryByTestId("scene-timeline-toggle"))
    fireEvent.click(screen.getByTestId("scene-timeline-toggle"));
  return rendered;
}

function timelineData(): SceneTimelineResponse & { frames: SceneTimelineFrameSummary[] } {
  return {
    summary_version: 1 as const,
    scene_id: "scene-1",
    scene_name: "nuScenes mini scene-0061",
    current_frame_index: 0,
    scene_start_frame: 0,
    scene_end_frame: 9999,
    populated_frame_count: 2,
    window_start_frame: 0,
    window_end_frame: 2,
    frames: [
      {
        frame_index: 0,
        state: "available" as const,
        task_id: "task-0",
        task_status: "in_progress",
        annotation_count: 2,
        selected_track: {
          annotation_id: "ann-0",
          source: "manual",
          class_name: "car",
          temporal_role: "keyframe",
        },
        selected_track_present: true,
      },
      {
        frame_index: 1,
        state: "available" as const,
        task_id: "task-1",
        task_status: "in_progress",
        annotation_count: 1,
        selected_track: {
          annotation_id: "ann-1",
          source: "manual",
          class_name: "car",
          temporal_role: "derived",
        },
        selected_track_present: true,
      },
      {
        frame_index: 2,
        state: "missing" as const,
        task_id: null,
        task_status: null,
        annotation_count: 0,
        selected_track: null,
        selected_track_present: false,
      },
    ],
  };
}

describe("SceneTimeline", () => {
  beforeEach(() => {
    getSceneTimelineMock.mockReset().mockResolvedValue(timelineData());
    getPointCloudManifestMock.mockReset().mockResolvedValue({
      task_id: "task-1",
      point_cloud_url: "https://assets.test/frame-1.pcd",
      cameras: [],
      expires_in: 900,
    });
    prefetchPointCloudFrameAssetsMock.mockReset().mockResolvedValue(undefined);
    isPointCloudFramePrefetchReadyMock.mockReset().mockReturnValue(false);
    useSceneTimelineMock.mockReturnValue({
      data: timelineData(),
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
  });

  it("renders only virtual cells for a 10000-frame scene and exposes track presence", () => {
    renderTimeline({ taskId: "task-0", trackId: "trk_car", onNavigateFrame: vi.fn() });

    expect(screen.getByTestId("three-d-scene-timeline")).toBeTruthy();
    expect(screen.getByText("nuScenes mini scene-0061")).toBeTruthy();
    expect(screen.getAllByTestId(/^scene-timeline-frame-/)).toHaveLength(3);
    expect(screen.getByTestId("scene-timeline-track-frame-0")).toBeTruthy();
    expect(screen.getByTestId("scene-timeline-frame-0").getAttribute("aria-current")).toBe("step");
    expect(screen.getByTestId("scene-timeline-frame-2")).toBeDisabled();
  });

  it("shows quality severity on frames and opens the quality panel", () => {
    const openQuality = vi.fn();
    renderTimeline({
      taskId: "task-0",
      trackId: null,
      onNavigateFrame: vi.fn(),
      qualityMarkers: { 1: "blocker" },
      qualityIssueCount: 3,
      onOpenQuality: openQuality,
    });

    expect(screen.getByTestId("scene-timeline-quality-1")).toBeTruthy();
    fireEvent.click(screen.getByTestId("scene-quality-open"));
    expect(openQuality).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "3D 质检 · 3" })).toBeTruthy();
  });

  it("navigates to an available frame with the matching annotation", async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: "trk_car", onNavigateFrame: navigate });

    fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-1", "ann-1"));
  });

  it("starts a decoded target after 120ms while retaining the click-settle window", async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate });
    await waitFor(() => expect(prefetchPointCloudFrameAssetsMock).toHaveBeenCalled());
    isPointCloudFramePrefetchReadyMock.mockReturnValue(true);
    vi.useFakeTimers();

    try {
      fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
      expect(isPointCloudFramePrefetchReadyMock).toHaveBeenCalledWith(
        expect.objectContaining({ point_cloud_url: "https://assets.test/frame-1.pcd" }),
        { depthRasters: false },
      );
      await act(async () => vi.advanceTimersByTimeAsync(119));
      expect(navigate).not.toHaveBeenCalled();
      await act(async () => vi.advanceTimersByTimeAsync(1));
      expect(navigate).toHaveBeenCalledWith("task-1", "ann-1");
    } finally {
      vi.useRealTimers();
    }
  });

  it("coalesces 100ms frame clicks and navigates only to the latest intent", async () => {
    const data = timelineData();
    data.frames[2] = {
      frame_index: 2,
      state: "available",
      task_id: "task-2",
      task_status: "in_progress",
      annotation_count: 0,
      selected_track: null,
    };
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    getPointCloudManifestMock.mockImplementation(async (requestedTaskId: string) => ({
      task_id: requestedTaskId,
      point_cloud_url: `https://assets.test/${requestedTaskId}.pcd`,
      cameras: [],
      expires_in: 900,
    }));
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate });
    await waitFor(() => expect(prefetchPointCloudFrameAssetsMock).toHaveBeenCalled());
    getPointCloudManifestMock.mockClear();
    prefetchPointCloudFrameAssetsMock.mockClear();
    vi.useFakeTimers();

    try {
      for (let index = 0; index < 10; index += 1) {
        fireEvent.click(screen.getByTestId(`scene-timeline-frame-${index % 2 === 0 ? 1 : 2}`));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(100);
        });
      }
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });

      expect(navigate).toHaveBeenCalledTimes(1);
      expect(navigate).toHaveBeenLastCalledWith("task-2", null);
      expect(
        prefetchPointCloudFrameAssetsMock.mock.calls.map(([manifest]) => manifest.task_id),
      ).toEqual(["task-1", "task-2"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("切到其他任务后不会执行旧时间轴定时导航", async () => {
    vi.useFakeTimers();
    const navigate = vi.fn().mockResolvedValue(true);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const rendered = render(
      <QueryClientProvider client={queryClient}>
        <SceneTimeline taskId="task-0" trackId={null} onNavigateFrame={navigate} />
      </QueryClientProvider>,
    );

    try {
      fireEvent.click(screen.getByTestId("scene-timeline-toggle"));
      fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
      rendered.rerender(
        <QueryClientProvider client={queryClient}>
          <SceneTimeline taskId="task-external" trackId={null} onNavigateFrame={navigate} />
        </QueryClientProvider>,
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });

      expect(navigate).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("restarts the settled target prefetch after repeated clicks on the same frame", async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate });
    await waitFor(() => expect(prefetchPointCloudFrameAssetsMock).toHaveBeenCalled());
    prefetchPointCloudFrameAssetsMock.mockClear();
    vi.useFakeTimers();

    try {
      fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(200);
      });

      expect(navigate).toHaveBeenCalledTimes(1);
      expect(
        prefetchPointCloudFrameAssetsMock.mock.calls.map(([manifest]) => manifest.task_id),
      ).toEqual(["task-1", "task-1"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not block task navigation on a slow target frame prefetch", async () => {
    let finishTargetPrefetch!: () => void;
    const targetPrefetch = new Promise<void>((resolve) => {
      finishTargetPrefetch = resolve;
    });
    prefetchPointCloudFrameAssetsMock.mockImplementation(async (manifest: { task_id: string }) => {
      if (manifest.task_id === "task-1") await targetPrefetch;
    });
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate });
    await waitFor(() =>
      expect(prefetchPointCloudFrameAssetsMock).toHaveBeenCalledWith(
        expect.objectContaining({ task_id: "task-1" }),
        { depthRasters: false },
      ),
    );

    fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-1", "ann-1"));

    finishTargetPrefetch();
  });

  it("waits for the selected frame prefetch before warming the following frame", async () => {
    let finishNavigation!: (allowed: boolean) => void;
    let finishTargetPrefetch!: () => void;
    const navigate = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          finishNavigation = resolve;
        }),
    );
    const data = timelineData();
    data.frames[2] = {
      frame_index: 2,
      state: "available",
      task_id: "task-2",
      task_status: "in_progress",
      annotation_count: 0,
      selected_track: null,
    };
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    getPointCloudManifestMock.mockImplementation(async (requestedTaskId: string) => ({
      task_id: requestedTaskId,
      point_cloud_url: `https://assets.test/${requestedTaskId}.pcd`,
      cameras: [],
      expires_in: 900,
    }));
    prefetchPointCloudFrameAssetsMock.mockImplementation((manifest: { task_id: string }) =>
      manifest.task_id === "task-1"
        ? new Promise<void>((resolve) => {
            finishTargetPrefetch = resolve;
          })
        : Promise.resolve(),
    );
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate });

    fireEvent.click(screen.getByTestId("scene-timeline-frame-1"));
    await waitFor(() => expect(finishTargetPrefetch).toBeTypeOf("function"));
    expect(getPointCloudManifestMock.mock.calls.some(([id]) => id === "task-2")).toBe(false);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-1", "ann-1"));

    finishNavigation(true);
    expect(getPointCloudManifestMock.mock.calls.some(([id]) => id === "task-2")).toBe(false);
    finishTargetPrefetch();

    await waitFor(() =>
      expect(getPointCloudManifestMock).toHaveBeenCalledWith(
        "task-2",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });

  it("collapses to the compact header and expands again", () => {
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: vi.fn() });

    const toggle = screen.getByTestId("scene-timeline-toggle");
    fireEvent.click(toggle);
    expect(screen.queryByTestId("scene-timeline-virtual-canvas")).toBeNull();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    expect(screen.getByTestId("scene-timeline-virtual-canvas")).toBeTruthy();
  });

  it("stays hidden for tasks without a multi-frame scene", () => {
    useSceneTimelineMock.mockReturnValue({
      data: { scene_id: null, frames: [] },
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    renderTimeline({ taskId: "task-single", trackId: null, onNavigateFrame: vi.fn() });
    expect(screen.queryByTestId("three-d-scene-timeline")).toBeNull();
  });

  it.each([
    [false, false],
    [true, true],
  ])("only requests adjacent depth rasters for real WebGPU (%s)", async (enabled, expected) => {
    renderTimeline({
      taskId: "task-0",
      trackId: null,
      prefetchDepthRasters: enabled,
      onNavigateFrame: vi.fn(),
    });

    await waitFor(() =>
      expect(prefetchPointCloudFrameAssetsMock).toHaveBeenCalledWith(
        expect.objectContaining({ task_id: "task-1" }),
        { depthRasters: expected },
      ),
    );
  });

  it("automatically prefetches only the next frame", async () => {
    const data = timelineData();
    data.current_frame_index = 1;
    data.frames[2] = {
      frame_index: 2,
      state: "available",
      task_id: "task-2",
      task_status: "in_progress",
      annotation_count: 0,
      selected_track: null,
    };
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    getPointCloudManifestMock.mockImplementation(async (requestedTaskId: string) => ({
      task_id: requestedTaskId,
      point_cloud_url: `https://assets.test/${requestedTaskId}.pcd`,
      cameras: [],
      expires_in: 900,
    }));

    renderTimeline({ taskId: "task-1", trackId: null, onNavigateFrame: vi.fn() });

    await waitFor(() =>
      expect(prefetchPointCloudFrameAssetsMock).toHaveBeenNthCalledWith(
        1,
        expect.objectContaining({ task_id: "task-2" }),
        { depthRasters: false },
      ),
    );
    expect(getPointCloudManifestMock.mock.calls.some(([id]) => id === "task-0")).toBe(false);
  });
  it("starts compact and commits a dragged seek only on release", async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate }, false);
    expect(screen.queryByTestId("scene-timeline-virtual-canvas")).toBeNull();
    const slider = screen.getByRole("slider", { name: "Scene 全段位置" });
    fireEvent.pointerDown(slider);
    fireEvent.change(slider, { target: { value: "1" } });
    expect(slider).toHaveAttribute("aria-valuetext", "帧 1，共 10000 帧");
    expect(navigate).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-1", "ann-1"));
  });

  it("uses actual nonzero frame numbers and queries unloaded exact targets", async () => {
    const data = timelineData();
    Object.assign(data, {
      scene_start_frame: 100,
      scene_end_frame: 138,
      current_frame_index: 105,
      frames: [],
    });
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    getSceneTimelineMock.mockResolvedValue({
      ...data,
      frames: [{ ...timelineData().frames[1], frame_index: 130, task_id: "task-130" }],
    });
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-105", trackId: null, onNavigateFrame: navigate }, false);
    expect(screen.getByText("· 6/39")).toBeTruthy();
    const input = screen.getByRole("textbox", { name: "Scene 帧号" });
    fireEvent.change(input, { target: { value: "99" } });
    fireEvent.submit(input.closest("form")!);
    expect(screen.getByRole("status")).toHaveTextContent("请输入 100–138 范围内的整数帧号");
    expect(getSceneTimelineMock).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "130" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-130", "ann-1"));
    expect(getSceneTimelineMock).toHaveBeenCalledWith(
      "task-105",
      130,
      130,
      null,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("skips inaccessible frames in bounded chunks without fetching their point clouds", async () => {
    const data = timelineData();
    Object.assign(data, { scene_end_frame: 401, frames: [data.frames[0]] });
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    getSceneTimelineMock.mockImplementation(async (_task: string, start: number, end: number) => ({
      ...data,
      frames: Array.from({ length: end - start + 1 }, (_, offset) => ({
        ...timelineData().frames[2],
        frame_index: start + offset,
        state: start + offset === 201 ? "available" : "unavailable",
        task_id: start + offset === 201 ? "task-201" : null,
      })),
    }));
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate }, false);
    fireEvent.click(screen.getByRole("button", { name: "下一帧" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("task-201", null));
    expect(getSceneTimelineMock.mock.calls.map((call) => [call[1], call[2]])).toEqual([
      [1, 200],
      [201, 400],
    ]);
    expect(screen.getByRole("status")).toHaveTextContent("已跳过 200 个缺失或不可访问帧");
    expect(getPointCloudManifestMock).toHaveBeenCalledTimes(1);
    expect(getPointCloudManifestMock).toHaveBeenCalledWith(
      "task-201",
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it("leaves an exact unavailable target in place and ignores input arrow shortcuts", async () => {
    const navigate = vi.fn().mockResolvedValue(true);
    renderTimeline({ taskId: "task-0", trackId: null, onNavigateFrame: navigate }, false);
    const input = screen.getByRole("textbox", { name: "Scene 帧号" });
    fireEvent.keyDown(input, { key: "ArrowRight" });
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("帧 2 缺失或不可访问"),
    );
    expect(navigate).not.toHaveBeenCalled();
  });
  it("disables a boundary button when all remaining summaries are inaccessible", () => {
    const data = timelineData();
    data.current_frame_index = 1;
    data.scene_end_frame = 2;
    data.frames[0] = { ...data.frames[0], state: "unavailable", task_id: null };
    useSceneTimelineMock.mockReturnValue({
      data,
      isError: false,
      isFetching: false,
      refetch: vi.fn(),
    });
    renderTimeline({ taskId: "task-1", trackId: null, onNavigateFrame: vi.fn() }, false);
    expect(screen.getByRole("button", { name: "上一帧" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一帧" })).toBeDisabled();
  });

  it("cancels queued hover prefetch before starting playback", async () => {
    const onActiveChange = vi.fn();
    renderTimeline({
      taskId: "task-0",
      trackId: null,
      onNavigateFrame: vi.fn(),
      frameState: { taskId: "task-0", status: "ready" },
      onPlaybackActiveChange: onActiveChange,
    });
    await waitFor(() => expect(getPointCloudManifestMock).toHaveBeenCalled());
    getPointCloudManifestMock.mockClear();
    fireEvent.pointerEnter(screen.getByTestId("scene-timeline-frame-1"));
    fireEvent.click(screen.getByTestId("scene-timeline-play"));
    expect(onActiveChange).toHaveBeenCalledWith(true);
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 180));
    });
    expect(getPointCloudManifestMock).not.toHaveBeenCalled();
  });
});
