import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import {
  referenceReplicasApi as api,
  type ReferenceReplicaCandidate,
  type ReferenceReplicaOut,
} from "@/api/referenceReplicas";
import { useAuthStore } from "@/stores/authStore";
import type { MeResponse } from "@/api/auth";
import { ReferenceReplicas } from "./ReferenceReplicas";

vi.mock("@/api/referenceReplicas", () => ({
  referenceReplicasApi: {
    list: vi.fn(),
    candidates: vi.fn(),
    add: vi.fn(),
    update: vi.fn(),
    remove: vi.fn(),
  },
}));

const candidate: ReferenceReplicaCandidate = {
  item_id: "item-c",
  asset_id: "asset-c",
  source_id: "source-c",
  source_name: "华东备用目录",
  location_id: "location-c",
  location_name: "华东存储",
  location_kind: "agent",
  site_id: "site-east",
  site_name: "华东站点",
  agent_id: "agent-east",
  agent_state: "offline",
};
const replica: ReferenceReplicaOut = {
  id: "link-b",
  item_id: "item-a",
  replica_item_id: "item-b",
  replica_asset_id: "asset-b",
  source_id: "source-b",
  source_name: "华北备用目录",
  location_id: "location-b",
  location_name: "华北存储",
  location_kind: "filesystem",
  site_id: null,
  site_name: null,
  agent_id: null,
  agent_state: null,
  priority: 100,
  created_at: "2026-10-26T08:00:00Z",
};
const clients: QueryClient[] = [];
const actor = { id: "manager" } as MeResponse;
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function mount(itemId = "item-a", archived = false, onChanged = vi.fn(async () => undefined)) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  const view = render(
    <QueryClientProvider client={client}>
      <ReferenceReplicas itemId={itemId} archived={archived} onChanged={onChanged} />
    </QueryClientProvider>,
  );
  return {
    ...view,
    client,
    onChanged,
    change: (nextId: string, nextArchived = false) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <ReferenceReplicas itemId={nextId} archived={nextArchived} onChanged={onChanged} />
        </QueryClientProvider>,
      ),
  };
}
async function expand() {
  await userEvent.click(screen.getByText(/^备用来源/));
  await screen.findByText("华北备用目录");
}
async function showCandidates() {
  await expand();
  await userEvent.click(screen.getByRole("button", { name: "关联备用来源" }));
  return screen.findByRole("region", { name: "候选备用来源" });
}
beforeEach(() => {
  vi.resetAllMocks();
  useAuthStore.getState().setAuth("manager-token", actor);
  vi.mocked(api.list).mockResolvedValue({ items: [replica], total: 1 });
  vi.mocked(api.candidates).mockResolvedValue({ items: [candidate], total: 1 });
  vi.mocked(api.add).mockResolvedValue({
    ...replica,
    id: "link-c",
    replica_item_id: candidate.item_id,
  });
  vi.mocked(api.update).mockResolvedValue(replica);
  vi.mocked(api.remove).mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
  useAuthStore.getState().logout();
});

it("loads on disclosure and explains priority and independent sample identity", async () => {
  mount();
  expect(api.list).not.toHaveBeenCalled();
  await expand();
  expect(api.list).toHaveBeenCalledWith(
    "item-a",
    { limit: 10, offset: 0 },
    expect.any(AbortSignal),
  );
  expect(screen.getByText(/数字越小越优先/)).toBeVisible();
  expect(screen.getByText(/不会合并样本或标注/)).toBeVisible();
  expect(screen.getByText("华北存储 · 服务器目录")).toBeVisible();
  expect(api.candidates).not.toHaveBeenCalled();
});

it("pages linked sources on the server and retains rows during the request", async () => {
  const next = deferred<{ items: ReferenceReplicaOut[]; total: number }>();
  vi.mocked(api.list)
    .mockResolvedValueOnce({ items: [replica], total: 11 })
    .mockReturnValueOnce(next.promise);
  mount();
  await expand();
  const pager = screen.getByRole("navigation", { name: "备用来源分页" });
  await userEvent.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(screen.getByRole("button", { name: "解除关联" })).toBeDisabled();
  expect(api.list).toHaveBeenLastCalledWith(
    "item-a",
    { limit: 10, offset: 10 },
    expect.any(AbortSignal),
  );
  await act(async () =>
    next.resolve({ items: [{ ...replica, id: "next", source_name: "南区目录" }], total: 11 }),
  );
  expect(await screen.findByText("南区目录")).toBeVisible();
  expect(screen.queryByText("华北备用目录")).not.toBeInTheDocument();
});

