import { CopyableError } from "@/components/ui/CopyableError";
import { useRef, useState } from "react";
import type { TrainingRunCreate } from "@/api/trainingRuns";
import {
  trainingStudiesApi,
  type TrainingStudyCreate,
  type StudyPlan,
} from "@/api/trainingStudies";
import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { trainingInputClass } from "./TrainingParameterField";
import { useTrainingViewOwner } from "./trainingViewOwner";

type Preview = {
  plan: StudyPlan;
  preview_digest: string;
  input: string;
  body: TrainingStudyCreate;
};

export function TrainingStudyCreate({
  projectId,
  schema,
  base,
  pending,
  onBusy,
  beforeStart,
  persist,
  onCreated,
  onDiscard,
}: {
  projectId: string;
  schema?: TrainingParameterSchema;
  base?: TrainingRunCreate;
  pending?: TrainingStudyCreate;
  onBusy: (busy: boolean) => void;
  beforeStart: () => Promise<boolean>;
  persist: (body: TrainingStudyCreate) => boolean;
  onCreated: (id: string) => void;
  onDiscard: () => void;
}) {
  const session = useTrainingViewOwner(
    projectId,
    base?.request_id ?? pending?.base.request_id ?? "study",
  );
  const [gridText, setGridText] = useState(() =>
    JSON.stringify(pending?.grid ?? { lr0: [0.01, 0.005] }, null, 2),
  );
  const [trials, setTrials] = useState(() => (pending ? String(pending.budgets.max_trials) : ""));
  const [epochs, setEpochs] = useState(() =>
    pending ? String(pending.budgets.max_planned_epochs) : "",
  );
  const [seconds, setSeconds] = useState(() =>
    pending ? String(pending.budgets.max_native_seconds) : "",
  );
  const [objective, setObjective] = useState<TrainingStudyCreate["objective"]>(
    pending?.objective ?? "metrics/mAP50-95",
  );
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const input = JSON.stringify([base, gridText, trials, epochs, seconds, objective]);
  const current = useRef(input);
  current.current = input;
  const reviewed = preview?.input === input ? preview : null;
  const metrics =
    (base?.framework ?? pending?.base.framework ?? "ultralytics") === "libreyolo"
      ? ["metrics/mAP50-95", "metrics/mAP50", "metrics/mAP75", "metrics/AR100"]
      : ["metrics/mAP50-95", "metrics/mAP50", "metrics/precision", "metrics/recall"];
  const editable = schema?.fields.filter((field) => field.status === "editable") ?? [];
  const supported = !schema || schema.task === "detect";
  const build = (): TrainingStudyCreate => {
    if (!supported) throw new Error("参数搜索目前仅通过图片检测资格验证");
    if (!base) throw new Error("请先检查训练配置，再预览参数搜索");
    if (gridText.length > 16 * 1024) throw new Error("网格内容超出大小限制");
    const grid: unknown = JSON.parse(gridText);
    if (
      !grid ||
      typeof grid !== "object" ||
      Array.isArray(grid) ||
      Object.entries(grid).some(
        ([key, values]) =>
          !editable.some((field) => field.key === key) || !Array.isArray(values) || !values.length,
      )
    )
      throw new Error("网格需要可编辑原生参数名及其 JSON 值列表");
    const budgets = {
      max_trials: Number(trials),
      max_planned_epochs: Number(epochs),
      max_native_seconds: Number(seconds),
    };
    if (
      !Number.isInteger(budgets.max_trials) ||
      budgets.max_trials < 1 ||
      budgets.max_trials > 20 ||
      !Number.isInteger(budgets.max_planned_epochs) ||
      budgets.max_planned_epochs < 1 ||
      !Number.isFinite(budgets.max_native_seconds) ||
      budgets.max_native_seconds <= 0
    )
      throw new Error("请明确填写试验数量、总轮数和原生计算秒数预算");
    return {
      base,
      grid: grid as TrainingStudyCreate["grid"],
      budgets,
      objective,
      direction: "maximize",
    };
  };
  const act = async (start: boolean) => {
    if (lock.current || !session.owns()) return;
    const captured = input;
    lock.current = true;
    setBusy(true);
    setError("");
    onBusy(true);
    try {
      if (!(await beforeStart())) return;
      session.assert();
      if (current.current !== captured && !pending) return;
      if (start) {
        const body =
          pending ??
          (reviewed ? { ...reviewed.body, preview_digest: reviewed.preview_digest } : null);
        if (!body) throw new Error("请重新预览全部组合与预算");
        if (!persist(body)) return;
        const study = await trainingStudiesApi.create(projectId, body);
        session.assert();
        if (study.project_id !== projectId) throw new Error("搜索归属已变化");
        onCreated(study.id);
      } else {
        const body = build();
        const result = await trainingStudiesApi.preview(projectId, body);
        session.assert();
        if (current.current === captured) setPreview({ ...result, input: captured, body });
      }
    } catch (reason) {
      if (session.owns() && (current.current === captured || pending))
        setError(reason instanceof Error ? reason.message : "搜索操作失败，请重试");
    } finally {
      lock.current = false;
      if (session.owns()) {
        setBusy(false);
        onBusy(false);
      }
    }
  };
  return (
    <section
      aria-label="有预算的参数搜索"
      className="mt-5 min-w-0 rounded-xl border border-border bg-card p-5"
    >
      <h3 className="text-sm font-semibold">有预算的参数搜索</h3>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        最多 20
        个组合，逐个加入现有训练队列。使用同一冻结数据与验证集，原生计算时间覆盖每次执行的准备、训练、评估和停止宽限。排队时间另计。
      </p>
      {!base && !pending && (
        <p className="mt-3 text-xs text-status-caution">先检查训练配置，再核对全部组合与预算。</p>
      )}
      {pending && (
        <p
          role="status"
          className="mt-3 rounded-lg bg-status-caution-soft p-3 text-xs text-status-caution"
        >
          搜索创建结果尚未确认。重试会提交已保存的同一计划；配置暂时锁定。
        </p>
      )}
      {!supported && (
        <p role="alert" className="mt-3 text-xs text-status-caution">
          参数搜索目前仅支持图片检测；分割请使用单个训练实验。
        </p>
      )}
      <fieldset
        disabled={!supported || busy || !!pending}
        className="mt-4 grid min-w-0 gap-4 sm:grid-cols-3"
      >
        <label className="min-w-0 text-xs sm:col-span-3">
          参数网格（JSON）
          <textarea
            aria-label="参数网格"
            className={`${trainingInputClass} mt-1.5 min-h-28 font-mono`}
            value={gridText}
            onChange={(event) => setGridText(event.target.value)}
          />
        </label>
        <label className="text-xs">
          最多试验数
          <input
            aria-label="最多试验数"
            type="number"
            min={1}
            max={20}
            className={`${trainingInputClass} mt-1.5`}
            value={trials}
            onChange={(event) => setTrials(event.target.value)}
          />
        </label>
        <label className="text-xs">
          计划总轮数上限
          <input
            aria-label="计划总轮数上限"
            type="number"
            min={1}
            max={20000}
            className={`${trainingInputClass} mt-1.5`}
            value={epochs}
            onChange={(event) => setEpochs(event.target.value)}
          />
        </label>
        <label className="text-xs">
          原生计算秒数上限
          <input
            aria-label="原生计算秒数上限"
            type="number"
            min={0.1}
            max={604800}
            step="any"
            className={`${trainingInputClass} mt-1.5`}
            value={seconds}
            onChange={(event) => setSeconds(event.target.value)}
          />
        </label>
        <label className="text-xs sm:col-span-3">
          最大化最终验证指标
          <select
            aria-label="搜索目标"
            className={`${trainingInputClass} mt-1.5`}
            value={objective}
            onChange={(event) =>
              setObjective(event.target.value as TrainingStudyCreate["objective"])
            }
          >
            {metrics.map((metric) => (
              <option key={metric} value={metric}>
                {metric.replace("metrics/", "")}
              </option>
            ))}
          </select>
        </label>
      </fieldset>
      <details className="mt-3 text-xs text-muted-foreground">
        <summary className="cursor-pointer">可搜索的原生参数</summary>
        <p className="mt-2 break-words leading-relaxed">
          {editable.map((field) => field.key).join(" · ")}
        </p>
      </details>
      {error && (
        <p role="alert" className="mt-3 text-xs text-status-danger">
          <CopyableError message={error} />
        </p>
      )}
      {reviewed && (
        <div className="mt-4 text-xs" aria-label="搜索计划预览">
          <p className="font-medium">
            共 {reviewed.plan.trials.length} 个试验 · 计划 {reviewed.plan.planned_epochs} 轮 · 最多{" "}
            {reviewed.plan.budgets.max_native_seconds} 秒原生计算
          </p>
          <p className="mt-2 leading-relaxed text-muted-foreground">
            每次试验预留当前剩余额度，结束后按已确认时间结算。时间未知时暂停后续试验；同一验证身份的最终报告才会排名。
          </p>
          <ol className="mt-3 space-y-2">
            {reviewed.plan.trials.map((trial) => (
              <li key={trial.id} className="min-w-0 rounded-lg border border-border p-3">
                <p className="break-words">
                  {trial.ordinal}. {JSON.stringify(trial.values)}
                </p>
                <p className="mt-1 flex items-center gap-1 text-2xs text-muted-foreground">
                  配置摘要 <CopyValueButton value={trial.config_digest} label="复制试验配置摘要" />
                </p>
              </li>
            ))}
          </ol>
          <p className="mt-2 flex items-center gap-1 text-2xs text-muted-foreground">
            计划摘要 <CopyValueButton value={reviewed.preview_digest} label="复制搜索计划摘要" />
          </p>
        </div>
      )}
      <div className="mt-4 flex flex-wrap gap-2">
        {!pending && (
          <Button size="sm" disabled={!supported || busy || !base} onClick={() => void act(false)}>
            {busy ? "处理中…" : "预览全部组合与预算"}
          </Button>
        )}
        <Button
          size="sm"
          variant="primary"
          disabled={!supported || busy || (!pending && !reviewed)}
          onClick={() => void act(true)}
        >
          {busy ? "处理中…" : pending ? "重试并确认搜索创建" : "开始参数搜索"}
        </Button>
        {pending && (
          <Button size="sm" variant="ghost" disabled={busy} onClick={onDiscard}>
            放弃此请求并编辑
          </Button>
        )}
      </div>
    </section>
  );
}
