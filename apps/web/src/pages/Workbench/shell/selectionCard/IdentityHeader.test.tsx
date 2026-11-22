import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import type { AnnotationResponse } from "@/types";
import { IdentityHeader, annotationSourceKind, confidenceTone } from "./IdentityHeader";

describe("confidenceTone", () => {
  it("阈值:≥0.8 高 / 0.5–0.8 中 / <0.5 低", () => {
    expect(confidenceTone(0.95)).toBe("high");
    expect(confidenceTone(0.8)).toBe("high");
    expect(confidenceTone(0.79)).toBe("mid");
    expect(confidenceTone(0.5)).toBe("mid");
    expect(confidenceTone(0.49)).toBe("low");
  });
});

describe("annotationSourceKind", () => {
  const base = { id: "a", source: "manual" } as AnnotationResponse;
  it("parent_prediction_id 命中 → 采纳", () => {
    expect(annotationSourceKind({ ...base, parent_prediction_id: "p1" })).toBe("accepted");
  });
  it("source 含 import → 导入", () => {
    expect(
      annotationSourceKind({ ...base, source: "external_import", parent_prediction_id: null }),
    ).toBe("import");
  });
  it("默认 → 手动", () => {
    expect(annotationSourceKind({ ...base, parent_prediction_id: null })).toBe("manual");
  });
});

describe("IdentityHeader", () => {
  it("手动框:类名 + 手动徽章,无置信度 pill", () => {
    const { getByText, container } = render(<IdentityHeader className="car" source="manual" />);
    expect(getByText("car")).not.toBeNull();
    expect(getByText("手动")).not.toBeNull();
    expect(container.textContent).not.toMatch(/%/);
  });

  it("AI 框:AI 预测徽章 + 置信度 pill", () => {
    const { getByText } = render(
      <IdentityHeader className="person" source="ai" confidence={0.82} />,
    );
    expect(getByText("AI 预测")).not.toBeNull();
    expect(getByText("82%")).not.toBeNull();
  });

  it("trailing 渲染在右侧附加位", () => {
    const { getByText } = render(
      <IdentityHeader className="car" source="manual" trailing={<span>F12</span>} />,
    );
    expect(getByText("F12")).not.toBeNull();
  });
});
