import type { DatasetItemFilters } from "@/api/datasets";
import { availabilityLabels, DATASET_ITEM_FILTER_KEYS } from "./datasetsUrlState";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const ranges = ["min_width", "max_width", "min_height", "max_height"] as const;
const invalid = "数据集筛选条件无效，请返回数据集重新选择；当前范围未扩大。";

export function validDatasetTrainingFilter(value: unknown): value is DatasetItemFilters {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  if (Object.keys(row).some((key) => !DATASET_ITEM_FILTER_KEYS.some((known) => known === key)))
    return false;
  for (const [key, field] of Object.entries(row)) {
    if (field == null) continue;
    if (ranges.some((known) => known === key)) {
      if (typeof field !== "number" || !Number.isInteger(field) || field < 1 || field > 10000000)
        return false;
    } else if (typeof field !== "string") return false;
    else if (key === "search" && field.length > 200) return false;
    else if (["source_id", "scene_id"].includes(key) && !uuid.test(field)) return false;
    else if (
      key === "availability" &&
      !Object.prototype.hasOwnProperty.call(availabilityLabels, field)
    )
      return false;
    else if (key === "media_type" && !["image", "video", "point_cloud", "other"].includes(field))
      return false;
    else if (key === "dimension_state" && !["known", "unknown"].includes(field)) return false;
    else if (key === "extension" && (field.length > 20 || !/^\.?[a-zA-Z0-9]+$/.test(field)))
      return false;
  }
  for (const axis of ["width", "height"] as const) {
    const lower = row[`min_${axis}`],
      upper = row[`max_${axis}`];
    if (typeof lower === "number" && typeof upper === "number" && lower > upper) return false;
  }
  return row.dimension_state !== "unknown" || !ranges.some((key) => row[key] != null);
}

export function datasetTrainingFilterJSON(filters: DatasetItemFilters): string | null {
  if (!validDatasetTrainingFilter(filters)) return null;
  return JSON.stringify(
    Object.fromEntries(
      DATASET_ITEM_FILTER_KEYS.flatMap((key) => {
        const value = filters[key];
        return value == null ? [] : [[key, value]];
      }),
    ),
  );
}

export function readDatasetTrainingFilter(raw: string | null, datasetId: string) {
  if (raw === null) return { filter: undefined, error: "" };
  try {
    if (!datasetId || raw.length > 2000) throw new Error(invalid);
    const filter: unknown = JSON.parse(raw);
    if (!validDatasetTrainingFilter(filter)) throw new Error(invalid);
    return {
      filter: Object.fromEntries(
        Object.entries(filter).filter(([, value]) => value != null),
      ) as DatasetItemFilters,
      error: "",
    };
  } catch {
    return { filter: undefined, error: invalid };
  }
}

export function datasetTrainingPath(projectId: string, datasetId: string, filter?: string) {
  const params = new URLSearchParams({ tab: "data", dataset_id: datasetId });
  if (filter !== undefined) params.set("dataset_filter", filter);
  return `/projects/${projectId}/learning?${params}`;
}

export function datasetTrainingFilterSummary(filter: DatasetItemFilters) {
  const labels: Record<string, string> = {
    search: "文件名",
    source_id: "来源 ID",
    scene_id: "Scene ID",
    availability: "可用性",
    media_type: "媒体类型",
    extension: "扩展名",
    dimension_state: "尺寸",
    min_width: "最小宽度",
    max_width: "最大宽度",
    min_height: "最小高度",
    max_height: "最大高度",
  };
  return (
    Object.entries(filter)
      .filter(([, value]) => value != null)
      .map(
        ([key, value]) =>
          `${labels[key]}：${key === "availability" ? availabilityLabels[value as keyof typeof availabilityLabels] : value}`,
      )
      .join(" · ") || "全部样本条件"
  );
}
