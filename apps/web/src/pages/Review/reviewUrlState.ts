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

export function reviewQueueScopeKey(authOwnerKey: string, scope: ReviewQueueParams): string {
  return [authOwnerKey, scope.project, scope.batch, scope.assignee].join("\u001f");
}

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

export function setReviewTaskId(
  searchParams: URLSearchParams,
  taskId: string | null,
): URLSearchParams {
  const next = new URLSearchParams(searchParams);
  if (taskId === null) next.delete("taskId");
  else next.set("taskId", taskId);
  return next;
}
