import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Plus, RefreshCw } from "lucide-react";
import {
  sampleEvaluationsApi,
  type EvaluationAggregate,
  type EvaluationComparison,
} from "@/api/sampleEvaluations";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { lineageReturn } from "@/api/modelLineage";
import { useAuthStore } from "@/stores/authStore";
import { trainingInputClass } from "./TrainingParameterField";
import {
  activeEvaluationStates,
  evaluationStatus,
  metricValue,
  parseEvaluationState,
} from "./sampleEvaluation";
import { evaluationReportOptions, evaluationTask } from "./useSampleEvaluation";
import { useTrainingSessionRevision, useTrainingViewOwner } from "./trainingViewOwner";
import { SampleEvaluationGallery } from "./SampleEvaluationGallery";
import { PickerPage } from "./SampleEvaluationCreate";
import { listPageSize } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";

export function SampleEvaluationPage() {
  const { id = "", evaluationId } = useParams();
  const userId = useAuthStore((s) => s.user?.id);
  const token = useAuthStore((s) => s.token);
  const revision = useTrainingSessionRevision();
  const scope = useMemo(
    () => ({ key: randomId(), id, evaluationId, userId, token, revision }),
    [id, evaluationId, userId, token, revision],
  );
  return (
    <EvaluationContent
      key={scope.key}
      projectId={id}
      evaluationId={evaluationId}
      scope={scope.key}
    />
  );
}

