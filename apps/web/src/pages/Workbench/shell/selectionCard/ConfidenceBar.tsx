import { type CSSProperties } from "react";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { confidenceTone } from "./IdentityHeader";

const TONE_TRACK_CLASS: Record<string, string> = {
  high: "bg-emerald-500",
  mid: "bg-amber-500",
  low: "bg-brand",
};

const TONE_VALUE_CLASS: Record<string, string> = {
  high: "text-status-positive",
  mid: "text-status-caution",
  low: "text-brand",
};

export interface ConfidenceBarProps {
  value: number;
}

export function ConfidenceBar({ value }: ConfidenceBarProps) {
  const pct = Math.round(value * 100);
  const tone = confidenceTone(value);
  const fillRef = useElementStyle<HTMLDivElement>({ "--cf-fill": `${pct}%` } as CSSProperties);

  return (
    <div className="flex items-center gap-2">
      <span className="flex-none text-xs text-muted-foreground">置信度</span>
      <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
        <div
          ref={fillRef}
          className={`h-full w-[var(--cf-fill)] rounded-full transition-[width] duration-[250ms] ease-in-out motion-reduce:transition-none ${TONE_TRACK_CLASS[tone]}`}
        />
      </div>
      <span className={`flex-none text-xs font-semibold tabular-nums ${TONE_VALUE_CLASS[tone]}`}>
        {pct}%
      </span>
    </div>
  );
}
