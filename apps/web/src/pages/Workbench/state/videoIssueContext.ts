import type { AnnotationFeedback, FeedbackVideoContext } from "@/api/feedbacks";

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const frame = (value: unknown): value is number =>
  finite(value) && Number.isInteger(value) && value >= 0;

export function readVideoIssueContext(issue: AnnotationFeedback): FeedbackVideoContext | null {
  const value = issue.anchor_position?.video_context;
  if (issue.anchor_type !== "pixel" || !frame(issue.anchor_position?.frame)) return null;
  if (!record(value) || value.schema_version !== 1) return null;
  if (value.track_id != null && typeof value.track_id !== "string") return null;
  if (
    value.annotation_version != null &&
    (!frame(value.annotation_version) || value.annotation_version < 1)
  )
    return null;
  const range = value.frame_range;
  if (
    range != null &&
    (!record(range) ||
      !frame(range.from_frame) ||
      !frame(range.to_frame) ||
      range.from_frame > issue.anchor_position.frame ||
      range.to_frame < issue.anchor_position.frame)
  )
    return null;
  const viewport = value.viewport;
  if (
    viewport != null &&
    (!record(viewport) ||
      !finite(viewport.center_x) ||
      !finite(viewport.center_y) ||
      !finite(viewport.zoom) ||
      viewport.zoom <= 0)
  )
    return null;
  const window = value.timeline_window;
  if (
    window != null &&
    (!record(window) ||
      !finite(window.from) ||
      !finite(window.to) ||
      window.from < 0 ||
      window.to < window.from)
  )
    return null;
  return structuredClone(value) as unknown as FeedbackVideoContext;
}

export function validIssueFrameRange(from: string, to: string, anchor: number, max?: number) {
  if (from.trim() === "" || to.trim() === "") return false;
  const start = Number(from);
  const end = Number(to);
  return (
    frame(start) &&
    frame(end) &&
    start <= anchor &&
    anchor <= end &&
    (max === undefined || end <= max)
  );
}
