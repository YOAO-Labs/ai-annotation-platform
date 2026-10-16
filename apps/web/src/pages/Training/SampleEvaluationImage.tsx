import { useEffect, useState } from "react";
import { sampleEvaluationsApi, type EvaluationSampleResult } from "@/api/sampleEvaluations";
import { Button } from "@/components/ui/Button";
import { classColor } from "@/pages/Workbench/stage/colors";
import { useTrainingViewOwner } from "./trainingViewOwner";

export function SampleEvaluationImage({
  projectId,
  evaluationId,
  attemptId,
  sample,
  classes,
  scope,
}: {
  projectId: string;
  evaluationId: string;
  attemptId: string;
  sample: EvaluationSampleResult;
  classes: string[];
  scope: string;
}) {
  const { assert, owns } = useTrainingViewOwner(projectId, scope);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [truth, setTruth] = useState(true);
  const [predictions, setPredictions] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let objectUrl = "";
    setUrl("");
    setError("");
    void (async () => {
      assert(controller.signal);
      const blob = await sampleEvaluationsApi.image(
        projectId,
        evaluationId,
        attemptId,
        sample.ordinal,
        { signal: controller.signal },
      );
      assert(controller.signal);
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    })().catch((reason: unknown) => {
      if (active && owns() && !controller.signal.aborted)
        setError(reason instanceof Error ? reason.message : "冻结图片读取失败");
    });
    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [assert, owns, projectId, evaluationId, attemptId, sample.ordinal, retry]);
  const boxes = [
    ...(truth ? sample.truth.map((box) => ({ ...box, layer: "truth" as const })) : []),
    ...(predictions
      ? (sample.predictions ?? []).map((box) => ({ ...box, layer: "prediction" as const }))
      : []),
  ];
  return (
    <figure className="min-w-0">
      <figcaption className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <label className="inline-flex items-center gap-2">
          <input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} />
          真值，实线
        </label>
        <label className="inline-flex items-center gap-2">
          <input
            type="checkbox"
            checked={predictions}
            onChange={(e) => setPredictions(e.target.checked)}
          />
          预测，虚线
        </label>
        <span className="text-muted-foreground">
          {sample.width} × {sample.height} · 已按冻结方向归一化
        </span>
      </figcaption>
      {error && (
        <div
          role="alert"
          className="rounded-md bg-status-danger-soft p-4 text-xs text-status-danger"
        >
          <p>{error}</p>
          <Button className="mt-3" size="sm" onClick={() => setRetry((n) => n + 1)}>
            重试图片
          </Button>
        </div>
      )}
      {!url && !error && (
        <p
          role="status"
          className="rounded-md bg-muted p-10 text-center text-xs text-muted-foreground"
        >
          正在校验并读取冻结图片…
        </p>
      )}
      {url && (
        <div className="relative mx-auto w-fit max-w-full overflow-hidden rounded-md bg-muted">
          <img
            src={url}
            alt={`冻结样本 ${sample.ordinal + 1}`}
            className="block max-h-[36rem] max-w-full"
            onError={() => {
              setUrl("");
              setError("浏览器无法解码冻结图片，请重试");
            }}
          />
          <svg
            viewBox="0 0 1 1"
            preserveAspectRatio="none"
            role="img"
            aria-label="冻结真值与预测框，实线为真值，虚线为预测"
            className="pointer-events-none absolute inset-0 size-full"
          >
            {boxes.map((box) => (
              <rect
                key={`${box.layer}:${box.index}`}
                data-layer={box.layer}
                x={box.xyxy[0]}
                y={box.xyxy[1]}
                width={box.xyxy[2] - box.xyxy[0]}
                height={box.xyxy[3] - box.xyxy[1]}
                fill="none"
                stroke={classColor(classes[box.class_index] ?? String(box.class_index), {})}
                strokeWidth={box.layer === "truth" ? 2 : 1.5}
                strokeDasharray={box.layer === "prediction" ? "6 4" : undefined}
                vectorEffect="non-scaling-stroke"
              >
                <title>
                  {box.layer === "truth" ? "真值" : "预测"} #{box.index} ·{" "}
                  {classes[box.class_index]}
                  {"score" in box ? ` · ${(Number(box.score) * 100).toFixed(1)}%` : ""}
                </title>
              </rect>
            ))}
          </svg>
        </div>
      )}
    </figure>
  );
}
