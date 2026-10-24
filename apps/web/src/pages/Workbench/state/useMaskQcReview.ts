import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type RefObject,
  type SetStateAction,
} from "react";
import type { AnnotationResponse } from "@/types";
import {
  maskQcApi,
  type MaskCompareBaseline,
  type MaskCompareResult,
  type MaskQcIssue,
} from "@/api/maskQc";
import type { CocoRle } from "../stage/shared/geometry/maskRle";
import { resolveVideoMaskTrackAtFrame } from "../stage/videoStageGeometry";
import type { Viewport } from "./useViewportTransform";
import { fitNormalizedRegion } from "../stage/shared/viewport/region";
import { MaskCompareTileStore } from "../stage/shared/maskCompareTileStore";
import type { RasterMaskWorkerPool } from "../stage/shared/rasterMaskWorkerPool";
import type { RasterMaskCompareMode } from "../stage/shared/rasterMaskWorkerProtocol";
import type { VideoStageControls } from "../stage/videoStageControls";
import type { VideoTrackerJobPreview } from "@/api/videoTracker";
import type { MaskFeedbackCompareLocator } from "@/api/feedbacks";

export type MaskQcNavigationPhase =
  | "idle"
  | "switching_task"
  | "waiting_task"
  | "waiting_annotations"
  | "waiting_manifest"
  | "seeking_frame"
  | "selecting_annotation"
  | "focusing_region"
  | "loading_compare"
  | "ready"
  | "error";

export class MaskQcNavigationStaleError extends Error {
  constructor() {
    super("Mask QC navigation generation is stale");
    this.name = "MaskQcNavigationStaleError";
  }
}

export class MaskQcNavigationGeneration {
  private value = 0;
  private controller: AbortController | null = null;

  next(): { value: number; signal: AbortSignal } {
    this.controller?.abort();
    this.controller = new AbortController();
    return { value: ++this.value, signal: this.controller.signal };
  }

  assert(value: number): void {
    if (value !== this.value || this.controller?.signal.aborted) {
      throw new MaskQcNavigationStaleError();
    }
  }

  cancel(): void {
    this.value += 1;
    this.controller?.abort();
    this.controller = null;
  }
}

interface ComparePayload {
  result: MaskCompareResult;
  currentRle: Awaited<ReturnType<typeof maskQcApi.content>>;
  baselineRle: Awaited<ReturnType<typeof maskQcApi.content>>;
}

export interface MaskQcReviewState {
  issue: MaskQcIssue | null;
  phase: MaskQcNavigationPhase;
  error: string | null;
  compare: MaskCompareResult | null;
  baseline: MaskCompareBaseline;
  mode: RasterMaskCompareMode;
  store: MaskCompareTileStore | null;
  trackerCandidate: MaskQcTrackerCandidate | null;
}

interface DynamicState {
  taskId: string | undefined;
  annotations: AnnotationResponse[] | undefined;
  annotationsReady: boolean;
  visibleAnnotationIds: ReadonlySet<string>;
  selectedId: string | null;
  isVideoTask: boolean;
  videoManifestReady: boolean;
  frameIndex: number;
  stageGeom: { imgW: number; imgH: number; vpSize: { w: number; h: number } };
  workerPool: RasterMaskWorkerPool | undefined;
  getAiCandidate: () => MaskQcLocalAiCandidate | null;
  getTrackerCandidates: (issue: MaskQcIssue, frameIndex: number) => MaskQcTrackerCandidate[];
}

export interface MaskQcLocalAiCandidate {
  taskId: string;
  digest: string;
  rle: CocoRle;
  frameIndex: number | null;
  refineSource?: { annotationId: string; sourceVersion: number };
}

export interface MaskQcTrackerCandidate {
  key: string;
  jobId: string;
  jobRevision: number;
  digest: string;
  frameIndex: number;
  annotationId: string;
  instanceId: string | null;
  label: string;
}

