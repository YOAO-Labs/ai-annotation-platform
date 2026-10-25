import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { MeResponse } from "@/api/auth";
import { ApiError } from "@/api/client";
import { MIGRATION_PURPOSES, type MigrationCopyItem } from "@/api/storageMigrationCopies";
import type { StorageMigrationInventoryItem } from "@/api/storageMigrationInventories";
import { useAuthStore } from "@/stores/authStore";
import { createTestQueryClient } from "@/test/queryClient";
import { MigrationInventoryPanel } from "./MigrationInventoryPanel";
const api = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  targets: vi.fn(),
  create: vi.fn(),
  generation: vi.fn(),
  attempt: vi.fn(),
  dispatch: vi.fn(),
  checkpoint: vi.fn(),
  failedObjects: vi.fn(),
  cancel: vi.fn(),
  inventories: vi.fn(),
}));
vi.mock("@/api/storageMigrationCopies", async (original) => ({
  ...(await original<typeof import("@/api/storageMigrationCopies")>()),
  storageMigrationCopiesApi: api,
}));
vi.mock("@/api/storageMigrationInventories", () => ({
  storageMigrationInventoriesApi: { list: api.inventories },
}));
vi.mock("@/api/asyncJobs", () => ({ asyncJobsApi: { cancel: api.cancel } }));
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: useAuthStore((state) => state.user?.role) }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId, onClose }: { jobId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="后台任务详情">
      <span>{jobId}</span>
      <button onClick={onClose}>关闭本次任务</button>
    </div>
  ),
}));
const COPY = "11111111-1111-1111-1111-111111111111";
const SOURCE = "22222222-2222-2222-2222-222222222222";
const JOB = "33333333-3333-3333-3333-333333333333";
const TARGET = "44444444-4444-4444-4444-444444444444";
const OTHER = "55555555-5555-5555-5555-555555555555";
const NEXT_SOURCE = "66666666-6666-6666-6666-666666666666";
const A = "a".repeat(64);
const B = "b".repeat(64);
const handling = Object.fromEntries(
  MIGRATION_PURPOSES.map(([key]) => [key, "migrate"]),
) as MigrationCopyItem["handling"];
const plan: MigrationCopyItem = {
  copy_id: COPY,
  created_at: "2026-10-25T00:00:00Z",
  updated_at: "2026-10-25T00:01:00Z",
  source_inventory_job_id: SOURCE,
  source_report_sha256: "f".repeat(64),
  source_complete: false,
  target_location_id: TARGET,
  target_name: "登记目标",
  handling,
  limits: {
    max_object_bytes: 268435456,
    max_source_bytes: 2147483648,
    max_upload_bytes: 2147483648,
    max_verify_bytes: 2147483648,
    max_objects: 10000,
    max_seconds: 1800,
    connect_timeout_seconds: 10,
    read_timeout_seconds: 60,
  },
  prepared: true,
  job_id: JOB,
  operation: "prepare",
  status: "completed",
  cancel_requested: false,
  counts: { verified: 0, pending: 3, writing: 0, uncertain: 0, failed: 0, skipped: 1 },
  decisions: { copy: 3, source_unverified: 1 },
  source_summary: {
    complete: false,
    observed_versions_and_markers: 4,
    objects: 4,
    delete_markers: 0,
    verified_objects: 3,
    unverified_objects: 1,
    over_object_limit: 0,
    mutable_null_objects: 0,
    unpinned_objects: 0,
    exact_catalog_matches: 0,
  },
  catalog_summary: {
    complete: false,
    enumeration_complete: true,
    resolution_complete: false,
    counts: { unexpanded: 1, issues: 0 },
  },
  copy_subset_complete: false,
  migration_ready: false,
  error_code: null,
  io: null,
  checkpoint_available: true,
  attempt_subset_complete: null,
  selected_count: null,
  selected_verified_count: null,
};
const source = {
  job_id: SOURCE,
  created_at: "2026-10-25T00:00:00Z",
  status: "failed",
  report_available: true,
  report_sha256: "f".repeat(64),
  complete: false,
  counts: null,
} as StorageMigrationInventoryItem;
const nextSource = { ...source, job_id: NEXT_SOURCE, status: "cancelled", report_sha256: A };
const child: MigrationCopyItem = {
  ...plan,
  copy_id: OTHER,
  generation: 2,
  parent_copy_id: COPY,
  parent_evidence_sha256: B,
  source_inventory_job_id: NEXT_SOURCE,
  source_report_sha256: A,
  generation_summary: {
    reused_objects: 2,
    new_objects: 1,
    mutable_recopies: 0,
    source_identity_conflicts: 1,
    previous_only_objects: 4,
    delete_markers: 1,
    history_records: 2,
  },
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
const unmounts: (() => void)[] = [];
function login(id = "owner", role = "super_admin") {
  useAuthStore.getState().setAuth(`${id}-token`, { id, role } as MeResponse);
}
function State() {
  const location = useLocation();
  return <output data-testid="url">{location.search}</output>;
}
function mount(url = `/storage?tab=migration&migration_copy=${COPY}`) {
  const client = createTestQueryClient();
  clients.push(client);
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <MigrationInventoryPanel />
        <State />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  unmounts.push(view.unmount);
  return { ...view, client };
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
  login();
  vi.stubGlobal(
    "URL",
    class extends URL {
      static createObjectURL = vi.fn(() => "blob:checkpoint");
      static revokeObjectURL = vi.fn();
    },
  );
  api.list.mockResolvedValue({ items: [plan], total: 1, limit: 20, offset: 0 });
  api.get.mockResolvedValue(plan);
  api.targets.mockResolvedValue({
    items: [{ location_id: TARGET, name: "登记目标", config_revision: 3 }],
    total: 21,
    limit: 20,
    offset: 0,
  });
  api.create.mockResolvedValue({
    item: { ...plan, prepared: false, status: "pending" },
    reused: false,
  });
  api.generation.mockResolvedValue({ item: { ...child, status: "pending" }, reused: false });
  api.attempt.mockImplementation((_id, operation) =>
    Promise.resolve({ item: { ...plan, operation, status: "pending" }, reused: false }),
  );
  api.dispatch.mockResolvedValue({ item: { ...plan, status: "pending" }, reused: true });
  api.cancel.mockResolvedValue({ status: "cancel_requested", id: JOB });
  api.checkpoint.mockResolvedValue(new Blob(["private checkpoint"]));
  api.inventories.mockResolvedValue({ items: [source], total: 1, limit: 20, offset: 0 });
  api.failedObjects.mockImplementation((_id, offset) =>
    Promise.resolve({
      items: [{ object_id: offset ? B : A, size_bytes: 123, error_code: "io_failure" }],
      total: 21,
      limit: 20,
      offset,
    }),
  );
});
afterEach(() => {
  unmounts.splice(0).forEach((unmount) => unmount());
  clients.splice(0).forEach((client) => client.clear());
  act(() => useAuthStore.getState().logout());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function review() {
  return screen.findByRole("dialog", { name: "审阅初次复制计划" });
}
async function fillPreparation() {
  fireEvent.click(await screen.findByRole("button", { name: "准备初次复制" }));
  const dialog = await screen.findByRole("dialog", { name: "准备初次复制" });
  await waitFor(() =>
    expect(within(dialog).getByRole("option", { name: "登记目标" })).toBeInTheDocument(),
  );
  fireEvent.change(within(dialog).getByLabelText("已登记 S3 目标"), { target: { value: TARGET } });
  for (const [, label] of MIGRATION_PURPOSES)
    fireEvent.change(within(dialog).getByLabelText(label), { target: { value: "migrate" } });
  return dialog;
}

it("partial sealed source prepares only, then requires a separate explicit start after review", async () => {
  mount("/storage?tab=migration&returnTo=%2Fdatasets%3Fdataset%3Dd&page=3");
  const dialog = await fillPreparation();
  expect(within(dialog).getByText(/清单不完整，仅处理合格子集/)).toBeInTheDocument();
  expect(within(dialog).getByText(`SHA-256：${source.report_sha256}`)).toHaveClass(
    "text-muted-foreground",
  );
  expect(within(dialog).getByText("高级：复制预算").closest("details")).not.toHaveAttribute("open");
  fireEvent.click(within(dialog).getByRole("button", { name: "准备复制计划" }));
  expect(await screen.findByRole("dialog", { name: "后台任务详情" })).toHaveTextContent(JOB);
  expect(api.create).toHaveBeenCalledWith({
    inventory_job_id: SOURCE,
    target_location_id: TARGET,
    handling,
    limits: plan.limits,
  });
  expect(api.attempt).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "关闭本次任务" }));
  const copy = await review();
  expect(within(copy).getByText(/定位未完整/)).toBeInTheDocument();
  fireEvent.click(within(copy).getByRole("button", { name: "开始初次复制" }));
  await waitFor(() => expect(api.attempt).toHaveBeenCalledWith(COPY, "run", false, undefined));
  expect(screen.getByTestId("url")).toHaveTextContent("page=3");
  expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
});

