import type {
  GPUArbiterResourceItem,
  RuntimeMemberSnapshot,
  RuntimePoolSnapshot,
  RuntimeSnapshotResponse,
  SourceFreshness,
  TopologyMemberInstance,
  TopologyPoolEntry,
  TopologyResponse,
} from "@/api/generated/types.gen";

export type HealthAxis = "healthy" | "degraded" | "offline" | "unknown";
export type RoutingAxis = "routable" | "draining" | "bypassed" | "blocked" | "unknown";
export type CapacityAxis = "idle" | "serving" | "saturated" | "unknown";
export type ResidencyAxis = "empty" | "loading" | "resident" | "draining" | "unloading" | "unknown";

export type Severity = "info" | "warning" | "critical" | "blocker";

export type DiagnosticSubjectType = "service_pool" | "instance" | "gpu_resource" | "model_pool";

export interface Diagnostic {
  id: string;
  code: string;
  severity: Severity;
  subject_type: DiagnosticSubjectType;
  subject_id: string;
  message: string;
  remediation?: string;
  affected_service_pool_ids: string[];
  affected_instance_ids: string[];
  affected_gpu_resource_ids: string[];
  observed_at: string;
  source: "topology" | "runtime_snapshot" | "gpu_resources";
}

export interface MemberViewModel {
  registry_id: string;
  name: string;
  traffic_state: TopologyMemberInstance["traffic_state"];
  weight: number | null;
  state: string | null;
  last_checked_at: string | null;
  gpu_resource_id: string | null;
  runtime?: {
    health_fresh: boolean;
    route_inflight: number | null;
    circuit_open: boolean | null;
    registry_state: string;
    last_selected_at: string | null;
    selection_count_window: number | null;
    rejection_count_window: number | null;
    p95_ms: number | null;
    error_rate: number | null;
  };
  routing: RoutingAxis;
  capacity: CapacityAxis;
}

export interface PoolViewModel {
  id: string;
  name: string;
  enabled: boolean;
  routing_policy: string;
  legacy_instance_id: string | null;
  capability_fingerprint: string | null;
  routing_generation: number;
  member_count: number;
  routable_instances: number;
  status: TopologyPoolEntry["status"];
  status_reason_codes: string[];
  members: MemberViewModel[];
  availability: {
    routable: number;
    total: number;
    draining: number;
    offline: number;
  };
  capacity: {
    inflight: number | null;
    limit: number | null;
    saturated: boolean;
  };
  metrics_available: boolean;
}

export interface FreshnessViewModel {
  name: SourceFreshness["name"];
  label: string;
  updated_at: string | null;
  stale: boolean;
  error: string | null;
}

export interface RuntimeTopologyViewModel {
  pools: PoolViewModel[];
  router_mode: RuntimeSnapshotResponse["router_mode"];
  generated_at: string;
  observed_at: string | null;
  partial: boolean;
  partial_reason: string | null;
  sources: FreshnessViewModel[];
  runtime_available: boolean;
}

export const METRICS_AVAILABLE = false as const;

export const FRESHNESS_LABELS: Record<SourceFreshness["name"], string> = {
  topology: "拓扑",
  router_ledger: "路由执行状态",
  health: "健康探活",
  gpu: "GPU 仲裁",
  residency: "模型驻留",
};

export const NO_METRICS_LABEL = "暂无路由指标";

export function mergeTopologyAndSnapshot(
  topology: TopologyResponse,
  snapshot: RuntimeSnapshotResponse | null,
): RuntimeTopologyViewModel {
  const snapByPool = new Map<string, RuntimePoolSnapshot>();
  if (snapshot) {
    for (const p of snapshot.pools ?? []) snapByPool.set(p.id, p);
  }
  const pools: PoolViewModel[] = (topology.pools ?? []).map((pool) =>
    toPoolViewModel(pool, snapByPool.get(pool.id) ?? null),
  );

  const sources: FreshnessViewModel[] = (snapshot?.sources ?? []).map(toFreshness);

  return {
    pools,
    router_mode: snapshot?.router_mode ?? topology.router_mode,
    generated_at: topology.generated_at,
    observed_at: snapshot?.observed_at ?? null,
    partial: snapshot?.partial ?? false,
    partial_reason: snapshot?.partial_reason ?? null,
    sources,
    runtime_available: snapshot !== null,
    ...(snapshot === null
      ? {
          partial: true,
          partial_reason: "runtime_snapshot_unavailable",
        }
      : {}),
  } as RuntimeTopologyViewModel;
}

