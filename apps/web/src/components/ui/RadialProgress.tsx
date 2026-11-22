import { cn } from "@/lib/utils";

interface RadialProgressProps {
  value: number;
  max?: number;
  size?: number;
  thickness?: number;
  color?: string;
  trackColor?: string;
  label?: string;
}

export function RadialProgress({
  value,
  max = 100,
  size = 56,
  thickness = 6,
  color = "var(--sc-brand)",
  trackColor = "var(--sc-border)",
  label,
}: RadialProgressProps) {
  const r = (size - thickness) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const c = 2 * Math.PI * r;
  const pct = Math.min(1, Math.max(0, value / max));
  const dashLen = c * pct;
  return (
    <div className="relative inline-block leading-[0]">
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        <circle cx={cx} cy={cy} r={r} fill="none" stroke={trackColor} strokeWidth={thickness} />
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={thickness}
          strokeLinecap="round"
          strokeDasharray={`${dashLen} ${c - dashLen}`}
          transform={`rotate(-90 ${cx} ${cy})`}
        />
      </svg>
      <div
        className={cn(
          "absolute inset-0 flex flex-col items-center justify-center font-semibold tabular-nums",
          size > 80 ? "text-base" : "text-xs",
        )}
      >
        <span>{Math.round(value)}</span>
        {label && (
          <span className="mt-px text-micro font-normal text-muted-foreground">{label}</span>
        )}
      </div>
    </div>
  );
}
