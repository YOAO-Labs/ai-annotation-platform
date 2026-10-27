import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type {
  ManagedOriginalVerificationHistory,
  ManagedOriginalVerificationItem,
  ManagedOriginalVerificationStart,
} from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { ManagedOriginalVerificationPanel } from "./ManagedOriginalVerificationPanel";

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
  useAuthStore: (select: (state: unknown) => unknown) =>
    select({ sessionId: `session-${auth.revision}` }),
}));
vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({
  AsyncJobDetailModal: ({ jobId, onClose }: { jobId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="核验任务详情">
      <p>{jobId}</p>
      <button onClick={onClose}>关闭任务</button>
    </div>
  ),
}));

const id = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const jobId = "33333333-3333-4333-8333-333333333333";
const verificationId = "44444444-4444-4444-8444-444444444444";
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
let state: ManagedOriginalVerificationHistory;
const clients: ReturnType<typeof createTestQueryClient>[] = [];

function item(
  fields: Partial<ManagedOriginalVerificationItem> = {},
): ManagedOriginalVerificationItem {
  return {
    id: verificationId,
    async_job_id: jobId,
    status: "completed",
    outcome: "matched",
    error_code: null,
    requested_at: "2026-10-24T04:00:00Z",
    started_at: "2026-10-24T04:00:01Z",
    completed_at: "2026-10-24T04:00:03Z",
    cancel_requested_at: null,
    context_matches: true,
    unsettled_operation: false,
    expected_sha256: "a".repeat(64),
    expected_size_bytes: 1024,
    observed_sha256: "a".repeat(64),
    observed_size_bytes: 1024,
    ...fields,
  };
}
function result(): ManagedOriginalVerificationHistory {
  return {
    retirement_id: id,
    measured_at: "2026-10-24T05:00:00Z",
    scope: "registered_retirement_assertion_only",
    execution_permission: false,
    can_start: true,
    blockers: [],
    max_bytes: 5 * 1024 ** 3,
    timeout_seconds: 60,
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    has_more: false,
  };
}
function startResult(): ManagedOriginalVerificationStart {
  return {
    retirement_id: state.retirement_id,
    verification_id: verificationId,
    async_job_id: jobId,
    existing: false,
  };
}
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const denied = vi.fn();
  const tree = (target = id) => (
    <QueryClientProvider client={client}>
      <ManagedOriginalVerificationPanel retirementId={target} onDenied={denied} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { denied, client, refresh: (target = id) => view.rerender(tree(target)) };
}
async function expand() {
  fireEvent.click(screen.getByText("原件完整核验", { selector: "summary" }));
  await screen.findByRole("button", { name: "启动完整核验" });
}
function running(fields: Partial<ManagedOriginalVerificationItem> = {}) {
  state.can_start = false;
  state.blockers = ["verification_active"];
  state.items = [
    item({
      status: "running",
      outcome: null,
      completed_at: null,
      observed_sha256: null,
      observed_size_bytes: null,
      ...fields,
    }),
  ];
  state.total = 1;
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  state = result();
  api.get.mockImplementation(async () => clone(state));
  api.post.mockImplementation(async () => startResult());
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("loads only on disclosure and uses bounded history parameters without performing a probe", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  await expand();
  expect(api.get).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/verifications?limit=20&offset=0`,
    { signal: expect.any(AbortSignal) },
  );
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.getByText(/单次读取上限 5 GiB/)).toBeInTheDocument();
  expect(screen.getByText(/不证明独占所有权、最小副本或物理删除许可/)).toBeInTheDocument();
  expect(screen.getByText(/尚无核验历史/)).toBeInTheDocument();
});

it("starts explicitly with no payload and refreshes the durable history", async () => {
  const view = mount();
  await expand();
  api.post.mockImplementationOnce(async () => {
    running();
    return startResult();
  });
  fireEvent.click(screen.getByRole("button", { name: "启动完整核验" }));
  await screen.findByText("核验中", { selector: "h3" });
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/verifications`,
  );
  expect(screen.getByRole("button", { name: "启动完整核验" })).toBeDisabled();
  expect(
    view.client.getQueryState(["managed-original-verifications", "session-1", 1, id, 20, 0])?.data,
  ).toEqual(state);
});

