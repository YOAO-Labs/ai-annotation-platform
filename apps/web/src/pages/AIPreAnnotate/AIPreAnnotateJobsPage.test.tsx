/**
 * v0.10.45 · AIPreAnnotateJobsPage 单测
 * 覆盖: 渲染 / 加载态 / 空态 / 有数据 / tab 切换
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { MeResponse } from "@/api/auth";
import { useAuthStore } from "@/stores/authStore";

// ── mock asyncJobsApi ────────────────────────────────────────────────────────
const mockAsyncJobsList = vi.fn();
const mockAsyncJobsCancel = vi.fn();
const mockAsyncJobsGet = vi.fn();
const mockAsyncJobsRetryFailed = vi.fn();
const mockBuildWorkbenchUrl = vi.fn((projectId: string) => `/workbench/${projectId}`);
vi.mock("@/api/asyncJobs", () => ({
  asyncJobsApi: {
    list: (...args: unknown[]) => mockAsyncJobsList(...args),
    cancel: (...args: unknown[]) => mockAsyncJobsCancel(...args),
    get: (...args: unknown[]) => mockAsyncJobsGet(...args),
    retryFailed: (...args: unknown[]) => mockAsyncJobsRetryFailed(...args),
  },
}));

// ── mock VideoTrackerJobsPanel (it has its own useQuery) ────────────────────
vi.mock("@/pages/ModelMarket/VideoTrackerJobsPage", () => ({
  VideoTrackerJobsPanel: ({ projectId }: { projectId?: string }) => (
    <div data-testid="video-tracker-panel">video-panel{projectId ? `-${projectId}` : ""}</div>
  ),
}));

// ── mock workbench navigation utils ─────────────────────────────────────────
vi.mock("@/utils/workbenchNavigation", () => ({
  buildWorkbenchUrl: (...args: unknown[]) => mockBuildWorkbenchUrl(...(args as [string])),
  currentWorkbenchReturnTo: () => "/ai-pre/jobs",
}));

import AIPreAnnotateJobsPage from "./AIPreAnnotateJobsPage";

function makeJob(overrides: Record<string, unknown> = {}) {
  return {
    id: "job-1",
    kind: "batch_predict",
    project_id: "p1",
    user_id: "u1",
    project_display_id: "P-1",
    project_name: "Demo Project",
    status: "completed",
    progress_pct: 100,
    payload: {
      batch_id: "batch-abc",
      batch_display_id: "B-1",
      prompt: "car, person",
      output_mode: "det",
      total_tasks: 10,
    },
    result: { failed_count: 0, duration_ms: 5000 },
    error_message: null,
    celery_task_id: "celery-1",
    started_at: "2026-05-01T10:00:00Z",
    completed_at: "2026-05-01T10:00:05Z",
    created_at: "2026-05-01T10:00:00Z",
    updated_at: "2026-05-01T10:00:05Z",
    ...overrides,
  };
}

function renderUI(initialPath = "/ai-pre/jobs") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialPath]}>
        <LocationProbe />
        <AIPreAnnotateJobsPage />
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
          navigate("/ai-pre/jobs?tab=image&project_id=p8&status=pending&q=server&page=1")
        }
      >
        外部导航
      </button>
    </>
  );
}

describe("AIPreAnnotateJobsPage", () => {
  beforeEach(() => {
    localStorage.removeItem("token");
    localStorage.removeItem("auth-storage");
    useAuthStore.setState({ token: null, user: null });
    mockAsyncJobsList.mockReset();
    mockAsyncJobsCancel.mockReset();
    mockAsyncJobsGet.mockReset();
    mockAsyncJobsRetryFailed.mockReset();
    mockBuildWorkbenchUrl.mockClear();
    // 默认: 返回空列表
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 0 });
    mockAsyncJobsCancel.mockResolvedValue({ status: "cancel_requested", id: "job-1" });
    mockAsyncJobsGet.mockResolvedValue(makeJob());
    mockAsyncJobsRetryFailed.mockResolvedValue({
      status: "queued",
      job_id: "job-1",
      queued: 1,
      skipped: 0,
    });
  });

  it("渲染页面标题与两个 tab", () => {
    renderUI();
    expect(screen.getByText("AI 任务历史")).toBeInTheDocument();
    expect(screen.getByText("图像")).toBeInTheDocument();
    expect(screen.getByText("视频")).toBeInTheDocument();
  });

  it("加载态 → 显示加载中文本", () => {
    // useQuery isLoading=true: 让 mockAsyncJobsList 挂起不 resolve
    mockAsyncJobsList.mockImplementation(() => new Promise(() => {}));
    renderUI();
    expect(screen.getByText("加载中…")).toBeInTheDocument();
  });

  it("空态 → 显示暂无历史提示", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 0 });
    renderUI();
    // 等待 query 完成
    await screen.findByText("暂无 prediction job 历史");
    expect(screen.getByText("历史 job (0)")).toBeInTheDocument();
  });

  it("有数据 → 渲染 job 行中的项目名与状态", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [makeJob()], total: 1 });
    renderUI();
    await screen.findByText("Demo Project");
    expect(screen.getByText("历史 job (1)")).toBeInTheDocument();
    expect(screen.getAllByText("已完成").length).toBeGreaterThan(0);
    expect(mockAsyncJobsList).toHaveBeenCalledWith(
      expect.objectContaining({ kind: ["batch_predict", "prediction_retry"] }),
      { signal: expect.any(AbortSignal) },
    );
  });

  it("图像 tab 渲染 prediction_retry job", async () => {
    mockAsyncJobsList.mockResolvedValue({
      items: [
        makeJob({
          kind: "prediction_retry",
          status: "failed",
          payload: {
            failed_prediction_id: "fp-12345678",
            task_display_id: "TASK-9",
            error_type: "TIMEOUT",
            ml_backend_name: "bk",
          },
          result: { failed_count: 1, duration_ms: 1200 },
          error_message: "timeout",
        }),
      ],
      total: 1,
    });
    renderUI();
    await screen.findByText("TASK-9");
    expect(screen.getByText("TIMEOUT")).toBeInTheDocument();
    expect(screen.getByText("retry")).toBeInTheDocument();
  });

  it("prediction_retry 没有批次但记录了任务时可进入工作台", async () => {
    mockAsyncJobsList.mockResolvedValue({
      items: [
        makeJob({
          kind: "prediction_retry",
          payload: {
            task_id: "task-9",
            task_display_id: "TASK-9",
            failed_prediction_id: "fp-12345678",
          },
        }),
      ],
      total: 1,
    });
    renderUI();

    const workbenchButton = await screen.findByTitle("去工作台");
    expect(workbenchButton).toBeEnabled();
    fireEvent.click(workbenchButton);
    expect(mockBuildWorkbenchUrl).toHaveBeenCalledWith(
      "p1",
      expect.objectContaining({ batchId: null, taskId: "task-9" }),
    );
  });

  it("点击「视频」tab → 渲染 VideoTrackerPanel", async () => {
    renderUI();
    fireEvent.click(screen.getByText("视频"));
    expect(await screen.findByTestId("video-tracker-panel")).toBeInTheDocument();
  });

  it("状态过滤 select → 传参给 asyncJobsApi.list", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 0 });
    renderUI();
    await screen.findByText("暂无 prediction job 历史");
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "failed" } });
    // 变更后重新调用 list
    expect(mockAsyncJobsList).toHaveBeenCalledWith(expect.objectContaining({ status: "failed" }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("从 URL 恢复图像项目、状态、q 和 one-based page", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 41 });
    renderUI("/ai-pre/jobs?tab=image&project_id=p9&status=failed&q=%20car%20&page=2");

    await screen.findByText("暂无 prediction job 历史");
    expect(screen.getByDisplayValue("car")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("q=car"));
    expect(mockAsyncJobsList).toHaveBeenCalledWith(
      expect.objectContaining({
        project_id: "p9",
        status: "failed",
        search: "car",
        offset: 20,
      }),
      { signal: expect.any(AbortSignal) },
    );
  });

  it("图像关键字与第一页在防抖后原子写回可恢复 URL", async () => {
    renderUI("/ai-pre/jobs?tab=image&q=old&page=3");
    await screen.findByText("暂无 prediction job 历史");

    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "  bus  " },
    });
    expect(screen.getByTestId("location-search")).toHaveTextContent("q=old");
    expect(screen.getByTestId("location-search")).toHaveTextContent("page=3");
    await waitFor(() => expect(screen.getByTestId("location-search")).toHaveTextContent("q=bus"));
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).not.toHaveTextContent("page="),
    );

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "failed" } });
    expect(screen.getByTestId("location-search")).toHaveTextContent("status=failed");
  });

  it("图像关键字从第二页防抖后与页码原子切换", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 41 });
    renderUI("/ai-pre/jobs?tab=image&page=2");
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "bus" },
    });
    expect(screen.getByTestId("location-search")).not.toHaveTextContent("q=bus");
    expect(screen.getByTestId("location-search")).toHaveTextContent("page=2");
    expect(mockAsyncJobsList).not.toHaveBeenCalled();

    await waitFor(() =>
      expect(mockAsyncJobsList).toHaveBeenCalledWith(
        expect.objectContaining({ search: "bus", offset: 0 }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    expect(
      mockAsyncJobsList.mock.calls.some(
        ([params]) => (params as { offset?: number }).offset === 20,
      ),
    ).toBe(false);
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).not.toHaveTextContent("page="),
    );
  });

  it("图像未应用草稿在重新加载时仍恢复已应用页码和关键字", async () => {
    const rendered = renderUI("/ai-pre/jobs?tab=image&q=old&page=3");
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "bus" },
    });
    const reloadSearch = screen.getByTestId("location-search").textContent ?? "";
    expect(reloadSearch).toContain("q=old");
    expect(reloadSearch).toContain("page=3");

    rendered.unmount();
    mockAsyncJobsList.mockClear();
    renderUI(`/ai-pre/jobs${reloadSearch}`);
    await screen.findByText("暂无 prediction job 历史");
    expect(screen.getByDisplayValue("old")).toBeInTheDocument();
    expect(mockAsyncJobsList).toHaveBeenCalledWith(
      expect.objectContaining({ search: "old", offset: 40 }),
      { signal: expect.any(AbortSignal) },
    );
  });

  it("外部 URL 导航以新图像筛选原子替换草稿", async () => {
    renderUI("/ai-pre/jobs?tab=image&project_id=p1&status=failed&q=old&page=2");
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "draft" },
    });
    fireEvent.click(screen.getByRole("button", { name: "外部导航" }));

    await waitFor(() => expect(screen.getByDisplayValue("server")).toBeInTheDocument());
    await waitFor(() =>
      expect(mockAsyncJobsList).toHaveBeenCalledWith(
        expect.objectContaining({
          project_id: "p8",
          status: "pending",
          search: "server",
          offset: 0,
        }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    expect(
      mockAsyncJobsList.mock.calls.some(
        ([params]) => (params as { search?: string }).search === "draft",
      ),
    ).toBe(false);
  });

  it("外部图像 URL 恢复期间再次输入仍能完成新的防抖提交", async () => {
    renderUI("/ai-pre/jobs?tab=image&q=old");
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    fireEvent.click(screen.getByRole("button", { name: "外部导航" }));
    await waitFor(() => expect(screen.getByDisplayValue("server")).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "server-next" },
    });

    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("q=server-next"),
    );
    await waitFor(() =>
      expect(mockAsyncJobsList).toHaveBeenCalledWith(
        expect.objectContaining({ project_id: "p8", status: "pending", search: "server-next" }),
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("图像任务查询按同一账号的 token epoch 分隔缓存", async () => {
    const user = { id: "image-u1", role: "employee" } as MeResponse;
    useAuthStore.getState().setAuth("image-token-1", user);
    renderUI();
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    act(() => useAuthStore.getState().setAuth("image-token-2", user));
    await waitFor(() => expect(mockAsyncJobsList).toHaveBeenCalledTimes(1));
    expect(mockAsyncJobsList).toHaveBeenCalledWith(expect.objectContaining({ offset: 0 }), {
      signal: expect.any(AbortSignal),
    });
  });

  it("关键字防抖期间切换状态先使用已应用关键字", async () => {
    mockAsyncJobsList.mockResolvedValue({ items: [], total: 0 });
    renderUI("/ai-pre/jobs?tab=image&page=3&q=old");
    await screen.findByText("暂无 prediction job 历史");
    mockAsyncJobsList.mockClear();

    fireEvent.change(screen.getByPlaceholderText("搜索 prompt..."), {
      target: { value: "new" },
    });
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "failed" } });

    await waitFor(() =>
      expect(mockAsyncJobsList).toHaveBeenCalledWith(
        expect.objectContaining({ search: "old", status: "failed", offset: 0 }),
        { signal: expect.any(AbortSignal) },
      ),
    );
    await waitFor(() =>
      expect(mockAsyncJobsList).toHaveBeenCalledWith(
        expect.objectContaining({ search: "new", status: "failed", offset: 0 }),
        { signal: expect.any(AbortSignal) },
      ),
    );
  });

  it("job 有 failed_count > 0 时展示 danger badge", async () => {
    mockAsyncJobsList.mockResolvedValue({
      items: [makeJob({ result: { failed_count: 3, duration_ms: 1000 } })],
      total: 1,
    });
    renderUI();
    await screen.findByText("3");
    // Badge variant=danger 渲染了 '3'
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("running batch_predict job 可触发取消", async () => {
    mockAsyncJobsList.mockResolvedValue({
      items: [makeJob({ status: "running", progress_pct: 30 })],
      total: 1,
    });
    renderUI();
    const cancelButton = await screen.findByTitle("取消 job");
    fireEvent.click(cancelButton);
    await waitFor(() => {
      expect(mockAsyncJobsCancel).toHaveBeenCalledWith("job-1");
    });
  });

  it("点击详情按钮打开该次后台任务详情并展示结果", async () => {
    const job = makeJob({
      result: {
        success_count: 7,
        failed_count: 1,
        total_cost: "0.1234",
        duration_ms: 1200,
        failed_prediction_ids: ["fp-1"],
      },
    });
    mockAsyncJobsList.mockResolvedValue({ items: [job], total: 1 });
    mockAsyncJobsGet.mockResolvedValue(job);
    renderUI();

    const detailButton = await screen.findByTitle("详情");
    fireEvent.click(detailButton);

    expect(await screen.findByText("后台任务详情")).toBeInTheDocument();
    expect(mockAsyncJobsGet).toHaveBeenCalledWith("job-1");
    expect(await screen.findByText("$0.1234")).toBeInTheDocument();
    expect(screen.getAllByText("重试失败项").length).toBeGreaterThan(0);
  });

  it("详情里可重试记录了 failed_prediction_ids 的 batch_predict 失败项", async () => {
    const job = makeJob({
      result: {
        failed_count: 1,
        failed_prediction_ids: ["fp-1"],
      },
    });
    mockAsyncJobsList.mockResolvedValue({ items: [job], total: 1 });
    mockAsyncJobsGet.mockResolvedValue(job);
    renderUI();

    fireEvent.click(await screen.findByTitle("详情"));
    const retryButton = await screen.findByRole("button", { name: /重试失败项/ });
    fireEvent.click(retryButton);

    await waitFor(() => {
      expect(mockAsyncJobsRetryFailed).toHaveBeenCalledWith("job-1");
    });
  });
});
