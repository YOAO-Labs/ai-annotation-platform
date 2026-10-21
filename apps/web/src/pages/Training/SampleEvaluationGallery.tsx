import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useRef, useState, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { ExternalLink } from "lucide-react";
import {
  sampleEvaluationsApi,
  type EvaluationReport,
  type EvaluationSampleResult,
  type SampleEvaluationOut,
} from "@/api/sampleEvaluations";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Modal } from "@/components/ui/Modal";
import { trainingInputClass } from "./TrainingParameterField";
import {
  errorLabels,
  metricValue,
  parseEvaluationState,
  sourceWorkbenchUrl,
} from "./sampleEvaluation";
import { useTrainingViewOwner } from "./trainingViewOwner";
import { SampleEvaluationImage } from "./SampleEvaluationImage";
import { PickerPage } from "./SampleEvaluationCreate";
import { evaluationTask } from "./useSampleEvaluation";

export function SampleEvaluationGallery({
  run,
  report,
  state,
  scope,
  update,
}: {
  run: SampleEvaluationOut;
  report: EvaluationReport;
  state: ReturnType<typeof parseEvaluationState>;
  scope: string;
  update: (values: Record<string, string | undefined>, replace?: boolean) => void;
}) {
  const { assert } = useTrainingViewOwner(run.project_id, scope);
  const [sourceOpen, setSourceOpen] = useState(false);
  const sourceTrigger = useRef<HTMLButtonElement>(null);
  const samples = useQuery({
    queryKey: [
      "evaluation-gallery",
      run.project_id,
      scope,
      run.id,
      report.attempt_id,
      state.filters,
    ],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.samples(
        run.project_id,
        run.id,
        report.attempt_id,
        state.filters,
        { signal },
      );
      assert(signal);
      if (
        result.evaluation_id !== run.id ||
        result.attempt_id !== report.attempt_id ||
        result.spec_digest !== run.spec_digest
      )
        throw new Error("样本页身份不符");
      return result;
    },
    retry: false,
  });
  const selected =
    state.ordinal === undefined
      ? samples.data?.items[0]
      : report.samples.find((sample) => sample.ordinal === state.ordinal);
  useEffect(() => {
    const first = samples.error ? undefined : samples.data?.items[0];
    if (state.ordinal === undefined && first) update({ sample: String(first.ordinal) }, true);
  }, [state.ordinal, samples.data, samples.error, update]);
  const filter = (key: string, value: string) =>
    update({ [key]: value || undefined, page: undefined, sample: undefined });
  return (
    <section
      className="mt-5 rounded-lg border border-border bg-card"
      aria-labelledby="evaluation-gallery-title"
    >
      <div className="border-b border-border p-4">
        <h2 id="evaluation-gallery-title" className="text-sm font-semibold">
          逐样本误差
        </h2>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <label className="space-y-1 text-xs">
            类别
            <select
              className={trainingInputClass}
              value={state.filters.class_index ?? ""}
              onChange={(e) => filter("class", e.target.value)}
            >
              <option value="">全部类别</option>
              {run.spec.classes.map((name, index) => (
                <option key={index} value={index}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-xs">
            误差类型
            <select
              className={trainingInputClass}
              value={state.filters.error ?? ""}
              onChange={(e) => filter("error", e.target.value)}
            >
              <option value="">全部样本</option>
              {Object.entries(errorLabels).map(([key, name]) => (
                <option key={key} value={key}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <label className="space-y-1 text-xs">
            证据置信度下限
            <input
              className={trainingInputClass}
              type="number"
              min="0"
              max="1"
              step="0.01"
              placeholder="不限"
              value={state.filters.confidence_min ?? ""}
              onChange={(e) => filter("confidence_min", e.target.value)}
            />
          </label>
          <label className="space-y-1 text-xs">
            证据置信度上限
            <input
              className={trainingInputClass}
              type="number"
              min="0"
              max="1"
              step="0.01"
              placeholder="不限"
              value={state.filters.confidence_max ?? ""}
              onChange={(e) => filter("confidence_max", e.target.value)}
            />
          </label>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          筛选发生在分页前，只查找已匹配的证据。FN
          没有置信度，漏检筛选忽略置信度范围。更改匹配口径需新建评估。
        </p>
      </div>
      <div className="grid min-w-0 lg:grid-cols-[16rem_minmax(0,1fr)]">
        <div className="min-w-0 border-b border-border p-3 lg:border-b-0 lg:border-r">
          {samples.isPending && (
            <p role="status" className="p-2 text-xs text-muted-foreground">
              正在校验样本列表…
            </p>
          )}
          {samples.error && (
            <div role="alert" className="p-2 text-xs text-status-danger">
              <p>
                <CopyableError message={samples.error.message} />
              </p>
              <Button size="sm" className="mt-2" onClick={() => void samples.refetch()}>
                重试列表
              </Button>
            </div>
          )}
          {samples.data && !samples.error && (
            <>
              <p className="mb-2 px-2 text-xs text-muted-foreground">
                筛选后 {samples.data.total} 个成员
              </p>
              <div className="grid gap-1 sm:grid-cols-2 lg:grid-cols-1">
                {samples.data.items.map((sample) => (
                  <button
                    key={sample.ordinal}
                    type="button"
                    aria-pressed={selected?.ordinal === sample.ordinal}
                    className={`min-w-0 rounded-md border p-3 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected?.ordinal === sample.ordinal ? "border-primary bg-accent text-accent-foreground" : "border-transparent hover:bg-muted"}`}
                    onClick={() => update({ sample: String(sample.ordinal) })}
                  >
                    <span className="font-medium">样本 {sample.ordinal + 1}</span>
                    <span className="ml-2 text-muted-foreground">
                      {sample.status === "succeeded"
                        ? `${sample.matching?.fp ?? 0} FP · ${sample.matching?.fn ?? 0} FN`
                        : sample.status === "failed"
                          ? "失败"
                          : "未完成"}
                    </span>
                    <span className="mt-1 block tabular-nums text-muted-foreground">
                      {sample.width} × {sample.height}
                    </span>
                  </button>
                ))}
              </div>
              {samples.data.total === 0 && (
                <p className="p-2 text-xs text-muted-foreground">没有符合筛选条件的样本。</p>
              )}
              <PickerPage
                page={state.page}
                pageSize={state.filters.limit}
                loading={samples.isFetching}
                onPageSizeChange={(size) =>
                  update({ page_size: String(size), page: undefined, sample: undefined })
                }
                total={samples.data.total}
                label="误差样本"
                onChange={(page) => update({ page: String(page), sample: undefined })}
              />
            </>
          )}
        </div>
        <div className="min-w-0 p-4">
          {state.ordinal !== undefined && !selected && (
            <p role="alert" className="text-sm text-status-danger">
              链接指定的冻结成员不存在，未替换为其他成员。
            </p>
          )}
          {selected && (
            <>
              <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
                <div className="flex min-w-0 items-center gap-1">
                  <h3 className="text-sm font-semibold">样本 {selected.ordinal + 1}</h3>
                  <CopyValueButton value={selected.task_id} kind="id" label="复制样本任务 ID" />
                </div>
                <Button ref={sourceTrigger} size="sm" onClick={() => setSourceOpen(true)}>
                  <ExternalLink className="size-3.5" />
                  核对当前标注任务
                </Button>
              </div>
              <SampleEvaluationImage
                key={`${scope}:${selected.ordinal}`}
                projectId={run.project_id}
                evaluationId={run.id}
                attemptId={report.attempt_id}
                sample={selected}
                classes={run.spec.classes}
                scope={`${scope}:image:${selected.ordinal}`}
                task={evaluationTask(run.spec)}
              />
              {selected.status !== "succeeded" ? (
                <p role="status" className="mt-4 text-sm text-status-caution">
                  {selected.status === "failed" ? (
                    <CopyableError
                      message={`样本失败：${selected.error_message ?? selected.error_code}`}
                    />
                  ) : (
                    "本次尝试未完成此成员，尚无预测证据。"
                  )}
                </p>
              ) : (
                <SampleEvidence
                  sample={selected}
                  classes={run.spec.classes}
                  task={evaluationTask(run.spec)}
                />
              )}
              <details className="mt-4 text-xs text-muted-foreground">
                <summary className="cursor-pointer">冻结成员身份与执行来源</summary>
                <dl className="mt-2 grid grid-cols-[max-content_minmax(0,1fr)] items-center gap-x-2 gap-y-2">
                  <dt>原图 SHA256</dt>
                  <dd>
                    <CopyValueButton value={selected.sha256} label="复制原图哈希" />
                  </dd>
                  <dt>标签 SHA256</dt>
                  <dd>
                    <CopyValueButton value={selected.label_sha256} label="复制标签哈希" />
                  </dd>
                  <dt>归一化图片 SHA256</dt>
                  <dd>
                    {selected.derived_sha256 ? (
                      <CopyValueButton value={selected.derived_sha256} label="复制归一化图片哈希" />
                    ) : (
                      "尚未生成"
                    )}
                  </dd>
                  <dt>证据产生于尝试</dt>
                  <dd>
                    {selected.produced_attempt_id ? (
                      <CopyValueButton
                        value={selected.produced_attempt_id}
                        kind="id"
                        label="复制证据执行尝试 ID"
                      />
                    ) : (
                      "尚无证据"
                    )}
                  </dd>
                </dl>
              </details>
              {sourceOpen && (
                <SourceDialog
                  key={`${scope}:source:${selected.ordinal}`}
                  run={run}
                  attemptId={report.attempt_id}
                  sample={selected}
                  scope={`${scope}:source:${selected.ordinal}`}
                  onClose={() => setSourceOpen(false)}
                  returnFocusRef={sourceTrigger}
                />
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

function SampleEvidence({
  sample,
  classes,
  task,
}: {
  sample: EvaluationSampleResult;
  classes: string[];
  task: "detect" | "segment";
}) {
  const matching = sample.matching!;
  return (
    <div className="mt-4 text-xs">
      <p className="tabular-nums">
        TP {matching.tp} · FP {matching.fp} · FN {matching.fn} · F1 {metricValue(matching.f1, true)}
      </p>
      <div className="mt-3 grid gap-4 xl:grid-cols-2">
        <div>
          <h4 className="font-medium">
            {task === "segment" ? "冻结真值掩码（蓝色）" : "冻结真值（实线）"}
          </h4>
          <ul className="mt-2 max-h-64 space-y-2 overflow-auto">
            {sample.truth.map((box) => (
              <li key={box.index}>
                <span className="font-medium">
                  #{box.index} {classes[box.class_index]}
                </span>{" "}
                · {matching.false_negative_indices.includes(box.index) ? "漏检 FN" : "已匹配 TP"}
                <code className="mt-0.5 block text-muted-foreground">
                  {"rle" in box
                    ? `${box.area_pixels.toLocaleString()} 原图像素`
                    : box.xyxy.map((v) => v.toFixed(4)).join(", ")}
                </code>
              </li>
            ))}
          </ul>
          {sample.truth.length === 0 && (
            <p className="mt-2 text-muted-foreground">冻结真值为空，预测会计为 FP。</p>
          )}
        </div>
        <div>
          <h4 className="font-medium">
            {task === "segment" ? "预测掩码（橙色）" : "预测（虚线）"}
          </h4>
          <ul className="mt-2 max-h-64 space-y-2 overflow-auto">
            {(sample.predictions ?? []).map((box) => {
              const match = matching.matches.find((item) => item.prediction_index === box.index);
              const diagnostic = matching.diagnostics.find(
                (item) => item.prediction_index === box.index,
              );
              return (
                <li key={box.index}>
                  <span className="font-medium">
                    #{box.index} {classes[box.class_index]}
                  </span>{" "}
                  · {metricValue(box.score, true)} ·{" "}
                  {match
                    ? `TP → 真值 #${match.truth_index}，${task === "segment" ? "Mask IoU" : "IoU"} ${match.iou.toFixed(3)}${"dice" in match ? `，Dice ${Number(match.dice).toFixed(3)}` : ""}`
                    : matching.excluded_prediction_indices.includes(box.index)
                      ? "低于固定阈值，不计入匹配"
                      : "误检 FP"}
                  {diagnostic && (
                    <span className="block text-status-caution">
                      {diagnostic.kind === "confusion" ? "类别混淆" : "定位偏差"} → 真值 #
                      {diagnostic.truth_index}，{task === "segment" ? "Mask IoU" : "IoU"}{" "}
                      {diagnostic.iou.toFixed(3)}
                    </span>
                  )}
                  <code className="mt-0.5 block text-muted-foreground">
                    {"rle" in box
                      ? `${box.area_pixels.toLocaleString()} 原图像素`
                      : box.xyxy.map((v) => v.toFixed(4)).join(", ")}
                  </code>
                </li>
              );
            })}
          </ul>
          {!sample.predictions?.length && (
            <p className="mt-2 text-muted-foreground">
              {task === "segment" ? "没有预测实例掩码。" : "没有预测框。"}
            </p>
          )}
        </div>
      </div>
      <p className="mt-3 text-muted-foreground">
        {task === "segment"
          ? "Mask IoU、Dice 直接比较原图掩码像素，仅已匹配实例计入均值；漏检另计 FN，不是 AP/mAP。"
          : "坐标为逻辑图片上的归一化 x1, y1, x2, y2。"}
        混淆与定位诊断解释已有 FP/FN，不重复增加主计数。
      </p>
    </div>
  );
}

function SourceDialog({
  run,
  attemptId,
  sample,
  scope,
  onClose,
  returnFocusRef,
}: {
  run: SampleEvaluationOut;
  attemptId: string;
  sample: EvaluationSampleResult;
  scope: string;
  onClose: () => void;
  returnFocusRef: RefObject<HTMLButtonElement | null>;
}) {
  const [requestScope] = useState(() => randomId());
  const { assert } = useTrainingViewOwner(run.project_id, `${scope}:${requestScope}`);
  const location = useLocation();
  const navigate = useNavigate();
  const source = useQuery({
    queryKey: [
      "evaluation-current-source",
      run.project_id,
      scope,
      requestScope,
      run.id,
      attemptId,
      sample.ordinal,
    ],
    queryFn: async ({ signal }) => {
      assert(signal);
      const result = await sampleEvaluationsApi.source(
        run.project_id,
        run.id,
        attemptId,
        sample.ordinal,
        { signal },
      );
      assert(signal);
      if (
        result.evaluation_id !== run.id ||
        result.attempt_id !== attemptId ||
        result.ordinal !== sample.ordinal ||
        (result.source === "found" &&
          (result.task_id !== sample.task_id || result.project_id !== run.project_id))
      )
        throw new Error("当前源任务返回身份不符，无法打开");
      return result;
    },
    retry: false,
    staleTime: 0,
  });
  const data = source.error ? undefined : source.data;
  const url = data
    ? sourceWorkbenchUrl(data, run.project_id, sample.task_id, location.pathname + location.search)
    : null;
  const label = {
    same: "相同",
    changed: "已变化",
    unavailable: "无法核对",
    unsupported: "当前标签类型不受支持",
  };
  return (
    <Modal
      open
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      title="当前标注任务与冻结证据"
      width={560}
    >
      {source.isPending && (
        <p role="status" className="text-sm text-muted-foreground">
          正在核对当前图片内容、标签与访问权限…
        </p>
      )}
      {source.error && (
        <div role="alert" className="space-y-3 text-sm text-status-danger">
          <p>
            <CopyableError message={source.error.message} />
          </p>
          <Button size="sm" onClick={() => void source.refetch()}>
            重新核对
          </Button>
        </div>
      )}
      {data && (
        <div className="space-y-4 text-sm">
          {data.source !== "found" ? (
            <p role="status">
              {data.source === "deleted" ? "源任务已删除或移出项目。" : "当前源任务不可用。"}
              {data.reason} 冻结报告仍然保留。
            </p>
          ) : (
            <>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
                <dt className="text-muted-foreground">当前图片</dt>
                <dd>{label[data.media]}</dd>
                <dt className="text-muted-foreground">当前标签</dt>
                <dd>{label[data.labels]}</dd>
                <dt className="text-muted-foreground">任务状态</dt>
                <dd>{data.task_status ?? "未知"}</dd>
                <dt className="text-muted-foreground">当前复核</dt>
                <dd>{data.reviewed ? "已复核" : "未复核"}</dd>
                <dt className="text-muted-foreground">当前负样本确认</dt>
                <dd>{data.negative_confirmed ? "已确认" : "未确认"}</dd>
              </dl>
              {(data.media !== "same" || data.labels !== "same") && (
                <p
                  role="status"
                  className="rounded-md bg-status-caution-soft p-3 text-status-caution"
                >
                  当前任务与冻结证据不同或无法完全核对。工作台将打开当前内容；修改不会改变这份历史真值或报告。
                  {data.reason}
                </p>
              )}
              {url && (
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    assert();
                    navigate(url);
                  }}
                >
                  <ExternalLink className="size-3.5" />
                  打开当前任务
                </Button>
              )}
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
