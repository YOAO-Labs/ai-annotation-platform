import { describe, expect, it, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import {
  useMaskEditor,
  MASK_BRUSH_MIN_PX,
  MASK_BRUSH_MAX_PX,
  MASK_BRUSH_DEFAULT_PX,
} from "./useMaskEditor";
import { applyMaskPolygon } from "../stage/shared/geometry/maskOperations";
import { planMaskJoin } from "../stage/shared/geometry/maskInstanceOperations";
import { MaskBuffer } from "../stage/shared/geometry/maskBuffer";
import { decodeCocoRle, type CocoRle } from "../stage/shared/geometry/maskRle";
import { applyMaskMorphology } from "../stage/shared/geometry/maskOperations";
import type { RasterMaskWorkerPool } from "../stage/shared/rasterMaskWorkerPool";
import type {
  RasterMaskMorphologyRoiRequest,
  RasterMaskTileOverride,
  RasterMaskTileRect,
} from "../stage/shared/rasterMaskWorkerProtocol";
import {
  buildRasterMaskWorkerSession,
  decodeRasterMaskSessionTile,
  mergeRasterMaskSessionTiles,
  morphologyRasterMaskSessionRoi,
  type RasterMaskWorkerSession,
} from "../stage/shared/rasterMaskWorkerRuntime";

class HookTileBackend {
  readonly sessions = new Map<string, RasterMaskWorkerSession>();
  mergeGate: Promise<void> | null = null;

  registerSession(
    sessionId: string,
    sha256: string,
    rle: { size: [number, number]; counts: number[] },
  ): void {
    this.sessions.set(
      sessionId,
      buildRasterMaskWorkerSession(sha256, {
        size: rle.size,
        counts: Uint32Array.from(rle.counts),
      }),
    );
  }

  releaseSession(sessionId: string): void {
    this.sessions.delete(sessionId);
  }

  async decodeTile(sessionId: string, sha256: string, rect: RasterMaskTileRect) {
    const session = this.sessions.get(sessionId);
    if (!session || session.sha256 !== sha256) throw new Error("missing test session");
    return { sessionId, sha256, rect, alpha: decodeRasterMaskSessionTile(session, rect) };
  }

  async mergeTiles(sessionId: string, sha256: string, tiles: readonly RasterMaskTileOverride[]) {
    await this.mergeGate;
    const session = this.sessions.get(sessionId);
    if (!session || session.sha256 !== sha256) throw new Error("missing test session");
    return { sessionId, sha256, rle: mergeRasterMaskSessionTiles(session, tiles) };
  }

  async morphologyRoi(request: Omit<RasterMaskMorphologyRoiRequest, "kind" | "id">) {
    const session = this.sessions.get(request.sessionId);
    if (!session || session.sha256 !== request.sha256) throw new Error("missing test session");
    return {
      kind: "morphology_roi" as const,
      id: 1,
      ok: true as const,
      sessionId: request.sessionId,
      sha256: request.sha256,
      ...morphologyRasterMaskSessionRoi(session, request),
    };
  }
}

describe("useMaskEditor · 初始态", () => {
  it("初始非 active，dirty=false，buffer=null，revision=0", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 50, height: 50 }));
    expect(result.current.active).toBe(false);
    expect(result.current.dirty).toBe(false);
    expect(result.current.buffer).toBeNull();
    expect(result.current.mode).toBe("brush");
    expect(result.current.radius).toBe(MASK_BRUSH_DEFAULT_PX);
    expect(result.current.revision).toBe(0);
  });

  it("initialRadius clamp 到合法区间", () => {
    const { result: small } = renderHook(() =>
      useMaskEditor({ width: 50, height: 50, initialRadius: -10 }),
    );
    expect(small.current.radius).toBe(MASK_BRUSH_MIN_PX);
    const { result: big } = renderHook(() =>
      useMaskEditor({ width: 50, height: 50, initialRadius: 9999 }),
    );
    expect(big.current.radius).toBe(MASK_BRUSH_MAX_PX);
  });
});

