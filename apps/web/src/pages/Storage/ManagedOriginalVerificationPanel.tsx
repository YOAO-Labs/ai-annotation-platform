import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { asyncJobsApi } from "@/api/asyncJobs";
import type {
  ManagedOriginalVerificationHistory,
  ManagedOriginalVerificationItem,
} from "@/api/generated/types.gen";
import { managedOriginalVerificationsApi } from "@/api/managedOriginalVerifications";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { Button } from "@/components/ui/Button";
import { ListPagination } from "@/components/ui/ListPagination";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { cacheBytes } from "@/utils/cachePolicy";

const hint = "text-xs leading-relaxed text-muted-foreground";
const statuses = {
  pending: "排队中",
  running: "核验中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
const outcomes = {
  matched: "本次完整匹配",
  missing: "本次未找到原件",
  changed: "本次内容或定位已变化",
  unverifiable: "本次无法核验",
  failed: "本次核验失败",
  cancelled: "本次已取消",
};
const blockers: Record<string, string> = {
  verification_active: "已有核验任务，等待其结束；排队任务可显式重新派发。",
  physical_operation_unsettled: "尚有读取未确认收尾，暂不能启动新的核验。",
  historical_content_unverified: "缺少可信的历史内容摘要或大小，不能据当前字节补造历史核验。",
  platform_identity_unproven: "原件的历史存储身份尚未确认。",
  platform_configuration_changed: "原件定位或平台配置已变化，当前不能核验。",
  verification_size_budget: "原件超过单次完整核验的读取上限。",
  storage_maintenance: "存储正在维护，暂不能启动新的核验。",
  verification_job_unavailable: "活动核验的后台任务记录不可用，暂不能重新派发、取消或打开该任务。",
};
const errors: Record<string, string> = {
  managed_original_object_missing: "本次读取未找到该原件。",
  managed_original_access_denied: "存储拒绝本次读取，不能据此判断原件缺失。",
  managed_original_object_changed: "对象在本次核验中发生变化，未取得一致内容证明。",
  managed_original_size_mismatch: "完整读取的字节数与历史记录不一致。",
  managed_original_sha256_mismatch: "完整读取的 SHA-256 与历史记录不一致。",
  managed_original_probe_timeout: "本次读取达到时间上限；实际收尾状态单独记录。",
  managed_original_byte_budget_exceeded: "本次读取超过字节预算。",
  managed_original_conditions_unsupported: "存储不支持本次核验所需的条件读取。",
  managed_original_invalid_response: "存储未返回可用于完整核验的有效响应。",
  managed_original_redirect_denied: "本次读取遇到重定向，未继续访问其他地址。",
  managed_original_storage_configuration_changed: "读取期间存储配置已变化，本次结果不能适用。",
  managed_original_source_unavailable: "本次无法连接存储或完成对象读取。",
  managed_original_probe_failed: "本次对象读取未能完成核验。",
  managed_original_invalid_input: "历史内容或定位条件不满足核验要求。",
  storage_operation_unsettled: "读取责任尚未确认收尾，不能把本次任务终态视为物理读取结束。",
  verification_authority_changed: "核验期间操作者或访问密钥的权限已变化，未发布内容证明。",
  verification_context_changed: "核验期间原件记录、定位或配置已变化，未发布内容证明。",
  storage_maintenance_changed: "核验期间存储维护状态已变化，未发布内容证明。",
  verification_interrupted: "本次后台执行中断，未取得完整核验结果。",
  verification_timeout: "本次核验达到时间上限，实际收尾状态单独记录。",
  verification_read_failed: "本次对象读取未能完成核验。",
  verification_invalid_result: "本次结果未通过完整性检查，未发布内容证明。",
};

const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const active = (item: ManagedOriginalVerificationItem) =>
  item.status === "pending" || item.status === "running";

function validHistory(data: ManagedOriginalVerificationHistory, id: string) {
  return (
    data.retirement_id === id &&
    data.scope === "registered_retirement_assertion_only" &&
    data.execution_permission === false &&
    typeof data.can_start === "boolean" &&
    Number.isFinite(Date.parse(data.measured_at)) &&
    Number.isSafeInteger(data.total) &&
    data.total >= 0 &&
    Number.isSafeInteger(data.max_bytes) &&
    data.max_bytes > 0 &&
    Number.isFinite(data.timeout_seconds) &&
    data.timeout_seconds > 0 &&
    Array.isArray(data.blockers) &&
    Array.isArray(data.items)
  );
}

function failureMessage(error: unknown) {
  if (storageAccessDenied(error)) return "当前权限或记录已失效，已隐藏核验历史。";
  if (error instanceof ApiError && error.status === 409)
    return "核验状态、原件记录或配置已变化，请刷新历史后重试。";
  return "请求未能确认完成，请刷新核验历史后重试。";
}

function usePageVisible() {
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => {
    const update = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);
  return visible;
}

interface Props {
  retirementId: string;
  onDenied: () => void;
}

export function ManagedOriginalVerificationPanel(props: Props) {
  const revision = useSessionRevision();
  return <VerificationDisclosure key={`${revision}/${props.retirementId}`} {...props} />;
}

function VerificationDisclosure(props: Props) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-border"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">原件完整核验</summary>
      {open && <VerificationOwner {...props} />}
    </details>
  );
}

