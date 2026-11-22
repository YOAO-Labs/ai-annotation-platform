import { describe, it, expect } from "vitest";
import { itemToField } from "./PrefillFromBackendDialog";

describe("itemToField · 导入/推荐字段 key 对齐", () => {
  it("原样保留 backend 自报的 key 作语义名", () => {
    expect(
      itemToField({
        key: "language",
        label: "语言",
        type: "select",
        options: [{ value: "universal", label: "通用(中英)" }],
      }).key,
    ).toBe("language");
    expect(itemToField({ key: "orientation", label: "方向", type: "select" }).key).toBe(
      "orientation",
    );
    expect(itemToField({ key: "text", label: "识别文本", type: "text" }).key).toBe("text");
  });

  it("透传 type 与 options，非法 type 回落 text", () => {
    const f = itemToField({
      key: "color",
      label: "颜色",
      type: "select",
      options: [{ value: "red", label: "红" }],
    });
    expect(f.type).toBe("select");
    expect(f.options).toEqual([{ value: "red", label: "红" }]);
    expect(itemToField({ key: "x", label: "X", type: "weird" }).type).toBe("text");
  });
});
