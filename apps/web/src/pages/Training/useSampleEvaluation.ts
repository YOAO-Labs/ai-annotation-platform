import { sampleEvaluationsApi } from "@/api/sampleEvaluations";
import type { AssertTrainingOwner } from "./trainingViewOwner";

export function evaluationTask(spec: { schema?: number; model_ref?: Record<string, unknown> }) {
  return spec.schema === 2 || ["segment", "segmentation"].includes(String(spec.model_ref?.task))
    ? "segment"
    : "detect";
}

export function evaluationReportOptions(
  projectId: string,
  id: string,
  attemptId: string,
  specDigest: string,
  scope: string,
  assert: AssertTrainingOwner,
) {
  return {
    queryKey: ["sample-evaluation-report", projectId, scope, id, attemptId, specDigest],
    queryFn: async ({ signal }: { signal: AbortSignal }) => {
      assert(signal);
      const report = await sampleEvaluationsApi.report(projectId, id, attemptId, { signal });
      assert(signal);
      if (
        report.evaluation_id !== id ||
        report.attempt_id !== attemptId ||
        report.spec_digest !== specDigest
      )
        throw new Error("评估报告身份不符，请重新加载");
      return report;
    },
    retry: false,
  };
}
