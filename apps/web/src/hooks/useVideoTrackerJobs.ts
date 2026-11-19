import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useQueryClient } from "@tanstack/react-query";

import {
  videoTrackerApi,
  type VideoTrackerJob,
  type VideoTrackerDecisionPayload,
  type VideoTrackerJobPreview,
  type VideoTrackerJobStatus,
} from "@/api/videoTracker";
import { ApiError } from "@/api/client";
import { useToastStore } from "@/components/ui";
import { buildWsUrl } from "@/lib/wsHost";
import { useAuthStore } from "@/stores/authStore";
import {
  projectTrackerReview,
  reviewInstanceIds,
  type TrackerReviewProjection,
  type TrackerReviewScope,
} from "./videoTrackerReviewScope";

const REMOVE_AFTER_DONE_MS = 1500;
const POLL_AFTER_DISCONNECT_MS = 2000;
const SOCKET_CONNECT_TIMEOUT_MS = 5000;
const MAX_POLL_FAILURES = 6;
const MAX_POLL_DELAY_MS = 30000;

export interface VideoTrackerJobState {
  jobId: string;
  taskId: string;
  // 无源检测 job 无 annotationId (画布级发起)。
  annotationId: string | null;
  jobKind: "tracking" | "correction";
  correctionFrame?: number | null;
  status: VideoTrackerJobStatus;
  revision?: number;
  fromFrame: number;
  toFrame: number;
  windowProgress?: { current: number; total: number };
  errorMessage?: string | null;
  modelKey: string;
  receivedAt: number;
}

export type TrackerReviewDecision = (
  | {
      instance_ids: string[];
      from_frame: number;
      to_frame: number;
      qc_issue_id?: never;
      candidate_digest?: never;
    }
  | {
      qc_issue_id: string;
      candidate_digest: string;
      instance_ids?: never;
      from_frame?: never;
      to_frame?: never;
    }
) & {
  decision: "accept" | "reject";
  override_manual?: boolean;
};

export interface TrackerReviewDecisionOutcome {
  ok: boolean;
  reason?: string;
}

// 候选/接受流: 追踪完成 (pending_review) 拉出的暂存预览, 供画布叠加 + 接受/丢弃。
export interface TrackerStoreState {
  jobs: Record<string, VideoTrackerJobState>;
  candidates: Record<string, VideoTrackerJobPreview>;
  submitting: Record<string, boolean>;
  activeReviewJobId: string | null;
  reviewScopes: Record<string, TrackerReviewScope>;
  activeReview: TrackerReviewProjection | null;
}

type Listener = (state: TrackerStoreState) => void;
interface JobOwner {
  taskId: string;
  taskEpoch: number;
  jobId: string;
  generation: number;
}

export class TrackerJobStore {
  private jobs: Record<string, VideoTrackerJobState> = {};
  private candidates: Record<string, VideoTrackerJobPreview> = {};
  private submitting: Record<string, boolean> = {};
  private activeReviewJobId: string | null = null;
  private reviewScopes: Record<string, TrackerReviewScope> = {};
  private intentRevision = 0;
  private taskEpoch = 0;
  private requestSequence = 0;
  private readRequests = new Map<string, number>();
  private listeners = new Set<Listener>();
  private sockets = new Map<string, WebSocket>();
  private removeTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private pollTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private pollFailures = new Map<string, number>();
  private jobGenerations = new Map<string, number>();
  private hydrationTasks = new Map<string, Promise<void>>();
  // 当前工作台聚焦的 task。切任务时用它把不属于当前 task 的 job/candidate/socket/timer
  // 清掉 (见 scopeToTask), 并作为异步恢复的护栏 (恢复回来发现已切走就丢弃)。
  private currentTaskId: string | null = null;
  // restoreReviewable 已成功拉取过 reviewable/active 数据的 task id。切任务时清空 (见
  // restoreReviewable 顶部); 同一 task 内重复调用 (例如 token 从 null 变为有值后 effect 重跑)
  // 靠它跳过重复拉数据, 只补连尚未连接的 socket。
  private hydratedTaskId: string | null = null;
  private invalidateAnnotations: (taskId: string) => void = () => {};

  setAnnotationInvalidator(fn: (taskId: string) => void): void {
    this.invalidateAnnotations = fn;
  }

  private snapshot(): TrackerStoreState {
    return {
      jobs: this.jobs,
      candidates: this.candidates,
      submitting: this.submitting,
      activeReviewJobId: this.activeReviewJobId,
      reviewScopes: this.reviewScopes,
      activeReview: this.activeReview(),
    };
  }

  private activeReview(): TrackerReviewProjection | null {
    const jobId = this.activeReviewJobId;
    const preview = jobId ? this.candidates[jobId] : undefined;
    const scope = jobId ? this.reviewScopes[jobId] : undefined;
    return preview && scope ? projectTrackerReview(preview, scope, this.taskEpoch) : null;
  }

