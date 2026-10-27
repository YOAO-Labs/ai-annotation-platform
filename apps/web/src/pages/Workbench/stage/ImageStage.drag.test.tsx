import { useState, type ComponentProps, type MutableRefObject } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react-konva", async () => {
  const React = await import("react");
  const components = await import("@/test/konvaMock");
  type StageProps = {
    children?: React.ReactNode;
    onMouseDown?: (event: { target: unknown; evt: MouseEvent }) => void;
    onMouseMove?: (event: { target: unknown; evt: MouseEvent }) => void;
  };
  return {
    ...components,
    Stage: React.forwardRef((props: StageProps, ref) => {
      const stage = React.useMemo(() => ({ draw: vi.fn(), getLayers: () => [] }), []);
      React.useImperativeHandle(ref, () => stage, [stage]);
      return (
        <div
          data-testid="konva-stage"
          onMouseDown={(event) => props.onMouseDown?.({ target: stage, evt: event.nativeEvent })}
          onMouseMove={(event) => props.onMouseMove?.({ target: stage, evt: event.nativeEvent })}
        >
          {props.children}
        </div>
      );
    }),
  };
});

const draftVisual = vi.hoisted(() => ({ fillOpacity: 0.37 }));

vi.mock("../state/useWorkbenchConfig", async () => {
  const { DEFAULT_WORKBENCH_PREFERENCES } = await import("@/api/auth");
  return {
    useWorkbenchConfig: () => ({
      config: {
        ...DEFAULT_WORKBENCH_PREFERENCES,
        common: { ...DEFAULT_WORKBENCH_PREFERENCES.common, fillOpacity: draftVisual.fillOpacity },
      },
    }),
  };
});
vi.mock("../state/useViewportTransform", () => ({
  useElementSize: (ref: MutableRefObject<HTMLDivElement | null>) => ({
    ref: (node: HTMLDivElement | null) => {
      ref.current = node;
    },
    size: { w: 100, h: 100 },
  }),
}));
vi.mock("./useImageStageFit", () => ({
  useImageStageFit: () => ({ fitted: true, fitNow: vi.fn() }),
}));
vi.mock("./useAbortableImage", () => ({
  useAbortableImage: () => [undefined, "loaded"],
}));

import { ImageStage } from "./ImageStage";
import type { RasterMaskRenderRecord } from "./shared/rasterMaskRender";

const commit = vi.fn();
const emptyBoxes: [] = [];
const defaults: ComponentProps<typeof ImageStage> = {
  fileUrl: "/image.png",
  mediaKey: "image-a",
  imageWidth: 100,
  imageHeight: 100,
  tool: "box",
  activeClass: "car",
  selectedId: null,
  userBoxes: emptyBoxes,
  aiBoxes: emptyBoxes,
  spacePan: false,
  vp: { scale: 1, tx: 0, ty: 0 },
  setVp: vi.fn(),
  fitTick: 0,
  onSelectBox: vi.fn(),
  onCursorMove: vi.fn(),
  onCommitDrawing: commit,
};

function CursorOwner() {
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  return (
    <>
      <output data-testid="cursor">{cursor ? `${cursor.x},${cursor.y}` : "none"}</output>
      <ImageStage
        {...defaults}
        onCursorMove={setCursor}
        onCommitDrawing={(geometry) => commit(geometry, cursor)}
      />
    </>
  );
}

const rotatedGeometry = {
  type: "rotated_bbox" as const,
  cx: 0.5,
  cy: 0.5,
  w: 0.2,
  h: 0.1,
  angle: 35,
};
const rotatedProps: ComponentProps<typeof ImageStage> = {
  ...defaults,
  tool: "select",
  selectedId: "rotated",
  userBoxes: [
    {
      id: "rotated",
      cls: "car",
      source: "manual",
      conf: 1,
      x: 0.4,
      y: 0.45,
      w: 0.2,
      h: 0.1,
      geometry: rotatedGeometry,
    },
  ],
};
const rotatedGroup = () => document.querySelector('[data-konva="Group"][data-id="rotated"]')!;
const rotatedBody = () => rotatedGroup().querySelector('[data-konva="Rect"]')!;

