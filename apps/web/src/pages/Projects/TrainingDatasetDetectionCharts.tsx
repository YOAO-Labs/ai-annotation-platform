import { useId } from "react";
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
}: {
  title: string;
  description: string;
  xLabel: string;
  yLabel: string;
  data: TrainingDatasetStatisticHeatmap;
}) {
  const id = useId();
  const maximum = Math.max(0, ...data.counts.flat());
  const unit = data.unit === "member_images" ? "张图片项" : "个实例";
  const width = data.x_bins.length;
  const height = data.y_bins.length;
  return (
    <figure className="min-w-0 rounded-lg border border-border p-4">
      <figcaption id={id} className="text-xs font-semibold">
        {title}
      </figcaption>
      <p className="mt-1 text-2xs leading-relaxed text-muted-foreground">{description}</p>
      <div className="mx-auto mt-4 max-w-72">
        <p className="mb-1 text-2xs text-muted-foreground">{yLabel} ↓</p>
        <svg
          viewBox={`0 0 ${width} ${height}`}
          role="img"
          aria-labelledby={id}
          className="aspect-square w-full rounded-sm bg-muted text-primary"
        >
          {data.counts.flatMap((row, y) =>
            row.map((count, x) => (
              <rect
                key={`${x}:${y}`}
                x={x + 0.04}
                y={y + 0.04}
                width={0.92}
                height={0.92}
                fill="currentColor"
                fillOpacity={count && maximum ? 0.12 + (0.88 * count) / maximum : 0}
              >
                <title>{`${xLabel} ${data.x_bins[x].label} · ${yLabel} ${data.y_bins[y].label}：${count} ${unit}`}</title>
              </rect>
            )),
          )}
        </svg>
        <p className="mt-1 text-right text-2xs text-muted-foreground">{xLabel} →</p>
        <p className="mt-2 flex justify-between text-2xs text-muted-foreground">
          <span>空白 0</span>
          <span>
            最深 {maximum} {unit}
          </span>
        </p>
      </div>
      <p className="mt-3 text-xs tabular-nums">
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
  return (
    <section aria-label="检测几何与目标密度" className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">检测几何与目标密度</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          来自服务端冻结成员统计，包含重复图片项。横纵坐标均从小到大递增；未知标注不计为零目标。
        </p>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        <Heatmap
          title="图片宽高分布"
          description="6 × 6 像素区间；按图片项计数。"
          xLabel="宽 / px"
          yLabel="高 / px"
          data={data.image_dimensions}
        />
        <Heatmap
          title="标注中心位置"
          description="20 × 20 归一化区间；左上为 (0, 0)，右下为 (1, 1)。"
          xLabel="中心 X"
          yLabel="中心 Y"
          data={data.box_centers}
        />
        <Heatmap
          title="标注宽高分布"
          description="20 × 20 归一化区间；表示框占图片宽高的比例。"
          xLabel="框宽"
          yLabel="框高"
          data={data.box_dimensions}
        />
      </div>
      <figure aria-label="每张图片目标数量" className="rounded-lg border border-border p-4">
        <figcaption className="text-xs font-semibold">每张图片目标数量</figcaption>
        <p className="mt-1 text-2xs text-muted-foreground">
          已知标注 {density.analyzed_members} 张；另有 {density.unknown_label_members}{" "}
          张未知，未放入零目标分组。
        </p>
        <ul className="mt-4 grid grid-cols-3 gap-4 sm:grid-cols-6">
          {density.bins.map((bin) => (
            <li key={bin.key} className="text-center text-xs">
              <div
                className="flex h-20 items-end justify-center rounded-sm bg-muted"
                aria-hidden="true"
              >
                <svg
                  viewBox="0 0 10 100"
                  preserveAspectRatio="none"
                  className="h-full w-1/2 text-primary"
                >
                  <rect
                    x="0"
                    y={100 - (bin.count / maximum) * 100}
                    width="10"
                    height={(bin.count / maximum) * 100}
                    fill="currentColor"
                  />
                </svg>
              </div>
              <p className="mt-2 font-medium tabular-nums">{bin.count} 张</p>
              <p className="mt-1 text-2xs text-muted-foreground">{bin.label} 个目标</p>
            </li>
          ))}
        </ul>
      </figure>
    </section>
  );
}
