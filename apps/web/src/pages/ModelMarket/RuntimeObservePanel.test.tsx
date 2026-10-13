import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// Radix Tooltip / ScrollArea read ResizeObserver; jsdom doesn't ship it.
// Minimal stub — enough for mount/render assertions (no real layout).
beforeAll(() => {
  class RO {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  }
  (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = RO;
});

// ── module mocks (same style as the legacy test) ───────────────────────────

const mockTopology = vi.fn();
const mockRuntimeSnapshot = vi.fn();
const mockObserve = vi.fn();
const mockListAll = vi.fn();
const mockOverview = vi.fn();
const mockSmokeTest = vi.fn();
const mockDrainPoolMember = vi.fn();
const mockResumePoolMember = vi.fn();

vi.mock("@/api/adminMlIntegrations", () => ({
  adminMlIntegrationsApi: {
    topology: () => mockTopology(),
    runtimeSnapshot: () => mockRuntimeSnapshot(),
    observe: () => mockObserve(),
    listAll: () => mockListAll(),
    overview: () => mockOverview(),
    observeSmokeTest: (payload: unknown) => mockSmokeTest(payload),
    drainPoolMember: (poolId: string, registryId: string) =>
      mockDrainPoolMember(poolId, registryId),
    resumePoolMember: (poolId: string, registryId: string) =>
      mockResumePoolMember(poolId, registryId),
  },
}));

vi.mock("@/api/ml-backends", () => ({
  mlBackendsApi: { reload: vi.fn(), setup: vi.fn(), warmup: vi.fn() },
  mlBackendSetupQueryKey: (projectId: string, backendId: string) => [
    "ml-backend-setup",
    projectId,
    backendId,
  ],
}));

const mockHealthMutate = vi.fn();
const mockUnloadMutate = vi.fn();
vi.mock("./useGlobalRegistry", () => ({
  useRegistryHealth: () => ({ mutate: mockHealthMutate, isPending: false }),
  useRegistryUnload: () => ({ mutate: mockUnloadMutate, isPending: false }),
}));

vi.mock("./VariantPanel", () => ({
  VariantPanel: () => <div>variant panel</div>,
}));

const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(selector: (state: any) => T) => selector({ push: mockPushToast }),
  };
});

import { RuntimeObservePanel } from "./RuntimeObservePanel";

// ── fixtures ───────────────────────────────────────────────────────────────

type MemberState = "active" | "draining" | "disabled";

function makeMember(
  opts: {
    registryId?: string;
    name?: string;
    traffic_state?: MemberState;
    weight?: number | null;
    route_inflight?: number;
    circuit_open?: boolean;
    registry_state?: string;
  } = {},
) {
  return {
    registry_id: opts.registryId ?? "inst-1",
    name: opts.name ?? "grounded-sam2-a",
    traffic_state: opts.traffic_state ?? "active",
    weight: opts.weight ?? 1,
    state: "connected",
    last_checked_at: new Date().toISOString(),
    gpu_resource_id: "node-a/GPU-abc",
    // runtime-snapshot member fields:
    health_fresh: true,
    route_inflight: opts.route_inflight ?? 0,
    circuit_open: opts.circuit_open ?? false,
    registry_state: opts.registry_state ?? "connected",
    // metrics-driven — always null in v0.23.4
    last_selected_at: null,
    selection_count_window: null,
    rejection_count_window: null,
    p95_ms: null,
    error_rate: null,
  };
}

function makePool(
  opts: {
    id?: string;
    name?: string;
    status?: "healthy" | "degraded" | "offline" | "unknown";
    routable_instances?: number;
    members?: ReturnType<typeof makeMember>[];
  } = {},
) {
  const members = opts.members ?? [makeMember()];
  return {
    id: opts.id ?? "pool-1",
    name: opts.name ?? "图像分割池",
    enabled: true,
    routing_policy: "weighted_round_robin",
    legacy_instance_id: null,
    capability_fingerprint: "fp-abc",
    routing_generation: 1,
    member_count: members.length,
    routable_instances: opts.routable_instances ?? members.length,
    status: opts.status ?? "healthy",
    status_reason_codes: [],
    members,
  };
}