function toPoolViewModel(pool: TopologyPoolEntry, snap: RuntimePoolSnapshot | null): PoolViewModel {
  const snapMembers = new Map<string, RuntimeMemberSnapshot>();
  if (snap) {
    for (const m of snap.members ?? []) snapMembers.set(m.registry_id, m);
  }
  const members: MemberViewModel[] = (pool.members ?? []).map((m) =>
    toMemberViewModel(m, snapMembers.get(m.registry_id) ?? null, pool),
  );

  const draining = members.filter((m) => m.traffic_state === "draining").length;
  const disabled = members.filter((m) => m.traffic_state === "disabled").length;
  const routable = pool.routable_instances;

  const runtimeInflight = snap?.members?.map((m) => m.route_inflight) ?? [];
  const inflight =
    snap && runtimeInflight.every((value) => value != null)
      ? runtimeInflight.reduce((sum, value) => sum + (value ?? 0), 0)
      : null;
  const limit = null;
  const saturated = snap ? (snap.members ?? []).some((m) => m.circuit_open) : false;

  return {
    id: pool.id,
    name: pool.name,
    enabled: pool.enabled,
    routing_policy: pool.routing_policy,
    legacy_instance_id: pool.legacy_instance_id ?? null,
    capability_fingerprint: pool.capability_fingerprint ?? null,
    routing_generation: pool.routing_generation,
    member_count: pool.member_count,
    routable_instances: routable,
    status: pool.status,
    status_reason_codes: pool.status_reason_codes ?? [],
    members,
    availability: {
      routable,
      total: pool.member_count,
      draining,
      offline: disabled,
    },
    capacity: { inflight, limit, saturated },
    metrics_available: METRICS_AVAILABLE,
  };
}

function toMemberViewModel(
  m: TopologyMemberInstance,
  snap: RuntimeMemberSnapshot | null,
  pool: TopologyPoolEntry,
): MemberViewModel {
  const routing = deriveMemberRouting(m.traffic_state, snap, pool.routing_policy);

  return {
    registry_id: m.registry_id,
    name: m.name,
    traffic_state: m.traffic_state,
    weight: m.weight ?? null,
    state: m.state ?? null,
    last_checked_at: m.last_checked_at ?? null,
    gpu_resource_id: m.gpu_resource_id ?? null,
    runtime: snap
      ? {
          health_fresh: snap.health_fresh,
          route_inflight: snap.route_inflight ?? null,
          circuit_open: snap.circuit_open ?? null,
          registry_state: snap.registry_state,
          last_selected_at: snap.last_selected_at ?? null,
          selection_count_window: snap.selection_count_window ?? null,
          rejection_count_window: snap.rejection_count_window ?? null,
          p95_ms: snap.p95_ms ?? null,
          error_rate: snap.error_rate ?? null,
        }
      : undefined,
    routing,
    capacity: deriveMemberCapacity(snap),
  };
}

export function deriveMemberRouting(
  trafficState: TopologyMemberInstance["traffic_state"],
  snap: RuntimeMemberSnapshot | null,
  routingPolicy: string,
): RoutingAxis {
  if (routingPolicy === "unknown") return "unknown";
  if (trafficState === "disabled") return "blocked";
  if (trafficState === "draining") return "draining";
  if (snap?.circuit_open) return "blocked";
  return "routable";
}

export function deriveMemberCapacity(snap: RuntimeMemberSnapshot | null): CapacityAxis {
  if (!snap || snap.route_inflight == null || snap.circuit_open == null) return "unknown";
  if (snap.circuit_open) return "saturated";
  if (snap.route_inflight > 0) return "serving";
  return "idle";
}

function toFreshness(s: SourceFreshness): FreshnessViewModel {
  return {
    name: s.name,
    label: FRESHNESS_LABELS[s.name] ?? s.name,
    updated_at: s.updated_at ?? null,
    stale: s.stale ?? false,
    error: s.error ?? null,
  };
}

