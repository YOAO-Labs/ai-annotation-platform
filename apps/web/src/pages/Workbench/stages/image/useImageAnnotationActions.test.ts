import { act, renderHook } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import type { Annotation, AnnotationResponse, CocoRleMaskRef } from "@/types";
import type { CocoRle } from "../../stage/shared/geometry/maskRle";
import type { MaskSaveResult } from "../../state/useMaskEditorSession";
import { useWorkbenchHotkeys } from "../../state/useWorkbenchHotkeys";
import {
  getBatchChangeTarget,
  hasUsableImageBounds,
  useImageAnnotationActions,
} from "./useImageAnnotationActions";

/** 结算当前排队的 decisionDialog,模拟用户点下某个按钮。 */
function settleDecisionDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  useDecisionDialogStore.getState().settle(value);
}

afterEach(() => {
  // decisionDialog store 是模块级单例,清空队列避免跨测试残留。
  act(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });
});

const nativeRequests = vi.hoisted(() => ({ upload: vi.fn() }));
vi.mock("@/api/rasterMasks", () => ({
  rasterMasksApi: { uploadTaskContent: nativeRequests.upload },
}));

const predictionMutations = vi.hoisted(() => ({ accept: vi.fn(), reject: vi.fn() }));
vi.mock("@/hooks/usePredictions", () => ({
  useAcceptPrediction: () => ({ mutate: vi.fn(), mutateAsync: predictionMutations.accept }),
  useRejectPrediction: () => ({ mutate: vi.fn(), mutateAsync: predictionMutations.reject }),
}));

vi.mock("../../state/useClipboard", () => ({
  useClipboard: () => ({ copy: vi.fn(), paste: vi.fn(), hasClipboard: false }),
}));

vi.mock("../../state/useWorkbenchAnnotationActions", () => ({
  useWorkbenchAnnotationActions: () => ({
    createBboxWithClass: vi.fn(),
    createRotatedBbox: vi.fn(),
    submitPolygon: vi.fn(),
  }),
}));

function box(id: string, cls = "Car"): Annotation {
  return {
    id,
    cls,
    x: 0.1,
    y: 0.2,
    w: 0.3,
    h: 0.4,
    source: "manual",
    conf: 1,
  };
}

