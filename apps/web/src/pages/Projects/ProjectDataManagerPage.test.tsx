import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import type { DataManagerSummary } from "@/api/taskViews";
import { DataManagerOverview } from "./data-manager/DataManagerOverview";

describe("DataManagerOverview", () => {
  it("shows the matched scope and schema-driven attribute aggregation", () => {
    const summary: DataManagerSummary = {
      scope: { visible_task_total: 12, matched_task_total: 3 },
      task_status: { pending: 2, review: 1 },
      annotations: {
        total: 8,
        single_frame: 6,
        tracked: 2,
        distinct_tracks: 1,
        imported: 0,
        by_source: { manual: 5, prediction_based: 3 },
        by_class: { car: 8 },
        by_tool_unit: { bbox: 8 },
        by_type: { bbox: 8 },
      },
      ai_review: {
        prediction_shapes: 2,
        low_confidence_prediction_shapes: 1,
        tracker_jobs: 1,
        confidence_threshold: 0.5,
        by_model_version: { "detector-v2": 2 },
        confidence_buckets: { "025_049": 1, gte_075: 1 },
      },
      unresolved_feedback: 1,
      attributes: [
        {
          tool_unit_id: "bbox",
          key: "color",
          label: "颜色",
          eligible: 8,
          present: 6,
          missing: 2,
          values: { red: 4, blue: 2 },
        },
      ],
      kind_metrics: { images_with_dimensions: 3 },
    };

    render(<DataManagerOverview summary={summary} isLoading={false} />);

    expect(screen.getByText("当前匹配")).toBeInTheDocument();
    expect(screen.getByText("可见 12")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "任务状态" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "标注来源" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "标注类别" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "待审模型版本" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "待审置信度" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /任务状态：待标注 2，待审核 1/ })).toBeInTheDocument();
    expect(screen.getByText("低置信 1")).toBeInTheDocument();
    expect(screen.getAllByText("颜色").length).toBeGreaterThan(0);
    expect(screen.getByText("6/8")).toBeInTheDocument();
  });
});
