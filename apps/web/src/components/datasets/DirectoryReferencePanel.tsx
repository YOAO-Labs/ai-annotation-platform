import { cachePolicyLabels, type CachePolicy } from "@/utils/cachePolicy";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { storageLocationLink } from "./storageNavigation";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { listStyles } from "@/components/ui/listStyles";
import { Button } from "@/components/ui/Button";
import { useAuthStore } from "@/stores/authStore";
import { useVerifiedImage, verifiedContentError } from "@/hooks/useVerifiedImage";
import { DirectoryRegistrationProgress } from "./DirectoryRegistrationProgress";
import { DirectorySyncSchedule } from "./DirectorySyncSchedule";
import { DirectorySourceProtection } from "./DirectorySourceProtection";
import { DirectoryCachedPreview, DirectorySourceControls } from "./DirectorySourceControls";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { parseReference } from "@/utils/referenceContent";

const disclosure =
  "cursor-pointer text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const scanStatus: Record<string, string> = {
  pending: "等待中",
  running: "进行中",
  completed: "完成",
  failed: "失败",
  cancelled: "已取消",
};

const field =
  "min-w-0 max-w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function ReferenceImagePreview({ itemId, sha256 }: { itemId: string; sha256: string }) {
  const image = useVerifiedImage(`/dataset-items/${itemId}/content?sha256=${sha256}`);
  return (
    <div className="mt-2 rounded-md border border-border p-2">
      {image.url ? (
        <img src={image.url} alt="已核验原图" className="max-h-56 max-w-full object-contain" />
      ) : (
        <p role="status" className="text-xs text-muted-foreground">
          {image.error ?? "正在核验并读取原图…"}
        </p>
      )}
      <Button size="sm" onClick={image.retry}>
        重读原图
      </Button>
    </div>
  );
}

export function ReferenceItemPreview({ filePath }: { filePath: string }) {
  const [open, setOpen] = useState(false);
  const identity = parseReference(filePath);
  if (!identity) return null;
  return (
    <>
      <Button size="sm" onClick={() => setOpen((v) => !v)}>
        {open ? "关闭原图" : "核验预览"}
      </Button>
      {open && <ReferenceImagePreview itemId={identity.itemId} sha256={identity.sha256} />}
    </>
  );
}

export interface DirectorySourceFocus {
  persistUrl?: boolean;
  archived?: boolean;
  initialSourceId?: string;
  initialScanId?: string;
}
export function DirectoryReferencePanel(props: { datasetId: string } & DirectorySourceFocus) {
  const revision = useSessionRevision();
  return (
    <DirectoryReferencePanelOwner
      key={`${revision}/${props.datasetId}/${props.archived ?? false}/${props.initialSourceId ?? ""}/${props.initialScanId ?? ""}`}
      {...props}
    />
  );
}