  private reconcileReviewScopes(): void {
    const scopes: Record<string, TrackerReviewScope> = {};
    for (const jobId of Object.keys(this.candidates).sort()) {
      if (!this.jobs[jobId]) continue;
      const preview = this.candidates[jobId];
      const available = reviewInstanceIds(preview);
      const previous = this.reviewScopes[jobId];
      if (previous) {
        const instanceIds = previous.instanceIds.filter((id) => available.includes(id));
        scopes[jobId] =
          instanceIds.length === previous.instanceIds.length
            ? previous
            : { ...previous, instanceIds, intentRevision: ++this.intentRevision };
      } else {
        const frames = preview.results.map((result) => result.frame_index);
        scopes[jobId] = {
          instanceIds: available,
          fromFrame: Math.min(...frames),
          toFrame: Math.max(...frames),
          intentRevision: ++this.intentRevision,
        };
      }
    }
    this.reviewScopes = scopes;
    if (!this.activeReviewJobId || !scopes[this.activeReviewJobId]) {
      this.activeReviewJobId = Object.keys(scopes).sort()[0] ?? null;
      if (this.activeReviewJobId) {
        scopes[this.activeReviewJobId] = {
          ...scopes[this.activeReviewJobId],
          intentRevision: ++this.intentRevision,
        };
      }
    }
  }

  chooseReviewJob(jobId: string): void {
    const scope = this.reviewScopes[jobId];
    if (!scope || this.activeReviewJobId === jobId) return;
    this.activeReviewJobId = jobId;
    this.reviewScopes = {
      ...this.reviewScopes,
      [jobId]: { ...scope, intentRevision: ++this.intentRevision },
    };
    this.emit();
  }

  setReviewInstances(instanceIds: string[]): void {
    const review = this.activeReview();
    if (!review) return;
    const selected = [...new Set(instanceIds)]
      .filter((id) => review.availableInstanceIds.includes(id))
      .sort();
    if (JSON.stringify(selected) === JSON.stringify(review.scope.instanceIds)) return;
    this.reviewScopes = {
      ...this.reviewScopes,
      [review.jobId]: {
        ...review.scope,
        instanceIds: selected,
        intentRevision: ++this.intentRevision,
      },
    };
    this.emit();
  }

  setReviewWindow(fromFrame: number, toFrame: number): void {
    const review = this.activeReview();
    if (!review || !Number.isFinite(fromFrame) || !Number.isFinite(toFrame)) return;
    const job = this.jobs[review.jobId];
    if (!job) return;
    const clamp = (frame: number) =>
      Math.min(job.toFrame, Math.max(job.fromFrame, Math.trunc(frame)));
    const from = clamp(fromFrame);
    const to = clamp(toFrame);
    if (review.scope.fromFrame === from && review.scope.toFrame === to) return;
    this.reviewScopes = {
      ...this.reviewScopes,
      [review.jobId]: {
        ...review.scope,
        fromFrame: from,
        toFrame: to,
        intentRevision: ++this.intentRevision,
      },
    };
    this.emit();
  }

  isReviewIntentCurrent(intentKey: string): boolean {
    return this.activeReview()?.intentKey === intentKey;
  }

  captureTaskEpoch(taskId: string): number | null {
    return this.currentTaskId === taskId ? this.taskEpoch : null;
  }

  isTaskEpochCurrent(taskId: string, epoch: number | null): boolean {
    return epoch !== null && this.currentTaskId === taskId && this.taskEpoch === epoch;
  }

  private owner(jobId: string): JobOwner | null {
    const job = this.jobs[jobId];
    return job && this.currentTaskId === job.taskId
      ? {
          taskId: job.taskId,
          taskEpoch: this.taskEpoch,
          jobId,
          generation: this.jobGenerations.get(jobId) ?? 0,
        }
      : null;
  }

  private owns(owner: JobOwner): boolean {
    return (
      this.isTaskEpochCurrent(owner.taskId, owner.taskEpoch) &&
      this.jobs[owner.jobId]?.taskId === owner.taskId &&
      (this.jobGenerations.get(owner.jobId) ?? 0) === owner.generation
    );
  }

  private beginRead(jobId: string): { owner: JobOwner; request: number } | null {
    const owner = this.owner(jobId);
    if (!owner || this.removeTimers.has(jobId)) return null;
    const request = ++this.requestSequence;
    this.readRequests.set(jobId, request);
    return { owner, request };
  }

  private ownsRead(read: { owner: JobOwner; request: number }): boolean {
    return this.owns(read.owner) && this.readRequests.get(read.owner.jobId) === read.request;
  }

  private currentRevision(jobId: string): number {
    return Math.max(this.jobs[jobId]?.revision ?? 1, this.candidates[jobId]?.job_revision ?? 1);
  }

  private admitPreview(jobId: string, preview: VideoTrackerJobPreview): boolean {
    const revision = preview.job_revision ?? 1;
    if (revision < this.currentRevision(jobId)) return false;
    if (preview.results.length > 0) this.candidates = { ...this.candidates, [jobId]: preview };
    else {
      const { [jobId]: _drop, ...rest } = this.candidates;
      this.candidates = rest;
    }
    return true;
  }

  private beginMutation(jobId: string): JobOwner | null {
    if (!this.owner(jobId) || this.submitting[jobId] || this.removeTimers.has(jobId)) return null;
    this.bumpGeneration(jobId);
    this.resetPoll(jobId);
    this.setSubmitting(jobId, true);
    return this.owner(jobId);
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    return () => {
      this.listeners.delete(listener);
    };
  }

  private emit(): void {
    this.reconcileReviewScopes();
    const s = this.snapshot();
    for (const fn of this.listeners) fn(s);
  }

