import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  resolveTrackerDefaultModel,
  visibleTrackerModelOptions,
  VideoTrackerPropagateDialog,
} from "./VideoTrackerPropagateDialog";
import { videoDialogMemoryStorageKey } from "../state/videoDialogMemory";

const baseProps = {
  open: true as const,
  maxFrame: 1000,
  nextKeyframeAfter: null,
  submitting: false,
  onCancel: vi.fn(),
};

describe("visibleTrackerModelOptions", () => {
  const has = (opts: Array<{ value: string }>, v: string) => opts.some((o) => o.value === v);

  it("生产构建始终隐藏 mock_bbox (即便没绑后端)", () => {
    expect(has(visibleTrackerModelOptions(false, false), "mock_bbox")).toBe(false);
    expect(has(visibleTrackerModelOptions(true, false), "mock_bbox")).toBe(false);
  });

  it("dev 构建未绑后端时保留 mock_bbox, 绑后端仍过滤", () => {
    expect(has(visibleTrackerModelOptions(false, true), "mock_bbox")).toBe(true);
    expect(has(visibleTrackerModelOptions(true, true), "mock_bbox")).toBe(false);
  });

  it("过滤后仍保留真实模型", () => {
    const prod = visibleTrackerModelOptions(false, false);
    expect(has(prod, "sam2_video")).toBe(true);
    expect(has(prod, "sam3_video_interactive")).toBe(true);
  });
});