function DirectoryReferencePanelOwner({
  datasetId,
  initialSourceId,
  initialScanId,
  persistUrl = false,
  archived = false,
}: { datasetId: string } & DirectorySourceFocus) {
  const hintId = useId();
  const navigation = useLocation();
  const [, setSearchParams] = useSearchParams();
  const syncFocus = (sourceId: string, scanId?: string) => {
    if (!persistUrl || navigation.pathname !== `/datasets/${datasetId}`) return;
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.set("tab", "sources");
        if (sourceId) next.set("source", sourceId);
        else next.delete("source");
        if (scanId) next.set("scan", scanId);
        else next.delete("scan");
        return next;
      },
      { replace: true },
    );
  };
  const session = useAuthStore((s) => s.sessionId);
  const userId = useAuthStore((s) => s.user?.id);
  const owner = `${userId}/${session}/${datasetId}`;
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [sourceId, setSourceId] = useState(initialSourceId ?? "");
  const [selectedScanId, setSelectedScanId] = useState(initialScanId ?? "");
  const [historyOffset, setHistoryOffset] = useState(0);
  const [incompleteLink, setIncompleteLink] = useState(!!initialScanId && !initialSourceId);
  const [jobId, setJobId] = useState<string | null>(null);
  const [locationId, setLocationId] = useState("");
  const [directory, setDirectory] = useState("");
  const [node, setNode] = useState("");
  const [ownership, setOwnership] = useState<"reference" | "managed_copy">("reference");
  const [cache, setCache] = useState<CachePolicy>("preview");
  const [recursive, setRecursive] = useState(true);
  const [include, setInclude] = useState("*.png,*.jpg,*.jpeg,*.webp,*.PNG,*.JPG,*.JPEG,*.WEBP");
  const [exclude, setExclude] = useState("");
  const [offset, setOffset] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [browseCursor, setBrowseCursor] = useState<string | undefined>();
  const [browsePath, setBrowsePath] = useState<string | null>(null);
  const sources = useQuery({
    queryKey: ["dataset-sources", owner],
    queryFn: ({ signal }) => api.sources(datasetId, signal),
  });
  const locations = useQuery({
    queryKey: ["storage-locations", owner],
    queryFn: ({ signal }) => api.locations(signal),
  });
  const activeSource = sourceId || sources.data?.items[0]?.id;
  const focusedSource = useQuery({
    queryKey: ["dataset-source", owner, sourceId],
    queryFn: ({ signal }) => api.source(sourceId, signal),
    enabled: !!sourceId,
  });
  const candidateSource = sourceId
    ? focusedSource.data
    : incompleteLink
      ? undefined
      : sources.data?.items[0];
  const source = candidateSource?.dataset_id === datasetId ? candidateSource : undefined;
  const sourceMismatch = !!candidateSource && candidateSource.dataset_id !== datasetId;
  const sourceOptions = sources.data?.items ?? [];
  const visibleSources = source
    ? sourceOptions.some((s) => s.id === source.id)
      ? sourceOptions.map((item) => (item.id === source.id ? source : item))
      : [source, ...sourceOptions]
    : sourceOptions;
  const writable = !archived && !!source?.enabled && !source.unregistered_at;
  const samplePath = (path: string) => {
    const base = source!.relative_path;
    return locations.data?.items.find((l) => l.id === source!.location_id)?.kind === "s3"
      ? base + (base && !base.endsWith("/") ? "/" : "") + path
      : [base, path].filter(Boolean).join("/");
  };
  const scans = useQuery({
    queryKey: ["dataset-scans", owner, source?.id, historyOffset],
    queryFn: ({ signal }) => api.scans(source!.id, signal, { offset: historyOffset }),
    enabled: !!source,
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => ["pending", "running"].includes(r.status)) ? 2000 : false,
  });
  const latest = useQuery({
    queryKey: ["dataset-scans", owner, source?.id, 0],
    queryFn: ({ signal }) => api.scans(source!.id, signal, { offset: 0 }),
    enabled: !!source,
    refetchInterval: (q) =>
      q.state.data?.items.some((r) => ["pending", "running"].includes(r.status)) ? 2000 : false,
  });
  const complete = useQuery({
    queryKey: ["dataset-scans", owner, source?.id, "complete"],
    queryFn: ({ signal }) => api.scans(source!.id, signal, { complete: true, limit: 1 }),
    enabled: !!source,
  });
  const active = useQuery({
    queryKey: ["dataset-scans", owner, source?.id, "active"],
    queryFn: ({ signal }) => api.scans(source!.id, signal, { active_only: true, limit: 1 }),
    enabled: !!source,
    refetchInterval: writable ? 5000 : false,
  });
  const selected = useQuery({
    queryKey: ["dataset-scans", owner, source?.id, "selected", selectedScanId],
    queryFn: ({ signal }) => api.scan(selectedScanId, signal),
    enabled: !!source && !!selectedScanId,
    refetchInterval: (q) =>
      q.state.data && ["pending", "running"].includes(q.state.data.status) ? 2000 : false,
  });
  const latestRun = latest.data?.items[0];
  useEffect(() => {
    if (latestRun?.status === "completed")
      void client.invalidateQueries({ queryKey: ["dataset-scans", owner, source?.id, "complete"] });
  }, [client, owner, source?.id, latestRun?.id, latestRun?.status]);
  const activeRun = active.data?.items[0];
  const previousActive = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!active.isSuccess) return;
    const current = activeRun?.id ?? null;
    if (previousActive.current !== undefined && previousActive.current !== current) {
      void client.invalidateQueries({ queryKey: ["dataset-scans", owner, source?.id, "complete"] });
      void client.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "dataset-scans" &&
          query.queryKey[1] === owner &&
          query.queryKey[2] === source?.id &&
          typeof query.queryKey[3] === "number",
      });
    }
    previousActive.current = current;
  }, [active.isSuccess, activeRun?.id, client, owner, source?.id]);
  const candidateRun = selectedScanId ? selected.data : latestRun;
  const runMismatch = !!candidateRun && candidateRun.source_id !== source?.id;
  const run = !runMismatch ? candidateRun : undefined;
  const sourceReturn = new URLSearchParams(navigation.search);
  sourceReturn.set("tab", "sources");
  if (source) sourceReturn.set("source", source.id);
  const focusedScanId = selectedScanId || run?.id;
  if (focusedScanId) sourceReturn.set("scan", focusedScanId);
  else sourceReturn.delete("scan");
  const sourceReturnPath = `${navigation.pathname.startsWith("/datasets/") ? navigation.pathname : `/datasets/${datasetId}`}?${sourceReturn}`;
  const registration = useQuery({
    queryKey: ["directory-active-registration", owner, source?.id],
    queryFn: ({ signal }) => api.activeRegistration(source!.id, signal),
    enabled: !!source,
    refetchInterval: writable ? 5000 : false,
  });
  const activeRegistration = registration.data;
  const readOnly =
    !writable ||
    !!activeRun ||
    run?.source_revision !== source?.config_revision ||
    run?.id !== latestRun?.id;
  const selectRun = (id: string) => {
    setSelectedScanId(id);
    if (source) syncFocus(source.id, id);
    setOffset(0);
  };
  const entries = useQuery({
    queryKey: ["dataset-scan-entries", owner, run?.id, offset],
    queryFn: ({ signal }) => api.entries(run!.id, offset, signal),
    enabled: !!run?.complete,
  });
  const dirs = useQuery({
    queryKey: ["reference-directories", owner, locationId, browsePath, browseCursor],
    queryFn: ({ signal }) => api.browse(locationId, browsePath!, browseCursor, signal),
    enabled: !!locationId && browsePath !== null,
  });
  const act = async (work: (current: () => boolean) => Promise<unknown>) => {
    if (archived) {
      setError("数据集已归档，来源管理保持只读。");
      return;
    }
    const current = capture();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await work(current);
      if (!current()) return;
      await Promise.all([
        client.invalidateQueries({ queryKey: ["dataset-sources", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-source", owner] }),
        client.invalidateQueries({ queryKey: ["source-sync-schedule", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-scans", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-scan-entries", owner] }),
        client.invalidateQueries({ queryKey: ["dataset-items", datasetId] }),
        client.invalidateQueries({ queryKey: ["dataset-item", datasetId] }),
        client.invalidateQueries({ queryKey: ["dataset-workbench", datasetId] }),
        client.invalidateQueries({ queryKey: ["datasets"] }),
      ]);
    } catch (e) {
      if (current()) setError(verifiedContentError(e));
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <section aria-label="服务器目录接入" className="mb-4 space-y-4 text-sm">
      <div className="rounded-lg border border-border bg-card p-4">
        <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-md font-semibold">目录来源</h2>
            <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
              先扫描目录，再预检差异，核对后登记图片。
            </p>
          </div>
          {source && (
            <Link
              className="text-xs text-brand hover:underline"
              to={storageLocationLink(source.location_id, sourceReturnPath)}
            >
              查看来源存储位置
            </Link>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            aria-label="已有目录来源"
            className={field}
            value={activeSource ?? ""}
            onChange={(e) => {
              setSourceId(e.target.value);
              syncFocus(e.target.value);
              setIncompleteLink(false);
              setSelectedScanId("");
              setHistoryOffset(0);
              setOffset(0);
            }}
          >
            <option value="" disabled hidden />
            {visibleSources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
                {s.unregistered_at ? "（已注销）" : !s.enabled ? "（停用）" : ""}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="primary"
            disabled={
              !writable ||
              busy ||
              !!activeRun ||
              active.isPending ||
              active.isError ||
              registration.isPending ||
              registration.isError ||
              !!activeRegistration
            }
            onClick={() =>
              void act(async (current) => {
                const probe = await api.probe(source!.location_id, source!.node_id);
                if (!current()) return;
                if (!probe.ok) throw new Error(`扫描节点不可用：${probe.code}`);
                await api.start(source!.id);
                if (current()) {
                  setOffset(0);
                  setSelectedScanId("");
                  setHistoryOffset(0);
                }
              })
            }
          >
            只读预扫描
          </Button>
          {run && (
            <>
              <span role="status" className="text-sm text-muted-foreground">
                {run.status === "completed"
                  ? "扫描完成"
                  : `扫描${scanStatus[run.status] ?? run.status}`}
                {run.counts.observed_count != null
                  ? ` · 已观测 ${run.counts.observed_count} 项`
                  : ""}
              </span>
              <Button
                size="sm"
                onClick={() => {
                  void client.invalidateQueries({ queryKey: ["dataset-scans", owner, source?.id] });
                  void registration.refetch();
                  if (run.complete) void entries.refetch();
                }}
              >
                刷新扫描
              </Button>
              {["failed", "cancelled"].includes(run.status) && (
                <Button
                  size="sm"
                  disabled={busy || readOnly || !!activeRun || !!activeRegistration}
                  onClick={() => void act(() => api.resume(run.id))}
                >
                  恢复扫描
                </Button>
              )}
            </>
          )}
        </div>
        {!activeSource && (
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            请先新增目录来源，再运行扫描。
          </p>
        )}
        {source && (
          <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-3 text-xs leading-relaxed text-muted-foreground">
            <span className="break-all">目录：{source.relative_path || "位置根目录"}</span>
            <span>
              {source.ownership_mode === "reference" ? "原地引用" : "托管复制"} ·{" "}
              {cachePolicyLabels[source.effective_cache_policy ?? source.cache_policy]}
            </span>
            <span>{source.unregistered_at ? "已注销" : source.enabled ? "已启用" : "已停用"}</span>
          </div>
        )}
        {source && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span role="status">
              当前执行：
              {active.isError || registration.isError
                ? "无法确认，请刷新"
                : active.isPending || registration.isPending
                  ? "读取中…"
                  : activeRun
                    ? `扫描 ${activeRun.status}`
                    : activeRegistration
                      ? `预检 / 登记 ${activeRegistration.status}`
                      : "无"}
            </span>
            {(activeRun?.async_job_id || activeRegistration?.async_job_id) && (
              <Button
                size="sm"
                onClick={() =>
                  setJobId(activeRun?.async_job_id ?? activeRegistration!.async_job_id)
                }
              >
                查看当前后台任务
              </Button>
            )}
          </div>
        )}
        <details hidden={archived} className="mt-3 border-t border-border pt-3" open={!source}>
          <summary className={disclosure}>
            新增来源{" "}
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              服务器目录、S3 或 SFTP
            </span>
          </summary>
          <p className="mb-4 mt-3 text-sm leading-relaxed text-muted-foreground">
            选择已注册的存储位置，填写其下的相对路径。原图和预览缓存策略分别设置；本机文件夹请通过上传接入。
          </p>
          <div className="grid items-start gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <label className="grid gap-1">
              存储位置
              <select
                aria-label="原地引用存储位置"
                aria-describedby={`${hintId}-location`}
                className={field}
                value={locationId}
                onChange={(e) => {
                  setLocationId(e.target.value);
                  setBrowsePath(null);
                }}
              >
                <option value="" disabled hidden />
                {locations.data?.items
                  .filter((l) => l.declared_capabilities.reference && l.enabled)
                  .map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}（{l.kind === "filesystem" ? "服务器目录" : l.kind.toUpperCase()}）
                    </option>
                  ))}
              </select>
              <span
                id={`${hintId}-location`}
                className="text-xs leading-relaxed text-muted-foreground"
              >
                选择已注册且启用的存储位置。
              </span>
            </label>
            <div className="flex min-w-0 flex-wrap items-end gap-2 sm:col-span-2">
              <label className="grid min-w-0 flex-1 gap-1">
                位置下相对目录 / S3 前缀
                <input
                  className={field}
                  value={directory}
                  onChange={(e) => setDirectory(e.target.value)}
                  placeholder="例如 traffic/vehicle"
                />
              </label>
              <Button
                size="sm"
                disabled={!locationId || busy}
                onClick={() => {
                  setBrowseCursor(undefined);
                  setBrowsePath(directory);
                }}
              >
                浏览目录
              </Button>
            </div>
            <label className="grid gap-1">
              扫描节点
              <input
                className={field}
                value={node}
                onChange={(e) => setNode(e.target.value)}
                placeholder="worker 的完整 hostname"
              />
            </label>
            <label className="grid gap-1">
              原图策略
              <select
                aria-label="原图策略"
                aria-describedby={`${hintId}-ownership`}
                className={field}
                value={ownership}
                onChange={(e) => setOwnership(e.target.value as typeof ownership)}
              >
                <option value="reference">原地引用</option>
                <option value="managed_copy">托管复制</option>
              </select>
              <span
                id={`${hintId}-ownership`}
                className="text-xs leading-relaxed text-muted-foreground"
              >
                {ownership === "reference"
                  ? "原图保留在来源，不复制到平台。"
                  : "核验原图后，复制到平台独立保存。"}
              </span>
            </label>
            <label className="grid gap-1">
              缓存策略
              <select
                aria-label="缓存策略"
                aria-describedby={`${hintId}-cache`}
                className={field}
                value={cache}
                onChange={(e) => setCache(e.target.value as typeof cache)}
              >
                {Object.entries(cachePolicyLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
              <span
                id={`${hintId}-cache`}
                className="text-xs leading-relaxed text-muted-foreground"
              >
                请求策略受位置上限约束；按需原图须由位置管理员配置额度。
              </span>
            </label>
            <label className="grid min-w-0 gap-1 sm:col-span-2">
              图片匹配规则
              <input
                aria-label="图片匹配规则"
                className={field}
                value={include}
                onChange={(e) => setInclude(e.target.value)}
              />
            </label>
            <label className="grid gap-1">
              排除规则
              <input
                aria-label="排除规则"
                className={field}
                value={exclude}
                onChange={(e) => setExclude(e.target.value)}
                placeholder="逗号分隔，例如 temp/*,*.tmp"
              />
            </label>
            <label className="flex items-center gap-1 py-1">
              <input
                type="checkbox"
                checked={recursive}
                onChange={(e) => setRecursive(e.target.checked)}
              />
              包含子目录
            </label>
            <div className="flex flex-wrap items-center gap-3 sm:col-span-2">
              <Button
                size="sm"
                variant="primary"
                disabled={!locationId || !node || busy}
                onClick={() =>
                  void act(async (current) => {
                    const probe = await api.probe(locationId, node);
                    if (!current()) return;
                    if (!probe.ok) throw new Error(`扫描节点不可用：${probe.code}`);
                    const created = await api.create({
                      dataset_id: datasetId,
                      location_id: locationId,
                      name: directory.split("/").filter(Boolean).pop()?.slice(0, 100) || "目录根",
                      relative_path: directory,
                      node_id: node,
                      recursive,
                      ownership_mode: ownership,
                      cache_policy: cache,
                      include_globs: include
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                      exclude_globs: exclude
                        .split(",")
                        .map((s) => s.trim())
                        .filter(Boolean),
                    });
                    if (current()) {
                      setSourceId(created.id);
                      syncFocus(created.id);
                      setSelectedScanId("");
                      setHistoryOffset(0);
                      setOffset(0);
                      setMessage("来源已配置，请运行只读预扫描");
                    }
                  })
                }
              >
                配置目录来源
              </Button>
              <a className="text-brand underline" href="/storage">
                配置存储位置
              </a>
            </div>
          </div>
          {locations.data?.items.find((l) => l.id === locationId)?.kind === "s3" && (
            <p className="mt-2 text-muted-foreground">
              S3
              前缀按原始字符匹配，保留空格、连续斜线和点段。版本或条件读取固定原件；每次使用仍核验完整摘要。
            </p>
          )}
          {locations.data?.items.find((l) => l.id === locationId)?.kind === "sftp" && (
            <p className="mt-2 text-muted-foreground">
              此位置由运维配置为只读隔离根。填写根内相对目录；不跟随发现的符号链接，每次读取仍核验完整摘要。
            </p>
          )}
          {browsePath !== null && (
            <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-muted p-2">
              <span>目录：{browsePath || "根目录"}</span>
              {dirs.data?.entries.map((d) => (
                <Button
                  size="sm"
                  key={d.relative_path}
                  onClick={() => {
                    setDirectory(d.relative_path);
                    setBrowsePath(d.relative_path);
                    setBrowseCursor(undefined);
                  }}
                >
                  {d.name}
                </Button>
              ))}
              {dirs.data?.next_cursor && (
                <Button size="sm" onClick={() => setBrowseCursor(dirs.data!.next_cursor!)}>
                  下一页目录
                </Button>
              )}
              <Button
                size="sm"
                onClick={() => {
                  setDirectory("");
                  setBrowsePath("");
                  setBrowseCursor(undefined);
                }}
              >
                返回位置根
              </Button>
              {dirs.error && <span role="alert">{verifiedContentError(dirs.error)}</span>}
            </div>
          )}
        </details>
      </div>
      {jobId && <AsyncJobDetailModal jobId={jobId} onClose={() => setJobId(null)} />}
      {(sourceMismatch || runMismatch || incompleteLink) && (
        <p role="alert" className="mt-2 text-status-danger">
          链接中的数据集、来源与扫描不匹配，请重新选择来源。
        </p>
      )}
      {(error ||
        sources.error ||
        focusedSource.error ||
        locations.error ||
        scans.error ||
        complete.error ||
        active.error ||
        latest.error ||
        selected.error ||
        registration.error ||
        entries.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {error ||
            verifiedContentError(
              sources.error ||
                focusedSource.error ||
                locations.error ||
                scans.error ||
                complete.error ||
                active.error ||
                latest.error ||
                selected.error ||
                registration.error ||
                entries.error,
            )}
        </p>
      )}
      {message && (
        <p role="status" className="mt-2 text-status-positive">
          {message}
        </p>
      )}
      {run?.complete && source && (
        <DirectoryRegistrationProgress
          key={run.id}
          scanId={run.id}
          datasetId={datasetId}
          owner={owner}
          sourceRevision={source.config_revision}
          readOnly={readOnly}
        />
      )}
      {source && (
        <details
          aria-label="扫描历史"
          className="rounded-lg border border-border bg-card p-4"
          open={!!initialScanId}
        >
          <summary className={disclosure}>扫描历史</summary>
          <div className="mt-4">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <span>
                最近完整扫描：
                {complete.data?.items[0]
                  ? new Date(
                      complete.data.items[0].completed_at ?? complete.data.items[0].created_at,
                    ).toLocaleString("zh-CN")
                  : "暂无"}
              </span>
              {complete.data?.items[0] && (
                <Button size="sm" onClick={() => selectRun(complete.data!.items[0].id)}>
                  查看最近完整扫描
                </Button>
              )}
              <Button size="sm" onClick={() => selectRun("")}>
                查看最新扫描
              </Button>
            </div>
            <ul className="grid gap-1">
              {scans.data?.items.map((item) => (
                <li key={item.id} className="flex flex-wrap items-center gap-2">
                  <Button
                    size="sm"
                    aria-pressed={run?.id === item.id}
                    onClick={() => selectRun(item.id)}
                  >
                    {new Date(item.created_at).toLocaleString("zh-CN")} · {item.status} ·{" "}
                    {item.id.slice(0, 8)}
                  </Button>
                  {item.async_job_id && (
                    <Button size="sm" onClick={() => setJobId(item.async_job_id)}>
                      查看扫描任务
                    </Button>
                  )}
                </li>
              ))}
            </ul>
            <div className="mt-2 flex items-center gap-2">
              <Button
                size="sm"
                disabled={!historyOffset || scans.isFetching}
                onClick={() => setHistoryOffset((n) => Math.max(0, n - 20))}
              >
                上一页扫描
              </Button>
              <span>
                第 {historyOffset / 20 + 1} 页 · 共 {scans.data?.total ?? 0} 次
              </span>
              <Button
                size="sm"
                disabled={scans.isFetching || historyOffset + 20 >= (scans.data?.total ?? 0)}
                onClick={() => setHistoryOffset((n) => n + 20)}
              >
                下一页扫描
              </Button>
            </div>
            {run && (
              <p className="mt-2 break-all">
                当前查看扫描：{run.id}
                {readOnly ? " · 历史只读" : ""}
              </p>
            )}
            {!writable && (
              <p className="mt-2 text-muted-foreground">
                {archived
                  ? "数据集已归档，来源历史保持只读。"
                  : "来源已停用或注销，仅查看保留历史。"}
              </p>
            )}
          </div>
        </details>
      )}
      {run?.complete && (
        <details className="rounded-lg border border-border bg-card p-4">
          <summary className={disclosure}>
            扫描条目{" "}
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              {entries.data?.total ?? 0} 项 · 逐条登记与缓存预览
            </span>
          </summary>
          <div className="mt-4 overflow-x-auto">
            <table className={listStyles.table}>
              <thead>
                <tr className="text-muted-foreground">
                  <th className="p-2">相对路径</th>
                  <th>观测字节</th>
                  <th>登记</th>
                </tr>
              </thead>
              <tbody>
                {entries.data?.items.map((entry) => (
                  <tr key={entry.path_digest} className="border-t border-border">
                    <td className="p-2">{entry.relative_path}</td>
                    <td>{entry.size_bytes ?? "—"}</td>
                    <td>
                      {entry.registered_item_id ? (
                        <span>
                          已登记 ·{" "}
                          {source?.ownership_mode === "managed_copy" ? "托管复制" : "原地引用"}
                        </span>
                      ) : entry.kind === "file" && entry.state === "observed" ? (
                        <Button
                          size="sm"
                          disabled={busy || readOnly || !!activeRun || !!activeRegistration}
                          onClick={() =>
                            void act(async (current) => {
                              const sample = samplePath(entry.relative_path);
                              const probe = await api.probe(source!.location_id, "api", sample);
                              if (!current()) return;
                              if (!probe.ok) throw new Error(`API 原图不可读：${probe.code}`);
                              await api.register(run.id, entry.path_digest);
                              if (current())
                                setMessage("已重新核验并登记，可在文件列表预览、关联项目后标注");
                            })
                          }
                        >
                          核验并登记
                        </Button>
                      ) : (
                        entry.state
                      )}
                      {entry.preview_state === "ready" &&
                        source?.enabled &&
                        !source.unregistered_at &&
                        (source.effective_cache_policy ?? source.cache_policy) !== "none" && (
                          <DirectoryCachedPreview
                            key={`${source.id}/${source.config_revision}/${entry.path_digest}`}
                            sourceId={source.id}
                            digest={entry.path_digest}
                          />
                        )}
                      {entry.preview_state === "failed" && (
                        <span className="text-status-caution">预览生成失败；原图登记已保留</span>
                      )}
                      {entry.preview_state === "failed" &&
                        source?.enabled &&
                        !source.unregistered_at && (
                          <Button
                            size="sm"
                            disabled={busy || readOnly || !!activeRun || !!activeRegistration}
                            onClick={() =>
                              void act(async (current) => {
                                const sample = samplePath(entry.relative_path);
                                const probe = await api.probe(source.location_id, "api", sample);
                                if (!current()) return;
                                if (!probe.ok) throw new Error(`API 原图不可读：${probe.code}`);
                                await api.register(run.id, entry.path_digest);
                              })
                            }
                          >
                            重新核验并生成预览
                          </Button>
                        )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="mt-2 flex items-center gap-2">
              <Button
                size="sm"
                disabled={!offset}
                onClick={() => setOffset((n) => Math.max(0, n - 20))}
              >
                上一页条目
              </Button>
              <span>
                {Math.min(offset + 1, entries.data?.total ?? 0)}–
                {Math.min(offset + 20, entries.data?.total ?? 0)} / {entries.data?.total ?? 0}
              </span>
              <Button
                size="sm"
                disabled={offset + 20 >= (entries.data?.total ?? 0)}
                onClick={() => setOffset((n) => n + 20)}
              >
                下一页条目
              </Button>
            </div>
          </div>
        </details>
      )}
      {source && (
        <DirectorySourceProtection
          key={source.id}
          source={source}
          onSaved={() =>
            Promise.all([
              client.invalidateQueries({ queryKey: ["dataset-sources", owner] }),
              client.invalidateQueries({ queryKey: ["dataset-source", owner] }),
            ])
          }
        />
      )}
      {!archived && source && (
        <details className="rounded-lg border border-border bg-card p-4">
          <summary className={disclosure}>
            来源设置{" "}
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              缓存、扫描规则与停用管理
            </span>
          </summary>
          <DirectorySourceControls
            key={`${source.id}/${source.config_revision}`}
            source={source}
            act={act}
          />
        </details>
      )}
      {!archived && source?.enabled && !source.unregistered_at && (
        <details className="rounded-lg border border-border bg-card p-4">
          <summary className={disclosure}>
            周期计划{" "}
            <span className="ml-2 text-xs font-normal text-muted-foreground">自动扫描与预检</span>
          </summary>
          <DirectorySyncSchedule
            key={source.id}
            sourceId={source.id}
            owner={owner}
            datasetId={datasetId}
          />
        </details>
      )}
    </section>
  );
}
