import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import { asyncJobsApi } from "@/api/asyncJobs";
import { Button } from "@/components/ui/Button";
import { useSessionGuard } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { DirectorySourceChanges } from "./DirectorySourceChanges";

const statusLabels: Record<string, string> = {
  pending: "等待执行",
  running: "进行中",
  completed: "已完成",
  failed: "已中断",
  cancelled: "已取消",
  ready: "已核验，待登记",
  registered: "本轮已登记",
  unchanged: "已登记，内容相同",
  error: "异常",
};
const errorLabels: Record<string, string> = {
  invalid_image: "图片损坏或无法解码",
  image_format_unsupported: "不支持的图片格式",
  source_changed: "文件已变化，请重新扫描；尚未核验的异常项可修复后重试",
  content_mismatch: "与已核验内容不同，请重新扫描，已有样本保持原身份",
  filesystem_unavailable: "文件暂不可读",
  content_too_large: "图片超过读取大小限制",
  image_pixel_budget_exceeded: "图片像素超出限制",
  dataset_busy: "数据集繁忙，可重试",
  snapshot_capacity_exceeded: "读取繁忙，可重试",
  dispatch_unavailable: "等待后台重新派发，可手动继续",
  reference_content_changed: "内容或身份观测已变化，保留旧样本；本轮不接纳此变化",
};
const bytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 * 1024
      ? `${(value / 1024).toFixed(2)} KiB`
      : `${(value / (1024 * 1024)).toFixed(2)} MiB`;

