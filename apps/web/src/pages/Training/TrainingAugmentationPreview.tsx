import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Image, RefreshCw, Square } from "lucide-react";
import {
  trainingPreviewsApi,
  type PreviewBox,
  type TrainingPreviewCreate,
  type TrainingPreviewOut,
} from "@/api/trainingPreviews";
import { useAuthStore } from "@/stores/authStore";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { trainingInputClass } from "./TrainingParameterField";
import {
  TrainingPreviewImage,
  ownsPreviewSession,
  type PreviewOwner,
} from "./TrainingPreviewImage";
import { classColor } from "@/pages/Workbench/stage/colors";

type Props = {
  projectId: string;
  input: Pick<TrainingPreviewCreate, "dataset_id" | "framework" | "base_model" | "configuration">;
  binding: string;
  epochs: number;
  classes: string[];
  disabled: boolean;
};
const active = new Set(["queued", "running", "cancelling", "lost"]);
const statuses: Record<TrainingPreviewOut["status"], string> = {
  queued: "等待 CPU 预览槽",
  running: "原生变换中",
  cancelling: "正在停止",
  lost: "执行器失联，等待退出确认",
  succeeded: "预览已完成",
  failed: "预览失败",
  cancelled: "预览已取消",
  expired: "预览已过期",
};
const canonical = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object")
    return `{${Object.entries(value)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(",")}}`;
  return JSON.stringify(value);
};
const message = (reason: unknown) =>
  reason instanceof Error ? reason.message : "原生预览请求失败";

export function TrainingAugmentationPreview(props: Props) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const inputIdentity = canonical(props.input);
  const owner = useMemo(
    () => ({
      userId,
      token,
      projectId: props.projectId,
      binding: props.binding,
      inputIdentity,
      key: randomId(),
    }),
    [userId, token, props.projectId, props.binding, inputIdentity],
  );
  return <PreviewContent key={owner.key} {...props} owner={owner} />;
}