export function assertMaskQcLocalAiCandidate(
  candidate: MaskQcLocalAiCandidate,
  issue: Pick<MaskQcIssue, "task_id" | "annotation_id" | "annotation_version">,
  targetFrame: number | null,
): void {
  if (candidate.taskId !== issue.task_id) throw new Error("AI Mask 候选不属于目标任务");
  if (candidate.frameIndex !== targetFrame) throw new Error("AI Mask 候选不属于当前帧");
  if (
    candidate.refineSource &&
    (candidate.refineSource.annotationId !== issue.annotation_id ||
      candidate.refineSource.sourceVersion !== issue.annotation_version)
  )
    throw new Error("精修 AI 候选不属于目标标注版本");
}

function issueBaseline(issue: MaskQcIssue, isVideo: boolean): MaskCompareBaseline {
  const source = issue.source;
  if (
    typeof source.candidate_job_id === "string" &&
    Number.isInteger(source.candidate_job_revision) &&
    typeof source.candidate_digest === "string"
  )
    return "tracker_candidate";
  if (issue.annotation_version > 1) return "previous_version";
  return isVideo ? "neighbor_keyframe" : "previous_version";
}

function trackerCandidateFromIssue(
  issue: MaskQcIssue,
  frameIndex: number,
): MaskQcTrackerCandidate | null {
  const source = issue.source;
  if (
    typeof source.candidate_job_id !== "string" ||
    !Number.isInteger(source.candidate_job_revision) ||
    typeof source.candidate_digest !== "string"
  )
    return null;
  const jobRevision = source.candidate_job_revision as number;
  const instanceId =
    typeof source.candidate_instance_id === "string" ? source.candidate_instance_id : null;
  return {
    key: `${source.candidate_job_id}:${jobRevision}:${source.candidate_digest}:${frameIndex}:${instanceId ?? "primary"}`,
    jobId: source.candidate_job_id,
    jobRevision,
    digest: source.candidate_digest,
    frameIndex,
    annotationId: issue.annotation_id,
    instanceId,
    label: `问题证据 · r${jobRevision}`,
  };
}

export function assertMaskQcTrackerCandidate(
  candidate: MaskQcTrackerCandidate,
  issue: Pick<MaskQcIssue, "annotation_id">,
  targetFrame: number,
): void {
  if (candidate.annotationId !== issue.annotation_id) {
    throw new Error("Tracker 候选不属于目标标注");
  }
  if (candidate.frameIndex !== targetFrame) throw new Error("Tracker 候选不属于目标帧");
  if (!Number.isInteger(candidate.jobRevision) || candidate.jobRevision < 1) {
    throw new Error("Tracker 候选缺少有效 revision");
  }
  if (!candidate.digest) throw new Error("Tracker 候选缺少不可变 digest");
}

export function collectMaskQcTrackerCandidates(
  issue: Pick<MaskQcIssue, "task_id" | "annotation_id">,
  targetFrame: number,
  previews: Record<string, VideoTrackerJobPreview>,
  jobs: Record<
    string,
    {
      taskId: string;
      revision?: number;
      modelKey: string;
    }
  >,
): MaskQcTrackerCandidate[] {
  const candidates = new Map<string, MaskQcTrackerCandidate>();
  for (const [jobId, preview] of Object.entries(previews)) {
    const job = jobs[jobId];
    if (job?.taskId !== issue.task_id) continue;
    const jobRevision = preview.job_revision ?? job.revision;
    if (!Number.isInteger(jobRevision) || (jobRevision ?? 0) < 1) continue;
    for (const result of preview.results) {
      if (
        result.frame_index !== targetFrame ||
        result.outside ||
        (result.geometry as { type?: string } | null)?.type !== "mask" ||
        !result.geometry_digest
      )
        continue;
      const annotationIds = [
        result.target_annotation_id,
        result.source_annotation_id,
        preview.annotation_id,
      ].filter((value): value is string => typeof value === "string");
      if (!annotationIds.includes(issue.annotation_id)) continue;
      const instanceId = result.instance_id ?? null;
      const key = `${jobId}:${jobRevision}:${result.geometry_digest}:${targetFrame}:${instanceId ?? "primary"}`;
      candidates.set(key, {
        key,
        jobId,
        jobRevision: jobRevision!,
        digest: result.geometry_digest,
        frameIndex: targetFrame,
        annotationId: issue.annotation_id,
        instanceId,
        label: `${job.modelKey || "Tracker"} · r${jobRevision}`,
      });
    }
  }
  return [...candidates.values()];
}

