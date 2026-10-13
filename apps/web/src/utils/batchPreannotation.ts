/**
 * Issue #124 · 批量预标注的批次准入规则（前端入口筛选用）。
 *
 * 与后端 `app/services/batch_permissions.py::allows_bulk_preannotation` 同源：
 * active 批次照旧可预标；「尚未分派标注员与质检员」的 draft 批次也放行，
 * 让管理员先跑 AI 再分派人工。已分派人员的 draft 与已进入人工流程的
 * 批次（annotating / reviewing / ...）一律不符合，后端会以 4xx 拒绝。
 */
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