  addJob(job: VideoTrackerJob, token?: string | null): void {
    if (this.currentTaskId !== job.task_id) this.scopeToTask(job.task_id);
    this.jobGenerations.set(job.id, (this.jobGenerations.get(job.id) ?? 0) + 1);
    this.pollFailures.delete(job.id);
    this.jobs = { ...this.jobs, [job.id]: toJobState(job) };
    this.emit();
    useToastStore.getState().push({
      msg: job.job_kind === "correction" ? "Mask 纠错传播已开始" : "AI 追踪已开始",
      sub: `${job.model_key} · F${job.from_frame}-F${job.to_frame}`,
      kind: "",
    });
    if (token) this.connect(job.id, token);
    else this.schedulePoll(job.id);
  }

  /**
   * 从服务端恢复刷新前的会话态: 仍待审阅的候选 + 仍在运行的追踪任务 (重连 WS)。
   * 单例 store 跨 task 复用, 切 task 时先把上一个 task 的残留清干净, 再按 task 去重恢复。
   */
  restoreReviewable(taskId: string, token?: string | null): Promise<void> {
    // 切 task: 先 scope 清理 (关旧 socket / 清旧 timer), 避免旧任务的完成 Toast 或候选
    // 借同名 annotation 浮到新任务上。
    if (this.currentTaskId !== taskId) {
      this.scopeToTask(taskId);
    }
    const epoch = this.taskEpoch;
    const hydrationKey = `${epoch}:${taskId}`;
    const pending = this.hydrationTasks.get(hydrationKey);
    if (pending) {
      // 拉取仍在飞行中: 待其结束后再补连 (届时 token 若已就绪, connectActiveJobs 是幂等的)。
      if (token)
        void pending.then(() => {
          if (!this.isTaskEpochCurrent(taskId, epoch)) return;
          if (this.hydratedTaskId === taskId) this.connectActiveJobs(taskId, token);
          else void this.restoreReviewable(taskId, token);
        });
      return pending;
    }
    if (this.hydratedTaskId === taskId) {
      // 本 task 已经拉取过一轮: 只是 token 从 null 变为有值 (如刷新后 auth store 延迟 hydrate)。
      // 不重新拉数据 (避免抖动), 只对 jobs 里仍是 queued/running 但还没连上 socket 的补连。
      if (token) this.connectActiveJobs(taskId, token);
      return Promise.resolve();
    }
    const hydration = this.loadReviewable(taskId, epoch, token)
      .then((loaded) => {
        if (loaded && this.isTaskEpochCurrent(taskId, epoch)) this.hydratedTaskId = taskId;
      })
      .finally(() => {
        this.hydrationTasks.delete(hydrationKey);
      });
    this.hydrationTasks.set(hydrationKey, hydration);
    return hydration;
  }

  /** 关闭并清掉不属于 taskId 的 job/candidate/submitting/socket/timer。同 task 的活跃 job 保留。 */
  scopeToTask(taskId: string | null): void {
    if (this.currentTaskId === taskId) return;
    this.currentTaskId = taskId;
    this.taskEpoch += 1;
    this.hydratedTaskId = null;
    const keptJobs: Record<string, VideoTrackerJobState> = {};
    for (const [jobId, job] of Object.entries(this.jobs)) {
      if (job.taskId === taskId) keptJobs[jobId] = job;
    }
    // candidates / submitting 以 jobId 为键, 其归属经 jobs[jobId].taskId 反查 —— 只保留仍在 keptJobs 里的。
    const keptCandidates: Record<string, VideoTrackerJobPreview> = {};
    for (const [jobId, preview] of Object.entries(this.candidates)) {
      if (keptJobs[jobId]) keptCandidates[jobId] = preview;
    }
    const keptSubmitting: Record<string, boolean> = {};
    for (const [jobId, on] of Object.entries(this.submitting)) {
      if (keptJobs[jobId]) keptSubmitting[jobId] = on;
    }
    // 关掉 / 清掉不再保留的 job 对应的 socket 与 timer, 避免泄漏与旧任务的迟到消息。
    for (const [jobId, socket] of this.sockets) {
      if (!keptJobs[jobId]) {
        try {
          socket.close();
        } catch {
          /* noop */
        }
        this.sockets.delete(jobId);
      }
    }
    for (const [jobId, timer] of this.removeTimers) {
      if (!keptJobs[jobId]) {
        clearTimeout(timer);
        this.removeTimers.delete(jobId);
      }
    }
    for (const [jobId, timer] of this.pollTimers) {
      if (!keptJobs[jobId]) {
        clearTimeout(timer);
        this.pollTimers.delete(jobId);
      }
    }
    for (const jobId of this.jobGenerations.keys()) {
      if (!keptJobs[jobId]) {
        this.jobGenerations.delete(jobId);
        this.pollFailures.delete(jobId);
        this.readRequests.delete(jobId);
      }
    }
    const changed =
      Object.keys(keptJobs).length !== Object.keys(this.jobs).length ||
      Object.keys(keptCandidates).length !== Object.keys(this.candidates).length ||
      Object.keys(keptSubmitting).length !== Object.keys(this.submitting).length;
    if (!changed) return;
    this.jobs = keptJobs;
    this.candidates = keptCandidates;
    this.submitting = keptSubmitting;
    this.emit();
  }

