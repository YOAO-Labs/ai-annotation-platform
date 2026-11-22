import { describe, expect, it } from "vitest";
import { buildUnitBindings, unitBindingsToPayload } from "./useProjectToolBindings";

describe("buildUnitBindings", () => {
  it("keeps video project settings on the video tool-unit set", () => {
    const bindings = buildUnitBindings({
      data_type: "video",
      type_key: "video-track",
      tool_bindings: {
        bbox: {
          enabled: true,
          classes: [{ name: "car", color: "#0ea5e9", order: 0 }],
          attribute_schema: { fields: [] },
        },
        region: {
          enabled: true,
          classes: [{ name: "person", color: "#22c55e", order: 0 }],
          attribute_schema: { fields: [] },
        },
        rotated_bbox: {
          enabled: true,
          classes: [{ name: "ship", color: "#f97316", order: 0 }],
          attribute_schema: { fields: [] },
        },
      },
    });

    expect(Object.keys(bindings).sort()).toEqual([
      "bbox",
      "keypoint",
      "polyline",
      "region",
      "rotated_bbox",
    ]);
    expect(bindings.bbox?.enabled).toBe(true);
    expect(bindings.bbox?.classRows).toEqual([{ name: "car", color: "#0ea5e9" }]);
    expect(bindings.region?.enabled).toBe(true);
    expect(bindings.region?.classRows).toEqual([{ name: "person", color: "#22c55e" }]);
    expect(bindings.rotated_bbox?.enabled).toBe(true);
    expect(bindings.rotated_bbox?.classRows).toEqual([{ name: "ship", color: "#f97316" }]);
  });

  it("legacy 视频类别落入 bbox; 视频几何单位 (bbox/polyline/region) 均种子化, 非几何 image 单位不加", () => {
    const bindings = buildUnitBindings({
      data_type: "video",
      type_key: "video-track",
      classes: ["vehicle"],
      classes_config: {
        vehicle: { color: "#0ea5e9", order: 0, alias: "vehicle" },
      },
      attribute_schema: {
        fields: [{ key: "occluded", type: "boolean", label: "Occluded" }],
      },
      tool_bindings: {},
    });

    expect(Object.keys(bindings).sort()).toEqual([
      "bbox",
      "keypoint",
      "polyline",
      "region",
      "rotated_bbox",
    ]);
    expect(bindings.polyline?.enabled).toBe(false);
    expect(bindings.region?.enabled).toBe(false);
    expect(bindings.keypoint?.enabled).toBe(false);
    expect(bindings.rotated_bbox?.enabled).toBe(false);
    expect(bindings.bbox?.enabled).toBe(true);
    expect(bindings.bbox?.classRows).toEqual([
      { name: "vehicle", color: "#0ea5e9", alias: "vehicle" },
    ]);
    expect(bindings.bbox?.attributeFields).toEqual([
      { key: "occluded", type: "boolean", label: "Occluded" },
    ]);
  });
});

