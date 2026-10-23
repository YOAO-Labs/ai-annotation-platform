import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  datasetQualityReviewApi as api,
  type ReviewBatch,
  type ReviewPreview,
} from "@/api/datasetQualityReview";
import type { QualityRun } from "@/api/datasetQuality";
import { asyncJobsApi } from "@/api/asyncJobs";
import { useDatasetOwner } from "@/hooks/useDatasets";
import {
  reviewBatchActive,
  useReviewBatch,
  useReviewBatches,
  useReviewMembers,
} from "@/hooks/useDatasetQualityReview";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";

const input =
  "w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const decisions: Record<string, string> = {
  open: "待复核",
  confirmed: "已确认",
  dismissed: "已忽略",
};
const statuses: Record<string, string> = {
  preview: "未确认预览",
  pending: "等待执行",
  running: "执行中",
  completed: "已完成",
  partial: "部分完成",
  cancelled: "已取消",
  failed: "已中断",
};
const outcomes: Record<string, string> = {
  pending: "等待处理",
  applied: "已复核",
  conflict: "已跳过 · 证据或复核修订变化",
  blocked: "已跳过 · 检测尚未完成",
};
const errors: Record<string, string> = {
  access_revoked: "执行权限已失效。恢复前请确认当前账号仍有访问权限。",
  worker_failed: "执行中断，可恢复尚未处理的成员。",
  cancelled: "已取消，已完成的复核和审计记录会保留。",
};
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
function Failure({ error }: { error: unknown }) {
  return (
    <div role="alert" className="rounded-md bg-status-danger-soft p-3 text-xs text-status-danger">
      <CopyableError message={error instanceof Error ? error.message : "操作失败，请重试"} />
    </div>
  );
}
function Counts({ batch }: { batch: ReviewBatch }) {
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
      {[
        ["固定成员", batch.total],
        ["可复核", batch.actionable_count],
        ["已复核", batch.summary.applied_count ?? 0],
        [
          "冲突 / 阻止",
          `${batch.summary.conflict_count ?? 0} / ${batch.summary.blocked_count ?? 0}`,
        ],
      ].map(([label, value]) => (
        <div key={String(label)}>
          <dt className="text-xs text-muted-foreground">{String(label)}</dt>
          <dd className="mt-1 font-medium tabular-nums">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function DatasetQualityBulkReview({
  datasetId,
  run,
  selectedIds,
  clearSelection,
  query,
  context,
}: {
  datasetId: string;
  run: QualityRun;
  selectedIds: string[];
  clearSelection: () => void;
  query: ReviewPreview["query"];
  context: string;
}) {
  const owner = useDatasetOwner();
  const client = useQueryClient();
  const [url, setUrl] = useSearchParams();
  const batchId = url.get("quality_batch");
  const [historyPage, setHistoryPage] = useState(0);
  const [historySize, setHistorySize] = useState(10);
  const history = useReviewBatches(datasetId, run.id, historyPage, historySize);
  const detail = useReviewBatch(datasetId, run.id, batchId);
  const [memberPage, setMemberPage] = useState(0);
  const [memberSize, setMemberSize] = useState(20);
  const members = useReviewMembers(datasetId, run.id, detail.data, memberPage, memberSize);
  const [form, setForm] = useState<{ ids: string[]; query: ReviewPreview["query"] } | null>(null);
  const [scope, setScope] = useState<"selected" | "filtered">("filtered");
  const [decision, setDecision] = useState<"open" | "confirmed" | "dismissed">("confirmed");
  const [note, setNote] = useState("");
  const [preview, setPreview] = useState<ReviewBatch | null>(null);
  const [previewPage, setPreviewPage] = useState(0);
  const [previewSize, setPreviewSize] = useState(20);
  const previewMembers = useReviewMembers(
    datasetId,
    run.id,
    preview ?? undefined,
    previewPage,
    previewSize,
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [job, setJob] = useState<string | null>(null);
  const [clock, setClock] = useState(Date.now());
  const trigger = useRef<HTMLElement | null>(null);
  const alive = useRef(true);
  const operation = useRef(0);
  const contextRef = useRef(`${context}/${batchId ?? ""}`);
  contextRef.current = `${context}/${batchId ?? ""}`;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    operation.current++;
    setForm(null);
    setPreview(null);
    setError(null);
    setPending(false);
    setJob(null);
  }, [context]);
  useEffect(() => {
    operation.current++;
    setMemberPage(0);
    setError(null);
    setPending(false);
    setJob(null);
  }, [batchId]);
  useEffect(() => {
    setPreviewPage(0);
  }, [preview?.id]);
  useEffect(() => {
    if (!history.data || history.isFetching || history.error) return;
    const last = Math.max(0, Math.ceil(history.data.total / historySize) - 1);
    if (historyPage > last) setHistoryPage(last);
  }, [history.data, history.isFetching, history.error, historyPage, historySize]);
  useEffect(() => {
    if (!preview) return;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [preview]);
  // Terminal batch keys also refresh results after a background completion.
  useEffect(() => {
    if (detail.data && !reviewBatchActive(detail.data) && detail.data.status !== "preview") {
      void client.invalidateQueries({ queryKey: ["dataset-quality", datasetId] });
    }
  }, [client, datasetId, detail.data]);

  function patchBatch(id: string | null) {
    setUrl((previous) => {
      const next = new URLSearchParams(previous);
      next.set("quality_run", run.id);
      if (id) next.set("quality_batch", id);
      else next.delete("quality_batch");
      return next;
    });
  }
  function closeForm() {
    operation.current++;
    setPending(false);
    setForm(null);
    setPreview(null);
    setError(null);
  }
  function edit() {
    setPreview(null);
    setError(null);
  }
  async function perform(
    work: () => Promise<ReviewBatch | unknown>,
    done?: (value: ReviewBatch) => void,
  ) {
    if (pending) return;
    const token = ++operation.current,
      captured = contextRef.current;
    const current = () =>
      alive.current && token === operation.current && captured === contextRef.current;
    setPending(true);
    setError(null);
    try {
      owner.assert();
      const value = await work();
      owner.assert();
      if (!current()) return;
      await client.invalidateQueries({ queryKey: ["dataset-quality-review", datasetId, run.id] });
      owner.assert();
      if (current()) done?.(value as ReviewBatch);
    } catch (e) {
      if (current()) setError(e);
    } finally {
      if (current()) setPending(false);
    }
  }
  const active = ["pending", "running"].includes(run.status);
  const expired = !!preview && new Date(preview.expires_at).getTime() <= clock;
  const batch = detail.data;
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Button
          size="sm"
          variant="default"
          disabled={active || pending}
          onClick={(e) => {
            trigger.current = e.currentTarget;
            setScope(selectedIds.length ? "selected" : "filtered");
            setDecision("confirmed");
            setNote("");
            setPreview(null);
            setError(null);
            setForm({ ids: [...selectedIds], query: { ...query } });
          }}
        >
          批量复核
        </Button>
        <p className="text-xs text-muted-foreground">
          已选 {selectedIds.length} 项 · 最多勾选 200 项，也可复核全部筛选结果
        </p>
        {!!selectedIds.length && (
          <Button size="sm" variant="ghost" onClick={clearSelection}>
            清空勾选
          </Button>
        )}
        {active && <p className="text-xs text-muted-foreground">检测停止后可生成批量预览。</p>}
      </div>
      {!!history.error && <Failure error={history.error} />}
      {!!(error && !form && !batchId) && <Failure error={error} />}
      {!!history.data?.total && (
        <details className={listStyles.surface}>
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
            批量复核历史 · {history.data.total} 条
          </summary>
          <div className={listStyles.scroll}>
            <table className={listStyles.table}>
              <thead>
                <tr>
                  <th>创建时间</th>
                  <th>决定</th>
                  <th>固定成员</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {history.data.items.map((row) => (
                  <tr key={row.id}>
                    <td className="whitespace-nowrap">{date(row.created_at)}</td>
                    <td>{decisions[row.decision]}</td>
                    <td>{row.total}</td>
                    <td>{statuses[row.status] ?? row.status}</td>
                    <td>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={(e) => {
                          trigger.current = e.currentTarget;
                          setError(null);
                          patchBatch(row.id);
                        }}
                      >
                        查看记录
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ListPagination
            label="批量复核历史"
            page={historyPage}
            pageSize={historySize}
            total={history.data.total}
            loading={history.isFetching}
            onPageChange={setHistoryPage}
            onPageSizeChange={(size) => {
              setHistorySize(size);
              setHistoryPage(0);
            }}
          />
        </details>
      )}
      <Modal
        open={!!form}
        onClose={closeForm}
        title="批量复核 · 固定范围"
        width={680}
        returnFocusRef={trigger}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button variant="ghost" onClick={closeForm}>
              关闭
            </Button>
            {preview ? (
              <Button
                disabled={pending || expired || !preview.actionable_count}
                onClick={() => {
                  const frozen = preview;
                  void perform(
                    () => api.execute(datasetId, run.id, frozen),
                    (value) => {
                      closeForm();
                      clearSelection();
                      patchBatch(value.id);
                    },
                  );
                }}
              >
                {pending ? "正在提交…" : "按固定范围执行"}
              </Button>
            ) : (
              <Button
                disabled={pending || !note.trim() || (scope === "selected" && !form?.ids.length)}
                onClick={() => {
                  if (!form) return;
                  const body: ReviewPreview = {
                    scope,
                    state: decision,
                    note: note.trim(),
                    query: form.query,
                    result_ids: scope === "selected" ? form.ids : [],
                  };
                  void perform(
                    () => api.preview(datasetId, run.id, body),
                    (value) => {
                      setPreview(value);
                      setClock(Date.now());
                    },
                  );
                }}
              >
                {pending ? "正在生成…" : "生成影响预览"}
              </Button>
            )}
          </div>
        }
      >
        <div className="space-y-4">
          <p className="text-xs leading-relaxed text-muted-foreground">
            先固定本轮质量证据，再确认复核。已变化的复核修订会跳过并记录冲突。此操作只更新复核决定与原因。
          </p>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            选择范围
            <select
              className={input}
              value={scope}
              disabled={pending}
              aria-label="选择范围"
              aria-describedby="bulk-scope-hint"
              onChange={(e) => {
                setScope(e.target.value as typeof scope);
                edit();
              }}
            >
              {!!form?.ids.length && <option value="selected">已勾选的结果</option>}
              <option value="filtered">全部筛选结果</option>
            </select>
            <span id="bulk-scope-hint" className="leading-relaxed">
              {scope === "selected"
                ? `固定已勾选的 ${form?.ids.length ?? 0} 项，包含其他分页的勾选。`
                : "固定当前筛选命中的所有结果，包含其他分页；上限 10000 项。"}
            </span>
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            复核决定
            <select
              className={input}
              value={decision}
              disabled={pending}
              onChange={(e) => {
                setDecision(e.target.value as typeof decision);
                edit();
              }}
            >
              {Object.entries(decisions).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
            复核原因
            <textarea
              className={input}
              rows={3}
              maxLength={2000}
              disabled={pending}
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
                edit();
              }}
              aria-label="复核原因"
              aria-describedby="bulk-note-hint"
            />
            <span id="bulk-note-hint">必填。每个成功处理的成员都会保留此原因和独立审计记录。</span>
          </label>
          {preview && (
            <section
              aria-label="固定范围影响预览"
              className="space-y-3 rounded-lg border border-border bg-muted/30 p-4"
            >
              <h3 className="text-sm font-medium">固定范围影响预览</h3>
              <Counts batch={preview} />
              <p className="text-xs text-muted-foreground">
                {decisions[preview.decision]} · 预览有效至 {date(preview.expires_at)}
              </p>
              {!!preview.historical_count && (
                <p className="text-xs leading-relaxed text-status-caution">
                  包含 {preview.historical_count}{" "}
                  项历史证据，其当前样本绑定已变化或删除。复核决定只关联预览固定的旧证据。
                </p>
              )}
              {!!preview.summary.blocked_count && (
                <p className="text-xs text-status-caution">
                  检测未完成的成员将保留为跳过，不会在稍后自动纳入执行。
                </p>
              )}
              {expired && (
                <p role="alert" className="text-xs text-status-caution">
                  预览已过期，请重新生成。
                </p>
              )}
              <details className="rounded-md border border-border">
                <summary className="cursor-pointer px-3 py-2 text-sm">查看固定名单</summary>
                {!!previewMembers.error && <Failure error={previewMembers.error} />}
                {previewMembers.isLoading && (
                  <p role="status" className="px-3 py-2 text-xs text-muted-foreground">
                    正在加载固定成员…
                  </p>
                )}
                <div className={listStyles.scroll}>
                  <table className={listStyles.table}>
                    <thead>
                      <tr>
                        <th>固定样本</th>
                        <th>预期复核修订</th>
                        <th>可处理性</th>
                      </tr>
                    </thead>
                    <tbody>
                      {previewMembers.data?.items.map((member) => (
                        <tr key={member.id}>
                          <td className="max-w-64 break-all">
                            {String(member.snapshot.file_name ?? "历史样本")}
                          </td>
                          <td>{String(member.snapshot.review_revision ?? 0)}</td>
                          <td>
                            {member.state === "blocked" ? "检测未完成，将跳过" : "按固定证据检查"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <ListPagination
                  label="预览固定成员"
                  page={previewPage}
                  pageSize={previewSize}
                  total={previewMembers.data?.total}
                  loading={previewMembers.isFetching}
                  onPageChange={setPreviewPage}
                  onPageSizeChange={(size) => {
                    setPreviewSize(size);
                    setPreviewPage(0);
                  }}
                />
              </details>
              <Button size="sm" variant="ghost" disabled={pending} onClick={() => setPreview(null)}>
                重新生成预览
              </Button>
            </section>
          )}
          {!!error && <Failure error={error} />}
        </div>
      </Modal>
      <Modal
        open={!!batchId}
        onClose={() => {
          operation.current++;
          setPending(false);
          setError(null);
          patchBatch(null);
        }}
        title="批量复核记录"
        width={880}
        returnFocusRef={trigger}
      >
        <div className="space-y-4">
          {!!(detail.error || members.error || error) && (
            <Failure error={detail.error || members.error || error} />
          )}
          {detail.isLoading && (
            <p role="status" className="text-sm text-muted-foreground">
              正在加载固定范围…
            </p>
          )}
          {batch && (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p className="text-sm font-medium">
                    {statuses[batch.status] ?? batch.status} · {decisions[batch.decision]}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {date(batch.created_at)} · 已处理 {batch.processed} / {batch.total}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {batch.async_job_id && (
                    <Button size="sm" variant="ghost" onClick={() => setJob(batch.async_job_id!)}>
                      任务详情
                    </Button>
                  )}
                  {reviewBatchActive(batch) && batch.async_job_id && (
                    <Button
                      size="sm"
                      variant="default"
                      disabled={pending}
                      onClick={() => {
                        void perform(() => asyncJobsApi.cancel(batch.async_job_id!));
                      }}
                    >
                      取消执行
                    </Button>
                  )}
                  {["failed", "cancelled"].includes(batch.status) &&
                    batch.processed < batch.total && (
                      <Button
                        size="sm"
                        disabled={pending}
                        onClick={() => {
                          void perform(() => api.resume(datasetId, run.id, batch.id));
                        }}
                      >
                        恢复未完成项
                      </Button>
                    )}
                </div>
              </div>
              <Counts batch={batch} />
              <p className="whitespace-pre-wrap break-words text-sm">{batch.note}</p>
              {batch.status === "preview" && (
                <p className="text-xs text-muted-foreground">
                  此预览尚未执行。如需操作，请关闭记录后重新生成影响预览。
                </p>
              )}
              {!!batch.error_code && (
                <p role="status" className="text-xs text-status-caution">
                  {errors[batch.error_code] ?? "执行已中断，请查看任务记录。"}
                </p>
              )}
              {batch.summary.partial === true && (
                <p className="text-xs text-status-caution">
                  冲突与检测未完成的成员已跳过；恢复只处理尚未处理的固定成员。
                </p>
              )}
              {!!batch.historical_count && (
                <p className="text-xs text-muted-foreground">
                  包含 {batch.historical_count} 项历史证据，决定不会转移到新内容。
                </p>
              )}
              <div className={listStyles.surface}>
                <div className={listStyles.scroll}>
                  <table className={`${listStyles.table} min-w-xl`}>
                    <thead>
                      <tr>
                        <th>固定样本</th>
                        <th>处理结果</th>
                        <th>复核修订</th>
                      </tr>
                    </thead>
                    <tbody>
                      {members.data?.items.map((member) => (
                        <tr key={member.id}>
                          <td>
                            <p className="max-w-64 break-all">
                              {String(member.snapshot.file_name ?? "历史样本")}
                            </p>
                            <p className="mt-1 text-xs text-muted-foreground">
                              {member.snapshot.historical === true ? "历史证据" : "预览固定证据"}
                            </p>
                          </td>
                          <td>{outcomes[member.state] ?? member.state}</td>
                          <td className="tabular-nums">
                            {String(member.snapshot.review_revision ?? 0)} →{" "}
                            {member.applied_revision ?? "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <ListPagination
                  label="固定复核成员"
                  page={memberPage}
                  pageSize={memberSize}
                  total={members.data?.total}
                  loading={members.isFetching}
                  onPageChange={setMemberPage}
                  onPageSizeChange={(size) => {
                    setMemberSize(size);
                    setMemberPage(0);
                  }}
                />
              </div>
            </>
          )}
        </div>
      </Modal>
      {job && <AsyncJobDetailModal jobId={job} onClose={() => setJob(null)} />}
    </>
  );
}
