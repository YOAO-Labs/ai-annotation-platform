/**
 * useBackendRouting 纯路由逻辑单测.
 * 覆盖: capIndex 构建 (多 model / 单 model / 文本能力 / tracker) · resolveInteractive 三情形 ·
 * 兜底链 (preferred → 项目默认 → 注册序) · reachable 降级 · pickDefaultPreferred。
 */
import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import type { MLBackendCapability } from "@/api/ml-backends";
import {
  buildCapEntry,
  candidatesFor,
  candidatesForRequest,
  resolveInteractive,
  resolveInteractiveRequest,
  pickDefaultPreferred,
  capFingerprint,
  useBackendRouting,
  type BackendRoutingArgs,
  type CapIndex,
} from "../useBackendRouting";
import { useMLCapabilities } from "../useMLCapabilities";

const mockSetup = vi.hoisted(() => vi.fn());
vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: { setup: mockSetup },
  mlBackendSetupQueryKey: (
    projectId: string | null | undefined,
    backendId: string | null | undefined,
  ) => ["ml-backends", projectId, backendId, "setup"],
}));

// gsam2 协议 2.1: 4 个 task model, 仅 interactive_seg 交互 (point/interactive_box); detection/seg 带 text。
// tracker 的 "bbox" 是视频追踪种子，不属于图像交互 prompt。
const GSAM2: MLBackendCapability = {
  name: "grounded-sam2",
  supported_prompts: ["point", "interactive_box", "text"],
  models: [
    { id: "det", task: "detection", is_interactive: false, supported_prompts: ["text"] },
    { id: "seg", task: "segmentation", is_interactive: false, supported_prompts: ["text"] },
    {
      id: "iseg",
      task: "interactive_seg",
      is_interactive: true,
      supported_prompts: ["point", "interactive_box"],
    },
    {
      id: "trk",
      task: "tracker",
      is_interactive: true,
      supported_prompts: ["bbox"],
      supported_trackers: ["sam2_video"],
    },
  ],
};

// yolo: 闭集几何, 无 prompt, 非交互。
const YOLO: MLBackendCapability = {
  name: "yolo",
  supported_prompts: ["none"],
  models: [{ id: "y-det", task: "detection", is_interactive: false, supported_prompts: ["none"] }],
};

// sam3: 老式单 model 后端 (无 models[]), 顶层 interactive_box/text/exemplar 交互。
const SAM3: MLBackendCapability = {
  name: "sam3",
  is_interactive: true,
  supported_prompts: ["interactive_box", "text", "exemplar"],
};

describe("buildCapEntry", () => {
  it("gsam2: 交互 prompt 仅取 is_interactive model 的并集 (point/interactive_box), text 来自非交互 model", () => {
    const e = buildCapEntry(GSAM2);
    // tracker 的 "bbox" 是视频种子, 不计入交互 prompt 集。
    expect([...e.prompts].sort()).toEqual(["interactive_box", "point"]);
    expect(e.prompts.has("exemplar")).toBe(false);
    expect(e.textCapable).toBe(true);
    expect(e.isInteractive).toBe(true);
    expect(e.trackers).toEqual(["sam2_video"]);
    expect(e.reachable).toBe(true);
  });

  it("yolo: 无交互 prompt, 非交互, 无文本", () => {
    const e = buildCapEntry(YOLO);
    expect(e.prompts.size).toBe(0);
    expect(e.textCapable).toBe(false);
    expect(e.isInteractive).toBe(false);
  });

  it("sam3: 老式单 model 走顶层字段, 交互 prompt = interactive_box/exemplar (text 归批量)", () => {
    const e = buildCapEntry(SAM3);
    expect([...e.prompts].sort()).toEqual(["exemplar", "interactive_box"]);
    expect(e.textCapable).toBe(true);
    expect(e.isInteractive).toBe(true);
  });

  it("保留 tracker 的文本驱动子集供模型选择器使用", () => {
    const e = buildCapEntry({
      ...SAM3,
      supported_trackers: ["sam3_video", "sam3_video_interactive"],
      text_driven_trackers: ["sam3_video"],
    });
    expect(e.trackers).toEqual(["sam3_video", "sam3_video_interactive"]);
    expect(e.textDrivenTrackers).toEqual(["sam3_video"]);
  });

  it("undefined (拉取失败): reachable=false, 全空", () => {
    const e = buildCapEntry(undefined);
    expect(e.reachable).toBe(false);
    expect(e.prompts.size).toBe(0);
    expect(e.isInteractive).toBe(false);
  });
});

