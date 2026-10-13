// v0.23.5 · WS-B · useMaskEditorSession 单测 — 覆盖 A1 / A2 / A7 复现场景。
//
// A1 视频"加载中落笔后被迟到 GET 覆盖": sessionKey 切换后旧 generation 的 loadRle 不得回写。
// A2 图片保存失败后 Buffer 被清空: save reject → phase=error, editor.buffer 仍在, 可 retry。
// A7 重复 Enter / 双击确认并发上传: save 单飞, 重复调用合并为同一 Promise。

import { describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import { promptMaskLeaveChoice, useMaskEditorSession } from "./useMaskEditorSession";
import type { MaskSessionKey } from "./useMaskEditorSession";

/** 结算当前排队的 decisionDialog 并清空队列,模拟用户点下某个选项。 */
function settleDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  useDecisionDialogStore.getState().settle(value);
  useDecisionDialogStore.getState().dispose();
}

const KEY_A: MaskSessionKey = {
  taskId: "t1",
  frameIndex: 0,
  selectionKey: "ann-a",
  annotationVersion: 1,
};
const KEY_B: MaskSessionKey = {
  taskId: "t1",
  frameIndex: 1,
  selectionKey: "ann-b",
  annotationVersion: 1,
};

function renderSession(overrides?: {
  sessionKey?: MaskSessionKey;
  onLeaveDirty?: ReturnType<typeof vi.fn>;
}) {
  return renderHook(
    ({ sessionKey, onLeaveDirty }) =>
      useMaskEditorSession({
        width: 20,
        height: 20,
        sessionKey,
        onLeaveDirty,
      }),
    {
      initialProps: {
        sessionKey: overrides?.sessionKey ?? KEY_A,
        onLeaveDirty: overrides?.onLeaveDirty ?? vi.fn(() => Promise.resolve("discard" as const)),
      },
    },
  );
}

const RLE_DIFFERENT: { encoding: "coco_rle"; size: [number, number]; counts: number[] } = {
  encoding: "coco_rle",
  size: [20, 20],
  counts: [200, 200],
};

it("保存后重开空白草稿可立即落笔，保留半径与笔刷形状", async () => {
  const { result } = renderSession({ sessionKey: { ...KEY_A, selectionKey: "blank" } });
  act(() => {
    result.current.loadBlank(result.current.generation);
    result.current.setRadius(2);
    result.current.setBrushShape("square");
  });
  act(() => result.current.paintAt(5, 5));
  const savedBuffer = result.current.buffer;
  expect(savedBuffer?.countSet()).toBeGreaterThan(0);
  await act(async () => {
    expect(await result.current.save(async () => ({ ok: true, retryable: false }))).toEqual({
      ok: true,
      retryable: false,
    });
    result.current.cancel();
    result.current.beginBlank();
  });
  expect(result.current.phase).toBe("ready");
  expect(result.current.active).toBe(true);
  expect(result.current.dirty).toBe(false);
  expect(result.current.buffer).not.toBe(savedBuffer);
  expect(result.current.buffer?.countSet()).toBe(0);
  expect(result.current.canUndo).toBe(false);
  expect(result.current.radius).toBe(2);
  expect(result.current.brushShape).toBe("square");
  act(() => result.current.paintAt(15, 15));
  expect(result.current.buffer?.countSet()).toBeGreaterThan(0);
  expect(result.current.phase).toBe("dirty");
});

