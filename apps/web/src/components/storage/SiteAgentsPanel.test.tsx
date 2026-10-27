import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import type { AgentOut, SiteOut } from "@/api/generated/types.gen";
import { createTestQueryClient } from "@/test/queryClient";
import { SiteAgentsPanel } from "./SiteAgentsPanel";

const state = vi.hoisted(() => ({ role: "super_admin", revision: 1, session: "s1" }));
const api = vi.hoisted(() => ({
  sites: vi.fn(),
  createSite: vi.fn(),
  agents: vi.fn(),
  agent: vi.fn(),
  createAgent: vi.fn(),
  drain: vi.fn(),
  rotate: vi.fn(),
  revoke: vi.fn(),
  locations: vi.fn(),
  registerLocation: vi.fn(),
}));
const copy = vi.hoisted(() => vi.fn());
const users = vi.hoisted(() => ({ page: vi.fn() }));
vi.mock("@/api/siteAgents", () => ({ siteAgentsApi: api }));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: copy }));
vi.mock("@/hooks/usePermissions", () => ({ usePermissions: () => ({ role: state.role }) }));
vi.mock("@/hooks/useVerifiedImage", () => ({ verifiedContentError: () => "请求失败" }));
vi.mock("@/hooks/useSessionGuard", async () => {
  const { useEffect, useRef } = await import("react");
  return {
    useSessionRevision: () => state.revision,
    useSessionGuard: () => {
      const alive = useRef(true);
      useEffect(
        () => () => {
          alive.current = false;
        },
        [],
      );
      return () => {
        const revision = state.revision;
        return () => alive.current && revision === state.revision;
      };
    },
  };
});
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (select: (value: unknown) => unknown) =>
    select({ user: { id: "user", role: state.role }, sessionId: state.session }),
}));
vi.mock("@/api/users", () => ({
  usersApi: users,
}));

const SITE = "11111111-1111-1111-1111-111111111111";
const AGENT = "22222222-2222-2222-2222-222222222222";
const LOCATION = "33333333-3333-3333-3333-333333333333";
const site: SiteOut = {
  id: SITE,
  name: "杭州站点",
  region: "杭州",
  created_by: "user",
  created_at: "2026-10-26T00:00:00Z",
};
const initialAgent: AgentOut = {
  id: AGENT,
  site_id: SITE,
  name: "Ceph 读取 Agent",
  protocol_version: 1,
  credential_revision: 3,
  state: "online",
  draining: false,
  revoked_at: null,
  pair_expires_at: null,
  session_id: "session",
  last_seen: "2026-10-26T00:00:00Z",
  roots: [{ root_handle: "ceph", root_revision: 7 }],
  limits: { max_concurrency: 1, bandwidth_bytes_per_second: 8388608, max_bytes: 67108864 },
  reported_limits: null,
  created_by: "user",
  created_at: "2026-10-26T00:00:00Z",
};
const pairing = {
  agent_id: AGENT,
  pair_code: "once-only-pair-code",
  expires_at: "2099-10-26T00:00:00Z",
};
const location = {
  id: LOCATION,
  name: "车辆数据",
  enabled: true,
  root_handle: "ceph",
  relative_path: "traffic/vehicle",
  source_usage: { reference_count: 8 },
};
let currentAgent: AgentOut;
const clients: ReturnType<typeof createTestQueryClient>[] = [];
afterEach(() => clients.splice(0).forEach((client) => client.clear()));
beforeEach(() => {
  vi.resetAllMocks();
  state.role = "super_admin";
  state.revision = 1;
  state.session = "s1";
  currentAgent = structuredClone(initialAgent);
  api.sites.mockResolvedValue({ items: [site], total: 1 });
  api.agents.mockImplementation(async () => ({ items: [currentAgent], total: 101 }));
  api.agent.mockImplementation(async () => currentAgent);
  api.locations.mockResolvedValue({ items: [location], total: 21 });
  api.createSite.mockResolvedValue(site);
  api.createAgent.mockResolvedValue(pairing);
  api.rotate.mockResolvedValue(pairing);
  api.registerLocation.mockResolvedValue(location);
  api.drain.mockImplementation(async (_id, _revision, draining) => {
    currentAgent = { ...currentAgent, draining };
    return currentAgent;
  });
  api.revoke.mockImplementation(async () => {
    currentAgent = { ...currentAgent, state: "revoked" };
    return currentAgent;
  });
  copy.mockResolvedValue(true);
  users.page.mockResolvedValue({ items: [], total: 0, pages: 0 });
});
function RouterState() {
  const location = useLocation();
  return (
    <output data-testid="url">
      {location.pathname}
      {location.search}
    </output>
  );
}
function mount(url = "/storage?tab=agents") {
  const client = createTestQueryClient();
  clients.push(client);
  const tree = () => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <SiteAgentsPanel />
        <RouterState />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree());
  return { ...view, client, refresh: () => view.rerender(tree()) };
}
async function detail(url = `/storage?tab=agents&agent=${AGENT}`) {
  const view = mount(url);
  const dialog = await screen.findByRole("dialog", { name: "Agent 详情" });
  await within(dialog).findByRole("heading", { name: initialAgent.name });
  return { ...view, dialog };
}

