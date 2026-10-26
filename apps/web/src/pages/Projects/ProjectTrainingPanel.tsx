import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useRef, useState, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { trainingRunsApi, type TrainingRunOut } from "@/api/trainingRuns";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Plus, FlaskConical, GitCompareArrows, Trash2 } from "lucide-react";
import { TrainingRunMonitor } from "../Training/TrainingRunMonitor";
import { TrainingStudyPanel } from "../Training/TrainingStudyPanel";
import { useTrainingEvents } from "../Training/useTrainingEvents";
import { trainingObservabilityApi } from "@/api/trainingObservability";
import { useTrainingViewOwner } from "../Training/trainingViewOwner";
import {
  activeTrainingStates as active,
  trainingStatusLabels as labels,
} from "../Training/trainingPresentation";
import { TrainingLoading, TrainingError } from "../Training/TrainingFeedback";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import {
  TrainingListControls,
  useTrainingListFilters,
  trainingListPath,
  trainingRecordPath,
} from "../Training/TrainingListControls";
import { TrainingRunTable } from "./TrainingRunTable";
import { ProjectLearningSectionHeader } from "./ProjectLearningSectionHeader";

export function ProjectTrainingPanel({
  projectId,
  initialRunId = "",
  initialAttemptId = "",
  visible = true,
  mode = "list",
  onAttemptSelect,
  scope = "",
  createButtonRef,
}: {
  projectId: string;
  mode?: "list" | "detail";
  initialRunId?: string;
  initialAttemptId?: string;
  visible?: boolean;
  onAttemptSelect?: (id: string, runId: string) => void;
  scope?: string;
  createButtonRef?: RefObject<HTMLButtonElement>;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const list = useTrainingListFilters("run");
  const filters = { ...list.filters, q: useDebouncedValue(list.filters.q) };
  const session = useTrainingViewOwner(projectId, scope);
  const revision = useRef(0);
  const attemptId = params.get("attempt") ?? initialAttemptId;
  const detailId = initialRunId || params.get("run") || "";
  const detailVisible = visible && mode === "detail";
  const [error, setError] = useState("");
  useEffect(() => {
    setError("");
    revision.current++;
  }, [detailId, attemptId, mode, projectId, scope]);
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
    queryKey: ["training-runs", projectId, scope, list.page, list.pageSize, filters],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const data = await trainingRunsApi.list(
        projectId,
        list.page * list.pageSize,
        { signal },
        filters,
        list.pageSize,
      );
      session.assert(signal);
      return data;
    },
    enabled: visible && mode === "list",
    refetchInterval: visible && mode === "list" ? 3000 : false,
  });
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
    enabled: detailVisible && !!detailId,
    refetchInterval: (query) =>
      detailVisible ? (active.has(query.state.data?.run.status ?? "") ? 3000 : 5000) : false,
  });
  const selectedAttemptId = attemptId || detail.data?.run.current_attempt_id || "";
  const events = useTrainingEvents(
    projectId,
    detailId,
    selectedAttemptId,
    detailVisible,
    !!detail.data?.run &&
      active.has(detail.data.run.status) &&
      selectedAttemptId === detail.data.run.current_attempt_id,
    scope,
    session.assert,
  );
  const current = detail.data?.run;
  const deletionBlocked = current
    ? active.has(current.status)
      ? "实验尚未结束，请先停止训练。"
      : detail.data?.registered_models?.length
        ? "实验已被项目模型引用，不能删除。归档模型仍会保留来源引用。"
        : ""
    : "正在加载实验详情。";
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
    enabled: detailVisible && !!current && ["failed", "cancelled"].includes(current.status),
    refetchInterval:
      detailVisible && !!current && ["failed", "cancelled"].includes(current.status) ? 5000 : false,
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
        let objectUrl: string | undefined;
        if (result.requires_authentication) {
          const blob = await trainingRunsApi.downloadContent(
            projectId,
            run.id,
            attemptId,
            filename,
          );
          if (!ownsView(view)) return false;
          objectUrl = URL.createObjectURL(blob);
        }
        const link = document.createElement("a");
        link.href = objectUrl ?? result.url;
        link.download = filename;
        link.rel = "noopener";
        try {
          link.click();
        } finally {
          if (objectUrl) {
            const downloaded = objectUrl;
            setTimeout(() => URL.revokeObjectURL(downloaded), 0);
          }
        }
        return false;
      } else if (kind === "cancel") await trainingRunsApi.cancel(projectId, run.id);
      else if (kind === "resume") {
        if (!recovery.data?.eligible || recovery.data.attempt_id !== run.current_attempt_id)
          return false;
        await trainingRunsApi.resume(projectId, run.id);
      } else if (
        kind === "delete" &&
        !active.has(run.status) &&
        !deletionBlocked &&
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
        refresh();
        if (detailId === run.id) navigate(trainingListPath(projectId, "experiments", params));
        return false;
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
      <ProjectLearningSectionHeader
        id="training-title"
        title="训练实验"
        description="选择实验查看实时进度、指标曲线与产物。每次执行和续训独立记录。"
        actions={
          <>
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
            {mode === "list" ? (
              <Button
                ref={createButtonRef}
                size="sm"
                variant="primary"
                disabled={!catalog.data?.enabled}
                onClick={() => navigate(`/projects/${projectId}/learning/new`)}
              >
                <Plus className="size-3.5" />
                新建实验
              </Button>
            ) : (
              <Button
                size="sm"
                variant="danger"
                disabled={!!deletionBlocked || action.isPending}
                aria-describedby={
                  current && deletionBlocked ? "training-deletion-blocked" : undefined
                }
                onClick={() => {
                  if (!current) return;
                  setError("");
                  action.mutate({
                    kind: "delete",
                    run: current,
                    view: { owner: session.owner, revision: revision.current },
                  });
                }}
              >
                <Trash2 className="size-3.5" />
                删除实验
              </Button>
            )}
          </>
        }
      />
      {mode === "detail" && current && deletionBlocked && (
        <p id="training-deletion-blocked" className="mb-3 text-xs text-muted-foreground">
          {deletionBlocked}
        </p>
      )}
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
      {mode === "list" && (
        <TrainingStudyPanel
          key={`${projectId}:${scope}:${session.owner.revision}`}
          projectId={projectId}
          scope={scope}
          visible={visible}
        />
      )}
      {error && (
        <p
          role="alert"
          className="mb-4 rounded-lg bg-status-danger-soft p-4 text-xs text-status-danger"
        >
          <CopyableError message={error} />
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
      {mode === "list" ? (
        <div className="min-w-0 overflow-hidden rounded-xl border border-border bg-card">
          <TrainingListControls
            kind="实验"
            filters={list.filters}
            onChange={list.update}
            onReset={list.reset}
            statuses={labels}
          />
          <TrainingRunTable
            data={runs.data}
            loading={runs.isLoading}
            failed={runs.isError}
            page={list.page}
            onPage={list.setPage}
            pageSize={list.pageSize}
            onPageSize={list.setPageSize}
            selectedId={detailId}
            href={(id) => trainingRecordPath(projectId, "experiments", id, params)}
          />
        </div>
      ) : (
        <div>
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
                if (onAttemptSelect) onAttemptSelect(id, current.id);
                else
                  setParams((previous) => {
                    const next = new URLSearchParams(previous);
                    next.set("attempt", id);
                    next.set("run", current.id);
                    return next;
                  });
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
              registrationEligibility={detail.data?.registration_eligibility}
              registeredModels={detail.data?.registered_models}
              scope={scope}
              visible={detailVisible}
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
      )}
    </section>
  );
}
