import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement, type ReactNode } from "react";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import {
  videoTrackerApi,
  type VideoTrackerJob,
  type VideoTrackerJobPreview,
} from "@/api/videoTracker";
import { ApiError } from "@/api/client";
import { useToastStore } from "@/components/ui";
import {
  TrackerJobStore,
  useVideoTrackerJobs,
  type TrackerStoreState,
} from "./useVideoTrackerJobs";

vi.mock("@/api/videoTracker", () => ({
  videoTrackerApi: {
    reviewable: vi.fn(),
    active: vi.fn(),
    preview: vi.fn(),
    get: vi.fn(),
    decide: vi.fn(),
    accept: vi.fn(),
    discard: vi.fn(),
    cancel: vi.fn(),
    track: vi.fn(),
    propagate: vi.fn(),
    correct: vi.fn(),
  },
}));

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  closed = false;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (error: unknown) => void;
} {
  let resolve!: (v: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((r, fail) => {
    resolve = r;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const reviewableJob: VideoTrackerJob = {
  id: "job-1",
  task_id: "task-1",
  dataset_item_id: "item-1",
  annotation_id: "annotation-1",
  segment_id: null,
  created_by: "user-1",
  status: "pending_review",
  revision: 1,
  model_key: "sam2_video",
  direction: "forward",
  from_frame: 0,
  to_frame: 20,
  prompt: {},
  event_channel: "video-tracker-job:job-1",
  celery_task_id: null,
  cancel_requested_at: null,
  started_at: null,
  completed_at: null,
  error_message: null,
  created_at: "2026-07-11T00:00:00Z",
  updated_at: null,
};

const runningJob: VideoTrackerJob = {
  ...reviewableJob,
  id: "job-run-1",
  status: "running",
  annotation_id: "annotation-2",
};

const runningCorrectionJob: VideoTrackerJob = {
  ...runningJob,
  id: "job-correction-1",
  annotation_id: "annotation-1",
  job_kind: "correction",
  correction_frame: 5,
};

const reviewableCorrectionJob: VideoTrackerJob = {
  ...runningCorrectionJob,
  status: "pending_review",
};

const stagedPreview: import("@/api/videoTracker").VideoTrackerJobPreview = {
  job_id: reviewableJob.id,
  status: reviewableJob.status,
  annotation_id: reviewableJob.annotation_id,
  results: [
    {
      frame_index: 1,
      instance_id: "1",
      geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    },
  ],
  grid_step: 1,
  output_geometry: "bbox",
  job_revision: 1,
  expected_source_versions: { "annotation-1": 1 },
  candidate_total: 2,
  candidate_pending: 1,
  candidate_accepted: 0,
  candidate_rejected: 0,
};

function emptySnapshot(): TrackerStoreState {
  return {
    jobs: {},
    candidates: {},
    submitting: {},
    activeReviewJobId: null,
    reviewScopes: {},
    activeReview: null,
  };
}

const stores = new Set<TrackerJobStore>();
function createStore(): TrackerJobStore {
  const store = new TrackerJobStore();
  stores.add(store);
  return store;
}

afterEach(() => {
  for (const store of stores) store.scopeToTask(null);
  stores.clear();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.resetAllMocks();
  MockWebSocket.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([]);
  vi.mocked(videoTrackerApi.active).mockResolvedValue([]);
  vi.mocked(videoTrackerApi.get).mockResolvedValue(reviewableJob);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

describe("TrackerJobStore.restoreReviewable", () => {
  it("retries failed listings when authentication becomes usable", async () => {
    vi.mocked(videoTrackerApi.reviewable)
      .mockRejectedValueOnce(new ApiError(401, "unauthorized"))
      .mockResolvedValueOnce([reviewableJob]);
    vi.mocked(videoTrackerApi.active)
      .mockRejectedValueOnce(new ApiError(401, "unauthorized"))
      .mockResolvedValueOnce([]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue(stagedPreview);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    await store.restoreReviewable("task-1");
    expect(snapshot.candidates).toEqual({});
    await store.restoreReviewable("task-1", "ready-token");
    expect(videoTrackerApi.reviewable).toHaveBeenCalledTimes(2);
    expect(snapshot.candidates["job-1"]).toBeDefined();
  });

  it("retries after an in-flight unauthenticated restore fails even if the token arrived earlier", async () => {
    const gate = deferred<VideoTrackerJob[]>();
    vi.mocked(videoTrackerApi.reviewable)
      .mockReturnValueOnce(gate.promise)
      .mockResolvedValueOnce([reviewableJob]);
    vi.mocked(videoTrackerApi.active)
      .mockRejectedValueOnce(new ApiError(401, "unauthorized"))
      .mockResolvedValueOnce([]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue(stagedPreview);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    const initial = store.restoreReviewable("task-1");
    const tokenReady = store.restoreReviewable("task-1", "ready-token");
    gate.reject(new ApiError(401, "unauthorized"));
    await Promise.all([initial, tokenReady]);
    await vi.waitFor(() => expect(snapshot.candidates["job-1"]).toBeDefined());
    expect(videoTrackerApi.reviewable).toHaveBeenCalledTimes(2);
  });

  it("restores a pending candidate from the server after page state is lost", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      job_id: "job-1",
      status: "pending_review",
      annotation_id: "annotation-1",
      results: [
        {
          frame_index: 1,
          geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
        },
      ],
      grid_step: 1,
      output_geometry: "bbox",
    });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1");

    expect(videoTrackerApi.reviewable).toHaveBeenCalledWith("task-1");
    expect(snapshot.jobs["job-1"]).toMatchObject({
      taskId: "task-1",
      annotationId: "annotation-1",
      status: "pending_review",
    });
    expect(snapshot.candidates["job-1"].results).toHaveLength(1);
  });

  it("preview 为空时保留作业以供轮询确认", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      job_id: "job-1",
      status: "pending_review",
      annotation_id: "annotation-1",
      results: [],
      grid_step: 1,
      output_geometry: "bbox",
    });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1");

    expect(snapshot.jobs["job-1"]?.status).toBe("pending_review");
    expect(snapshot.candidates).toEqual({});
    await store.restoreReviewable("task-2");
  });
});

describe("TrackerJobStore.restoreReviewable · 重连运行中任务 (#10)", () => {
  it("恢复运行中任务到 UI 并按 token 重连 WebSocket", async () => {
    vi.mocked(videoTrackerApi.active).mockResolvedValue([runningJob]);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1", "tok-abc");

    expect(videoTrackerApi.active).toHaveBeenCalledWith("task-1");
    expect(snapshot.jobs["job-run-1"]).toMatchObject({
      taskId: "task-1",
      status: "running",
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].url).toContain("/ws/video-tracker-jobs/job-run-1");
    expect(MockWebSocket.instances[0].url).toContain("token=tok-abc");
  });

  it("无 token 时仍恢复运行中任务到 UI, 但不重连 WebSocket", async () => {
    vi.mocked(videoTrackerApi.active).mockResolvedValue([runningJob]);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1");

    expect(snapshot.jobs["job-run-1"]?.status).toBe("running");
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it("无 token 时轮询到终态并恢复纠错候选", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(videoTrackerApi.active).mockResolvedValue([runningCorrectionJob]);
      vi.mocked(videoTrackerApi.get).mockResolvedValue(reviewableCorrectionJob);
      vi.mocked(videoTrackerApi.preview).mockResolvedValue({
        ...stagedPreview,
        job_id: runningCorrectionJob.id,
        job_kind: "correction",
        correction_frame: 5,
      });
      const store = createStore();
      let snapshot = emptySnapshot();
      store.subscribe((next) => {
        snapshot = next;
      });

      await store.restoreReviewable("task-1");
      await vi.advanceTimersByTimeAsync(2000);
      await Promise.resolve();
      await Promise.resolve();

      expect(videoTrackerApi.get).toHaveBeenCalledWith("job-correction-1");
      expect(videoTrackerApi.preview).toHaveBeenCalledWith("job-correction-1");
      expect(snapshot.jobs["job-correction-1"]?.status).toBe("pending_review");
      expect(snapshot.candidates["job-correction-1"]?.results).toHaveLength(1);
      await store.restoreReviewable("task-2");
    } finally {
      vi.useRealTimers();
    }
  });

  it("WebSocket 断开后轮询恢复作业状态", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(videoTrackerApi.active).mockResolvedValue([runningCorrectionJob]);
      vi.mocked(videoTrackerApi.get).mockResolvedValue({
        ...runningCorrectionJob,
        status: "running",
      });
      const store = createStore();
      await store.restoreReviewable("task-1", "tok-abc");
      expect(MockWebSocket.instances).toHaveLength(1);

      MockWebSocket.instances[0].onclose?.();
      await vi.advanceTimersByTimeAsync(2000);

      expect(videoTrackerApi.get).toHaveBeenCalledWith("job-correction-1");
      await store.restoreReviewable("task-2");
    } finally {
      vi.useRealTimers();
    }
  });

  it("reviewable 拉取失败不阻断运行中任务的重连", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockRejectedValue(new Error("boom"));
    vi.mocked(videoTrackerApi.active).mockResolvedValue([runningJob]);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1", "tok-abc");

    expect(snapshot.jobs["job-run-1"]?.status).toBe("running");
    expect(MockWebSocket.instances).toHaveLength(1);
  });
});