describe("useMaskEditor · beginBlank / initFromPolygon", () => {
  it("initFromRle / commitToRle 保持像素合同", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 3, height: 2 }));
    const rle = {
      encoding: "coco_rle" as const,
      size: [2, 3] as [number, number],
      counts: [1, 2, 2, 1],
    };
    act(() => {
      result.current.initFromRle(rle);
    });
    expect(result.current.buffer?.countSet()).toBe(3);
    expect(result.current.commitToRle()).toEqual(rle);
  });

  it("initFromRle 拒绝尺寸不匹配", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 3, height: 2 }));
    expect(() =>
      act(() => {
        result.current.initFromRle({ encoding: "coco_rle", size: [3, 2], counts: [6] });
      }),
    ).toThrow(/does not match/);
  });

  it("beginBlank 进入 active；buffer 全 0", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    act(() => {
      result.current.beginBlank();
    });
    expect(result.current.active).toBe(true);
    expect(result.current.buffer).not.toBeNull();
    expect(result.current.buffer!.countSet()).toBe(0);
  });

  it("initFromPolygon 填充矩形 buffer", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    act(() => {
      result.current.initFromPolygon([
        [5, 5],
        [25, 5],
        [25, 25],
        [5, 25],
      ]);
    });
    expect(result.current.active).toBe(true);
    expect(result.current.buffer!.countSet()).toBeGreaterThan(300);
  });
});

describe("useMaskEditor · paintAt / mode / radius", () => {
  it("paintAt brush 模式在 buffer 留下圆形", () => {
    const { result } = renderHook(() =>
      useMaskEditor({ width: 80, height: 80, initialRadius: 10 }),
    );
    act(() => {
      result.current.beginBlank();
    });
    const revAfterBegin = result.current.revision;
    act(() => {
      result.current.paintAt(40, 40);
    });
    expect(result.current.dirty).toBe(true);
    expect(result.current.buffer!.get(40, 40)).toBe(255);
    expect(result.current.buffer!.countSet()).toBeGreaterThan(250);
    expect(result.current.revision).toBeGreaterThan(revAfterBegin);
  });

  it("paintAt erase 模式抹掉已画区域", () => {
    const { result } = renderHook(() =>
      useMaskEditor({ width: 80, height: 80, initialRadius: 10 }),
    );
    act(() => {
      result.current.beginBlank();
    });
    act(() => {
      result.current.paintAt(40, 40);
    });
    const before = result.current.buffer!.countSet();
    expect(before).toBeGreaterThan(0);
    act(() => {
      result.current.setMode("erase");
    });
    act(() => {
      result.current.setRadius(12);
    });
    act(() => {
      result.current.paintAt(40, 40);
    });
    expect(result.current.buffer!.countSet()).toBe(0);
  });

  it("paintAt 非 active 时静默", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    act(() => {
      result.current.paintAt(15, 15);
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.buffer).toBeNull();
  });

  it("setRadius clamp", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    act(() => {
      result.current.setRadius(-5);
    });
    expect(result.current.radius).toBe(MASK_BRUSH_MIN_PX);
    act(() => {
      result.current.setRadius(1e6);
    });
    expect(result.current.radius).toBe(MASK_BRUSH_MAX_PX);
    act(() => {
      result.current.setRadius(NaN);
    });
    expect(result.current.radius).toBe(MASK_BRUSH_DEFAULT_PX);
  });
});

describe("useMaskEditor · cancel / commitToPolygon", () => {
  it("cancel 清空 buffer 与 active", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 50, height: 50 }));
    act(() => {
      result.current.beginBlank();
    });
    act(() => {
      result.current.paintAt(25, 25);
    });
    expect(result.current.dirty).toBe(true);
    act(() => {
      result.current.cancel();
    });
    expect(result.current.active).toBe(false);
    expect(result.current.buffer).toBeNull();
    expect(result.current.dirty).toBe(false);
  });

  it("commitToPolygon 空 mask 返回 null", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    act(() => {
      result.current.beginBlank();
    });
    expect(result.current.commitToPolygon()).toBeNull();
  });

  it("commitToPolygon 有内容时返回外环顶点", () => {
    const { result } = renderHook(() =>
      useMaskEditor({ width: 80, height: 80, initialRadius: 15 }),
    );
    act(() => {
      result.current.beginBlank();
    });
    act(() => {
      result.current.paintAt(40, 40);
    });
    const out = result.current.commitToPolygon();
    expect(out).not.toBeNull();
    expect(out!.points.length).toBeGreaterThanOrEqual(3);
    expect(out!.multipleComponents).toBe(false);
  });

  it("commitToPolygon 非 active 时返回 null", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 30, height: 30 }));
    expect(result.current.commitToPolygon()).toBeNull();
  });
});

