// v0.20.11 · buildSecondaryInferencePayload 能力→请求参数映射单测。
import { describe, it, expect, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createElement, type ReactNode } from "react";
import { tasksApi } from "@/api/tasks";
import type { MLModelCapability } from "@/api/ml-backends";
import type { SecondaryInferenceResponse } from "@/api/tasks";
import type { AnnotationResponse } from "@/types";
import {
  buildSecondaryInferencePayload,
  mergeSecondaryResult,
  needsTextPrompt,
  useRunSecondaryInference,
  type SecondaryCapability,
} from "./useSecondaryInference";

it("updates only the originating task cache when concurrent task requests finish out of order", async () => {
  const client = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { gcTime: Infinity } },
  });
  const a = { id: "a", task_id: "task-a", attributes: {} } as AnnotationResponse;
  const b = { id: "b", task_id: "task-b", attributes: {} } as AnnotationResponse;
  client.setQueryData(["annotations", "task-a"], [a]);
  client.setQueryData(["annotations", "task-b"], [b]);
  let resolveA!: (response: SecondaryInferenceResponse) => void;
  const request = vi
    .spyOn(tasksApi, "secondaryInference")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveA = resolve;
        }),
    )
    .mockResolvedValueOnce({
      annotation: { ...b, attributes: { result: "B" } },
      created_children: [],
    } as unknown as SecondaryInferenceResponse);
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const view = renderHook(() => useRunSecondaryInference(), { wrapper });
  try {
    let pendingA!: Promise<SecondaryInferenceResponse>;
    await act(async () => {
      pendingA = view.result.current.mutateAsync({
        taskId: "task-a",
        annotationId: "a",
        body: { ml_backend_id: "be", model_id: "model" },
      });
    });
    view.rerender();
    await act(async () => {
      await view.result.current.mutateAsync({
        taskId: "task-b",
        annotationId: "b",
        body: { ml_backend_id: "be", model_id: "model" },
      });
    });
    await act(async () => {
      resolveA({
        annotation: { ...a, attributes: { result: "A" } },
        created_children: [],
      } as unknown as SecondaryInferenceResponse);
      await pendingA;
    });
    expect(request).toHaveBeenNthCalledWith(1, "task-a", "a", expect.anything());
    expect(request).toHaveBeenNthCalledWith(2, "task-b", "b", expect.anything());
    expect(client.getQueryData(["annotations", "task-a"])).toEqual([
      expect.objectContaining({ id: "a", attributes: { result: "A" } }),
    ]);
    expect(client.getQueryData(["annotations", "task-b"])).toEqual([
      expect.objectContaining({ id: "b", attributes: { result: "B" } }),
    ]);
  } finally {
    view.unmount();
    client.clear();
    request.mockRestore();
  }
});

function cap(
  model: Partial<MLModelCapability>,
  writeTarget: "attributes" | "geometry",
): SecondaryCapability {
  return {
    backendId: "be-1",
    backendName: "yolo",
    model: { id: "m1", ...model } as MLModelCapability,
    writeTarget,
    label: model.display_name || "m1",
  };
}