it("requires all nine decisions and valid budgets and retains a target selected on another page", async () => {
  mount("/storage?tab=migration");
  fireEvent.click(await screen.findByRole("button", { name: "准备初次复制" }));
  const dialog = await screen.findByRole("dialog", { name: "准备初次复制" });
  expect(within(dialog).getByRole("button", { name: "准备复制计划" })).toBeDisabled();
  await waitFor(() =>
    expect(within(dialog).getByRole("option", { name: "登记目标" })).toBeInTheDocument(),
  );
  fireEvent.change(within(dialog).getByLabelText("已登记 S3 目标"), { target: { value: TARGET } });
  api.targets.mockResolvedValue({
    items: [{ location_id: OTHER, name: "另一目标", config_revision: 1 }],
    total: 21,
  });
  fireEvent.click(
    within(within(dialog).getByRole("navigation", { name: "复制目标分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  await waitFor(() =>
    expect(api.targets).toHaveBeenCalledWith(SOURCE, 20, 20, expect.any(AbortSignal)),
  );
  expect(within(dialog).getByLabelText("已登记 S3 目标")).toHaveValue(TARGET);
  for (const [, label] of MIGRATION_PURPOSES)
    fireEvent.change(within(dialog).getByLabelText(label), { target: { value: "retain" } });
  const submit = within(dialog).getByRole("button", { name: "准备复制计划" });
  expect(submit).toBeEnabled();
  for (const label of ["每次执行时限（秒）", "连接超时（秒）", "读取超时（秒）"]) {
    const input = within(dialog).getByLabelText(label);
    expect(input).toHaveAttribute("step", "1");
    expect(input).toHaveAccessibleDescription(/填写整数/);
    fireEvent.change(input, { target: { value: "1.5" } });
    expect(submit).toBeDisabled();
    fireEvent.change(input, { target: { value: "1" } });
    expect(submit).toBeEnabled();
  }
  fireEvent.change(within(dialog).getByLabelText("单对象上限（bytes）"), {
    target: { value: "0" },
  });
  expect(within(dialog).getByRole("button", { name: "准备复制计划" })).toBeDisabled();
  expect(api.create).not.toHaveBeenCalled();
});

it.each([
  [
    "继续未完成项",
    { operation: "run", status: "cancelled", job_id: null, source_inventory_job_id: null },
    "run",
    false,
  ],
  ["续跑并重试全部失败项", { counts: { ...plan.counts, failed: 2 } }, "run", true],
  ["重新核验", { counts: { ...plan.counts, verified: 2 } }, "verify", false],
  ["重试准备", { prepared: false, counts: null, status: "failed" }, "prepare", false],
] as const)(
  "%s reuses the persistent copy owner with a new explicit attempt",
  async (label, change, operation, retry) => {
    api.get.mockResolvedValue({ ...plan, ...change });
    mount();
    const dialog = await review();
    fireEvent.click(await within(dialog).findByRole("button", { name: label }));
    await waitFor(() =>
      expect(api.attempt).toHaveBeenCalledWith(COPY, operation, retry, undefined),
    );
    expect(await screen.findByRole("dialog", { name: "后台任务详情" })).toHaveTextContent(JOB);
  },
);

it("pending redispatch and shared cancellation never create another attempt", async () => {
  api.get.mockResolvedValue({ ...plan, status: "pending", operation: "run" });
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByRole("button", { name: "取消当前任务" }));
  await waitFor(() => expect(api.cancel).toHaveBeenCalledWith(JOB));
  await waitFor(() =>
    expect(within(dialog).getByRole("button", { name: "重投等待任务" })).toBeEnabled(),
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "重投等待任务" }));
  await waitFor(() => expect(api.dispatch).toHaveBeenCalledWith(COPY));
  expect(api.attempt).not.toHaveBeenCalled();
});