it("loads deep-linked detail independently of list pagination and preserves existing storage context", async () => {
  await detail(
    `/storage?tab=agents&agent=${AGENT}&agents_page=3&agents_page_size=50&page=4&page_size=100&returnTo=%2Fdatasets%3Fdataset%3Dd`,
  );
  expect(api.agents).toHaveBeenCalledWith(100, 50, "", expect.any(AbortSignal));
  expect(api.agent).toHaveBeenCalledWith(AGENT, expect.any(AbortSignal));
  expect(await screen.findByRole("link", { name: "车辆数据" })).toHaveAttribute(
    "href",
    expect.stringContaining(`location=${LOCATION}`),
  );
  expect(screen.getByRole("link", { name: "车辆数据" })).toHaveAttribute(
    "href",
    expect.stringContaining("returnTo="),
  );
  expect(screen.getByText("8 个已登记引用 · 已启用")).toBeInTheDocument();
  fireEvent.click(
    within(screen.getByRole("navigation", { name: "Agent 关联位置分页" })).getByRole("button", {
      name: "下一页",
    }),
  );
  await waitFor(() =>
    expect(api.locations).toHaveBeenLastCalledWith(AGENT, 20, 20, expect.any(AbortSignal)),
  );
  await waitFor(() =>
    expect(screen.getByRole("navigation", { name: "Agent 关联位置分页" })).toHaveTextContent(
      "共 21 条",
    ),
  );
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByTestId("url")).toHaveTextContent("page=4&page_size=100");
  expect(screen.getByTestId("url")).not.toHaveTextContent(`agent=${AGENT}`);
});

it("uses server totals when an affected-location page is empty", async () => {
  api.locations.mockResolvedValue({ items: [], total: 21 });
  await detail();
  expect(await screen.findByText("当前页没有位置，请返回上一页。")).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Agent 关联位置分页" })).toHaveTextContent(
    "共 21 条",
  );
});