describe("TrackerJobStore.cancel · Mask 纠错", () => {
  it("取消成功后立即移除候选并保留终态", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableCorrectionJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      ...stagedPreview,
      job_id: reviewableCorrectionJob.id,
      job_kind: "correction",
      correction_frame: 5,
    });
    vi.mocked(videoTrackerApi.cancel).mockResolvedValue({
      ...reviewableCorrectionJob,
      status: "cancelled",
    });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    await store.restoreReviewable("task-1");
    expect(snapshot.candidates["job-correction-1"]).toBeDefined();

    await store.cancel("job-correction-1");

    expect(snapshot.jobs["job-correction-1"]?.status).toBe("cancelled");
    expect(snapshot.candidates["job-correction-1"]).toBeUndefined();
    await store.restoreReviewable("task-2");
  });

  it("取消失败时保留作业与候选", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableCorrectionJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      ...stagedPreview,
      job_id: reviewableCorrectionJob.id,
      job_kind: "correction",
      correction_frame: 5,
    });
    vi.mocked(videoTrackerApi.cancel).mockRejectedValue(new Error("offline"));
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    await store.restoreReviewable("task-1");

    await store.cancel("job-correction-1");

    expect(snapshot.jobs["job-correction-1"]?.status).toBe("pending_review");
    expect(snapshot.candidates["job-correction-1"]).toBeDefined();
    await store.restoreReviewable("task-2");
  });

  it("取消期间迟到的轮询响应不会复活作业", async () => {
    vi.useFakeTimers();
    try {
      const gate = deferred<VideoTrackerJob>();
      vi.mocked(videoTrackerApi.active).mockResolvedValue([runningCorrectionJob]);
      vi.mocked(videoTrackerApi.get).mockReturnValue(gate.promise);
      vi.mocked(videoTrackerApi.cancel).mockResolvedValue({
        ...runningCorrectionJob,
        status: "cancelled",
      });
      const store = createStore();
      let snapshot = emptySnapshot();
      store.subscribe((next) => {
        snapshot = next;
      });
      await store.restoreReviewable("task-1");
      vi.advanceTimersByTime(2000);
      await Promise.resolve();
      expect(videoTrackerApi.get).toHaveBeenCalledWith("job-correction-1");

      await store.cancel("job-correction-1");
      gate.resolve({ ...runningCorrectionJob, status: "running" });
      await Promise.resolve();
      await Promise.resolve();

      expect(snapshot.jobs["job-correction-1"]?.status).toBe("cancelled");
      await store.restoreReviewable("task-2");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("TrackerJobStore.restoreReviewable · 切任务 scope 清理 (#9)", () => {
  it("切到新任务时清掉旧任务的候选与 job", async () => {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...stagedPreview });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1");
    expect(snapshot.candidates["job-1"]).toBeDefined();

    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([]);
    await store.restoreReviewable("task-2");

    expect(snapshot.jobs["job-1"]).toBeUndefined();
    expect(snapshot.candidates["job-1"]).toBeUndefined();
    expect(snapshot.jobs).toEqual({});
    expect(snapshot.candidates).toEqual({});
  });

  it("切任务时关闭旧任务运行中 job 的 WebSocket", async () => {
    vi.mocked(videoTrackerApi.active).mockResolvedValue([runningJob]);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1", "tok-abc");
    expect(MockWebSocket.instances).toHaveLength(1);
    const staleSocket = MockWebSocket.instances[0];

    vi.mocked(videoTrackerApi.active).mockResolvedValue([]);
    await store.restoreReviewable("task-2", "tok-abc");

    expect(staleSocket.closed).toBe(true);
    expect(snapshot.jobs["job-run-1"]).toBeUndefined();
  });

  it("同一任务重复恢复不会误删本任务的活跃 job", async () => {
    vi.mocked(videoTrackerApi.active).mockResolvedValue([runningJob]);
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    await store.restoreReviewable("task-1", "tok-abc");
    await store.restoreReviewable("task-1", "tok-abc");

    expect(snapshot.jobs["job-run-1"]?.status).toBe("running");
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(MockWebSocket.instances[0].closed).toBe(false);
  });

  it("恢复期间切走任务, 迟到的旧任务结果不会被塞回", async () => {
    const gate = deferred<VideoTrackerJob[]>();
    vi.mocked(videoTrackerApi.reviewable).mockReturnValueOnce(gate.promise);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...stagedPreview });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });

    const inflight = store.restoreReviewable("task-1");
    await store.restoreReviewable("task-2");
    gate.resolve([reviewableJob]);
    await inflight;

    expect(snapshot.jobs["job-1"]).toBeUndefined();
    expect(snapshot.candidates["job-1"]).toBeUndefined();
  });
});