export function DirectoryRegistrationProgress({
  scanId,
  datasetId,
  owner,
  sourceRevision,
  readOnly = false,
}: {
  scanId: string;
  datasetId: string;
  owner: string;
  sourceRevision: number;
  readOnly?: boolean;
}) {
  const client = useQueryClient();
  const capture = useSessionGuard();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [changeKind, setChangeKind] = useState("");
  const [changeOffset, setChangeOffset] = useState(0);
  const registration = useQuery({
    queryKey: ["directory-registration", owner, scanId],
    queryFn: ({ signal }) => api.registration(scanId, signal),
    refetchInterval: (q) =>
      q.state.data && ["pending", "running"].includes(q.state.data.status) ? 1500 : false,
  });
  const run = registration.data;
  const revisionMismatch = !!run && run.source_revision !== sourceRevision;
  const stale = readOnly || revisionMismatch;
  const active = !!run && ["pending", "running"].includes(run.status);
  const entries = useQuery({
    queryKey: ["directory-registration-entries", owner, run?.id, offset, errorsOnly],
    queryFn: ({ signal }) => api.registrationEntries(run!.id, offset, errorsOnly, signal),
    enabled: !!run,
    refetchInterval: active ? 2000 : false,
  });
  const changes = useQuery({
    queryKey: [
      "directory-source-changes",
      owner,
      run?.id,
      run?.generation,
      run?.comparison_complete,
      changeKind,
      changeOffset,
    ],
    queryFn: ({ signal }) => api.changes(run!.id, changeKind, changeOffset, signal),
    enabled: !!run?.comparison_complete,
    refetchInterval: active ? 2000 : false,
  });
  const registeredCount = (run?.counts.registered_count ?? 0) + (run?.counts.unchanged_count ?? 0);
  useEffect(() => {
    if (!run?.id) return;
    void client.invalidateQueries({ queryKey: ["directory-registration-entries", owner, run.id] });
    void client.invalidateQueries({ queryKey: ["directory-source-changes", owner, run.id] });
  }, [client, owner, run?.id, run?.phase, run?.status, run?.counts]);
  useEffect(() => {
    setReviewed(false);
  }, [run?.phase, run?.status, run?.counts.ready_count, run?.counts.error_count]);
  useEffect(() => {
    if (!registeredCount) return;
    void client.invalidateQueries({ queryKey: ["dataset-items", datasetId] });
    void client.invalidateQueries({ queryKey: ["datasets"] });
    void client.invalidateQueries({ queryKey: ["dataset-scan-entries", owner, scanId] });
  }, [registeredCount, client, datasetId, owner, scanId]);
  const act = async (work: () => Promise<unknown>) => {
    const current = capture();
    setBusy(true);
    setError("");
    try {
      await work();
      return current();
    } catch (e) {
      if (current()) setError(verifiedContentError(e));
      return false;
    } finally {
      if (current()) {
        await Promise.all([
          client.invalidateQueries({ queryKey: ["directory-registration", owner, scanId] }),
          client.invalidateQueries({ queryKey: ["directory-registration-entries", owner] }),
          client.invalidateQueries({ queryKey: ["dataset-scan-entries", owner] }),
          client.invalidateQueries({ queryKey: ["directory-source-changes", owner] }),
          client.invalidateQueries({ queryKey: ["dataset-items", datasetId] }),
          ...[
            "datasets",
            "tasks",
            "projects",
            "project",
            "project-datasets",
            "project-stats",
            "unclassified-count",
          ].map((key) => client.invalidateQueries({ queryKey: [key] })),
        ]);
        if (current()) setBusy(false);
      }
    }
  };
  return (
    <section aria-label="批量预检与登记" className="mt-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h5 className="text-sm font-semibold">批量预检与登记</h5>
        {!run && !registration.isPending && !registration.error && (
          <Button
            size="sm"
            variant="primary"
            disabled={busy || stale}
            onClick={() => void act(() => api.preflight(scanId))}
          >
            运行接入预检
          </Button>
        )}
        {run && (
          <span role="status">
            {run.phase === "preflight" ? "接入预检" : "正式登记"} ·{" "}
            {statusLabels[run.status] ?? run.status}
          </span>
        )}
        {run && (
          <Button
            size="sm"
            onClick={() => {
              void registration.refetch();
              void entries.refetch();
              if (run.comparison_complete) void changes.refetch();
            }}
          >
            刷新登记进度
          </Button>
        )}
        {active && run.async_job_id && (
          <Button
            size="sm"
            disabled={busy || stale}
            onClick={() => void act(() => asyncJobsApi.cancel(run.async_job_id!))}
          >
            取消当前阶段
          </Button>
        )}
        {run &&
          (["failed", "cancelled"].includes(run.status) ||
            (run.status === "pending" && run.error_code)) && (
            <Button
              size="sm"
              disabled={busy || stale}
              onClick={() => void act(() => api.resumeRegistration(run.id))}
            >
              继续执行
            </Button>
          )}
      </div>
      {revisionMismatch && (
        <p role="status" className="mt-2 text-status-caution">
          来源或位置已变更，本轮历史保留。请重新扫描和预检；旧尝试不能继续。
        </p>
      )}
      {readOnly && (
        <p className="mt-2 text-muted-foreground">本轮历史仅供查看，不能继续或接纳变更。</p>
      )}
      {registration.isPending && <p role="status">正在读取登记进度…</p>}
      <p className="mt-2 text-muted-foreground">
        预检会读取图片、核验格式与内容，不创建样本。确认后逐项登记；关闭页面或取消会保留已提交结果，可在后台任务及本面板继续查看。
      </p>
      {run && (
        <>
          <dl className="my-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {[
              ["已索引", run.counts.indexed_count],
              ["待核验", run.counts.pending_count],
              ["已核验待登记", run.counts.ready_count],
              ["本轮登记与保留", registeredCount],
              ["原已登记路径", run.counts.existing_count],
              ["预计新增", run.counts.estimated_new_count],
              ["异常", run.counts.error_count],
              ["扫描排除", run.counts.skipped_count],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="font-medium">{value ?? 0}</dd>
              </div>
            ))}
          </dl>
          <p>
            扫描观测体量 {bytes(run.counts.indexed_bytes ?? 0)} ·{" "}
            {run.ownership_mode === "managed_copy" ? "托管复制" : "原地引用"} /{" "}
            {run.cache_policy === "preview" ? "预览缓存" : "无缓存"} · 原图复制预计{" "}
            {bytes(run.counts.original_estimated_bytes ?? 0)} / 已新增{" "}
            {bytes(run.counts.original_copy_bytes ?? 0)} · 预览预计{" "}
            {bytes(run.counts.preview_estimated_bytes ?? 0)}（已测{" "}
            {run.counts.preview_estimated_count ?? 0} 项，
            {run.counts.preview_estimate_error_count ?? 0} 项未知）· 本轮路径已存预览{" "}
            {bytes(run.counts.preview_retained_bytes ?? 0)}，失败{" "}
            {run.counts.preview_failed_count ?? 0} 项
          </p>
          <p className="mt-1 text-muted-foreground">
            观测时间 {new Date(run.created_at).toLocaleString()}
            。体量与新增数是扫描估算，正式登记会重新读取；修复重试不会改写已有内容身份。
          </p>
          {run.comparison_available && (
            <DirectorySourceChanges
              key={`${owner}:${run.id}:${run.generation}:${run.phase}:${run.status}`}
              scanId={scanId}
              registrationId={run.id}
              onRelocate={
                run.phase === "preflight" && run.status === "completed" && !stale
                  ? (entry, target) =>
                      act(() =>
                        api.relocate(run.id, target.path_digest, {
                          expected_generation: run.generation,
                          before_revision_id: entry.before_revision_id!,
                          candidate_sha256: target.candidate_sha256,
                        }),
                      )
                  : undefined
              }
              busy={busy || active || stale}
              actionError={error}
              onAccept={
                run.phase === "preflight" && run.status === "completed" && !stale
                  ? (entry) =>
                      act(() =>
                        (entry.can_revalidate ? api.revalidate : api.acceptRevision)(
                          run.id,
                          entry.path_digest,
                          {
                            expected_generation: run.generation,
                            before_revision_id: entry.before_revision_id!,
                            candidate_sha256: entry.candidate_sha256!,
                          },
                        ),
                      )
                  : undefined
              }
              complete={run.comparison_complete}
              data={changes.data}
              pending={changes.isFetching}
              error={changes.error}
              kind={changeKind}
              offset={changeOffset}
              onKind={(value) => {
                setChangeKind(value);
                setChangeOffset(0);
              }}
              onOffset={setChangeOffset}
            />
          )}
          {run.comparison_complete && !!changes.data?.counts.changed && (
            <p className="mt-2 text-status-caution">
              接纳新内容或重新核验需在预检阶段逐项确认；如还需登记新增，请先处理变化项。开始仅新增登记后，未处理的变化需重新扫描和预检。
            </p>
          )}
          {run.phase === "preflight" &&
            run.status === "completed" &&
            run.comparison_complete &&
            changes.data?.remaining_added_count === 0 && (
              <p className="mt-2 text-muted-foreground">
                没有待登记的新增路径；已用于重定位的目标保留原样本。
              </p>
            )}
          {run.phase === "preflight" && run.status === "completed" && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={reviewed}
                  onChange={(e) => setReviewed(e.target.checked)}
                />
                {run.comparison_complete
                  ? "已核对差异，仅登记新增路径，保留旧样本和标注"
                  : "已核对影响，先登记通过核验的图片"}
              </label>
              <Button
                size="sm"
                variant="primary"
                disabled={
                  !reviewed ||
                  busy ||
                  stale ||
                  (run.comparison_complete && !changes.data?.remaining_added_count)
                }
                onClick={() =>
                  void act(() =>
                    api.confirmRegistration(
                      run.id,
                      run.comparison_complete ? "additions_only" : "all_safe",
                    ),
                  )
                }
              >
                {run.comparison_complete ? "确认仅登记新增" : "确认批量登记"}
              </Button>
            </div>
          )}
          {run.phase === "registering" && run.status === "completed" && (
            <p role="status" className="mt-2 text-status-positive">
              本轮登记结束：已登记或保留 {registeredCount}，异常 {run.counts.error_count}
              。已登记样本可关联项目并开始标注。
            </p>
          )}
          <p className="mt-3 text-muted-foreground">
            下方保留原预检与仅新增登记记录。新修订的接纳结果见上方差异表，不改写原差异和异常计数。
          </p>
          <div className="mt-3 flex items-center gap-2">
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={errorsOnly}
                onChange={(e) => {
                  setErrorsOnly(e.target.checked);
                  setOffset(0);
                }}
              />
              只看异常
            </label>
            <span>{entries.isPending ? "正在加载结果…" : `${entries.data?.total ?? 0} 条`}</span>
          </div>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="text-muted-foreground">
                  <th className="p-2">相对路径</th>
                  <th>结果</th>
                  <th>错误与处理</th>
                </tr>
              </thead>
              <tbody>
                {entries.data?.items.map((entry) => (
                  <tr key={entry.path_digest} className="border-t border-border">
                    <td className="p-2">{entry.relative_path}</td>
                    <td>{statusLabels[entry.state] ?? entry.state}</td>
                    <td>
                      {entry.error_code && (
                        <>
                          <span className="text-status-danger">
                            {errorLabels[entry.error_code] ?? entry.error_code}
                          </span>{" "}
                          <Button
                            size="sm"
                            disabled={
                              busy ||
                              active ||
                              stale ||
                              entry.error_code === "reference_content_changed"
                            }
                            onClick={() =>
                              void act(() => api.retryRegistrationEntry(run.id, entry.path_digest))
                            }
                          >
                            修复后重试此项
                          </Button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              disabled={entries.isPending || !offset}
              onClick={() => setOffset((n) => Math.max(0, n - 20))}
            >
              上一页登记结果
            </Button>
            {!entries.isPending && (
              <span>
                {Math.min(offset + 1, entries.data?.total ?? 0)}–
                {Math.min(offset + 20, entries.data?.total ?? 0)} / {entries.data?.total ?? 0}
              </span>
            )}
            <Button
              size="sm"
              disabled={entries.isPending || offset + 20 >= (entries.data?.total ?? 0)}
              onClick={() => setOffset((n) => n + 20)}
            >
              下一页登记结果
            </Button>
          </div>
        </>
      )}
      {run?.error_code && (
        <p role="status" className="mt-2 text-status-caution">
          {errorLabels[run.error_code] ?? run.error_code}
        </p>
      )}
      {(error || registration.error || entries.error) && (
        <p role="alert" className="mt-2 text-status-danger">
          {error || verifiedContentError(registration.error || entries.error)}
        </p>
      )}
    </section>
  );
}
