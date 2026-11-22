import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { MLModelCapability } from "@/api/ml-backends";
import type { ToolBindings } from "@/api/projects";
import { useCapabilityValidation } from "./useCapabilityValidation";

function model(partial: Partial<MLModelCapability>): MLModelCapability {
  return { id: "m1", display_name: "测试模型", ...partial };
}

describe("useCapabilityValidation", () => {
  it("returns no warnings when there is no active model", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: undefined,
        enabledToolUnits: new Set(["bbox"]),
        toolBindings: {},
      }),
    );
    expect(result.current).toEqual([]);
  });

  it("warns when model geometry has no matching enabled tool_unit", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({ supported_geometric_outputs: ["polygon"] }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings: {},
      }),
    );
    expect(result.current).toHaveLength(1);
    expect(result.current[0].key).toBe("geom-polygon");
  });

  it("does not warn when geometry matches an enabled unit", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({ supported_geometric_outputs: ["bbox"] }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings: {},
      }),
    );
    expect(result.current).toEqual([]);
  });

  it("skips geometry check when project has no explicit enabled units (legacy)", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({ supported_geometric_outputs: ["polygon"] }),
        enabledToolUnits: null,
        toolBindings: {},
      }),
    );
    expect(result.current).toEqual([]);
  });

  it("warns when model outputs text but project has no text attribute", () => {
    const toolBindings: ToolBindings = {
      bbox: {
        enabled: true,
        attribute_schema: { fields: [{ key: "tag", label: "标签", type: "select" }] },
      },
    };
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({
          task: "ocr",
          supported_geometric_outputs: ["bbox"],
          output_attribute_types: ["text"],
        }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings,
      }),
    );
    expect(result.current.some((w) => w.key === "attr-text")).toBe(true);
  });

  it("does not warn about text when a text attribute exists", () => {
    const toolBindings: ToolBindings = {
      bbox: {
        enabled: true,
        attribute_schema: { fields: [{ key: "ocr_text", label: "文本", type: "text" }] },
      },
    };
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({
          task: "ocr",
          supported_geometric_outputs: ["bbox"],
          output_attribute_types: ["text"],
        }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings,
      }),
    );
    expect(result.current.some((w) => w.key === "attr-text")).toBe(false);
  });

  it("does not warn about class (taxonomy 几乎恒在, 刻意跳过)", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({
          supported_geometric_outputs: ["bbox"],
          output_attribute_types: ["class"],
        }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings: {},
      }),
    );
    expect(result.current.some((w) => w.key.startsWith("attr-"))).toBe(false);
  });

  it("warns when model outputs language but project has no key=language field", () => {
    const toolBindings: ToolBindings = {
      bbox: {
        enabled: true,
        attribute_schema: { fields: [{ key: "ocr_text", label: "文本", type: "text" }] },
      },
    };
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({ task: "ocr", output_attribute_types: ["language"] }),
        enabledToolUnits: new Set(["bbox"]),
        toolBindings,
      }),
    );
    expect(result.current.some((w) => w.key === "attr-language")).toBe(true);
  });

  it("does not warn about orientation when rotated_bbox tool is enabled", () => {
    const { result } = renderHook(() =>
      useCapabilityValidation({
        activeModel: model({ output_attribute_types: ["orientation"] }),
        enabledToolUnits: new Set(["rotated_bbox"]),
        toolBindings: {},
      }),
    );
    expect(result.current.some((w) => w.key === "attr-orientation")).toBe(false);
  });
});