describe("TrackerJobStore.decide · 局部审阅", () => {
  async function restoredStore() {
    vi.mocked(videoTrackerApi.reviewable).mockResolvedValue([reviewableJob]);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...stagedPreview });
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    await store.restoreReviewable("task-1");
    return { store, getSnapshot: () => snapshot };
  }

  it("局部成功后用服务端 revision/source version 刷新并保留审阅", async () => {
    const { store, getSnapshot } = await restoredStore();
    const updatedJob = { ...reviewableJob, status: "partially_reviewed" as const, revision: 2 };
    const updatedPreview = {
      ...stagedPreview,
      status: "partially_reviewed" as const,
      job_revision: 2,
      expected_source_versions: { "annotation-1": 2 },
      results: [{ ...stagedPreview.results[0], frame_index: 2 }],
      candidate_accepted: 1,
    };
    vi.mocked(videoTrackerApi.decide).mockResolvedValue(updatedJob);
    vi.mocked(videoTrackerApi.get).mockResolvedValue(updatedJob);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue(updatedPreview);

    const outcome = await store.decide("job-1", {
      instance_ids: ["1"],
      from_frame: 1,
      to_frame: 1,
      decision: "accept",
      override_manual: false,
    });

    expect(outcome).toEqual({ ok: true });
    expect(videoTrackerApi.decide).toHaveBeenCalledWith(
      "job-1",
      expect.objectContaining({
        job_revision: 1,
        expected_source_versions: { "annotation-1": 1 },
      }),
    );
    expect(getSnapshot().jobs["job-1"]?.status).toBe("partially_reviewed");
    expect(getSnapshot().candidates["job-1"]?.job_revision).toBe(2);
  });

  it("manual 409 保留候选并把原因交给二次确认", async () => {
    const { store, getSnapshot } = await restoredStore();
    vi.mocked(videoTrackerApi.decide).mockRejectedValue(
      new ApiError(409, "manual", { reason: "manual_keyframe_protected" }),
    );
    const outcome = await store.decide("job-1", {
      instance_ids: ["1"],
      from_frame: 1,
      to_frame: 1,
      decision: "accept",
      override_manual: false,
    });
    expect(outcome).toEqual({ ok: false, reason: "manual_keyframe_protected" });
    expect(getSnapshot().candidates["job-1"]).toBeDefined();
  });

  it.each(["job_revision_conflict", "candidate_decision_conflict"])(
    "%s 刷新预览且不清候选",
    async (reason) => {
      const { store, getSnapshot } = await restoredStore();
      const refreshedJob = { ...reviewableJob, status: "partially_reviewed" as const, revision: 3 };
      vi.mocked(videoTrackerApi.decide).mockRejectedValue(new ApiError(409, "stale", { reason }));
      vi.mocked(videoTrackerApi.get).mockResolvedValue(refreshedJob);
      vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...stagedPreview, job_revision: 3 });
      const outcome = await store.decide("job-1", {
        instance_ids: ["1"],
        from_frame: 1,
        to_frame: 1,
        decision: "reject",
        override_manual: false,
      });
      expect(outcome.reason).toBe(reason);
      expect(getSnapshot().candidates["job-1"]?.job_revision).toBe(3);
    },
  );
});

