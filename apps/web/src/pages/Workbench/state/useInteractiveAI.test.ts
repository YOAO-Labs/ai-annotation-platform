/**
 * v0.9.2 · useInteractiveAI hook 单测.
 * 覆盖 point / bbox / text 三种 prompt 路由 + backend 失败 toast + mlBackendId 缺失守卫
 * + 80ms 防抖合并连续点击。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

import { ApiError } from "@/api/client";
import { newMaskIdempotencyKey, useInteractiveAI, simplifyCandidateRing } from "./useInteractiveAI";
import { simplifyPolygon } from "../stage/shared/geometry/simplify";

const interactiveAnnotateMock = vi.fn();
const pushToastMock = vi.fn();
const recordPredictCacheHitMock = vi.fn();

vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: {
    interactiveAnnotate: (...args: unknown[]) => interactiveAnnotateMock(...args),
  },
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (s: { push: typeof pushToastMock }) => unknown) =>
    selector({ push: pushToastMock }),
}));

vi.mock("./sessionVariantCache", () => ({
  recordPredictCacheHit: (...args: unknown[]) => recordPredictCacheHitMock(...args),
}));

const ARGS = { projectId: "p1", taskId: "t1", mlBackendId: "b1" };

const POLY_RESPONSE = {
  result: [
    {
      type: "polygonlabels",
      value: {
        points: [
          [0.1, 0.1],
          [0.5, 0.1],
          [0.5, 0.5],
        ],
        polygonlabels: ["person"],
      },
      score: 0.92,
    },
  ],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function nativeResponse(maskInputNext: string | null = null) {
  const candidateId = `sha256:${"a".repeat(64)}`;
  return {
    result: [
      {
        type: "mask",
        value: {
          rle: { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 3] },
          preview: {
            points: [
              [0, 0],
              [2 / 3, 0],
              [2 / 3, 1],
              [0, 1],
            ],
          },
          masklabels: ["person"],
        },
        score: 0.93,
        candidate_id: candidateId,
      },
    ],
    prompt_revision: `prompt-${maskInputNext ?? "initial"}`,
    output_geometry: "mask",
    frame_index: null,
    routing: {
      requested_backend_id: "11111111-1111-1111-1111-111111111111",
      backend_pool_id: null,
      backend_instance_id: "11111111-1111-1111-1111-111111111111",
      model_id: "sam-mask",
    },
    accept_receipts: { [candidateId]: "signed-receipt-value" },
    mask_input_next: maskInputNext,
    prompt_summary: {
      family: "scribble",
      positive_points: 0,
      negative_points: 0,
      boxes: 0,
      positive_scribbles: 0,
      negative_scribbles: 1,
      multimask: false,
      parameters_digest: null,
    },
  };
}

describe("useInteractiveAI", () => {
  beforeEach(() => {
    interactiveAnnotateMock.mockReset();
    pushToastMock.mockReset();
    recordPredictCacheHitMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("非安全 HTTP 上的幂等键 fallback 满足后端最小长度", () => {
    vi.stubGlobal("crypto", {});

    const key = newMaskIdempotencyKey();

    expect(key).toMatch(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/);
    expect(key.length).toBeGreaterThanOrEqual(16);
  });

  it("runBbox 路由到 ctx.type='interactive_box' (v0.18.17 · 旧 bbox 改名)", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));
    const [pid, bid, payload] = interactiveAnnotateMock.mock.calls[0];
    expect(pid).toBe("p1");
    expect(bid).toBe("b1");
    expect(payload.task_id).toBe("t1");
    expect(payload.context.type).toBe("interactive_box");
    expect(payload.context.bbox).toEqual([0.1, 0.1, 0.4, 0.4]);
    expect(payload.context.multimask_output).toBe(false);
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(result.current.candidates[0].label).toBe("person");
    expect(result.current.candidates[0].source).toBe("bbox");
  });

  it("runPoint 累加全量点 (v0.18.17 · 防抖窗口内多次点击累加成一个会话, 重发全量)", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    vi.useFakeTimers();
    try {
      const { result } = renderHook(() => useInteractiveAI(ARGS));
      act(() => result.current.runPoint([0.1, 0.1], 1));
      act(() => result.current.runPoint([0.2, 0.2], 0));
      act(() => result.current.runPoint([0.3, 0.3], 1));
      expect(interactiveAnnotateMock).not.toHaveBeenCalled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1);
      const ctx = interactiveAnnotateMock.mock.calls[0][2].context;
      // 累加: 全量点 + 对应极性; ≥2 点 → multimask_output=false (单 mask 精修).
      expect(ctx.points).toEqual([
        [0.1, 0.1],
        [0.2, 0.2],
        [0.3, 0.3],
      ]);
      expect(ctx.labels).toEqual([1, 0, 1]);
      expect(ctx.multimask_output).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("runPoint 首点 multimask_output=true (单点歧义出候选)", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));
    const ctx = interactiveAnnotateMock.mock.calls[0][2].context;
    expect(ctx.points).toEqual([[0.5, 0.5]]);
    expect(ctx.multimask_output).toBe(true);
  });

  it("§5.4 mask_input 回灌: 首点不回灌, ≥2 点回传上一轮 low-res logits", async () => {
    // 首点 multimask → 后端 mask_input_next=null; 第 2 点起单 mask → 返回 token 供下次回传。
    interactiveAnnotateMock
      .mockResolvedValueOnce({ ...POLY_RESPONSE, mask_input_next: null })
      .mockResolvedValueOnce({ ...POLY_RESPONSE, mask_input_next: "TOKEN_A" })
      .mockResolvedValueOnce({ ...POLY_RESPONSE, mask_input_next: "TOKEN_B" });
    const { result } = renderHook(() => useInteractiveAI(ARGS));

    act(() => result.current.runPoint([0.1, 0.1], 1));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      await Promise.resolve();
    });
    // 首点候选阶段 (multimask=true) 不回灌。
    expect(interactiveAnnotateMock.mock.calls[0][2].context.mask_input).toBeUndefined();

    act(() => result.current.runPoint([0.2, 0.2], 1));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      await Promise.resolve();
    });
    // 第 2 点: 上一轮 (首点) 返回 null → maskInputRef 仍空, 不回灌。
    expect(interactiveAnnotateMock.mock.calls[1][2].context.mask_input).toBeUndefined();

    act(() => result.current.runPoint([0.3, 0.3], 1));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(3));
    // 第 3 点: 回传第 2 点返回的 TOKEN_A。
    expect(interactiveAnnotateMock.mock.calls[2][2].context.mask_input).toBe("TOKEN_A");
  });

  it("§5.5 sessionPoints 累加并随 cancel 清空", async () => {
    interactiveAnnotateMock.mockResolvedValue({ ...POLY_RESPONSE, mask_input_next: null });
    const { result } = renderHook(() => useInteractiveAI(ARGS));

    act(() => result.current.runPoint([0.1, 0.1], 1));
    await waitFor(() => expect(result.current.sessionPoints).toHaveLength(1));
    act(() => result.current.runPoint([0.2, 0.2], 0));
    await waitFor(() => expect(result.current.sessionPoints).toHaveLength(2));
    expect(result.current.sessionPoints).toEqual([
      { pt: [0.1, 0.1], polarity: 1 },
      { pt: [0.2, 0.2], polarity: 0 },
    ]);
    act(() => result.current.cancel());
    expect(result.current.sessionPoints).toHaveLength(0);
  });

  it("Alt+点击 (polarity=0) 透传 negative label", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runPoint([0.5, 0.5], 0));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));
    expect(interactiveAnnotateMock.mock.calls[0][2].context.labels).toEqual([0]);
  });

  it("mlBackendId 缺失 → 不发请求 + 弹 toast", async () => {
    const { result } = renderHook(() => useInteractiveAI({ ...ARGS, mlBackendId: null }));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await new Promise((r) => setTimeout(r, 20));
    expect(interactiveAnnotateMock).not.toHaveBeenCalled();
    expect(pushToastMock).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "项目未绑定 ML Backend", kind: "error" }),
    );
  });

  it("backend 抛错 → 不更新 candidates + 弹错误 toast", async () => {
    interactiveAnnotateMock.mockRejectedValue(new Error("connection refused"));
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "SAM 推理失败", kind: "error" }),
      ),
    );
    expect(result.current.candidates).toHaveLength(0);
    expect(result.current.error).toBe("connection refused");
  });

  it("重试在途立即清除上轮错误，成功和缓存命中保持清除", async () => {
    const retry = deferred<typeof POLY_RESPONSE>();
    interactiveAnnotateMock
      .mockRejectedValueOnce(new Error("connection refused"))
      .mockImplementationOnce(() => retry.promise)
      .mockRejectedValueOnce(new Error("another prompt failed"));
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    expect(result.current.error).toBeNull();

    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    await waitFor(() => expect(result.current.error).toBe("connection refused"));
    expect(result.current.canRetry).toBe(true);

    act(() => result.current.retryLast());
    expect(result.current.error).toBeNull();
    expect(result.current.isRunning).toBe(true);
    await act(async () => {
      retry.resolve(POLY_RESPONSE);
      await retry.promise;
    });
    expect(result.current.error).toBeNull();
    expect(result.current.canAcceptCandidates).toBe(true);

    act(() => result.current.runBbox([0.5, 0.5, 0.7, 0.7]));
    await waitFor(() => expect(result.current.error).toBe("another prompt failed"));
    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    expect(result.current.error).toBeNull();
    expect(result.current.canAcceptCandidates).toBe(true);
    expect(result.current.isRunning).toBe(false);
    expect(interactiveAnnotateMock).toHaveBeenCalledTimes(3);
  });

  it("不可重试的纯负向 Scribble 仍保留本轮错误，取消后清除", async () => {
    interactiveAnnotateMock.mockRejectedValue(
      new ApiError(409, "mask session expired", { reason: "mask_session_expired" }),
    );
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() =>
      result.current.runScribble(
        [
          [0.2, 0.2],
          [0.3, 0.3],
        ],
        0,
      ),
    );
    await waitFor(() => expect(result.current.error).toBe("mask session expired"));
    expect(result.current.canRetry).toBe(false);
    act(() => result.current.retryLast());
    expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1);
    expect(result.current.error).toBe("mask session expired");
    act(() => result.current.cancel());
    expect(result.current.error).toBeNull();
  });

  it.each(["success", "failure"])("旧请求的 %s 不得覆盖当前请求错误", async (outcome) => {
    const old = deferred<typeof POLY_RESPONSE>();
    interactiveAnnotateMock
      .mockImplementationOnce(() => old.promise)
      .mockRejectedValueOnce(new Error("current request failed"));
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0.1, 0.1, 0.3, 0.3]));
    const oldSignal = interactiveAnnotateMock.mock.calls[0][3] as AbortSignal;
    act(() => result.current.runBbox([0.5, 0.5, 0.7, 0.7]));
    await waitFor(() => expect(result.current.error).toBe("current request failed"));
    expect(oldSignal.aborted).toBe(true);

    await act(async () => {
      if (outcome === "success") old.resolve(POLY_RESPONSE);
      else old.reject(new Error("old request failed"));
      await old.promise.catch(() => undefined);
    });
    expect(result.current.error).toBe("current request failed");
    expect(result.current.canRetry).toBe(true);
    expect(result.current.candidates).toEqual([]);
    expect(pushToastMock).toHaveBeenCalledTimes(1);
  });

  it.each(["projectId", "taskId", "mlBackendId", "cacheScope"] as const)(
    "%s 变更清除旧错误，取消后的异步失败不污染新 owner",
    async (ownerKey) => {
      const old = deferred<typeof POLY_RESPONSE>();
      interactiveAnnotateMock
        .mockRejectedValueOnce(new Error("old owner failed"))
        .mockImplementationOnce(() => old.promise)
        .mockRejectedValueOnce(new Error("current owner failed"));
      const initialOwner = { ...ARGS, cacheScope: "frame-0" };
      const { result, rerender } = renderHook((owner) => useInteractiveAI(owner), {
        initialProps: initialOwner,
      });
      act(() => result.current.runBbox([0.1, 0.1, 0.3, 0.3]));
      await waitFor(() => expect(result.current.error).toBe("old owner failed"));

      rerender({ ...initialOwner, [ownerKey]: "owner-b" });
      expect(result.current.error).toBeNull();
      expect(result.current.canRetry).toBe(false);
      act(() => result.current.runBbox([0.4, 0.4, 0.5, 0.5]));
      const oldSignal = interactiveAnnotateMock.mock.calls[1][3] as AbortSignal;
      rerender({ ...initialOwner, [ownerKey]: "owner-c" });
      expect(oldSignal.aborted).toBe(true);
      expect(result.current.error).toBeNull();
      expect(result.current.isRunning).toBe(false);

      act(() => result.current.runBbox([0.6, 0.6, 0.7, 0.7]));
      await waitFor(() => expect(result.current.error).toBe("current owner failed"));
      await act(async () => {
        old.reject(new DOMException("The operation was aborted", "AbortError"));
        await old.promise.catch(() => undefined);
      });
      expect(result.current.error).toBe("current owner failed");
      expect(result.current.canRetry).toBe(true);
      expect(pushToastMock).toHaveBeenCalledTimes(2);
    },
  );

  it("空 result → 提示 + candidates 清空", async () => {
    interactiveAnnotateMock.mockResolvedValue({ result: [] });
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0.1, 0.1, 0.2, 0.2]));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "SAM 未返回候选" }),
      ),
    );
    expect(result.current.candidates).toHaveLength(0);
  });

  it("v0.18.26 · 出候选 → 弹候选数提示 (与无候选对齐)", async () => {
    interactiveAnnotateMock.mockResolvedValue({
      result: [
        {
          type: "rectanglelabels",
          value: { x: 1, y: 1, width: 5, height: 5, rectanglelabels: ["a"] },
          score: 0.9,
        },
        {
          type: "rectanglelabels",
          value: { x: 1, y: 1, width: 5, height: 5, rectanglelabels: ["b"] },
          score: 0.8,
        },
      ],
    });
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0.1, 0.1, 0.2, 0.2]));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "2 个候选", kind: "success" }),
      ),
    );
  });

  it("多连通 mask (value.polygons) → 取面积最大外环, 不丢候选", async () => {
    // 后端多环结构: 一个大三角 (面积 0.5) + 一个碎屑小三角 (面积 ~0.005)。
    interactiveAnnotateMock.mockResolvedValue({
      result: [
        {
          type: "polygonlabels",
          value: {
            polygons: [
              {
                points: [
                  [0, 0],
                  [0.1, 0],
                  [0, 0.1],
                ],
              }, // 碎屑
              {
                points: [
                  [0, 0],
                  [1, 0],
                  [0, 1],
                ],
              }, // 主体 (最大)
            ],
            polygonlabels: ["object"],
          },
          score: 0.9,
        },
      ],
    });
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    // 取最大外环 (主体三角), 而非碎屑。
    const candidate = result.current.candidates[0];
    expect(candidate.type).toBe("polygonlabels");
    if (candidate.type !== "polygonlabels") throw new Error("expected polygon candidate");
    expect(candidate.points).toEqual([
      [0, 0],
      [1, 0],
      [0, 1],
    ]);
  });

  it("原生 Mask 保留 RLE 与签名血缘，并独立接收显示预览", async () => {
    const candidateId = `sha256:${"a".repeat(64)}`;
    interactiveAnnotateMock.mockResolvedValue({
      result: [
        {
          type: "mask",
          value: {
            rle: { encoding: "coco_rle", size: [2, 3], counts: [1, 2, 3] },
            preview: {
              points: [
                [0, 0],
                [2 / 3, 0],
                [2 / 3, 1],
                [0, 1],
              ],
            },
            masklabels: ["person"],
          },
          score: 0.93,
          candidate_id: candidateId,
        },
      ],
      score: 0.93,
      model_version: "sam-test",
      inference_time_ms: 12,
      cache_hit: false,
      model_load_ms: 4,
      prompt_revision: "prompt-revision",
      output_geometry: "mask",
      routing: {
        requested_backend_id: "11111111-1111-1111-1111-111111111111",
        backend_pool_id: null,
        backend_instance_id: "11111111-1111-1111-1111-111111111111",
        model_id: "sam-mask",
      },
      accept_receipts: { [candidateId]: "signed-receipt-value" },
      prompt_summary: {
        family: "point",
        positive_points: 1,
        negative_points: 0,
        boxes: 0,
        positive_scribbles: 0,
        negative_scribbles: 0,
        multimask: true,
        parameters_digest: null,
      },
    });
    const { result } = renderHook(() =>
      useInteractiveAI({
        ...ARGS,
        requestContextDefaults: { model_id: "sam-mask", output_geometry: "mask" },
      }),
    );

    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    const candidate = result.current.candidates[0];
    expect(candidate.type).toBe("mask");
    if (candidate.type !== "mask") throw new Error("expected native mask candidate");
    expect(candidate.id).toBe(candidateId);
    expect(candidate.rle).toEqual({
      encoding: "coco_rle",
      size: [2, 3],
      counts: [1, 2, 3],
    });
    expect(candidate).not.toHaveProperty("points");
    expect(candidate.previewPoints).toEqual([
      [0, 0],
      [2 / 3, 0],
      [2 / 3, 1],
      [0, 1],
    ]);
    expect(candidate.receipt).toBe("signed-receipt-value");
    expect(candidate.idempotencyKey.length).toBeGreaterThanOrEqual(16);
    expect(interactiveAnnotateMock.mock.calls[0][2].context).toMatchObject({
      model_id: "sam-mask",
      output_geometry: "mask",
    });
  });

  it("已存 Mask 种子让首个点直接进单候选精修，并绑定原位更新版本", async () => {
    interactiveAnnotateMock.mockResolvedValue(nativeResponse("SIGNED_LOGITS_A"));
    const { result } = renderHook(() =>
      useInteractiveAI({
        ...ARGS,
        requestContextDefaults: {
          model_id: "sam-mask",
          output_geometry: "mask",
          mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
        },
      }),
    );

    act(() => result.current.runPoint([0.5, 0.5], 0));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    const context = interactiveAnnotateMock.mock.calls[0][2].context;
    expect(context).toMatchObject({
      type: "point",
      labels: [0],
      multimask_output: false,
      mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
    });
    const item = result.current.candidates[0];
    expect(item.type).toBe("mask");
    if (item.type !== "mask") throw new Error("expected native mask candidate");
    expect(item.refineSource).toEqual({ annotationId: "annotation-mask-1", sourceVersion: 7 });
  });

  it("负 scribble 累加且跨工具回灌 logits；网络失败保留候选与笔迹并可原样重试", async () => {
    interactiveAnnotateMock
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_A"))
      .mockRejectedValueOnce(new Error("network lost"))
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_B"));
    const { result } = renderHook(() =>
      useInteractiveAI({
        ...ARGS,
        requestContextDefaults: {
          model_id: "sam-mask",
          output_geometry: "mask",
          mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
        },
      }),
    );

    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(result.current.canAcceptCandidates).toBe(true);
    const stroke: [number, number][] = [
      [0.6, 0.4],
      [0.7, 0.45],
      [0.75, 0.5],
    ];
    act(() => result.current.runScribble(stroke, 0, 0.008));
    await waitFor(() => expect(result.current.canRetry).toBe(true));

    expect(result.current.candidates).toHaveLength(1);
    expect(result.current.canAcceptCandidates).toBe(false);
    expect(result.current.sessionPoints).toEqual([]);
    expect(result.current.sessionScribbles).toEqual([
      { points: stroke, polarity: 0, width: 0.008 },
    ]);
    expect(interactiveAnnotateMock.mock.calls[1][2].context).toMatchObject({
      type: "scribble",
      scribbles: [{ points: stroke, polarity: 0, width: 0.008 }],
      mask_input: "SIGNED_LOGITS_A",
    });

    act(() => result.current.retryLast());
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(result.current.canRetry).toBe(false));
    expect(result.current.canAcceptCandidates).toBe(true);
    expect(interactiveAnnotateMock.mock.calls[2][2].context).toEqual(
      interactiveAnnotateMock.mock.calls[1][2].context,
    );
    const retried = result.current.candidates[0];
    expect(retried.type).toBe("mask");
    if (retried.type !== "mask") throw new Error("expected native mask candidate");
    expect(retried.promptSummary).toMatchObject({
      family: "scribble",
      positive_scribbles: 0,
      negative_scribbles: 1,
    });
  });

  it("过期 mask session 清掉旧 logits，保留已授权种子与笔迹后可重试", async () => {
    interactiveAnnotateMock
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_A"))
      .mockRejectedValueOnce(
        new ApiError(409, "mask session expired", { reason: "mask_session_expired" }),
      )
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_B"));
    const { result } = renderHook(() =>
      useInteractiveAI({
        ...ARGS,
        requestContextDefaults: {
          model_id: "sam-mask",
          output_geometry: "mask",
          mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
        },
      }),
    );

    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    const stroke: [number, number][] = [
      [0.2, 0.2],
      [0.3, 0.3],
    ];
    act(() => result.current.runScribble(stroke, 1));
    await waitFor(() => expect(result.current.canRetry).toBe(true));
    expect(interactiveAnnotateMock.mock.calls[1][2].context.mask_input).toBe("SIGNED_LOGITS_A");

    act(() => result.current.retryLast());
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(3));
    const retryContext = interactiveAnnotateMock.mock.calls[2][2].context;
    expect(retryContext.mask_input).toBeUndefined();
    expect(retryContext).toMatchObject({
      type: "scribble",
      scribbles: [{ points: stroke, polarity: 1, width: 0.008 }],
      mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
    });
  });

  it("模型变体变化时不跨变体回灌 mask session", async () => {
    interactiveAnnotateMock
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_A"))
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_B"))
      .mockResolvedValueOnce(nativeResponse("SIGNED_LOGITS_DEFAULT"));
    const { result } = renderHook(() =>
      useInteractiveAI({
        ...ARGS,
        requestContextDefaults: {
          model_id: "sam-mask",
          output_geometry: "mask",
          mask_prompt_source: { annotation_id: "annotation-mask-1", source_version: 7 },
        },
      }),
    );

    act(() => result.current.runPoint([0.5, 0.5], 1, { model_variants: { sam_variant: "tiny" } }));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));

    act(() =>
      result.current.runScribble(
        [
          [0.2, 0.2],
          [0.3, 0.3],
        ],
        1,
        0.008,
        { model_variants: { sam_variant: "large" } },
      ),
    );
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
    expect(interactiveAnnotateMock.mock.calls[1][2].context.mask_input).toBeUndefined();
    expect(interactiveAnnotateMock.mock.calls[1][2].context.model_variants).toEqual({
      sam_variant: "large",
    });

    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(3));
    expect(interactiveAnnotateMock.mock.calls[2][2].context.mask_input).toBeUndefined();
    expect(interactiveAnnotateMock.mock.calls[2][2].context.model_variants).toBeUndefined();
  });

  it("cycle 在候选间循环切换", async () => {
    interactiveAnnotateMock.mockResolvedValue({
      result: [
        {
          type: "polygonlabels",
          value: {
            points: [
              [0, 0],
              [1, 0],
              [1, 1],
            ],
            polygonlabels: ["a"],
          },
          score: 0.9,
        },
        {
          type: "polygonlabels",
          value: {
            points: [
              [0, 0],
              [1, 0],
              [0, 1],
            ],
            polygonlabels: ["b"],
          },
          score: 0.8,
        },
        {
          type: "polygonlabels",
          value: {
            points: [
              [0, 0],
              [0, 1],
              [1, 1],
            ],
            polygonlabels: ["c"],
          },
          score: 0.7,
        },
      ],
    });
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0.1, 0.1, 0.2, 0.2]));
    await waitFor(() => expect(result.current.candidates).toHaveLength(3));
    expect(result.current.activeIdx).toBe(0);
    act(() => result.current.cycle(1));
    expect(result.current.activeIdx).toBe(1);
    act(() => result.current.cycle(1));
    act(() => result.current.cycle(1));
    expect(result.current.activeIdx).toBe(0); // 3 → 0 wrap
    act(() => result.current.cycle(-1));
    expect(result.current.activeIdx).toBe(2);
  });

  // v0.10.23 · 会话级模型变体切换三态通知
  it("切换变体后首次预测 → 切换中 + 成功 toast", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() =>
      result.current.runBbox([0, 0, 0.5, 0.5], { sam_variant: "large", dino_variant: "base" }),
    );
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(pushToastMock).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "正在切换到 SAM large/DINO base 模型…" }),
    );
    expect(pushToastMock).toHaveBeenCalledWith(
      expect.objectContaining({ msg: "已切换到 SAM large/DINO base", kind: "success" }),
    );
    expect(interactiveAnnotateMock.mock.calls[0][2].context).toMatchObject({
      model_variants: { sam_variant: "large", dino_variant: "base" },
    });
    expect(interactiveAnnotateMock.mock.calls[0][2].context.sam_variant).toBeUndefined();
    expect(interactiveAnnotateMock.mock.calls[0][2].context.dino_variant).toBeUndefined();
  });

  it("model_variants 响应 cache_hit 真信号写入 variant hot map", async () => {
    interactiveAnnotateMock.mockResolvedValue({ ...POLY_RESPONSE, cache_hit: false });
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() =>
      result.current.runBbox([0, 0, 0.5, 0.5], {
        model_variants: { series: "yolov11", size: "s" },
      }),
    );
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(interactiveAnnotateMock.mock.calls[0][2].context.model_variants).toEqual({
      series: "yolov11",
      size: "s",
    });
    expect(recordPredictCacheHitMock).toHaveBeenCalledWith(
      "b1",
      { series: "yolov11", size: "s" },
      false,
    );
  });

  it("同变体的后续预测不再弹切换 toast", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5], { sam_variant: "large" }));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    pushToastMock.mockClear();
    // 不同 bbox (避开前端缓存) 但同变体 → 不应再弹切换通知
    act(() => result.current.runBbox([0.1, 0.1, 0.6, 0.6], { sam_variant: "large" }));
    await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
    expect(pushToastMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ msg: expect.stringContaining("切换") }),
    );
  });

  it("缺 dino_variant → 文案仅显示 SAM 段", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5], { sam_variant: "tiny" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({ msg: "已切换到 SAM tiny", kind: "success" }),
      ),
    );
  });

  it("切换变体后预测失败 → 模型切换失败 toast 且 backend detail 透出", async () => {
    interactiveAnnotateMock.mockRejectedValue(new Error("checkpoint not provisioned"));
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5], { sam_variant: "large" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          msg: "模型切换失败",
          sub: "checkpoint not provisioned",
          kind: "error",
        }),
      ),
    );
  });

  it("503 推理错误 → 显示模型暂不可用与 Retry-After", async () => {
    interactiveAnnotateMock.mockRejectedValue(
      new ApiError(503, "ML backend: model unavailable", undefined, { "retry-after": "30" }),
    );
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          msg: "模型暂不可用",
          sub: "30 秒后重试\nML backend: model unavailable",
          kind: "error",
        }),
      ),
    );
    expect(result.current.error).toBe("模型暂不可用：30 秒后重试\nML backend: model unavailable");
  });

  it.each([422, 503, 500, 400])("保留 HTTP %s 推理错误全文供界面和通知复制", async (status) => {
    const message = `Traceback\n${"diagnostic ".repeat(200)}\n最后一行`;
    interactiveAnnotateMock.mockRejectedValue(new ApiError(status, message));
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() => expect(result.current.error).toContain(message));
    expect(pushToastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        sub: expect.stringContaining(message),
        kind: "error",
      }),
    );
  });

  it("无变体字段的预测不弹任何切换 toast", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(pushToastMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ msg: expect.stringContaining("切换") }),
    );
  });

  it("cancel 清空候选", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    act(() => result.current.cancel());
    expect(result.current.candidates).toHaveLength(0);
  });

  // claude[bot] P1 回归 · cache-hit-after-inflight: 上一个真实请求 in-flight 时, 紧接着
  // 触发一次命中前端缓存的调用 → 旧请求被 abort + inflightRef 自增, 但 cache 分支直接 return,
  // 旧请求 finally 守卫不通过 → isRunning 卡在 true。修后 cache 分支显式 setIsRunning(false)。
  it("cache 命中且上一个请求 in-flight 时 isRunning 复位 (P1 回归)", async () => {
    // 调用次序: 1) 跑通填 cache → 2) hang 住模拟 in-flight → 3) cache 命中(不再调 mock)。
    interactiveAnnotateMock.mockResolvedValueOnce(POLY_RESPONSE);
    let secondResolve: ((v: typeof POLY_RESPONSE) => void) | undefined;
    interactiveAnnotateMock.mockImplementationOnce(
      () =>
        new Promise((res) => {
          secondResolve = res as typeof secondResolve;
        }),
    );

    const { result } = renderHook(() => useInteractiveAI(ARGS));

    // 第 1 次: 同一 bbox 跑通 → 缓存填好。
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));
    expect(result.current.isRunning).toBe(false);

    // 第 2 次: 换 prompt, hang 住 (mockImplementationOnce 走这次)。
    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    await waitFor(() => expect(result.current.isRunning).toBe(true));

    // 第 3 次: 回到第 1 次的 prompt → cache 命中, 在 in-flight 期间触发。
    act(() => result.current.runBbox([0, 0, 0.5, 0.5]));
    // 候选立刻复用 (cache 同步命中); isRunning 必须立刻复位, 不等 in-flight resolve。
    expect(result.current.candidates).toHaveLength(1);
    expect(result.current.isRunning).toBe(false);

    // 收尾: 让 hang 的请求 resolve, 防止泄漏 (旧请求的结果被 inflight 守卫丢弃)。
    secondResolve?.(POLY_RESPONSE);
  });

  // v0.18.19 · exemplar refine 会话 (多正负框 + text 组合 + 阈值重过滤)
  describe("exemplar refine 会话", () => {
    it("框与原生 Mask 混合候选消费矩形后仍保留 Mask 的签名索引", async () => {
      const response = nativeResponse();
      const mask = response.result[0];
      const secondMask = { ...mask, candidate_id: `sha256:${"b".repeat(64)}` };
      const box = {
        type: "rectanglelabels",
        value: { x: 0, y: 0, width: 1, height: 1, rectanglelabels: ["person"] },
        score: 0.93,
      };
      interactiveAnnotateMock.mockResolvedValue({
        ...response,
        result: [box, mask, box, secondMask],
        accept_receipts: {
          [mask.candidate_id]: "receipt-index-1",
          [secondMask.candidate_id]: "receipt-index-3",
        },
      });
      const { result } = renderHook(() =>
        useInteractiveAI({
          ...ARGS,
          requestContextDefaults: { model_id: "sam-mask", output_geometry: "mask" },
        }),
      );
      act(() => result.current.runExemplar([0.1, 0.2, 0.4, 0.6], 1, "both"));
      await waitFor(() => expect(result.current.candidates).toHaveLength(4));
      expect(result.current.candidates.map((candidate) => candidate.type)).toEqual([
        "rectanglelabels",
        "mask",
        "rectanglelabels",
        "mask",
      ]);
      expect(interactiveAnnotateMock.mock.calls[0][2].context).toMatchObject({
        output: "both",
        output_geometry: "mask",
      });
      act(() => result.current.consume(0));
      expect(result.current.candidates[0]).toMatchObject({
        type: "mask",
        candidateIndex: 1,
        receipt: "receipt-index-1",
      });
      act(() => result.current.consume(0));
      expect(result.current.candidates[1]).toMatchObject({
        type: "mask",
        candidateIndex: 3,
        receipt: "receipt-index-3",
      });
    });

    it("仅框召回不受 Mask 保存偏好影响，切回掩膜恢复原有设置", async () => {
      const defaults = { model_id: "sam-mask", output_geometry: "mask" };
      const extra = { output_geometry: "mask", score_threshold: 0.7 };
      const boxResponse = {
        result: [
          {
            type: "rectanglelabels",
            value: { x: 0.1, y: 0.2, width: 0.3, height: 0.4, rectanglelabels: ["car"] },
            score: 0.9,
          },
        ],
      };
      interactiveAnnotateMock
        .mockResolvedValueOnce(boxResponse)
        .mockResolvedValueOnce(nativeResponse());
      const { result } = renderHook(() =>
        useInteractiveAI({ ...ARGS, requestContextDefaults: defaults }),
      );

      act(() => result.current.runExemplar([0.1, 0.2, 0.4, 0.6], 1, "box", extra));
      await waitFor(() => expect(result.current.candidates).toHaveLength(1));
      expect(interactiveAnnotateMock.mock.calls[0][2].context).toMatchObject({
        type: "exemplar",
        output: "box",
        output_geometry: "polygon",
        model_id: "sam-mask",
        score_threshold: 0.7,
      });
      expect(result.current.candidates[0].type).toBe("rectanglelabels");
      expect(result.current.error).toBeNull();

      act(() => result.current.rerunExemplar("mask"));
      await waitFor(() => expect(result.current.candidates[0]?.type).toBe("mask"));
      expect(interactiveAnnotateMock.mock.calls[1][2].context).toMatchObject({
        output: "mask",
        output_geometry: "mask",
      });

      act(() => result.current.rerunExemplar("box"));
      await waitFor(() => expect(result.current.candidates[0]?.type).toBe("rectanglelabels"));
      expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2);
      expect(defaults.output_geometry).toBe("mask");
      expect(extra.output_geometry).toBe("mask");
    });

    it("视频自定义 transport 的仅框召回同样不携带原生 Mask 契约", async () => {
      const transport = vi.fn().mockResolvedValue({ result: [], frame_index: 12 });
      const { result } = renderHook(() =>
        useInteractiveAI({
          ...ARGS,
          transport,
          cacheScope: "frame:12",
          requestContextDefaults: { model_id: "sam-mask", output_geometry: "mask" },
        }),
      );

      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "box"));
      await waitFor(() => expect(transport).toHaveBeenCalledTimes(1));
      expect(transport.mock.calls[0][0].context).toMatchObject({
        type: "exemplar",
        output: "box",
        output_geometry: "polygon",
      });
      expect(interactiveAnnotateMock).not.toHaveBeenCalled();
    });

    it("runExemplar 累加正/负框, 每次重发全量 exemplars[]", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));

      act(() => result.current.runExemplar([0.1, 0.1, 0.2, 0.2], 1, "mask"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));
      expect(interactiveAnnotateMock.mock.calls[0][2].context).toMatchObject({
        type: "exemplar",
        exemplars: [{ bbox: [0.1, 0.1, 0.2, 0.2], label: true }],
        output: "mask",
      });

      act(() => result.current.runExemplar([0.5, 0.5, 0.6, 0.6], 0, "mask"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
      // 第二次重发全量 (正框 + 负框)。
      expect(interactiveAnnotateMock.mock.calls[1][2].context.exemplars).toEqual([
        { bbox: [0.1, 0.1, 0.2, 0.2], label: true },
        { bbox: [0.5, 0.5, 0.6, 0.6], label: false },
      ]);
      // 会话框镜像供画布 overlay。
      expect(result.current.sessionExemplars).toEqual([
        { bbox: [0.1, 0.1, 0.2, 0.2], polarity: 1 },
        { bbox: [0.5, 0.5, 0.6, 0.6], polarity: 0 },
      ]);
    });

    it("setExemplarText 会话进行中即重跑, 携带 text 组合", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));

      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "mask"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));

      act(() => result.current.setExemplarText("car"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
      expect(interactiveAnnotateMock.mock.calls[1][2].context).toMatchObject({
        type: "exemplar",
        exemplars: [{ bbox: [0.1, 0.1, 0.3, 0.3], label: true }],
        text: "car",
      });
    });

    it("setExemplarThreshold 会话进行中重过滤, 携带 score_threshold", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));

      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "mask"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));

      act(() => result.current.setExemplarThreshold(0.8));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
      expect(interactiveAnnotateMock.mock.calls[1][2].context.score_threshold).toBe(0.8);
    });

    it("无会话时 setExemplarText/Threshold 不发请求 (仅暂存)", async () => {
      const { result } = renderHook(() => useInteractiveAI(ARGS));
      act(() => result.current.setExemplarText("car"));
      act(() => result.current.setExemplarThreshold(0.7));
      await new Promise((r) => setTimeout(r, 20));
      expect(interactiveAnnotateMock).not.toHaveBeenCalled();
      expect(result.current.exemplarText).toBe("car");
      expect(result.current.exemplarThreshold).toBe(0.7);
    });

    it("rerunExemplar(outputMode) 用当前会话重跑, 透传新 output", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));
      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "mask"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1));

      act(() => result.current.rerunExemplar("both"));
      await waitFor(() => expect(interactiveAnnotateMock).toHaveBeenCalledTimes(2));
      expect(interactiveAnnotateMock.mock.calls[1][2].context.output).toBe("both");
    });

    it("切到 point/bbox 模式 → 重置 exemplar 会话框", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));
      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "mask"));
      await waitFor(() => expect(result.current.sessionExemplars).toHaveLength(1));
      act(() => result.current.runBbox([0.4, 0.4, 0.6, 0.6]));
      expect(result.current.sessionExemplars).toHaveLength(0);
    });

    it("cancel 清空 exemplar 会话 (框 + text + 阈值)", async () => {
      interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
      const { result } = renderHook(() => useInteractiveAI(ARGS));
      act(() => result.current.runExemplar([0.1, 0.1, 0.3, 0.3], 1, "mask"));
      await waitFor(() => expect(result.current.sessionExemplars).toHaveLength(1));
      act(() => result.current.setExemplarText("car"));
      act(() => result.current.cancel());
      expect(result.current.sessionExemplars).toHaveLength(0);
      expect(result.current.exemplarText).toBe("");
      expect(result.current.exemplarThreshold).toBeNull();
    });
  });

  // claude[bot] P1 #2 回归: cache 命中分支需显式 setIsRunning(false), 否则上一次 in-flight
  // 被 abort + inflightRef 已自增 → 旧请求 finally 守卫不再通过, 旋转图标永不清除。
  // 复现路径: 真实请求 in-flight 时同一 key 再发一次 (命中 cache) → isRunning 应回 false。
  it("§P1 #2 · cache 命中 (紧跟 in-flight) 时 isRunning 必须复位", async () => {
    // 第 1 发: 真实 HTTP, 永不 resolve (模拟 in-flight)。
    let firstResolve: (v: unknown) => void = () => {};
    const firstPromise = new Promise((res) => {
      firstResolve = res;
    });
    // 第 2 发: 同 key, 应命中本地 cache, 不进 mock。
    interactiveAnnotateMock.mockImplementationOnce(() => firstPromise);

    const { result } = renderHook(() => useInteractiveAI(ARGS));
    // 第 1 次: in-flight, isRunning=true。
    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    await waitFor(() => expect(result.current.isRunning).toBe(true));

    // 让第 1 个请求 resolve 出候选, 触发 cache 写入 (同 key 下次命中)。
    await act(async () => {
      firstResolve(POLY_RESPONSE);
      await firstPromise;
    });
    await waitFor(() => expect(result.current.isRunning).toBe(false));
    await waitFor(() => expect(result.current.candidates).toHaveLength(1));

    // 第 2 次相同 bbox: 命中 cache; 旧 abort + inflightRef++ 仍发生, 但 isRunning 必须回 false。
    act(() => result.current.runBbox([0.1, 0.1, 0.4, 0.4]));
    // 不要 await waitFor (cache 同步生效); 立即断言。
    expect(result.current.isRunning).toBe(false);
    // 第 2 次未发 HTTP (cache 命中)。
    expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1);
  });
});

// v0.21.23 · transport 注入 + cacheScope（视频当前帧交互式 SAM 复用本 hook）
describe("useInteractiveAI · transport 注入与 cacheScope", () => {
  beforeEach(() => {
    interactiveAnnotateMock.mockReset();
    pushToastMock.mockReset();
  });

  it("省略 transport → 走图片链路 interactiveAnnotate（默认行为不变）", async () => {
    interactiveAnnotateMock.mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI(ARGS));
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates.length).toBe(1));
    expect(interactiveAnnotateMock).toHaveBeenCalledTimes(1);
  });

  it("给了 transport → 全部请求改走它，interactiveAnnotate 不被调用", async () => {
    const transport = vi.fn().mockResolvedValue(POLY_RESPONSE);
    const { result } = renderHook(() => useInteractiveAI({ ...ARGS, transport, cacheScope: 7 }));
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates.length).toBe(1));

    expect(interactiveAnnotateMock).not.toHaveBeenCalled();
    expect(transport).toHaveBeenCalledTimes(1);
    const call = transport.mock.calls[0][0];
    expect(call.projectId).toBe("p1");
    expect(call.taskId).toBe("t1");
    expect(call.mlBackendId).toBe("b1");
    expect(call.context).toMatchObject({ type: "point", points: [[0.5, 0.5]] });
    expect(call.signal).toBeInstanceOf(AbortSignal);
  });

  it("同一 prompt 在不同 cacheScope 下不串候选（跨帧缓存隔离）", async () => {
    const transport = vi.fn().mockResolvedValue(POLY_RESPONSE);
    const { result, rerender } = renderHook(
      ({ scope }) => useInteractiveAI({ ...ARGS, transport, cacheScope: scope }),
      { initialProps: { scope: 1 } },
    );

    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(1));

    // cancel 明确释放原始候选缓存，同 prompt 也必须重新推理。
    act(() => result.current.cancel());
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates.length).toBe(1));
    expect(transport).toHaveBeenCalledTimes(2);

    // 换帧（scope 变）→ 同样的 prompt 必须重新推理，不能复用上一帧候选。
    rerender({ scope: 2 });
    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(3));
  });

  it("cacheScope 变化 → 点会话重置（上一帧的点不喂给这一帧）", async () => {
    const transport = vi.fn().mockResolvedValue(POLY_RESPONSE);
    const { result, rerender } = renderHook(
      ({ scope }) => useInteractiveAI({ ...ARGS, transport, cacheScope: scope }),
      { initialProps: { scope: 1 } },
    );

    act(() => result.current.runPoint([0.2, 0.2], 1));
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(1));
    expect(result.current.sessionPoints.length).toBe(1);

    rerender({ scope: 2 });
    await waitFor(() => expect(result.current.sessionPoints.length).toBe(0));

    // 新帧首击 → 只带 1 个点（若会话未重置会带 2 个）。
    act(() => result.current.runPoint([0.8, 0.8], 1));
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(2));
    expect(transport.mock.calls[1][0].context.points).toEqual([[0.8, 0.8]]);
  });

  // 回归: 候选跨帧存活会被采纳到新帧上 —— 提示发生在 F20、Enter 时画布已到 F28,
  // 于是标注落在 F28 而当前帧看不见它。候选只属它被算出来的那一帧。
  it("cacheScope 变化 → 候选清空（不得跨帧存活被采纳到新帧）", async () => {
    const transport = vi.fn().mockResolvedValue(POLY_RESPONSE);
    const { result, rerender } = renderHook(
      ({ scope }) => useInteractiveAI({ ...ARGS, transport, cacheScope: scope }),
      { initialProps: { scope: 20 } },
    );

    act(() => result.current.runPoint([0.5, 0.5], 1));
    await waitFor(() => expect(result.current.candidates.length).toBeGreaterThan(0));

    rerender({ scope: 28 });
    await waitFor(() => expect(result.current.candidates).toEqual([]));
    expect(result.current.activeIdx).toBe(0);
  });
});

describe("simplifyCandidateRing · 候选顶点简化", () => {
  const area = (r: [number, number][]) => {
    let a = 0;
    for (let i = 0; i < r.length; i++) {
      const [x1, y1] = r[i];
      const [x2, y2] = r[(i + 1) % r.length];
      a += x1 * y2 - x2 * y1;
    }
    return Math.abs(a) / 2;
  };

  /** 逐像素台阶的锯齿圆环 —— 复刻 SAM mask 轮廓的形态。 */
  const jaggedRing = (cx: number, cy: number, radius: number, n: number, jitter: number) => {
    const ring: [number, number][] = [];
    for (let i = 0; i < n; i++) {
      const t = (i / n) * Math.PI * 2;
      // 交替 ±jitter 制造台阶; 幅度远小于半径, 简化后应被抹平。
      const r = radius + (i % 2 === 0 ? jitter : -jitter);
      ring.push([cx + r * Math.cos(t), cy + r * Math.sin(t)]);
    }
    return ring;
  };

  it("大目标: 顶点大幅减少, 面积基本不变", () => {
    const big = jaggedRing(0.5, 0.5, 0.35, 600, 0.0005);
    const out = simplifyCandidateRing(big);
    expect(out.length).toBeLessThan(big.length / 2);
    expect(area(out) / area(big)).toBeGreaterThan(0.99);
    expect(area(out) / area(big)).toBeLessThan(1.01);
  });

  it("小目标: 顶点几乎不动（容差随目标尺度缩小）", () => {
    // 与大目标同形状、同锯齿相对幅度, 只是整体缩小 20 倍。
    const small = jaggedRing(0.5, 0.5, 0.0175, 24, 0.000025);
    const out = simplifyCandidateRing(small);
    expect(out.length).toBeGreaterThanOrEqual(small.length - 2);
    expect(area(out) / area(small)).toBeGreaterThan(0.99);
  });

  it("固定像素容差会毁掉小目标 —— 这正是用相对容差的理由", () => {
    const small = jaggedRing(0.5, 0.5, 0.0175, 24, 0.000025);
    // 5px / 1920 ≈ 0.0026, 足以压平路面, 但对这个半径 0.0175 的目标是灾难。
    const absolute = simplifyPolygon(small, 5 / 1920);
    expect(absolute.length).toBeLessThan(small.length / 2); // 24 → 8, 形状已面目全非
    // 相对容差下同一目标保住了形状。
    expect(simplifyCandidateRing(small).length).toBeGreaterThan(20);
  });

  it("三角形(顶点 < 4)原样返回, 不会退化", () => {
    const tri: [number, number][] = [
      [0, 0],
      [0.1, 0],
      [0.05, 0.1],
    ];
    expect(simplifyCandidateRing(tri)).toEqual(tri);
  });

  it("退化环(对角线为 0)原样返回", () => {
    const dot: [number, number][] = [
      [0.5, 0.5],
      [0.5, 0.5],
      [0.5, 0.5],
      [0.5, 0.5],
    ];
    expect(simplifyCandidateRing(dot)).toEqual(dot);
  });
});
