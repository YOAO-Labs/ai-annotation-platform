import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KonvaBox } from "./ImageStageShapes";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import type { Annotation } from "@/types";

function makeBox(): Annotation {
  return {
    id: "a1",
    x: 0.1,
    y: 0.2,
    w: 0.3,
    h: 0.4,
    cls: "商品",
    conf: 1,
    source: "manual",
  };
}

const COMMON_PROPS = {
  isAi: false,
  editable: true,
  faded: false,
  visual: DEFAULT_ANNOTATION_VISUAL,
  imgW: 1000,
  imgH: 800,
  scale: 1,
  onMoveStart: null,
  onResizeStart: null,
};

describe("KonvaBox · konva mock 范式样板", () => {
  it("渲染出主体 Rect,几何 props 按 imgW/imgH 透传", () => {
    render(<KonvaBox b={makeBox()} selected={false} onClick={vi.fn()} {...COMMON_PROPS} />);
    const rects = document.querySelectorAll('[data-konva="Rect"]');
    expect(rects.length).toBeGreaterThanOrEqual(1);
    const main = rects[0];
    expect(main.getAttribute("data-x")).toBe("100");
    expect(main.getAttribute("data-width")).toBe("300");
    expect(main.getAttribute("data-y")).toBe("160");
    expect(main.getAttribute("data-height")).toBe("320");
  });

  it("选中态 strokeWidth 在基值上加粗(strokeWidthFor 选中 +0.5)", () => {
    const { rerender } = render(
      <KonvaBox b={makeBox()} selected={false} onClick={vi.fn()} {...COMMON_PROPS} />,
    );
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-strokewidth")).toBe(
      "1.5",
    );

    rerender(<KonvaBox b={makeBox()} selected onClick={vi.fn()} {...COMMON_PROPS} />);
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-strokewidth")).toBe(
      "2",
    );
  });

  it("AI 框走虚线(dash 透传),人工框不带 dash", () => {
    const { rerender } = render(
      <KonvaBox
        b={makeBox()}
        selected={false}
        isAi
        onClick={vi.fn()}
        editable={false}
        faded={false}
        visual={DEFAULT_ANNOTATION_VISUAL}
        imgW={1000}
        imgH={800}
        scale={1}
        onMoveStart={null}
        onResizeStart={null}
      />,
    );
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-dash")).toBe(
      JSON.stringify([4, 3]),
    );

    rerender(<KonvaBox b={makeBox()} selected={false} onClick={vi.fn()} {...COMMON_PROPS} />);
    expect(document.querySelector('[data-konva="Rect"]')!.hasAttribute("data-dash")).toBe(false);
  });

  it("fireEvent 点击主体 Rect 触发 onClick 回调,回调收到近似 Konva 事件", () => {
    const onClick = vi.fn();
    render(<KonvaBox b={makeBox()} selected={false} onClick={onClick} {...COMMON_PROPS} />);
    const main = document.querySelector('[data-konva="Rect"]')!;
    fireEvent.click(main);
    expect(onClick).toHaveBeenCalledTimes(1);
    const arg = onClick.mock.calls[0][0];
    expect(arg).toBeDefined();
    expect(arg).toHaveProperty("evt");
  });

  it("选中 + 可编辑时渲染 8 个 resize 手柄(getByTestId/data-konva 计数)", () => {
    render(
      <KonvaBox
        b={makeBox()}
        selected
        onClick={vi.fn()}
        {...COMMON_PROPS}
        onResizeStart={vi.fn()}
      />,
    );
    const rects = document.querySelectorAll('[data-konva="Rect"]');
    expect(rects.length).toBe(1 + 8);
    expect(screen.getByText(/商品/)).toBeInTheDocument();
  });
});
