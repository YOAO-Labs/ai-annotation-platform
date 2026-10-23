import { storageAccessDenied } from "./storageQueryState";
import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { Button } from "@/components/ui/Button";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

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
    <nav aria-label={`${label}分页`} className="mt-2 flex items-center gap-2">
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
  const { role } = usePermissions();
  const session = useAuthStore((s) => s.sessionId);
  const actor = useAuthStore((s) => s.user?.id);
  const allowed = role === "super_admin" || role === "project_admin";
  const [locationOffset, setLocationOffset] = useState(0);
  const [pickedLocationId, setLocationId] = useState("");
  const locationId = selectedLocationId ?? pickedLocationId;
  const [sourceOffset, setSourceOffset] = useState(0);
  const [jobsOffset, setJobsOffset] = useState(0);
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
      <h2 className="mb-1 text-md font-semibold">位置关联来源与后台任务</h2>
      <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
        选择存储位置，查看当前账号可见的来源和任务；进入数据集查看同步差异与扫描历史。
      </p>
      {!selectedLocationId && (
        <>
          <label className="flex flex-wrap items-center gap-2">
            存储位置历史
            <select
              aria-label="查看关联来源的存储位置"
              aria-describedby={locationHint}
              className="rounded-md border border-input bg-background px-2 py-1.5 text-foreground"
              value={locationId}
              onChange={(event) => {
                setLocationId(event.target.value);
                setSourceOffset(0);
                setJobsOffset(0);
                setJobId(null);
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
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-sm font-semibold">关联来源 · {sourceData?.total ?? "…"}</h3>
            <p className="mb-2 text-xs text-muted-foreground">
              受影响数据集 · {sourceData?.dataset_count ?? "待统计"}
            </p>
            {sourceData && (
              <Link
                className="mb-3 inline-block text-brand underline"
                to={`/datasets?${new URLSearchParams({ location_id: locationId, ...(returnTo ? { return: returnTo } : {}) })}`}
              >
                查看受影响数据集
              </Link>
            )}
            {sourceData?.items.map((source) => (
              <p
                key={source.source_id}
                className="mb-2 rounded-md border border-border p-3 leading-relaxed"
              >
                {source.dataset_id ? (
                  <Link
                    className="text-brand underline"
                    to={`/datasets/${source.dataset_id}?${new URLSearchParams({ tab: "sources", source: source.source_id, ...(returnTo ? { return: returnTo } : {}) })}`}
                  >
                    {source.dataset_name} / {source.source_name}
                  </Link>
                ) : (
                  <span>{source.source_name}（原数据集已删除）</span>
                )}
                {source.unregistered_at ? " · 已注销" : !source.enabled ? " · 已停用" : ""}
              </p>
            ))}
            <Pagination
              label="来源"
              offset={sourceOffset}
              total={sourceData?.total ?? 0}
              loading={summary.isFetching}
              onChange={setSourceOffset}
            />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-semibold">
              关联后台任务 · {sourceData?.jobs.total ?? "…"}
            </h3>
            {sourceData?.jobs.items.map((job) => (
              <p key={job.id} className="mb-2">
                <Button size="sm" onClick={() => setJobId(job.id)}>
                  {job.kind === "dataset_scan" ? "扫描" : "预检 / 登记"} · {job.status} ·{" "}
                  {new Date(job.created_at).toLocaleString("zh-CN")}
                </Button>
              </p>
            ))}
            <Pagination
              label="任务"
              offset={jobsOffset}
              total={sourceData?.jobs.total ?? 0}
              loading={summary.isFetching}
              onChange={setJobsOffset}
            />
          </div>
        </div>
      )}
      {(locations.error || summary.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {verifiedContentError(locations.error || summary.error)}
        </p>
      )}
      {jobId && !denied && <AsyncJobDetailModal jobId={jobId} onClose={() => setJobId(null)} />}
    </section>
  );
}
