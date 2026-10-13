import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ToolGestureHintPill } from "./ToolGestureHintPill";

describe("ToolGestureHintPill", () => {
  it("renders active tool label, hotkey and gesture hints for 2D tools", () => {
    render(<ToolGestureHintPill tool="box" />);
    expect(screen.getByText("矩形框")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
    expect(screen.getByText(/拖拽画框/)).toBeInTheDocument();
  });

  it("switches context hint dynamically when tool changes", () => {
    const { rerender } = render(<ToolGestureHintPill tool="polygon" />);
    expect(screen.getByText("多边形")).toBeInTheDocument();
    expect(screen.getByText(/Enter\/双击闭合/)).toBeInTheDocument();

    rerender(<ToolGestureHintPill tool="smart-point" />);
    expect(screen.getByText("SAM 智能点")).toBeInTheDocument();
    expect(screen.getByText(/左键加正点/)).toBeInTheDocument();
  });

  it("supports collapsing and expanding via the toggle button", () => {
    render(<ToolGestureHintPill tool="select" />);
    expect(screen.getByText("选择")).toBeInTheDocument();

    const collapseBtn = screen.getByRole("button", { name: "收起操作提示" });
    fireEvent.click(collapseBtn);
    expect(screen.queryByText("单击选中 · 拖拽移动 · Del 删除 · ESC 取消")).toBeNull();

    const expandBtn = screen.getByRole("button", { name: "展开 选择 手势操作提示" });
    fireEvent.click(expandBtn);
    expect(screen.getByText(/单击选中 · 拖拽移动/)).toBeInTheDocument();
  });

  it("renders video tool hints in video stage", () => {
    render(<ToolGestureHintPill tool="select" stageKind="video" videoTool="track" />);
    expect(screen.getByText("目标轨迹")).toBeInTheDocument();
    expect(screen.getByText(/空间传播追踪/)).toBeInTheDocument();
  });

  it("renders review mode hints when reviewMode is active", () => {
    render(<ToolGestureHintPill tool="hand" reviewMode />);
    expect(screen.getByText("审阅模式")).toBeInTheDocument();
    expect(screen.getByText(/A 键采纳 \/ R 键驳回/)).toBeInTheDocument();
  });
});