it("explicit interruption recovery lets the server preserve hidden retry scope and reports unsettled execution honestly", async () => {
  api.get.mockResolvedValue({ ...plan, status: "running", operation: "run" });
  api.attempt.mockRejectedValue(
    new ApiError(409, "unsettled", { code: "copy_execution_unsettled" }),
  );
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByText("高级：检查中断执行"));
  fireEvent.click(within(dialog).getByRole("button", { name: "检查并恢复中断执行" }));
  await waitFor(() => expect(api.attempt).toHaveBeenCalledWith(COPY, "recover", false, undefined));
  expect(await within(dialog).findByText(/原执行结束尚未确认，仍保留原任务/)).toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
});

it("retains selected failed IDs across pages and reports selected success separately from global pending work", async () => {
  api.get.mockResolvedValue({ ...plan, operation: "run", counts: { ...plan.counts, failed: 21 } });
  api.attempt.mockImplementation((_id, operation) => {
    api.get.mockResolvedValue({
      ...plan,
      operation,
      counts: { ...plan.counts, verified: 2, failed: 19 },
      selected_count: 2,
      selected_verified_count: 2,
      attempt_subset_complete: true,
    });
    return Promise.resolve({ item: { ...plan, status: "pending" }, reused: false });
  });
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByText("选择失败对象重试"));
  fireEvent.click(await within(dialog).findByRole("checkbox", { name: new RegExp(A) }));
  fireEvent.click(
    within(within(dialog).getByRole("navigation", { name: "失败对象分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  fireEvent.click(await within(dialog).findByRole("checkbox", { name: new RegExp(B) }));
  expect(within(dialog).getByText("已选 2 项")).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "仅重试已选失败项" }));
  await waitFor(() => expect(api.attempt).toHaveBeenCalledWith(COPY, "run", true, [A, B]));
  fireEvent.click(await screen.findByRole("button", { name: "关闭本次任务" }));
  const restored = await review();
  expect(await within(restored).findByText(/选定子集已完成/)).toHaveTextContent(
    "整体计划状态以上方累计为准",
  );
  expect(within(restored).getByText("可复制子集尚未全部核验")).toBeInTheDocument();
});

