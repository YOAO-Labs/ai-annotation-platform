import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { maskQcApi, type MaskQcIssue } from "@/api/maskQc";
import type { AnnotationResponse } from "@/types";
import type { VideoFrameSeekResult, VideoStageControls } from "../stage/videoStageControls";
import {
  assertMaskQcLocalAiCandidate,
  assertMaskQcTrackerCandidate,
  collectMaskQcTrackerCandidates,
  maskQcNavigationContextMatches,
  maskQcReadyContextMatches,
  MaskQcNavigationGeneration,
  MaskQcNavigationStaleError,
  useMaskQcReview,
} from "./useMaskQcReview";

describe("Mask QC waits for actual frame presentation", () => {
  const issue = {
    id: "issue-1",
    task_id: "task-1",
    annotation_id: "annotation-1",
    annotation_version: 1,
    frame_start: 17,
    source: {},
    region_bbox: { x0: 0.1, y0: 0.2, x1: 0.5, y1: 0.6 },
  } as MaskQcIssue;
  function setup(isSourceReady?: () => boolean) {
    let finish!: (value: VideoFrameSeekResult) => void;
    const seek = new Promise<VideoFrameSeekResult>((resolve) => {
      finish = resolve;
    });
    const seekToFrameReady = vi.fn(() => seek);
    const focusRegion = vi.fn();
    const setSelectedId = vi.fn();
    const setFrameIndex = vi.fn();
    const compare = vi
      .spyOn(maskQcApi, "compare")
      .mockRejectedValue(new Error("comparison unavailable in navigation test"));
    const view = renderHook(() =>
      useMaskQcReview({
        enabled: true,
        taskId: "task-1",
        annotationsReady: true,
        annotations: [{ id: "annotation-1", version: 1 }] as AnnotationResponse[],
        visibleAnnotationIds: new Set(["annotation-1"]),
        selectedId: "annotation-1",
        isVideoTask: true,
        videoManifestReady: true,
        frameIndex: 17,
        stageGeom: { imgW: 1000, imgH: 500, vpSize: { w: 1000, h: 500 } },
        workerPool: undefined,
        getAiCandidate: () => null,
        getTrackerCandidates: () => [],
        videoControlsRef: {
          current: {
            seekToFrameReady,
            focusRegion,
            isSourceReady,
          } as unknown as VideoStageControls,
        },
        selectTask: async () => true,
        setSelectedId,
        setFrameIndex,
        setVp: vi.fn(),
      }),
    );
    return {
      ...view,
      finish,
      seekToFrameReady,
      setSelectedId,
      setFrameIndex,
      focusRegion,
      compare,
    };
  }

  it("metadata success waits for the current original lease and native source", async () => {
    let ready = false;
    const view = setup(() => ready);
    let navigation!: Promise<void>;
    act(() => {
      navigation = view.result.current.navigate(issue);
    });
    await waitFor(() => expect(view.result.current.phase).toBe("waiting_manifest"));
    expect(view.seekToFrameReady).not.toHaveBeenCalled();
    ready = true;
    await waitFor(() => expect(view.seekToFrameReady).toHaveBeenCalledOnce());
    await act(async () => {
      view.finish({ status: "unavailable", frameIndex: 17, source: null });
      await navigation;
    });
    expect(view.setSelectedId).not.toHaveBeenCalled();
    view.compare.mockRestore();
  });

  it.each([
    { status: "cancelled", frameIndex: 17, source: null },
    { status: "timeout", frameIndex: 17, source: null },
    { status: "unavailable", frameIndex: 17, source: null },
    { status: "ready", frameIndex: 16, source: "webcodecs" },
  ] satisfies VideoFrameSeekResult[])(
    "rejects $status at F$frameIndex before selecting or comparing masks",
    async (value) => {
      const view = setup();
      let navigation!: Promise<void>;
      act(() => {
        navigation = view.result.current.navigate(issue);
      });
      await waitFor(() => expect(view.seekToFrameReady).toHaveBeenCalledOnce());
      expect(view.setSelectedId).not.toHaveBeenCalled();
      await act(async () => {
        view.finish(value);
        await navigation;
      });
      expect(view.result.current.phase).toBe("error");
      expect(view.setSelectedId).not.toHaveBeenCalled();
      expect(view.setFrameIndex).not.toHaveBeenCalled();
      expect(view.focusRegion).not.toHaveBeenCalled();
      expect(view.compare).not.toHaveBeenCalled();
      view.compare.mockRestore();
    },
  );

  it("an optimistic matching frame cannot advance until the checked request completes", async () => {
    const view = setup();
    let navigation!: Promise<void>;
    act(() => {
      navigation = view.result.current.navigate(issue);
    });
    await waitFor(() => expect(view.seekToFrameReady).toHaveBeenCalledOnce());
    expect(view.result.current.phase).toBe("seeking_frame");
    expect(view.setSelectedId).not.toHaveBeenCalled();
    await act(async () => {
      view.finish({ status: "ready", frameIndex: 17, source: "webcodecs" });
      await navigation;
    });
    expect(view.setSelectedId).toHaveBeenCalledOnce();
    expect(view.setSelectedId).toHaveBeenCalledWith("annotation-1");
    expect(view.focusRegion).toHaveBeenCalledOnce();
    expect(view.focusRegion).toHaveBeenCalledWith(issue.region_bbox);
    expect(view.compare).toHaveBeenCalledOnce();
    view.compare.mockRestore();
  });
});