  private async loadReviewable(
    taskId: string,
    epoch: number,
    token?: string | null,
  ): Promise<boolean> {
    const generations = new Map(this.jobGenerations);
    const reads = new Map(this.readRequests);
    let listingsLoaded = true;
    // 候选 (pending_review / cancelled+staged) 与运行中 (queued/running) 任务分别拉取;
    // 任一失败都不阻断另一路。
    let reviewable: VideoTrackerJob[] = [];
    try {
      reviewable = (await videoTrackerApi.reviewable(taskId)) ?? [];
    } catch {
      reviewable = [];
      listingsLoaded = false;
    }
    let active: VideoTrackerJob[] = [];
    try {
      active = (await videoTrackerApi.active(taskId)) ?? [];
    } catch {
      active = [];
      listingsLoaded = false;
    }
    const restored = await Promise.all(
      reviewable.map(async (job) => {
        try {
          const preview = await videoTrackerApi.preview(job.id);
          return { job, preview: preview.results?.length ? preview : null };
        } catch {
          return { job, preview: null };
        }
      }),
    );
    // 护栏: 恢复期间用户已切走 task → 丢弃这批结果, 别把旧任务塞回来 (scopeToTask 会用当前 task 兜底)。
    if (!this.isTaskEpochCurrent(taskId, epoch)) return false;
    const canRestore = (job: VideoTrackerJob) =>
      job.task_id === taskId &&
      !this.removeTimers.has(job.id) &&
      (this.jobGenerations.get(job.id) ?? 0) === (generations.get(job.id) ?? 0) &&
      this.readRequests.get(job.id) === reads.get(job.id);
    for (const entry of restored) {
      if (!entry || !canRestore(entry.job)) continue;
      const { job, preview } = entry;
      this.jobGenerations.set(job.id, this.jobGenerations.get(job.id) ?? 0);
      if ((job.revision ?? 1) >= this.currentRevision(job.id)) {
        this.jobs = { ...this.jobs, [job.id]: toJobState(job) };
      }
      if (preview) this.admitPreview(job.id, preview);
    }
    // 运行中任务: 恢复到 UI 并重连 WS, 让刷新后仍能收进度 / 完成时冒候选。
    for (const job of active) {
      if (canRestore(job) && !this.jobs[job.id]) {
        this.jobGenerations.set(job.id, this.jobGenerations.get(job.id) ?? 0);
        this.jobs = { ...this.jobs, [job.id]: toJobState(job) };
      }
    }
    this.emit();
    for (const entry of restored) {
      if (entry && !entry.preview) this.schedulePoll(entry.job.id);
    }
    if (token) {
      for (const job of active) this.connect(job.id, token);
    } else {
      for (const job of active) this.schedulePoll(job.id);
    }
    return listingsLoaded;
  }

  private clearPoll(jobId: string): void {
    const timer = this.pollTimers.get(jobId);
    if (timer) clearTimeout(timer);
    this.pollTimers.delete(jobId);
  }

  private resetPoll(jobId: string): void {
    this.clearPoll(jobId);
    this.pollFailures.delete(jobId);
  }

  private bumpGeneration(jobId: string): number {
    const next = (this.jobGenerations.get(jobId) ?? 0) + 1;
    this.jobGenerations.set(jobId, next);
    return next;
  }

  private schedulePollFailure(jobId: string): void {
    const failures = (this.pollFailures.get(jobId) ?? 0) + 1;
    this.pollFailures.set(jobId, failures);
    if (failures > MAX_POLL_FAILURES) {
      useToastStore.getState().push({
        msg: "AI 追踪状态自动恢复已暂停",
        sub: "作业仍已保留，请手动刷新后重试",
        kind: "warning",
      });
      return;
    }
    const delay = Math.min(
      POLL_AFTER_DISCONNECT_MS * 2 ** Math.max(0, failures - 1),
      MAX_POLL_DELAY_MS,
    );
    this.schedulePoll(jobId, delay);
  }

  private schedulePoll(jobId: string, delay = POLL_AFTER_DISCONNECT_MS): void {
    if (this.pollTimers.has(jobId)) return;
    const current = this.jobs[jobId];
    if (
      !current ||
      !["queued", "running", "pending_review", "partially_reviewed"].includes(current.status)
    ) {
      return;
    }
    this.pollTimers.set(
      jobId,
      setTimeout(() => {
        this.pollTimers.delete(jobId);
        void this.pollJob(jobId);
      }, delay),
    );
  }

  private async pollJob(jobId: string): Promise<void> {
    const current = this.jobs[jobId];
    if (!current || this.submitting[jobId]) return;
    const read = this.beginRead(jobId);
    if (!read) return;
    try {
      const job = await videoTrackerApi.get(jobId);
      if (!this.ownsRead(read) || (job.revision ?? 1) < this.currentRevision(jobId)) return;
      this.jobs = { ...this.jobs, [jobId]: toJobState(job) };
      this.emit();
      if (job.status === "queued" || job.status === "running") {
        this.pollFailures.delete(jobId);
        this.schedulePoll(jobId);
      } else if (job.status === "pending_review" || job.status === "partially_reviewed") {
        await this.enterReview(jobId);
      } else if (job.status === "cancelled" && current.jobKind !== "correction") {
        await this.enterReview(jobId);
      } else {
        this.scheduleTerminalCleanup(jobId);
      }
    } catch {
      if (this.ownsRead(read)) {
        this.schedulePollFailure(jobId);
      }
    }
  }