function makeSnapshot(
  opts: {
    pools?: Array<{
      id: string;
      name: string;
      enabled?: boolean;
      routing_generation?: number;
      members?: ReturnType<typeof makeMember>[];
    }>;
    router_mode?: "off" | "observe" | "enforce";
    partial?: boolean;
    partial_reason?: string | null;
    sources?: Array<{
      name: "topology" | "router_ledger" | "health" | "gpu" | "residency";
      stale?: boolean;
      error?: string | null;
      updated_at?: string | null;
    }>;
  } = {},
) {
  return {
    observed_at: "2026-07-20T10:00:00Z",
    partial: opts.partial ?? false,
    partial_reason: opts.partial_reason ?? null,
    router_mode: opts.router_mode ?? "enforce",
    schema_version: "runtime_snapshot.v1",
    pools: (opts.pools ?? []).map((p) => ({
      id: p.id,
      name: p.name,
      enabled: p.enabled ?? true,
      routing_generation: p.routing_generation ?? 1,
      members: p.members ?? [],
    })),
    sources: opts.sources ?? [
      { name: "topology", stale: false, updated_at: "2026-07-20T10:00:00Z" },
      { name: "router_ledger", stale: false, updated_at: "2026-07-20T10:00:00Z" },
      { name: "health", stale: false, updated_at: "2026-07-20T10:00:00Z" },
      { name: "gpu", stale: false, updated_at: "2026-07-20T10:00:00Z" },
      { name: "residency", stale: false, updated_at: "2026-07-20T10:00:00Z" },
    ],
  };
}

function makeBackend(overrides: Record<string, unknown> = {}) {
  return {
    id: "inst-1",
    name: "grounded-sam2-a",
    url: "http://172.17.0.1:8001",
    state: "connected",
    is_interactive: true,
    auth_method: "none",
    extra_params: {},
    gpu_resource_id: "node-a/GPU-abc",
    vram_budget_mb: 8192,
    eviction_priority: 0,
    gpu_config: null,
    health_meta: null,
    source_project_id: "",
    source_project_name: "manual",
    last_checked_at: new Date().toISOString(),
    ...overrides,
  };
}

function makeObserveTarget(
  opts: {
    url?: string;
    registered?: boolean;
    ok?: boolean;
    residency?: unknown;
  } = {},
) {
  return {
    url: opts.url ?? "http://172.17.0.1:8001",
    ok: opts.ok ?? true,
    latency_ms: 12,
    status_code: 200,
    supports_variants: false,
    registered: opts.registered ?? true,
    compute: null,
    gpu_info: null,
    residency: opts.residency ?? null,
  };
}

function defaultOverview() {
  return {
    storage: { items: [], total_object_count: 0, total_size_bytes: 0 },
    projects: [],
    total_backends: 1,
    connected_backends: 1,
  };
}

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <RuntimeObservePanel />
    </QueryClientProvider>,
  );
}

