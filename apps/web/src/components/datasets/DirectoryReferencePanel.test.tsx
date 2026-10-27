import { MemoryRouter, useLocation } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectoryReferencePanel } from "./DirectoryReferencePanel";
const api = vi.hoisted(() => ({
  sources: vi.fn(),
  start: vi.fn(),
  source: vi.fn(),
  probe: vi.fn(),
  create: vi.fn(),
  locations: vi.fn(),
  scans: vi.fn(),
  syncSchedule: vi.fn(),
  activeRegistration: vi.fn().mockResolvedValue(null),
  previewRules: vi.fn(),
  applyRules: vi.fn(),
  configureSync: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({
  useSessionGuard: () => () => () => true,
  useSessionRevision: () => 1,
}));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (value: unknown) => unknown) =>
    selector({ user: { id: "user" }, sessionId: "session" }),
}));
const source = {
  id: "source",
  cache_policy: "none",
  dataset_id: "dataset",
  enabled: true,
  location_id: "location",
  name: "source",
  node_id: "node",
  ownership_mode: "reference",
  policy_revision: 1,
  relative_path: "root",
  config_revision: 2,
  recursive: true,
  include_globs: ["*"],
  exclude_globs: [],
  unregistered_at: null,
};
it("shows the selected source heartbeat outside its select and keeps metadata visible", async () => {
  vi.clearAllMocks();
  api.sources.mockResolvedValue({
    items: [
      {
        ...source,
        agent_status: {
          state: "offline",
          reason_code: "agent_offline",
          site_name: "上海站点",
          agent_name: "图片 Agent",
          last_seen: null,
          measured_at: "2026-10-26T01:00:00Z",
        },
      },
    ],
    total: 1,
  });
  api.locations.mockResolvedValue({ items: [], total: 0 });
  api.scans.mockResolvedValue({ items: [], total: 0 });
  api.syncSchedule.mockResolvedValue(null);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DirectoryReferencePanel datasetId="dataset" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  expect(await screen.findByText("上海站点 / 图片 Agent · 离线")).toBeVisible();
  expect(screen.getByText(/连接恢复后可重试/).closest("select")).toBeNull();
  expect(screen.getByRole("option", { name: "source" })).toBeInTheDocument();
  expect(screen.getByText("扫描历史", { exact: false })).toBeVisible();
  view.unmount();
  client.clear();
});
it("refreshes a paused schedule revision after saving source rules before re-enabling", async () => {
  let changed = false;

  api.sources.mockImplementation(async () => ({
    items: [{ ...source, config_revision: changed ? 3 : 2 }],
    total: 1,
  }));
  api.locations.mockResolvedValue({ items: [], total: 0 });
  api.scans.mockResolvedValue({ items: [], total: 0 });
  api.syncSchedule.mockImplementation(async () => ({
    revision: changed ? 2 : 1,
    enabled: false,
    state: changed ? "blocked" : "idle",
    interval_minutes: 60,
    new_file_policy: "manual",
    timezone: "UTC",
    last_error: changed ? "configuration_changed" : null,
  }));
  api.previewRules.mockResolvedValue({
    proof: "proof",
    total: 0,
    counts: {},
    active_attempt_count: 0,
    items: [],
  });
  api.applyRules.mockImplementation(async () => {
    changed = true;
    return { ...source, config_revision: 3 };
  });
  api.configureSync.mockResolvedValue({});
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <MemoryRouter>
        <DirectoryReferencePanel datasetId="dataset" />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByRole("option", { name: "source" });
  const creation = screen.getByText("新增来源", { exact: false }).closest("details")!;
  const settings = screen.getByText("来源设置", { exact: false }).closest("details")!;
  const schedule = screen
    .getByText("周期计划", { exact: false, selector: "summary" })
    .closest("details")!;
  expect(
    screen.getByRole("combobox", { name: "原图策略", hidden: true }),
  ).toHaveAccessibleDescription("原图保留在来源，不复制到平台。");
  expect(screen.getByRole("option", { name: "原地引用", hidden: true })).toBeInTheDocument();
  expect(
    screen.queryByRole("option", { name: /不复制原图/, hidden: true }),
  ).not.toBeInTheDocument();
  expect(creation).not.toHaveAttribute("open");
  expect(settings).not.toHaveAttribute("open");
  expect(schedule).not.toHaveAttribute("open");
  fireEvent.click(settings.querySelector("summary")!);
  fireEvent.click(schedule.querySelector("summary")!);
  expect(settings).toHaveAttribute("open");
  expect(schedule).toHaveAttribute("open");
  await screen.findByRole("button", { name: "启用周期扫描" });
  fireEvent.change(screen.getByRole("textbox", { name: "新的排除规则 1" }), {
    target: { value: "hidden" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /已核对影响/ }));
  fireEvent.click(screen.getByRole("button", { name: "确认保存扫描规则" }));
  await waitFor(() => expect(api.syncSchedule.mock.calls.length).toBeGreaterThanOrEqual(2));
  await screen.findByText(/来源或位置配置已变化，请核对后重新启用/);
  fireEvent.click(screen.getByRole("button", { name: "启用周期扫描" }));
  await waitFor(() =>
    expect(api.configureSync).toHaveBeenCalledWith("source", {
      expected_revision: 2,
      enabled: true,
      interval_minutes: 60,
      new_file_policy: "manual",
      timezone: "UTC",
    }),
  );
});

