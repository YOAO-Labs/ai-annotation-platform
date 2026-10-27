import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ReferenceReconciliationOut } from "@/api/generated/types.gen";
import { managedOriginalReclaimsApi } from "@/api/managedOriginalReclaims";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { Button } from "@/components/ui/Button";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";

const hint = "text-xs leading-relaxed text-muted-foreground";
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const labels: Record<string, string> = {
  total_assertions: "退休断言",
  physical_groups: "物理对象组",
  protected_groups: "受引用保护",
  retained_groups: "保留中",
  blocked_groups: "待核实或阻断",
  unreferenced_groups: "未发现引用",
};
const unknownLabels: Record<string, string> = {
  unknown_training_versions: "训练快照",
  unknown_active_learning_rounds: "主动学习历史",
  unknown_reference_aliases: "引用别名",
  unknown_operations: "未确认定位的活动读取",
};
const statuses = {
  idle: "尚未核对",
  pending: "核对排队中",
  running: "正在核对",
  completed: "最近一轮已完成",
  failed: "最近一轮失败",
};
const active = (data: ReferenceReconciliationOut) =>
  data.status === "pending" || data.status === "running";
const validDate = (value: string | null | undefined) =>
  value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
const countsValid = (value: Record<string, number> | undefined) =>
  !!value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  Object.values(value).every((count) => Number.isSafeInteger(count) && count >= 0);
function valid(data: ReferenceReconciliationOut): data is Required<ReferenceReconciliationOut> {
  return (
    data &&
    typeof data.status === "string" &&
    Object.prototype.hasOwnProperty.call(statuses, data.status) &&
    validDate(data.measured_at) &&
    validDate(data.last_success_at) &&
    countsValid(data.counts) &&
    countsValid(data.unknown) &&
    Array.isArray(data.deltas) &&
    data.deltas.every(
      (delta) =>
        typeof delta.metric === "string" &&
        Number.isSafeInteger(delta.before) &&
        Number.isSafeInteger(delta.after) &&
        Number.isSafeInteger(delta.change),
    )
  );
}

export function ManagedOriginalReferenceAuditPanel({ onDenied }: { onDenied: () => void }) {
  const revision = useSessionRevision();
  return <AuditDisclosure key={revision} onDenied={onDenied} />;
}

function AuditDisclosure({ onDenied }: { onDenied: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-md border border-border"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">周期引用核对</summary>
      {open && <AuditOwner onDenied={onDenied} />}
    </details>
  );
}