export function collectDiagnostics(
  topology: TopologyResponse,
  snapshot: RuntimeSnapshotResponse | null,
  gpuResources: GPUArbiterResourceItem[] | null,
): Diagnostic[] {
  const byKey = new Map<string, Diagnostic>();
  const now = new Date().toISOString();

  for (const pool of topology.pools ?? []) {
    if (pool.status === "offline") {
      pushDiagnostic(byKey, {
        id: `pool_offline:service_pool:${pool.id}`,
        code: "pool_offline",
        severity: "critical",
        subject_type: "service_pool",
        subject_id: pool.id,
        message: `服务池「${pool.name}」无可路由实例（全部 disabled）`,
        remediation: "恢复至少一个成员的接流状态或加入新成员",
        affected_service_pool_ids: [pool.id],
        affected_instance_ids: (pool.members ?? []).map((m) => m.registry_id),
        affected_gpu_resource_ids: [],
        observed_at: topology.generated_at,
        source: "topology",
      });
    } else if (pool.status === "degraded") {
      pushDiagnostic(byKey, {
        id: `pool_degraded:service_pool:${pool.id}`,
        code: "pool_degraded",
        severity: "warning",
        subject_type: "service_pool",
        subject_id: pool.id,
        message: `服务池「${pool.name}」处于降级（${(pool.status_reason_codes ?? []).join(", ") || "原因未知"}）`,
        affected_service_pool_ids: [pool.id],
        affected_instance_ids: [],
        affected_gpu_resource_ids: [],
        observed_at: topology.generated_at,
        source: "topology",
      });
    }
  }

  if (snapshot) {
    for (const pool of snapshot.pools ?? []) {
      for (const m of pool.members ?? []) {
        if (m.circuit_open) {
          pushDiagnostic(byKey, {
            id: `circuit_open:instance:${m.registry_id}`,
            code: "circuit_open",
            severity: "critical",
            subject_type: "instance",
            subject_id: m.registry_id,
            message: `实例「${m.name}」被动熔断中（连续传输失败）`,
            remediation: "检查实例可达性，恢复后熔断会自动半开试探",
            affected_service_pool_ids: [pool.id],
            affected_instance_ids: [m.registry_id],
            affected_gpu_resource_ids: m.gpu_resource_id ? [m.gpu_resource_id] : [],
            observed_at: snapshot.observed_at,
            source: "runtime_snapshot",
          });
        }
        if (!m.health_fresh) {
          pushDiagnostic(byKey, {
            id: `health_stale:instance:${m.registry_id}`,
            code: "health_stale",
            severity: "warning",
            subject_type: "instance",
            subject_id: m.registry_id,
            message: `实例「${m.name}」健康快照过期`,
            remediation: "触发一次健康检查刷新探活结果",
            affected_service_pool_ids: [pool.id],
            affected_instance_ids: [m.registry_id],
            affected_gpu_resource_ids: [],
            observed_at: snapshot.observed_at,
            source: "runtime_snapshot",
          });
        }
      }
    }
    for (const s of snapshot.sources ?? []) {
      if (s.stale && s.name === "router_ledger") {
        pushDiagnostic(byKey, {
          id: `router_ledger_unavailable:service_pool:_global`,
          code: "router_ledger_unavailable",
          severity: "warning",
          subject_type: "service_pool",
          subject_id: "_global",
          message: `路由执行状态不可用：${s.error ?? "原因未知"}（活动执行数或熔断提示可能滞后）`,
          remediation: "检查运行状态来源；无法确认活动执行数时，卸载请求会被禁用",
          affected_service_pool_ids: (snapshot.pools ?? []).map((p) => p.id),
          affected_instance_ids: [],
          affected_gpu_resource_ids: [],
          observed_at: snapshot.observed_at,
          source: "runtime_snapshot",
        });
      }
    }
  }

  if (gpuResources) {
    for (const r of gpuResources) {
      if (r.status === "critical" || r.status === "blocker") {
        const sev: Severity = r.status === "blocker" ? "blocker" : "critical";
        pushDiagnostic(byKey, {
          id: `gpu_${r.status}:gpu_resource:${r.gpu_resource_id}`,
          code: `gpu_${r.status}`,
          severity: sev,
          subject_type: "gpu_resource",
          subject_id: r.gpu_resource_id,
          message: `GPU 资源「${r.gpu_resource_id}」状态 ${r.status}`,
          remediation: "查看资源诊断与仲裁状态",
          affected_service_pool_ids: [],
          affected_instance_ids: [],
          affected_gpu_resource_ids: [r.gpu_resource_id],
          observed_at: now,
          source: "gpu_resources",
        });
      } else if (r.status === "warning") {
        pushDiagnostic(byKey, {
          id: `gpu_warning:gpu_resource:${r.gpu_resource_id}`,
          code: "gpu_warning",
          severity: "warning",
          subject_type: "gpu_resource",
          subject_id: r.gpu_resource_id,
          message: `GPU 资源「${r.gpu_resource_id}」存在告警`,
          affected_service_pool_ids: [],
          affected_instance_ids: [],
          affected_gpu_resource_ids: [r.gpu_resource_id],
          observed_at: now,
          source: "gpu_resources",
        });
      }
    }
  }

  return Array.from(byKey.values());
}

