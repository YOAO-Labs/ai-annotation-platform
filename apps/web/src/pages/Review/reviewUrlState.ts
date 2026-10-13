/**
 * Pure URL rules for the Review queue.
 *
 * The queue scope (project / batch / assignee) and the open task drawer are
 * owned by the URL so browser navigation changes the request scope before the
 * next render.  All param parsing and manipulation lives here so the page only
 * wires results into navigation and React state.
 */

export type ReviewQueueParams = {
  project: string;
  batch: string;
  assignee: string;
};

export function readReviewQueueParams(searchParams: URLSearchParams): ReviewQueueParams {
  return {
    project: searchParams.get("project") ?? "",
    batch: searchParams.get("batch") ?? "",
    assignee: searchParams.get("assignee") ?? "",
  };
}

export function reviewQueueIsUnchanged(
  previous: ReviewQueueParams & { authOwnerKey: string },
  next: ReviewQueueParams & { authOwnerKey: string },
): boolean {
  return (
    previous.project === next.project &&
    previous.batch === next.batch &&
    previous.assignee === next.assignee &&
    previous.authOwnerKey === next.authOwnerKey
  );
}

/**
 * Identity of the current queue scope; async review flows snapshot it and
 * re-check before committing so a stale response cannot write a newer queue.
 */
export function reviewQueueScopeKey(authOwnerKey: string, scope: ReviewQueueParams): string {
  return [authOwnerKey, scope.project, scope.batch, scope.assignee].join("\u001f");
}

/** Select a batch (or clear the selection); other filters such as assignee survive. */
export function selectReviewBatch(
  searchParams: URLSearchParams,
  batch: { project_id: string; batch_id: string } | null,
): URLSearchParams {
  const next = new URLSearchParams(searchParams);
  next.delete("project");
  next.delete("batch");
  if (batch) {
    next.set("project", batch.project_id);
    next.set("batch", batch.batch_id);
  }
  return next;
}

export function clearReviewAssignee(searchParams: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(searchParams);
  next.delete("assignee");
  return next;
}

/** Back to the card grid: drop project / batch / assignee selection. */
export function clearReviewSelection(searchParams: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(searchParams);
  next.delete("project");
  next.delete("batch");
  next.delete("assignee");
  return next;
}

export function readReviewTaskId(searchParams: URLSearchParams): string | null {
  return searchParams.get("taskId");
}

/** Open (`taskId`) or close (`null`) the task drawer. */
export function setReviewTaskId(
  searchParams: URLSearchParams,
  taskId: string | null,
): URLSearchParams {
  const next = new URLSearchParams(searchParams);
  if (taskId === null) next.delete("taskId");
  else next.set("taskId", taskId);
  return next;
}
