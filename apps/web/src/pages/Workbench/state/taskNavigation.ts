export async function runWorkbenchLeaveGuards(
  videoGuard: (isCurrent: () => boolean) => Promise<boolean>,
  maskGuard: () => Promise<boolean>,
  isCurrent: () => boolean,
): Promise<boolean> {
  if (!isCurrent() || !(await videoGuard(isCurrent)) || !isCurrent()) return false;
  return (await maskGuard()) && isCurrent();
}

export async function commitAfterNavigationGuard(
  guard: () => Promise<boolean>,
  signal: AbortSignal | readonly (AbortSignal | undefined)[] | undefined,
  commit: () => void,
): Promise<boolean> {
  const allowed = await guard();
  const aborted = signal
    ? "aborted" in signal
      ? signal.aborted
      : signal.some((candidate) => candidate?.aborted)
    : false;
  if (!allowed || aborted) return false;
  commit();
  return true;
}

type LatestTaskNavigationRun = (signal: AbortSignal) => Promise<boolean>;

interface PendingTaskNavigation {
  taskId: string;
  run: LatestTaskNavigationRun;
  resolve: (allowed: boolean) => void;
}

export class LatestTaskNavigationScheduler {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: PendingTaskNavigation | null = null;
  private activeController: AbortController | null = null;
  private disposed = false;

  constructor(private readonly settleMs: number) {}

  activate(): void {
    this.disposed = false;
  }

  schedule(taskId: string, run: LatestTaskNavigationRun): Promise<boolean> {
    if (this.disposed) return Promise.resolve(false);
    return new Promise<boolean>((resolve) => {
      if (this.timer === null && this.pending === null && this.activeController === null) {
        this.runNow({ taskId, run, resolve });
        this.armTimer();
        return;
      }
      this.activeController?.abort();
      this.pending?.resolve(false);
      this.pending = { taskId, run, resolve };
      this.armTimer();
    });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.activeController?.abort();
    this.activeController = null;
    this.pending?.resolve(false);
    this.pending = null;
  }

  private armTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flushPending();
    }, this.settleMs);
  }

  private flushPending(): void {
    if (this.disposed || this.activeController !== null) return;
    const pending = this.pending;
    this.pending = null;
    if (!pending) return;
    this.runNow(pending);
    this.armTimer();
  }

  private runNow(pending: PendingTaskNavigation): void {
    const controller = new AbortController();
    this.activeController = controller;
    void Promise.resolve()
      .then(() => pending.run(controller.signal))
      .then((allowed) => pending.resolve(controller.signal.aborted ? false : allowed))
      .catch(() => pending.resolve(false))
      .finally(() => {
        if (this.activeController !== controller) return;
        this.activeController = null;
        if (this.pending && this.timer === null) this.flushPending();
      });
  }
}

export const TASK_NAVIGATION_SETTLE_MS = 160;

export interface LocalTaskUrlSyncDecision {
  holdRequestedTask: boolean;
  clearPendingTarget: boolean;
}

export function resolveLocalTaskUrlSync(
  requestedTaskId: string | null,
  pendingLocalTaskId: string | null,
): LocalTaskUrlSyncDecision {
  if (!pendingLocalTaskId) {
    return { holdRequestedTask: false, clearPendingTarget: false };
  }
  if (requestedTaskId === pendingLocalTaskId) {
    return { holdRequestedTask: false, clearPendingTarget: true };
  }
  return { holdRequestedTask: true, clearPendingTarget: false };
}
