import { listStyles } from "@/components/ui/listStyles";
import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import {
  activeLearningApi,
  type ActiveRound,
  type Arm,
  type DecisionInput,
  type FeedbackInput,
  type FrozenSample,
  type MaskPrediction,
} from "@/api/activeLearning";
import { useAuthStore } from "@/stores/authStore";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Badge } from "@/components/ui/Badge";
import { trainingInputClass } from "./TrainingParameterField";
import { useTrainingViewOwner, type AssertTrainingOwner } from "./trainingViewOwner";
import {
  armLabel,
  definitiveRejection,
  loadPending,
  activeLearningReason,
  activeLearningTask,
  reportOptions,
  roundStatus,
  runningStates,
  savePending,
  useActiveLearningScope,
} from "./useActiveLearning";
import { trainingTaskLabel } from "./trainingPresentation";
import { ActiveLearningMaskPreview } from "./ActiveLearningMaskPreview";

export function ActiveLearningRoundPage() {
  const { id = "", roundId = "" } = useParams();
  const scope = useActiveLearningScope(id, roundId);
  return <RoundContent key={scope} projectId={id} roundId={roundId} scope={scope} />;
}
export function RoundContent({
  projectId,
  roundId,
  scope,
}: {
  projectId: string;
  roundId: string;
  scope: string;
}) {
  const { assert } = useTrainingViewOwner(projectId, scope);
  const userId = useAuthStore((state) => state.user?.id);
  const [params, setParams] = useSearchParams();
  const client = useQueryClient();
  const [selected, setSelected] = useState<number[]>([]);
  const [focused, setFocused] = useState<number | null>(null);
  const [batchName, setBatchName] = useState("主动学习人工复核");
  const decisionKey = `active-learning-decision:${userId}:${projectId}:${roundId}`;
  const feedbackKey = `active-learning-feedback:${userId}:${projectId}:${roundId}`;
  const [pendingDecision, setPendingDecision] = useState(() =>
    loadPending<DecisionInput>(decisionKey),
  );
  const [pendingFeedback, setPendingFeedback] = useState(() =>
    loadPending<FeedbackInput>(feedbackKey),
  );
  const [evaluationIds, setEvaluationIds] = useState(["", ""]);
  const [evaluationAttempts, setEvaluationAttempts] = useState(["", ""]);
  const [storageError, setStorageError] = useState("");
  const guarded = async <T,>(signal: AbortSignal, operation: () => Promise<T>) => {
    assert(signal);
    const value = await operation();
    assert(signal);
    return value;
  };
  const detail = useQuery({
    queryKey: ["active-learning-round", projectId, scope, roundId],
    retry: false,
    queryFn: async ({ signal }) => {
      const value = await guarded(signal, () =>
        activeLearningApi.get(projectId, roundId, { signal }),
      );
      if (value.id !== roundId || value.project_id !== projectId || value.spec.round_id !== roundId)
        throw new Error("选样轮次身份不符");
      const task = activeLearningTask(value.spec.model_ref.task ?? value.spec.task);
      if (
        !task ||
        (task === "segment" && (value.spec.schema !== 2 || value.spec.task !== "segment"))
      )
        throw new Error("选样轮次任务或分割规格不受支持");
      return value;
    },
    refetchInterval: (query) =>
      query.state.data && runningStates.has(query.state.data.status) ? 3000 : false,
  });
  const round = detail.error ? undefined : detail.data;
  const task = activeLearningTask(round?.spec.model_ref.task ?? round?.spec.task) ?? "detect";
  const attemptId = params.get("attempt") ?? round?.current_attempt_id ?? "";
  const attempt = round?.attempts.find((item) => item.id === attemptId);
  const report = useQuery({
    ...reportOptions(projectId, round, attemptId, scope, assert),
    enabled: !!round && !!attempt?.has_report,
  });
  const rows = useQuery({
    queryKey: [
      "active-learning-candidates",
      projectId,
      scope,
      roundId,
      round?.status,
      round?.accepted_tasks,
    ],
    retry: false,
    queryFn: async ({ signal }) => {
      const value = await guarded(signal, () =>
        activeLearningApi.candidates(projectId, roundId, { signal }),
      );
      if (
        value.items.some((item) =>
          item.evidence?.predictions.some((prediction) =>
            task === "segment" ? !("rle" in prediction) : !("xyxy" in prediction),
          ),
        )
      )
        throw new Error("候选几何证据与本轮任务不一致");
      return value;
    },
    enabled: !!round,
    refetchInterval: round && runningStates.has(round.status) ? 3000 : false,
  });
  const [decisionPage, setDecisionPage] = useState(0);
  const [decisionPageSize, setDecisionPageSize] = useState(20);
  const [feedbackPage, setFeedbackPage] = useState(0);
  const [feedbackPageSize, setFeedbackPageSize] = useState(20);
  const decisions = useQuery({
    queryKey: [
      "active-learning-decisions",
      projectId,
      scope,
      roundId,
      decisionPage,
      decisionPageSize,
    ],
    retry: false,
    queryFn: ({ signal }) =>
      guarded(signal, () =>
        activeLearningApi.decisions(
          projectId,
          roundId,
          { signal },
          decisionPage * decisionPageSize,
          decisionPageSize,
        ),
      ),
    enabled: !!round,
  });
  const feedback = useQuery({
    queryKey: [
      "active-learning-feedback",
      projectId,
      scope,
      roundId,
      feedbackPage,
      feedbackPageSize,
    ],
    retry: false,
    queryFn: ({ signal }) =>
      guarded(signal, () =>
        activeLearningApi.feedback(
          projectId,
          roundId,
          { signal },
          feedbackPage * feedbackPageSize,
          feedbackPageSize,
        ),
      ),
    enabled: !!round,
  });
  const refresh = async () => {
    assert();
    await client.invalidateQueries({
      predicate: (query) => query.queryKey[1] === projectId && query.queryKey[2] === scope,
    });
    assert();
  };
  const action = useMutation({
    mutationFn: async (operation: "cancel" | "retry") => {
      assert();
      const value = await activeLearningApi[operation](projectId, roundId);
      assert();
      if (value.id !== roundId || value.project_id !== projectId)
        throw new Error("操作返回的轮次身份不符");
      return value;
    },
    onSuccess: () => refresh(),
  });
  const decide = useMutation({
    mutationFn: async (body: DecisionInput) => {
      assert();
      const value = await activeLearningApi.decide(projectId, roundId, body);
      assert();
      if (value.request_id !== body.request_id) throw new Error("决策返回的请求身份不符");
      return value;
    },
    onSuccess: async () => {
      assert();
      savePending(decisionKey, null);
      setPendingDecision(null);
      setSelected([]);
      await refresh();
    },
    onError: (error) => {
      try {
        assert();
      } catch {
        return;
      }
      if (definitiveRejection(error)) {
        savePending(decisionKey, null);
        setPendingDecision(null);
      }
    },
  });
  const record = useMutation({
    mutationFn: async (body: FeedbackInput) => {
      assert();
      const value = await activeLearningApi.recordFeedback(projectId, roundId, body);
      assert();
      return value;
    },
    onSuccess: async () => {
      assert();
      savePending(feedbackKey, null);
      setPendingFeedback(null);
      await refresh();
    },
    onError: (error) => {
      try {
        assert();
      } catch {
        return;
      }
      if (definitiveRejection(error)) {
        savePending(feedbackKey, null);
        setPendingFeedback(null);
      }
    },
  });
  const sendDecision = (operation: "accept" | "reject") => {
    const body: DecisionInput = {
      request_id: randomId(),
      action: operation,
      ordinals: [...selected].sort((a, b) => a - b),
      batch_name: batchName.trim(),
    };
    if (!savePending(decisionKey, body)) {
      setStorageError("无法保存决策请求，请允许会话存储后重试。");
      return;
    }
    setPendingDecision(body);
    decide.mutate(body);
  };
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const validEvaluations = evaluationIds.every(
    (id, index) =>
      (!id.trim() && !evaluationAttempts[index].trim()) ||
      (uuid.test(id.trim()) && uuid.test(evaluationAttempts[index].trim())),
  );
  const sendFeedback = () => {
    const body: FeedbackInput = {
      request_id: randomId(),
      evaluations: evaluationIds.flatMap((id, index) =>
        id.trim()
          ? [{ evaluation_id: id.trim(), attempt_id: evaluationAttempts[index].trim() }]
          : [],
      ),
    };
    if (!savePending(feedbackKey, body)) {
      setStorageError("无法保存反馈请求，请允许会话存储后重试。");
      return;
    }
    setPendingFeedback(body);
    record.mutate(body);
  };
  const candidateRows = rows.error ? [] : (rows.data?.items ?? []);
  const ranking = report.error ? [] : (report.data?.ranking ?? []);
  const activePage = useListPagination(
    ranking.filter((item) => item.arm === "active"),
    roundId,
  );
  const controlPage = useListPagination(
    ranking.filter((item) => item.arm === "random_control"),
    roundId,
  );
  const focus = candidateRows.find((item) => item.ordinal === focused);
  const sample = round?.spec.samples.find((item) => item.ordinal === focused);
  const canDecide =
    round?.status === "succeeded" &&
    attemptId === round.current_attempt_id &&
    !!report.data &&
    !report.error &&
    !rows.error &&
    !!rows.data;
  const canAccept =
    selected.length > 0 &&
    selected.every(
      (ordinal) =>
        ranking.some((item) => item.ordinal === ordinal && item.recommended) &&
        !candidateRows.find((item) => item.ordinal === ordinal)?.disposition,
    );
  const locked = decide.isPending || !!pendingDecision;
  return (
    <PageContainer>
      <Link
        className="inline-flex items-center gap-2 text-xs text-muted-foreground"
        to={`/projects/${projectId}/learning/active-learning`}
      >
        <ArrowLeft size={14} />
        返回选样轮次
      </Link>
      {detail.isPending && <p role="status">加载轮次…</p>}
      {round && (
        <>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
            <h1 className="min-w-0 break-words text-xl font-semibold">{round.name}</h1>
            <Badge
              variant={
                round.status === "succeeded"
                  ? "success"
                  : round.status === "failed"
                    ? "danger"
                    : "default"
              }
            >
              {roundStatus[round.status]}
            </Badge>
          </div>
          <p className="text-sm text-muted-foreground">
            {round.spec.candidate_count} 个独立候选 · 每组计划 {round.spec.batch_size} 张 · 派发{" "}
            {round.inference_dispatches}/{round.spec.max_inference_dispatches} · 已接纳{" "}
            {round.accepted_tasks}/{round.spec.max_review_tasks} 张
          </p>
          <div className="mb-4 flex flex-wrap gap-3">
            <Link
              className="rounded-md border border-border px-3 py-2 text-xs text-primary hover:bg-muted"
              to={`/projects/${projectId}/learning/active-learning/new`}
            >
              新建下一轮
            </Link>
            {(runningStates.has(round.status) || round.status === "lost") && (
              <Button size="sm" disabled={action.isPending} onClick={() => action.mutate("cancel")}>
                取消并确认停止
              </Button>
            )}
            {["lost", "failed"].includes(round.status) && (
              <Button
                size="sm"
                disabled={action.isPending || round.spec.expires_at * 1000 <= Date.now()}
                onClick={() => action.mutate("retry")}
              >
                核对同一请求并恢复
              </Button>
            )}
            <Button size="sm" disabled={detail.isFetching} onClick={() => void refresh()}>
              刷新
            </Button>
          </div>
          {round.status === "cancelling" && (
            <p role="status" className="text-sm text-status-caution">
              正在核对并封存所有已派发请求。暂时无法确认退出时会保留此状态。
            </p>
          )}
          {round.error_message && (
            <p role="alert" className="text-sm text-status-danger">
              <CopyableError message={round.error_message} />
            </p>
          )}
          <details className="mb-5 rounded-md border border-border p-3 text-xs">
            <summary className="cursor-pointer font-medium">固定来源与执行尝试</summary>
            <dl className="grid min-w-0 grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-2 gap-y-2 text-muted-foreground">
              <dt>规格摘要</dt>
              <dd className="m-0">
                <CopyValueButton value={round.spec_digest} label="复制选样规格摘要" />
              </dd>
              <dt>选样任务</dt>
              <dd className="m-0">{trainingTaskLabel(task)}</dd>
              {task === "segment" && (
                <>
                  <dt>选样信号</dt>
                  <dd className="m-0">实例置信度与跨类别原图掩码 IoU</dd>
                </>
              )}
              <dt>权重 SHA256</dt>
              <dd className="m-0">
                <CopyValueButton
                  value={String(round.spec.model_ref.sha256 ?? "")}
                  label="复制权重哈希"
                />
              </dd>
              <dt>推理环境摘要</dt>
              <dd className="m-0">
                <CopyValueButton value={round.spec.profile_digest} label="复制推理环境摘要" />
              </dd>
            </dl>
            <label className="mt-3 block">
              执行尝试
              <select
                className={`${trainingInputClass} ml-2 max-w-full`}
                value={attemptId}
                onChange={(event) => setParams({ attempt: event.target.value })}
              >
                {round.attempts.map((item) => (
                  <option key={item.id} value={item.id}>
                    #{item.ordinal} · {roundStatus[item.status]}
                  </option>
                ))}
              </select>
            </label>
          </details>
          {!!params.get("attempt") && !attempt && (
            <p role="alert" className="text-sm text-status-danger">
              执行尝试不属于本轮次或不在最近记录中，请选择有效尝试。
            </p>
          )}
          {!!attempt?.has_report && report.isPending && <p role="status">核验选样报告…</p>}
          {!attempt?.has_report && !["failed", "cancelled", "lost"].includes(round.status) && (
            <p role="status" className="text-sm text-muted-foreground">
              推理完成并核验证据后显示完整建议。{task === "segment" ? "零实例" : "零检测"}
              只表示缺少信号。
            </p>
          )}
          {report.data && !report.error && (
            <>
              <p className="text-xs text-muted-foreground">
                {task === "segment"
                  ? "低置信度信号取各实例 1−置信度的最大值，重叠信号取跨类别原图掩码 IoU 的最大值；优先级取两者较大值。不使用像素概率，也不是 AP。"
                  : "低置信度与类别重叠是检测启发式。"}
                {report.data.missing_signal_count} 个候选无{task === "segment" ? "实例" : "检测"}
                信号，将随机补位；随机对照来自评分前的独立分组。本轮未证明质量、效率或因果收益。
              </p>
              <div className="grid gap-5 lg:grid-cols-2">
                {(["active", "random_control"] as Arm[]).map((arm) => (
                  <section
                    key={arm}
                    aria-label={armLabel[arm]}
                    className="min-w-0 rounded-lg border border-border bg-card p-4"
                  >
                    <h2 className="mb-3 text-sm font-semibold">
                      {armLabel[arm]} · 计划 {round.spec.batch_size} 张
                    </h2>
                    <div className="divide-y divide-border">
                      {(arm === "active" ? activePage.items : controlPage.items).map((item) => {
                        const candidate = candidateRows.find((row) => row.ordinal === item.ordinal);
                        return (
                          <div key={item.ordinal} className="flex items-start gap-3 py-3 text-xs">
                            <input
                              className="mt-1"
                              type="checkbox"
                              aria-label={`选择${armLabel[arm]} ${item.rank}`}
                              checked={selected.includes(item.ordinal)}
                              disabled={!canDecide || locked || !!candidate?.disposition}
                              onChange={(event) =>
                                setSelected((old) =>
                                  event.target.checked
                                    ? [...old, item.ordinal]
                                    : old.filter((ordinal) => ordinal !== item.ordinal),
                                )
                              }
                            />
                            <div className="min-w-0 flex-1">
                              <div className="flex min-w-0 items-center gap-1">
                                <button
                                  type="button"
                                  className="text-left text-primary hover:underline"
                                  onClick={() => setFocused(item.ordinal)}
                                >
                                  #{item.rank} · {activeLearningReason(item.reason, task)}
                                </button>
                                <CopyValueButton
                                  value={item.task_id}
                                  kind="id"
                                  label="复制样本任务 ID"
                                />
                                <span className="text-muted-foreground">
                                  {item.recommended ? "计划内" : "备选"}
                                  {candidate?.disposition === "accepted"
                                    ? " · 已接纳"
                                    : candidate?.disposition === "rejected"
                                      ? " · 已拒绝"
                                      : ""}
                                </span>
                              </div>
                              <p className="mb-0 mt-1 tabular-nums text-muted-foreground">
                                优先级{" "}
                                {item.features.priority === null
                                  ? "未知"
                                  : item.features.priority.toFixed(3)}{" "}
                                · {item.features.detections} 个
                                {task === "segment" ? "实例" : "检测"}
                                {item.features.possible_truncation ? " · 输出可能截断" : ""}
                              </p>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                    <ListPagination
                      label={armLabel[arm]}
                      page={arm === "active" ? activePage.page : controlPage.page}
                      pageSize={arm === "active" ? activePage.pageSize : controlPage.pageSize}
                      total={arm === "active" ? activePage.total : controlPage.total}
                      onPageChange={arm === "active" ? activePage.setPage : controlPage.setPage}
                      onPageSizeChange={
                        arm === "active" ? activePage.setPageSize : controlPage.setPageSize
                      }
                    />
                  </section>
                ))}
              </div>
              {focus && sample && (
                <section
                  className="mt-5 rounded-lg border border-border bg-card p-4"
                  aria-label="候选证据"
                >
                  <h2 className="mb-3 text-sm font-semibold">
                    候选 #{focus.ordinal + 1} · {armLabel[sample.arm]}
                  </h2>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <CandidateImage
                      round={round}
                      ordinal={focus.ordinal}
                      scope={scope}
                      assert={assert}
                      sample={sample}
                      maskPredictions={
                        task === "segment"
                          ? (focus.evidence?.predictions as MaskPrediction[])
                          : undefined
                      }
                    />
                    <div className="min-w-0 text-xs">
                      <p className="flex items-center gap-1">
                        SHA256 <CopyValueButton value={sample.sha256} label="复制样本哈希" />
                      </p>
                      <p>
                        {sample.width} × {sample.height} · 同组来源 {sample.group_task_ids.length}{" "}
                        张
                      </p>
                      <p>
                        {task === "segment" ? "实例低置信度" : "低置信度"}{" "}
                        {focus.evidence?.features.low_confidence?.toFixed(3) ?? "未知"} ·
                        {task === "segment" ? "跨类掩码 IoU" : "类别重叠"}{" "}
                        {focus.evidence?.features.ambiguity?.toFixed(3) ?? "未知"}
                      </p>
                      {task === "segment" && (
                        <p>
                          最小实例 {focus.evidence?.features.minimum_instance_pixels ?? "未知"} 像素
                          · 实例像素合计（含重叠）
                          {focus.evidence?.features.total_instance_pixels_including_overlap ??
                            "未知"}{" "}
                          · 重叠计算游程访问 {focus.evidence?.features.overlap_run_visits ?? "未知"}
                        </p>
                      )}
                      <p className="text-muted-foreground">
                        {task === "segment" ? "逐实例掩码" : "这些框"}
                        用于解释选样，没有写入标注真值。
                      </p>
                      <details>
                        <summary className="cursor-pointer">
                          {task === "segment" ? "查看逐实例掩码证据" : "查看原生检测框"}
                        </summary>
                        <ul className="max-h-64 overflow-auto pl-4">
                          {focus.evidence?.predictions.map((prediction, index) => (
                            <li key={index} className="mt-2 break-words">
                              类别 {prediction.class_index} · 置信度 {prediction.score.toFixed(3)} ·{" "}
                              {"rle" in prediction ? (
                                <>
                                  {prediction.area_pixels} 像素 · 原图 {prediction.rle.size[1]} ×{" "}
                                  {prediction.rle.size[0]} ·{" "}
                                  <CopyValueButton
                                    value={prediction.mask_sha256}
                                    label="复制候选实例掩码哈希"
                                  />
                                </>
                              ) : (
                                prediction.xyxy.map((value) => value.toFixed(3)).join(", ")
                              )}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </div>
                  </div>
                </section>
              )}
              <section className="mt-5 rounded-lg border border-border p-4" aria-label="人工决策">
                <h2 className="mb-3 text-sm font-semibold">
                  接纳或拒绝所选候选 · {selected.length} 张
                </h2>
                <p className="text-xs text-muted-foreground">
                  接纳仅限两组计划内候选，并创建普通草稿批次。任何来源、分派、编辑锁或历史成员变化都会阻止接纳。
                </p>
                <label className="block text-xs">
                  新批次名称
                  <input
                    className={`${trainingInputClass} mt-2 w-full sm:max-w-md`}
                    value={batchName}
                    maxLength={120}
                    disabled={locked}
                    onChange={(event) => setBatchName(event.target.value)}
                  />
                </label>
                <div className="mt-3 flex flex-wrap gap-3">
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={!canDecide || !canAccept || !batchName.trim() || locked}
                    onClick={() => sendDecision("accept")}
                  >
                    接纳并创建人工批次
                  </Button>
                  <Button
                    size="sm"
                    disabled={!canDecide || !selected.length || locked}
                    onClick={() => sendDecision("reject")}
                  >
                    拒绝所选候选
                  </Button>
                </div>
                {pendingDecision && (
                  <p role="status" className="text-sm text-status-caution">
                    决策请求已保存，结果待确认。
                    <Button
                      size="sm"
                      disabled={decide.isPending}
                      onClick={() => decide.mutate(pendingDecision)}
                    >
                      重试同一决策
                    </Button>
                  </p>
                )}
              </section>
            </>
          )}
          <section className="mt-5" aria-label="历史人工决策">
            <h2 className="text-sm font-semibold">人工决策记录</h2>
            {!decisions.error && decisions.data?.items.length === 0 && (
              <p className="text-xs text-muted-foreground">尚未接纳或拒绝候选。</p>
            )}
            <ul className="space-y-3 pl-4 text-sm">
              {!decisions.error &&
                decisions.data?.items.map((decision) => (
                  <li key={decision.id}>
                    {decision.action === "accept" ? "接纳" : "拒绝"}{" "}
                    {decision.snapshot.tasks.length} 张
                    {decision.snapshot.batch && (
                      <span>
                        {" "}
                        ·{" "}
                        {decision.accepted_batch_id ? (
                          <Link
                            className="text-primary hover:underline"
                            to={`/projects/${projectId}/settings?section=batches`}
                          >
                            {decision.snapshot.batch.display_id} {decision.snapshot.batch.name}
                          </Link>
                        ) : (
                          `${decision.snapshot.batch.display_id}（批次已删除，历史仍保留）`
                        )}
                      </span>
                    )}
                  </li>
                ))}
            </ul>
            <ListPagination
              label="人工决策"
              page={decisionPage}
              pageSize={decisionPageSize}
              total={decisions.data?.total}
              count={decisions.data?.items.length}
              hasNext={
                decisions.data?.total == null
                  ? decisions.data?.items.length === decisionPageSize
                  : undefined
              }
              loading={decisions.isFetching}
              onPageChange={setDecisionPage}
              onPageSizeChange={(size) => {
                setDecisionPageSize(size);
                setDecisionPage(0);
              }}
            />
            <Link
              className="text-xs text-primary hover:underline"
              to={`/projects/${projectId}/settings?section=batches`}
            >
              前往批次分派与正常标注
            </Link>
          </section>
          {round.accepted_tasks > 0 && (
            <section
              className="mt-5 rounded-lg border border-border bg-card p-4"
              aria-label="人工反馈"
            >
              <h2 className="text-sm font-semibold">实际人工反馈</h2>
              <p className="text-xs text-muted-foreground">
                正常提交、审核后追加观察快照。纠正次数和完整工时保持未知；已记录的会话时间只反映部分作业。
              </p>
              <details className="mb-3 text-xs">
                <summary className="cursor-pointer">关联两份后续逐样本评估（可选）</summary>
                <Link
                  className="mt-2 block text-primary hover:underline"
                  to={`/projects/${projectId}/learning/evaluations`}
                >
                  查看逐样本评估及执行尝试
                </Link>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  {evaluationIds.map((value, index) => (
                    <div key={index} className="space-y-2">
                      <label className="block">
                        评估 {index + 1} UUID
                        <input
                          className={`${trainingInputClass} mt-1 w-full`}
                          value={value}
                          disabled={!!pendingFeedback}
                          onChange={(event) =>
                            setEvaluationIds((old) =>
                              old.map((item, position) =>
                                position === index ? event.target.value : item,
                              ),
                            )
                          }
                        />
                      </label>
                      <label className="block">
                        执行尝试 {index + 1} UUID
                        <input
                          className={`${trainingInputClass} mt-1 w-full`}
                          value={evaluationAttempts[index]}
                          disabled={!!pendingFeedback}
                          onChange={(event) =>
                            setEvaluationAttempts((old) =>
                              old.map((item, position) =>
                                position === index ? event.target.value : item,
                              ),
                            )
                          }
                        />
                      </label>
                    </div>
                  ))}
                </div>
                {!validEvaluations && (
                  <p role="alert" className="text-status-danger">
                    每份引用须同时填写有效的评估和执行尝试 UUID。
                  </p>
                )}
              </details>
              <Button
                size="sm"
                disabled={record.isPending || !validEvaluations}
                onClick={() => (pendingFeedback ? record.mutate(pendingFeedback) : sendFeedback())}
              >
                {pendingFeedback ? "重试同一反馈快照" : "记录当前人工反馈快照"}
              </Button>
              {!feedback.error && feedback.data?.items.length === 0 && (
                <p className="text-xs text-muted-foreground">尚未记录人工反馈，不能推断收益。</p>
              )}
              {!feedback.error &&
                feedback.data?.items.map((item) => (
                  <div key={item.id} className="mt-4 border-t border-border pt-4 text-xs">
                    <p className="text-muted-foreground">
                      观察于 {new Date(item.snapshot.observed_at).toLocaleString()}
                    </p>
                    <div className="overflow-x-auto">
                      <table className={listStyles.table}>
                        <thead>
                          <tr>
                            <th className="py-2 pr-3">组别</th>
                            <th className="px-2">接纳／计划</th>
                            <th className="px-2">已审核</th>
                            <th className="px-2">驳回记录</th>
                            <th className="px-2">纠正次数</th>
                            <th className="px-2">记录标注／审核时间</th>
                          </tr>
                        </thead>
                        <tbody>
                          {item.snapshot.arms.map((arm) => (
                            <tr key={arm.arm} className="border-t border-border">
                              <td className="py-3 pr-3 whitespace-nowrap">{armLabel[arm.arm]}</td>
                              <td className="px-2 tabular-nums">
                                {arm.accepted_tasks}/{arm.planned_tasks}
                              </td>
                              <td className="px-2 tabular-nums">{arm.reviewed_tasks}</td>
                              <td className="px-2 tabular-nums">{arm.reviewer_rejection_count}</td>
                              <td className="px-2">{arm.correction_count ?? "未知"}</td>
                              <td className="px-2">
                                {recordedMinutes(arm.effort.annotate.recorded_ms)} /{" "}
                                {recordedMinutes(arm.effort.review.recorded_ms)}（部分记录）
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="text-muted-foreground">
                      {item.snapshot.evaluations.reasons.join("；") ||
                        "两份评估满足比较条件，变化不代表主动选样的因果收益"}
                    </p>
                    {item.snapshot.evaluations.delta_second_minus_first && (
                      <p className="tabular-nums">
                        F1 变化{" "}
                        {item.snapshot.evaluations.delta_second_minus_first.f1?.toFixed(3) ??
                          "未知"}
                      </p>
                    )}
                    <p className="text-muted-foreground">
                      未建立效率收益 · {item.snapshot.limitations.join("；")}
                    </p>
                  </div>
                ))}
              <ListPagination
                label="人工反馈"
                page={feedbackPage}
                pageSize={feedbackPageSize}
                total={feedback.data?.total}
                count={feedback.data?.items.length}
                hasNext={
                  feedback.data?.total == null
                    ? feedback.data?.items.length === feedbackPageSize
                    : undefined
                }
                loading={feedback.isFetching}
                onPageChange={setFeedbackPage}
                onPageSizeChange={(size) => {
                  setFeedbackPageSize(size);
                  setFeedbackPage(0);
                }}
              />
            </section>
          )}
        </>
      )}
      {[
        detail.error,
        report.error,
        rows.error,
        decisions.error,
        feedback.error,
        action.error,
        decide.error,
        record.error,
      ]
        .filter(Boolean)
        .map((error, index) => (
          <p key={index} role="alert" className="text-sm text-status-danger">
            <CopyableError message={error?.message} />
          </p>
        ))}
      {storageError && (
        <p role="alert" className="text-sm text-status-danger">
          {storageError}
        </p>
      )}
    </PageContainer>
  );
}
function recordedMinutes(milliseconds: number | null) {
  return milliseconds === null ? "未知" : `${(milliseconds / 60_000).toFixed(1)} 分钟`;
}
function CandidateImage({
  round,
  ordinal,
  scope,
  assert,
  sample,
  maskPredictions,
}: {
  round: ActiveRound;
  ordinal: number;
  scope: string;
  assert: AssertTrainingOwner;
  sample: FrozenSample;
  maskPredictions?: MaskPrediction[];
}) {
  const image = useQuery({
    queryKey: ["active-learning-image", round.project_id, scope, round.id, ordinal],
    retry: false,
    queryFn: async ({ signal }) => {
      assert(signal);
      const value = await activeLearningApi.image(round.project_id, round.id, ordinal, { signal });
      assert(signal);
      return value;
    },
  });
  const [url, setUrl] = useState<{ blob: Blob; value: string } | null>(null);
  useEffect(() => {
    if (!image.data || image.error) return;
    const value = URL.createObjectURL(image.data);
    setUrl({ blob: image.data, value });
    return () => URL.revokeObjectURL(value);
  }, [image.data, image.error]);
  return (
    <div className="min-w-0">
      {image.isPending && (
        <p role="status" className="text-xs">
          读取冻结图片…
        </p>
      )}
      {image.error && (
        <p role="alert" className="text-xs text-status-danger">
          <CopyableError message={image.error.message} />
        </p>
      )}
      {!image.error &&
        url &&
        url.blob === image.data &&
        (maskPredictions ? (
          <ActiveLearningMaskPreview
            scopeKey={`${scope}:${round.id}:${round.spec_digest}:${ordinal}`}
            sampleUrl={url.value}
            width={sample.width}
            height={sample.height}
            predictions={maskPredictions}
            assertOwner={assert}
          />
        ) : (
          <img
            className="max-h-80 w-full rounded-md bg-muted object-contain"
            src={url?.value}
            alt={`冻结候选 ${ordinal + 1}`}
          />
        ))}
    </div>
  );
}
