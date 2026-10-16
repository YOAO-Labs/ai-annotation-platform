import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { TrainingDatasetDetectionCharts } from "./TrainingDatasetDetectionCharts";
import type { TrainingDatasetDetectionStatistics } from "@/api/generated/types.gen";

afterEach(cleanup);

it("distinguishes member-image and instance populations and retains unknown evidence beside the charts", () => {
  const axis = [
    { key: "0", label: "0–0.5", lower: 0, upper: 0.5 },
    { key: "1", label: "0.5–1", lower: 0.5, upper: 1 },
  ];
  const heatmap = {
    x_bins: axis,
    y_bins: axis,
    counts: [
      [1, 0],
      [0, 2],
    ],
    population: 3,
    unknown_members: 2,
  };
  const data: TrainingDatasetDetectionStatistics = {
    task: "detect",
    schema_version: 1,
    image_dimensions: { ...heatmap, unit: "member_images" },
    box_centers: { ...heatmap, unit: "instances" },
    box_dimensions: { ...heatmap, unit: "instances" },
    objects_per_member: {
      analyzed_members: 3,
      unknown_label_members: 2,
      bins: [
        { key: "0", label: "0", count: 1 },
        { key: "1", label: "1", count: 2 },
      ],
    },
  };
  render(<TrainingDatasetDetectionCharts data={data} />);
  expect(screen.getByText("已统计 3 张图片项 · 2 张成员证据未知")).toBeVisible();
  expect(screen.getAllByText("已统计 3 个实例 · 2 张成员证据未知")).toHaveLength(2);
  expect(screen.getByText(/另有 2 张未知，未放入零目标分组/)).toBeVisible();
  const table = screen.getByRole("table", { name: /标注中心位置计数表/, hidden: true });
  expect(
    within(table).getByRole("columnheader", { name: "中心 Y / 中心 X", hidden: true }),
  ).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "图片宽高分布" })).toBeVisible();
});
