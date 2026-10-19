import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { asyncJobsApi } from "@/api/asyncJobs";
import { PageContainer } from "@/components/layout/PageContainer";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useAuthStore } from "@/stores/authStore";
import { randomId } from "@/utils/id";
import { TrainingDatasetInsights } from "../Projects/TrainingDatasetInsights";
import { ProjectTrainingDataActions } from "../Projects/ProjectTrainingDataVersions";
import { TrainingError, TrainingLoading } from "./TrainingFeedback";
import { useTrainingSessionRevision, useTrainingViewOwner } from "./trainingViewOwner";
import { trainingListPath } from "./TrainingListControls";

export function TrainingDatasetPage() {
  const { id = "", datasetId = "" } = useParams();
  const revision = useTrainingSessionRevision();
  const userId = useAuthStore((state) => state.user?.id);
  const { key: scope } = useMemo(
    () => ({ key: randomId(), id, datasetId, revision, userId }),
    [id, datasetId, revision, userId],
  );
  return <TrainingDatasetContent key={scope} projectId={id} datasetId={datasetId} scope={scope} />;
}

function TrainingDatasetContent({
  projectId,
  datasetId,
  scope,
}: {
  projectId: string;
  datasetId: string;
  scope: string;
}) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const session = useTrainingViewOwner(projectId, scope);
  const [error, setError] = useState("");
  const detail = useQuery({
    queryKey: ["training-dataset", projectId, scope, datasetId],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const version = await trainingDatasetsApi.getSummary(projectId, datasetId, { signal });
      session.assert(signal);
      if (version.id !== datasetId || version.project_id !== projectId)
        throw new Error("数据版本归属已变化，请重新选择");
      return version;
    },
    refetchInterval: (query) =>
      query.state.data && ["pending", "running"].includes(query.state.data.status) ? 2000 : false,
    retry: false,
  });
  const returnParams = new URLSearchParams(params);
  if (detail.data?.task === "segment" || (detail.data?.task && params.has("data_task")))
    returnParams.set("data_task", detail.data.task);
  const back = trainingListPath(projectId, "data", returnParams);
  const action = useMutation({
    mutationFn: async (kind: "retry" | "download" | "delete" | "cancel") => {
      session.assert();
      const version = detail.data;
      if (!version) return;
      setError("");
      if (kind === "download") {
        const result = await trainingDatasetsApi.download(projectId, datasetId);
        if (!session.owns()) return;
        const link = document.createElement("a");
        link.href = result.url;
        link.download = `${version.name}.zip`;
        link.rel = "noopener";
        link.click();
      } else if (kind === "retry") await trainingDatasetsApi.retry(projectId, datasetId);
      else if (kind === "cancel" && version.async_job_id)
        await asyncJobsApi.cancel(version.async_job_id);
      else if (
        kind === "delete" &&
        (await confirmDialog({
          title: `删除训练数据版本“${version.name}”`,
          description: "将删除冻结图片和训练包。被后续版本引用的数据不能删除。",
          tone: "danger",
          confirmLabel: "删除数据版本",
        }))
      ) {
        session.assert();
        await trainingDatasetsApi.remove(projectId, datasetId);
        if (!session.owns()) return;
        navigate(back);
      }
    },
    onSuccess: () => {
      if (!session.owns()) return;
      void client.invalidateQueries({ queryKey: ["training-dataset", projectId] });
      void client.invalidateQueries({ queryKey: ["training-datasets", projectId] });
      void client.invalidateQueries({ queryKey: ["task-datasets", projectId] });
    },
    onError: (reason: Error) => {
      if (session.owns()) setError(reason.message);
    },
  });
  return (
    <PageContainer>
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-1.5 rounded-sm text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <ArrowLeft aria-hidden="true" className="size-3.5" />
        返回数据版本列表
      </Link>
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">训练数据详情</h1>
        {detail.data && (
          <ProjectTrainingDataActions
            version={detail.data}
            pending={action.isPending}
            onAction={(kind) => action.mutate(kind)}
          />
        )}
      </header>
      {detail.isLoading && <TrainingLoading label="正在加载所选数据版本…" />}
      {(error || detail.error) && (
        <TrainingError
          message={error || detail.error!.message}
          onRetry={() => {
            setError("");
            void detail.refetch();
          }}
        />
      )}
      {detail.data && (
        <TrainingDatasetInsights
          projectId={projectId}
          version={detail.data}
          onClose={() => navigate(back)}
        />
      )}
    </PageContainer>
  );
}
