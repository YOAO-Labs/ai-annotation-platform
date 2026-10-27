import { fireEvent, render, screen } from "@testing-library/react";
import { useState, type ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import type { PreannotateConfig } from "@/pages/AIPreAnnotate/components/usePreannotateConfig";
import type { AiBox } from "../state/transforms";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";

vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: count }, (_, index) => ({
        index,
        start: index * 68,
        size: 68,
        key: index,
      })),
    getTotalSize: () => count * 68,
    measureElement: () => {},
  }),
}));

vi.mock("../stage/BoxListItem", () => ({
  BoxListItem: ({ b, onAccept }: { b: AiBox; onAccept?: () => void }) => (
    <div data-testid={`candidate-row-${b.id}`}>
      {b.cls}
      <button type="button" onClick={onAccept} data-testid={`candidate-row-accept-${b.id}`}>
        行内接受
      </button>
    </div>
  ),
}));

vi.mock("@/pages/AIPreAnnotate/components/PreannotateConfigForm", () => ({
  PreannotateConfigForm: function ConfigForm({ cfg }: { cfg: PreannotateConfig }) {
    const [presetName, setPresetName] = useState("");
    return (
      <div data-testid="preannotate-config-form">
        <label>
          高级提示词
          <input value={cfg.prompt} onChange={(event) => cfg.setPrompt(event.target.value)} />
        </label>
        <label>
          预设名
          <input value={presetName} onChange={(event) => setPresetName(event.target.value)} />
        </label>
      </div>
    );
  },
}));

import { AIInspectorPanel, AIPredictionPopover } from "./AIInspectorPanel";

type PopoverProps = ComponentProps<typeof AIPredictionPopover>;
type Request = NonNullable<PopoverProps["request"]>;
type InspectorProps = ComponentProps<typeof AIInspectorPanel>;

function config(overrides: Partial<PreannotateConfig> = {}): PreannotateConfig {
  return {
    backendId: "backend-a",
    configReady: true,
    prompt: "car",
    setPrompt: vi.fn(),
    isDocMode: false,
    isGeometricBackend: false,
    sourceBatchableWarning: null,
    ...overrides,
  } as PreannotateConfig;
}

function request(overrides: Partial<Request> = {}): Request {
  return {
    status: "idle",
    summary: null,
    progressPct: null,
    error: null,
    canCancel: false,
    cancelling: false,
    canRetry: false,
    ...overrides,
  };
}

function runningRequest(overrides: Partial<Request> = {}): Request {
  return request({
    status: "running",
    summary: {
      projectId: "project-original",
      taskId: "task-original",
      frameIndex: 4,
      backendName: "Original backend",
      modelName: "Original model",
      input: {
        prompt: "original prompt",
        task_type: "segmentation",
        model_variants: { size: "large" },
        output_mode: "mask",
        params: { internal_option: "diagnostic-only" },
      },
    },
    canCancel: true,
    progressPct: 42,
    ...overrides,
  });
}

function popoverProps(overrides: Partial<PopoverProps> = {}): PopoverProps {
  return {
    aiModel: "Current model",
    aiRunning: false,
    aiBoxCount: 0,
    confThreshold: 0.5,
    aiTakeoverRate: 40,
    onClose: vi.fn(),
    onRunAi: vi.fn(),
    onAcceptAll: vi.fn(),
    onSetConfThreshold: vi.fn(),
    onRetryRequest: vi.fn(),
    onCancelRequest: vi.fn(),
    onReviewCandidates: vi.fn(),
    cfg: config(),
    ...overrides,
  };
}

function candidate(id = "candidate-1"): AiBox {
  return {
    id,
    predictionId: `prediction-${id}`,
    shapeIndex: 0,
    annotation_type: "bbox",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    x: 0.1,
    y: 0.1,
    w: 0.2,
    h: 0.2,
    cls: "car",
    conf: 0.9,
    source: "prediction_based",
    predictionSource: "ml_backend",
    attributes: { color: "blue" },
  };
}