function mkIndex(entries: Record<string, MLBackendCapability | undefined>): CapIndex {
  const idx: CapIndex = {};
  for (const [id, cap] of Object.entries(entries)) idx[id] = buildCapEntry(cap);
  return idx;
}

describe("candidatesFor", () => {
  it("按注册序返回 reachable 且支持该 prompt 的后端", () => {
    const idx = mkIndex({ yolo: YOLO, gsam2: GSAM2, sam3: SAM3 });
    const order = ["yolo", "gsam2", "sam3"];
    expect(candidatesFor(idx, order, "point")).toEqual(["gsam2"]);
    expect(candidatesFor(idx, order, "interactive_box")).toEqual(["gsam2", "sam3"]);
    expect(candidatesFor(idx, order, "exemplar")).toEqual(["sam3"]);
  });
});

describe("精确交互请求路由", () => {
  it("不把同一后端不同 model 的 prompt/input/output 能力拼成虚假组合", () => {
    const split: MLBackendCapability = {
      name: "split",
      supported_prompts: ["point", "mask"],
      models: [
        {
          id: "point-only",
          is_interactive: true,
          supported_prompts: ["point"],
          supported_inputs: ["point_prompt"],
          supported_geometric_outputs: ["mask"],
        },
        {
          id: "mask-only",
          is_interactive: true,
          supported_prompts: ["mask"],
          supported_inputs: ["mask_prompt"],
          supported_geometric_outputs: ["mask"],
        },
      ],
    };
    const nativeRefine: MLBackendCapability = {
      name: "native-refine",
      supported_prompts: ["point", "mask"],
      models: [
        {
          id: "refine",
          is_interactive: true,
          supported_prompts: ["point", "mask"],
          supported_inputs: ["point_prompt", "mask_prompt"],
          supported_geometric_outputs: ["mask"],
        },
      ],
    };
    const idx = mkIndex({ split, nativeRefine });
    const requirement = {
      prompt: "point" as const,
      requiredInputs: ["point_prompt", "mask_prompt"],
      output: "mask",
    };

    expect(candidatesForRequest(idx, ["split", "nativeRefine"], requirement)).toEqual([
      "nativeRefine",
    ]);
    expect(
      resolveInteractiveRequest(idx, ["split", "nativeRefine"], "split", "split", requirement),
    ).toBe("nativeRefine");
  });
});

describe("resolveInteractive — 三情形 + 兜底链", () => {
  it("情形1 只1个交互后端: 全部路由到它", () => {
    const idx = mkIndex({ yolo: YOLO, gsam2: GSAM2 });
    const order = ["yolo", "gsam2"];
    // 项目默认 = yolo (非交互), preferred 缺省
    expect(resolveInteractive(idx, order, "yolo", null, "point")).toBe("gsam2");
    expect(resolveInteractive(idx, order, "yolo", null, "interactive_box")).toBe("gsam2");
    // exemplar 无候选 → null (工具置灰)
    expect(resolveInteractive(idx, order, "yolo", null, "exemplar")).toBeNull();
  });

  it("情形2 两个都支持 interactive_box: preferred 优先, 缺省回落项目默认", () => {
    const idx = mkIndex({ gsam2: GSAM2, sam3: SAM3 });
    const order = ["gsam2", "sam3"];
    // 项目默认 = gsam2, 无 preferred → 默认优先
    expect(resolveInteractive(idx, order, "gsam2", null, "interactive_box")).toBe("gsam2");
    // 用户 preferred = sam3 → 覆盖默认 (关键: 默认本身交互时选择器仍生效)
    expect(resolveInteractive(idx, order, "gsam2", "sam3", "interactive_box")).toBe("sam3");
  });

  it("情形3 异构 A只point B只interactive_box: 自动分流", () => {
    const A: MLBackendCapability = {
      name: "A",
      supported_prompts: ["point"],
      models: [{ id: "a", is_interactive: true, supported_prompts: ["point"] }],
    };
    const B: MLBackendCapability = {
      name: "B",
      supported_prompts: ["interactive_box"],
      models: [{ id: "b", is_interactive: true, supported_prompts: ["interactive_box"] }],
    };
    const idx = mkIndex({ A, B });
    const order = ["A", "B"];
    // preferred=A 但 interactive_box 不被 A 支持 → 按兜底链落到 B
    expect(resolveInteractive(idx, order, null, "A", "point")).toBe("A");
    expect(resolveInteractive(idx, order, null, "A", "interactive_box")).toBe("B");
  });

  it("兜底链: preferred 不在候选 → 项目默认 → 注册序第一个", () => {
    const idx = mkIndex({ gsam2: GSAM2, sam3: SAM3 });
    const order = ["gsam2", "sam3"];
    // preferred=不存在的id, 默认=sam3 (候选) → 用默认
    expect(resolveInteractive(idx, order, "sam3", "ghost", "interactive_box")).toBe("sam3");
    // preferred 和默认都不在候选 → 注册序第一个 (gsam2)
    expect(resolveInteractive(idx, order, "ghost", "ghost2", "interactive_box")).toBe("gsam2");
  });

  it("reachable 降级: 候选后端 /setup 失败 → 从候选排除", () => {
    const idx = mkIndex({ gsam2: undefined, sam3: SAM3 }); // gsam2 拉取失败
    const order = ["gsam2", "sam3"];
    expect(candidatesFor(idx, order, "interactive_box")).toEqual(["sam3"]);
    expect(resolveInteractive(idx, order, "gsam2", "gsam2", "interactive_box")).toBe("sam3");
    // point 仅 gsam2 支持但已不可达 → null
    expect(resolveInteractive(idx, order, "gsam2", null, "point")).toBeNull();
  });
});

