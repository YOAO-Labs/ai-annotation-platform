import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { VideoTrackBatchCardContent } from "./VideoTrackBatchCardContent";

const baseProps = {
  count: 3,
  readOnly: false,
  classes: ["car"],
  canMerge: false,
  canJoin: false,
  allLocked: false,
  allHidden: false,
  onChangeClass: vi.fn(),
  onToggleHidden: vi.fn(),
  onToggleLock: vi.fn(),
  onMerge: vi.fn(),
  onJoin: vi.fn(),
  onDelete: vi.fn(),
  onClear: vi.fn(),
};

describe("VideoTrackBatchCardContent · 显隐/锁定切换按钮", () => {
  it("默认态只有「批量隐藏」「批量锁定」各一个，不存在反向按钮", () => {
    render(<VideoTrackBatchCardContent {...baseProps} />);
    expect(screen.getByLabelText("批量隐藏")).toBeInTheDocument();
    expect(screen.getByLabelText("批量锁定")).toBeInTheDocument();
    expect(screen.queryByLabelText("批量显示")).toBeNull();
    expect(screen.queryByLabelText("批量解锁")).toBeNull();
  });

  it("全部已隐藏/已锁定 → 同一个按钮翻转为反向动作", () => {
    render(<VideoTrackBatchCardContent {...baseProps} allHidden allLocked />);
    expect(screen.getByLabelText("批量显示")).toBeInTheDocument();
    expect(screen.getByLabelText("批量解锁")).toBeInTheDocument();
    expect(screen.queryByLabelText("批量隐藏")).toBeNull();
    expect(screen.queryByLabelText("批量锁定")).toBeNull();
    expect(screen.getByLabelText("批量显示")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("批量解锁")).toHaveAttribute("aria-pressed", "true");
  });

  it("点击切换按钮各只发一次 toggle（不再区分 show/hide、lock/unlock 两个入口）", async () => {
    const onToggleHidden = vi.fn();
    const onToggleLock = vi.fn();
    render(
      <VideoTrackBatchCardContent
        {...baseProps}
        onToggleHidden={onToggleHidden}
        onToggleLock={onToggleLock}
      />,
    );
    await userEvent.click(screen.getByLabelText("批量隐藏"));
    await userEvent.click(screen.getByLabelText("批量锁定"));
    expect(onToggleHidden).toHaveBeenCalledTimes(1);
    expect(onToggleLock).toHaveBeenCalledTimes(1);
  });

  it("任务只读 → 锁定按钮禁用，显隐按钮仍可用（显隐是本地视图状态，不写库）", () => {
    render(<VideoTrackBatchCardContent {...baseProps} readOnly />);
    expect(screen.getByLabelText("批量锁定")).toBeDisabled();
    expect(screen.getByLabelText("批量隐藏")).not.toBeDisabled();
  });

  it("多选 AI 入口明确为批量延展", async () => {
    const onBatchTrack = vi.fn();
    render(<VideoTrackBatchCardContent {...baseProps} onBatchTrack={onBatchTrack} />);
    await userEvent.click(screen.getByRole("button", { name: "批量延展轨迹" }));
    expect(onBatchTrack).toHaveBeenCalledTimes(1);
  });
});