function inspectorProps(overrides: Partial<InspectorProps> = {}): InspectorProps {
  return {
    open: true,
    width: 300,
    onResize: vi.fn(),
    aiBoxes: [candidate()],
    userBoxes: [],
    selectedId: "candidate-1",
    imageWidth: 800,
    imageHeight: 600,
    attributeSchema: {
      fields: [{ key: "color", label: "颜色", type: "text" }],
    },
    onSelect: vi.fn(),
    onAcceptPrediction: vi.fn(),
    onRejectPrediction: vi.fn(),
    onClearSelection: vi.fn(),
    onDeleteUserBox: vi.fn(),
    ...overrides,
  };
}

describe("AIPredictionPopover phases", () => {
  it("keeps panel names and run actions consistent with the image/video scope", () => {
    const props = popoverProps();
    const { rerender } = render(<AIPredictionPopover {...props} />);
    expect(screen.getByText("单图 AI")).toBeVisible();
    expect(screen.getByRole("button", { name: "关闭单图 AI" })).toBeVisible();
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent("运行当前图片");
    rerender(<AIPredictionPopover {...props} isVideoTask />);
    expect(screen.getByText("单帧 AI")).toBeVisible();
    expect(screen.getByRole("button", { name: "关闭单帧 AI" })).toBeVisible();
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent("运行当前帧");
  });

  it("gates pipelines independently of the selected single-task backend", () => {
    const props = popoverProps({
      runUnavailableReason: "当前后端离线",
      hasProjectPipeline: true,
      projectPipelineRunnable: true,
      onRunPipeline: vi.fn(),
    });
    const { rerender } = render(<AIPredictionPopover {...props} />);
    const pipeline = screen.getByTestId("ai-prediction-run-pipeline");
    expect(pipeline).toBeEnabled();
    fireEvent.click(pipeline);
    expect(props.onRunPipeline).toHaveBeenCalledOnce();
    rerender(
      <AIPredictionPopover
        {...props}
        projectPipelineRunnable={false}
        pipelineMissingBackendCount={1}
      />,
    );
    expect(pipeline).toBeDisabled();
    fireEvent.click(pipeline);
    expect(props.onRunPipeline).toHaveBeenCalledOnce();
  });

  it("blocks new inference offline without blocking running requests or candidate review", () => {
    const props = popoverProps({ runUnavailableReason: "当前 AI 后端离线" });
    const { rerender } = render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(props.onRunAi).not.toHaveBeenCalled();
    rerender(<AIPredictionPopover {...props} request={runningRequest()} />);
    expect(screen.getByRole("button", { name: "取消本次请求" })).toBeEnabled();
    rerender(
      <AIPredictionPopover {...props} request={request({ status: "completed" })} aiBoxCount={2} />,
    );
    expect(screen.getByTestId("ai-prediction-run-next")).toBeDisabled();
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(props.onReviewCandidates).toHaveBeenCalledOnce();
    rerender(<AIPredictionPopover {...props} runUnavailableReason={undefined} />);
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeEnabled();
  });

  it("shows idle input and a single run action while advanced configuration stays mounted", () => {
    const props = popoverProps({ request: request() });
    render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "idle");
    expect(screen.getByTestId("ai-prediction-popover")).toHaveAttribute(
      "data-workbench-ai-toolbar",
    );
    const prompt = screen.getByRole("textbox", { name: "提示词" });
    expect(prompt).toBeVisible();
    fireEvent.change(prompt, { target: { value: "person" } });
    expect(props.cfg.setPrompt).toHaveBeenCalledWith("person");
    expect(screen.getByTestId("preannotate-config-form")).not.toBeVisible();
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent("运行当前图片");
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(props.onRunAi).toHaveBeenCalledTimes(1);
    expect(props.onAcceptAll).not.toHaveBeenCalled();
  });

  it("uses the owned running snapshot even as the next configuration changes", () => {
    const active = runningRequest();
    const props = popoverProps({ request: active, aiBoxCount: 3 });
    const { rerender } = render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "running");
    expect(screen.getByRole("progressbar", { name: "本次请求进度" })).toHaveAttribute(
      "aria-valuenow",
      "42",
    );
    const summary = screen.getByTestId("ai-request-summary");
    expect(summary).toHaveTextContent("Original model");
    expect(summary).toHaveTextContent("Original backend");
    expect(summary).toHaveTextContent("task-original");
    expect(summary).toHaveTextContent("当前帧 F4");
    expect(summary).toHaveTextContent("original prompt");
    expect(summary).toHaveTextContent("尺寸：large");
    expect(summary).not.toHaveTextContent("diagnostic-only");

    fireEvent.click(screen.getByTestId("ai-prediction-advanced-toggle"));
    fireEvent.change(screen.getByRole("textbox", { name: "高级提示词" }), {
      target: { value: "next prompt" },
    });
    expect(props.cfg.setPrompt).toHaveBeenCalledWith("next prompt");
    rerender(
      <AIPredictionPopover
        {...props}
        aiModel="Next model"
        cfg={config({ prompt: "next prompt" })}
        isVariantWarm={false}
      />,
    );
    expect(screen.getByTestId("ai-request-summary")).toHaveTextContent("Original model");
    expect(screen.getByTestId("ai-request-summary")).toHaveTextContent("original prompt");
    expect(screen.getByTestId("ai-request-summary")).not.toHaveTextContent("Next model");
    expect(screen.getByTestId("ai-request-summary")).not.toHaveTextContent("next prompt");
    expect(screen.getByTestId("ai-prediction-run-next")).toBeDisabled();
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(props.onCancelRequest).not.toHaveBeenCalled();
    expect(props.onRetryRequest).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(props.onCancelRequest).toHaveBeenCalledTimes(1);
  });

  it("preserves the one form instance and its local draft across all phases", () => {
    const props = popoverProps();
    const { rerender } = render(<AIPredictionPopover {...props} request={request()} />);
    const form = screen.getByTestId("preannotate-config-form");
    const advanced = screen.getByTestId("ai-prediction-advanced");
    const toggle = screen.getByTestId("ai-prediction-advanced-toggle");
    expect(toggle).toHaveAttribute("aria-controls", advanced.id);
    fireEvent.click(toggle);
    fireEvent.change(screen.getByRole("textbox", { name: "预设名" }), {
      target: { value: "Keep this draft" },
    });
    fireEvent.click(toggle);

    for (const active of [
      runningRequest(),
      request({ status: "completed" }),
      request({ status: "error", error: "服务失败", canRetry: true }),
      request(),
    ]) {
      rerender(<AIPredictionPopover {...props} request={active} aiBoxCount={2} />);
      expect(screen.getByTestId("preannotate-config-form")).toBe(form);
      expect(advanced).not.toBeVisible();
    }
    fireEvent.click(screen.getByTestId("ai-prediction-advanced-toggle"));
    expect(screen.getByRole("textbox", { name: "预设名" })).toHaveValue("Keep this draft");
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(props.onCancelRequest).not.toHaveBeenCalled();
  });

  it("opens the existing candidate review from the review primary action", () => {
    const props = popoverProps({ request: request({ status: "completed" }), aiBoxCount: 4 });
    render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "review");
    const primary = screen.getByTestId("ai-prediction-primary-action");
    expect(primary).toHaveTextContent("审阅候选");
    expect(screen.getByTestId("ai-prediction-phase")).toHaveTextContent("4 个候选待审阅");
    fireEvent.click(primary);
    expect(props.onReviewCandidates).toHaveBeenCalledTimes(1);
    expect(props.onAcceptAll).not.toHaveBeenCalled();
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(screen.queryByTestId("ai-inspector-panel")).toBeNull();
  });

  it("prioritizes a failed request and retries its owner without changing next-run inputs", () => {
    const props = popoverProps({
      request: runningRequest({ status: "error", canRetry: true, error: "请求失败，请重试" }),
      aiBoxCount: 2,
    });
    render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "error");
    expect(screen.getByRole("alert")).toHaveTextContent("请求失败，请重试");
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent("重试本次请求");
    fireEvent.click(screen.getByTestId("ai-prediction-advanced-toggle"));
    fireEvent.change(screen.getByRole("textbox", { name: "预设名" }), {
      target: { value: "Retry should retain this" },
    });
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(props.onRetryRequest).toHaveBeenCalledTimes(1);
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(props.cfg.setPrompt).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "预设名" })).toHaveValue("Retry should retain this");
  });

  it("labels a terminal job rerun as a new submission with the captured configuration", () => {
    const props = popoverProps({
      request: runningRequest({
        status: "error",
        error: "模型执行失败",
        canRetry: true,
        canCancel: false,
        retryCreatesNewRequest: true,
      }),
    });
    render(<AIPredictionPopover {...props} />);
    const action = screen.getByRole("button", { name: "按原配置重新运行" });
    fireEvent.click(action);
    expect(props.onRetryRequest).toHaveBeenCalledTimes(1);
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "重试本次请求" })).toBeNull();
  });

  it("keeps polling errors in running and offers only the supported polling retry", () => {
    const props = popoverProps({
      request: runningRequest({
        error: "暂时无法查询作业状态",
        canRetry: true,
        canCancel: false,
        cancelling: true,
        progressPct: null,
      }),
    });
    render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "running");
    expect(screen.getByRole("alert")).toHaveTextContent("暂时无法查询作业状态");
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByRole("button", { name: "重试状态查询" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重试状态查询" }));
    expect(props.onRetryRequest).toHaveBeenCalledTimes(1);
    expect(props.onRunAi).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "重试本次请求" })).toBeNull();
  });

  it("does not invent progress or cancellation when the executor supplies neither", () => {
    render(
      <AIPredictionPopover
        {...popoverProps({ request: runningRequest({ progressPct: null, canCancel: false }) })}
      />,
    );
    expect(screen.getByTestId("ai-request-progress")).toHaveTextContent("尚无可用进度");
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
  });

  it("respects cancellation state and ignores a stale compatibility running flag", () => {
    const props = popoverProps();
    const { rerender } = render(
      <AIPredictionPopover {...props} request={runningRequest({ cancelling: true })} />,
    );
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent(
      "正在取消本次请求",
    );
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
    rerender(
      <AIPredictionPopover {...props} aiRunning request={request({ status: "cancelled" })} />,
    );
    expect(screen.getByTestId("ai-prediction-phase")).toHaveAttribute("data-phase", "idle");
    expect(screen.getByText(/本次请求已取消/)).toBeVisible();
    expect(screen.getByTestId("ai-prediction-primary-action")).toHaveTextContent("运行当前图片");
  });

  it("retains pipeline, threshold, bulk scope and efficiency in advanced configuration", () => {
    const props = popoverProps({
      aiBoxCount: 2,
      hasProjectPipeline: true,
      projectPipelineStageCount: 3,
      onRunPipeline: vi.fn(),
      taskAiPredictionCount: 2,
      taskAiCost: 0.25,
      taskAiAvgMs: 120,
    });
    const { rerender } = render(<AIPredictionPopover {...props} />);
    fireEvent.click(screen.getByTestId("ai-prediction-advanced-toggle"));
    fireEvent.click(screen.getByTestId("ai-prediction-run-pipeline"));
    expect(props.onRunPipeline).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "候选筛选与批量采纳" })).toBeVisible();
    fireEvent.change(screen.getByRole("slider", { name: "置信度阈值" }), {
      target: { value: "0.63" },
    });
    expect(props.onSetConfThreshold).toHaveBeenCalledWith(0.63);
    fireEvent.click(screen.getByTestId("ai-prediction-accept-all"));
    expect(props.onAcceptAll).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("task-ai-cost")).toHaveTextContent("¥0.2500");
    expect(screen.getByTestId("task-ai-cost")).toHaveTextContent("120ms");
    fireEvent.click(screen.getByTestId("ai-prediction-run-next"));
    expect(props.onRunAi).toHaveBeenCalledTimes(1);
    rerender(<AIPredictionPopover {...props} isVideoTask />);
    expect(screen.queryByTestId("ai-prediction-run-pipeline")).toBeNull();
    expect(screen.getByTestId("ai-prediction-bulk-scope")).toHaveTextContent("其他帧");
  });

  it("labels and gates batch acceptance with the loaded eligible count", () => {
    const props = popoverProps({ aiBoxCount: 1, batchEligibleCount: 3 });
    render(<AIPredictionPopover {...props} />);
    fireEvent.click(screen.getByTestId("ai-prediction-advanced-toggle"));

    expect(screen.getByTestId("ai-prediction-bulk-scope")).toHaveTextContent("3 个");
    expect(screen.getByTestId("ai-prediction-accept-all")).toHaveTextContent("3");
    expect(screen.getByTestId("ai-prediction-accept-all")).toHaveAttribute(
      "title",
      "采纳当前题已加载的 3 个候选",
    );
    fireEvent.click(screen.getByTestId("ai-prediction-accept-all"));
    expect(props.onAcceptAll).toHaveBeenCalledTimes(1);
  });

  it("gates incomplete inputs and unsupported recovery while retaining configuration access", () => {
    const props = popoverProps({ cfg: config({ configReady: false }) });
    const { rerender } = render(<AIPredictionPopover {...props} />);
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
    expect(screen.getByTestId("ai-prediction-advanced-toggle")).toBeEnabled();
    rerender(
      <AIPredictionPopover
        {...props}
        request={request({ status: "error", error: "无法恢复", canRetry: false })}
      />,
    );
    expect(screen.getByTestId("ai-prediction-primary-action")).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("无法恢复");
  });
});