vi.mock("@/components/jobs/AsyncJobDetailModal", () => ({ AsyncJobDetailModal: () => null }));

function LocationOutput() {
  return <output data-testid="location">{useLocation().search}</output>;
}
it("configures the first source, closes its form and focuses the new URL identity", async () => {
  let created = false;
  api.sources.mockImplementation(async () => ({
    items: created ? [source] : [],
    total: created ? 1 : 0,
  }));
  api.source.mockResolvedValue(source);
  api.locations.mockResolvedValue({
    items: [
      {
        id: "location",
        name: "CephFS",
        kind: "filesystem",
        enabled: true,
        declared_capabilities: { reference: true },
      },
    ],
    total: 1,
  });
  api.scans.mockResolvedValue({ items: [], total: 0 });
  api.syncSchedule.mockResolvedValue(null);
  api.probe.mockResolvedValue({ ok: true });
  api.create.mockImplementation(async () => {
    created = true;
    return source;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/datasets/dataset?tab=sources"]}>
        <DirectoryReferencePanel datasetId="dataset" persistUrl />
        <LocationOutput />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByRole("option", { name: "CephFS（服务器目录）" });
  fireEvent.change(screen.getByRole("combobox", { name: "原地引用存储位置" }), {
    target: { value: "location" },
  });
  fireEvent.change(screen.getByPlaceholderText("worker 的完整 hostname"), {
    target: { value: "node" },
  });
  expect(screen.getByRole("button", { name: "配置目录来源" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "配置目录来源" }));
  await screen.findByText("来源已配置，请运行只读预扫描");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("source=source");
  await waitFor(() =>
    expect(screen.getByText("新增来源", { exact: false }).closest("details")).not.toHaveAttribute(
      "open",
    ),
  );
  expect(api.create).toHaveBeenCalledWith(
    expect.objectContaining({ dataset_id: "dataset", location_id: "location", node_id: "node" }),
  );
  view.unmount();
  client.clear();
});

it("retains source history navigation while archived and removes source write configuration", async () => {
  vi.clearAllMocks();
  api.sources.mockResolvedValue({ items: [source], total: 1 });
  api.locations.mockResolvedValue({ items: [], total: 0 });
  api.scans.mockResolvedValue({ items: [], total: 0 });
  api.activeRegistration.mockResolvedValue(null);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DirectoryReferencePanel datasetId="dataset" archived />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await screen.findByRole("option", { name: "source" });
  expect(screen.getByRole("button", { name: "只读预扫描" })).toBeDisabled();
  expect(screen.getByText("新增来源", { exact: false }).closest("details")).toHaveAttribute(
    "hidden",
  );
  expect(screen.queryByText("来源设置", { exact: false })).not.toBeInTheDocument();
  expect(screen.queryByText("周期计划", { exact: false })).not.toBeInTheDocument();
  expect(screen.getByText("扫描历史", { exact: false })).toBeVisible();
  expect(screen.getByText("数据集已归档，来源历史保持只读。")).toBeInTheDocument();
  expect(screen.queryByText("来源已停用或注销，仅查看保留历史。")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "只读预扫描" }));
  expect(api.start).not.toHaveBeenCalled();
  view.unmount();
  client.clear();
});
