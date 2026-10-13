/**
 * v0.23.4 P3 · small shared helpers for the registry section components.
 *
 * Pure presentation logic only — no React, no query. The four section
 * components (ServicePools / BackendInstances / GPUResources /
 * ProjectBindings) and IssueCenter reuse these to keep labels and "unknown"
 * sentinels consistent with the frozen contracts (plan Appendix A.2).
 */
import type { GlobalBackendItem } from "@/api/adminMlIntegrations";
import type { TopologyPoolEntry, TopologyMemberInstance } from "@/api/generated/types.gen";
import type { MemberViewModel, PoolViewModel } from "../runtimeTopology";

/**
 * Compact display form of a stable id (plan §4.3: 名称 + 短 ID 优先，完整值靠
 * tooltip / 复制 / 详情)。≤12 字符原样显示；更长的取前 8 字符 + 省略号。
 * 不发明名称——只截断真实值。
 */
export function formatShortId(value: string): string {
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}…`;
}

/** Format an ISO timestamp as a localized, hour-24 string. Returns "—" on null. */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return iso;
  }
}

/** Format an ISO timestamp as a compact HH:mm time. Returns "—" on null. */
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

/** Copy text to the clipboard and resolve to whether the write succeeded. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

/** Map a legacy connected/disconnected/error registry state → health axis. */
export function registryStateToHealthAxis(
  state: string | null | undefined,
): "healthy" | "degraded" | "offline" | "unknown" {
  if (!state) return "unknown";
  if (state === "connected") return "healthy";
  if (state === "error") return "offline";
  if (state === "disconnected") return "degraded";
  return "unknown";
}

/**
 * Build a lookup from a topology response: registry_id → owning pool.
 *
 * Plan Appendix A.4: the instance ↔ pool relationship is only valid via stable
 * `service_pool_id` ↔ `registry_id`, never via URL. Used by the instance table
 * to show "所属服务池" without a second network call.
 */
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

/** Read the GPU resource claim summary off a global backend item, null-safe. */
export function gpuClaimOf(
  backend: GlobalBackendItem,
): { gpu_resource_id: string; vram_budget_mb: number } | null {
  if (!backend.gpu_resource_id) return null;
  const budget = backend.vram_budget_mb;
  if (budget == null) return null;
  return { gpu_resource_id: backend.gpu_resource_id, vram_budget_mb: budget };
}

/** Sentinel used everywhere a routing metric is null (plan Appendix A.2). */
export const NO_METRICS = "暂无路由指标";
/** Sentinel used where a configured limit is null. */
export const NO_LIMIT = "未声明";
/** Sentinel for unknown routing policy (Project Admin projection). */
export const UNKNOWN_POLICY = "未知";
