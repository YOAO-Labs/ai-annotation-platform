import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AsyncJob, StorageJobProgressObservation } from "@/api/asyncJobs";
import { StorageJobProgress } from "./StorageJobProgress";
const observation: StorageJobProgressObservation = {
  schema: 1,
  execution_id: "11111111-1111-1111-1111-111111111111",
  stage: "scanning",
  unit: "directories",
  completed: 4,
  sequence: 1,
  observed_at: "2026-10-26T00:30:00Z",
  idle_seconds: 900,
  attention_after_seconds: 900,
};
const job: AsyncJob = {
  id: "job",
  kind: "dataset_scan",
  project_id: null,
  user_id: "owner",
  project_display_id: null,
  project_name: null,
  status: "running",
  progress_pct: 50,
  payload: {},
  result: {},
  error_message: null,
  celery_task_id: "task",
  created_at: "2026-10-26T00:00:00Z",
  started_at: "2026-10-26T00:10:00Z",
  completed_at: null,
  updated_at: "2026-10-26T00:30:00Z",
  last_progress_at: "2026-10-26T00:15:00Z",
  progress_observation: observation,
};
afterEach(() => vi.restoreAllMocks());
it("shows meaningful persisted units, real timestamp, server elapsed time and a bounded running heads-up", () => {
  render(<StorageJobProgress job={job} />);
  expect(screen.getByText("扫描目录 · 已提交 4 个目录")).toBeInTheDocument();
  expect(screen.getByText("已执行 20 分钟")).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("已 15 分钟 没有新的已提交进展");
  expect(screen.getByRole("status")).toHaveTextContent("不表示任务失败、死锁或已经停止");
  expect(screen.getByText(/最后记录推进/)).toHaveClass("text-muted-foreground");
  expect(screen.getByText(/处置角色：数据集管理员/)).toBeInTheDocument();
});
it("updates actual progress independently of an unchanged percentage and stops warning", () => {
  const view = render(<StorageJobProgress job={job} />);
  view.rerender(
    <StorageJobProgress
      job={{
        ...job,
        last_progress_at: observation.observed_at,
        progress_observation: { ...observation, completed: 8, sequence: 2, idle_seconds: 0 },
      }}
    />,
  );
  expect(screen.getByText("扫描目录 · 已提交 8 个目录")).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
it("never advances idle time from the browser clock or a recent heartbeat", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2030-01-01T00:00:00Z"));
  render(
    <StorageJobProgress
      job={{
        ...job,
        updated_at: "2030-01-01T00:00:00Z",
        progress_observation: {
          ...observation,
          observed_at: "2026-10-26T00:29:59Z",
          idle_seconds: 899,
        },
      }}
    />,
  );
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.getByText("已执行 19 分钟")).toBeInTheDocument();
});
it("pending jobs remain waiting even with an attached historical observation", () => {
  render(<StorageJobProgress job={{ ...job, status: "pending", started_at: null }} />);
  expect(screen.getByText(/等待执行；尚不能确认本轮已提交进展/)).toBeInTheDocument();
  expect(screen.getByText("已等待 30 分钟")).toBeInTheDocument();
  expect(screen.queryByText(/已提交 4 个目录/)).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
it.each(["completed", "failed", "cancelled"] as const)(
  "%s retains the last observation without a running warning",
  (status) => {
    render(<StorageJobProgress job={{ ...job, status, completed_at: "2026-10-26T00:25:00Z" }} />);
    expect(screen.getByText("扫描目录 · 已提交 4 个目录")).toBeInTheDocument();
    expect(screen.getByText("本次执行 15 分钟")).toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  },
);
it.each([
  ["historical omitted fields", { last_progress_at: undefined, progress_observation: undefined }],
  ["null observation", { last_progress_at: null, progress_observation: null }],
  [
    "future progress time",
    {
      last_progress_at: "2026-10-26T00:31:00Z",
      progress_observation: { ...observation, idle_seconds: null },
    },
  ],
  ["invalid timestamp", { last_progress_at: "not-a-time" }],
  ["unknown schema", { progress_observation: { ...observation, schema: 2 } }],
  [
    "invalid execution identity",
    { progress_observation: { ...observation, execution_id: "unsafe/name" } },
  ],
  ["wrong kind/stage pair", { progress_observation: { ...observation, stage: "copying" } }],
  ["wrong stage/unit pair", { progress_observation: { ...observation, unit: "entries" } }],
  ["negative count", { progress_observation: { ...observation, completed: -1 } }],
  ["invalid sequence", { progress_observation: { ...observation, sequence: 0 } }],
  ["nonfinite count", { progress_observation: { ...observation, completed: Infinity } }],
  ["inconsistent idle", { progress_observation: { ...observation, idle_seconds: 1200 } }],
  ["invalid threshold", { progress_observation: { ...observation, attention_after_seconds: 1 } }],
] as const)("%s is explicitly unknown and cannot warn", (_label, patch) => {
  render(<StorageJobProgress job={{ ...job, ...patch } as AsyncJob} />);
  expect(screen.getByText(/尚无可核对的已提交进展/)).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByText(/已提交 4 个目录/)).not.toBeInTheDocument();
});
it("unobserved prepare never borrows its percentage or updated_at as progress", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse(observation.observed_at));
  render(
    <StorageJobProgress
      job={{
        ...job,
        kind: "storage_migration_copy",
        payload: { operation: "prepare" },
        result: { stage: "prepare" },
        last_progress_at: null,
        progress_observation: null,
      }}
    />,
  );
  expect(screen.getByText(/可能尚未产生首条记录、正在准备/)).toBeInTheDocument();
  expect(screen.getByText(/已执行约 20 分钟（本机估计）/)).toBeInTheDocument();
  expect(screen.getByText(/处置角色：超级管理员/)).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
it("future or reversed lifecycle clocks leave elapsed duration unknown", () => {
  vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-10-25T00:00:00Z"));
  render(
    <StorageJobProgress
      job={{
        ...job,
        status: "pending",
        started_at: null,
        progress_observation: null,
        last_progress_at: null,
      }}
    />,
  );
  expect(screen.getByText("已等待时长未知")).toBeInTheDocument();
});
it("unrelated jobs keep their existing detail presentation", () => {
  render(<StorageJobProgress job={{ ...job, kind: "training_run" }} />);
  expect(screen.queryByRole("region", { name: "已提交进展" })).not.toBeInTheDocument();
});
it.each([
  ["dataset_registration", "registering", "entries", "登记样本 · 已提交 4 条登记项"],
  ["storage_usage", "enumerating", "pages", "读取清单 · 已提交 4 页"],
  ["storage_migration_inventory", "publishing", "records", "发布结果 · 已提交 4 条清单记录"],
  ["storage_migration_copy", "verifying", "objects", "核验对象 · 已提交 4 个对象"],
] as const)("%s displays its actual accepted unit", (kind, stage, unit, expected) => {
  render(
    <StorageJobProgress
      job={{ ...job, kind, progress_observation: { ...observation, stage, unit } }}
    />,
  );
  expect(screen.getByText(expected)).toBeInTheDocument();
});

it("prototype-shaped stages remain unknown without crashing the detail", () => {
  render(
    <StorageJobProgress
      job={
        {
          ...job,
          progress_observation: { ...observation, stage: "__proto__" },
        } as unknown as AsyncJob
      }
    />,
  );
  expect(screen.getByText(/尚无可核对的已提交进展/)).toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
it("prototype-shaped job kinds never acquire storage observation UI", () => {
  render(<StorageJobProgress job={{ ...job, kind: "__proto__" }} />);
  expect(screen.queryByRole("region", { name: "已提交进展" })).not.toBeInTheDocument();
});
