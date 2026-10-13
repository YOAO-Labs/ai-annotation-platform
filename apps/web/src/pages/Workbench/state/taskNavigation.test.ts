import { describe, it, expect, vi } from "vitest";
import {
  LatestTaskNavigationScheduler,
  commitAfterNavigationGuard,
  resolveLocalTaskUrlSync,
  runWorkbenchLeaveGuards,
} from "./taskNavigation";

describe("resolveLocalTaskUrlSync", () => {
  it("本地目标尚未写入 URL 时不允许旧 requestedTaskId 回退当前任务", () => {
    expect(resolveLocalTaskUrlSync("task-old", "task-new")).toEqual({
      holdRequestedTask: true,
      clearPendingTarget: false,
    });
  });

  it("URL 追上本地目标后解除保护，之后恢复外部 URL 同步", () => {
    expect(resolveLocalTaskUrlSync("task-new", "task-new")).toEqual({
      holdRequestedTask: false,
      clearPendingTarget: true,
    });
    expect(resolveLocalTaskUrlSync("task-external", null)).toEqual({
      holdRequestedTask: false,
      clearPendingTarget: false,
    });
  });
});

describe("commitAfterNavigationGuard", () => {
  it("快速导航时不允许较旧 guard 的迟到结果提交切题", async () => {
    const firstController = new AbortController();
    let releaseFirst: ((allowed: boolean) => void) | undefined;
    const firstGuard = () =>
      new Promise<boolean>((resolve) => {
        releaseFirst = resolve;
      });
    const commits: string[] = [];
    const first = commitAfterNavigationGuard(firstGuard, firstController.signal, () =>
      commits.push("first"),
    );
    firstController.abort();
    const second = commitAfterNavigationGuard(
      async () => true,
      undefined,
      () => commits.push("second"),
    );
    await expect(second).resolves.toBe(true);
    releaseFirst?.(true);
    await expect(first).resolves.toBe(false);
    expect(commits).toEqual(["second"]);
  });
});

describe("Workbench leave admission for task, batch and detail routes", () => {
  it("video cancellation preserves the current owner before Mask or state writes", async () => {
    const mask = vi.fn(async () => true);
    const commit = vi.fn();
    const allowed = await commitAfterNavigationGuard(
      () =>
        runWorkbenchLeaveGuards(
          async () => false,
          mask,
          () => true,
        ),
      undefined,
      commit,
    );
    expect(allowed).toBe(false);
    expect(mask).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
  });

  it("awaits both owners before committing the target batch and task together", async () => {
    let confirmVideo!: (allowed: boolean) => void;
    let confirmMask!: (allowed: boolean) => void;
    const video = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          confirmVideo = resolve;
        }),
    );
    const mask = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          confirmMask = resolve;
        }),
    );
    let context = { taskId: "t1", batchId: "b1" };
    const pending = commitAfterNavigationGuard(
      () => runWorkbenchLeaveGuards(video, mask, () => true),
      undefined,
      () => {
        context = { taskId: "t9", batchId: "b2" };
      },
    );
    expect(context).toEqual({ taskId: "t1", batchId: "b1" });
    expect(mask).not.toHaveBeenCalled();
    confirmVideo(true);
    await Promise.resolve();
    expect(mask).toHaveBeenCalledOnce();
    expect(context).toEqual({ taskId: "t1", batchId: "b1" });
    confirmMask(true);
    await expect(pending).resolves.toBe(true);
    expect(context).toEqual({ taskId: "t9", batchId: "b2" });
  });

  it.each(["video", "mask"])("rejects an obsolete owner after waiting for %s", async (stage) => {
    let current = true;
    const video = vi.fn(async () => {
      if (stage === "video") current = false;
      return true;
    });
    const mask = vi.fn(async () => {
      current = false;
      return true;
    });
    const commit = vi.fn();
    await expect(
      commitAfterNavigationGuard(
        () => runWorkbenchLeaveGuards(video, mask, () => current),
        undefined,
        commit,
      ),
    ).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
    expect(mask).toHaveBeenCalledTimes(stage === "video" ? 0 : 1);
  });
});

describe("LatestTaskNavigationScheduler", () => {
  it("立即提交首次导航，并把 100ms 连续输入合并成最终目标", async () => {
    vi.useFakeTimers();
    const scheduler = new LatestTaskNavigationScheduler(160);
    const committed: string[] = [];

    try {
      const first = scheduler.schedule("task-1", async () => {
        committed.push("task-1");
        return true;
      });
      await vi.advanceTimersByTimeAsync(0);

      const second = scheduler.schedule("task-2", async () => {
        committed.push("task-2");
        return true;
      });
      await vi.advanceTimersByTimeAsync(100);
      const latest = scheduler.schedule("task-3", async () => {
        committed.push("task-3");
        return true;
      });
      await vi.advanceTimersByTimeAsync(159);

      expect(committed).toEqual(["task-1"]);
      await vi.advanceTimersByTimeAsync(1);
      await expect(Promise.all([first, second, latest])).resolves.toEqual([true, false, true]);
      expect(committed).toEqual(["task-1", "task-3"]);
    } finally {
      scheduler.dispose();
      vi.useRealTimers();
    }
  });

  it("新目标会中止仍在等待 guard 的旧导航", async () => {
    vi.useFakeTimers();
    const scheduler = new LatestTaskNavigationScheduler(160);
    const observedSignals: AbortSignal[] = [];

    try {
      const first = scheduler.schedule("task-1", async (signal) => {
        observedSignals.push(signal);
        await new Promise<void>((resolve) => signal.addEventListener("abort", () => resolve()));
        return !signal.aborted;
      });
      await vi.advanceTimersByTimeAsync(0);
      const latest = scheduler.schedule("task-2", async () => true);
      await vi.advanceTimersByTimeAsync(160);

      expect(observedSignals[0]).toBeInstanceOf(AbortSignal);
      expect(observedSignals[0]?.aborted).toBe(true);
      await expect(first).resolves.toBe(false);
      await expect(latest).resolves.toBe(true);
    } finally {
      scheduler.dispose();
      vi.useRealTimers();
    }
  });

  it("旧 guard 未结束时不会并发打开第二个 guard", async () => {
    vi.useFakeTimers();
    const scheduler = new LatestTaskNavigationScheduler(160);
    let finishFirst!: (allowed: boolean) => void;
    const secondRun = vi.fn().mockResolvedValue(true);

    try {
      const first = scheduler.schedule(
        "task-1",
        () =>
          new Promise<boolean>((resolve) => {
            finishFirst = resolve;
          }),
      );
      await vi.advanceTimersByTimeAsync(0);
      const second = scheduler.schedule("task-2", secondRun);
      await vi.advanceTimersByTimeAsync(160);

      expect(secondRun).not.toHaveBeenCalled();
      finishFirst(true);
      await vi.advanceTimersByTimeAsync(0);

      await expect(first).resolves.toBe(false);
      await expect(second).resolves.toBe(true);
      expect(secondRun).toHaveBeenCalledOnce();
    } finally {
      scheduler.dispose();
      vi.useRealTimers();
    }
  });
});
