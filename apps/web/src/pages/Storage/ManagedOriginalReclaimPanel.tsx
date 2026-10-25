import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { asyncJobsApi } from "@/api/asyncJobs";
import type {
  ManagedOriginalReclaimHistory,
  ManagedOriginalReclaimItem,
  ManagedOriginalReclaimPreview,
  ManagedOriginalReclaimReview,
} from "@/api/generated/types.gen";
import { managedOriginalReclaimsApi } from "@/api/managedOriginalReclaims";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { cacheBytes } from "@/utils/cachePolicy";

const hint = "text-xs leading-relaxed text-muted-foreground";
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const statuses = {
  pending: "排队中",
  running: "回收中",
  completed: "任务已结束",
  failed: "回收失败",
  cancelled: "任务已取消",
  unknown: "结果待确认",
};
const outcomes = {
  deleted: "准确版本已回收",
  already_absent: "准确版本已缺失",
  missing: "读取时未找到准确版本",
  changed: "内容或执行条件已变化",
  failed: "本次回收失败",
  unknown: "本次删除结果待确认",
  cancelled: "任务已取消",
};
const phases = {
  claimed: "已认领，等待读取",
  reading: "正在完整读取准确版本",
  delete_intent: "已登记删除责任",
  delete_ack: "已收到删除应答",
  absence_verified: "已验证准确版本缺失",
  settled: "本次操作已收尾",
};
const conditions: Record<string, string> = {
  reclaim_creation_unproven: "缺少可信的条件创建及完整读回证据，历史对象不能据当前内容补认。",
  reclaim_immutable_version_required: "未保存准确且非空的 VersionId，当前不能执行准确版本回收。",
  reclaim_content_unverified: "保存的内容摘要或大小尚未完成核验。",
  reclaim_size_budget: "原件超过单次 5 GiB 的完整读取预算。",
  reclaim_creation_changed: "原件记录与保存的创建目标不一致。",
  reclaim_configuration_changed: "保存位置的当前配置无法核实，请检查位置配置。",
  reclaim_location_not_stopped: "准确逻辑位置尚未停止，请先完成对应位置或迁移窗口的维护停止。",
  physical_operation_unsettled: "仍有实际存储操作未确认收尾，不能启动新的回收。",
  reclaim_claimed: "该物理版本已有回收认领，请查看已有任务与收尾记录。",
  reclaim_already_deleted: "该准确版本已确认缺失，不会再次删除或累计释放容量。",
  reclaim_retry_unavailable: "该历史尝试当前不具备重试资格，请检查终态和未收尾责任。",
  reclaim_preview_invalid: "回收预演已失效，请重新预览。",
  reclaim_preview_changed: "预演绑定的条件已变化，请重新预览。",
  reclaim_authority_changed: "操作者或访问密钥的权限已变化。",
  reclaim_context_changed: "引用、存储配置或维护代次已变化，请重新预览。",
  reclaim_delete_unknown: "删除结果尚未确认，不能把任务终态解释为未删除或已回收。",
  retention_window: "准确版本仍在保留期内。",
  indefinite_retention: "相关记录仍为无限期保留。",
  asset_locations: "仍有内容位置引用此物理对象。",
  dataset_items: "仍有数据集样本引用此物理对象。",
  tasks: "仍有任务引用此物理对象。",
  task_dataset_links: "仍有任务与数据集关联保护此物理对象。",
  training_versions: "仍有保留的训练版本引用此物理对象。",
  active_learning_roots: "仍有主动学习历史引用此物理对象。",
  ambiguous_legacy_tasks: "旧任务的物理桶归属无法确认，不能视为零引用。",
  task_originals: "仍有任务原件引用此物理对象。",
  reference_aliases: "仍有引用来源的别名指向此物理对象。",
  migration_inventories: "仍有迁移清单任务保护此物理对象。",
  migration_copies: "仍有迁移复制任务保护此物理对象。",
  operations: "此物理对象或别名位置仍有实际存储操作未确认收尾。",
  unknown_training_versions: "存在无法完整解析的训练快照，不能视为零引用。",
  unknown_active_learning_rounds: "存在无法完整解析的主动学习历史，不能视为零引用。",
  unknown_reference_aliases: "存在无法确认定位的引用别名，不能视为零引用。",
  unknown_operations: "存在无法确认物理定位的存储操作，不能视为已经排空。",
  declared_backup_holds: "相关记录仍有有效的备份保留要求。",
  declared_migration_holds: "相关记录仍有有效的迁移观察要求。",
};
const conditionMessage = (code: string) =>
  conditions[code] ?? "仍有尚未满足的保护或执行条件，请刷新并检查保留关系与维护状态。";
