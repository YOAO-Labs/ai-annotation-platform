/**
 * v0.23.4 · runtimeTopology view-model tests.
 *
 * Covers the Appendix A.5 golden fixture matrix plus the negative assertions
 * (forbidden behaviors): missing values must NOT render as 0 / healthy / idle,
 * stale must NOT borrow real-time color, same diagnostic must NOT duplicate.
 */
import { describe, expect, it } from "vitest";
import {
  METRICS_AVAILABLE,
  NO_METRICS_LABEL,
  collectDiagnostics,
  deriveMemberCapacity,
  deriveMemberRouting,
  evaluateUnloadGate,
  filterDiagnostics,
  mergeTopologyAndSnapshot,
  sortDiagnostics,
  sortPoolsBySeverity,
  type Diagnostic,
  type PoolViewModel,
} from "./runtimeTopology";
import type {
  GPUArbiterResourceItem,
  RuntimeMemberSnapshot,
  RuntimePoolSnapshot,
  RuntimeSnapshotResponse,
  TopologyPoolEntry,
  TopologyResponse,
} from "@/api/generated/types.gen";

// ── fixture builders ───────────────────────────────────────────────────────

function makePool(overrides: Partial<TopologyPoolEntry> = {}): TopologyPoolEntry {
  return {
    id: "pool-1",
    name: "pool-1",
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
        registry_id: "inst-1",
        name: "inst-1",
        traffic_state: "active",
        weight: 1,
        state: "connected",
        last_checked_at: "2026-07-20T10:00:00Z",
        gpu_resource_id: "gpu-0",
      },
    ],
    ...overrides,
  };
}

function makeSnapshotPool(overrides: Partial<RuntimePoolSnapshot> = {}): RuntimePoolSnapshot {
  return {
    id: "pool-1",
    name: "pool-1",
    enabled: true,
    routing_generation: 1,
    members: [
      {
        registry_id: "inst-1",
        name: "inst-1",
        traffic_state: "active",
        weight: 1,
        registry_state: "connected",
        health_fresh: true,
        last_checked_at: "2026-07-20T10:00:00Z",
        route_inflight: 0,
        circuit_open: false,
        gpu_resource_id: "gpu-0",
        last_selected_at: null,
        selection_count_window: null,
        rejection_count_window: null,
        p95_ms: null,
        error_rate: null,
      },
    ],
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
    pools: [makeSnapshotPool()],
    ...overrides,
  };
}

function makeTopology(overrides: Partial<TopologyResponse> = {}): TopologyResponse {
  return {
    schema_version: "topology.v1",
    generated_at: "2026-07-20T10:00:00Z",
    router_mode: "off",
    pools: [makePool()],
    ...overrides,
  };
}

// ── F_empty ────────────────────────────────────────────────────────────────

describe("F_empty · 0 pool", () => {
  it("空态渲染时不出现 0 / healthy", () => {
    const vm = mergeTopologyAndSnapshot(makeTopology({ pools: [] }), makeSnapshot({ pools: [] }));
    expect(vm.pools).toHaveLength(0);
    // No pool → no aggregate "0 routable" rendered at pool level.
    // (Header counter logic lives in component; here we verify the data contract.)
  });
});

// ── F_single_healthy ───────────────────────────────────────────────────────

describe("F_single_healthy", () => {
  it("单实例正常池：availability 正常, metrics_available=false", () => {
    const vm = mergeTopologyAndSnapshot(makeTopology(), makeSnapshot());
    expect(vm.pools).toHaveLength(1);
    const pool = vm.pools[0];
    expect(pool.status).toBe("healthy");
    expect(pool.availability).toEqual({
      routable: 1,
      total: 1,
      draining: 0,
      offline: 0,
    });
    expect(pool.metrics_available).toBe(false);
    expect(pool.members[0].routing).toBe("routable");
    expect(pool.members[0].capacity).toBe("idle");
  });
});

