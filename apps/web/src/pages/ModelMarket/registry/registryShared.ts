import type { GlobalBackendItem } from "@/api/adminMlIntegrations";
import type { TopologyPoolEntry, TopologyMemberInstance } from "@/api/generated/types.gen";
import type { MemberViewModel, PoolViewModel } from "../runtimeTopology";

export interface DuplicateNameOrdinal {
  index: number;
  total: number;
}

export function duplicateNameOrdinals<T>(
  items: readonly T[],
  getId: (item: T) => string,
  getName: (item: T) => string,
): Map<string, DuplicateNameOrdinal> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const name = getName(item).trim();
    const group = groups.get(name);
    if (group) group.push(item);
    else groups.set(name, [item]);
  }
  const ordinals = new Map<string, DuplicateNameOrdinal>();
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const sorted = [...group].sort((left, right) => getId(left).localeCompare(getId(right)));
    sorted.forEach((item, index) => {
      ordinals.set(getId(item), { index: index + 1, total: sorted.length });
    });
  }
  return ordinals;
}

export function formatShortId(value: string): string {
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}…`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function registryStateToHealthAxis(
  state: string | null | undefined,
): "healthy" | "degraded" | "offline" | "unknown" {
  if (!state) return "unknown";
  if (state === "connected") return "healthy";
  if (state === "error") return "offline";
  if (state === "disconnected") return "degraded";
  return "unknown";
}

export function buildRegistryToPoolMap(
  pools: PoolViewModel[] | TopologyPoolEntry[],
): Map<string, { poolId: string; poolName: string }> {
  const out = new Map<string, { poolId: string; poolName: string }>();
  for (const p of pools) {
    const members: MemberViewModel[] | TopologyMemberInstance[] =
      (p as PoolViewModel).members ?? (p as TopologyPoolEntry).members ?? [];
    for (const m of members) {
      out.set(m.registry_id, { poolId: p.id, poolName: p.name });
    }
  }
  return out;
}

export function gpuClaimOf(
  backend: GlobalBackendItem,
): { gpu_resource_id: string; vram_budget_mb: number } | null {
  if (!backend.gpu_resource_id) return null;
  const budget = backend.vram_budget_mb;
  if (budget == null) return null;
  return { gpu_resource_id: backend.gpu_resource_id, vram_budget_mb: budget };
}

export const NO_METRICS = "暂无路由指标";
export const NO_LIMIT = "未声明";
export const UNKNOWN_POLICY = "未知";