it("does not duplicate a start while its first request remains unresolved", async () => {
  mount();
  await expand();
  let complete!: (value: ManagedOriginalVerificationStart) => void;
  api.post.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  const start = screen.getByRole("button", { name: "启动完整核验" });
  fireEvent.click(start);
  fireEvent.click(start);
  expect(api.post).toHaveBeenCalledTimes(1);
  await act(async () => complete(startResult()));
});

it("shows a failed dispatch as a reusable pending attempt and only redispatches explicitly", async () => {
  mount();
  await expand();
  api.post.mockImplementationOnce(async () => {
    running({ status: "pending", started_at: null });
    throw new ApiError(503, "dispatch failed");
  });
  fireEvent.click(screen.getByRole("button", { name: "启动完整核验" }));
  const redispatch = await screen.findByRole("button", { name: "重新派发核验" });
  expect(screen.getByText("排队中", { selector: "h3" })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  api.post.mockResolvedValueOnce({ ...startResult(), existing: true });
  fireEvent.click(redispatch);
  expect(await screen.findByText(/已复用已有核验请求/)).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(2);
});

it("does not redispatch when the active job is missing or its context has changed", async () => {
  running({ status: "pending", started_at: null, context_matches: false, async_job_id: null });
  state.blockers.push("verification_job_unavailable");
  mount();
  await expand();
  expect(screen.queryByRole("button", { name: "重新派发核验" })).not.toBeInTheDocument();
  expect(screen.getByText(/后台任务记录不可用/)).toBeInTheDocument();
  expect(screen.getByText(/后台任务记录已清理/)).toBeInTheDocument();
});

it("closes an active task and removes its actions when the job association becomes unavailable", async () => {
  running({ status: "pending", started_at: null });
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "查看核验任务" }));
  expect(screen.getByRole("dialog", { name: "核验任务详情" })).toBeInTheDocument();
  state.blockers.push("verification_job_unavailable");
  fireEvent.click(screen.getByRole("button", { name: "刷新核验历史" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "重新派发核验" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "请求取消核验" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看核验任务" })).not.toBeInTheDocument();
  expect(api.post).not.toHaveBeenCalled();
});

it("requests cancellation without calling a running read cancelled or permitting another start", async () => {
  running();
  mount();
  await expand();
  api.post.mockImplementationOnce(async () => {
    state.items[0].cancel_requested_at = "2026-10-24T05:01:00Z";
    return { id: jobId, status: "cancel_requested" };
  });
  fireEvent.click(screen.getByRole("button", { name: "请求取消核验" }));
  expect(
    await screen.findByText(/已请求取消，等待实际读取收尾；当前仍为核验中/),
  ).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(`/async-jobs/${jobId}/cancel`);
  expect(screen.getByRole("button", { name: "已请求取消" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "启动完整核验" })).toBeDisabled();
  expect(screen.queryByText("本次已取消")).not.toBeInTheDocument();
});

it("keeps a local cancellation request visible while the next history response is still running", async () => {
  running();
  mount();
  await expand();
  api.post.mockResolvedValueOnce({ id: jobId, status: "cancel_requested" });
  fireEvent.click(screen.getByRole("button", { name: "请求取消核验" }));
  await screen.findByText(/已请求取消，等待实际读取收尾/);
  expect(screen.getByRole("button", { name: "已请求取消" })).toBeDisabled();
});

it("retains unsettled physical responsibility even after the logical task failed", async () => {
  state.items = [item({ status: "failed", outcome: "failed", unsettled_operation: true })];
  state.total = 1;
  state.can_start = false;
  state.blockers = ["physical_operation_unsettled"];
  mount();
  await expand();
  expect(screen.getByText(/读取尚未确认收尾，不能视为已经停止/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "启动完整核验" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "重新派发核验" })).not.toBeInTheDocument();
});

