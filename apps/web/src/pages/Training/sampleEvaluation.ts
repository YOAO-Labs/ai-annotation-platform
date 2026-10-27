import { listPageSize } from "@/components/ui/ListPagination";
import type {
  EvaluationFilters,
  EvaluationSourceOut,
  SampleEvaluationOut,
} from "@/api/sampleEvaluations";
import { buildWorkbenchUrl } from "@/utils/workbenchNavigation";

export const evaluationStatus: Record<SampleEvaluationOut["status"], string> = {
  queued: "等待执行",
  running: "评估中",
  cancelling: "正在确认停止",
  lost: "执行失联",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};
export const activeEvaluationStates = new Set(["queued", "running", "cancelling"]);
export const errorLabels: Record<NonNullable<EvaluationFilters["error"]>, string> = {
  false_positive: "误检 FP",
  false_negative: "漏检 FN",
  confusion: "类别混淆",
  localization: "定位偏差",
  failed: "样本失败",
};
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function parseEvaluationState(params: URLSearchParams) {
  const errors: string[] = [];
  const single = (key: string) => {
    if (params.getAll(key).length > 1) errors.push(`${key} 重复`);
    return params.get(key);
  };
  const identity = (key: string) => {
    const value = single(key);
    if (value !== null && !uuid.test(value)) errors.push(`${key} 无效`);
    return value;
  };
  const number = (key: string, max: number, integer: boolean) => {
    const value = single(key);
    if (value === null) return undefined;
    const result = Number(value);
    if (
      !value.trim() ||
      !Number.isFinite(result) ||
      result < 0 ||
      result > max ||
      (integer && !Number.isInteger(result))
    ) {
      errors.push(`${key} 无效`);
      return undefined;
    }
    return result;
  };
  const attemptId = identity("attempt");
  const secondId = identity("second");
  const classIndex = number("class", 999, true);
  const page = number("page", 1000000, true) ?? 0;
  const pageSize = listPageSize(params.get("page_size"));
  const ordinal = number("sample", 31, true);
  const confidenceMin = number("confidence_min", 1, false);
  const confidenceMax = number("confidence_max", 1, false);
  const error = single("error");
  if (error !== null && !Object.prototype.hasOwnProperty.call(errorLabels, error))
    errors.push("误差类型无效");
  if (confidenceMin !== undefined && confidenceMax !== undefined && confidenceMin > confidenceMax)
    errors.push("置信度下限不能大于上限");
  return {
    attemptId,
    secondId,
    page,
    ordinal,
    filters: {
      class_index: classIndex,
      error: error === null ? undefined : (error as EvaluationFilters["error"]),
      confidence_min: confidenceMin,
      confidence_max: confidenceMax,
      offset: page * pageSize,
      limit: pageSize,
    } satisfies EvaluationFilters,
    error: errors.join("；"),
  };
}

export const metricValue = (value: number | null | undefined, fraction = false) =>
  value === null || value === undefined
    ? "不可用"
    : fraction
      ? `${(value * 100).toFixed(1)}%`
      : String(value);

export function sourceWorkbenchUrl(
  source: EvaluationSourceOut,
  projectId: string,
  taskId: string,
  returnTo: string,
) {
  if (
    source.source !== "found" ||
    source.project_id !== projectId ||
    source.task_id !== taskId ||
    !uuid.test(taskId)
  )
    return null;
  return buildWorkbenchUrl(projectId, { taskId, batchId: source.batch_id, returnTo });
}