describe("unitBindingsToPayload", () => {
  it("禁用但有配置的单位仍序列化 (enabled:false, 保留 classes/属性)", () => {
    const out = unitBindingsToPayload({
      bbox: {
        enabled: true,
        classRows: [{ name: "car", color: "#0ea5e9" }],
        attributeFields: [],
      },
      region: {
        enabled: false,
        classRows: [{ name: "road", color: "#22c55e" }],
        attributeFields: [{ key: "occluded", type: "boolean", label: "遮挡" }],
      },
    });

    expect(out.bbox?.enabled).toBe(true);
    expect(out.region?.enabled).toBe(false);
    expect(out.region?.classes).toEqual([{ name: "road", color: "#22c55e", order: 0 }]);
    expect(out.region?.attribute_schema).toEqual({
      fields: [{ key: "occluded", type: "boolean", label: "遮挡" }],
    });
  });

  it("纯空且禁用的单位不落库, 保持 tool_bindings 精简", () => {
    const out = unitBindingsToPayload({
      bbox: {
        enabled: true,
        classRows: [{ name: "car", color: "#0ea5e9" }],
        attributeFields: [],
      },
      region: { enabled: false, classRows: [], attributeFields: [] },
    });

    expect(out.bbox).toBeDefined();
    expect(out.region).toBeUndefined();
  });

  it("链接的类序列化时省略自身 color/alias, 只带 alias_to", () => {
    const ref = { tool_unit_id: "bbox", class_name: "person" };
    const out = unitBindingsToPayload({
      bbox: {
        enabled: true,
        classRows: [{ name: "person", color: "#ff0000", alias: "person" }],
        attributeFields: [],
      },
      region: {
        enabled: true,
        classRows: [{ name: "pedestrian", color: "#999999", aliasTo: ref }],
        attributeFields: [],
      },
    });
    expect(out.region?.classes).toEqual([{ name: "pedestrian", order: 0, alias_to: ref }]);
    expect(out.bbox?.classes).toEqual([
      { name: "person", color: "#ff0000", order: 0, alias: "person" },
    ]);
  });
  it("视频几何单位 video_modes 往返 (每单位独立 box/track)", () => {
    const out = unitBindingsToPayload({
      bbox: {
        enabled: true,
        classRows: [{ name: "car", color: "#0ea5e9" }],
        attributeFields: [],
        videoModes: { box: true, track: false },
      },
      region: {
        enabled: true,
        classRows: [{ name: "car", color: "#0ea5e9" }],
        attributeFields: [],
        videoModes: { box: false, track: true },
      },
    });
    expect(out.bbox?.video_modes).toEqual({ box: true, track: false });
    expect(out.region?.video_modes).toEqual({ box: false, track: true });
  });
});

describe("buildUnitBindings · video_modes", () => {
  it("从 tool_bindings 读出各单位 video_modes (缺省补 true)", () => {
    const bindings = buildUnitBindings({
      data_type: "video",
      type_key: "video-track",
      tool_bindings: {
        bbox: {
          enabled: true,
          classes: [{ name: "car", color: "#0ea5e9", order: 0 }],
          video_modes: { box: true },
        },
      },
    });
    expect(bindings.bbox?.videoModes).toEqual({ box: true, track: true });
  });
});

describe("buildUnitBindings · 退役 ai_interactive 折叠", () => {
  it("把残留 ai_interactive 的 classes 折叠进 region/bbox, 同名保留目标单位的", () => {
    const bindings = buildUnitBindings({
      data_type: "image",
      type_key: "image-det",
      tool_bindings: {
        bbox: {
          enabled: true,
          classes: [{ name: "car", color: "#0ea5e9", order: 0 }],
          attribute_schema: { fields: [] },
        },
        region: {
          enabled: true,
          classes: [{ name: "car", color: "#22c55e", order: 0 }],
          attribute_schema: { fields: [] },
        },
        ai_interactive: {
          enabled: true,
          classes: [
            { name: "car", color: "#a855f7", order: 0 },
            { name: "prompt", color: "#a855f7", order: 1 },
          ],
          attribute_schema: { fields: [] },
        },
      } as never,
    });

    expect(bindings.bbox?.classRows).toEqual([
      { name: "car", color: "#0ea5e9" },
      { name: "prompt", color: "#a855f7" },
    ]);
    expect(bindings.region?.classRows).toEqual([
      { name: "car", color: "#22c55e" },
      { name: "prompt", color: "#a855f7" },
    ]);
    expect(Object.keys(bindings)).not.toContain("ai_interactive");
  });
});

describe("buildUnitBindings · alias_to", () => {
  it("从 tool_bindings 读出 aliasTo", () => {
    const ref = { tool_unit_id: "bbox", class_name: "person" };
    const bindings = buildUnitBindings({
      data_type: "image",
      type_key: "image-det",
      tool_bindings: {
        bbox: {
          enabled: true,
          classes: [{ name: "person", color: "#ff0000", order: 0 }],
          attribute_schema: { fields: [] },
        },
        region: {
          enabled: true,
          classes: [{ name: "pedestrian", order: 0, alias_to: ref }],
          attribute_schema: { fields: [] },
        },
      },
    });
    expect(bindings.region?.classRows[0].aliasTo).toEqual(ref);
  });
});
