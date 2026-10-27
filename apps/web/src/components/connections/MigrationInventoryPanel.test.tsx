import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { StorageMigrationInventoryItem } from "@/api/storageMigrationInventories";
import { useAuthStore } from "@/stores/authStore";
import { createTestQueryClient } from "@/test/queryClient";
import { MigrationInventoryPanel } from "./MigrationInventoryPanel";

const api = vi.hoisted(() => ({
  list: vi.fn(),
  start: vi.fn(),
  dispatch: vi.fn(),
  report: vi.fn(),
  cancel: vi.fn(),
}));
vi.mock("@/api/storageMigrationInventories", () => ({ storageMigrationInventoriesApi: api }));
vi.mock("./MigrationCopyPanel", () => ({ MigrationCopyPanel: () => null }));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { cancel: api.cancel } }));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId, onClose }: { jobId: string; onClose: () => void }) => (
    <div role="dialog">
      <span>{jobId}</span>
      <button onClick={onClose}>关闭详情</button>
    </div>
  ),
}));
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: useAuthStore((state) => state.user?.role) }),
}));
const ID = "11111111-1111-1111-1111-111111111111";
const limits = {
  max_pages: 1000,
  max_entries: 10000,
  max_object_bytes: 268435456,
  max_total_bytes: 2147483648,
  max_seconds: 600,
  read_timeout_seconds: 30,
  page_size: 1000,
};
const item: StorageMigrationInventoryItem = {
  job_id: ID,
  status: "completed",
  created_at: "2026-10-25T00:00:00Z",
  started_at: null,
  completed_at: "2026-10-25T00:00:10Z",
  scope: "selected-configured-source",
  source_location_id: "legacy",
  migration_ready: false,
  limits,
  handling: {},
  cancel_requested: false,
  complete: true,
  counts: {
    buckets: 1,
    objects: 2,
    delete_markers: 1,
    multipart: 0,
    verified_objects: 2,
    verified_bytes: 10,
    bytes_read: 10,
    pages: 2,
    issues: 0,
    entries: 3,
  },
  error_code: null,
  report_available: true,
  report_sha256: "a".repeat(64),
  report_size_bytes: 100,
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function login(id = "owner", role = "super_admin", token = `${id}-token`) {
  useAuthStore.getState().setAuth(token, { id, role } as MeResponse);
}
function State() {
  const location = useLocation();
  return <output data-testid="url">{location.search}</output>;
}
function mount(url = "/storage?tab=migration") {
  const client = createTestQueryClient();
  clients.push(client);
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[url]}>
          <MigrationInventoryPanel />
          <State />
        </MemoryRouter>
      </QueryClientProvider>,
    ),
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = vi.fn(() => "blob:inventory");
      static revokeObjectURL = vi.fn();
    },
  );
  login();
  api.list.mockResolvedValue({ items: [item], total: 21, offset: 0, limit: 20 });
  api.start.mockResolvedValue({ item: { ...item, status: "pending" }, reused: false });
  api.dispatch.mockResolvedValue({ item, reused: true });
  api.cancel.mockResolvedValue({ status: "cancel_requested", id: ID });
  api.report.mockResolvedValue(new Blob(["report"]));
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  act(() => useAuthStore.getState().logout());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("keeps source boundaries, retention and honest zero counts visible with budgets disclosed", async () => {
  mount();
  expect(await screen.findByText("清单完整 · 报告可下载")).toBeInTheDocument();
  expect(screen.getByText(/报告随任务保留 30 天/)).toBeInTheDocument();
  expect(screen.getByText(/九用途处理声明均未审定/)).toBeInTheDocument();
  expect(screen.getByText(/未完成上传 0/)).toBeInTheDocument();
  const input = screen.getByLabelText("最多清单条目");
  expect(input.closest("details")).not.toHaveAttribute("open");
  expect(document.getElementById(input.getAttribute("aria-describedby")!)).toHaveClass(
    "text-muted-foreground",
  );
  expect(screen.queryByText("迁移完成")).not.toBeInTheDocument();
  expect(api.dispatch).not.toHaveBeenCalled();
});

it("submits actual limits then opens shared details, preserving independent list and return context", async () => {
  mount("/storage?tab=migration&page=3&returnTo=%2Fdatasets%3Fdataset%3Dd&other=keep");
  fireEvent.change(screen.getByLabelText("最多清单条目"), { target: { value: "123" } });
  fireEvent.click(screen.getByRole("button", { name: "建立迁移清单" }));
  expect(await screen.findByRole("dialog")).toHaveTextContent(ID);
  expect(api.start).toHaveBeenCalledWith({ ...limits, max_entries: 123 });
  expect(screen.getByTestId("url")).toHaveTextContent("page=3");
  expect(screen.getByTestId("url")).toHaveTextContent("other=keep");
  fireEvent.click(screen.getByRole("button", { name: "关闭详情" }));
  expect(screen.getByTestId("url").textContent).not.toContain("migration_job");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(20, 20, expect.any(AbortSignal)));
  expect(screen.getByTestId("url")).toHaveTextContent("migration_page=2");
  expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
});