describe("ImageStage drag scheduling", () => {
  let frames: Map<number, FrameRequestCallback>;
  beforeEach(() => {
    commit.mockReset();
    frames = new Map();
    let nextFrame = 0;
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.set(++nextFrame, callback);
      return nextFrame;
    });
    vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const flushFrame = () => {
    act(() => {
      const pending = [...frames.values()];
      frames.clear();
      for (const callback of pending) callback(16);
    });
  };

  it("picks candidate pixels, misses holes and removes filtered candidates", () => {
    const select = vi.fn();
    const box = {
      id: "pred-a-8",
      cls: "car",
      conf: 0.9,
      source: "prediction_based",
      predictionId: "a",
      shapeIndex: 8,
      geometry: { type: "raster_mask", mask: {} },
    };
    const record = {
      id: box.id,
      source: "prediction",
      image: document.createElement("canvas"),
      bounds: { x: 0, y: 0, w: 1, h: 1 },
      area: 8,
      componentCount: 1,
      holeCount: 1,
      boundaryPixelCount: 8,
      sourceWidth: 3,
      sourceHeight: 3,
      crop: {
        x: 0,
        y: 0,
        width: 3,
        height: 3,
        alpha: new Uint8Array([255, 255, 255, 255, 0, 255, 255, 255, 255]),
      },
      zOrder: 0,
      selected: false,
      cacheKey: "candidate",
      preview: false,
    } satisfies RasterMaskRenderRecord<"prediction">;
    const props = {
      ...defaults,
      tool: "select" as const,
      onSelectBox: select,
      aiBoxes: [box] as never,
      predictionRasterMaskRecords: [record],
    };
    const view = render(<ImageStage {...props} />);
    expect(screen.getAllByTestId("raster-mask-prediction")).toHaveLength(1);
    fireEvent.mouseDown(screen.getByTestId("konva-stage"), { clientX: 10, clientY: 10, button: 0 });
    expect(select).toHaveBeenLastCalledWith(box.id, { shift: false });
    fireEvent.mouseUp(window);
    select.mockClear();
    fireEvent.mouseDown(screen.getByTestId("konva-stage"), { clientX: 50, clientY: 50, button: 0 });
    expect(select).not.toHaveBeenCalledWith(box.id, expect.anything());
    fireEvent.mouseUp(window);
    view.rerender(<ImageStage {...props} aiBoxes={[]} />);
    expect(screen.queryByTestId("raster-mask-prediction")).toBeNull();
    select.mockClear();
    fireEvent.mouseDown(screen.getByTestId("konva-stage"), { clientX: 10, clientY: 10, button: 0 });
    expect(select).not.toHaveBeenCalledWith(box.id, expect.anything());
    fireEvent.mouseUp(window);
    view.rerender(
      <ImageStage
        {...props}
        userBoxes={[{ ...box, id: "saved", source: "manual" }] as never}
        rasterMaskRecords={[{ ...record, id: "saved", source: "annotation" }]}
      />,
    );
    select.mockClear();
    fireEvent.mouseDown(screen.getByTestId("konva-stage"), { clientX: 10, clientY: 10, button: 0 });
    expect(select).toHaveBeenLastCalledWith("saved", { shift: false });
  });

  it("does not render a persisted hidden raster mask while retaining its row data", () => {
    const annotation = {
      id: "hidden-mask",
      cls: "car",
      source: "manual" as const,
      conf: 1,
      is_hidden: true,
      geometry: {
        type: "raster_mask" as const,
        mask: {
          encoding: "coco_rle_ref" as const,
          size: [2, 2] as [number, number],
          object_key: "mask.json",
          sha256: "a".repeat(64),
          runs: 2,
          bytes: 4,
        },
      },
    };
    const record = {
      id: annotation.id,
      source: "annotation" as const,
      image: document.createElement("canvas"),
      bounds: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      area: 1,
      componentCount: 1,
      holeCount: 0,
      boundaryPixelCount: 1,
      sourceWidth: 2,
      sourceHeight: 2,
      crop: { x: 0, y: 0, width: 2, height: 2, alpha: new Uint8Array([255, 0, 0, 0]) },
      zOrder: 0,
      selected: false,
      cacheKey: "hidden-mask-cache",
      preview: false,
    } satisfies RasterMaskRenderRecord<"annotation">;

    render(
      <ImageStage
        {...defaults}
        tool="select"
        userBoxes={[annotation] as never}
        rasterMaskRecords={[record]}
      />,
    );

    expect(screen.queryByTestId("raster-mask-annotation")).toBeNull();
  });

  it("keeps the pending drag update when cursor feedback rebuilds parent callbacks", () => {
    render(<CursorOwner />);
    const canvas = screen.getByTestId("konva-stage");
    const stage = screen.getByTestId("workbench-stage");
    fireEvent.mouseDown(canvas, { clientX: 10, clientY: 20, button: 0 });
    expect(stage).toHaveAttribute("data-drag-kind", "draw");
    fireEvent.mouseMove(canvas, { clientX: 60, clientY: 80, buttons: 1 });
    expect(screen.getByTestId("cursor")).toHaveTextContent("0.6,0.8");
    flushFrame();
    expect(stage).toHaveAttribute("data-drag-changed", "true");
    fireEvent.mouseUp(canvas, { clientX: 60, clientY: 80, button: 0 });
    expect(commit).toHaveBeenCalledWith(
      { x: 0.1, y: 0.2, w: 0.5, h: 0.6000000000000001 },
      { x: 0.6, y: 0.8 },
    );
  });

  it("commits the last pointer position when release happens before the animation frame", () => {
    render(<ImageStage {...defaults} />);
    const canvas = screen.getByTestId("konva-stage");
    fireEvent.mouseDown(canvas, { clientX: 10, clientY: 20, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 60, clientY: 80, buttons: 1 });
    fireEvent.mouseUp(canvas, { clientX: 60, clientY: 80, button: 0 });
    expect(commit).toHaveBeenCalledOnce();
    expect(commit.mock.calls[0][0]).toMatchObject({ x: 0.1, y: 0.2, w: 0.5 });
    flushFrame();
    expect(commit).toHaveBeenCalledOnce();
    expect(screen.getByTestId("workbench-stage")).toHaveAttribute("data-drag-kind", "none");
  });

  it("drops a pending move when the stage unmounts", () => {
    const view = render(<ImageStage {...defaults} />);
    const canvas = screen.getByTestId("konva-stage");
    fireEvent.mouseDown(canvas, { clientX: 10, clientY: 20, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 60, clientY: 80, buttons: 1 });
    expect(frames.size).toBeGreaterThan(0);
    view.unmount();
    expect(frames.size).toBe(0);
    flushFrame();
    fireEvent.mouseUp(window);
    expect(commit).not.toHaveBeenCalled();
  });

  it.each([
    { width: 1280, height: 720, angle: 35, scale: 1, tx: 0, ty: 0 },
    { width: 720, height: 1280, angle: 35, scale: 0.6, tx: 70, ty: 30 },
    { width: 1280, height: 720, angle: -35, scale: 1.2, tx: -90, ty: 40 },
  ])(
    "rotates in image pixels on a $width × $height image",
    ({ width, height, angle, scale, tx, ty }) => {
      const geometry = {
        type: "rotated_bbox" as const,
        cx: 0.5,
        cy: 0.5,
        w: 0.2,
        h: 0.2,
        angle: 0,
      };
      const onCommitRotateBbox = vi.fn();
      render(
        <ImageStage
          {...defaults}
          imageWidth={width}
          imageHeight={height}
          tool="select"
          selectedId="rotated"
          userBoxes={[
            {
              id: "rotated",
              cls: "car",
              source: "manual",
              conf: 1,
              x: 0.4,
              y: 0.4,
              w: 0.2,
              h: 0.2,
              geometry,
            },
          ]}
          vp={{ scale, tx, ty }}
          onCommitRotateBbox={onCommitRotateBbox}
        />,
      );
      const handle = document.querySelector('[data-konva="Circle"]')!;
      const centerX = geometry.cx * width * scale + tx;
      const centerY = geometry.cy * height * scale + ty;
      const radius = 100 * scale;
      fireEvent.mouseDown(handle, { clientX: centerX, clientY: centerY - radius, button: 0 });
      expect(screen.getByTestId("workbench-stage")).toHaveAttribute("data-drag-kind", "rotateBox");
      fireEvent.mouseMove(window, {
        clientX: centerX + radius * Math.sin((angle * Math.PI) / 180),
        clientY: centerY - radius * Math.cos((angle * Math.PI) / 180),
        buttons: 1,
      });
      fireEvent.mouseUp(window, { button: 0 });
      expect(onCommitRotateBbox).toHaveBeenCalledOnce();
      const [id, before, after] = onCommitRotateBbox.mock.calls[0];
      expect(id).toBe("rotated");
      expect(before).toEqual(geometry);
      expect(after).toMatchObject({ ...geometry, angle: expect.any(Number) });
      expect(after.angle).toBeCloseTo((angle + 360) % 360, 8);
    },
  );

  it.each([
    { width: 1280, height: 720, scale: 1, tx: 0, ty: 0 },
    { width: 720, height: 1280, scale: 0.6, tx: 70, ty: 30 },
  ])("moves a selected rotated box without changing its size or angle: %j", (view) => {
    const { width, height, scale, tx, ty } = view;
    const onCommitRotateBbox = vi.fn();
    const props = {
      ...rotatedProps,
      imageWidth: width,
      imageHeight: height,
      vp: { scale, tx, ty },
      onCommitRotateBbox,
    };
    const rendered = render(<ImageStage {...props} />);
    const startX = rotatedGeometry.cx * width * scale + tx;
    const startY = rotatedGeometry.cy * height * scale + ty;
    fireEvent.mouseDown(rotatedBody(), { clientX: startX, clientY: startY, button: 0 });
    expect(screen.getByTestId("workbench-stage")).toHaveAttribute(
      "data-drag-kind",
      "moveRotatedBox",
    );
    fireEvent.mouseMove(window, {
      clientX: startX + 0.1 * width * scale,
      clientY: startY - 0.1 * height * scale,
      buttons: 1,
    });
    flushFrame();
    expect(Number(rotatedGroup().getAttribute("data-x"))).toBeCloseTo(0.6 * width);
    expect(Number(rotatedGroup().getAttribute("data-y"))).toBeCloseTo(0.4 * height);
    expect(rotatedGroup()).toHaveAttribute("data-rotation", "35");
    expect(onCommitRotateBbox).not.toHaveBeenCalled();
    fireEvent.mouseMove(window, {
      clientX: startX + 0.2 * width * scale,
      clientY: startY - 0.2 * height * scale,
      buttons: 1,
    });
    fireEvent.mouseUp(window, { button: 0 });
    expect(onCommitRotateBbox).toHaveBeenCalledOnce();
    expect(onCommitRotateBbox).toHaveBeenCalledWith("rotated", rotatedGeometry, {
      ...rotatedGeometry,
      cx: expect.closeTo(0.7),
      cy: expect.closeTo(0.3),
    });
    const saved = onCommitRotateBbox.mock.calls[0][2];
    rendered.rerender(<ImageStage {...props} pendingGeomMap={new Map([["rotated", saved]])} />);
    expect(Number(rotatedGroup().getAttribute("data-x"))).toBeCloseTo(0.7 * width);
    expect(Number(rotatedGroup().getAttribute("data-y"))).toBeCloseTo(0.3 * height);
    flushFrame();
    expect(onCommitRotateBbox).toHaveBeenCalledOnce();
  });

  it("does not save a rotated box when its body is clicked without movement", () => {
    const onCommitRotateBbox = vi.fn();
    render(<ImageStage {...rotatedProps} onCommitRotateBbox={onCommitRotateBbox} />);
    fireEvent.mouseDown(rotatedBody(), { clientX: 50, clientY: 50, button: 0 });
    fireEvent.mouseUp(window, { button: 0 });
    expect(onCommitRotateBbox).not.toHaveBeenCalled();
  });

  it.each([
    { readOnly: true },
    { selectedId: null },
    { selectedIds: ["rotated", "other"] },
    { userBoxes: [{ ...rotatedProps.userBoxes[0], is_locked: true }] },
  ])("does not move a rotated box when editing is unavailable: %j", (guard) => {
    const onCommitRotateBbox = vi.fn();
    render(<ImageStage {...rotatedProps} {...guard} onCommitRotateBbox={onCommitRotateBbox} />);
    fireEvent.mouseDown(rotatedBody(), { clientX: 50, clientY: 50, button: 0 });
    fireEvent.mouseMove(window, { clientX: 70, clientY: 30, buttons: 1 });
    fireEvent.mouseUp(window, { button: 0 });
    expect(onCommitRotateBbox).not.toHaveBeenCalled();
    expect(rotatedGroup()).toHaveAttribute("data-x", "50");
    expect(rotatedGroup()).toHaveAttribute("data-y", "50");
  });

  it.each([{ mediaKey: "image-b" }, { readOnly: true }])(
    "cancels a rotated box move when its owner changes: %j",
    (next) => {
      const onCommitRotateBbox = vi.fn();
      const props = { ...rotatedProps, onCommitRotateBbox };
      const view = render(<ImageStage {...props} />);
      fireEvent.mouseDown(rotatedBody(), { clientX: 50, clientY: 50, button: 0 });
      expect(screen.getByTestId("workbench-stage")).toHaveAttribute(
        "data-drag-kind",
        "moveRotatedBox",
      );
      fireEvent.mouseMove(window, { clientX: 70, clientY: 30, buttons: 1 });
      view.rerender(<ImageStage {...props} {...next} />);
      flushFrame();
      fireEvent.mouseUp(window, { button: 0 });
      expect(onCommitRotateBbox).not.toHaveBeenCalled();
    },
  );

  it.each([{ mediaKey: "image-b" }, { tool: "select" as const }, { readOnly: true }])(
    "ends the drag when its editing context changes: %j",
    (next) => {
      const view = render(<ImageStage {...defaults} />);
      const canvas = screen.getByTestId("konva-stage");
      fireEvent.mouseDown(canvas, { clientX: 10, clientY: 20, button: 0 });
      fireEvent.mouseMove(canvas, { clientX: 60, clientY: 80, buttons: 1 });
      flushFrame();
      expect(screen.getByTestId("workbench-stage")).toHaveAttribute("data-drag-changed", "true");
      fireEvent.mouseMove(canvas, { clientX: 80, clientY: 90, buttons: 1 });
      expect(frames.size).toBeGreaterThan(0);
      view.rerender(<ImageStage {...defaults} {...next} />);
      flushFrame();
      expect(screen.getByTestId("workbench-stage")).toHaveAttribute("data-drag-kind", "none");
      fireEvent.mouseUp(canvas, { clientX: 60, clientY: 80, button: 0 });
      expect(commit).not.toHaveBeenCalled();
    },
  );
});