function pushDiagnostic(map: Map<string, Diagnostic>, d: Diagnostic): void {
  const existing = map.get(d.id);
  if (!existing) {
    map.set(d.id, d);
    return;
  }
  const sevRank: Record<Severity, number> = {
    info: 0,
    warning: 1,
    critical: 2,
    blocker: 3,
  };
  const merged: Diagnostic = {
    ...existing,
    severity: sevRank[d.severity] > sevRank[existing.severity] ? d.severity : existing.severity,
    affected_service_pool_ids: dedup([
      ...existing.affected_service_pool_ids,
      ...d.affected_service_pool_ids,
    ]),
    affected_instance_ids: dedup([...existing.affected_instance_ids, ...d.affected_instance_ids]),
    affected_gpu_resource_ids: dedup([
      ...existing.affected_gpu_resource_ids,
      ...d.affected_gpu_resource_ids,
    ]),
    observed_at: existing.observed_at < d.observed_at ? existing.observed_at : d.observed_at,
  };
  map.set(d.id, merged);
}

function dedup(xs: string[]): string[] {
  return Array.from(new Set(xs));
}

export interface UnloadRequestGate {
  blocked_routable: boolean;
  blocked_inflight: boolean;
  blocked_stale: boolean;
  /** @deprecated Always false; draining is enforced in every router mode. */
  blocked_shadow_mode: false;
  can_request_unload: boolean;
  /** @deprecated Compatibility alias for can_request_unload; server decides acceptance. */
  can_unload: boolean;
  reasons: string[];
}

export function evaluateUnloadGate(
  member: MemberViewModel,
  /** @deprecated Retained for callers; router mode does not change drain gating. */
  _routerMode: RuntimeSnapshotResponse["router_mode"],
  ledgerFresh: boolean,
): UnloadRequestGate {
  const reasons: string[] = [];
  const blocked_routable = member.traffic_state !== "draining";
  if (blocked_routable) {
    reasons.push("实例必须先进入 draining 状态，才能确认已停止接收新路由执行");
  }
  const inflight = member.runtime?.route_inflight;
  const blocked_inflight = inflight != null && inflight > 0;
  if (blocked_inflight) {
    reasons.push(`仍有 ${inflight} 个执行操作未结束，需等待归零`);
  }
  const blocked_stale = member.runtime === undefined || inflight == null || !ledgerFresh;
  if (blocked_stale) {
    reasons.push("路由执行状态不可用或已过期，无法确认活动执行已归零");
  }
  const can_request_unload = !blocked_routable && !blocked_inflight && !blocked_stale;
  return {
    blocked_routable,
    blocked_inflight,
    blocked_stale,
    blocked_shadow_mode: false,
    can_request_unload,
    can_unload: can_request_unload,
    reasons,
  };
}

const SEVERITY_RANK: Record<Severity, number> = {
  blocker: 0,
  critical: 1,
  warning: 2,
  info: 3,
};

export function sortDiagnostics(list: Diagnostic[]): Diagnostic[] {
  return [...list].sort((a, b) => {
    const sev = SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity];
    if (sev !== 0) return sev;
    return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
  });
}

export interface DiagnosticFilter {
  pool_id?: string;
  instance_id?: string;
  gpu_resource_id?: string;
  code?: string;
}

export function filterDiagnostics(list: Diagnostic[], filter: DiagnosticFilter): Diagnostic[] {
  return list.filter((d) => {
    if (filter.pool_id && !d.affected_service_pool_ids.includes(filter.pool_id)) return false;
    if (filter.instance_id && !d.affected_instance_ids.includes(filter.instance_id)) return false;
    if (filter.gpu_resource_id && !d.affected_gpu_resource_ids.includes(filter.gpu_resource_id))
      return false;
    if (filter.code && d.code !== filter.code) return false;
    return true;
  });
}

export function sortPoolsBySeverity(pools: PoolViewModel[]): PoolViewModel[] {
  const rank: Record<TopologyPoolEntry["status"], number> = {
    offline: 0,
    degraded: 1,
    unknown: 2,
    healthy: 3,
  };
  return [...pools].sort((a, b) => {
    const sev = rank[a.status] - rank[b.status];
    if (sev !== 0) return sev;
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });
}

export function derivePoolEffectiveRouting(
  pool: PoolViewModel,
): "routable" | "draining" | "blocked" | "bypassed" | "unknown" {
  if (pool.routing_policy === "unknown") return "unknown";
  if (pool.availability.routable > 0) {
    return "routable";
  }
  if (pool.availability.draining > 0 && pool.availability.routable === 0) return "draining";
  if (pool.status === "offline") return "blocked";
  return "unknown";
}