// ── F_multi_draining ───────────────────────────────────────────────────────

describe("F_multi_draining", () => {
  it("draining 成员被隔离：routable=2, draining=1", () => {
    const topology = makeTopology({
      pools: [
        makePool({
          id: "p",
          name: "p",
          member_count: 3,
          routable_instances: 2,
          status: "degraded",
          status_reason_codes: ["1_draining"],
          members: [
            {
              registry_id: "i1",
              name: "i1",
              traffic_state: "active",
              weight: 1,
              state: "connected",
              last_checked_at: "2026-07-20T10:00:00Z",
              gpu_resource_id: "g",
            },
            {
              registry_id: "i2",
              name: "i2",
              traffic_state: "active",
              weight: 1,
              state: "connected",
              last_checked_at: "2026-07-20T10:00:00Z",
              gpu_resource_id: "g",
            },
            {
              registry_id: "i3",
              name: "i3",
              traffic_state: "draining",
              weight: 1,
              state: "connected",
              last_checked_at: "2026-07-20T10:00:00Z",
              gpu_resource_id: "g",
            },
          ],
        }),
      ],
    });
    const vm = mergeTopologyAndSnapshot(topology, null);
    expect(vm.pools[0].availability).toEqual({
      routable: 2,
      total: 3,
      draining: 1,
      offline: 0,
    });
    // Draining member's routing axis is "draining" not "routable".
    const draining = vm.pools[0].members.find((m) => m.registry_id === "i3")!;
    expect(draining.routing).toBe("draining");
  });
});

// ── F_offline_all_disabled ─────────────────────────────────────────────────

describe("F_offline_all_disabled", () => {
  it("全部 disabled → status=offline + offline 计数", () => {
    const topology = makeTopology({
      pools: [
        makePool({
          member_count: 2,
          routable_instances: 0,
          status: "offline",
          status_reason_codes: ["2_disabled"],
          members: [
            {
              registry_id: "i1",
              name: "i1",
              traffic_state: "disabled",
              weight: 1,
              state: "disconnected",
              last_checked_at: null,
              gpu_resource_id: null,
            },
            {
              registry_id: "i2",
              name: "i2",
              traffic_state: "disabled",
              weight: 1,
              state: "disconnected",
              last_checked_at: null,
              gpu_resource_id: null,
            },
          ],
        }),
      ],
    });
    const vm = mergeTopologyAndSnapshot(topology, null);
    expect(vm.pools[0].status).toBe("offline");
    expect(vm.pools[0].availability.offline).toBe(2);
    // Disabled members → routing=blocked.
    expect(vm.pools[0].members.every((m) => m.routing === "blocked")).toBe(true);
  });
});

// ── F_degraded_partial_health + freshness ──────────────────────────────────

describe("F_degraded_partial_health", () => {
  it("health 来源 stale → partial=true, 但 topology 数据保留", () => {
    const snapshot = makeSnapshot({
      partial: true,
      partial_reason: "1/5 sources stale: health",
      sources: [
        { name: "topology", updated_at: "2026-07-20T10:00:00Z", stale: false },
        { name: "router_ledger", updated_at: "2026-07-20T10:00:00Z", stale: false },
        { name: "health", stale: true, error: "some_member_health_stale" },
        { name: "gpu", stale: true, error: "not_bundled_in_v0_23_3" },
        { name: "residency", stale: true, error: "not_bundled_in_v0_23_3" },
      ],
    });
    const vm = mergeTopologyAndSnapshot(makeTopology(), snapshot);
    expect(vm.partial).toBe(true);
    expect(vm.partial_reason).toContain("health");
    // Pool data is still present (not erased).
    expect(vm.pools).toHaveLength(1);
    expect(vm.pools[0].members).toHaveLength(1);
    // The stale health source is surfaced.
    const healthSrc = vm.sources.find((s) => s.name === "health")!;
    expect(healthSrc.stale).toBe(true);
    expect(vm.sources.find((s) => s.name === "router_ledger")?.label).toBe("路由执行状态");
  });
});

