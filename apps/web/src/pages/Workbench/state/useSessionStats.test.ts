/**
 * v0.8.3 · useSessionStats ring buffer 单测。
 *
 * 覆盖：
 *  - < MIN_SAMPLES (10) 时 avgMs=null，etaMs=null
 *  - ≥ MIN_SAMPLES 时 avgMs=平均，etaMs(n)=avg*n
 *  - dt < 1.5s 误触、idle 暂停、长段切分后仍按任务累计
 *  - 满 RING_SIZE (20) 时旧样本被丢弃
 *  - formatDuration 边界（mm:ss / h:mm / 负值）
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { StrictMode } from "react";
import { ApiError } from "../../../api/client";
import type { TaskEventIn } from "../../../api/me";
import { useSessionStats, formatDuration } from "./useSessionStats";

const submitTaskEvents = vi.hoisted(() => vi.fn().mockResolvedValue({ accepted: 1 }));
vi.mock("../../../api/me", () => ({
  meApi: { submitTaskEvents },
}));

describe("formatDuration", () => {
  it("负数返回 —", () => {
    expect(formatDuration(-1)).toBe("—");
  });
  it("分钟:秒 (< 1h)", () => {
    expect(formatDuration(65 * 1000)).toBe("1:05");
    expect(formatDuration(125 * 1000)).toBe("2:05");
  });
  it("小时:分 (≥ 1h)", () => {
    expect(formatDuration(3600 * 1000)).toBe("1:00");
    expect(formatDuration(3725 * 1000)).toBe("1:02");
  });
});

describe("useSessionStats", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-06T00:00:00Z"));
  });
  afterEach(() => {
    submitTaskEvents.mockReset();
    submitTaskEvents.mockResolvedValue({ accepted: 1 });
    vi.useRealTimers();
  });

  function advance(ms: number) {
    vi.setSystemTime(new Date(Date.now() + ms));
  }

  it("首次切换不入样本（无前一次 tick）", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: "t1" } as { id: string | null } },
    );
    expect(result.current.samplesCount).toBe(0);
    expect(result.current.avgMs).toBeNull();
    expect(result.current.etaMs(5)).toBeNull();

    advance(5_000);
    rerender({ id: "t2" });
    // 现在累计 1 个样本
    expect(result.current.samplesCount).toBe(1);
    // 仍 < MIN_SAMPLES = 10
    expect(result.current.avgMs).toBeNull();
  });

  it("累计 10 个样本 → avgMs 与 etaMs 报值", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: "t0" } as { id: string | null } },
    );
    for (let i = 1; i <= 10; i++) {
      advance(2_000);
      rerender({ id: `t${i}` });
    }
    expect(result.current.samplesCount).toBe(10);
    // 每次间隔 2s
    expect(result.current.avgMs).toBe(2_000);
    expect(result.current.etaMs(3)).toBe(6_000);
    expect(result.current.etaMs(0)).toBeNull();
  });

  it("忽略误触，并把长时间间隔限制在 idle 窗口", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: "t0" } as { id: string | null } },
    );
    advance(500);
    rerender({ id: "t1" });
    expect(result.current.samplesCount).toBe(0);

    advance(31 * 60 * 1000);
    rerender({ id: "t2" });
    // A long interval is bounded by the idle cutoff instead of being lost.
    expect(result.current.samplesCount).toBe(1);

    advance(2_000);
    rerender({ id: "t3" });
    expect(result.current.samplesCount).toBe(2);
  });

  it("满 RING_SIZE=20 时丢弃最旧样本", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: "t0" } as { id: string | null } },
    );
    for (let i = 1; i <= 25; i++) {
      advance(2_000);
      rerender({ id: `t${i}` });
    }
    expect(result.current.samplesCount).toBe(20);
  });

  it("currentTaskId=null 时 useEffect 提前退出", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: null } as { id: string | null } },
    );
    expect(result.current.samplesCount).toBe(0);
    advance(5_000);
    rerender({ id: null });
    expect(result.current.samplesCount).toBe(0);
  });

  it("任务暂时为空后切换项目时仍重置 ETA 样本", () => {
    type Props = { id: string | null; project: string; kind: "annotate" | "review" };
    const { result, rerender } = renderHook(
      ({ id, project, kind }: Props) => useSessionStats(id, project, kind, "user-1"),
      {
        initialProps: { id: "t1", project: "p1", kind: "annotate" } as Props,
      },
    );
    advance(2_000);
    rerender({ id: null, project: "p1", kind: "annotate" });
    expect(result.current.samplesCount).toBe(1);
    rerender({ id: "t2", project: "p2", kind: "annotate" });
    expect(result.current.samplesCount).toBe(0);
  });

  it("act 包装：相同 id 重复 rerender 不增样本", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      { initialProps: { id: "t1" } as { id: string | null } },
    );
    advance(3_000);
    act(() => rerender({ id: "t1" }));
    expect(result.current.samplesCount).toBe(0);
  });

  it("StrictMode 重复挂载后仍恢复收集状态", () => {
    const { result, rerender } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id),
      {
        initialProps: { id: "t1" } as { id: string | null },
        wrapper: StrictMode,
      },
    );
    advance(2_000);
    rerender({ id: "t2" });
    expect(result.current.samplesCount).toBe(1);
  });

  it("切换项目与工作类型时，闭合事件保留上一题上下文", async () => {
    type Props = { id: string; project: string; kind: "annotate" | "review" };
    const { rerender } = renderHook(
      ({ id, project, kind }: Props) => useSessionStats(id, project, kind, "user-1"),
      { initialProps: { id: "t1", project: "p1", kind: "annotate" } as Props },
    );
    advance(2_000);
    rerender({ id: "t2", project: "p1", kind: "annotate" });
    advance(2_000);
    rerender({ id: "t3", project: "p2", kind: "review" });
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    const events = submitTaskEvents.mock.calls.flatMap(
      ([batch]) => batch as Array<{ task_id: string; project_id: string; kind: string }>,
    );
    expect(events).toEqual([
      expect.objectContaining({ task_id: "t1", project_id: "p1", kind: "annotate" }),
      expect.objectContaining({ task_id: "t2", project_id: "p1", kind: "annotate" }),
      expect.objectContaining({ task_id: "t3", project_id: "p2", kind: "review" }),
    ]);
  });

  it("隐藏期间暂停，切题与卸载都关闭当前间隔", async () => {
    const { result, rerender, unmount } = renderHook(
      ({ id }: { id: string | null }) => useSessionStats(id, "project-1", "annotate", "user-1"),
      { initialProps: { id: "t1" } as { id: string | null } },
    );
    advance(2_000);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "hidden",
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(result.current.samplesCount).toBe(0);

    advance(10 * 60 * 1_000);
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      value: "visible",
    });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    advance(2_000);
    rerender({ id: "t2" });
    expect(result.current.samplesCount).toBe(1);
    advance(2_000);
    unmount();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const events = submitTaskEvents.mock.calls.flatMap(
      ([batch]) => batch as Array<{ client_id?: string; duration_ms: number }>,
    );
    expect(events).toHaveLength(3);
    expect(events.every((event) => event.client_id)).toBe(true);
    expect(events.every((event) => event.duration_ms === 2_000)).toBe(true);
    expect(new Set(events.map((event) => event.client_id)).size).toBe(3);
  });

  it("watchdog 在 idle 边界关闭，活动后重新开始", async () => {
    const { result, unmount } = renderHook(() =>
      useSessionStats("t1", "project-1", "annotate", "user-1"),
    );
    await act(async () => {
      vi.advanceTimersByTime(5 * 60 * 1_000 + 15_000);
      await Promise.resolve();
    });
    expect(result.current.samplesCount).toBe(0);

    act(() => document.dispatchEvent(new Event("pointerdown")));
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      await Promise.resolve();
    });
    expect(result.current.samplesCount).toBe(1);
    const events = submitTaskEvents.mock.calls.flatMap(
      ([batch]) => batch as Array<{ duration_ms: number }>,
    );
    expect(events.map((event) => event.duration_ms)).toEqual([5 * 60 * 1_000, 2_000]);
    unmount();
  });

  it("持续活动超过单段上限时切成不超过 30 分钟的事件", async () => {
    const { result, unmount } = renderHook(() =>
      useSessionStats("t1", "project-1", "annotate", "user-1"),
    );
    for (let i = 0; i < 8; i++) {
      act(() => vi.advanceTimersByTime(4 * 60 * 1_000));
      act(() => document.dispatchEvent(new Event("pointermove")));
    }
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    const events = submitTaskEvents.mock.calls.flatMap(
      ([batch]) => batch as Array<{ duration_ms: number }>,
    );
    expect(events.length).toBe(2);
    expect(events[0]?.duration_ms).toBe(30 * 60 * 1_000);
    expect(events[1]?.duration_ms).toBe(2 * 60 * 1_000);
    expect(events.every((event) => event.duration_ms <= 30 * 60 * 1_000)).toBe(true);
    expect(result.current.samplesCount).toBe(1);
    unmount();
  });

  it("网络恢复后按 API 上限分块冲刷离线队列", async () => {
    let resolveFirst!: (value: { accepted: number }) => void;
    submitTaskEvents.mockImplementationOnce(
      () => new Promise((resolve) => (resolveFirst = resolve)),
    );
    const { rerender, unmount } = renderHook(
      ({ id }: { id: string }) => useSessionStats(id, "project-1", "annotate", "user-1"),
      { initialProps: { id: "t0" } },
    );
    for (let i = 1; i <= 220; i++) {
      act(() => vi.advanceTimersByTime(2_000));
      act(() => document.dispatchEvent(new Event("pointermove")));
      rerender({ id: `t${i}` });
    }
    act(() => vi.advanceTimersByTime(2_000));
    act(() => window.dispatchEvent(new Event("pagehide")));
    act(() => resolveFirst({ accepted: 20 }));
    await act(async () => {
      for (let i = 0; i < 12; i++) await Promise.resolve();
    });
    const batches = submitTaskEvents.mock.calls.map(([batch]) => batch as Array<unknown>);
    expect(batches.every((batch) => batch.length <= 200)).toBe(true);
    expect(batches.reduce((total, batch) => total + batch.length, 0)).toBe(221);
    unmount();
  });

  it("失败时保留事件且最多重试两次", async () => {
    submitTaskEvents.mockRejectedValue(new Error("offline"));
    const { unmount } = renderHook(() => useSessionStats("t1", "project-1", "annotate", "user-1"));
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(1_000);
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(2_000);
      await Promise.resolve();
    });
    expect(submitTaskEvents).toHaveBeenCalledTimes(3);
    unmount();
  });

  it("永久无效的单条事件只丢弃该条，后续有效事件仍会上报", async () => {
    submitTaskEvents
      .mockRejectedValueOnce(new ApiError(404, "Task not found", { reason: "task_not_found" }))
      .mockResolvedValue({ accepted: 1, queued_async: false });
    const { rerender, unmount } = renderHook(
      ({ id }: { id: string }) => useSessionStats(id, "project-1", "annotate", "user-1"),
      { initialProps: { id: "stale-task" } },
    );

    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });
    expect(submitTaskEvents).toHaveBeenCalledTimes(1);

    rerender({ id: "valid-task" });
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    await act(async () => {
      for (let i = 0; i < 6; i++) await Promise.resolve();
    });

    expect(submitTaskEvents).toHaveBeenCalledTimes(2);
    expect((submitTaskEvents.mock.calls[1]?.[0] as Array<{ task_id: string }>)[0]).toEqual(
      expect.objectContaining({ task_id: "valid-task" }),
    );
    unmount();
  });

  it("本地队列溢出后给保留事件标记 partial 覆盖", async () => {
    let resolveFirst!: (value: { accepted: number; queued_async: boolean }) => void;
    submitTaskEvents
      .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
      .mockResolvedValue({ accepted: 1, queued_async: false });
    const { rerender, unmount } = renderHook(
      ({ id }: { id: string }) => useSessionStats(id, "project-1", "annotate", "user-1"),
      { initialProps: { id: "t0" } },
    );

    for (let i = 1; i <= 1_020; i++) {
      act(() => vi.advanceTimersByTime(2_000));
      act(() => document.dispatchEvent(new Event("pointermove")));
      rerender({ id: `t${i}` });
    }
    act(() => vi.advanceTimersByTime(2_000));
    act(() => window.dispatchEvent(new Event("pagehide")));
    act(() => resolveFirst({ accepted: 20, queued_async: false }));
    await act(async () => {
      for (let i = 0; i < 20; i++) await Promise.resolve();
    });

    const batches = submitTaskEvents.mock.calls.map(([batch]) => batch as Array<TaskEventIn>);
    expect(batches.length).toBeGreaterThan(1);
    expect(batches[1]?.[0]).toEqual(expect.objectContaining({ collection_coverage: "partial" }));
    unmount();
  });

  it("账号切换后旧请求失败不会清空新账号队列", async () => {
    let rejectOld!: (reason?: unknown) => void;
    submitTaskEvents
      .mockImplementationOnce(() => new Promise((_resolve, reject) => (rejectOld = reject)))
      .mockResolvedValue({ accepted: 1 });
    const { rerender, unmount } = renderHook(
      ({ id, account }: { id: string | null; account: string }) =>
        useSessionStats(id, "project-1", "annotate", account),
      {
        initialProps: { id: "t1", account: "user-1" } as {
          id: string | null;
          account: string;
        },
      },
    );
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(submitTaskEvents).toHaveBeenCalledTimes(1);

    rerender({ id: null, account: "user-2" });
    rerender({ id: "t2", account: "user-2" });
    advance(2_000);
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(submitTaskEvents).toHaveBeenCalledTimes(2);

    await act(async () => {
      rejectOld(new Error("expired account request"));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(submitTaskEvents).toHaveBeenCalledTimes(2);
    expect((submitTaskEvents.mock.calls[1]?.[0] as Array<{ task_id: string }>)[0]).toEqual(
      expect.objectContaining({ task_id: "t2" }),
    );
    unmount();
  });
});
