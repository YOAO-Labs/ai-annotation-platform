// MaskTool 单测：onPointerDown 返回 maskBrush DragInit + 自动 beginBlank。

import { describe, expect, it, vi } from "vitest";
import { MaskTool } from "./MaskTool";
import type { ToolPointerContext, DragInit } from "./index";
import type { UseMaskEditorReturn } from "../../state/useMaskEditor";

function fakeMaskEditor(active: boolean): UseMaskEditorReturn {
  return {
    active,
    mode: "brush",
    tool: "brush",
    brushShape: "circle",
    connectivity: 4,
    radius: 16,
    dirty: false,
    buffer: null,
    backend: "dense",
    tiledTiles: [],
    tiledResources: null,
    tiledReadOnly: false,
    commitInFlight: false,
    revision: 0,
    canUndo: false,
    canRedo: false,
    historyResources: {
      maxBytes: 32 * 1024 * 1024,
      maxCommands: 100,
      retainedBytes: 0,
      undoCommands: 0,
      redoCommands: 0,
      evictedCommands: 0,
      droppedCommands: 0,
    },
    operationPreview: null,
    instanceOperationPreview: null,
    operationStatus: "idle",
    operationError: undefined,
    beginBlank: vi.fn(),
    initFromPolygon: vi.fn(),
    initFromRle: vi.fn(),
    materializeFromRle: vi.fn(),
    paintAt: vi.fn(),
    beginStroke: vi.fn(),
    endStroke: vi.fn(),
    undo: vi.fn(),
    redo: vi.fn(),
    setMode: vi.fn(),
    setTool: vi.fn(),
    setBrushShape: vi.fn(),
    setConnectivity: vi.fn(),
    setRadius: vi.fn(),
    previewOperation: vi.fn(() => true),
    runOperation: vi.fn(async () => true),
    previewInstanceOperation: vi.fn(() => true),
    runInstanceOperation: vi.fn(async () => true),
    confirmOperation: vi.fn(() => true),
    cancelOperation: vi.fn(),
    cancel: vi.fn(),
    commitToPolygon: vi.fn(() => null),
    commitToRle: vi.fn(() => null),
    commitToRleAsync: vi.fn(async () => null),
    setViewport: vi.fn(),
  };
}

function baseCtx(
  maskEditor: UseMaskEditorReturn | undefined,
  readOnly = false,
): ToolPointerContext {
  return {
    pt: { x: 0.5, y: 0.5 },
    evt: {} as unknown as MouseEvent,
    vp: { scale: 1, tx: 0, ty: 0 },
    activeClass: "",
    imgW: 800,
    imgH: 600,
    spacePan: false,
    readOnly,
    pendingDrawing: false,
    onClearSelection: () => {},
    maskEditor,
  };
}

describe("MaskTool", () => {
  it("readOnly 时返回 null", () => {
    const me = fakeMaskEditor(false);
    expect(MaskTool.onPointerDown!(baseCtx(me, true))).toBeNull();
    expect(me.beginBlank).not.toHaveBeenCalled();
  });

  it("没有 maskEditor 时返回 null", () => {
    expect(MaskTool.onPointerDown!(baseCtx(undefined))).toBeNull();
  });

  it("未激活 → beginBlank + paintAt + 返回 maskBrush", () => {
    const me = fakeMaskEditor(false);
    const init = MaskTool.onPointerDown!(baseCtx(me)) as Extract<DragInit, { kind: "maskBrush" }>;
    expect(me.beginBlank).toHaveBeenCalledTimes(1);
    expect(me.paintAt).toHaveBeenCalledWith(0.5 * 800, 0.5 * 600);
    expect(init.kind).toBe("maskBrush");
    expect(init.lastX).toBe(400);
    expect(init.lastY).toBe(300);
  });

  it("已激活 → 不再 beginBlank, 直接 paintAt 一次", () => {
    const me = fakeMaskEditor(true);
    const init = MaskTool.onPointerDown!(baseCtx(me)) as Extract<DragInit, { kind: "maskBrush" }>;
    expect(me.beginBlank).not.toHaveBeenCalled();
    expect(me.paintAt).toHaveBeenCalledTimes(1);
    expect(init.kind).toBe("maskBrush");
  });

  it("lasso 工具只创建像素路径草稿，不提前修改 Buffer", () => {
    const me = fakeMaskEditor(true);
    me.tool = "lasso_subtract";
    const init = MaskTool.onPointerDown!(baseCtx(me)) as Extract<DragInit, { kind: "maskLasso" }>;
    expect(init).toEqual({ kind: "maskLasso", points: [[400, 300]] });
    expect(me.beginStroke).not.toHaveBeenCalled();
    expect(me.paintAt).not.toHaveBeenCalled();
  });

  it("flood fill 通过统一 operation runner 生成预览", () => {
    const me = fakeMaskEditor(true);
    me.tool = "fill_subtract";
    expect(MaskTool.onPointerDown!(baseCtx(me))).toBeNull();
    expect(me.runOperation).toHaveBeenCalledWith("fill_subtract", {
      type: "flood_fill",
      x: 400,
      y: 300,
      value: 0,
      connectivity: 4,
    });
    expect(me.paintAt).not.toHaveBeenCalled();
  });

  it("component 与 hole 工具按像素 membership 生成预览", () => {
    const component = fakeMaskEditor(true);
    component.tool = "component_keep";
    expect(MaskTool.onPointerDown!(baseCtx(component))).toBeNull();
    expect(component.runOperation).toHaveBeenCalledWith("component_keep", {
      type: "component",
      action: "keep",
      x: 400,
      y: 300,
      connectivity: 4,
    });

    const hole = fakeMaskEditor(true);
    hole.tool = "hole_fill";
    expect(MaskTool.onPointerDown!(baseCtx(hole))).toBeNull();
    expect(hole.runOperation).toHaveBeenCalledWith("hole_fill", {
      type: "fill_holes",
      mode: "hit",
      x: 400,
      y: 300,
    });
  });

  it("copy component 只生成待原子提交的 instance plan", () => {
    const me = fakeMaskEditor(true);
    me.tool = "component_copy";
    expect(MaskTool.onPointerDown!(baseCtx(me))).toBeNull();
    expect(me.runInstanceOperation).toHaveBeenCalledWith("copy_component", {
      type: "copy_component",
      x: 400,
      y: 300,
      connectivity: 4,
    });
    expect(me.runOperation).not.toHaveBeenCalled();
  });
});
