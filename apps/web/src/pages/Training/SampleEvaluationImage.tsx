import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";
import { sampleEvaluationsApi, type EvaluationSampleResult } from "@/api/sampleEvaluations";
import { Button } from "@/components/ui/Button";
import { classColor } from "@/pages/Workbench/stage/colors";
import { useTrainingViewOwner } from "./trainingViewOwner";
import { InstanceMaskPreview } from "../Projects/InstanceMaskPreview";
import { validateCocoRle } from "../Workbench/stage/shared/geometry/maskRle";
import type { RasterMaskRecordDescriptor } from "../Workbench/stage/shared/useRasterMaskRecords";

export function SampleEvaluationImage({
  projectId,
  evaluationId,
  attemptId,
  sample,
  classes,
  scope,
  task = "detect",
}: {
  projectId: string;
  evaluationId: string;
  attemptId: string;
  sample: EvaluationSampleResult;
  classes: string[];
  scope: string;
  task?: "detect" | "segment";
}) {
  const { assert, owns } = useTrainingViewOwner(projectId, scope);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [truth, setTruth] = useState(true);
  const [predictions, setPredictions] = useState(true);
  const maskPreview = useMemo(() => {
    const descriptors: RasterMaskRecordDescriptor<"frozen-instance">[] = [];
    if (task !== "segment") return { descriptors, error: "" };
    try {
      const layers = [
        ...(truth ? sample.truth.map((item) => ({ item, layer: "truth" as const })) : []),
        ...(predictions
          ? (sample.predictions ?? []).map((item) => ({ item, layer: "prediction" as const }))
          : []),
      ];
      for (const { item, layer } of layers) {
        if (!("rle" in item)) throw new Error("分割证据缺少原图实例掩码，请重新加载报告");
        const rle = validateCocoRle(item.rle);
        if (
          rle.size[0] !== sample.height ||
          rle.size[1] !== sample.width ||
          !/^[a-f0-9]{64}$/.test(item.mask_sha256) ||
          rle.counts.reduce((area, count, index) => area + (index % 2 ? count : 0), 0) !==
            item.area_pixels
        )
          throw new Error("冻结掩码尺寸或像素面积与证据不一致");
        descriptors.push({
          id: `${layer}:${item.index}`,
          source: "frozen-instance",
          ref: { size: rle.size, sha256: item.mask_sha256 },
          revision: sample.evidence_digest ?? sample.label_sha256,
          color: layer === "truth" ? "#2563eb" : "#d97706",
          colorRevision: "mask-evaluation-1",
          zOrder: descriptors.length,
          selected: false,
          deduplicateLoad: false,
          load: async () => {
            assert();
            return rle;
          },
        });
      }
      return { descriptors, error: "" };
    } catch (reason) {
      return {
        descriptors: [],
        error: reason instanceof Error ? reason.message : "冻结掩码解码失败",
      };
    }
  }, [task, truth, predictions, sample, assert]);
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
    ...(truth
      ? sample.truth
          .filter((box) => "xyxy" in box)
          .map((box) => ({ ...box, layer: "truth" as const }))
      : []),
    ...(predictions
      ? (sample.predictions ?? [])
          .filter((box) => "xyxy" in box)
          .map((box) => ({ ...box, layer: "prediction" as const }))
      : []),
  ];
  return (
    <figure className="min-w-0">
      <figcaption className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <label className="inline-flex items-center gap-2">
          <input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} />
          {task === "segment" ? "真值掩码，蓝色" : "真值，实线"}
        </label>
        <label className="inline-flex items-center gap-2">
          <input
            type="checkbox"
            checked={predictions}
            onChange={(e) => setPredictions(e.target.checked)}
          />
          {task === "segment" ? "预测掩码，橙色" : "预测，虚线"}
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
          <p>
            <CopyableError message={error} />
          </p>
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
      {url && task === "segment" && (
        <InstanceMaskPreview
          key={`${scope}:${truth}:${predictions}`}
          scopeKey={`${scope}:${sample.ordinal}:${truth}:${predictions}`}
          sampleUrl={url}
          descriptors={maskPreview.descriptors}
          error={maskPreview.error}
        />
      )}
      {url && task === "detect" && (
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
