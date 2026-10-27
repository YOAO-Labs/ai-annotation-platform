import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type {
  ManagedOriginalReclaimHistory,
  ManagedOriginalReclaimItem,
  ManagedOriginalReclaimPreview,
  ManagedOriginalReclaimStart,
} from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { Modal } from "@/components/ui/Modal";
import { ManagedOriginalReclaimPanel } from "./ManagedOriginalReclaimPanel";

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
  AsyncJobDetailModal: ({ jobId, onClose }: { jobId: string; onClose: () => void }) => (
    <div role="dialog" aria-label="回收任务详情">
      <p>{jobId}</p>
      <button onClick={onClose}>关闭任务</button>
    </div>
  ),
}));

const id = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const attemptId = "33333333-3333-4333-8333-333333333333";
const jobId = "44444444-4444-4444-8444-444444444444";
const creationId = "55555555-5555-4555-8555-555555555555";
const locationId = "66666666-6666-4666-8666-666666666666";
const reason = "回收已经完成保留核对的准确版本";
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
let history: ManagedOriginalReclaimHistory;
let receipt: ManagedOriginalReclaimPreview;
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function item(fields: Partial<ManagedOriginalReclaimItem> = {}): ManagedOriginalReclaimItem {
  return {
    id: attemptId,
    async_job_id: jobId,
    retry_of_id: null,
    status: "failed",
    phase: "settled",
    outcome: "failed",
    error_code: "reclaim_read_failed",
    requested_at: "2026-10-26T04:00:00Z",
    started_at: "2026-10-26T04:00:01Z",
    completed_at: "2026-10-26T04:00:03Z",
    cancel_requested_at: null,
    expected_size_bytes: 1024,
    version_id: "exact-original-version",
    creation_id: creationId,
    location_id: locationId,
    delete_started: false,
    absence_proven: false,
    unsettled_operation: false,
    ...fields,
  };
}
function state(): ManagedOriginalReclaimHistory {
  return {
    retirement_id: id,
    measured_at: "2026-10-26T05:00:00Z",
    can_preview: true,
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
function preview(): ManagedOriginalReclaimPreview {
  return {
    retirement_id: id,
    measured_at: "2026-10-26T05:00:00Z",
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    eligible: true,
    blockers: [],
    impact_token: "review-token",
    expected_size_bytes: 1024,
    version_id: "exact-original-version",
    creation_id: creationId,
    location_id: locationId,
    irreversible: true,
  };
}
function start(): ManagedOriginalReclaimStart {
  return {
    retirement_id: history.retirement_id,
    reclaim_id: attemptId,
    async_job_id: jobId,
    existing: false,
  };
}
function mount(nested = false) {
  const client = createTestQueryClient();
  clients.push(client);
  const denied = vi.fn();
  const tree = (target = id, policyRevision = 1) => {
    const content = (
      <ManagedOriginalReclaimPanel
        retirementId={target}
        policyRevision={policyRevision}
        onDenied={denied}
      />
    );
    return (
      <QueryClientProvider client={client}>
        {nested ? (
          <Modal open onClose={vi.fn()} title="托管原件保留详情">
            {content}
          </Modal>
        ) : (
          content
        )}
      </QueryClientProvider>
    );
  };
  const view = render(tree());
  return {
    denied,
    refresh: (target = id, policyRevision = 1) => view.rerender(tree(target, policyRevision)),
  };
}
async function expand() {
  fireEvent.click(screen.getByText("受控回收", { selector: "summary" }));
  await waitFor(() => expect(api.get).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.queryByText("正在读取回收资格与历史…")).not.toBeInTheDocument(),
  );
}
async function review() {
  fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览准确版本回收" }));
  return screen.findByRole("dialog", { name: "确认准确版本回收" });
}
beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  history = state();
  receipt = preview();
  api.get.mockImplementation(async () => clone(history));
  api.post.mockImplementation(async (path: string) =>
    path.endsWith("reclaim-preview") ? clone(receipt) : start(),
  );
});
afterEach(() => {
  clients.splice(0).forEach((client) => client.clear());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

it("loads bounded history only after disclosure without probing or starting a reclaim", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  await expand();
  expect(api.get).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/reclaims?limit=20&offset=0`,
    { signal: expect.any(AbortSignal) },
  );
  expect(api.post).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "预览准确版本回收" })).toBeDisabled();
  expect(
    screen.getByText(/同键其他版本、外部原件、缓存和训练副本不在本次删除范围/),
  ).toBeInTheDocument();
});

it("requires a fresh exact-version preview and explicit irreversible confirmation before starting", async () => {
  mount();
  await expand();
  const dialog = await review();
  expect(api.post).toHaveBeenCalledTimes(1);
  expect(api.post).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/reclaim-preview`,
    { reason },
  );
  expect(within(dialog).getByText("exact-original-version")).toBeInTheDocument();
  const confirm = within(dialog).getByRole("button", { name: "确认不可撤销回收" });
  expect(confirm).toBeDisabled();
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(confirm);
  await screen.findByText(/准确版本回收请求已受理/);
  expect(api.post).toHaveBeenLastCalledWith(
    `/storage/managed-original-retirements/${id}/reclaims`,
    { reason, impact_token: "review-token" },
  );
});

