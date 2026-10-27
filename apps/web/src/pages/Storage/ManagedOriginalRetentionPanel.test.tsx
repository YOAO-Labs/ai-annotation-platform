import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type {
  ManagedOriginalRetentionChange,
  ManagedOriginalRetentionHold,
  ManagedOriginalRetentionPreview,
  ManagedOriginalRetentionState,
} from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { createTestQueryClient } from "@/test/queryClient";
import { ManagedOriginalRetentionPanel } from "./ManagedOriginalRetentionPanel";

const auth = vi.hoisted(() => ({ revision: 1 }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const toast = vi.hoisted(() => vi.fn());
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
vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (select: (state: unknown) => unknown) => select({ push: toast }),
}));

const id = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
let state: ManagedOriginalRetentionState;
const clients: ReturnType<typeof createTestQueryClient>[] = [];

function hold(fields: Partial<ManagedOriginalRetentionHold> = {}): ManagedOriginalRetentionHold {
  return {
    id: "33333333-3333-4333-8333-333333333333",
    retirement_id: id,
    kind: "backup",
    reference_code: "backup-20261024",
    reason: "Preserve the original for a recovery generation",
    created_by: "44444444-4444-4444-8444-444444444444",
    created_at: "2026-10-24T00:00:00Z",
    revision: 1,
    released_at: null,
    released_by: null,
    release_reason: null,
    ...fields,
  };
}

function result(): ManagedOriginalRetentionState {
  return {
    retirement_id: id,
    policy_revision: 1,
    measured_at: "2026-10-24T05:00:00Z",
    summary: {
      retain_until: null,
      effective_retain_until: null,
      indefinite_retention: true,
      peer_assertions: 1,
      backup_holds: 0,
      migration_holds: 0,
    },
    holds: [],
    total_holds: 0,
    limit: 20,
    offset: 0,
    has_more: false,
    coverage: "declared_requirements_only",
    execution_permission: false,
  };
}

function receipt(payload: ManagedOriginalRetentionChange): ManagedOriginalRetentionPreview {
  const after = clone(state.summary);
  if (payload.action === "set_retention") {
    after.retain_until = payload.retain_until ?? null;
    if (after.peer_assertions === 1) {
      after.effective_retain_until = after.retain_until;
      after.indefinite_retention = after.retain_until === null;
    }
  }
  if (payload.action === "add_hold")
    after[payload.hold_kind === "backup" ? "backup_holds" : "migration_holds"] += 1;
  const target = state.holds.find((h) => h.id === payload.hold_id);
  if (payload.action === "release_hold")
    after[target?.kind === "backup" ? "backup_holds" : "migration_holds"] -= 1;
  return {
    retirement_id: state.retirement_id,
    policy_revision: state.policy_revision,
    action: payload.action,
    measured_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
    impact_token: "reviewed-retention-token",
    before: clone(state.summary),
    after,
    shortens_retention: payload.action === "set_retention" && payload.retain_until !== null,
    hold_id: payload.hold_id ?? null,
    hold_kind: payload.hold_kind ?? target?.kind ?? null,
    reference_code: payload.reference_code ?? target?.reference_code ?? null,
    coverage: "declared_requirements_only",
    execution_permission: false,
  };
}

function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const changed = vi.fn().mockResolvedValue(undefined);
  const denied = vi.fn();
  const tree = (target = id) => (
    <QueryClientProvider client={client}>
      <ManagedOriginalRetentionPanel retirementId={target} onChanged={changed} onDenied={denied} />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { changed, denied, refresh: (target = id) => view.rerender(tree(target)) };
}
async function expand() {
  fireEvent.click(screen.getByText("保留设置与要求", { selector: "summary" }));
  await screen.findByRole("button", { name: "调整本条期限" });
}
function policy() {
  fireEvent.click(screen.getByRole("button", { name: "调整本条期限" }));
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/ }), {
    target: { value: "An explicit reviewed retention decision" },
  });
}
async function preview() {
  fireEvent.click(screen.getByRole("button", { name: "预览保留变更" }));
  return await screen.findByRole("dialog", { name: "确认原件保留变更" });
}
function futureDate() {
  const value = new Date(Date.now() + 2 * 86400000);
  return new Date(value.getTime() - value.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.revision = 1;
  state = result();
  api.get.mockImplementation(async () => clone(state));
  api.post.mockImplementation(async (path: string, payload: ManagedOriginalRetentionChange) =>
    path.endsWith("-preview") ? receipt(payload) : clone(state),
  );
});
afterEach(() => clients.splice(0).forEach((client) => client.clear()));

it("loads bounded history only on disclosure and does not describe empty declarations as complete coverage", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  await expand();
  expect(api.get).toHaveBeenCalledWith(
    `/storage/managed-original-retirements/${id}/retention?limit=20&offset=0`,
    { signal: expect.any(AbortSignal) },
  );
  expect(screen.getByText(/未登记备份或迁移的覆盖仍未知/)).toBeInTheDocument();
  expect(screen.getByText(/不能证明备份可恢复或约束其他清理器/)).toBeInTheDocument();
});

