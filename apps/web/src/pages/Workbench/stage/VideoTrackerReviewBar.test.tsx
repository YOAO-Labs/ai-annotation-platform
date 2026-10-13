import { act, fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

function render(ui: Parameters<typeof rtlRender>[0]) {
  const result = rtlRender(ui);
  const trigger = screen.queryByTestId("tracker-review-settings-trigger");
  if (trigger) {
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "更多 追踪审阅 工具" }));
  }
  return result;
}

import type { VideoTrackerJobPreview } from "@/api/videoTracker";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import type { TrackerReviewDecisionOutcome } from "@/hooks/useVideoTrackerJobs";
import { projectTrackerReview, type TrackerReviewScope } from "@/hooks/videoTrackerReviewScope";
import { VideoTrackerReviewBar, type VideoTrackerReviewBarProps } from "./VideoTrackerReviewBar";

/** 结算当前排队的 decisionDialog,模拟用户点下某个按钮。 */
function settleDecisionDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  useDecisionDialogStore.getState().settle(value);
}

const preview: VideoTrackerJobPreview = {
  job_id: "job-1",
  status: "partially_reviewed",
  annotation_id: "annotation-1",
  job_revision: 2,
  expected_source_versions: { "annotation-1": 3 },
  candidate_total: 5,
  candidate_pending: 3,
  candidate_accepted: 1,
  candidate_rejected: 1,
  results: [
    {
      frame_index: 10,
      instance_id: "A",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    },
    {
      frame_index: 12,
      instance_id: "A",
      manual_protected: true,
      geometry: { type: "bbox", x: 0.2, y: 0.1, w: 0.2, h: 0.2 },
    },
    {
      frame_index: 11,
      instance_id: "B",
      geometry: { type: "bbox", x: 0.3, y: 0.1, w: 0.2, h: 0.2 },
    },
  ],
  grid_step: 1,
  output_geometry: "bbox",
};

function review(scope: Partial<TrackerReviewScope> = {}, source = preview) {
  return projectTrackerReview(
    source,
    { instanceIds: ["A", "B"], fromFrame: 10, toFrame: 12, intentRevision: 1, ...scope },
    1,
  );
}

function props(overrides: Partial<VideoTrackerReviewBarProps> = {}): VideoTrackerReviewBarProps {
  return {
    review: review(),
    jobs: [
      { jobId: "job-1", label: "追踪一" },
      { jobId: "job-2", label: "追踪二" },
    ],
    onChooseJob: vi.fn(),
    onSetInstances: vi.fn(),
    onSetWindow: vi.fn(),
    onSeekFrame: vi.fn(),
    onDecide: vi.fn().mockResolvedValue({ ok: true }),
    onRefresh: vi.fn(),
    isIntentCurrent: () => true,
    ...overrides,
  };
}

function deferredOutcome() {
  let resolve!: (value: TrackerReviewDecisionOutcome) => void;
  const promise = new Promise<TrackerReviewDecisionOutcome>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.restoreAllMocks();
  // decisionDialog store 是模块级单例,清空队列避免跨测试残留。
  act(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });
});