it("rejects invalid advanced limits before submitting", () => {
  mount();
  fireEvent.change(screen.getByLabelText("执行时限（秒）"), { target: { value: "3601" } });
  expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeDisabled();
  expect(api.start).not.toHaveBeenCalled();
});

it("ordinary managers do not request global migration endpoints, including deep links", () => {
  login("manager", "project_admin");
  mount(`/storage?tab=migration&migration_job=${ID}`);
  expect(screen.getByText("迁移准备仅超级管理员可查看。")).toBeInTheDocument();
  expect(api.list).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("refresh errors retain history but permission rejection clears data and actions", async () => {
  const { client } = mount(`/storage?tab=migration&migration_job=${ID}`);
  await screen.findByText("清单完整 · 报告可下载");
  api.list.mockRejectedValueOnce(new Error("network"));
  await act(() => client.invalidateQueries({ queryKey: ["storage-migration-inventories"] }));
  expect(await screen.findByText(/刷新失败，保留上次清单历史/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "下载清单报告" })).toBeInTheDocument();
  api.list.mockRejectedValueOnce(new ApiError(403, "denied"));
  await act(() => client.invalidateQueries({ queryKey: ["storage-migration-inventories"] }));
  await screen.findByText(/当前账号已无权读取迁移准备/);
  expect(screen.queryByText("清单完整 · 报告可下载")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeDisabled();
});

it("pending tasks dispatch only on explicit request and cancel through the shared endpoint", async () => {
  api.list.mockResolvedValue({
    items: [{ ...item, status: "pending", complete: null, counts: null, report_available: false }],
    total: 1,
  });
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "重新派发" }));
  await waitFor(() => expect(api.dispatch).toHaveBeenCalledWith(ID));
  await waitFor(() => expect(screen.getByRole("button", { name: "取消任务" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "取消任务" }));
  await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(ID));
  expect(await screen.findByText(/已提交取消请求/)).toBeInTheDocument();
  expect(api.start).not.toHaveBeenCalled();
});

it("downloads a private blob and releases the URL and temporary anchor", async () => {
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:inventory");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "下载清单报告" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(api.report).toHaveBeenCalledWith(ID, expect.any(AbortSignal));
  expect(create).toHaveBeenCalledOnce();
  expect(revoke).toHaveBeenCalledWith("blob:inventory");
  expect(document.querySelector("a[download]")).toBeNull();
});

it("a missing or expired report retains history and allows a refresh", async () => {
  api.report.mockRejectedValueOnce(new ApiError(404, "报告不存在或已过期"));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "下载清单报告" }));
  expect(await screen.findByText(/报告不存在或已过期/)).toBeInTheDocument();
  expect(screen.getByText("清单完整 · 报告可下载")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "刷新历史" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeEnabled();
});

it("a late start cannot open a previous session's task, even after returning to the same user", async () => {
  const request = deferred<{ item: StorageMigrationInventoryItem; reused: boolean }>();
  api.start.mockReturnValueOnce(request.promise);
  mount();
  fireEvent.click(screen.getByRole("button", { name: "建立迁移清单" }));
  act(() => login("other"));
  act(() => login());
  await act(async () => {
    request.resolve({ item, reused: false });
    await request.promise;
  });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByTestId("url").textContent).not.toContain("migration_job");
});

it("late download is aborted and never creates an object URL after logout", async () => {
  const request = deferred<Blob>();
  api.report.mockReturnValueOnce(request.promise);
  const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:inventory");
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "下载清单报告" }));
  const signal = api.report.mock.calls[0][1] as AbortSignal;
  act(() => useAuthStore.getState().logout());
  expect(signal.aborted).toBe(true);
  await act(async () => {
    request.resolve(new Blob(["private"]));
    await request.promise;
  });
  expect(create).not.toHaveBeenCalled();
  expect(screen.queryByText("清单完整 · 报告可下载")).not.toBeInTheDocument();
});

it("late history cannot surface after session switch or same-session role revocation", async () => {
  const request = deferred<unknown>();
  api.list.mockReturnValueOnce(request.promise);
  mount();
  act(() => login("other", "super_admin"));
  api.list.mockResolvedValue({ items: [], total: 0 });
  act(() => login("other", "project_admin"));
  await act(async () => {
    request.resolve({ items: [item], total: 1 });
    await request.promise;
  });
  expect(screen.queryByText("清单完整 · 报告可下载")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "建立迁移清单" })).not.toBeInTheDocument();
});
