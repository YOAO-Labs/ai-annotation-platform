import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DataManagerSummary } from "@/api/taskViews";
import { DataManagerSummaryStrip } from "./DataManagerOverview";

const summary: DataManagerSummary = {
  scope: { visible_task_total: 10, matched_task_total: 8 },
  task_status: { pending: 8 },
  annotations: {
    total: 5,
    single_frame: 5,
    tracked: 0,
    distinct_tracks: 0,
    imported: 0,
    by_source: { manual: 5 },
    by_class: { car: 5 },
    by_tool_unit: { bbox: 5 },
    by_type: { bbox: 5 },
  },
  ai_review: {
    prediction_shapes: 3,
    low_confidence_prediction_shapes: 1,
    tracker_jobs: 0,
    confidence_threshold: 0.5,
    by_model_version: {},
    confidence_buckets: {},
  },
  unresolved_feedback: 2,
  attributes: [],
  kind_metrics: {},
};

describe("DataManagerSummaryStrip", () => {
  it("clicking a drillable KPI emits onDrill with its filter rule", () => {
    const onDrill = vi.fn();
    render(<DataManagerSummaryStrip summary={summary} isLoading={false} onDrill={onDrill} />);

    fireEvent.click(screen.getByRole("button", { name: /未解决问题/ }));
    expect(onDrill).toHaveBeenCalledWith({
      field: "feedback.unresolved_count",
      op: "gt",
      value: "0",
    });
  });

  it("non-drillable KPI is not a button", () => {
    render(<DataManagerSummaryStrip summary={summary} isLoading={false} onDrill={vi.fn()} />);

    expect(screen.queryByRole("button", { name: /当前匹配/ })).toBeNull();
  });
});