  private connect(jobId: string, token: string): void {
    const owner = this.owner(jobId);
    if (!owner || this.removeTimers.has(jobId) || this.sockets.has(jobId)) return;
    const url = buildWsUrl(`/ws/video-tracker-jobs/${jobId}`, { token });
    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch {
      this.schedulePoll(jobId);
      return;
    }
    this.sockets.set(jobId, socket);
    const ownsSocket = () =>
      this.isTaskEpochCurrent(owner.taskId, owner.taskEpoch) &&
      this.sockets.get(jobId) === socket &&
      !!this.jobs[jobId] &&
      !this.removeTimers.has(jobId);
    this.schedulePoll(jobId, SOCKET_CONNECT_TIMEOUT_MS);
    socket.onopen = () => {
      if (ownsSocket()) this.resetPoll(jobId);
    };
    socket.onmessage = (evt) => {
      if (ownsSocket() && !this.submitting[jobId]) this.handleMessage(jobId, evt);
    };
    socket.onclose = () => {
      if (!ownsSocket()) return;
      this.sockets.delete(jobId);
      this.clearPoll(jobId);
      this.schedulePoll(jobId);
    };
    socket.onerror = () => {
      try {
        socket.close();
      } catch {
        /* noop */
      }
    };
  }

  /** 对 taskId 下仍是 queued/running 的 job 补连 socket; connect() 内部按 jobId 判重, 已连的会跳过。 */
  private connectActiveJobs(taskId: string, token: string): void {
    for (const job of Object.values(this.jobs)) {
      if (job.taskId === taskId && (job.status === "queued" || job.status === "running")) {
        this.connect(job.jobId, token);
      }
    }
  }

  private handleMessage(jobId: string, evt: MessageEvent): void {
    let payload: {
      type?: string;
      status?: VideoTrackerJobStatus;
      error_message?: string;
      current?: number;
      total?: number;
    } | null = null;
    try {
      const data = JSON.parse(evt.data);
      if (data?.type === "ping") return;
      payload = data;
    } catch {
      return;
    }
    if (!payload) return;
    const cur = this.jobs[jobId];
    if (!cur) return;
    const status: VideoTrackerJobStatus =
      payload.status ?? mapEventToStatus(payload.type, cur.status);
    const next: VideoTrackerJobState = {
      ...cur,
      status,
      errorMessage: payload.error_message ?? cur.errorMessage,
      receivedAt: Date.now(),
    };
    if (typeof payload.current === "number" && typeof payload.total === "number") {
      next.windowProgress = { current: payload.current, total: payload.total };
    }
    this.jobs = { ...this.jobs, [jobId]: next };
    this.emit();

    const range = `${cur.modelKey} · F${cur.fromFrame}-F${cur.toFrame}`;
    // 候选流: 完成/取消 = 结果暂存待审, 拉候选预览进候选态 (接受才落库), 不直接
    // invalidate; 失败无候选、直接清理。
    if (payload.type === "job_completed") {
      this.resetPoll(jobId);
      useToastStore.getState().push({ msg: "AI 追踪完成, 待接受", sub: range, kind: "success" });
      void this.enterReview(jobId);
    } else if (payload.type === "job_cancelled") {
      this.resetPoll(jobId);
      if (cur.jobKind === "correction") {
        const { [jobId]: _dropCandidate, ...restCandidates } = this.candidates;
        this.candidates = restCandidates;
        this.emit();
        useToastStore.getState().push({
          msg: "Mask 纠错传播已取消",
          sub: "人工纠错帧已保留，候选已清除",
          kind: "warning",
        });
        this.scheduleTerminalCleanup(jobId);
      } else {
        useToastStore
          .getState()
          .push({ msg: "AI 追踪已取消 (部分结果待审)", sub: range, kind: "warning" });
        void this.enterReview(jobId);
      }
    } else if (payload.type === "job_failed") {
      this.resetPoll(jobId);
      useToastStore.getState().push({
        msg: "AI 追踪失败",
        sub: payload.error_message ?? cur.errorMessage ?? range,
        kind: "error",
      });
      this.scheduleTerminalCleanup(jobId);
    }
  }

  /** 拉候选预览; 有暂存结果则进候选态 (等用户接受/丢弃), 无结果直接清理。 */
  private async enterReview(jobId: string): Promise<void> {
    const read = this.beginRead(jobId);
    if (!read) return;
    try {
      const preview = await videoTrackerApi.preview(jobId);
      if (!this.ownsRead(read) || !this.admitPreview(jobId, preview)) return;
      if (!preview.results || preview.results.length === 0) {
        this.scheduleTerminalCleanup(jobId);
        return;
      }
      this.pollFailures.delete(jobId);
      this.emit();
    } catch {
      if (!this.ownsRead(read)) return;
      useToastStore.getState().push({
        msg: "候选预览暂时不可用",
        sub: "作业已保留，可稍后刷新重试",
        kind: "warning",
      });
      this.schedulePollFailure(jobId);
    }
  }

  private setSubmitting(jobId: string, on: boolean): void {
    if (on) {
      this.submitting = { ...this.submitting, [jobId]: true };
    } else {
      const { [jobId]: _drop, ...rest } = this.submitting;
      this.submitting = rest;
    }
    this.emit();
  }

