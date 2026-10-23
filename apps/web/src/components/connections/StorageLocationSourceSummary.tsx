import { useState } from "react";
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

export function StorageLocationSourceSummary() {
  const revision = useSessionRevision();
  return <SummaryOwner key={revision} />;
}

function SummaryOwner() {
  const { role } = usePermissions();
  const session = useAuthStore((s) => s.sessionId);
  const actor = useAuthStore((s) => s.user?.id);
  const allowed = role === "super_admin" || role === "project_admin";
  const [locationOffset, setLocationOffset] = useState(0);
  const [locationId, setLocationId] = useState("");
  const [sourceOffset, setSourceOffset] = useState(0);
  const [jobsOffset, setJobsOffset] = useState(0);
  const [jobId, setJobId] = useState<string | null>(null);
  const locations = useQuery({
    queryKey: ["storage-source-locations", actor, session, locationOffset],
    queryFn: ({ signal }) => api.locationPage(locationOffset, signal),
    enabled: allowed,
  });
  const summary = useQuery({
    queryKey: ["storage-source-summary", actor, session, locationId, sourceOffset, jobsOffset],
    queryFn: ({ signal }) => api.sourceSummary(locationId, sourceOffset, jobsOffset, signal),
    enabled: allowed && !!locationId,
  });
  if (!allowed) return null;
  return (
    <section
      aria-label="存储位置关联来源"
      className="mb-5 rounded-lg border border-border bg-card p-4 text-xs"
    >
      <h2 className="mb-1 text-sm font-semibold">位置关联来源与后台任务</h2>
      <p className="mb-3 text-muted-foreground">
        选择存储位置，查看当前账号可见的来源和任务；进入数据集查看同步差异与扫描历史。
      </p>
      <label className="flex flex-wrap items-center gap-2">
        存储位置历史
        <select
          aria-label="查看关联来源的存储位置"
          className="rounded-md border border-input bg-background px-2 py-1.5 text-foreground"
          value={locationId}
          onChange={(event) => {
            setLocationId(event.target.value);
            setSourceOffset(0);
            setJobsOffset(0);
            setJobId(null);
          }}
        >
          <option value="">请选择位置</option>
          {locations.data?.items.map((location) => (
            <option key={location.id} value={location.id}>
              {location.name} · {location.kind}
              {location.enabled ? "" : "（停用）"}
            </option>
          ))}
        </select>
      </label>
      <Pagination
        label="位置"
        offset={locationOffset}
        total={locations.data?.total ?? 0}
        loading={locations.isFetching}
        onChange={(offset) => {
          setLocationOffset(offset);
          setLocationId("");
          setSourceOffset(0);
          setJobsOffset(0);
        }}
      />
      {locationId && (
        <div className="mt-3 grid gap-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 font-semibold">关联来源 · {summary.data?.total ?? "…"}</h3>
            {summary.data?.items.map((source) => (
              <p key={source.source_id} className="mb-2">
                {source.dataset_id ? (
                  <Link
                    className="text-brand underline"
                    to={`/datasets?${new URLSearchParams({ dataset: source.dataset_id, source: source.source_id })}`}
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
              total={summary.data?.total ?? 0}
              loading={summary.isFetching}
              onChange={setSourceOffset}
            />
          </div>
          <div>
            <h3 className="mb-2 font-semibold">关联后台任务 · {summary.data?.jobs.total ?? "…"}</h3>
            {summary.data?.jobs.items.map((job) => (
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
              total={summary.data?.jobs.total ?? 0}
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
      {jobId && <AsyncJobDetailModal jobId={jobId} onClose={() => setJobId(null)} />}
    </section>
  );
}
