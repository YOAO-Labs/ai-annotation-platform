import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { CopyableError } from "@/components/ui/CopyableError";
import {
  useRasterMaskRecords,
  type RasterMaskRecordDescriptor,
} from "../Workbench/stage/shared/useRasterMaskRecords";
import { rasterMaskPreviewDimensions } from "../Workbench/stage/shared/rasterMaskRender";

export function InstanceMaskPreview({
  scopeKey,
  sampleUrl,
  descriptors,
  error,
  onReady,
}: {
  scopeKey: string;
  sampleUrl?: string;
  descriptors: RasterMaskRecordDescriptor<"frozen-instance">[];
  error?: string;
  onReady?: (ready: boolean) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [renderError, setRenderError] = useState("");
  const [renderedSignature, setRenderedSignature] = useState("");
  const [loadedUrl, setLoadedUrl] = useState("");
  const { records, statusById, retry } = useRasterMaskRecords({
    scopeKey,
    descriptors,
    maxConcurrent: 2,
    maxCachedRecords: 300,
  });
  const failed = [...statusById.entries()].filter(([, status]) =>
    ["error", "deferred"].includes(status.state),
  );
  const complete = descriptors.length === records.length && failed.length === 0;
  const signature = records.map((record) => record.cacheKey).join(";");
  const ready =
    !!sampleUrl &&
    loadedUrl === sampleUrl &&
    complete &&
    !error &&
    !renderError &&
    (records.length === 0 || signature === renderedSignature);
  useEffect(() => {
    onReady?.(ready);
  }, [ready, onReady]);
  useEffect(() => {
    const target = canvas.current;
    if (!target || !records.length) return;
    try {
      const first = records[0];
      const dimensions = rasterMaskPreviewDimensions(
        first.sourceWidth,
        first.sourceHeight,
        1_048_576,
      );
      target.width = dimensions.width;
      target.height = dimensions.height;
      const context = target.getContext("2d");
      if (!context) throw new Error("当前浏览器无法绘制掩码预览");
      context.clearRect(0, 0, target.width, target.height);
      context.imageSmoothingEnabled = false;
      context.globalAlpha = 0.5;
      for (const record of records) {
        if (record.sourceWidth !== first.sourceWidth || record.sourceHeight !== first.sourceHeight)
          throw new Error("实例掩码尺寸不一致，请重新加载试用结果");
        context.drawImage(
          record.image,
          record.bounds.x * target.width,
          record.bounds.y * target.height,
          record.bounds.w * target.width,
          record.bounds.h * target.height,
        );
      }
      setRenderError("");
      setRenderedSignature(signature);
    } catch (reason) {
      setRenderError(reason instanceof Error ? reason.message : "掩码预览绘制失败");
    }
  }, [records, signature]);
  return (
    <div className="mt-3 space-y-2">
      {sampleUrl && (
        <div className="relative max-w-lg overflow-hidden rounded-md border border-border bg-background">
          <img
            src={sampleUrl}
            alt="冻结样本与逐实例掩码"
            className="block h-auto w-full"
            onLoad={() => setLoadedUrl(sampleUrl)}
            onError={() => {
              setLoadedUrl("");
              setRenderError("冻结试用图片读取失败，请重新加载");
            }}
          />
          <canvas
            ref={canvas}
            aria-label="逐实例掩码像素预览"
            className="pointer-events-none absolute inset-0 h-full w-full"
          />
        </div>
      )}
      {!complete && !failed.length && (
        <p role="status" className="text-xs text-muted-foreground">
          正在读取冻结实例掩码…
        </p>
      )}
      {(error || renderError) && (
        <p role="alert" className="text-xs text-status-danger">
          <CopyableError message={error || renderError} />
        </p>
      )}
      {failed.map(([id, status]) => (
        <div key={id} className="flex items-center gap-2 text-xs text-status-danger">
          <span>
            <CopyableError message={"message" in status ? status.message : "实例掩码读取失败"} />
          </span>
          <Button size="xs" onClick={() => retry(id)}>
            重试掩码
          </Button>
        </div>
      ))}
      {records.some((record) => record.preview) && (
        <p className="text-xs text-muted-foreground">
          当前设备显示受限像素预览；原始实例掩码仍完整保存。
        </p>
      )}
    </div>
  );
}