describe("useMaskEditor · stroke undo / redo", () => {
  it("records one history step for a complete pointer stroke", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 8, height: 8, initialRadius: 1 }));
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(2, 2);
      result.current.paintAt(3, 2);
      result.current.endStroke();
    });
    expect(result.current.canUndo).toBe(true);
    const painted = result.current.buffer?.countSet() ?? 0;
    expect(painted).toBeGreaterThan(0);

    act(() => result.current.undo());
    expect(result.current.buffer?.countSet()).toBe(0);
    expect(result.current.canRedo).toBe(true);

    act(() => result.current.redo());
    expect(result.current.buffer?.countSet()).toBe(painted);
  });

  it("does not encode before/after RLE and records cross-tile strokes as bit patches", () => {
    const toRle = vi.spyOn(MaskBuffer.prototype, "toRle");
    const { result } = renderHook(() =>
      useMaskEditor({
        width: 514,
        height: 4,
        initialRadius: 1,
        deviceMemory: 2,
      }),
    );
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(511, 2);
      result.current.paintAt(512, 2);
      result.current.endStroke();
    });
    expect(toRle).not.toHaveBeenCalled();
    expect(result.current.historyResources).toMatchObject({
      maxBytes: 16 * 1024 * 1024,
      undoCommands: 1,
      redoCommands: 0,
    });
    expect(result.current.historyResources.retainedBytes).toBeLessThan(1024);
    const painted = result.current.buffer?.countSet();
    act(() => result.current.undo());
    expect(result.current.buffer?.countSet()).toBe(0);
    act(() => result.current.redo());
    expect(result.current.buffer?.countSet()).toBe(painted);
    toRle.mockRestore();
  });

  it("does not retain a no-op stroke", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 8, height: 8, initialRadius: 1 }));
    act(() => result.current.beginBlank());
    act(() => result.current.setMode("erase"));
    act(() => {
      result.current.beginStroke();
      result.current.paintAt(2, 2);
      result.current.endStroke();
    });
    expect(result.current.canUndo).toBe(false);
    expect(result.current.historyResources.retainedBytes).toBe(0);
  });

  it("evicts the oldest undo by byte budget and clears redo on a new stroke", () => {
    const { result } = renderHook(() =>
      useMaskEditor({
        width: 8,
        height: 8,
        initialRadius: 1,
        historyMaxBytes: 110,
      }),
    );
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(1, 1);
      result.current.endStroke();
      result.current.beginStroke();
      result.current.paintAt(6, 6);
      result.current.endStroke();
    });
    expect(result.current.historyResources).toMatchObject({ undoCommands: 1, evictedCommands: 1 });
    act(() => result.current.undo());
    expect(result.current.buffer?.get(1, 1)).toBe(255);
    expect(result.current.buffer?.get(6, 6)).toBe(0);
    expect(result.current.canRedo).toBe(true);
    act(() => {
      result.current.beginStroke();
      result.current.paintAt(4, 4);
      result.current.endStroke();
    });
    expect(result.current.canRedo).toBe(false);
    expect(result.current.historyResources.redoCommands).toBe(0);
  });
});