describe("useMaskEditorSession · A1 迟到 GET 不得覆盖当前 Buffer", () => {
  it("paint revision 变化不会改变 load callback 引用", async () => {
    const { result } = renderSession();
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    const loadRle = result.current.loadRle;
    const loadBlank = result.current.loadBlank;
    act(() => result.current.paintAt(5, 5));
    expect(result.current.loadRle).toBe(loadRle);
    expect(result.current.loadBlank).toBe(loadBlank);
  });

  it("sessionKey 切换后旧 generation 的 loadRle 静默丢弃", async () => {
    const { result, rerender } = renderSession({ sessionKey: KEY_A });
    // session A 进入 loading → loadBlank 让它 ready
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    expect(result.current.phase).toBe("ready");
    const genA = result.current.generation;

    // 切到 session B
    rerender({
      sessionKey: KEY_B,
      onLeaveDirty: vi.fn(() => Promise.resolve("discard" as const)),
    });
    await act(async () => {
      // 让微任务跑完 (onLeaveDirty)
      await Promise.resolve();
    });
    const genB = result.current.generation;
    expect(genB).toBeGreaterThan(genA);
    expect(result.current.phase).toBe("loading");

    // session B 自己先 loadBlank, 得到一个已知状态的 buffer (全 0)
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    expect(result.current.phase).toBe("ready");
    const bufferBeforeLateGet = result.current.buffer;
    const countBeforeLateGet = result.current.buffer?.countSet() ?? 0;

    // 旧 session A 的迟到 GET 回包: 用 genA 调 loadRle —— 必须被丢弃
    act(() => {
      result.current.loadRle(genA, RLE_DIFFERENT);
    });
    // phase 不应被旧回包改回 loading; buffer 引用与内容不变 (迟到回包被丢弃)
    expect(result.current.phase).toBe("ready");
    expect(result.current.buffer).toBe(bufferBeforeLateGet);
    expect(result.current.buffer?.countSet()).toBe(countBeforeLateGet);
  });

  it("当前 generation 的 loadRle 正常写入", async () => {
    const { result } = renderSession();
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    const before = result.current.buffer?.countSet() ?? 0;
    // 用当前 generation 重新 load 一个有内容的 RLE
    act(() => {
      result.current.loadRle(result.current.generation, RLE_DIFFERENT);
    });
    expect(result.current.phase).toBe("ready");
    // RLE_DIFFERENT = [200,200] → 第二段 200 个前景 → countSet === 200
    expect(result.current.buffer?.countSet()).toBe(200);
    expect(result.current.buffer?.countSet()).not.toBe(before);
  });

  it("外部刷新 rebase 后接纳同一新 session 不会再次推进 generation", async () => {
    const onLeaveDirty = vi.fn(async () => "continue" as const);
    const { result, rerender } = renderSession({ sessionKey: KEY_A, onLeaveDirty });
    await act(async () => result.current.loadBlank(result.current.generation));
    const refreshedKey = { ...KEY_A, annotationVersion: 2 };

    act(() => {
      result.current.cancel();
      result.current.rebaseSession(refreshedKey);
      result.current.initFromRle(RLE_DIFFERENT);
    });
    const refreshedGeneration = result.current.generation;

    rerender({ sessionKey: refreshedKey, onLeaveDirty });
    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.generation).toBe(refreshedGeneration);
    expect(result.current.phase).toBe("ready");
    expect(result.current.buffer?.countSet()).toBe(200);
    expect(onLeaveDirty).not.toHaveBeenCalled();
  });
});

