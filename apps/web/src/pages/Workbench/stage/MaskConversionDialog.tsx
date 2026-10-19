import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";
import { AlertTriangleIcon, ArrowRightIcon, InfoIcon } from "lucide-react";

import {
  annotationConversionsApi,
  type AnnotationConversionDryRunResponse,
  type AnnotationConversionExecuteResponse,
  type AnnotationConversionOperation,
  type AnnotationConversionScope,
  type AnnotationConversionTarget,
} from "@/api/annotationConversions";
import { ApiError } from "@/api/client";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/shadcn/ui/alert";
import { Button } from "@/components/shadcn/ui/button";
import { Checkbox } from "@/components/shadcn/ui/checkbox";
import { randomId } from "@/utils/id";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/shadcn/ui/field";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/ui/select";
import { Spinner } from "@/components/shadcn/ui/spinner";

export interface MaskConversionDialogRequest {
  taskId: string;
  annotationIds: string[];
  sourceType: string;
  frameIndex?: number;
}

interface MaskConversionDialogProps {
  open: boolean;
  request: MaskConversionDialogRequest | null;
  onOpenChange: (open: boolean) => void;
  onCompleted: (result: AnnotationConversionExecuteResponse) => Promise<void> | void;
}

function isImageSource(sourceType: string): boolean {
  return ["polygon", "multi_polygon", "raster_mask"].includes(sourceType);
}

function targetOptions(sourceType: string): AnnotationConversionTarget[] {
  if (["polygon", "multi_polygon", "video_polygon", "video_track_polygon"].includes(sourceType)) {
    return ["mask"];
  }
  if (sourceType === "raster_mask") return ["polygon", "bbox"];
  if (sourceType === "video_track_mask") return ["bbox"];
  return [];
}

function scopeOptions(sourceType: string): AnnotationConversionScope[] {
  if (isImageSource(sourceType)) return ["image"];
  if (sourceType === "video_track_polygon") return ["current_frame", "keyframes"];
  return ["current_frame"];
}

function targetLabel(target: AnnotationConversionTarget): string {
  if (target === "mask") return "原生 Mask";
  if (target === "polygon") return "Polygon / MultiPolygon";
  return "紧致 BBox";
}

function scopeLabel(scope: AnnotationConversionScope): string {
  if (scope === "image") return "图片对象";
  if (scope === "keyframes") return "全部可见关键帧";
  return "当前帧";
}

function sourceLabel(sourceType: string): string {
  const labels: Record<string, string> = {
    polygon: "Polygon",
    multi_polygon: "MultiPolygon",
    raster_mask: "Raster Mask",
    video_polygon: "视频单帧 Polygon",
    video_track_polygon: "视频 Polygon Track",
    video_track_mask: "视频 Mask Track",
  };
  return labels[sourceType] ?? sourceType;
}

function reasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    topology_changed_on_rasterization: "栅格分辨率改变了组件或孔洞拓扑",
    pixel_xor_changed: "矢量往返后像素发生变化",
    source_pixels_dropped: "矢量往返时丢失了来源像素",
    bbox_includes_background: "紧致框包含了背景像素",
  };
  return labels[reason] ?? reason;
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object") {
    const detail = error.detailRaw as { reason?: unknown; message?: unknown };
    const reason = typeof detail.reason === "string" ? detail.reason : null;
    const message = typeof detail.message === "string" ? detail.message : error.message;
    return reason ? `${message}（${reason}）` : message;
  }
  return error instanceof Error ? error.message : String(error);
}