describe("useMaskEditor · operation preview / command", () => {
  it("preview 不修改 live buffer，confirm 后只产生一个可撤销 command", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 6, height: 4 }));
    act(() => result.current.beginBlank());
    const sourceRevision = result.current.revision;
    const operation = applyMaskPolygon(result.current.buffer!.data, 6, 4, {
      points: [
        [1, 1],
        [5, 1],
        [5, 3],
        [1, 3],
      ],
      value: 255,
    });

    let accepted = false;
    act(() => {
      accepted = result.current.previewOperation("lasso_add", operation, sourceRevision);
    });
    expect(accepted).toBe(true);
    expect(result.current.buffer?.countSet()).toBe(0);
    expect(result.current.dirty).toBe(false);
    expect(result.current.canUndo).toBe(false);
    expect(result.current.operationPreview?.report.afterArea).toBe(8);

    act(() => result.current.confirmOperation());
    expect(result.current.operationPreview).toBeNull();
    expect(result.current.buffer?.countSet()).toBe(8);
    expect(result.current.dirty).toBe(true);
    expect(result.current.canUndo).toBe(true);

    act(() => result.current.undo());
    expect(result.current.buffer?.countSet()).toBe(0);
    act(() => result.current.redo());
    expect(result.current.buffer?.countSet()).toBe(8);
  });

  it("确认 no-op 操作不写入 history", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 6, height: 4 }));
    act(() => result.current.beginBlank());
    const noOp = applyMaskPolygon(result.current.buffer!.data, 6, 4, {
      points: [],
      value: 255,
    });
    act(() => result.current.previewOperation("no-op", noOp));
    let confirmed = true;
    act(() => {
      confirmed = result.current.confirmOperation();
    });
    expect(confirmed).toBe(false);
    expect(result.current.operationPreview).toBeNull();
    expect(result.current.canUndo).toBe(false);
    expect(result.current.historyResources.retainedBytes).toBe(0);
  });

  it("cancel preview 不 dirty，旧 revision 的 Worker 结果被拒绝", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 5, height: 5, initialRadius: 1 }));
    act(() => result.current.beginBlank());
    const staleRevision = result.current.revision;
    const stale = applyMaskPolygon(result.current.buffer!.data, 5, 5, {
      points: [
        [0, 0],
        [3, 0],
        [3, 3],
        [0, 3],
      ],
      value: 255,
    });
    act(() => result.current.paintAt(4, 4));

    let accepted = true;
    act(() => {
      accepted = result.current.previewOperation("stale", stale, staleRevision);
    });
    expect(accepted).toBe(false);
    expect(result.current.operationPreview).toBeNull();

    const current = applyMaskPolygon(result.current.buffer!.data, 5, 5, {
      points: [
        [0, 0],
        [2, 0],
        [2, 2],
        [0, 2],
      ],
      value: 255,
    });
    act(() => {
      result.current.previewOperation("current", current, result.current.revision);
      result.current.cancelOperation();
    });
    expect(result.current.operationPreview).toBeNull();
    expect(result.current.buffer?.get(4, 4)).toBe(255);
  });

  it("操作取消保留已有 XOR history", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 8, height: 8, initialRadius: 1 }));
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(2, 2);
      result.current.endStroke();
    });
    const retainedBytes = result.current.historyResources.retainedBytes;
    const preview = applyMaskPolygon(result.current.buffer!.data, 8, 8, {
      points: [
        [4, 4],
        [7, 4],
        [7, 7],
        [4, 7],
      ],
      value: 255,
    });
    act(() => {
      result.current.previewOperation("lasso_add", preview);
      result.current.cancelOperation();
    });
    expect(result.current.historyResources.retainedBytes).toBe(retainedBytes);
    expect(result.current.canUndo).toBe(true);
    act(() => result.current.undo());
    expect(result.current.buffer?.countSet()).toBe(0);
  });

  it("materializeFromRle 明确进入 dirty，不伪造像素笔画", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 3, height: 2 }));
    const rle = {
      encoding: "coco_rle" as const,
      size: [2, 3] as [number, number],
      counts: [1, 2, 2, 1],
    };
    act(() => result.current.materializeFromRle(rle));
    expect(result.current.active).toBe(true);
    expect(result.current.dirty).toBe(true);
    expect(result.current.commitToRle()).toEqual(rle);
    expect(result.current.canUndo).toBe(false);
  });

  it("高级工具与 brush/erase 兼容 mode 同步", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 3, height: 2 }));
    act(() => result.current.setTool("lasso_add"));
    expect(result.current.tool).toBe("lasso_add");
    expect(result.current.mode).toBe("brush");
    act(() => result.current.setMode("erase"));
    expect(result.current.tool).toBe("erase");
    act(() => result.current.setBrushShape("square"));
    expect(result.current.brushShape).toBe("square");
    act(() => result.current.setTool("component_keep"));
    expect(result.current.tool).toBe("component_keep");
    expect(result.current.mode).toBe("erase");
  });

  it("runOperation 在小图同步计算并进入 preview 状态", async () => {
    const { result } = renderHook(() => useMaskEditor({ width: 4, height: 3 }));
    act(() => result.current.beginBlank());
    await act(async () => {
      expect(
        await result.current.runOperation("fill_add", {
          type: "flood_fill",
          x: 0,
          y: 0,
          value: 255,
          connectivity: 4,
        }),
      ).toBe(true);
    });
    expect(result.current.operationStatus).toBe("preview");
    expect(result.current.operationPreview?.report.afterArea).toBe(12);
    expect(result.current.buffer?.countSet()).toBe(0);
  });

  it("instance plan 只进入待提交预览，不能绕过原子提交修改 Buffer", async () => {
    const { result } = renderHook(() => useMaskEditor({ width: 5, height: 1 }));
    act(() =>
      result.current.initFromRle({
        encoding: "coco_rle",
        size: [1, 5],
        counts: [0, 1, 2, 2],
      }),
    );
    await act(async () => {
      expect(
        await result.current.runInstanceOperation("split_components", {
          type: "split_components",
          keep: "largest",
          connectivity: 4,
        }),
      ).toBe(true);
    });

    expect(result.current.instanceOperationPreview?.plan.resultAreas).toEqual([2, 1]);
    expect(result.current.buffer?.countSet()).toBe(3);
    expect(result.current.dirty).toBe(false);
    expect(result.current.confirmOperation()).toBe(false);
    expect(result.current.instanceOperationPreview).not.toBeNull();
    act(() => result.current.cancelOperation());
    expect(result.current.instanceOperationPreview).toBeNull();
    expect(result.current.buffer?.countSet()).toBe(3);
  });

  it("外部 join plan 复用同一 stale revision 隔离边界", () => {
    const { result } = renderHook(() => useMaskEditor({ width: 3, height: 1 }));
    act(() =>
      result.current.initFromRle({
        encoding: "coco_rle",
        size: [1, 3],
        counts: [0, 1, 2],
      }),
    );
    const sourceRevision = result.current.revision;
    const plan = planMaskJoin([Uint8Array.of(255, 0, 0), Uint8Array.of(0, 255, 0)], 3, 1);
    act(() => result.current.paintAt(2, 0));
    expect(result.current.previewInstanceOperation("join_masks", plan, sourceRevision)).toBe(false);
    expect(result.current.instanceOperationPreview).toBeNull();
  });
});

