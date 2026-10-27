// v0.6.3 P1 单测：覆盖 v0.6.3 新增的 applyLeaf tmpId undo 本地分支。
//
// 思路：直接测从 hook 模块导出的纯函数 applyLeaf，构造 mock handlers，
// 不需要 React 渲染环境。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  applyLeaf,
  loadHistoryFromSession,
  saveHistoryToSession,
  type Command,
  type HistoryHandlers,
} from "./useAnnotationHistory";

function makeHandlers(over: Partial<HistoryHandlers> = {}): HistoryHandlers {
  return {
    createAnnotation: vi.fn(async () => ({ id: "real-1" }) as never),
    deleteAnnotation: vi.fn(async () => ({})),
    updateAnnotation: vi.fn(async () => ({})),
    ...over,
  };
}

const dummyPayload = { annotation_type: "bbox", class_name: "x", geometry: {} as never };
afterEach(() => vi.unstubAllGlobals());

describe("applyLeaf · create undo (v0.6.3 P0 tmpId 本地分支)", () => {
  it("annotationId 是 tmpId 且 removeLocalCreate 提供 → 走本地分支，不调 deleteAnnotation", async () => {
    const removeLocalCreate = vi.fn(async () => {});
    const h = makeHandlers({ removeLocalCreate });

    await applyLeaf({ kind: "create", annotationId: "tmp_abc", payload: dummyPayload }, "undo", h);

    expect(removeLocalCreate).toHaveBeenCalledWith("tmp_abc");
    expect(h.deleteAnnotation).not.toHaveBeenCalled();
  });

  it("annotationId 是真实 id → 调 deleteAnnotation", async () => {
    const removeLocalCreate = vi.fn(async () => {});
    const h = makeHandlers({ removeLocalCreate });

    await applyLeaf(
      { kind: "create", annotationId: "real-uuid", payload: dummyPayload },
      "undo",
      h,
    );

    expect(h.deleteAnnotation).toHaveBeenCalledWith("real-uuid");
    expect(removeLocalCreate).not.toHaveBeenCalled();
  });

  it("annotationId 是 tmpId 但 removeLocalCreate 未提供 → 退回 deleteAnnotation（向后兼容）", async () => {
    const h = makeHandlers(); // removeLocalCreate undefined

    await applyLeaf({ kind: "create", annotationId: "tmp_xyz", payload: dummyPayload }, "undo", h);

    expect(h.deleteAnnotation).toHaveBeenCalledWith("tmp_xyz");
  });
});

describe("applyLeaf · create redo / update / delete 不受 tmpId 分支影响", () => {
  it("uses a new creation key after undo and retains it for a failed redo retry", async () => {
    vi.stubGlobal("crypto", { getRandomValues: crypto.getRandomValues.bind(crypto) });
    const create = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("lost response"))
      .mockResolvedValue({ id: "redo-created" });
    const handlers = makeHandlers({ createAnnotation: create });
    const cmd = {
      kind: "create" as const,
      annotationId: "original",
      payload: {
        ...dummyPayload,
        client_request_id: "original-key",
      },
    };
    await applyLeaf(cmd, "undo", handlers);
    const redoKey = cmd.payload.client_request_id;
    expect(redoKey).not.toBe("original-key");
    await expect(applyLeaf(cmd, "redo", handlers)).rejects.toBeInstanceOf(TypeError);
    await applyLeaf(cmd, "redo", handlers);
    expect(create.mock.calls[0][0].client_request_id).toBe(redoKey);
    expect(create.mock.calls[1][0].client_request_id).toBe(redoKey);
    await applyLeaf(cmd, "undo", handlers);
    expect(cmd.payload.client_request_id).not.toBe(redoKey);
  });
  it("redo create → 调 createAnnotation，cmd.annotationId 改写为新 id", async () => {
    const h = makeHandlers({
      createAnnotation: vi.fn(async () => ({ id: "fresh-1" }) as never),
    });
    const cmd = { kind: "create" as const, annotationId: "tmp_x", payload: dummyPayload };

    await applyLeaf(cmd, "redo", h);

    expect(h.createAnnotation).toHaveBeenCalledWith(dummyPayload);
    expect(cmd.annotationId).toBe("fresh-1");
  });

  it("update undo → 用 before 调 updateAnnotation", async () => {
    const h = makeHandlers();
    await applyLeaf(
      {
        kind: "update",
        annotationId: "id",
        before: { class_name: "A" },
        after: { class_name: "B" },
      },
      "undo",
      h,
    );
    expect(h.updateAnnotation).toHaveBeenCalledWith("id", { class_name: "A" });
  });

  it("update redo → 用 after 调 updateAnnotation", async () => {
    const h = makeHandlers();
    await applyLeaf(
      {
        kind: "update",
        annotationId: "id",
        before: { class_name: "A" },
        after: { class_name: "B" },
      },
      "redo",
      h,
    );
    expect(h.updateAnnotation).toHaveBeenCalledWith("id", { class_name: "B" });
  });
});