it("pages candidates separately and presents a safe offline state without paths", async () => {
  vi.mocked(api.candidates).mockImplementation(async (_id, page) => ({
    items: page.offset ? [{ ...candidate, item_id: "next", source_name: "南区候选" }] : [candidate],
    total: 11,
  }));
  mount();
  const region = await showCandidates();
  expect(await within(region).findByText("华东备用目录")).toBeVisible();
  expect(within(region).getByText(/华东站点.*Agent 离线/)).toBeVisible();
  expect(within(region).getByText(/读取时仍会核对完整图片/)).toBeVisible();
  const pager = within(region).getByRole("navigation", { name: "候选来源分页" });
  await userEvent.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(await within(region).findByText("南区候选")).toBeVisible();
  expect(api.candidates).toHaveBeenLastCalledWith(
    "item-a",
    { limit: 10, offset: 10 },
    expect.any(AbortSignal),
  );
  expect(api.list).toHaveBeenCalledTimes(1);
});

it("adds an explicit priority and retains the linked list while saving", async () => {
  const added = deferred<ReferenceReplicaOut>();
  vi.mocked(api.add).mockReturnValueOnce(added.promise);
  const view = mount();
  const region = await showCandidates();
  await within(region).findByText("华东备用目录");
  const priority = within(region).getByRole("spinbutton", { name: "新增来源优先级" });
  await userEvent.clear(priority);
  await userEvent.type(priority, "50");
  await userEvent.click(within(region).getByRole("button", { name: "关联华东备用目录" }));
  expect(api.add).toHaveBeenCalledWith("item-a", "item-c", 50, expect.any(AbortSignal));
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(screen.getByRole("button", { name: "解除关联" })).toBeDisabled();
  vi.mocked(api.list).mockResolvedValue({
    items: [
      replica,
      {
        ...replica,
        id: "link-c",
        replica_item_id: "item-c",
        source_name: candidate.source_name,
        priority: 50,
      },
    ],
    total: 2,
  });
  vi.mocked(api.candidates).mockResolvedValue({ items: [], total: 0 });
  await act(async () => added.resolve(replica));
  await waitFor(() => expect(view.onChanged).toHaveBeenCalledTimes(1));
  expect(screen.getByText("备用来源已关联，样本保持独立。")).toBeVisible();
  expect(screen.getByText("暂无可关联的其他来源。")).toBeVisible();
  expect(screen.getByRole("spinbutton", { name: "华东备用目录优先级" })).toHaveValue(50);
});

it("validates candidate priority before dispatch and allows zero", async () => {
  mount();
  const region = await showCandidates();
  const add = await within(region).findByRole("button", { name: "关联华东备用目录" });
  const priority = within(region).getByRole("spinbutton", { name: "新增来源优先级" });
  await userEvent.clear(priority);
  expect(add).toBeDisabled();
  await userEvent.type(priority, "1001");
  expect(add).toBeDisabled();
  await userEvent.clear(priority);
  await userEvent.type(priority, "0");
  expect(add).toBeEnabled();
  await userEvent.click(add);
  await waitFor(() =>
    expect(api.add).toHaveBeenCalledWith("item-a", "item-c", 0, expect.any(AbortSignal)),
  );
});

