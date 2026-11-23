import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { TaskResponse } from "@/types";
import { ReviewClaimNotice } from "@/pages/Review/ReviewClaimNotice";
import { WorkbenchBanners } from "./WorkbenchBanners";

function task(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: "task-1",
    project_id: "project-1",
    display_id: "T-1",
    file_name: "image.jpg",
    file_url: null,
    file_type: "image",
    tags: [],
    status: "in_progress",
    assignee_id: "user-1",
    assignee: null,
    reviewer: null,
    is_labeled: false,
    overlap: 0,
    total_annotations: 0,
    total_predictions: 0,
    batch_id: null,
    sequence_order: null,
    image_width: null,
    image_height: null,
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

const baseProps = {
  mode: "annotate" as const,
  task: task(),
  lockError: null,
  canWithdraw: false,
  canReopen: false,
  isWithdrawing: false,
  isReopening: false,
  isAcceptingRejection: false,
  onWithdraw: vi.fn(),
  onReopen: vi.fn(),
  onAcceptRejection: vi.fn(),
};

describe("WorkbenchBanners", () => {
  it("renders lock error banner", () => {
    render(<WorkbenchBanners {...baseProps} lockError="Lock expired" />);

    expect(screen.getByText("任务锁已过期，请刷新页面")).toBeTruthy();
  });

  it("renders lock holder name when the backend reports it", () => {
    render(
      <WorkbenchBanners
        {...baseProps}
        lockError="Task is locked by another user"
        lockConflict={{
          reason: "task_locked_by_other",
          user_id: "user-2",
          expire_at: "2026-05-11T08:05:00Z",
          locked_by: {
            id: "user-2",
            name: "李静雯",
            email: "li@example.com",
            role: "employee",
            avatar_initial: "李",
          },
        }}
      />,
    );

    expect(screen.getByText("该任务正被 李静雯 编辑")).toBeTruthy();
  });

  it("renders review claim and skipped-task banners", () => {
    render(
      <WorkbenchBanners
        {...baseProps}
        mode="review"
        task={task({ skip_reason: "no_target" })}
        modeBanner={<ReviewClaimNotice message="已被其他审核员认领，无法提交审核结果" />}
      />,
    );

    expect(screen.getByText(/已被其他审核员认领/)).toBeTruthy();
    expect(screen.getByText("标注员跳过此题 · 可通过（无目标即视为完成）或退回重派")).toBeTruthy();
  });

  it("renders annotate review banner and triggers withdraw", () => {
    const onWithdraw = vi.fn();
    render(
      <WorkbenchBanners
        {...baseProps}
        task={task({ status: "review" })}
        canWithdraw
        onWithdraw={onWithdraw}
      />,
    );

    fireEvent.click(screen.getByText("撤回提交"));
    expect(onWithdraw).toHaveBeenCalledTimes(1);
  });

  it("renders rejected banner and triggers accept rejection", () => {
    const onAcceptRejection = vi.fn();
    render(
      <WorkbenchBanners
        {...baseProps}
        task={task({ status: "rejected", reject_reason: "框偏移" })}
        onAcceptRejection={onAcceptRejection}
      />,
    );

    expect(screen.getByText(/框偏移/)).toBeTruthy();
    fireEvent.click(screen.getByText("接受退回开始重做"));
    expect(onAcceptRejection).toHaveBeenCalledTimes(1);
  });

  it("renders content blocked banner with secondary code/request_id receipt and retry button", () => {
    const onRetryContent = vi.fn();
    render(
      <WorkbenchBanners
        {...baseProps}
        contentBlockedReason="原件记录正在被其他操作占用，请稍后重读原图"
        contentBlockedDetail="错误码：storage_original_busy · 请求回执：req-banner-409"
        onRetryContent={onRetryContent}
      />,
    );

    expect(screen.getByText("原件记录正在被其他操作占用，请稍后重读原图")).toBeTruthy();
    expect(
      screen.getByText("错误码：storage_original_busy · 请求回执：req-banner-409"),
    ).toBeTruthy();
    fireEvent.click(screen.getByText("重读原图"));
    expect(onRetryContent).toHaveBeenCalledTimes(1);
  });
});
