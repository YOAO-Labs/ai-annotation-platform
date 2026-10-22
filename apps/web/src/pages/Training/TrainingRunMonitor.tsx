import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Check,
  Circle,
  Clock3,
  Download,
  FileText,
  Layers,
  Gauge,
  Terminal,
} from "lucide-react";

import type { TrainingAttemptOut, TrainingEpochEvent, TrainingRunOut } from "@/api/trainingRuns";
import { cn } from "@/lib/utils";
import { TrainingLoading } from "./TrainingFeedback";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Button as NavigationButton } from "@/components/shadcn/ui/button";
import type { ModelReturnEligibility, TrainingRegisteredModel } from "@/api/generated/types.gen";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { TrainingMetricChart } from "./TrainingMetricChart";
import { TrainingRecipeSave } from "./TrainingRecipeLibrary";
import type { TrainingRecovery } from "@/api/trainingObservability";
import { useTrainingObservability } from "./useTrainingObservability";
import { TrainingResourcePanel } from "./TrainingResourcePanel";
import { TrainingLogViewer } from "./TrainingLogViewer";
import { failureGuidance, recoveryReasons } from "./trainingObservability";
import type { AssertTrainingOwner } from "./trainingViewOwner";
import {
  activeTrainingStates,
  artifactLabels,
  epochPoints,
  formatDuration,
  metricLabel,
  trainingProgress,
  trainingStatusLabels,
  trainingStatusVariant,
  trainingTaskOf,
  trainingTaskLabel,
  segmentationTask,
} from "./trainingPresentation";