// ── F_metrics_absent (forbidden: never 0/healthy) ─────────────────────────

describe("F_metrics_absent · 负向断言", () => {
  it("metrics 字段恒为 null, METRICS_AVAILABLE=false", () => {
    expect(METRICS_AVAILABLE).toBe(false);
    const vm = mergeTopologyAndSnapshot(makeTopology(), makeSnapshot());
    const m = vm.pools[0].members[0];
    expect(m.runtime?.last_selected_at).toBeNull();
    expect(m.runtime?.selection_count_window).toBeNull();
    expect(m.runtime?.rejection_count_window).toBeNull();
    expect(m.runtime?.p95_ms).toBeNull();
    expect(m.runtime?.error_rate).toBeNull();
    // Sentinel label is exported for components to use.
    expect(NO_METRICS_LABEL).toBe("暂无路由指标");
  });

  it("capacity limit 恒为 null（v0.23.3 合同无 max_concurrency）", () => {
    const vm = mergeTopologyAndSnapshot(makeTopology(), makeSnapshot());
    expect(vm.pools[0].capacity.limit).toBeNull();
    // Execution counts come from the current routing-state observation; these stay.
    expect(vm.pools[0].capacity.inflight).toBe(0);
    expect(vm.pools[0].capacity.saturated).toBe(false);
  });
});

// ── F_project_admin projection ─────────────────────────────────────────────

describe("F_project_admin · 服务端投影后前端不推断", () => {
  it("routing_policy=unknown → 成员 routing=unknown（不冒充 routable）", () => {
    const topology = makeTopology({
      pools: [
        makePool({
          routing_policy: "unknown",
          members: [
            {
              registry_id: "i1",
              name: "i1",
              traffic_state: "active",
              weight: null,
              state: null,
              last_checked_at: null,
              gpu_resource_id: null,
            },
          ],
        }),
      ],
    });
    const vm = mergeTopologyAndSnapshot(topology, null);
    const m = vm.pools[0].members[0];
    expect(m.routing).toBe("unknown"); // NOT "routable"
    expect(m.weight).toBeNull(); // NOT 1 or 0
    expect(m.state).toBeNull(); // NOT "connected"
  });
});

// ── runtime unavailable (Project Admin / load failure) ─────────────────────

describe("runtime 不可用", () => {
  it("snapshot=null → partial=true + reason, 成员无 runtime 字段", () => {
    const vm = mergeTopologyAndSnapshot(makeTopology(), null);
    expect(vm.runtime_available).toBe(false);
    expect(vm.partial).toBe(true);
    expect(vm.partial_reason).toBe("runtime_snapshot_unavailable");
    expect(vm.pools[0].members[0].runtime).toBeUndefined();
    // capacity unknown (no runtime evidence).
    expect(vm.pools[0].members[0].capacity).toBe("unknown");
  });
});

// ── routing axis derivation matrix ─────────────────────────────────────────

describe("deriveMemberRouting", () => {
  it("disabled → blocked", () => {
    expect(deriveMemberRouting("disabled", null, "smooth_weighted_round_robin")).toBe("blocked");
  });
  it("draining → draining", () => {
    expect(deriveMemberRouting("draining", null, "smooth_weighted_round_robin")).toBe("draining");
  });
  it("active + circuit_open → blocked", () => {
    expect(
      deriveMemberRouting(
        "active",
        {
          circuit_open: true,
          route_inflight: 0,
          health_fresh: true,
          registry_state: "connected",
        } as RuntimeMemberSnapshot,
        "smooth_weighted_round_robin",
      ),
    ).toBe("blocked");
  });
  it("active + no circuit → routable", () => {
    expect(
      deriveMemberRouting(
        "active",
        {
          circuit_open: false,
          route_inflight: 0,
          health_fresh: true,
          registry_state: "connected",
        } as RuntimeMemberSnapshot,
        "smooth_weighted_round_robin",
      ),
    ).toBe("routable");
  });
});

