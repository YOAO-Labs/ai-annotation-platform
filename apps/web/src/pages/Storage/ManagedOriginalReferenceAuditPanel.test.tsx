import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { ReferenceReconciliationOut } from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { ManagedOriginalReferenceAuditPanel } from "./ManagedOriginalReferenceAuditPanel";

const auth = vi.hoisted(() => ({ revision: 1 }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: api,
}));
vi.mock("@/stores/authSession", () => ({
  getAuthSessionRevision: () => auth.revision,
  subscribeAuthSession: () => () => {},
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ sessionId: `session-${auth.revision}` }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId }: { jobId: string }) => (
    <div role="dialog" aria-label="引用核对任务详情">
      {jobId}
    </div>
  ),
}));

const path = "/storage/managed-original-reference-reconciliation";
const jobId = "44444444-4444-4444-8444-444444444444";
const clients: ReturnType<typeof createTestQueryClient>[] = [];
let state: ReferenceReconciliationOut;
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
function result(): ReferenceReconciliationOut {
  return {
    status: "completed",
    async_job_id: jobId,
    measured_at: "2026-10-26T05:00:00Z",
    last_success_at: "2026-10-26T05:00:00Z",
    counts: {
      total_assertions: 8,
      physical_groups: 6,
      protected_groups: 2,
      retained_groups: 1,
      blocked_groups: 1,
      unreferenced_groups: 2,
    },
    unknown: {
      unknown_training_versions: 1,
      unknown_active_learning_rounds: 0,
      unknown_reference_aliases: 2,
      unknown_operations: 0,
    },
    deltas: [{ metric: "protected_groups", before: 3, after: 2, change: -1 }],
    error_code: null,
  };
}
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const denied = vi.fn();
  const tree = () => (
    <QueryClientProvider client={client}>
      <ManagedOriginalReferenceAuditPanel onDenied={denied} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { denied, refresh: () => view.rerender(tree()) };
}
async function expand() {
  fireEvent.click(screen.getByText("周期引用核对", { selector: "summary" }));
  await waitFor(() => expect(api.get).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText("正在读取引用核对记录…")).not.toBeInTheDocument());
}
beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  state = result();
  api.get.mockImplementation(async () => clone(state));
  api.post.mockImplementation(async () => {
    state.status = "pending";
    return clone(state);
  });
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  vi.restoreAllMocks();
});

it("reads only after disclosure and separates daily DB observations from cache cleanup", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  await expand();
  expect(api.get).toHaveBeenCalledWith(path, { signal: expect.any(AbortSignal) });
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.getByText(/每日自动核对.*不读取或删除对象/)).toBeInTheDocument();
  expect(screen.getByText(/完整缓存的周期清理由独立维护任务处理/)).toBeInTheDocument();
  expect(screen.getByText(/未发现引用不等于允许删除/)).toBeInTheDocument();
});

it("shows actual group counts, unknown coverage and bounded drift separately from assertions", async () => {
  mount();
  await expand();
  expect(screen.getByText("退休断言").parentElement).toHaveTextContent("8");
  expect(screen.getByText("物理对象组").parentElement).toHaveTextContent("6");
  expect(screen.getByText(/未知覆盖：训练快照 1 · 引用别名 2/)).toBeInTheDocument();
  fireEvent.click(screen.getByText("查看与上次成功核对的差异"));
  expect(screen.getByText("受引用保护：3 → 2（-1）")).toBeInTheDocument();
  expect(screen.getByText(/最近成功：/)).toBeInTheDocument();
});

it("shows unknown physical-operation coverage from the final backend metric names", async () => {
  state.unknown = {
    unknown_training_versions: 1,
    unknown_active_learning_rounds: 2,
    unknown_reference_aliases: 3,
    unknown_operations: 4,
  };
  mount();
  await expand();
  expect(
    screen.getByText(/未知覆盖：训练快照 1 · 主动学习历史 2 · 引用别名 3 · 未确认定位的活动读取 4/),
  ).toHaveTextContent("未知不能视为零引用");
});

