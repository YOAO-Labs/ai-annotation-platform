import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { createTestQueryClient } from "@/test/queryClient";
import { StorageLocationsConsole } from "./StorageLocationsConsole";

const state = vi.hoisted(() => ({ role: "super_admin", session: "session", revision: 1 }));
const api = vi.hoisted(() => ({ locationPage: vi.fn(), location: vi.fn(), probe: vi.fn() }));
const storage = vi.hoisted(() => ({ bindings: vi.fn(), probes: vi.fn() }));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/api/storage", () => ({ storageApi: storage }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: state.role }) }));
vi.mock("@/hooks/useSessionGuard", () => ({
  useSessionRevision: () => state.revision,
  useSessionGuard: () => () => () => true,
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "user" }, sessionId: state.session }),
}));
vi.mock("./FilesystemLocationsPanel", () => ({
  FilesystemLocationsPanel: ({ locationId }: { locationId?: string }) => (
    <output data-testid="filesystem">{locationId ?? "create"}</output>
  ),
}));
vi.mock("./StorageConnectionsPanel", () => ({
  StorageConnectionsPanel: ({
    connectionId,
    allowManage,
  }: {
    connectionId?: string;
    allowManage?: boolean;
  }) => (
    <output data-testid="connection">
      {connectionId}/{String(allowManage)}
    </output>
  ),
}));
vi.mock("./StorageLocationSourceSummary", () => ({
  StorageLocationSourceSummary: ({
    locationId,
    returnTo,
  }: {
    locationId: string;
    returnTo?: string;
  }) => (
    <>
      <output data-testid="summary">{locationId}</output>
      <output data-testid="summary-return">{returnTo}</output>
    </>
  ),
}));
const ID = "11111111-1111-1111-1111-111111111111";
const location = {
  id: ID,
  name: "Company archive",
  kind: "filesystem",
  enabled: false,
  config_revision: 3,
  connection_id: null,
  declared_capabilities: { read: true, write: false, reference: true },
  allowed_actions: ["read", "manage_location"],
};
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
function RouterState() {
  const location = useLocation();
  return (
    <output data-testid="url">
      {location.pathname}
      {location.search}
    </output>
  );
}
function mount(url = `/storage?location=${ID}`) {
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <StorageLocationsConsole />
        <RouterState />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree());
  return { ...view, client, refresh: () => view.rerender(tree()) };
}
beforeEach(() => {
  vi.clearAllMocks();
  state.role = "super_admin";
  state.session = "session";
  state.revision = 1;
  api.locationPage.mockResolvedValue({ items: [location], total: 21 });
  api.location.mockResolvedValue(location);
  storage.probes.mockResolvedValue([
    {
      node_id: "api:node",
      config_revision: 3,
      observed_at: "2026-10-01T00:00:00Z",
      expires_at: "2026-10-01T00:05:00Z",
      ok: true,
      code: "ok",
      capabilities: { read: "verified", list: "verified" },
    },
  ]);
  api.probe.mockResolvedValue({ ok: true });
  storage.bindings.mockResolvedValue([
    { purpose: "datasets", location_id: ID, bucket: "shared", generation: 1, state: "configured" },
    { purpose: "export", location_id: ID, bucket: "shared", generation: 1, state: "configured" },
    { purpose: "training", location_id: ID, bucket: "training", generation: 2, state: "mismatch" },
  ]);
});
it("loads exact URL detail, separates disabled state from expired node health and preserves unknown metrics", async () => {
  mount();
  expect(await screen.findByRole("heading", { name: "Company archive" })).toBeInTheDocument();
  expect(api.location).toHaveBeenCalledWith(ID, expect.any(AbortSignal));
  expect(await screen.findByText(/api:node · 已过期/)).toBeInTheDocument();
  expect(screen.getByText("读取：支持")).toBeInTheDocument();
  expect(screen.getByText("写入：不支持")).toBeInTheDocument();
  expect(screen.getAllByText("未提供")).toHaveLength(2);
  expect(screen.queryByText("待统计")).not.toBeInTheDocument();
  expect(screen.getByTestId("filesystem")).toHaveTextContent(ID);
  expect(screen.getByTestId("summary")).toHaveTextContent(ID);
  expect(storage.bindings).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name: "存储位置详情" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByTestId("url")).not.toHaveTextContent("location=");
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "存储位置分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  await waitFor(() =>
    expect(api.locationPage).toHaveBeenLastCalledWith(20, expect.any(AbortSignal), 20),
  );
});
it("shows all nine purposes, keeps shared buckets separate and identifies missing/mismatched bindings", async () => {
  mount("/storage?tab=bindings");
  await screen.findByText("配置不匹配");
  const rows = within(screen.getByRole("table")).getAllByRole("row");
  expect(rows).toHaveLength(10);
  expect(screen.getAllByText("shared")).toHaveLength(2);
  expect(screen.getByText("annotations")).toBeInTheDocument();
  expect(screen.getByText("avatars")).toBeInTheDocument();
  expect(screen.getAllByText("未配置").length).toBeGreaterThan(0);
  expect(api.locationPage).not.toHaveBeenCalled();
});
it("only exposes safe dataset return links and does not query global bindings for managers", async () => {
  state.role = "project_admin";
  mount("/storage?tab=bindings&returnTo=https://evil.test");
  expect(screen.getByText("全局用途映射仅超级管理员可查看。")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /返回数据集/ })).not.toBeInTheDocument();
  expect(storage.bindings).not.toHaveBeenCalled();
});
it("retains old probe values on refresh errors and retires old-session detail", async () => {
  const view = mount();
  await screen.findByText(/api:node · 已过期/);
  storage.probes.mockRejectedValue(new Error("offline"));
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["storage-location-probes"] });
  });
  expect(await screen.findByText(/刷新失败，保留上次探测记录/)).toBeInTheDocument();
  expect(screen.getByText(/api:node · 已过期/)).toBeInTheDocument();
  let resolve!: (value: unknown) => void;
  api.location.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  state.session = "new-session";
  state.revision++;
  view.refresh();
  expect(screen.queryByRole("heading", { name: "Company archive" })).not.toBeInTheDocument();
  await act(async () => {
    resolve({ ...location, name: "New session location" });
  });
  expect(await screen.findByRole("heading", { name: "New session location" })).toBeInTheDocument();
});
it("does not read locations or detail for users without management access", () => {
  state.role = "viewer";
  mount();
  expect(api.locationPage).not.toHaveBeenCalled();
  expect(api.location).not.toHaveBeenCalled();
  expect(storage.probes).not.toHaveBeenCalled();
});

