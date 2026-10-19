import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { apiErrorDetailMessage } from "@/api/client";
import { asyncJobsApi, CANCELLABLE_ASYNC_JOB_KINDS, type AsyncJob } from "@/api/asyncJobs";
import { useAsyncJob } from "@/hooks/useAsyncJob";
import { randomId } from "@/utils/id";

export interface WorkbenchAiRequestSummary {
  readonly projectId: string;
  readonly taskId: string;
  readonly taskLabel?: string;
  readonly frameIndex: number | null;
  readonly backendName: string;
  readonly modelName: string;
  readonly input: Record<string, unknown>;
}

export interface WorkbenchAiRequestPresentation {
  status: "idle" | "running" | "completed" | "error" | "cancelled";
  summary: WorkbenchAiRequestSummary | null;
  progressPct: number | null;
  error: string | null;
  canCancel: boolean;
  cancelling: boolean;
  canRetry: boolean;
  retryCreatesNewRequest?: boolean;
}

export interface WorkbenchAiRequestStart {
  summary: WorkbenchAiRequestSummary;
  execute: (context: {
    signal: AbortSignal;
    isCurrent: () => boolean;
    summary: WorkbenchAiRequestSummary;
    idempotencyKey: string;
  }) => Promise<{ kind: "queued"; celeryTaskId: string } | { kind: "completed" }>;
  /** Local execution supports abort; queued jobs negotiate cancellation after lookup. */
  cancellable: boolean;
}

interface RequestOwner extends WorkbenchAiRequestStart {
  idempotencyKey: string;
  scopeKey: string;
  onCompleted: (summary: WorkbenchAiRequestSummary) => Promise<void>;
  controller: AbortController;
  status: WorkbenchAiRequestPresentation["status"];
  progressPct: number | null;
  error: string | null;
  recovery: "execute" | "monitor" | "refresh" | null;
  celeryTaskId: string | null;
  job: AsyncJob | null;
  lookupInFlight: boolean;
  lookupAttempts: number;
  lookupTimer: ReturnType<typeof setTimeout> | null;
  refreshing: boolean;
  monitorRetrying: boolean;
  cancelling: boolean;
  cancelInFlight: boolean;
}

const LOOKUP_INTERVAL_MS = 1500;
const SLOW_LOOKUP_INTERVAL_MS = 5000;
const LOOKUP_DIAGNOSTIC_AFTER = 5;
const PAGE_SIZE = 200;
const TERMINAL_JOB_STATUSES = new Set(["completed", "failed", "cancelled"]);
const idle = (): WorkbenchAiRequestPresentation => ({
  status: "idle",
  summary: null,
  progressPct: null,
  error: null,
  canCancel: false,
  cancelling: false,
  canRetry: false,
});

function freezeSummary(summary: WorkbenchAiRequestSummary): WorkbenchAiRequestSummary {
  const copy = structuredClone(summary);
  const seen = new WeakSet<object>();
  const freeze = (value: unknown) => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  };
  freeze(copy);
  return copy;
}

function clearLookup(owner: RequestOwner) {
  if (owner.lookupTimer !== null) clearTimeout(owner.lookupTimer);
  owner.lookupTimer = null;
}

function canCancel(owner: RequestOwner): boolean {
  if (owner.status !== "running" || owner.refreshing || owner.cancelling) return false;
  if (!owner.celeryTaskId) return owner.cancellable;
  return Boolean(
    owner.job &&
    CANCELLABLE_ASYNC_JOB_KINDS.has(owner.job.kind) &&
    !TERMINAL_JOB_STATUSES.has(owner.job.status),
  );
}

function presentationOf(owner: RequestOwner): WorkbenchAiRequestPresentation {
  return {
    status: owner.status,
    summary: owner.summary,
    progressPct: owner.progressPct,
    error: owner.error,
    canCancel: canCancel(owner),
    cancelling: owner.cancelling,
    retryCreatesNewRequest: owner.job?.status === "failed" || owner.job?.status === "cancelled",
    canRetry:
      !owner.refreshing &&
      !owner.lookupInFlight &&
      !owner.monitorRetrying &&
      !owner.cancelInFlight &&
      (owner.status === "error" ||
        owner.status === "cancelled" ||
        (owner.status === "running" && owner.recovery === "monitor")),
  };
}

