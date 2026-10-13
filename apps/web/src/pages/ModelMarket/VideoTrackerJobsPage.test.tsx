import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

import type {
  VideoTrackerJobCounts,
  VideoTrackerJobListItem,
  VideoTrackerJobStatus,
} from "@/api/videoTrackerJobs";

const mockListVideoJobs = vi.fn();
const mockListProjects = vi.fn();
const mockBuildWorkbenchUrl = vi.fn(
  (..._args: unknown[]) => "/projects/project-video/annotate?task=task-1",
);

vi.mock("@/api/videoTrackerJobs", () => ({
  videoTrackerJobsApi: {
    list: (...args: unknown[]) => mockListVideoJobs(...args),
  },
}));

vi.mock("@/api/projects", () => ({
  projectsApi: {
    list: (...args: unknown[]) => mockListProjects(...args),
  },
}));

vi.mock("@/utils/workbenchNavigation", () => ({
  buildWorkbenchUrl: (...args: unknown[]) => mockBuildWorkbenchUrl(...args),
  currentWorkbenchReturnTo: () => "/ai-pre/jobs?tab=video",
}));

import { VideoTrackerJobsPanel } from "./VideoTrackerJobsPage";

const EMPTY_COUNTS: VideoTrackerJobCounts = {
  queued: 0,
  running: 0,
  completed: 0,
  failed: 0,
  cancelled: 0,
  pending_review: 0,
  partially_reviewed: 0,
  accepted: 0,
  discarded: 0,
};

function makeJob(status: VideoTrackerJobStatus, index: number): VideoTrackerJobListItem {
  return {
    id: `job-${index}`,
    task_id: `task-${index}`,
    project_id: "project-video",
    project_name: "城市交通追踪",
    project_display_id: "P-VIDEO",
    dataset_item_id: `item-${index}`,
    annotation_id: `track-${index}`,
    segment_id: null,
    created_by: "user-1",
    status,
    model_key: "sam3_video",
    direction: "forward",
    from_frame: index * 10,
    to_frame: index * 10 + 90,
    error_message: null,
    started_at: "2026-08-16T08:00:00Z",
    completed_at: status === "running" ? null : "2026-08-16T08:00:10Z",
    created_at: "2026-08-16T08:00:00Z",
  };
}

function response(items: VideoTrackerJobListItem[], nextCursor: string | null = null) {
  const counts = { ...EMPTY_COUNTS };
  items.forEach((item) => {
    counts[item.status] += 1;
  });
  return { items, next_cursor: nextCursor, counts };
}

function renderPanel(projectId?: string, initialPath = "/ai-pre/jobs?tab=video") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialPath]}>
        <LocationProbe />
        <VideoTrackerJobsPanel projectId={projectId} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function LocationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location-search">{location.search}</output>
      <button
        type="button"
        onClick={() =>
          navigate(
            "/ai-pre/jobs?tab=video&project_id=project-road&video_status=failed&video_model_key=new-model",
          )
        }
      >
        外部导航
      </button>
    </>
  );
}

