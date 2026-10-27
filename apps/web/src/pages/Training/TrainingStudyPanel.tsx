import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { CopyableError } from "@/components/ui/CopyableError";
import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { trainingStudiesApi } from "@/api/trainingStudies";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Badge } from "@/components/ui/Badge";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useTrainingViewOwner } from "./trainingViewOwner";
import { trainingStatusLabels } from "./trainingPresentation";
import { secondsLabel } from "./trainingObservability";

const labels: Record<string, string> = {
  active: "进行中",
  completed: "已完成",
  cancelled: "已停止",
  cancelling: "正在停止",
  blocked: "已暂停",
  budget_exhausted: "预算已用尽",
  pending: "尚未放行",
  skipped: "已跳过",
};

export function TrainingStudyPanel({
  projectId,
  scope = "",
  visible = true,
}: {
  projectId: string;
  scope?: string;
  visible?: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const [expanded, setExpanded] = useState(() => params.has("study"));
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const session = useTrainingViewOwner(projectId, scope);
  const client = useQueryClient();
  const identity = [projectId, scope, session.owner.userId, session.owner.revision];
  const studies = useQuery({
    queryKey: ["training-studies", ...identity, page, pageSize],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await trainingStudiesApi.list(
        projectId,
        page * pageSize,
        { signal },
        pageSize,
      );
      session.assert(signal);
      return result;
    },
    enabled: visible && expanded,
    refetchInterval: visible && expanded ? 5000 : false,
  });
  const selected = params.get("study") ?? studies.data?.items[0]?.id ?? "";
  const current = useRef(selected);
  current.current = selected;
  const detail = useQuery({
    queryKey: ["training-study", ...identity, selected],
    queryFn: async ({ signal }) => {
      session.assert(signal);
      const result = await trainingStudiesApi.get(projectId, selected, { signal });
      session.assert(signal);
      if (result.id !== selected || result.project_id !== projectId)
        throw new Error("搜索归属已变化，请重新选择");
      return result;
    },
    enabled: visible && expanded && !!selected,
    refetchInterval: visible && expanded ? 5000 : false,
    retry: false,
  });
  const study = detail.data;
  const trials = useListPagination(study?.trials ?? [], selected);
  const cancel = async () => {
    if (!study || lock.current || !session.owns()) return;
    const id = study.id;
    lock.current = true;
    setBusy(true);
    setError("");
    try {
      const accepted = await confirmDialog({
        title: "停止参数搜索",
        description:
          "后续组合不再开始。活动试验会按普通训练协议停止，已提交的检查点与全部预算证据保留。",
        confirmLabel: "停止搜索",
        tone: "danger",
      });
      if (!accepted || !session.owns() || current.current !== id) return;
      await trainingStudiesApi.cancel(projectId, id);
      session.assert();
      if (current.current !== id) return;
      void client.invalidateQueries({ queryKey: ["training-study", projectId] });
      void client.invalidateQueries({ queryKey: ["training-studies", projectId] });
      void client.invalidateQueries({ queryKey: ["training-runs", projectId] });
      void client.invalidateQueries({ queryKey: ["training-run", projectId] });
    } catch (reason) {
      if (session.owns() && current.current === id)
        setError(reason instanceof Error ? reason.message : "停止搜索失败");
    } finally {
      lock.current = false;
      if (session.owns()) setBusy(false);
    }
  };
  const choose = (id: string) => {
    session.assert();
    const next = new URLSearchParams(params);
    next.set("study", id);
    next.set("tab", "training");
    setParams(next);
    setError("");
  };
  return (
    <section
      aria-label="参数搜索"
      className="mb-5 min-w-0 rounded-xl border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
          className="flex items-center gap-2 text-sm font-medium"
        >
          <SlidersHorizontal className="size-4 text-muted-foreground" />
          参数搜索
          <ChevronDown className={`size-3.5 ${expanded ? "rotate-180" : ""}`} />
        </button>
        <Link
          to={`/projects/${projectId}/learning/new?mode=study`}
          className="text-xs text-primary hover:underline"
        >
          新建参数搜索
        </Link>
      </div>
      {expanded && (
        <div className="mt-4">
          {(error || studies.error || detail.error) && (
            <p role="alert" className="mb-3 text-xs text-status-danger">
              <CopyableError message={error || studies.error?.message || detail.error?.message} />
            </p>
          )}
          {studies.isLoading && (
            <p role="status" className="text-xs text-muted-foreground">
              正在加载参数搜索…
            </p>
          )}
          {studies.data?.total === 0 && (
            <p className="text-xs text-muted-foreground">
              还没有参数搜索。新建时先核对完整网格与明确预算。
            </p>
          )}
          {!!studies.data?.items.length && (
            <div className="mb-4 flex flex-wrap gap-2" aria-label="搜索列表">
              {studies.data.items.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  aria-pressed={selected === item.id}
                  onClick={() => choose(item.id)}
                  className={`max-w-full break-words rounded-lg border px-3 py-2 text-left text-xs ${selected === item.id ? "border-primary bg-muted" : "border-border"}`}
                >
                  {item.name} · {labels[item.status] ?? item.status}
                </button>
              ))}
            </div>
          )}
          <ListPagination
            label="搜索"
            page={page}
            pageSize={pageSize}
            total={studies.data?.total ?? 0}
            loading={studies.isFetching}
            onPageChange={setPage}
            onPageSizeChange={(size) => {
              setPageSize(size);
              setPage(0);
            }}
            previousLabel="上一页搜索"
            nextLabel="下一页搜索"
          />
          {study && (
            <div aria-label="搜索详情" className="min-w-0">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <h3 className="min-w-0 break-words text-sm font-semibold">{study.name}</h3>
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    variant={
                      study.status === "blocked" || study.status === "budget_exhausted"
                        ? "warning"
                        : "default"
                    }
                  >
                    {labels[study.status] ?? study.status}
                  </Badge>
                  {(study.scheduling ||
                    study.status === "active" ||
                    study.status === "cancelling") && (
                    <Button size="sm" disabled={busy} onClick={() => void cancel()}>
                      {busy ? "处理中…" : "停止搜索"}
                    </Button>
                  )}
                </div>
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                {study.message} · 最大化 {study.plan.objective.replace("metrics/", "")} ·{" "}
                {study.plan.trials.length} 个组合 / {study.plan.planned_epochs} 轮计划
              </p>
              <dl className="mt-4 grid gap-3 text-xs sm:grid-cols-4">
                {[
                  ["已结算原生时间", secondsLabel(study.accounting.consumed_seconds)],
                  ["活动试验预留", secondsLabel(study.accounting.reserved_seconds)],
                  ["可用余额", secondsLabel(study.accounting.remaining_seconds)],
                  ["原生时间总上限", secondsLabel(study.plan.budgets.max_native_seconds)],
                ].map(([label, value]) => (
                  <div key={label} className="rounded-lg bg-muted p-3">
                    <dt className="text-muted-foreground">{label}</dt>
                    <dd className="mt-1 font-medium">{value}</dd>
                  </div>
                ))}
              </dl>
              {!!study.accounting.unknown_attempt_ids.length && (
                <p role="status" className="mt-3 text-xs text-status-caution">
                  有 {study.accounting.unknown_attempt_ids.length}{" "}
                  次执行的原生时间未知。后续试验与续训暂停，不能按零结算。
                </p>
              )}
              {study.accounting.overrun_seconds > 0 && (
                <p className="mt-3 text-xs text-status-caution">
                  已超出总预算 {secondsLabel(study.accounting.overrun_seconds)}
                  ，停止宽限中的实际计算已完整计入。
                </p>
              )}
              <p className="mt-3 flex items-center gap-1 text-2xs text-muted-foreground">
                计划摘要 <CopyValueButton value={study.plan_digest} label="复制搜索计划摘要" />
              </p>
              <div className="mt-4 space-y-3">
                {trials.items.map((trial) => (
                  <article
                    key={trial.id}
                    className="min-w-0 rounded-lg border border-border p-3 text-xs"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <p className="min-w-0 break-words font-medium">
                        试验 {trial.ordinal} · {JSON.stringify(trial.values)}
                      </p>
                      <Badge>
                        {trainingStatusLabels[trial.status] ?? labels[trial.status] ?? trial.status}
                      </Badge>
                    </div>
                    <p className="mt-2 leading-relaxed text-muted-foreground">
                      计划 {trial.planned_epochs} 轮 · 最新完成轮次 {trial.actual_epochs ?? "未知"}
                      {trial.completion === "early_stopped"
                        ? " · 原生提前停止"
                        : trial.completion === "target_reached"
                          ? " · 达到目标轮数"
                          : ""}{" "}
                      · 原生时间{" "}
                      {trial.native_time_unknown
                        ? "未知"
                        : trial.status === "pending" || trial.status === "skipped"
                          ? "尚未启动"
                          : trial.queue_seconds != null &&
                              [
                                "queued",
                                "preparing",
                                "training",
                                "evaluating",
                                "uploading",
                                "lost",
                                "cancelling",
                              ].includes(trial.status)
                            ? "等待退出结算"
                            : secondsLabel(trial.native_seconds)}
                    </p>
                    {trial.queue_seconds != null && (
                      <p className="mt-1 text-muted-foreground">
                        累计排队 {secondsLabel(trial.queue_seconds)} · 领取至退出结算{" "}
                        {secondsLabel(trial.lease_seconds ?? 0)}
                      </p>
                    )}
                    <p className="mt-2 flex items-center gap-1 text-2xs text-muted-foreground">
                      配置摘要{" "}
                      <CopyValueButton value={trial.config_digest} label="复制试验配置摘要" />
                    </p>
                    {trial.result && (
                      <p className="mt-2">最终目标指标 {(trial.result.value * 100).toFixed(2)}%</p>
                    )}
                    {trial.result_error && (
                      <p className="mt-2 text-status-caution">
                        <CopyableError message={trial.result_error} />
                      </p>
                    )}
                    {trial.run_id && (
                      <Link
                        to={`/projects/${projectId}/learning?tab=training&run=${trial.run_id}${trial.result ? `&attempt=${trial.result.attempt_id}` : ""}`}
                        className="mt-2 inline-block text-primary hover:underline"
                      >
                        查看实验、日志与续训
                      </Link>
                    )}
                  </article>
                ))}
              </div>
              <ListPagination
                label="搜索试验"
                page={trials.page}
                pageSize={trials.pageSize}
                total={trials.total}
                onPageChange={trials.setPage}
                onPageSizeChange={trials.setPageSize}
              />
              <div className="mt-4 space-y-3" aria-label="兼容结果排名">
                {study.rankings.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    暂无可排名的已验证最终报告。失败与缺失指标不会补零。
                  </p>
                )}
                {study.rankings.map((group, index) => {
                  const ranked = group.trial_ids
                    .map((id) => study.trials.find((trial) => trial.id === id))
                    .filter((trial) => !!trial);
                  const selections = ranked
                    .slice(0, 4)
                    .map((trial) => `${trial.run_id}:${trial.result?.attempt_id}`)
                    .join(",");
                  return (
                    <div
                      key={group.compatibility_digest}
                      className="min-w-0 rounded-lg bg-muted p-3 text-xs"
                    >
                      <h4 className="font-medium">兼容评估组 {index + 1} · 组内排名</h4>
                      <ol className="mt-2 space-y-1">
                        {ranked.map((trial, place) => (
                          <li key={trial.id}>
                            {place + 1}. 试验 {trial.ordinal} ·{" "}
                            {((trial.result?.value ?? 0) * 100).toFixed(2)}%
                          </li>
                        ))}
                      </ol>
                      <p className="mt-2 flex items-center gap-1 text-2xs text-muted-foreground">
                        评估身份{" "}
                        <CopyValueButton
                          value={group.compatibility_digest}
                          label="复制评估身份摘要"
                        />
                      </p>
                      {ranked.length >= 2 && (
                        <Link
                          className="mt-2 inline-block text-primary hover:underline"
                          to={`/projects/${projectId}/learning/compare?runs=${encodeURIComponent(selections)}`}
                        >
                          比较组内前 {Math.min(4, ranked.length)} 个实验
                        </Link>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