it("changed selected failures show a recoverable conflict without silently starting a broad retry", async () => {
  api.get.mockResolvedValue({ ...plan, counts: { ...plan.counts, failed: 1 } });
  api.attempt.mockRejectedValue(
    new ApiError(409, "selection_changed", { code: "selection_changed" }),
  );
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByText("选择失败对象重试"));
  fireEvent.click(await within(dialog).findByRole("checkbox", { name: new RegExp(A) }));
  fireEvent.click(within(dialog).getByRole("button", { name: "仅重试已选失败项" }));
  expect(await within(dialog).findByText(/已选失败项状态已变化/)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "刷新失败对象" }));
  expect(within(dialog).getByText("已选 0 项")).toBeInTheDocument();
  expect(api.attempt).toHaveBeenCalledTimes(1);
});

it("zero eligible copies remain distinct from verified copies", async () => {
  api.get.mockResolvedValue({
    ...plan,
    counts: { verified: 0, pending: 0, writing: 0, uncertain: 0, failed: 0, skipped: 4 },
    decisions: { retain: 4 },
    copy_subset_complete: true,
  });
  mount();
  const dialog = await review();
  expect(await within(dialog).findByText("无可复制对象")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "开始初次复制" })).not.toBeInTheDocument();
  expect(within(dialog).queryByText("可复制子集已核验")).not.toBeInTheDocument();
});