describe("VideoTrackerJobsPanel", () => {
  beforeEach(() => {
    localStorage.removeItem("token");
    localStorage.removeItem("auth-storage");
    useAuthStore.setState({ token: null, user: null });
    mockListVideoJobs.mockReset();
    mockListProjects.mockReset();
    mockBuildWorkbenchUrl.mockClear();
    mockListProjects.mockResolvedValue([
      { id: "project-video", name: "城市交通追踪", display_id: "P-VIDEO" },
      { id: "project-road", name: "道路目标追踪", display_id: "P-ROAD" },
    ]);
    mockListVideoJobs.mockResolvedValue(response([]));
  });

  it("使用专用 API 同时展示执行态和候选审阅态", async () => {
    const items = [
      makeJob("running", 1),
      makeJob("pending_review", 2),
      makeJob("accepted", 3),
      makeJob("discarded", 4),
    ];
    mockListVideoJobs.mockResolvedValue(response(items));

    renderPanel();

    expect(await screen.findByText("视频追踪任务 (4)")).toBeInTheDocument();
    expect(screen.getAllByText(/运行中/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/待审阅/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/已采纳/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/已丢弃/).length).toBeGreaterThan(0);
    expect(mockListVideoJobs).toHaveBeenCalledWith(expect.objectContaining({ limit: 20 }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("项目和状态筛选传给视频追踪任务 API", async () => {
    renderPanel();
    await screen.findByText("暂无视频追踪任务");

    fireEvent.change(screen.getByLabelText("筛选视频项目"), {
      target: { value: "project-road" },
    });
    fireEvent.change(screen.getByLabelText("筛选视频任务状态"), {
      target: { value: "pending_review" },
    });
    fireEvent.change(screen.getByLabelText("筛选追踪模型"), {
      target: { value: "sam3_video" },
    });

    await waitFor(() => {
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({
          project_id: "project-road",
          status: "pending_review",
          model_key: "sam3_video",
          cursor: undefined,
        }),
        { signal: expect.any(AbortSignal) },
      );
    });
  });

  it("URL 项目过滤会初始化专用 API 查询", async () => {
    renderPanel("project-video");

    await waitFor(() => {
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({ project_id: "project-video" }),
        { signal: expect.any(AbortSignal) },
      );
    });
  });

  it("从 URL 恢复视频命名空间筛选并忽略图像 status", async () => {
    renderPanel(
      undefined,
      "/ai-pre/jobs?tab=video&project_id=project-video&status=failed&video_status=pending_review&video_model_key=sam3_video",
    );

    await screen.findByText("暂无视频追踪任务");
    expect(mockListVideoJobs).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: "project-video",
        status: "pending_review",
        model_key: "sam3_video",
      }),
      { signal: expect.any(AbortSignal) },
    );
    expect(screen.getByTestId("location-search")).toHaveTextContent("status=failed");
  });

  it("模型筛选保留输入空格并在防抖后应用", async () => {
    renderPanel();
    await screen.findByText("暂无视频追踪任务");
    mockListVideoJobs.mockClear();

    const input = screen.getByLabelText("筛选追踪模型") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "sam3 " } });
    expect(input).toHaveValue("sam3 ");
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("video_model_key=sam3");
    expect(mockListVideoJobs).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("video_model_key=sam3"),
    );
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({ model_key: "sam3", cursor: undefined }),
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("外部 URL 导航以新视频筛选原子替换草稿", async () => {
    renderPanel(
      undefined,
      "/ai-pre/jobs?tab=video&project_id=project-video&video_status=running&video_model_key=old-model",
    );
    await screen.findByText("暂无视频追踪任务");
    mockListVideoJobs.mockClear();

    fireEvent.change(screen.getByLabelText("筛选追踪模型"), {
      target: { value: "draft-model" },
    });
    fireEvent.click(screen.getByRole("button", { name: "外部导航" }));

    await waitFor(() => expect(screen.getByDisplayValue("new-model")).toBeInTheDocument());
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({
          project_id: "project-road",
          status: "failed",
          model_key: "new-model",
          cursor: undefined,
        }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    expect(
      mockListVideoJobs.mock.calls.some(
        ([params]) => (params as { model_key?: string }).model_key === "draft-model",
      ),
    ).toBe(false);
  });

  it("外部视频 URL 恢复期间再次输入仍能完成新的防抖提交", async () => {
    renderPanel(
      undefined,
      "/ai-pre/jobs?tab=video&project_id=project-video&video_status=running&video_model_key=old-model",
    );
    await screen.findByText("暂无视频追踪任务");
    mockListVideoJobs.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "外部导航" }));
    await waitFor(() => expect(screen.getByDisplayValue("new-model")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("筛选追踪模型"), {
      target: { value: "new-model-next" },
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent(
        "video_model_key=new-model-next",
      ),
    );
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({
          project_id: "project-road",
          status: "failed",
          model_key: "new-model-next",
        }),
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("清除视频模型筛选不会被旧 debounce 恢复", async () => {
    renderPanel(undefined, "/ai-pre/jobs?tab=video&video_model_key=old-model");
    await screen.findByText("暂无视频追踪任务");
    mockListVideoJobs.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).not.toHaveTextContent("video_model_key="),
    );
    await waitFor(() => expect(mockListVideoJobs).toHaveBeenCalledTimes(1));
    expect(mockListVideoJobs).toHaveBeenCalledWith(
      expect.objectContaining({ model_key: undefined, cursor: undefined }),
      { signal: expect.any(AbortSignal) },
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("video_model_key=");
    expect(mockListVideoJobs).toHaveBeenCalledTimes(1);
  });

  it("视频任务查询按同一账号的 token epoch 分隔缓存", async () => {
    const user = { id: "video-u1", role: "employee" } as MeResponse;
    useAuthStore.getState().setAuth("video-token-1", user);
    renderPanel();
    await screen.findByText("暂无视频追踪任务");
    mockListVideoJobs.mockClear();

    act(() => useAuthStore.getState().setAuth("video-token-2", user));
    await waitFor(() => expect(mockListVideoJobs).toHaveBeenCalledTimes(1));
    expect(mockListVideoJobs).toHaveBeenCalledWith(expect.objectContaining({ cursor: undefined }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("视频筛选原子清空 cursor 历史并写回独立 URL 键", async () => {
    mockListVideoJobs.mockResolvedValue(response([makeJob("running", 1)], "cursor-1"));
    renderPanel();
    await screen.findByText("视频追踪任务 (1)");

    fireEvent.click(screen.getByRole("button", { name: /下一页/ }));
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({ cursor: "cursor-1" }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    fireEvent.change(screen.getByLabelText("筛选视频任务状态"), {
      target: { value: "pending_review" },
    });
    expect(screen.getByTestId("location-search")).toHaveTextContent("video_status=pending_review");
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({
          status: "pending_review",
          cursor: undefined,
        }),
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("切换同一账号 token epoch 时不会复用已有 cursor", async () => {
    const user = { id: "video-u1", role: "employee" } as MeResponse;
    useAuthStore.getState().setAuth("video-token-1", user);
    mockListVideoJobs
      .mockResolvedValueOnce(response([makeJob("running", 1)], "cursor-1"))
      .mockResolvedValue(response([]));
    renderPanel();
    await screen.findByText("视频追踪任务 (1)");

    fireEvent.click(screen.getByRole("button", { name: /下一页/ }));
    await waitFor(() =>
      expect(mockListVideoJobs).toHaveBeenCalledWith(
        expect.objectContaining({ cursor: "cursor-1" }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    mockListVideoJobs.mockClear();

    act(() => useAuthStore.getState().setAuth("video-token-2", user));
    await waitFor(() => expect(mockListVideoJobs).toHaveBeenCalledTimes(1));
    expect(mockListVideoJobs).toHaveBeenCalledWith(expect.objectContaining({ cursor: undefined }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("逐任务返回对应视频工作台并保留返回地址", async () => {
    mockListVideoJobs.mockResolvedValue(response([makeJob("pending_review", 2)]));
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: /返回视频工作台/ }));
    expect(mockBuildWorkbenchUrl).toHaveBeenCalledWith(
      "project-video",
      expect.objectContaining({
        taskId: "task-2",
        trackId: "track-2",
        frameIndex: 20,
        returnTo: "/ai-pre/jobs?tab=video",
      }),
    );
  });
});
