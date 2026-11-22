import type { PoolLoadedKey } from "@/api/adminMlIntegrations";

const GSAM2_IMAGE_KEY = /^sam=(.+?)\/dino=(.+)$/;

export interface Gsam2ImageVariant {
  sam_variant: string;
  dino_variant: string;
}

export function parseGsam2ImageKey(key: string): Gsam2ImageVariant | null {
  const m = key.match(GSAM2_IMAGE_KEY);
  if (!m) return null;
  return { sam_variant: m[1], dino_variant: m[2] };
}

export function loadedKeysAsGsam2ImageVariants(
  keys: PoolLoadedKey[] | undefined,
): Gsam2ImageVariant[] {
  if (!keys) return [];
  const out: Gsam2ImageVariant[] = [];
  for (const k of keys) {
    const v = parseGsam2ImageKey(k.key);
    if (v) out.push(v);
  }
  return out;
}

export function lastUsedSecondsAgo(iso: string | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

export function loadedKeysLastUsedMap(keys: PoolLoadedKey[] | undefined): Record<string, number> {
  if (!keys) return {};
  const out: Record<string, number> = {};
  for (const k of keys) {
    const s = lastUsedSecondsAgo(k.last_used_at);
    if (s != null) out[k.key] = s;
  }
  return out;
}

export function gsam2ImageVariantsAsCacheBucketKey(v: Gsam2ImageVariant): string {
  return `${v.sam_variant}/${v.dino_variant}`;
}

export function gsam2ImageVariantsAsLoadedKey(v: Gsam2ImageVariant): string {
  return `sam=${v.sam_variant}/dino=${v.dino_variant}`;
}