function decisionHarness() {
  const s = {
    activeClass: "Car",
    confThreshold: 0,
    selectedId: "pred-p-0",
    selectedIds: ["pred-p-0"],
    videoFrameIndex: 0,
    editingClass: null,
    pendingDrawing: null,
    tool: "select",
    workbenchConfig: {
      common: { autoAdvanceOnDecide: true },
      image: { afterBoxCreate: "pick_class" },
    },
    setSelectedId: vi.fn(),
    setEditingClass: vi.fn(),
    setActiveClass: vi.fn(),
    setClipboard: vi.fn(),
    replaceSelected: vi.fn(),
    setPendingDrawing: vi.fn(),
  };
  const shapes = [0, 0, 10].map((frame_index) => ({
    geometry: { type: "video_bbox", frame_index, x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    class_name: "Car",
    confidence: 0.9,
  }));
  const args = {
    taskId: "task-1",
    projectId: "project-1",
    s,
    queryClient: {},
    history: { push: vi.fn(), pushBatch: vi.fn() },
    pushToast: vi.fn(),
    recordRecentClass: vi.fn(),
    annotationsData: [],
    annotationsRef: { current: [] },
    predictionsData: [{ id: "p", result: shapes }],
    userBoxes: [],
    stageGeom: { imgW: 100, imgH: 100, vpSize: { w: 100, h: 100 } },
    iouDedupThreshold: 0.7,
    classes: ["Car"],
    sam: { candidates: [], cancel: vi.fn(), cycle: vi.fn() },
    acceptNativeMask: vi.fn(),
    createAnnotationAsync: vi.fn(),
    updateAnnotationAsync: vi.fn(),
    mutations: {
      create: { mutate: vi.fn() },
      update: { mutate: vi.fn() },
      delete: { mutate: vi.fn() },
    },
    enqueueOnError: vi.fn(),
  };
  const view = renderHook(() => useImageAnnotationActions(args as never));
  return { ...view, args, s };
}

describe("SAM candidate actions", () => {
  function samHarness() {
    const view = decisionHarness();
    view.s.tool = "smart-point";
    Object.assign(view.args.sam, {
      activeIdx: 1,
      isRunning: false,
      canAcceptCandidates: true,
      candidates: [0.1, 0.5].map((x) => ({
        type: "rectanglelabels",
        points: [],
        bbox: { x, y: 0.2, width: 0.2, height: 0.3 },
        label: "Car",
      })),
    });
    view.rerender();
    return view;
  }

  it("button and canvas Enter open the same current-candidate class picker", () => {
    const view = samHarness();
    act(() => view.result.current.requestSamAccept());
    expect(view.result.current.samClassPickerActive).toBe(true);
    const buttonGeometry = view.result.current.samPendingGeom;
    expect(buttonGeometry?.x).toBe(0.5);
    act(() => view.result.current.handleSamCancelClass());
    act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })));
    expect(view.result.current.samPendingGeom).toEqual(buttonGeometry);
    expect(view.args.acceptNativeMask).not.toHaveBeenCalled();
    view.unmount();
  });

  it.each(["select", "button", "input"])(
    "leaves %s keyboard input to the focused control",
    (tag) => {
      const view = samHarness();
      const control = document.createElement(tag);
      document.body.append(control);
      act(() => {
        for (const key of ["Enter", "Escape", "Tab", "r"]) {
          control.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        }
      });
      expect(view.result.current.samClassPickerActive).toBe(false);
      expect(view.args.sam.cancel).not.toHaveBeenCalled();
      expect(view.args.sam.cycle).not.toHaveBeenCalled();
      control.remove();
      view.unmount();
    },
  );

  it("ignores repeats, IME and locked candidate acceptance", () => {
    const view = samHarness();
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", repeat: true }));
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", isComposing: true }));
    });
    expect(view.result.current.samClassPickerActive).toBe(false);
    Object.assign(view.args, { isLocked: true });
    view.rerender();
    act(() => view.result.current.requestSamAccept());
    expect(view.result.current.samClassPickerActive).toBe(false);
    view.unmount();
  });

  it("SAM capture and the global listener both yield Tab on an AI dock button", () => {
    const view = samHarness();
    view.unmount();
    const joint = renderHook(() => {
      const actions = useImageAnnotationActions(view.args as never);
      useWorkbenchHotkeys({
        s: view.s,
        history: view.args.history,
        annotationsRef: view.args.annotationsRef,
        classes: [],
        currentProject: null,
        aiBoxes: actions.aiBoxes,
        polygonDraftPoints: [],
        updateMutation: view.args.mutations.update,
        stageGeom: view.args.stageGeom,
        clipboard: {},
      } as never);
      return actions;
    });
    const button = document.createElement("button");
    document.body.append(button);
    const event = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
    act(() => button.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(false);
    expect(view.args.sam.cycle).not.toHaveBeenCalled();
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    expect(joint.result.current.samClassPickerActive).toBe(false);
    button.remove();
    joint.unmount();
  });
});

