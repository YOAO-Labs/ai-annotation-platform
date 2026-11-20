/**
 * pure parser for the defensive ML Backend residency payload.
 *
 * Extracted from the legacy `RuntimeObservePanel.tsx` (P4) so the new
 * `InstanceDetailSheet` and any future diagnostic surface can consume the same
 * normalized shape. Parsing behavior is unchanged (malformed-field detection,
 * `gpu_loaded=false` handling, pool normalization, identity extraction).
 *
 * The residency payload comes from `/observe` (a per-URL direct probe) and is
 * untrusted: backends may omit fields, send nested objects of the wrong shape,
 * or return partial payloads on degraded probes. This module normalizes them
 * into a stable {@link NormalizedResidency} (or `null` when the top-level
 * shape is unrecognizable) and flags malformed leaves via `malformed: true`
 * so the UI can render a caution note instead of inferring "GPU empty".
 *
 * Plan §A.1 (residency axis) + §A.2: never smooth unknown into healthy/empty.
 */
import type { ResidencyAxis } from "../runtimeTopology";

/** States reported by `/observe` residency.state. */
const RESIDENCY_STATES = new Set([
  "unloaded",
  "loading",
  "resident",
  "draining",
  "unloading",
  "unknown",
]);

export type ResidencyState =
  | "unloaded"
  | "loading"
  | "resident"
  | "draining"
  | "unloading"
  | "unknown";

/** Map a raw residency.state string → the 4-axis residency token value. */
export function residencyStateToAxis(state: ResidencyState): ResidencyAxis {
  switch (state) {
    case "resident":
      return "resident";
    case "loading":
      return "loading";
    case "unloading":
    case "draining":
      return "draining";
    case "unloaded":
      return "empty";
    default:
      return "unknown";
  }
}

export interface NormalizedResidencyPool {
  id: string;
  resident: boolean | null;
  device: string | null;
  provider: string | null;
}

export interface NormalizedResidency {
  state: ResidencyState;
  gpuLoaded: boolean | null;
  activeRequests: number | null;
  builders: number | null;
  borrowers: number | null;
  draining: boolean | null;
  evictable: boolean | null;
  lifecycleGate: string | null;
  generation: string | null;
  identityResourceId: string | null;
  pools: NormalizedResidencyPool[];
  /**
   * Strict empty: gpu_loaded=false AND builders=0 AND borrowers=0 AND every
   * pool reports resident=false. Used to safely conclude "GPU empty" only
   * when the whole payload agrees.
   */
  strictEmpty: boolean;
  /** True iff any leaf field had an unexpected type (coerced to null). */
  malformed: boolean;
}

/** Object (non-array) guard for defensive parsing. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/**
 * Parse the raw `/observe` residency payload into a normalized shape.
 *
 * Returns `null` only when the top-level value is not a record or `state`
 * is missing/unknown — i.e. we cannot even identify what the backend meant.
 * Malformed leaves are kept and flagged via `malformed: true`; the caller
 * decides whether to trust them.
 */
