export function batchPreannotationEligible(batch: {
  status: string;
  annotator_id?: string | null;
  reviewer_id?: string | null;
}): boolean {
  if (batch.status === "active") return true;
  if (batch.status === "draft") {
    return !batch.annotator_id && !batch.reviewer_id;
  }
  return false;
}
