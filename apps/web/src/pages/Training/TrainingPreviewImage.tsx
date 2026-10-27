import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useState } from "react";
import { trainingDatasetsApi } from "@/api/trainingDatasets";
import { trainingPreviewsApi, type PreviewBox } from "@/api/trainingPreviews";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { classColor } from "@/pages/Workbench/stage/colors";

export type PreviewOwner = { userId: string; token: string; key: string };
export const ownsPreviewSession = (owner: PreviewOwner) =>
  useAuthStore.getState().user?.id === owner.userId &&
  useAuthStore.getState().token === owner.token &&
  localStorage.getItem("token") === owner.token;

type Props = {
  owner: PreviewOwner;
  projectId: string;
  datasetId: string;
  previewId: string;
  sourceId?: string;
  index?: number;
  title: string;
  boxes: PreviewBox[];
  classes: string[];
};

export function TrainingPreviewImage(props: Props) {
  const { owner, projectId, datasetId, previewId, sourceId, index, title, boxes, classes } = props;
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let url = "";
    setImage("");
    setError("");
    const request = sourceId
      ? trainingDatasetsApi.sampleMedia(projectId, datasetId, sourceId, {
          signal: controller.signal,
        })
      : trainingPreviewsApi.image(projectId, previewId, index ?? 0, { signal: controller.signal });
    void request
      .then((blob) => {
        if (!active || !ownsPreviewSession(owner)) return;
        url = URL.createObjectURL(blob);
        setImage(url);
      })
      .catch((reason: unknown) => {
        if (active && ownsPreviewSession(owner) && !controller.signal.aborted)
          setError(reason instanceof Error ? reason.message : "预览图片读取失败");
      });
    return () => {
      active = false;
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [owner, projectId, datasetId, previewId, sourceId, index, retry]);
  return (
    <figure className="min-w-0 rounded-lg border border-border bg-card p-3">
      <figcaption className="mb-2 break-all text-xs font-medium">{title}</figcaption>
      {!image && !error && (
        <p role="status" className="py-8 text-center text-xs text-muted-foreground">
          正在校验图片…
        </p>
      )}
      {error && (
        <div role="alert" className="text-xs text-status-danger">
          <p>
            <CopyableError message={error} />
          </p>
          <Button size="sm" className="mt-2" onClick={() => setRetry((value) => value + 1)}>
            重试图片
          </Button>
        </div>
      )}
      {image && (
        <div className="relative mx-auto w-fit max-w-full overflow-hidden rounded-md bg-muted">
          <img
            src={image}
            alt={title}
            className="block max-h-96 max-w-full"
            onError={() => {
              setImage("");
              setError("浏览器无法解码预览图片，请重试");
            }}
          />
          {visible && (
            <svg
              viewBox="0 0 1 1"
              preserveAspectRatio="none"
              role="img"
              aria-label={`${title}矩形框`}
              className="pointer-events-none absolute inset-0 size-full"
            >
              {boxes.map((box, position) => (
                <rect
                  key={position}
                  x={box.xywh[0] - box.xywh[2] / 2}
                  y={box.xywh[1] - box.xywh[3] / 2}
                  width={box.xywh[2]}
                  height={box.xywh[3]}
                  fill="none"
                  stroke={classColor(classes[box.class_index] ?? String(box.class_index), {})}
                  strokeWidth={1.5}
                  vectorEffect="non-scaling-stroke"
                >
                  <title>{classes[box.class_index]}</title>
                </rect>
              ))}
            </svg>
          )}
        </div>
      )}
      <div className="mt-3 flex items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>{boxes.length} 个框</span>
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setVisible((value) => !value)}
          aria-pressed={visible}
        >
          {visible ? "隐藏框" : "显示框"}
        </Button>
      </div>
      <details className="mt-2 text-xs">
        <summary className="cursor-pointer text-muted-foreground">完整框坐标</summary>
        <pre className="mt-2 max-h-40 overflow-auto rounded bg-muted p-2 text-xs">
          {boxes
            .map(
              (box) =>
                `${classes[box.class_index]} ${box.xywh.map((value) => value.toFixed(6)).join(" ")}`,
            )
            .join("\n") || "无目标框"}
        </pre>
      </details>
    </figure>
  );
}
