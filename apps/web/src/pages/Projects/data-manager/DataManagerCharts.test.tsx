import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  DataManagerEntityFacets,
  DataManagerFilterField,
  DataManagerSummary,
} from "@/api/taskViews";
import { DataManagerCharts } from "./DataManagerCharts";

const facets: DataManagerEntityFacets = {
  matched_total: 12,
  task_total: 4,
  by_class: { car: 8, person: 4 },
  by_source: { manual: 7, ai_tracker: 5 },
  by_tool_unit: { bbox: 12 },
  by_type: { video_track_bbox: 12 },
  by_quality: { duplicate_frame: 2, inconsistent_attributes: 1 },
};

const summary: DataManagerSummary = {
  scope: { visible_task_total: 3, matched_task_total: 2 },
  task_status: { review: 2 },
  annotations: {
    total: 2,
    single_frame: 2,
    tracked: 0,
    distinct_tracks: 0,
    imported: 0,
    by_source: { manual: 2 },
    by_class: { car: 2 },
    by_tool_unit: { bbox: 2 },
    by_type: { bbox: 2 },
  },
  ai_review: {
    prediction_shapes: 5,
    low_confidence_prediction_shapes: 3,
    tracker_jobs: 0,
    confidence_threshold: 0.5,
    by_model_version: { "detector-v2": 5 },
    confidence_buckets: { "025_049": 3, gte_075: 2 },
  },
  unresolved_feedback: 0,
  attributes: [],
  kind_metrics: {},
};

describe("DataManagerCharts", () => {
  beforeEach(() => {
    // Recharts measures the container when ResizeObserver exists; jsdom has no layout.
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 320, 160),
    );
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders track facets as accessible chart summaries", () => {
    render(<DataManagerCharts scope="tracks" facets={facets} />);

    expect(screen.getByRole("heading", { name: "轨迹来源" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "轨迹类别" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "质量异常" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /轨迹来源：人工 7，AI 追踪 5/ })).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /质量异常：同帧重复 2，属性不一致 1/ }),
    ).toBeInTheDocument();
  });

  it("renders pending model and candidate confidence distributions for tasks", () => {
    render(<DataManagerCharts scope="tasks" summary={summary} />);

    expect(screen.getByRole("img", { name: "待审模型版本：detector-v2 5" })).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: "待审置信度：25–49% 3，75–100% 2" }),
    ).toBeInTheDocument();
  });

  it("clicking a selectable bar emits onSelect(field, key)", () => {
    const onSelect = vi.fn();
    const { container } = render(
      <DataManagerCharts scope="tracks" facets={facets} onSelect={onSelect} />,
    );

    const bar = container.querySelector(".recharts-bar-rectangle");
    expect(bar).not.toBeNull();
    fireEvent.click(bar!);

    // 轨迹来源图（第一张，可点）→ 点最大的一根柱 manual(7)
    expect(onSelect).toHaveBeenCalledWith("annotation.source", "manual");
  });

  it("renders attribute value distribution with label mapping and drills into it", () => {
    const onSelect = vi.fn();
    const summaryWithAttr: DataManagerSummary = {
      ...summary,
      attributes: [
        {
          tool_unit_id: "bbox",
          key: "vehicle_type",
          label: "车型",
          eligible: 20,
          present: 15,
          missing: 5,
          values: { truck: 5, car: 10 },
        },
      ],
    };
    const fields: DataManagerFilterField[] = [
      {
        key: "annotation.attribute.bbox.vehicle_type",
        label: "车型",
        group: "属性 · bbox",
        value_type: "select",
        operators: ["eq", "in", "exists", "missing"],
        options: [
          { value: "truck", label: "卡车" },
          { value: "car", label: "小汽车" },
        ],
        expensive: true,
        tool_unit_id: "bbox",
        attribute_key: "vehicle_type",
      },
    ];
    const { container } = render(
      <DataManagerCharts
        scope="tasks"
        summary={summaryWithAttr}
        fields={fields}
        onSelect={onSelect}
      />,
    );

    // value→label 映射生效、按数量降序
    expect(screen.getByRole("img", { name: "车型：小汽车 10，卡车 5" })).toBeInTheDocument();

    // 点最大的一根柱（小汽车）→ 注入的是存储值 car，非显示名
    const attrChart = container.querySelector('[aria-label^="车型"]');
    const bar = attrChart?.querySelector(".recharts-bar-rectangle");
    expect(bar).not.toBeNull();
    fireEvent.click(bar!);
    expect(onSelect).toHaveBeenCalledWith("annotation.attribute.bbox.vehicle_type", "car");
  });
});