describe("pickDefaultPreferred", () => {
  it("项目默认是交互后端 → 取它", () => {
    const idx = mkIndex({ yolo: YOLO, gsam2: GSAM2 });
    expect(pickDefaultPreferred(idx, ["yolo", "gsam2"], "gsam2")).toBe("gsam2");
  });
  it("项目默认非交互 (yolo) → 取第一个交互后端", () => {
    const idx = mkIndex({ yolo: YOLO, gsam2: GSAM2 });
    expect(pickDefaultPreferred(idx, ["yolo", "gsam2"], "yolo")).toBe("gsam2");
  });
  it("无交互后端 → null", () => {
    const idx = mkIndex({ yolo: YOLO });
    expect(pickDefaultPreferred(idx, ["yolo"], "yolo")).toBeNull();
  });
});

describe("capFingerprint — capSignature 内容变化感知", () => {
  it("undefined → 空串", () => {
    expect(capFingerprint(undefined)).toBe("");
  });
  it("同内容稳定 (两次 ok 之间不变 → 不触发多余重建)", () => {
    expect(capFingerprint(GSAM2)).toBe(capFingerprint(GSAM2));
    expect(capFingerprint(SAM3)).toBe(capFingerprint(SAM3));
  });
  it("supported_prompts 变化 → 指纹变化 (动态宣称能力可被感知)", () => {
    const sam3NoExemplar: MLBackendCapability = {
      ...SAM3,
      supported_prompts: ["bbox", "text"],
    };
    expect(capFingerprint(sam3NoExemplar)).not.toBe(capFingerprint(SAM3));
  });
  it("多 model 后端某 model 的 prompt 变化 → 指纹变化", () => {
    const gsam2More: MLBackendCapability = {
      ...GSAM2,
      models: GSAM2.models!.map((m) =>
        m.id === "iseg" ? { ...m, supported_prompts: ["point", "bbox", "exemplar"] } : m,
      ),
    };
    expect(capFingerprint(gsam2More)).not.toBe(capFingerprint(GSAM2));
  });
  it("tracker 变化 → 指纹变化", () => {
    const sam3Trk: MLBackendCapability = { ...SAM3, supported_trackers: ["sam2_video"] };
    expect(capFingerprint(sam3Trk)).not.toBe(capFingerprint(SAM3));
  });
  it("text-driven tracker 变化 → 指纹变化", () => {
    const sam3TextTrk: MLBackendCapability = {
      ...SAM3,
      text_driven_trackers: ["sam3_video"],
    };
    expect(capFingerprint(sam3TextTrk)).not.toBe(capFingerprint(SAM3));
  });
  it("同 model 的 input/output 契约变化 → 指纹变化", () => {
    const base: MLBackendCapability = {
      name: "native",
      supported_prompts: ["point"],
      models: [
        {
          id: "interactive",
          is_interactive: true,
          supported_prompts: ["point"],
          supported_inputs: ["point_prompt"],
          supported_geometric_outputs: ["polygon"],
        },
      ],
    };
    const changed: MLBackendCapability = {
      ...base,
      models: [
        {
          ...base.models![0],
          supported_inputs: ["point_prompt", "mask_prompt"],
          supported_geometric_outputs: ["mask"],
        },
      ],
    };
    expect(capFingerprint(changed)).not.toBe(capFingerprint(base));
  });
  it("视频纠错能力按单个 model 行保留，不交叉拼接", () => {
    const entry = buildCapEntry({
      name: "split-tracker",
      supported_prompts: [],
      models: [
        {
          id: "prompt-only",
          task: "tracker",
          supported_trackers: ["sam2_video"],
          supported_prompts: ["correction_frame"],
          supported_inputs: ["video", "mask_prompt"],
          supported_geometric_outputs: ["bbox"],
          max_window_frames: 32,
        },
        {
          id: "output-only",
          task: "tracker",
          supported_trackers: ["sam2_video"],
          supported_inputs: ["video"],
          supported_geometric_outputs: ["mask"],
          max_window_frames: 16,
        },
      ],
    });

    expect(entry.videoModels).toHaveLength(2);
    expect(entry.videoModels[0].outputs.has("mask")).toBe(false);
    expect(entry.videoModels[1].prompts.has("correction_frame")).toBe(false);
    expect(entry.videoModels.map((model) => model.maxWindowFrames)).toEqual([32, 16]);
  });
  it("视频单窗上限变化 → 指纹变化", () => {
    const base: MLBackendCapability = {
      name: "tracker",
      supported_prompts: [],
      models: [
        {
          id: "tracker",
          task: "tracker",
          supported_trackers: ["sam2_video"],
          max_window_frames: 16,
        },
      ],
    };
    const changed: MLBackendCapability = {
      ...base,
      models: [{ ...base.models![0], max_window_frames: 32 }],
    };

    expect(capFingerprint(changed)).not.toBe(capFingerprint(base));
  });
});