describe("ImageStage polygon fill preview", () => {
  it.each([true, false])(
    "previews cursor geometry with closed=%s and configured fill",
    (closed) => {
      const polygonDraft = {
        points: [
          [0.1, 0.1],
          [0.8, 0.1],
        ] as [number, number][],
        addPoint: vi.fn(),
        close: vi.fn(),
        cancel: vi.fn(),
        closed,
      };
      draftVisual.fillOpacity = 0.37;
      const props = {
        ...defaults,
        tool: closed ? ("polygon" as const) : ("polyline" as const),
        polygonDraft,
      };
      const view = render(<ImageStage {...props} />);
      fireEvent.mouseMove(screen.getByTestId("konva-stage"), { clientX: 80, clientY: 80 });
      const line = () => view.container.querySelector('[data-konva="Line"][data-dash]')!;
      expect(line()).toHaveAttribute("data-closed", String(closed));
      expect(JSON.parse(line().getAttribute("data-points")!)).toEqual([10, 10, 80, 10, 80, 80]);
      if (closed) expect(line().getAttribute("data-fill")).toContain("0.37");
      else expect(line()).not.toHaveAttribute("data-fill");
      draftVisual.fillOpacity = 0;
      view.rerender(<ImageStage {...props} />);
      if (closed) expect(line().getAttribute("data-fill")).toMatch(/,\s*0\)$/);
      expect(polygonDraft.close).not.toHaveBeenCalled();
      view.unmount();
    },
  );
});