describe("RuntimeObservePanel · service-pool tree (P4)", () => {
  beforeEach(() => {
    mockTopology.mockReset();
    mockRuntimeSnapshot.mockReset();
    mockObserve.mockReset();
    mockListAll.mockReset();
    mockOverview.mockReset();
    mockSmokeTest.mockReset();
    mockDrainPoolMember.mockReset();
    mockResumePoolMember.mockReset();
    mockHealthMutate.mockReset();
    mockUnloadMutate.mockReset();
    mockPushToast.mockReset();
    mockOverview.mockResolvedValue(defaultOverview());
    mockDrainPoolMember.mockResolvedValue({});
    mockResumePoolMember.mockResolvedValue({});
  });

  afterEach(() => cleanup());

  it("展开/收起服务池：点击 chevron 显隐成员行；metrics 字段渲染「暂无路由指标」", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember()] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember()],
          },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    await screen.findByText("图像分割池");

    // collapsed by default — member row not present
    expect(screen.queryByText("grounded-sam2-a")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /展开服务池成员/ }));
    expect(await screen.findByText("grounded-sam2-a")).toBeInTheDocument();

    // metrics sentinel — v0.23.4 metrics are null; rendered as 「暂无路由指标」
    expect(screen.getAllByText("暂无路由指标").length).toBeGreaterThanOrEqual(1);

    // collapse
    fireEvent.click(screen.getByRole("button", { name: /收起服务池成员/ }));
    await waitFor(() => expect(screen.queryByText("grounded-sam2-a")).not.toBeInTheDocument());
  });

  it.each([
    ["direct", { status: "resident" }, true, "未知"],
    ["cached", { status: "resident" }, true, "未知"],
    ["direct", "unsupported", true, "未知"],
    ["direct", "resident", true, "resident"],
    ["direct", "resident", false, "resident（未核实）"],
  ] as const)("%s 驻留 state=%j、ok=%s 时安全显示 %s", async (source, state, ok, label) => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({
      configured_count: 1,
      targets: source === "direct" ? [makeObserveTarget({ residency: { state }, ok })] : [],
    });
    mockListAll.mockResolvedValue({
      items: [
        makeBackend(
          source === "cached"
            ? {
                health_meta: { residency: { state } },
                last_checked_at: new Date().toISOString(),
              }
            : {},
        ),
      ],
    });
    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    const member = await screen.findByText("grounded-sam2-a");
    expect(member).toBeInTheDocument();
    expect(within(member.closest("article")!).getByText("驻留", { exact: true })).toHaveTextContent(
      `驻留${label}`,
    );
  });

  it.each([
    // 回归（PR #103 Codex P2）：池级「驻留」已知性此前只看原始字段非空，
    // {state:"unknown"} / 畸形载荷 / 未核实来源会把「未知」冒充成「0 个」。
    ["direct", { state: "unknown" }, true],
    ["cached", { state: "unknown" }, true],
    ["direct", "unsupported", true],
    ["direct", { state: "resident" }, false],
  ] as const)(
    "%s 驻留=%j、ok=%s → 池级驻留显示「未知」而不是 0 个",
    async (source, residencyPayload, ok) => {
      mockTopology.mockResolvedValue({
        generated_at: "2026-07-20T10:00:00Z",
        router_mode: "enforce",
        schema_version: "topology.v1",
        pools: [makePool()],
      });
      mockRuntimeSnapshot.mockResolvedValue(
        makeSnapshot({
          pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
        }),
      );
      mockObserve.mockResolvedValue({
        configured_count: 1,
        targets:
          source === "direct" ? [makeObserveTarget({ residency: residencyPayload, ok })] : [],
      });
      mockListAll.mockResolvedValue({
        items: [
          makeBackend(
            source === "cached"
              ? {
                  health_meta: { residency: residencyPayload },
                  last_checked_at: new Date().toISOString(),
                }
              : {},
          ),
        ],
      });

      renderPanel();
      // 不展开成员：池卡字段带是唯一渲染「驻留」标签的位置。
      const poolName = await screen.findByText("图像分割池");
      const poolCard = poolName.closest("article")!;
      const residencyField = within(poolCard).getByText("驻留", { exact: true }).parentElement!;
      expect(residencyField).toHaveTextContent("驻留未知");
      expect(residencyField).not.toHaveTextContent("0 个");
    },
  );

  it("unloaded 驻留数据不计入服务池驻留实例数", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({
      configured_count: 1,
      targets: [
        makeObserveTarget({
          residency: {
            state: "unloaded",
            gpu_loaded: false,
            active_requests: 0,
            builders: 0,
            borrowers: 0,
            pools: { image: { resident: false } },
          },
        }),
      ],
    });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();

    expect(await screen.findByText("0 个")).toBeInTheDocument();
  });

  it("缺少实时探活时将新鲜 connected 缓存标为非实时", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));

    expect(await screen.findByText("缓存在线")).toBeInTheDocument();
    expect(screen.getByText("缓存探活，非实时")).toBeInTheDocument();
    expect(screen.queryByText("健康探活正常")).not.toBeInTheDocument();
  });

  it("缺少实时探活且 connected 缓存过期时显示状态过期", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({
      items: [makeBackend({ last_checked_at: "2026-07-20T09:00:00Z" })],
    });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));

    expect(await screen.findByText("状态过期")).toBeInTheDocument();
    expect(screen.getByText("探活状态已过期")).toBeInTheDocument();
  });

  it("实例详情 Sheet：打开、复制 ID、关闭", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    fireEvent.click(await screen.findByRole("button", { name: /^详情/ }));

    // sheet is open — copy-id button visible inside it
    const copyBtn = await screen.findByRole("button", { name: /复制 ID/ });
    fireEvent.click(copyBtn);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("inst-1"));

    // close via Radix Sheet close (aria-label "Close")
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /复制 ID/ })).not.toBeInTheDocument(),
    );
  });

  it("未纳管容器隔离：不渲染 routable/weight/traffic 字段", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [],
    });
    mockRuntimeSnapshot.mockResolvedValue(makeSnapshot({ pools: [] }));
    mockObserve.mockResolvedValue({
      configured_count: 1,
      targets: [
        makeObserveTarget({
          url: "http://172.17.0.1:9001",
          registered: false,
        }),
      ],
    });
    mockListAll.mockResolvedValue({ items: [] });

    renderPanel();
    // wait for queries to resolve + env-only section to render
    const envToggle = await screen.findByRole("button", { name: /未纳管容器/ });
    fireEvent.click(envToggle);
    expect(await screen.findByText("http://172.17.0.1:9001")).toBeInTheDocument();

    // isolation: env card has NO weight / routable / drain / unload fields
    expect(screen.queryByText("可路由")).not.toBeInTheDocument();
    expect(screen.queryByText("权重")).not.toBeInTheDocument();
    expect(screen.queryByText("停流")).not.toBeInTheDocument();
    expect(screen.queryByText("卸载")).not.toBeInTheDocument();
    // it DOES have register + smoke-test actions
    expect(screen.getByRole("button", { name: /显式注册/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /试启动/ })).toBeInTheDocument();
  });

  it("unload 门控：routable 实例卸载按钮禁用；点击停流触发 drain", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember({ traffic_state: "active" })] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        router_mode: "enforce",
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember({ traffic_state: "active", route_inflight: 0 })],
          },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));

    const unloadBtn = await screen.findByRole("button", { name: /^卸载$/ });
    expect(unloadBtn).toBeDisabled();

    // drain fires the pool-member drain mutation
    fireEvent.click(screen.getByRole("button", { name: /停流/ }));
    await waitFor(() => expect(mockDrainPoolMember).toHaveBeenCalledWith("pool-1", "inst-1"));
  });

  it("drained + inflight=0 + 新鲜账本 → 卸载启用并触发 unload", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember({ traffic_state: "draining" })] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        router_mode: "enforce",
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember({ traffic_state: "draining", route_inflight: 0 })],
          },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    const unloadBtn = await screen.findByRole("button", { name: /^卸载$/ });
    expect(unloadBtn).not.toBeDisabled();

    fireEvent.click(unloadBtn);
    await waitFor(() => expect(mockUnloadMutate).toHaveBeenCalledWith("inst-1", expect.anything()));
  });

  it("陈旧账本阻塞卸载（drained 但 router_ledger stale）", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember({ traffic_state: "draining" })] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        router_mode: "enforce",
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember({ traffic_state: "draining", route_inflight: 0 })],
          },
        ],
        sources: [
          { name: "topology", stale: false, updated_at: "2026-07-20T10:00:00Z" },
          {
            name: "router_ledger",
            stale: true,
            error: "redis timeout",
            updated_at: "2026-07-20T09:00:00Z",
          },
          { name: "health", stale: false, updated_at: "2026-07-20T10:00:00Z" },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    const unloadBtn = await screen.findByRole("button", { name: /^卸载$/ });
    expect(unloadBtn).toBeDisabled();
  });

  it("router_mode=observe：drained 实例卸载仍被阻塞（shadow drain）", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "observe",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember({ traffic_state: "draining" })] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        router_mode: "observe",
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember({ traffic_state: "draining", route_inflight: 0 })],
          },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    const unloadBtn = await screen.findByRole("button", { name: /^卸载$/ });
    expect(unloadBtn).toBeDisabled();
  });

  it("stale inflight 阻塞安全卸载：route_inflight=3 → 卸载禁用", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool({ members: [makeMember({ traffic_state: "draining" })] })],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        router_mode: "enforce",
        pools: [
          {
            id: "pool-1",
            name: "图像分割池",
            members: [makeMember({ traffic_state: "draining", route_inflight: 3 })],
          },
        ],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    fireEvent.click(await screen.findByRole("button", { name: /展开服务池成员/ }));
    const unloadBtn = await screen.findByRole("button", { name: /^卸载$/ });
    expect(unloadBtn).toBeDisabled();
  });

  it("部分来源失败：topology ok 但 runtime-snapshot partial → 服务池仍渲染 + partial banner", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        partial: true,
        partial_reason: "router_ledger stale",
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
        sources: [{ name: "router_ledger", stale: true, error: "redis timeout" }],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    // pool still renders despite partial
    expect(await screen.findByText("图像分割池")).toBeInTheDocument();

    // expand data source region to reveal partial banner
    fireEvent.click(screen.getByRole("button", { name: /数据来源/ }));
    expect(await screen.findByText(/部分数据来源失败/)).toBeInTheDocument();
  });

  it("单一刷新按钮（不是两个）", async () => {
    mockTopology.mockResolvedValue({
      generated_at: "2026-07-20T10:00:00Z",
      router_mode: "enforce",
      schema_version: "topology.v1",
      pools: [makePool()],
    });
    mockRuntimeSnapshot.mockResolvedValue(
      makeSnapshot({
        pools: [{ id: "pool-1", name: "图像分割池", members: [makeMember()] }],
      }),
    );
    mockObserve.mockResolvedValue({ configured_count: 1, targets: [] });
    mockListAll.mockResolvedValue({ items: [makeBackend()] });

    renderPanel();
    await screen.findByText("图像分割池");

    const refreshButtons = screen.getAllByRole("button", { name: /^刷新$/ });
    expect(refreshButtons).toHaveLength(1);

    fireEvent.click(refreshButtons[0]!);
    await waitFor(() => expect(mockTopology).toHaveBeenCalled());
    expect(mockRuntimeSnapshot).toHaveBeenCalled();
    expect(mockObserve).toHaveBeenCalled();
  });
});

