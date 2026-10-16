import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import userEvent from "@testing-library/user-event";
import { TrainingDatasetDetectionCharts } from "./TrainingDatasetDetectionCharts";
import type { TrainingDatasetDetectionStatistics } from "@/api/generated/types.gen";

afterEach(cleanup);

function statistics(): TrainingDatasetDetectionStatistics {
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
  return data;
}

it("distinguishes member-image and instance populations and retains unknown evidence beside the charts", () => {
  render(<TrainingDatasetDetectionCharts data={statistics()} />);
  expect(screen.getByText("已统计 3 张图片项 · 2 张成员证据未知")).toBeVisible();
  expect(screen.getAllByText("已统计 3 个实例 · 2 张成员证据未知")).toHaveLength(2);
  expect(screen.getByText(/另有 2 张未知，未放入零目标分组/)).toBeVisible();
  const table = screen.getByRole("table", { name: /标注中心位置计数表/, hidden: true });
  expect(
    within(table).getByRole("columnheader", { name: "中心 Y / 中心 X", hidden: true }),
  ).toBeInTheDocument();
  expect(screen.getByRole("grid", { name: "图片宽高分布" })).toBeVisible();
});

it("locks a touched heatmap cell until explicitly cleared", async () => {
  const user = userEvent.setup();
  render(<TrainingDatasetDetectionCharts data={statistics()} />);
  const grid = screen.getByRole("grid", { name: "标注中心位置" });
  const populated = within(grid).getByRole("gridcell", {
    name: "中心 X 0.5–1 · 中心 Y 0.5–1：2 个实例",
  });
  const empty = within(grid).getByRole("gridcell", {
    name: "中心 X 0.5–1 · 中心 Y 0–0.5：0 个实例",
  });
  await user.click(populated);
  await user.hover(empty);
  expect(populated).toHaveAttribute("aria-selected", "true");
  expect(empty).toHaveAttribute("aria-selected", "false");
  await user.click(screen.getByRole("button", { name: "取消固定" }));
  await user.hover(empty);
  expect(empty).toHaveAttribute("aria-selected", "true");
});

it("inspects heatmap coordinates with the keyboard and clears the inspection with Escape", async () => {
  const user = userEvent.setup();
  render(<TrainingDatasetDetectionCharts data={statistics()} />);
  const grid = screen.getByRole("grid", { name: "标注宽高分布" });
  await user.click(grid);
  await user.keyboard("{ArrowRight}{ArrowDown}");
  const cell = within(grid).getByRole("gridcell", { name: "框宽 0.5–1 · 框高 0.5–1：2 个实例" });
  expect(grid).toHaveAttribute("aria-activedescendant", cell.id);
  expect(cell).toHaveAttribute("aria-selected", "true");
  await user.keyboard("{Escape}");
  expect(grid).not.toHaveAttribute("aria-activedescendant");
  expect(cell).toHaveAttribute("aria-selected", "false");
});

it("enhances sparse color intensity without changing cell counts", async () => {
  const user = userEvent.setup();
  render(<TrainingDatasetDetectionCharts data={statistics()} />);
  const grid = screen.getByRole("grid", { name: "图片宽高分布" });
  const cell = within(grid).getByRole("gridcell", {
    name: "宽 / px 0–0.5 · 高 / px 0–0.5：1 张图片项",
  });
  const before = Number(cell.getAttribute("fill-opacity"));
  await user.click(within(grid.closest("figure")!).getByRole("button", { name: "增强稀疏区域" }));
  expect(Number(cell.getAttribute("fill-opacity"))).toBeGreaterThan(before);
  expect(cell).toHaveAttribute("aria-label", "宽 / px 0–0.5 · 高 / px 0–0.5：1 张图片项");
});

it("reports density percentages using known-label members only", async () => {
  const user = userEvent.setup();
  render(<TrainingDatasetDetectionCharts data={statistics()} />);
  await user.click(screen.getByRole("button", { name: "1 个目标：2 张图片" }));
  expect(screen.getByText(/占已知标注图片 66.7%/)).toBeVisible();
});
