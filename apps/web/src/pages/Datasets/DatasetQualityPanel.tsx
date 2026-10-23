import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { datasetQualityApi, type QualityConfig, type QualityResult } from "@/api/datasetQuality";
import { asyncJobsApi } from "@/api/asyncJobs";
import { useDatasetOwner } from "@/hooks/useDatasets";
import {
  qualityActive,
  useQualityRun,
  useQualityResults,
  useQualityRuns,
} from "@/hooks/useDatasetQuality";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { JOB_STATUS_LABEL } from "@/components/jobs/asyncJobPresentation";
import { DatasetQualityBulkReview } from "./DatasetQualityBulkReview";
import { clampDatasetPage, pageNumber } from "./datasetsUrlState";

const input =
  "w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const defaults: QualityConfig = {
  min_width: 64,
  min_height: 64,
  max_width: 8192,
  max_height: 8192,
};
const findings: Record<string, string> = {
  decode_failed: "图片无法解码",
  dimensions_out_of_range: "尺寸超出范围",
  exact_duplicate: "精确重复",
  skipped: "未检测",
};
const reviews: Record<string, string> = {
  open: "待复核",
  confirmed: "已确认",
  dismissed: "已忽略",
};
const reasons: Record<string, string> = {
  path_unavailable: "源路径缺失或不可达",
  permission_denied: "源文件访问被拒绝",
  filesystem_unavailable: "文件系统不可用",
  directory_identity_changed: "目录身份已变化",
  root_identity_changed: "存储根身份已变化",
  path_changed: "源路径已变化",
  content_unverified: "尚无可信内容身份",
  media_not_supported: "本轮仅检测图片",
  item_removed: "样本已删除",
  content_mismatch: "原图字节已变化",
  source_changed: "源文件已变化",
  content_binding_changed: "内容绑定已变化",
  source_unavailable: "来源不可用",
  location_unavailable: "存储位置不可用",
  no_readable_original: "无可读原图",
  content_source_unavailable: "原图读取失败",
  source_missing: "源文件缺失",
  file_missing: "源文件缺失",
  worker_read_not_verified: "执行节点未核验此目录",
  image_format_unsupported: "图片编码暂不支持",
  image_pixel_budget_exceeded: "图片超过像素预算",
  content_too_large: "原图超过读取预算",
  content_read_timeout: "原图读取超时",
  snapshot_capacity_exceeded: "读取容量繁忙",
  access_revoked: "当前执行权限已失效",
  worker_failed: "执行中断，可恢复未完成项",
  cancelled: "已取消，保留已完成项",
};
const date = (value: string | null) => (value ? new Date(value).toLocaleString("zh-CN") : "—");
const message = (error: unknown) => (error instanceof Error ? error.message : "操作失败，请重试");
function Failure({ error }: { error: unknown }) {
  return (
    <div role="alert" className="rounded-md bg-status-danger-soft p-3 text-xs text-status-danger">
      <CopyableError message={message(error)} />
    </div>
  );
}
export function DatasetQualityPanel({
  datasetId,
  canManage,
}: {
  datasetId: string;
  canManage: boolean;
}) {
  const [url, setUrl] = useSearchParams();
  const owner = useDatasetOwner();
  const client = useQueryClient();
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const historyPage = pageNumber(url.get("quality_history_page"));
  const historySize = listPageSize(url.get("quality_history_page_size"), 10);
  const history = useQualityRuns(datasetId, historyPage, historySize, canManage);
  const selected = url.get("quality_run") || history.data?.items[0]?.id;
  const run = useQualityRun(datasetId, selected, canManage);
  const page = pageNumber(url.get("quality_page"));
  const size = listPageSize(url.get("quality_page_size"));
  const params = new URLSearchParams({ limit: String(size), offset: String(page * size) });
  const finding = url.get("quality_finding") || "";
  const review = url.get("quality_review") || "";
  const sha = url.get("quality_sha") || "";
  if (finding) params.set("finding", finding);
  if (review) params.set("review", review);
  if (sha) params.set("sha256", sha);
  const results = useQualityResults(datasetId, run.data, params);
  const [form, setForm] = useState<
    { kind: "start" } | { kind: "review"; run: string; result: QualityResult } | null
  >(null);
  const [config, setConfig] = useState<QualityConfig>({ ...defaults });
  const [decision, setDecision] = useState("confirmed");
  const [note, setNote] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [pending, setPending] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const context = `${datasetId}/${selected}/${finding}/${review}/${sha}/${page}/${size}/${owner.revision}`;
  const selectionContext = `${datasetId}/${selected}/${finding}/${review}/${sha}/${owner.revision}`;
  const [selection, setSelection] = useState<{ context: string; ids: string[] }>({
    context: selectionContext,
    ids: [],
  });
  const selectedIds = selection.context === selectionContext ? selection.ids : [];
  function toggleRows(ids: string[], checked: boolean) {
    setSelection((previous) => {
      const values = new Set(previous.context === selectionContext ? previous.ids : []);
      ids.forEach((id) => {
        if (!checked) values.delete(id);
        else if (values.size < 200) values.add(id);
      });
      return { context: selectionContext, ids: [...values] };
    });
  }
  const pageIds =
    results.data?.items.filter((row) => row.state !== "pending").map((row) => row.id) ?? [];
  const operation = useRef(0);
  const contextRef = useRef(context);
  contextRef.current = context;
  useEffect(() => {
    operation.current++;
    setForm(null);
    setError(null);
    setPending(false);
  }, [context]);
  useEffect(() => {
    if (!results.data || results.isFetching || results.error) return;
    const clamped = clampDatasetPage(page, size, results.data.total);
    if (clamped !== page)
      setUrl(
        (previous) => {
          const next = new URLSearchParams(previous);
          if (clamped) next.set("quality_page", String(clamped + 1));
          else next.delete("quality_page");
          return next;
        },
        { replace: true },
      );
  }, [results.data, results.isFetching, results.error, page, size, setUrl]);
  function closeForm() {
    operation.current++;
    setForm(null);
    setError(null);
    setPending(false);
  }
  function patch(values: Record<string, string | undefined>) {
    setUrl((previous) => {
      const next = new URLSearchParams(previous);
      next.delete("quality_batch");
      Object.entries(values).forEach(([key, value]) =>
        value ? next.set(key, value) : next.delete(key),
      );
      return next;
    });
  }
  async function perform(work: () => Promise<unknown>, done?: (value: unknown) => void) {
    if (pending) return;
    const operationContext = contextRef.current;
    const token = ++operation.current;
    const current = () =>
      alive.current && contextRef.current === operationContext && operation.current === token;
    setPending(true);
    setError(null);
    try {
      owner.assert();
      const value = await work();
      owner.assert();
      if (!current()) return;
      await client.invalidateQueries({ queryKey: ["dataset-quality", datasetId] });
      if (!current()) return;
      owner.assert();
      if (current()) done?.(value);
    } catch (e) {
      if (current()) setError(e);
    } finally {
      if (current()) setPending(false);
    }
  }
  function openReview(result: QualityResult, target: HTMLElement) {
    trigger.current = target;
    setError(null);
    setDecision(result.review_state === "open" ? "confirmed" : result.review_state);
    setNote(result.review_note);
    setForm({ kind: "review", run: run.data!.id, result });
  }
  if (!canManage)
    return (
      <p className="py-8 text-sm text-muted-foreground">需要数据集管理权限才能检测和复核质量。</p>
    );
  const active = qualityActive(run.data);
  return (
    <section aria-label="图片质量管理" className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-md font-semibold">图片质量</h2>
          <p className="mt-1 max-w-prose text-xs leading-relaxed text-muted-foreground">
            检查原图解码、尺寸范围与精确重复。每轮固定整份数据集的成员，结果保留检测时的内容身份。
          </p>
        </div>
        <Button
          size="sm"
          disabled={
            pending || history.isLoading || active || history.data?.items.some(qualityActive)
          }
          onClick={(e) => {
            trigger.current = e.currentTarget;
            setConfig({ ...defaults });
            setError(null);
            setForm({ kind: "start" });
          }}
        >
          新建检测
        </Button>
      </div>
      {!!(history.error || run.error || results.error || (error && !form)) && (
        <Failure error={history.error || run.error || results.error || error} />
      )}
      {(history.isLoading || run.isLoading) && (
        <p role="status" className="text-sm text-muted-foreground">
          正在加载质量记录…
        </p>
      )}
      {!history.isLoading && !selected && !history.error && (
        <div className={`${listStyles.surface} ${listStyles.empty}`}>
          尚未检测。新建一轮检测，查看可复核的图片问题与重复成员。
        </div>
      )}
      {run.data && (
        <>
          <div className="rounded-xl border border-border bg-card p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <p className="text-sm font-medium">
                  {date(run.data.created_at)} ·{" "}
                  {JOB_STATUS_LABEL[run.data.status as keyof typeof JOB_STATUS_LABEL] ??
                    run.data.status}
                </p>
                <p className="text-xs tabular-nums text-muted-foreground">
                  已处理 {run.data.processed} / {run.data.total} 个固定成员 · 宽{" "}
                  {run.data.config.min_width}–{run.data.config.max_width}，高{" "}
                  {run.data.config.min_height}–{run.data.config.max_height} px
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {run.data.async_job_id && (
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => setJobId(run.data!.async_job_id)}
                  >
                    任务详情
                  </Button>
                )}
                {active && run.data.async_job_id && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={pending}
                    onClick={() => void perform(() => asyncJobsApi.cancel(run.data!.async_job_id!))}
                  >
                    取消检测
                  </Button>
                )}
                {["failed", "cancelled"].includes(run.data.status) && (
                  <Button
                    size="sm"
                    disabled={pending}
                    onClick={() =>
                      void perform(() => datasetQualityApi.resume(datasetId, run.data!.id))
                    }
                  >
                    继续未完成项
                  </Button>
                )}
              </div>
            </div>
            {run.data.error_code && (
              <p role="status" className="mt-3 text-xs text-status-caution">
                {reasons[run.data.error_code] ?? run.data.error_code}
              </p>
            )}
            {!active && (
              <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-3 border-t border-border pt-3 text-sm tabular-nums">
                {Object.entries({
                  checked_count: "已检测",
                  skipped_count: "未检测",
                  issue_count: "解码或尺寸异常",
                  duplicate_group_count: "重复组",
                  duplicate_member_count: "重复成员",
                }).map(([key, label]) => (
                  <div key={key}>
                    <dt className="text-xs text-muted-foreground">{label}</dt>
                    <dd className="mt-1 font-medium">{Number(run.data!.summary[key] ?? 0)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-40 flex-col gap-1.5 text-xs text-muted-foreground">
              检测结果
              <select
                className={input}
                value={finding}
                onChange={(e) =>
                  patch({ quality_finding: e.target.value, quality_page: undefined })
                }
              >
                <option value="">全部结果</option>
                {Object.entries(findings).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex min-w-36 flex-col gap-1.5 text-xs text-muted-foreground">
              复核状态
              <select
                className={input}
                value={review}
                onChange={(e) => patch({ quality_review: e.target.value, quality_page: undefined })}
              >
                <option value="">全部状态</option>
                {Object.entries(reviews).map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            {sha && (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => patch({ quality_sha: undefined, quality_page: undefined })}
              >
                退出重复组
              </Button>
            )}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                void run.refetch();
                void results.refetch();
              }}
            >
              刷新结果
            </Button>
          </div>
          <DatasetQualityBulkReview
            key={selectionContext}
            datasetId={datasetId}
            run={run.data}
            selectedIds={selectedIds}
            clearSelection={() => setSelection({ context: selectionContext, ids: [] })}
            query={{
              finding:
                (finding as
                  | "decode_failed"
                  | "dimensions_out_of_range"
                  | "exact_duplicate"
                  | "skipped") || undefined,
              review: (review as "open" | "confirmed" | "dismissed") || undefined,
              sha256: sha || undefined,
            }}
            context={context}
          />
          <p className="text-xs text-muted-foreground">
            重复仅依据本轮完整读取的 SHA-256 与字节数。
            {active
              ? "当前结果仍在增加，重复组可能变化。"
              : "复核决定只作用于本轮证据，不删除样本。"}
          </p>
          <div className={listStyles.surface}>
            <div className={listStyles.scroll}>
              <table className={`${listStyles.table} min-w-3xl`}>
                <thead>
                  <tr>
                    <th>
                      <input
                        type="checkbox"
                        aria-label="勾选本页可复核结果"
                        disabled={active || !pageIds.length}
                        checked={
                          !!pageIds.length && pageIds.every((id) => selectedIds.includes(id))
                        }
                        onChange={(e) => toggleRows(pageIds, e.target.checked)}
                      />
                    </th>
                    <th>样本</th>
                    <th>检测结果</th>
                    <th>检测尺寸</th>
                    <th>复核</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {results.data?.items.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`勾选 ${row.file_name}`}
                          checked={selectedIds.includes(row.id)}
                          disabled={
                            active ||
                            row.state === "pending" ||
                            (selectedIds.length >= 200 && !selectedIds.includes(row.id))
                          }
                          onChange={(e) => toggleRows([row.id], e.target.checked)}
                        />
                      </td>
                      <td>
                        <button
                          className="max-w-64 break-all text-left text-sm text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          onClick={() => patch({ item: row.item_id })}
                        >
                          {row.file_name}
                        </button>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {row.stale ? "历史证据 · 当前绑定已变化或删除" : date(row.observed_at)}
                        </p>
                      </td>
                      <td>
                        <div className="space-y-1">
                          {row.state === "pending" ? (
                            "等待检测"
                          ) : row.state === "skipped" ? (
                            <span className="text-muted-foreground">
                              未检测 · {reasons[row.reason_code ?? ""] ?? row.reason_code}
                            </span>
                          ) : row.findings.length ? (
                            row.findings.map((code) => (
                              <p key={code} className="text-status-caution">
                                {findings[code] ?? code}
                              </p>
                            ))
                          ) : (
                            "解码与尺寸正常"
                          )}
                          {row.duplicate_count > 1 && (
                            <p>
                              <button
                                className="text-sm text-status-caution underline"
                                onClick={() =>
                                  patch({
                                    quality_sha: row.sha256 ?? undefined,
                                    quality_page: undefined,
                                  })
                                }
                              >
                                精确重复 · {row.duplicate_count} 个成员
                              </button>
                            </p>
                          )}
                        </div>
                      </td>
                      <td className="whitespace-nowrap tabular-nums">
                        {row.width && row.height ? `${row.width} × ${row.height}` : "—"}
                      </td>
                      <td className="whitespace-nowrap">
                        {reviews[row.review_state] ?? row.review_state}
                      </td>
                      <td>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={pending || row.state === "pending"}
                          onClick={(e) => openReview(row, e.currentTarget)}
                        >
                          复核详情
                        </Button>
                      </td>
                    </tr>
                  ))}
                  {!results.isLoading && !results.data?.items.length && (
                    <tr>
                      <td colSpan={6} className={listStyles.empty}>
                        没有符合条件的质量结果
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            <ListPagination
              label="质量结果"
              page={page}
              pageSize={size}
              total={results.data?.total}
              loading={results.isFetching}
              onPageChange={(p) => patch({ quality_page: p ? String(p + 1) : undefined })}
              onPageSizeChange={(s) =>
                patch({ quality_page_size: String(s), quality_page: undefined })
              }
            />
          </div>
        </>
      )}
      {!!history.data?.total && (
        <details className={listStyles.surface}>
          <summary className="cursor-pointer px-4 py-3 text-sm font-medium">
            检测历史 · {history.data.total} 轮
          </summary>
          <div className={listStyles.scroll}>
            <table className={listStyles.table}>
              <thead>
                <tr>
                  <th>提交时间</th>
                  <th>状态</th>
                  <th>固定成员</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {history.data.items.map((row) => (
                  <tr key={row.id}>
                    <td>{date(row.created_at)}</td>
                    <td>
                      {JOB_STATUS_LABEL[row.status as keyof typeof JOB_STATUS_LABEL] ?? row.status}
                    </td>
                    <td>{row.total}</td>
                    <td>
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-current={selected === row.id ? "true" : undefined}
                        onClick={() =>
                          patch({
                            quality_run: row.id,
                            quality_page: undefined,
                            quality_sha: undefined,
                          })
                        }
                      >
                        查看结果
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <ListPagination
            label="检测历史"
            page={historyPage}
            pageSize={historySize}
            total={history.data.total}
            onPageChange={(p) => patch({ quality_history_page: p ? String(p + 1) : undefined })}
            onPageSizeChange={(s) =>
              patch({ quality_history_page_size: String(s), quality_history_page: undefined })
            }
          />
        </details>
      )}
      {form && (
        <Modal
          open
          onClose={closeForm}
          title={form.kind === "start" ? "新建图片质量检测" : "复核质量证据"}
          returnFocusRef={trigger}
          width={560}
        >
          <form
            className="space-y-4"
            onSubmit={(e) => {
              e.preventDefault();
              const frozen = form;
              if (frozen.kind === "start")
                void perform(
                  () => datasetQualityApi.start(datasetId, config),
                  (value) => {
                    setForm(null);
                    patch({
                      quality_run: (value as { id: string }).id,
                      quality_page: undefined,
                      quality_sha: undefined,
                    });
                  },
                );
              else
                void perform(
                  () =>
                    datasetQualityApi.review(datasetId, frozen.run, frozen.result.id, {
                      state: decision as "open" | "confirmed" | "dismissed",
                      note,
                      expected_revision: frozen.result.review_revision,
                    }),
                  () => setForm(null),
                );
            }}
          >
            {form.kind === "start" ? (
              <>
                <p className="text-sm leading-relaxed">
                  提交时固定整份数据集，最多 10000
                  个成员。未核验内容和非图片将标为未检测。完成后可按结果筛选与复核。
                </p>
                <details>
                  <summary className="cursor-pointer text-sm">尺寸规则</summary>
                  <div className="mt-3 grid grid-cols-2 gap-3">
                    {Object.entries({
                      min_width: "最小宽度",
                      max_width: "最大宽度",
                      min_height: "最小高度",
                      max_height: "最大高度",
                    }).map(([key, label]) => (
                      <label key={key} className="space-y-1.5 text-xs text-muted-foreground">
                        <span className="block">{label}（px）</span>
                        <input
                          className={input}
                          type="number"
                          min={1}
                          max={100000}
                          required
                          value={config[key as keyof QualityConfig]}
                          onChange={(e) =>
                            setConfig((v) => ({ ...v, [key]: Number(e.target.value) }))
                          }
                        />
                      </label>
                    ))}
                  </div>
                </details>
                <p className="text-xs leading-relaxed text-muted-foreground">
                  默认宽高各 64–8192 px；原图读取最多 64 MiB，解码最多 3200
                  万像素。检测和复核均不会删除、合并样本或改变训练资格。
                </p>
              </>
            ) : (
              <>
                <div className="space-y-2 text-sm">
                  <p className="break-all font-medium">{form.result.file_name}</p>
                  <p className="text-xs text-muted-foreground">
                    {form.result.findings.map((code) => findings[code] ?? code).join(" · ") ||
                      (form.result.state === "skipped"
                        ? `未检测 · ${reasons[form.result.reason_code ?? ""] ?? form.result.reason_code}`
                        : "解码与尺寸正常")}
                    {form.result.duplicate_count > 1 &&
                      ` · 精确重复 ${form.result.duplicate_count} 个成员`}
                  </p>
                  <dl className="space-y-2 text-xs text-muted-foreground">
                    <div>
                      <dt>内容 SHA-256</dt>
                      <dd className="mt-1 break-all font-mono">
                        {form.result.sha256 ?? "尚无可信身份"}
                      </dd>
                    </div>
                    <div>
                      <dt>完整字节核验</dt>
                      <dd>
                        {form.result.bytes_verified
                          ? `本轮已核验 · ${date(form.result.observed_at)}`
                          : "本轮未完成核验"}
                      </dd>
                    </div>
                  </dl>
                  {form.result.stale && (
                    <p className="text-xs text-status-caution">
                      这是历史证据，当前样本绑定已变化或删除。
                    </p>
                  )}
                </div>
                <label className="block space-y-1.5 text-xs text-muted-foreground">
                  <span className="block">复核决定</span>
                  <select
                    className={input}
                    value={decision}
                    onChange={(e) => setDecision(e.target.value)}
                    aria-describedby="quality-review-hint"
                  >
                    {Object.entries(reviews).map(([key, label]) => (
                      <option key={key} value={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <p id="quality-review-hint" className="text-xs text-muted-foreground">
                  决定只记录在这份内容与本轮规则的证据上，不应用到其他运行。
                </p>
                <label className="block space-y-1.5 text-xs text-muted-foreground">
                  <span className="block">复核原因</span>
                  <textarea
                    className={input}
                    required
                    maxLength={2000}
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    rows={3}
                  />
                </label>
                {form.result.reviewed_at && (
                  <p className="text-xs text-muted-foreground">
                    上次复核：{date(form.result.reviewed_at)} · 修订 {form.result.review_revision}
                  </p>
                )}
              </>
            )}
            {!!error && <Failure error={error} />}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" onClick={closeForm}>
                关闭
              </Button>
              <Button type="submit" disabled={pending}>
                {pending ? "正在提交…" : form.kind === "start" ? "开始检测" : "保存复核"}
              </Button>
            </div>
          </form>
        </Modal>
      )}
      {jobId && <AsyncJobDetailModal jobId={jobId} onClose={() => setJobId(null)} />}
    </section>
  );
}