describe("VideoTrackerPropagateDialog", () => {
  it("按 项目默认 > 用户记忆 > 首个真实模型 > mock 解析默认模型", () => {
    expect(
      resolveTrackerDefaultModel({
        projectDefaultModel: "sam3_video",
        rememberedModel: "sam2_video",
        preferNonMockModel: true,
      }),
    ).toBe("sam3_video");
    expect(
      resolveTrackerDefaultModel({
        projectDefaultModel: "missing",
        rememberedModel: "sam2_video",
        preferNonMockModel: true,
      }),
    ).toBe("sam2_video");
    expect(
      resolveTrackerDefaultModel({
        projectDefaultModel: null,
        rememberedModel: null,
        preferNonMockModel: true,
      }),
    ).toBe("sam2_video");
    expect(
      resolveTrackerDefaultModel({
        projectDefaultModel: null,
        rememberedModel: null,
        preferNonMockModel: false,
      }),
    ).toBe("mock_bbox");
  });

  it("已绑真实后端 (preferNonMockModel) 时下拉隐藏 mock_bbox, 默认落真实模型", () => {
    window.localStorage.setItem(
      videoDialogMemoryStorageKey("u1", "trackerPropagate"),
      JSON.stringify({
        rangePreset: "30",
        direction: "forward",
        modelKey: "mock_bbox",
        samVariant: "",
      }),
    );
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        userId="u1"
        preferNonMockModel
        onSubmit={vi.fn()}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    const values = Array.from(modelSelect.options).map((o) => o.value);
    expect(values).not.toContain("mock_bbox");
    expect(modelSelect.value).toBe("sam2_video");
  });

  it("未绑后端 / 测试环境仍保留 mock_bbox 可见", () => {
    render(<VideoTrackerPropagateDialog {...baseProps} frameIndex={50} onSubmit={vi.fn()} />);
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    const values = Array.from(modelSelect.options).map((o) => o.value);
    expect(values).toContain("mock_bbox");
  });

  it("上报当前影响范围给时间轴高亮, 随方向/预设变化更新, 关闭清空", () => {
    const onRangeChange = vi.fn();
    const { rerender } = render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        samplingStep={1}
        onRangeChange={onRangeChange}
        onSubmit={vi.fn()}
      />,
    );
    expect(onRangeChange).toHaveBeenLastCalledWith({ startFrame: 50, endFrame: 80 });
    fireEvent.click(screen.getByTestId("tracker-direction-backward"));
    expect(onRangeChange).toHaveBeenLastCalledWith({ startFrame: 20, endFrame: 50 });
    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        open={false}
        frameIndex={50}
        onRangeChange={onRangeChange}
        onSubmit={vi.fn()}
      />,
    );
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
  });

  it("后台保留的追踪面板不发布范围，显示后恢复并跟随当前帧", () => {
    const onRangeChange = vi.fn();
    const onSubmit = vi.fn();
    const view = (visible: boolean, frameIndex: number) => (
      <VideoTrackerPropagateDialog
        {...baseProps}
        visible={visible}
        frameIndex={frameIndex}
        onRangeChange={onRangeChange}
        onSubmit={onSubmit}
      />
    );
    const { rerender, unmount } = render(view(false, 50));
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
    rerender(view(true, 50));
    expect(onRangeChange).toHaveBeenLastCalledWith({ startFrame: 50, endFrame: 80 });
    rerender(view(false, 50));
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
    rerender(view(false, 70));
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
    rerender(view(true, 70));
    expect(onRangeChange).toHaveBeenLastCalledWith({ startFrame: 70, endFrame: 100 });
    unmount();
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("隐藏范围预览不清空自定义范围或方向", () => {
    const onRangeChange = vi.fn();
    const onSubmit = vi.fn();
    const brushedRange = { startFrame: 10, endFrame: 42 };
    const view = (visible: boolean, frameIndex: number, brushed = true) => (
      <VideoTrackerPropagateDialog
        {...baseProps}
        visible={visible}
        frameIndex={frameIndex}
        brushedRange={brushed ? brushedRange : null}
        onRangeChange={onRangeChange}
        onSubmit={onSubmit}
      />
    );
    const { rerender } = render(view(true, 50, false));
    fireEvent.click(screen.getByTestId("tracker-direction-backward"));
    rerender(view(true, 50));
    expect(onRangeChange).toHaveBeenLastCalledWith(brushedRange);
    rerender(view(false, 70));
    expect(onRangeChange).toHaveBeenLastCalledWith(null);
    rerender(view(true, 70));
    expect(onRangeChange).toHaveBeenLastCalledWith(brushedRange);
    expect(screen.getByTestId("tracker-direction-backward")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByTestId("tracker-range-custom")).toBeInTheDocument();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("时间轴刷选回填自定义范围, 覆盖预设; 改预设即回派生范围", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const onRangeChange = vi.fn();
    const { rerender } = render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        samplingStep={1}
        brushedRange={null}
        onRangeChange={onRangeChange}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByText("F50 → F80")).toBeTruthy();
    expect(screen.queryByTestId("tracker-range-custom")).toBeNull();

    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        samplingStep={1}
        brushedRange={{ startFrame: 10, endFrame: 42 }}
        onRangeChange={onRangeChange}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByText("F10 → F42")).toBeTruthy();
    expect(screen.getByTestId("tracker-range-custom")).toBeInTheDocument();
    expect(onRangeChange).toHaveBeenLastCalledWith({ startFrame: 10, endFrame: 42 });
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ from_frame: 10, to_frame: 42 }),
    );

    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "60" } });
    expect(screen.getByText("F50 → F110")).toBeTruthy();
    expect(screen.queryByTestId("tracker-range-custom")).toBeNull();
  });

  it("closed 时不渲染", () => {
    render(
      <VideoTrackerPropagateDialog {...baseProps} open={false} frameIndex={5} onSubmit={vi.fn()} />,
    );
    expect(screen.queryByTestId("video-tracker-propagate-dialog")).toBeNull();
  });

  it("作为 Dockview 内容填满面板，并与 AI 单题共用内部视觉骨架", () => {
    render(<VideoTrackerPropagateDialog {...baseProps} frameIndex={5} onSubmit={vi.fn()} />);

    const panel = screen.getByTestId("video-tracker-propagate-dialog");
    expect(panel.className).toContain("h-full");
    expect(panel.className).toContain("border-violet-500/35");
    expect(panel.className).toContain("rounded-none");
    expect(screen.queryByTestId("tracker-panel-resize-handle")).toBeNull();

    const header = screen.getByTestId("tracker-panel-header");
    expect(header.className).toContain("from-violet-500/10");
    expect(header.className).toContain("px-3.5");
    expect(header.className).toContain("py-3");

    const settings = screen.getByTestId("tracker-settings-section");
    expect(settings.className).toContain("bg-muted");
    expect(settings.className).toContain("px-3.5");
    expect(settings.className).toContain("py-2.5");

    expect(screen.getByRole("button", { name: "开始延展" }).className).toContain(
      "border-violet-500/30",
    );
  });

  it("隐藏时保留表单且不响应 Escape", () => {
    const onCancel = vi.fn();
    const { rerender } = render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={5}
        onCancel={onCancel}
        onSubmit={vi.fn()}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledOnce();

    onCancel.mockClear();
    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        visible={false}
        frameIndex={5}
        onCancel={onCancel}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByTestId("video-tracker-propagate-dialog")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("step===1 (采样关闭): 预设标签为「N 帧」, range 用 F{from}→F{to}, 提交源帧", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        samplingStep={1}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByText("30 帧")).toBeTruthy();
    expect(screen.getByText("F50 → F80")).toBeTruthy();
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ from_frame: 50, to_frame: 80, direction: "forward" }),
    );
  });

  it("submits the explicitly selected mask geometry", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={20}
        preferNonMockModel
        onSubmit={onSubmit}
      />,
    );
    fireEvent.change(screen.getByTestId("tracker-output-geometry"), { target: { value: "mask" } });
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ output_geometry: "mask" }));
  });

  it("step>1 (采样开启): 预设语义为网格格子, span 乘 step, range 显示网格序号", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={100}
        samplingStep={10}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.getByText("30 格 (≈300 帧)")).toBeTruthy();
    expect(screen.getByText("G10 → G40 (F100 → F400)")).toBeTruthy();
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ from_frame: 100, to_frame: 400, direction: "forward" }),
    );
  });

  it("step>1 + backward: span 向前乘 step", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={500}
        samplingStep={10}
        onSubmit={onSubmit}
      />,
    );
    fireEvent.click(screen.getByTestId("tracker-direction-backward"));
    expect(screen.getByText("G20 → G50 (F200 → F500)")).toBeTruthy();
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ from_frame: 200, to_frame: 500, direction: "backward" }),
    );
  });

  it("到结尾 / 到下一关键帧 预设不受 step 影响 (标签固定)", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={100}
        samplingStep={10}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByText("到结尾")).toBeTruthy();
    expect(screen.getByText("到下一关键帧")).toBeTruthy();
  });

  it("提交成功后记住范围 / 方向 / 模型 / 变体,重开作为初值", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={80}
        userId="u1"
        onSubmit={onSubmit}
      />,
    );

    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "60" } });
    fireEvent.click(screen.getByTestId("tracker-direction-backward"));
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "sam2_video" },
    });
    fireEvent.change(screen.getAllByRole("combobox")[2], {
      target: { value: "large" },
    });
    fireEvent.click(screen.getByText("开始延展"));

    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        from_frame: 20,
        to_frame: 80,
        direction: "backward",
        model_key: "sam2_video",
        sam_variant: "large",
      }),
    );
    await waitFor(() =>
      expect(
        window.localStorage.getItem(videoDialogMemoryStorageKey("u1", "trackerPropagate")),
      ).toContain("sam2_video"),
    );

    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        open={false}
        frameIndex={80}
        userId="u1"
        onSubmit={onSubmit}
      />,
    );
    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={80}
        userId="u1"
        onSubmit={onSubmit}
      />,
    );

    expect(screen.getByText("F20 → F80")).toBeTruthy();
    expect((screen.getAllByRole("combobox")[1] as HTMLSelectElement).value).toBe("sam2_video");
    expect((screen.getAllByRole("combobox")[2] as HTMLSelectElement).value).toBe("large");
  });

  it("SAM 尺寸档位只对 sam2_video 显示, sam3 系隐藏且提交不带 sam_variant", () => {
    const onSubmit = vi.fn();
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        projectDefaultModel="sam2_video"
        preferNonMockModel
        onSubmit={onSubmit}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    expect(modelSelect.value).toBe("sam2_video");
    expect(screen.queryByText("尺寸")).toBeTruthy();
    fireEvent.change(modelSelect, { target: { value: "sam3_video_interactive" } });
    expect(screen.queryByText("尺寸")).toBeNull();
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ model_key: "sam3_video_interactive" }),
    );
    expect(onSubmit.mock.calls[0][0].sam_variant).toBeUndefined();
  });

  it("画布级发现中项目默认模型优先于用户记忆", async () => {
    window.localStorage.setItem(
      videoDialogMemoryStorageKey("u1", "trackerPropagate"),
      JSON.stringify({
        rangePreset: "30",
        direction: "forward",
        modelKey: "mock_bbox",
        samVariant: "",
      }),
    );
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={80}
        userId="u1"
        sourceless
        projectDefaultModel="sam3_video"
        onSubmit={vi.fn()}
      />,
    );

    expect((screen.getAllByRole("combobox")[1] as HTMLSelectElement).value).toBe("sam3_video");
  });

  it("未由项目后端声明的模型不出现在下拉中", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceless
        projectDefaultModel="sam3_video"
        supportedTrackers={[]}
        onSubmit={vi.fn()}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    const values = Array.from(modelSelect.options).map((option) => option.value);
    expect(values).not.toContain("sam3_video");
    expect(values).not.toContain("sam2_video");
    expect(values).toEqual(["mock_bbox"]);
  });

  it("backend 声明 sam3_video 后可选, 显 text 框, 提交带 text", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceless
        projectDefaultModel="sam3_video"
        supportedTrackers={["sam2_video", "sam3_video"]}
        textDrivenTrackers={["sam3_video"]}
        trackerModelProviders={{ sam3_video: ["SAM3 GPU"] }}
        availableClasses={["car"]}
        onSubmit={onSubmit}
      />,
    );
    expect((screen.getAllByRole("combobox")[1] as HTMLSelectElement).value).toBe("sam3_video");
    expect(screen.getByRole("option", { name: /SAM3 GPU/ })).toBeInTheDocument();
    const textInput = screen.getByTestId("tracker-text-input") as HTMLInputElement;
    expect(textInput).toBeTruthy();
    fireEvent.click(screen.getByText("开始发现"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("文本驱动追踪需填写文本描述")).toBeTruthy();
    fireEvent.change(textInput, { target: { value: "the red car" } });
    fireEvent.click(screen.getByText("开始发现"));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith(
        expect.objectContaining({ model_key: "sam3_video", text: "the red car" }),
      ),
    );
  });

  it("seed-bbox tracker (sam2_video) 不显 text 框, 提交不带 text", () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        projectDefaultModel="sam2_video"
        supportedTrackers={["sam2_video"]}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.queryByTestId("tracker-text-input")).toBeNull();
    fireEvent.click(screen.getByText("开始延展"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ model_key: "sam2_video", text: undefined }),
    );
  });

  it("种子入口: sam3_video_interactive 与 sam2_video 都显示, mock/文本驱动不显示", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        onSubmit={vi.fn()}
        onToggleSeedCollecting={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("tracker-seed-toggle")).toBeNull();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "sam3_video_interactive" },
    });
    expect(screen.getByTestId("tracker-seed-toggle")).toBeTruthy();
    fireEvent.change(screen.getAllByRole("combobox")[1], {
      target: { value: "sam2_video" },
    });
    expect(screen.getByTestId("tracker-seed-toggle")).toBeTruthy();
  });

  it("点「落点选目标」调 onToggleSeedCollecting; 采集态改文案; 有落点显计数 + 清空", () => {
    const onToggle = vi.fn();
    const onClear = vi.fn();
    const seedProps = {
      ...baseProps,
      frameIndex: 50,
      projectDefaultModel: "sam3_video_interactive",
      sourceless: true,
      onSubmit: vi.fn(),
      onToggleSeedCollecting: onToggle,
      onClearSeeds: onClear,
    };
    const { rerender } = render(<VideoTrackerPropagateDialog {...seedProps} />);
    const toggle = screen.getByTestId("tracker-seed-toggle");
    expect(toggle.textContent).toContain("落点选目标");
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalled();
    rerender(
      <VideoTrackerPropagateDialog
        {...seedProps}
        seedCollecting
        seedPointCount={2}
        seedTargets={[{ targetId: 1, pointCount: 2, boxCount: 0, frames: [50] }]}
      />,
    );
    expect(screen.getByTestId("tracker-seed-toggle").textContent).toContain("落点中");
    expect(screen.getByTestId("tracker-seed-count").textContent).toContain("目标 1");
    expect(screen.getByTestId("tracker-seed-count").textContent).toContain("2 点");
    expect(screen.getByTestId("tracker-seed-count").textContent).toContain("帧 F50");
    fireEvent.click(screen.getByText("清空"));
    expect(onClear).toHaveBeenCalled();
  });

  it("多目标: 每个目标逐行显示点、框、所在帧和当前归属", () => {
    const onNewTarget = vi.fn();
    const seedProps = {
      ...baseProps,
      frameIndex: 50,
      projectDefaultModel: "sam3_video_interactive",
      sourceless: true,
      onSubmit: vi.fn(),
      onToggleSeedCollecting: vi.fn(),
      onNewSeedTarget: onNewTarget,
      seedPointCount: 1,
      seedTargets: [{ targetId: 1, pointCount: 1, boxCount: 0, frames: [50] }],
    };
    const { rerender } = render(<VideoTrackerPropagateDialog {...seedProps} />);
    expect(screen.getByTestId("tracker-seed-target-1").textContent).toContain("帧 F50");
    fireEvent.click(screen.getByTestId("tracker-seed-new-target"));
    expect(onNewTarget).toHaveBeenCalled();
    rerender(<VideoTrackerPropagateDialog {...seedProps} activeSeedTargetId={2} />);
    expect(screen.getByTestId("tracker-seed-target-2").textContent).toContain("等待在画布添加种子");
    expect(screen.getByTestId("tracker-seed-target-2").textContent).toContain("当前");
    rerender(
      <VideoTrackerPropagateDialog
        {...seedProps}
        seedPointCount={3}
        seedBoxCount={1}
        activeSeedTargetId={2}
        seedTargets={[
          { targetId: 1, pointCount: 1, boxCount: 0, frames: [50] },
          { targetId: 2, pointCount: 2, boxCount: 1, frames: [54, 58] },
        ]}
      />,
    );
    const target2 = screen.getByTestId("tracker-seed-target-2").textContent ?? "";
    expect(target2).toContain("2 点");
    expect(target2).toContain("1 框");
    expect(target2).toContain("帧 F54、F58");
  });

  it("画布级种子模型没有点或框时不提交", () => {
    const onSubmit = vi.fn();
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceless
        availableClasses={["car"]}
        projectDefaultModel="sam3_video_interactive"
        supportedTrackers={["sam3_video_interactive"]}
        onSubmit={onSubmit}
        onToggleSeedCollecting={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByText("开始发现"));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("需先在画布添加点或框种子");
  });

  it("取消不写记忆,且非法模型 / 变体安全回退", () => {
    const key = videoDialogMemoryStorageKey("u1", "trackerPropagate");
    const remembered = {
      rangePreset: "60",
      direction: "bidirectional",
      modelKey: "missing-model",
      samVariant: "huge",
    };
    window.localStorage.setItem(key, JSON.stringify(remembered));
    render(
      <VideoTrackerPropagateDialog {...baseProps} frameIndex={80} userId="u1" onSubmit={vi.fn()} />,
    );

    expect(screen.getByText("F20 → F140")).toBeTruthy();
    expect((screen.getAllByRole("combobox")[1] as HTMLSelectElement).value).toBe("mock_bbox");
    fireEvent.click(screen.getByTestId("tracker-direction-backward"));
    fireEvent.click(screen.getByTestId("video-tracker-propagate-dialog"));

    expect(window.localStorage.getItem(key)).toBe(JSON.stringify(remembered));
  });

  it("B-combo · 发现追踪需双 sam3 能力, 提交带 model_key + text + 目标类别", () => {
    const onSubmit = vi.fn();
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={0}
        sourceless
        supportedTrackers={["sam3_video", "sam3_video_interactive"]}
        textDrivenTrackers={["sam3_video"]}
        availableClasses={["car", "person"]}
        onSubmit={onSubmit}
        onToggleSeedCollecting={vi.fn()}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    const comboOption = Array.from(modelSelect.options).find(
      (o) => o.value === "sam3_video_combo",
    ) as HTMLOptionElement;
    expect(comboOption.disabled).toBe(false);
    fireEvent.change(modelSelect, { target: { value: "sam3_video_combo" } });
    fireEvent.change(screen.getByTestId("tracker-text-input"), { target: { value: "car" } });
    expect((screen.getByTestId("tracker-target-class") as HTMLSelectElement).value).toBe("car");
    fireEvent.click(screen.getByText("开始发现"));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({
        model_key: "sam3_video_combo",
        text: "car",
        target_class_name: "car",
        target_tool_unit_id: "bbox",
      }),
    );
  });

  it("B-combo · 缺任一 sam3 能力时不显示组合模型", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={0}
        sourceless
        supportedTrackers={["sam3_video"]}
        textDrivenTrackers={["sam3_video"]}
        onSubmit={vi.fn()}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    expect(Array.from(modelSelect.options).map((option) => option.value)).not.toContain(
      "sam3_video_combo",
    );
  });

  it("U1: 方向按钮用消歧标签 (更晚/更早帧) + testid", () => {
    render(<VideoTrackerPropagateDialog {...baseProps} frameIndex={50} onSubmit={vi.fn()} />);
    const legend = screen.getByText("追踪方向");
    expect(legend.className).toContain("leading-normal");
    expect(legend.closest("fieldset")?.className).toContain("p-0");
    expect(screen.getByTestId("tracker-direction-forward").textContent).toContain("更晚帧");
    expect(screen.getByTestId("tracker-direction-backward").textContent).toContain("更早帧");
    expect(screen.getByTestId("tracker-direction-bidirectional").textContent).toContain("双向");
  });

  it("U6: 大范围显示 ≈N 窗 (sam3 系 16 帧/窗), 小范围不显", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={0}
        projectDefaultModel="sam3_video_interactive"
        onSubmit={vi.fn()}
        onToggleSeedCollecting={vi.fn()}
      />,
    );
    expect(screen.getByTestId("tracker-window-estimate").textContent).toContain("≈2 窗");
    fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "10" } });
    expect(screen.queryByTestId("tracker-window-estimate")).toBeNull();
  });

  it("框修正: 点/框模式切换调 onChangeSeedMode; 框计数并入「已落」文案", () => {
    const onChangeMode = vi.fn();
    const seedProps = {
      ...baseProps,
      frameIndex: 50,
      projectDefaultModel: "sam3_video_interactive",
      onSubmit: vi.fn(),
      onToggleSeedCollecting: vi.fn(),
      onChangeSeedMode: onChangeMode,
    };
    const { rerender } = render(<VideoTrackerPropagateDialog {...seedProps} seedMode="point" />);
    fireEvent.click(screen.getByTestId("tracker-seed-mode-box"));
    expect(onChangeMode).toHaveBeenCalledWith("box");
    rerender(<VideoTrackerPropagateDialog {...seedProps} seedMode="box" />);
    expect(screen.getByTestId("tracker-seed-toggle").textContent).toContain("画框选目标");
    rerender(
      <VideoTrackerPropagateDialog
        {...seedProps}
        seedMode="box"
        seedPointCount={1}
        seedBoxCount={2}
        seedTargets={[{ targetId: 1, pointCount: 1, boxCount: 2, frames: [50, 55] }]}
      />,
    );
    const count = screen.getByTestId("tracker-seed-count").textContent ?? "";
    expect(count).toContain("1 点");
    expect(count).toContain("2 框");
    expect(count).toContain("帧 F50、F55");
  });

  it("A2/A3 · 本次影响摘要: 单纯延展显示源类别", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceTrackClassName="car"
        onSubmit={vi.fn()}
      />,
    );
    const summary = screen.getByTestId("tracker-impact-summary").textContent ?? "";
    expect(summary).toContain("延展当前轨迹");
    expect(summary).toContain("car");
  });

  it("单轨延展不混入画布级新目标语义", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        projectDefaultModel="sam3_video_interactive"
        sourceTrackClassName="car"
        onSubmit={vi.fn()}
        onToggleSeedCollecting={vi.fn()}
      />,
    );
    expect(screen.getByTestId("tracker-impact-summary").textContent).toBe("延展当前轨迹「car」");
    expect(screen.queryByTestId("tracker-seed-new-target")).toBeNull();
  });

  it("M2 · 多选批量: 摘要显示延展 N 条轨迹 (单类展示类名)", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceCount={3}
        sourceClassNames={["car"]}
        onSubmit={vi.fn()}
      />,
    );
    const summary = screen.getByTestId("tracker-impact-summary").textContent ?? "";
    expect(summary).toContain("延展 3 条轨迹");
    expect(summary).toContain("car");
  });

  it("M2 · 多选批量混类: 摘要显示「N 类」", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        sourceCount={2}
        sourceClassNames={["car", "person"]}
        onSubmit={vi.fn()}
      />,
    );
    const summary = screen.getByTestId("tracker-impact-summary").textContent ?? "";
    expect(summary).toContain("延展 2 条轨迹");
    expect(summary).toContain("2 类");
  });

  it("单轨延展保留可用的 sam3 文本模型", () => {
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        projectDefaultModel="sam3_video"
        supportedTrackers={["sam3_video"]}
        textDrivenTrackers={["sam3_video"]}
        sourceTrackClassName="car"
        onSubmit={vi.fn()}
      />,
    );
    const modelSelect = screen.getAllByRole("combobox")[1] as HTMLSelectElement;
    expect(Array.from(modelSelect.options).map((option) => option.value)).toContain("sam3_video");
    expect(modelSelect.value).toBe("sam3_video");
    expect(screen.getByTestId("tracker-impact-summary").textContent).toContain("延展当前轨迹");
  });

  it("U8: tracking 态就地转作用域进度视图并隐藏表单", () => {
    render(
      <VideoTrackerPropagateDialog {...baseProps} frameIndex={50} tracking onSubmit={vi.fn()} />,
    );
    expect(screen.getByTestId("tracker-progress").textContent).toContain("正在延展轨迹");
    expect(screen.queryByText("开始延展")).toBeNull();
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByTestId("video-tracker-propagate-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("tracker-panel-header").className).toContain("from-violet-500/10");
  });

  it("U8: 有分窗进度时显示「第 c/t 窗」; 单窗 (total<=1) 不显", () => {
    const { rerender } = render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        tracking
        trackingWindow={{ current: 2, total: 5 }}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByTestId("tracker-progress-window").textContent).toContain("第 2/5 窗");
    rerender(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        tracking
        trackingWindow={{ current: 1, total: 1 }}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("tracker-progress-window")).toBeNull();
  });

  it("U8: 进度态点「后台继续」调 onCancel (关闭对话框, 后台继续追踪)", () => {
    const onCancel = vi.fn();
    render(
      <VideoTrackerPropagateDialog
        {...baseProps}
        frameIndex={50}
        tracking
        onCancel={onCancel}
        onSubmit={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByText("后台继续"));
    expect(onCancel).toHaveBeenCalled();
  });
});
