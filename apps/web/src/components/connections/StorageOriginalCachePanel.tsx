import { StorageLocationChangeButton } from "./StorageLocationChangeButton";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  OriginalCacheCleanupResult,
  OriginalCacheUsage,
  StorageLocationOut,
} from "@/api/generated/types.gen";
import { apiClient } from "@/api/client";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { cacheBytes, originalCacheValues, type CachePolicy } from "@/utils/cachePolicy";
import { StorageCacheFields } from "./StorageCacheFields";
import { storageAccessDenied } from "./storageQueryState";

export function StorageOriginalCachePanel({ location }: { location: StorageLocationOut }) {
  const session = useAuthStore((state) => state.sessionId);
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [policy, setPolicy] = useState<CachePolicy>(location.cache_policy ?? "preview");
  const [quota, setQuota] = useState(
    String((location.original_cache_quota_bytes ?? 0) / 1024 ** 2),
  );
  const [ttl, setTTL] = useState(String(location.original_cache_ttl_seconds ?? 86400));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [message, setMessage] = useState("");
  const [confirm, setConfirm] = useState<OriginalCacheUsage | null>(null);
  const path = `/storage-locations/${location.id}/original-cache`;
  const query = useQuery({
    queryKey: ["storage-original-cache", session, location.id, location.config_revision],
    queryFn: ({ signal }) => apiClient.get<OriginalCacheUsage>(path, { signal }),
    refetchInterval: (query) =>
      forbidden || storageAccessDenied(query.state.error) ? false : 5000,
    retry: false,
  });
  const values = originalCacheValues(policy, quota, ttl);
  const changed =
    policy !== location.cache_policy ||
    values.original_cache_quota_bytes !== (location.original_cache_quota_bytes ?? 0) ||
    values.original_cache_ttl_seconds !== (location.original_cache_ttl_seconds ?? 86400);
  const refresh = async () => {
    await Promise.all(
      [
        ["storage-original-cache"],
        ["storage-source-locations"],
        ["storage-location"],
        ["filesystem-locations"],
        ["dataset-sources"],
        ["dataset-items"],
        ["dataset-item"],
        ["storage-source-summary"],
        ["dataset-source"],
        ["dataset-workbench"],
      ].map((queryKey) => client.invalidateQueries({ queryKey })),
    );
  };
  const act = async (work: () => Promise<void>) => {
    const owner = capture();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await work();
      if (owner()) await refresh();
    } catch (failure) {
      if (owner()) {
        setError(verifiedContentError(failure));
        setForbidden(storageAccessDenied(failure));
      }
    } finally {
      if (owner()) setBusy(false);
    }
  };
  const denied = forbidden || storageAccessDenied(query.error);
  const usage = denied ? undefined : query.data;
  return (
    <section aria-label="完整原图缓存" className="my-4 rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-md font-semibold">完整原图缓存</h3>
      {!denied && (
        <>
          <StorageCacheFields
            policy={policy}
            quotaMiB={quota}
            ttl={ttl}
            onPolicy={setPolicy}
            onQuota={setQuota}
            onTTL={setTTL}
            disabled={busy}
          />
          <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
            保存后，旧配置生成的缓存将停止读取并由后台回收，重新读源后可生成副本。缓存满时按需读取，优先淘汰未在读取中的旧副本；不会删除来源文件。
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <StorageLocationChangeButton
              location={location}
              payload={{
                expected_revision: location.config_revision,
                cache_policy: policy,
                original_cache_quota_bytes: values.original_cache_quota_bytes,
                original_cache_ttl_seconds: values.original_cache_ttl_seconds,
              }}
              label="预览缓存配置变更"
              disabled={busy || !changed || !values.valid}
              onApplied={async () => {
                setMessage("缓存配置已保存。");
                await refresh();
              }}
              onDenied={() => setForbidden(true)}
            />
            <Button size="sm" disabled={busy} onClick={() => void refresh()}>
              刷新配置与用量
            </Button>
          </div>
          {query.isPending && (
            <p role="status" className="mt-3 text-xs text-muted-foreground">
              正在读取缓存账本…
            </p>
          )}
          {usage && (
            <div className="mt-4 border-t border-border pt-3">
              <p className="text-sm">
                已计费{" "}
                <span className="font-medium tabular-nums">{cacheBytes(usage.charged_bytes)}</span>{" "}
                / 额度 {cacheBytes(usage.quota_bytes)}
              </p>
              <dl className="mt-3 grid grid-cols-3 gap-3 text-sm">
                {(
                  [
                    ["上传中", usage.uploading],
                    ["已登记", usage.ready],
                    ["待删除", usage.deleting],
                  ] as const
                ).map(([label, state]) => (
                  <div key={label}>
                    <dt className="text-xs text-muted-foreground">{label}</dt>
                    <dd className="mt-1 tabular-nums">
                      {state.count} 个 · {cacheBytes(state.bytes)}
                    </dd>
                  </div>
                ))}
              </dl>
              <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                账本观测于 {new Date(usage.measured_at).toLocaleString("zh-CN")}
                ；登记副本仍须在读取时复核。回收异常 {usage.failed_count} 个，待确认写入{" "}
                {usage.uncertain_count} 个；删除成功前继续占用额度。
              </p>
              <Button
                className="mt-3"
                size="sm"
                disabled={busy || usage.ready.count === 0}
                onClick={() => setConfirm(usage)}
              >
                请求清理原图缓存
              </Button>
            </div>
          )}
          {confirm && (
            <div
              className="mt-3 rounded-md border border-border p-3 text-sm"
              role="region"
              aria-label="确认清理原图缓存"
            >
              <p>
                清理这次观测前已登记的最多 100
                份完整副本；正在上传或读取的副本会跳过。后台逐步删除，来源原件、预览和标注保留。
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const owner = capture();
                      const result = await apiClient.post<OriginalCacheCleanupResult>(
                        `${path}/cleanup`,
                        {
                          expected_revision: confirm.expected_revision,
                          recorded_before: confirm.measured_at,
                        },
                      );
                      if (owner()) {
                        setConfirm(null);
                        setMessage(
                          `已提交 ${result.retired_count} 份副本回收，跳过读取中 ${result.skipped_leased_count} 份；仍有 ${result.remaining_ready_count} 份副本已登记。实际删除前继续计费。`,
                        );
                      }
                    })
                  }
                >
                  确认提交回收
                </Button>
                <Button size="sm" disabled={busy} onClick={() => setConfirm(null)}>
                  取消清理
                </Button>
              </div>
            </div>
          )}
        </>
      )}
      {(error || query.error) && (
        <p role="alert" className="mt-3 text-xs text-status-danger">
          {error || verifiedContentError(query.error)}
        </p>
      )}
      {denied && (
        <Button
          className="mt-3"
          size="sm"
          onClick={() => {
            const owner = capture();
            void query.refetch().then((result) => {
              if (owner() && !result.error) {
                setForbidden(false);
                setError("");
                setConfirm(null);
              }
            });
          }}
        >
          重新核对权限与用量
        </Button>
      )}
      {message && !denied && (
        <p role="status" className="mt-3 text-xs text-muted-foreground">
          {message}
        </p>
      )}
    </section>
  );
}