it("creates the first site and Agent and keeps one-time pairing data out of query caches", async () => {
  api.sites.mockResolvedValue({ items: [], total: 0 });
  const { client } = mount();
  await screen.findByRole("dialog", { name: "新建站点" });
  fireEvent.change(screen.getByLabelText("站点名称"), { target: { value: "杭州站点" } });
  fireEvent.change(screen.getByLabelText("区域（可选）"), { target: { value: "杭州" } });
  fireEvent.click(screen.getByRole("button", { name: "创建站点并添加 Agent" }));
  await screen.findByRole("dialog", { name: "添加 Agent" });
  expect(api.createSite).toHaveBeenCalledWith({ name: "杭州站点", region: "杭州" });
  fireEvent.change(screen.getByLabelText("Agent 名称"), { target: { value: "Ceph Agent" } });
  fireEvent.click(screen.getByRole("button", { name: "创建并获取配对码" }));
  expect(await screen.findByText(pairing.pair_code)).toBeInTheDocument();
  expect(api.createAgent).toHaveBeenCalledWith(SITE, {
    name: "Ceph Agent",
    limits: initialAgent.limits,
  });
  fireEvent.click(screen.getByRole("button", { name: "复制配对信息" }));
  await screen.findByText("已复制配对信息");
  expect(JSON.parse(copy.mock.calls[0][0])).toEqual({
    agent_id: AGENT,
    pair_code: pairing.pair_code,
  });
  expect(
    JSON.stringify(
      client
        .getQueryCache()
        .getAll()
        .map((q) => q.state.data),
    ),
  ).not.toContain(pairing.pair_code);
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  expect(screen.queryByText(pairing.pair_code)).not.toBeInTheDocument();
});

it("discards a secret response after its create dialog closes", async () => {
  let finish!: (value: typeof pairing) => void;
  api.createAgent.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "添加 Agent" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "添加 Agent" }));
  fireEvent.change(screen.getByLabelText("Agent 名称"), { target: { value: "Slow Agent" } });
  fireEvent.click(screen.getByRole("button", { name: "创建并获取配对码" }));
  await waitFor(() => expect(api.createAgent).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "关闭" }));
  await act(async () => finish(pairing));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText(pairing.pair_code)).not.toBeInTheDocument();
});

it("explains a duplicate site name without showing an unrelated Agent state error", async () => {
  api.sites.mockResolvedValue({ items: [], total: 0 });
  api.createSite.mockRejectedValue({ status: 409, detailRaw: { code: "site_name_conflict" } });
  mount();
  await screen.findByRole("dialog", { name: "新建站点" });
  fireEvent.change(screen.getByLabelText("站点名称"), { target: { value: "重复站点" } });
  fireEvent.click(screen.getByRole("button", { name: "创建站点并添加 Agent" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("该站点名称已存在，请使用其他名称。");
});

it("retires forms and in-flight pairing results when the session changes", async () => {
  let finish!: (value: typeof pairing) => void;
  api.createAgent.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const view = mount();
  await waitFor(() => expect(screen.getByRole("button", { name: "添加 Agent" })).toBeEnabled());
  fireEvent.click(screen.getByRole("button", { name: "添加 Agent" }));
  fireEvent.change(screen.getByLabelText("Agent 名称"), { target: { value: "Old session" } });
  fireEvent.click(screen.getByRole("button", { name: "创建并获取配对码" }));
  state.session = "s2";
  state.revision += 1;
  view.refresh();
  await act(async () => finish(pairing));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(screen.queryByText(pairing.pair_code)).not.toBeInTheDocument();
});

it("pauses with expected revision, then requires explicit confirmation before rotating", async () => {
  await detail();
  expect(screen.getByRole("button", { name: "轮换身份" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "暂停派发" }));
  await screen.findByRole("button", { name: "恢复派发" });
  expect(api.drain).toHaveBeenCalledWith(AGENT, 3, true);
  fireEvent.click(screen.getByRole("button", { name: "轮换身份" }));
  expect(api.rotate).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name: "轮换 Agent 身份" })).toHaveTextContent("旧凭证失效");
  fireEvent.click(screen.getByRole("button", { name: "确认轮换身份" }));
  await screen.findByText(pairing.pair_code);
  expect(api.rotate).toHaveBeenCalledWith(AGENT, 3);
});

