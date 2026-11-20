// MetaFooter 单测:ID 短码 / z-order / 额外行;来源映射中文。

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MetaFooter } from "./MetaFooter";

describe("MetaFooter", () => {
  it("渲染 ID 短码 / 层级 / 额外行", () => {
    const { getByText } = render(
      <MetaFooter
        id="abcd1234-5678-90ef-aaaa-bbbbbbbbbbbb"
        zOrder={3}
        extra={[{ label: "模型", value: "grounding-sam2" }]}
      />,
    );
    expect(getByText("abcd1234")).not.toBeNull(); // 短码 = 前 8 位
    expect(getByText("3")).not.toBeNull();
    expect(getByText("grounding-sam2")).not.toBeNull();
  });

  it("来源映射为中文", () => {
    const { getByText } = render(<MetaFooter id="x" source="prediction_based" />);
    expect(getByText("AI 采纳")).not.toBeNull();
  });
});
