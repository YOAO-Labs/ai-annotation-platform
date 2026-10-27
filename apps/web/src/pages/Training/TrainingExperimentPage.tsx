import { useMemo } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageContainer } from "@/components/layout/PageContainer";
import { useAuthStore } from "@/stores/authStore";
import { randomId } from "@/utils/id";
import { ProjectTrainingPanel } from "../Projects/ProjectTrainingPanel";
import { useTrainingSessionRevision } from "./trainingViewOwner";
import { trainingListPath } from "./TrainingListControls";

export function TrainingExperimentPage() {
  const { id = "", runId = "" } = useParams();
  const [params] = useSearchParams();
  const revision = useTrainingSessionRevision();
  const userId = useAuthStore((state) => state.user?.id);
  const { key: scope } = useMemo(
    () => ({ key: randomId(), id, runId, revision, userId }),
    [id, runId, revision, userId],
  );
  return (
    <PageContainer>
      <Link
        to={trainingListPath(id, "experiments", params)}
        className="mb-4 inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft aria-hidden="true" className="size-3.5" />
        返回实验列表
      </Link>
      <ProjectTrainingPanel
        key={scope}
        projectId={id}
        scope={scope}
        mode="detail"
        initialRunId={runId}
        initialAttemptId={params.get("attempt") ?? ""}
      />
    </PageContainer>
  );
}