function AuditOwner({ onDenied }: { onDenied: () => void }) {
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => {
    const change = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [actionDenied, setActionDenied] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const jobTrigger = useRef<HTMLElement | null>(null);
  const query = useQuery({
    queryKey: ["managed-original-reference-reconciliation", session, revision],
    queryFn: ({ signal }) => managedOriginalReclaimsApi.reconciliation(signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchIntervalInBackground: false,
    refetchInterval: (current) =>
      visible &&
      !actionDenied &&
      !current.state.error &&
      current.state.data &&
      valid(current.state.data) &&
      active(current.state.data)
        ? 5000
        : false,
  });
  const denied = actionDenied || storageAccessDenied(query.error);
  const data = !denied && query.data && valid(query.data) ? query.data : undefined;
  useEffect(() => {
    if (denied) {
      setJobId(null);
      onDenied();
    }
  }, [denied, onDenied]);
  const run = async () => {
    if (pending.current || busy || query.isFetching || query.error || !data) return;
    const current = capture();
    pending.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    let accepted = false;
    try {
      const result = await managedOriginalReclaimsApi.reconcile();
      if (!current()) return;
      if (!valid(result)) throw new Error("Invalid reconciliation response");
      accepted = true;
      setMessage("引用核对请求已受理；本次只观察数据库关系，不删除对象。");
      await query.refetch({ throwOnError: true });
    } catch (failure) {
      if (!current()) return;
      if (storageAccessDenied(failure)) setActionDenied(true);
      setError(
        storageAccessDenied(failure)
          ? "当前权限已失效，已隐藏引用核对记录。"
          : accepted
            ? "核对请求已受理，但刷新记录失败，请稍后刷新。"
            : "核对请求未能确认完成，请刷新记录后检查。",
      );
    } finally {
      if (current()) {
        pending.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <div className="min-w-0 space-y-3 border-t border-border p-3 text-sm">
      <p className={hint}>
        每日自动核对已登记退休原件的数据库引用与保留关系，不读取或删除对象，不自动回收。完整缓存的周期清理由独立维护任务处理。
      </p>
      {query.isPending && (
        <p role="status" className={hint}>
          正在读取引用核对记录…
        </p>
      )}
      {(denied || query.error || (query.data && !data)) && (
        <p role="alert" className="text-xs leading-relaxed text-status-danger">
          {denied
            ? "当前权限已失效，已隐藏引用核对记录。"
            : data
              ? "刷新失败，以下为上次记录；当前不能启动新的核对。"
              : "无法读取有效的引用核对记录。"}
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
          <p className="font-medium">{statuses[data.status]}</p>
          <p className={hint}>
            最近成功：{data.last_success_at ? date(data.last_success_at) : "尚无成功记录"}
            {data.measured_at ? ` · 计数观测于 ${date(data.measured_at)}` : " · 当前尚无完成观测"}
          </p>
          {data.error_code && (
            <p className="text-xs leading-relaxed text-status-warning">
              最近一轮未完成有效核对；保留的旧结果不代表当前引用已经重新检查。
            </p>
          )}
          {Object.keys(data.counts).length > 0 && (
            <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {Object.entries(labels)
                .filter(([key]) => key in data.counts)
                .map(([key, label]) => (
                  <div key={key}>
                    <dt className={hint}>{label}</dt>
                    <dd className="mt-1 tabular-nums">{data.counts[key]}</dd>
                  </div>
                ))}
            </dl>
          )}
          {Object.entries(unknownLabels).some(([key]) => (data.unknown[key] ?? 0) > 0) && (
            <p className="text-xs leading-relaxed text-status-warning">
              未知覆盖：
              {Object.entries(unknownLabels)
                .filter(([key]) => (data.unknown[key] ?? 0) > 0)
                .map(([key, label]) => `${label} ${data.unknown[key]}`)
                .join(" · ")}
              。未知不能视为零引用。
            </p>
          )}
          <p className={hint}>未发现引用不等于允许删除；回收确认和执行前仍会重新检查全部条件。</p>
          {data.deltas.length > 0 && (
            <details>
              <summary className="cursor-pointer text-xs text-muted-foreground">
                查看与上次成功核对的差异
              </summary>
              <ul className={`${hint} mt-2 space-y-1`}>
                {data.deltas
                  .filter((delta) => typeof delta.metric === "string" && labels[delta.metric])
                  .slice(0, 6)
                  .map((delta) => (
                    <li key={String(delta.metric)}>
                      {labels[String(delta.metric)]}：{delta.before} → {delta.after}（
                      {Number(delta.change) >= 0 ? "+" : ""}
                      {delta.change}）
                    </li>
                  ))}
              </ul>
            </details>
          )}
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={busy || query.isFetching || !!query.error || !data || active(data)}
          onClick={() => void run()}
        >
          立即核对
        </Button>
        {data?.status === "pending" && data.async_job_id && (
          <Button
            size="sm"
            disabled={busy || query.isFetching || !!query.error}
            onClick={() => void run()}
          >
            重新派发核对
          </Button>
        )}
        <Button size="sm" disabled={busy || query.isFetching} onClick={() => void query.refetch()}>
          刷新核对记录
        </Button>
        {data?.async_job_id && (
          <Button
            size="sm"
            onClick={(event) => {
              jobTrigger.current = event.currentTarget;
              setJobId(data.async_job_id);
            }}
          >
            查看核对任务
          </Button>
        )}
      </div>
      {data?.status === "pending" && (
        <p className={hint}>
          核对尚未开始；派发未确认时可显式重新派发同一任务，不创建新的核对轮次。
        </p>
      )}
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
