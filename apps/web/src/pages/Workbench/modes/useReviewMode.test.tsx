import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import type { TaskResponse } from "@/types";
import { useReviewMode } from "./useReviewMode";

const mocks = vi.hoisted(() => ({
  approveMutate: vi.fn(),
  rejectMutate: vi.fn(),
  claimMutate: vi.fn(),
}));

vi.mock("@/hooks/useTasks", () => ({
  useApproveTask: () => ({ mutate: mocks.approveMutate, isPending: false }),
  useRejectTask: () => ({ mutate: mocks.rejectMutate, isPending: false }),
  useReviewClaim: () => ({ mutate: mocks.claimMutate, isPending: false }),
}));

vi.mock("@/pages/Review/ReviewerMiniPanel", () => ({
  ReviewerMiniPanel: () => <span data-testid="reviewer-mini-panel" />,
}));

function task(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: "t1",
    project_id: "p1",
    display_id: "T-1",
    file_name: "image.jpg",
    file_url: null,
    file_type: "image",
    tags: [],
    status: "review",
    assignee_id: "u1",
    assignee: null,
    reviewer: null,
    is_labeled: false,
    overlap: 0,
    total_annotations: 0,
    total_predictions: 0,
    batch_id: "b1",
    sequence_order: null,
    image_width: 100,
    image_height: 100,
    thumbnail_url: null,
    blurhash: null,
    video_metadata: null,
    submitted_at: null,
    reviewer_id: null,
    reviewer_claimed_at: null,
    reviewed_at: null,
    reject_reason: null,
    skip_reason: null,
    skipped_at: null,
    reopened_count: 0,
    last_reopened_at: null,
    created_at: "2026-05-11T00:00:00Z",
    updated_at: null,
    ...overrides,
  };
}

function renderReview(
  mode: "annotate" | "review" = "review",
  overrides: Partial<TaskResponse> = {},
) {
  const navigateTask = vi.fn();
  const pushToast = vi.fn();
  const taskId = overrides.id ?? "t1";
  const initialProps = {
    mode,
    taskId,
    task: task(overrides),
    navigateTask,
    pushToast,
  };
  const rendered = renderHook((props: typeof initialProps) => useReviewMode(props), {
    initialProps,
  });
  return { ...rendered, navigateTask, pushToast, initialProps };
}

/** 结算队首 decisionDialog 并出队(等价 Host 的 settle + 退场 dispose),模拟用户按下按钮。 */
async function settleDecisionDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  await act(async () => {
    useDecisionDialogStore.getState().settle(value);
  });
  await act(async () => {
    useDecisionDialogStore.getState().dispose();
  });
}

function queuedTitle() {
  return useDecisionDialogStore.getState().queue[0]?.request.title;
}

describe("useReviewMode", () => {
  beforeEach(() => {
    mocks.approveMutate.mockReset();
    mocks.rejectMutate.mockReset();
    mocks.claimMutate.mockReset();
  });

  // decisionDialog store 是模块级单例,清空队列避免跨测试残留(T3 store-settle 约定)。
  afterEach(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });

  it("claims review tasks only in review mode", () => {
    renderReview("annotate");
    expect(mocks.claimMutate).not.toHaveBeenCalled();

    renderReview("review");
    expect(mocks.claimMutate).toHaveBeenCalledWith("t1", expect.any(Object));

    mocks.claimMutate.mockClear();
    renderReview("review", { status: "completed" });
    expect(mocks.claimMutate).not.toHaveBeenCalled();
  });

  it("exposes diff mode state for review mode", () => {
    const { result } = renderReview();

    expect(result.current.diffMode).toBe("diff");
    act(() => result.current.onSetDiffMode?.("raw"));
    expect(result.current.diffMode).toBe("raw");
  });

  it("handles A/R review hotkeys", async () => {
    renderReview();

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" }));
    });
    expect(mocks.approveMutate).toHaveBeenCalledWith("t1", expect.any(Object));

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "r" }));
    });
    // 'r' 打开退回两步流的第一步（reason_type choiceDialog）
    await waitFor(() => expect(queuedTitle()).toBe("退回原因（1 个任务）"));
  });

  it("routes the two-step reject flow to reject mutation with skip hint", async () => {
    const { result } = renderReview("review", { skip_reason: "no_target" });

    await act(async () => {
      result.current.topbarActions.onReject?.();
    });
    // 第一步：skip 提示透传到 choice 描述
    const choice = useDecisionDialogStore.getState().queue[0];
    expect(choice?.request.title).toBe("退回原因（1 个任务）");
    expect(choice?.request.description).toContain("此任务被标注员跳过：no_target");
    await settleDecisionDialog("wrong_geometry");
    // 第二步：可选补充说明
    await waitFor(() => expect(queuedTitle()).toBe("补充说明"));
    await settleDecisionDialog("框不完整");
    expect(mocks.rejectMutate).toHaveBeenCalledWith(
      { taskId: "t1", reason_type: "wrong_geometry", reason: "框不完整" },
      expect.any(Object),
    );
  });

  it("cancelling the second step drops the whole reject", async () => {
    const { result } = renderReview();

    await act(async () => {
      result.current.topbarActions.onReject?.();
    });
    await settleDecisionDialog("missing");
    await waitFor(() => expect(queuedTitle()).toBe("补充说明"));
    await settleDecisionDialog(null);
    expect(mocks.rejectMutate).not.toHaveBeenCalled();
  });

  it("aborts the reject when the task switched while the dialog is open", async () => {
    const { result, rerender, initialProps } = renderReview();

    await act(async () => {
      result.current.topbarActions.onReject?.();
    });
    await settleDecisionDialog("wrong_geometry");
    // async-await gap：第二步弹窗期间切到另一任务，确认后按 currentTaskIdRef 复验放弃
    rerender({ ...initialProps, taskId: "t2", task: task({ id: "t2" }) });
    await settleDecisionDialog("框不完整");
    expect(mocks.rejectMutate).not.toHaveBeenCalled();
  });

  it("ignores A/R from settings buttons and restores review shortcuts after close", () => {
    renderReview();
    const settings = document.createElement("button");
    settings.dataset.workbenchSettings = "";
    settings.dataset.state = "open";
    document.body.append(settings);
    act(() => {
      settings.dispatchEvent(new KeyboardEvent("keydown", { key: "a", bubbles: true }));
      settings.dispatchEvent(new KeyboardEvent("keydown", { key: "r", bubbles: true }));
    });
    expect(mocks.approveMutate).not.toHaveBeenCalled();
    expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
    settings.remove();
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "a" })));
    expect(mocks.approveMutate).toHaveBeenCalledTimes(1);
  });
});
