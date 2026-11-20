/**
 * 视频交互层(Konva)渲染 + 句柄事件测试(konva-mock)。
 *
 * 断言:可编辑选中框出 8 向 resize 句柄、句柄 pointerdown 回传方向、画框/拖拽 live 预览
 * (虚线 + 像素空间几何)。真实命中/拖拽精度交给 Playwright(决策 C)。
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { VideoKonvaInteractionLayer } from "./VideoKonvaInteractionLayer";
import type { VideoDragState } from "./videoStageTypes";

const size = { w: 1000, h: 500 };

describe("VideoKonvaInteractionLayer", () => {
  it("可编辑选中框 → 8 向句柄;无 handleBox → 无句柄", () => {
    const { rerender } = render(
      <VideoKonvaInteractionLayer
        size={size}
        scale={1}
        drag={null}
        handleBox={{ id: "a", geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 }, color: "#ff0000" }}
        preview={null}
        onResizeHandlePointerDown={vi.fn()}
      />,
    );
    expect(document.querySelector('[data-konva="Layer"]')!.getAttribute("data-testid")).toBe(
      "interaction",
    );
    expect(document.querySelectorAll('[data-testid="video-resize-handle"]').length).toBe(8);

    rerender(
      <VideoKonvaInteractionLayer
        size={size}
        scale={1}
        drag={null}
        handleBox={null}
        preview={null}
        onResizeHandlePointerDown={vi.fn()}
      />,
    );
    expect(document.querySelectorAll('[data-testid="video-resize-handle"]').length).toBe(0);
  });

  it("句柄 pointerdown → 回传方向 nw + id + geom", () => {
    const onResize = vi.fn();
    const geom = { x: 0.1, y: 0.2, w: 0.3, h: 0.4 };
    render(
      <VideoKonvaInteractionLayer
        size={size}
        scale={1}
        drag={null}
        handleBox={{ id: "box-1", geom, color: "#ff0000" }}
        preview={null}
        onResizeHandlePointerDown={onResize}
      />,
    );
    const handles = document.querySelectorAll('[data-testid="video-resize-handle"]');
    // 第一个句柄是 nw(锚点顺序与图片 / 旧 SVG 栈一致)。
    fireEvent.pointerDown(handles[0]);
    expect(onResize).toHaveBeenCalledTimes(1);
    expect(onResize.mock.calls[0][0]).toBe("nw");
    expect(onResize.mock.calls[0][1]).toBe("box-1");
    expect(onResize.mock.calls[0][2]).toEqual(geom);
  });

  it("句柄几何 = 锚点像素 - 半句柄 + 句柄尺寸随 /scale", () => {
    render(
      <VideoKonvaInteractionLayer
        size={size}
        scale={2}
        drag={null}
        handleBox={{ id: "a", geom: { x: 0, y: 0, w: 0.4, h: 0.4 }, color: "#ff0000" }}
        preview={null}
        onResizeHandlePointerDown={vi.fn()}
      />,
    );
    // 句柄尺寸 = BOX_HANDLE_SCREEN_PX(8) / scale(2) = 4;nw 锚点在 (0,0) → x = 0 - 4/2 = -2。
    const nw = document.querySelectorAll('[data-testid="video-resize-handle"]')[0];
    expect(nw.getAttribute("data-width")).toBe("4");
    expect(nw.getAttribute("data-x")).toBe("-2");
  });

  it("draw 拖拽 → video-pending-draft 虚线预览,像素空间几何", () => {
    const drag: VideoDragState = {
      kind: "draw",
      start: { x: 0.1, y: 0.2 },
      current: { x: 0.4, y: 0.6 },
    };
    render(
      <VideoKonvaInteractionLayer
        size={size}
        scale={1}
        drag={drag}
        handleBox={null}
        preview={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 }, color: "#00ff00" }}
        onResizeHandlePointerDown={vi.fn()}
      />,
    );
    const draft = document.querySelector('[data-testid="video-pending-draft"]')!;
    expect(draft).not.toBeNull();
    expect(draft.getAttribute("data-x")).toBe("100"); // 0.1 * 1000
    expect(draft.getAttribute("data-width")).toBe("300"); // 0.3 * 1000
    expect(draft.getAttribute("data-dash")).toBe(JSON.stringify([6, 4]));
  });

  it("move/resize 拖拽 → video-drag-preview 预览", () => {
    const drag: VideoDragState = {
      kind: "move",
      id: "a",
      start: { x: 0.1, y: 0.1 },
      origin: { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      current: { x: 0.3, y: 0.3, w: 0.2, h: 0.2 },
    };
    render(
      <VideoKonvaInteractionLayer
        size={size}
        scale={1}
        drag={drag}
        handleBox={null}
        preview={{ geom: { x: 0.3, y: 0.3, w: 0.2, h: 0.2 }, color: "#00ff00" }}
        onResizeHandlePointerDown={vi.fn()}
      />,
    );
    expect(document.querySelector('[data-testid="video-drag-preview"]')).not.toBeNull();
    expect(document.querySelector('[data-testid="video-pending-draft"]')).toBeNull();
  });
});