it("keeps a finite own policy distinct from indefinite related protection", async () => {
  state.summary.retain_until = new Date(Date.now() + 86400000).toISOString();
  state.summary.peer_assertions = 2;
  mount();
  await expand();
  expect(screen.getByText("本条保留期限").nextElementSibling).not.toHaveTextContent("无限期保护");
  expect(screen.getByText("相关记录共同保护至").nextElementSibling).toHaveTextContent("无限期保护");
});

it("previews a timezone-aware shortening, keeps peer protection visible, and requires confirmation before apply", async () => {
  state.summary.peer_assertions = 2;
  const view = mount();
  await expand();
  policy();
  fireEvent.change(screen.getByRole("combobox", { name: /保留方式/ }), {
    target: { value: "deadline" },
  });
  fireEvent.change(screen.getByLabelText(/^保留截止时间/), { target: { value: futureDate() } });
  const dialog = await preview();
  expect(within(dialog).getByText(/本次将缩短本条保留期限/)).toBeInTheDocument();
  const body = api.post.mock.calls[0][1];
  expect(body.retain_until).toMatch(/Z$/);
  expect(body.expected_revision).toBe(1);
  expect(api.post.mock.calls.every(([path]) => path.endsWith("-preview"))).toBe(true);
  fireEvent.click(within(dialog).getByRole("button", { name: "确认更新保留要求" }));
  await waitFor(() => expect(view.changed).toHaveBeenCalledOnce());
  expect(api.post).toHaveBeenLastCalledWith(
    `/storage/managed-original-retirements/${id}/retention`,
    { ...body, impact_token: "reviewed-retention-token" },
  );
  expect(toast).toHaveBeenCalledWith({ msg: "原件保留要求已更新", kind: "success" });
});

it("requires a nonblank reason and a future deadline", async () => {
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "调整本条期限" }));
  expect(screen.getByRole("button", { name: "预览保留变更" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/ }), { target: { value: "  " } });
  expect(screen.getByRole("button", { name: "预览保留变更" })).toBeDisabled();
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/ }), {
    target: { value: "Review" },
  });
  fireEvent.change(screen.getByRole("combobox", { name: /保留方式/ }), {
    target: { value: "deadline" },
  });
  fireEvent.change(screen.getByLabelText(/^保留截止时间/), {
    target: { value: "2020-01-01T10:00" },
  });
  expect(screen.getByRole("button", { name: "预览保留变更" })).toBeDisabled();
  expect(api.post).not.toHaveBeenCalled();
});

it("cancels only the confirmation and restores preview focus", async () => {
  mount();
  await expand();
  policy();
  const button = screen.getByRole("button", { name: "预览保留变更" });
  button.focus();
  await preview();
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(button).toHaveFocus());
  expect(screen.getByRole("textbox", { name: /^变更原因/ })).toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(1);
});

it("invalidates a confirmation if its candidate changes", async () => {
  mount();
  await expand();
  policy();
  await preview();
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/, hidden: true }), {
    target: { value: "A changed decision" },
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(api.post).toHaveBeenCalledTimes(1);
});

it("keeps select explanations outside options and retains the same create identity on retry", async () => {
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "登记保留要求" }));
  const select = screen.getByRole("combobox", { name: /要求类型/ });
  expect(
    within(select)
      .getAllByRole("option")
      .map((o) => o.textContent),
  ).toEqual(["备份保留", "迁移观察"]);
  const description = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(description).toHaveClass("text-muted-foreground");
  expect(select).not.toContainElement(description);
  fireEvent.change(select, { target: { value: "migration" } });
  fireEvent.change(screen.getByRole("textbox", { name: /^引用编号/ }), {
    target: { value: "migration:local-1" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/ }), {
    target: { value: "Observe migration before releasing the original" },
  });
  api.post.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "预览保留变更" }));
  await screen.findByRole("alert");
  await preview();
  expect(api.post.mock.calls[0][1].hold_id).toBe(api.post.mock.calls[1][1].hold_id);
  expect(api.post.mock.calls[1][1]).toMatchObject({
    action: "add_hold",
    hold_kind: "migration",
    reference_code: "migration:local-1",
  });
});

it("does not accept URLs as hold reference codes", async () => {
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "登记保留要求" }));
  fireEvent.change(screen.getByRole("textbox", { name: /^引用编号/ }), {
    target: { value: "https://private.example/backup" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: /^变更原因/ }), {
    target: { value: "Review" },
  });
  expect(screen.getByRole("button", { name: "预览保留变更" })).toBeDisabled();
});

it("releases only an own-anchor hold with its revision and independent reason", async () => {
  state.holds = [hold()];
  state.total_holds = 1;
  state.summary.backup_holds = 1;
  mount();
  await expand();
  fireEvent.click(screen.getByRole("button", { name: "解除 backup-20261024" }));
  fireEvent.change(screen.getByRole("textbox", { name: /^解除原因/ }), {
    target: { value: "Recovery generation retired after review" },
  });
  const dialog = await preview();
  expect(api.post.mock.calls[0][1]).toEqual({
    action: "release_hold",
    expected_revision: 1,
    hold_id: hold().id,
    expected_hold_revision: 1,
    reason: "Recovery generation retired after review",
  });
  expect(within(dialog).getByText(/其他要求和期限保持各自保护/)).toBeInTheDocument();
  expect(api.post.mock.calls[0][1]).not.toHaveProperty("retain_until");
});

