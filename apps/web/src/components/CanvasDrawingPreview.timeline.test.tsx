import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { CanvasDrawingPreview } from "./CanvasDrawingEditor";

describe("CanvasDrawingPreview · 笔画 timeline", () => {
  it("旧 shape 缺 started_at/ended_at → 不渲染 timeline bar", () => {
    render(
      <CanvasDrawingPreview
        drawing={{
          shapes: [{ type: "line", points: [0.1, 0.1, 0.5, 0.5], stroke: "#ef4444" }],
        }}
      />,
    );
    expect(screen.queryByTestId("canvas-drawing-timeline")).toBeNull();
  });

  it("全字段 shape → 渲染 timeline bar + 段数=shape 数", () => {
    render(
      <CanvasDrawingPreview
        drawing={{
          shapes: [
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#ef4444",
              id: "a",
              started_at: 1000,
              ended_at: 2000,
            },
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#10b981",
              id: "b",
              started_at: 3000,
              ended_at: 6000,
            },
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#3b82f6",
              id: "c",
              started_at: 7000,
              ended_at: 8000,
            },
          ],
        }}
      />,
    );
    expect(screen.getByTestId("canvas-drawing-timeline")).toBeInTheDocument();
    const segments = screen.getAllByTestId("canvas-drawing-timeline-segment");
    expect(segments).toHaveLength(3);
  });

  it("混合 (含一段缺字段) → 整段降级不渲染 timeline", () => {
    render(
      <CanvasDrawingPreview
        drawing={{
          shapes: [
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#ef4444",
              started_at: 1000,
              ended_at: 2000,
            },
            { type: "line", points: [0, 0, 1, 1], stroke: "#10b981" },
          ],
        }}
      />,
    );
    expect(screen.queryByTestId("canvas-drawing-timeline")).toBeNull();
  });

  it("hover segment → 仅对应 stroke 高亮 (其他 opacity=0.25)", () => {
    render(
      <CanvasDrawingPreview
        drawing={{
          shapes: [
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#ef4444",
              id: "a",
              started_at: 1000,
              ended_at: 2000,
            },
            {
              type: "line",
              points: [0, 0, 1, 1],
              stroke: "#10b981",
              id: "b",
              started_at: 3000,
              ended_at: 6000,
            },
          ],
        }}
      />,
    );
    const segments = screen.getAllByTestId("canvas-drawing-timeline-segment");
    fireEvent.mouseEnter(segments[0]);

    const polylines = document.querySelectorAll("polyline");
    expect(polylines[0].getAttribute("opacity")).toBe("1");
    expect(polylines[1].getAttribute("opacity")).toBe("0.25");

    fireEvent.mouseLeave(segments[0]);
    const polylinesAfter = document.querySelectorAll("polyline");
    expect(polylinesAfter[0].getAttribute("opacity")).toBe("1");
    expect(polylinesAfter[1].getAttribute("opacity")).toBe("1");
  });
});
