/**
 * Histogram —— 直方图，由 flex 容器和命令式动态高度、颜色组成。
 * 柱色走命令式 style.background;默认使用 --sc-brand。
 */
interface HistogramProps {
  values: number[];
  height?: number;
  color?: string;
  /** 可选:在某 index 处渲染竖向标注线(如 p50 / p95) */
  markers?: Array<{ index: number; label: string }>;
  /** 横轴标签(可选,长度需与 values 一致) */
  xLabels?: string[];
}

export function Histogram({
  values,
  height = 80,
  color = "var(--sc-brand)",
  markers = [],
  xLabels,
}: HistogramProps) {
  const peak = Math.max(1, ...values);
  return (
    <div className="min-w-0">
      <div
        ref={(element) => {
          if (!element) return;
          element.style.height = `${height}px`;
        }}
        className="relative flex items-end gap-0.5"
      >
        {values.map((v, i) => {
          const h = Math.max(1, (v / peak) * height);
          return (
            <div
              key={i}
              title={xLabels?.[i] ? `${xLabels[i]}: ${v}` : String(v)}
              ref={(element) => {
                if (!element) return;
                element.style.height = `${h}px`;
                element.style.background = color;
                // borderRadius 命令式写在元素上(而非 Tailwind class):Histogram 单测靠
                // style.borderRadius 识别 bar(区分 marker 竖线),保持该选择器可用。
                element.style.borderRadius = "2px 2px 0 0";
              }}
              className="min-h-px flex-1"
            />
          );
        })}
        {markers.map((m) => {
          const left = `${(m.index / Math.max(1, values.length - 1)) * 100}%`;
          return (
            <div
              key={m.label}
              ref={(element) => {
                if (!element) return;
                element.style.left = left;
              }}
              className="pointer-events-none absolute inset-y-0 w-px bg-muted-foreground"
            >
              <span className="absolute -top-3.5 left-1 whitespace-nowrap text-2xs text-muted-foreground">
                {m.label}
              </span>
            </div>
          );
        })}
      </div>
      {xLabels && (
        <div className="mt-1 flex justify-between text-2xs text-muted-foreground">
          <span>{xLabels[0]}</span>
          <span>{xLabels[xLabels.length - 1]}</span>
        </div>
      )}
    </div>
  );
}