const routingClients: QueryClient[] = [];
function routingWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  routingClients.push(client);
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

function routingHarness(overrides: Partial<BackendRoutingArgs> = {}) {
  const args: BackendRoutingArgs = {
    projectId: "p1",
    backends: [
      { id: "a", name: "Backend A" },
      { id: "b", name: "Backend B" },
    ],
    defaultBackendId: "a",
    savedInteractiveBackendId: "b",
    onSaveInteractiveBackend: vi.fn(),
    ...overrides,
  };
  return {
    ...renderHook((props: BackendRoutingArgs) => useBackendRouting(props), {
      initialProps: args,
      wrapper: routingWrapper(),
    }),
    args,
  };
}

function deferredCapability() {
  let resolve!: (capability: MLBackendCapability) => void;
  const promise = new Promise<MLBackendCapability>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

describe("useBackendRouting capability query recovery", () => {
  beforeEach(() => {
    mockSetup.mockReset();
  });
  afterEach(() => {
    routingClients.splice(0).forEach((client) => client.clear());
  });

  it("所有 setup 失败时仍提供逐后端错误，重试恢复路由及原偏好", async () => {
    mockSetup.mockImplementation((_projectId: string, backendId: string) =>
      Promise.reject(new Error(`${backendId} unavailable`)),
    );
    const { result, args } = routingHarness();
    await waitFor(() =>
      expect(result.current.capabilityErrors).toEqual([
        { backendId: "a", backendName: "Backend A", message: "a unavailable" },
        { backendId: "b", backendName: "Backend B", message: "b unavailable" },
      ]),
    );
    expect(result.current.resolveInteractive("point")).toBeNull();
    expect(result.current.isPromptSupported("point")).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isFetching).toBe(false);

    const a = deferredCapability();
    const b = deferredCapability();
    mockSetup.mockImplementation((_projectId: string, backendId: string) =>
      backendId === "a" ? a.promise : b.promise,
    );
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.retryCapabilities();
    });
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    await act(async () => {
      a.resolve(GSAM2);
      b.resolve(GSAM2);
      await pending;
    });
    await waitFor(() => expect(result.current.capabilityErrors).toEqual([]));
    expect(result.current.isFetching).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.resolveInteractive("point")).toBe("b");
    expect(result.current.preferredInteractiveId).toBe("b");
    expect(args.onSaveInteractiveBackend).not.toHaveBeenCalled();
    expect(mockSetup).toHaveBeenCalledTimes(4);
  });

  it("部分失败只重试失败后端，健康路由保持可用且不写偏好", async () => {
    mockSetup.mockImplementation((_projectId: string, backendId: string) =>
      backendId === "a" ? Promise.resolve(GSAM2) : Promise.reject(new Error("B setup failed")),
    );
    const { result, args } = routingHarness();
    await waitFor(() =>
      expect(result.current.capabilityErrors).toEqual([
        { backendId: "b", backendName: "Backend B", message: "B setup failed" },
      ]),
    );
    expect(result.current.resolveInteractive("point")).toBe("a");
    expect(result.current.resolveInteractive("exemplar")).toBeNull();
    const retry = deferredCapability();
    mockSetup.mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.retryCapabilities();
    });
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.resolveInteractive("point")).toBe("a");
    expect(mockSetup).toHaveBeenLastCalledWith("p1", "b");
    expect(mockSetup).toHaveBeenCalledTimes(3);
    await act(async () => {
      retry.resolve(SAM3);
      await pending;
    });
    await waitFor(() => expect(result.current.capabilityErrors).toEqual([]));
    expect(result.current.resolveInteractive("exemplar")).toBe("b");
    expect(result.current.preferredInteractiveId).toBe("b");
    expect(args.onSaveInteractiveBackend).not.toHaveBeenCalled();
  });

  it.each(["missing-project", "no-backends"])(
    "%s 不显示加载或错误，重试不发请求",
    async (scope) => {
      const { result } = routingHarness(
        scope === "missing-project" ? { projectId: null } : { backends: [] },
      );
      await act(async () => {
        await result.current.retryCapabilities();
      });
      expect(mockSetup).not.toHaveBeenCalled();
      expect(result.current.capabilityErrors).toEqual([]);
      expect(result.current.isFetching).toBe(false);
      expect(result.current.isLoading).toBe(false);
      expect(result.current.resolveInteractive("point")).toBeNull();
    },
  );

  it("退出项目后不保留旧错误，也不能触发旧项目重试", async () => {
    mockSetup.mockRejectedValue(new Error("old project unavailable"));
    const { result, rerender, args } = routingHarness();
    await waitFor(() => expect(result.current.capabilityErrors).toHaveLength(2));
    rerender({ ...args, projectId: null });
    await act(async () => {
      await result.current.retryCapabilities();
    });
    expect(result.current.capabilityErrors).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isFetching).toBe(false);
    expect(mockSetup).toHaveBeenCalledTimes(2);
  });

  it("单后端详情与路由复用同一 query，路由重试同步清除两处错误", async () => {
    mockSetup.mockRejectedValueOnce(new Error("shared setup failed"));
    const { result } = renderHook(
      () => ({
        routing: useBackendRouting({
          projectId: "p1",
          backends: [{ id: "a", name: "Backend A" }],
          defaultBackendId: "a",
          onSaveInteractiveBackend: vi.fn(),
        }),
        selected: useMLCapabilities("p1", "a"),
      }),
      { wrapper: routingWrapper() },
    );
    await waitFor(() => expect(result.current.selected.error).toBe("shared setup failed"));
    expect(result.current.routing.capabilityErrors).toHaveLength(1);
    expect(mockSetup).toHaveBeenCalledTimes(1);
    const retry = deferredCapability();
    mockSetup.mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    let duplicate!: Promise<unknown>;
    act(() => {
      pending = result.current.routing.retryCapabilities();
    });
    await waitFor(() => expect(result.current.selected.isFetching).toBe(true));
    act(() => {
      duplicate = result.current.selected.refetch();
    });
    expect(mockSetup).toHaveBeenCalledTimes(2);
    await act(async () => {
      retry.resolve(GSAM2);
      await Promise.all([pending, duplicate]);
    });
    await waitFor(() => expect(result.current.selected.error).toBeNull());
    expect(result.current.routing.capabilityErrors).toEqual([]);
    expect(result.current.routing.resolveInteractive("point")).toBe("a");
    expect(result.current.selected.isPromptSupported("point")).toBe(true);
  });
});
