import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import type { MaskQcIssue } from "@/api/maskQc";
import type { MaskQcTrackerCandidate } from "../state/useMaskQcReview";
import type { FeedbackAnchorPosition } from "@/api/feedbacks";

/** 结算当前排队的 decisionDialog,模拟用户点下某个按钮。 */
function settleDecisionDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  useDecisionDialogStore.getState().settle(value);
}

const mocks = vi.hoisted(() => ({
  useIssues: vi.fn(),
  runMutate: vi.fn(),
  patchMutate: vi.fn(),
  createFeedbackMutate: vi.fn(),
  summaryRefetch: vi.fn(),
  feedback: null as unknown,
}));

vi.mock("@/hooks/useMaskQc", () => ({
  useMaskQcIssues: (params: unknown) => mocks.useIssues(params),
  useTaskMaskQcSummary: () => ({
    data: { status: "completed", progress_pct: 100, counts: { open: 1 }, run_id: "run-1" },
    isLoading: false,
    refetch: mocks.summaryRefetch,
  }),
  useRunTaskMaskQc: () => ({ mutate: mocks.runMutate, isPending: false }),
  usePatchMaskQcIssue: () => ({ mutate: mocks.patchMutate, isPending: false }),
}));

vi.mock("@/hooks/useFeedbacks", () => ({
  useInfiniteFeedbacks: () => mocks.feedback,
  useCreateFeedback: () => ({ mutate: mocks.createFeedbackMutate, isPending: false }),
}));

vi.mock("./MaskRepairSheet", () => ({
  MaskRepairSheet: ({ open, actions }: { open: boolean; actions: Array<{ issue_id: string }> }) => (
    <div
      data-testid="mask-repair-sheet"
      data-open={open ? "true" : "false"}
      data-actions={actions.map((action) => action.issue_id).join(",")}
    />
  ),
}));

import { MaskQcPanel, type MaskQcPanelProps } from "./MaskQcPanel";

function currentFeedbackAnchor(): FeedbackAnchorPosition {
  return (
    mocks.feedback as {
      data: { pages: Array<{ items: Array<{ anchor_position: FeedbackAnchorPosition }> }> };
    }
  ).data.pages[0].items[0].anchor_position;
}

function issue(overrides: Partial<MaskQcIssue> = {}): MaskQcIssue {
  return {
    id: "issue-1",
    run_id: "run-1",
    last_seen_run_id: "run-1",
    project_id: "project-1",
    task_id: "task-1",
    annotation_id: "annotation-1",
    annotation_version: 7,
    related_annotation_ids: [],
    source_versions: { "annotation-1": 7 },
    code: "small_island",
    severity: "warning",
    status: "open",
    effective_status: "open",
    frame_start: 3,
    frame_end: 3,
    metric: {},
    threshold: {},
    region_bbox: { x0: 0.1, y0: 0.2, x1: 0.3, y1: 0.4 },
    region_mask_ref: null,
    region_digest: "region-digest",
    source: {},
    suggestion: "检查小孤岛",
    resolved_by_id: null,
    resolved_at: null,
    created_at: "2026-07-23T00:00:00Z",
    updated_at: "2026-07-23T00:00:00Z",
    ...overrides,
  };
}

const trackerCandidate: MaskQcTrackerCandidate = {
  key: "job-1:4:digest:3:a",
  jobId: "job-1",
  jobRevision: 4,
  digest: "digest",
  frameIndex: 3,
  annotationId: "annotation-1",
  instanceId: "a",
  label: "SAM2 · r4 · 实例 a",
};

