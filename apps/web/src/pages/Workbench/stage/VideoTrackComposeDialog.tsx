import { useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";

export type VideoTrackGapMode = "interpolate" | "outside";

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

const GAP_MODE_OPTIONS: { value: VideoTrackGapMode; label: string; hint: string }[] = [
  {
    value: "interpolate",
    label: "插值过渡",
    hint: "两段之间靠现有线性插值平滑过渡（gap 区视为可见）。",
  },
  {
    value: "outside",
    label: "标记消失",
    hint: "把两段之间的 gap 区标为消失（outside）后再合并。",
  },
];

interface VideoTrackComposeDialogProps {
  open: boolean;
  onCancel: () => void;
  onSubmit: (gapMode: VideoTrackGapMode) => void;
}

/**
 * Track Join 对话框：选中两条同类且帧号不重叠的轨迹跳连前，
 * 选择 gap 区填充模式（interpolate / outside）。merge / split 走各自既有路径，
 * 本对话框只覆盖 join 的 gap_mode 选择。
 */
export function VideoTrackComposeDialog({
  open,
  onCancel,
  onSubmit,
}: VideoTrackComposeDialogProps) {
  const [gapMode, setGapMode] = useState<VideoTrackGapMode>("interpolate");

  useEffect(() => {
    if (open) setGapMode("interpolate");
  }, [open]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-label="跳连轨迹"
      data-testid="video-track-compose-dialog"
      className="fixed inset-0 z-workbench-modal grid place-items-center bg-black/40"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel();
      }}
    >
      <div className="grid gap-3 w-[360px] p-4 border border-border rounded-[10px] bg-card shadow-lg">
        <div className="flex items-center justify-between">
          <b className="text-sm">跳连两条轨迹</b>
          <button
            type="button"
            onClick={onCancel}
            className="border-0 bg-transparent text-muted-foreground cursor-pointer text-sm"
          >
            ✕
          </button>
        </div>

        <fieldset className="m-0 p-0 border-0">
          <legend className="p-0 mb-2 text-muted-foreground text-xs">gap 填充模式</legend>
          <div className="grid gap-2">
            {GAP_MODE_OPTIONS.map((option) => (
              <label
                key={option.value}
                className={cn(
                  "flex items-start gap-2 py-2 px-2.5 border rounded-lg bg-background cursor-pointer",
                  gapMode === option.value
                    ? "border-emerald-600 dark:border-emerald-400 bg-status-positive-soft"
                    : "border-border",
                )}
              >
                <input
                  type="radio"
                  name="video-track-gap-mode"
                  value={option.value}
                  checked={gapMode === option.value}
                  onChange={() => setGapMode(option.value)}
                />
                <span className="grid gap-0.5">
                  <b className="text-foreground text-sm">{option.label}</b>
                  <span className="text-muted-foreground text-xs leading-[1.4]">{option.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="flex gap-2 justify-end">
          <Button variant="ghost" size="sm" onClick={onCancel}>
            取消
          </Button>
          <Button size="sm" onClick={() => onSubmit(gapMode)}>
            跳连
          </Button>
        </div>
      </div>
    </div>
  );
}
