import { ListPagination } from "@/components/ui/ListPagination";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQueries, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { ArrowLeft, Plus, X } from "lucide-react";
import { trainingRunsApi } from "@/api/trainingRuns";
import { useAuthStore } from "@/stores/authStore";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { trainingInputClass } from "./TrainingParameterField";
import { trainingEventsOptions } from "./useTrainingEvents";
import { activeTrainingStates, trainingStatusLabels } from "./trainingPresentation";
import { TrainingComparisonResults } from "./TrainingComparisonResults";
import {
  parseComparison,
  selectionValue,
  type ComparisonExperiment,
  type ComparisonSelection,
} from "./trainingComparison";

export function TrainingComparisonPage() {
  const { id = "" } = useParams();
  const userId = useAuthStore((state) => state.user?.id);
  const token = useAuthStore((state) => state.token);
  const scope = useMemo(() => ({ key: randomId(), id, userId, token }), [id, userId, token]);
  return <TrainingComparisonContent key={scope.key} projectId={id} scope={scope.key} />;
}

function TrainingComparisonContent({ projectId, scope }: { projectId: string; scope: string }) {
  const [params, setParams] = useSearchParams();
  const state = parseComparison(params);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const client = useQueryClient();
  const runs = useQuery({
    queryKey: ["training-comparison-picker", projectId, scope, page, pageSize],
    queryFn: ({ signal }) =>
      trainingRunsApi.list(projectId, page * pageSize, { signal }, {}, pageSize),
    retry: false,
  });
  const details = useQueries({
    queries: state.selections.map((selection) => ({
      queryKey: ["training-comparison-run", projectId, scope, selection.runId],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const data = await trainingRunsApi.get(projectId, selection.runId, { signal });
        signal.throwIfAborted();
        if (data.run.id !== selection.runId || data.run.project_id !== projectId)
          throw new Error("返回的实验身份不符");
        return data;
      },
      retry: false,
      refetchInterval: (query: Query<Awaited<ReturnType<typeof trainingRunsApi.get>>>) =>
        !query.state.error &&
        query.state.data?.run &&
        activeTrainingStates.has(query.state.data.run.status)
          ? 5000
          : false,
    })),
  });
  const attempts = state.selections.map((s, index) =>
    details[index].error
      ? undefined
      : details[index].data?.attempts.find((a) => a.id === s.attemptId),
  );
  const events = useQueries({
    queries: state.selections.map((s, index) => ({
      ...trainingEventsOptions(
        client,
        projectId,
        s.runId,
        s.attemptId,
        !!attempts[index],
        !!attempts[index] && activeTrainingStates.has(attempts[index]!.status),
        scope,
      ),
      retry: false,
    })),
  });
  const reports = useQueries({
    queries: state.selections.map((s, index) => ({
      queryKey: [
        "training-comparison-evaluation",
        projectId,
        scope,
        s.runId,
        s.attemptId,
        state.classPage,
        state.classPageSize,
      ],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const result = await trainingRunsApi.evaluation(
          projectId,
          s.runId,
          s.attemptId,
          state.classPage * state.classPageSize,
          { signal },
          state.classPageSize,
        );
        signal.throwIfAborted();
        if (result.provenance.run_id !== s.runId || result.provenance.attempt_id !== s.attemptId)
          throw new Error("返回的评估执行身份不符");
        return result;
      },
      enabled: attempts[index]?.status === "succeeded",
      retry: false,
      staleTime: 30000,
    })),
  });
  const experiments: ComparisonExperiment[] = state.selections.map((selection, index) => ({
    selection,
    run: details[index].error ? undefined : details[index].data?.run,
    attempt: attempts[index],
    attempts: details[index].data?.attempts ?? [],
    events: details[index].error || events[index].error ? [] : (events[index].data ?? []),
    report: details[index].error || reports[index].error ? undefined : reports[index].data,
    error:
      details[index].error?.message ??
      (details[index].data && !attempts[index]
        ? "所选执行不存在或不属于此实验，请显式选择其他执行"
        : undefined),
    eventsError: events[index].error?.message,
    reportError: reports[index].error?.message,
    loading:
      details[index].isPending ||
      (!!attempts[index] && events[index].isPending) ||
      (attempts[index]?.status === "succeeded" && reports[index].isPending),
  }));
  const update = (key: string, value: string) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set(key, value);
      if (key === "class_page_size") next.delete("class_page");
      return next;
    });
  const choose = (selections: ComparisonSelection[]) =>
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      next.set("runs", selections.map(selectionValue).join(","));
      if (!selections.some((s) => s.runId === next.get("reference"))) next.delete("reference");
      next.delete("class_page");
      return next;
    });
  return (
    <PageContainer>
      <Link
        to={`/projects/${projectId}/learning?tab=training`}
        className="mb-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        返回项目训练
      </Link>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">实验比较</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            选择 2–4 个实验，核对变化的输入与同一口径下的表现。
          </p>
        </div>
        <Badge>{state.selections.length} / 4 已选</Badge>
      </div>
      {state.error && (
        <div
          role="alert"
          className="mb-4 rounded-lg bg-status-danger-soft p-4 text-sm text-status-danger"
        >
          <p>{state.error}。保留原链接，未替换任何选择。</p>
          <Button size="sm" className="mt-3" onClick={() => setParams(new URLSearchParams())}>
            清空比较链接
          </Button>
        </div>
      )}
      <details
        className="mb-5 rounded-xl border border-border bg-card p-4"
        open={state.selections.length < 2 && !state.error}
      >
        <summary className="cursor-pointer text-sm font-medium">
          选择实验{" "}
          <span className="text-xs font-normal text-muted-foreground">
            · 已选实验保留到移除为止
          </span>
        </summary>
        <div className="mt-4 space-y-2">
          {runs.error && (
            <p role="alert" className="text-sm text-status-danger">
              <CopyableError message={runs.error.message} />
            </p>
          )}
          {runs.isPending && (
            <p role="status" className="text-sm text-muted-foreground">
              正在加载实验…
            </p>
          )}
          {runs.data?.items.map((run) => {
            const chosen = state.selections.some((s) => s.runId === run.id);
            return (
              <div
                key={run.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border p-3"
              >
                <div className="min-w-0">
                  <p className="break-words text-sm font-medium">{run.name}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {String(run.config.framework)} ·{" "}
                    {trainingStatusLabels[run.status] ?? run.status}
                  </p>
                </div>
                <Button
                  size="sm"
                  disabled={
                    !!state.error ||
                    chosen ||
                    state.selections.length >= 4 ||
                    !run.current_attempt_id
                  }
                  onClick={() =>
                    choose([
                      ...state.selections,
                      { runId: run.id, attemptId: run.current_attempt_id! },
                    ])
                  }
                  aria-label={`加入比较：${run.name}`}
                >
                  <Plus className="size-3.5" />
                  {chosen ? "已加入" : "加入比较"}
                </Button>
              </div>
            );
          })}
          {runs.data && !runs.data.total && (
            <p className="py-4 text-sm text-muted-foreground">项目尚无训练实验。</p>
          )}
        </div>
        <ListPagination
          label="实验"
          page={page}
          pageSize={pageSize}
          total={runs.data?.total ?? 0}
          loading={runs.isFetching}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(0);
          }}
          previousLabel="上一页实验"
          nextLabel="下一页实验"
        />
      </details>
      <div
        className={`mb-5 grid items-start gap-3 ${experiments.length === 2 ? "md:grid-cols-2" : experiments.length === 3 ? "md:grid-cols-2 xl:grid-cols-3" : "md:grid-cols-2 xl:grid-cols-4"}`}
      >
        {experiments.map((e, index) => (
          <fieldset
            key={e.selection.runId}
            className="min-w-0 rounded-xl border border-border bg-card p-4"
          >
            <legend className="px-1 text-xs text-muted-foreground">实验 {index + 1}</legend>
            <div className="flex items-start justify-between gap-2">
              <h2 className="min-w-0 break-words text-sm font-semibold">
                {e.run?.name ?? e.selection.runId}
              </h2>
              <Button
                size="sm"
                variant="ghost"
                aria-label={`移除：${e.run?.name ?? e.selection.runId}`}
                onClick={() =>
                  choose(state.selections.filter((s) => s.runId !== e.selection.runId))
                }
              >
                <X className="size-3.5" />
              </Button>
            </div>
            {e.error && (
              <p role="alert" className="mt-3 text-xs text-status-danger">
                <CopyableError message={e.error} />
              </p>
            )}
            {!e.run && !e.error ? (
              <p role="status" className="text-xs text-muted-foreground">
                正在加载所选实验…
              </p>
            ) : e.run ? (
              <>
                <Badge className="mt-2" variant="outline">
                  {trainingStatusLabels[e.attempt?.status ?? e.run.status]}
                </Badge>
                <label className="mt-3 block text-xs text-muted-foreground">
                  {e.run.name}的执行
                  <select
                    className={`${trainingInputClass} mt-1`}
                    value={e.selection.attemptId}
                    onChange={(event) =>
                      choose(
                        state.selections.map((s) =>
                          s.runId === e.selection.runId
                            ? { ...s, attemptId: event.target.value }
                            : s,
                        ),
                      )
                    }
                  >
                    {!e.attempt && <option value={e.selection.attemptId}>所选执行不可用</option>}
                    {e.attempts.map((a) => (
                      <option key={a.id} value={a.id}>
                        执行 #{a.ordinal} · {trainingStatusLabels[a.status] ?? a.status}
                      </option>
                    ))}
                  </select>
                </label>
                {e.run.current_attempt_id !== e.selection.attemptId && (
                  <p className="mt-2 text-xs text-status-info">
                    当前比较固定在此执行；可在上方选择其他执行。
                  </p>
                )}
              </>
            ) : null}
          </fieldset>
        ))}
      </div>
      {experiments.length < 2 ? (
        <p className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          {experiments.length ? "再加入一个实验即可开始比较。" : "从上方列表加入需要比较的实验。"}
        </p>
      ) : (
        <>
          <TrainingComparisonResults experiments={experiments} state={state} onChange={update} />
        </>
      )}
    </PageContainer>
  );
}