describe("parseResidency (extracted module)", () => {
  it("畸形顶层 payload 返回 null", async () => {
    const { parseResidency } = await import("./runtime/parseResidency");
    expect(parseResidency(null)).toBeNull();
    expect(parseResidency("not an object")).toBeNull();
    expect(parseResidency({ state: "bogus" })).toBeNull();
    expect(parseResidency({})).toBeNull();
  });

  it("畸形叶子字段被标记为 malformed 且不推断为空闲", async () => {
    const { parseResidency } = await import("./runtime/parseResidency");
    // resident 非 boolean → poolsValid=false → strictEmpty 不能成立
    const r = parseResidency({
      state: "unloaded",
      gpu_loaded: false,
      active_requests: { invalid: true },
      builders: 0,
      borrowers: 0,
      pools: { image: { resident: "yes", device: { invalid: true } } },
    });
    expect(r).not.toBeNull();
    expect(r!.malformed).toBe(true);
    expect(r!.strictEmpty).toBe(false);
  });

  it("结构完整的 gpu_loaded=false → strictEmpty=true", async () => {
    const { parseResidency } = await import("./runtime/parseResidency");
    const r = parseResidency({
      state: "unloaded",
      gpu_loaded: false,
      active_requests: 0,
      builders: 0,
      borrowers: 0,
      draining: false,
      evictable: true,
      generation: "7",
      lifecycle_gate: "enforce",
      pools: { image: { resident: false, device: null, provider: null } },
    });
    expect(r).not.toBeNull();
    expect(r!.malformed).toBe(false);
    expect(r!.strictEmpty).toBe(true);
  });

  it("isFreshCachedHealth 沿用 3 分钟新鲜窗口", async () => {
    const { isFreshCachedHealth } = await import("./runtime/parseResidency");
    const now = Date.parse("2026-07-20T10:00:00Z");
    expect(isFreshCachedHealth("connected", "2026-07-20T09:58:00Z", now)).toBe(true);
    expect(isFreshCachedHealth("connected", "2026-07-20T09:50:00Z", now)).toBe(false);
    expect(isFreshCachedHealth("disconnected", "2026-07-20T09:58:00Z", now)).toBe(false);
  });
});