  /** 接受/丢弃失败: 4xx (如 409 状态冲突) 全局拦截器不弹 toast, 这里显式提示并保留审阅条。 */
  private pushActionError(action: string, err: unknown): void {
    const status = err instanceof ApiError ? err.status : undefined;
    const detail =
      status === 409
        ? "候选状态已变化 (可能已被处理, 或源标注被删)"
        : err instanceof Error && err.message
          ? err.message
          : "请重试";
    useToastStore.getState().push({
      msg: `${action} AI 追踪候选失败: ${detail}`,
      sub: status ? `HTTP ${status}` : undefined,
      kind: "error",
    });
  }

  async refreshReview(jobId: string): Promise<void> {
    const read = this.beginRead(jobId);
    if (!read) return;
    try {
      const [job, preview] = await Promise.all([
        videoTrackerApi.get(jobId),
        videoTrackerApi.preview(jobId),
      ]);
      if (!this.ownsRead(read)) return;
      const jobIsCurrent = (job.revision ?? 1) >= this.currentRevision(jobId);
      if (jobIsCurrent) {
        this.jobs = { ...this.jobs, [jobId]: toJobState(job) };
      }
      this.admitPreview(jobId, preview);
      if (jobIsCurrent && ["accepted", "discarded", "failed"].includes(job.status)) {
        this.finishReview(jobId, job.status);
        return;
      }
      this.emit();
    } catch {
      if (!this.ownsRead(read)) return;
      useToastStore.getState().push({
        msg: "刷新 AI 追踪候选失败",
        sub: "当前选择已保留，请稍后重试",
        kind: "error",
      });
    }
  }

  async decide(
    jobId: string,
    selection: TrackerReviewDecision,
  ): Promise<TrackerReviewDecisionOutcome> {
    const current = this.jobs[jobId];
    const preview = this.candidates[jobId];
    if (!current || !preview) return { ok: false, reason: "candidate_missing" };
    if (
      selection.instance_ids &&
      (!Number.isInteger(selection.from_frame) ||
        !Number.isInteger(selection.to_frame) ||
        !preview.results.some(
          (result) =>
            selection.instance_ids!.includes(result.instance_id ?? "1") &&
            result.frame_index >= selection.from_frame! &&
            result.frame_index <= selection.to_frame!,
        ))
    ) {
      return { ok: false, reason: "empty_selection" };
    }
    const owner = this.beginMutation(jobId);
    if (!owner) return { ok: false, reason: "request_in_progress" };
    const payload: VideoTrackerDecisionPayload = {
      ...selection,
      expected_source_versions: { ...preview.expected_source_versions },
      job_revision: preview.job_revision ?? current.revision ?? 1,
    };
    try {
      let updated: VideoTrackerJob;
      try {
        updated = await videoTrackerApi.decide(jobId, payload);
      } catch (err) {
        if (!this.owns(owner)) return { ok: false, reason: "stale_request" };
        const detail =
          err instanceof ApiError && err.detailRaw && typeof err.detailRaw === "object"
            ? (err.detailRaw as { reason?: string })
            : undefined;
        const reason = detail?.reason;
        if (reason === "manual_keyframe_protected") return { ok: false, reason };
        if (
          reason === "job_revision_conflict" ||
          reason === "source_version_conflict" ||
          reason === "candidate_decision_conflict"
        ) {
          await this.refreshReview(jobId);
          if (!this.owns(owner)) return { ok: false, reason: "stale_request" };
          useToastStore.getState().push({
            msg: "追踪候选已发生变化",
            sub: "已刷新最新版本，请重新确认选区",
            kind: "warning",
          });
          return { ok: false, reason };
        }
        this.pushActionError(selection.decision === "accept" ? "接受" : "拒绝", err);
        return { ok: false, reason };
      }
      if (!this.owns(owner)) return { ok: false, reason: "stale_request" };
      if (selection.decision === "accept") this.invalidateAnnotations(current.taskId);
      // A concurrent review refresh may already contain a later persisted decision.
      if ((updated.revision ?? 1) < this.currentRevision(jobId)) return { ok: true };
      if (updated.status === "partially_reviewed") {
        this.jobs = { ...this.jobs, [jobId]: toJobState(updated) };
        await this.refreshReview(jobId);
        if (!this.owns(owner)) return { ok: false, reason: "stale_request" };
        useToastStore.getState().push({
          msg: selection.decision === "accept" ? "已接受所选追踪候选" : "已拒绝所选追踪候选",
          sub: "仍有候选待审阅",
          kind: "success",
        });
      } else {
        useToastStore.getState().push({
          msg: updated.status === "accepted" ? "追踪候选审阅完成" : "已丢弃全部追踪候选",
          kind: "success",
        });
        this.finishReview(jobId, updated.status);
      }
      return { ok: true };
    } finally {
      if (this.owns(owner)) this.setSubmitting(jobId, false);
    }
  }

  async accept(jobId: string): Promise<void> {
    const owner = this.beginMutation(jobId);
    if (!owner) return;
    try {
      const updated = await videoTrackerApi.accept(jobId);
      if (!this.owns(owner)) return;
      this.invalidateAnnotations(owner.taskId);
      if ((updated.revision ?? 1) < this.currentRevision(jobId)) return;
      useToastStore.getState().push({ msg: "已接受 AI 追踪结果", kind: "success" });
      this.finishReview(jobId, updated.status);
    } catch (err) {
      if (this.owns(owner)) this.pushActionError("接受", err);
    } finally {
      if (this.owns(owner)) this.setSubmitting(jobId, false);
    }
  }