describe("ordinary prediction decisions", () => {
  beforeEach(() => {
    predictionMutations.accept.mockReset();
    predictionMutations.reject.mockReset();
  });

  it("等待成功才推进同帧选择，并合并同一候选的在途决策", async () => {
    let complete!: (value: unknown[]) => void;
    predictionMutations.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    expect(candidate).toBeDefined();
    let first: unknown;
    act(() => {
      first = view.result.current.handleAcceptPrediction(candidate);
      void view.result.current.handleAcceptPrediction(candidate);
      void view.result.current.handleRejectPrediction(candidate);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
    expect(predictionMutations.reject).not.toHaveBeenCalled();
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    await act(async () => {
      complete([{ id: "created" }]);
      await first;
    });
    expect(view.s.setSelectedId).toHaveBeenCalledWith("pred-p-1");
    expect(view.args.history.push).toHaveBeenCalledTimes(1);
    // A stale prediction query must not permit a second write after the response.
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
  });

  it("拒绝失败保留候选与选择，允许重试", async () => {
    predictionMutations.reject
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({});
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    await act(async () => {
      expect(await view.result.current.handleRejectPrediction(candidate)).toEqual({
        status: "failed",
      });
    });
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    expect(view.result.current.aiBoxes.some((b) => b.id === candidate.id)).toBe(true);
    await act(async () => {
      await view.result.current.handleRejectPrediction(candidate);
    });
    expect(predictionMutations.reject).toHaveBeenCalledTimes(2);
    expect(view.s.setSelectedId).toHaveBeenCalledWith("pred-p-1");
  });

  it("422 补选延续原决定并保留属性，只在最终成功后推进", async () => {
    predictionMutations.accept
      .mockRejectedValueOnce({ status: 422 })
      .mockResolvedValueOnce([{ id: "created" }]);
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    await act(async () => {
      expect(
        await view.result.current.handleAcceptPrediction(candidate, { reviewed: true }),
      ).toEqual({ status: "awaiting-class" });
    });
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    // A mutation notification can commit a render before the picker state update.
    view.rerender();
    view.s.editingClass = view.s.setEditingClass.mock.calls[0][0];
    view.rerender();
    await act(async () => {
      await view.result.current.handleCommitChangeClass("Car");
    });
    expect(predictionMutations.accept).toHaveBeenLastCalledWith({
      predictionId: "p",
      shapeIndex: 0,
      attributeOverrides: { reviewed: true },
      overrideClassName: "Car",
    });
    expect(view.s.setSelectedId).toHaveBeenCalledWith("pred-p-1");
    expect(view.args.history.push).toHaveBeenCalledTimes(1);
  });

  it.each(["task", "frame", "selection"])("迟到成功不能改变新的 %s 上下文", async (change) => {
    let complete!: (value: unknown[]) => void;
    predictionMutations.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const view = decisionHarness();
    let pending: unknown;
    act(() => {
      pending = view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
    });
    if (change === "task") view.args.taskId = "task-2";
    if (change === "frame") view.s.videoFrameIndex = 10;
    if (change === "selection") view.s.selectedId = "manual";
    view.rerender();
    await act(async () => {
      complete([{ id: "created" }]);
      await pending;
    });
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    if (change === "task") expect(view.args.history.push).not.toHaveBeenCalled();
  });

  it("来源/阈值隐藏不能解除成功防重；真实接受记录出现后撤销可重新接受", async () => {
    predictionMutations.accept.mockResolvedValue([{ id: "created" }]);
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    view.s.confThreshold = 1;
    view.rerender();
    view.s.confThreshold = 0;
    view.rerender();
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
    view.args.annotationsData = [
      { id: "created", parent_prediction_id: "p", attributes: { _shape_index: 0 } },
    ] as never;
    view.rerender();
    expect(view.result.current.aiBoxes.some((box) => box.id === candidate.id)).toBe(false);
    view.args.annotationsData = [];
    view.rerender();
    await act(async () => {
      await view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(2);
  });

  it("批量与单项共享在途保护，批量保留跨帧原有范围", async () => {
    let complete!: (value: unknown[]) => void;
    predictionMutations.accept.mockImplementation(({ shapeIndex }) =>
      shapeIndex === 0
        ? new Promise((resolve) => {
            complete = resolve;
          })
        : Promise.resolve([{ id: `created-${shapeIndex}` }]),
    );
    const view = decisionHarness();
    let single: unknown;
    let bulk: unknown;
    act(() => {
      single = view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
      bulk = view.result.current.handleAcceptAll();
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(3);
    await act(async () => {
      complete([{ id: "created" }]);
      await single;
      await bulk;
    });
    expect(view.args.history.push).toHaveBeenCalledTimes(3);
  });

  it.each(["task", "frame"])("补选弹层随所属 %s 退出，不能在新上下文重试", async (change) => {
    predictionMutations.accept.mockRejectedValue({ status: 422 });
    const view = decisionHarness();
    await act(async () => {
      await view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
    });
    view.s.editingClass = view.s.setEditingClass.mock.calls[0][0];
    view.rerender();
    view.s.setEditingClass.mockClear();
    if (change === "task") view.args.taskId = "task-2";
    else view.s.videoFrameIndex = 10;
    view.rerender();
    expect(view.s.setEditingClass).toHaveBeenCalledWith(null);
    await act(async () => {
      await view.result.current.handleCommitChangeClass("Car");
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
  });

  it("取消补选释放决定，下一次接受可以再次补选", async () => {
    predictionMutations.accept.mockRejectedValue({ status: 422 });
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    view.s.editingClass = view.s.setEditingClass.mock.calls[0][0];
    view.rerender();
    act(() => view.result.current.handleCancelChangeClass());
    view.s.editingClass = null;
    view.rerender();
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(2);
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
  });

  it("请求未返回时离开再回到同题，同一候选仍只有一次写入", async () => {
    let complete!: (value: unknown[]) => void;
    predictionMutations.accept.mockReturnValue(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    const view = decisionHarness();
    const candidate = view.result.current.aiBoxes[0];
    let pending: unknown;
    act(() => {
      pending = view.result.current.handleAcceptPrediction(candidate);
    });
    view.args.taskId = "task-2";
    view.rerender();
    view.args.taskId = "task-1";
    view.rerender();
    act(() => {
      void view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
    await act(async () => {
      complete([{ id: "created" }]);
      await pending;
    });
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
    await act(async () => {
      await view.result.current.handleAcceptPrediction(candidate);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(1);
    view.args.annotationsData = [
      { id: "created", parent_prediction_id: "p", attributes: { _shape_index: 0 } },
    ] as never;
    view.rerender();
    view.args.annotationsData = [];
    view.rerender();
    act(() => {
      void view.result.current.handleAcceptPrediction(view.result.current.aiBoxes[0]);
    });
    expect(predictionMutations.accept).toHaveBeenCalledTimes(2);
  });

  it("全部帧列表的显式按钮保留操作范围，但不推进当前帧选择", async () => {
    predictionMutations.accept.mockResolvedValue([{ id: "created" }]);
    const view = decisionHarness();
    const offFrame = view.result.current.aiBoxes[2];
    view.s.selectedId = offFrame.id;
    view.rerender();
    await act(async () => {
      expect(await view.result.current.handleAcceptPrediction(offFrame)).toEqual({
        status: "success",
      });
    });
    expect(predictionMutations.accept).toHaveBeenCalledWith(
      expect.objectContaining({ shapeIndex: 2 }),
    );
    expect(view.s.setSelectedId).not.toHaveBeenCalled();
  });
});

describe("useImageAnnotationActions module", () => {
  it("exports the hook", () => {
    expect(typeof useImageAnnotationActions).toBe("function");
  });

  it("resets prediction source visibility when the project changes", () => {
    const view = decisionHarness();
    view.args.predictionsData = [
      {
        id: "p",
        source: "ml_backend",
        result: [
          {
            geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
            class_name: "Car",
            confidence: 0.9,
          },
        ],
      },
    ] as never;
    view.rerender();
    act(() => view.result.current.predictionSourceFilter.onToggle("ml_backend", false));
    expect(view.result.current.aiBoxes).toHaveLength(0);
    predictionMutations.accept.mockReset();
    act(() => {
      void view.result.current.handleAcceptAll();
    });
    expect(predictionMutations.accept).not.toHaveBeenCalled();

    view.args.projectId = "project-2";
    view.rerender();
    expect(view.result.current.predictionSourceFilter.visibility.ml_backend).toBe(true);
    expect(view.result.current.aiBoxes).toHaveLength(1);
  });

  it("keeps batch acceptance on loaded source-visible candidates and excludes IoU duplicates", async () => {
    const view = decisionHarness();
    view.args.predictionsData = [
      {
        id: "p",
        source: "ml_backend",
        result: [0.1, 0.4, 0.7].map((x, shape_index) => ({
          geometry: { type: "bbox", x, y: 0.1, w: 0.2, h: 0.2 },
          class_name: "Car",
          confidence: 0.9,
          shape_index,
        })),
      },
    ] as never;
    view.args.userBoxes = [{ ...box("manual"), x: 0.1, y: 0.1, w: 0.2, h: 0.2 }] as never;
    predictionMutations.accept.mockResolvedValue([]);
    view.rerender();

    expect(view.result.current.aiBoxes).toHaveLength(3);
    expect(view.result.current.batchEligibleCount).toBe(2);
    await act(async () => {
      await view.result.current.handleAcceptAll();
    });

    expect(predictionMutations.accept).toHaveBeenCalledTimes(2);
    expect(predictionMutations.accept).not.toHaveBeenCalledWith(
      expect.objectContaining({ shapeIndex: 0 }),
    );
  });

  it("builds batch class-change target from current selection", () => {
    expect(getBatchChangeTarget(["b"], [box("a"), box("b", "Bike")])).toEqual({
      geom: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      className: "Bike",
      count: 1,
    });
  });

  it("rejects empty or non-finite class-picker image bounds", () => {
    expect(hasUsableImageBounds({ x: 0, y: 0, w: 0, h: 0 })).toBe(false);
    expect(hasUsableImageBounds({ x: Number.NaN, y: 0, w: 0.2, h: 0.3 })).toBe(false);
    expect(hasUsableImageBounds({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 })).toBe(true);
  });

  it.each(["data-workbench-settings", "data-workbench-tool-menu"])(
    "取消 Mask 回选择，%s 内的候选键不影响背景 SAM",
    (marker) => {
      const cancel = vi.fn();
      const setTool = vi.fn();
      const s = {
        activeClass: "",
        clipboard: null,
        confThreshold: 0.5,
        editingClass: null,
        pendingDrawing: null,
        selectedId: null,
        selectedIds: [],
        tool: "mask",
        videoFrameIndex: 0,
        workbenchConfig: { image: { afterBoxCreate: "pick_class" } },
        replaceSelected: vi.fn(),
        setActiveClass: vi.fn(),
        setClipboard: vi.fn(),
        setEditingClass: vi.fn(),
        setPendingDrawing: vi.fn(),
        setSelectedId: vi.fn(),
        setTool,
      };
      const sam = {
        activeIdx: 0,
        canAcceptCandidates: false,
        candidates: [],
        isRunning: false,
        cancel: vi.fn(),
        consume: vi.fn(),
        cycle: vi.fn(),
      };
      const mutate = vi.fn();
      const view = renderHook(() =>
        useImageAnnotationActions({
          taskId: "task-1",
          projectId: "project-1",
          meUserId: "user-1",
          queryClient: {},
          history: { push: vi.fn(), pushBatch: vi.fn() },
          s,
          pushToast: vi.fn(),
          recordRecentClass: vi.fn(),
          annotationsData: [],
          annotationsRef: { current: [] },
          predictionsData: [],
          userBoxes: [],
          stageGeom: { imgW: 100, imgH: 100, vpSize: { w: 100, h: 100 } },
          iouDedupThreshold: 0.7,
          classes: [],
          sam,
          acceptNativeMask: vi.fn(),
          createAnnotationAsync: vi.fn(),
          updateAnnotationAsync: vi.fn(),
          mutations: {
            create: { mutate },
            update: { mutate },
            delete: { mutate },
          },
          enqueueOnError: vi.fn(),
          maskEditor: { cancel },
        } as never),
      );

      act(() => view.result.current.cancelMaskEdit());

      expect(cancel).toHaveBeenCalledTimes(1);
      expect(setTool).toHaveBeenCalledWith("select");

      Object.assign(s, { tool: "smart-point" });
      Object.assign(sam, {
        canAcceptCandidates: true,
        candidates: [
          {
            type: "polygonlabels",
            points: [
              [0, 0],
              [1, 0],
              [1, 1],
            ],
          },
        ],
      });
      view.rerender();
      const settings = document.createElement("button");
      settings.setAttribute(marker, "");
      settings.dataset.state = "open";
      document.body.append(settings);
      act(() => {
        for (const key of ["Enter", "Escape", "Tab", "r"]) {
          settings.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        }
      });
      expect(view.result.current.samPendingGeom).toBeNull();
      expect(sam.cancel).not.toHaveBeenCalled();
      expect(sam.cycle).not.toHaveBeenCalled();
      settings.remove();
      act(() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" })));
      expect(sam.cancel).toHaveBeenCalledTimes(1);
    },
  );
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const maskRle: CocoRle = { encoding: "coco_rle", size: [100, 100], counts: [0, 10_000] };
const maskRef: CocoRleMaskRef = {
  encoding: "coco_rle_ref",
  size: [100, 100],
  object_key: "mask-key",
  sha256: "mask-hash",
  runs: 2,
  bytes: 20,
};
const savedMask = {
  id: "mask-1",
  class_name: "Car",
  version: 2,
  is_locked: false,
  geometry: { type: "raster_mask", mask: maskRef },
} as AnnotationResponse;

function maskCommitHarness() {
  const view = decisionHarness();
  const editor = {
    active: true,
    dirty: true,
    phase: "dirty",
    generation: 1,
    sessionId: "task-1|mask|blank|v?",
    commitToRleAsync: vi.fn<() => Promise<CocoRle | null>>().mockResolvedValue(maskRle),
    commitToPolygon: vi.fn().mockReturnValue({
      points: [
        [0, 0],
        [100, 0],
        [100, 100],
      ],
      lossy: false,
    }),
    save: vi.fn(async (commit: () => Promise<MaskSaveResult>) => commit()),
    rebaseSession: vi.fn(),
    beginBlank: vi.fn(),
    cancel: vi.fn(),
    initFromPolygon: vi.fn(),
    undo: vi.fn(),
    canUndo: true,
  };
  const args = Object.assign(view.args, {
    maskEditor: editor,
    maskPersistenceMode: "native" as "native" | "legacy",
    maskRouteKey: "/workbench/task-1",
    maskSessionKey: {
      taskId: "task-1",
      frameIndex: 0,
      toolKey: "image:mask",
      routeKey: "/workbench/task-1",
      selectionKey: "blank",
      annotationVersion: undefined as number | undefined,
    },
    activeToolHasOwnClasses: false,
    isLocked: false,
  });
  const s = Object.assign(view.s, { tool: "mask", selectedId: "", setTool: vi.fn() });
  Object.assign(args.sam, { consume: vi.fn() });
  args.createAnnotationAsync.mockResolvedValue(savedMask);
  args.updateAnnotationAsync.mockResolvedValue(savedMask);
  view.rerender();
  return { ...view, args, s, editor };
}

function expectNoMaskCompletion(view: ReturnType<typeof maskCommitHarness>) {
  expect(view.args.history.push).not.toHaveBeenCalled();
  expect(view.args.recordRecentClass).not.toHaveBeenCalled();
  expect(view.args.pushToast).not.toHaveBeenCalled();
  expect(view.editor.cancel).not.toHaveBeenCalled();
  expect(view.editor.beginBlank).not.toHaveBeenCalled();
  expect(view.s.setTool).not.toHaveBeenCalled();
  expect(view.s.setSelectedId).not.toHaveBeenCalled();
}

describe("ordinary image Mask commit ownership", () => {
  beforeEach(() => {
    nativeRequests.upload.mockReset().mockResolvedValue(maskRef);
  });

  it.each(["task", "return-to-task", "route", "tool", "selection", "lock", "generation"])(
    "异步合并期间切换 %s 后不能选类、上传或修改新会话",
    async (change) => {
      const merge = deferred<CocoRle>();
      const view = maskCommitHarness();
      view.args.activeToolHasOwnClasses = true;
      view.editor.commitToRleAsync.mockReturnValueOnce(merge.promise);
      view.rerender();
      const pending = view.result.current.commitMaskAsPolygon();
      if (change === "task" || change === "return-to-task") view.args.taskId = "task-2";
      if (change === "route") view.args.maskRouteKey = "/review/task-1";
      if (change === "tool") view.s.tool = "box";
      if (change === "selection") view.s.selectedId = "another";
      if (change === "lock") view.args.isLocked = true;
      if (change === "generation") view.editor.generation += 1;
      view.rerender();
      if (change === "return-to-task") {
        view.args.taskId = "task-1";
        view.rerender();
      }
      await act(async () => {
        merge.resolve(maskRle);
        expect(await pending).toEqual({ ok: false, retryable: false });
      });
      expect(view.s.setPendingDrawing).not.toHaveBeenCalled();
      expect(nativeRequests.upload).not.toHaveBeenCalled();
      expect(view.editor.save).not.toHaveBeenCalled();
      expectNoMaskCompletion(view);
    },
  );

  it("过期合并失败保持静默", async () => {
    const merge = deferred<CocoRle>();
    const view = maskCommitHarness();
    view.editor.commitToRleAsync.mockReturnValueOnce(merge.promise);
    const pending = view.result.current.commitMaskAsPolygon();
    view.args.taskId = "task-2";
    view.rerender();
    await act(async () => {
      merge.reject(new Error("released old worker"));
      expect(await pending).toEqual({ ok: false, retryable: false });
    });
    expectNoMaskCompletion(view);
  });

  it.each(["native", "legacy"] as const)(
    "%s 选类弹层在切题后结束，不能沿用到新题",
    async (mode) => {
      const view = maskCommitHarness();
      view.args.maskPersistenceMode = mode;
      view.args.activeToolHasOwnClasses = true;
      view.rerender();
      let pending!: Promise<MaskSaveResult>;
      await act(async () => {
        pending = view.result.current.commitMaskAsPolygon();
      });
      expect(view.s.setPendingDrawing).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "raster_mask" }),
      );
      view.s.pendingDrawing =
        view.s.setPendingDrawing.mock.calls[view.s.setPendingDrawing.mock.calls.length - 1][0];
      view.rerender();
      view.args.taskId = "task-2";
      view.rerender();
      await act(async () => {
        expect(await pending).toEqual({ ok: false, retryable: false });
      });
      expect(view.s.setPendingDrawing).toHaveBeenLastCalledWith(null);
      expect(nativeRequests.upload).not.toHaveBeenCalled();
      expect(view.editor.save).not.toHaveBeenCalled();
      expectNoMaskCompletion(view);
    },
  );

  it.each(["task", "generation"])("上传期间切换 %s，上传完成不能继续创建标注", async (change) => {
    const upload = deferred<CocoRleMaskRef>();
    nativeRequests.upload.mockReturnValueOnce(upload.promise);
    const view = maskCommitHarness();
    let pending!: Promise<MaskSaveResult>;
    await act(async () => {
      pending = view.result.current.commitMaskAsPolygon();
    });
    expect(nativeRequests.upload).toHaveBeenCalledTimes(1);
    if (change === "task") view.args.taskId = "task-2";
    else view.editor.generation += 1;
    view.rerender();
    await act(async () => {
      upload.resolve(maskRef);
      expect(await pending).toEqual({ ok: false, retryable: false });
    });
    expect(view.args.createAnnotationAsync).not.toHaveBeenCalled();
    expect(view.args.updateAnnotationAsync).not.toHaveBeenCalled();
    expectNoMaskCompletion(view);
  });

  it.each(["native", "legacy"] as const)(
    "%s 迟到创建成功不能写入历史、选择或取消新稿件",
    async (mode) => {
      const save = deferred<AnnotationResponse>();
      const view = maskCommitHarness();
      view.args.maskPersistenceMode = mode;
      view.args.createAnnotationAsync.mockReturnValueOnce(save.promise);
      view.rerender();
      let pending!: Promise<MaskSaveResult>;
      await act(async () => {
        pending = view.result.current.commitMaskAsPolygon();
      });
      expect(view.args.createAnnotationAsync).toHaveBeenCalledTimes(1);
      view.args.taskId = "task-2";
      view.rerender();
      view.args.taskId = "task-1";
      view.rerender();
      await act(async () => {
        save.resolve(savedMask);
        expect(await pending).toEqual({ ok: false, retryable: false });
      });
      expectNoMaskCompletion(view);
    },
  );

  it("过期保存失败不向新会话显示旧错误", async () => {
    const save = deferred<AnnotationResponse>();
    const view = maskCommitHarness();
    view.args.createAnnotationAsync.mockReturnValueOnce(save.promise);
    let pending!: Promise<MaskSaveResult>;
    await act(async () => {
      pending = view.result.current.commitMaskAsPolygon();
    });
    view.args.taskId = "task-2";
    view.rerender();
    await act(async () => {
      save.reject(new Error("offline"));
      expect(await pending).toEqual({ ok: false, retryable: false });
    });
    expectNoMaskCompletion(view);
  });

  it("自身成功更新的对象版本变化仍可完成历史和编辑器收尾", async () => {
    const save = deferred<AnnotationResponse>();
    const view = maskCommitHarness();
    view.args.maskSessionKey = {
      ...view.args.maskSessionKey,
      selectionKey: savedMask.id,
      annotationVersion: 1,
    };
    view.s.selectedId = savedMask.id;
    view.args.annotationsRef.current = [{ ...savedMask, version: 1 }] as never;
    view.args.updateAnnotationAsync.mockReturnValueOnce(save.promise);
    view.rerender();
    let pending!: Promise<MaskSaveResult>;
    await act(async () => {
      pending = view.result.current.commitMaskAsPolygon();
    });
    expect(view.args.updateAnnotationAsync).toHaveBeenCalledWith(
      savedMask.id,
      { geometry: savedMask.geometry },
      'W/"1"',
    );
    view.args.annotationsRef.current = [savedMask] as never;
    view.editor.generation += 1;
    view.editor.sessionId = "task-1|mask|mask-1|v2";
    view.rerender();
    await act(async () => {
      save.resolve(savedMask);
      expect(await pending).toEqual({ ok: true, retryable: false });
    });
    expect(view.args.history.push).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "update", annotationId: savedMask.id }),
    );
    expect(view.editor.rebaseSession).toHaveBeenCalledWith({
      ...view.args.maskSessionKey,
      annotationVersion: 2,
    });
    expect(view.editor.rebaseSession.mock.invocationCallOrder[0]).toBeLessThan(
      view.editor.cancel.mock.invocationCallOrder[0],
    );
    expect(view.editor.cancel).toHaveBeenCalledTimes(1);
    expect(view.s.setTool).not.toHaveBeenCalled();
    expect(view.editor.beginBlank).toHaveBeenCalledOnce();
    expect(view.s.setSelectedId).toHaveBeenCalledWith(null);
  });

  it("删除空 Mask 的迟到成功不清空新会话选择和历史", async () => {
    const view = maskCommitHarness();
    view.s.selectedId = savedMask.id;
    view.args.annotationsRef.current = [savedMask] as never;
    view.editor.commitToRleAsync.mockResolvedValueOnce({ ...maskRle, counts: [10_000] });
    view.rerender();
    let pending!: Promise<MaskSaveResult>;
    await act(async () => {
      pending = view.result.current.commitMaskAsPolygon();
    });
    // 擦空稿件走应用内三选一;选「删除对象」后进入删除分支。
    await act(async () => {
      settleDecisionDialog("delete");
      await Promise.resolve();
    });
    expect(view.args.mutations.delete.mutate).toHaveBeenCalledTimes(1);
    const callbacks = view.args.mutations.delete.mutate.mock.calls[0][1];
    view.args.taskId = "task-2";
    view.rerender();
    await act(async () => {
      callbacks.onSuccess();
      expect(await pending).toEqual({ ok: false, retryable: false });
    });
    expectNoMaskCompletion(view);
  });

  it("卸载后合并成功保持静默", async () => {
    const merge = deferred<CocoRle>();
    const view = maskCommitHarness();
    view.editor.commitToRleAsync.mockReturnValueOnce(merge.promise);
    const pending = view.result.current.commitMaskAsPolygon();
    view.unmount();
    merge.resolve(maskRle);
    expect(await pending).toEqual({ ok: false, retryable: false });
    expect(nativeRequests.upload).not.toHaveBeenCalled();
    expectNoMaskCompletion(view);
  });

  it("旧入口在切题后被调用也不能启动合并", async () => {
    const view = maskCommitHarness();
    const oldCommit = view.result.current.commitMaskAsPolygon;
    view.args.taskId = "task-2";
    view.rerender();
    expect(await oldCommit()).toEqual({ ok: false, retryable: false });
    expect(view.editor.commitToRleAsync).not.toHaveBeenCalled();
    expectNoMaskCompletion(view);
  });

  it.each(["native", "legacy"] as const)(
    "%s 保存后保留 Mask 工具并准备独立的新草稿",
    async (mode) => {
      const view = maskCommitHarness();
      view.args.maskPersistenceMode = mode;
      view.rerender();
      await act(async () => {
        expect(await view.result.current.commitMaskAsPolygon()).toEqual({
          ok: true,
          retryable: false,
        });
      });
      expect(view.args.createAnnotationAsync).toHaveBeenCalledTimes(1);
      if (mode === "native") {
        expect(nativeRequests.upload).toHaveBeenCalledWith("task-1", maskRle);
        expect(view.args.history.push).toHaveBeenCalledTimes(1);
      }
      expect(view.editor.cancel).toHaveBeenCalledTimes(1);
      expect(view.editor.beginBlank).toHaveBeenCalledTimes(1);
      expect(view.editor.cancel.mock.invocationCallOrder[0]).toBeLessThan(
        view.editor.beginBlank.mock.invocationCallOrder[0],
      );
      expect(view.s.setTool).not.toHaveBeenCalled();
      expect(view.s.setSelectedId).toHaveBeenCalledWith(null);
    },
  );

  it("锁定对象不能开始合并或保存", async () => {
    const view = maskCommitHarness();
    view.s.selectedId = savedMask.id;
    view.args.annotationsRef.current = [{ ...savedMask, is_locked: true }] as never;
    view.rerender();
    await act(async () => {
      expect(await view.result.current.commitMaskAsPolygon()).toEqual({
        ok: false,
        retryable: false,
      });
    });
    expect(view.editor.commitToRleAsync).not.toHaveBeenCalled();
    expect(view.editor.save).not.toHaveBeenCalled();
    expect(view.args.pushToast).toHaveBeenCalledWith(expect.objectContaining({ kind: "warning" }));
  });
});
