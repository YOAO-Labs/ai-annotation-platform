import { useEffect, useRef } from "react";
import type { CSSProperties } from "react";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { TRACK_COLOR_PALETTE } from "./colors";

interface VideoTrackColorPickerProps {
  currentColor: string;
  hasOverride: boolean;
  onPick: (color: string) => void;
  onReset: () => void;
  onClose: () => void;
}

function SwatchButton({
  color,
  label,
  active,
  onPick,
}: {
  color: string;
  label: string;
  active: boolean;
  onPick: (color: string) => void;
}) {
  const ref = useElementStyle<HTMLButtonElement>({ backgroundColor: color } as CSSProperties);
  return (
    <button
      ref={ref}
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={active}
      className={`w-[18px] h-[18px] p-0 border rounded cursor-pointer hover:border-foreground/20 ${active ? "border-brand shadow-[0_0_0_1px_var(--sc-brand)]" : "border-border"}`}
      onClick={(e) => {
        e.stopPropagation();
        onPick(color);
      }}
    />
  );
}

export function VideoTrackColorPicker({
  currentColor,
  hasOverride,
  onPick,
  onReset,
  onClose,
}: VideoTrackColorPickerProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown, true);
    };
  }, [onClose]);

  return (
    <div
      ref={rootRef}
      className="flex flex-col gap-1.5 p-1.5 bg-card border border-border rounded-md shadow-lg"
      role="dialog"
      aria-label="选择轨迹颜色"
    >
      <div className="grid grid-cols-4 gap-1">
        {TRACK_COLOR_PALETTE.map((entry) => (
          <SwatchButton
            key={entry.value}
            color={entry.value}
            label={entry.label}
            active={entry.value === currentColor}
            onPick={onPick}
          />
        ))}
      </div>
      {hasOverride ? (
        <button
          type="button"
          className="py-0.5 px-1.5 text-xs text-muted-foreground bg-transparent border border-border rounded cursor-pointer hover:text-foreground hover:bg-muted"
          onClick={(e) => {
            e.stopPropagation();
            onReset();
          }}
        >
          恢复默认
        </button>
      ) : null}
    </div>
  );
}
