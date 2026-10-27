import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { StorageUsageBreakdown as Breakdown } from "@/api/generated/types.gen";
import { StorageUsageBreakdown } from "./StorageUsageBreakdown";

export const breakdown: Breakdown = {
  categories: [
    { category: "managed_original", object_count: 2, size_bytes: 1024 },
    { category: "preview_cache", object_count: 3, size_bytes: 2048 },
    { category: "training_version", object_count: 4, size_bytes: 4096 },
    { category: "temporary", object_count: 5, size_bytes: 8192 },
    { category: "other_platform", object_count: 6, size_bytes: 16384 },
    { category: "unclassified", object_count: 0, size_bytes: 0 },
  ],
  sampled_size_bytes: 31744,
  sampled_object_count: 20,
  total_size_bytes: 31744,
  conflict_object_count: 0,
  measured_bucket_count: 9,
  bucket_count: 9,
  measured_from: "2026-10-25T00:00:00Z",
  measured_through: "2026-10-25T00:01:00Z",
  has_stale_data: false,
  coverage: "complete",
  scope: "configured_platform_buckets_current_objects",
  source: "s3_list_objects_v2_and_saved_ownership",
  deduplication_basis: "endpoint_bucket_key",
  includes_versions: false,
  is_estimate: true,
};

it("shows exclusive category observations and their sample scope", () => {
  render(<StorageUsageBreakdown value={breakdown} />);
  expect(screen.getByText("托管原件")).toBeInTheDocument();
  expect(screen.getByText("训练版本文件")).toBeInTheDocument();
  expect(screen.getByText("4 KiB")).toBeInTheDocument();
  expect(screen.getByText("分项已测 9 / 9 个桶")).toBeInTheDocument();
  fireEvent.click(screen.getByText("分类口径"));
  expect(screen.getByText(/训练版本文件包含准备中/)).toBeVisible();
  expect(screen.getByText(/未归类字节仍在对象总量中/)).toBeVisible();
});

it("keeps an old or unmeasured classification unknown", () => {
  const { rerender } = render(<StorageUsageBreakdown />);
  expect(screen.getByText(/分项尚未统计/)).toBeInTheDocument();
  expect(screen.queryByText("0 B")).not.toBeInTheDocument();
  rerender(<StorageUsageBreakdown value={{ ...breakdown, coverage: "none" }} />);
  expect(screen.queryByText("0 B")).not.toBeInTheDocument();
});

it("displays confirmed empty categories as zero", () => {
  render(
    <StorageUsageBreakdown
      value={{
        ...breakdown,
        categories: breakdown.categories.map((item) => ({
          ...item,
          object_count: 0,
          size_bytes: 0,
        })),
        sampled_object_count: 0,
        sampled_size_bytes: 0,
        total_size_bytes: 0,
      }}
    />,
  );
  expect(screen.getAllByText("0 B")).toHaveLength(6);
  expect(screen.queryByText(/分项尚未统计/)).not.toBeInTheDocument();
});

it("marks missing buckets and retains observed subtotals", () => {
  render(
    <StorageUsageBreakdown
      value={{
        ...breakdown,
        measured_bucket_count: 1,
        total_size_bytes: null,
        coverage: "partial",
        has_stale_data: true,
      }}
    />,
  );
  expect(screen.getByText(/分项已测 1 \/ 9 个桶/)).toHaveTextContent("含过期快照");
  expect(screen.getByText(/未测部分未知/)).toBeInTheDocument();
  expect(screen.getByText("4 KiB")).toBeInTheDocument();
});

it("explains unclassified bytes and cross-category conflicts", () => {
  render(
    <StorageUsageBreakdown
      value={{ ...breakdown, coverage: "partial", conflict_object_count: 2 }}
    />,
  );
  expect(screen.getByText(/部分对象缺少一致归属/)).toHaveTextContent("2 个跨分类归属冲突");
});
