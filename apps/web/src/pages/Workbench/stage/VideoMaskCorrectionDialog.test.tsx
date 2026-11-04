import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  correctionWindow,
  VideoMaskCorrectionDialog,
  type VideoMaskCorrectionModel,
} from "./VideoMaskCorrectionDialog";

const nativeModel: VideoMaskCorrectionModel = {
  backendId: "backend-native",
  modelKey: "sam3_video_interactive",
  modelId: "sam3-video-interactive-tracker",
  nativeMask: true,
  textRequired: false,
  maxWindowFrames: 16,
};

const fallbackModel: VideoMaskCorrectionModel = {
  backendId: "backend-fallback",
  modelKey: "sam3_video",
  modelId: "sam3-video-tracker",
  nativeMask: false,
  textRequired: true,
  maxWindowFrames: 16,
};

function renderDialog(
  overrides: Partial<React.ComponentProps<typeof VideoMaskCorrectionDialog>> = {},
) {
  const props: React.ComponentProps<typeof VideoMaskCorrectionDialog> = {
    open: true,
    frameIndex: 50,
    minFrame: 40,
    maxFrame: 80,
    segmentId: "segment-1",
    models: [nativeModel],
    submitting: false,
    onOpenChange: vi.fn(),
    onSubmit: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
  render(<VideoMaskCorrectionDialog {...props} />);
  return props;
}

describe("correctionWindow", () => {
  it("按 segment 与模型单窗上限截断四种模式", () => {
    expect(correctionWindow("save_only", 50, 80, 15, 40)).toEqual({
      fromFrame: 50,
      toFrame: 50,
    });
    expect(correctionWindow("backward", 50, 80, 15, 40)).toEqual({
      fromFrame: 40,
      toFrame: 50,
    });
    expect(correctionWindow("forward", 50, 80, 15, 40)).toEqual({
      fromFrame: 50,
      toFrame: 65,
    });
    expect(correctionWindow("bidirectional", 50, 80, 15, 40)).toEqual({
      fromFrame: 40,
      toFrame: 65,
    });
  });
});

describe("VideoMaskCorrectionDialog", () => {
  it("原生 PVS 使用 16 帧单窗并提交冻结的 segment", async () => {
    const props = renderDialog();

    fireEvent.click(screen.getByRole("radio", { name: "更晚帧 →" }));
    expect(screen.getByText(/生效窗口 F50–F65/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存并启动传播" }));

    await waitFor(() =>
      expect(props.onSubmit).toHaveBeenCalledWith({
        mode: "forward",
        direction: "forward",
        fromFrame: 50,
        toFrame: 65,
        modelKey: "sam3_video_interactive",
        modelId: "sam3-video-interactive-tracker",
        backendId: "backend-native",
        allowBboxFallback: false,
        text: undefined,
        segmentId: "segment-1",
      }),
    );
  });

  it("分段边界禁用无效方向，未加载 segment 时阻止传播", () => {
    renderDialog({ frameIndex: 40, segmentId: undefined });

    expect(screen.getByRole("radio", { name: "← 更早帧" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "⇆ 双向" })).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: "更晚帧 →" }));
    expect(screen.getByText("尚未取得当前视频分段")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存并启动传播" })).toBeDisabled();
  });

  it("模型单窗只有种子帧时禁用传播", () => {
    renderDialog({ models: [{ ...nativeModel, maxWindowFrames: 1 }] });

    expect(screen.getByRole("radio", { name: "← 更早帧" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "更晚帧 →" })).toBeDisabled();
    expect(screen.getByRole("radio", { name: "⇆ 双向" })).toBeDisabled();
  });

  it("bbox fallback 必须填写文本并显式确认", async () => {
    const props = renderDialog({ models: [fallbackModel] });

    fireEvent.click(screen.getByRole("radio", { name: "更晚帧 →" }));
    const submit = screen.getByRole("button", { name: "保存并启动传播" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("目标文本"), {
      target: { value: "red car" },
    });
    fireEvent.click(screen.getByLabelText(/我确认使用 bbox seed 降级/));
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() =>
      expect(props.onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({
          modelKey: "sam3_video",
          allowBboxFallback: true,
          text: "red car",
        }),
      ),
    );
  });

  it("创建失败后明确只重试传播，提交中禁止关闭", () => {
    const props = renderDialog({
      keyframeSaved: true,
      createError: "broker unavailable",
      submitting: true,
    });

    expect(screen.getByText("人工纠错帧已保存")).toBeInTheDocument();
    expect(screen.getByText(/不会重复保存当前帧/)).toHaveTextContent(
      "上次失败：broker unavailable",
    );
    expect(screen.getByRole("button", { name: "启动中…" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "关闭" })).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(props.onOpenChange).not.toHaveBeenCalled();
  });

  it("版本或 digest 冲突后禁止使用旧快照盲重试", () => {
    renderDialog({
      keyframeSaved: true,
      createError: "source_version_conflict",
      createRetryable: false,
    });

    expect(screen.getByText(/关闭后刷新标注/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "仅保存" })).toBeDisabled();
  });
});
