import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";
import { InfoIcon } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/shadcn/ui/alert";
import { Button } from "@/components/shadcn/ui/button";
import { Checkbox } from "@/components/shadcn/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/shadcn/ui/field";
import { Input } from "@/components/shadcn/ui/input";
import { Spinner } from "@/components/shadcn/ui/spinner";
import { ToggleGroup, ToggleGroupItem } from "@/components/shadcn/ui/toggle-group";
import type { VideoTrackerDirection } from "@/api/videoTracker";

type CorrectionMode = "save_only" | "backward" | "forward" | "bidirectional";

export interface VideoMaskCorrectionIntent {
  mode: CorrectionMode;
  direction?: VideoTrackerDirection;
  fromFrame: number;
  toFrame: number;
  modelKey?: string;
  modelId?: string;
  backendId?: string;
  allowBboxFallback: boolean;
  text?: string;
  segmentId?: string;
}

export interface VideoMaskCorrectionModel {
  backendId: string;
  modelKey: string;
  modelId: string;
  nativeMask: boolean;
  textRequired: boolean;
  maxWindowFrames: number;
}

interface VideoMaskCorrectionDialogProps {
  open: boolean;
  frameIndex: number;
  minFrame: number;
  maxFrame: number;
  segmentId?: string;
  models: VideoMaskCorrectionModel[];
  keyframeSaved?: boolean;
  createError?: string | null;
  createRetryable?: boolean;
  submitting: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmit: (intent: VideoMaskCorrectionIntent) => Promise<void>;
}

const DEFAULT_SPAN = 30;

function preferredCorrectionModel(
  models: VideoMaskCorrectionModel[],
): VideoMaskCorrectionModel | null {
  for (const modelKey of ["sam3_video_interactive", "sam2_video", "sam3_video"]) {
    const model = models.find((item) => item.modelKey === modelKey);
    if (model) return model;
  }
  return models[0] ?? null;
}

function modelLabel(model: VideoMaskCorrectionModel | null): string {
  if (model?.modelKey === "sam3_video_interactive") return "SAM3 PVS · 原生 Mask seed";
  if (model?.modelKey === "sam2_video") return "Grounded-SAM2 · 原生 Mask seed";
  if (model?.modelKey === "sam3_video") return "SAM3 Multiplex · bbox seed 降级";
  if (model) return `${model.modelKey} · ${model.nativeMask ? "原生 Mask seed" : "bbox seed 降级"}`;
  return "无可用纠错模型";
}

export function correctionWindow(
  mode: CorrectionMode,
  frameIndex: number,
  maxFrame: number,
  span = DEFAULT_SPAN,
  minFrame = 0,
): { fromFrame: number; toFrame: number } {
  if (mode === "save_only") return { fromFrame: frameIndex, toFrame: frameIndex };
  return {
    fromFrame: mode === "forward" ? frameIndex : Math.max(minFrame, frameIndex - span),
    toFrame: mode === "backward" ? frameIndex : Math.min(maxFrame, frameIndex + span),
  };
}

