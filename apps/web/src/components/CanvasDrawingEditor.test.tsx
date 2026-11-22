import { createEvent, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CanvasDrawingEditor } from "./CanvasDrawingEditor";
import { isWorkbenchInteractionBlocked } from "@/pages/Workbench/state/workbenchInteractionGuards";

function setCanvasBounds(svg: SVGSVGElement) {
  vi.spyOn(svg, "getBoundingClientRect").mockReturnValue({
    bottom: 100,
    height: 100,
    left: 0,
    right: 100,
    top: 0,
    width: 100,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect);
}

function firePointer(
  svg: SVGSVGElement,
  type: "pointerDown" | "pointerMove" | "pointerUp",
  clientX: number,
  clientY: number,
  pointerId: number,
) {
  const event = createEvent[type](svg, { bubbles: true, cancelable: true });
  Object.defineProperties(event, {
    clientX: { configurable: true, value: clientX },
    clientY: { configurable: true, value: clientY },
    pointerId: { configurable: true, value: pointerId },
  });
  fireEvent(svg, event);
}

describe("CanvasDrawingEditor draft ownership", () => {
  it("keeps portal header and editor keyboard input away from the underlying canvas", () => {
    const view = render(<CanvasDrawingEditor open onClose={vi.fn()} onSave={vi.fn()} />);
    const event = new KeyboardEvent("keydown", { key: "Enter" });
    expect(isWorkbenchInteractionBlocked(event)).toBe(true);
    const headerClose = screen.getByRole("button", { name: "关闭" });
    expect(headerClose.closest("[data-workbench-discussion]")).toBeNull();
    view.unmount();
    expect(isWorkbenchInteractionBlocked(event)).toBe(false);
  });

  it("reports pointer progress synchronously before React flushes state", () => {
    const onDraftChange = vi.fn();
    render(
      <CanvasDrawingEditor open onClose={vi.fn()} onSave={vi.fn()} onDraftChange={onDraftChange} />,
    );
    const svg = screen.getByTestId("canvas-drawing-editor") as unknown as SVGSVGElement;
    setCanvasBounds(svg);

    firePointer(svg, "pointerDown", 10, 20, 1);
    expect(onDraftChange).toHaveBeenLastCalledWith({
      shapes: [
        expect.objectContaining({
          ended_at: null,
          points: [0.1, 0.2],
          type: "line",
        }),
      ],
    });

    firePointer(svg, "pointerMove", 80, 90, 1);
    expect(onDraftChange).toHaveBeenLastCalledWith({
      shapes: [
        expect.objectContaining({
          ended_at: null,
          points: [0.1, 0.2, 0.8, 0.9],
          type: "line",
        }),
      ],
    });

    firePointer(svg, "pointerUp", 80, 90, 1);
    expect(onDraftChange).toHaveBeenLastCalledWith({
      shapes: [expect.objectContaining({ points: [0.1, 0.2, 0.8, 0.9], type: "line" })],
    });
  });

  it("does not reset an in-progress stroke when autosave changes initial props", () => {
    const onDraftChange = vi.fn();
    const view = render(
      <CanvasDrawingEditor open onClose={vi.fn()} onSave={vi.fn()} onDraftChange={onDraftChange} />,
    );
    const svg = screen.getByTestId("canvas-drawing-editor") as unknown as SVGSVGElement;
    setCanvasBounds(svg);
    firePointer(svg, "pointerDown", 10, 10, 1);
    firePointer(svg, "pointerMove", 30, 30, 1);

    view.rerender(
      <CanvasDrawingEditor
        open
        onClose={vi.fn()}
        onSave={vi.fn()}
        onDraftChange={onDraftChange}
        initial={{ shapes: [{ type: "line", points: [0.1, 0.1, 0.3, 0.3] }] }}
      />,
    );
    firePointer(svg, "pointerMove", 60, 60, 1);

    expect(onDraftChange).toHaveBeenLastCalledWith({
      shapes: [
        expect.objectContaining({
          points: [0.1, 0.1, 0.3, 0.3, 0.6, 0.6],
          type: "line",
        }),
      ],
    });
  });

  it("reports the latest partial pointer when Escape closes the modal", () => {
    const onDraftChange = vi.fn();
    render(
      <CanvasDrawingEditor open onClose={vi.fn()} onSave={vi.fn()} onDraftChange={onDraftChange} />,
    );
    const svg = screen.getByTestId("canvas-drawing-editor") as unknown as SVGSVGElement;
    setCanvasBounds(svg);
    firePointer(svg, "pointerDown", 15, 25, 1);
    fireEvent.keyDown(document, { key: "Escape" });

    expect(onDraftChange).toHaveBeenLastCalledWith({
      shapes: [expect.objectContaining({ points: [0.15, 0.25], ended_at: null })],
    });
  });

  it("reports clear and undo as draft replacements", () => {
    const onDraftChange = vi.fn();
    render(
      <CanvasDrawingEditor open onClose={vi.fn()} onSave={vi.fn()} onDraftChange={onDraftChange} />,
    );
    const svg = screen.getByTestId("canvas-drawing-editor") as unknown as SVGSVGElement;
    setCanvasBounds(svg);
    firePointer(svg, "pointerDown", 10, 10, 1);
    firePointer(svg, "pointerMove", 20, 20, 1);
    firePointer(svg, "pointerUp", 20, 20, 1);
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(onDraftChange).toHaveBeenLastCalledWith(null);

    firePointer(svg, "pointerDown", 10, 10, 2);
    firePointer(svg, "pointerMove", 20, 20, 2);
    firePointer(svg, "pointerUp", 20, 20, 2);
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(onDraftChange).toHaveBeenLastCalledWith(null);
  });
});