export function MaskConversionDialog({
  open,
  request,
  onOpenChange,
  onCompleted,
}: MaskConversionDialogProps) {
  const targets = useMemo(() => targetOptions(request?.sourceType ?? ""), [request?.sourceType]);
  const scopes = useMemo(() => scopeOptions(request?.sourceType ?? ""), [request?.sourceType]);
  const [target, setTarget] = useState<AnnotationConversionTarget>("mask");
  const [operation, setOperation] = useState<AnnotationConversionOperation>("copy");
  const [scope, setScope] = useState<AnnotationConversionScope>("image");
  const [materializeHeld, setMaterializeHeld] = useState(false);
  const [preview, setPreview] = useState<AnnotationConversionDryRunResponse | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);
  const [planning, setPlanning] = useState(false);
  const [executing, setExecuting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  useEffect(() => {
    if (!open || !request) return;
    const nextTargets = targetOptions(request.sourceType);
    const nextScopes = scopeOptions(request.sourceType);
    setTarget(nextTargets[0] ?? "mask");
    setOperation("copy");
    setScope(nextScopes[0] ?? "image");
    setMaterializeHeld(false);
    setPreview(null);
    setIdempotencyKey(null);
    setError(null);
    setConfirmOpen(false);
  }, [open, request]);

  const busy = planning || executing;
  const heldOptionVisible =
    request?.sourceType === "video_track_polygon" && scope === "current_frame";
  const canPreview = !!request && targets.length > 0 && !busy;
  const needsConfirmation = operation === "replace" || (preview?.summary.lossy_count ?? 0) > 0;

  const runDryRun = async () => {
    if (!request || !canPreview) return;
    setPlanning(true);
    setError(null);
    try {
      const result = await annotationConversionsApi.dryRun(request.taskId, {
        annotation_ids: request.annotationIds,
        target,
        operation,
        scope,
        frame_index: scope === "current_frame" ? request.frameIndex : null,
        materialize_held: heldOptionVisible && materializeHeld,
      });
      setPreview(result);
      setIdempotencyKey(`conversion:${randomId()}`);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setPlanning(false);
    }
  };

  const execute = async () => {
    if (!request || !preview || !idempotencyKey || executing) return;
    setExecuting(true);
    setError(null);
    try {
      const result = await annotationConversionsApi.execute(request.taskId, {
        plan_token: preview.plan_token,
        idempotency_key: idempotencyKey,
        confirm_replace: operation === "replace",
        confirm_lossy: preview.summary.lossy_count > 0,
      });
      await onCompleted(result);
      setConfirmOpen(false);
      onOpenChange(false);
    } catch (nextError) {
      setError(errorMessage(nextError));
      setConfirmOpen(false);
    } finally {
      setExecuting(false);
    }
  };

  return (
    <>
      <Dialog
        open={open}
        onOpenChange={(nextOpen) => {
          if (!nextOpen && busy) return;
          onOpenChange(nextOpen);
        }}
      >
        <DialogContent className="max-h-[85vh] overflow-y-auto" showCloseButton={!busy}>
          <DialogHeader>
            <DialogTitle>标注转换中心</DialogTitle>
            <DialogDescription>
              先冻结对象版本并生成逐项报告，再以单事务执行；任何对象漂移都会使整批失败。
            </DialogDescription>
          </DialogHeader>

          {request ? (
            <Alert>
              <InfoIcon />
              <AlertTitle>
                {sourceLabel(request.sourceType)} · {request.annotationIds.length} 个对象
              </AlertTitle>
              <AlertDescription>
                {request.frameIndex === undefined
                  ? "图片 / 关键帧范围"
                  : `当前 F${request.frameIndex}`}
              </AlertDescription>
            </Alert>
          ) : null}

          <FieldGroup>
            <Field>
              <FieldLabel>目标类型</FieldLabel>
              <Select
                value={target}
                disabled={busy || !!preview}
                onValueChange={(value) => setTarget(value as AnnotationConversionTarget)}
              >
                <SelectTrigger className="w-full" aria-label="目标类型">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {targets.map((option) => (
                    <SelectItem key={option} value={option}>
                      {targetLabel(option)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              {target === "bbox" ? (
                <FieldDescription>生成前景像素的紧致 AABB；不会冒充旋转框。</FieldDescription>
              ) : null}
            </Field>

            <Field>
              <FieldLabel>结果策略</FieldLabel>
              <Select
                value={operation}
                disabled={busy || !!preview}
                onValueChange={(value) => setOperation(value as AnnotationConversionOperation)}
              >
                <SelectTrigger className="w-full" aria-label="结果策略">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="copy">创建副本并保留来源</SelectItem>
                  <SelectItem value="replace">替换来源</SelectItem>
                </SelectContent>
              </Select>
              <FieldDescription>
                视频 current-frame 替换只抑制当前帧，不会删除范围外关键帧。
              </FieldDescription>
            </Field>

            {scopes.length > 1 ? (
              <Field>
                <FieldLabel>转换范围</FieldLabel>
                <Select
                  value={scope}
                  disabled={busy || !!preview}
                  onValueChange={(value) => {
                    setScope(value as AnnotationConversionScope);
                    setMaterializeHeld(false);
                  }}
                >
                  <SelectTrigger className="w-full" aria-label="转换范围">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {scopes.map((option) => (
                      <SelectItem key={option} value={option}>
                        {scopeLabel(option)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            ) : null}

            {heldOptionVisible ? (
              <Field orientation="horizontal">
                <Checkbox
                  id="conversion-materialize-held"
                  checked={materializeHeld}
                  disabled={busy || !!preview}
                  onCheckedChange={(checked) => setMaterializeHeld(checked === true)}
                />
                <div className="flex min-w-0 flex-1 flex-col gap-1">
                  <FieldLabel htmlFor="conversion-materialize-held">
                    允许物化 held / 插值帧
                  </FieldLabel>
                  <FieldDescription>当前帧不是精确关键帧时必须显式开启。</FieldDescription>
                </div>
              </Field>
            ) : null}
          </FieldGroup>

          {error ? (
            <Alert variant="destructive">
              <AlertTriangleIcon />
              <AlertTitle>转换未完成</AlertTitle>
              <AlertDescription>
                <CopyableError message={error} />
              </AlertDescription>
            </Alert>
          ) : null}

          {preview ? (
            <div className="flex flex-col gap-3" aria-label="转换预览报告">
              <div className="text-xs text-muted-foreground">
                {sourceLabel(request?.sourceType ?? "")} → {targetLabel(preview.target)} ·{" "}
                {scopeLabel(preview.scope)} ·
                {preview.operation === "copy" ? " 保留来源" : " 替换来源"}
              </div>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {[
                  ["来源", preview.summary.source_count],
                  ["结果", preview.summary.result_count],
                  ["物化帧", preview.summary.materialized_held_frames],
                  ["有损项", preview.summary.lossy_count],
                ].map(([label, value]) => (
                  <div key={label} className="rounded-md border border-border bg-muted px-3 py-2">
                    <div className="text-xs text-muted-foreground">{label}</div>
                    <div className="text-sm font-medium text-foreground">{value}</div>
                  </div>
                ))}
              </div>
              <div className="max-h-56 space-y-2 overflow-y-auto pr-1">
                {preview.items.map((item) => (
                  <div
                    key={item.source_annotation_id}
                    className="rounded-md border border-border p-3 text-xs"
                  >
                    <div className="flex items-center gap-2 font-medium text-foreground">
                      <span
                        className="font-mono text-muted-foreground"
                        title={item.source_annotation_id}
                      >
                        {item.source_annotation_id.slice(0, 8)}
                      </span>
                      <span>{item.source_type}</span>
                      <ArrowRightIcon className="size-3" />
                      <span>{item.target_type}</span>
                      {item.lossy ? <span className="text-status-caution">有损</span> : null}
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-muted-foreground sm:grid-cols-3">
                      <span>
                        面积 {item.source_area_pixels} → {item.target_area_pixels}
                      </span>
                      <span>XOR {item.changed_pixels} px</span>
                      <span>
                        组件 {item.source_components} → {item.target_components}
                      </span>
                      <span>
                        孔洞 {item.source_holes} → {item.target_holes}
                      </span>
                      <span>
                        顶点 {item.source_vertices} → {item.target_vertices}
                      </span>
                      <span>
                        帧 {item.frame_indexes.length ? item.frame_indexes.join(", ") : "图片"}
                      </span>
                    </div>
                    {item.reasons.length ? (
                      <div className="mt-2 text-status-caution">
                        {item.reasons.map(reasonLabel).join("；")}
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
              <p className="m-0 text-xs text-muted-foreground">
                计划有效至 {new Date(preview.expires_at).toLocaleTimeString()}
                ；执行前会重新检查版本、锁和内容摘要。
              </p>
            </div>
          ) : null}

          <DialogFooter>
            <Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
              取消
            </Button>
            {preview ? (
              <>
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={() => {
                    setPreview(null);
                    setIdempotencyKey(null);
                  }}
                >
                  重新配置
                </Button>
                <Button
                  disabled={busy}
                  onClick={() => {
                    if (needsConfirmation) setConfirmOpen(true);
                    else void execute();
                  }}
                >
                  {executing ? <Spinner /> : null}
                  执行转换
                </Button>
              </>
            ) : (
              <Button disabled={!canPreview} onClick={() => void runDryRun()}>
                {planning ? <Spinner /> : null}
                生成预览
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirmOpen} onOpenChange={(next) => !executing && setConfirmOpen(next)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认执行转换？</AlertDialogTitle>
            <AlertDialogDescription>
              {operation === "replace" ? "来源对象会被替换；" : ""}
              {(preview?.summary.lossy_count ?? 0) > 0
                ? `${preview?.summary.lossy_count} 项会改变像素真值。`
                : "转换报告显示无像素损失。"}
              执行时任一对象冲突都会整批回滚。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={executing}>返回预览</AlertDialogCancel>
            <AlertDialogAction
              variant={operation === "replace" ? "destructive" : "default"}
              disabled={executing}
              onClick={(event) => {
                event.preventDefault();
                void execute();
              }}
            >
              {executing ? <Spinner /> : null}
              确认执行
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