export function parseResidency(value: unknown): NormalizedResidency | null {
  if (!isRecord(value)) return null;
  const state = value.state;
  if (typeof state !== "string" || !RESIDENCY_STATES.has(state)) return null;

  let malformed = false;
  const readBoolean = (key: string): boolean | null => {
    const raw = value[key];
    if (raw == null) return null;
    if (typeof raw === "boolean") return raw;
    malformed = true;
    return null;
  };
  const readInteger = (key: string): number | null => {
    const raw = value[key];
    if (raw == null) return null;
    if (typeof raw === "number" && Number.isInteger(raw)) return raw;
    malformed = true;
    return null;
  };
  const readString = (key: string): string | null => {
    const raw = value[key];
    if (raw == null) return null;
    if (typeof raw === "string") return raw;
    malformed = true;
    return null;
  };

  const gpuLoaded = readBoolean("gpu_loaded");
  const activeRequests = readInteger("active_requests");
  const builders = readInteger("builders");
  const borrowers = readInteger("borrowers");
  const draining = readBoolean("draining");
  const evictable = readBoolean("evictable");
  const lifecycleGate = readString("lifecycle_gate");
  const generation = readString("generation");

  let identityResourceId: string | null = null;
  if (value.identity != null) {
    if (!isRecord(value.identity)) {
      malformed = true;
    } else if (value.identity.gpu_resource_id != null) {
      if (typeof value.identity.gpu_resource_id === "string") {
        identityResourceId = value.identity.gpu_resource_id;
      } else {
        malformed = true;
      }
    }
  }

  const pools: NormalizedResidencyPool[] = [];
  let poolsValid = false;
  if (isRecord(value.pools)) {
    poolsValid = true;
    for (const [id, rawPool] of Object.entries(value.pools)) {
      if (!isRecord(rawPool)) {
        malformed = true;
        poolsValid = false;
        continue;
      }
      const rawResident = rawPool.resident;
      const resident =
        rawResident == null ? null : typeof rawResident === "boolean" ? rawResident : null;
      if (rawResident != null && typeof rawResident !== "boolean") {
        malformed = true;
        poolsValid = false;
      }
      const device =
        rawPool.device == null ? null : typeof rawPool.device === "string" ? rawPool.device : null;
      const provider =
        rawPool.provider == null
          ? null
          : typeof rawPool.provider === "string"
            ? rawPool.provider
            : null;
      if (
        (rawPool.device != null && typeof rawPool.device !== "string") ||
        (rawPool.provider != null && typeof rawPool.provider !== "string")
      ) {
        malformed = true;
      }
      pools.push({ id, resident, device, provider });
    }
  } else if (value.pools != null) {
    malformed = true;
  }

  const strictEmpty =
    gpuLoaded === false &&
    builders === 0 &&
    borrowers === 0 &&
    poolsValid &&
    pools.every((pool) => pool.resident === false);

  return {
    state: state as ResidencyState,
    gpuLoaded,
    activeRequests,
    builders,
    borrowers,
    draining,
    evictable,
    lifecycleGate,
    generation,
    identityResourceId,
    pools,
    strictEmpty,
    malformed,
  };
}

/**
 * Derive the effective "GPU loaded" truth from a normalized residency + a
 * trusted flag (fresh direct probe vs stale cached health).
 *
 * - trusted + gpu_loaded=true → true
 * - trusted + (no malformed) + strictEmpty → false
 * - otherwise → null (unknown; do not infer empty)
 */
export function effectiveGpuLoaded(
  residency: NormalizedResidency,
  trusted: boolean,
): boolean | null {
  if (trusted && residency.gpuLoaded === true) return true;
  if (trusted && !residency.malformed && residency.strictEmpty) return false;
  return null;
}

/**
 * Whether a trusted residency reading proves that the instance is occupying,
 * acquiring, or releasing residency resources. Empty and unknown readings do
 * not contribute to pool-level resident counts.
 */
export function isActiveResidency(value: unknown, trusted: boolean): boolean {
  if (!trusted) return false;
  const residency = parseResidency(value);
  if (!residency) return false;
  const axis = residencyStateToAxis(residency.state);
  return axis === "resident" || axis === "loading" || axis === "draining";
}

/**
 * Whether cached `state === "connected"` health is fresh enough to trust as a
 * residency source. Mirrors the legacy panel's 3-minute freshness window.
 *
 * Reusable across any "is this cached health reading still trustworthy?" call
 * site (registry list, runtime snapshot fallback, …).
 */
export function isFreshCachedHealth(
  state: string,
  lastCheckedAt: string | null,
  now: number = Date.now(),
): boolean {
  if (state !== "connected" || !lastCheckedAt) return false;
  const checkedAt = Date.parse(lastCheckedAt);
  if (!Number.isFinite(checkedAt)) return false;
  const ageMs = now - checkedAt;
  return ageMs >= -60_000 && ageMs <= 180_000;
}
