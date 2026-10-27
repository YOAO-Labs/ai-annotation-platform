import { useState } from "react";
import { ChevronDown } from "lucide-react";
import type { DatasetItemFilters as Filters } from "@/api/datasets";
import { Button } from "@/components/ui/Button";
import { DatasetSceneFilter } from "./DatasetSceneFilter";
import { availabilityLabels } from "./datasetsUrlState";

const control =
  "w-full rounded-md border border-input bg-background px-2.5 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const rangeKeys = ["min_width", "max_width", "min_height", "max_height"] as const;
const moreKeys = ["media_type", "extension", "dimension_state", ...rangeKeys] as const;

export function DatasetItemFilters({
  filters,
  datasetId,
  hasScenes,
  sources,
  onChange,
}: {
  filters: Filters;
  datasetId: string;
  hasScenes: boolean;
  sources: { id: string; name: string }[];
  onChange: (filters: Filters) => void;
}) {
  const [draft, setDraft] = useState(() =>
    Object.fromEntries(moreKeys.map((key) => [key, String(filters[key] ?? "")])),
  );
  const [error, setError] = useState("");
  const active = moreKeys.filter((key) => filters[key] != null).length;
  const field = (key: string, value: string) => {
    setDraft((old) => ({ ...old, [key]: value }));
    setError("");
  };
  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="space-y-1 text-xs text-muted-foreground">
          搜索样本
          <input
            className={control}
            placeholder="搜索文件名…"
            maxLength={200}
            value={filters.search ?? ""}
            onChange={(e) => onChange({ ...filters, search: e.target.value || undefined })}
          />
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">
          来源筛选
          <select
            className={control}
            value={filters.source_id ?? ""}
            onChange={(e) => onChange({ ...filters, source_id: e.target.value || undefined })}
          >
            <option value="">全部来源</option>
            {filters.source_id && !sources.some((s) => s.id === filters.source_id) && (
              <option value={filters.source_id}>指定来源</option>
            )}
            {sources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-xs text-muted-foreground">
          可用性筛选
          <select
            className={control}
            value={filters.availability ?? ""}
            onChange={(e) =>
              onChange({
                ...filters,
                availability: (e.target.value as Filters["availability"]) || undefined,
              })
            }
          >
            <option value="">全部可用性</option>
            {Object.entries(availabilityLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {(hasScenes || filters.scene_id) && (
        <DatasetSceneFilter
          datasetId={datasetId}
          sceneId={filters.scene_id}
          onChange={(scene_id) => onChange({ ...filters, scene_id })}
        />
      )}
      <details
        open={active > 0 || undefined}
        className="rounded-md border border-border bg-muted/20"
      >
        <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm">
          <ChevronDown className="size-3.5" />
          更多筛选
          {active > 0 && <span className="text-xs text-muted-foreground">{active} 项条件</span>}
        </summary>
        <form
          className="space-y-3 border-t border-border p-3"
          onSubmit={(e) => {
            e.preventDefault();
            const next: Filters = { ...filters };
            for (const key of rangeKeys) {
              const value = draft[key] ? Number(draft[key]) : undefined;
              if (
                value !== undefined &&
                (!Number.isInteger(value) || value < 1 || value > 10_000_000)
              ) {
                setError("尺寸须为 1–10000000 的整数");
                return;
              }
              next[key] = value;
            }
            if (
              (next.min_width && next.max_width && next.min_width > next.max_width) ||
              (next.min_height && next.max_height && next.min_height > next.max_height)
            ) {
              setError("最小尺寸不能大于最大尺寸");
              return;
            }
            if (
              draft.dimension_state === "unknown" &&
              rangeKeys.some((key) => next[key] !== undefined)
            ) {
              setError("尺寸未知不能与宽高范围同时使用");
              return;
            }
            if (draft.extension && !/^\.?[a-zA-Z0-9]{1,19}$/.test(draft.extension.trim())) {
              setError("扩展名仅支持字母和数字，例如 jpg");
              return;
            }
            onChange({
              ...next,
              media_type: (draft.media_type as Filters["media_type"]) || undefined,
              extension: draft.extension.trim().replace(/^\./, "").toLowerCase() || undefined,
              dimension_state: (draft.dimension_state as Filters["dimension_state"]) || undefined,
            });
          }}
        >
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1 text-xs text-muted-foreground">
              媒体类型
              <select
                className={control}
                value={draft.media_type}
                onChange={(e) => field("media_type", e.target.value)}
              >
                <option value="">全部类型</option>
                <option value="image">图片</option>
                <option value="video">视频</option>
                <option value="point_cloud">点云</option>
                <option value="other">其他文件</option>
              </select>
            </label>
            <div className="space-y-1">
              <label htmlFor="dataset-extension" className="text-xs text-muted-foreground">
                文件扩展名
              </label>
              <input
                id="dataset-extension"
                className={control}
                maxLength={20}
                value={draft.extension}
                onChange={(e) => field("extension", e.target.value)}
                aria-describedby="dataset-extension-hint"
              />
              <p
                id="dataset-extension-hint"
                className="text-xs leading-relaxed text-muted-foreground"
              >
                按文件名后缀筛选；jpg 与 jpeg 归为一组。
              </p>
            </div>
            <label className="space-y-1 text-xs text-muted-foreground">
              尺寸状态
              <select
                className={control}
                value={draft.dimension_state}
                onChange={(e) => field("dimension_state", e.target.value)}
              >
                <option value="">全部尺寸</option>
                <option value="known">尺寸已知</option>
                <option value="unknown">尺寸未知</option>
              </select>
            </label>
          </div>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {rangeKeys.map((key, index) => (
              <label key={key} className="space-y-1 text-xs text-muted-foreground">
                {["最小宽度", "最大宽度", "最小高度", "最大高度"][index]}
                <input
                  className={control}
                  type="number"
                  min={1}
                  max={10_000_000}
                  step={1}
                  value={draft[key]}
                  onChange={(e) => field(key, e.target.value)}
                />
              </label>
            ))}
          </div>
          {error && (
            <p role="alert" className="text-xs text-status-danger">
              {error}
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="primary" type="submit">
              应用筛选
            </Button>
            <Button
              size="sm"
              type="button"
              onClick={() => {
                setDraft(Object.fromEntries(moreKeys.map((key) => [key, ""])));
                setError("");
                onChange({
                  search: filters.search,
                  source_id: filters.source_id,
                  scene_id: filters.scene_id,
                  availability: filters.availability,
                });
              }}
            >
              清除更多条件
            </Button>
            <span className="text-xs text-muted-foreground">
              宽高单位为像素；范围只匹配尺寸已知的样本。
            </span>
          </div>
        </form>
      </details>
    </div>
  );
}
