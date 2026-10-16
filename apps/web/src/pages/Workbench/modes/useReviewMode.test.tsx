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
  lockLifecycle: {
    releaseTaskLock?: (taskId: string) => Promise<void>;
    resumeTaskLock?: (taskId: string) => void;
  } = {},
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
    ...lockLifecycle,
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

  it("locks retired review tasks and does not expose review decisions", () => {
    const { result, rerender, initialProps } = renderReview();
    rerender({ ...initialProps, task: task({ status: "rejected" }) });

    expect(result.current.isLocked).toBe(true);
    expect(result.current.topbarActions.canApprove).toBe(false);
    expect(result.current.topbarActions.canReject).toBe(false);
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(mocks.approveMutate).not.toHaveBeenCalled();
  });

  it("releases the review lock before a decision and resumes it after mutation failure", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi.fn(() => releasePending);
    const resumeTaskLock = vi.fn();
    const { result } = renderReview("review", {}, { releaseTaskLock, resumeTaskLock });

    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(releaseTaskLock).toHaveBeenCalledWith("t1");
    expect(mocks.approveMutate).not.toHaveBeenCalled();

    await act(async () => {
      finishRelease();
      await releasePending;
    });
    await waitFor(() => expect(mocks.approveMutate).toHaveBeenCalledWith("t1", expect.any(Object)));
    const callbacks = mocks.approveMutate.mock.calls[0]?.[1] as { onError: () => void };
    act(() => callbacks.onError());
    expect(resumeTaskLock).toHaveBeenCalledWith("t1");
  });

  it("serializes button and keyboard decisions while lock release is pending", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi.fn(() => releasePending);
    const { result } = renderReview("review", {}, { releaseTaskLock });
    act(() => {
      result.current.topbarActions.onApprove?.();
      result.current.topbarActions.onApprove?.();
      result.current.topbarActions.onReject?.();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "a", repeat: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "r", repeat: true }));
    });
    expect(releaseTaskLock).toHaveBeenCalledTimes(1);
    expect(result.current.topbarActions.isApproving).toBe(true);
    expect(result.current.topbarActions.canApprove).toBe(false);
    expect(result.current.topbarActions.canReject).toBe(false);
    expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
    await act(async () => {
      finishRelease();
      await releasePending;
    });
    expect(mocks.approveMutate).toHaveBeenCalledTimes(1);
    act(() => mocks.approveMutate.mock.calls[0][1].onSuccess());
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(releaseTaskLock).toHaveBeenCalledTimes(1);
  });

  it("reserves one reject dialog and decision through lock release", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi.fn(() => releasePending);
    const { result } = renderReview("review", {}, { releaseTaskLock });
    act(() => {
      result.current.topbarActions.onReject?.();
      result.current.topbarActions.onReject?.();
      result.current.topbarActions.onApprove?.();
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "r", repeat: true }));
    });
    expect(useDecisionDialogStore.getState().queue).toHaveLength(1);
    expect(result.current.topbarActions.isRejecting).toBe(true);
    expect(result.current.topbarActions.canApprove).toBe(false);
    expect(result.current.topbarActions.canReject).toBe(false);
    await settleDecisionDialog("missing");
    await settleDecisionDialog("Missing object");
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(releaseTaskLock).toHaveBeenCalledTimes(1);
    expect(mocks.approveMutate).not.toHaveBeenCalled();
    await act(async () => {
      finishRelease();
      await releasePending;
    });
    expect(mocks.rejectMutate).toHaveBeenCalledTimes(1);
    act(() => mocks.rejectMutate.mock.calls[0][1].onError());
    expect(result.current.topbarActions.isRejecting).toBe(false);
    await act(async () => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(mocks.approveMutate).toHaveBeenCalledTimes(1);
  });

  it("restores decision actions after reject cancellation at either step", async () => {
    const releaseTaskLock = vi.fn().mockResolvedValue(undefined);
    const { result } = renderReview("review", {}, { releaseTaskLock });
    act(() => {
      result.current.topbarActions.onReject?.();
    });
    await settleDecisionDialog(null);
    expect(result.current.topbarActions.isRejecting).toBe(false);
    expect(result.current.topbarActions.canReject).toBe(true);
    act(() => {
      result.current.topbarActions.onReject?.();
    });
    await settleDecisionDialog("missing");
    await settleDecisionDialog(null);
    expect(result.current.topbarActions.canApprove).toBe(true);
    expect(releaseTaskLock).not.toHaveBeenCalled();
    await act(async () => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(mocks.approveMutate).toHaveBeenCalledTimes(1);
  });

  it("restores the lock and permits retry when releasing it fails", async () => {
    const releaseTaskLock = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue(undefined);
    const resumeTaskLock = vi.fn();
    const { result, pushToast } = renderReview("review", {}, { releaseTaskLock, resumeTaskLock });
    await act(async () => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(mocks.approveMutate).not.toHaveBeenCalled();
    expect(resumeTaskLock).toHaveBeenCalledWith("t1");
    expect(pushToast).toHaveBeenCalledWith({ msg: "通过失败，请重试", kind: "error" });
    expect(result.current.topbarActions.isApproving).toBe(false);
    await act(async () => {
      void result.current.topbarActions.onApprove?.();
    });
    expect(mocks.approveMutate).toHaveBeenCalledTimes(1);
  });

  it("does not apply an old release after switching away and back to the same task", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi
      .fn()
      .mockReturnValueOnce(releasePending)
      .mockResolvedValue(undefined);
    const { result, rerender, initialProps } = renderReview("review", {}, { releaseTaskLock });
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    rerender({ ...initialProps, taskId: "t2", task: task({ id: "t2" }) });
    rerender(initialProps);
    expect(result.current.topbarActions.isApproving).toBe(false);
    act(() => {
      result.current.topbarActions.onReject?.();
    });
    await act(async () => {
      finishRelease();
      await releasePending;
    });
    expect(mocks.approveMutate).not.toHaveBeenCalled();
    expect(result.current.topbarActions.isRejecting).toBe(true);
    await settleDecisionDialog("missing");
    await settleDecisionDialog("Missing object");
    expect(mocks.rejectMutate).toHaveBeenCalledTimes(1);
  });

  it("ignores retired mutation callbacks without clearing the next task's decision", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi
      .fn()
      .mockResolvedValueOnce(undefined)
      .mockReturnValueOnce(releasePending);
    const resumeTaskLock = vi.fn();
    const { result, rerender, initialProps, navigateTask, pushToast } = renderReview(
      "review",
      {},
      { releaseTaskLock, resumeTaskLock },
    );
    await act(async () => {
      void result.current.topbarActions.onApprove?.();
    });
    const oldCallbacks = mocks.approveMutate.mock.calls[0][1];
    rerender({ ...initialProps, taskId: "t2", task: task({ id: "t2" }) });
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    act(() => {
      oldCallbacks.onError();
      oldCallbacks.onSuccess();
    });
    expect(resumeTaskLock).not.toHaveBeenCalled();
    expect(pushToast).not.toHaveBeenCalled();
    expect(navigateTask).not.toHaveBeenCalled();
    expect(result.current.topbarActions.isApproving).toBe(true);
    await act(async () => {
      finishRelease();
      await releasePending;
    });
    expect(mocks.approveMutate).toHaveBeenCalledTimes(2);
    expect(mocks.approveMutate.mock.calls[1][0]).toBe("t2");
  });

  it("abandons a pending lock release after unmount", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const { result, unmount } = renderReview(
      "review",
      {},
      { releaseTaskLock: () => releasePending },
    );
    act(() => {
      void result.current.topbarActions.onApprove?.();
    });
    unmount();
    await act(async () => {
      finishRelease();
      await releasePending;
    });
    expect(mocks.approveMutate).not.toHaveBeenCalled();
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
    await waitFor(() => expect(mocks.approveMutate).toHaveBeenCalledWith("t1", expect.any(Object)));
    act(() => mocks.approveMutate.mock.calls[0][1].onError());

    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "r" }));
    });
    // 'r' 打开退回两步流的第一步（reason_type choiceDialog）
    await waitFor(() => expect(queuedTitle()).toBe("退回原因（1 个任务）"));
  });

  it("releases the review lock before the two-step reject mutation", async () => {
    let finishRelease!: () => void;
    const releasePending = new Promise<void>((resolve) => {
      finishRelease = resolve;
    });
    const releaseTaskLock = vi.fn(() => releasePending);
    const { result } = renderReview("review", { skip_reason: "no_target" }, { releaseTaskLock });

    await act(async () => {
      result.current.topbarActions.onReject?.();
    });
    // 第一步：skip 提示透传到 choice 描述
    const choice = useDecisionDialogStore.getState().queue[0];
    expect(choice?.request.title).toBe("退回原因（1 个任务）");
    expect(choice?.request.description).toContain("此任务被标注员跳过：no_target");
    await settleDecisionDialog("wrong_geometry");
    await waitFor(() => expect(queuedTitle()).toBe("补充说明"));
    await settleDecisionDialog("框不完整");
    await waitFor(() => expect(releaseTaskLock).toHaveBeenCalledWith("t1"));
    expect(mocks.rejectMutate).not.toHaveBeenCalled();

    await act(async () => {
      finishRelease();
      await releasePending;
    });
    await waitFor(() =>
      expect(mocks.rejectMutate).toHaveBeenCalledWith(
        { taskId: "t1", reason_type: "wrong_geometry", reason: "框不完整" },
        expect.any(Object),
      ),
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

  it("ignores A/R from settings buttons and restores review shortcuts after close", async () => {
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
    await waitFor(() => expect(mocks.approveMutate).toHaveBeenCalledTimes(1));
  });
});
