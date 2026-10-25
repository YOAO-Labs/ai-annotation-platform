import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import {
  storageApi,
  type StorageUsageAccountingSummary,
  type StorageUsageReconciliationSummary,
  type UsageScheduleSummary,
} from "@/api/storage";
import { useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import { StorageUsageAccounting } from "./StorageUsageAccounting";

const mocks = vi.hoisted(() => ({ save: vi.fn(), denied: vi.fn() }));
vi.mock("@/hooks/useStorage", () => ({
  useUpdateStorageUsageSchedule: () => ({ mutateAsync: mocks.save, isPending: false }),
}));

const accounting: StorageUsageAccountingSummary = {
  scope: "current_objects",
  coverage: "partial",
  producers: ["prediction_inputs"],
  confirmed_added_bytes: 2048,
  confirmed_removed_bytes: 3072,
  net_change_bytes: -1024,
  pending_operation_count: 2,
  unknown_operation_count: 1,
  projected_total_bytes: null,
};
const schedule: UsageScheduleSummary = {
  enabled: false,
  interval_hours: 24,
  status: "off",
  next_due_at: null,
  last_started_at: null,
  pause_reason: null,
};
const reconciliation: StorageUsageReconciliationSummary = {
  scope: "current_objects",
  coverage: "partial",
  status: "partial",
  measured_at: "2026-10-25T00:00:00Z",
  bucket_count: 2,
  compared_object_count: 4,
  uncertain_object_count: 1,
  unexplained_object_count: 1,
  confirmed_change_bytes: -1024,
  known_unexplained_change_bytes: 512,
  unexplained_change_bytes: null,
};

function open() {
  fireEvent.click(screen.getByText("增量账目与周期对账"));
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthStore.setState({
    token: "token",
    sessionId: "accounting",
    user: { id: "admin", role: "super_admin" } as any,
  });
  mocks.save.mockResolvedValue(schedule);
});
afterEach(() => vi.restoreAllMocks());

it("starts collapsed with an explicit disabled schedule and a plain 24-hour value", () => {
  const view = render(<StorageUsageAccounting onAccessDenied={mocks.denied} />);
  expect(view.container.querySelector("details")).not.toHaveAttribute("open");
  open();
  expect(screen.getByRole("checkbox", { name: "周期盘点" })).not.toBeChecked();
  const interval = screen.getByRole("spinbutton", { name: "盘点间隔（小时）" });
  expect(interval).toHaveValue(24);
  const hint = document.getElementById(interval.getAttribute("aria-describedby")!);
  expect(hint).toHaveClass("text-xs", "text-muted-foreground");
  expect(hint).not.toHaveClass("text-foreground");
  expect(screen.getByText("计划已关闭。普通查看不会自动启用。")).toBeVisible();
  expect(mocks.save).not.toHaveBeenCalled();
});