export function maskQcReadyContextMatches(
  input: {
    taskId: string | undefined;
    selectedId: string | null;
    visibleAnnotationIds: ReadonlySet<string>;
    isVideoTask: boolean;
    frameIndex: number;
    annotationVersion: number | null;
  },
  expected: {
    taskId: string;
    annotationId: string;
    annotationVersion: number;
    frameIndex: number;
  },
): boolean {
  return (
    maskQcNavigationContextMatches(input, expected) &&
    input.annotationVersion === expected.annotationVersion
  );
}

export function maskQcNavigationContextMatches(
  input: {
    taskId: string | undefined;
    selectedId: string | null;
    visibleAnnotationIds: ReadonlySet<string>;
    isVideoTask: boolean;
    frameIndex: number;
  },
  expected: {
    taskId: string;
    annotationId: string;
    frameIndex: number;
  },
): boolean {
  return (
    input.taskId === expected.taskId &&
    input.selectedId === expected.annotationId &&
    input.visibleAnnotationIds.has(expected.annotationId) &&
    (!input.isVideoTask || input.frameIndex === expected.frameIndex)
  );
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "Mask 质检定位失败";
}

async function waitFor(
  read: () => boolean,
  generation: MaskQcNavigationGeneration,
  token: number,
  timeoutMs = 12_000,
): Promise<void> {
  const started = Date.now();
  while (!read()) {
    generation.assert(token);
    if (Date.now() - started >= timeoutMs) throw new Error("等待工作台数据就绪超时");
    await new Promise<void>((resolve) => setTimeout(resolve, 25));
  }
  generation.assert(token);
}

