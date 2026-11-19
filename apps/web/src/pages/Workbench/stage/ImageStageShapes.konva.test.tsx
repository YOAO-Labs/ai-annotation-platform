/**
 * konva mock 范式样板(画布栈统一地基)。
 *
 * 本测试是后续视频测试迁移到 Konva 的「范式样板」:证明
 *     react-konva mock(src/test/konvaMock.tsx)+ fireEvent + getByTestId / 查 data-konva
 * 三件套跑得通——对 Konva 组件做交互断言与 props 透传断言。
 *
 * 它只验证交互 / props 透传,不验证真实 canvas 渲染(渲染回归交给 Playwright)。
 * 目标组件:ImageStageShapes 导出的 KonvaBox(图片侧 bbox 形状)。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { KonvaBox } from "./ImageStageShapes";
import { DEFAULT_ANNOTATION_VISUAL } from "./annotationVisual";
import type { Annotation } from "@/types";

/** 最小可渲染的人工 bbox 标注(归一化坐标)。 */
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
    // mock 把 Konva 组件渲染成 <div data-konva="Rect" data-*>;主体框是第一个 Rect。
    const rects = document.querySelectorAll('[data-konva="Rect"]');
    expect(rects.length).toBeGreaterThanOrEqual(1);
    const main = rects[0];
    // x = b.x * imgW = 0.1 * 1000 = 100; width = b.w * imgW = 0.3 * 1000 = 300。
    expect(main.getAttribute("data-x")).toBe("100");
    expect(main.getAttribute("data-width")).toBe("300");
    expect(main.getAttribute("data-y")).toBe("160"); // 0.2 * 800
    expect(main.getAttribute("data-height")).toBe("320"); // 0.4 * 800
  });

  it("选中态 strokeWidth 在基值上加粗(strokeWidthFor 选中 +0.5)", () => {
    const { rerender } = render(
      <KonvaBox b={makeBox()} selected={false} onClick={vi.fn()} {...COMMON_PROPS} />,
    );
    // 未选中:strokeWidth = 1.5 / scale(1) = 1.5
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-strokewidth")).toBe(
      "1.5",
    );

    rerender(<KonvaBox b={makeBox()} selected onClick={vi.fn()} {...COMMON_PROPS} />);
    // 选中:strokeWidth = (1.5 + 0.5) / scale(1) = 2
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
    // isAi → dash = [4/scale, 3/scale] = [4,3]
    expect(document.querySelector('[data-konva="Rect"]')!.getAttribute("data-dash")).toBe(
      JSON.stringify([4, 3]),
    );

    rerender(<KonvaBox b={makeBox()} selected={false} onClick={vi.fn()} {...COMMON_PROPS} />);
    // 人工框 dash = undefined → data-dash 不存在
    expect(document.querySelector('[data-konva="Rect"]')!.hasAttribute("data-dash")).toBe(false);
  });

  it("fireEvent 点击主体 Rect 触发 onClick 回调,回调收到近似 Konva 事件", () => {
    const onClick = vi.fn();
    render(<KonvaBox b={makeBox()} selected={false} onClick={onClick} {...COMMON_PROPS} />);
    const main = document.querySelector('[data-konva="Rect"]')!;
    fireEvent.click(main);
    expect(onClick).toHaveBeenCalledTimes(1);
    // 回调拿到近似 Konva 事件对象(含 evt),被测组件读 e.cancelBubble 不炸。
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
    // 主体 1 个 Rect + 8 个手柄 Rect = 9(label 用 Label/Tag/Text,不计入 Rect)。
    const rects = document.querySelectorAll('[data-konva="Rect"]');
    expect(rects.length).toBe(1 + 8);
    // 标签默认 always 可见 → 渲染出 Label/Tag/Text。
    expect(screen.getByText(/商品/)).toBeInTheDocument();
  });
});