describe("VideoTrackerReviewBar", () => {
  it("keeps selected scope and decisions visible when collapsed without clearing pending work", async () => {
    const deferred = deferredOutcome();
    const initial = props({ onDecide: vi.fn().mockReturnValue(deferred.promise) });
    const view = rtlRender(<VideoTrackerReviewBar {...initial} />);
    expect(screen.getByTestId("tracker-review-scope-summary")).toHaveTextContent("F10–F12");
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    expect(initial.onDecide).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByTestId("tracker-review-settings-trigger"));
    fireEvent.click(screen.getByRole("button", { name: "更多 追踪审阅 工具" }));
    expect(screen.getByTestId("tracker-review-accept")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(screen.getByTestId("tracker-review-accept")).toBeDisabled();
    view.rerender(<VideoTrackerReviewBar {...initial} presentationHidden />);
    await act(async () => deferred.resolve({ ok: false, reason: "manual_keyframe_protected" }));
    expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
    expect(initial.onDecide).toHaveBeenCalledOnce();
    view.rerender(<VideoTrackerReviewBar {...initial} />);
    expect(screen.getByTestId("tracker-review-accept")).toBeEnabled();
    expect(initial.onSetInstances).not.toHaveBeenCalled();
    expect(initial.onSetWindow).not.toHaveBeenCalled();
  });
  it("没有当前审阅任务时不渲染", () => {
    render(<VideoTrackerReviewBar {...props({ review: null })} />);
    expect(screen.queryByTestId("video-tracker-review-bar")).toBeNull();
  });

  it("按目标和帧窗提交局部 selector", async () => {
    const initial = props();
    const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
    expect(screen.getByText(/已审 2\/5/)).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("tracker-review-instance-B"));
    expect(initial.onSetInstances).toHaveBeenCalledWith(["A"]);
    expect(screen.getByTestId("tracker-review-instance-B")).toBeChecked();
    rerender(<VideoTrackerReviewBar {...initial} review={review({ instanceIds: ["A"] })} />);
    fireEvent.change(screen.getByTestId("tracker-review-to-frame"), {
      target: { value: "10" },
    });
    expect(initial.onSetWindow).toHaveBeenCalledWith(10, 10);
    expect(screen.getByTestId("tracker-review-to-frame")).toHaveValue(12);
    rerender(
      <VideoTrackerReviewBar
        {...initial}
        review={review({ instanceIds: ["A"], toFrame: 10, intentRevision: 2 })}
      />,
    );
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    await waitFor(() =>
      expect(initial.onDecide).toHaveBeenCalledWith({
        instance_ids: ["A"],
        from_frame: 10,
        to_frame: 10,
        decision: "accept",
        override_manual: false,
      }),
    );
  });

  it("显式选择 job，展示同一份范围与待审计数", () => {
    const initial = props({ review: review({ instanceIds: ["A"], fromFrame: 12 }) });
    const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
    expect(screen.getByTestId("video-tracker-review-bar")).toHaveAttribute(
      "data-workbench-tracker-review",
    );
    expect(screen.getByTestId("tracker-review-scope-summary")).toHaveTextContent(
      "审阅 1 个目标 · F12–F12 · 所选待审 1 · 全部待审 3",
    );
    fireEvent.change(screen.getByTestId("tracker-review-job"), { target: { value: "job-2" } });
    expect(initial.onChooseJob).toHaveBeenCalledWith("job-2");
    expect(screen.getByTestId("tracker-review-job")).toHaveValue("job-1");
    rerender(
      <VideoTrackerReviewBar
        {...initial}
        review={review({ instanceIds: ["B"] }, { ...preview, job_id: "job-2" })}
      />,
    );
    expect(screen.queryByTestId("tracker-review-job")).toBeNull();
    expect(screen.getByTestId("video-tracker-review-compact")).toHaveAttribute(
      "data-review-job-id",
      "job-2",
    );
    fireEvent.click(screen.getByTestId("tracker-review-settings-trigger"));
    fireEvent.click(screen.getByRole("button", { name: "更多 追踪审阅 工具" }));
    expect(screen.getByTestId("tracker-review-job")).toHaveValue("job-2");
    expect(screen.getByTestId("video-tracker-review-bar")).toHaveAttribute(
      "data-review-job-id",
      "job-2",
    );
    expect(initial.onSetInstances).not.toHaveBeenCalled();
    expect(initial.onSetWindow).not.toHaveBeenCalled();
  });

  it("revision 刷新遵循 owner 范围，剩余入口只跳源帧", () => {
    const initial = props({ review: review({ instanceIds: ["A"], fromFrame: 12 }) });
    const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
    rerender(
      <VideoTrackerReviewBar
        {...initial}
        review={review(
          { instanceIds: ["A"], fromFrame: 12 },
          { ...preview, job_revision: 3, results: preview.results.slice(1), candidate_pending: 2 },
        )}
      />,
    );
    expect(screen.getByTestId("tracker-review-from-frame")).toHaveValue(12);
    expect(screen.getByTestId("tracker-review-instance-B")).not.toBeChecked();
    expect(screen.getByTestId("tracker-review-scope-summary")).toHaveTextContent(
      "审阅 1 个目标 · F12–F12 · 所选待审 1 · 全部待审 2",
    );
    fireEvent.click(screen.getByTestId("tracker-review-remaining-11-12"));
    expect(initial.onSeekFrame).toHaveBeenCalledWith(11);
    expect(initial.onSetInstances).not.toHaveBeenCalled();
    expect(initial.onSetWindow).not.toHaveBeenCalled();
    expect(initial.onDecide).not.toHaveBeenCalled();
  });

  it("选区含 manual 时在 409 后二次确认覆盖", async () => {
    const onDecide = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, reason: "manual_keyframe_protected" })
      .mockResolvedValueOnce({ ok: true });
    render(<VideoTrackerReviewBar {...props({ onDecide })} />);
    expect(screen.getByTestId("tracker-review-manual-warning")).toHaveTextContent("1 个");
    expect(screen.getByTestId("tracker-review-accept")).not.toBeDisabled();
    expect(screen.getByTestId("tracker-review-discard")).not.toBeDisabled();
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    // 409 后弹应用内覆盖确认;确认 → 同 selector 以 override_manual=true 重试。
    await waitFor(() => expect(useDecisionDialogStore.getState().queue[0]).toBeDefined());
    act(() => settleDecisionDialog(true));
    await waitFor(() => expect(onDecide).toHaveBeenCalledTimes(2));
    expect(onDecide).toHaveBeenNthCalledWith(1, {
      instance_ids: ["A", "B"],
      from_frame: 10,
      to_frame: 12,
      decision: "accept",
      override_manual: false,
    });
    expect(onDecide).toHaveBeenNthCalledWith(2, {
      instance_ids: ["A", "B"],
      from_frame: 10,
      to_frame: 12,
      decision: "accept",
      override_manual: true,
    });
  });

  it.each([
    { review: review({ instanceIds: [] }) },
    { review: review({ fromFrame: 11, toFrame: 10 }) },
    { review: review({}, { ...preview, results: [], candidate_pending: 0 }) },
    { submitting: true },
  ])("无可选 candidate、无效窗口或提交中时禁用决策 %#", (overrides) => {
    const initial = props(overrides);
    render(<VideoTrackerReviewBar {...initial} />);
    expect(screen.getByTestId("tracker-review-accept")).toBeDisabled();
    expect(screen.getByTestId("tracker-review-discard")).toBeDisabled();
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    expect(initial.onDecide).not.toHaveBeenCalled();
  });

  it("等待决定时仍能操作 job 与范围，但不重复提交", async () => {
    const deferred = deferredOutcome();
    const initial = props({ onDecide: vi.fn(() => deferred.promise) });
    render(<VideoTrackerReviewBar {...initial} />);
    fireEvent.click(screen.getByTestId("tracker-review-discard"));
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    expect(initial.onDecide).toHaveBeenCalledOnce();
    expect(screen.getByTestId("tracker-review-job")).toBeEnabled();
    expect(screen.getByTestId("tracker-review-instance-B")).toBeEnabled();
    expect(screen.getByTestId("tracker-review-from-frame")).toBeEnabled();
    fireEvent.change(screen.getByTestId("tracker-review-job"), { target: { value: "job-2" } });
    fireEvent.click(screen.getByTestId("tracker-review-instance-B"));
    fireEvent.change(screen.getByTestId("tracker-review-from-frame"), {
      target: { value: "11" },
    });
    expect(initial.onChooseJob).toHaveBeenCalledWith("job-2");
    expect(initial.onSetInstances).toHaveBeenCalledWith(["A"]);
    expect(initial.onSetWindow).toHaveBeenCalledWith(11, 12);
    await act(async () => deferred.resolve({ ok: false, reason: "revision_conflict" }));
    expect(screen.getByTestId("tracker-review-accept")).toBeEnabled();
  });

  it.each(["job", "task-session"])(
    "换 %s 后可以提交新决定，旧请求完成不会解除新请求的忙碌状态",
    async (change) => {
      const first = deferredOutcome();
      const second = deferredOutcome();
      const initial = props({ onDecide: vi.fn(() => first.promise) });
      const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
      fireEvent.click(screen.getByTestId("tracker-review-accept"));
      expect(screen.getByTestId("tracker-review-accept")).toBeDisabled();
      const nextReview =
        change === "job"
          ? review({}, { ...preview, job_id: "job-2" })
          : projectTrackerReview(preview, initial.review!.scope, 2);
      const onNextDecide = vi.fn(() => second.promise);
      rerender(<VideoTrackerReviewBar {...initial} review={nextReview} onDecide={onNextDecide} />);
      expect(screen.getByTestId("tracker-review-accept")).toBeEnabled();
      fireEvent.click(screen.getByTestId("tracker-review-accept"));
      fireEvent.click(screen.getByTestId("tracker-review-discard"));
      expect(onNextDecide).toHaveBeenCalledOnce();
      await act(async () => first.resolve({ ok: false, reason: "manual_keyframe_protected" }));
      expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
      expect(initial.onDecide).toHaveBeenCalledOnce();
      expect(screen.getByTestId("tracker-review-accept")).toBeDisabled();
      expect(screen.getByTestId("tracker-review-discard")).toBeDisabled();
      await act(async () => second.resolve({ ok: true }));
      expect(screen.getByTestId("tracker-review-accept")).toBeEnabled();
    },
  );

  it.each(["job", "scope", "preview", "owner"])(
    "%s 已变化时不展示旧请求的人工帧确认",
    async (change) => {
      const deferred = deferredOutcome();
      const initial = props({ onDecide: vi.fn(() => deferred.promise) });
      const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
      fireEvent.click(screen.getByTestId("tracker-review-accept"));
      const next =
        change === "job"
          ? review({}, { ...preview, job_id: "job-2" })
          : change === "scope"
            ? review({ instanceIds: ["A"], intentRevision: 2 })
            : change === "preview"
              ? review({}, { ...preview, job_revision: 3 })
              : initial.review;
      rerender(
        <VideoTrackerReviewBar
          {...initial}
          review={next}
          isIntentCurrent={() => change !== "owner"}
        />,
      );
      await act(async () => deferred.resolve({ ok: false, reason: "manual_keyframe_protected" }));
      expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
      expect(initial.onDecide).toHaveBeenCalledOnce();
    },
  );

  it("卸载后不弹出人工帧确认", async () => {
    const deferred = deferredOutcome();
    const initial = props({ onDecide: vi.fn(() => deferred.promise) });
    const { unmount } = render(<VideoTrackerReviewBar {...initial} />);
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    unmount();
    await act(async () => deferred.resolve({ ok: false, reason: "manual_keyframe_protected" }));
    expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
    expect(initial.onDecide).toHaveBeenCalledOnce();
  });

  it("人工帧确认返回后再次检查当前意图", async () => {
    let current = true;
    const initial = props({
      onDecide: vi.fn().mockResolvedValue({ ok: false, reason: "manual_keyframe_protected" }),
      isIntentCurrent: () => current,
    });
    render(<VideoTrackerReviewBar {...initial} />);
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    await waitFor(() => expect(useDecisionDialogStore.getState().queue[0]).toBeDefined());
    // 确认对话框返回前意图已过期 → 不再 override。
    current = false;
    act(() => settleDecisionDialog(true));
    await act(async () => {});
    expect(initial.onDecide).toHaveBeenCalledOnce();
  });

  it("人工帧重试使用点击时的 selector 和决定回调", async () => {
    const deferred = deferredOutcome();
    const onDecide = vi.fn().mockReturnValueOnce(deferred.promise).mockResolvedValue({ ok: true });
    const replacementDecide = vi.fn().mockResolvedValue({ ok: true });
    const initial = props({
      review: review({ instanceIds: ["A"], fromFrame: 12 }),
      onDecide,
    });
    const { rerender } = render(<VideoTrackerReviewBar {...initial} />);
    fireEvent.click(screen.getByTestId("tracker-review-accept"));
    rerender(<VideoTrackerReviewBar {...initial} onDecide={replacementDecide} />);
    await act(async () => deferred.resolve({ ok: false, reason: "manual_keyframe_protected" }));
    await waitFor(() => expect(useDecisionDialogStore.getState().queue[0]).toBeDefined());
    act(() => settleDecisionDialog(true));
    await waitFor(() => expect(onDecide).toHaveBeenCalledTimes(2));
    expect(onDecide).toHaveBeenNthCalledWith(2, {
      instance_ids: ["A"],
      from_frame: 12,
      to_frame: 12,
      decision: "accept",
      override_manual: true,
    });
    expect(replacementDecide).not.toHaveBeenCalled();
  });

  it("展示 Mask 纠错窗口、种子和 fallback lineage", () => {
    render(
      <VideoTrackerReviewBar
        {...props()}
        review={review(
          {},
          {
            ...preview,
            job_kind: "correction",
            correction_frame: 12,
            direction: "backward",
            from_frame: 2,
            to_frame: 12,
            seed_mode: "bbox",
            fallback_reason: "mask_prompt_unsupported",
            protect_manual: true,
          },
        )}
      />,
    );
    expect(screen.getByText("Mask 纠错传播候选")).toBeInTheDocument();
    expect(screen.getByTestId("tracker-review-correction-summary")).toHaveTextContent(
      "F12 人工纠错帧 · 窗口 F2–F12 · 向更早帧 · bbox seed 降级 · 保护人工帧",
    );
    expect(screen.getByTestId("tracker-review-fallback-warning")).toHaveTextContent(
      "mask_prompt_unsupported",
    );
  });
});
