import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { VideoStickyTrackHint } from "./VideoStickyTrackHint";

describe("VideoStickyTrackHint", () => {
  it("当前帧无关键帧 → 提示延展到本帧", () => {
    render(<VideoStickyTrackHint label="#3 car" hasKeyframeAtFrame={false} />);
    const hint = screen.getByTestId("video-sticky-track-hint");
    expect(hint.textContent).toContain("正在延展轨迹 #3 car");
    expect(hint.textContent).toContain("画框延展到本帧");
    expect(hint.textContent).not.toContain("新建物体");
  });

  it("当前帧已有关键帧 → 提示同帧画框新建物体", () => {
    render(<VideoStickyTrackHint label="#3 car" hasKeyframeAtFrame />);
    const hint = screen.getByTestId("video-sticky-track-hint");
    expect(hint.textContent).toContain("本帧已有关键帧, 画框新建物体");
  });
});
