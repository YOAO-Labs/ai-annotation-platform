/**
 * v0.10.16 · ReviewerDashboard 单测：加载态 / 空 pending / handleApprove /
 * handleReject（两步 decisionDialog 选 reason_type + 可选补充说明）/ recentReviews 渲染。
 * plan 1789527942 · T2：原 RejectReasonModal 已迁移 choiceDialog + inputDialog，
 * 这里渲染真实 <DecisionDialogHost /> 驱动完整两步流。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApiError } from "@/api/client";

const mockUseReviewerStats = vi.fn();
const mockUseMyRecentReviews = vi.fn();
const mockApproveMutate = vi.fn();
const mockRejectMutate = vi.fn();
const mockInvalidate = vi.fn();
const mockPushToast = vi.fn();

vi.mock("@/hooks/useDashboard", () => ({
  useReviewerStats: () => mockUseReviewerStats(),
  useMyRecentReviews: () => mockUseMyRecentReviews(),
}));
vi.mock("@/hooks/useTasks", () => ({
  useApproveTask: () => ({ mutate: mockApproveMutate }),
  useRejectTask: () => ({ mutate: mockRejectMutate }),
}));
vi.mock("@tanstack/react-query", async () => {
  const actual =
    await vi.importActual<typeof import("@tanstack/react-query")>("@tanstack/react-query");
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: mockInvalidate }),
  };
});
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

import { ReviewerDashboard } from "./ReviewerDashboard";
import { DecisionDialogHost } from "@/components/ui/DecisionDialogHost";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";

const baseStats = {
  pending_review_count: 3,
  today_reviewed: 5,
  approval_rate: 88,
  approval_rate_24h: 91,
  total_reviewed: 220,
  pending_tasks: [],
  reviewing_batches: [],
  median_review_duration_ms: 30000,
  reopen_after_approve_rate: 1.2,
  weekly_compare_pct: 4.5,
  daily_review_counts: [3, 4, 5, 6, 7, 5, 8],
};

function renderUI() {
  return render(
    <MemoryRouter>
      <ReviewerDashboard />
      <DecisionDialogHost />
    </MemoryRouter>,
  );
}

describe("ReviewerDashboard", () => {
  beforeEach(() => {
    mockApproveMutate.mockReset();
    mockRejectMutate.mockReset();
    mockInvalidate.mockReset();
    mockPushToast.mockReset();
    mockUseMyRecentReviews.mockReturnValue({ data: [] });
  });

  // vitest afterEach 先于 RTL cleanup 跑,此刻 Host 仍挂载;清空 decisionDialog 队列防跨用例残留。
  afterEach(() => {
    act(() => {
      useDecisionDialogStore.setState({ queue: [] });
    });
  });

  it("isLoading=true → 显示加载中", () => {
    mockUseReviewerStats.mockReturnValue({ data: undefined, isLoading: true });
    renderUI();
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("统计首屏离线暂停 → 显示等待网络连接而不是持续加载", () => {
    mockUseReviewerStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
    });
    renderUI();
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，审核统计会在恢复后自动继续",
    );
  });

  it("统计初始失败 → 显示错误态并支持重试", () => {
    const refetch = vi.fn();
    mockUseReviewerStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("server unavailable"),
      refetch,
    });
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("无法加载审核统计");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("统计刷新失败 → 保留已有 KPI 并提示更新失败", () => {
    const refetch = vi.fn();
    mockUseReviewerStats.mockReturnValue({
      data: baseStats,
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
    });
    renderUI();
    expect(screen.getByText("220")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("审核统计更新失败");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("统计 403 → 显示权限提示而不是空数据", () => {
    mockUseReviewerStats.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new ApiError(403, "forbidden"),
      refetch: vi.fn(),
    });
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("没有权限查看审核统计");
  });

  it("空 pending → 显示「暂无待审核任务」", () => {
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    renderUI();
    expect(screen.getByText("暂无待审核任务")).toBeInTheDocument();
    expect(screen.getByText("所有标注任务已审核完毕")).toBeInTheDocument();
  });

  it("最近审核记录首屏离线暂停 → 显示等待网络连接而不是空态", () => {
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    mockUseMyRecentReviews.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      isPaused: true,
      fetchStatus: "paused",
    });
    renderUI();
    expect(screen.getByRole("status")).toHaveTextContent(
      "网络连接已断开，最近审核记录会在恢复后自动继续",
    );
    expect(screen.queryByText("暂无审核记录")).not.toBeInTheDocument();
  });

  it("产能/质量数值正确显示", () => {
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    renderUI();
    expect(screen.getByText("3")).toBeInTheDocument(); // pending_review_count
    expect(screen.getByText("88%")).toBeInTheDocument(); // approval_rate
    expect(screen.getByText("91%")).toBeInTheDocument(); // approval_rate_24h
    expect(screen.getByText("220")).toBeInTheDocument(); // total_reviewed
  });

  it("median_review_duration_ms < 60s → 显示 X.Xs；>= 60s → Xm", () => {
    let s = { ...baseStats, median_review_duration_ms: 45000 };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    const { unmount } = renderUI();
    expect(screen.getByText("45.0s")).toBeInTheDocument();
    unmount();

    s = { ...baseStats, median_review_duration_ms: 120000 };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    renderUI();
    expect(screen.getByText("2m")).toBeInTheDocument();
  });

  it("median_review_duration_ms=null → —", () => {
    const s = { ...baseStats, median_review_duration_ms: null };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    renderUI();
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("有 pending_tasks → 渲染行 + 通过按钮触发 approveMut", () => {
    const s = {
      ...baseStats,
      pending_tasks: [
        {
          task_id: "t1",
          task_display_id: "T-1",
          file_name: "a.jpg",
          project_id: "p1",
          project_name: "Proj",
          total_annotations: 3,
          total_predictions: 1,
          updated_at: "2026-01-01T10:00:00Z",
        },
      ],
    };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    mockApproveMutate.mockImplementation((_id, opts) => opts?.onSuccess?.());
    renderUI();
    expect(screen.getByText("T-1")).toBeInTheDocument();
    fireEvent.click(screen.getByText("通过"));
    expect(mockApproveMutate).toHaveBeenCalledWith(
      "t1",
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
    expect(mockPushToast).toHaveBeenCalledWith({
      msg: "任务已通过审核",
      kind: "success",
    });
    expect(mockInvalidate).toHaveBeenCalledWith({
      queryKey: ["dashboard", "reviewer"],
    });
  });

  it("退回按钮 → 两步决策弹窗选漏标并留空补充 → 调用 rejectMut", async () => {
    const s = {
      ...baseStats,
      pending_tasks: [
        {
          task_id: "t4",
          task_display_id: "T-4",
          file_name: "d.jpg",
          project_id: "p1",
          project_name: "Proj",
          total_annotations: 2,
          total_predictions: 1,
          updated_at: null,
        },
      ],
    };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    mockRejectMutate.mockImplementation((_args, opts) => opts?.onSuccess?.());
    renderUI();
    fireEvent.click(screen.getByText("退回"));
    // 第一步 choiceDialog：选项 label 沿用 rejectReasonTypes 单点
    let dialog = await screen.findByRole("alertdialog", { name: "退回原因（1 个任务）" });
    fireEvent.click(within(dialog).getByRole("button", { name: "漏标" }));
    // 第二步 inputDialog：补充说明可留空
    dialog = await screen.findByRole("alertdialog", { name: "补充说明" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认退回" }));
    await waitFor(() =>
      expect(mockRejectMutate).toHaveBeenCalledWith(
        { taskId: "t4", reason_type: "missing", reason: undefined },
        expect.objectContaining({ onSuccess: expect.any(Function) }),
      ),
    );
    expect(mockPushToast).toHaveBeenCalledWith({
      msg: "任务已退回标注员",
      kind: "success",
    });
  });

  it("退回弹窗切换 type 后 payload 反映新 type", async () => {
    const s = {
      ...baseStats,
      pending_tasks: [
        {
          task_id: "t5",
          task_display_id: "T-5",
          file_name: "e.jpg",
          project_id: "p1",
          project_name: "Proj",
          total_annotations: 0,
          total_predictions: 0,
          updated_at: null,
        },
      ],
    };
    mockUseReviewerStats.mockReturnValue({ data: s, isLoading: false });
    renderUI();
    fireEvent.click(screen.getByText("退回"));
    let dialog = await screen.findByRole("alertdialog", { name: "退回原因（1 个任务）" });
    fireEvent.click(within(dialog).getByRole("button", { name: "类别错误" }));
    dialog = await screen.findByRole("alertdialog", { name: "补充说明" });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认退回" }));
    await waitFor(() =>
      expect(mockRejectMutate).toHaveBeenCalledWith(
        expect.objectContaining({ taskId: "t5", reason_type: "wrong_label" }),
        expect.any(Object),
      ),
    );
  });

  it("recentReviews 列表渲染 + 空态", () => {
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    mockUseMyRecentReviews.mockReturnValue({
      data: [
        {
          task_id: "r1",
          task_display_id: "R-1",
          file_name: "x.jpg",
          project_id: "p1",
          project_name: "Proj",
          status: "completed",
          reviewed_at: "2026-01-02T08:00:00Z",
        },
      ],
    });
    renderUI();
    expect(screen.getByText("R-1")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
  });

  it("最近审核记录初始失败 → 保留独立区域并可重试", () => {
    const refetch = vi.fn();
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    mockUseMyRecentReviews.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
    });
    renderUI();
    expect(screen.getByRole("alert")).toHaveTextContent("无法加载最近审核记录");
    expect(screen.queryByText("暂无审核记录")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });

  it("最近审核记录刷新失败 → 保留旧记录并提示更新失败", () => {
    const refetch = vi.fn();
    mockUseReviewerStats.mockReturnValue({ data: baseStats, isLoading: false });
    mockUseMyRecentReviews.mockReturnValue({
      data: [
        {
          task_id: "r1",
          task_display_id: "R-1",
          file_name: "x.jpg",
          project_id: "p1",
          project_name: "Proj",
          status: "completed",
          reviewed_at: "2026-01-02T08:00:00Z",
        },
      ],
      isLoading: false,
      isError: true,
      error: new Error("network"),
      refetch,
    });
    renderUI();
    expect(screen.getByText("R-1")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("最近审核记录更新失败");
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});