export function VideoMaskCorrectionDialog({
  open,
  frameIndex,
  minFrame,
  maxFrame,
  segmentId,
  models = [],
  keyframeSaved = false,
  createError = null,
  createRetryable = true,
  submitting,
  onOpenChange,
  onSubmit,
}: VideoMaskCorrectionDialogProps) {
  const [mode, setMode] = useState<CorrectionMode>("save_only");
  const [allowFallback, setAllowFallback] = useState(false);
  const [text, setText] = useState("");
  const model = useMemo(() => preferredCorrectionModel(models), [models]);
  const nativeMask = model?.nativeMask === true;
  const fallback = model !== null && !nativeMask;
  const span = nativeMask ? Math.max(0, model.maxWindowFrames - 1) : DEFAULT_SPAN;
  const propagationAvailable = !nativeMask || model.maxWindowFrames > 1;
  const window = correctionWindow(mode, frameIndex, maxFrame, span, minFrame);

  useEffect(() => {
    if (!open) return;
    setMode("save_only");
    setAllowFallback(false);
    setText("");
  }, [open, frameIndex]);

  const propagationRequested = mode !== "save_only";
  const validDirectionalWindow =
    mode === "save_only" ||
    (mode === "backward" && window.fromFrame < frameIndex) ||
    (mode === "forward" && frameIndex < window.toFrame) ||
    (mode === "bidirectional" && window.fromFrame < frameIndex && frameIndex < window.toFrame);
  const canSubmit =
    !submitting &&
    (!keyframeSaved || !createError || createRetryable) &&
    validDirectionalWindow &&
    (!propagationRequested ||
      (model !== null &&
        segmentId !== undefined &&
        (!fallback || (allowFallback && (!model.textRequired || text.trim().length > 0))) &&
        window.fromFrame < window.toFrame));

  const submit = async () => {
    if (!canSubmit) return;
    await onSubmit({
      mode,
      direction: propagationRequested ? (mode as VideoTrackerDirection) : undefined,
      fromFrame: window.fromFrame,
      toFrame: window.toFrame,
      modelKey: propagationRequested ? model?.modelKey : undefined,
      modelId: propagationRequested ? model?.modelId : undefined,
      backendId: propagationRequested ? model?.backendId : undefined,
      allowBboxFallback: fallback && allowFallback,
      text: fallback ? text.trim() || undefined : undefined,
      segmentId: propagationRequested ? segmentId : undefined,
    });
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen && submitting) return;
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>保存 Mask 纠错帧</DialogTitle>
          <DialogDescription>
            当前帧 F{frameIndex} 会先保存为人工关键帧；重传播仅生成待审候选。
          </DialogDescription>
        </DialogHeader>

        <FieldGroup>
          <FieldSet>
            <FieldLegend variant="label">保存后动作</FieldLegend>
            <ToggleGroup
              type="single"
              variant="outline"
              value={mode}
              onValueChange={(value) => value && setMode(value as CorrectionMode)}
              className="flex w-full flex-wrap justify-start"
              aria-label="保存后动作"
            >
              <ToggleGroupItem value="save_only">仅保存</ToggleGroupItem>
              <ToggleGroupItem
                value="backward"
                disabled={!propagationAvailable || frameIndex <= minFrame}
              >
                ← 更早帧
              </ToggleGroupItem>
              <ToggleGroupItem
                value="forward"
                disabled={!propagationAvailable || frameIndex >= maxFrame}
              >
                更晚帧 →
              </ToggleGroupItem>
              <ToggleGroupItem
                value="bidirectional"
                disabled={!propagationAvailable || frameIndex <= minFrame || frameIndex >= maxFrame}
              >
                ⇆ 双向
              </ToggleGroupItem>
            </ToggleGroup>
          </FieldSet>

          <Alert>
            <InfoIcon />
            <AlertTitle>{modelLabel(model)}</AlertTitle>
            <AlertDescription>
              生效窗口 F{window.fromFrame}–F{window.toFrame}；F{frameIndex} 只作种子，
              不进入候选。其它人工关键帧默认受保护，覆盖时仍需二次确认。
            </AlertDescription>
          </Alert>

          {propagationRequested && !segmentId ? (
            <Alert variant="destructive">
              <InfoIcon />
              <AlertTitle>尚未取得当前视频分段</AlertTitle>
              <AlertDescription>可以先仅保存纠错帧；分段加载后再启动定向传播。</AlertDescription>
            </Alert>
          ) : null}

          {keyframeSaved ? (
            <Alert>
              <InfoIcon />
              <AlertTitle>人工纠错帧已保存</AlertTitle>
              <AlertDescription>
                再次提交只会重试创建传播作业，不会重复保存当前帧。
                {createError && (
                  <>
                    {" "}
                    上次失败：
                    <CopyableError message={createError} />
                  </>
                )}
                {createError && !createRetryable
                  ? " 当前快照已不能安全重试，请关闭后刷新标注。"
                  : ""}
              </AlertDescription>
            </Alert>
          ) : null}

          {fallback && propagationRequested && (
            <FieldSet>
              <FieldLegend variant="label">显式 bbox 降级</FieldLegend>
              <FieldDescription>
                当前模型不能消费 Mask seed。服务端会从已保存 RLE 计算 bbox，并在 lineage 记录
                mask_prompt_unsupported；结果仍要求输出 Mask。
              </FieldDescription>
              <FieldGroup>
                <Field data-invalid={model?.textRequired === true && text.trim().length === 0}>
                  <FieldLabel htmlFor="video-mask-correction-text">目标文本</FieldLabel>
                  <Input
                    id="video-mask-correction-text"
                    value={text}
                    onChange={(event) => setText(event.target.value)}
                    placeholder="例如：行人、车辆"
                    required={model?.textRequired === true}
                    aria-invalid={model?.textRequired === true && text.trim().length === 0}
                  />
                </Field>
                <Field orientation="horizontal" data-invalid={!allowFallback}>
                  <Checkbox
                    id="video-mask-correction-fallback"
                    checked={allowFallback}
                    onCheckedChange={(checked) => setAllowFallback(checked === true)}
                    aria-invalid={!allowFallback}
                  />
                  <FieldLabel htmlFor="video-mask-correction-fallback">
                    我确认使用 bbox seed 降级，不将其视为原生 Mask 纠错
                  </FieldLabel>
                </Field>
              </FieldGroup>
            </FieldSet>
          )}
        </FieldGroup>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            取消
          </Button>
          <Button onClick={() => void submit()} disabled={!canSubmit}>
            {submitting ? <Spinner aria-hidden="true" data-icon="inline-start" /> : null}
            {submitting
              ? keyframeSaved
                ? "启动中…"
                : "保存中…"
              : propagationRequested
                ? keyframeSaved
                  ? "重试启动传播"
                  : "保存并启动传播"
                : "仅保存"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
