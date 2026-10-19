import { Link, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { PageContainer } from "@/components/layout/PageContainer";
import { ProjectModelsPanel } from "../Projects/ProjectModelsPanel";
import { trainingListPath } from "./TrainingListControls";

export function TrainingModelPage() {
  const { id = "", modelId = "" } = useParams();
  const [params] = useSearchParams();
  return (
    <PageContainer>
      <Link
        to={trainingListPath(id, "models", params)}
        className="mb-4 inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft aria-hidden="true" className="size-3.5" />
        返回模型列表
      </Link>
      <ProjectModelsPanel projectId={id} classes={[]} mode="detail" initialModelId={modelId} />
    </PageContainer>
  );
}
