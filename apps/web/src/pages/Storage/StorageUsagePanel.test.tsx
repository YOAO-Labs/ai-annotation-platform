import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import type { BucketsResponse } from "@/api/storage";
import { StorageUsagePanel } from "./StorageUsagePanel";

const mocks = vi.hoisted(() => ({
  query: vi.fn(),
  start: vi.fn(),
  refetch: vi.fn(),
  focus: vi.fn(),
}));
vi.mock("@/hooks/useStorage", () => ({
  useStorageBuckets: () => mocks.query(),
  useStartStorageInventory: () => ({ mutateAsync: mocks.start, isPending: false }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({
    jobId,
    onClose,
    returnFocusRef,
  }: {
    jobId: string;
    onClose: () => void;
    returnFocusRef?: { current: HTMLElement | null };
  }) => (
    <div role="dialog">
      任务 {jobId}
      <button
        onClick={() => {
          onClose();
          mocks.focus(returnFocusRef?.current?.tagName);
        }}
      >
        关闭任务
      </button>
    </div>
  ),
}));
function data(overrides: Partial<BucketsResponse> = {}): BucketsResponse {
  return {
    items: [
      {
        name: "platform-bucket",
        role: "datasets",
        roles: ["datasets", "training"],
        status: "unknown",
        object_count: null,
        total_size_bytes: null,
        error: null,
        measured_at: null,
        measurement_started_at: null,
        attempt_status: "never",
        attempted_at: null,
        scope: "current_objects",
        source: "s3_list_objects_v2",
        coverage: "none",
        is_estimate: true,
      },
    ],
    total_size_bytes: null,
    total_object_count: null,
    measured_bucket_count: 0,
    bucket_count: 1,
    physical_total_bytes: null,
    physical_available_bytes: null,
    quota_bytes: null,
    inventory_job_id: null,
    inventory_status: null,
    location_id: "location",
    ...overrides,
  };
}
beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    token: "token",
    sessionId: "usage",
    user: { id: "admin", role: "super_admin" } as any,
  });
  mocks.query.mockReturnValue({
    data: data(),
    isPending: false,
    error: null,
    refetch: mocks.refetch,
  });
  mocks.start.mockResolvedValue({ job_id: "job-1", reused: false });
});
it("keeps never measured distinct from zero and does not start inventory on mount", () => {
  render(<StorageUsagePanel />);
  expect(screen.getByText("物理容量、可用空间与配额：未提供")).toBeInTheDocument();
  expect(screen.getAllByText("未统计").length).toBeGreaterThan(0);
  expect(screen.queryByText("0 B")).not.toBeInTheDocument();
  expect(mocks.start).not.toHaveBeenCalled();
  expect(document.querySelector("details")).not.toHaveAttribute("open");
});
it("displays successful zero without treating it as missing", () => {
  const value = data({ total_size_bytes: 0, total_object_count: 0, measured_bucket_count: 1 });
  value.items[0] = {
    ...value.items[0],
    status: "ok",
    object_count: 0,
    total_size_bytes: 0,
    measured_at: "2026-10-23T00:00:00Z",
    measurement_started_at: "2026-10-22T23:59:00Z",
    attempt_status: "completed",
    coverage: "complete",
  };
  mocks.query.mockReturnValue({ data: value });
  render(<StorageUsagePanel />);
  expect(screen.getAllByText("0 B")).toHaveLength(2);
  expect(screen.queryByText("未统计")).not.toBeInTheDocument();
  expect(screen.getByText("数据集原件、训练资产")).toBeInTheDocument();
});
it("keeps deployment-scope bytes distinct from uncovered current binding targets", () => {
  mocks.query.mockReturnValue({
    data: data({
      total_size_bytes: 1024,
      measured_bucket_count: 1,
      uncovered_binding_purposes: ["avatars"],
    }),
  });
  render(<StorageUsagePanel />);
  expect(screen.getByText(/部署范围对象字节小计/)).toHaveTextContent("1 KiB");
  expect(screen.getByText(/当前写入目标未覆盖/)).toHaveTextContent("头像");
  expect(screen.getByText(/当前写入目标未覆盖/)).toHaveTextContent("旧部署桶中保留的对象继续统计");
  expect(screen.getByText(/桶标签不代表当前写入绑定/)).toBeInTheDocument();
  expect(mocks.start).not.toHaveBeenCalled();
});
it("retains old successful values, failure and measurement time", () => {
  const value = data({
    inventory_job_id: "failed-job",
    inventory_status: "failed",
    total_size_bytes: 1024,
    measured_bucket_count: 1,
  });
  value.items[0] = {
    ...value.items[0],
    total_size_bytes: 1024,
    object_count: 2,
    status: "stale",
    measured_at: "2026-10-22T12:00:00Z",
    measurement_started_at: "2026-10-22T11:58:00Z",
    error: "object_listing_failed",
    attempt_status: "failed",
  };
  mocks.query.mockReturnValue({ data: value, error: { status: 503 } });
  render(<StorageUsagePanel />);
  expect(screen.getAllByText("1 KiB")).toHaveLength(2);
  expect(screen.getByText("最近盘点失败")).toBeInTheDocument();
  expect(screen.getByText("对象列举失败")).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("保留上次快照");
  fireEvent.click(screen.getByRole("button", { name: "查看盘点任务" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("failed-job");
});
it("starts only explicitly and opens the returned existing job", async () => {
  mocks.start.mockResolvedValue({ job_id: "existing", reused: true });
  render(<StorageUsagePanel />);
  fireEvent.click(screen.getByRole("button", { name: "重新盘点" }));
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("existing"));
  expect(mocks.start).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "关闭任务" }));
  expect(mocks.refetch).toHaveBeenCalledTimes(1);
});
it("shows active work outside the collapsed table and blocks duplicate clicks", () => {
  mocks.query.mockReturnValue({
    data: data({ inventory_job_id: "active", inventory_status: "running" }),
  });
  render(<StorageUsagePanel />);
  expect(screen.getByRole("button", { name: "盘点中…" })).toBeDisabled();
  expect(screen.getByText("后台分页盘点中")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "查看盘点任务" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("active");
});
it("hides retained data after denied refresh", () => {
  mocks.query.mockReturnValue({ data: data({ total_size_bytes: 1024 }), error: { status: 403 } });
  render(<StorageUsagePanel />);
  expect(screen.queryByText("1 KiB")).not.toBeInTheDocument();
  expect(screen.queryByText("platform-bucket")).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("无权");
});
it("retires a late start result on logout or account switch", async () => {
  let done!: (value: unknown) => void;
  mocks.start.mockReturnValue(
    new Promise((resolve) => {
      done = resolve;
    }),
  );
  render(<StorageUsagePanel />);
  fireEvent.click(screen.getByRole("button", { name: "重新盘点" }));
  act(() => useAuthStore.setState({ sessionId: "next" }));
  await act(async () => {
    done({ job_id: "retired" });
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("does not query or render global usage for a project administrator", () => {
  useAuthStore.setState({ user: { id: "manager", role: "project_admin" } as any });
  render(<StorageUsagePanel />);
  expect(mocks.query).not.toHaveBeenCalled();
  expect(screen.queryByRole("region", { name: "平台对象占用" })).not.toBeInTheDocument();
});

it("hides cached global usage immediately when starting reports lost access", async () => {
  mocks.start.mockRejectedValue({ status: 403 });
  render(<StorageUsagePanel />);
  fireEvent.click(screen.getByRole("button", { name: "重新盘点" }));
  await waitFor(() => expect(screen.queryByText("platform-bucket")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "重新盘点" })).toBeDisabled();
});

it("unlocks after a new successful snapshot read restores access", async () => {
  mocks.start.mockRejectedValue({ status: 403 });
  const view = render(<StorageUsagePanel />);
  fireEvent.click(screen.getByRole("button", { name: "重新盘点" }));
  await waitFor(() => expect(screen.queryByText("platform-bucket")).not.toBeInTheDocument());
  mocks.query.mockReturnValue({ data: data(), dataUpdatedAt: Date.now() + 100, error: null });
  view.rerender(<StorageUsagePanel />);
  await waitFor(() => expect(screen.getByRole("button", { name: "重新盘点" })).toBeEnabled());
  expect(screen.getByText("platform-bucket")).toBeInTheDocument();
  expect(screen.queryByText("无法启动盘点，请检查权限与存储配置后重试。")).not.toBeInTheDocument();
});

it("returns focus to the summary when the active task trigger disappears on completion", () => {
  mocks.query.mockReturnValue({
    data: data({ inventory_job_id: "active", inventory_status: "running" }),
    refetch: mocks.refetch,
  });
  const view = render(<StorageUsagePanel />);
  fireEvent.click(screen.getByRole("button", { name: "查看盘点任务" }));
  mocks.query.mockReturnValue({
    data: data({ inventory_job_id: "active", inventory_status: "completed" }),
    refetch: mocks.refetch,
  });
  view.rerender(<StorageUsagePanel />);
  expect(screen.queryByRole("button", { name: "查看盘点任务" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭任务" }));
  expect(mocks.focus).toHaveBeenCalledWith("SUMMARY");
});