it.each([
  ["missing", "本次未找到原件"],
  ["changed", "本次内容或定位已变化"],
  ["unverifiable", "本次无法核验"],
  ["failed", "本次核验失败"],
  ["cancelled", "本次已取消"],
] as const)("distinguishes %s as a dated observation", async (outcome, label) => {
  state.items = [
    item({
      outcome,
      status: outcome === "cancelled" ? "cancelled" : outcome === "failed" ? "failed" : "completed",
    }),
  ];
  state.total = 1;
  mount();
  await expand();
  expect(screen.getByText(label, { selector: "h3" })).toBeInTheDocument();
  expect(screen.getByText(/申请于.*观测于/)).toBeInTheDocument();
});

it("keeps a matched observation historical when the anchor context no longer matches", async () => {
  state.items = [item({ context_matches: false })];
  state.total = 1;
  mount();
  await expand();
  expect(screen.getByText("本次完整匹配")).toBeInTheDocument();
  expect(screen.getByText(/不适用于当前记录/)).toBeInTheDocument();
  expect(screen.getByText(/此后同键内容仍可能变化/)).toBeInTheDocument();
  fireEvent.click(screen.getByText("查看摘要与字节数", { selector: "summary" }));
  expect(screen.getByText("历史 SHA-256").nextElementSibling).toHaveTextContent("a".repeat(64));
});

it("distinguishes access denial from object absence and never exposes unknown error details", async () => {
  state.items = [
    item({ status: "failed", outcome: "failed", error_code: "managed_original_access_denied" }),
    item({
      id: otherId,
      status: "failed",
      outcome: "failed",
      error_code: "private-endpoint/private-key",
    }),
  ];
  state.total = 2;
  mount();
  await expand();
  expect(screen.getByText(/存储拒绝本次读取，不能据此判断原件缺失/)).toBeInTheDocument();
  expect(screen.getByText(/本次未取得完整且一致的内容证明/)).toBeInTheDocument();
  expect(screen.queryByText("private-endpoint/private-key")).not.toBeInTheDocument();
});

it("paginates on the server and resets the offset after changing the page size", async () => {
  state.total = 41;
  state.has_more = true;
  mount();
  await expand();
  const pager = screen.getByRole("navigation", { name: "原件核验历史分页" });
  state.can_start = false;
  state.blockers = ["verification_active"];
  fireEvent.click(within(pager).getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(api.get).toHaveBeenLastCalledWith(
      expect.stringContaining("limit=20&offset=20"),
      expect.anything(),
    ),
  );
  fireEvent.click(await screen.findByRole("button", { name: "查看最新核验" }));
  await waitFor(() =>
    expect(api.get).toHaveBeenLastCalledWith(
      expect.stringContaining("limit=20&offset=0"),
      expect.anything(),
    ),
  );
  fireEvent.change(await screen.findByRole("combobox", { name: "每页原件核验历史数" }), {
    target: { value: "10" },
  });
  await waitFor(() =>
    expect(api.get).toHaveBeenLastCalledWith(
      expect.stringContaining("limit=10&offset=0"),
      expect.anything(),
    ),
  );
});