function VerificationOwner({ retirementId, onDenied }: Props) {
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const visible = usePageVisible();
  const client = useQueryClient();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const requestPending = useRef(false);
  const [actionDenied, setActionDenied] = useState(false);
  const [actionError, setActionError] = useState("");
  const [message, setMessage] = useState("");
  const [cancelRequested, setCancelRequested] = useState<Set<string>>(new Set());
  const [jobId, setJobId] = useState<string | null>(null);
  const jobTrigger = useRef<HTMLElement | null>(null);
  const query = useQuery({
    queryKey: ["managed-original-verifications", session, revision, retirementId, pageSize, page],
    queryFn: ({ signal }) =>
      managedOriginalVerificationsApi.history(retirementId, pageSize, page * pageSize, signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
    refetchInterval: (current) => {
      const data = current.state.data;
      return visible &&
        !actionDenied &&
        !current.state.error &&
        data &&
        validHistory(data, retirementId) &&
        (data.items.some(active) || data.blockers.includes("verification_active"))
        ? 5000
        : false;
    },
  });
  const denied = actionDenied || storageAccessDenied(query.error);
  const data =
    !denied && query.data && validHistory(query.data, retirementId) ? query.data : undefined;
  const disabled = busy || query.isFetching || !!query.error || !data;
  useEffect(() => {
    if (denied) {
      setJobId(null);
      onDenied();
    }
  }, [denied, onDenied]);
  useEffect(() => {
    if (!data || query.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total / pageSize) - 1);
    if (page > last) setPage(last);
  }, [data, page, pageSize, query.isFetching]);
  useEffect(() => {
    if (
      jobId &&
      data?.blockers.includes("verification_job_unavailable") &&
      data.items.some((item) => active(item) && item.async_job_id === jobId)
    )
      setJobId(null);
  }, [data, jobId]);

  const run = async (operation: "start" | "cancel", item?: ManagedOriginalVerificationItem) => {
    if (disabled || requestPending.current) return;
    if (operation === "cancel" && data?.blockers.includes("verification_job_unavailable")) return;
    const current = capture();
    requestPending.current = true;
    setBusy(true);
    setActionError("");
    setMessage("");
    let accepted = false;
    try {
      if (operation === "start") {
        const result = await managedOriginalVerificationsApi.start(retirementId);
        if (!current()) return;
        if (
          result.retirement_id !== retirementId ||
          !result.verification_id ||
          !result.async_job_id
        )
          throw new Error("Verification owner mismatch");
        accepted = true;
        setMessage(
          result.existing
            ? "已复用已有核验请求，请查看历史中的进展。"
            : "核验请求已受理，请查看历史中的进展。",
        );
      } else {
        if (!item?.async_job_id) return;
        const result = await asyncJobsApi.cancel(item.async_job_id);
        if (!current()) return;
        accepted = true;
        if (result.status === "cancel_requested")
          setCancelRequested((previous) => new Set(previous).add(item.id));
        setMessage("取消操作已受理，请以核验历史的实际收尾状态为准。");
      }
      if (!current()) return;
      await Promise.all([
        client.invalidateQueries({
          queryKey: ["managed-original-verifications", session, revision, retirementId],
          refetchType: "none",
        }),
        client.invalidateQueries({ queryKey: ["async-jobs"] }),
      ]);
      if (!current()) return;
      if (operation === "start" && page > 0) setPage(0);
      else await query.refetch({ throwOnError: true });
    } catch (error) {
      if (!current()) return;
      if (storageAccessDenied(error)) {
        setActionDenied(true);
        setMessage("");
      }
      setActionError(
        accepted && !storageAccessDenied(error)
          ? "请求已受理，但刷新核验历史失败，请刷新后继续操作。"
          : failureMessage(error),
      );
      if (!accepted && !storageAccessDenied(error)) {
        const refreshed = await query.refetch();
        if (current() && storageAccessDenied(refreshed.error)) setActionDenied(true);
      }
    } finally {
      if (current()) {
        requestPending.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <div className="min-w-0 space-y-3 border-t border-border p-3 text-sm">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className={hint}>
          核验单条记录的完整字节，保存本次观测。历史结果不证明独占所有权、最小副本或物理删除许可。
        </p>
        <Button size="sm" disabled={busy || query.isFetching} onClick={() => void query.refetch()}>
          刷新核验历史
        </Button>
      </div>
      {query.isPending && (
        <p role="status" className={hint}>
          正在读取核验历史…
        </p>
      )}
      {(denied || query.error || (query.data && !data)) && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {denied
            ? "当前权限或记录已失效，已隐藏核验历史。"
            : data
              ? "刷新失败，以下为上次观测；刷新成功前不能启动、重新派发或取消核验。"
              : "无法读取当前记录的核验历史。"}
        </p>
      )}
      {!denied && actionError && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {actionError}
        </p>
      )}
      {!denied && message && (
        <p role="status" className={hint}>
          {message}
        </p>
      )}
      {data && (
        <>
          <p className={hint}>历史状态观测于 {date(data.measured_at)}</p>
          <p className={hint}>
            单次读取上限 {cacheBytes(data.max_bytes)} · 协作截止 {data.timeout_seconds}{" "}
            秒。取消或超时后仍须等待实际读取收尾。
          </p>
          <Button
            size="sm"
            disabled={disabled || !data.can_start}
            onClick={() => void run("start")}
          >
            {busy ? "正在提交请求…" : "启动完整核验"}
          </Button>
          {page > 0 && data.blockers.includes("verification_active") && (
            <Button size="sm" disabled={disabled} onClick={() => setPage(0)}>
              查看最新核验
            </Button>
          )}
          {data.blockers.length > 0 && (
            <ul className={`${hint} list-inside list-disc space-y-1`}>
              {data.blockers.map((code) => (
                <li key={code}>{blockers[code] ?? "存在尚未核实的启动条件，请刷新后检查。"}</li>
              ))}
            </ul>
          )}
          {data.items.length === 0 && (
            <p className={hint}>尚无核验历史；可启动条件不等于已有现场内容证明。</p>
          )}
          <ul className="divide-y divide-border">
            {data.items.map((item) => {
              const requested = !!item.cancel_requested_at || cancelRequested.has(item.id);
              const jobUnavailable =
                active(item) && data.blockers.includes("verification_job_unavailable");
              const redispatch =
                item.status === "pending" &&
                !!item.async_job_id &&
                !requested &&
                item.context_matches &&
                !item.unsettled_operation &&
                data.blockers.every((code) => code === "verification_active");
              return (
                <li key={item.id} className="min-w-0 space-y-2 py-3">
                  <h3 className="text-sm font-medium">
                    {item.outcome ? outcomes[item.outcome] : statuses[item.status]}
                  </h3>
                  <p className={hint}>
                    核验记录 {item.id.slice(0, 8)} · 申请于 {date(item.requested_at)}
                    {item.started_at ? ` · 开始于 ${date(item.started_at)}` : ""}
                    {item.completed_at ? ` · 观测于 ${date(item.completed_at)}` : ""}
                  </p>
                  {item.outcome === "matched" && (
                    <p className={hint}>
                      仅表示此次完整读取与历史摘要一致；此后同键内容仍可能变化。
                    </p>
                  )}
                  {item.error_code && (
                    <p className={hint}>
                      {errors[item.error_code] ??
                        "本次未取得完整且一致的内容证明，请根据结果检查后重试。"}
                    </p>
                  )}
                  {!item.context_matches && (
                    <p className="text-xs leading-relaxed text-status-warning">
                      锚点内容、定位或配置已变化；本条仅保留为历史观测，不适用于当前记录。
                    </p>
                  )}
                  {requested && active(item) && (
                    <p role="status" className={hint}>
                      已请求取消，等待实际读取收尾；当前仍为{statuses[item.status]}。
                    </p>
                  )}
                  {item.unsettled_operation && (
                    <p className="text-xs leading-relaxed text-status-danger">
                      读取尚未确认收尾，不能视为已经停止；新的核验继续阻断。
                    </p>
                  )}
                  {item.status === "pending" && !requested && (
                    <p className={hint}>
                      任务尚未开始；派发失败不会自动重试，可在条件满足时重新派发同一核验。
                    </p>
                  )}
                  <details className="text-xs">
                    <summary className="cursor-pointer text-muted-foreground">
                      查看摘要与字节数
                    </summary>
                    <dl className="mt-2 grid gap-2 sm:grid-cols-2">
                      <div className="min-w-0">
                        <dt className={hint}>历史 SHA-256</dt>
                        <dd className="mt-1 break-all font-mono">{item.expected_sha256}</dd>
                      </div>
                      <div className="min-w-0">
                        <dt className={hint}>本次 SHA-256</dt>
                        <dd className="mt-1 break-all font-mono">
                          {item.observed_sha256 ?? "未取得完整摘要"}
                        </dd>
                      </div>
                      <div>
                        <dt className={hint}>历史字节数</dt>
                        <dd className="mt-1 tabular-nums">
                          {cacheBytes(item.expected_size_bytes)}
                        </dd>
                      </div>
                      <div>
                        <dt className={hint}>本次字节数</dt>
                        <dd className="mt-1 tabular-nums">
                          {item.observed_size_bytes === null
                            ? "未取得"
                            : cacheBytes(item.observed_size_bytes)}
                        </dd>
                      </div>
                    </dl>
                  </details>
                  <div className="flex flex-wrap gap-2">
                    {redispatch && (
                      <Button size="sm" disabled={disabled} onClick={() => void run("start")}>
                        重新派发核验
                      </Button>
                    )}
                    {active(item) && item.async_job_id && !jobUnavailable && (
                      <Button
                        size="sm"
                        disabled={disabled || requested}
                        onClick={() => void run("cancel", item)}
                      >
                        {requested ? "已请求取消" : "请求取消核验"}
                      </Button>
                    )}
                    {item.async_job_id && !jobUnavailable ? (
                      <Button
                        size="sm"
                        disabled={denied}
                        onClick={(event) => {
                          jobTrigger.current = event.currentTarget;
                          setJobId(item.async_job_id);
                        }}
                      >
                        查看核验任务
                      </Button>
                    ) : !item.async_job_id ? (
                      <p className={hint}>后台任务记录已清理，核验历史仍保留。</p>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
          <ListPagination
            label="原件核验历史"
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
      {jobId && !denied && (
        <AsyncJobDetailModal
          jobId={jobId}
          onClose={() => setJobId(null)}
          returnFocusRef={jobTrigger}
        />
      )}
    </div>
  );
}