describe("applyLeaf · acceptPrediction undo (v0.20.22 parent_prediction_id 防御过滤)", () => {
  it("getAnnotation 未注入 → fallback 无过滤直删 (旧行为不 regress)", async () => {
    const h = makeHandlers();
    await applyLeaf(
      {
        kind: "acceptPrediction",
        predictionId: "pred-1",
        createdAnnotationIds: ["a1", "a2"],
      },
      "undo",
      h,
    );
    expect(h.deleteAnnotation).toHaveBeenCalledTimes(2);
    expect(h.deleteAnnotation).toHaveBeenCalledWith("a1");
    expect(h.deleteAnnotation).toHaveBeenCalledWith("a2");
  });

  it("getAnnotation 返回 parent_prediction_id === cmd.predictionId → 删", async () => {
    const getAnnotation = vi.fn(
      (id: string) =>
        ({
          id,
          parent_prediction_id: "pred-1",
        }) as never,
    );
    const h = makeHandlers({ getAnnotation });
    await applyLeaf(
      {
        kind: "acceptPrediction",
        predictionId: "pred-1",
        createdAnnotationIds: ["a1", "a2"],
      },
      "undo",
      h,
    );
    expect(h.deleteAnnotation).toHaveBeenCalledTimes(2);
  });

  it("createdAnnotationIds 混入他 prediction 派生 id → 只删本 predictionId 的", async () => {
    const getAnnotation = vi.fn((id: string) => {
      if (id === "own-1" || id === "own-2") {
        return { id, parent_prediction_id: "pred-1" } as never;
      }
      // 他 prediction 派生 (脏 id, 模拟旧后端契约或未来 regression)
      return { id, parent_prediction_id: "pred-other" } as never;
    });
    const h = makeHandlers({ getAnnotation });
    await applyLeaf(
      {
        kind: "acceptPrediction",
        predictionId: "pred-1",
        createdAnnotationIds: ["own-1", "dirty-1", "own-2", "dirty-2"],
      },
      "undo",
      h,
    );
    expect(h.deleteAnnotation).toHaveBeenCalledTimes(2);
    expect(h.deleteAnnotation).toHaveBeenCalledWith("own-1");
    expect(h.deleteAnnotation).toHaveBeenCalledWith("own-2");
    expect(h.deleteAnnotation).not.toHaveBeenCalledWith("dirty-1");
    expect(h.deleteAnnotation).not.toHaveBeenCalledWith("dirty-2");
  });

  it("getAnnotation 返回 null (cache miss) → fallback 直删", async () => {
    const getAnnotation = vi.fn(() => null);
    const h = makeHandlers({ getAnnotation });
    await applyLeaf(
      {
        kind: "acceptPrediction",
        predictionId: "pred-1",
        createdAnnotationIds: ["a1"],
      },
      "undo",
      h,
    );
    expect(h.deleteAnnotation).toHaveBeenCalledWith("a1");
  });

  it("redo → 不触发 deleteAnnotation (仅消费 redo 栈)", async () => {
    const h = makeHandlers();
    await applyLeaf(
      {
        kind: "acceptPrediction",
        predictionId: "pred-1",
        createdAnnotationIds: ["a1"],
      },
      "redo",
      h,
    );
    expect(h.deleteAnnotation).not.toHaveBeenCalled();
  });
});