const active = (item: ManagedOriginalReclaimItem) =>
  item.status === "pending" || item.status === "running";
const retryable = (item: ManagedOriginalReclaimItem) =>
  !active(item) &&
  !item.unsettled_operation &&
  !item.absence_proven &&
  item.outcome !== "deleted" &&
  item.outcome !== "already_absent";
const has = (record: object, key: unknown) =>
  typeof key === "string" && Object.prototype.hasOwnProperty.call(record, key);
function validItem(item: ManagedOriginalReclaimItem) {
  return (
    item &&
    typeof item.id === "string" &&
    !!item.id &&
    has(statuses, item.status) &&
    has(phases, item.phase) &&
    (item.outcome === null || has(outcomes, item.outcome)) &&
    typeof item.version_id === "string" &&
    !!item.version_id &&
    item.version_id !== "null" &&
    Number.isSafeInteger(item.expected_size_bytes) &&
    item.expected_size_bytes >= 0 &&
    Number.isFinite(Date.parse(item.requested_at)) &&
    typeof item.delete_started === "boolean" &&
    typeof item.absence_proven === "boolean" &&
    typeof item.unsettled_operation === "boolean"
  );
}

function validHistory(data: ManagedOriginalReclaimHistory, id: string) {
  return (
    data.retirement_id === id &&
    Number.isFinite(Date.parse(data.measured_at)) &&
    typeof data.can_preview === "boolean" &&
    Number.isSafeInteger(data.total) &&
    data.total >= 0 &&
    Number.isSafeInteger(data.max_bytes) &&
    data.max_bytes > 0 &&
    Number.isFinite(data.timeout_seconds) &&
    data.timeout_seconds > 0 &&
    Array.isArray(data.blockers) &&
    Array.isArray(data.items) &&
    data.items.every(validItem)
  );
}

function validPreview(data: ManagedOriginalReclaimPreview, id: string) {
  return (
    data.retirement_id === id &&
    data.irreversible === true &&
    typeof data.eligible === "boolean" &&
    Number.isFinite(Date.parse(data.measured_at)) &&
    Number.isFinite(Date.parse(data.expires_at)) &&
    Array.isArray(data.blockers) &&
    (!data.eligible ||
      (typeof data.impact_token === "string" &&
        data.impact_token.length > 0 &&
        typeof data.version_id === "string" &&
        data.version_id.length > 0 &&
        data.version_id !== "null" &&
        typeof data.expected_size_bytes === "number" &&
        Number.isSafeInteger(data.expected_size_bytes) &&
        data.expected_size_bytes >= 0))
  );
}

function failureMessage(error: unknown) {
  if (storageAccessDenied(error)) return "当前权限或记录已失效，已隐藏回收历史。";
  if (error instanceof ApiError && error.status === 409)
    return "引用、保留、存储维护或确认条件已变化，请刷新后重新预览。";
  if (error instanceof ApiError && error.status === 422)
    return "回收输入不符合要求，请检查原因并重新预览。";
  return "请求未能确认完成，请刷新回收历史后检查，避免重复提交。";
}

interface Props {
  retirementId: string;
  policyRevision: number;
  onDenied: () => void;
}

export function ManagedOriginalReclaimPanel(props: Props) {
  const revision = useSessionRevision();
  return (
    <ReclaimDisclosure
      key={`${revision}/${props.retirementId}/${props.policyRevision}`}
      {...props}
    />
  );
}

