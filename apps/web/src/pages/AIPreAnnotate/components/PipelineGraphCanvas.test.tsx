import { describe, it, expect, vi, beforeAll } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import PipelineGraphCanvas from "./PipelineGraphCanvas";
import { roleOf, type GraphNodeModel } from "../utils/pipelineGraph";
import type { PipelineStagePayload } from "@/hooks/usePreannotation";

beforeAll(() => {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

const attr = (): PipelineStagePayload =>
  ({
    stage: 0,
    ml_backend_id: "bk",
    write: { target: "attributes", keys: ["color"] },
    label: "hat",
  }) as PipelineStagePayload;

const models: GraphNodeModel[] = [
  {
    sid: "root",
    parentSid: null,
    role: { label: "检测", variant: "accent", icon: "box" },
    detail: "源检测",
    runState: "pending",
    ok: 5,
    producesGeometry: true,
    canAddChild: true,
    conflict: false,
    ready: true,
    backendName: "grounded-sam2",
  },
  {
    sid: "a",
    parentSid: "root",
    role: roleOf(attr()),
    detail: "hat_color",
    runState: "pending",
    producesGeometry: false,
    canAddChild: false,
    conflict: false,
    ready: true,
    backendName: "onnxtools",
    classFilter: "全部框",
  },
];

function renderCanvas(over: Partial<React.ComponentProps<typeof PipelineGraphCanvas>> = {}) {
  const onSelect = vi.fn();
  render(
    <PipelineGraphCanvas
      models={models}
      selectedSid="root"
      onSelect={onSelect}
      onAddChild={vi.fn()}
      onRemove={vi.fn()}
      onReparent={vi.fn()}
      canReparentConn={() => true}
      {...over}
    />,
  );
  return { onSelect };
}

describe("PipelineGraphCanvas", () => {
  it("渲染输入节点 + 下游节点的角色与详情", () => {
    renderCanvas();
    expect(screen.getByText("输入")).toBeInTheDocument();
    expect(screen.getByText("分类")).toBeInTheDocument();
    expect(screen.getByText("hat_color")).toBeInTheDocument();
    expect(screen.getByText("grounded-sam2")).toBeInTheDocument();
  });

  it("点击节点回调 onSelect(sid)", () => {
    const { onSelect } = renderCanvas();
    fireEvent.click(screen.getByText("hat_color"));
    expect(onSelect).toHaveBeenCalledWith("a");
  });
});