it("navigates to a foreign hold anchor before offering its release", async () => {
  state.holds = [hold({ retirement_id: otherId })];
  state.total_holds = 1;
  state.summary.peer_assertions = 2;
  mount();
  await expand();
  expect(screen.queryByRole("button", { name: "解除 backup-20261024" })).not.toBeInTheDocument();
  api.get.mockImplementation(async () => ({ ...clone(state), retirement_id: otherId }));
  fireEvent.click(screen.getByRole("button", { name: "打开登记记录" }));
  await screen.findByRole("button", { name: "解除 backup-20261024" });
  expect(api.get).toHaveBeenLastCalledWith(
    expect.stringContaining(`/${otherId}/retention?`),
    expect.anything(),
  );
  expect(screen.getByRole("button", { name: "返回原记录的设置" })).toBeInTheDocument();
});

it("shows released history without a release action", async () => {
  state.holds = [
    hold({
      revision: 2,
      released_at: "2026-10-24T06:00:00Z",
      released_by: hold().created_by,
      release_reason: "Reviewed release",
    }),
  ];
  state.total_holds = 1;
  mount();
  await expand();
  expect(screen.getByText(/解除原因：Reviewed release/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "解除 backup-20261024" })).not.toBeInTheDocument();
});

it("rejects stale confirmation rather than automatically repeating a mutation", async () => {
  mount();
  await expand();
  policy();
  const dialog = await preview();
  api.post.mockRejectedValueOnce(new ApiError(409, "stale"));
  fireEvent.click(within(dialog).getByRole("button", { name: "确认更新保留要求" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/请刷新后重新预览/);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(api.post).toHaveBeenCalledTimes(2);
});

it.each([401, 403, 404])(
  "hides the editor and notifies its owner after a denied mutation (%s)",
  async (status) => {
    const view = mount();
    await expand();
    policy();
    api.post.mockRejectedValueOnce(new ApiError(status, "denied"));
    fireEvent.click(screen.getByRole("button", { name: "预览保留变更" }));
    await waitFor(() => expect(view.denied).toHaveBeenCalled());
    expect(screen.queryByRole("textbox", { name: /^变更原因/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  },
);

it("preserves dated evidence on a failed read refresh and disables changes", async () => {
  mount();
  await expand();
  api.get.mockRejectedValueOnce(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新保留设置" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(/上次观测/);
  expect(screen.getByText(/观测于/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "调整本条期限" })).toBeDisabled();
});

it("hides stale evidence when read authority is revoked", async () => {
  const view = mount();
  await expand();
  api.get.mockRejectedValueOnce(new ApiError(403, "denied"));
  fireEvent.click(screen.getByRole("button", { name: "刷新保留设置" }));
  await waitFor(() => expect(view.denied).toHaveBeenCalled());
  expect(screen.queryByText("本条保留期限")).not.toBeInTheDocument();
});

it.each(["session", "record"])(
  "ignores late preview responses after changing %s",
  async (transition) => {
    const view = mount();
    await expand();
    policy();
    let complete!: (value: ManagedOriginalRetentionPreview) => void;
    api.post.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    fireEvent.click(screen.getByRole("button", { name: "预览保留变更" }));
    const payload = api.post.mock.calls[0][1];
    const oldReceipt = receipt(payload);
    if (transition === "session") auth.revision += 1;
    view.refresh(transition === "record" ? otherId : id);
    await act(async () => complete(oldReceipt));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /^变更原因/ })).not.toBeInTheDocument();
  },
);

it("rejects a preview response owned by another assertion", async () => {
  mount();
  await expand();
  policy();
  api.post.mockImplementationOnce(async (_path, payload) => ({
    ...receipt(payload),
    retirement_id: otherId,
  }));
  fireEvent.click(screen.getByRole("button", { name: "预览保留变更" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("prevents confirmation of an expired preview", async () => {
  mount();
  await expand();
  policy();
  api.post.mockImplementationOnce(async (_path, payload) => ({
    ...receipt(payload),
    expires_at: new Date(Date.now() - 1).toISOString(),
  }));
  const dialog = await preview();
  expect(within(dialog).getByRole("button", { name: "确认更新保留要求" })).toBeDisabled();
  expect(within(dialog).getByText(/预览已过期/)).toBeInTheDocument();
});

it("uses real bounded history pagination instead of downloading all declarations", async () => {
  state.total_holds = 21;
  state.has_more = true;
  mount();
  await expand();
  const pager = screen.getByRole("navigation", { name: "原件保留要求历史分页" });
  fireEvent.click(within(pager).getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(api.get).toHaveBeenLastCalledWith(
      expect.stringContaining("limit=20&offset=20"),
      expect.anything(),
    ),
  );
});