const secondReviewJob: VideoTrackerJob = { ...reviewableJob, id: "job-2", annotation_id: null };
const multiPreview: VideoTrackerJobPreview = {
  ...stagedPreview,
  candidate_total: 6,
  candidate_pending: 6,
  results: ["a", "b"].flatMap((instanceId) =>
    [10, 12, 15].map((frame) => ({
      ...stagedPreview.results[0],
      instance_id: instanceId,
      frame_index: frame,
      manual_protected: instanceId === "a" && frame === 12,
    })),
  ),
};
const localDecision = {
  instance_ids: ["a"],
  from_frame: 10,
  to_frame: 12,
  decision: "accept" as const,
};

async function ownedReviewStore(twoJobs = false) {
  vi.mocked(videoTrackerApi.reviewable).mockResolvedValue(
    twoJobs ? [secondReviewJob, reviewableJob] : [reviewableJob],
  );
  vi.mocked(videoTrackerApi.preview).mockImplementation(async (jobId) => ({
    ...multiPreview,
    job_id: jobId,
  }));
  vi.mocked(videoTrackerApi.get).mockImplementation(async (jobId) =>
    jobId === secondReviewJob.id ? secondReviewJob : reviewableJob,
  );
  const store = createStore();
  let snapshot = emptySnapshot();
  store.subscribe((next) => {
    snapshot = next;
  });
  await store.restoreReviewable("task-1");
  return { store, snapshot: () => snapshot };
}

