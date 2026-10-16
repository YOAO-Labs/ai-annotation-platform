import { useState } from "react";
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
  return (
    <figure aria-label="训练与验证图片划分图" className="rounded-lg border border-border p-4">
      <figcaption className="flex flex-wrap items-center justify-between gap-2 text-xs">
        <span className="font-semibold">训练 / 验证划分</span>
        <span className="text-muted-foreground">按训练包图片项计数 · 共 {total} 张</span>
      </figcaption>
      {total > 0 ? (
        <div className="my-4 flex h-5 overflow-hidden rounded-md bg-muted">
          {(["train", "val"] as const).map((split) => (
            <button
              key={split}
              aria-label={`查看${split === "train" ? "训练" : "验证"}集 ${splits[split].images} 张图片`}
              className={
                split === "train"
                  ? "min-w-0 bg-primary transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                  : "min-w-0 bg-primary/35 transition-opacity hover:opacity-80 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
              }
              ref={(element) => {
                if (element) element.style.width = `${(splits[split].images / total) * 100}%`;
              }}
              disabled={!splits[split].images}
              onClick={() => onSelect(split)}
            />
          ))}
          {unknown > 0 && (
            <span
              className="min-w-0 bg-muted-foreground/25"
              aria-label={`划分未知 ${unknown} 张图片`}
              ref={(element) => {
                if (element) element.style.width = `${(unknown / total) * 100}%`;
              }}
            />
          )}
        </div>
      ) : (
        <p className="my-4 text-xs text-muted-foreground">没有入包图片。</p>
      )}
      <dl className="grid grid-cols-2 gap-4 text-xs">
        {(["train", "val"] as const).map((split) => (
          <div key={split}>
            <dt className="text-muted-foreground">{split === "train" ? "训练集" : "验证集"}</dt>
            <dd className="mt-1 font-medium tabular-nums">
              {splits[split].images} 张 ·{" "}
              {total ? ((splits[split].images / total) * 100).toFixed(1) : "0.0"}%
            </dd>
          </div>
        ))}
      </dl>
      {unknown > 0 && (
        <p className="mt-3 text-xs text-muted-foreground">
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
  const [unit, setUnit] = useState<"images" | "instances">("instances");
  const [split, setSplit] = useState<"all" | Split>("all");
  const count = (row: TrainingDatasetClassPage["items"][number]) =>
    (split !== "val" ? row[`train_${unit}`] : 0) + (split !== "train" ? row[`val_${unit}`] : 0);
  const shown = [...rows].sort((a, b) => count(b) - count(a) || a.index - b.index).slice(0, 10);
  const maximum = Math.max(1, ...shown.map(count));
  return (
    <figure aria-label="冻结类别数量图" className="mb-4 rounded-lg bg-muted/40 p-4">
      <figcaption className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs font-semibold">本页类别分布</p>
          <p className="mt-1 text-2xs text-muted-foreground">
            显示本页计数最多的 10 类；完整类别见下表。
            {partial && "成员证据或划分不完整，仅展示已知数量。"}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <div role="group" aria-label="类别图计数单位" className="flex gap-1">
            {(
              [
                ["instances", "实例"],
                ["images", "图片"],
              ] as const
            ).map(([value, label]) => (
              <Button
                key={value}
                size="sm"
                variant={unit === value ? "primary" : "ghost"}
                aria-pressed={unit === value}
                onClick={() => setUnit(value)}
              >
                {label}
              </Button>
            ))}
          </div>
          <label className="sr-only" htmlFor="class-chart-split">
            类别图划分
          </label>
          <select
            id="class-chart-split"
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
      <ul className="mt-4 space-y-2">
        {shown.map((row) => (
          <li key={row.index}>
            <button
              className="grid w-full grid-cols-[minmax(4rem,1fr)_minmax(0,3fr)_auto] items-center gap-3 rounded-sm text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`按类别图查看 ${row.name} ${count(row)} ${unit === "images" ? "张图片" : "个实例"}`}
              onClick={() => onSelect(row.index, split === "all" ? undefined : split)}
            >
              <span className="truncate" title={row.name}>
                {row.name}
              </span>
              <span className="h-2 overflow-hidden rounded-sm bg-muted" aria-hidden="true">
                <span
                  className="block h-full rounded-sm bg-primary"
                  ref={(element) => {
                    if (element) element.style.width = `${(count(row) / maximum) * 100}%`;
                  }}
                />
              </span>
              <span className="font-mono tabular-nums">{count(row)}</span>
            </button>
          </li>
        ))}
      </ul>
      {!shown.length && (
        <p className="mt-4 text-xs text-muted-foreground">没有可展示的冻结类别。</p>
      )}
    </figure>
  );
}
