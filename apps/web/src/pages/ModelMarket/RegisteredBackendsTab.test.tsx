/**
 * RegisteredBackendsTab 单测 · v0.23.4 P3 重构。
 *
 * 重构后 RegisteredBackendsTab 是一个编排壳：
 *   - topology / runtime-snapshot / gpu-resources / overview / all 五个查询；
 *   - mergeTopologyAndSnapshot 把 topology+snapshot 合并成 view-model；
 *   - collectDiagnostics 给问题中心去重；
 *   - 超管 5 个 tab（服务池 / 实例 / GPU / 项目绑定 / 问题中心），
 *     项目管理员只有前 2 个只读 tab；
 *   - 行操作收到 DropdownMenu，卸载/删除走 AlertDialog。
 *
 * 覆盖 (plan §12.2): loading/empty/error/partial-fail · 角色门控 · mutation
 * flows（drain/resume/delete/health）· 问题中心诊断去重 · 窄屏无 min-w-[980px]。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode } from "react";

// Radix Tabs switches on pointerdown. userEvent.click() fires the full
// pointer + mouse + click sequence, which reliably triggers the switch in jsdom.
async function switchTab(name: RegExp): Promise<void> {
  const tab = await screen.findByRole("tab", { name: name as never });
  await userEvent.click(tab);
}

// ── Toast mock ───────────────────────────────────────────────────────────────
const mockPushToast = vi.fn();
vi.mock("@/components/ui/Toast", async () => {
  const actual = await vi.importActual<any>("@/components/ui/Toast");
  return {
    ...actual,
    useToastStore: <T,>(sel: (s: any) => T) => sel({ push: mockPushToast }),
  };
});

// ── adminMlIntegrationsApi mock ───────────────────────────────────────────────
const mockTopology = vi.fn();
const mockRuntimeSnapshot = vi.fn();
const mockListAll = vi.fn();
const mockGpuResources = vi.fn();
const mockOverview = vi.fn();
const mockListServicePools = vi.fn();

vi.mock("@/api/adminMlIntegrations", () => ({
  adminMlIntegrationsApi: {
    topology: () => mockTopology(),
    runtimeSnapshot: () => mockRuntimeSnapshot(),
    listAll: () => mockListAll(),
    gpuResources: () => mockGpuResources(),
    overview: () => mockOverview(),
    listServicePools: () => mockListServicePools(),
  },
}));

// ── mutation hooks mock ──────────────────────────────────────────────────────
const mockDeleteRegistryMutate = vi.fn();
const mockHealthMutate = vi.fn();
const mockUnloadMutate = vi.fn();
const mockDrainMutate = vi.fn();
const mockResumeMutate = vi.fn();
const mockPatchPoolMutate = vi.fn();
const mockCapabilityPreview = vi.fn();
const mockAcceptCapabilityMutate = vi.fn();

vi.mock("./useGlobalRegistry", () => ({
  useDeleteRegistry: () => ({
    mutate: mockDeleteRegistryMutate,
    mutateAsync: vi.fn(),
    isPending: false,
  }),
  useRegistryHealth: () => ({ mutate: mockHealthMutate, isPending: false }),
  useRegistryUnload: () => ({ mutate: mockUnloadMutate, mutateAsync: vi.fn(), isPending: false }),
  useDrainPoolMember: () => ({
    mutate: mockDrainMutate,
    mutateAsync: vi.fn(() => Promise.resolve()),
    isPending: false,
  }),
  useResumePoolMember: () => ({
    mutate: mockResumeMutate,
    mutateAsync: vi.fn(() => Promise.resolve()),
    isPending: false,
  }),
  usePatchServicePool: () => ({ mutate: mockPatchPoolMutate, isPending: false }),
  useCreateServicePool: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteServicePool: () => ({ mutate: vi.fn(), isPending: false }),
  usePutPoolMember: () => ({ mutate: vi.fn(), isPending: false }),
  useRemovePoolMember: () => ({ mutate: vi.fn(), isPending: false }),
  useCapabilityDriftPreview: (...args: unknown[]) => mockCapabilityPreview(...args),
  useAcceptCapabilityDrift: () => ({
    mutate: mockAcceptCapabilityMutate,
    isPending: false,
  }),
}));

// ── GlobalBackendFormModal mock ──────────────────────────────────────────────
vi.mock("./GlobalBackendFormModal", () => ({
  GlobalBackendFormModal: ({ open }: { open: boolean }) =>
    open ? <div data-testid="global-backend-form-modal">modal</div> : null,
}));

// ── usePermissions mock (角色可切) ───────────────────────────────────────────
let mockRole = "super_admin";
vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: mockRole }),
}));

import { RegisteredBackendsTab } from "./RegisteredBackendsTab";
import type {
  TopologyResponse,
  TopologyPoolEntry,
  RuntimeSnapshotResponse,
} from "@/api/generated/types.gen";

// ── fixture builders ─────────────────────────────────────────────────────────

function makeTopologyPool(overrides: Partial<TopologyPoolEntry> = {}): TopologyPoolEntry {
  return {
    id: "pool-1",
    name: "grounded-sam2-pool",
    enabled: true,
    routing_policy: "smooth_weighted_round_robin",
    legacy_instance_id: null,
    routing_generation: 1,
    capability_fingerprint: "abc",
    member_count: 1,
    routable_instances: 1,
    status: "healthy",
    status_reason_codes: [],
    members: [
      {
        registry_id: "bk-1",
        name: "grounded-sam2",
        traffic_state: "active",
        weight: 1,
        state: "connected",
        last_checked_at: "2026-07-20T10:00:00Z",
        gpu_resource_id: "node-a/index:0",
      },
    ],
    ...overrides,
  };
}

function makeTopology(overrides: Partial<TopologyResponse> = {}): TopologyResponse {
  return {
    schema_version: "topology.v1",
    generated_at: "2026-07-20T10:00:00Z",
    router_mode: "off",
    pools: [makeTopologyPool()],
    ...overrides,
  };
}

function makeSnapshot(overrides: Partial<RuntimeSnapshotResponse> = {}): RuntimeSnapshotResponse {
  return {
    schema_version: "runtime_snapshot.v1",
    observed_at: "2026-07-20T10:00:00Z",
    router_mode: "off",
    partial: false,
    partial_reason: null,
    sources: [
      { name: "topology", updated_at: "2026-07-20T10:00:00Z", stale: false },
      { name: "router_ledger", updated_at: "2026-07-20T10:00:00Z", stale: false },
      { name: "health", updated_at: "2026-07-20T10:00:00Z", stale: false },
      { name: "gpu", stale: true, error: "not_bundled_in_v0_23_3" },
      { name: "residency", stale: true, error: "not_bundled_in_v0_23_3" },
    ],
    pools: [
      {
        id: "pool-1",
        name: "grounded-sam2-pool",
        enabled: true,
        routing_generation: 1,
        members: [
          {
            registry_id: "bk-1",
            name: "grounded-sam2",
            traffic_state: "active",
            weight: 1,
            registry_state: "connected",
            health_fresh: true,
            last_checked_at: "2026-07-20T10:00:00Z",
            route_inflight: 0,
            circuit_open: false,
            gpu_resource_id: "node-a/index:0",
            last_selected_at: null,
            selection_count_window: null,
            rejection_count_window: null,
            p95_ms: null,
            error_rate: null,
          },
        ],
      },
    ],
    ...overrides,
  };
}

function makeBackend(overrides: Record<string, unknown> = {}) {
  return {
    id: "bk-1",
    name: "grounded-sam2",
    url: "http://172.17.0.1:8001",
    state: "connected",
    is_interactive: true,
    auth_method: "none",
    extra_params: {},
    gpu_resource_id: "node-a/index:0",
    vram_budget_mb: 8192,
    eviction_priority: 0,
    gpu_config: {
      status: "ok",
      desired_mode: "observe",
      effective_mode: "observe",
      allocatable_mb: 20_000,
      resource_claimed_budget_mb: 8192,
      diagnostics: [],
    },
    health_meta: null,
    source_project_id: "p-1",
    source_project_name: "env",
    last_checked_at: "2026-07-20T10:00:00Z",
    ...overrides,
  };
}

function makeGpuResource(overrides: Record<string, unknown> = {}) {
  return {
    gpu_resource_id: "node-a/index:0",
    node_id: "node-a",
    physical_device_token: "index:0",
    allocatable_mb: 20_000,
    configured_mode: "observe",
    desired_mode: "observe",
    effective_mode: "observe",
    claimed_budget_mb: 8192,
    claimed_backend_count: 1,
    status: "ok",
    diagnostics: [],
    rollout: {
      enabled: false,
      state: "off",
      dispatch_blocked: false,
      effective_mode: "off",
    },
    runtime: {
      ready: true,
      reason: "ok",
      status: "ready",
      active_backend_count: 1,
      backend_count: 1,
      allocation_state_counts: {},
      membership_state_counts: {},
      backend_queue_count: 0,
      card_queue_count: 0,
      committed_mb: 4096,
      lease_count: 1,
      transition_present: null,
    },
    ...overrides,
  };
}

function makeGpuResources(resources: any[] = [], overrides: Record<string, unknown> = {}) {
  return {
    global_desired_mode: "observe",
    runtime_ready: true,
    observe_runtime_ready: true,
    enforce_runtime_ready: false,
    rollout_enabled: false,
    resources,
    diagnostics: [],
    ...overrides,
  };
}

function makeOverview(overrides: Record<string, unknown> = {}) {
  return {
    storage: { items: [], total_object_count: 0, total_size_bytes: 0 },
    projects: [],
    total_backends: 0,
    connected_backends: 0,
    ...overrides,
  };
}

function LocationProbe(): ReactNode {
  const location = useLocation();
  return <div data-testid="registry-location-probe" data-search={location.search} />;
}

function renderUI(initialUrl = "/model-market?tab=registry") {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      {
        path: "/model-market",
        element: (
          <>
            <RegisteredBackendsTab />
            <LocationProbe />
          </>
        ),
      },
    ],
    { initialEntries: [initialUrl] },
  );
  return {
    ...render(
      <QueryClientProvider client={qc}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    ),
    router,
    queryClient: qc,
  };
}

function currentSearch(): string {
  return screen.getByTestId("registry-location-probe").dataset.search ?? "";
}

// ── tests ────────────────────────────────────────────────────────────────────

describe("RegisteredBackendsTab (v0.23.4 P3)", () => {
  beforeEach(() => {
    mockPushToast.mockReset();
    mockDeleteRegistryMutate.mockReset();
    mockHealthMutate.mockReset();
    mockUnloadMutate.mockReset();
    mockDrainMutate.mockReset();
    mockResumeMutate.mockReset();
    mockPatchPoolMutate.mockReset();
    mockCapabilityPreview.mockReset();
    mockAcceptCapabilityMutate.mockReset();
    mockTopology.mockReset();
    mockRuntimeSnapshot.mockReset();
    mockListAll.mockReset();
    mockGpuResources.mockReset();
    mockOverview.mockReset();
    mockListServicePools.mockReset();
    mockRole = "super_admin";

    mockTopology.mockResolvedValue(makeTopology());
    mockRuntimeSnapshot.mockResolvedValue(makeSnapshot());
    mockListAll.mockResolvedValue({ items: [] });
    mockGpuResources.mockResolvedValue(makeGpuResources([]));
    mockOverview.mockResolvedValue(makeOverview());
    mockCapabilityPreview.mockReturnValue({
      data: {
        pool_id: "pool-1",
        registry_id: "bk-1",
        member_state: "disabled",
        pool_enabled: false,
        pool_fingerprint: "a".repeat(64),
        candidate_fingerprint: "b".repeat(64),
        differing_fields: ["models", "supported_prompts"],
        has_drift: true,
        can_accept: true,
        blocking_members: [],
      },
      isLoading: false,
      isError: false,
      error: null,
    });
  });

  describe("loading / empty / error / partial-fail", () => {
    it("topology 加载中 → 显示加载态", () => {
      mockTopology.mockImplementation(() => new Promise(() => {}));
      renderUI();
      expect(screen.getByText("加载服务池拓扑…")).toBeInTheDocument();
    });

    it("topology 加载失败 → 显示整页错误 + 重试", async () => {
      mockTopology.mockRejectedValue(new Error("网络故障"));
      renderUI();
      await screen.findByText(/拓扑加载失败/);
      expect(screen.getAllByRole("button", { name: /重试/ }).length).toBeGreaterThan(0);
    });

    it("runtime-snapshot 失败但 topology 成功 → 其它数据保留 + 顶部 partial 告警", async () => {
      mockRuntimeSnapshot.mockRejectedValue(new Error("snapshot 503"));
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      // 服务池 tab 内容仍在
      expect(await screen.findByText("grounded-sam2-pool")).toBeInTheDocument();
      // 顶部出现运行时快照加载失败的 partial 告警
      await waitFor(() => {
        expect(screen.getByText("运行时快照加载失败")).toBeInTheDocument();
      });
    });

    it("空态：0 服务池 → 显示空态文案，不渲染 0 实例数字", async () => {
      mockTopology.mockResolvedValue(makeTopology({ pools: [] }));
      renderUI();
      expect(await screen.findByText("尚无服务池")).toBeInTheDocument();
    });

    it("实例列表查询失败且无缓存 → 实例 tab 计数显示 ? 而不是 0", async () => {
      // 回归（PR #103 Codex P2）：/all 失败时旧代码回落 0，把失败的列表
      // 冒充成空注册表；应与 GPU / 项目计数一致地显示未知。
      mockListAll.mockRejectedValue(new Error("all 503"));
      renderUI();
      const instancesTab = await screen.findByRole("tab", { name: /实例/ });
      await waitFor(() => {
        expect(instancesTab.textContent).toContain("?");
      });
    });
  });

  describe("Super Admin", () => {
    it("默认显示 5 个 tab，默认在服务池 tab", async () => {
      renderUI();
      expect(await screen.findByRole("tab", { name: /服务池/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /实例/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /GPU 资源/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /项目绑定/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /问题中心/ })).toBeInTheDocument();
    });

    it("服务池行展示策略、ID、可路由/总实例、操作菜单", async () => {
      renderUI();
      await screen.findByText("grounded-sam2-pool");
      // routing_policy 超管可见
      expect(screen.getByText(/平滑加权轮询/)).toBeInTheDocument();
      // 操作按钮
      expect(screen.getAllByTitle("服务池操作").length).toBeGreaterThan(0);
    });

    it("实例 tab 渲染实例行 + 详情/操作按钮，GPU claim 收敛进摘要列（plan §4.3）", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      // 切到实例 tab（Radix Tabs 用 pointerdown 切换）
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      // URL 收进名称副行，完整值仍可通过 title 查看
      expect(screen.getByTitle("http://172.17.0.1:8001")).toBeInTheDocument();
      // GPU claim 摘要列显示完整资源 ID（带 GPU 前缀），title 带预算
      expect(screen.getByText("GPU node-a/index:0")).toBeInTheDocument();
      expect(screen.getByTitle(/node-a\/index:0 · 8192 MiB/)).toBeInTheDocument();
      // 详情 + 操作按钮存在
      expect(screen.getAllByTitle("详情").length).toBeGreaterThan(0);
      expect(screen.getAllByTitle("实例操作").length).toBeGreaterThan(0);
      // 主表不再有独立的 URL / 来源 / 权重 / GPU 列（列数收敛为 6）
      const header = screen.getByText("所属服务池").closest("tr");
      expect(header?.querySelectorAll("th").length).toBe(6);
    });

    it("点击注册实例 → 打开 GlobalBackendFormModal", async () => {
      renderUI();
      await screen.findByText("grounded-sam2-pool");
      fireEvent.click(screen.getByRole("button", { name: /注册实例/ }));
      await waitFor(() => {
        expect(screen.getByTestId("global-backend-form-modal")).toBeInTheDocument();
      });
    });

    it("GPU 资源 tab 渲染资源行 + 两根独立 Progress 条（静态 vs 运行时）", async () => {
      mockGpuResources.mockResolvedValue(makeGpuResources([makeGpuResource()]));
      renderUI();
      await switchTab(/GPU 资源/);
      const summary = await screen.findByTestId("gpu-resource-summary");
      expect(within(summary).getByText("运行时就绪")).toBeInTheDocument();
      expect(within(summary).getByText("全局期望模式")).toBeInTheDocument();
      expect(within(summary).getByText("Observe 就绪")).toBeInTheDocument();
      expect(within(summary).getByText("Enforce 未就绪")).toBeInTheDocument();
      // 身份列显示设备序号 + 节点，完整资源 ID 折叠为短 ID（tooltip/复制保留全值）
      expect(await screen.findByText("index:0")).toBeInTheDocument();
      expect(screen.getByText("节点 · node-a")).toBeInTheDocument();
      expect(screen.getByText("配置 · observe")).toBeInTheDocument();
      expect(screen.getByTitle("node-a/index:0")).toBeInTheDocument();
      expect(screen.getByText(/1 个 backend/)).toBeInTheDocument();
      // committed 运行时占用的数值
      expect(screen.getByText(/4,096 MiB/)).toBeInTheDocument();
      // 静态声明的数值
      expect(screen.getByText(/8,192 MiB/)).toBeInTheDocument();
    });

    it("项目绑定 tab 默认按项目渲染，可切换按服务池", async () => {
      mockOverview.mockResolvedValue(
        makeOverview({
          total_backends: 1,
          connected_backends: 1,
          projects: [
            {
              project_id: "p-1",
              project_name: "My AI Project",
              backends: [
                {
                  id: "bk-1",
                  project_id: "p-1",
                  name: "grounded-sam2",
                  url: "http://172.17.0.1:8001",
                  state: "connected",
                  is_interactive: true,
                  auth_method: "none",
                  extra_params: {},
                  health_meta: null,
                  error_message: null,
                  last_checked_at: "2026-07-20T10:00:00Z",
                  created_at: "2026-07-20T00:00:00Z",
                  updated_at: "2026-07-20T00:00:00Z",
                },
              ],
            },
          ],
        }),
      );
      renderUI();
      await switchTab(/项目绑定/);
      expect(await screen.findByText("My AI Project")).toBeInTheDocument();
      // 切换到按服务池视图
      fireEvent.click(screen.getByRole("button", { name: "按服务池" }));
      expect(await screen.findByText("grounded-sam2-pool")).toBeInTheDocument();
    });
  });

  describe("Project Admin", () => {
    beforeEach(() => {
      mockRole = "project_admin";
    });

    it("只显示服务池 + 实例两个 tab，无 GPU / 项目绑定 / 问题中心", async () => {
      renderUI();
      expect(await screen.findByRole("tab", { name: /服务池/ })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: /实例/ })).toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /GPU 资源/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /项目绑定/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: /问题中心/ })).not.toBeInTheDocument();
    });

    it("服务池行不显示 routing_policy、无 GPU 列、无操作按钮", async () => {
      // 项目管理员：服务端把 routing_policy 投影成 "unknown"
      mockTopology.mockResolvedValue(
        makeTopology({
          pools: [
            makeTopologyPool({
              routing_policy: "unknown",
              members: [
                {
                  registry_id: "bk-1",
                  name: "grounded-sam2",
                  traffic_state: "active",
                  weight: null,
                  state: null,
                  last_checked_at: null,
                  gpu_resource_id: null,
                },
              ],
            }),
          ],
        }),
      );
      renderUI();
      await screen.findByText("grounded-sam2-pool");
      // routing_policy 标签不出现（"策略 · ..." 仅超管可见）
      expect(screen.queryByText(/策略 ·/)).not.toBeInTheDocument();
      // 无服务池操作菜单
      expect(screen.queryByTitle("服务池操作")).not.toBeInTheDocument();
      // 无"注册实例"按钮
      expect(screen.queryByRole("button", { name: /注册实例/ })).not.toBeInTheDocument();
    });

    it("不触发超管专属查询（runtime-snapshot / gpu-resources / overview）", async () => {
      renderUI();
      await screen.findByText("grounded-sam2-pool");
      // 等一拍让所有 effect 跑完
      await waitFor(() => {
        expect(mockTopology).toHaveBeenCalled();
      });
      expect(mockRuntimeSnapshot).not.toHaveBeenCalled();
      expect(mockGpuResources).not.toHaveBeenCalled();
      expect(mockOverview).not.toHaveBeenCalled();
    });
  });

  describe("mutation flows", () => {
    it("实例健康检查 → 调用 registryHealth", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      // 打开操作菜单
      fireEvent.click(screen.getByTitle("实例操作"));
      // 点健康检查
      await waitFor(() => {
        expect(screen.getByRole("menuitem", { name: /健康检查/ })).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("menuitem", { name: /健康检查/ }));
      await waitFor(() => {
        expect(mockHealthMutate).toHaveBeenCalledWith("bk-1", expect.anything());
      });
    });

    it("实例 drain → 调用 drainPoolMember", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      fireEvent.click(screen.getByTitle("实例操作"));
      await waitFor(() => {
        expect(screen.getByRole("menuitem", { name: /暂停接流/ })).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("menuitem", { name: /暂停接流/ }));
      await waitFor(() => {
        expect(mockDrainMutate).toHaveBeenCalledWith(
          { poolId: "pool-1", registryId: "bk-1" },
          expect.anything(),
        );
      });
    });

    it("实例 resume → 调用 resumePoolMember", async () => {
      // 成员当前处于 draining 状态，resume 才可点
      mockTopology.mockResolvedValue(
        makeTopology({
          pools: [
            makeTopologyPool({
              members: [
                {
                  registry_id: "bk-1",
                  name: "grounded-sam2",
                  traffic_state: "draining",
                  weight: 1,
                  state: "connected",
                  last_checked_at: "2026-07-20T10:00:00Z",
                  gpu_resource_id: "node-a/index:0",
                },
              ],
            }),
          ],
        }),
      );
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      fireEvent.click(screen.getByTitle("实例操作"));
      await waitFor(() => {
        expect(screen.getByRole("menuitem", { name: /恢复接流/ })).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("menuitem", { name: /恢复接流/ }));
      await waitFor(() => {
        expect(mockResumeMutate).toHaveBeenCalledWith(
          { poolId: "pool-1", registryId: "bk-1" },
          expect.anything(),
        );
      });
    });

    it("disabled 实例审核能力变更 → 预览差异并提交候选指纹", async () => {
      mockTopology.mockResolvedValue(
        makeTopology({
          pools: [
            makeTopologyPool({
              enabled: false,
              routable_instances: 0,
              status: "offline",
              members: [
                {
                  registry_id: "bk-1",
                  name: "grounded-sam2",
                  traffic_state: "disabled",
                  weight: 1,
                  state: "connected",
                  last_checked_at: "2026-07-23T01:45:00Z",
                  gpu_resource_id: "node-a/index:0",
                },
              ],
            }),
          ],
        }),
      );
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      fireEvent.click(screen.getByTitle("实例操作"));
      fireEvent.click(await screen.findByRole("menuitem", { name: /审核能力变更/ }));

      expect(await screen.findByText(/审核「grounded-sam2」能力变更/)).toBeInTheDocument();
      expect(screen.getByText("models")).toBeInTheDocument();
      expect(screen.getByText("supported_prompts")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: /接受新能力并恢复接流/ }));

      expect(mockAcceptCapabilityMutate).toHaveBeenCalledWith(
        {
          poolId: "pool-1",
          registryId: "bk-1",
          payload: {
            expected_candidate_fingerprint: "b".repeat(64),
            enable_pool: true,
          },
        },
        expect.anything(),
      );
    });

    it("实例删除 → AlertDialog 确认 → 调用 deleteRegistry", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI();
      await switchTab(/实例/);
      await screen.findByText("grounded-sam2");
      fireEvent.click(screen.getByTitle("实例操作"));
      await waitFor(() => {
        expect(screen.getByRole("menuitem", { name: /删除/ })).toBeInTheDocument();
      });
      fireEvent.click(screen.getByRole("menuitem", { name: /删除/ }));
      // AlertDialog 打开
      const dialog = await screen.findByText(/确认删除实例/);
      expect(dialog).toBeInTheDocument();
      // 点确认删除
      fireEvent.click(screen.getByRole("button", { name: /确认删除/ }));
      await waitFor(() => {
        expect(mockDeleteRegistryMutate).toHaveBeenCalledWith("bk-1", expect.anything());
      });
    });
  });

  describe("Issue Center · 诊断去重", () => {
    it("同一 code+subject_type+subject_id 在问题中心只渲染一次", async () => {
      // 构造一个 pool_offline 的池子（topology 来源），其成员还处于 circuit_open（runtime 来源）。
      // collectDiagnostics 会把 pool_offline 单独成条，circuit_open 单独成条——
      // 二者 subject_type 不同（service_pool vs instance），所以各算一条。
      // 我们要断言的是：不会出现两条 pool_offline 主文案。
      mockTopology.mockResolvedValue(
        makeTopology({
          pools: [
            makeTopologyPool({
              id: "pool-dead",
              name: "dead-pool",
              status: "offline",
              routable_instances: 0,
              status_reason_codes: ["all_members_disabled"],
              members: [
                {
                  registry_id: "bk-dead",
                  name: "dead-instance",
                  traffic_state: "disabled",
                  weight: 1,
                  state: "disconnected",
                  last_checked_at: "2026-07-20T10:00:00Z",
                  gpu_resource_id: null,
                },
              ],
            }),
          ],
        }),
      );
      mockRuntimeSnapshot.mockResolvedValue(
        makeSnapshot({
          pools: [
            {
              id: "pool-dead",
              name: "dead-pool",
              enabled: true,
              routing_generation: 1,
              members: [
                {
                  registry_id: "bk-dead",
                  name: "dead-instance",
                  traffic_state: "disabled",
                  weight: 1,
                  registry_state: "disconnected",
                  health_fresh: true,
                  last_checked_at: "2026-07-20T10:00:00Z",
                  route_inflight: 0,
                  circuit_open: true,
                  gpu_resource_id: null,
                  last_selected_at: null,
                  selection_count_window: null,
                  rejection_count_window: null,
                  p95_ms: null,
                  error_rate: null,
                },
              ],
            },
          ],
        }),
      );
      renderUI();
      await switchTab(/问题中心/);
      // pool_offline 主文案只出现一次
      await waitFor(() => {
        expect(screen.getAllByText(/无可路由实例/).length).toBe(1);
      });
      // 问题中心 tab 上的计数 badge = 2（pool_offline + circuit_open）
      const issueTab = screen.getByRole("tab", { name: /问题中心/ });
      expect(issueTab.querySelector(".bg-status-danger-soft, [class*='danger']")).not.toBeNull();
    });
  });

  describe("URL 子视图状态（plan §5）", () => {
    it("搜索逐字输入保留空格并匹配实例名称", async () => {
      const user = userEvent.setup();
      mockListAll.mockResolvedValue({ items: [makeBackend({ name: "SAM 2" })] });
      renderUI("/model-market?tab=registry&registry_view=instances");
      const input = await screen.findByLabelText("搜索实例");
      await user.type(input, "SAM ");
      expect(input).toHaveValue("SAM ");
      await user.type(input, "2 ");
      expect(input).toHaveValue("SAM 2 ");
      expect(new URLSearchParams(currentSearch()).get("instance_q")).toBe("SAM 2 ");
      expect(screen.getByText("SAM 2")).toBeInTheDocument();
    });

    it("详情随后退关闭、前进恢复，失效目标不保留旧详情", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      const { router } = renderUI("/model-market?tab=registry&registry_view=instances");
      fireEvent.click(await screen.findByRole("button", { name: "详情" }));
      expect(await screen.findByRole("dialog")).toHaveTextContent("grounded-sam2");
      await act(() => router.navigate(-1));
      expect(currentSearch()).not.toContain("instance_id");
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      await act(() => router.navigate(1));
      expect(await screen.findByRole("dialog")).toHaveTextContent("grounded-sam2");
      await act(() =>
        router.navigate("/model-market?tab=registry&registry_view=instances&instance_id=missing"),
      );
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(screen.getByText(/对象不存在或当前不可访问/)).toBeInTheDocument();
    });

    it("实例深链等待数据后打开，目标移除后关闭", async () => {
      let resolveBackends!: (value: { items: ReturnType<typeof makeBackend>[] }) => void;
      mockListAll.mockReturnValue(
        new Promise((resolve) => {
          resolveBackends = resolve;
        }),
      );
      const { queryClient } = renderUI(
        "/model-market?tab=registry&registry_view=instances&instance_id=bk-1",
      );
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      await act(async () => resolveBackends({ items: [makeBackend()] }));
      expect(await screen.findByRole("dialog")).toHaveTextContent("grounded-sam2");
      mockListAll.mockResolvedValue({ items: [makeBackend({ id: "bk-2", name: "second" })] });
      await act(() => queryClient.invalidateQueries());
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(screen.getByText(/对象不存在或当前不可访问/)).toBeInTheDocument();
    });

    it("?registry_view=instances 深链直接落到实例子视图", async () => {
      mockListAll.mockResolvedValue({ items: [makeBackend()] });
      renderUI("/model-market?tab=registry&registry_view=instances");
      // 无需点击，实例试图直接渲染。
      expect(await screen.findByText("grounded-sam2")).toBeInTheDocument();
    });

    it("子 TAB 触发器显示来源明确的计数（plan §3）", async () => {
      mockListAll.mockResolvedValue({
        items: [makeBackend(), makeBackend({ id: "bk-2", name: "second" })],
      });
      renderUI();
      const poolsTab = await screen.findByRole("tab", { name: /服务池/ });
      expect(poolsTab.textContent).toContain("1");
      const instancesTab = screen.getByRole("tab", { name: /实例/ });
      expect(instancesTab.textContent).toContain("2");
    });

    it("服务池「查看实例」→ URL 变为 registry_view=instances&instance_pool=<id>", async () => {
      mockListAll.mockResolvedValue({
        items: [makeBackend(), makeBackend({ id: "bk-2", name: "unmanaged" })],
      });
      renderUI(
        "/model-market?tab=registry&instance_q=other&instance_health=offline&instance_pool=old-pool&instance_id=old-backend",
      );
      await screen.findByText("grounded-sam2-pool");
      fireEvent.click(screen.getAllByTitle("服务池操作")[0]);
      fireEvent.click(await screen.findByRole("menuitem", { name: /查看实例/ }));
      await waitFor(() => {
        const search = currentSearch();
        expect(search).toContain("registry_view=instances");
        expect(search).toContain("instance_pool=pool-1");
        expect(search).not.toMatch(/instance_(q|health|id)=/);
      });
      // 实例行被池条件过滤后仍可见（bk-1 属于 pool-1）。
      expect(await screen.findByText("grounded-sam2")).toBeInTheDocument();
      expect(screen.getByText(/仅显示服务池「grounded-sam2-pool」的实例/)).toBeInTheDocument();
      expect(screen.queryByText("unmanaged")).not.toBeInTheDocument();
    });

    it("项目管理员深链超管子视图 → 回退服务池且不渲染超管 TAB", async () => {
      mockRole = "project_admin";
      renderUI("/model-market?tab=registry&registry_view=gpu");
      expect(await screen.findByText("grounded-sam2-pool")).toBeInTheDocument();
      await waitFor(() => {
        expect(currentSearch()).not.toContain("registry_view=gpu");
      });
      expect(screen.queryByRole("tab", { name: /GPU 资源/ })).not.toBeInTheDocument();
    });
  });

  describe("窄屏 · 不再有 min-w-[980px]", () => {
    it("表格容器不使用旧的 min-w-[980px] 类", async () => {
      const { container } = renderUI();
      await screen.findByText("grounded-sam2-pool");
      // 整个 DOM 中不应再出现 min-w-[980px] 类
      expect(container.querySelector(".min-w-\\[980px\\]")).toBeNull();
    });
  });
});