describe("buildSecondaryInferencePayload", () => {
  it("geometry: 带 model_variants (协议 v2), 无 write_keys", () => {
    const payload = buildSecondaryInferencePayload(
      cap({ id: "yolo-det", task: "detection", default_variants: { size: "l" } }, "geometry"),
    );
    expect(payload.ml_backend_id).toBe("be-1");
    expect(payload.write_target).toBe("geometry");
    expect(payload.model_id).toBe("yolo-det");
    expect(payload.model_variants).toEqual({ size: "l" });
    expect(payload.task).toBe("detect");
    expect(payload.write_keys).toBeNull();
  });

  it("geometry: 无 default_variants 回落 {} (仍走 v2 透传)", () => {
    const payload = buildSecondaryInferencePayload(
      cap({ id: "det2", task: "detection" }, "geometry"),
    );
    expect(payload.model_variants).toEqual({});
  });

  it("attributes: model_variants=null (扁平路径), write_keys 取输出属性 schema 键", () => {
    const payload = buildSecondaryInferencePayload(
      cap(
        {
          id: "onnx-cls",
          task: "classify",
          output_attribute_schema: [
            { key: "color", label: "颜色", type: "select", options: [] },
            { key: "vehicle_type", label: "类型", type: "select", options: [] },
          ] as MLModelCapability["output_attribute_schema"],
        },
        "attributes",
      ),
    );
    expect(payload.write_target).toBe("attributes");
    expect(payload.model_variants).toBeNull();
    expect(payload.write_keys).toEqual(["color", "vehicle_type"]);
    expect(payload.task).toBe("classify");
  });

  it("attributes: 无输出 schema → write_keys=null (全取)", () => {
    const payload = buildSecondaryInferencePayload(cap({ id: "ocr", task: "ocr" }, "attributes"));
    expect(payload.write_keys).toBeNull();
  });

  it("params: 用户调过的参数带进请求; 空 → null", () => {
    const c = cap({ id: "det", task: "detection" }, "geometry");
    expect(buildSecondaryInferencePayload(c, { score_threshold: 0.4 }).params).toEqual({
      score_threshold: 0.4,
    });
    expect(buildSecondaryInferencePayload(c, {}).params).toBeNull();
    expect(buildSecondaryInferencePayload(c).params).toBeNull();
  });

  it("variants: 用户所选档位覆盖模型默认 (缺轴回落默认)", () => {
    const c = cap(
      { id: "yolo-det", task: "detection", default_variants: { series: "yolo11", size: "l" } },
      "geometry",
    );
    // 用户只改 size, series 保留默认。
    expect(buildSecondaryInferencePayload(c, undefined, { size: "s" }).model_variants).toEqual({
      series: "yolo11",
      size: "s",
    });
    // 未传 variants → 纯默认。
    expect(buildSecondaryInferencePayload(c).model_variants).toEqual({
      series: "yolo11",
      size: "l",
    });
  });

  it("variants: 无变体轴的 attributes 能力 → null (走扁平路径)", () => {
    const c = cap({ id: "cls", task: "classify" }, "attributes");
    expect(buildSecondaryInferencePayload(c, undefined, { size: "s" }).model_variants).toBeNull();
  });

  it("variants: 有变体轴的 OCR 属性能力 → 带 model_variants (协议 v2)", () => {
    // rapidocr rec/e2e 是 attributes 写回, 但声明了 version/size/lang 轴, 也要能选档透传。
    const c = cap(
      {
        id: "ocr-rec",
        task: "ocr",
        default_variants: { version: "v5", size: "mobile", lang: "universal" },
        supported_variants: [
          { key: "lang", variants: [{ value: "universal" }, { value: "en" }] },
        ] as MLModelCapability["supported_variants"],
      },
      "attributes",
    );
    // 用户改 lang, version/size 保留默认。
    expect(buildSecondaryInferencePayload(c, undefined, { lang: "en" }).model_variants).toEqual({
      version: "v5",
      size: "mobile",
      lang: "en",
    });
  });

  it("prompt: 开集文本带进请求 (trim); 空/空白 → null", () => {
    const c = cap({ id: "gdino", task: "detection" }, "geometry");
    expect(buildSecondaryInferencePayload(c, undefined, undefined, "  car . person  ").prompt).toBe(
      "car . person",
    );
    expect(buildSecondaryInferencePayload(c, undefined, undefined, "   ").prompt).toBeNull();
    expect(buildSecondaryInferencePayload(c).prompt).toBeNull();
  });

  it("needsTextPrompt: supported_prompts 含 text → true", () => {
    expect(needsTextPrompt(cap({ id: "g", supported_prompts: ["text"] }, "geometry"))).toBe(true);
    expect(needsTextPrompt(cap({ id: "y", supported_prompts: ["none"] }, "geometry"))).toBe(false);
    expect(needsTextPrompt(cap({ id: "z" }, "geometry"))).toBe(false);
  });
});

describe("mergeSecondaryResult", () => {
  const box = (id: string, extra: Partial<AnnotationResponse> = {}) =>
    ({
      id,
      task_id: "t-1",
      annotation_type: "bbox",
      class_name: "car",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      ...extra,
    }) as AnnotationResponse;

  const resp = (
    annotation: AnnotationResponse,
    created_children: AnnotationResponse[] = [],
  ): SecondaryInferenceResponse => ({ annotation, created_children });

  it("子框直接追加进缓存 (无需 refetch, 画布/侧栏立即可见)", () => {
    const prev = [box("a")];
    const out = mergeSecondaryResult(prev, resp(box("a"), [box("c1"), box("c2")]));
    expect(out?.map((a) => a.id)).toEqual(["a", "c1", "c2"]);
  });

  it("原框并入 AI 写的属性 + 溯源, 几何不动 (不覆盖 in-flight 乐观写)", () => {
    const prev = [box("a", { geometry: { type: "bbox", x: 0.9, y: 0.9, w: 0.05, h: 0.05 } })];
    const server = box("a", {
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
      attributes: { color: "blue" },
      attributes_meta: { color: { origin: "ai" } },
    });
    const [merged] = mergeSecondaryResult(prev, resp(server))!;
    expect(merged.attributes).toEqual({ color: "blue" });
    expect(merged.attributes_meta?.color?.origin).toBe("ai");
    // 用户拖动中的几何保留, 未被服务端快照顶回。
    expect(merged.geometry).toEqual({ type: "bbox", x: 0.9, y: 0.9, w: 0.05, h: 0.05 });
  });

  it("已在缓存里的子框不重复追加 (重复 onSuccess 幂等)", () => {
    const prev = [box("a"), box("c1")];
    const out = mergeSecondaryResult(prev, resp(box("a"), [box("c1")]));
    expect(out?.map((a) => a.id)).toEqual(["a", "c1"]);
  });

  it("缓存未建立 → 原样返回, 不凭空造列表", () => {
    expect(mergeSecondaryResult(undefined, resp(box("a"), [box("c1")]))).toBeUndefined();
  });
});
