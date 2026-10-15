import { useState } from "react";
import { Activity, Check, Circle, Clock3, Download, FileText, Layers } from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TrainingAttemptOut, TrainingEpochEvent, TrainingRunOut } from "@/api/trainingRuns";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import {
  activeTrainingStates,
  artifactLabels,
  epochPoints,
  formatDuration,
  metricLabel,
  trainingProgress,
  trainingStatusLabels,
  trainingStatusVariant,
} from "./trainingPresentation";

type Action = {
  kind: "cancel" | "resume" | "delete" | "download";
  run: TrainingRunOut;
  attemptId?: string;
  filename?: string;
};
const chartColors = [
  "var(--sc-status-info)",
  "var(--sc-status-info-alt)",
  "var(--sc-status-positive)",
  "var(--sc-status-caution)",
];

function MetricChart({
  title,
  points,
  keys,
  empty,
}: {
  title: string;
  points: Record<string, number>[];
  keys: string[];
  empty: string;
}) {
  return (
    <section className="min-w-0 rounded-lg border border-border bg-card p-4" aria-label={title}>
      <h4 className="text-sm font-medium">{title}</h4>
      {!keys.length ? (
        <p className="flex h-44 items-center justify-center text-xs text-muted-foreground">
          {empty}
        </p>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
            {keys.map((key, i) => (
              <span key={key} className="flex items-center gap-1.5">
                <svg width="12" height="4" aria-hidden="true">
                  <rect width="12" height="4" rx="2" fill={chartColors[i % chartColors.length]} />
                </svg>
                {metricLabel(key)}
              </span>
            ))}
          </div>
          <div
            className="mt-4 h-44 w-full min-w-0"
            role="img"
            aria-label={`${title}，${points.length} 轮上报数据；详细数值见下方指标表`}
          >
            <ResponsiveContainer
              width="100%"
              height="100%"
              minWidth={0}
              initialDimension={{ width: 320, height: 176 }}
            >
              <LineChart data={points} margin={{ top: 5, right: 8, bottom: 0, left: -22 }}>
                <CartesianGrid stroke="var(--sc-border)" strokeDasharray="3 3" vertical={false} />
                <XAxis
                  dataKey="epoch"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  allowDecimals={false}
                  tick={{ fill: "var(--sc-muted-foreground)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                />
                <YAxis
                  domain={title === "验证表现" ? [0, 1] : [0, "auto"]}
                  tick={{ fill: "var(--sc-muted-foreground)", fontSize: 11 }}
                  tickLine={false}
                  axisLine={false}
                  width={60}
                  tickFormatter={(n: number) => n.toFixed(2)}
                />
                <Tooltip
                  content={({ active, payload, label }) =>
                    active && payload?.length ? (
                      <div className="rounded-md border border-border bg-popover p-3 text-xs text-popover-foreground shadow-md">
                        <p className="mb-1 font-medium">第 {label} 轮</p>
                        {payload.map((item) => (
                          <p key={String(item.dataKey)}>
                            {metricLabel(String(item.dataKey))}：{Number(item.value).toFixed(4)}
                          </p>
                        ))}
                      </div>
                    ) : null
                  }
                />
                {keys.map((key, i) => (
                  <Line
                    key={key}
                    type="linear"
                    dataKey={key}
                    stroke={chartColors[i % chartColors.length]}
                    strokeWidth={2}
                    dot={points.length < 20 ? { r: 2.5 } : false}
                    activeDot={{ r: 4 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <p className="mt-1 text-center text-2xs text-muted-foreground">训练轮次</p>
        </>
      )}
    </section>
  );
}

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
}) {
  const [tab, setTab] = useState("metrics");
  const progress = trainingProgress(run);
  const framework = String(run.config.framework ?? "");
  const points = epochPoints(events, framework);
  const keys = [
    ...new Set(points.flatMap((point) => Object.keys(point).filter((key) => key !== "epoch"))),
  ];
  const scores = keys.filter((key) =>
    /^metrics\/(mAP50-95|mAP50|mAP75|precision|recall|AR100)(\(B\))?$/.test(key),
  );
  const losses = keys.filter((key) => /loss/i.test(key));
  const latest = points[points.length - 1];
  const mapKey = scores.find((key) => key.includes("mAP50-95"));
  const attempt = attempts.find((item) => item.id === selectedAttemptId);
  const currentAttempt = attempts.find((item) => item.id === run.current_attempt_id);
  const canResume =
    ["failed", "cancelled"].includes(run.status) &&
    currentAttempt &&
    Object.values(currentAttempt.artifacts).some((value) => {
      const artifact = value as { role?: string; epoch?: number };
      return artifact.role === "checkpoint" && Number(artifact.epoch) < progress.total;
    });
  const stages = ["queued", "preparing", "training", "evaluating", "uploading", "succeeded"];
  const stage = stages.indexOf(run.status);
  return (
    <article className="min-w-0 rounded-xl border border-border bg-card" aria-label="训练实验详情">
      <header className="border-b border-border p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="break-words text-md font-semibold">{run.name}</h3>
              <Badge dot variant={trainingStatusVariant(run.status)}>
                {trainingStatusLabels[run.status] ?? run.status}
              </Badge>
            </div>
            <p className="mt-2 text-xs text-muted-foreground">
              {framework} · {run.auto_trigger_id ? "自动训练" : "手动实验"} ·{" "}
              {new Date(run.created_at).toLocaleString("zh-CN")}
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
          <ProgressBar value={progress.percent} />
        </div>
        <ol className="mt-4 grid grid-cols-3 gap-2 sm:grid-cols-6" aria-label="训练阶段">
          {stages.map((value, i) => (
            <li
              key={value}
              aria-current={i === stage ? "step" : undefined}
              className={`flex items-center gap-1.5 text-xs ${i <= stage ? "text-status-info" : "text-muted-foreground"}`}
            >
              {i < stage ? (
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
        {run.error_message && (
          <p
            role="alert"
            className="mt-3 rounded-md bg-status-danger-soft p-3 text-xs text-status-danger"
          >
            {run.error_message}
          </p>
        )}
      </header>
      <div className="p-5">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            查看执行
            <select
              aria-label="查看执行记录"
              value={selectedAttemptId}
              onChange={(event) => onAttemptChange(event.target.value)}
              className="max-w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs text-foreground"
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
            执行耗时 {formatDuration(attempt?.started_at, attempt?.completed_at)}
          </span>
        </div>
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList aria-label="实验详情视图" className="mb-3 max-w-full overflow-x-auto">
            <TabsTrigger value="metrics">
              <Activity className="size-3.5" />
              指标曲线
            </TabsTrigger>
            <TabsTrigger value="artifacts">
              <Download className="size-3.5" />
              产物与记录
            </TabsTrigger>
            <TabsTrigger value="config">
              <FileText className="size-3.5" />
              训练配置
            </TabsTrigger>
          </TabsList>
          <TabsContent value="metrics">
            {eventsError && (
              <p role="alert" className="mb-3 text-xs text-status-danger">
                {eventsError}
              </p>
            )}
            <div className="mb-4 grid grid-cols-2 gap-3">
              <div className="rounded-lg bg-muted p-3">
                <p className="text-xs text-muted-foreground">最近验证 mAP50–95</p>
                <p className="mt-1 font-mono text-stat">
                  {mapKey && latest?.[mapKey] != null ? latest[mapKey].toFixed(4) : "—"}
                </p>
              </div>
              <div className="rounded-lg bg-muted p-3">
                <p className="text-xs text-muted-foreground">本次已上报轮次</p>
                <p className="mt-1 font-mono text-stat">
                  {latest?.epoch ?? "—"}
                  <span className="ml-1 text-xs text-muted-foreground">轮</span>
                </p>
              </div>
            </div>
            {eventsPending ? (
              <p role="status" className="py-8 text-center text-sm text-muted-foreground">
                正在加载训练指标…
              </p>
            ) : (
              <div className="grid gap-4 xl:grid-cols-2">
                <MetricChart
                  title="验证表现"
                  points={points}
                  keys={scores}
                  empty="尚未收到验证指标"
                />
                <MetricChart
                  title="损失变化"
                  points={points}
                  keys={losses}
                  empty="此执行尚未上报损失指标"
                />
              </div>
            )}
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
              展示所选执行已上报的指标，训练时每 3
              秒刷新。续训记录分别展示；最近轮次指标与最终最佳权重的评估结果可能不同。
              {framework === "libreyolo" && "LibreYOLO 使用原生 mAP 与 AR100 指标。"}
            </p>
            {!!points.length && (
              <details className="mt-4 text-xs">
                <summary className="cursor-pointer font-medium">
                  逐轮指标表 · {points.length} 轮
                </summary>
                <div className="mt-2 overflow-x-auto">
                  <table className="w-full text-left tabular-nums">
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
                      {[...points].reverse().map((point, index) => (
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
              </details>
            )}
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
                  <p className="mt-2 text-xs text-status-danger">{item.error_message}</p>
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
                      <button
                        key={filename}
                        disabled={pending}
                        onClick={() =>
                          onAction({ kind: "download", run, attemptId: item.id, filename })
                        }
                        className="flex min-w-0 items-center gap-3 rounded-md border border-border p-3 text-left hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                        title={artifact.sha256 ? `SHA-256: ${artifact.sha256}` : filename}
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
                    );
                  })}
                </div>
              </section>
            ))}
            {!activeTrainingStates.has(run.status) && (
              <Button
                size="sm"
                variant="ghost"
                disabled={pending}
                onClick={() => onAction({ kind: "delete", run })}
              >
                删除实验
              </Button>
            )}
          </TabsContent>
          <TabsContent value="config">
            <dl className="grid gap-4 rounded-lg border border-border p-4 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">训练器</dt>
                <dd className="mt-1 font-medium">
                  {framework} {String(run.config.framework_version ?? "")}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">数据版本</dt>
                <dd className="mt-1 break-all font-mono">{run.dataset_id}</dd>
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
              <p className="mt-2 break-all text-muted-foreground">配置摘要：{run.config_digest}</p>
            </details>
          </TabsContent>
        </Tabs>
      </div>
    </article>
  );
}
