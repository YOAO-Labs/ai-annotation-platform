import type { AnnotationResponse, TaskResponse } from "@/types";
import { tasksApi } from "./tasks";
import { videoTrackerApi } from "./videoTracker";

export async function resolveActiveDiscussionAnnotation(
  task: TaskResponse,
  annotationId: string,
  signal?: AbortSignal,
): Promise<AnnotationResponse | null> {
  signal?.throwIfAborted();
  let segments: Array<string | null> = [null];
  if (task.file_type === "video") {
    const result = await videoTrackerApi.segments(task.id, { signal });
    signal?.throwIfAborted();
    if (result.task_id !== task.id) throw new Error("Video segment task mismatch");
    if (result.collaboration_enabled) {
      segments = [...new Set(result.segments.map((segment) => segment.id))];
    }
  }
  for (const segmentId of segments) {
    signal?.throwIfAborted();
    const annotations = await tasksApi.getAnnotations(task.id, segmentId, { signal });
    signal?.throwIfAborted();
    const annotation = annotations.find(
      (item) =>
        item.id === annotationId &&
        item.is_active === true &&
        item.task_id === task.id &&
        (!item.project_id || item.project_id === task.project_id) &&
        (segmentId === null || item.video_segment_id === segmentId),
    );
    if (annotation) return annotation;
  }
  return null;
}
