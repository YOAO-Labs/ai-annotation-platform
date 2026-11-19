// MetricGrid 单测:渲染 label/value/hint;空数组不渲染。

import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { MetricGrid } from "./MetricGrid";

describe("MetricGrid", () => {
  it("渲染各指标的 label / value / hint", () => {
    const { getByText } = render(
      <MetricGrid
        metrics={[
          { label: "尺寸", value: "480×216 px" },
          { label: "旋转角", value: "37°", hint: "顺时针" },
        ]}
      />,
    );
    expect(getByText("尺寸")).not.toBeNull();
    expect(getByText("480×216 px")).not.toBeNull();
    expect(getByText("37°")).not.toBeNull();
    expect(getByText("顺时针")).not.toBeNull();
  });

  it("空数组不渲染任何 DOM", () => {
    const { container } = render(<MetricGrid metrics={[]} />);
    expect(container.firstChild).toBeNull();
  });
});