describe("useMaskEditorSession · dirty leave guard", () => {
  it("continue 不推进 generation，discard 才接受新 session", async () => {
    const onLeaveDirty = vi.fn(async () => "continue" as const);
    const { result, rerender } = renderSession({ sessionKey: KEY_A, onLeaveDirty });
    await act(async () => result.current.loadBlank(result.current.generation));
    act(() => result.current.paintAt(5, 5));
    const generation = result.current.generation;

    rerender({ sessionKey: KEY_B, onLeaveDirty });
    await act(async () => {
      await Promise.resolve();
    });
    expect(onLeaveDirty).toHaveBeenCalledWith(KEY_A, KEY_B);
    expect(result.current.generation).toBe(generation);
    expect(result.current.acceptedSessionId).not.toBe(result.current.sessionId);
    expect(result.current.buffer?.countSet()).toBeGreaterThan(0);

    const discard = vi.fn(async () => "discard" as const);
    rerender({ sessionKey: KEY_A, onLeaveDirty: discard });
    await act(async () => {
      await Promise.resolve();
    });
    rerender({ sessionKey: KEY_B, onLeaveDirty: discard });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.generation).toBeGreaterThan(generation);
    expect(result.current.phase).toBe("loading");
  });

  it("三段选择结果明确", async () => {
    let choice = promptMaskLeaveChoice();
    settleDialog("save");
    await expect(choice).resolves.toBe("save");
    choice = promptMaskLeaveChoice();
    settleDialog("discard");
    await expect(choice).resolves.toBe("discard");
    // 取消 / Esc / 点遮罩 → null → 继续编辑
    choice = promptMaskLeaveChoice();
    settleDialog(null);
    await expect(choice).resolves.toBe("continue");
  });

  it("区域预览尚未应用时仍属于旧会话，切换取消保留预览", async () => {
    const onLeaveDirty = vi.fn(async () => "continue" as const);
    const { result, rerender } = renderSession({ sessionKey: KEY_A, onLeaveDirty });
    await act(async () => {
      result.current.loadRle(result.current.generation, {
        encoding: "coco_rle",
        size: [20, 20],
        counts: [0, 1, 399],
      });
      expect(
        await result.current.runOperation("dilate", {
          type: "morphology",
          operation: "dilate",
          kernelShape: "disk",
          radius: 1,
        }),
      ).toBe(true);
    });
    expect(result.current.dirty).toBe(false);
    const preview = result.current.operationPreview;
    expect(preview).not.toBeNull();
    const generation = result.current.generation;
    rerender({ sessionKey: KEY_B, onLeaveDirty });
    await act(async () => {
      await Promise.resolve();
    });
    expect(onLeaveDirty).toHaveBeenCalledWith(KEY_A, KEY_B);
    expect(result.current.generation).toBe(generation);
    expect(result.current.operationPreview).toBe(preview);
  });

  it("实例预览即使 Buffer 未变也会阻止切换 session", async () => {
    const onLeaveDirty = vi.fn(async () => "continue" as const);
    const { result, rerender } = renderSession({ sessionKey: KEY_A, onLeaveDirty });
    await act(async () => {
      result.current.loadRle(result.current.generation, {
        encoding: "coco_rle",
        size: [20, 20],
        counts: [0, 1, 10, 1, 388],
      });
      expect(
        await result.current.runInstanceOperation("split_components", {
          type: "split_components",
          keep: "largest",
          connectivity: 4,
        }),
      ).toBe(true);
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.instanceOperationPreview).not.toBeNull();
    expect(result.current.phase).toBe("dirty");
    const generation = result.current.generation;

    rerender({ sessionKey: KEY_B, onLeaveDirty });
    await act(async () => {
      await Promise.resolve();
    });

    expect(onLeaveDirty).toHaveBeenCalledWith(KEY_A, KEY_B);
    expect(result.current.generation).toBe(generation);
    expect(result.current.instanceOperationPreview).not.toBeNull();
  });
});

describe("useMaskEditorSession · A2 保存失败后 Buffer 保留 + 可 retry", () => {
  it("save reject → phase=error, buffer 保留, 可 recover + retry 成功", async () => {
    const { result } = renderSession();
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    // 画一笔让 dirty=true
    act(() => {
      result.current.paintAt(10, 10);
    });
    expect(result.current.dirty).toBe(true);
    expect(result.current.phase).toBe("dirty");
    const bufferBeforeSave = result.current.buffer;

    // 第一次 save 失败 (模拟 409 / 网络错误)
    const failingCommit = vi.fn(async () => ({
      ok: false,
      retryable: true,
      error: new Error("409"),
    }));
    let firstResult: { ok: boolean } | undefined;
    await act(async () => {
      firstResult = await result.current.save(failingCommit);
    });
    expect(firstResult!.ok).toBe(false);
    expect(result.current.phase).toBe("error");
    expect(result.current.lastSaveError).toBeInstanceOf(Error);
    // Buffer 保留 (不丢稿)
    expect(result.current.buffer).toBe(bufferBeforeSave);
    expect(result.current.buffer?.countSet()).toBeGreaterThan(0);

    // recover 回到 dirty
    act(() => {
      result.current.recoverFromError();
    });
    expect(result.current.phase).toBe("dirty");

    // retry 成功
    const okCommit = vi.fn(async () => ({ ok: true, retryable: false }));
    let retryResult: { ok: boolean } | undefined;
    await act(async () => {
      retryResult = await result.current.save(okCommit);
    });
    expect(retryResult!.ok).toBe(true);
    expect(result.current.phase).not.toBe("error");
    expect(result.current.lastSaveError).toBeUndefined();
  });
});