it("retains dated history on read failure and disables all mutations until refreshed", async () => {
  running({ status: "pending", started_at: null });
  mount();
  await expand();
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新核验历史" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/以下为上次观测/);
  expect(screen.getByText(/历史状态观测于/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新派发核验" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "请求取消核验" })).toBeDisabled();
});

it.each([401, 403, 404])(
  "hides retained evidence and its job modal after read denial %s",
  async (status) => {
    state.items = [item()];
    state.total = 1;
    const view = mount();
    await expand();
    fireEvent.click(screen.getByRole("button", { name: "查看核验任务" }));
    expect(screen.getByRole("dialog", { name: "核验任务详情" })).toBeInTheDocument();
    api.get.mockRejectedValueOnce(new ApiError(status, "denied"));
    fireEvent.click(screen.getByRole("button", { name: "刷新核验历史" }));
    await waitFor(() => expect(view.denied).toHaveBeenCalled());
    expect(screen.queryByText("本次完整匹配")).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  },
);

it.each([401, 403, 404])("hides evidence after a denied mutation %s", async (status) => {
  state.items = [item()];
  state.total = 1;
  const view = mount();
  await expand();
  api.post.mockRejectedValueOnce(new ApiError(status, "denied"));
  fireEvent.click(screen.getByRole("button", { name: "启动完整核验" }));
  await waitFor(() => expect(view.denied).toHaveBeenCalled());
  expect(screen.queryByText("本次完整匹配")).not.toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent(/当前权限或记录已失效，已隐藏核验历史/);
});

it.each(["session", "record"])(
  "ignores a late start response after %s changes",
  async (transition) => {
    const view = mount();
    await expand();
    let complete!: (value: ManagedOriginalVerificationStart) => void;
    api.post.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "启动完整核验" }));
    const response = startResult();
    if (transition === "session") auth.revision += 1;
    else state.retirement_id = otherId;
    view.refresh(transition === "record" ? otherId : id);
    await expand();
    const reads = api.get.mock.calls.length;
    await act(async () => complete(response));
    expect(api.get).toHaveBeenCalledTimes(reads);
    expect(screen.queryByText(/请求已受理/)).not.toBeInTheDocument();
  },
);

it("rejects history belonging to another retirement without offering a start", async () => {
  state.retirement_id = otherId;
  mount();
  fireEvent.click(screen.getByText("原件完整核验", { selector: "summary" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/无法读取当前记录/);
  expect(screen.queryByRole("button", { name: "启动完整核验" })).not.toBeInTheDocument();
});

it("does not replace a new owner's history with a late prior read", async () => {
  const previous = { ...result(), items: [item()], total: 1 };
  let complete!: (value: ManagedOriginalVerificationHistory) => void;
  api.get.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  const view = mount();
  fireEvent.click(screen.getByText("原件完整核验", { selector: "summary" }));
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
  state.retirement_id = otherId;
  state.items = [item({ outcome: "changed" })];
  state.total = 1;
  view.refresh(otherId);
  await expand();
  await act(async () => complete(previous));
  expect(screen.getByText("本次内容或定位已变化")).toBeInTheDocument();
  expect(screen.queryByText("本次完整匹配")).not.toBeInTheDocument();
});

it("does not retry a conflicted mutation and refreshes the changed conditions", async () => {
  mount();
  await expand();
  api.post.mockImplementationOnce(async () => {
    state.can_start = false;
    state.blockers = ["platform_configuration_changed"];
    throw new ApiError(409, "changed");
  });
  fireEvent.click(screen.getByRole("button", { name: "启动完整核验" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/原件记录或配置已变化/);
  expect(await screen.findByText(/原件定位或平台配置已变化/)).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

it.each(["visible_item", "other_page"])(
  "polls only active visible history (%s) and stops after completion or disclosure closure",
  async (location) => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    running();
    if (location === "other_page") state.items = [];
    mount();
    fireEvent.click(screen.getByText("原件完整核验", { selector: "summary" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.get).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(api.get).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue("hidden");
    fireEvent(document, new Event("visibilitychange"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.get).toHaveBeenCalledTimes(2);
    visibility.mockReturnValue("visible");
    fireEvent(document, new Event("visibilitychange"));
    state.items = [item()];
    state.can_start = true;
    state.blockers = [];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5001);
    });
    expect(api.get).toHaveBeenCalledTimes(3);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.get).toHaveBeenCalledTimes(3);
    fireEvent.click(screen.getByText("原件完整核验", { selector: "summary" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(api.get).toHaveBeenCalledTimes(3);
  },
);