it("confirms irreversible revocation and reports revision conflicts without automatic retry", async () => {
  api.revoke.mockRejectedValue({ status: 409 });
  await detail();
  fireEvent.click(screen.getByRole("button", { name: "撤销 Agent" }));
  expect(api.revoke).not.toHaveBeenCalled();
  expect(screen.getByRole("dialog", { name: "撤销 Agent" })).toHaveTextContent("无法恢复原凭证");
  fireEvent.click(screen.getByRole("button", { name: "确认撤销 Agent" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("请刷新详情后重试");
  expect(api.revoke).toHaveBeenCalledOnce();
  expect(api.revoke).toHaveBeenCalledWith(AGENT, 3);
});

it("registers exactly the advertised root revision and opens the existing location detail", async () => {
  await detail();
  fireEvent.click(screen.getByRole("button", { name: "登记存储位置" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("ceph · 根修订 7");
  fireEvent.change(screen.getByLabelText("位置名称"), { target: { value: "车辆数据" } });
  fireEvent.change(screen.getByLabelText("根下相对目录"), { target: { value: "traffic/vehicle" } });
  fireEvent.click(screen.getByRole("button", { name: "登记并查看位置" }));
  await waitFor(() =>
    expect(api.registerLocation).toHaveBeenCalledWith({
      name: "车辆数据",
      agent_id: AGENT,
      root_handle: "ceph",
      root_revision: 7,
      relative_path: "traffic/vehicle",
      scope: "restricted",
      use_user_ids: [],
      cache_policy: "preview",
      original_cache_quota_bytes: 0,
      original_cache_ttl_seconds: 86400,
    }),
  );
  await waitFor(() =>
    expect(screen.getByTestId("url")).toHaveTextContent(`tab=locations&location=${LOCATION}`),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("waits for paired Agent root reports without inventing a root or disabling its connection", async () => {
  currentAgent = { ...currentAgent, roots: [] };
  await detail();
  expect(screen.getByText("尚未报告受控根，请启动 Agent 并等待心跳。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "登记存储位置" })).not.toBeInTheDocument();
  expect(api.revoke).not.toHaveBeenCalled();
});

it("loads remaining sites outside the select and keeps select hints muted and separate", async () => {
  api.sites.mockImplementation(async (offset) =>
    offset === 0
      ? { items: [site], total: 2 }
      : { items: [{ ...site, id: LOCATION, name: "成都站点" }], total: 2 },
  );
  mount();
  const select = await screen.findByLabelText("站点筛选");
  fireEvent.click(await screen.findByRole("button", { name: "加载更多站点" }));
  await waitFor(() =>
    expect(within(select).getByRole("option", { name: "成都站点" })).toBeInTheDocument(),
  );
  expect(api.sites).toHaveBeenCalledWith(1, expect.any(AbortSignal));
  const description = document.getElementById(select.getAttribute("aria-describedby")!);
  expect(description).toHaveClass("text-xs", "text-muted-foreground");
  expect(select).not.toContainElement(description);
  fireEvent.change(select, { target: { value: LOCATION } });
  await waitFor(() =>
    expect(api.agents).toHaveBeenLastCalledWith(0, 20, LOCATION, expect.any(AbortSignal)),
  );
});

it("hides cached detail after access denial and sends no Agent requests for ordinary managers", async () => {
  const view = await detail();
  api.agent.mockRejectedValue({ status: 403 });
  await act(async () => {
    await view.client.invalidateQueries({ queryKey: ["site-agent-detail"] });
  });
  await waitFor(() => expect(screen.queryByText("受控根")).not.toBeInTheDocument());
  expect(screen.getByRole("alert")).toHaveTextContent("无法读取 Agent 详情");
  vi.clearAllMocks();
  state.role = "project_admin";
  view.refresh();
  expect(screen.getByText("站点与 Agent 仅超级管理员可查看。")).toBeInTheDocument();
  expect(api.sites).not.toHaveBeenCalled();
  expect(api.agents).not.toHaveBeenCalled();
  expect(api.agent).not.toHaveBeenCalled();
});
