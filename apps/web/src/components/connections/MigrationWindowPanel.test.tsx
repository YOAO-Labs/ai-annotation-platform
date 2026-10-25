import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import type { MigrationWindowItem } from "@/api/storageMigrationWindows";
import type { MigrationCopyItem } from "@/api/storageMigrationCopies";
import { useAuthStore } from "@/stores/authStore";
import { createTestQueryClient } from "@/test/queryClient";
import { MigrationInventoryPanel } from "./MigrationInventoryPanel";
const api = vi.hoisted(() => ({
  windows: vi.fn(),
  window: vi.fn(),
  create: vi.fn(),
  transition: vi.fn(),
  copies: vi.fn(),
  copy: vi.fn(),
  inventories: vi.fn(),
  start: vi.fn(),
}));
vi.mock("@/api/storageMigrationWindows", () => ({
  storageMigrationWindowsApi: {
    list: api.windows,
    get: api.window,
    create: api.create,
    transition: api.transition,
  },
}));
vi.mock("@/api/storageMigrationCopies", async (original) => ({
  ...(await original<typeof import("@/api/storageMigrationCopies")>()),
  storageMigrationCopiesApi: { list: api.copies, get: api.copy },
}));
vi.mock("@/api/storageMigrationInventories", () => ({
  storageMigrationInventoriesApi: { list: api.inventories, start: api.start },
}));
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: useAuthStore((state) => state.user?.role) }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({
    onClose,
    renderContext,
  }: {
    onClose: () => void;
    renderContext?: (job: { kind: string; payload: Record<string, unknown> }) => React.ReactNode;
  }) => (
    <div role="dialog" aria-label="后台任务详情">
      {renderContext?.({
        kind: "storage_migration_inventory",
        payload: { window_id: WINDOW, window_revision: 3 },
      })}
      <button onClick={onClose}>关闭任务详情</button>
    </div>
  ),
}));
const WINDOW = "11111111-1111-1111-1111-111111111111";
const COPY = "22222222-2222-2222-2222-222222222222";
const JOB = "33333333-3333-3333-3333-333333333333";
const OTHER = "44444444-4444-4444-4444-444444444444";
const item: MigrationWindowItem = {
  window_id: WINDOW,
  parent_copy_id: COPY,
  target_location_id: "target",
  state: "draining",
  revision: 3,
  source_gate_revision: 9,
  source_config_revision: 2,
  created_at: "2026-10-26T00:00:00Z",
  updated_at: "2026-10-26T00:00:00Z",
  created_by: null,
  registered_operations: 0,
  registered_access_drained: false,
  source_quiescence_proven: false,
  migration_ready: false,
};
const copy = {
  copy_id: COPY,
  prepared: true,
  target_name: "登记目标",
  target_location_id: "target",
  generation: 1,
  status: "completed",
  created_at: item.created_at,
  updated_at: item.updated_at,
  source_report_sha256: "a".repeat(64),
  source_complete: false,
  counts: null,
  handling: {},
  limits: {},
  decisions: null,
  source_summary: null,
  catalog_summary: null,
  io: null,
  window_id: WINDOW,
  window_revision: 3,
} as MigrationCopyItem;
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function login(id = "owner", role = "super_admin") {
  useAuthStore.getState().setAuth(`${id}-token`, { id, role } as MeResponse);
}
function State() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="url">{location.search}</output>
      <button onClick={() => navigate(`/storage?tab=migration&migration_window=${OTHER}`)}>
        其他窗口
      </button>
    </>
  );
}
function mount(url = "/storage?tab=migration&returnTo=%2Fdatasets%3Fdataset%3Dd&other=keep") {
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
function failure(code: string) {
  return new ApiError(409, "conflict", { code });
}
async function openCreate() {
  fireEvent.click(screen.getByRole("button", { name: "开启迁移窗口" }));
  return screen.findByRole("dialog", { name: "开启迁移窗口" });
}
async function chooseParent() {
  await waitFor(() => expect(screen.getByRole("option", { name: /登记目标/ })).toBeInTheDocument());
  fireEvent.change(screen.getByLabelText("父复制批次"), { target: { value: COPY } });
  fireEvent.click(screen.getByRole("checkbox"));
}
async function expandSelector() {
  fireEvent.click(screen.getByText("可选：在迁移窗口内建立清单"));
  await waitFor(() => expect(api.windows).toHaveBeenCalled());
}
beforeEach(() => {
  vi.clearAllMocks();
  login();
  api.windows.mockResolvedValue({ items: [item], total: 21, limit: 20, offset: 0 });
  api.window.mockResolvedValue(item);
  api.create.mockResolvedValue(item);
  api.transition.mockImplementation(async (_id, action) => ({
    ...item,
    revision: 4,
    state: action === "stop" ? "stopped" : "closed",
    registered_access_drained: true,
  }));
  api.copies.mockResolvedValue({ items: [copy], total: 21, limit: 20, offset: 0 });
  api.copy.mockResolvedValue(copy);
  api.inventories.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  api.start.mockResolvedValue({ item: { job_id: JOB, status: "pending" }, reused: false });
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  act(() => useAuthStore.getState().logout());
  vi.restoreAllMocks();
});

it("keeps window history disclosed, paginated and linked without reserving a detail column", async () => {
  mount();
  expect(api.windows).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("窗口历史"));
  const section = screen.getByRole("region", { name: "迁移窗口" });
  fireEvent.click(await within(section).findByRole("button", { name: "查看窗口详情" }));
  await waitFor(() =>
    expect(screen.getByRole("dialog", { name: "迁移窗口详情" })).toHaveTextContent("注册操作：0"),
  );
  expect(screen.getByTestId("url")).toHaveTextContent("other=keep");
  fireEvent.click(screen.getByRole("button", { name: "返回原页面" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  fireEvent.click(within(section).getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(api.windows).toHaveBeenCalledWith(20, 20, expect.any(AbortSignal)));
  expect(screen.getByTestId("url")).toHaveTextContent("migration_window_page=2");
});
it("requires an eligible parent and explicit impact acknowledgement with blank select and external muted hint", async () => {
  api.copies.mockResolvedValue({
    items: [
      copy,
      { ...copy, copy_id: OTHER, status: "running", target_name: "活动批次" },
      { ...copy, copy_id: JOB, prepared: false, target_name: "未封存批次" },
    ],
    total: 3,
  });
  mount();
  const dialog = await openCreate();
  expect(within(dialog).getByRole("button", { name: "确认开启窗口" })).toBeDisabled();
  const select = screen.getByLabelText("父复制批次");
  expect(select.querySelector('option[value=""]')).toHaveAttribute("disabled");
  expect(select.querySelector('option[value=""]')).toHaveAttribute("hidden");
  expect(select.querySelector('option[value=""]')).toHaveTextContent("");
  expect(document.getElementById(select.getAttribute("aria-describedby")!)).toHaveClass(
    "text-muted-foreground",
  );
  await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(1));
  fireEvent.change(select, { target: { value: COPY } });
  expect(within(dialog).getByRole("button", { name: "确认开启窗口" })).toBeDisabled();
  expect(dialog).toHaveTextContent("普通注册访问将停止接纳新操作");
  expect(dialog).toHaveTextContent("登记访问已停止不等于全源停写");
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认开启窗口" }));
  expect(await screen.findByRole("dialog", { name: "迁移窗口详情" })).toBeInTheDocument();
  expect(api.create).toHaveBeenCalledWith(COPY);
});
it("pages parent choices independently and preserves selected parent on another page", async () => {
  mount();
  const dialog = await openCreate();
  await chooseParent();
  fireEvent.click(within(dialog).getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(api.copies).toHaveBeenCalledWith(20, 20, expect.any(AbortSignal)));
  expect(screen.getByLabelText("父复制批次")).toHaveValue(COPY);
});
it("invalidates a parent that becomes active on refresh rather than opening it", async () => {
  mount();
  await openCreate();
  await chooseParent();
  api.copies.mockResolvedValue({ items: [{ ...copy, status: "running" }], total: 1 });
  fireEvent.click(screen.getByRole("button", { name: "刷新父复制批次" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "确认开启窗口" })).toBeDisabled());
  expect(screen.getByRole("checkbox")).not.toBeChecked();
  expect(api.create).not.toHaveBeenCalled();
});
it("does not reopen or navigate from a create response arriving after Escape", async () => {
  const late = deferred<MigrationWindowItem>();
  api.create.mockReturnValue(late.promise);
  mount();
  await openCreate();
  await chooseParent();
  fireEvent.click(screen.getByRole("button", { name: "确认开启窗口" }));
  await waitFor(() => expect(api.create).toHaveBeenCalled());
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await act(async () => late.resolve(item));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByTestId("url").textContent).not.toContain("migration_window=");
});
it("retires pending window creation across auth owner changes", async () => {
  const late = deferred<MigrationWindowItem>();
  api.create.mockReturnValue(late.promise);
  mount();
  await openCreate();
  await chooseParent();
  fireEvent.click(screen.getByRole("button", { name: "确认开启窗口" }));
  act(() => login("next"));
  await act(async () => late.resolve(item));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.getByTestId("url").textContent).not.toContain("migration_window=");
});
it("stops only on explicit zero-count CAS using the displayed revision", async () => {
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  fireEvent.click(await screen.findByRole("button", { name: "确认停止登记访问" }));
  await waitFor(() => expect(api.transition).toHaveBeenCalledWith(WINDOW, "stop", 3));
  expect(await screen.findByText("登记访问已排空。")).toBeInTheDocument();
  expect(screen.getByRole("dialog")).toHaveTextContent("登记访问已停止不等于全源停写");
});
it("blocks stop with unresolved registered operations while permitting a close request", async () => {
  api.window.mockResolvedValue({ ...item, registered_operations: 2 });
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  expect(await screen.findByRole("button", { name: "确认停止登记访问" })).toBeDisabled();
  expect(screen.getByRole("dialog")).toHaveTextContent("注册操作：2");
  expect(screen.getByRole("button", { name: "关闭窗口" })).toBeEnabled();
  expect(api.transition).not.toHaveBeenCalled();
});
it("preserves closing with live operations and only explicit retries can reopen ordinary access", async () => {
  api.window.mockResolvedValue({ ...item, state: "closing", registered_operations: 1 });
  api.transition
    .mockResolvedValueOnce({ ...item, state: "closing", revision: 4, registered_operations: 1 })
    .mockResolvedValueOnce({ ...item, state: "closed", revision: 5 });
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  fireEvent.click(await screen.findByRole("button", { name: "重试关闭窗口" }));
  await waitFor(() => expect(api.transition).toHaveBeenCalledWith(WINDOW, "close", 3));
  expect(screen.getByRole("dialog")).toHaveTextContent("本页不会自动恢复访问");
  expect(screen.queryByText(/窗口已关闭，普通注册访问已恢复/)).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("button", { name: "重试关闭窗口" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "重试关闭窗口" }));
  await waitFor(() => expect(api.transition).toHaveBeenLastCalledWith(WINDOW, "close", 4));
  expect(await screen.findByText(/窗口已关闭，普通注册访问已恢复/)).toBeInTheDocument();
});
it("refreshes a CAS conflict and retries against the newly displayed revision", async () => {
  api.transition.mockRejectedValueOnce(failure("migration_window_changed"));
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  await screen.findByRole("button", { name: "确认停止登记访问" });
  api.window.mockResolvedValue({ ...item, revision: 8 });
  fireEvent.click(screen.getByRole("button", { name: "确认停止登记访问" }));
  expect(await screen.findByText(/窗口状态已变化，已重新读取/)).toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("窗口 8"));
  fireEvent.click(screen.getByRole("button", { name: "确认停止登记访问" }));
  await waitFor(() => expect(api.transition).toHaveBeenLastCalledWith(WINDOW, "stop", 8));
});
it("ignores old transition responses after navigating to another window", async () => {
  const late = deferred<MigrationWindowItem>();
  api.transition.mockReturnValue(late.promise);
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  fireEvent.click(await screen.findByRole("button", { name: "确认停止登记访问" }));
  api.window.mockResolvedValue({ ...item, window_id: OTHER, revision: 12 });
  fireEvent.click(screen.getByRole("button", { name: "其他窗口", hidden: true }));
  await waitFor(() => expect(screen.getByRole("dialog")).toHaveTextContent("窗口 12"));
  await act(async () => late.resolve({ ...item, state: "stopped", revision: 4 }));
  expect(screen.getByRole("dialog")).toHaveTextContent("窗口 12");
  expect(screen.getByTestId("url")).toHaveTextContent(OTHER);
});
it("clears sensitive window data and all actions on permission revocation", async () => {
  api.transition.mockRejectedValueOnce(new ApiError(403, "denied"));
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  fireEvent.click(await screen.findByRole("button", { name: "确认停止登记访问" }));
  expect(await screen.findByText("当前账号已无权读取迁移准备。")).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "开启迁移窗口" })).not.toBeInTheDocument();
});
it("ordinary managers cannot query windows even through a window deep link", () => {
  login("manager", "project_admin");
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  expect(api.windows).not.toHaveBeenCalled();
  expect(api.window).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
it("preserves the ordinary inventory default and sends no implicit window", async () => {
  mount();
  fireEvent.click(screen.getByRole("button", { name: "建立迁移清单" }));
  await waitFor(() => expect(api.start).toHaveBeenCalledOnce());
  expect(api.start.mock.calls[0]).toHaveLength(1);
});
it("offers only stopped windows with grey external hints and binds explicitly selected inventories", async () => {
  api.windows.mockResolvedValue({
    items: [item, { ...item, window_id: OTHER, state: "stopped" }],
    total: 2,
  });
  api.window.mockResolvedValue({ ...item, window_id: OTHER, state: "stopped" });
  mount();
  await expandSelector();
  const select = screen.getByRole("combobox", { name: "迁移窗口" });
  await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(1));
  expect(select.querySelector('option[value=""]')).toHaveAttribute("hidden");
  expect(document.getElementById(select.getAttribute("aria-describedby")!)).toHaveClass(
    "text-muted-foreground",
  );
  fireEvent.change(select, { target: { value: OTHER } });
  await waitFor(() => expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "建立迁移清单" }));
  await waitFor(() => expect(api.start).toHaveBeenCalledWith(expect.any(Object), OTHER));
});
it("does not silently switch an invalidated window selection to ordinary mode", async () => {
  api.windows.mockResolvedValue({ items: [{ ...item, state: "stopped" }], total: 1 });
  api.window.mockResolvedValue({ ...item, state: "stopped" });
  const { client } = mount();
  await expandSelector();
  await waitFor(() =>
    expect(screen.getByRole("option", { name: new RegExp(WINDOW) })).toBeInTheDocument(),
  );
  fireEvent.change(screen.getByRole("combobox", { name: "迁移窗口" }), {
    target: { value: WINDOW },
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeEnabled());
  api.window.mockResolvedValue({ ...item, state: "closing" });
  await act(() => client.invalidateQueries({ queryKey: ["storage-migration-window"] }));
  await waitFor(() => expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeDisabled());
  expect(screen.getByRole("combobox", { name: "迁移窗口" })).toHaveValue(WINDOW);
  fireEvent.click(screen.getByRole("button", { name: "使用普通清单路径" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "建立迁移清单" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "建立迁移清单" }));
  await waitFor(() => expect(api.start).toHaveBeenCalledOnce());
  expect(api.start.mock.calls[0]).toHaveLength(1);
});
it("shows window binding in off-page inventory details and returns to the original task on Escape", async () => {
  mount(`/storage?tab=migration&migration_job=${JOB}&returnTo=%2Fdatasets%3Fdataset%3Dd`);
  fireEvent.click(await screen.findByRole("button", { name: "查看迁移窗口" }));
  await waitFor(() =>
    expect(screen.getByRole("dialog", { name: "迁移窗口详情" })).toHaveTextContent(WINDOW),
  );
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "后台任务详情" })).toBeInTheDocument();
  expect(screen.getByTestId("url")).toHaveTextContent(`migration_job=${JOB}`);
  expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
});
it("shows copy binding, returns focus to its window link, and opens the fixed parent from window details", async () => {
  mount(`/storage?tab=migration&migration_copy=${COPY}`);
  const link = await screen.findByRole("button", { name: "查看迁移窗口" });
  fireEvent.click(link);
  await screen.findByRole("dialog", { name: "迁移窗口详情" });
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(screen.getByRole("button", { name: "查看迁移窗口" })).toHaveFocus());
  fireEvent.click(screen.getByRole("button", { name: "查看迁移窗口" }));
  fireEvent.click(await screen.findByRole("button", { name: "查看父复制批次" }));
  expect(await screen.findByRole("dialog", { name: "审阅初次复制计划" })).toBeInTheDocument();
  expect(screen.getByTestId("url")).toHaveTextContent(`migration_copy=${COPY}`);
});