it("copy pagination and selected plan preserve source pagination and dataset return context", async () => {
  api.list.mockResolvedValue({ items: [plan], total: 41 });
  api.inventories.mockResolvedValue({ items: [source], total: 61 });
  mount(
    "/storage?tab=migration&migration_page=3&migration_copy_page=2&returnTo=%2Fdatasets%3Fdataset%3Dd&page=4",
  );
  await screen.findByRole("button", { name: "审阅复制计划" });
  expect(api.list).toHaveBeenCalledWith(20, 20, expect.any(AbortSignal));
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "复制计划分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  await waitFor(() => expect(api.list).toHaveBeenCalledWith(40, 20, expect.any(AbortSignal)));
  expect(screen.getByTestId("url")).toHaveTextContent("migration_page=3");
  expect(screen.getByTestId("url")).toHaveTextContent("page=4");
  fireEvent.click(await screen.findByRole("button", { name: "审阅复制计划" }));
  await review();
  expect(screen.getByTestId("url")).toHaveTextContent(`migration_copy=${COPY}`);
});

it("checkpoint uses a private authenticated Blob and always releases its object URL", async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByRole("button", { name: "下载私密检查点" }));
  await waitFor(() => expect(click).toHaveBeenCalledOnce());
  expect(api.checkpoint).toHaveBeenCalledWith(COPY, expect.any(AbortSignal));
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:checkpoint");
  expect(document.querySelector("a[download]")).toBeNull();
});

it("an old-session checkpoint cannot download after account replacement", async () => {
  const blob = deferred<Blob>();
  api.checkpoint.mockReturnValueOnce(blob.promise);
  const view = mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByRole("button", { name: "下载私密检查点" }));
  const signal = api.checkpoint.mock.calls[0][1] as AbortSignal;
  act(() => login("other", "project_admin"));
  expect(signal.aborted).toBe(true);
  await act(async () => {
    blob.resolve(new Blob(["private"]));
  });
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  view.unmount();
});

it("late copy detail cannot reveal private results after same-account role revocation", async () => {
  const response = deferred<MigrationCopyItem>();
  api.get.mockReturnValueOnce(response.promise);
  mount();
  await waitFor(() => expect(api.get).toHaveBeenCalledOnce());
  act(() => useAuthStore.getState().setUser({ id: "owner", role: "project_admin" } as MeResponse));
  await act(async () => response.resolve(plan));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("登记目标")).not.toBeInTheDocument();
});

it("an attempt response cannot replace a different plan selected while it was pending", async () => {
  const response = deferred<{ item: MigrationCopyItem; reused: boolean }>();
  api.attempt.mockReturnValueOnce(response.promise);
  const another = { ...plan, copy_id: OTHER, target_name: "另一计划" };
  api.list.mockResolvedValue({ items: [plan, another], total: 2 });
  api.get.mockImplementation((id) => Promise.resolve(id === COPY ? plan : another));
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByRole("button", { name: "开始初次复制" }));
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
  fireEvent.click(screen.getAllByRole("button", { name: "审阅复制计划" })[1]);
  expect(
    await within(await review()).findByRole("heading", { name: "另一计划" }),
  ).toBeInTheDocument();
  await act(async () => response.resolve({ item: { ...plan, status: "pending" }, reused: false }));
  expect(screen.getByTestId("url")).toHaveTextContent(`migration_copy=${OTHER}`);
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
});

