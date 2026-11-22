import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/authStore";
import type { ReviewClaimResponse } from "@/types";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const state = vi.hoisted(() => ({
  blocked: "正在核验并读取原图，请稍候" as string | null,
  status: "loading",
  retry: vi.fn(),
  claim: vi.fn(),
}));
vi.mock("@/api/tasks", () => ({
  tasksApi: {
    get: async (id: string) => ({
      id,
      project_id: "project",
      file_type: "image",
      status: "review",
      tags: [],
    }),
    getAnnotations: async () => [],
    reviewClaim: state.claim,
  },
}));
vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({ data: { id: "project", tool_bindings: {} } }),
}));
vi.mock("@/hooks/usePredictions", () => ({ usePredictions: () => ({ data: { pages: [] } }) }));
vi.mock("@/pages/Workbench/state/useSessionStats", () => ({ useSessionStats: vi.fn() }));
vi.mock("@/pages/Workbench/stage/ImageStage", () => ({ ImageStage: () => <div /> }));
vi.mock("@/pages/Workbench/shell/CommentsPanel", () => ({ CommentsPanel: () => null }));
vi.mock("./ReviewerMiniPanel", () => ({ ReviewerMiniPanel: () => null }));
vi.mock("@/pages/Workbench/stage/useWorkbenchImageSource", () => ({
  useWorkbenchImageSource: () => ({
    source: state.blocked ? null : { kind: "single", url: "blob:owned" },
    contentBlockedReason: state.blocked,
    refresh: state.retry,
  }),
  workbenchImagePreviewUrl: (source: { url?: string } | null) => source?.url ?? null,
}));
vi.mock("@/pages/Workbench/stage/useAbortableImage", () => ({
  useAbortableImage: () => [undefined, state.status],
}));
import { ReviewWorkbench } from "./ReviewWorkbench";

function ownClaim(taskId: string): ReviewClaimResponse {
  return {
    task_id: taskId,
    reviewer_id: "reviewer",
    reviewer_claimed_at: "2026-05-11T08:00:00Z",
    is_self: true,
  };
}

function deferredClaim() {
  let resolve!: (claim: ReviewClaimResponse) => void;
  const promise = new Promise<ReviewClaimResponse>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function renderReview() {
  const approve = vi.fn();
  const reject = vi.fn();
  const client = new QueryClient({ defaultOptions: { mutations: { retryDelay: 0 } } });
  const view = (taskId: string) => (
    <QueryClientProvider client={client}>
      <ReviewWorkbench taskId={taskId} onApprove={approve} onReject={reject} />
    </QueryClientProvider>
  );
  const { rerender } = render(view("task"));
  return {
    approve,
    reject,
    showTask: (taskId: string) => rerender(view(taskId)),
    approveButton: screen.getByTestId("review-approve"),
    rejectButton: screen.getByTestId("review-reject"),
  };
}

function readyImage() {
  state.blocked = null;
  state.status = "loaded";
}

beforeEach(() => {
  state.blocked = "正在核验并读取原图，请稍候";
  state.status = "loading";
  state.retry.mockReset();
  state.claim.mockReset().mockImplementation(async (taskId: string) => ownClaim(taskId));
  useAuthStore.getState().setAuth("token", {
    id: "reviewer",
    email: "reviewer@example.test",
    name: "Reviewer",
    role: "employee",
    group_name: null,
    status: "active",
    created_at: "2026-05-11T08:00:00Z",
  });
});
afterEach(() => {
  cleanup();
  useAuthStore.getState().logout();
});
describe("review image authority gate", () => {
  it("blocks review decisions until the current picture has loaded", async () => {
    const { approve, approveButton, rejectButton, showTask } = renderReview();
    await waitFor(() => expect(state.claim).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText(/正在认领审核任务/)).not.toBeInTheDocument());
    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
    fireEvent.click(approveButton);
    expect(approve).not.toHaveBeenCalled();
    readyImage();
    showTask("task");
    fireEvent.click(approveButton);
    expect(approve).toHaveBeenCalledTimes(1);
  });

  it("shows a scoped failure and actionable retry while keeping decisions blocked", () => {
    state.blocked = "来源图片已变化";
    state.status = "failed";
    const { approveButton } = renderReview();
    expect(screen.getByText("来源图片已变化")).toHaveAttribute("role", "status");
    fireEvent.click(screen.getByRole("button", { name: "重试图片读取" }));
    expect(state.retry).toHaveBeenCalledTimes(1);
    expect(approveButton).toBeDisabled();
  });
});

describe("review claim authority gate", () => {
  it("keeps decisions blocked after a busy claim and enables them only after a successful retry", async () => {
    readyImage();
    const busy = new ApiError(409, "Conflict", { reason: "batch_busy" });
    state.claim.mockRejectedValueOnce(busy).mockRejectedValueOnce(busy);
    const { approve, reject, approveButton, rejectButton } = renderReview();

    const retry = await screen.findByRole("button", { name: "重试认领" });
    expect(screen.getByRole("status")).toHaveTextContent("任务正忙，未能认领");
    expect(state.claim).toHaveBeenCalledTimes(2);
    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
    fireEvent.click(approveButton);
    fireEvent.click(rejectButton);
    expect(approve).not.toHaveBeenCalled();
    expect(reject).not.toHaveBeenCalled();

    fireEvent.click(retry);
    await waitFor(() => expect(approveButton).toBeEnabled());
    expect(state.claim).toHaveBeenLastCalledWith("task");
    expect(state.claim).toHaveBeenCalledTimes(3);
    expect(rejectButton).toBeEnabled();
    fireEvent.click(approveButton);
    expect(approve).toHaveBeenCalledTimes(1);
  });

  it("does not expose decisions or retry for another reviewer's claim", async () => {
    readyImage();
    state.claim.mockResolvedValue({ ...ownClaim("task"), reviewer_id: "other", is_self: false });
    const { approveButton, rejectButton } = renderReview();
    expect(await screen.findByText(/已被其他审核员认领/)).toBeInTheDocument();
    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
    expect(screen.queryByRole("button", { name: "重试认领" })).not.toBeInTheDocument();
  });

  it("does not retry a permission failure", async () => {
    readyImage();
    state.claim.mockRejectedValue(new ApiError(403, "Forbidden"));
    const { approveButton, rejectButton } = renderReview();
    expect(await screen.findByText("无法认领审核任务")).toBeInTheDocument();
    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
    expect(state.claim).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "重试认领" })).not.toBeInTheDocument();
  });

  it("ignores an old claim response even after returning to the same task", async () => {
    readyImage();
    const old = deferredClaim();
    const current = deferredClaim();
    state.claim
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ ...ownClaim("other-task"), is_self: false, reviewer_id: "other" })
      .mockReturnValueOnce(current.promise);
    const { approveButton, rejectButton, showTask } = renderReview();
    await waitFor(() => expect(state.claim).toHaveBeenCalledWith("task"));

    showTask("other-task");
    expect(await screen.findByText(/已被其他审核员认领/)).toBeInTheDocument();
    showTask("task");
    expect(screen.queryByText(/已被其他审核员认领/)).not.toBeInTheDocument();
    await waitFor(() => expect(state.claim).toHaveBeenCalledTimes(3));

    await act(async () => old.resolve(ownClaim("task")));
    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
    await act(async () => current.resolve(ownClaim("task")));
    await waitFor(() => expect(approveButton).toBeEnabled());
  });
});
