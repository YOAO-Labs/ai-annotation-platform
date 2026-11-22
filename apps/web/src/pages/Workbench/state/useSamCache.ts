import type { PendingCandidate } from "./useInteractiveAI";

const DEFAULT_CAP = 32;
const DEFAULT_MAX_BYTES = 32 * 1024 * 1024;
export const SAM_CACHE_TTL_MS = 2 * 60 * 1000;
const COORD_PRECISION = 4;

function roundCoord(n: number): number {
  const f = 10 ** COORD_PRECISION;
  return Math.round(n * f) / f;
}

function normalizeCtx(ctx: Record<string, unknown>): string {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(ctx).sort()) {
    const v = ctx[k];
    if (Array.isArray(v)) {
      out[k] = v.map((entry) => {
        if (Array.isArray(entry) && entry.every((e) => typeof e === "number")) {
          return entry.map((n) => roundCoord(n as number));
        }
        return entry;
      });
    } else if (typeof v === "number") {
      out[k] = roundCoord(v);
    } else {
      out[k] = v;
    }
  }
  return JSON.stringify(out);
}

export interface SamCacheKeyArgs {
  taskId: string;
  mlBackendId: string;
  ctxKind: string;
  ctx: Record<string, unknown>;
  scope?: string | number;
}

export function makeSamCacheKey({
  taskId,
  mlBackendId,
  ctxKind,
  ctx,
  scope,
}: SamCacheKeyArgs): string {
  const base = `${taskId}|${mlBackendId}|${ctxKind}|${normalizeCtx(ctx)}`;
  return scope === undefined ? base : `${base}|@${scope}`;
}

export interface SamCacheEntry {
  candidates: PendingCandidate[];
  expiresAt: number;
  byteSize: number;
}

export interface SamCache {
  get: (key: string) => SamCacheLookup | undefined;
  set: (key: string, candidates: PendingCandidate[]) => SamCacheLookup | undefined;
  delete: (key: string) => void;
  clearAll: () => void;
  readonly size: number;
  readonly byteSize: number;
  readonly stats: { hits: number; misses: number };
}

export interface SamCacheLookup {
  candidates: PendingCandidate[];
  expiresAt: number;
}

export interface SamCacheOptions {
  maxEntries?: number;
  maxBytes?: number;
  ttlMs?: number;
  now?: () => number;
}

export function estimatePendingCandidateBytes(candidate: PendingCandidate): number {
  const common = candidate.id.length * 2 + candidate.label.length * 2 + 128;
  if (candidate.type === "mask") {
    return (
      common +
      candidate.rle.counts.length * 8 +
      candidate.receipt.length * 2 +
      candidate.promptRevision.length * 2 +
      512
    );
  }
  if (candidate.type === "polygonlabels") {
    return common + candidate.points.length * 16;
  }
  return common + 32;
}

function normalizePositive(value: number | undefined, fallback: number): number {
  if (value == null || !Number.isFinite(value)) return fallback;
  return Math.max(1, Math.floor(value));
}

export function createSamCache(options: number | SamCacheOptions = {}): SamCache {
  const normalized = typeof options === "number" ? { maxEntries: options } : options;
  const maxEntries = normalizePositive(normalized.maxEntries, DEFAULT_CAP);
  const maxBytes = normalizePositive(normalized.maxBytes, DEFAULT_MAX_BYTES);
  const ttlMs = normalizePositive(normalized.ttlMs, SAM_CACHE_TTL_MS);
  const now = normalized.now ?? Date.now;
  const map = new Map<string, SamCacheEntry>();
  let hits = 0;
  let misses = 0;
  let byteSize = 0;

  const remove = (key: string) => {
    const entry = map.get(key);
    if (!entry) return;
    map.delete(key);
    byteSize -= entry.byteSize;
  };

  const evict = () => {
    while (map.size > maxEntries || byteSize > maxBytes) {
      const oldestKey = map.keys().next().value;
      if (oldestKey === undefined) break;
      remove(oldestKey);
    }
  };

  return {
    get(key) {
      const entry = map.get(key);
      if (!entry) {
        misses += 1;
        return undefined;
      }
      if (entry.expiresAt <= now()) {
        remove(key);
        misses += 1;
        return undefined;
      }
      map.delete(key);
      map.set(key, entry);
      hits += 1;
      return { candidates: entry.candidates, expiresAt: entry.expiresAt };
    },
    set(key, candidates) {
      remove(key);
      const entryBytes = candidates.reduce(
        (total, candidate) => total + estimatePendingCandidateBytes(candidate),
        0,
      );
      if (entryBytes > maxBytes) return undefined;
      map.set(key, {
        candidates,
        expiresAt: now() + ttlMs,
        byteSize: entryBytes,
      });
      byteSize += entryBytes;
      evict();
      const entry = map.get(key);
      return entry ? { candidates: entry.candidates, expiresAt: entry.expiresAt } : undefined;
    },
    delete(key) {
      remove(key);
    },
    clearAll() {
      map.clear();
      byteSize = 0;
    },
    get size() {
      return map.size;
    },
    get byteSize() {
      return byteSize;
    },
    get stats() {
      return { hits, misses };
    },
  };
}