it("late prepare cannot open an old task after returning to the same account", async () => {
  const response = deferred<{ item: MigrationCopyItem; reused: boolean }>();
  api.create.mockReturnValueOnce(response.promise);
  mount("/storage?tab=migration");
  const dialog = await fillPreparation();
  fireEvent.click(within(dialog).getByRole("button", { name: "准备复制计划" }));
  act(() => login("other"));
  act(() => login());
  await act(async () => response.resolve({ item: plan, reused: false }));
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
  expect(screen.getByTestId("url").textContent).not.toContain("migration_copy=");
});

it("ordinary managers request none of the global migration APIs even for copy deep links", () => {
  login("manager", "project_admin");
  mount();
  expect(api.list).not.toHaveBeenCalled();
  expect(api.get).not.toHaveBeenCalled();
  expect(api.inventories).not.toHaveBeenCalled();
});

it("network refresh retains plan history; permission refusal clears all migration data and dialogs", async () => {
  const { client } = mount();
  await within(await review()).findByText("登记目标");
  api.list.mockRejectedValueOnce(new Error("network"));
  await act(() => client.invalidateQueries({ queryKey: ["storage-migration-copies"] }));
  expect(await screen.findByText(/刷新失败，保留上次复制计划/)).toBeInTheDocument();
  api.get.mockRejectedValueOnce(new ApiError(403, "denied"));
  await act(() => client.invalidateQueries({ queryKey: ["storage-migration-copy"] }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "准备初次复制" })).not.toBeInTheDocument();
});

it("Escape dismisses a prepared-source modal and restores its row trigger", async () => {
  mount("/storage?tab=migration");
  const trigger = await screen.findByRole("button", { name: "准备初次复制" });
  await userEvent.click(trigger);
  await screen.findByRole("dialog", { name: "准备初次复制" });
  await userEvent.keyboard("{Escape}");
  await waitFor(() => expect(trigger).toHaveFocus());
});

async function generationForm() {
  const original = await review();
  fireEvent.click(await within(original).findByRole("button", { name: "准备后续批次" }));
  return screen.findByRole("dialog", { name: "准备后续批次" });
}

it("prepares a partial follow-up report with inherited settings, then separately starts the reviewed generation", async () => {
  api.inventories.mockResolvedValue({ items: [source, nextSource], total: 61 });
  api.get.mockImplementation((id) => Promise.resolve(id === OTHER ? child : plan));
  mount(
    `/storage?tab=migration&migration_copy=${COPY}&migration_page=3&page=4&returnTo=%2Fdatasets`,
  );
  const dialog = await generationForm();
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  await waitFor(() =>
    expect(
      within(within(dialog).getByLabelText("后续封存清单")).getAllByRole("option"),
    ).toHaveLength(1),
  );
  expect(within(dialog).getAllByRole("combobox")).toHaveLength(2);
  expect(within(dialog).queryByRole("spinbutton")).not.toBeInTheDocument();
  expect(within(dialog).getByText("目标：登记目标")).toBeInTheDocument();
  expect(within(dialog).getByText("继承的九用途处理声明（只读）")).toBeInTheDocument();
  expect(within(dialog).getByText("继承的复制预算（只读）")).toBeInTheDocument();
  const select = within(dialog).getByLabelText("后续封存清单");
  expect(select).toHaveAccessibleDescription(/不完整清单也可准备合格子集/);
  fireEvent.change(select, { target: { value: NEXT_SOURCE } });
  expect(within(dialog).getByText("清单不完整，仅处理合格子集")).toBeInTheDocument();
  expect(within(dialog).getByText(`SHA-256：${A}`).parentElement).toHaveClass(
    "text-muted-foreground",
  );
  fireEvent.click(within(dialog).getByRole("button", { name: "准备后续批次" }));
  expect(await screen.findByRole("dialog", { name: "后台任务详情" })).toHaveTextContent(JOB);
  expect(api.generation).toHaveBeenCalledOnce();
  expect(api.generation).toHaveBeenCalledWith(COPY, NEXT_SOURCE);
  expect(api.create).not.toHaveBeenCalled();
  expect(api.targets).not.toHaveBeenCalled();
  expect(api.attempt).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "关闭本次任务" }));
  const followup = await screen.findByRole("dialog", { name: "审阅后续复制批次" });
  const differences = await within(followup).findByRole("region", { name: "后续清单差异" });
  expect(within(differences).getByText("复用映射").nextElementSibling).toHaveTextContent("2");
  expect(within(differences).getByText("本次未观察到").nextElementSibling).toHaveTextContent("4");
  expect(within(differences).getByText(/仍须在本批次读回准确版本后才计为已核验/)).toHaveTextContent(
    "均不表示源端已删除，也不代表可以切换",
  );
  expect(within(followup).getByText(/本批次累计：已核验 0 · 待处理 3/)).toBeInTheDocument();
  expect(within(followup).getByText("可复制子集尚未全部核验")).toBeInTheDocument();
  fireEvent.click(within(followup).getByRole("button", { name: "开始增量复制" }));
  await waitFor(() => expect(api.attempt).toHaveBeenCalledWith(OTHER, "run", false, undefined));
  expect(screen.getByTestId("url")).toHaveTextContent("migration_page=3");
  expect(screen.getByTestId("url")).toHaveTextContent("page=4");
  expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
});

