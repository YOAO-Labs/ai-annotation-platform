import type { ResidencyAxis } from "../runtimeTopology";

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
  strictEmpty: boolean;
  malformed: boolean;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

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

export function effectiveGpuLoaded(
  residency: NormalizedResidency,
  trusted: boolean,
): boolean | null {
  if (trusted && residency.gpuLoaded === true) return true;
  if (trusted && !residency.malformed && residency.strictEmpty) return false;
  return null;
}

export function isActiveResidency(value: unknown, trusted: boolean): boolean {
  if (!trusted) return false;
  const residency = parseResidency(value);
  if (!residency) return false;
  const axis = residencyStateToAxis(residency.state);
  return axis === "resident" || axis === "loading" || axis === "draining";
}

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
