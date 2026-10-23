import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import type { ManagedOriginalReclamationPreview } from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { ManagedOriginalReclamationPanel } from "./ManagedOriginalReclamationPanel";

const auth = vi.hoisted(() => ({ role: "super_admin", session: "one", revision: 1 }));
const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...(await original<typeof import("@/api/client")>()),
  apiClient: api,
}));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionRevision: () => auth.revision }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (state: unknown) => unknown) =>
    select({ sessionId: auth.session, user: { role: auth.role } }),
}));

function result(): ManagedOriginalReclamationPreview {
  return {
    measured_at: "2026-10-24T05:00:00Z",
    scope: "registered_managed_original_retirement_assertions",
    unit: "retirement_assertion",
    execution_permission: false,
    reclaimable_bytes: null,
    coverage: {
      historical_locators: "not_backfilled",
      unregistered_originals: "not_recorded",
      object_presence: "not_checked",
      exclusive_ownership: "not_established",
      backup_holds: "unknown",
      migration_holds: "unknown",
      declared_requirements: "registered_retirement_peers_only",
      training_snapshots: "recorded_supported_shapes",
      active_learning_roots: "recorded_members_and_source_snapshots",
    },
    counts: {
      total_assertions: 1,
      retained_assertions: 1,
      protected_assertions: 0,
      blocked_assertions: 0,
      policy_candidate_assertions: 0,
      unknown_size_assertions: 0,
      known_asserted_bytes: 1024,
      unknown_training_versions: 0,
      unknown_active_learning_rounds: 0,
    },
    items: [
      {
        id: "11111111-1111-4111-8111-111111111111",
        asset_id: "22222222-2222-4222-8222-222222222222",
        dataset_id: "33333333-3333-4333-8333-333333333333",
        dataset_item_id: "44444444-4444-4444-8444-444444444444",
        retired_at: "2026-10-24T04:00:00Z",
        policy_revision: 1,
        retain_until: null,
        effective_retain_until: null,
        indefinite_retention: true,
        peer_assertions: 1,
        declared_backup_holds: 0,
        declared_migration_holds: 0,
        size_bytes: 1024,
        identity_state: "current",
        status: "retained",
        policy_candidate: false,
        roots: {
          asset_locations: 0,
          dataset_items: 0,
          tasks: 0,
          task_dataset_links: 0,
          training_versions: 0,
          active_learning_roots: 0,
          ambiguous_legacy_tasks: 0,
        },
        blockers: [
          "indefinite_retention",
          "exclusive_ownership_unproven",
          "backup_holds_unknown",
          "migration_holds_unknown",
          "object_presence_not_checked",
        ],
      },
    ],
    limit: 20,
    offset: 0,
    has_more: false,
  };
}
const clients: ReturnType<typeof createTestQueryClient>[] = [];
function mount() {
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <ManagedOriginalReclamationPanel />
    </QueryClientProvider>
  );
  const view = render(tree());
  return { refresh: () => view.rerender(tree()) };
}
function expand() {
  fireEvent.click(screen.getByText("托管原件保留与回收预演", { selector: "summary" }));
}
async function loaded() {
  await screen.findByText("原件记录 11111111");
}
beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(auth, { role: "super_admin", session: "one", revision: 1 });
  api.get.mockResolvedValue(result());
});
afterEach(() => clients.splice(0).forEach((client) => client.clear()));

it("loads only after disclosure and uses read-only paginated HTTP parameters", async () => {
  mount();
  expect(api.get).not.toHaveBeenCalled();
  expand();
  await loaded();
  expect(api.get).toHaveBeenCalledTimes(1);
  expect(api.get.mock.calls[0][0]).toBe("/storage/managed-original-reclamation?limit=20&offset=0");
  expect(api.get.mock.calls[0][1].signal).toBeInstanceOf(AbortSignal);
  expect(screen.getByText(/大小会重复累计，不代表可释放容量/)).toBeInTheDocument();
  expect(screen.getByText(/当前没有物理删除许可/)).toBeInTheDocument();
  expect(screen.getByText(/历史未登记或已丢失的位置不在覆盖内/)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /确认.*回收|删除文件|提交回收/ }),
  ).not.toBeInTheDocument();
});

it.each(["employee", "project_admin", ""])("hides global evidence for role %s", (role) => {
  auth.role = role;
  mount();
  expect(screen.queryByText("托管原件保留与回收预演")).not.toBeInTheDocument();
  expect(api.get).not.toHaveBeenCalled();
});

