import { expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { StorageLocationHealth, StorageSourceUsage } from "@/api/generated/types.gen";
import { LocationHealthSummary, LocationSourceScale } from "./StorageLocationListSummary";

const now = Date.parse("2026-10-25T10:00:00Z");
const health: StorageLocationHealth = {
  observed_node_count: 3,
  current_success_count: 1,
  current_failure_count: 1,
  stale_node_count: 1,
  measured_at: "2026-10-25T09:59:00Z",
  fresh_until: "2026-10-25T10:01:00Z",
  latest: null,
};

it("does not turn an unprovided health summary into a claim that no probe exists", () => {
  render(<LocationHealthSummary now={now} />);
  expect(screen.getByText("健康未知")).toBeInTheDocument();
  expect(screen.queryByText("未探测")).not.toBeInTheDocument();
});

it("shows mixed failures without treating a single passed node as global health", () => {
  render(<LocationHealthSummary value={health} now={now} />);
  expect(screen.getByText("部分探测失败")).toBeInTheDocument();
  expect(screen.getByText("3 个节点 · 有效通过 1／失败 1")).toBeInTheDocument();
});

it("expires previously fresh counts at the first saved expiry without a new probe", () => {
  const view = render(<LocationHealthSummary value={health} now={now} />);
  view.rerender(<LocationHealthSummary value={health} now={Date.parse(health.fresh_until!)} />);
  expect(screen.getByText("摘要已过期")).toBeInTheDocument();
  expect(screen.queryByText(/有效通过/)).not.toBeInTheDocument();
});

it("identifies an old-configuration observation without describing it as a current failure", () => {
  render(
    <LocationHealthSummary
      now={now}
      configRevision={3}
      value={{
        ...health,
        current_success_count: 0,
        current_failure_count: 0,
        fresh_until: null,
        latest: {
          node_id: "api",
          config_revision: 2,
          observed_at: "2026-10-25T09:59:00Z",
          expires_at: "2026-10-25T10:01:00Z",
          ok: false,
          code: "unavailable",
          capabilities: {},
        },
      }}
    />,
  );
  expect(screen.getByText("配置已变化")).toBeInTheDocument();
  expect(screen.queryByText("探测失败")).not.toBeInTheDocument();
});

it.each([
  [0, 0, 0, "未探测"],
  [2, 0, 0, "探测已过期"],
  [1, 0, 1, "探测失败"],
  [1, 1, 0, "探测通过"],
] as const)(
  "separates unknown, stale and explicit failure (%s/%s/%s)",
  (nodes, ok, fail, label) => {
    render(
      <LocationHealthSummary
        now={now}
        value={{
          ...health,
          observed_node_count: nodes,
          current_success_count: ok,
          current_failure_count: fail,
          fresh_until: ok || fail ? health.fresh_until : null,
        }}
      />,
    );
    expect(screen.getByText(label)).toBeInTheDocument();
  },
);

it("keeps a partial source total unknown and labels its confirmed part and actual observation", () => {
  const value = {
    coverage: "partial",
    total_size_bytes: null,
    known_size_bytes: 226,
    file_count: 2,
    reference_count: 5,
    observed_through: "2026-10-01T12:00:00Z",
    measured_at: "2026-10-25T12:00:00Z",
  } as StorageSourceUsage;
  render(<LocationSourceScale value={value} />);
  expect(screen.getByText("总量未知")).toBeInTheDocument();
  expect(screen.getByText("已确认 226 B · 2 个文件")).toBeInTheDocument();
  expect(screen.getByText(/索引观测/)).toHaveTextContent(
    new Date(value.observed_through!).toLocaleString("zh-CN"),
  );
  expect(screen.queryByText(new Date(value.measured_at).toLocaleString("zh-CN"))).toBeNull();
});

it("distinguishes an unprovided summary from a measured empty location", () => {
  const view = render(<LocationSourceScale />);
  expect(screen.getByText("待统计")).toBeInTheDocument();
  view.rerender(
    <LocationSourceScale
      value={
        {
          coverage: "complete",
          total_size_bytes: 0,
          known_size_bytes: 0,
          file_count: 0,
          reference_count: 0,
          observed_through: null,
        } as StorageSourceUsage
      }
    />,
  );
  expect(screen.getByText("0 B")).toBeInTheDocument();
  expect(screen.getByText("0 个文件 · 尚无采纳记录")).toBeInTheDocument();
});
