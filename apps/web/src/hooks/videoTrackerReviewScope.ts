import type { VideoTrackerJobPreview } from "@/api/videoTracker";

export interface TrackerReviewScope {
  instanceIds: string[];
  fromFrame: number;
  toFrame: number;
  intentRevision: number;
}

export interface TrackerReviewProjection {
  jobId: string;
  preview: VideoTrackerJobPreview;
  scope: TrackerReviewScope;
  availableInstanceIds: string[];
  selectedResults: VideoTrackerJobPreview["results"];
  selectedPending: number;
  jobPending: number;
  manualCount: number;
  remainingIntervals: Array<{ fromFrame: number; toFrame: number }>;
  intentKey: string;
}

export function reviewInstanceIds(preview: VideoTrackerJobPreview): string[] {
  return [...new Set(preview.results.map((result) => result.instance_id ?? "1"))].sort();
}

export function referenceReviewInstanceIds(
  preview: VideoTrackerJobPreview,
  annotationId: string | null,
): string[] {
  if (!annotationId) return [];
  const matches = preview.results.filter(
    (result) =>
      result.source_annotation_id === annotationId || result.target_annotation_id === annotationId,
  );
  if (matches.length > 0) {
    return [...new Set(matches.map((result) => result.instance_id ?? "1"))].sort();
  }
  const hasExplicitMapping = preview.results.some(
    (result) => result.source_annotation_id || result.target_annotation_id,
  );
  const ids = reviewInstanceIds(preview);
  return !hasExplicitMapping && preview.annotation_id === annotationId && ids.length === 1
    ? ids
    : [];
}

export function projectTrackerReview(
  preview: VideoTrackerJobPreview,
  scope: TrackerReviewScope,
  taskEpoch: number,
): TrackerReviewProjection {
  const selectedIds = new Set(scope.instanceIds);
  const selectedResults = preview.results.filter(
    (result) =>
      selectedIds.has(result.instance_id ?? "1") &&
      result.frame_index >= scope.fromFrame &&
      result.frame_index <= scope.toFrame,
  );
  const frames = [...new Set(preview.results.map((result) => result.frame_index))].sort(
    (left, right) => left - right,
  );
  const remainingIntervals: TrackerReviewProjection["remainingIntervals"] = [];
  const step = Math.max(1, preview.grid_step || 1);
  for (const frame of frames) {
    const last = remainingIntervals[remainingIntervals.length - 1];
    if (last && frame - last.toFrame <= step) last.toFrame = frame;
    else remainingIntervals.push({ fromFrame: frame, toFrame: frame });
  }
  return {
    jobId: preview.job_id,
    preview,
    scope,
    availableInstanceIds: reviewInstanceIds(preview),
    selectedResults,
    selectedPending: selectedResults.length,
    jobPending: preview.candidate_pending ?? preview.results.length,
    manualCount: selectedResults.filter((result) => result.manual_protected).length,
    remainingIntervals,
    intentKey: JSON.stringify([
      taskEpoch,
      preview.job_id,
      scope.intentRevision,
      preview.job_revision ?? 1,
      Object.entries(preview.expected_source_versions ?? {}).sort(),
    ]),
  };
}