function props(overrides: Partial<MaskQcPanelProps> = {}): MaskQcPanelProps {
  return {
    projectId: "project-1",
    taskId: "task-1",
    activeIssue: issue(),
    phase: "ready",
    error: null,
    compare: null,
    baseline: "previous_version",
    aiCandidateAvailable: false,
    trackerCandidates: [trackerCandidate],
    trackerCandidateKey: null,
    mode: "overlay",
    onNavigateIssue: vi.fn(),
    onReplayFeedback: vi.fn(),
    onRetryNavigation: vi.fn(),
    onClearIssue: vi.fn(),
    onSetMode: vi.fn(),
    onSetBaseline: vi.fn(),
    onSetTrackerCandidate: vi.fn(),
    onDecideTrackerRegion: vi.fn().mockResolvedValue({ ok: true }),
    onUpdateIssue: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  // decisionDialog store 是模块级单例,清空队列避免跨测试残留。
  act(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.useIssues.mockReturnValue({
    data: { pages: [{ items: [issue()], next_cursor: null }], pageParams: [null] },
    isLoading: false,
    isError: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
  });
  mocks.feedback = {
    data: {
      pages: [
        {
          items: [
            {
              id: "feedback-1",
              body: "边界需要复核",
              author_name: "审核员",
              anchor_position: {
                x: 0.2,
                y: 0.3,
                frame: 3,
                region_bbox: [0.1, 0.2, 0.3, 0.4],
                region_digest: "region-digest",
                boundary_digest: "boundary-digest",
              },
            },
          ],
          next_cursor: null,
        },
      ],
      pageParams: [null],
    },
    isLoading: false,
    hasNextPage: false,
    isFetchingNextPage: false,
    fetchNextPage: vi.fn(),
  };
});

describe("MaskQcPanel", () => {
  it("只将当前可确定修复的 open issue 加入批量预览", () => {
    render(<MaskQcPanel {...props()} />);
    const checkbox = screen.getByRole("checkbox", { name: "选择修复 小孤岛" });
    const preview = screen.getByRole("button", { name: "预览批量修复" });
    expect(preview).toBeDisabled();
    fireEvent.click(checkbox);
    expect(preview).toBeEnabled();
  });

  it("filters selected repair ids when a loaded issue becomes ineligible", async () => {
    const view = render(<MaskQcPanel {...props()} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "选择修复 小孤岛" }));
    expect(screen.getByRole("button", { name: "预览批量修复" })).toBeEnabled();

    mocks.useIssues.mockReturnValue({
      data: {
        pages: [
          {
            items: [issue({ effective_status: "stale" }), issue({ id: "issue-2" })],
            next_cursor: null,
          },
        ],
      },
      isLoading: false,
      isError: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    });
    view.rerender(<MaskQcPanel {...props()} />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "预览批量修复" })).toBeDisabled(),
    );
    expect(screen.getAllByRole("checkbox", { name: "选择修复 小孤岛" })[0]).toBeDisabled();
  });

  it("closes the repair preview when its selected issue leaves the active query", async () => {
    const view = render(<MaskQcPanel {...props()} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "选择修复 小孤岛" }));
    fireEvent.click(screen.getByRole("button", { name: "预览批量修复" }));
    expect(screen.getByTestId("mask-repair-sheet")).toHaveAttribute("data-open", "true");

    mocks.useIssues.mockReturnValue({
      data: { pages: [{ items: [issue({ id: "issue-2" })], next_cursor: null }] },
      isLoading: false,
      isError: false,
      hasNextPage: false,
      isFetchingNextPage: false,
      fetchNextPage: vi.fn(),
      refetch: vi.fn(),
    });
    view.rerender(<MaskQcPanel {...props()} />);
    await waitFor(() =>
      expect(screen.getByTestId("mask-repair-sheet")).toHaveAttribute("data-open", "false"),
    );
    expect(screen.getByTestId("mask-repair-sheet")).toHaveAttribute("data-actions", "");
  });

  it("可切到项目范围并保留 cursor 列表入口", () => {
    render(<MaskQcPanel {...props()} />);
    expect(mocks.useIssues).toHaveBeenLastCalledWith(expect.objectContaining({ taskId: "task-1" }));
    fireEvent.click(screen.getByRole("button", { name: "整个项目" }));
    expect(mocks.useIssues).toHaveBeenLastCalledWith(
      expect.objectContaining({ taskId: undefined }),
    );
  });

  it("选择 Tracker 基线时传递精确候选三元组", () => {
    const onSetTrackerCandidate = vi.fn();
    render(<MaskQcPanel {...props({ onSetTrackerCandidate })} />);
    fireEvent.change(screen.getByLabelText("对比基线"), {
      target: { value: "tracker_candidate" },
    });
    expect(onSetTrackerCandidate).toHaveBeenCalledWith(trackerCandidate);
  });

  it("刷新后展示 digest 匹配的区域评论，并让 stale 问题保持只读", () => {
    render(
      <MaskQcPanel
        {...props({
          activeIssue: issue({ effective_status: "stale" }),
        })}
      />,
    );
    expect(screen.getByText("边界需要复核")).toBeTruthy();
    expect(screen.getByText("boundary-dig")).toBeTruthy();
    expect(screen.getByPlaceholderText("旧版本问题仅可查看")).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("同 digest 但不同帧的评论不会串到当前问题", () => {
    currentFeedbackAnchor().frame = 4;
    render(<MaskQcPanel {...props()} />);
    expect(screen.queryByText("边界需要复核")).toBeNull();
    expect(screen.getByText("当前区域暂无评论。")).toBeTruthy();
  });

  it("切换问题时清空未提交评论草稿", () => {
    const view = render(<MaskQcPanel {...props()} />);
    const input = screen.getByPlaceholderText("记录区域评论") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "属于问题 A" } });
    expect(input.value).toBe("属于问题 A");
    view.rerender(<MaskQcPanel {...props({ activeIssue: issue({ id: "issue-2" }) })} />);
    expect((screen.getByPlaceholderText("记录区域评论") as HTMLInputElement).value).toBe("");
  });

  it("用评论保存的对比定位器重放证据", () => {
    const onReplayFeedback = vi.fn();
    currentFeedbackAnchor().compare_locator = {
      baseline_kind: "previous_version",
      mode: "boundary",
      current_digest: "a".repeat(64),
      baseline_digest: "b".repeat(64),
    };
    const activeIssue = issue();
    render(<MaskQcPanel {...props({ activeIssue, onReplayFeedback })} />);
    fireEvent.click(screen.getByRole("button", { name: "定位" }));
    expect(onReplayFeedback).toHaveBeenCalledWith(
      activeIssue,
      expect.objectContaining({ mode: "boundary", baseline_kind: "previous_version" }),
    );
  });

  it("对精确 Tracker 候选提交区域决定", async () => {
    const onDecideTrackerRegion = vi.fn().mockResolvedValue({ ok: true });
    render(
      <MaskQcPanel
        {...props({
          baseline: "tracker_candidate",
          trackerCandidateKey: trackerCandidate.key,
          onDecideTrackerRegion,
          compare: {
            baseline_kind: "tracker_candidate",
            current: {
              annotation_id: "annotation-1",
              annotation_version: 7,
              frame_index: 3,
              source: "prediction",
              state: "exact",
              digest: "current",
              size: [4, 4],
              content_path: "/current",
              candidate_job_id: null,
              candidate_digest: null,
              candidate_instance_id: null,
            },
            baseline: {
              annotation_id: "annotation-1",
              annotation_version: 7,
              frame_index: 3,
              source: "tracker_candidate",
              state: "candidate",
              digest: "candidate",
              size: [4, 4],
              content_path: "/candidate",
              candidate_job_id: trackerCandidate.jobId,
              candidate_digest: trackerCandidate.digest,
              candidate_instance_id: trackerCandidate.instanceId,
            },
            metrics: {
              current_area_pixels: 4,
              baseline_area_pixels: 5,
              intersection_pixels: 4,
              union_pixels: 5,
              changed_pixels: 1,
              added_pixels: 1,
              removed_pixels: 0,
              iou_numerator: 4,
              iou_denominator: 5,
              dice_numerator: 8,
              dice_denominator: 9,
            },
            loss: [],
          },
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "区域接受" }));
    // 区域决定前弹应用内确认;确认后才提交。
    await waitFor(() => expect(useDecisionDialogStore.getState().queue[0]).toBeDefined());
    act(() => settleDecisionDialog(true));
    await waitFor(() => {
      expect(onDecideTrackerRegion).toHaveBeenCalledWith(
        expect.objectContaining({ id: "issue-1" }),
        trackerCandidate,
        "accept",
      );
    });
  });
});