it("uses a centered detail modal with honest identity/retention labels and restores focus on Escape", async () => {
  mount();
  expand();
  await loaded();
  const trigger = screen.getByRole("button", { name: "查看原件记录 11111111 详情" });
  trigger.focus();
  fireEvent.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "托管原件保留详情" });
  expect(within(dialog).getAllByText("无限期保护")).toHaveLength(2);
  expect(within(dialog).getByText(/当前配置匹配，仅为配置证据/)).toBeInTheDocument();
  expect(within(dialog).getByText("未登记备份要求的覆盖仍未知")).toBeInTheDocument();
  expect(within(dialog).getByText(result().items[0].asset_id)).toBeInTheDocument();
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
});

it("keeps unknown size unknown and presents malformed history as blocking evidence", async () => {
  const value = result();
  value.items[0].size_bytes = null;
  value.items[0].blockers.push(
    "unsupported_training_snapshot",
    "content_identity_unverified",
    "locator_unverified",
  );
  value.counts.unknown_size_assertions = 1;
  value.counts.unknown_training_versions = 2;
  api.get.mockResolvedValue(value);
  mount();
  expand();
  await loaded();
  expect(screen.getByText(/保留中 · 大小未知/)).toBeInTheDocument();
  expect(screen.getByText(/无法完整解析的训练快照 2 份/)).toBeInTheDocument();
  expect(screen.getByText(/内容身份尚未核验；原图定位尚未核验/)).toBeInTheDocument();
});

it("does not equate an empty registered scope with an empty bucket", async () => {
  const value = result();
  value.items = [];
  value.counts.total_assertions =
    value.counts.retained_assertions =
    value.counts.known_asserted_bytes =
      0;
  api.get.mockResolvedValue(value);
  mount();
  expand();
  expect(await screen.findByText(/此范围暂无托管原件保留记录/)).toHaveTextContent(
    "不代表存储中没有遗留原件或保留要求",
  );
});

it("preserves dated evidence on a failed refresh", async () => {
  mount();
  expand();
  await loaded();
  api.get.mockRejectedValue(new Error("offline"));
  fireEvent.click(screen.getByRole("button", { name: "刷新保留预演" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("刷新失败，以下保留上次观测及时间");
  expect(screen.getByText("原件记录 11111111")).toBeInTheDocument();
  expect(screen.getByText(/观测于/)).toBeInTheDocument();
});

it.each([401, 403, 404])("hides previous rows and detail evidence after %s", async (status) => {
  mount();
  expand();
  await loaded();
  api.get.mockRejectedValue(new ApiError(status, "denied"));
  fireEvent.click(screen.getByRole("button", { name: "刷新保留预演" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("已隐藏保留记录");
  expect(screen.queryByText("原件记录 11111111")).not.toBeInTheDocument();
});

it.each(["scope", "permission", "capacity"])(
  "rejects response with mismatched %s semantics",
  async (field) => {
    const value: any = result();
    if (field === "scope") value.scope = "all_objects";
    if (field === "permission") value.execution_permission = true;
    if (field === "capacity") value.reclaimable_bytes = 0;
    api.get.mockResolvedValue(value);
    mount();
    expand();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取符合范围的保留预演");
    expect(screen.queryByText("原件记录 11111111")).not.toBeInTheDocument();
  },
);

it("requests the next page and resets pagination when page size changes", async () => {
  const value = result();
  value.counts.total_assertions = 41;
  value.has_more = true;
  api.get.mockResolvedValue(value);
  mount();
  expand();
  await loaded();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(api.get.mock.calls.some(([path]) => path.endsWith("limit=20&offset=20"))).toBe(true),
  );
  await waitFor(() =>
    expect(screen.getByRole("combobox", { name: "每页托管原件保留记录数" })).not.toBeDisabled(),
  );
  fireEvent.change(screen.getByRole("combobox", { name: "每页托管原件保留记录数" }), {
    target: { value: "50" },
  });
  await waitFor(() =>
    expect(api.get.mock.calls.some(([path]) => path.endsWith("limit=50&offset=0"))).toBe(true),
  );
});

it("retires the old session, including a late response", async () => {
  let resolve!: (value: ManagedOriginalReclamationPreview) => void;
  api.get.mockReturnValueOnce(
    new Promise((done) => {
      resolve = done;
    }),
  );
  const view = mount();
  expand();
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(1));
  auth.session = "two";
  auth.revision++;
  view.refresh();
  expect(screen.getByText("托管原件保留与回收预演").closest("details")).not.toHaveAttribute("open");
  await act(async () => resolve(result()));
  expect(screen.queryByText("原件记录 11111111")).not.toBeInTheDocument();
});
