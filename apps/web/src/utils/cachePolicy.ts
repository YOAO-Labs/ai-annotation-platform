import type { DatasetSourceOut } from "@/api/generated/types.gen";

export type CachePolicy = DatasetSourceOut["cache_policy"];
export const cachePolicyLabels: Record<CachePolicy, string> = {
  none: "无持久缓存",
  preview: "仅预览",
  on_demand: "按需原图",
};

export function originalCacheValues(policy: CachePolicy, quotaMiB: string, ttl: string) {
  const quotaBytes = Number(quotaMiB) * 1024 ** 2;
  const ttlSeconds = Number(ttl);
  return {
    original_cache_quota_bytes: quotaBytes,
    original_cache_ttl_seconds: ttlSeconds,
    valid:
      quotaMiB.trim() !== "" &&
      ttl.trim() !== "" &&
      Number.isSafeInteger(quotaBytes) &&
      quotaBytes >= 0 &&
      quotaBytes <= 2 ** 50 &&
      (policy !== "on_demand" || quotaBytes > 0) &&
      Number.isSafeInteger(ttlSeconds) &&
      ttlSeconds >= 60 &&
      ttlSeconds <= 2592000,
  };
}

export function cacheBytes(value: number) {
  if (value < 1024) return `${value.toLocaleString()} B`;
  if (value < 1024 ** 2)
    return `${(value / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 })} KiB`;
  if (value < 1024 ** 3)
    return `${(value / 1024 ** 2).toLocaleString(undefined, { maximumFractionDigits: 2 })} MiB`;
  return `${(value / 1024 ** 3).toLocaleString(undefined, { maximumFractionDigits: 2 })} GiB`;
}