type Action = {
  kind: "cancel" | "resume" | "delete" | "download";
  run: TrainingRunOut;
  attemptId?: string;
  filename?: string;
};
export function TrainingRunMonitor({
  run,
  attempts,
  events,
  selectedAttemptId,
  onAttemptChange,
  eventsPending,
  eventsError,
  pending,
  onAction,
  recovery,
  recoveryError,
  registrationEligibility,
  registeredModels = [],
  scope,
  visible,
  assertOwner,
}: {
  run: TrainingRunOut;
  attempts: TrainingAttemptOut[];
  events: TrainingEpochEvent[];
  selectedAttemptId: string;
  onAttemptChange: (id: string) => void;
  eventsPending: boolean;
  eventsError?: string;
  pending: boolean;
  onAction: (action: Action) => void;
  recovery?: TrainingRecovery;
  recoveryError?: string;
  registrationEligibility?: ModelReturnEligibility | null;
  registeredModels?: TrainingRegisteredModel[];
  scope: string;
  visible: boolean;
  assertOwner: AssertTrainingOwner;
}) {
  const [tab, setTab] = useState("metrics");
  const [now, setNow] = useState(Date.now);
  const progress = trainingProgress(run);
  const framework = String(run.config.framework ?? "");
  const task = run.task ?? trainingTaskOf(run.config);
  const segmentation = task === segmentationTask;
  const configuration = run.config.configuration as
    | { effective_values?: { evaluate_train?: boolean } }
    | undefined;
  const points = epochPoints(events, framework);
  const epochPage = useListPagination([...points].reverse(), `${run.id}:${selectedAttemptId}`);
  const keys = [
    ...new Set(points.flatMap((point) => Object.keys(point).filter((key) => key !== "epoch"))),
  ];
  const scorePattern = /^metrics\/(mAP50-95|mAP50|mAP75|precision|recall|AR100)(\([BM]\))?$/;
  const scores = keys.filter((key) => scorePattern.test(key));
  const trainingScores = keys.filter(
    (key) => key.startsWith("train/") && scorePattern.test(key.slice(6)),
  );
  const trainingLosses = keys.filter((key) => key.startsWith("train/") && /loss/i.test(key));
  const validationLosses = keys.filter((key) => key.startsWith("val/") && /loss/i.test(key));
  const latest = points[points.length - 1];
  const mapKey = scores.find(
    (key) => key.includes("mAP50-95") && (!segmentation || key.endsWith("(M)")),
  );
  const boxMapKey = segmentation ? scores.find((key) => key === "metrics/mAP50-95(B)") : undefined;
  const attempt = attempts.find((item) => item.id === selectedAttemptId);
  const registeredModel = registeredModels.find((model) => model.attempt_id === selectedAttemptId);
  const currentAttempt = attempts.find((item) => item.id === run.current_attempt_id);
  const canResume =
    ["failed", "cancelled"].includes(run.status) &&
    recovery?.eligible &&
    recovery.attempt_id === run.current_attempt_id;
  const live = !!attempt && activeTrainingStates.has(attempt.status);
  const observations = useTrainingObservability(
    run.project_id,
    run.id,
    selectedAttemptId,
    scope,
    visible,
    live,
    tab === "logs",
    assertOwner,
  );
  useEffect(() => {
    if (!visible || !live) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [visible, live]);
  const stages = ["queued", "preparing", "training", "evaluating", "uploading", "succeeded"];
  const stage = stages.indexOf(run.status);
  return (
    <article className="min-w-0 rounded-xl border border-border bg-card" aria-label="训练实验详情">
      <header className="border-b border-border p-4 sm:p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="break-words text-md font-semibold tracking-tight">{run.name}</h3>
              <Badge dot variant={trainingStatusVariant(run.status)}>
                {trainingStatusLabels[run.status] ?? run.status}
              </Badge>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              {framework === "libreyolo"
                ? "LibreYOLO"
                : framework === "ultralytics"
                  ? "Ultralytics"
                  : framework}{" "}
              · {trainingTaskLabel(task)} · {run.auto_trigger_id ? "自动训练" : "手动实验"} ·{" "}
              {new Date(run.created_at).toLocaleString("zh-CN")}
              {currentAttempt && ` · 当前第 ${currentAttempt.ordinal} 次执行`}
            </p>
          </div>
          {activeTrainingStates.has(run.status) && run.status !== "cancelling" && (
            <Button size="sm" disabled={pending} onClick={() => onAction({ kind: "cancel", run })}>
              停止训练
            </Button>
          )}
          {canResume && (
            <Button
              size="sm"
              variant="primary"
              disabled={pending}
              onClick={() => onAction({ kind: "resume", run })}
            >
              从检查点续训
            </Button>
          )}
        </div>
        {attempt?.status === "succeeded" && (
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-lg bg-muted p-3">
            <div className="min-w-0 text-xs">
              <p className="font-medium">将训练结果回流到项目</p>
              <p className="mt-1 text-muted-foreground">
                {registeredModel
                  ? `本次执行已添加为项目模型“${registeredModel.name}”，可在「模型与发布」查看和操作。`
                  : registrationEligibility?.qualified
                    ? segmentation
                      ? "绑定 region 项目类别后，可加载试用并人工发布实例分割模型。预测采用逐实例掩码；栅格掩码训练仍须通过数据资格预检。"
                      : "绑定项目类别后，模型会出现在「模型与发布」，供后续试用和发布。"
                    : (registrationEligibility?.reason ?? "尚未确认回流资格，请刷新后重试。")}
                {!registeredModel &&
                  !registrationEligibility?.qualified &&
                  " 仍可在「产物与记录」下载权重。"}
              </p>
            </div>
            {registeredModel ? (
              <Button size="sm" disabled>
                已回流到项目模型
              </Button>
            ) : (
              registrationEligibility?.qualified && (
                <NavigationButton asChild size="sm">
                  <Link
                    to={`/projects/${run.project_id}/learning?${new URLSearchParams({
                      tab: "models",
                      model_task: task,
                      register_run: run.id,
                      register_attempt: attempt.id,
                    })}`}
                  >
                    回流到项目模型
                  </Link>
                </NavigationButton>
              )
            )}
          </div>
        )}
        <div className="mt-5 flex items-center justify-between text-xs">
          <span className="text-muted-foreground">已完成训练轮次</span>
          <span className="font-mono tabular-nums">
            {progress.epoch} / {progress.total}
          </span>
        </div>
        <div
          className="mt-2"
          role="progressbar"
          aria-label="训练轮次进度"
          aria-valuenow={progress.epoch}
          aria-valuemin={0}
          aria-valuemax={Math.max(progress.total, progress.epoch)}
        >
          <ProgressBar
            value={progress.percent}
            color={run.status === "succeeded" ? "var(--sc-status-positive)" : "var(--sc-brand)"}
          />
        </div>
        <ol className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-6" aria-label="训练阶段">
          {stages.map((value, i) => (
            <li
              key={value}
              aria-current={i === stage ? "step" : undefined}
              className={cn(
                "flex items-center gap-1.5 text-2xs",
                i < stage || run.status === "succeeded"
                  ? "text-status-positive"
                  : i === stage
                    ? "font-medium text-foreground"
                    : "text-muted-foreground",
              )}
            >
              {i < stage || run.status === "succeeded" ? (
                <Check className="size-3.5" />
              ) : i === stage ? (
                <Activity className="size-3.5" />
              ) : (
                <Circle className="size-3" />
              )}
              {trainingStatusLabels[value]}
            </li>
          ))}
        </ol>
        {progress.epoch < progress.total && run.status === "succeeded" && (
          <p className="mt-3 text-xs text-muted-foreground">
            训练提前完成；实际轮次以执行器上报为准。
          </p>
        )}
        {run.status === "queued" && (
          <p className="mt-3 text-xs text-muted-foreground">
            等待共享 GPU 执行器领取任务。执行器离线或繁忙时会继续排队。
          </p>
        )}
        {run.status === "cancelling" && (
          <p className="mt-3 text-xs text-status-caution">
            已发出停止请求，等待执行器确认退出并保存可用产物。
          </p>
        )}
        {run.status === "lost" && (
          <p className="mt-3 text-xs text-status-caution">
            等待执行器核对旧进程退出；资源仍保留，遥测中断不会释放 GPU。
          </p>
        )}
        {run.status === "uploading" && (
          <p className="mt-3 text-xs text-muted-foreground">
            正在按已冻结的产物清单保存；连接中断后执行器会继续重试，无需重新训练。
          </p>
        )}
        {canResume && !recovery.executor_online && (
          <p className="mt-3 text-xs text-muted-foreground">
            兼容执行器当前离线；续训请求会排队等待上线。
          </p>
        )}
        {["failed", "cancelled"].includes(run.status) && !canResume && (
          <p className="mt-3 text-xs text-muted-foreground">
            续训资格：
            {recoveryError ? (
              <CopyableError message={recoveryError} preview={`暂无法核对（${recoveryError}）`} />
            ) : recovery?.attempt_id === run.current_attempt_id ? (
              recovery.reasons.map((reason) => recoveryReasons[reason] ?? "资格尚未确认").join("；")
            ) : (
              "正在核对当前执行的检查点与环境"
            )}
          </p>
        )}
        {run.error_message && (
          <p
            role="alert"
            className="mt-3 rounded-md bg-status-danger-soft p-3 text-xs text-status-danger"
          >
            <CopyableError message={run.error_message} />
          </p>
        )}
      </header>
      <div className="p-4 sm:p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            查看执行
            <select
              aria-label="查看执行记录"
              value={selectedAttemptId}
              onChange={(event) => onAttemptChange(event.target.value)}
              className="min-w-0 max-w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {attempts.map((item) => (
                <option key={item.id} value={item.id}>
                  第 {item.ordinal} 次 · {trainingStatusLabels[item.status] ?? item.status}
                  {item.resume_from_id ? " · 续训" : ""}
                </option>
              ))}
            </select>
          </label>
          <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock3 className="size-3.5" />
            执行耗时 {formatDuration(attempt?.started_at, attempt?.completed_at, now)}
          </span>
        </div>
        {attempt && ["failed", "cancelled", "lost"].includes(attempt.status) && (
          <section
            aria-label="所选执行失败与恢复说明"
            className="mb-4 rounded-lg bg-status-caution-soft p-3 text-xs"
          >
            <p className="font-medium">
              第 {attempt.ordinal} 次执行 ·{" "}
              {attempt.status === "cancelled" && !attempt.failure
                ? "执行器已确认停止"
                : failureGuidance(attempt.failure)}
            </p>
            {attempt.failure?.diagnostic && (
              <p className="mt-2 break-words text-muted-foreground">
                脱敏诊断：
                <CopyableError message={attempt.failure.diagnostic} />
              </p>
            )}
            {attempt.failure?.code === "cuda_oom" && !!run.config.configuration && (
              <Link
                className="mt-3 inline-flex rounded-md border border-border bg-background px-3 py-2 font-medium hover:bg-muted"
                to={`/projects/${run.project_id}/learning/new?clone_run=${run.id}&clone_attempt=${attempt.id}&return_run=${run.id}`}
              >
                用原配置初始化权重新建草稿，检查 batch / imgsz
              </Link>
            )}
            {attempt.failure?.code === "cuda_oom" && !run.config.configuration && (
              <Link
                className="mt-3 inline-flex rounded-md border border-border bg-background px-3 py-2 font-medium hover:bg-muted"
                to={`/projects/${run.project_id}/learning/new?return_run=${run.id}`}
              >
                在配置工作区明确迁移，检查 batch / imgsz
              </Link>
            )}
            {attempt.status === "cancelled" && !attempt.failure && (
              <p className="mt-2 text-muted-foreground">
                执行器已确认停止，已提交的检查点仍按当前执行资格核对。
              </p>
            )}
          </section>
        )}
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList
            variant="line"
            aria-label="实验详情视图"
            className="mb-4 w-full justify-start overflow-x-auto border-b border-border pb-2 [&>button]:flex-none [&>button]:text-xs"
          >
            <TabsTrigger value="metrics">
              <Activity className="size-3.5" />
              指标曲线
            </TabsTrigger>
            <TabsTrigger value="resources">
              <Gauge aria-hidden="true" className="size-3.5" />
              资源与耗时
            </TabsTrigger>
            <TabsTrigger value="artifacts">
              <Download className="size-3.5" />
              产物与记录
            </TabsTrigger>
            <TabsTrigger value="config">
              <FileText className="size-3.5" />
              训练配置
            </TabsTrigger>
            <TabsTrigger value="logs">
              <Terminal aria-hidden="true" className="size-3.5" />
              实时日志
            </TabsTrigger>
          </TabsList>
          <TabsContent value="metrics">
            {eventsError && (
              <p role="alert" className="mb-3 text-xs text-status-danger">
                <CopyableError message={eventsError} />
              </p>
            )}
            <p className="mb-3 text-xs text-muted-foreground">{trainingTaskLabel(task)}</p>
            <div
              className={cn(
                "mb-4 grid grid-cols-2 overflow-hidden rounded-lg border border-border",
                segmentation && "sm:grid-cols-3",
              )}
            >
              <div className="p-3 sm:p-4">
                <p className="text-xs text-muted-foreground">
                  {segmentation ? "最近掩码验证 mAP50–95" : "最近验证 mAP50–95"}
                </p>
                <p className="mt-1 font-mono text-stat tabular-nums tracking-tight">
                  {mapKey && latest?.[mapKey] != null ? latest[mapKey].toFixed(4) : "—"}
                </p>
              </div>
              {segmentation && (
                <div className="border-l border-border p-3 sm:p-4">
                  <p className="text-xs text-muted-foreground">最近框验证 mAP50–95</p>
                  <p className="mt-1 font-mono text-stat tabular-nums tracking-tight">
                    {boxMapKey && latest?.[boxMapKey] != null ? latest[boxMapKey].toFixed(4) : "—"}
                  </p>
                </div>
              )}
              <div
                className={cn(
                  "border-border p-3 sm:p-4",
                  segmentation
                    ? "col-span-2 border-t sm:col-span-1 sm:border-l sm:border-t-0"
                    : "border-l",
                )}
              >
                <p className="text-xs text-muted-foreground">本次已上报轮次</p>
                <p className="mt-1 font-mono text-stat tabular-nums tracking-tight">
                  {latest?.epoch ?? "—"}
                  <span className="ml-1 text-xs text-muted-foreground">轮</span>
                </p>
              </div>
            </div>
            {eventsPending ? (
              <TrainingLoading label="正在加载训练指标…" />
            ) : (
              <div className="grid gap-4 xl:grid-cols-2">
                <TrainingMetricChart
                  title="训练表现"
                  points={points}
                  keys={trainingScores}
                  domain={[0, 1]}
                  empty={
                    configuration?.effective_values?.evaluate_train === false
                      ? "未启用逐轮训练集评估"
                      : "此执行尚未上报训练集表现"
                  }
                />
                <TrainingMetricChart
                  title="训练损失"
                  points={points}
                  keys={trainingLosses}
                  empty="此执行尚未上报训练损失"
                />
                <TrainingMetricChart
                  title="验证表现"
                  points={points}
                  keys={scores}
                  empty="尚未收到验证指标"
                />
                <TrainingMetricChart
                  title="验证损失"
                  points={points}
                  keys={validationLosses}
                  empty="此执行尚未上报验证损失"
                />
              </div>
            )}
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
              展示所选执行已上报的指标，训练时每 3
              秒刷新。续训记录分别展示；最近轮次指标与最终最佳权重的评估结果可能不同。
              训练表现来自关闭增强后的训练集评估，损失越低越好；历史未上报的指标不补算，缺失值不补零。
              {framework === "libreyolo" &&
                "LibreYOLO 使用原生 mAP 与 AR100 验证指标，仅上报训练总损失，未提供训练集表现。"}
            </p>
            {!!points.length && (
              <details className="mt-4 text-xs">
                <summary className="cursor-pointer font-medium">
                  逐轮指标表 · {points.length} 轮
                </summary>
                <div className="mt-2 overflow-x-auto">
                  <table className={listStyles.table}>
                    <thead>
                      <tr className="border-b border-border">
                        <th className="whitespace-nowrap py-2 pr-3">轮次</th>
                        {keys.map((key) => (
                          <th
                            key={key}
                            className="whitespace-nowrap px-2 py-2 font-medium text-muted-foreground"
                          >
                            {metricLabel(key)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {epochPage.items.map((point, index) => (
                        <tr key={`${point.epoch}-${index}`} className="border-b border-border">
                          <td className="py-2 pr-3">{point.epoch}</td>
                          {keys.map((key) => (
                            <td key={key} className="px-2 py-2">
                              {point[key] == null ? "—" : point[key].toFixed(4)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <ListPagination
                  label="训练轮次"
                  page={epochPage.page}
                  pageSize={epochPage.pageSize}
                  total={epochPage.total}
                  onPageChange={epochPage.setPage}
                  onPageSizeChange={epochPage.setPageSize}
                />
              </details>
            )}
          </TabsContent>
          <TabsContent value="resources">
            <TrainingResourcePanel
              data={observations.telemetry.data}
              error={observations.telemetry.error?.message}
              pending={observations.telemetry.isLoading}
              live={live}
              now={now}
              status={attempt?.status ?? run.status}
            />
          </TabsContent>
          <TabsContent value="logs">
            <TrainingLogViewer
              key={`${scope}:${selectedAttemptId}`}
              projectId={run.project_id}
              runId={run.id}
              attemptId={selectedAttemptId}
              data={observations.logs.data}
              pending={observations.logs.isLoading}
              error={observations.logs.error?.message}
              assertOwner={assertOwner}
            />
          </TabsContent>
          <TabsContent value="artifacts" className="space-y-4">
            <p className="text-xs text-muted-foreground">
              每次执行独立保存日志与产物。续训沿用原实验的数据、验证集和参数。
            </p>
            {attempts.map((item) => (
              <section key={item.id} className="rounded-lg border border-border p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h4 className="flex items-center gap-2 text-sm font-medium">
                    <Layers className="size-3.5" />第 {item.ordinal} 次执行
                    {item.resume_from_id && <Badge>续训</Badge>}
                  </h4>
                  <Badge variant={trainingStatusVariant(item.status)}>
                    {trainingStatusLabels[item.status] ?? item.status}
                  </Badge>
                </div>
                <p className="mt-2 text-xs text-muted-foreground">
                  耗时 {formatDuration(item.started_at, item.completed_at)}
                  {item.heartbeat_at &&
                    ` · 最近心跳 ${new Date(item.heartbeat_at).toLocaleString("zh-CN")}`}
                </p>
                {item.error_message && (
                  <p className="mt-2 text-xs text-status-danger">
                    <CopyableError message={item.error_message} />
                  </p>
                )}
                {!Object.keys(item.artifacts).length && (
                  <p className="mt-3 text-xs text-muted-foreground">
                    尚无可下载产物，完成保存后会显示在此处。
                  </p>
                )}
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  {Object.entries(item.artifacts).map(([filename, value]) => {
                    const artifact = value as {
                      role: string;
                      size_bytes?: number;
                      sha256?: string;
                      epoch?: number;
                    };
                    return (
                      <div key={filename} className="flex min-w-0 items-center gap-2">
                        <button
                          disabled={pending}
                          onClick={() =>
                            onAction({ kind: "download", run, attemptId: item.id, filename })
                          }
                          className="flex min-w-0 flex-1 items-center gap-3 rounded-md border border-border p-3 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                        >
                          <Download className="size-4 shrink-0 text-muted-foreground" />
                          <span className="min-w-0">
                            <span className="block text-xs font-medium">
                              下载{artifactLabels[artifact.role] ?? filename}
                            </span>
                            <span className="mt-1 block truncate font-mono text-2xs text-muted-foreground">
                              {filename}
                              {artifact.size_bytes != null
                                ? ` · ${(artifact.size_bytes / 1024 / 1024).toFixed(2)} MiB`
                                : ""}
                            </span>
                          </span>
                        </button>
                        {artifact.sha256 && (
                          <CopyValueButton
                            value={artifact.sha256}
                            label={`复制${artifact.role === "log" ? "原始产物" : (artifactLabels[artifact.role] ?? filename)}哈希`}
                          />
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
            ))}
          </TabsContent>
          <TabsContent value="config">
            <div className="mb-4 flex flex-wrap items-center gap-2">
              {run.config.configuration ? (
                <>
                  <Link
                    className="inline-flex rounded-md border border-border px-3 py-2 text-xs hover:bg-muted"
                    to={`/projects/${run.project_id}/learning/new?clone_run=${run.id}${selectedAttemptId ? `&clone_attempt=${selectedAttemptId}` : ""}&return_run=${run.id}`}
                  >
                    使用原配置初始化权重新建草稿
                  </Link>
                  {run.status === "succeeded" && (
                    <TrainingRecipeSave
                      projectId={run.project_id}
                      source={{
                        source_run_id: run.id,
                        ...(selectedAttemptId ? { source_attempt_id: selectedAttemptId } : {}),
                      }}
                      disabled={pending}
                    />
                  )}
                </>
              ) : (
                <p className="text-xs text-muted-foreground">
                  旧实验只记录六项参数与固定优化器。请在配置工作区明确迁移后另存配方。
                </p>
              )}
            </div>
            {run.origin && (
              <div className="mb-4 flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                <span>
                  来源：{String(run.origin.label ?? "已固定配置")}
                  {run.origin.modified ? " · 已修改参数" : ""} · 摘要
                </span>
                <CopyValueButton value={String(run.origin.digest ?? "")} label="复制配置来源摘要" />
              </div>
            )}
            <dl className="grid gap-4 rounded-lg border border-border p-4 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">训练器</dt>
                <dd className="mt-1 font-medium">
                  {framework} {String(run.config.framework_version ?? "")}
                </dd>
              </div>
              <div className="flex items-center gap-1">
                <dt className="text-muted-foreground">数据版本</dt>
                <dd>
                  <CopyValueButton value={run.dataset_id} kind="id" label="复制训练数据版本 ID" />
                </dd>
              </div>
              {Object.entries((run.config.parameters as Record<string, unknown>) ?? {}).map(
                ([key, value]) => (
                  <div key={key}>
                    <dt className="text-muted-foreground">
                      {(
                        {
                          epochs: "训练轮数",
                          batch: "批大小",
                          imgsz: "图片尺寸",
                          lr0: "初始学习率",
                          seed: "随机种子",
                          patience: "早停耐心轮数",
                        } as Record<string, string>
                      )[key] ?? key}
                    </dt>
                    <dd className="mt-1 font-mono">{String(value)}</dd>
                  </div>
                ),
              )}
            </dl>
            <details className="mt-4 text-xs">
              <summary className="cursor-pointer font-medium">完整冻结配置</summary>
              <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 font-mono">
                {JSON.stringify(run.config, null, 2)}
              </pre>
              <p className="mt-2 flex items-center gap-1 text-muted-foreground">
                配置摘要：
                <CopyValueButton value={run.config_digest} label="复制训练配置摘要" />
              </p>
            </details>
          </TabsContent>
        </Tabs>
      </div>
    </article>
  );
}