describe("useMaskEditor · tiled large canvas", () => {
  it("rejects full-canvas polygon initialization with a stable reason", () => {
    const backend = new HookTileBackend();
    const { result } = renderHook(() =>
      useMaskEditor({
        width: 4097,
        height: 512,
        workerPool: backend as unknown as RasterMaskWorkerPool,
      }),
    );

    expect(() =>
      result.current.initFromPolygon([
        [0, 0],
        [10, 0],
        [10, 10],
      ]),
    ).toThrow(expect.objectContaining({ reason: "large_mask_full_scan_required" }));
    expect(result.current.active).toBe(false);
  });

  it("keeps 8K brush/lasso/history/save on sparse tiles without a dense buffer", async () => {
    const backend = new HookTileBackend();
    const { result, unmount } = renderHook(() =>
      useMaskEditor({
        width: 8192,
        height: 8192,
        initialRadius: 2,
        workerPool: backend as unknown as RasterMaskWorkerPool,
        deviceMemory: 2,
      }),
    );

    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(511, 511);
      result.current.paintAt(512, 512);
      result.current.endStroke();
    });
    let painted = 0;
    await act(async () => {
      const rle = await result.current.commitToRleAsync();
      painted = rle?.counts.reduce((area, count, index) => area + (index % 2 ? count : 0), 0) ?? 0;
    });

    expect(result.current.backend).toBe("tiled");
    expect(result.current.buffer).toBeNull();
    expect(result.current.tiledResources).toMatchObject({ dirtyTiles: 4 });
    expect(result.current.canUndo).toBe(true);
    expect(painted).toBeGreaterThan(0);

    await act(async () => {
      result.current.undo();
      const undone = await result.current.commitToRleAsync();
      expect(undone?.counts).toEqual([67_108_864]);
    });
    await act(async () => {
      result.current.redo();
      const redone = await result.current.commitToRleAsync();
      const area = redone?.counts.reduce((sum, count, index) => sum + (index % 2 ? count : 0), 0);
      expect(area).toBe(painted);
    });

    unmount();
    expect(backend.sessions.size).toBe(0);
  });

  it("applies morphology only to the current ROI core and rejects full-scan tools", async () => {
    const backend = new HookTileBackend();
    const width = 4097;
    const height = 16;
    const { result } = renderHook(() =>
      useMaskEditor({
        width,
        height,
        initialRadius: 1,
        workerPool: backend as unknown as RasterMaskWorkerPool,
      }),
    );
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(100, 8);
      result.current.endStroke();
      result.current.setViewport({ x: 90, y: 2, width: 20, height: 12 });
    });
    let before: Uint8Array = new Uint8Array();
    await act(async () => {
      before = decodeCocoRle(await result.current.commitToRleAsync());
      expect(
        await result.current.runOperation("dilate", {
          type: "morphology",
          operation: "dilate",
          kernelShape: "disk",
          radius: 2,
        }),
      ).toBe(true);
    });
    let finalRle: CocoRle | null = null;
    await act(async () => {
      finalRle = await result.current.commitToRleAsync();
    });
    const after = decodeCocoRle(finalRle);
    const denseGolden = applyMaskMorphology(before, width, height, {
      operation: "dilate",
      kernelShape: "disk",
      radius: 2,
    }).alpha;
    for (let y = 2; y < 14; y += 1) {
      for (let x = 90; x < 110; x += 1) {
        expect(after[y * width + x]).toBe(denseGolden[y * width + x]);
      }
    }
    expect(after[0]).toBe(before[0]);

    await act(async () => {
      expect(
        await result.current.runOperation("fill_add", {
          type: "flood_fill",
          x: 100,
          y: 8,
          value: 255,
          connectivity: 4,
        }),
      ).toBe(false);
    });
    expect(result.current.operationStatus).toBe("error");
    expect(result.current.operationError).toMatchObject({
      reason: "large_mask_full_scan_required",
    });
  });

  it("enters a stable read-only state when visible tiles cannot fit the cache budget", async () => {
    const backend = new HookTileBackend();
    const { result } = renderHook(() =>
      useMaskEditor({
        width: 4097,
        height: 512,
        workerPool: backend as unknown as RasterMaskWorkerPool,
        tileMaxBytes: 1,
      }),
    );

    act(() => {
      result.current.beginBlank();
      result.current.setViewport({ x: 0, y: 0, width: 512, height: 512 });
    });

    await waitFor(() => expect(result.current.tiledReadOnly).toBe(true));
    expect(result.current.tiledResources).toMatchObject({
      admissionBlocked: true,
      liveTiles: 0,
      dirtyTiles: 0,
    });
    expect(result.current.operationStatus).toBe("idle");
  });

  it("locks the editor while a tiled merge is in flight", async () => {
    const backend = new HookTileBackend();
    let releaseMerge!: () => void;
    backend.mergeGate = new Promise<void>((resolve) => {
      releaseMerge = resolve;
    });
    const { result } = renderHook(() =>
      useMaskEditor({
        width: 4097,
        height: 512,
        workerPool: backend as unknown as RasterMaskWorkerPool,
      }),
    );
    act(() => {
      result.current.beginBlank();
      result.current.beginStroke();
      result.current.paintAt(100, 100);
      result.current.endStroke();
    });
    let commit!: Promise<unknown>;
    act(() => {
      commit = result.current.commitToRleAsync();
    });

    await waitFor(() => expect(result.current.commitInFlight).toBe(true));
    act(() => releaseMerge());
    await act(async () => {
      await commit;
    });
    expect(result.current.commitInFlight).toBe(false);
  });
});