describe("MaskQcNavigationGeneration", () => {
  it("invalidates every phase of an older asynchronous navigation", () => {
    const generation = new MaskQcNavigationGeneration();
    const first = generation.next();
    const second = generation.next();
    expect(first.signal.aborted).toBe(true);
    expect(() => generation.assert(first.value)).toThrow(MaskQcNavigationStaleError);
    expect(() => generation.assert(second.value)).not.toThrow();
    generation.cancel();
    expect(second.signal.aborted).toBe(true);
    expect(() => generation.assert(second.value)).toThrow(MaskQcNavigationStaleError);
  });
});

describe("Mask QC candidate identity", () => {
  const issue = {
    task_id: "task-1",
    annotation_id: "annotation-1",
    annotation_version: 7,
  };

  it("绑定本地 AI 候选的 task、frame 与精修源版本", () => {
    const candidate = {
      taskId: "task-1",
      digest: "digest",
      rle: { encoding: "coco_rle" as const, size: [2, 2] as [number, number], counts: [4] },
      frameIndex: 3,
      refineSource: { annotationId: "annotation-1", sourceVersion: 7 },
    };
    expect(() => assertMaskQcLocalAiCandidate(candidate, issue, 3)).not.toThrow();
    expect(() =>
      assertMaskQcLocalAiCandidate({ ...candidate, taskId: "task-2" }, issue, 3),
    ).toThrow(/目标任务/);
    expect(() => assertMaskQcLocalAiCandidate({ ...candidate, frameIndex: 4 }, issue, 3)).toThrow(
      /当前帧/,
    );
    expect(() =>
      assertMaskQcLocalAiCandidate(
        {
          ...candidate,
          refineSource: { annotationId: "annotation-2", sourceVersion: 7 },
        },
        issue,
        3,
      ),
    ).toThrow(/目标标注版本/);
  });

  it("Tracker 候选必须精确匹配 annotation、frame、revision 与 digest", () => {
    const candidate = {
      key: "candidate",
      jobId: "job-1",
      jobRevision: 2,
      digest: "digest",
      frameIndex: 3,
      annotationId: "annotation-1",
      instanceId: "1",
      label: "Tracker",
    };
    expect(() => assertMaskQcTrackerCandidate(candidate, issue, 3)).not.toThrow();
    expect(() =>
      assertMaskQcTrackerCandidate({ ...candidate, annotationId: "annotation-2" }, issue, 3),
    ).toThrow(/目标标注/);
    expect(() => assertMaskQcTrackerCandidate({ ...candidate, frameIndex: 4 }, issue, 3)).toThrow(
      /目标帧/,
    );
    expect(() => assertMaskQcTrackerCandidate({ ...candidate, jobRevision: 0 }, issue, 3)).toThrow(
      /revision/,
    );
  });

  it("从多源 staged preview 只选择目标 annotation 的不可变候选", () => {
    const candidates = collectMaskQcTrackerCandidates(
      issue,
      3,
      {
        "job-1": {
          job_revision: 4,
          annotation_id: null,
          results: [
            {
              frame_index: 3,
              geometry: { type: "mask" },
              geometry_digest: "digest-a",
              target_annotation_id: "annotation-1",
              instance_id: "a",
            },
            {
              frame_index: 3,
              geometry: { type: "mask" },
              geometry_digest: "digest-b",
              target_annotation_id: "annotation-2",
              instance_id: "b",
            },
            {
              frame_index: 4,
              geometry: { type: "mask" },
              geometry_digest: "digest-late",
              target_annotation_id: "annotation-1",
              instance_id: "a",
            },
          ],
        } as never,
      },
      {
        "job-1": { taskId: "task-1", revision: 4, modelKey: "SAM2" },
      },
    );
    expect(candidates).toEqual([
      expect.objectContaining({
        jobId: "job-1",
        jobRevision: 4,
        digest: "digest-a",
        annotationId: "annotation-1",
        frameIndex: 3,
        instanceId: "a",
        label: "SAM2 · r4",
      }),
    ]);
  });
});

describe("maskQcReadyContextMatches", () => {
  const expected = {
    taskId: "task-1",
    annotationId: "annotation-1",
    annotationVersion: 7,
    frameIndex: 3,
  };
  const current = {
    taskId: "task-1",
    selectedId: "annotation-1",
    visibleAnnotationIds: new Set(["annotation-1"]),
    isVideoTask: true,
    frameIndex: 3,
    annotationVersion: 7,
  };

  it("任一 task/frame/selection/version 偏离都会让旧 compare 失效", () => {
    expect(maskQcReadyContextMatches(current, expected)).toBe(true);
    expect(maskQcReadyContextMatches({ ...current, taskId: "task-2" }, expected)).toBe(false);
    expect(maskQcReadyContextMatches({ ...current, selectedId: "annotation-2" }, expected)).toBe(
      false,
    );
    expect(maskQcReadyContextMatches({ ...current, frameIndex: 4 }, expected)).toBe(false);
    expect(maskQcReadyContextMatches({ ...current, annotationVersion: 8 }, expected)).toBe(false);
  });

  it("加载与回退区域期间也会校验 task/frame/selection", () => {
    expect(maskQcNavigationContextMatches(current, expected)).toBe(true);
    expect(maskQcNavigationContextMatches({ ...current, taskId: "task-2" }, expected)).toBe(false);
    expect(
      maskQcNavigationContextMatches({ ...current, selectedId: "annotation-2" }, expected),
    ).toBe(false);
    expect(maskQcNavigationContextMatches({ ...current, frameIndex: 4 }, expected)).toBe(false);
    const versionDrift = {
      ...current,
      annotationVersion: 99,
    };
    expect(maskQcNavigationContextMatches(versionDrift, expected)).toBe(true);
  });
});