it("starts an observation explicitly and does not duplicate or automatically reclaim", async () => {
  mount();
  await expand();
  let resolve!: (value: ReferenceReconciliationOut) => void;
  api.post.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  const button = screen.getByRole("button", { name: "立即核对" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(path);
  state.status = "pending";
  await act(async () => resolve(clone(state)));
  await screen.findByText("核对排队中");
  expect(button).toBeDisabled();
  expect(screen.getByText(/本次只观察数据库关系，不删除对象/)).toBeInTheDocument();
});

it("opens the existing shared task details for an active reconciliation", async () => {
  state.status = "running";
  mount();
  await expand();
  expect(screen.getByRole("button", { name: "立即核对" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "查看核对任务" }));
  expect(screen.getByRole("dialog", { name: "引用核对任务详情" })).toHaveTextContent(jobId);
});

it("keeps a failed run distinct from its older successful observation", async () => {
  state.status = "failed";
  state.error_code = "reconciliation_failed";
  mount();
  await expand();
  expect(screen.getByText("最近一轮失败")).toBeInTheDocument();
  expect(screen.getByText(/保留的旧结果不代表当前引用已经重新检查/)).toBeInTheDocument();
  expect(screen.getByText(/最近成功：/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "立即核对" })).not.toBeDisabled();
});

it("does not turn a missing first observation into a successful zero count", async () => {
  state = {
    status: "idle",
    async_job_id: null,
    measured_at: null,
    last_success_at: null,
    counts: {},
    unknown: {},
    deltas: [],
    error_code: null,
  };
  mount();
  await expand();
  expect(screen.getByText("尚未核对")).toBeInTheDocument();
  expect(screen.getByText(/尚无成功记录.*尚无完成观测/)).toBeInTheDocument();
  expect(screen.queryByText("退休断言")).not.toBeInTheDocument();
});

it("preserves old records on refresh failure while disabling new mutation", async () => {
  mount();
  await expand();
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新核对记录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("以下为上次记录");
  expect(screen.getByRole("button", { name: "立即核对" })).toBeDisabled();
});

it("distinguishes an accepted request from a failed subsequent refresh", async () => {
  mount();
  await expand();
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "立即核对" }));
  expect(await screen.findByText(/核对请求已受理，但刷新记录失败/)).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

it("hides all previous evidence and the task modal after authorization is revoked", async () => {
  const view = mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "查看核对任务" }));
  api.get.mockRejectedValueOnce(new ApiError(403, "revoked"));
  fireEvent.click(screen.getByRole("button", { name: "刷新核对记录" }));
  await waitFor(() => expect(view.denied).toHaveBeenCalled());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("退休断言")).not.toBeInTheDocument();
});

it("rejects invalid counts without granting an action or presenting them as zero", async () => {
  state.counts = { total_assertions: -1 };
  mount();
  await expand();
  expect(screen.getByRole("alert")).toHaveTextContent("无法读取有效的引用核对记录");
  expect(screen.getByRole("button", { name: "立即核对" })).toBeDisabled();
  expect(screen.queryByText("退休断言")).not.toBeInTheDocument();
});

it("retires a late manual request when the authentication session changes", async () => {
  const view = mount();
  await expand();
  let resolve!: (value: ReferenceReconciliationOut) => void;
  api.post.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent.click(screen.getByRole("button", { name: "立即核对" }));
  auth.revision++;
  view.refresh();
  const reads = api.get.mock.calls.length;
  await act(async () => resolve(result()));
  expect(screen.getByText("周期引用核对").closest("details")).not.toHaveAttribute("open");
  expect(api.get).toHaveBeenCalledTimes(reads);
  expect(screen.queryByText(/引用核对请求已受理/)).not.toBeInTheDocument();
});

it("allows explicit redispatch of a pending reference audit using the same existing task", async () => {
  state.status = "pending";
  mount();
  await expand();
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "立即核对" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "重新派发核对" }));
  await screen.findByText(/引用核对请求已受理/);
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(path);
  expect(screen.getByText(/同一任务，不创建新的核对轮次/)).toBeInTheDocument();
});
