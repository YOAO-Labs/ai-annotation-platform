import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TrainingDatasetClassChart, TrainingDatasetSplitChart } from "./TrainingDatasetCharts";

afterEach(cleanup);

describe("frozen data charts", () => {
  it("keeps class member and instance units distinct and drills into the chosen split", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(
      <TrainingDatasetClassChart
        partial
        rows={[
          {
            index: 4,
            name: "车辆",
            train_images: 2,
            val_images: 1,
            train_instances: 9,
            val_instances: 3,
          },
        ]}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByRole("button", { name: "按类别图查看 车辆 12 个实例" })).toBeVisible();
    expect(screen.getByText(/仅展示已知数量/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "图片" }));
    await user.selectOptions(screen.getByLabelText("类别图划分"), "val");
    await user.click(screen.getByRole("button", { name: "按类别图查看 车辆 1 张图片" }));
    expect(onSelect).toHaveBeenCalledWith(4, "val");
  });

  it("shows actual member proportions and handles an empty frozen package", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    const { rerender } = render(
      <TrainingDatasetSplitChart
        splits={{ train: { images: 7, instances: 18 }, val: { images: 3, instances: 5 } }}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText("7 张 · 70.0%")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "查看验证集 3 张图片" }));
    expect(onSelect).toHaveBeenCalledWith("val");
    rerender(
      <TrainingDatasetSplitChart
        splits={{ train: { images: 0 }, val: { images: 0 } }}
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText("没有入包图片。")).toBeVisible();
    expect(screen.queryByText(/NaN|Infinity/)).not.toBeInTheDocument();
  });
  it("keeps unknown partitions in the package denominator and never calls them an empty package", () => {
    const { rerender } = render(
      <TrainingDatasetSplitChart
        totalMembers={5}
        unknownMembers={2}
        splits={{ train: { images: 2 }, val: { images: 1 } }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByText("2 张 · 40.0%")).toBeVisible();
    expect(screen.getByText(/划分未知 2 张 · 40.0%/)).toBeVisible();
    rerender(
      <TrainingDatasetSplitChart
        totalMembers={5}
        unknownMembers={5}
        splits={{ train: { images: 0 }, val: { images: 0 } }}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByText("没有入包图片。")).not.toBeInTheDocument();
    expect(screen.getByText(/划分未知 5 张 · 100.0%/)).toBeVisible();
  });
});
