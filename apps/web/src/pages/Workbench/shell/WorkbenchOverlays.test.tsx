import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WorkbenchOverlays } from "./WorkbenchOverlays";

const noop = () => {};
const baseProps = {
  pendingDrawing: null,
  editingClass: null,
  samPendingGeom: null,
  samDefaultClass: "Car",
  batchChanging: false,
  batchChangeTarget: null,
  imageOverlayEnabled: true,
  stageGeom: { imgW: 0, imgH: 0 },
  vp: { scale: 1, tx: 0, ty: 0 },
  classes: ["Car", "Bike"],
  editingClassClasses: ["Car", "Bike"],
  recentClasses: [],
  activeClass: "Car",
  onPickPendingClass: noop,
  onCancelPending: noop,
  onCommitChangeClass: noop,
  onCancelChangeClass: noop,
  onSamCommitClass: noop,
  onSamCancelClass: noop,
  onCommitBatchChangeClass: noop,
  onCancelBatchChange: noop,
};

describe("WorkbenchOverlays", () => {
  it("renders video pending class picker with fixed anchor even without image geometry", () => {
    const onPickPendingClass = vi.fn();
    render(
      <WorkbenchOverlays
        {...baseProps}
        pendingDrawing={{
          kind: "video_bbox",
          frameIndex: 12,
          geom: { x: 0.2, y: 0.3, w: 0.1, h: 0.2 },
          anchor: { left: 48, top: 64 },
        }}
        onPickPendingClass={onPickPendingClass}
      />,
    );

    const popover = screen.getByTestId("class-picker-popover");
    expect(popover).toHaveClass("fixed");
    expect(popover.style.getPropertyValue("--class-picker-left")).toBe("48px");
    expect(popover.style.getPropertyValue("--class-picker-top")).toBe("64px");

    fireEvent.click(screen.getByText("Bike"));
    expect(onPickPendingClass).toHaveBeenCalledWith("Bike");
  });

  it("waits for image geometry before rendering image-anchored pending picker", () => {
    const { rerender } = render(
      <WorkbenchOverlays
        {...baseProps}
        pendingDrawing={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }}
      />,
    );
    expect(screen.queryByTestId("class-picker-popover")).toBeNull();

    rerender(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        pendingDrawing={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }}
      />,
    );
    expect(screen.getByTestId("class-picker-popover")).toHaveClass("absolute");
  });

  it("reports Escape separately from outside-click cancellation", async () => {
    const onCancelPending = vi.fn();
    const { unmount } = render(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        pendingDrawing={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }}
        onCancelPending={onCancelPending}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancelPending).toHaveBeenCalledWith("escape");
    unmount();

    const onOutsideCancel = vi.fn();
    render(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        pendingDrawing={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }}
        onCancelPending={onOutsideCancel}
      />,
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.pointerDown(document.body);
    expect(onOutsideCancel).toHaveBeenCalledWith("outside");
  });

  it("renders SAM and batch pickers only when no higher-priority picker is active", () => {
    const { rerender } = render(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        samPendingGeom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        samDefaultClass="Bike"
      />,
    );
    expect(screen.getByText("接受 SAM 候选 → 选类别")).toBeTruthy();

    rerender(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        batchChanging
        batchChangeTarget={{
          geom: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
          className: "Car",
          count: 3,
        }}
      />,
    );
    expect(screen.getByText("批量改类别 (3 个)")).toBeTruthy();

    rerender(
      <WorkbenchOverlays
        {...baseProps}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        pendingDrawing={{ geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 } }}
        batchChanging
        batchChangeTarget={{
          geom: { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
          className: "Car",
          count: 3,
        }}
      />,
    );
    expect(screen.getByText("选择类别")).toBeTruthy();
    expect(screen.queryByText("批量改类别 (3 个)")).toBeNull();
  });

  it("suppresses image-position overlays when the current stage is not image", () => {
    render(
      <WorkbenchOverlays
        {...baseProps}
        imageOverlayEnabled={false}
        stageGeom={{ imgW: 1000, imgH: 500 }}
        editingClass={{
          annotationId: "ann-1",
          geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
          currentClass: "Car",
        }}
      />,
    );
    expect(screen.queryByTestId("class-picker-popover")).toBeNull();
  });

  it("renders video class editing picker with fixed anchor", () => {
    const onCommitChangeClass = vi.fn();
    render(
      <WorkbenchOverlays
        {...baseProps}
        imageOverlayEnabled={false}
        editingClass={{
          annotationId: "ann-1",
          geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
          currentClass: "Car",
          anchor: { left: 120, top: 88 },
        }}
        onCommitChangeClass={onCommitChangeClass}
      />,
    );

    const popover = screen.getByTestId("class-picker-popover");
    expect(popover).toHaveClass("fixed");
    expect(popover.style.getPropertyValue("--class-picker-left")).toBe("120px");
    expect(popover.style.getPropertyValue("--class-picker-top")).toBe("88px");

    fireEvent.click(screen.getByText("Bike"));
    expect(onCommitChangeClass).toHaveBeenCalledWith("Bike");
  });
});

describe("WorkbenchOverlays · SAM 候选类选择器的两种定位", () => {
  it("给了 anchor → fixed 定位, 不依赖 imageOverlayEnabled / stageGeom", () => {
    render(
      <WorkbenchOverlays
        {...baseProps}
        imageOverlayEnabled={false}
        stageGeom={{ imgW: 0, imgH: 0 }}
        samPendingGeom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        samPendingAnchor={{ left: 120, top: 240 }}
        samDefaultClass="Car"
      />,
    );
    expect(screen.getByText("接受 SAM 候选 → 选类别")).toBeTruthy();
  });

  it("无 anchor 且非图片舞台 → 不渲染（避免 popover 定位到 0,0）", () => {
    render(
      <WorkbenchOverlays
        {...baseProps}
        imageOverlayEnabled={false}
        stageGeom={{ imgW: 0, imgH: 0 }}
        samPendingGeom={{ x: 0.1, y: 0.2, w: 0.3, h: 0.4 }}
        samDefaultClass="Car"
      />,
    );
    expect(screen.queryByText("接受 SAM 候选 → 选类别")).toBeNull();
  });

  it("没有候选几何 → 两条路径都不渲染", () => {
    render(
      <WorkbenchOverlays
        {...baseProps}
        samPendingGeom={null}
        samPendingAnchor={{ left: 10, top: 20 }}
        stageGeom={{ imgW: 1000, imgH: 500 }}
      />,
    );
    expect(screen.queryByText("接受 SAM 候选 → 选类别")).toBeNull();
  });
});
