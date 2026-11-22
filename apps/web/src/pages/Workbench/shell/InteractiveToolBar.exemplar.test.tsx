import { fireEvent, render as rtlRender, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

function render(ui: Parameters<typeof rtlRender>[0]) {
  const result = rtlRender(ui);
  const trigger = screen.queryByTestId("interactive-settings-trigger");
  if (trigger) {
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("button", { name: "更多 AI 工具" }));
  }
  return result;
}

import { InteractiveToolBar } from "./InteractiveToolBar";
import type { MLModelCapability } from "@/api/ml-backends";

function exemplarModel(caps: MLModelCapability["exemplar_capabilities"]): MLModelCapability {
  return {
    id: "exemplar-yoloe",
    task: "interactive_seg",
    is_interactive: true,
    supported_prompts: ["exemplar"],
    supported_geometric_outputs: ["bbox", "polygon"],
    exemplar_capabilities: caps,
  };
}

function renderDrawer(model: MLModelCapability) {
  return render(
    <InteractiveToolBar
      tool="exemplar"
      backendName="yolo-backend"
      capability={undefined}
      samPolarity="positive"
      onSetSamPolarity={vi.fn()}
      isLoading={false}
      isError={false}
      exemplarOutputMode="mask"
      onSetExemplarOutputMode={vi.fn()}
      singleFrameOutputGeometry="polygon"
      onSetSingleFrameOutputGeometry={vi.fn()}
      exemplarText=""
      onSetExemplarText={vi.fn()}
      exemplarThreshold={null}
      onSetExemplarThreshold={vi.fn()}
      models={[model]}
      activeModelId="exemplar-yoloe"
    />,
  );
}

describe("InteractiveToolBar · exemplar 能力门控", () => {
  it("negative_box=false 隐藏负极性按钮", () => {
    renderDrawer(exemplarModel({ negative_box: false, text_combination: false }));
    expect(screen.queryByTestId("ai-tool-polarity")).toBeNull();
  });

  it("text_combination=false 隐藏叠加文本输入", () => {
    renderDrawer(exemplarModel({ negative_box: false, text_combination: false }));
    expect(screen.queryByTestId("exemplar-text")).toBeNull();
  });

  it("全支持 (sam3 风格) 显示负极性按钮与文本输入", () => {
    renderDrawer(exemplarModel({ negative_box: true, text_combination: true }));
    expect(screen.getByTestId("ai-tool-polarity")).toBeVisible();
    expect(screen.getByTestId("exemplar-text")).toBeVisible();
    expect(screen.getByTestId("exemplar-threshold")).not.toBeVisible();
  });

  it("缺 exemplar_capabilities 向后兼容: 控件全显示", () => {
    renderDrawer(exemplarModel(undefined));
    expect(screen.queryByTestId("ai-tool-polarity")).not.toBeNull();
    expect(screen.queryByTestId("exemplar-text")).not.toBeNull();
  });

  it("输出形态三选恒显示 (与能力无关)", () => {
    renderDrawer(exemplarModel({ negative_box: false }));
    expect(screen.queryByTestId("exemplar-output-mode")).not.toBeNull();
  });

  it("单帧持久几何与 exemplar 召回形态分别显示", () => {
    renderDrawer(exemplarModel({ negative_box: false }));
    expect(screen.getByTestId("single-frame-output-geometry")).toBeVisible();
    expect(screen.getByTestId("exemplar-output-mode")).toBeVisible();
  });

  it("能力门禁关闭时禁用原生 Mask 选项并保留显式 polygon", () => {
    const onChange = vi.fn();
    render(
      <InteractiveToolBar
        tool="smart-point"
        backendName="backend"
        capability={undefined}
        samPolarity="positive"
        onSetSamPolarity={vi.fn()}
        isLoading={false}
        isError={false}
        singleFrameOutputGeometry="polygon"
        onSetSingleFrameOutputGeometry={onChange}
        nativeMaskOutputDisabledReason="当前模型未声明原生 Mask 输出能力"
      />,
    );
    const select = screen.getByTestId("single-frame-output-geometry-select");
    expect(select).toHaveAttribute("title", "当前模型未声明原生 Mask 输出能力");
    expect(select.querySelector('option[value="mask"]')).toBeDisabled();
    fireEvent.change(select, { target: { value: "polygon" } });
    expect(onChange).toHaveBeenCalledWith("polygon");
  });
});
