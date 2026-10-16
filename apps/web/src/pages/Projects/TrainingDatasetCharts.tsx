import { useId, useState } from "react";
import type {
  TrainingDatasetClassPage,
  TrainingDatasetInsightOverview,
} from "@/api/trainingDatasets";
import { Button } from "@/components/ui/Button";

type Split = "train" | "val";

export function TrainingDatasetSplitChart({
  splits,
  totalMembers,
  unknownMembers = 0,
  onSelect,
}: {
  splits: TrainingDatasetInsightOverview["splits"];
  totalMembers?: number;
  unknownMembers?: number;
  onSelect: (split: Split) => void;
}) {
  const known = splits.train.images + splits.val.images;
  const unknown = Math.max(unknownMembers, (totalMembers ?? known) - known);
  const total = known + unknown;
  const [active, setActive] = useState<Split | null>(null);
  const circumference = 2 * Math.PI * 42;
  const trainLength = total ? (splits.train.images / total) * circumference : 0;
  const valLength = total ? (splits.val.images / total) * circumference : 0;
  return (
    <figure
      aria-label="训练与验证图片划分图"
      className="rounded-xl border border-border bg-card p-4"
    >
      <figcaption>
        <h3 className="text-sm font-semibold">训练 / 验证划分</h3>
        <p className="mt-1 text-2xs text-muted-foreground">按训练包图片项计数 · 点击分区查看样本</p>
      </figcaption>
      {total > 0 ? (
        <div className="mt-5 flex flex-wrap items-center justify-center gap-4">
          <svg
            viewBox="0 0 112 112"
            className="size-32 shrink-0"
            role="img"
            aria-label={`共 ${total} 张图片，训练 ${splits.train.images} 张，验证 ${splits.val.images} 张，未知 ${unknown} 张`}
          >
            <circle cx="56" cy="56" r="42" fill="none" stroke="var(--sc-muted)" strokeWidth="12" />
            <g transform="rotate(-90 56 56)">
              <circle
                cx="56"
                cy="56"
                r="42"
                fill="none"
                stroke="var(--sc-chart-1)"
                strokeWidth={active === "train" ? 15 : 12}
                strokeDasharray={`${trainLength} ${circumference}`}
              />
              <circle
                cx="56"
                cy="56"
                r="42"
                fill="none"
                stroke="var(--sc-chart-2)"
                strokeWidth={active === "val" ? 15 : 12}
                strokeDasharray={`${valLength} ${circumference}`}
                strokeDashoffset={-trainLength}
              />
            </g>
            <text
              x="56"
              y="54"
              textAnchor="middle"
              fill="var(--sc-foreground)"
              className="text-xl font-semibold tabular-nums"
            >
              {active ? splits[active].images : total}
            </text>
            <text
              x="56"
              y="70"
              textAnchor="middle"
              fill="var(--sc-muted-foreground)"
              className="text-micro"
            >
              {active === "train" ? "训练图片" : active === "val" ? "验证图片" : "冻结图片"}
            </text>
          </svg>
          <div className="min-w-0 flex-1 space-y-2">
            {(["train", "val"] as const).map((split) => (
              <button
                key={split}
                aria-label={`查看${split === "train" ? "训练" : "验证"}集 ${splits[split].images} 张图片`}
                className="group flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-default"
                disabled={!splits[split].images}
                onPointerEnter={() => setActive(split)}
                onPointerLeave={() => setActive(null)}
                onFocus={() => setActive(split)}
                onBlur={() => setActive(null)}
                onClick={() => onSelect(split)}
              >
                <span
                  aria-hidden="true"
                  className={`mt-1 size-2 shrink-0 rounded-full ${split === "train" ? "bg-chart-1" : "bg-chart-2"}`}
                />
                <span className="min-w-0 flex-1 text-xs">
                  <span className="font-medium">{split === "train" ? "训练集" : "验证集"}</span>
                  <span className="mt-1 block tabular-nums">
                    {splits[split].images} 张 ·{" "}
                    {total ? ((splits[split].images / total) * 100).toFixed(1) : "0.0"}%
                  </span>
                  <span className="mt-1 block text-2xs text-muted-foreground">
                    {splits[split].instances ?? "未知"} 个实例
                  </span>
                </span>
                <span
                  aria-hidden="true"
                  className="text-muted-foreground transition-transform group-hover:translate-x-0.5"
                >
                  →
                </span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <p className="my-6 text-xs text-muted-foreground">没有入包图片。</p>
      )}
      {unknown > 0 && (
        <p className="mt-3 rounded-md bg-muted p-2 text-2xs text-muted-foreground">
          划分未知 {unknown} 张 · {((unknown / total) * 100).toFixed(1)}%；比例以全部 {total}{" "}
          张入包图片项为分母。
        </p>
      )}
    </figure>
  );
}

export function TrainingDatasetClassChart({
  rows,
  partial,
  onSelect,
}: {
  rows: TrainingDatasetClassPage["items"];
  partial: boolean;
  onSelect: (index: number, split?: Split) => void;
}) {
  const id = useId();
  const [unit, setUnit] = useState<"images" | "instances">("instances");
  const [split, setSplit] = useState<"all" | Split>("all");
  const [active, setActive] = useState<number | null>(null);
  const count = (row: TrainingDatasetClassPage["items"][number]) =>
    (split !== "val" ? row[`train_${unit}`] : 0) + (split !== "train" ? row[`val_${unit}`] : 0);
  const shown = [...rows].sort((a, b) => count(b) - count(a) || a.index - b.index).slice(0, 10);
  const maximum = Math.max(1, ...shown.map(count));
  const hovered = shown.find((row) => row.index === active);
  const label = unit === "images" ? "张图片" : "个实例";
  return (
    <figure aria-label="冻结类别数量图" className="rounded-lg bg-muted/30 p-3">
      <figcaption className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-2xs text-muted-foreground">当前页前 10 类 · 按数量排序</p>
        <div className="flex flex-wrap gap-2">
          <div
            role="group"
            aria-label="类别图计数单位"
            className="flex rounded-md border border-border p-0.5"
          >
            {(
              [
                ["instances", "实例"],
                ["images", "图片"],
              ] as const
            ).map(([value, name]) => (
              <Button
                key={value}
                size="xs"
                variant="ghost"
                className={
                  unit === value
                    ? "bg-chart-1/10 text-chart-1 hover:bg-chart-1/15"
                    : "text-muted-foreground"
                }
                aria-pressed={unit === value}
                onClick={() => setUnit(value)}
              >
                {name}
              </Button>
            ))}
          </div>
          <label className="sr-only" htmlFor={`${id}-split`}>
            类别图划分
          </label>
          <select
            id={`${id}-split`}
            value={split}
            className="rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onChange={(event) => setSplit(event.target.value as typeof split)}
          >
            <option value="all">全部划分</option>
            <option value="train">训练集</option>
            <option value="val">验证集</option>
          </select>
        </div>
      </figcaption>
      <div className="mt-3 flex gap-3 text-2xs text-muted-foreground">
        <span className="flex items-center gap-1">
          <i className="size-2 rounded-sm bg-chart-1" />
          训练
        </span>
        <span className="flex items-center gap-1">
          <i className="size-2 rounded-sm bg-chart-2" />
          验证
        </span>
      </div>
      <ul className="mt-3 space-y-2">
        {shown.map((row) => (
          <li key={row.index}>
            <button
              className="grid w-full grid-cols-[minmax(3rem,1fr)_minmax(0,3fr)_auto] items-center gap-3 rounded-md py-1.5 text-left text-xs transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`按类别图查看 ${row.name} ${count(row)} ${label}`}
              onPointerEnter={() => setActive(row.index)}
              onPointerLeave={() => setActive(null)}
              onFocus={() => setActive(row.index)}
              onBlur={() => setActive(null)}
              onClick={() => onSelect(row.index, split === "all" ? undefined : split)}
            >
              <span className="truncate font-medium" title={row.name}>
                {row.name}
              </span>
              <span className="flex h-3 overflow-hidden rounded-sm bg-muted" aria-hidden="true">
                {split !== "val" && (
                  <span
                    className="h-full bg-chart-1 transition-[width]"
                    ref={(e) => {
                      if (e) e.style.width = `${(row[`train_${unit}`] / maximum) * 100}%`;
                    }}
                  />
                )}
                {split !== "train" && (
                  <span
                    className="h-full bg-chart-2 transition-[width]"
                    ref={(e) => {
                      if (e) e.style.width = `${(row[`val_${unit}`] / maximum) * 100}%`;
                    }}
                  />
                )}
              </span>
              <span className="tabular-nums">{count(row)}</span>
            </button>
          </li>
        ))}
      </ul>
      {!shown.length && (
        <p className="mt-4 text-xs text-muted-foreground">没有可展示的冻结类别。</p>
      )}
      <p
        role="status"
        className="mt-3 min-h-8 border-t border-border pt-2 text-2xs leading-relaxed text-muted-foreground"
      >
        {hovered
          ? `${hovered.name}：训练 ${hovered[`train_${unit}`]} ${label} · 验证 ${hovered[`val_${unit}`]} ${label}。点击查看对应冻结图片。`
          : "悬停或聚焦查看分区数量，点击类别柱查看冻结样本；完整类别可继续翻页。"}
        {partial && "成员证据或划分不完整，仅展示已知数量。"}
      </p>
    </figure>
  );
}
