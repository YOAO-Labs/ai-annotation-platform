import { useReviewClaim } from "@/hooks/useTasks";
import type { TaskResponse } from "@/types";
import { useWorkbenchImageSource } from "../stage/useWorkbenchImageSource";

export function useWorkbenchTaskImage({
  task,
  requestedTaskId,
  review,
}: {
  task: TaskResponse | undefined;
  requestedTaskId: string | null;
  review: boolean;
}) {
  const taskId = task?.id;
  const reviewClaim = useReviewClaim(
    review && task?.status === "review" && (!requestedTaskId || taskId === requestedTaskId)
      ? taskId
      : undefined,
  );
  const admittedTaskId = review
    ? reviewClaim.initialSettled
      ? (taskId ?? null)
      : null
    : undefined;
  const image = useWorkbenchImageSource(
    task,
    task?.dataset_item_id ?? taskId ?? null,
    requestedTaskId,
    admittedTaskId,
  );
  return { reviewClaim, image };
}