describe("TrackerJobStore review ownership", () => {
  it("clamps explicit window edits to the job boundary, without reordering an empty window", async () => {
    const { store, snapshot } = await ownedReviewStore();
    store.setReviewWindow(-10, 90);
    expect(snapshot().activeReview!.scope).toMatchObject({ fromFrame: 0, toFrame: 20 });
    store.setReviewWindow(15, 10);
    expect(snapshot().activeReview!.scope).toMatchObject({ fromFrame: 15, toFrame: 10 });
    expect(snapshot().activeReview!.selectedPending).toBe(0);
    store.setReviewWindow(Number.NaN, 15);
    expect(snapshot().activeReview!.scope).toMatchObject({ fromFrame: 15, toFrame: 10 });
  });
  it("initializes deterministically once, remembers each scope, and never steals the selected job", async () => {
    const { store, snapshot } = await ownedReviewStore(true);
    expect(snapshot().activeReviewJobId).toBe("job-1");
    store.setReviewInstances(["a"]);
    store.setReviewWindow(11, 13);
    const firstIntent = snapshot().activeReview!.intentKey;
    store.chooseReviewJob("job-2");
    store.setReviewInstances(["b"]);
    store.setReviewWindow(14, 15);
    await store.refreshReview("job-1");
    expect(snapshot().activeReview).toMatchObject({
      jobId: "job-2",
      scope: { instanceIds: ["b"], fromFrame: 14, toFrame: 15 },
    });
    store.chooseReviewJob("job-1");
    expect(snapshot().activeReview!.scope).toMatchObject({
      instanceIds: ["a"],
      fromFrame: 11,
      toFrame: 13,
    });
    expect(store.isReviewIntentCurrent(firstIntent)).toBe(false);
    const stableIntent = snapshot().activeReview!.intentKey;
    await store.refreshReview("job-1");
    expect(store.isReviewIntentCurrent(stableIntent)).toBe(true);
    store.chooseReviewJob("job-2");
    vi.mocked(videoTrackerApi.discard).mockResolvedValue({ ...reviewableJob, status: "discarded" });
    await store.discard("job-1");
    expect(snapshot().activeReview!.jobId).toBe("job-2");
    expect(snapshot().reviewScopes["job-1"]).toBeUndefined();
  });

  it("intersects a newer candidate revision without adding targets or widening the window", async () => {
    const { store, snapshot } = await ownedReviewStore();
    store.setReviewInstances(["a"]);
    store.setReviewWindow(11, 13);
    const oldIntent = snapshot().activeReview!.intentKey;
    vi.mocked(videoTrackerApi.get).mockResolvedValue({ ...reviewableJob, revision: 2 });
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      ...multiPreview,
      job_revision: 2,
      candidate_pending: 2,
      results: [
        { ...multiPreview.results[0], instance_id: "b", frame_index: 14 },
        { ...multiPreview.results[0], instance_id: "c", frame_index: 20 },
      ],
    });
    await store.refreshReview("job-1");
    expect(snapshot().activeReview).toMatchObject({
      scope: { instanceIds: [], fromFrame: 11, toFrame: 13 },
      selectedPending: 0,
      jobPending: 2,
      availableInstanceIds: ["b", "c"],
      remainingIntervals: [
        { fromFrame: 14, toFrame: 14 },
        { fromFrame: 20, toFrame: 20 },
      ],
    });
    expect(store.isReviewIntentCurrent(oldIntent)).toBe(false);
    expect(await store.decide("job-1", { ...localDecision, instance_ids: [] })).toEqual({
      ok: false,
      reason: "empty_selection",
    });
    expect(videoTrackerApi.decide).not.toHaveBeenCalled();
    await store.refreshReview("job-1");
    expect(snapshot().activeReview!.scope.instanceIds).toEqual([]);
  });

  it("partial accept updates pending counts and source versions while retaining the explicit scope", async () => {
    const { store, snapshot } = await ownedReviewStore();
    store.setReviewInstances(["a"]);
    store.setReviewWindow(10, 12);
    const updated = { ...reviewableJob, revision: 2, status: "partially_reviewed" as const };
    vi.mocked(videoTrackerApi.decide).mockResolvedValue(updated);
    vi.mocked(videoTrackerApi.get).mockResolvedValue(updated);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({
      ...multiPreview,
      job_revision: 2,
      expected_source_versions: { "annotation-1": 7 },
      candidate_pending: 4,
      candidate_accepted: 2,
      results: multiPreview.results.filter(
        (row) => row.instance_id !== "a" || row.frame_index > 12,
      ),
    });
    expect(await store.decide("job-1", localDecision)).toEqual({ ok: true });
    expect(snapshot().activeReview).toMatchObject({
      scope: { instanceIds: ["a"], fromFrame: 10, toFrame: 12 },
      selectedPending: 0,
      jobPending: 4,
      preview: { job_revision: 2, expected_source_versions: { "annotation-1": 7 } },
    });
    expect(snapshot().submitting).toEqual({});
  });

  it("a delayed partial decision updates its own job without reselecting it", async () => {
    const { store, snapshot } = await ownedReviewStore(true);
    const gate = deferred<VideoTrackerJob>();
    vi.mocked(videoTrackerApi.decide).mockReturnValue(gate.promise);
    const pending = store.decide("job-1", localDecision);
    store.setReviewInstances(["b"]);
    store.setReviewWindow(15, 18);
    store.chooseReviewJob("job-2");
    const secondIntent = snapshot().activeReview!.intentKey;
    const updated = { ...reviewableJob, revision: 2, status: "partially_reviewed" as const };
    vi.mocked(videoTrackerApi.get).mockResolvedValue(updated);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...multiPreview, job_revision: 2 });
    gate.resolve(updated);
    expect(await pending).toEqual({ ok: true });
    expect(store.isReviewIntentCurrent(secondIntent)).toBe(true);
    store.chooseReviewJob("job-1");
    expect(snapshot().activeReview!.scope).toMatchObject({
      instanceIds: ["b"],
      fromFrame: 15,
      toFrame: 18,
    });
  });

  it("ignores old task A decisions after A→B→A and cannot clear a newer submission", async () => {
    const { store, snapshot } = await ownedReviewStore();
    const invalidator = vi.fn();
    store.setAnnotationInvalidator(invalidator);
    const push = vi.spyOn(useToastStore.getState(), "push");
    const oldGate = deferred<VideoTrackerJob>();
    const newGate = deferred<VideoTrackerJob>();
    vi.mocked(videoTrackerApi.decide)
      .mockReturnValueOnce(oldGate.promise)
      .mockReturnValueOnce(newGate.promise);
    const oldRequest = store.decide("job-1", localDecision);
    await store.restoreReviewable("task-2");
    await store.restoreReviewable("task-1");
    const newRequest = store.decide("job-1", localDecision);
    const currentIntent = snapshot().activeReview!.intentKey;
    oldGate.resolve({ ...reviewableJob, revision: 2, status: "partially_reviewed" });
    expect(await oldRequest).toEqual({ ok: false, reason: "stale_request" });
    expect(snapshot().submitting["job-1"]).toBe(true);
    expect(snapshot().candidates["job-1"].job_revision).toBe(1);
    expect(store.isReviewIntentCurrent(currentIntent)).toBe(true);
    expect(invalidator).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    newGate.resolve({ ...reviewableJob, status: "accepted" });
    expect(await newRequest).toEqual({ ok: true });
    expect(snapshot().submitting).toEqual({});
    push.mockRestore();
  });

  it("restores a fresh A session while its earlier hydration is still pending", async () => {
    const gate = deferred<VideoTrackerJob[]>();
    vi.mocked(videoTrackerApi.reviewable)
      .mockReturnValueOnce(gate.promise)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([secondReviewJob]);
    vi.mocked(videoTrackerApi.preview).mockImplementation(async (jobId) => ({
      ...multiPreview,
      job_id: jobId,
    }));
    const store = createStore();
    let snapshot = emptySnapshot();
    store.subscribe((next) => {
      snapshot = next;
    });
    const oldHydration = store.restoreReviewable("task-1", "old-token");
    await store.restoreReviewable("task-2");
    await store.restoreReviewable("task-1");
    gate.resolve([reviewableJob]);
    await oldHydration;
    expect(Object.keys(snapshot.jobs)).toEqual(["job-2"]);
    expect(snapshot.activeReview!.jobId).toBe("job-2");
    expect(videoTrackerApi.reviewable).toHaveBeenCalledTimes(3);
  });

  it("rejects both out-of-order refreshes and later reads of an older revision", async () => {
    const { store, snapshot } = await ownedReviewStore();
    const oldPreview = deferred<VideoTrackerJobPreview>();
    vi.mocked(videoTrackerApi.preview)
      .mockReturnValueOnce(oldPreview.promise)
      .mockResolvedValueOnce({ ...multiPreview, job_revision: 3 });
    vi.mocked(videoTrackerApi.get)
      .mockResolvedValueOnce({ ...reviewableJob, revision: 2 })
      .mockResolvedValueOnce({ ...reviewableJob, revision: 3 });
    const staleRefresh = store.refreshReview("job-1");
    await store.refreshReview("job-1");
    oldPreview.resolve({ ...multiPreview, job_revision: 2 });
    await staleRefresh;
    vi.mocked(videoTrackerApi.get).mockResolvedValue({ ...reviewableJob, revision: 2 });
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...multiPreview, job_revision: 2 });
    await store.refreshReview("job-1");
    expect(snapshot().candidates["job-1"].job_revision).toBe(3);
    expect(snapshot().jobs["job-1"].revision).toBe(3);
  });

  it("keeps a newer preview that arrived before an older decision response", async () => {
    const { store, snapshot } = await ownedReviewStore();
    const gate = deferred<VideoTrackerJob>();
    vi.mocked(videoTrackerApi.decide).mockReturnValueOnce(gate.promise);
    const pending = store.decide("job-1", localDecision);
    const latestJob = { ...reviewableJob, status: "partially_reviewed" as const, revision: 3 };
    vi.mocked(videoTrackerApi.get).mockResolvedValue(latestJob);
    vi.mocked(videoTrackerApi.preview).mockResolvedValue({ ...multiPreview, job_revision: 3 });
    await store.refreshReview("job-1");
    const intent = snapshot().activeReview!.intentKey;
    gate.resolve({ ...latestJob, revision: 2 });
    expect(await pending).toEqual({ ok: true });
    expect(snapshot().jobs["job-1"].revision).toBe(3);
    expect(snapshot().candidates["job-1"].job_revision).toBe(3);
    expect(store.isReviewIntentCurrent(intent)).toBe(true);
    expect(snapshot().submitting).toEqual({});
  });

  it.each(["discard", "cancel"] as const)(
    "a late preview cannot revive candidates after %s retires its generation",
    async (action) => {
      vi.useFakeTimers();
      const store = createStore();
      let snapshot = emptySnapshot();
      store.subscribe((next) => {
        snapshot = next;
      });
      store.addJob(runningCorrectionJob, "token");
      const previewGate = deferred<VideoTrackerJobPreview>();
      vi.mocked(videoTrackerApi.preview).mockReturnValueOnce(previewGate.promise);
      MockWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({ type: "job_completed" }),
      } as MessageEvent);
      const terminal = {
        ...runningCorrectionJob,
        status: (action === "discard" ? "discarded" : "cancelled") as VideoTrackerJob["status"],
      };
      vi.mocked(videoTrackerApi[action]).mockResolvedValue(terminal);
      await store[action](runningCorrectionJob.id);
      previewGate.resolve({ ...multiPreview, job_id: runningCorrectionJob.id });
      await Promise.resolve();
      expect(snapshot.candidates).toEqual({});
      expect(snapshot.activeReview).toBeNull();
      vi.advanceTimersByTime(1500);
      expect(snapshot.jobs).toEqual({});
    },
  );

  it("keeps the current scope after a failed request and preserves the QC selector union", async () => {
    const { store, snapshot } = await ownedReviewStore();
    store.setReviewInstances(["a"]);
    store.setReviewWindow(11, 12);
    const intent = snapshot().activeReview!.intentKey;
    vi.mocked(videoTrackerApi.decide).mockRejectedValue(new ApiError(500, "unavailable"));
    await store.decide("job-1", localDecision);
    expect(store.isReviewIntentCurrent(intent)).toBe(true);
    expect(snapshot().submitting).toEqual({});
    vi.mocked(videoTrackerApi.decide).mockRejectedValue(
      new ApiError(409, "manual", { reason: "manual_keyframe_protected" }),
    );
    expect(
      await store.decide("job-1", {
        decision: "accept",
        qc_issue_id: "qc-1",
        candidate_digest: "digest",
      }),
    ).toMatchObject({ reason: "manual_keyframe_protected" });
    expect(videoTrackerApi.decide).toHaveBeenLastCalledWith("job-1", {
      decision: "accept",
      qc_issue_id: "qc-1",
      candidate_digest: "digest",
      expected_source_versions: { "annotation-1": 1 },
      job_revision: 1,
    });
  });

  it("task launch ownership expires on task ABA and on leaving video", async () => {
    const { store } = await ownedReviewStore();
    const epoch = store.captureTaskEpoch("task-1");
    store.scopeToTask("task-2");
    store.scopeToTask("task-1");
    expect(store.isTaskEpochCurrent("task-1", epoch)).toBe(false);
    const latest = store.captureTaskEpoch("task-1");
    store.scopeToTask(null);
    expect(store.isTaskEpochCurrent("task-1", latest)).toBe(false);
  });

  it("suppresses an old-task manual conflict, including its error and finally effects", async () => {
    const { store, snapshot } = await ownedReviewStore();
    const gate = deferred<VideoTrackerJob>();
    vi.mocked(videoTrackerApi.decide).mockReturnValueOnce(gate.promise);
    const push = vi.spyOn(useToastStore.getState(), "push");
    const pending = store.decide("job-1", localDecision);
    await store.restoreReviewable("task-2");
    gate.reject(new ApiError(409, "manual", { reason: "manual_keyframe_protected" }));
    expect(await pending).toEqual({ ok: false, reason: "stale_request" });
    expect(snapshot().jobs).toEqual({});
    expect(snapshot().submitting).toEqual({});
    expect(push).not.toHaveBeenCalled();
    push.mockRestore();
  });

  it("does not admit a refresh that finishes after terminal cleanup", async () => {
    vi.useFakeTimers();
    const { store, snapshot } = await ownedReviewStore();
    const gate = deferred<VideoTrackerJobPreview>();
    vi.mocked(videoTrackerApi.preview).mockReturnValueOnce(gate.promise);
    const pending = store.refreshReview("job-1");
    vi.mocked(videoTrackerApi.discard).mockResolvedValue({ ...reviewableJob, status: "discarded" });
    await store.discard("job-1");
    vi.advanceTimersByTime(1500);
    gate.resolve({ ...multiPreview, job_revision: 99 });
    await pending;
    expect(snapshot().jobs).toEqual({});
    expect(snapshot().candidates).toEqual({});
    expect(snapshot().activeReview).toBeNull();
  });
});