function PreviewContent({
  projectId,
  input,
  epochs,
  classes,
  disabled,
  owner,
}: Props & { owner: PreviewOwner }) {
  const [seed, setSeed] = useState("42");
  const [epoch, setEpoch] = useState("1");
  const [count, setCount] = useState(1);
  const [result, setResult] = useState<TrainingPreviewOut | null>(null);
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [pollRetry, setPollRetry] = useState(0);
  const [selected, setSelected] = useState(0);
  const [sourceId, setSourceId] = useState("");
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const request = useRef<TrainingPreviewCreate | null>(null);
  const alive = useRef(true);
  const caps = useQuery({
    queryKey: ["training-preview-capability", owner.key, projectId],
    queryFn: ({ signal }) => trainingPreviewsApi.capabilities(projectId, { signal }),
    enabled: input.framework === "ultralytics" && !input.base_model?.endsWith("-seg"),
    retry: false,
    refetchInterval: 5000,
  });
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      controller.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (!result || result.status === "expired") return;
    const remaining = new Date(result.expires_at).getTime() - Date.now();
    const timer = setTimeout(
      () => {
        sequence.current += 1;
        controller.current?.abort();
        setSending(false);
        setResult({ ...result, status: "expired", report: null });
      },
      Math.max(0, remaining),
    );
    return () => clearTimeout(timer);
  }, [result]);
  const owns = (ticket: number) =>
    alive.current && sequence.current === ticket && ownsPreviewSession(owner);
  const accepts = (value: TrainingPreviewOut, body: TrainingPreviewCreate) =>
    value.project_id === projectId &&
    canonical(value.request) === canonical(body) &&
    (!value.report ||
      (value.report.preview_id === value.id &&
        value.report.input_digest === value.input_digest &&
        value.report.config_digest === value.config_digest &&
        value.report.seed === body.seed &&
        value.report.epoch === body.epoch)) &&
    (value.status !== "succeeded" || !!value.report);
  const invalidate = () => {
    sequence.current += 1;
    controller.current?.abort();
    request.current = null;
    setResult(null);
    setError("");
    setSending(false);
    setSelected(0);
    setSourceId("");
  };
  const validControls =
    /^\d+$/.test(seed) &&
    Number(seed) <= 2147483647 &&
    /^\d+$/.test(epoch) &&
    Number(epoch) >= 1 &&
    Number(epoch) <= epochs;
  const generating = !!result && active.has(result.status);

  const generate = async (retry = false) => {
    if (sending || generating || !validControls || disabled || !caps.data?.online) return;
    const body =
      retry && request.current
        ? request.current
        : {
            ...input,
            seed: Number(seed),
            epoch: Number(epoch),
            count,
            request_id: randomId(),
          };
    request.current = body;
    const ticket = ++sequence.current;
    controller.current?.abort();
    const pending = (controller.current = new AbortController());
    setSending(true);
    setError("");
    setResult(null);
    setSelected(0);
    setSourceId("");
    try {
      const value = await trainingPreviewsApi.create(projectId, body, { signal: pending.signal });
      if (!owns(ticket)) return;
      if (!accepts(value, body)) throw new Error("预览输入身份不符，请重新生成");
      setResult(value);
    } catch (reason) {
      if (owns(ticket) && !pending.signal.aborted) setError(message(reason));
    } finally {
      if (owns(ticket)) setSending(false);
    }
  };

  useEffect(() => {
    if (!result || !active.has(result.status) || !request.current) return;
    const ticket = sequence.current;
    const body = request.current;
    const pending = new AbortController();
    const timer = setTimeout(() => {
      void trainingPreviewsApi
        .get(projectId, result.id, { signal: pending.signal })
        .then((value) => {
          if (!owns(ticket) || pending.signal.aborted) return;
          if (
            value.id !== result.id ||
            value.input_digest !== result.input_digest ||
            !accepts(value, body)
          )
            throw new Error("预览输入身份不符，请重新生成");
          setError("");
          setResult(value);
        })
        .catch((reason: unknown) => {
          if (owns(ticket) && !pending.signal.aborted) setError(message(reason));
        });
    }, 1000);
    return () => {
      clearTimeout(timer);
      pending.abort();
    };
    // The keyed owner freezes the draft, runtime, credentials and request inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, owner, projectId, pollRetry]);

  const cancel = async () => {
    if (!result || sending) return;
    const ticket = sequence.current;
    const body = request.current;
    const pending = (controller.current = new AbortController());
    setSending(true);
    setError("");
    try {
      const value = await trainingPreviewsApi.cancel(projectId, result.id, {
        signal: pending.signal,
      });
      if (owns(ticket) && body) {
        if (!accepts(value, body)) throw new Error("预览输入身份不符");
        setResult(value);
      }
    } catch (reason) {
      if (owns(ticket) && !pending.signal.aborted) setError(message(reason));
    } finally {
      if (owns(ticket)) setSending(false);
    }
  };

  const report = result?.status === "succeeded" ? result.report : null;
  const output = report?.outputs.find((item) => item.index === selected) ?? report?.outputs[0];
  const usedSources = output
    ? (report?.source_pool.filter((source) => output.source_draws.includes(source.task_id)) ?? [])
    : [];
  const source = usedSources.find((item) => item.task_id === sourceId) ?? usedSources[0];
  const sourceBoxes: PreviewBox[] =
    source?.labels
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const values = line.trim().split(/\s+/).map(Number);
        return { class_index: values[0], xywh: [values[1], values[2], values[3], values[4]] };
      }) ?? [];
  const unavailable = input.base_model?.endsWith("-seg")
    ? "实例分割尚未通过原生增强预览资格验证；可继续配置与训练。"
    : input.framework !== "ultralytics"
      ? "LibreYOLO 尚未通过原生增强预览验证；可继续配置与训练。"
      : caps.error
        ? message(caps.error)
        : !caps.data?.online
          ? (caps.data?.reason ?? "正在检查原生预览能力…")
          : "";
  return (
    <section aria-label="原生增强预览" className="mt-6 rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Image className="size-4 text-muted-foreground" />
          原生增强预览
        </h3>
        <Badge variant="outline">独立 CPU</Badge>
      </div>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        查看当前参数经过训练器原生变换后的图片与框。种子和轮次仅用于预览，不会修改训练配置；随机训练批次会有所不同。
      </p>
      {unavailable && (
        <p role="status" className="mt-3 text-xs text-muted-foreground">
          {unavailable}
        </p>
      )}
      {input.framework === "ultralytics" && (
        <>
          <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="text-xs text-muted-foreground">
              预览种子
              <input
                aria-label="预览种子"
                inputMode="numeric"
                className={`${trainingInputClass} mt-1.5`}
                value={seed}
                onChange={(event) => {
                  invalidate();
                  setSeed(event.target.value);
                }}
              />
            </label>
            <label className="text-xs text-muted-foreground">
              预览轮次
              <input
                aria-label="预览轮次"
                inputMode="numeric"
                className={`${trainingInputClass} mt-1.5`}
                value={epoch}
                onChange={(event) => {
                  invalidate();
                  setEpoch(event.target.value);
                }}
              />
              <span className="mt-1 block">1–{epochs}，遵循关闭 Mosaic 的轮次</span>
            </label>
            <label className="text-xs text-muted-foreground">
              输出张数
              <select
                aria-label="预览张数"
                className={`${trainingInputClass} mt-1.5`}
                value={count}
                onChange={(event) => {
                  invalidate();
                  setCount(Number(event.target.value));
                }}
              >
                {[1, 2, 3, 4].map((value) => (
                  <option key={value} value={value}>
                    {value} 张
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!validControls && (
            <p className="mt-2 text-xs text-status-caution">
              请输入 0–2147483647 的整数种子及训练轮次内的整数。
            </p>
          )}
          <div className="mt-3 flex flex-wrap gap-2">
            <Button
              size="sm"
              disabled={disabled || sending || generating || !caps.data?.online || !validControls}
              onClick={() => void generate()}
            >
              {sending && !result ? "正在提交…" : "生成原生预览"}
            </Button>
            <Button
              size="sm"
              disabled={sending}
              onClick={() => {
                invalidate();
                setSeed(String(crypto.getRandomValues(new Uint32Array(1))[0] & 0x7fffffff));
              }}
            >
              <RefreshCw className="size-3.5" />
              更换预览种子
            </Button>
            {generating && (
              <Button
                size="sm"
                disabled={sending || result.status === "cancelling"}
                onClick={() => void cancel()}
              >
                <Square className="size-3.5" />
                取消预览
              </Button>
            )}
          </div>
          {error && (
            <div
              role="alert"
              className="mt-3 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution"
            >
              <p>
                <CopyableError message={error} />
              </p>
              <Button
                size="sm"
                className="mt-2"
                onClick={() => (result ? setPollRetry((value) => value + 1) : void generate(true))}
              >
                {result ? "重试读取预览" : "重试原请求"}
              </Button>
            </div>
          )}
          {result && (
            <div role="status" className="mt-3 text-xs text-muted-foreground">
              <p>
                {statuses[result.status]} · 有效至 {new Date(result.expires_at).toLocaleString()}
              </p>
              {result.error_message && (
                <p className="mt-1">
                  <CopyableError message={result.error_message} />
                </p>
              )}
            </div>
          )}
          {report && output && result && (
            <div className="mt-4 space-y-4">
              <div className="flex flex-wrap items-end gap-3 text-xs">
                <label>
                  输出图片
                  <select
                    aria-label="查看输出图片"
                    className={`${trainingInputClass} mt-1`}
                    value={selected}
                    onChange={(event) => {
                      setSelected(Number(event.target.value));
                      setSourceId("");
                    }}
                  >
                    {report.outputs.map((item) => (
                      <option key={item.index} value={item.index}>
                        第 {item.index + 1} 张 · {item.width} × {item.height}
                      </option>
                    ))}
                  </select>
                </label>
                <div>
                  <label htmlFor="augmentation-source">原生使用的来源</label>
                  <div className="mt-1 flex items-center gap-2">
                    <select
                      id="augmentation-source"
                      aria-label="查看原始来源"
                      className={`${trainingInputClass} min-w-0 flex-1`}
                      value={source?.task_id ?? ""}
                      onChange={(event) => setSourceId(event.target.value)}
                    >
                      {usedSources.map((item, index) => (
                        <option key={item.task_id} value={item.task_id}>
                          来源 {index + 1} · 取用{" "}
                          {output.source_draws.filter((id) => id === item.task_id).length} 次
                        </option>
                      ))}
                    </select>
                    {source && (
                      <CopyValueButton
                        value={source.task_id}
                        kind="id"
                        label="复制原始来源任务 ID"
                      />
                    )}
                  </div>
                </div>
              </div>
              <div className="grid min-w-0 gap-3 lg:grid-cols-2">
                {source && (
                  <TrainingPreviewImage
                    key={`source:${source.task_id}`}
                    owner={owner}
                    projectId={projectId}
                    datasetId={input.dataset_id}
                    previewId={result.id}
                    sourceId={source.task_id}
                    title={`原始来源 ${usedSources.findIndex((item) => item.task_id === source.task_id) + 1}`}
                    boxes={sourceBoxes}
                    classes={classes}
                  />
                )}
                <TrainingPreviewImage
                  key={`output:${output.index}`}
                  owner={owner}
                  projectId={projectId}
                  datasetId={input.dataset_id}
                  previewId={result.id}
                  index={output.index}
                  title={`原生增强结果 ${output.index + 1}`}
                  boxes={output.boxes}
                  classes={classes}
                />
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                {classes
                  .filter((_, index) => output.boxes.some((box) => box.class_index === index))
                  .map((name) => (
                    <span key={name} className="flex items-center gap-1.5">
                      <svg className="size-2" viewBox="0 0 10 10" aria-hidden="true">
                        <circle cx="5" cy="5" r="5" fill={classColor(name, {})} />
                      </svg>
                      {name}
                    </span>
                  ))}
              </div>
              <p className="text-xs leading-relaxed text-muted-foreground">
                种子 {report.seed} · 第 {report.epoch} 轮 ·{" "}
                {report.mosaic_closed ? "已关闭 Mosaic / MixUp / CutMix" : "遵循当前增强概率"}
                。原生累计裁剪 {output.clipped} 次、过滤 {output.filtered}{" "}
                次，包含中间分支；不是唯一目标数或原图与结果框数之差。来源池{" "}
                {report.source_pool.length} 张；实际取用 {output.source_draws.length} 次，读取{" "}
                {output.sources_read.length} 次（可包含跳过的混合分支）。
              </p>
              <details className="text-xs">
                <summary className="cursor-pointer font-medium">实际参数与原生步骤</summary>
                <pre className="mt-2 max-h-64 overflow-auto rounded bg-muted p-3 text-xs">
                  {JSON.stringify(
                    {
                      native_parameters: report.native_parameters,
                      transform_parameters: report.transform_parameters,
                      operations: output.operations,
                      pool: report.pool_summary,
                    },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </div>
          )}
        </>
      )}
    </section>
  );
}