it("can inspect a blocked fresh preview without creating a task", async () => {
  history.can_preview = false;
  history.blockers = ["retention_window"];
  receipt.eligible = false;
  receipt.blockers = ["retention_window"];
  receipt.impact_token = null;
  mount();
  await expand();
  fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
    target: { value: reason },
  });
  expect(screen.getByRole("button", { name: "预览准确版本回收" })).not.toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "预览准确版本回收" }));
  const dialog = await screen.findByRole("dialog", { name: "准确版本回收受阻" });
  expect(within(dialog).getByText(/未创建任务或发出删除/)).toBeInTheDocument();
  expect(
    within(dialog).queryByRole("button", { name: "确认不可撤销回收" }),
  ).not.toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

it.each(["expired", "wrong owner", "missing version"])(
  "does not confirm an %s preview",
  async (mode) => {
    if (mode === "expired") receipt.expires_at = "2000-01-01T00:00:00Z";
    if (mode === "wrong owner") receipt.retirement_id = otherId;
    if (mode === "missing version") receipt.version_id = null;
    mount();
    await expand();
    fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
      target: { value: reason },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览准确版本回收" }));
    if (mode === "expired") {
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByRole("checkbox"));
      expect(within(dialog).getByRole("button", { name: "确认不可撤销回收" })).toBeDisabled();
      expect(within(dialog).getByRole("alert")).toHaveTextContent("预演已过期");
    } else {
      await screen.findByRole("alert");
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    }
    expect(api.post).toHaveBeenCalledTimes(1);
  },
);

it("does not duplicate a confirmed start while the original request remains unresolved", async () => {
  mount();
  await expand();
  const dialog = await review();
  let resolve!: (value: ManagedOriginalReclaimStart) => void;
  api.post.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent.click(within(dialog).getByRole("checkbox"));
  const button = within(dialog).getByRole("button", { name: "确认不可撤销回收" });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(api.post).toHaveBeenCalledTimes(2);
  await act(async () => resolve(start()));
});

it("redispatches only the explicit existing pending attempt", async () => {
  history.items = [
    item({
      status: "pending",
      phase: "claimed",
      outcome: null,
      error_code: null,
      started_at: null,
      completed_at: null,
    }),
  ];
  history.total = 1;
  mount();
  await expand();
  expect(api.post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "重新派发回收" }));
  await waitFor(() =>
    expect(api.post).toHaveBeenCalledWith(
      `/storage/managed-original-reclaims/${attemptId}/redispatch`,
    ),
  );
  expect(api.post).toHaveBeenCalledTimes(1);
});