function EvaluationContent({
  projectId,
  evaluationId,
  scope,
}: {
  projectId: string;
  evaluationId?: string;
  scope: string;
}) {
  const { assert } = useTrainingViewOwner(projectId, scope);
  const location = useLocation();
  const view = useMemo(() => ({ key: randomId(), locationKey: location.key }), [location.key]);
  const currentView = useRef(view.key);
  currentView.current = view.key;
  const [params, setParams] = useSearchParams();
  const parsedState = parseEvaluationState(params);
  const update = (values: Record<string, string | undefined>, replace = false) =>
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous);
        Object.entries(values).forEach(([key, value]) =>
          value === undefined ? next.delete(key) : next.set(key, value),
        );
        return next;
      },
      { replace },
    );
  const [pickerPage, setPickerPage] = useState(0);
  const [deleting, setDeleting] = useState(false);
  const client = useQueryClient();
  const navigate = useNavigate();
  const listPage = evaluationId ? pickerPage : parsedState.page;
  const pageSize = listPageSize(params.get("page_size"));
  const list = useQuery({
    queryKey: ["sample-evaluations", projectId, scope, listPage, pageSize],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.list(
        projectId,
        listPage * pageSize,
        { signal },
        pageSize,
      );
      assert(signal);
      if (result.items.some((item) => item.project_id !== projectId))
        throw new Error("评估列表所属项目不符");
      return result;
    },
    retry: false,
    refetchInterval: (q) =>
      q.state.data?.items.some((item) => activeEvaluationStates.has(item.status)) ? 5000 : false,
  });
  const detail = useQuery({
    queryKey: ["sample-evaluation", projectId, scope, evaluationId],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.get(projectId, evaluationId!, { signal });
      assert(signal);
      if (
        result.project_id !== projectId ||
        result.id !== evaluationId ||
        result.spec.evaluation_id !== evaluationId
      )
        throw new Error("评估所属项目或身份不符");
      return result;
    },
    enabled: !!evaluationId,
    retry: false,
    refetchInterval: (q) =>
      q.state.data && activeEvaluationStates.has(q.state.data.status) ? 3000 : false,
  });
  const run = detail.error ? undefined : detail.data;
  const state = {
    ...parsedState,
    error:
      parsedState.error ||
      (run &&
      parsedState.filters.class_index !== undefined &&
      parsedState.filters.class_index >= run.spec.classes.length
        ? "筛选类别不属于冻结类别集合"
        : ""),
  };
  useEffect(() => {
    if (run?.current_attempt_id && !params.has("attempt") && !state.error) {
      setParams(
        (previous) => {
          const next = new URLSearchParams(previous);
          if (!next.has("attempt")) next.set("attempt", run.current_attempt_id!);
          return next;
        },
        { replace: true },
      );
    }
  }, [run?.current_attempt_id, params, state.error, setParams]);
  const attempt = run?.attempts.find((item) => item.id === state.attemptId);
  const report = useQuery({
    ...evaluationReportOptions(
      projectId,
      evaluationId ?? "",
      state.attemptId ?? "",
      run?.spec_digest ?? "",
      scope,
      assert,
    ),
    enabled: !!run && !!attempt?.report && !state.error && !run.deletion_requested_at,
  });
  const secondDetail = useQuery({
    queryKey: ["sample-evaluation", projectId, scope, state.secondId],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.get(projectId, state.secondId!, { signal });
      assert(signal);
      if (result.project_id !== projectId || result.id !== state.secondId)
        throw new Error("第二候选身份不符");
      return result;
    },
    enabled: !!state.secondId && state.secondId !== evaluationId && !state.error,
    retry: false,
    refetchInterval: (q) =>
      q.state.data && activeEvaluationStates.has(q.state.data.status) ? 3000 : false,
  });
  const comparison = useQuery({
    queryKey: [
      "sample-evaluation-comparison",
      projectId,
      scope,
      evaluationId,
      state.attemptId,
      state.secondId,
      run?.current_attempt_id,
      run?.status,
      secondDetail.data?.current_attempt_id,
      secondDetail.data?.status,
    ],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.compare(projectId, evaluationId!, state.secondId!, {
        signal,
      });
      assert(signal);
      if (
        result.candidates.length !== 2 ||
        result.candidates[0].evaluation_id !== evaluationId ||
        result.candidates[1].evaluation_id !== state.secondId ||
        result.candidates[0].attempt_id !== state.attemptId ||
        result.candidates[1].attempt_id !== secondDetail.data?.current_attempt_id
      )
        throw new Error("比较候选或执行尝试已变化，请重新选择");
      return result;
    },
    enabled:
      !!run &&
      !!state.secondId &&
      state.secondId !== evaluationId &&
      !!secondDetail.data &&
      !secondDetail.error &&
      state.attemptId === run.current_attempt_id &&
      !state.error,
    retry: false,
  });
  const mutation = useMutation({
    mutationFn: async (action: "cancel" | "retry" | "remove") => {
      assert();
      const submittedView = currentView.current;
      const result = await sampleEvaluationsApi[action](projectId, evaluationId!);
      assert();
      if (result && (result.id !== evaluationId || result.project_id !== projectId))
        throw new Error("评估操作返回身份不符");
      return { action, result, submittedView };
    },
    onSuccess: async ({ action, result, submittedView }) => {
      assert();
      if (action === "remove")
        navigate(`/projects/${projectId}/learning/evaluations`, { replace: true });
      else {
        await client.invalidateQueries({
          queryKey: ["sample-evaluation", projectId, scope, evaluationId],
        });
        assert();
        if (
          currentView.current === submittedView &&
          action === "retry" &&
          result?.current_attempt_id
        )
          update({ attempt: result.current_attempt_id, sample: undefined, page: undefined });
        void client.invalidateQueries({ queryKey: ["sample-evaluations", projectId] });
      }
    },
  });
  return (
    <PageContainer>
      {params.get("returnTo") && (
        <Link
          className="mb-3 inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
          to={lineageReturn(projectId, params.get("returnTo"))}
        >
          <ArrowLeft className="size-3.5" />
          返回来源与使用记录
        </Link>
      )}
      <Link
        to={
          evaluationId
            ? `/projects/${projectId}/learning/evaluations`
            : `/projects/${projectId}/learning?tab=models`
        }
        className="mb-4 inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-3.5" />
        {evaluationId ? "评估历史" : "返回项目模型"}
      </Link>
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="break-words text-xl font-semibold">{run?.name ?? "逐样本评估"}</h1>
          <p className="mt-1 text-sm text-muted-foreground">冻结成员上的误差证据与候选比较。</p>
        </div>
        <Link
          to={`/projects/${projectId}/learning/evaluations/new`}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-3 py-2 text-xs font-medium hover:bg-muted"
        >
          <Plus className="size-3.5" />
          新建评估
        </Link>
      </div>
      {state.error && (
        <div
          role="alert"
          className="mb-4 rounded-md bg-status-danger-soft p-4 text-sm text-status-danger"
        >
          <p>链接参数无效：{state.error}。保留原选择。</p>
          <Button size="sm" className="mt-2" onClick={() => setParams(new URLSearchParams())}>
            清空链接参数
          </Button>
        </div>
      )}
      {!evaluationId && !state.error && (
        <>
          {list.isPending && (
            <p role="status" className="text-sm text-muted-foreground">
              正在加载评估历史…
            </p>
          )}
          {list.error && (
            <p role="alert" className="text-sm text-status-danger">
              <CopyableError message={list.error.message} />
            </p>
          )}
          {list.data && !list.error && (
            <>
              <div className="divide-y divide-border rounded-lg border border-border bg-card">
                {list.data.items.map((item) => (
                  <Link
                    key={item.id}
                    to={`/projects/${projectId}/learning/evaluations/${item.id}?attempt=${item.current_attempt_id}`}
                    className="flex flex-wrap items-center justify-between gap-3 px-4 py-4 hover:bg-muted"
                  >
                    <div className="min-w-0">
                      <p className="break-words text-sm font-medium">{item.name}</p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.spec.samples.length} 个冻结成员 ·{" "}
                        {new Date(item.created_at).toLocaleString("zh-CN")} ·{" "}
                        {item.spec.role === "held_out_test" ? "独立测试" : "模型选择验证"}
                      </p>
                    </div>
                    <Badge>{evaluationStatus[item.status]}</Badge>
                  </Link>
                ))}
                {list.data.total === 0 && (
                  <p className="p-8 text-center text-sm text-muted-foreground">
                    还没有逐样本评估。选择已安装候选和冻结验证成员，开始核对误差。
                  </p>
                )}
              </div>
              <PickerPage
                page={state.page}
                total={list.data.total}
                label="评估历史"
                pageSize={pageSize}
                loading={list.isFetching}
                onPageSizeChange={(size) => update({ page_size: String(size), page: undefined })}
                onChange={(page) => update({ page: String(page) })}
              />
            </>
          )}
        </>
      )}
      {evaluationId && (
        <>
          {detail.isPending && (
            <p role="status" className="text-sm text-muted-foreground">
              正在读取评估与执行历史…
            </p>
          )}
          {detail.error && (
            <div role="alert" className="space-y-2 text-sm text-status-danger">
              <p>
                <CopyableError message={detail.error.message} />
              </p>
              <Button size="sm" onClick={() => void detail.refetch()}>
                重新加载
              </Button>
            </div>
          )}
          {run && (
            <>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge>{evaluationStatus[run.status]}</Badge>
                    <span className="text-xs text-muted-foreground">
                      {run.progress.succeeded ?? 0} / {run.spec.samples.length} 成员成功
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {["queued", "running", "lost", "failed"].includes(run.status) && (
                      <Button
                        size="sm"
                        disabled={mutation.isPending || !!run.deletion_requested_at}
                        onClick={() => mutation.mutate("cancel")}
                      >
                        请求停止
                      </Button>
                    )}
                    {["lost", "failed"].includes(run.status) && !run.deletion_requested_at && (
                      <Button
                        size="sm"
                        disabled={mutation.isPending || run.spec.expires_at * 1000 <= Date.now()}
                        onClick={() => mutation.mutate("retry")}
                      >
                        <RefreshCw className="size-3.5" />
                        恢复未完成成员
                      </Button>
                    )}
                  </div>
                </div>
                {run.status === "lost" && (
                  <p role="status" className="mt-3 text-xs text-status-caution">
                    执行已失联，尚不能据此确认原生推理结束。恢复会先核对同一请求的收据与锁，已验证成员会保留。
                  </p>
                )}
                {run.status === "cancelling" && (
                  <p role="status" className="mt-3 text-xs text-status-caution">
                    停止意图已保存，正在等待后端确认原生请求结束。
                  </p>
                )}
                {run.error_message && (
                  <p className="mt-3 break-words text-xs text-status-danger">
                    <CopyableError message={run.error_message} />
                  </p>
                )}
                {run.deletion_requested_at && (
                  <p role="status" className="mt-3 text-xs text-status-caution">
                    删除已排队，正在清理保留报告。
                  </p>
                )}
                <dl className="mt-4 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
                  <div>
                    <dt className="text-muted-foreground">数据用途</dt>
                    <dd className="mt-1">
                      {run.spec.role === "held_out_test" ? "独立测试，已明确确认" : "模型选择验证"}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">固定匹配设置</dt>
                    <dd className="mt-1">
                      置信度 ≥ {run.spec.confidence} ·{" "}
                      {evaluationTask(run.spec) === "segment" ? "匹配 Mask IoU" : "匹配 IoU"} ≥{" "}
                      {run.spec.matching_iou} · NMS IoU {run.spec.nms_iou}
                    </dd>
                  </div>
                  <div className="flex items-center gap-1">
                    <dt className="text-muted-foreground">候选版本</dt>
                    <dd>
                      <CopyValueButton
                        value={run.model_version_id}
                        kind="id"
                        label="复制候选模型版本 ID"
                      />
                    </dd>
                  </div>
                  <div className="flex items-center gap-1">
                    <dt className="text-muted-foreground">冻结数据版本</dt>
                    <dd>
                      <CopyValueButton
                        value={run.dataset_id}
                        kind="id"
                        label="复制冻结数据版本 ID"
                      />
                    </dd>
                  </div>
                </dl>
                <div className="mt-4 max-w-xl space-y-1 text-xs">
                  <label htmlFor="evaluation-attempt">查看执行尝试</label>
                  <div className="flex items-center gap-2">
                    <select
                      id="evaluation-attempt"
                      className={`${trainingInputClass} min-w-0 flex-1`}
                      value={state.attemptId ?? ""}
                      onChange={(e) =>
                        update({ attempt: e.target.value, sample: undefined, page: undefined })
                      }
                    >
                      <option value="" disabled hidden>
                        选择执行尝试
                      </option>
                      {state.attemptId && !attempt && (
                        <option value={state.attemptId}>链接中的尝试不存在</option>
                      )}
                      {run.attempts.map((item) => (
                        <option key={item.id} value={item.id}>
                          第 {item.ordinal} 次 · {evaluationStatus[item.status]}
                          {item.id === run.current_attempt_id ? " · 当前" : ""}
                        </option>
                      ))}
                    </select>
                    {state.attemptId && (
                      <CopyValueButton
                        value={state.attemptId}
                        kind="id"
                        label="复制所选执行尝试 ID"
                      />
                    )}
                  </div>
                </div>
              </div>
              {mutation.error && mutation.error.name !== "AbortError" && (
                <p role="alert" className="mt-3 text-sm text-status-danger">
                  <CopyableError message={mutation.error.message} />
                </p>
              )}
              {!state.error && state.attemptId && !attempt && (
                <p role="alert" className="mt-4 text-sm text-status-danger">
                  所选尝试不属于此评估，未切换到其他历史。
                </p>
              )}
              {attempt && !state.error && (
                <>
                  {attempt.error_message && (
                    <p className="mt-4 break-words text-xs text-status-caution">
                      所选尝试：
                      <CopyableError message={attempt.error_message} />
                    </p>
                  )}
                  {!attempt.report && (
                    <p
                      role="status"
                      className="mt-4 rounded-lg border border-border p-5 text-sm text-muted-foreground"
                    >
                      此尝试尚无可验证报告。
                      {activeEvaluationStates.has(attempt.status)
                        ? "结果发布后会自动显示。"
                        : "历史状态保留；可以查看其他尝试或新建评估。"}
                    </p>
                  )}
                  {attempt.report && report.isPending && (
                    <p role="status" className="mt-4 text-sm text-muted-foreground">
                      正在校验完整报告…
                    </p>
                  )}
                  {report.error && (
                    <div role="alert" className="mt-4 space-y-2 text-sm text-status-danger">
                      <p>
                        报告不可用：
                        <CopyableError message={report.error.message} />
                      </p>
                      <Button size="sm" onClick={() => void report.refetch()}>
                        重新校验报告
                      </Button>
                    </div>
                  )}
                  {report.data &&
                    !report.error &&
                    !!attempt.report &&
                    !run.deletion_requested_at && (
                      <>
                        {report.data.aggregate ? (
                          <AggregateMetrics aggregate={report.data.aggregate} />
                        ) : (
                          <p
                            role="status"
                            className="mt-4 rounded-md bg-status-caution-soft p-4 text-sm text-status-caution"
                          >
                            本次报告不完整：{report.data.succeeded_samples} 成功、
                            {report.data.failed_samples} 失败、{report.data.pending_samples}{" "}
                            未完成，不提供整体指标。
                          </p>
                        )}
                        <SampleEvaluationGallery
                          key={`${scope}:${params.toString()}`}
                          run={run}
                          report={report.data}
                          state={state}
                          scope={`${scope}:${params.toString()}`}
                          update={update}
                        />
                      </>
                    )}
                </>
              )}
              <section
                className="mt-5 rounded-lg border border-border bg-card p-4"
                aria-labelledby="evaluation-compare"
              >
                <h2 id="evaluation-compare" className="text-sm font-semibold">
                  两个候选比较
                </h2>
                <p className="mt-2 text-xs text-muted-foreground">
                  只在冻结基准、类别、用途、匹配设置和推理环境一致时显示差值。差值为第二候选减当前候选，不提供自动排名。
                </p>
                <div className="mt-3 flex max-w-xl items-end gap-2">
                  <label className="min-w-0 flex-1 space-y-1 text-xs">
                    第二个明确评估
                    <select
                      className={trainingInputClass}
                      value={state.secondId ?? ""}
                      onChange={(e) => update({ second: e.target.value || undefined })}
                    >
                      <option value="" disabled hidden>
                        选择第二个评估
                      </option>
                      {state.secondId &&
                        !list.data?.items.some(
                          (item) => item.id === state.secondId && item.id !== run.id,
                        ) && (
                          <option
                            value={state.secondId}
                            disabled={state.secondId === run.id}
                            hidden={state.secondId === run.id}
                          >
                            {state.secondId === run.id
                              ? `${run.name}（不能与自身比较）`
                              : state.secondId}
                          </option>
                        )}
                      {list.data?.items
                        .filter((item) => item.id !== run.id)
                        .map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name} · {evaluationStatus[item.status]}
                          </option>
                        ))}
                    </select>
                  </label>
                  {state.secondId && (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      onClick={() => update({ second: undefined })}
                    >
                      取消评估比较
                    </Button>
                  )}
                </div>
                <PickerPage
                  page={pickerPage}
                  total={list.data?.total ?? 0}
                  label="比较候选"
                  pageSize={pageSize}
                  onChange={setPickerPage}
                />
                {list.error && (
                  <p role="alert" className="mt-2 text-xs text-status-danger">
                    候选列表：
                    <CopyableError message={list.error.message} />
                  </p>
                )}
                {state.secondId === run.id && (
                  <p role="alert" className="mt-3 text-xs text-status-danger">
                    须选择两个不同的评估。
                  </p>
                )}
                {state.secondId && state.attemptId !== run.current_attempt_id && (
                  <p role="status" className="mt-3 text-xs text-status-caution">
                    当前查看的是历史尝试。候选比较使用各评估的当前尝试，请显式选择当前尝试后比较。
                  </p>
                )}
                {comparison.isFetching && state.secondId && (
                  <p role="status" className="mt-3 text-xs text-muted-foreground">
                    正在校验两个候选报告…
                  </p>
                )}
                {comparison.error && (
                  <p role="alert" className="mt-3 text-xs text-status-danger">
                    <CopyableError message={comparison.error.message} />
                  </p>
                )}
                {comparison.data &&
                  !comparison.error &&
                  !secondDetail.error &&
                  !!secondDetail.data &&
                  !secondDetail.data.deletion_requested_at &&
                  !run.deletion_requested_at &&
                  !state.error &&
                  state.attemptId === run.current_attempt_id && (
                    <ComparisonResults
                      data={comparison.data}
                      projectId={projectId}
                      ordinal={state.ordinal}
                    />
                  )}
              </section>
              <details className="mt-5 rounded-lg border border-border p-4 text-xs">
                <summary className="cursor-pointer font-medium">冻结基准与评估身份</summary>
                <dl className="mt-3 grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-2 gap-y-2 text-muted-foreground">
                  <dt>权重 SHA256</dt>
                  <dd>
                    <CopyValueButton
                      value={String(run.spec.model_ref.sha256 ?? "")}
                      label="复制评估权重哈希"
                    />
                  </dd>
                  <dt>基准摘要</dt>
                  <dd>
                    <CopyValueButton value={run.benchmark_digest} label="复制评估基准摘要" />
                  </dd>
                  <dt>评估设置摘要</dt>
                  <dd>
                    <CopyValueButton value={run.spec_digest} label="复制评估设置摘要" />
                  </dd>
                  <dt>推理环境</dt>
                  <dd>
                    训练版本 {String(run.spec.model_ref.framework_version ?? "未知")} · 推理版本
                    Ultralytics {run.spec.profile.ultralytics_version} ·{" "}
                    {run.spec.profile.effective_device}
                  </dd>
                </dl>
                <p className="mt-3 text-muted-foreground">
                  固定阈值一对一匹配，不等同于训练报告中的 AP/mAP；分母为零的指标标记为不可用。
                </p>
              </details>
              {["succeeded", "failed", "cancelled"].includes(run.status) && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="mt-5 text-status-danger"
                  disabled={mutation.isPending}
                  onClick={() => setDeleting(true)}
                >
                  删除本次评估及报告
                </Button>
              )}
              {deleting && (
                <Modal open onClose={() => setDeleting(false)} title="删除评估" width={480}>
                  <p className="text-sm">
                    将删除“{run.name}”的执行历史与报告，冻结数据版本和当前标注任务仍保留。
                  </p>
                  <div className="mt-5 flex justify-end gap-2">
                    <Button size="sm" onClick={() => setDeleting(false)}>
                      保留评估
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={mutation.isPending}
                      onClick={() => mutation.mutate("remove")}
                    >
                      {mutation.isPending ? "正在删除…" : "删除评估"}
                    </Button>
                  </div>
                </Modal>
              )}
            </>
          )}
          {secondDetail.error && (
            <p role="alert" className="mt-3 text-xs text-status-danger">
              第二候选不可用：
              <CopyableError message={secondDetail.error.message} />
            </p>
          )}
        </>
      )}
    </PageContainer>
  );
}

