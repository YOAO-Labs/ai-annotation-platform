import { useState, type ReactNode } from "react";
import type { MLBackendCapability } from "@/api/ml-backends";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockSetup = vi.hoisted(() => vi.fn());

vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: { setup: mockSetup },
  mlBackendSetupQueryKey: (
    projectId: string | null | undefined,
    backendId: string | null | undefined,
  ) => ["ml-backends", projectId, backendId, "setup"],
}));

import { useMLCapabilities } from "./useMLCapabilities";

function Wrapper({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: 0 } },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve };
}

describe("useMLCapabilities", () => {
  beforeEach(() => {
    mockSetup.mockReset();
  });

  it("returns prompts and params from /setup response", async () => {
    mockSetup.mockResolvedValue({
      name: "sam3-backend",
      version: "0.10.1",
      model_version: "sam3.1",
      supported_prompts: ["bbox", "text", "exemplar"],
      supported_variants: [{ key: "sam_variant", variants: [{ value: "tiny" }] }],
      params: { type: "object", properties: { box_threshold: { type: "number" } } },
    });
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.prompts).toEqual(["bbox", "text", "exemplar"]);
    expect(result.current.isPromptSupported("bbox")).toBe(true);
    expect(result.current.isPromptSupported("point")).toBe(false);
    expect(result.current.paramsSchema?.type).toBe("object");
    expect(result.current.capability?.supported_variants?.[0]?.key).toBe("sam_variant");
  });

  it("falls back to point/interactive_box/text when supported_prompts missing", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockSetup.mockResolvedValue({ name: "legacy-backend" });
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.prompts).toEqual(["point", "interactive_box", "text"]);
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  it("returns empty prompts on error", async () => {
    mockSetup.mockRejectedValue(new Error("502 backend unreachable"));
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.prompts).toEqual([]);
    expect(result.current.isPromptSupported("bbox")).toBe(false);
  });

  it("is disabled when backendId is null", () => {
    const { result } = renderHook(() => useMLCapabilities("p1", null), { wrapper: Wrapper });
    expect(mockSetup).not.toHaveBeenCalled();
    expect(result.current.prompts).toEqual([]);
  });

  it("prefers active model prompts/params and defaults to first interactive model", async () => {
    mockSetup.mockResolvedValue({
      name: "multi-backend",
      // 顶层 supported_prompts 故意与 model 不同, 验证 activeModel 优先级.
      supported_prompts: ["text"],
      params: { type: "object", properties: { top_threshold: { type: "number" } } },
      models: [
        {
          id: "det",
          task: "detection",
          display_name: "检测模型",
          supported_prompts: ["text"],
          supported_geometric_outputs: ["bbox"],
        },
        {
          id: "seg",
          task: "interactive_seg",
          display_name: "交互分割",
          is_interactive: true,
          supported_prompts: ["point", "bbox"],
          supported_geometric_outputs: ["polygon"],
          params: { type: "object", properties: { mask_threshold: { type: "number" } } },
        },
      ],
    });
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    // 默认选第一个 interactive 的 model (seg).
    expect(result.current.activeModelId).toBe("seg");
    expect(result.current.hasMultipleModels).toBe(true);
    expect(result.current.prompts).toEqual(["point", "bbox"]);
    expect(result.current.paramsSchema?.properties?.mask_threshold).toBeTruthy();
  });

  it("setActiveModelId switches prompts/params to the chosen model", async () => {
    mockSetup.mockResolvedValue({
      name: "multi-backend",
      models: [
        {
          id: "det",
          task: "detection",
          supported_prompts: ["text"],
          params: { type: "object", properties: { box_threshold: { type: "number" } } },
        },
        {
          id: "seg",
          task: "interactive_seg",
          is_interactive: true,
          supported_prompts: ["point"],
        },
      ],
    });
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.activeModelId).toBe("seg");
    act(() => result.current.setActiveModelId("det"));
    await waitFor(() => expect(result.current.activeModelId).toBe("det"));
    expect(result.current.prompts).toEqual(["text"]);
    expect(result.current.paramsSchema?.properties?.box_threshold).toBeTruthy();
  });

  it("精确请求会过滤不兼容的持久化 model 偏好并回落到同 model 完整契约", async () => {
    mockSetup.mockResolvedValue({
      name: "multi-backend",
      models: [
        {
          id: "saved-but-split",
          is_interactive: true,
          supported_prompts: ["point"],
          supported_inputs: ["point_prompt"],
          supported_geometric_outputs: ["mask"],
        },
        {
          id: "native-refine",
          is_interactive: true,
          supported_prompts: ["point", "mask"],
          supported_inputs: ["point_prompt", "mask_prompt"],
          supported_geometric_outputs: ["mask"],
        },
      ],
    });
    const { result } = renderHook(
      () =>
        useMLCapabilities("p1", "b1", "saved-but-split", {
          prompt: "point",
          requiredInputs: ["point_prompt", "mask_prompt"],
          output: "mask",
        }),
      { wrapper: Wrapper },
    );

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.models.map((model) => model.id)).toEqual(["native-refine"]);
    expect(result.current.activeModelId).toBe("native-refine");
    expect(result.current.inputs).toEqual(["point_prompt", "mask_prompt"]);
    expect(result.current.isInputSupported("mask_prompt")).toBe(true);
  });

  it("暴露失败原因并通过原查询重试恢复能力", async () => {
    mockSetup.mockRejectedValueOnce(new Error("502 backend unreachable"));
    const { result } = renderHook(() => useMLCapabilities("p1", "b1"), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.error).toBe("502 backend unreachable"));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isFetching).toBe(false);
    expect(result.current.prompts).toEqual([]);

    const retry = deferred<MLBackendCapability>();
    mockSetup.mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.refetch();
    });
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    await act(async () => {
      retry.resolve({ name: "recovered", supported_prompts: ["point"] });
      await pending;
    });
    await waitFor(() => expect(result.current.error).toBeNull());
    expect(result.current.isError).toBe(false);
    expect(result.current.isFetching).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isPromptSupported("point")).toBe(true);
    expect(mockSetup).toHaveBeenCalledTimes(2);
    expect(mockSetup).toHaveBeenLastCalledWith("p1", "b1");
  });

  it("后台刷新与失败恢复不重置模型选择，也不误标初次加载", async () => {
    const capability: MLBackendCapability = {
      name: "multi",
      supported_prompts: [],
      models: [
        { id: "first", is_interactive: true, supported_prompts: ["point"] },
        { id: "selected", is_interactive: true, supported_prompts: ["interactive_box"] },
      ],
    };
    mockSetup.mockResolvedValueOnce(capability);
    const { result } = renderHook(() => useMLCapabilities("p1", "b1", "first"), {
      wrapper: Wrapper,
    });
    await waitFor(() => expect(result.current.activeModelId).toBe("first"));
    act(() => result.current.setActiveModelId("selected"));
    mockSetup.mockRejectedValueOnce(new Error("refresh failed"));
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.error).toBe("refresh failed"));
    expect(result.current.activeModelId).toBe("selected");

    const retry = deferred<MLBackendCapability>();
    mockSetup.mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.refetch();
    });
    await waitFor(() => expect(result.current.isFetching).toBe(true));
    expect(result.current.isLoading).toBe(false);
    expect(result.current.activeModelId).toBe("selected");
    // A second retry joins the running query instead of restarting its request.
    let duplicate!: Promise<unknown>;
    act(() => {
      duplicate = result.current.refetch();
    });
    expect(mockSetup).toHaveBeenCalledTimes(3);
    await act(async () => {
      retry.resolve(capability);
      await Promise.all([pending, duplicate]);
    });
    await waitFor(() => expect(result.current.error).toBeNull());
    expect(result.current.activeModelId).toBe("selected");
    expect(result.current.prompts).toEqual(["interactive_box"]);
  });

  it.each([
    { projectId: null, backendId: "b1" },
    { projectId: "p1", backendId: null },
  ])("缺少 project/backend 标识时重试不发请求：%j", async ({ projectId, backendId }) => {
    const { result } = renderHook(() => useMLCapabilities(projectId, backendId), {
      wrapper: Wrapper,
    });
    await act(async () => {
      await result.current.refetch();
    });
    expect(mockSetup).not.toHaveBeenCalled();
    expect(result.current.error).toBeNull();
    expect(result.current.isError).toBe(false);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.isFetching).toBe(false);
  });

  it("切换项目时旧查询的迟到恢复不覆盖当前错误", async () => {
    mockSetup.mockRejectedValueOnce(new Error("p1 failed"));
    const { result, rerender } = renderHook(({ projectId }) => useMLCapabilities(projectId, "b1"), {
      initialProps: { projectId: "p1" },
      wrapper: Wrapper,
    });
    await waitFor(() => expect(result.current.error).toBe("p1 failed"));
    const retry = deferred<MLBackendCapability>();
    mockSetup.mockReturnValueOnce(retry.promise);
    let pending!: Promise<unknown>;
    act(() => {
      pending = result.current.refetch();
    });
    mockSetup.mockRejectedValueOnce(new Error("p2 failed"));
    rerender({ projectId: "p2" });
    await waitFor(() => expect(result.current.error).toBe("p2 failed"));
    await act(async () => {
      retry.resolve({ name: "old", supported_prompts: ["point"] });
      await pending;
    });
    expect(result.current.error).toBe("p2 failed");
    expect(result.current.isPromptSupported("point")).toBe(false);
  });
});