it("keeps cancellation distinct from an actual stopped or undeleted operation", async () => {
  history.items = [
    item({
      status: "running",
      phase: "delete_intent",
      outcome: null,
      error_code: null,
      delete_started: true,
      completed_at: null,
    }),
  ];
  history.total = 1;
  mount();
  await expand();
  api.post.mockResolvedValueOnce({ id: jobId, status: "cancel_requested" });
  fireEvent.click(screen.getByRole("button", { name: "请求取消回收" }));
  expect(
    await screen.findByText(/已请求取消，等待实际操作收尾；当前仍为回收中/),
  ).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledWith(`/async-jobs/${jobId}/cancel`);
  expect(screen.getByRole("button", { name: "已请求取消" })).toBeDisabled();
  expect(screen.getByText(/可能已经发出 DELETE/)).toBeInTheDocument();
  expect(screen.queryByText("本次已取消")).not.toBeInTheDocument();
});

it("retries a settled failed attempt through a new preview bound to that attempt", async () => {
  history.items = [item()];
  history.total = 1;
  mount();
  await expand();
  fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览重试此版本" }));
  const dialog = await screen.findByRole("dialog", { name: "确认准确版本回收" });
  expect(api.post).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/reclaim-preview`,
    { reason, retry_of_id: attemptId },
  );
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认不可撤销回收" }));
  await waitFor(() =>
    expect(api.post).toHaveBeenLastCalledWith(
      `/storage/managed-original-retirements/${id}/reclaims`,
      { reason, retry_of_id: attemptId, impact_token: "review-token" },
    ),
  );
});

it("preserves unknown deletion and unsettled responsibility without offering retry", async () => {
  history.can_preview = false;
  history.items = [
    item({
      status: "unknown",
      phase: "delete_ack",
      outcome: "unknown",
      delete_started: true,
      unsettled_operation: true,
    }),
  ];
  history.total = 1;
  mount();
  await expand();
  expect(screen.getByText(/实际操作尚未确认收尾/)).toBeInTheDocument();
  expect(screen.getByText(/迟到应答和物理责任需继续保留/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "预览重试此版本" })).not.toBeInTheDocument();
});

it.each(["deleted", "already_absent"] as const)(
  "does not retry or double count a successful %s version",
  async (outcome) => {
    history.items = [
      item({ status: "completed", outcome, absence_proven: true, delete_started: true }),
    ];
    history.total = 1;
    mount();
    await expand();
    expect(screen.getByText(/相关断言不会重复累计为多次释放容量/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "预览重试此版本" })).not.toBeInTheDocument();
  },
);

it("keeps durable history when the generic job has been removed", async () => {
  history.items = [item({ async_job_id: null })];
  history.total = 1;
  mount();
  await expand();
  expect(screen.getByText(/后台任务记录已清理，回收责任与历史仍保留/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "查看回收任务" })).not.toBeInTheDocument();
});

it("preserves dated data after refresh failure but disables mutation controls", async () => {
  mount();
  await expand();
  fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
    target: { value: reason },
  });
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新回收历史" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("刷新失败");
  expect(screen.getByRole("button", { name: "预览准确版本回收" })).toBeDisabled();
});

it("hides evidence and closes job details on permission revocation", async () => {
  history.items = [item()];
  history.total = 1;
  const view = mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "查看回收任务" }));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  api.get.mockRejectedValueOnce(new ApiError(403, "revoked"));
  fireEvent.click(screen.getByRole("button", { name: "刷新回收历史" }));
  await waitFor(() => expect(view.denied).toHaveBeenCalled());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("准确版本 exact-original-version")).not.toBeInTheDocument();
});

it.each(["status", "size"])(
  "rejects a malformed history %s before enabling actions",
  async (field) => {
    history.items = [item()];
    history.total = 1;
    const malformed = clone(history);
    if (field === "status") Object.assign(malformed.items[0], { status: "unexpected" });
    else malformed.items[0].expected_size_bytes = -1;
    api.get.mockResolvedValue(malformed);
    mount();
    await expand();
    expect(screen.getByRole("alert")).toHaveTextContent("无法读取符合当前记录范围");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  },
);

it("uses bounded history pagination and resets on page size changes", async () => {
  history.total = 41;
  history.has_more = true;
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(api.get).toHaveBeenCalledWith(
      expect.stringContaining("limit=20&offset=20"),
      expect.anything(),
    ),
  );
  await waitFor(() => expect(screen.getByRole("combobox")).not.toBeDisabled());
  fireEvent.change(screen.getByRole("combobox"), { target: { value: "50" } });
  await waitFor(() =>
    expect(api.get).toHaveBeenCalledWith(
      expect.stringContaining("limit=50&offset=0"),
      expect.anything(),
    ),
  );
});

it.each(["record", "session", "policy"])(
  "retires a late preview after the %s owner changes",
  async (owner) => {
    const view = mount();
    await expand();
    let resolve!: (value: ManagedOriginalReclaimPreview) => void;
    api.post.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
      target: { value: reason },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览准确版本回收" }));
    if (owner === "session") auth.revision++;
    view.refresh(owner === "record" ? otherId : id, owner === "policy" ? 2 : 1);
    await act(async () => resolve(preview()));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByText("受控回收").closest("details")).not.toHaveAttribute("open");
  },
);

it("scopes Escape to the confirmation and restores the trigger inside the original detail", async () => {
  mount(true);
  await expand();
  const dialog = await review();
  expect(dialog).toBeInTheDocument();
  fireEvent.keyDown(dialog, { key: "Escape" });
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "确认准确版本回收" })).not.toBeInTheDocument(),
  );
  expect(screen.getByRole("dialog", { name: "托管原件保留详情" })).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "预览准确版本回收" })).toHaveFocus(),
  );
});

it("translates precise reference, Gate and immutable-version blockers without exposing internal codes", async () => {
  receipt.eligible = false;
  receipt.impact_token = null;
  receipt.blockers = [
    "training_versions",
    "reclaim_location_not_stopped",
    "reclaim_immutable_version_required",
    "unknown_operations",
  ];
  mount();
  await expand();
  fireEvent.change(screen.getByRole("textbox", { name: "回收原因" }), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览准确版本回收" }));
  const dialog = await screen.findByRole("dialog", { name: "准确版本回收受阻" });
  expect(within(dialog).getByText(/仍有保留的训练版本引用/)).toBeInTheDocument();
  expect(within(dialog).getByText(/请先完成对应位置或迁移窗口的维护停止/)).toBeInTheDocument();
  expect(within(dialog).getByText(/未保存准确且非空的 VersionId/)).toBeInTheDocument();
  expect(within(dialog).getByText(/不能视为已经排空/)).toBeInTheDocument();
  expect(dialog).not.toHaveTextContent("unknown_operations");
});

it("ignores a late accepted start after the selected retirement changes", async () => {
  const view = mount();
  await expand();
  const dialog = await review();
  let resolve!: (value: ManagedOriginalReclaimStart) => void;
  api.post.mockImplementationOnce(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认不可撤销回收" }));
  view.refresh(otherId);
  const reads = api.get.mock.calls.length;
  await act(async () => resolve(start()));
  expect(api.get).toHaveBeenCalledTimes(reads);
  expect(screen.queryByText(/准确版本回收请求已受理/)).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("distinguishes an accepted reclaim from failed history refresh without automatically retrying", async () => {
  mount();
  await expand();
  const dialog = await review();
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认不可撤销回收" }));
  expect(await screen.findByText(/请求已受理，但刷新回收历史失败/)).toBeInTheDocument();
  expect(api.get).toHaveBeenCalledTimes(2);
  expect(api.post).toHaveBeenCalledTimes(2);
});
