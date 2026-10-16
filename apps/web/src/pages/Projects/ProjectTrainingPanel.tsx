import { useEffect, useRef, useState, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingRunOut } from "@/api/trainingRuns";
import { useNavigate } from "react-router-dom";
import { Plus, FlaskConical, GitCompareArrows, Search } from "lucide-react";
import { TrainingRunMonitor } from "../Training/TrainingRunMonitor";
import { TrainingStudyPanel } from "../Training/TrainingStudyPanel";
import { useTrainingEvents } from "../Training/useTrainingEvents";
import { trainingObservabilityApi } from "@/api/trainingObservability";
import { useTrainingViewOwner } from "../Training/trainingViewOwner";
import {
  activeTrainingStates as active,
  trainingStatusLabels as labels,
  trainingProgress,
  trainingStatusVariant,
  trainingTaskLabel,
  trainingTaskOf,
} from "../Training/trainingPresentation";
import { cn } from "@/lib/utils";
import { TrainingLoading, TrainingError } from "../Training/TrainingFeedback";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { confirmDialog } from "@/components/ui/decisionDialog";

export function ProjectTrainingPanel({
  projectId,
  initialRunId = "",
  initialAttemptId = "",
  visible = true,
  onRunSelect,
  onAttemptSelect,
  scope = "",
  createButtonRef,
}: {
  projectId: string;
  initialRunId?: string;
  initialAttemptId?: string;
  visible?: boolean;
  onRunSelect?: (id: string) => void;
  onAttemptSelect?: (id: string, runId: string) => void;
  scope?: string;
  createButtonRef?: RefObject<HTMLButtonElement>;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const session = useTrainingViewOwner(projectId, scope);
  const revision = useRef(0);
  const [attemptId, setAttemptId] = useState(initialAttemptId);
  const [error, setError] = useState("");
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  const [selectedId, setDetailId] = useState(initialRunId);
  useEffect(() => {
    setDetailId(initialRunId);
    setAttemptId(initialAttemptId);
    setError("");
    setPage(0);
    revision.current++;
  }, [initialRunId, initialAttemptId, projectId, scope]);
  const catalog = useQuery({
    queryKey: ["training-catalog", projectId, scope],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const data = await trainingRunsApi.catalog(projectId, { signal });
      session.assert(signal);
      return data;
    },
    enabled: visible,
    refetchInterval: visible ? 5000 : false,
  });
  const runs = useQuery({
    queryKey: ["training-runs", projectId, scope, page],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const data = await trainingRunsApi.list(projectId, page * 20, { signal });
      session.assert(signal);
      return data;
    },
    enabled: visible,
    refetchInterval: visible ? 3000 : false,
  });
  const detailId = selectedId || runs.data?.items[0]?.id || "";
  const selectRun = (id: string) => {
    session.assert();
    revision.current++;
    setDetailId(id);
    setAttemptId("");
    setError("");
    onRunSelect?.(id);
  };
  const detail = useQuery({
    queryKey: ["training-run", projectId, scope, detailId],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const data = await trainingRunsApi.get(projectId, detailId, { signal });
      session.assert(signal);
      if (data.run.id !== detailId || data.run.project_id !== projectId)
        throw new Error("实验归属已变化，请重新选择");
      return data;
    },
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
    scope,
    session.assert,
  );
  const current = detail.data?.run;
  const viewKey = `${scope}:${detailId}:${selectedAttemptId}:${current?.current_attempt_id ?? ""}`;
  const currentView = useRef(viewKey);
  if (currentView.current !== viewKey) {
    currentView.current = viewKey;
    revision.current++;
  }
  const recovery = useQuery({
    queryKey: ["training-recovery", projectId, scope, detailId, current?.current_attempt_id],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await trainingObservabilityApi.recovery(projectId, detailId, { signal });
      session.assert(signal);
      return result;
    },
    enabled: visible && !!current && ["failed", "cancelled"].includes(current.status),
    refetchInterval:
      visible && !!current && ["failed", "cancelled"].includes(current.status) ? 5000 : false,
    retry: false,
  });
  type View = { owner: typeof session.owner; revision: number };
  const ownsView = (view?: View) =>
    !!view && session.owns() && view.owner === session.owner && view.revision === revision.current;
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
    void client.invalidateQueries({ queryKey: ["training-run", projectId] });
    void client.invalidateQueries({ queryKey: ["training-events", projectId] });
    void client.invalidateQueries({ queryKey: ["training-recovery", projectId, scope] });
    void client.invalidateQueries({ queryKey: ["training-catalog", projectId] });
    void client.invalidateQueries({ queryKey: ["training-hub"] });
  };
  const action = useMutation({
    mutationFn: async ({
      kind,
      run,
      attemptId,
      filename,
      view,
    }: {
      kind: "cancel" | "resume" | "delete" | "download";
      run: TrainingRunOut;
      attemptId?: string;
      filename?: string;
      view: View;
    }) => {
      if (!ownsView(view)) return false;
      if (kind === "download" && attemptId && filename) {
        const result = await trainingRunsApi.download(projectId, run.id, attemptId, filename);
        if (!ownsView(view)) return false;
        const link = document.createElement("a");
        link.href = result.url;
        link.download = filename;
        link.rel = "noopener";
        link.click();
        return false;
      } else if (kind === "cancel") await trainingRunsApi.cancel(projectId, run.id);
      else if (kind === "resume") {
        if (!recovery.data?.eligible || recovery.data.attempt_id !== run.current_attempt_id)
          return false;
        await trainingRunsApi.resume(projectId, run.id);
      } else if (
        kind === "delete" &&
        (await confirmDialog({
          title: `删除实验“${run.name}”`,
          description: "将删除此实验的全部权重、检查点和日志。训练数据版本会保留。",
          tone: "danger",
          confirmLabel: "删除实验",
        }))
      ) {
        if (!ownsView(view)) return false;
        await trainingRunsApi.remove(projectId, run.id);
        if (!ownsView(view)) return false;
        if (detailId === run.id) {
          setDetailId("");
          onRunSelect?.("");
        }
      } else {
        return false;
      }
      return true;
    },
    onSuccess: (changed, variables) => {
      if (changed && ownsView(variables.view)) refresh();
    },
    onError: (reason: Error, variables) => {
      if (ownsView(variables.view)) setError(reason.message);
    },
  });
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
            ref={createButtonRef}
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
          variant={
            catalog.isError
              ? "danger"
              : catalog.data?.online
                ? catalog.data.busy
                  ? "warning"
                  : "success"
                : "default"
          }
        >
          {catalog.isLoading
            ? "正在连接执行器…"
            : catalog.isError
              ? "执行器状态读取失败"
              : catalog.data?.enabled
                ? catalog.data.online
                  ? catalog.data.busy
                    ? "GPU 正忙 · 新实验将排队"
                    : "执行器在线 · 可开始训练"
                  : "执行器离线 · 实验可排队"
                : "训练执行器尚未启用"}
        </Badge>
        <span>每 3 秒更新实验状态</span>
      </div>
      <TrainingStudyPanel
        key={`${projectId}:${scope}:${session.owner.revision}`}
        projectId={projectId}
        scope={scope}
        visible={visible}
      />
      {error && (
        <p
          role="alert"
          className="mb-4 rounded-lg bg-status-danger-soft p-4 text-xs text-status-danger"
        >
          {error}
        </p>
      )}
      {(catalog.error || runs.error || detail.error) && (
        <div className="mb-4">
          <TrainingError
            message={(catalog.error ?? runs.error ?? detail.error)!.message}
            onRetry={() => {
              if (catalog.error) void catalog.refetch();
              if (runs.error) void runs.refetch();
              if (detail.error) void detail.refetch();
            }}
          />
        </div>
      )}
      <div className="grid items-start gap-5 lg:grid-cols-[18rem_minmax(0,1fr)]">
        <aside
          className="min-w-0 overflow-hidden rounded-xl border border-border bg-card lg:sticky lg:top-4"
          aria-label="实验列表"
        >
          <div className="space-y-3 border-b border-border p-4">
            <div className="flex items-baseline justify-between gap-2">
              <h3 className="text-sm font-semibold">实验记录</h3>
              <span className="text-xs tabular-nums text-muted-foreground">
                共 {runs.data?.total ?? "—"} 个
              </span>
            </div>
            <label className="flex items-center gap-2 rounded-md border border-border bg-background px-2.5 py-2 focus-within:ring-2 focus-within:ring-ring">
              <Search aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
              <input
                aria-label="搜索本页实验"
                placeholder="搜索本页实验"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                className="min-w-0 flex-1 bg-transparent text-xs outline-none"
              />
            </label>
          </div>
          {runs.isLoading && <TrainingLoading label="正在加载训练实验…" rows={2} />}
          <div className="max-h-80 divide-y divide-border overflow-y-auto lg:max-h-128">
            {runs.data?.items
              .filter((run) =>
                run.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
              )
              .map((run) => {
                const progress = trainingProgress(run);
                return (
                  <button
                    key={run.id}
                    aria-label={run.name}
                    aria-pressed={detailId === run.id}
                    onClick={() => selectRun(run.id)}
                    className={cn(
                      "w-full border-l-2 p-4 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                      detailId === run.id
                        ? "border-l-primary bg-primary/5"
                        : "border-l-transparent hover:bg-muted/50",
                    )}
                  >
                    <span
                      title={run.name}
                      className="line-clamp-2 break-words text-sm font-semibold"
                    >
                      {run.name}
                    </span>
                    <p className="mt-1.5 text-2xs text-muted-foreground">
                      {run.config.framework === "libreyolo"
                        ? "LibreYOLO"
                        : run.config.framework === "ultralytics"
                          ? "Ultralytics"
                          : String(run.config.framework ?? "—")}{" "}
                      · {trainingTaskLabel(run.task ?? trainingTaskOf(run.config))}
                    </p>
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                      <Badge dot variant={trainingStatusVariant(run.status)}>
                        {run.status === "lost" ? "执行器失联" : (labels[run.status] ?? run.status)}
                      </Badge>
                      <span className="text-2xs tabular-nums text-muted-foreground">
                        {run.status === "queued"
                          ? "等待调度"
                          : `${progress.epoch} / ${progress.total} 轮`}
                      </span>
                    </div>
                    {run.status !== "queued" && progress.total > 0 && (
                      <div className="mt-2.5">
                        <ProgressBar
                          value={progress.percent}
                          color={
                            run.status === "succeeded"
                              ? "var(--sc-status-positive)"
                              : "var(--sc-brand)"
                          }
                        />
                      </div>
                    )}
                  </button>
                );
              })}
          </div>
          {runs.data &&
            !runs.error &&
            !runs.data.items.some((run) =>
              run.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
            ) && (
              <div className="p-5 text-center">
                <p className="text-xs text-muted-foreground">
                  {search ? "本页没有匹配的实验" : "尚无训练实验"}
                </p>
                {search && (
                  <Button size="xs" variant="ghost" className="mt-2" onClick={() => setSearch("")}>
                    清除搜索
                  </Button>
                )}
              </div>
            )}
          {((runs.data?.total ?? 0) > 20 || page > 0) && (
            <nav
              aria-label="实验分页"
              className="flex items-center justify-between gap-2 border-t border-border p-3"
            >
              <Button
                size="xs"
                disabled={!page}
                onClick={() => {
                  setSearch("");
                  setPage((value) => value - 1);
                }}
              >
                上一页实验
              </Button>
              <span className="text-2xs tabular-nums text-muted-foreground">
                {page + 1} / {Math.max(1, Math.ceil((runs.data?.total ?? 0) / 20))}
              </span>
              <Button
                size="xs"
                disabled={(page + 1) * 20 >= (runs.data?.total ?? 0)}
                onClick={() => {
                  setSearch("");
                  setPage((value) => value + 1);
                }}
              >
                下一页实验
              </Button>
            </nav>
          )}
        </aside>
        {current &&
        (initialAttemptId || attemptId) &&
        !detail.data?.attempts.some((item) => item.id === selectedAttemptId) ? (
          <p role="alert" className="p-4 text-sm text-status-warning">
            指定的执行尝试不存在或不属于此实验，请明确选择其他尝试。
          </p>
        ) : current ? (
          <TrainingRunMonitor
            key={current.id}
            run={current}
            attempts={detail.data?.attempts ?? []}
            events={events.data ?? []}
            selectedAttemptId={selectedAttemptId}
            onAttemptChange={(id) => {
              session.assert();
              revision.current++;
              setAttemptId(id);
              onAttemptSelect?.(id, current.id);
              setError("");
            }}
            eventsPending={events.isLoading}
            eventsError={events.error?.message}
            pending={action.isPending && ownsView(action.variables?.view)}
            onAction={(value) => {
              setError("");
              action.mutate({
                ...value,
                view: { owner: session.owner, revision: revision.current },
              });
            }}
            recovery={recovery.data}
            recoveryError={recovery.error?.message}
            scope={scope}
            visible={visible}
            assertOwner={session.assert}
          />
        ) : runs.isLoading || detail.isLoading ? (
          <div className="rounded-xl border border-border bg-card">
            <TrainingLoading label="正在加载实验详情…" rows={5} />
          </div>
        ) : (
          <div className="flex min-h-80 flex-col items-center justify-center rounded-xl border border-border bg-card p-8 text-center">
            <div className="mb-4 flex size-11 items-center justify-center rounded-xl bg-muted">
              <FlaskConical aria-hidden="true" className="size-5 text-muted-foreground" />
            </div>
            <h3 className="text-sm font-semibold">
              {runs.error || detail.error
                ? "暂时无法读取实验"
                : runs.data?.items.length
                  ? "选择一个训练实验"
                  : "创建你的第一个训练实验"}
            </h3>
            <p className="mt-2 max-w-sm text-xs leading-relaxed text-muted-foreground">
              {runs.error || detail.error
                ? "请重试读取记录，再查看实验指标与产物。"
                : "使用就绪数据创建实验，在这里跟进训练指标、执行资源与产物。"}
            </p>
            {!runs.error && !detail.error && !runs.data?.items.length && (
              <Button
                size="sm"
                className="mt-5"
                onClick={() => navigate(`/projects/${projectId}/learning?tab=data`)}
              >
                准备训练数据
              </Button>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
