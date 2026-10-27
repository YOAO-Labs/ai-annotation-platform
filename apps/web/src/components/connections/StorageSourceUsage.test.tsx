import { expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { StorageSourceUsage as Usage } from "@/api/generated/types.gen";
import { StorageSourceUsage } from "./StorageSourceUsage";

export const usage: Usage = {
  scope: "visible_current_accepted_source_heads",
  source: "accepted_source_index",
  unit: "bytes",
  coverage: "complete",
  is_estimate: false,
  deduplication_basis: "location_frozen_address_and_object_version",
  includes_versions: false,
  reference_count: 3,
  file_count: 2,
  duplicate_reference_count: 1,
  known_size_bytes: 2048,
  total_size_bytes: 2048,
  unknown_reference_count: 0,
  conflict_file_count: 0,
  incomparable_file_count: 0,
  inactive_reference_count: 0,
  observed_from: "2026-10-23T01:00:00Z",
  observed_through: "2026-10-24T02:00:00Z",
  measured_at: "2026-10-25T03:00:00Z",
};

it("shows deduplicated source bytes separately and explains its saved scope", () => {
  render(<StorageSourceUsage value={usage} />);
  expect(screen.getByText("2 KiB")).toBeInTheDocument();
  expect(screen.getByText(/去重后 2 个文件/)).toHaveTextContent("3 条采纳记录 · 合并 1 条重复引用");
  expect(screen.getByText(/索引观测/)).toHaveTextContent("计算时间");
  fireEvent.click(screen.getByText("统计口径"));
  expect(screen.getByText(/不能与平台对象字节相加/)).toBeVisible();
});

it("keeps a known subtotal distinct from an unknown total and displays conflicts", () => {
  render(
    <StorageSourceUsage
      value={{
        ...usage,
        coverage: "partial",
        total_size_bytes: null,
        unknown_reference_count: 4,
        conflict_file_count: 1,
        incomparable_file_count: 1,
        inactive_reference_count: 1,
      }}
    />,
  );
  expect(screen.getByText("总量未知")).toBeInTheDocument();
  expect(screen.getByText(/已确认部分 2 个文件/)).toHaveTextContent("2 KiB");
  expect(screen.getByText(/4 条记录缺少/)).toHaveTextContent("1 组同址内容冲突");
  expect(screen.getByText(/4 条记录缺少/)).toHaveTextContent("1 组挂载或节点不可比较");
  expect(screen.getByText(/含暂停或注销范围/)).toBeInTheDocument();
});

it("renders a confirmed empty accepted scope as zero", () => {
  render(
    <StorageSourceUsage
      value={{
        ...usage,
        reference_count: 0,
        file_count: 0,
        duplicate_reference_count: 0,
        known_size_bytes: 0,
        total_size_bytes: 0,
      }}
    />,
  );
  expect(screen.getByText("0 B")).toBeInTheDocument();
  expect(screen.getByText(/去重后 0 个文件/)).toBeInTheDocument();
});

it("does not turn an old response without usage into zero", () => {
  render(<StorageSourceUsage />);
  expect(screen.getByText("总量未知")).toBeInTheDocument();
  expect(screen.getByText("尚无可用统计。")).toBeInTheDocument();
  expect(screen.queryByText("0 B")).not.toBeInTheDocument();
});

it("does not render unsupported empty statistics as confirmed zero", () => {
  render(
    <StorageSourceUsage
      value={{
        ...usage,
        coverage: "none",
        total_size_bytes: null,
        reference_count: 0,
        file_count: 0,
        known_size_bytes: 0,
        duplicate_reference_count: 0,
      }}
    />,
  );
  expect(screen.getByText("此位置尚无可用的来源规模统计。")).toBeInTheDocument();
  expect(screen.queryByText(/已确认部分 0/)).not.toBeInTheDocument();
});

it("keeps missing observations unknown and explains retained object versions", () => {
  render(
    <StorageSourceUsage
      value={{
        ...usage,
        coverage: "none",
        total_size_bytes: null,
        file_count: 0,
        known_size_bytes: 0,
        includes_versions: true,
        observed_from: null,
        observed_through: null,
      }}
    />,
  );
  expect(screen.getByText("总量未知")).toBeInTheDocument();
  expect(screen.getByText(/索引观测/)).toHaveTextContent("索引观测：暂无记录");
  expect(screen.getByText(/索引观测/)).not.toHaveTextContent("暂无记录 至 暂无记录");
  fireEvent.click(screen.getByText("统计口径"));
  expect(screen.getByText(/不同 S3 对象版本分别计量/)).toBeVisible();
});