  async discard(jobId: string): Promise<void> {
    const owner = this.beginMutation(jobId);
    if (!owner) return;
    try {
      const updated = await videoTrackerApi.discard(jobId);
      if (!this.owns(owner)) return;
      if ((updated.revision ?? 1) < this.currentRevision(jobId)) return;
      useToastStore.getState().push({ msg: "已丢弃 AI 追踪候选", kind: "" });
      this.finishReview(jobId, updated.status);
    } catch (err) {
      if (this.owns(owner)) this.pushActionError("丢弃", err);
    } finally {
      if (this.owns(owner)) this.setSubmitting(jobId, false);
    }
  }

  private finishReview(jobId: string, status: VideoTrackerJobStatus): void {
    const { [jobId]: _dropCand, ...restCand } = this.candidates;
    this.candidates = restCand;
    const cur = this.jobs[jobId];
    if (cur) this.jobs = { ...this.jobs, [jobId]: { ...cur, status, receivedAt: Date.now() } };
    this.emit();
    this.scheduleTerminalCleanup(jobId);
  }

  private scheduleTerminalCleanup(jobId: string): void {
    this.bumpGeneration(jobId);
    this.resetPoll(jobId);
    const { [jobId]: _candidate, ...candidates } = this.candidates;
    const { [jobId]: _submitting, ...submitting } = this.submitting;
    this.candidates = candidates;
    this.submitting = submitting;
    const timer = this.removeTimers.get(jobId);
    if (timer) clearTimeout(timer);
    this.removeTimers.set(
      jobId,
      setTimeout(() => {
        const { [jobId]: _drop, ...rest } = this.jobs;
        this.jobs = rest;
        const { [jobId]: _dropCand, ...restCand } = this.candidates;
        this.candidates = restCand;
        this.removeTimers.delete(jobId);
        this.readRequests.delete(jobId);
        this.pollFailures.delete(jobId);
        const sock = this.sockets.get(jobId);
        if (sock) {
          try {
            sock.close();
          } catch {
            /* noop */
          }
          this.sockets.delete(jobId);
        }
        this.emit();
      }, REMOVE_AFTER_DONE_MS),
    );
    this.emit();
  }

  async cancel(jobId: string): Promise<void> {
    const cur = this.jobs[jobId];
    const owner = this.beginMutation(jobId);
    if (!cur || !owner) return;
    let updated: VideoTrackerJob;
    try {
      updated = await videoTrackerApi.cancel(jobId);
    } catch (error) {
      if (!this.owns(owner)) return;
      useToastStore.getState().push({
        msg: cur.jobKind === "correction" ? "取消 Mask 纠错传播失败" : "取消 AI 追踪失败",
        sub: error instanceof Error ? error.message : "请重试",
        kind: "error",
      });
      this.schedulePoll(jobId);
      return;
    } finally {
      if (this.owns(owner)) this.setSubmitting(jobId, false);
    }
    if (!this.owns(owner) || (updated.revision ?? 1) < this.currentRevision(jobId)) return;
    this.jobs = {
      ...this.jobs,
      [jobId]: {
        ...cur,
        status: updated.status,
        errorMessage: updated.error_message ?? cur.errorMessage,
        receivedAt: Date.now(),
      },
    };
    this.emit();
    // 取消也暂存部分结果 → 进候选态待审 (而非直接落库)。
    if (updated.status === "cancelled" && cur.jobKind !== "correction") {
      void this.enterReview(jobId);
    } else if (updated.status === "cancelled") {
      const { [jobId]: _dropCandidate, ...restCandidates } = this.candidates;
      this.candidates = restCandidates;
      const socket = this.sockets.get(jobId);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
      this.sockets.delete(jobId);
      this.emit();
      useToastStore.getState().push({
        msg: "Mask 纠错传播已取消",
        sub: "人工纠错帧已保留，候选已清除",
        kind: "warning",
      });
      this.scheduleTerminalCleanup(jobId);
    } else if (updated.status === "failed") {
      this.scheduleTerminalCleanup(jobId);
    }
  }
}

function toJobState(job: VideoTrackerJob): VideoTrackerJobState {
  return {
    jobId: job.id,
    taskId: job.task_id,
    annotationId: job.annotation_id,
    jobKind: job.job_kind ?? "tracking",
    correctionFrame: job.correction_frame,
    status: job.status,
    revision: job.revision,
    fromFrame: job.from_frame,
    toFrame: job.to_frame,
    modelKey: job.model_key,
    errorMessage: job.error_message,
    receivedAt: Date.now(),
  };
}

function mapEventToStatus(
  type: string | undefined,
  prev: VideoTrackerJobStatus,
): VideoTrackerJobStatus {
  switch (type) {
    case "job_started":
    case "job_progress":
    case "frame_result":
      return "running";
    // 完成 = 暂存待审 (非直接 completed)。
    case "job_completed":
      return "pending_review";
    case "job_failed":
      return "failed";
    case "job_cancelled":
      return "cancelled";
    default:
      return prev;
  }
}

const trackerStore = new TrackerJobStore();

