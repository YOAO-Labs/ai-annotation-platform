// PoolStatus.loaded_keys 中 backend-specific key 字符串的解析 helpers.
// 协议层 loaded_keys[].key 是不透明字符串 (各 backend 自定义命名), 但 ModelMarket
// 这层 admin UI 想按 sam/dino 拆维度展示, 所以认定 gsam2 image pool 的 key 形如
// "sam=<sv>/dino=<dv>". 不是 gsam2 的 backend 走 key 原样展示.

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

/**
 * 把 loaded_keys 拆成 gsam2 image pool 视角的 {sam, dino} 数组; 解析失败的 key 丢弃.
 * 上层 fallback 路径 (老 backend 仅有 loaded_variants[]) 由调用方处理.
 */
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

/**
 * 把 loaded_keys 中的 last_used_at (ISO 字符串) 还原成相对启动 t0 的秒数 — 当前
 * 没有显式 t0, 临时实现: 相对于 ISO 数组中最早 loaded_at 之前的偏移没意义, 直接
 * 用相对"现在"的秒数 (negative ⇒ 未来, 实际不会发生). 调用方按 `t-Xs` 展示更直观.
 */
export function lastUsedSecondsAgo(iso: string | undefined): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return Math.max(0, (Date.now() - t) / 1000);
}

/**
 * 同上, 但写成 lastUsedSecondsAgo 的 map 形式, 便于按 backend-specific key 查询.
 * key → 秒数 (相对现在). 缺失的 last_used_at 不入 map.
 */
export function loadedKeysLastUsedMap(keys: PoolLoadedKey[] | undefined): Record<string, number> {
  if (!keys) return {};
  const out: Record<string, number> = {};
  for (const k of keys) {
    const s = lastUsedSecondsAgo(k.last_used_at);
    if (s != null) out[k.key] = s;
  }
  return out;
}

/** 兼容老字段视图: lru_ts 形如 Record<sam/dino, monotonic_seconds>, video 形如 Record<sam, ...>. */
export function gsam2ImageVariantsAsCacheBucketKey(v: Gsam2ImageVariant): string {
  return `${v.sam_variant}/${v.dino_variant}`;
}

export function gsam2ImageVariantsAsLoadedKey(v: Gsam2ImageVariant): string {
  return `sam=${v.sam_variant}/dino=${v.dino_variant}`;
}