describe("useVideoTrackerJobs launch ownership", () => {
  function launchHarness() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children);
    return {
      ...renderHook(({ taskId }) => useVideoTrackerJobs(taskId), {
        initialProps: { taskId: "task-1" },
        wrapper,
      }),
      queryClient,
    };
  }

  it("rejects a late launch on task ABA, then polls an admitted launch without a token", async () => {
    vi.useFakeTimers();
    const { result, rerender, unmount, queryClient } = launchHarness();
    const gate = deferred<VideoTrackerJob>();
    vi.mocked(videoTrackerApi.track)
      .mockReturnValueOnce(gate.promise)
      .mockResolvedValueOnce(runningJob);
    const payload = {
      from_frame: 0,
      to_frame: 20,
      model_key: "sam2_video",
      direction: "forward" as const,
    };
    let pending!: Promise<VideoTrackerJob>;
    act(() => {
      pending = result.current.track("task-1", payload);
    });
    rerender({ taskId: "task-2" });
    rerender({ taskId: "task-1" });
    await act(async () => {
      gate.resolve(runningJob);
      await pending;
    });
    expect(result.current.jobs).toEqual({});
    await act(async () => {
      await result.current.track("task-1", payload);
    });
    expect(result.current.jobs[runningJob.id]?.status).toBe("running");
    vi.mocked(videoTrackerApi.get).mockResolvedValue(runningJob);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(videoTrackerApi.get).toHaveBeenCalledWith(runningJob.id);
    unmount();
    queryClient.clear();
  });
});