describe("applyLeaf · video keyframe undo / redo", () => {
  it("undo videoKeyframe → 用 before 调 updateVideoKeyframe", async () => {
    const updateVideoKeyframe = vi.fn(async () => ({}));
    const h = makeHandlers({ updateVideoKeyframe });
    const before = {
      frame_index: 3,
      bbox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
      source: "manual" as const,
    };
    const after = {
      frame_index: 3,
      bbox: { x: 0.2, y: 0.2, w: 0.3, h: 0.4 },
      source: "manual" as const,
    };

    await applyLeaf(
      { kind: "videoKeyframe", annotationId: "track-1", frameIndex: 3, before, after },
      "undo",
      h,
    );

    expect(updateVideoKeyframe).toHaveBeenCalledWith("track-1", 3, before);
  });

  it("redo videoKeyframe deletion → 用 null 调 updateVideoKeyframe", async () => {
    const updateVideoKeyframe = vi.fn(async () => ({}));
    const h = makeHandlers({ updateVideoKeyframe });

    await applyLeaf(
      {
        kind: "videoKeyframe",
        annotationId: "track-1",
        frameIndex: 3,
        before: {
          frame_index: 3,
          bbox: { x: 0.1, y: 0.2, w: 0.3, h: 0.4 },
          source: "manual",
        },
        after: null,
      },
      "redo",
      h,
    );

    expect(updateVideoKeyframe).toHaveBeenCalledWith("track-1", 3, null);
  });
});

describe("applyLeaf · video Mask frame undo / redo", () => {
  const keyframe = {
    frame_index: 4,
    source: "manual" as const,
    mask: {
      encoding: "coco_rle_ref" as const,
      size: [2, 3] as [number, number],
      object_key: "raster-masks/frame-4.json",
      sha256: "a".repeat(64),
      runs: 3,
      bytes: 12,
    },
  };

  it("undo 删除关键帧时恢复精确帧及 manual outside 状态", async () => {
    const updateVideoMaskFrame = vi.fn(async () => ({}));
    const h = makeHandlers({ updateVideoMaskFrame });
    const before = { keyframe, manualOutside: true };
    const after = { keyframe: null, manualOutside: false };

    await applyLeaf(
      { kind: "videoMaskFrame", annotationId: "mask-1", frameIndex: 4, before, after },
      "undo",
      h,
    );

    expect(updateVideoMaskFrame).toHaveBeenCalledWith("mask-1", 4, before);
  });

  it("redo outside 操作时应用 after 状态", async () => {
    const updateVideoMaskFrame = vi.fn(async () => ({}));
    const h = makeHandlers({ updateVideoMaskFrame });
    const after = { keyframe, manualOutside: true };

    await applyLeaf(
      {
        kind: "videoMaskFrame",
        annotationId: "mask-1",
        frameIndex: 4,
        before: { keyframe, manualOutside: false },
        after,
      },
      "redo",
      h,
    );

    expect(updateVideoMaskFrame).toHaveBeenCalledWith("mask-1", 4, after);
  });
});

// ── v0.8.7 F8 · sessionStorage 持久化 ────────────────────────────────

describe("history sessionStorage 持久化", () => {
  beforeEach(() => {
    if (typeof window !== "undefined") window.sessionStorage.clear();
  });

  const sample: Command = {
    kind: "create",
    annotationId: "ann_1",
    payload: {
      annotation_type: "bbox",
      class_name: "car",
      geometry: {} as never,
    },
  };

  it("save → load round-trip 同 taskId", () => {
    saveHistoryToSession("task-A", [sample], []);
    const back = loadHistoryFromSession("task-A");
    expect(back?.undo).toHaveLength(1);
    expect(back?.undo[0].kind).toBe("create");
  });

  it("空栈写时清除 key", () => {
    saveHistoryToSession("task-A", [sample], []);
    saveHistoryToSession("task-A", [], []);
    expect(loadHistoryFromSession("task-A")).toBeNull();
  });

  it("TTL 过期不 restore 并自清", () => {
    const expired = JSON.stringify({
      undo: [sample],
      redo: [],
      ts: Date.now() - 6 * 60 * 1000, // 6min
    });
    window.sessionStorage.setItem("wb:hist:task-old", expired);
    expect(loadHistoryFromSession("task-old")).toBeNull();
    expect(window.sessionStorage.getItem("wb:hist:task-old")).toBeNull();
  });

  it("不同 taskId 互相隔离", () => {
    saveHistoryToSession("task-A", [sample], []);
    saveHistoryToSession("task-B", [], []);
    expect(loadHistoryFromSession("task-A")?.undo).toHaveLength(1);
    expect(loadHistoryFromSession("task-B")).toBeNull();
  });

  it("undefined taskId 不写不读", () => {
    saveHistoryToSession(undefined, [sample], []);
    expect(loadHistoryFromSession(undefined)).toBeNull();
  });

  it("损坏 JSON 静默忽略", () => {
    window.sessionStorage.setItem("wb:hist:bad", "{not json");
    expect(loadHistoryFromSession("bad")).toBeNull();
  });
});
