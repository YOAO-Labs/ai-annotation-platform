import { useId } from "react";
import { cachePolicyLabels, type CachePolicy } from "@/utils/cachePolicy";

const field =
  "min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";

export function StorageCacheFields({
  policy,
  quotaMiB,
  ttl,
  onPolicy,
  onQuota,
  onTTL,
  disabled = false,
}: {
  policy: CachePolicy;
  quotaMiB: string;
  ttl: string;
  disabled?: boolean;
  onPolicy: (value: CachePolicy) => void;
  onQuota: (value: string) => void;
  onTTL: (value: string) => void;
}) {
  const id = useId();
  return (
    <div className="grid items-start gap-4 sm:grid-cols-3">
      <label className="grid min-w-0 gap-1 text-sm">
        位置缓存上限
        <select
          aria-label="位置缓存上限"
          className={field}
          value={policy}
          disabled={disabled}
          aria-describedby={`${id}-policy`}
          onChange={(event) => onPolicy(event.target.value as CachePolicy)}
        >
          {Object.entries(cachePolicyLabels).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <span id={`${id}-policy`} className="text-xs leading-relaxed text-muted-foreground">
          来源请求受此上限约束；按需原图同时允许预览。
        </span>
      </label>
      <label className="grid min-w-0 gap-1 text-sm">
        原图缓存额度（MiB）
        <input
          aria-label="原图缓存额度（MiB）"
          className={field}
          type="number"
          min="0"
          max={2 ** 30}
          step="any"
          value={quotaMiB}
          disabled={disabled}
          aria-describedby={`${id}-quota`}
          onChange={(event) => onQuota(event.target.value)}
        />
        <span id={`${id}-quota`} className="text-xs leading-relaxed text-muted-foreground">
          按需原图须大于零。仅计完整缓存，不包含预览、托管原件或磁盘容量。
        </span>
      </label>
      <label className="grid min-w-0 gap-1 text-sm">
        保留时间（秒）
        <input
          aria-label="保留时间（秒）"
          className={field}
          type="number"
          min="60"
          max="2592000"
          step="1"
          value={ttl}
          disabled={disabled}
          aria-describedby={`${id}-ttl`}
          onChange={(event) => onTTL(event.target.value)}
        />
        <span id={`${id}-ttl`} className="text-xs leading-relaxed text-muted-foreground">
          60 秒至 30 天；1 天为 86400 秒。读取不会延长到期时间。
        </span>
      </label>
    </div>
  );
}