it("keeps signed confirmed changes separate from unknown coverage and unknown reconciliation", () => {
  render(
    <StorageUsageAccounting
      accounting={accounting}
      reconciliation={reconciliation}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(screen.getByText("+2 KiB")).toBeVisible();
  expect(screen.getByText("−3 KiB")).toBeVisible();
  expect(screen.getAllByText("−1 KiB")).toHaveLength(2);
  expect(screen.getByText("部分覆盖")).toBeVisible();
  expect(screen.getByText(/待确认 2 次 · 结果未知 1 次/)).toBeVisible();
  expect(screen.getByText("未知", { exact: true })).toBeVisible();
  expect(screen.getByText(/可核对部分的未解释净变化/)).toHaveTextContent("+512 B，整体变化仍未知");
  expect(screen.getByText(/其他用途和外部变动尚未覆盖/)).toBeVisible();
  expect(screen.queryByText("0 B", { exact: true })).not.toBeInTheDocument();
});

it("distinguishes no prior baseline from a measured zero difference", () => {
  const view = render(
    <StorageUsageAccounting
      reconciliation={{ ...reconciliation, status: "no_baseline" }}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(screen.getByText("已建立首次基准，尚无前次完整盘点可比较。")).toBeVisible();
  expect(screen.queryByText("未解释变化", { exact: true })).not.toBeInTheDocument();
  view.rerender(
    <StorageUsageAccounting
      reconciliation={{ ...reconciliation, uncertain_object_count: 0, unexplained_change_bytes: 0 }}
      onAccessDenied={mocks.denied}
    />,
  );
  expect(screen.getByText("0 B", { exact: true })).toBeVisible();
});

it("only sends an enabled plan after explicit save", async () => {
  mocks.save.mockResolvedValue({ ...schedule, enabled: true, status: "active" });
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.click(screen.getByRole("checkbox", { name: "周期盘点" }));
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  await waitFor(() =>
    expect(mocks.save).toHaveBeenCalledWith({ enabled: true, interval_hours: 24 }),
  );
  expect(await screen.findByRole("status")).toHaveTextContent("周期计划已保存");
});

it.each(["", "0", "169", "1.5"])("rejects an invalid interval %j without a request", (value) => {
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.change(screen.getByRole("spinbutton", { name: "盘点间隔（小时）" }), {
    target: { value },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  expect(screen.getByRole("alert")).toHaveTextContent("1 至 168 小时的整数");
  expect(mocks.save).not.toHaveBeenCalled();
});

it("keeps a paused plan paused until explicitly reauthorized", async () => {
  render(
    <StorageUsageAccounting
      schedule={{
        ...schedule,
        enabled: true,
        status: "paused",
        pause_reason: "configuration_changed",
      }}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(screen.getByText(/计划已暂停。存储配置已变化/)).toBeVisible();
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("spinbutton", { name: "盘点间隔（小时）" }), {
    target: { value: "168" },
  });
  fireEvent.click(screen.getByRole("button", { name: "重新授权并保存" }));
  await waitFor(() =>
    expect(mocks.save).toHaveBeenCalledWith({ enabled: true, interval_hours: 168 }),
  );
});

it("does not replace unsaved settings when a background snapshot refreshes", () => {
  const view = render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.change(screen.getByRole("spinbutton", { name: "盘点间隔（小时）" }), {
    target: { value: "6" },
  });
  view.rerender(
    <StorageUsageAccounting schedule={{ ...schedule }} onAccessDenied={mocks.denied} />,
  );
  expect(screen.getByRole("spinbutton", { name: "盘点间隔（小时）" })).toHaveValue(6);
});

it("blocks duplicate saves while a request is pending", async () => {
  let finish!: (value: UsageScheduleSummary) => void;
  mocks.save.mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  expect(screen.getByRole("button", { name: "正在保存…" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "周期盘点" })).toBeDisabled();
  expect(mocks.save).toHaveBeenCalledTimes(1);
  await act(async () => finish(schedule));
});

it("retires late success when the authentication session changes", async () => {
  let finish!: (value: UsageScheduleSummary) => void;
  mocks.save.mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.click(screen.getByRole("checkbox", { name: "周期盘点" }));
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  act(() => useAuthStore.setState({ sessionId: "next" }));
  await act(async () => finish({ ...schedule, enabled: true, status: "active" }));
  expect(screen.queryByText("周期计划已保存。")).not.toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "周期盘点" })).not.toBeChecked();
});

it("retires a late denied response instead of revoking a new session", async () => {
  let reject!: (cause: unknown) => void;
  mocks.save.mockReturnValue(
    new Promise((_resolve, fail) => {
      reject = fail;
    }),
  );
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  act(() => useAuthStore.setState({ sessionId: "next" }));
  await act(async () => reject({ status: 403 }));
  expect(mocks.denied).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reports revoked access to the global usage owner", async () => {
  mocks.save.mockRejectedValue({ status: 403 });
  render(<StorageUsageAccounting schedule={schedule} onAccessDenied={mocks.denied} />);
  open();
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  await waitFor(() => expect(mocks.denied).toHaveBeenCalledTimes(1));
});

it("retains known accounting after a network failure", async () => {
  mocks.save.mockRejectedValue({ status: 503 });
  render(
    <StorageUsageAccounting
      accounting={accounting}
      schedule={schedule}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  fireEvent.click(screen.getByRole("button", { name: "保存周期计划" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("无法保存周期计划");
  expect(screen.getByText("+2 KiB")).toBeVisible();
  expect(screen.getByRole("button", { name: "保存周期计划" })).toBeEnabled();
  expect(mocks.denied).not.toHaveBeenCalled();
});

it("invalidates only the matching session's buckets after saving", async () => {
  const { useUpdateStorageUsageSchedule } =
    await vi.importActual<typeof import("@/hooks/useStorage")>("@/hooks/useStorage");
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  vi.spyOn(storageApi, "saveUsageSchedule").mockResolvedValue(schedule);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useUpdateStorageUsageSchedule(), { wrapper });
  await act(async () => hook.result.current.mutateAsync({ enabled: false, interval_hours: 24 }));
  expect(invalidate).toHaveBeenCalledTimes(1);
  expect(invalidate).toHaveBeenCalledWith({
    queryKey: ["storage-buckets", "admin/accounting/super_admin", getAuthSessionRevision()],
  });
  client.clear();
});

it.each(["session", "role"])(
  "does not invalidate new-owner cache after a %s change",
  async (kind) => {
    const { useUpdateStorageUsageSchedule } =
      await vi.importActual<typeof import("@/hooks/useStorage")>("@/hooks/useStorage");
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    let finish!: (value: UsageScheduleSummary) => void;
    vi.spyOn(storageApi, "saveUsageSchedule").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const hook = renderHook(() => useUpdateStorageUsageSchedule(), { wrapper });
    let pending!: Promise<UsageScheduleSummary>;
    act(() => {
      pending = hook.result.current.mutateAsync({ enabled: false, interval_hours: 24 });
    });
    await waitFor(() => expect(storageApi.saveUsageSchedule).toHaveBeenCalled());
    act(() =>
      useAuthStore.setState(
        kind === "session"
          ? { sessionId: "next" }
          : { user: { id: "admin", role: "project_admin" } as any },
      ),
    );
    await act(async () => {
      finish(schedule);
      await pending;
    });
    expect(invalidate).not.toHaveBeenCalled();
    client.clear();
  },
);

it("explains only the producers returned by the summary and never treats retirement as deletion", () => {
  const view = render(
    <StorageUsageAccounting accounting={accounting} onAccessDenied={mocks.denied} />,
  );
  open();
  expect(screen.getByText(/已接入临时输入写入／清理/)).toBeVisible();
  expect(screen.queryByText(/已接入托管原件受控新增/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入媒体缓存受控新增/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入独立导出媒体受控新增/)).not.toBeInTheDocument();
  view.rerender(
    <StorageUsageAccounting
      accounting={{ ...accounting, producers: ["managed_original_creation"] }}
      onAccessDenied={mocks.denied}
    />,
  );
  expect(screen.queryByText(/已接入临时输入写入／清理/)).not.toBeInTheDocument();
  expect(screen.getByText(/已接入托管原件受控新增；原件退役不代表物理移除/)).toBeVisible();
  expect(screen.getByText(/不能据此推算平台总占用/)).toBeVisible();
});

it("explains all covered media producers while retaining partial accounting and deletion limits", () => {
  render(
    <StorageUsageAccounting
      accounting={{
        ...accounting,
        producers: [
          "prediction_inputs",
          "managed_original_creation",
          "thumbnail_outputs",
          "original_cache",
          "image_pyramid_outputs",
          "video_derivative_outputs",
          "export_media_outputs",
        ],
      }}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(
    screen.getByText(
      /已接入媒体缓存受控新增：目录及普通缩略图、完整原图缓存、图像金字塔、视频派生物。缓存退休和清理记录不代表已确认移除/,
    ),
  ).toBeVisible();
  expect(screen.getByText("部分覆盖")).toBeVisible();
  expect(screen.getByText(/已接入独立导出媒体受控新增；ZIP 和导出清理尚未计入/)).toBeVisible();
  expect(screen.getByText(/不能据此推算平台总占用/)).toBeVisible();
  expect(screen.getByText("+2 KiB")).toBeVisible();
});

it("limits media coverage to producers explicitly present in the response", () => {
  render(
    <StorageUsageAccounting
      accounting={{ ...accounting, producers: ["original_cache", "unknown_future_producer"] }}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(screen.getByText(/已接入媒体缓存受控新增：完整原图缓存。/)).toBeVisible();
  expect(screen.queryByText(/目录及普通缩略图/)).not.toBeInTheDocument();
  expect(screen.queryByText(/图像金字塔/)).not.toBeInTheDocument();
  expect(screen.queryByText(/视频派生物/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入临时输入写入/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入独立导出媒体受控新增/)).not.toBeInTheDocument();
});

it("explains an export-only producer response without claiming ZIP, deletion or cache coverage", () => {
  render(
    <StorageUsageAccounting
      accounting={{ ...accounting, producers: ["export_media_outputs"] }}
      onAccessDenied={mocks.denied}
    />,
  );
  open();
  expect(screen.getByText(/已接入独立导出媒体受控新增；ZIP 和导出清理尚未计入/)).toBeVisible();
  expect(screen.queryByText(/已接入媒体缓存受控新增/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入临时输入写入/)).not.toBeInTheDocument();
  expect(screen.queryByText(/已接入托管原件受控新增/)).not.toBeInTheDocument();
  expect(screen.getByText("部分覆盖")).toBeVisible();
  expect(screen.getByText(/不能据此推算平台总占用/)).toBeVisible();
});