function failureMessage(error: unknown, fallback: string): string {
  return apiErrorDetailMessage(error) || fallback;
}

/** Owns one submitted ordinary prediction, independently of Inspector visibility. */
export function useWorkbenchAiRequest({
  scopeKey,
  onCompleted,
}: {
  scopeKey: string | null;
  onCompleted: (summary: WorkbenchAiRequestSummary) => Promise<void>;
}) {
  const ownerRef = useRef<RequestOwner | null>(null);
  const scopeRef = useRef(scopeKey);
  const mountedRef = useRef(false);
  const [view, setView] = useState<{
    scopeKey: string | null;
    presentation: WorkbenchAiRequestPresentation;
    jobId: string | null;
  }>({ scopeKey, presentation: idle(), jobId: null });

  const owns = useCallback(
    (owner: RequestOwner) =>
      mountedRef.current && ownerRef.current === owner && scopeRef.current === owner.scopeKey,
    [],
  );
  const publish = useCallback(
    (owner: RequestOwner) => {
      if (!owns(owner)) return;
      setView({
        scopeKey: owner.scopeKey,
        presentation: presentationOf(owner),
        jobId: owner.job?.id ?? null,
      });
    },
    [owns],
  );

  useLayoutEffect(() => {
    mountedRef.current = true;
    scopeRef.current = scopeKey;
    setView({ scopeKey, presentation: idle(), jobId: null });
    return () => {
      mountedRef.current = false;
      const previous = ownerRef.current;
      ownerRef.current = null;
      if (!previous) return;
      clearLookup(previous);
      // Retiring this view never sends a server-side cancellation request.
      if (previous.cancellable && !previous.celeryTaskId) previous.controller.abort();
    };
  }, [scopeKey]);

  const complete = useCallback(
    async (owner: RequestOwner) => {
      if (!owns(owner) || owner.status !== "running" || owner.refreshing) return;
      owner.refreshing = true;
      owner.error = null;
      owner.recovery = null;
      owner.cancelling = false;
      clearLookup(owner);
      publish(owner);
      try {
        await owner.onCompleted(owner.summary);
        if (!owns(owner) || owner.status !== "running") return;
        owner.status = "completed";
        owner.progressPct = 100;
        owner.error = null;
        owner.recovery = null;
      } catch (error) {
        if (!owns(owner) || owner.status !== "running") return;
        owner.status = "error";
        owner.error = `预测已完成，刷新候选失败：${failureMessage(error, "请重试刷新")}`;
        owner.recovery = "refresh";
      } finally {
        owner.refreshing = false;
        publish(owner);
      }
    },
    [owns, publish],
  );

  const observeJob = useCallback(
    (owner: RequestOwner, job: AsyncJob) => {
      if (!owns(owner) || owner.status !== "running" || owner.refreshing) return;
      if (
        job.celery_task_id !== owner.celeryTaskId ||
        job.project_id !== owner.summary.projectId ||
        job.kind !== "batch_predict" ||
        (owner.job && job.id !== owner.job.id)
      ) {
        owner.error = "作业响应与本次请求不匹配，请重试查询";
        owner.recovery = "monitor";
        publish(owner);
        return;
      }
      owner.job = job;
      owner.progressPct = Number.isFinite(job.progress_pct)
        ? Math.max(0, Math.min(100, job.progress_pct))
        : null;
      owner.error = null;
      owner.recovery = null;
      if (owner.cancelInFlight) {
        publish(owner);
        return;
      }
      if (job.status === "completed") {
        void complete(owner);
        return;
      }
      if (job.status === "failed") {
        owner.status = "error";
        owner.error = job.error_message || "本次预测失败，请重试";
        owner.recovery = "execute";
      } else if (job.status === "cancelled") {
        owner.status = "cancelled";
      }
      if (TERMINAL_JOB_STATUSES.has(job.status)) owner.cancelling = false;
      publish(owner);
    },
    [complete, owns, publish],
  );

  const lookupJob = useCallback(
    async function lookup(owner: RequestOwner): Promise<void> {
      if (!owns(owner) || owner.status !== "running" || owner.lookupInFlight) return;
      clearLookup(owner);
      owner.lookupInFlight = true;
      owner.lookupAttempts += 1;
      owner.error = null;
      owner.recovery = null;
      publish(owner);
      try {
        let scanTotal: number | null = null;
        for (let offset = 0; ; offset += PAGE_SIZE) {
          const page = await asyncJobsApi.list({
            project_id: owner.summary.projectId,
            kind: "batch_predict",
            limit: PAGE_SIZE,
            offset,
          });
          if (!owns(owner) || owner.status !== "running") return;
          if (!Number.isSafeInteger(page.total) || page.total < 0)
            throw new Error("作业列表分页响应无效");
          // New jobs can arrive during pagination; keep this scan's boundary finite.
          scanTotal ??= page.total;
          const match = page.items.find(
            (job) =>
              job.celery_task_id === owner.celeryTaskId &&
              job.project_id === owner.summary.projectId &&
              job.kind === "batch_predict",
          );
          if (match) {
            observeJob(owner, match);
            return;
          }
          if (page.items.length < PAGE_SIZE || offset + PAGE_SIZE >= scanTotal) break;
        }
        const waitingForJob = owner.lookupAttempts >= LOOKUP_DIAGNOSTIC_AFTER;
        if (waitingForJob) {
          owner.error = "仍在等待本次预测的作业记录，将继续自动查询；也可立即重试查询";
          owner.recovery = "monitor";
        }
        owner.lookupTimer = setTimeout(
          () => void lookup(owner),
          waitingForJob ? SLOW_LOOKUP_INTERVAL_MS : LOOKUP_INTERVAL_MS,
        );
      } catch (error) {
        if (!owns(owner) || owner.status !== "running") return;
        owner.error = `无法查询本次作业：${failureMessage(error, "请重试查询")}`;
        owner.recovery = "monitor";
      } finally {
        owner.lookupInFlight = false;
        publish(owner);
      }
    },
    [observeJob, owns, publish],
  );

  const execute = useCallback(
    async (owner: RequestOwner) => {
      try {
        const response = await owner.execute({
          signal: owner.controller.signal,
          isCurrent: () => owns(owner) && owner.status === "running",
          summary: owner.summary,
          idempotencyKey: owner.idempotencyKey,
        });
        if (!owns(owner) || owner.status !== "running") return;
        if (response.kind === "completed") await complete(owner);
        else {
          if (!response.celeryTaskId) throw new Error("预测响应缺少作业标识");
          owner.celeryTaskId = response.celeryTaskId;
          publish(owner);
          await lookupJob(owner);
        }
      } catch (error) {
        if (!owns(owner) || owner.status !== "running") return;
        owner.status = "error";
        owner.error = failureMessage(error, "本次预测失败，请重试");
        owner.recovery = "execute";
        publish(owner);
      }
    },
    [complete, lookupJob, owns, publish],
  );

  const begin = useCallback(
    (
      request: WorkbenchAiRequestStart,
      completedHandler: (summary: WorkbenchAiRequestSummary) => Promise<void>,
      replayKey?: string,
    ): boolean => {
      if (!scopeKey || scopeRef.current !== scopeKey || !mountedRef.current) return false;
      if (ownerRef.current?.status === "running") return false;
      if (ownerRef.current) clearLookup(ownerRef.current);
      const owner: RequestOwner = {
        ...request,
        idempotencyKey: replayKey ?? randomId(),
        summary: freezeSummary(request.summary),
        scopeKey,
        onCompleted: completedHandler,
        controller: new AbortController(),
        status: "running",
        progressPct: null,
        error: null,
        recovery: null,
        celeryTaskId: null,
        job: null,
        lookupInFlight: false,
        lookupAttempts: 0,
        lookupTimer: null,
        refreshing: false,
        monitorRetrying: false,
        cancelling: false,
        cancelInFlight: false,
      };
      // This ref is claimed before invoking an executor that may await video capture.
      ownerRef.current = owner;
      publish(owner);
      void execute(owner);
      return true;
    },
    [execute, publish, scopeKey],
  );
  const start = useCallback(
    (request: WorkbenchAiRequestStart) => begin(request, onCompleted),
    [begin, onCompleted],
  );

  const jobQuery = useAsyncJob(view.scopeKey === scopeKey ? view.jobId : null, true);
  useEffect(() => {
    const owner = ownerRef.current;
    if (
      !owner ||
      !owns(owner) ||
      owner.status !== "running" ||
      owner.refreshing ||
      owner.job?.id !== view.jobId
    )
      return;
    if (jobQuery.isError) {
      owner.error = `作业状态暂时不可用：${failureMessage(jobQuery.error, "请重试查询")}`;
      owner.recovery = "monitor";
      publish(owner);
    } else if (jobQuery.data && !jobQuery.isFetching) observeJob(owner, jobQuery.data);
  }, [
    jobQuery.data,
    jobQuery.error,
    jobQuery.isError,
    jobQuery.isFetching,
    observeJob,
    owns,
    publish,
    view.jobId,
  ]);

  const retry = useCallback((): boolean => {
    const owner = ownerRef.current;
    if (!owner || owner.scopeKey !== scopeKey || !owns(owner) || !presentationOf(owner).canRetry)
      return false;
    if (owner.recovery === "refresh") {
      owner.status = "running";
      void complete(owner);
    } else if (owner.status === "running") {
      if (owner.job) {
        owner.monitorRetrying = true;
        publish(owner);
        void jobQuery.refetch({ cancelRefetch: false }).finally(() => {
          owner.monitorRetrying = false;
          publish(owner);
        });
      } else {
        owner.lookupAttempts = 0;
        void lookupJob(owner);
      }
    } else {
      return begin(
        { summary: owner.summary, execute: owner.execute, cancellable: owner.cancellable },
        owner.onCompleted,
        owner.job?.status === "failed" || owner.job?.status === "cancelled"
          ? undefined
          : owner.idempotencyKey,
      );
    }
    return true;
  }, [begin, complete, jobQuery, lookupJob, owns, publish, scopeKey]);

  const cancel = useCallback(async () => {
    const owner = ownerRef.current;
    if (!owner || owner.scopeKey !== scopeKey || !owns(owner) || !canCancel(owner)) return;
    if (!owner.celeryTaskId) {
      owner.status = "cancelled";
      owner.error = null;
      owner.recovery = null;
      owner.controller.abort();
      publish(owner);
      return;
    }
    const job = owner.job;
    if (!job) return;
    owner.cancelling = true;
    owner.cancelInFlight = true;
    owner.error = null;
    owner.recovery = null;
    publish(owner);
    try {
      const response = await asyncJobsApi.cancel(job.id);
      if (!owns(owner) || owner.status !== "running") return;
      if (response.id !== job.id) throw new Error("取消响应与本次作业不匹配");
      owner.cancelInFlight = false;
      if (response.status === "cancelled") {
        observeJob(owner, { ...job, status: "cancelled" });
      } else if (owner.job && TERMINAL_JOB_STATUSES.has(owner.job.status)) {
        observeJob(owner, owner.job);
      }
    } catch (error) {
      if (!owns(owner) || owner.status !== "running") return;
      owner.cancelling = false;
      owner.cancelInFlight = false;
      owner.error = `取消请求失败：${failureMessage(error, "可重试取消或查询状态")}`;
      owner.recovery = "monitor";
      if (owner.job && TERMINAL_JOB_STATUSES.has(owner.job.status)) observeJob(owner, owner.job);
    } finally {
      owner.cancelInFlight = false;
      publish(owner);
    }
  }, [observeJob, owns, publish, scopeKey]);

  return {
    presentation: view.scopeKey === scopeKey ? view.presentation : idle(),
    start,
    retry,
    cancel,
  };
}
