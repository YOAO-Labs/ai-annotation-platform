/**
 * 固化「从 backend 导入/推荐的属性字段 key == backend 自报的语义名」。
 *
 * 落点校验 (useCapabilityValidation) 对 language/orientation 按 key 具名匹配，所以导入/推荐
 * 路径必须原样保留 backend 自报的 key（text/language/orientation），否则手建/导入的字段会
 * 漏判承接位。本测试钉住 itemToField 的 key 透传，防止未来重构悄悄破坏对齐。
 */
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