it("filters unsealed and same reports while retaining a selected partial report across inventory pages", async () => {
  const unavailable = {
    ...nextSource,
    job_id: "77777777-7777-7777-7777-777777777777",
    report_available: false,
  };
  const active = {
    ...nextSource,
    job_id: "88888888-8888-8888-8888-888888888888",
    status: "running",
  };
  const same = { ...source, job_id: "99999999-9999-9999-9999-999999999999" };
  api.inventories.mockImplementation((offset) =>
    Promise.resolve({
      items: offset ? [] : [source, nextSource, unavailable, active, same],
      total: 21,
      offset,
      limit: 20,
    }),
  );
  mount();
  const dialog = await generationForm();
  await waitFor(() =>
    expect(
      within(within(dialog).getByLabelText("后续封存清单")).getAllByRole("option"),
    ).toHaveLength(1),
  );
  fireEvent.change(within(dialog).getByLabelText("后续封存清单"), {
    target: { value: NEXT_SOURCE },
  });
  fireEvent.click(
    within(within(dialog).getByRole("navigation", { name: "后续清单分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  await waitFor(() =>
    expect(api.inventories).toHaveBeenCalledWith(20, 20, expect.any(AbortSignal)),
  );
  expect(within(dialog).getByLabelText("后续封存清单")).toHaveValue(NEXT_SOURCE);
  expect(within(dialog).getByRole("button", { name: "准备后续批次" })).toBeEnabled();
  fireEvent.click(within(dialog).getByRole("button", { name: "准备后续批次" }));
  await waitFor(() => expect(api.generation).toHaveBeenCalledWith(COPY, NEXT_SOURCE));
});

it.each(["completed", "pending", "running"] as const)(
  "frozen %s parents retain read navigation and hide every mutation control",
  async (status) => {
    const frozen = {
      ...plan,
      status,
      read_only: true,
      successor_copy_id: OTHER,
      counts: { ...plan.counts!, failed: 1, verified: 1 },
    };
    api.get.mockImplementation((id) => Promise.resolve(id === OTHER ? child : frozen));
    mount(
      `/storage?tab=migration&migration_copy=${COPY}&migration_copy_page=1&returnTo=%2Fdatasets`,
    );
    const dialog = await review();
    await within(dialog).findByText(/已预留后续批次，原批次保持只读/);
    const buttons = within(dialog)
      .getAllByRole("button")
      .map((button) => button.textContent);
    expect(buttons).toEqual(
      expect.arrayContaining(["查看后续批次", "查看本次任务", "下载私密检查点"]),
    );
    for (const label of [
      "准备后续批次",
      "开始初次复制",
      "继续未完成项",
      "重新核验",
      "重试准备",
      "重投等待任务",
      "取消当前任务",
      "续跑并重试全部失败项",
      "检查并恢复中断执行",
    ])
      expect(within(dialog).queryByRole("button", { name: label })).not.toBeInTheDocument();
    expect(within(dialog).queryByText("选择失败对象重试")).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "查看后续批次" }));
    const followup = await screen.findByRole("dialog", { name: "审阅后续复制批次" });
    fireEvent.click(await within(followup).findByRole("button", { name: "查看原批次" }));
    await review();
    expect(screen.getByTestId("url")).toHaveTextContent(`migration_copy=${COPY}`);
    expect(screen.getByTestId("url")).toHaveTextContent("migration_copy_page=1");
    expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
    expect(api.generation).not.toHaveBeenCalled();
    expect(api.attempt).not.toHaveBeenCalled();
    expect(api.cancel).not.toHaveBeenCalled();
  },
);

