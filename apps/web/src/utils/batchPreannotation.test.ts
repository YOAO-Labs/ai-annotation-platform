import { describe, expect, it } from "vitest";

import { batchPreannotationEligible } from "./batchPreannotation";

/** Issue #124 · 与后端 allows_bulk_preannotation 同源的准入规则。 */
describe("batchPreannotationEligible", () => {
  it("active 批次始终放行（含已分派人员）", () => {
    expect(batchPreannotationEligible({ status: "active" })).toBe(true);
    expect(
      batchPreannotationEligible({
        status: "active",
        annotator_id: "u1",
        reviewer_id: "u2",
      }),
    ).toBe(true);
  });

  it("未分派标注员与质检员的 draft 批次放行", () => {
    expect(batchPreannotationEligible({ status: "draft" })).toBe(true);
    expect(
      batchPreannotationEligible({
        status: "draft",
        annotator_id: null,
        reviewer_id: null,
      }),
    ).toBe(true);
  });

  it("已分派任一人员的 draft 批次拒绝", () => {
    expect(batchPreannotationEligible({ status: "draft", annotator_id: "u1" })).toBe(false);
    expect(batchPreannotationEligible({ status: "draft", reviewer_id: "u2" })).toBe(false);
    expect(
      batchPreannotationEligible({
        status: "draft",
        annotator_id: "u1",
        reviewer_id: "u2",
      }),
    ).toBe(false);
  });

  it("已进入人工流程的其它状态一律拒绝", () => {
    for (const status of [
      "pre_annotated",
      "annotating",
      "reviewing",
      "approved",
      "rejected",
      "archived",
    ]) {
      expect(batchPreannotationEligible({ status })).toBe(false);
    }
  });
});