it("keeps candidate and linked rows after a rejected association and supports refresh", async () => {
  vi.mocked(api.add).mockRejectedValueOnce(
    new ApiError(409, "failure", {
      code: "reference_replica_content_mismatch",
    }),
  );
  const view = mount();
  const region = await showCandidates();
  await userEvent.click(await within(region).findByRole("button", { name: "关联华东备用目录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "两张图片的内容记录不一致，无法关联。",
  );
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(within(region).getByText("华东备用目录")).toBeVisible();
  expect(view.onChanged).not.toHaveBeenCalled();
  const refreshed = deferred<{ items: ReferenceReplicaCandidate[]; total: number }>();
  vi.mocked(api.candidates).mockReturnValueOnce(refreshed.promise);
  await userEvent.click(screen.getByRole("button", { name: "刷新列表" }));
  await waitFor(() => expect(api.candidates).toHaveBeenCalledTimes(2));
  expect(api.list).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(within(region).getByText("华东备用目录")).toBeVisible();
  expect(within(region).getByRole("button", { name: "关联华东备用目录" })).toBeDisabled();
  await act(async () => refreshed.resolve({ items: [candidate], total: 1 }));
  await waitFor(() =>
    expect(within(region).getByRole("button", { name: "关联华东备用目录" })).toBeEnabled(),
  );
});

it("keeps a failed priority edit intact with a clear inline error and retry", async () => {
  vi.mocked(api.update).mockRejectedValueOnce(
    new ApiError(409, "failure", { code: "reference_replica_changed" }),
  );
  const view = mount();
  await expand();
  const priority = screen.getByRole("spinbutton", { name: "华北备用目录优先级" });
  await userEvent.clear(priority);
  await userEvent.type(priority, "25");
  await userEvent.click(screen.getByRole("button", { name: "保存优先级" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("备用来源关联已变化，请刷新后重试。");
  expect(priority).toHaveValue(25);
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(view.onChanged).not.toHaveBeenCalled();
  vi.mocked(api.list).mockResolvedValue({ items: [{ ...replica, priority: 25 }], total: 1 });
  await userEvent.click(screen.getByRole("button", { name: "保存优先级" }));
  await waitFor(() => expect(view.onChanged).toHaveBeenCalledTimes(1));
  expect(api.update).toHaveBeenLastCalledWith("item-a", "link-b", 25, expect.any(AbortSignal));
});

it("unlinks without deleting samples and refreshes the current list", async () => {
  const removed = deferred<void>();
  vi.mocked(api.remove).mockReturnValueOnce(removed.promise);
  const view = mount();
  await expand();
  await userEvent.click(screen.getByRole("button", { name: "解除关联" }));
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(api.remove).toHaveBeenCalledWith("item-a", "link-b", expect.any(AbortSignal));
  vi.mocked(api.list).mockResolvedValue({ items: [], total: 0 });
  await act(async () => removed.resolve());
  expect(await screen.findByText("尚未关联备用来源。")).toBeVisible();
  expect(screen.getByText("已解除备用来源关联。")).toBeVisible();
  await waitFor(() => expect(view.onChanged).toHaveBeenCalledTimes(1));
});

it("keeps archived datasets readable and omits all mutations", async () => {
  mount("item-a", true);
  await expand();
  expect(screen.getByText("数据集已归档，备用来源保持只读。")).toBeVisible();
  expect(screen.getByText("优先级：100")).toBeVisible();
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "关联备用来源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "解除关联" })).not.toBeInTheDocument();
  expect(api.candidates).not.toHaveBeenCalled();
});

it("switches to read-only when the server archives the dataset during editing", async () => {
  vi.mocked(api.update).mockRejectedValueOnce(
    new ApiError(409, "failure", { code: "dataset_archived" }),
  );
  mount();
  await expand();
  const priority = screen.getByRole("spinbutton", { name: "华北备用目录优先级" });
  await userEvent.clear(priority);
  await userEvent.type(priority, "50");
  await userEvent.click(screen.getByRole("button", { name: "保存优先级" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("数据集已归档，备用来源保持只读。");
  expect(screen.getByText("华北备用目录")).toBeVisible();
  expect(screen.queryByRole("button", { name: "解除关联" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "关联备用来源" })).not.toBeInTheDocument();
  expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "刷新列表" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByText("数据集已归档，备用来源保持只读。")).toBeVisible();
});

it.each(["item", "session", "archive"] as const)(
  "retires an in-flight write on %s change",
  async (change) => {
    const saved = deferred<ReferenceReplicaOut>();
    vi.mocked(api.add).mockReturnValueOnce(saved.promise);
    const view = mount();
    const region = await showCandidates();
    await userEvent.click(await within(region).findByRole("button", { name: "关联华东备用目录" }));
    const signal = vi.mocked(api.add).mock.calls[0][3]!;
    if (change === "item") view.change("item-new");
    if (change === "archive") view.change("item-a", true);
    if (change === "session") act(() => useAuthStore.getState().setAuth("new-token", actor));
    await waitFor(() => expect(signal.aborted).toBe(true));
    await act(async () => saved.resolve(replica));
    expect(view.onChanged).not.toHaveBeenCalled();
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("备用来源已关联，样本保持独立。")).not.toBeInTheDocument();
    expect(screen.getByText(/^备用来源/).closest("details")).not.toHaveAttribute("open");
  },
);

it("does not display a late list response after switching items", async () => {
  const old = deferred<{ items: ReferenceReplicaOut[]; total: number }>();
  vi.mocked(api.list)
    .mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce({ items: [], total: 0 });
  const view = mount();
  await userEvent.click(screen.getByText("备用来源"));
  await waitFor(() => expect(api.list).toHaveBeenCalledTimes(1));
  view.change("item-new");
  await userEvent.click(screen.getByText("备用来源"));
  expect(await screen.findByText("尚未关联备用来源。")).toBeVisible();
  await act(async () => old.resolve({ items: [replica], total: 1 }));
  expect(screen.queryByText("华北备用目录")).not.toBeInTheDocument();
});