// ── capacity axis derivation ───────────────────────────────────────────────

describe("deriveMemberCapacity", () => {
  it("no snapshot → unknown", () => {
    expect(deriveMemberCapacity(null)).toBe("unknown");
  });
  it("circuit_open → saturated", () => {
    expect(
      deriveMemberCapacity({
        circuit_open: true,
        route_inflight: 99,
      } as RuntimeMemberSnapshot),
    ).toBe("saturated");
  });
  it("inflight>0 → serving", () => {
    expect(
      deriveMemberCapacity({
        circuit_open: false,
        route_inflight: 3,
      } as RuntimeMemberSnapshot),
    ).toBe("serving");
  });
  it("inflight=0 → idle", () => {
    expect(
      deriveMemberCapacity({
        circuit_open: false,
        route_inflight: 0,
      } as RuntimeMemberSnapshot),
    ).toBe("idle");
  });
});

// ── diagnostic dedup ───────────────────────────────────────────────────────

describe("collectDiagnostics · 去重", () => {
  it("off 模式下路由执行状态过期仍阻止卸载请求", () => {
    const snapshot = makeSnapshot({
      router_mode: "off",
      sources: [{ name: "router_ledger", stale: true, error: "status unavailable" }],
    });
    const diagnostic = collectDiagnostics(makeTopology(), snapshot, null).find(
      (item) => item.code === "router_ledger_unavailable",
    );

    expect(diagnostic?.message).toContain("路由执行状态不可用");
    expect(diagnostic?.remediation).toContain("卸载请求会被禁用");
    expect(diagnostic?.remediation).not.toContain("Redis");
  });

  it("同一 code+subject_type+subject_id 只出现一次", () => {
    const pool = makePool({
      id: "p1",
      name: "p1",
      status: "offline",
      members: [
        {
          registry_id: "i1",
          name: "i1",
          traffic_state: "disabled",
          weight: 1,
          state: "disconnected",
          last_checked_at: null,
          gpu_resource_id: null,
        },
      ],
    });
    const topology = makeTopology({ pools: [pool] });
    const snapshot = makeSnapshot({
      pools: [
        makeSnapshotPool({
          id: "p1",
          members: [
            {
              registry_id: "i1",
              name: "i1",
              traffic_state: "disabled",
              weight: 1,
              registry_state: "disconnected",
              health_fresh: false,
              last_checked_at: null,
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
        }),
      ],
    });
    const diags = collectDiagnostics(topology, snapshot, null);
    // Three distinct records: pool_offline (p1) + circuit_open (i1) + health_stale (i1).
    const ids = diags.map((d) => d.id);
    expect(new Set(ids).size).toBe(ids.length);
    // Affected instance list on pool_offline includes i1.
    const offline = diags.find((d) => d.code === "pool_offline")!;
    expect(offline.affected_instance_ids).toContain("i1");
  });

  it("GPU critical 诊断独立计数", () => {
    const diags = collectDiagnostics(makeTopology(), null, [
      {
        gpu_resource_id: "gpu-x",
        node_id: "n1",
        physical_device_token: "tok",
        allocatable_mb: 24000,
        desired_mode: "enforce",
        effective_mode: "enforce",
        claimed_budget_mb: 30000,
        claimed_backend_count: 2,
        status: "critical",
        diagnostics: [],
      } as unknown as GPUArbiterResourceItem,
    ]);
    expect(diags.some((d) => d.code === "gpu_critical")).toBe(true);
  });

  it("diagnostic.id 是稳定的 code:subject_type:subject_id（dedup 键）", () => {
    // collectDiagnostics 的 pushDiagnostic 用 d.id 做 Map key 合并 affected_*_ids。
    // 当前各 emitter 不会产生同 id 冲突（topology/runtime/gpu 各自的 code 不重叠），
    // 但 dedup 合并路径是为未来新增 emitter 准备的。这里固定 id 格式合同，
    // 保证未来若两个来源 emit 同 code+subject_type+subject_id，它们会落到同一 Map key。
    const pool = makePool({
      id: "p-stable",
      name: "stable-pool",
      status: "offline",
      members: [
        {
          registry_id: "i-stable",
          name: "i-stable",
          traffic_state: "disabled",
          weight: 1,
          state: "disconnected",
          last_checked_at: null,
          gpu_resource_id: null,
        },
      ],
    });
    const diags = collectDiagnostics(makeTopology({ pools: [pool] }), null, null);
    const offline = diags.find((d) => d.code === "pool_offline")!;
    // id = `${code}:${subject_type}:${subject_id}` —— 稳定去重键。
    expect(offline.id).toBe("pool_offline:service_pool:p-stable");
    // 不同 subject 的同 code 不会冲突（subject_id 区分）。
    const pool2 = makePool({
      id: "p-other",
      name: "other-pool",
      status: "offline",
      members: [],
    });
    const diags2 = collectDiagnostics(makeTopology({ pools: [pool, pool2] }), null, null);
    const offlineIds = diags2.filter((d) => d.code === "pool_offline").map((d) => d.id);
    expect(offlineIds).toEqual([
      "pool_offline:service_pool:p-stable",
      "pool_offline:service_pool:p-other",
    ]);
  });
});

// ── diagnostic sort + filter ───────────────────────────────────────────────

describe("sortDiagnostics + filterDiagnostics", () => {
  const sample: Diagnostic[] = [
    {
      id: "a:instance:i1",
      code: "a",
      severity: "info",
      subject_type: "instance",
      subject_id: "i1",
      message: "info",
      affected_service_pool_ids: [],
      affected_instance_ids: ["i1"],
      affected_gpu_resource_ids: [],
      observed_at: "2026-07-20T10:00:00Z",
      source: "runtime_snapshot",
    },
    {
      id: "b:service_pool:p1",
      code: "b",
      severity: "blocker",
      subject_type: "service_pool",
      subject_id: "p1",
      message: "blocker",
      affected_service_pool_ids: ["p1"],
      affected_instance_ids: ["i1"],
      affected_gpu_resource_ids: [],
      observed_at: "2026-07-20T10:00:00Z",
      source: "topology",
    },
    {
      id: "c:gpu_resource:g1",
      code: "c",
      severity: "critical",
      subject_type: "gpu_resource",
      subject_id: "g1",
      message: "critical",
      affected_service_pool_ids: [],
      affected_instance_ids: [],
      affected_gpu_resource_ids: ["g1"],
      observed_at: "2026-07-20T10:00:00Z",
      source: "gpu_resources",
    },
  ];

  it("blocker → critical → warning → info 排序", () => {
    const sorted = sortDiagnostics(sample);
    expect(sorted.map((d) => d.severity)).toEqual(["blocker", "critical", "info"]);
  });

  it("按 instance_id 过滤", () => {
    const filtered = filterDiagnostics(sample, { instance_id: "i1" });
    // blocker (affects i1) + info (subject i1) match; gpu one doesn't.
    expect(filtered.map((d) => d.id).sort()).toEqual(["a:instance:i1", "b:service_pool:p1"]);
  });
});

// ── unload request gate (§8.1) ────────────────────────────────────────────

describe("evaluateUnloadGate · drain→observed idle→unload request gate", () => {
  function makeMember(
    overrides: Partial<PoolViewModel["members"][number]> = {},
  ): PoolViewModel["members"][number] {
    return {
      registry_id: "i1",
      name: "i1",
      traffic_state: "draining",
      weight: 1,
      state: "connected",
      last_checked_at: "2026-07-20T10:00:00Z",
      gpu_resource_id: null,
      routing: "draining",
      capacity: "idle",
      runtime: {
        health_fresh: true,
        route_inflight: 0,
        circuit_open: false,
        registry_state: "connected",
        last_selected_at: null,
        selection_count_window: null,
        rejection_count_window: null,
        p95_ms: null,
        error_rate: null,
      },
      ...overrides,
    };
  }

  it("routable 实例不显示 unload 请求", () => {
    const gate = evaluateUnloadGate(
      makeMember({ routing: "routable", traffic_state: "active" }),
      "enforce",
      true,
    );
    expect(gate.can_request_unload).toBe(false);
    expect(gate.blocked_routable).toBe(true);
    expect(gate.reasons[0]).toContain("drain");
  });

  it.each(["off", "observe", "enforce"] as const)(
    "router_mode=%s 时 draining + fresh zero observation 可请求卸载",
    (routerMode) => {
      const gate = evaluateUnloadGate(makeMember(), routerMode, true);
      expect(gate.can_request_unload).toBe(true);
      expect(gate.can_unload).toBe(true);
      expect(gate.blocked_shadow_mode).toBe(false);
      expect(gate.reasons).toHaveLength(0);
    },
  );

  it("inflight>0 不显示 unload 请求", () => {
    const gate = evaluateUnloadGate(
      makeMember({
        runtime: {
          health_fresh: true,
          route_inflight: 3,
          circuit_open: false,
          registry_state: "connected",
          last_selected_at: null,
          selection_count_window: null,
          rejection_count_window: null,
          p95_ms: null,
          error_rate: null,
        },
      }),
      "enforce",
      true,
    );
    expect(gate.can_request_unload).toBe(false);
    expect(gate.blocked_inflight).toBe(true);
  });

  it("陈旧执行状态不显示 unload 请求", () => {
    const gate = evaluateUnloadGate(makeMember(), "enforce", false);
    expect(gate.can_request_unload).toBe(false);
    expect(gate.blocked_stale).toBe(true);
  });

  it("disabled 成员也必须先进入 draining", () => {
    const gate = evaluateUnloadGate(
      makeMember({ routing: "blocked", traffic_state: "disabled" }),
      "enforce",
      true,
    );
    expect(gate.can_request_unload).toBe(false);
    expect(gate.blocked_routable).toBe(true);
  });
});

// ── pool sort ──────────────────────────────────────────────────────────────

describe("sortPoolsBySeverity", () => {
  it("offline 先于 degraded 先于 healthy", () => {
    const pools: PoolViewModel[] = [
      {
        id: "p1",
        name: "healthy-pool",
        enabled: true,
        routing_policy: "smooth_weighted_round_robin",
        legacy_instance_id: null,
        capability_fingerprint: null,
        routing_generation: 1,
        member_count: 1,
        routable_instances: 1,
        status: "healthy",
        status_reason_codes: [],
        members: [],
        availability: { routable: 1, total: 1, draining: 0, offline: 0 },
        capacity: { inflight: 0, limit: null, saturated: false },
        metrics_available: false,
      },
      {
        id: "p2",
        name: "offline-pool",
        enabled: true,
        routing_policy: "smooth_weighted_round_robin",
        legacy_instance_id: null,
        capability_fingerprint: null,
        routing_generation: 1,
        member_count: 1,
        routable_instances: 0,
        status: "offline",
        status_reason_codes: [],
        members: [],
        availability: { routable: 0, total: 1, draining: 0, offline: 1 },
        capacity: { inflight: 0, limit: null, saturated: false },
        metrics_available: false,
      },
    ];
    const sorted = sortPoolsBySeverity(pools);
    expect(sorted[0].name).toBe("offline-pool");
    expect(sorted[1].name).toBe("healthy-pool");
  });
});