describe("AIInspectorPanel selected candidate review", () => {
  it("handles local A/D with edited attributes and stops background dispatch", () => {
    const props = inspectorProps();
    render(<AIInspectorPanel {...props} />);
    fireEvent.change(screen.getByRole("textbox", { name: "颜色" }), {
      target: { value: "black" },
    });
    const captureGuard = vi.fn((event: KeyboardEvent) => isWorkbenchInteractionBlocked(event));
    const globalKeyDown = vi.fn();
    window.addEventListener("keydown", captureGuard, true);
    window.addEventListener("keydown", globalKeyDown);
    try {
      const accept = screen.getByTestId("ai-candidate-accept");
      accept.focus();
      expect(fireEvent.keyDown(accept, { key: "a" })).toBe(false);
      expect(props.onAcceptPrediction).toHaveBeenCalledWith(props.aiBoxes[0], {
        color: "black",
      });
      expect(props.onAcceptPrediction).toHaveBeenCalledTimes(1);
      expect(fireEvent.keyDown(accept, { key: "d" })).toBe(false);
      expect(props.onRejectPrediction).toHaveBeenCalledWith(props.aiBoxes[0]);
      expect(props.onRejectPrediction).toHaveBeenCalledTimes(1);
      expect(props.onClearSelection).not.toHaveBeenCalled();
      expect(globalKeyDown).not.toHaveBeenCalled();
      expect(captureGuard).toHaveBeenCalledTimes(2);
      expect(captureGuard).toHaveNthReturnedWith(1, true);
      expect(captureGuard).toHaveNthReturnedWith(2, true);
    } finally {
      window.removeEventListener("keydown", captureGuard, true);
      window.removeEventListener("keydown", globalKeyDown);
    }
  });

  it("preserves global frame navigation from a focused manual video control", () => {
    const props = inspectorProps({
      selectedId: "manual-video-mask",
      currentFrameIndex: 0,
      videoTrackPanel: <button type="button">展开手工轨迹信息</button>,
    });
    render(<AIInspectorPanel {...props} />);
    const control = screen.getByRole("button", { name: "展开手工轨迹信息" });
    control.focus();
    const seekFrame = vi.fn();
    const globalKeyDown = vi.fn((event: KeyboardEvent) => {
      const blocked = isWorkbenchInteractionBlocked(event);
      if (!blocked && event.key === "ArrowRight") seekFrame();
      return blocked;
    });
    window.addEventListener("keydown", globalKeyDown);
    try {
      expect(control).toHaveFocus();
      expect(screen.getByTestId("ai-inspector-panel")).not.toHaveAttribute(
        "data-workbench-ai-toolbar",
      );
      expect(fireEvent.keyDown(control, { key: "ArrowRight" })).toBe(true);
      expect(fireEvent.keyDown(control, { key: "ArrowRight" })).toBe(true);
      expect(globalKeyDown).toHaveBeenCalledTimes(2);
      expect(globalKeyDown).toHaveNthReturnedWith(1, false);
      expect(globalKeyDown).toHaveNthReturnedWith(2, false);
      expect(seekFrame).toHaveBeenCalledTimes(2);
      expect(props.onAcceptPrediction).not.toHaveBeenCalled();
      expect(props.onRejectPrediction).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", globalKeyDown);
    }
  });

  it("yields A/D to attribute inputs, native selects, menus and dock tabs", () => {
    const props = inspectorProps({
      videoTrackPanel: (
        <div>
          <textarea aria-label="备注" />
          <select aria-label="原生选项">
            <option>选项</option>
          </select>
          <div role="menu">
            <button type="button" role="menuitem" data-testid="review-menu-item">
              菜单项
            </button>
          </div>
          <button type="button" role="tab" data-testid="review-dock-tab">
            停靠标签
          </button>
          <div contentEditable data-testid="review-editable" />
        </div>
      ),
    });
    render(<AIInspectorPanel {...props} />);
    for (const target of [
      screen.getByRole("textbox", { name: "颜色" }),
      screen.getByRole("textbox", { name: "备注" }),
      screen.getByRole("combobox", { name: "原生选项" }),
      screen.getByTestId("review-menu-item"),
      screen.getByTestId("review-dock-tab"),
      screen.getByTestId("review-editable"),
    ]) {
      // jsdom does not expose isContentEditable, so model the browser property for this target.
      if (target.hasAttribute("contenteditable")) {
        Object.defineProperty(target, "isContentEditable", { configurable: true, value: true });
      }
      fireEvent.keyDown(target, { key: "a" });
      fireEvent.keyDown(target, { key: "d" });
    }
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    expect(props.onRejectPrediction).not.toHaveBeenCalled();
  });

  it("ignores repeated, composing, modified and already consumed A/D events", () => {
    const props = inspectorProps();
    render(<AIInspectorPanel {...props} />);
    const target = screen.getByTestId("ai-candidate-accept");
    for (const key of ["a", "d"]) {
      for (const modifiers of [
        { repeat: true },
        { isComposing: true },
        { keyCode: 229 },
        { ctrlKey: true },
        { metaKey: true },
        { altKey: true },
        { shiftKey: true },
      ]) {
        fireEvent.keyDown(target, { key, ...modifiers });
      }
      const consumed = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      consumed.preventDefault();
      fireEvent(target, consumed);
    }
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    expect(props.onRejectPrediction).not.toHaveBeenCalled();
  });

  it("does not route local A/D without a writable single candidate", () => {
    const props = inspectorProps();
    const { rerender } = render(<AIInspectorPanel {...props} readOnly />);
    const press = () => {
      fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "a" });
      fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "d" });
    };
    press();
    rerender(<AIInspectorPanel {...props} selectedId={null} />);
    press();
    rerender(
      <AIInspectorPanel
        {...props}
        aiBoxes={[...props.aiBoxes, candidate("candidate-2")]}
        selectedIds={["candidate-1", "candidate-2"]}
      />,
    );
    press();
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    expect(props.onRejectPrediction).not.toHaveBeenCalled();
  });

  it("keeps local video A/D on the current frame and explicit buttons on their existing scope", () => {
    const box: AiBox = {
      ...candidate(),
      annotation_type: "video_bbox",
      geometry: { type: "video_bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2, frame_index: 5 },
    };
    const props = inspectorProps({ aiBoxes: [box], currentFrameIndex: 0 });
    const { rerender } = render(<AIInspectorPanel {...props} />);
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "a" });
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "d" });
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    expect(props.onRejectPrediction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("ai-candidate-accept"));
    expect(props.onAcceptPrediction).toHaveBeenCalledWith(box, undefined);
    vi.mocked(props.onAcceptPrediction).mockClear();
    rerender(<AIInspectorPanel {...props} currentFrameIndex={5} />);
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "a" });
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "d" });
    expect(props.onAcceptPrediction).toHaveBeenCalledTimes(1);
    expect(props.onRejectPrediction).toHaveBeenCalledTimes(1);
  });

  it("applies the same required-attribute gate to local A while allowing rejection", () => {
    const props = inspectorProps({
      aiBoxes: [{ ...candidate(), attributes: {} }],
      attributeSchema: {
        fields: [{ key: "color", label: "颜色", type: "text", required: true }],
      },
    });
    render(<AIInspectorPanel {...props} />);
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "a" });
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key: "d" });
    expect(props.onRejectPrediction).toHaveBeenCalledTimes(1);
  });

  it("leaves Enter, Escape and Tab with their existing control owners", () => {
    const props = inspectorProps();
    render(<AIInspectorPanel {...props} />);
    const globalKeyDown = vi.fn();
    window.addEventListener("keydown", globalKeyDown);
    try {
      for (const key of ["Enter", "Escape", "Tab"]) {
        expect(fireEvent.keyDown(screen.getByTestId("ai-inspector-panel"), { key })).toBe(true);
      }
      expect(globalKeyDown).toHaveBeenCalledTimes(3);
      expect(props.onAcceptPrediction).not.toHaveBeenCalled();
      expect(props.onRejectPrediction).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener("keydown", globalKeyDown);
    }
  });

  it("keeps the latest attribute edit through hiding, temporary absence and new props", () => {
    const props = inspectorProps();
    const { rerender } = render(<AIInspectorPanel {...props} />);
    fireEvent.change(screen.getByRole("textbox", { name: "颜色" }), {
      target: { value: "black" },
    });
    rerender(<AIInspectorPanel {...props} open={false} aiBoxes={[]} />);
    expect(screen.queryByTestId("ai-inspector-panel")).toBeNull();
    const refreshed = candidate();
    rerender(<AIInspectorPanel {...props} aiBoxes={[refreshed]} />);
    expect(screen.getByRole("textbox", { name: "颜色" })).toHaveValue("black");
    fireEvent.click(screen.getByTestId("ai-candidate-accept"));
    expect(props.onAcceptPrediction).toHaveBeenCalledWith(refreshed, { color: "black" });
    expect(props.onAcceptPrediction).toHaveBeenCalledTimes(1);
  });

  it("keeps edited attributes on the existing row accept path", () => {
    const props = inspectorProps();
    render(<AIInspectorPanel {...props} />);
    fireEvent.change(screen.getByRole("textbox", { name: "颜色" }), {
      target: { value: "white" },
    });
    fireEvent.click(screen.getByTestId("candidate-row-accept-candidate-1"));
    expect(props.onAcceptPrediction).toHaveBeenCalledWith(props.aiBoxes[0], { color: "white" });
  });

  it("keeps rejection with the existing owner and respects read-only controls", () => {
    const props = inspectorProps();
    const { rerender } = render(<AIInspectorPanel {...props} />);
    fireEvent.click(screen.getByTestId("ai-candidate-reject"));
    expect(props.onRejectPrediction).toHaveBeenCalledWith(props.aiBoxes[0]);
    expect(props.onClearSelection).not.toHaveBeenCalled();
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    rerender(<AIInspectorPanel {...props} readOnly />);
    expect(screen.getByTestId("ai-candidate-accept")).toBeDisabled();
    expect(screen.getByTestId("ai-candidate-reject")).toBeDisabled();
    expect(screen.getByRole("textbox", { name: "颜色" })).toBeDisabled();
  });

  it("allows empty candidate attributes to be completed before acceptance", () => {
    const props = inspectorProps({
      aiBoxes: [{ ...candidate(), attributes: {} }],
      attributeSchema: {
        fields: [{ key: "color", label: "颜色", type: "text", required: true }],
      },
    });
    render(<AIInspectorPanel {...props} />);
    expect(screen.getByTestId("ai-candidate-accept")).toBeDisabled();
    expect(screen.getByText(/1 项必填未填/)).toBeVisible();
    fireEvent.click(screen.getByTestId("candidate-row-accept-candidate-1"));
    expect(props.onAcceptPrediction).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: /颜色/ }), {
      target: { value: "red" },
    });
    expect(screen.getByTestId("ai-candidate-accept")).toBeEnabled();
    fireEvent.click(screen.getByTestId("ai-candidate-accept"));
    expect(props.onAcceptPrediction).toHaveBeenCalledWith(props.aiBoxes[0], { color: "red" });
  });

  it("retains the candidate draft while the request fails and retries", () => {
    const inspector = inspectorProps();
    const popover = popoverProps({ aiBoxCount: 1 });
    const ui = (active: Request) => (
      <>
        <AIPredictionPopover {...popover} request={active} />
        <AIInspectorPanel {...inspector} />
      </>
    );
    const { rerender } = render(ui(request({ status: "completed" })));
    fireEvent.change(screen.getByRole("textbox", { name: "颜色" }), {
      target: { value: "green" },
    });
    rerender(ui(request({ status: "error", error: "请求失败", canRetry: true })));
    fireEvent.click(screen.getByTestId("ai-prediction-primary-action"));
    expect(popover.onRetryRequest).toHaveBeenCalledTimes(1);
    rerender(ui(runningRequest()));
    expect(screen.getByRole("textbox", { name: "颜色" })).toHaveValue("green");
    fireEvent.click(screen.getByTestId("ai-candidate-accept"));
    expect(inspector.onAcceptPrediction).toHaveBeenCalledWith(inspector.aiBoxes[0], {
      color: "green",
    });
  });

  it("uses a different candidate's own attributes and keeps video candidate editing available", () => {
    const props = inspectorProps({ videoTrackPanel: <div>视频轨迹</div> });
    const { rerender } = render(<AIInspectorPanel {...props} />);
    fireEvent.change(screen.getByRole("textbox", { name: "颜色" }), {
      target: { value: "black" },
    });
    const second = { ...candidate("candidate-2"), attributes: { color: "red" } };
    rerender(
      <AIInspectorPanel {...props} aiBoxes={[...props.aiBoxes, second]} selectedId="candidate-2" />,
    );
    expect(screen.getByRole("textbox", { name: "颜色" })).toHaveValue("red");
    fireEvent.click(screen.getByTestId("ai-candidate-accept"));
    expect(props.onAcceptPrediction).toHaveBeenCalledWith(second, undefined);
  });
});
