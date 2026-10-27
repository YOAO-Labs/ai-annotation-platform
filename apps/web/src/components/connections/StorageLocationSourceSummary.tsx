import { storageAccessDenied } from "./storageQueryState";
import { useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { Button } from "@/components/ui/Button";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { SourceDiagnostics, evidenceDate, sourceHref } from "./StorageSourceDiagnostics";
import { JOB_STATUS_LABEL } from "@/components/jobs/asyncJobPresentation";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { StorageSourceUsage } from "./StorageSourceUsage";
import { StorageFaultsPanel } from "./StorageFaultsPanel";
import { ExternalProtectionSummary } from "@/components/datasets/DirectorySourceProtection";

function Pagination({
  label,
  offset,
  total,
  loading,
  onChange,
}: {
  label: string;
  offset: number;
  total: number;
  loading: boolean;
  onChange: (offset: number) => void;
}) {
  return (
    <nav
      aria-label={`${label}分页`}
      className="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
    >
      <Button
        size="sm"
        disabled={!offset || loading}
        onClick={() => onChange(Math.max(0, offset - 20))}
      >
        上一页{label}
      </Button>
      <span>
        第 {offset / 20 + 1} 页 · 共 {total} 条
      </span>
      <Button
        size="sm"
        disabled={loading || offset + 20 >= total}
        onClick={() => onChange(offset + 20)}
      >
        下一页{label}
      </Button>
    </nav>
  );
}

interface SummaryProps {
  locationId?: string;
  returnTo?: string;
}
export function StorageLocationSourceSummary(props: SummaryProps = {}) {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  return <SummaryOwner key={`${revision}/${role}/${props.locationId ?? "picker"}`} {...props} />;
}

function SummaryOwner({ locationId: selectedLocationId, returnTo }: SummaryProps) {
  const locationHint = useId();
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const refreshRef = useRef<HTMLButtonElement>(null);
  const openJob = (id: string, trigger: HTMLElement) => {
    returnFocusRef.current = trigger;
    setJobId(id);
  };
  const { role } = usePermissions();
  const session = useAuthStore((s) => s.sessionId);
  const actor = useAuthStore((s) => s.user?.id);
  const allowed = role === "super_admin" || role === "project_admin";
  const [locationOffset, setLocationOffset] = useState(0);
  const [pickedLocationId, setLocationId] = useState("");
  const locationId = selectedLocationId ?? pickedLocationId;
  const [sourceOffset, setSourceOffset] = useState(0);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const locations = useQuery({
    queryKey: ["storage-source-locations", actor, session, role, locationOffset],
    queryFn: ({ signal }) => api.locationPage(locationOffset, signal),
    enabled: allowed && !selectedLocationId,
  });
  const summary = useQuery({
    queryKey: [
      "storage-source-summary",
      actor,
      session,
      role,
      locationId,
      sourceOffset,
      jobsOffset,
    ],
    queryFn: ({ signal }) => api.sourceSummary(locationId, sourceOffset, jobsOffset, signal),
    enabled: allowed && !!locationId,
    refetchInterval: (query) => {
      if (storageAccessDenied(query.state.error)) return false;
      return query.state.data?.items.some((item) =>
        [item.diagnostics.scan.recent, item.diagnostics.preflight.recent].some(
          (run) => run && ["pending", "running"].includes(run.status),
        ),
      )
        ? 5000
        : false;
    },
  });
  const denied =
    storageAccessDenied(summary.error) ||
    (!selectedLocationId && storageAccessDenied(locations.error));
  const sourceData = denied ? undefined : summary.data;
  const locationData = storageAccessDenied(locations.error) ? undefined : locations.data;
  if (!allowed) return null;
  return (
    <section
      aria-label="存储位置关联来源"
      className="mb-5 rounded-lg border border-border bg-card p-4 text-sm"
    >
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-md font-semibold">来源诊断</h2>
        {locationId && (
          <Button
            ref={refreshRef}
            size="sm"
            variant="default"
            disabled={summary.isFetching}
            onClick={() => void summary.refetch()}
          >
            刷新诊断
          </Button>
        )}
      </div>
      <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
        只读已有观测，不探测原件或生成预览。原件、预览和任务进展分别展示，详细差异可进入数据集查看。
      </p>
      {!selectedLocationId && (
        <>
          <label className="flex max-w-full flex-col items-start gap-2">
            存储位置历史
            <select
              aria-label="查看关联来源的存储位置"
              aria-describedby={locationHint}
              className="max-w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground"
              value={locationId}
              onChange={(event) => {
                setLocationId(event.target.value);
                setSourceOffset(0);
                setJobsOffset(0);
                setJobId(null);
                setHistoryOpen(false);
              }}
            >
              <option value="" disabled hidden />
              {locationData?.items.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name} · {location.kind}
                  {location.enabled ? "" : "（停用）"}
                </option>
              ))}
            </select>
          </label>
          <p id={locationHint} className="mt-2 text-xs leading-relaxed text-muted-foreground">
            选择位置后查看其关联来源与后台任务。
          </p>
          <Pagination
            label="位置"
            offset={locationOffset}
            total={locationData?.total ?? 0}
            loading={locations.isFetching}
            onChange={(offset) => {
              setLocationOffset(offset);
              setLocationId("");
              setSourceOffset(0);
              setJobsOffset(0);
            }}
          />
        </>
      )}
      {locationId && (
        <div className="mt-3 space-y-4">
          {summary.isPending && (
            <p role="status" className="text-muted-foreground">
              正在读取来源诊断…
            </p>
          )}
          {!denied && (
            <StorageFaultsPanel locationId={locationId} returnTo={returnTo} onJob={openJob} />
          )}
          {sourceData && (
            <>
              <StorageSourceUsage value={sourceData.usage} />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">关联来源 · {sourceData.total}</h3>
                <Link
                  className="text-xs text-brand underline"
                  to={`/datasets?${new URLSearchParams({ location_id: locationId, ...(returnTo ? { return: returnTo } : {}) })}`}
                >
                  查看受影响数据集 · {sourceData.dataset_count}
                </Link>
              </div>
              {!sourceData.items.length && (
                <p className="text-muted-foreground">此页暂无可见来源。</p>
              )}
              {sourceData.items.map((source) => (
                <article
                  key={source.source_id}
                  className="min-w-0 rounded-md border border-border p-3 leading-relaxed"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="min-w-0 break-words text-sm font-semibold">
                      {source.dataset_id ? (
                        <Link
                          className="text-brand underline"
                          to={sourceHref(source.dataset_id, source.source_id, returnTo)}
                        >
                          {source.dataset_name} / {source.source_name}
                        </Link>
                      ) : (
                        <span>{source.source_name}（原数据集已删除）</span>
                      )}
                    </h3>
                    <span className="text-xs text-muted-foreground">
                      {source.unregistered_at ? "已注销" : source.enabled ? "已启用" : "已停用"}
                    </span>
                  </div>
                  <div className="mt-2">
                    <ExternalProtectionSummary value={source.external_protection} />
                  </div>
                  <SourceDiagnostics
                    value={source.diagnostics}
                    datasetId={source.dataset_id}
                    sourceId={source.source_id}
                    showFaults={role === "super_admin"}
                    returnTo={returnTo}
                    onJob={openJob}
                  />
                </article>
              ))}
              <Pagination
                label="来源"
                offset={sourceOffset}
                total={sourceData.total}
                loading={summary.isFetching}
                onChange={setSourceOffset}
              />
              <details
                open={historyOpen}
                onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
                className="rounded-md border border-border p-3"
              >
                <summary className="cursor-pointer text-sm font-semibold">
                  关联后台任务历史 · {sourceData.jobs.total}
                </summary>
                <div className="mt-3 space-y-2">
                  {!sourceData.jobs.items.length && (
                    <p className="text-muted-foreground">此页暂无保留的任务记录。</p>
                  )}
                  {sourceData.jobs.items.map((job) => (
                    <div key={job.id}>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-auto max-w-full whitespace-normal text-left"
                        onClick={(event) => openJob(job.id, event.currentTarget)}
                      >
                        {job.kind === "dataset_scan" ? "扫描" : "预检 / 登记"} ·{" "}
                        {JOB_STATUS_LABEL[job.status as keyof typeof JOB_STATUS_LABEL] ??
                          job.status}{" "}
                        · {evidenceDate(job.created_at)}
                      </Button>
                    </div>
                  ))}
                  <Pagination
                    label="任务"
                    offset={jobsOffset}
                    total={sourceData.jobs.total}
                    loading={summary.isFetching}
                    onChange={setJobsOffset}
                  />
                </div>
              </details>
            </>
          )}
        </div>
      )}
      {(locations.error || summary.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {sourceData ? "诊断刷新失败，当前显示上次结果。" : ""}
          {verifiedContentError(locations.error || summary.error)}
        </p>
      )}
      {jobId && !denied && (
        <AsyncJobDetailModal
          jobId={jobId}
          returnFocusRef={returnFocusRef}
          onClose={() => {
            if (!returnFocusRef.current?.isConnected) returnFocusRef.current = refreshRef.current;
            setJobId(null);
          }}
        />
      )}
    </section>
  );
}
