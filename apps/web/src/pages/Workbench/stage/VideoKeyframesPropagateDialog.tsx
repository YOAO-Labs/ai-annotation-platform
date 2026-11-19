import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import type { VideoPropagateDirection } from "../state/videoTrackCommands";
import { readDialogMemory, writeDialogMemory } from "../state/videoDialogMemory";

const COUNT_PRESETS = [1, 5, 10, 30] as const;

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function validateKeyframesMemory(value: unknown): VideoKeyframesPropagateSubmit | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Partial<VideoKeyframesPropagateSubmit>;
  const direction =
    raw.direction === "forward" || raw.direction === "backward" ? raw.direction : null;
  const count =
    typeof raw.count === "number" && Number.isFinite(raw.count) && raw.count > 0
      ? Math.floor(raw.count)
      : null;
  if (!direction || !count || typeof raw.overwrite !== "boolean") return null;
  return { direction, count, overwrite: raw.overwrite };
}

export interface VideoKeyframesPropagateSubmit {
  direction: VideoPropagateDirection;
  count: number;
  overwrite: boolean;
}

interface VideoKeyframesPropagateDialogProps {
  open: boolean;
  frameIndex: number;
  userId?: string | null;
  /** 采样网格步长 (源帧). >1 时 count 以网格格子为单位; 缺省 1 = 现状 (按源帧). */
  samplingStep?: number;
  overwriteOverride?: boolean | null;
  onCancel: () => void;
  onSubmit: (payload: VideoKeyframesPropagateSubmit) => void;
}

/**
 * 关键帧 Propagate 对话框 (纯前端, 区别于 AI 版 VideoTrackerPropagateDialog)。
 * 把当前帧的框复制到后续 / 向前 N 帧, overwrite 控制是否覆盖目标帧已有关键帧。
 *
 * §A: 采样开启 (samplingStep>1) 时, count 以网格格子为单位, 与 ←/→ 网格导航统一;
 * 提交时换算回源帧 count (count * samplingStep), 底层 D2 (propagateKeyframes) 仍逐源帧不变。
 */
export function VideoKeyframesPropagateDialog({
  open,
  frameIndex,
  userId,
  samplingStep = 1,
  overwriteOverride = null,
  onCancel,
  onSubmit,
}: VideoKeyframesPropagateDialogProps) {
  const [direction, setDirection] = useState<VideoPropagateDirection>("forward");
  const [count, setCount] = useState<number>(10);
  const [overwrite, setOverwrite] = useState<boolean>(false);

  useEffect(() => {
    if (open) {
      const remembered = readDialogMemory(userId, "kfPropagate", validateKeyframesMemory);
      setDirection(remembered?.direction ?? "forward");
      setCount(remembered?.count ?? 10);
      setOverwrite(overwriteOverride ?? remembered?.overwrite ?? false);
    }
  }, [open, overwriteOverride, userId]);

  if (!open) return null;

  const grid = Math.max(1, Math.round(samplingStep));
  const dir = direction === "backward" ? -1 : 1;
  // count 是网格格子数; 采样开启时换算成源帧跨度。
  const sourceCount = count * grid;
  const target = Math.max(0, frameIndex + dir * sourceCount);
  const overwriteLocked = overwriteOverride !== null && overwriteOverride !== undefined;
  const effectiveOverwrite = overwriteLocked ? overwriteOverride === true : overwrite;

  const handleSubmit = () => {
    if (count <= 0) return;
    onSubmit({ direction, count: sourceCount, overwrite: effectiveOverwrite });
    if (!overwriteLocked) {
      writeDialogMemory(userId, "kfPropagate", { direction, count, overwrite });
    }
  };

  return (
    <div
      role="dialog"
      aria-label="复制到后续帧"
      data-testid="video-keyframes-propagate-dialog"
      className="fixed inset-0 z-workbench-modal grid place-items-center bg-black/40"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="grid gap-3 w-[340px] p-4 border border-border rounded-[10px] bg-card shadow-lg">
        <div className="flex items-center justify-between">
          <b className="text-sm">复制框到后续帧</b>
          <button
            type="button"
            onClick={onCancel}
            className="border-0 bg-transparent text-muted-foreground cursor-pointer text-sm"
          >
            ✕
          </button>
        </div>

        <label className="grid gap-1 text-muted-foreground text-xs">
          方向
          <div className="grid grid-flow-col auto-cols-fr gap-1">
            {(["forward", "backward"] as VideoPropagateDirection[]).map((d) => (
              <button
                key={d}
                type="button"
                data-testid={`keyframes-direction-${d}`}
                title={
                  d === "forward"
                    ? "向后: 复制框到更晚 (时间轴更右) 的帧"
                    : "向前: 复制框到更早 (时间轴更左) 的帧"
                }
                onClick={() => setDirection(d)}
                className={cn(
                  "py-1.5 border rounded-md bg-background text-foreground cursor-pointer text-xs whitespace-nowrap",
                  direction === d ? "border-brand bg-brand/10" : "border-border",
                )}
              >
                {/* 消歧, 与 AI 追踪对话框方向标签统一 */}
                {d === "forward" ? "更晚帧 →" : "← 更早帧"}
              </button>
            ))}
          </div>
        </label>

        <label className="grid gap-1 text-muted-foreground text-xs">
          {grid > 1 ? "格数" : "帧数"}
          <div className="grid grid-flow-col auto-cols-fr gap-1">
            {COUNT_PRESETS.map((preset) => (
              <button
                key={preset}
                type="button"
                onClick={() => setCount(preset)}
                className={cn(
                  "py-1.5 border rounded-md bg-background text-foreground cursor-pointer text-xs",
                  count === preset ? "border-brand bg-brand/10" : "border-border",
                )}
              >
                {preset}
              </button>
            ))}
          </div>
          <input
            type="number"
            min={1}
            value={count}
            onChange={(e) => setCount(Math.max(1, Math.floor(Number(e.target.value) || 0)))}
            className="py-1.5 px-2 border border-border rounded-md bg-background text-foreground text-sm"
          />
          <span className={cn("mono", "text-muted-foreground text-xs")}>
            {grid > 1 ? (
              <>
                G{Math.round(frameIndex / grid)} → G{Math.round(target / grid)} (F
                {frameIndex} → F{target})
              </>
            ) : (
              <>
                F{frameIndex} → F{target}
              </>
            )}
          </span>
        </label>

        <label className="flex items-center gap-2 text-foreground text-xs cursor-pointer">
          <input
            type="checkbox"
            checked={effectiveOverwrite}
            disabled={overwriteLocked}
            onChange={(e) => setOverwrite(e.target.checked)}
          />
          {overwriteLocked ? "覆盖目标帧已有关键帧（项目锁定）" : "覆盖目标帧已有关键帧"}
        </label>

        <div className="flex gap-2 justify-end">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            取消
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={count <= 0}>
            复制
          </Button>
        </div>
      </div>
    </div>
  );
}