export function useMaskQcReview(params: {
  enabled: boolean;
  taskId: string | undefined;
  annotations: AnnotationResponse[] | undefined;
  annotationsReady: boolean;
  visibleAnnotationIds: ReadonlySet<string>;
  selectedId: string | null;
  isVideoTask: boolean;
  videoManifestReady: boolean;
  frameIndex: number;
  stageGeom: DynamicState["stageGeom"];
  workerPool: RasterMaskWorkerPool | undefined;
  getAiCandidate: () => MaskQcLocalAiCandidate | null;
  getTrackerCandidates: (issue: MaskQcIssue, frameIndex: number) => MaskQcTrackerCandidate[];
  videoControlsRef: RefObject<VideoStageControls | null>;
  selectTask: (taskId: string, options?: { signal?: AbortSignal }) => Promise<boolean>;
  setSelectedId: (annotationId: string | null) => void;
  setFrameIndex: (frameIndex: number) => void;
  setVp: Dispatch<SetStateAction<Viewport>>;
}) {
  const dynamicRef = useRef<DynamicState>(params);
  dynamicRef.current = params;
  const generationRef = useRef(new MaskQcNavigationGeneration());
  const navigationTaskRef = useRef<{
    origin: string | undefined;
    target: string;
    reached: boolean;
  } | null>(null);
  const storeRef = useRef<MaskCompareTileStore | null>(null);
  const [state, setState] = useState<MaskQcReviewState>({
    issue: null,
    phase: "idle",
    error: null,
    compare: null,
    baseline: "previous_version",
    mode: "overlay",
    store: null,
    trackerCandidate: null,
  });

  const disposeStore = useCallback(() => {
    storeRef.current?.dispose();
    storeRef.current = null;
  }, []);

  const installStore = useCallback(
    (payload: ComparePayload, mode: RasterMaskCompareMode) => {
      const backend = dynamicRef.current.workerPool;
      if (!backend) throw new Error("Raster Mask Worker 不可用");
      disposeStore();
      const store = new MaskCompareTileStore({
        backend,
        current: { sha256: payload.result.current.digest, rle: payload.currentRle },
        baseline: { sha256: payload.result.baseline.digest, rle: payload.baselineRle },
        mode,
        scopeKey: `${payload.result.current.annotation_id}:${payload.result.current.annotation_version}`,
        display: {
          annotationId: payload.result.current.annotation_id,
          hideAiCandidate: payload.result.baseline_kind === "ai_candidate",
          hideTrackerCandidate: payload.result.baseline_kind === "tracker_candidate",
        },
      });
      storeRef.current = store;
      return store;
    },
    [disposeStore],
  );

  const setMode = useCallback((mode: RasterMaskCompareMode) => {
    try {
      const store = storeRef.current;
      store?.setMode(mode);
      setState((current) => ({ ...current, mode, store, error: null }));
    } catch (error) {
      setState((current) => ({
        ...current,
        mode,
        store: null,
        phase: "error",
        error: errorMessage(error),
      }));
    }
  }, []);

  const clear = useCallback(() => {
    generationRef.current.cancel();
    navigationTaskRef.current = null;
    disposeStore();
    setState({
      issue: null,
      phase: "idle",
      error: null,
      compare: null,
      baseline: "previous_version",
      mode: "overlay",
      store: null,
      trackerCandidate: null,
    });
  }, [disposeStore]);

  const navigate = useCallback(
    async (
      issue: MaskQcIssue,
      requestedBaseline?: MaskCompareBaseline,
      requestedTrackerCandidate?: MaskQcTrackerCandidate | null,
      requestedMode: RasterMaskCompareMode = "overlay",
      expectedDigests?: { current: string; baseline: string },
    ) => {
      const generation = generationRef.current;
      const token = generation.next();
      let baseline = requestedBaseline ?? "previous_version";
      let trackerCandidate = requestedTrackerCandidate ?? null;
      let navigationContextReady = false;
      const targetFrame = issue.frame_start ?? 0;
      const liveNavigationContextMatches = () =>
        maskQcNavigationContextMatches(
          {
            taskId: dynamicRef.current.taskId,
            selectedId: dynamicRef.current.selectedId,
            visibleAnnotationIds: dynamicRef.current.visibleAnnotationIds,
            isVideoTask: dynamicRef.current.isVideoTask,
            frameIndex: dynamicRef.current.frameIndex,
          },
          {
            taskId: issue.task_id,
            annotationId: issue.annotation_id,
            frameIndex: targetFrame,
          },
        );
      disposeStore();
      navigationTaskRef.current = {
        origin: dynamicRef.current.taskId,
        target: issue.task_id,
        reached: dynamicRef.current.taskId === issue.task_id,
      };
      setState({
        issue,
        phase: "switching_task",
        error: null,
        compare: null,
        baseline,
        mode: requestedMode,
        store: null,
        trackerCandidate,
      });
      try {
        const allowed = await params.selectTask(issue.task_id, { signal: token.signal });
        generation.assert(token.value);
        if (!allowed) throw new Error("当前 Mask 编辑未完成，已取消质检定位");
        setState((current) => ({ ...current, phase: "waiting_task" }));
        await waitFor(() => dynamicRef.current.taskId === issue.task_id, generation, token.value);
        if (navigationTaskRef.current?.target === issue.task_id) {
          navigationTaskRef.current.reached = true;
        }

        setState((current) => ({ ...current, phase: "waiting_annotations" }));
        await waitFor(() => dynamicRef.current.annotationsReady, generation, token.value);
        const annotation = dynamicRef.current.annotations?.find(
          (item) => item.id === issue.annotation_id,
        );
        if (!annotation) throw new Error("质检问题引用的标注已不存在");
        if (!dynamicRef.current.visibleAnnotationIds.has(issue.annotation_id)) {
          throw new Error("目标标注当前被隐藏，请先关闭孤儿标注过滤");
        }

        baseline = requestedBaseline ?? issueBaseline(issue, dynamicRef.current.isVideoTask);
        if (baseline === "tracker_candidate") {
          trackerCandidate =
            requestedTrackerCandidate ??
            trackerCandidateFromIssue(issue, targetFrame) ??
            dynamicRef.current.getTrackerCandidates(issue, targetFrame)[0] ??
            null;
          if (!trackerCandidate) throw new Error("当前没有与该标注和帧匹配的 Tracker 候选");
          assertMaskQcTrackerCandidate(trackerCandidate, issue, targetFrame);
        } else {
          trackerCandidate = null;
        }
        setState((current) => ({ ...current, baseline, trackerCandidate }));
        if (dynamicRef.current.isVideoTask) {
          setState((current) => ({ ...current, phase: "waiting_manifest" }));
          await waitFor(() => dynamicRef.current.videoManifestReady, generation, token.value);
          await waitFor(() => params.videoControlsRef.current !== null, generation, token.value);
          await waitFor(
            () =>
              params.videoControlsRef.current?.isSourceReady?.() ??
              params.videoControlsRef.current !== null,
            generation,
            token.value,
          );
          setState((current) => ({ ...current, phase: "seeking_frame" }));
          const frameResult = await params.videoControlsRef.current!.seekToFrameReady(targetFrame, {
            recordHistory: false,
          });
          generation.assert(token.value);
          if (frameResult.status !== "ready" || frameResult.frameIndex !== targetFrame) {
            throw new Error(
              frameResult.status === "timeout"
                ? "源帧准备超时，请重新定位该问题"
                : frameResult.status === "cancelled"
                  ? "源帧定位已取消，请重新定位该问题"
                  : "目标源帧尚未就绪，请重新定位该问题",
            );
          }
          await waitFor(
            () => dynamicRef.current.frameIndex === targetFrame,
            generation,
            token.value,
          );
        }

        setState((current) => ({ ...current, phase: "selecting_annotation" }));
        params.setSelectedId(issue.annotation_id);
        await waitFor(
          () => dynamicRef.current.selectedId === issue.annotation_id,
          generation,
          token.value,
        );

        if (issue.region_bbox) {
          setState((current) => ({ ...current, phase: "focusing_region" }));
          if (dynamicRef.current.isVideoTask) {
            params.videoControlsRef.current?.focusRegion(issue.region_bbox);
          } else {
            await waitFor(
              () => {
                const { imgW, imgH, vpSize } = dynamicRef.current.stageGeom;
                return imgW > 0 && imgH > 0 && vpSize.w > 0 && vpSize.h > 0;
              },
              generation,
              token.value,
            );
            const { imgW, imgH, vpSize } = dynamicRef.current.stageGeom;
            params.setVp((current) =>
              fitNormalizedRegion(
                current,
                issue.region_bbox!,
                { width: imgW, height: imgH },
                { width: vpSize.w, height: vpSize.h },
              ),
            );
          }
        }
        generation.assert(token.value);
        navigationContextReady = true;

        setState((current) => ({ ...current, phase: "loading_compare" }));
        if (baseline === "ai_candidate") {
          const candidate = dynamicRef.current.getAiCandidate();
          if (!candidate) throw new Error("当前没有可用的本地 AI Mask 候选");
          assertMaskQcLocalAiCandidate(
            candidate,
            issue,
            dynamicRef.current.isVideoTask ? targetFrame : null,
          );
          const currentAnnotation = dynamicRef.current.annotations?.find(
            (item) => item.id === issue.annotation_id,
          );
          if (!currentAnnotation || (currentAnnotation.version ?? 1) !== issue.annotation_version) {
            throw new Error("AI 候选只能与当前 Mask 版本对比");
          }
          let currentDigest: string;
          let currentSource = currentAnnotation.source;
          let currentState = "exact";
          if (currentAnnotation.geometry.type === "raster_mask") {
            currentDigest = currentAnnotation.geometry.mask.sha256;
          } else if (currentAnnotation.geometry.type === "video_track_mask") {
            const resolved = resolveVideoMaskTrackAtFrame(currentAnnotation.geometry, targetFrame);
            if (!resolved) throw new Error("当前视频帧没有 Mask 内容");
            currentDigest = resolved.mask.sha256;
            currentSource = resolved.source;
            currentState = resolved.keyframeFrame === targetFrame ? "exact" : "held";
          } else {
            throw new Error("当前标注不是原生 Mask");
          }
          const currentContentPath = `/annotations/${issue.annotation_id}/mask-compare/content`;
          const currentFrame = dynamicRef.current.isVideoTask ? targetFrame : null;
          const currentRle = await maskQcApi.versionContent(
            {
              annotationId: issue.annotation_id,
              annotationVersion: issue.annotation_version,
              digest: currentDigest,
              frameIndex: currentFrame,
            },
            token.signal,
          );
          generation.assert(token.value);
          const result: MaskCompareResult = {
            baseline_kind: "ai_candidate",
            current: {
              annotation_id: issue.annotation_id,
              annotation_version: issue.annotation_version,
              frame_index: currentFrame,
              source: currentSource,
              state: currentState,
              digest: currentDigest,
              size: currentRle.size,
              content_path: `${currentContentPath}?annotation_version=${issue.annotation_version}&digest=${currentDigest}${currentFrame == null ? "" : `&frame_index=${currentFrame}`}`,
              candidate_job_id: null,
              candidate_digest: null,
              candidate_instance_id: null,
            },
            baseline: {
              annotation_id: issue.annotation_id,
              annotation_version: issue.annotation_version,
              frame_index: candidate.frameIndex,
              source: "ai_candidate",
              state: "candidate",
              digest: candidate.digest,
              size: candidate.rle.size,
              content_path: "local://ai-candidate",
              candidate_job_id: null,
              candidate_digest: candidate.digest,
              candidate_instance_id: null,
            },
            metrics: {
              current_area_pixels: 0,
              baseline_area_pixels: 0,
              intersection_pixels: 0,
              union_pixels: 0,
              changed_pixels: 0,
              added_pixels: 0,
              removed_pixels: 0,
              iou_numerator: 0,
              iou_denominator: 0,
              dice_numerator: 0,
              dice_denominator: 0,
            },
            loss: [],
          };
          if (
            expectedDigests &&
            (result.current.digest !== expectedDigests.current ||
              result.baseline.digest !== expectedDigests.baseline)
          )
            throw new Error("评论引用的 Mask 对比证据已变化");
          const store = installStore(
            { result, currentRle, baselineRle: candidate.rle },
            requestedMode,
          );
          try {
            const metrics = await store.metrics({ priority: "current", signal: token.signal });
            result.metrics = {
              current_area_pixels: metrics.currentAreaPixels,
              baseline_area_pixels: metrics.baselineAreaPixels,
              intersection_pixels: metrics.intersectionPixels,
              union_pixels: metrics.unionPixels,
              changed_pixels: metrics.changedPixels,
              added_pixels: metrics.addedPixels,
              removed_pixels: metrics.removedPixels,
              iou_numerator: metrics.intersectionPixels,
              iou_denominator: metrics.unionPixels,
              dice_numerator: metrics.intersectionPixels * 2,
              dice_denominator: metrics.currentAreaPixels + metrics.baselineAreaPixels,
            };
          } catch (error) {
            if (storeRef.current === store) {
              store.dispose();
              storeRef.current = null;
            }
            throw error;
          }
          generation.assert(token.value);
          if (!liveNavigationContextMatches()) {
            if (storeRef.current === store) {
              store.dispose();
              storeRef.current = null;
            }
            throw new Error("工作台上下文已变化，请重新定位该问题");
          }
          setState({
            issue,
            phase: "ready",
            error: null,
            compare: result,
            baseline,
            store,
            trackerCandidate: null,
            mode: requestedMode,
          });
          return;
        }
        const result = await maskQcApi.compare(
          {
            annotationId: issue.annotation_id,
            annotationVersion: issue.annotation_version,
            baseline,
            frameIndex: dynamicRef.current.isVideoTask ? targetFrame : null,
            candidateJobId: trackerCandidate?.jobId ?? null,
            candidateJobRevision: trackerCandidate?.jobRevision ?? null,
            candidateDigest: trackerCandidate?.digest ?? null,
            candidateInstanceId: trackerCandidate?.instanceId ?? null,
          },
          token.signal,
        );
        generation.assert(token.value);
        if (
          expectedDigests &&
          (result.current.digest !== expectedDigests.current ||
            result.baseline.digest !== expectedDigests.baseline)
        )
          throw new Error("评论引用的 Mask 对比证据已变化");
        const [currentRle, baselineRle] = await Promise.all([
          maskQcApi.content(result.current.content_path, token.signal),
          maskQcApi.content(result.baseline.content_path, token.signal),
        ]);
        generation.assert(token.value);
        const payload = { result, currentRle, baselineRle };
        if (!liveNavigationContextMatches()) {
          throw new Error("工作台上下文已变化，请重新定位该问题");
        }
        const store = installStore(payload, requestedMode);
        generation.assert(token.value);
        setState({
          issue,
          phase: "ready",
          error: null,
          compare: result,
          baseline,
          mode: requestedMode,
          store,
          trackerCandidate,
        });
      } catch (error) {
        try {
          generation.assert(token.value);
        } catch {
          return;
        }
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (
          navigationContextReady &&
          liveNavigationContextMatches() &&
          !storeRef.current &&
          issue.region_digest
        ) {
          try {
            const regionRle = await maskQcApi.issueRegion(
              issue.id,
              issue.region_digest,
              token.signal,
            );
            generation.assert(token.value);
            if (!liveNavigationContextMatches()) throw new MaskQcNavigationStaleError();
            const backend = dynamicRef.current.workerPool;
            if (!backend) throw new Error("Raster Mask Worker 不可用");
            const regionStore = new MaskCompareTileStore({
              backend,
              current: { sha256: issue.region_digest, rle: regionRle },
              baseline: {
                sha256: `empty:${issue.region_digest}`,
                rle: {
                  encoding: "coco_rle",
                  size: regionRle.size,
                  counts: [regionRle.size[0] * regionRle.size[1]],
                },
              },
              mode: "added",
              scopeKey: `issue:${issue.id}`,
              display: {
                annotationId: issue.annotation_id,
                hideAiCandidate: true,
                hideTrackerCandidate: true,
              },
            });
            storeRef.current = regionStore;
          } catch (fallbackError) {
            if (
              fallbackError instanceof MaskQcNavigationStaleError ||
              (fallbackError instanceof DOMException && fallbackError.name === "AbortError")
            )
              return;
          }
        }
        generation.assert(token.value);
        setState((current) => ({
          ...current,
          phase: "error",
          error: errorMessage(error),
          store: storeRef.current,
          trackerCandidate,
        }));
      }
    },
    [disposeStore, installStore, params],
  );

  const retry = useCallback(() => {
    if (state.issue) void navigate(state.issue, state.baseline, state.trackerCandidate, state.mode);
  }, [navigate, state.baseline, state.issue, state.mode, state.trackerCandidate]);

  const setBaseline = useCallback(
    (baseline: MaskCompareBaseline) => {
      if (state.issue) {
        void navigate(
          state.issue,
          baseline,
          baseline === "tracker_candidate" ? state.trackerCandidate : null,
          state.mode,
        );
      }
    },
    [navigate, state.issue, state.mode, state.trackerCandidate],
  );

  const setTrackerCandidate = useCallback(
    (candidate: MaskQcTrackerCandidate) => {
      if (state.issue) void navigate(state.issue, "tracker_candidate", candidate, state.mode);
    },
    [navigate, state.issue, state.mode],
  );

  const replayFeedback = useCallback(
    (issue: MaskQcIssue, locator: MaskFeedbackCompareLocator) => {
      let candidate: MaskQcTrackerCandidate | null = null;
      if (locator.baseline_kind === "tracker_candidate") {
        if (
          !locator.candidate_job_id ||
          !locator.candidate_job_revision ||
          !locator.candidate_digest
        ) {
          setState((current) => ({
            ...current,
            issue,
            phase: "error",
            error: "Tracker 评论缺少可重放的候选身份",
          }));
          return;
        }
        candidate = {
          key: `${locator.candidate_job_id}:${locator.candidate_job_revision}:${locator.candidate_digest}:${issue.frame_start ?? 0}:${locator.candidate_instance_id ?? "primary"}`,
          jobId: locator.candidate_job_id,
          jobRevision: locator.candidate_job_revision,
          digest: locator.candidate_digest,
          frameIndex: issue.frame_start ?? 0,
          annotationId: issue.annotation_id,
          instanceId: locator.candidate_instance_id ?? null,
          label: `评论证据 · r${locator.candidate_job_revision}`,
        };
      }
      void navigate(issue, locator.baseline_kind, candidate, locator.mode, {
        current: locator.current_digest,
        baseline: locator.baseline_digest,
      });
    },
    [navigate],
  );

  const updateIssue = useCallback((issue: MaskQcIssue) => {
    setState((current) => (current.issue?.id === issue.id ? { ...current, issue } : current));
  }, []);

  useEffect(
    () => () => {
      generationRef.current.cancel();
      storeRef.current?.dispose();
    },
    [],
  );

  useEffect(() => {
    if (!state.issue || !params.taskId || params.taskId === state.issue.task_id) return;
    const navigation = navigationTaskRef.current;
    if (
      navigation?.target === state.issue.task_id &&
      !navigation.reached &&
      params.taskId === navigation.origin
    )
      return;
    clear();
  }, [clear, params.taskId, state.issue]);

  useEffect(() => {
    if (!params.enabled && state.issue) clear();
  }, [clear, params.enabled, state.issue]);

  useEffect(() => {
    if (!state.issue || !state.store) return;
    const issue = state.issue;
    const annotation = params.annotations?.find((item) => item.id === issue.annotation_id);
    const expectedFrame = issue.frame_start ?? 0;
    const currentVersion = annotation?.version ?? 1;
    const navigationMatches = maskQcNavigationContextMatches(
      {
        taskId: params.taskId,
        selectedId: params.selectedId,
        visibleAnnotationIds: params.visibleAnnotationIds,
        isVideoTask: params.isVideoTask,
        frameIndex: params.frameIndex,
      },
      {
        taskId: issue.task_id,
        annotationId: issue.annotation_id,
        frameIndex: expectedFrame,
      },
    );
    const contextMatches =
      navigationMatches &&
      (issue.effective_status === "stale" ||
        state.phase !== "ready" ||
        !state.compare ||
        maskQcReadyContextMatches(
          {
            taskId: params.taskId,
            selectedId: params.selectedId,
            visibleAnnotationIds: params.visibleAnnotationIds,
            isVideoTask: params.isVideoTask,
            frameIndex: params.frameIndex,
            annotationVersion: annotation ? currentVersion : null,
          },
          {
            taskId: issue.task_id,
            annotationId: issue.annotation_id,
            annotationVersion: state.compare.current.annotation_version,
            frameIndex: expectedFrame,
          },
        ));
    if (contextMatches) return;
    generationRef.current.cancel();
    navigationTaskRef.current = null;
    disposeStore();
    setState((current) =>
      current.issue?.id === issue.id
        ? {
            ...current,
            phase: "error",
            error: "工作台上下文已变化，请重新定位该问题",
            compare: null,
            store: null,
          }
        : current,
    );
  }, [
    disposeStore,
    params.annotations,
    params.frameIndex,
    params.isVideoTask,
    params.selectedId,
    params.taskId,
    params.visibleAnnotationIds,
    state.compare,
    state.issue,
    state.phase,
    state.store,
  ]);

  return {
    ...state,
    navigate,
    retry,
    clear,
    setMode,
    setBaseline,
    setTrackerCandidate,
    replayFeedback,
    updateIssue,
  };
}