const metrics = [
  ["tp", "TP", false],
  ["fp", "FP", false],
  ["fn", "FN", false],
  ["precision", "精确率", true],
  ["recall", "召回率", true],
  ["f1", "F1", true],
] as const;
const maskMetrics = [
  ["matched_mean_iou", "已匹配 Mask IoU", true],
  ["matched_mean_dice", "已匹配 Dice", true],
] as const;
type MetricKey = (typeof metrics)[number][0] | (typeof maskMetrics)[number][0];
function aggregateValue(aggregate: EvaluationAggregate | null | undefined, key: MetricKey) {
  if (key === "matched_mean_iou" || key === "matched_mean_dice")
    return aggregate && "matched_mean_iou" in aggregate ? aggregate[key] : undefined;
  return aggregate?.[key];
}
function AggregateMetrics({ aggregate }: { aggregate: EvaluationAggregate }) {
  const classes = useListPagination(aggregate.per_class);
  return (
    <section className="mt-5 rounded-lg border border-border bg-card p-4" aria-label="完整评估指标">
      <dl className="grid grid-cols-3 gap-4 sm:grid-cols-6">
        {metrics.map(([key, label, fraction]) => (
          <div key={key}>
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="mt-1 text-lg font-semibold tabular-nums">
              {metricValue(aggregate[key], fraction)}
            </dd>
          </div>
        ))}
      </dl>
      {"matched_mean_iou" in aggregate && (
        <div className="mt-4 border-t border-border pt-4">
          <dl className="grid grid-cols-2 gap-4">
            {maskMetrics.map(([key, label]) => (
              <div key={key}>
                <dt className="text-xs text-muted-foreground">{label}</dt>
                <dd className="mt-1 text-lg font-semibold tabular-nums">
                  {metricValue(aggregate[key], true)}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            Mask IoU、Dice 均值仅统计已匹配实例，漏检另计 FN；固定阈值像素指标不是 AP/mAP。
          </p>
          <div className="overflow-auto">
            <table className="mt-3 w-full text-xs" aria-label="按原图掩码像素面积统计">
              <caption className="mb-2 text-left text-muted-foreground">
                按原图前景像素面积分组：小 &lt; 1,024；中 1,024–9,215；大 ≥ 9,216。TP/FN
                按真值面积，FP 按预测面积。
              </caption>
              <thead>
                <tr>
                  <th className="py-2 text-left font-normal">面积组</th>
                  {metrics.map(([, label]) => (
                    <th key={label} className="px-2 py-2 text-right font-normal">
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {aggregate.area_groups.map((group) => (
                  <tr key={group.group} className="border-t border-border">
                    <th className="py-2 text-left font-normal">
                      {{ small: "小", medium: "中", large: "大" }[group.group]}
                    </th>
                    {metrics.map(([key, , fraction]) => (
                      <td key={key} className="px-2 py-2 text-right tabular-nums">
                        {metricValue(group[key], fraction)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <details className="mt-4 text-xs">
        <summary className="cursor-pointer text-muted-foreground">按类别统计与诊断</summary>
        <div className="mt-3 overflow-auto">
          <table className={listStyles.table}>
            <thead>
              <tr className="text-muted-foreground">
                <th className="py-2 text-left">类别</th>
                {metrics.map(([, label]) => (
                  <th key={label} className="px-2 py-2 text-right font-normal">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {classes.items.map((item) => (
                <tr key={item.class_index} className="border-t border-border">
                  <td className="py-2">{item.name}</td>
                  {metrics.map(([key, , fraction]) => (
                    <td key={key} className="px-2 py-2 text-right tabular-nums">
                      {metricValue(item[key], fraction)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <PickerPage
          label="评估类别"
          page={classes.page}
          pageSize={classes.pageSize}
          total={classes.total}
          onChange={classes.setPage}
          onPageSizeChange={classes.setPageSize}
        />
        <p className="mt-3 text-muted-foreground">
          混淆 {aggregate.diagnostics.confusion ?? 0} · 定位偏差{" "}
          {aggregate.diagnostics.localization ?? 0}。诊断不重复计数。
        </p>
        {aggregate.confusion.map((item) => (
          <p key={`${item.truth_class_index}:${item.prediction_class_index}`} className="mt-1">
            {aggregate.per_class[item.truth_class_index]?.name} →{" "}
            {aggregate.per_class[item.prediction_class_index]?.name}：{item.count}
          </p>
        ))}
      </details>
    </section>
  );
}

function ComparisonResults({
  data,
  projectId,
  ordinal,
}: {
  data: EvaluationComparison;
  projectId: string;
  ordinal?: number;
}) {
  const pair =
    ordinal === undefined
      ? undefined
      : data.sample_pairs.find((item) => item.left_ordinal === ordinal);
  const second = data.candidates[1];
  const comparisonMetrics = data.candidates.some(
    (item) => item.aggregate && "matched_mean_iou" in item.aggregate,
  )
    ? [...metrics, ...maskMetrics]
    : metrics;
  return (
    <div className="mt-4">
      {!data.compatible && (
        <p
          role="status"
          className="mb-3 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution"
        >
          无法比较：{data.reasons.join("；")}。各候选仍独立保留。
        </p>
      )}
      <div className="overflow-auto">
        <table className={listStyles.table}>
          <thead>
            <tr className="text-muted-foreground">
              <th className="py-2 text-left font-normal">指标</th>
              {data.candidates.map((item) => (
                <th key={item.evaluation_id} className="min-w-32 px-3 py-2 text-right font-normal">
                  <Link
                    className="text-primary hover:underline"
                    to={`/projects/${projectId}/learning/evaluations/${item.evaluation_id}?attempt=${item.attempt_id}`}
                  >
                    {item.name}
                  </Link>
                  <span className="mt-1 block">{evaluationStatus[item.status]}</span>
                </th>
              ))}
              {data.compatible && <th className="px-3 py-2 text-right font-normal">第二减当前</th>}
            </tr>
          </thead>
          <tbody>
            {comparisonMetrics.map(([key, label, fraction]) => (
              <tr key={key} className="border-t border-border">
                <th className="py-2 text-left font-normal">{label}</th>
                {data.candidates.map((item) => (
                  <td key={item.evaluation_id} className="px-3 py-2 text-right tabular-nums">
                    {metricValue(aggregateValue(item.aggregate, key), fraction)}
                  </td>
                ))}
                {data.compatible && (
                  <td className="px-3 py-2 text-right tabular-nums">
                    {data.delta_right_minus_left?.[key] === null
                      ? "不可用"
                      : fraction
                        ? `${((data.delta_right_minus_left?.[key] ?? 0) * 100).toFixed(1)} 个百分点`
                        : metricValue(data.delta_right_minus_left?.[key])}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.compatible && pair && second && (
        <Link
          className="mt-3 inline-flex text-xs text-primary hover:underline"
          to={`/projects/${projectId}/learning/evaluations/${second.evaluation_id}?attempt=${second.attempt_id}&sample=${pair.right_ordinal}&second=${data.candidates[0].evaluation_id}`}
        >
          查看第二候选中的同一冻结成员（样本 {pair.right_ordinal + 1}）
        </Link>
      )}
    </div>
  );
}