it("closed count-zero windows never ask for a further stop confirmation", async () => {
  api.window.mockResolvedValue({ ...item, state: "closed", registered_access_drained: false });
  mount(`/storage?tab=migration&migration_window=${WINDOW}`);
  expect(await screen.findByText(/窗口已关闭，普通注册访问已恢复/)).toBeInTheDocument();
  expect(screen.queryByText(/仍需显式确认停止/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "确认停止登记访问" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "关闭窗口" })).not.toBeInTheDocument();
});
it("window inventory reports guide successor preparation and cannot start a new root copy", async () => {
  api.inventories.mockResolvedValue({
    items: [
      {
        job_id: JOB,
        status: "completed",
        created_at: item.created_at,
        complete: true,
        counts: null,
        report_available: true,
        report_sha256: "a".repeat(64),
        window_id: WINDOW,
        window_revision: 3,
      },
    ],
    total: 1,
  });
  mount();
  expect(await screen.findByText("窗口清单须沿固定父复制批次准备后续批次。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "准备初次复制" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "查看迁移窗口" })).toBeInTheDocument();
});

it("invalid window IDs do not request details or suppress an existing copy detail", async () => {
  mount(`/storage?tab=migration&migration_window=invalid&migration_copy=${COPY}`);
  expect(await screen.findByRole("dialog", { name: "审阅初次复制计划" })).toBeInTheDocument();
  expect(api.window).not.toHaveBeenCalled();
});
