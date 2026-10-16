import { useId, useState, type KeyboardEvent } from "react";
import { Button } from "@/components/ui/Button";
import type {
  TrainingDatasetDetectionStatistics,
  TrainingDatasetStatisticHeatmap,
} from "@/api/generated/types.gen";

function Heatmap({
  title,
  description,
  xLabel,
  yLabel,
  data,
  color,
}: {
  title: string;
  description: string;
  xLabel: string;
  yLabel: string;
  data: TrainingDatasetStatisticHeatmap;
  color: "text-chart-1" | "text-chart-3" | "text-chart-4";
}) {
  const id = useId();
  const [active, setActive] = useState<{ x: number; y: number } | null>(null);
  const [locked, setLocked] = useState(false);
  const [scale, setScale] = useState<"linear" | "sqrt">("linear");
  const maximum = Math.max(0, ...data.counts.flat());
  const unit = data.unit === "member_images" ? "张图片项" : "个实例";
  const width = data.x_bins.length;
  const height = data.y_bins.length;
  const selected = active ? data.counts[active.y]?.[active.x] : undefined;
  const inspect = (x: number, y: number) => {
    if (!locked) setActive({ x, y });
  };
  const keyboard = (event: KeyboardEvent<SVGSVGElement>) => {
    const point = active ?? { x: 0, y: 0 };
    if (event.key === "Escape") {
      setActive(null);
      setLocked(false);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setActive(point);
      setLocked((value) => !value);
      return;
    }
    const directions: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0],
      ArrowRight: [1, 0],
      ArrowUp: [0, -1],
      ArrowDown: [0, 1],
    };
    const direction = directions[event.key];
    if (!direction || !width || !height) return;
    event.preventDefault();
    setActive({
      x: Math.max(0, Math.min(width - 1, point.x + direction[0])),
      y: Math.max(0, Math.min(height - 1, point.y + direction[1])),
    });
    setLocked(true);
  };
  return (
    <figure className="min-w-0 rounded-xl border border-border bg-card p-4">
      <figcaption id={id} className="text-sm font-semibold">
        {title}
      </figcaption>
      <p className="mt-1 text-2xs leading-relaxed text-muted-foreground">{description}</p>
      <div role="group" aria-label={`${title}色阶`} className="mt-3 flex gap-1">
        {(
          [
            ["linear", "线性色阶"],
            ["sqrt", "增强稀疏区域"],
          ] as const
        ).map(([value, label]) => (
          <Button
            key={value}
            size="xs"
            variant="ghost"
            aria-pressed={scale === value}
            className={scale === value ? "bg-muted text-foreground" : "text-muted-foreground"}
            onClick={() => setScale(value)}
          >
            {label}
          </Button>
        ))}
      </div>
      <div className={`mx-auto mt-3 max-w-64 ${color}`}>
        <p className="mb-1 text-2xs text-muted-foreground">{yLabel} ↓</p>
        <svg
          viewBox={`0 0 ${width || 1} ${height || 1}`}
          role="grid"
          tabIndex={0}
          aria-labelledby={id}
          aria-describedby={`${id}-detail`}
          aria-rowcount={height}
          aria-colcount={width}
          aria-activedescendant={active ? `${id}-${active.x}-${active.y}` : undefined}
          className="aspect-square w-full rounded-md bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onKeyDown={keyboard}
          onFocus={() => {
            if (!active && width && height) setActive({ x: 0, y: 0 });
          }}
          onPointerLeave={() => {
            if (!locked) setActive(null);
          }}
        >
          {data.counts.map((row, y) => (
            <g key={y} role="row">
              {row.map((count, x) => {
                const intensity = maximum ? count / maximum : 0;
                const selectedCell = active?.x === x && active.y === y;
                return (
                  <rect
                    key={x}
                    id={`${id}-${x}-${y}`}
                    role="gridcell"
                    aria-label={`${xLabel} ${data.x_bins[x].label} · ${yLabel} ${data.y_bins[y].label}：${count} ${unit}`}
                    aria-selected={selectedCell}
                    x={x + 0.04}
                    y={y + 0.04}
                    width={0.92}
                    height={0.92}
                    rx={0.05}
                    fill={count ? "currentColor" : "var(--sc-muted)"}
                    fillOpacity={
                      count
                        ? 0.16 + 0.84 * (scale === "sqrt" ? Math.sqrt(intensity) : intensity)
                        : 1
                    }
                    stroke={selectedCell ? "var(--sc-foreground)" : undefined}
                    strokeWidth={0.1}
                    className="cursor-crosshair"
                    onPointerEnter={() => inspect(x, y)}
                    onClick={() => {
                      setActive({ x, y });
                      setLocked(true);
                    }}
                  >
                    <title>{`${xLabel} ${data.x_bins[x].label} · ${yLabel} ${data.y_bins[y].label}：${count} ${unit}`}</title>
                  </rect>
                );
              })}
            </g>
          ))}
        </svg>
        <p className="mt-1 text-right text-2xs text-muted-foreground">{xLabel} →</p>
        <div className="mt-2 flex items-center gap-2 text-2xs text-muted-foreground">
          <span>0</span>
          <svg
            viewBox="0 0 100 6"
            preserveAspectRatio="none"
            className={`h-1.5 min-w-0 flex-1 rounded-full ${color}`}
            aria-hidden="true"
          >
            <defs>
              <linearGradient id={`${id}-legend`}>
                <stop stopColor="currentColor" stopOpacity="0.16" />
                <stop offset="1" stopColor="currentColor" />
              </linearGradient>
            </defs>
            <rect width="100" height="6" fill={`url(#${id}-legend)`} />
          </svg>
          <span>
            {maximum} {unit}
          </span>
        </div>
      </div>
      <div id={`${id}-detail`} className="mt-3 min-h-20 rounded-lg bg-muted/50 p-3 text-xs">
        {active && selected !== undefined ? (
          <>
            <p className="flex justify-between gap-2 font-medium tabular-nums">
              <span>
                {selected} {unit}
              </span>
              <span className="text-muted-foreground">
                {data.population ? ((selected / data.population) * 100).toFixed(1) : "0.0"}%
              </span>
            </p>
            <p className="mt-1 text-2xs text-muted-foreground">
              {xLabel} {data.x_bins[active.x].label} · {yLabel} {data.y_bins[active.y].label}
            </p>
            {locked && (
              <button
                className="mt-1 text-2xs text-chart-1 underline underline-offset-2 focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => {
                  setLocked(false);
                  setActive(null);
                }}
              >
                取消固定
              </button>
            )}
          </>
        ) : (
          <p className="text-2xs leading-relaxed text-muted-foreground">
            悬停查看计数，点击或触控固定格子。键盘方向键移动，Enter 固定，Esc 清除。
          </p>
        )}
      </div>
      <p className="mt-3 text-2xs tabular-nums text-muted-foreground">
        已统计 {data.population} {unit} · {data.unknown_members} 张成员证据未知
      </p>
      <details className="mt-3 text-2xs text-muted-foreground">
        <summary className="cursor-pointer">查看坐标区间与计数表</summary>
        <div className="mt-2 max-h-64 overflow-auto">
          <table className="w-full text-right tabular-nums">
            <caption className="sr-only">
              {title}计数表；行是{yLabel}，列是{xLabel}
            </caption>
            <thead>
              <tr>
                <th scope="col" className="whitespace-nowrap p-1 text-left">
                  {yLabel} / {xLabel}
                </th>
                {data.x_bins.map((bin) => (
                  <th scope="col" className="whitespace-nowrap p-1 font-normal" key={bin.key}>
                    {bin.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.y_bins.map((bin, y) => (
                <tr key={bin.key} className="border-t border-border">
                  <th scope="row" className="whitespace-nowrap p-1 text-left font-normal">
                    {bin.label}
                  </th>
                  {data.counts[y].map((count, x) => (
                    <td key={x} className="p-1">
                      {count}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

export function TrainingDatasetDetectionCharts({
  data,
}: {
  data: TrainingDatasetDetectionStatistics;
}) {
  const density = data.objects_per_member;
  const maximum = Math.max(1, ...density.bins.map((bin) => bin.count));
  const [selected, setSelected] = useState<string | null>(null);
  const bin = density.bins.find((item) => item.key === selected);
  return (
    <section aria-label="检测几何与目标密度" className="space-y-4">
      <div>
        <h3 className="text-sm font-semibold">检测几何与目标密度</h3>
        <p className="mt-1 text-2xs leading-relaxed text-muted-foreground">
          检查目标在画面中的位置、大小与聚集情况。各图来自冻结成员，包含重复图片项；未知标注不计为零目标。
        </p>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        <Heatmap
          title="图片宽高分布"
          description="6 × 6 像素区间；按图片项计数。"
          xLabel="宽 / px"
          yLabel="高 / px"
          data={data.image_dimensions}
          color="text-chart-1"
        />
        <Heatmap
          title="标注中心位置"
          description="20 × 20 归一化区间；左上 (0, 0)，右下 (1, 1)。"
          xLabel="中心 X"
          yLabel="中心 Y"
          data={data.box_centers}
          color="text-chart-4"
        />
        <Heatmap
          title="标注宽高分布"
          description="20 × 20 归一化区间；框占图片宽高的比例。"
          xLabel="框宽"
          yLabel="框高"
          data={data.box_dimensions}
          color="text-chart-3"
        />
      </div>
      <figure aria-label="每张图片目标数量" className="rounded-xl border border-border bg-card p-4">
        <figcaption className="text-sm font-semibold">每张图片目标数量</figcaption>
        <p className="mt-1 text-2xs text-muted-foreground">
          已知标注 {density.analyzed_members} 张；另有 {density.unknown_label_members}{" "}
          张未知，未放入零目标分组。
        </p>
        <ul className="mt-4 grid grid-cols-3 gap-3 sm:grid-cols-6">
          {density.bins.map((item) => (
            <li key={item.key}>
              <button
                aria-label={`${item.label} 个目标：${item.count} 张图片`}
                aria-pressed={selected === item.key}
                className="group w-full rounded-lg p-2 text-center text-xs transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onPointerEnter={() => setSelected(item.key)}
                onFocus={() => setSelected(item.key)}
                onClick={() => setSelected(item.key)}
              >
                <span
                  className="flex h-20 items-end justify-center rounded-md bg-muted/50"
                  aria-hidden="true"
                >
                  <svg
                    viewBox="0 0 10 100"
                    preserveAspectRatio="none"
                    className="h-full w-1/2 text-chart-4"
                  >
                    <rect
                      x="0"
                      y={100 - (item.count / maximum) * 100}
                      width="10"
                      height={(item.count / maximum) * 100}
                      rx="1"
                      fill="currentColor"
                      fillOpacity={selected === item.key ? 1 : 0.65}
                    />
                  </svg>
                </span>
                <span className="mt-2 block font-semibold tabular-nums">{item.count} 张</span>
                <span className="mt-1 block text-2xs text-muted-foreground">
                  {item.label} 个目标
                </span>
              </button>
            </li>
          ))}
        </ul>
        <p
          role="status"
          className="mt-3 rounded-md bg-muted/40 px-3 py-2 text-2xs text-muted-foreground"
        >
          {bin
            ? `${bin.label} 个目标的图片：${bin.count} 张，占已知标注图片 ${density.analyzed_members ? ((bin.count / density.analyzed_members) * 100).toFixed(1) : "0.0"}%。`
            : "悬停或点击柱子查看该组数量与占比。"}{" "}
          这里检查分布，不改变样本筛选。
        </p>
      </figure>
    </section>
  );
}