it("probes only the selected authorized location and refreshes persisted node observations", async () => {
  api.location.mockResolvedValue({
    ...location,
    enabled: true,
    allowed_actions: ["read", "probe"],
  });
  mount();
  fireEvent.change(await screen.findByRole("textbox", { name: "探测节点" }), {
    target: { value: "worker-one" },
  });
  fireEvent.change(screen.getByRole("textbox", { name: "可选样例文件" }), {
    target: { value: "sample.png" },
  });
  fireEvent.click(screen.getByRole("button", { name: "探测此位置" }));
  await waitFor(() => expect(api.probe).toHaveBeenCalledWith(ID, "worker-one", "sample.png"));
  await waitFor(() => expect(storage.probes.mock.calls.length).toBeGreaterThan(1));
  expect(screen.queryByTestId("filesystem")).not.toBeInTheDocument();
});

it("hides retained configuration when exact location access is revoked", async () => {
  const view = mount();
  await screen.findByRole("heading", { name: "Company archive" });
  api.location.mockRejectedValue(Object.assign(new Error("Access revoked"), { status: 404 }));
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["storage-location"] });
  });
  await screen.findByRole("alert");
  expect(screen.queryByRole("heading", { name: "Company archive" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("filesystem")).not.toBeInTheDocument();
  expect(screen.queryByTestId("summary")).not.toBeInTheDocument();
});

it("keeps the original dataset return while removing nested return links from new source navigation", async () => {
  const datasetReturn = `/datasets/${ID}?tab=sources&source=source&scan=scan&page=3`;
  mount(`/storage?${new URLSearchParams({ location: ID, returnTo: datasetReturn })}`);
  await screen.findByTestId("summary");
  expect(screen.getByRole("link", { name: "返回数据集工作台" })).toHaveAttribute(
    "href",
    datasetReturn,
  );
  const storageReturn = new URL(
    screen.getByTestId("summary-return").textContent!,
    "https://local.test",
  );
  expect(storageReturn.pathname).toBe("/storage");
  expect(storageReturn.searchParams.get("location")).toBe(ID);
  expect(storageReturn.searchParams.has("returnTo")).toBe(false);
});

it("opens location details from the full list and restores focus and list context on Escape", async () => {
  mount("/storage?page=2&returnTo=%2Fdatasets%3Fpage%3D3");
  await screen.findByRole("table", { name: "存储位置列表" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText("选择位置，查看配置、能力和关联来源。")).not.toBeInTheDocument();
  const trigger = await screen.findByRole("button", { name: "查看 Company archive 详情" });
  trigger.focus();
  fireEvent.click(trigger);
  await screen.findByRole("dialog", { name: "存储位置详情" });
  expect(screen.getByTestId("url")).toHaveTextContent(`location=${ID}`);
  expect(await within(screen.getByRole("dialog")).findByTestId("summary")).toHaveTextContent(ID);
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(screen.getByTestId("url")).toHaveTextContent("page=2");
  expect(screen.getByTestId("url")).toHaveTextContent("returnTo=");
  expect(screen.getByTestId("url")).not.toHaveTextContent("location=");
});
it.each([
  ["登记目录", "filesystem"],
  ["新建远端连接", "connection"],
])("opens %s in a centered creation dialog", async (label, testId) => {
  mount("/storage");
  const trigger = screen.getByRole("button", { name: label });
  fireEvent.click(trigger);
  const dialog = await screen.findByRole("dialog", { name: label });
  expect(within(dialog).getByTestId(testId)).toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
});
it("keeps invalid deep links dismissible without querying unrelated location data", async () => {
  mount("/storage?location=invalid&page=2");
  const dialog = await screen.findByRole("dialog", { name: "存储位置详情" });
  expect(within(dialog).getByRole("alert")).toHaveTextContent("存储位置链接无效");
  expect(api.location).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: "关闭" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByTestId("url")).toHaveTextContent("page=2");
});