function ReclaimDisclosure(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-border"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">受控回收</summary>
      {open && <ReclaimOwner {...props} />}
    </details>
  );
}

function ReclaimOwner({ retirementId, onDenied }: Props) {
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const client = useQueryClient();
  const formId = useId();
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => {
    const change = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [actionDenied, setActionDenied] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reason, setReason] = useState("");
  const [retry, setRetry] = useState<ManagedOriginalReclaimItem | null>(null);
  const [review, setReview] = useState<{
    receipt: ManagedOriginalReclaimPreview;
    payload: ManagedOriginalReclaimReview;
  } | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [expired, setExpired] = useState(false);
  const [applying, setApplying] = useState(false);
  const [cancelRequested, setCancelRequested] = useState<Set<string>>(new Set());
  const [jobId, setJobId] = useState<string | null>(null);
  const previewTrigger = useRef<HTMLElement | null>(null);
  const cancelButton = useRef<HTMLButtonElement | null>(null);
  const jobTrigger = useRef<HTMLElement | null>(null);
  const query = useQuery({
    queryKey: ["managed-original-reclaims", session, revision, retirementId, pageSize, page],
    queryFn: ({ signal }) =>
      managedOriginalReclaimsApi.history(retirementId, pageSize, page * pageSize, signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
    refetchInterval: (current) =>
      visible &&
      !actionDenied &&
      !current.state.error &&
      current.state.data &&
      validHistory(current.state.data, retirementId) &&
      current.state.data.items.some(active)
        ? 5000
        : false,
  });
  const denied = actionDenied || storageAccessDenied(query.error);
  const data =
    !denied && query.data && validHistory(query.data, retirementId) ? query.data : undefined;
  const disabled = busy || query.isFetching || !!query.error || !data;
  useEffect(() => {
    if (denied) {
      setReview(null);
      setJobId(null);
      onDenied();
    }
  }, [denied, onDenied]);
  useEffect(() => {
    if (review) {
      setExpired(Date.parse(review.receipt.expires_at) <= Date.now());
      const timer = window.setTimeout(
        () => setExpired(true),
        Math.max(0, Date.parse(review.receipt.expires_at) - Date.now()),
      );
      return () => window.clearTimeout(timer);
    }
  }, [review]);
  useEffect(() => {
    if (!data || query.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total / pageSize) - 1);
    if (page > last) setPage(last);
  }, [data, page, pageSize, query.isFetching]);

  const fail = (failure: unknown) => {
    setReview(null);
    setError(failureMessage(failure));
    if (storageAccessDenied(failure)) setActionDenied(true);
  };
  const refresh = async (current: () => boolean) => {
    await Promise.all([
      client.invalidateQueries({
        queryKey: ["managed-original-reclaims", session, revision, retirementId],
        refetchType: "none",
      }),
      client.invalidateQueries({ queryKey: ["async-jobs"] }),
    ]);
    if (!current()) return;
    if (page > 0) setPage(0);
    else await query.refetch({ throwOnError: true });
  };
  const preview = async (candidate: ManagedOriginalReclaimItem | null) => {
    if (disabled || pending.current || reason.trim().length < 8) return;
    const current = capture();
    const payload: ManagedOriginalReclaimReview = {
      reason: reason.trim(),
      ...(candidate ? { retry_of_id: candidate.id } : {}),
    };
    pending.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const receipt = await managedOriginalReclaimsApi.preview(retirementId, payload);
      if (!current()) return;
      if (!validPreview(receipt, retirementId)) throw new Error("Reclaim preview owner mismatch");
      setRetry(candidate);
      setConfirmed(false);
      setReview({ receipt, payload });
    } catch (failure) {
      if (current()) fail(failure);
    } finally {
      if (current()) {
        pending.current = false;
        setBusy(false);
      }
    }
  };
  const run = async (
    operation: "start" | "redispatch" | "cancel",
    item?: ManagedOriginalReclaimItem,
  ) => {
    if (disabled || pending.current) return;
    if (
      operation === "start" &&
      (!review?.receipt.eligible ||
        !review.receipt.impact_token ||
        !confirmed ||
        expired ||
        Date.parse(review.receipt.expires_at) <= Date.now())
    )
      return;
    const current = capture();
    pending.current = true;
    setBusy(true);
    setApplying(operation === "start");
    setError("");
    setMessage("");
    let accepted = false;
    try {
      if (operation === "cancel") {
        if (!item?.async_job_id) return;
        const result = await asyncJobsApi.cancel(item.async_job_id);
        if (!current()) return;
        if (result.id !== item.async_job_id) throw new Error("Cancellation owner mismatch");
        accepted = true;
        if (["cancel_requested", "cancelled"].includes(result.status))
          setCancelRequested((previous) => new Set(previous).add(item.id));
        setMessage("取消请求已受理；请以回收历史中的实际删除与收尾记录为准。");
      } else {
        const result =
          operation === "start" && review?.receipt.impact_token
            ? await managedOriginalReclaimsApi.start(retirementId, {
                ...review.payload,
                impact_token: review.receipt.impact_token,
              })
            : item
              ? await managedOriginalReclaimsApi.redispatch(item.id)
              : null;
        if (!current()) return;
        if (
          !result ||
          result.retirement_id !== retirementId ||
          !result.reclaim_id ||
          !result.async_job_id
        )
          throw new Error("Reclaim task owner mismatch");
        accepted = true;
        setReview(null);
        setRetry(null);
        setMessage(
          result.existing
            ? "已复用已有回收请求，请查看实际任务进展。"
            : "准确版本回收请求已受理，请查看实际任务进展。",
        );
      }
      if (!current()) return;
      await refresh(current);
    } catch (failure) {
      if (!current()) return;
      if (accepted && !storageAccessDenied(failure))
        setError("请求已受理，但刷新回收历史失败，请刷新后继续操作。");
      else fail(failure);
      if (!accepted && !storageAccessDenied(failure)) await query.refetch();
    } finally {
      if (current()) {
        pending.current = false;
        setBusy(false);
        setApplying(false);
      }
    }
  };

  return (
    <div className="min-w-0 space-y-3 border-t border-border p-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className={hint}>
          每次仅处理绑定的一个准确版本；同键其他版本、外部原件、缓存和训练副本不在本次删除范围。
        </p>
        <Button
          size="sm"
          disabled={busy || query.isFetching}
          onClick={() => {
            setReview(null);
            void query.refetch();
          }}
        >
          刷新回收历史
        </Button>
      </div>
      {query.isPending && (
        <p role="status" className={hint}>
          正在读取回收资格与历史…
        </p>
      )}
      {(denied || query.error || (query.data && !data)) && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {denied
            ? "当前权限或记录已失效，已隐藏回收历史。"
            : data
              ? "刷新失败，以下保留上次观测；刷新成功前不能执行回收操作。"
              : "无法读取符合当前记录范围的回收历史。"}
        </p>
      )}
      {!denied && error && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      {!denied && message && (
        <p role="status" className={hint}>
          {message}
        </p>
      )}
      {data && (
        <>
          <p className={hint}>
            资格观测于 {date(data.measured_at)} · 单次完整读取上限 {cacheBytes(data.max_bytes)} ·
            协作预算 {data.timeout_seconds} 秒。
          </p>
          {data.blockers.length > 0 && (
            <ul className={`${hint} list-inside list-disc space-y-1`}>
              {data.blockers.map((code) => (
                <li key={code}>{conditionMessage(code)}</li>
              ))}
            </ul>
          )}
          <label className="grid gap-1 text-sm" htmlFor={`${formId}-reason`}>
            回收原因
            <textarea
              id={`${formId}-reason`}
              className="w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
              rows={2}
              minLength={8}
              maxLength={1000}
              disabled={busy}
              value={reason}
              onChange={(event) => {
                setReason(event.target.value);
                setReview(null);
              }}
              aria-describedby={`${formId}-hint`}
            />
          </label>
          <p id={`${formId}-hint`} className={hint}>
            填写至少8个字符的可审计原因，不要包含路径、密码或访问令牌。确认后无法通过本操作恢复已删除版本。
          </p>
          <Button
            size="sm"
            disabled={disabled || reason.trim().length < 8}
            onClick={(event) => {
              previewTrigger.current = event.currentTarget;
              void preview(null);
            }}
          >
            预览准确版本回收
          </Button>
          {!data.can_preview && (
            <p className={hint}>当前不能创建新回收；已有任务和保护要求以服务器重新核对为准。</p>
          )}
          {data.items.length === 0 && (
            <p className={hint}>尚无回收任务，展开和刷新本区不会删除对象。</p>
          )}
          <ul className="divide-y divide-border">
            {data.items.map((item) => {
              const requested = !!item.cancel_requested_at || cancelRequested.has(item.id);
              return (
                <li key={item.id} className="min-w-0 space-y-2 py-3">
                  <h3 className="text-sm font-medium">
                    {item.outcome ? outcomes[item.outcome] : statuses[item.status]}
                  </h3>
                  <p className={hint}>
                    任务 {item.id.slice(0, 8)} · 申请于 {date(item.requested_at)}
                    {item.completed_at ? ` · 结束于 ${date(item.completed_at)}` : ""}
                  </p>
                  <p className={hint}>
                    {item.unsettled_operation ? "实际操作收尾待确认" : phases[item.phase]} ·
                    单个版本 {cacheBytes(item.expected_size_bytes)}
                  </p>
                  <details className={hint}>
                    <summary className="cursor-pointer">
                      准确版本{" "}
                      {item.version_id.length > 32
                        ? `${item.version_id.slice(0, 32)}…`
                        : item.version_id}
                    </summary>
                    <dl className="mt-2 space-y-2">
                      {[
                        ["完整 VersionId", item.version_id],
                        ["创建记录", item.creation_id],
                        ["逻辑位置", item.location_id],
                        ...(item.retry_of_id ? [["上次尝试", item.retry_of_id]] : []),
                      ].map(([label, value]) => (
                        <div key={label}>
                          <dt>{label}</dt>
                          <dd className="mt-1 max-h-24 overflow-y-auto break-all">{value}</dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                  {item.absence_proven && (
                    <p className={hint}>
                      已验证该准确版本缺失；相关断言不会重复累计为多次释放容量。
                    </p>
                  )}
                  {item.delete_started && !item.absence_proven && (
                    <p className="text-xs leading-relaxed text-status-warning">
                      可能已经发出 DELETE，尚未证明准确版本缺失；迟到应答和物理责任需继续保留。
                    </p>
                  )}
                  {item.unsettled_operation && (
                    <p className="text-xs leading-relaxed text-status-danger">
                      实际操作尚未确认收尾，不能视为已经停止，也不能启动新的回收。
                    </p>
                  )}
                  {requested && active(item) && (
                    <p role="status" className={hint}>
                      已请求取消，等待实际操作收尾；当前仍为{statuses[item.status]}。
                    </p>
                  )}
                  {item.error_code && <p className={hint}>{conditionMessage(item.error_code)}</p>}
                  <div className="flex flex-wrap gap-2">
                    {item.status === "pending" &&
                      item.async_job_id &&
                      !requested &&
                      !item.unsettled_operation && (
                        <Button
                          size="sm"
                          disabled={disabled}
                          onClick={() => void run("redispatch", item)}
                        >
                          重新派发回收
                        </Button>
                      )}
                    {active(item) && item.async_job_id && (
                      <Button
                        size="sm"
                        disabled={disabled || requested}
                        onClick={() => void run("cancel", item)}
                      >
                        {requested ? "已请求取消" : "请求取消回收"}
                      </Button>
                    )}
                    {retryable(item) && (
                      <Button
                        size="sm"
                        disabled={disabled || reason.trim().length < 8}
                        onClick={(event) => {
                          previewTrigger.current = event.currentTarget;
                          void preview(item);
                        }}
                      >
                        预览重试此版本
                      </Button>
                    )}
                    {item.async_job_id ? (
                      <Button
                        size="sm"
                        disabled={denied}
                        onClick={(event) => {
                          jobTrigger.current = event.currentTarget;
                          setJobId(item.async_job_id);
                        }}
                      >
                        查看回收任务
                      </Button>
                    ) : (
                      <p className={hint}>后台任务记录已清理，回收责任与历史仍保留。</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          <ListPagination
            label="准确版本回收历史"
            page={page}
            pageSize={pageSize}
            total={data.total}
            hasNext={data.has_more}
            loading={busy || query.isFetching}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPage(0);
              setPageSize(size);
            }}
          />
        </>
      )}
      <Modal
        open={!!review && !denied}
        onClose={() => {
          if (!applying) setReview(null);
        }}
        title={
          <span className="text-md">
            {review?.receipt.eligible ? "确认准确版本回收" : "准确版本回收受阻"}
          </span>
        }
        width={600}
        stopEscapePropagation
        returnFocusRef={previewTrigger}
        initialFocusRef={cancelButton}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button
              ref={cancelButton}
              size="sm"
              disabled={applying}
              onClick={() => setReview(null)}
            >
              返回检查
            </Button>
            {review?.receipt.eligible && (
              <Button
                variant="danger"
                size="sm"
                disabled={disabled || !confirmed || expired || denied}
                onClick={() => void run("start")}
              >
                {applying ? "正在提交…" : "确认不可撤销回收"}
              </Button>
            )}
          </div>
        }
      >
        {review && (
          <div className="space-y-3 text-sm">
            <p>
              {retry
                ? "本次重新检查同一物理版本，创建新的回收尝试；历史责任保持。"
                : "本次仅回收该记录绑定的一个准确版本，不删除同键其他版本。"}
            </p>
            <dl className="grid gap-3 sm:grid-cols-2">
              <div className="min-w-0">
                <dt className={hint}>准确 VersionId</dt>
                <dd className="mt-1 max-h-24 overflow-y-auto break-all">
                  {review.receipt.version_id ?? "未取得可信准确版本"}
                </dd>
              </div>
              <div>
                <dt className={hint}>单个版本大小</dt>
                <dd className="mt-1">
                  {review.receipt.expected_size_bytes === null
                    ? "未知"
                    : cacheBytes(review.receipt.expected_size_bytes)}
                </dd>
              </div>
            </dl>
            <p className={hint}>
              预演于 {date(review.receipt.measured_at)} · 有效至 {date(review.receipt.expires_at)}
            </p>
            {!review.receipt.eligible && (
              <p className="text-xs leading-relaxed text-status-warning">
                当前准确版本不满足回收条件，未创建任务或发出删除。
              </p>
            )}
            {review.receipt.blockers.length > 0 && (
              <ul className={`${hint} list-inside list-disc`}>
                {review.receipt.blockers.map((code) => (
                  <li key={code}>{conditionMessage(code)}</li>
                ))}
              </ul>
            )}
            {review.receipt.eligible && (
              <>
                <p className="text-xs leading-relaxed text-status-danger">
                  删除准确版本不可撤销。已有备份或其他版本不会由本操作自动恢复。
                </p>
                <p className={`${hint} whitespace-pre-wrap break-words`}>
                  回收原因：{review.payload.reason}
                </p>
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={confirmed}
                    disabled={busy}
                    onChange={(event) => setConfirmed(event.target.checked)}
                    className="mt-0.5"
                  />
                  我确认删除上述准确版本，并理解本操作不可撤销。
                </label>
              </>
            )}
            {expired && (
              <p role="alert" className="text-xs text-status-warning">
                预演已过期，请返回重新预览。
              </p>
            )}
          </div>
        )}
      </Modal>
      {jobId && !denied && (
        <AsyncJobDetailModal
          key={jobId}
          jobId={jobId}
          onClose={() => setJobId(null)}
          returnFocusRef={jobTrigger}
        />
      )}
    </div>
  );
}