describe("useMaskEditorSession · A7 重复 Enter / 双击单飞去重", () => {
  it("同一 session 重复 save 合并为一次 commit 调用", async () => {
    const { result } = renderSession();
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    act(() => {
      result.current.paintAt(5, 5);
    });

    let resolveCommit: (r: { ok: boolean; retryable: boolean }) => void = () => {};
    const commit = vi.fn(
      () =>
        new Promise<{ ok: boolean; retryable: boolean }>((resolve) => {
          resolveCommit = resolve;
        }),
    );

    // 连续触发两次 save (模拟双击 Enter / 重复按键)
    let p1: Promise<{ ok: boolean }> | undefined;
    let p2: Promise<{ ok: boolean }> | undefined;
    act(() => {
      p1 = result.current.save(commit);
      p2 = result.current.save(commit);
    });
    expect(result.current.saveInFlight).toBe(true);
    // 单飞: 两个 Promise 是同一引用
    expect(p1).toBe(p2);
    // commit 只被调用一次
    expect(commit).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCommit({ ok: true, retryable: false });
      await p1;
    });
    expect(result.current.saveInFlight).toBe(false);
    expect(commit).toHaveBeenCalledTimes(1);
  });

  it("sessionKey 切换后旧 save 的迟到 resolve 不污染新 session phase", async () => {
    const { result, rerender } = renderSession({ sessionKey: KEY_A });
    await act(async () => {
      result.current.loadBlank(result.current.generation);
    });
    act(() => {
      result.current.paintAt(5, 5);
    });

    let resolveOld: (r: { ok: boolean; retryable: boolean }) => void = () => {};
    const oldCommit = vi.fn(
      () =>
        new Promise<{ ok: boolean; retryable: boolean }>((resolve) => {
          resolveOld = resolve;
        }),
    );
    let oldPromise: Promise<{ ok: boolean }> | undefined;
    act(() => {
      oldPromise = result.current.save(oldCommit);
    });
    expect(result.current.phase).toBe("saving");

    // 切到 session B (旧 save 仍在 in-flight)
    rerender({
      sessionKey: KEY_B,
      onLeaveDirty: vi.fn(() => Promise.resolve("discard" as const)),
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.generation).toBeGreaterThan(0);
    expect(result.current.phase).toBe("loading"); // 新 session

    // 旧 save 现在 resolve —— 不得把新 session 的 phase 改成 ready/error
    await act(async () => {
      resolveOld({ ok: true, retryable: false });
      await oldPromise;
    });
    expect(result.current.phase).toBe("loading");
    expect(result.current.saveInFlight).toBe(false);
  });

  it("旧 save resolve 不会清除新 session 的单飞 Promise", async () => {
    const { result, rerender } = renderSession({ sessionKey: KEY_A });
    await act(async () => result.current.loadBlank(result.current.generation));
    act(() => result.current.paintAt(3, 3));
    let resolveOld!: (value: { ok: boolean; retryable: boolean }) => void;
    let resolveNew!: (value: { ok: boolean; retryable: boolean }) => void;
    let oldPromise!: Promise<{ ok: boolean }>;
    act(() => {
      oldPromise = result.current.save(
        () =>
          new Promise((resolve) => {
            resolveOld = resolve;
          }),
      );
    });
    rerender({ sessionKey: KEY_B, onLeaveDirty: vi.fn(async () => "discard" as const) });
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => result.current.loadBlank(result.current.generation));
    act(() => result.current.paintAt(4, 4));
    let newPromise!: Promise<{ ok: boolean }>;
    const newCommit = vi.fn(
      () =>
        new Promise<{ ok: boolean; retryable: boolean }>((resolve) => {
          resolveNew = resolve;
        }),
    );
    act(() => {
      newPromise = result.current.save(newCommit);
    });

    await act(async () => {
      resolveOld({ ok: true, retryable: false });
      await oldPromise;
    });
    expect(result.current.saveInFlight).toBe(true);
    expect(result.current.save(newCommit)).toBe(newPromise);
    expect(newCommit).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveNew({ ok: true, retryable: false });
      await newPromise;
    });
  });
});
