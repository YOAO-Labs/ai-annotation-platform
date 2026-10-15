import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingRunOut } from "@/api/trainingRuns";
import { useNavigate } from "react-router-dom";
import { Plus, FlaskConical, GitCompareArrows } from "lucide-react";
import { TrainingRunMonitor } from "../Training/TrainingRunMonitor";
import { useTrainingEvents } from "../Training/useTrainingEvents";
import {
  activeTrainingStates as active,
  trainingStatusLabels as labels,
  trainingProgress,
  trainingStatusVariant,
} from "../Training/trainingPresentation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { confirmDialog } from "@/components/ui/decisionDialog";

export function ProjectTrainingPanel({
  projectId,
  initialRunId = "",
  visible = true,
  onRunSelect,
}: {
  projectId: string;
  initialRunId?: string;
  visible?: boolean;
  onRunSelect?: (id: string) => void;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [attemptId, setAttemptId] = useState("");
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [selectedId, setDetailId] = useState(initialRunId);
  useEffect(() => {
    setDetailId(initialRunId);
    setAttemptId("");
  }, [initialRunId]);
  const catalog = useQuery({
    queryKey: ["training-catalog", projectId],
    queryFn: () => trainingRunsApi.catalog(projectId),
    enabled: visible,
    refetchInterval: visible ? 5000 : false,
  });
  const runs = useQuery({
    queryKey: ["training-runs", projectId, page],
    queryFn: () => trainingRunsApi.list(projectId, page * 20),
    enabled: visible,
    refetchInterval: visible ? 3000 : false,
  });
  const detailId = selectedId || runs.data?.items[0]?.id || "";
  const selectRun = (id: string) => {
    setDetailId(id);
    setAttemptId("");
    onRunSelect?.(id);
  };
  const detail = useQuery({
    queryKey: ["training-run", projectId, detailId],
    queryFn: () => trainingRunsApi.get(projectId, detailId),
    enabled: visible && !!detailId,
    refetchInterval: (query) =>
      visible && query.state.data?.run && active.has(query.state.data.run.status) ? 3000 : false,
  });
  const selectedAttemptId = attemptId || detail.data?.run.current_attempt_id || "";
  const events = useTrainingEvents(
    projectId,
    detailId,
    selectedAttemptId,
    visible,
    !!detail.data?.run &&
      active.has(detail.data.run.status) &&
      selectedAttemptId === detail.data.run.current_attempt_id,
  );
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
    void client.invalidateQueries({ queryKey: ["training-run", projectId] });
    void client.invalidateQueries({ queryKey: ["training-events", projectId] });
    void client.invalidateQueries({ queryKey: ["training-catalog", projectId] });
    void client.invalidateQueries({ queryKey: ["training-hub"] });
  };
  const action = useMutation({
    mutationFn: async ({
      kind,
      run,
      attemptId,
      filename,
    }: {
      kind: "cancel" | "resume" | "delete" | "download";
      run: TrainingRunOut;
      attemptId?: string;
      filename?: string;
    }) => {
      if (kind === "download" && attemptId && filename) {
        const result = await trainingRunsApi.download(projectId, run.id, attemptId, filename);
        const link = document.createElement("a");
        link.href = result.url;
        link.download = filename;
        link.rel = "noopener";
        link.click();
      } else if (kind === "cancel") await trainingRunsApi.cancel(projectId, run.id);
      else if (kind === "resume") await trainingRunsApi.resume(projectId, run.id);
      else if (
        kind === "delete" &&
        (await confirmDialog({
          title: `删除实验“${run.name}”`,
          description: "将删除此实验的全部权重、检查点和日志。训练数据版本会保留。",
          tone: "danger",
          confirmLabel: "删除实验",
        }))
      ) {
        await trainingRunsApi.remove(projectId, run.id);
        if (detailId === run.id) setDetailId("");
      }
    },
    onSuccess: refresh,
    onError: (reason: Error) => setError(reason.message),
  });
  const current = detail.data?.run;
  return (
    <section aria-labelledby="training-title">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="training-title" className="text-md font-semibold">
            训练实验
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            选择实验查看实时进度、指标曲线与产物。每次执行和续训独立记录。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            onClick={() =>
              navigate(
                `/projects/${projectId}/learning/compare${current && selectedAttemptId ? `?runs=${current.id}:${selectedAttemptId}` : ""}`,
              )
            }
          >
            <GitCompareArrows className="size-3.5" />
            比较实验
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={!catalog.data?.enabled}
            onClick={() =>
              navigate(
                `/projects/${projectId}/learning/new${detailId ? `?return_run=${encodeURIComponent(detailId)}` : ""}`,
              )
            }
          >
            <Plus className="size-3.5" />
            新建实验
          </Button>
        </div>
      </div>
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge
          dot
          variant={catalog.data?.online ? (catalog.data.busy ? "warning" : "success") : "default"}
        >
          {catalog.data?.enabled
            ? catalog.data.online
              ? catalog.data.busy
                ? "GPU 正忙 · 新实验将排队"
                : "执行器在线 · 可开始训练"
              : "执行器离线 · 实验可排队"
            : "训练执行器尚未启用"}
        </Badge>
        <span>共 {runs.data?.total ?? 0} 个实验</span>
      </div>
      {(error || catalog.error || runs.error || detail.error) && (
        <p
          role="alert"
          className="mb-4 rounded-md bg-status-danger-soft p-3 text-sm text-status-danger"
        >
          {error || catalog.error?.message || runs.error?.message || detail.error?.message}
        </p>
      )}
      {runs.isLoading && (
        <p role="status" className="py-8 text-sm text-muted-foreground">
          正在加载训练实验…
        </p>
      )}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(240px,0.7fr)_minmax(0,1.8fr)]">
        <aside className="min-w-0" aria-label="实验列表">
          <div className="space-y-2">
            {runs.data?.items.map((run) => {
              const progress = trainingProgress(run);
              return (
                <button
                  key={run.id}
                  aria-label={run.name}
                  aria-pressed={detailId === run.id}
                  onClick={() => selectRun(run.id)}
                  className={`w-full rounded-lg border p-4 text-left transition-colors focus-visible:ring-2 focus-visible:ring-ring ${detailId === run.id ? "border-primary bg-primary/5" : "border-border bg-card hover:bg-muted"}`}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="break-words text-sm font-medium">{run.name}</span>
                    <Badge dot variant={trainingStatusVariant(run.status)}>
                      {labels[run.status] ?? run.status}
                    </Badge>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    {String(run.config.framework)} · {run.auto_trigger_id ? "自动" : "手动"}
                  </p>
                  <div className="mt-3 flex justify-between text-xs tabular-nums text-muted-foreground">
                    <span>{new Date(run.created_at).toLocaleDateString("zh-CN")}</span>
                    <span>
                      {progress.epoch} / {progress.total} 轮
                    </span>
                  </div>
                  <div className="mt-2">
                    <ProgressBar value={progress.percent} />
                  </div>
                </button>
              );
            })}
          </div>
          {!!runs.data?.total && (
            <div className="mt-3 flex items-center justify-between gap-2">
              <Button size="sm" disabled={!page} onClick={() => setPage((value) => value - 1)}>
                上一页实验
              </Button>
              <span className="text-xs text-muted-foreground">
                {page + 1} / {Math.ceil(runs.data.total / 20)}
              </span>
              <Button
                size="sm"
                disabled={(page + 1) * 20 >= runs.data.total}
                onClick={() => setPage((value) => value + 1)}
              >
                下一页实验
              </Button>
            </div>
          )}
        </aside>
        {current ? (
          <TrainingRunMonitor
            key={current.id}
            run={current}
            attempts={detail.data?.attempts ?? []}
            events={events.data ?? []}
            selectedAttemptId={selectedAttemptId}
            onAttemptChange={setAttemptId}
            eventsPending={events.isLoading}
            eventsError={events.error?.message}
            pending={action.isPending}
            onAction={(value) => action.mutate(value)}
          />
        ) : (
          <div className="flex min-h-80 flex-col items-center justify-center rounded-xl border border-dashed border-border p-8 text-center">
            <FlaskConical className="mb-4 size-8 text-muted-foreground" />
            <h3 className="text-sm font-medium">
              {detail.isLoading
                ? "正在加载实验详情…"
                : runs.data?.items.length
                  ? "选择一个训练实验"
                  : "创建你的第一个训练实验"}
            </h3>
            <p className="mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
              使用已就绪的数据版本创建实验，训练开始后将在这里显示进度和验证表现。
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