it("Escape returns from generation preparation to the original review trigger without nested dialogs", async () => {
  mount();
  const original = await review();
  await userEvent.click(await within(original).findByRole("button", { name: "准备后续批次" }));
  await screen.findByRole("dialog", { name: "准备后续批次" });
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  await userEvent.keyboard("{Escape}");
  const restored = await review();
  await waitFor(() =>
    expect(within(restored).getByRole("button", { name: "准备后续批次" })).toHaveFocus(),
  );
});

it("a successor conflict stays in the generation form and never starts a copy attempt", async () => {
  api.inventories.mockResolvedValue({ items: [nextSource], total: 1 });
  api.generation.mockRejectedValue(
    new ApiError(409, "conflict", { code: "copy_successor_exists" }),
  );
  mount();
  const dialog = await generationForm();
  await waitFor(() =>
    expect(
      within(within(dialog).getByLabelText("后续封存清单")).getAllByRole("option"),
    ).toHaveLength(1),
  );
  fireEvent.change(within(dialog).getByLabelText("后续封存清单"), {
    target: { value: NEXT_SOURCE },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "准备后续批次" }));
  expect(await within(dialog).findByText(/原批次已有另一份清单的后续批次/)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "准备后续批次" })).toBeEnabled();
  expect(api.attempt).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
});

it("late generation preparation cannot open a private child task after account replacement", async () => {
  const response = deferred<{ item: MigrationCopyItem; reused: boolean }>();
  api.inventories.mockResolvedValue({ items: [nextSource], total: 1 });
  api.generation.mockReturnValueOnce(response.promise);
  mount();
  const dialog = await generationForm();
  await waitFor(() =>
    expect(
      within(within(dialog).getByLabelText("后续封存清单")).getAllByRole("option"),
    ).toHaveLength(1),
  );
  fireEvent.change(within(dialog).getByLabelText("后续封存清单"), {
    target: { value: NEXT_SOURCE },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "准备后续批次" }));
  act(() => login("other", "project_admin"));
  act(() => login());
  await act(async () => response.resolve({ item: child, reused: false }));
  expect(screen.queryByRole("dialog", { name: "后台任务详情" })).not.toBeInTheDocument();
  expect(screen.getByTestId("url")).toHaveTextContent(`migration_copy=${COPY}`);
});

it("generation report permission refusal clears the private review and choices", async () => {
  api.inventories.mockImplementationOnce(() => Promise.resolve({ items: [source], total: 1 }));
  api.inventories.mockRejectedValue(new ApiError(403, "denied"));
  mount();
  const dialog = await review();
  fireEvent.click(await within(dialog).findByRole("button", { name: "准备后续批次" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByText("登记目标")).not.toBeInTheDocument();
  expect(api.generation).not.toHaveBeenCalled();
});