export function useVideoTrackerJobs(taskId?: string, enabled = true) {
  const token = useAuthStore((s) => s.token);
  const qc = useQueryClient();
  const [state, setState] = useState<TrackerStoreState>({
    jobs: {},
    candidates: {},
    submitting: {},
    activeReviewJobId: null,
    reviewScopes: {},
    activeReview: null,
  });

  useEffect(() => {
    trackerStore.setAnnotationInvalidator((taskId: string) => {
      qc.invalidateQueries({ queryKey: ["annotations", taskId] });
    });
  }, [qc]);

  useEffect(() => trackerStore.subscribe(setState), []);

  const tokenRef = useRef(token);
  tokenRef.current = token;

  useEffect(() => {
    trackerStore.scopeToTask(enabled && taskId ? taskId : null);
    return () => trackerStore.scopeToTask(null);
  }, [taskId, enabled]);

  useEffect(() => {
    // 传 token 让恢复顺带重连运行中任务的 WS (刷新后仍能收进度 / 完成时冒候选)。token 进依赖:
    // 刷新时 auth store 可能还没 hydrate (token=null), effect 先跑一轮拉数据但因无 token 连不上
    // socket; token 就位后这里重跑, restoreReviewable 内部会跳过重复拉数据、只补连 socket。
    if (taskId && enabled) void trackerStore.restoreReviewable(taskId, token);
  }, [taskId, enabled, token]);

  const propagate = useCallback(
    async (
      taskId: string,
      annotationId: string,
      payload: Parameters<typeof videoTrackerApi.propagate>[2],
    ) => {
      const epoch = trackerStore.captureTaskEpoch(taskId);
      const job = await videoTrackerApi.propagate(taskId, annotationId, payload);
      if (trackerStore.isTaskEpochCurrent(taskId, epoch))
        trackerStore.addJob(job, tokenRef.current);
      return job;
    },
    [],
  );

  // 无源检测发起 (画布级入口, 不绑选中轨迹)。
  const track = useCallback(
    async (taskId: string, payload: Parameters<typeof videoTrackerApi.track>[1]) => {
      const epoch = trackerStore.captureTaskEpoch(taskId);
      const job = await videoTrackerApi.track(taskId, payload);
      if (trackerStore.isTaskEpochCurrent(taskId, epoch))
        trackerStore.addJob(job, tokenRef.current);
      return job;
    },
    [],
  );

  const correct = useCallback(
    async (
      taskId: string,
      annotationId: string,
      payload: Parameters<typeof videoTrackerApi.correct>[2],
    ) => {
      const epoch = trackerStore.captureTaskEpoch(taskId);
      const job = await videoTrackerApi.correct(taskId, annotationId, payload);
      if (trackerStore.isTaskEpochCurrent(taskId, epoch))
        trackerStore.addJob(job, tokenRef.current);
      return job;
    },
    [],
  );

  const cancel = useCallback((jobId: string) => trackerStore.cancel(jobId), []);
  const accept = useCallback((jobId: string) => trackerStore.accept(jobId), []);
  const discard = useCallback((jobId: string) => trackerStore.discard(jobId), []);
  const decide = useCallback(
    (jobId: string, selection: TrackerReviewDecision) => trackerStore.decide(jobId, selection),
    [],
  );
  const refreshReview = useCallback((jobId: string) => trackerStore.refreshReview(jobId), []);
  const chooseReviewJob = useCallback((jobId: string) => trackerStore.chooseReviewJob(jobId), []);
  const setReviewInstances = useCallback(
    (ids: string[]) => trackerStore.setReviewInstances(ids),
    [],
  );
  const setReviewWindow = useCallback(
    (from: number, to: number) => trackerStore.setReviewWindow(from, to),
    [],
  );
  const isReviewIntentCurrent = useCallback(
    (key: string) => trackerStore.isReviewIntentCurrent(key),
    [],
  );

  const jobs = state.jobs;
  const candidates = state.candidates;
  const submitting = state.submitting;

  const byAnnotation = useMemo(() => {
    const map: Record<string, VideoTrackerJobState> = {};
    for (const job of Object.values(jobs)) {
      // 无源 job (annotationId=null) 不进 annotation 索引 (画布按选中轨迹取
      // 候选时不误命中); 其审阅走 job 级 review bar。
      if (!job.annotationId) continue;
      const existing = map[job.annotationId];
      if (!existing || existing.receivedAt < job.receivedAt) {
        map[job.annotationId] = job;
      }
    }
    return map;
  }, [jobs]);

  // 候选按 annotation 归并 (供画布/审阅条按当前选中轨迹取候选)。
  const candidateByAnnotation = useMemo(() => {
    const map: Record<string, VideoTrackerJobPreview> = {};
    for (const preview of Object.values(candidates)) {
      // 无源候选无 annotation_id, 不进 annotation 索引 (走 job 级审阅)。
      if (!preview.annotation_id) continue;
      map[preview.annotation_id] = preview;
    }
    return map;
  }, [candidates]);

  return {
    jobs,
    byAnnotation,
    candidates,
    candidateByAnnotation,
    submitting,
    activeReview: state.activeReview,
    chooseReviewJob,
    setReviewInstances,
    setReviewWindow,
    isReviewIntentCurrent,
    propagate,
    track,
    correct,
    cancel,
    accept,
    discard,
    decide,
    refreshReview,
  };
}
