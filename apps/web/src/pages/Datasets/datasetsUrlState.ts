import type { UrlStateCodec, UrlStateIssue } from "@/hooks/useUrlFilterState";
import { listPageSize } from "@/components/ui/ListPagination";
import type {
  DatasetAvailabilityState,
  DatasetItemFilters,
  DatasetLifecycleState,
} from "@/api/datasets";

export const DATASET_DATA_TYPE_VALUES = ["image", "video", "point_cloud", "multimodal"] as const;
export type DatasetDataType = (typeof DATASET_DATA_TYPE_VALUES)[number];
export interface DatasetsUrlState {
  query: string;
  state: DatasetLifecycleState;
  data_type?: DatasetDataType;
  location_id?: string;
  page: number;
  pageSize: number;
}
export const EMPTY_DATASETS_URL_STATE: DatasetsUrlState = {
  query: "",
  state: "active",
  page: 0,
  pageSize: 20,
};
export const DATASET_FILTER_KEYS = [
  "q",
  "state",
  "data_type",
  "location_id",
  "page",
  "page_size",
] as const;
export const availabilityLabels: Record<DatasetAvailabilityState, string> = {
  verified: "已核验（读取时复核）",
  missing: "文件缺失",
  changed: "内容变化待确认",
  preview_only: "仅预览可用",
  unavailable: "原图不可用",
  unverified: "待核验",
};
export function pageNumber(value: string | null): number {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? Math.min(number - 1, 1_000_000) : 0;
}
export function clampDatasetPage(page: number, pageSize: number, total: number): number {
  return Math.min(page, Math.max(0, Math.ceil(total / pageSize) - 1));
}
export function parseDatasetsUrlWithIssues(search: string | URLSearchParams): {
  state: DatasetsUrlState;
  issues: UrlStateIssue[];
} {
  const params = new URLSearchParams(typeof search === "string" ? search : search.toString());
  const issues: UrlStateIssue[] = [];
  const rawType = params.get("data_type")?.trim() ?? "";
  const type = rawType === "all" || !rawType ? undefined : rawType;
  const valid = DATASET_DATA_TYPE_VALUES.includes(type as DatasetDataType);
  if (type && !valid) issues.push({ key: "data_type", message: "未知的数据集类型" });
  const rawState = params.get("state") ?? "active";
  const stateValid = ["active", "archived", "all"].includes(rawState);
  if (!stateValid) issues.push({ key: "state", message: "未知的数据集状态" });
  return {
    state: {
      state: stateValid ? (rawState as DatasetLifecycleState) : "active",
      query: params.get("q")?.trim() ?? "",
      data_type: valid ? (type as DatasetDataType) : undefined,
      location_id: params.get("location_id")?.trim() || undefined,
      page: pageNumber(params.get("page")),
      pageSize: listPageSize(params.get("page_size")),
    },
    issues,
  };
}
function updateDatasetsUrl(current: URLSearchParams, state: DatasetsUrlState): URLSearchParams {
  const next = new URLSearchParams(current);
  const values = {
    q: state.query.trim(),
    state: state.state === "active" ? undefined : state.state,
    data_type: state.data_type,
    location_id: state.location_id,
    page: state.page > 0 ? String(state.page + 1) : undefined,
    page_size: state.pageSize !== 20 ? String(state.pageSize) : undefined,
  };
  Object.entries(values).forEach(([key, value]) =>
    value ? next.set(key, value) : next.delete(key),
  );
  return next;
}
export const datasetsUrlCodec: UrlStateCodec<DatasetsUrlState> = {
  parse: parseDatasetsUrlWithIssues,
  encode: updateDatasetsUrl,
  clear: (current, defaults) => updateDatasetsUrl(current, defaults),
};
export const parseDatasetsUrl = (search: string | URLSearchParams) =>
  parseDatasetsUrlWithIssues(search).state;

export const DATASET_ITEM_FILTER_KEYS = [
  "search",
  "source_id",
  "scene_id",
  "availability",
  "media_type",
  "extension",
  "dimension_state",
  "min_width",
  "max_width",
  "min_height",
  "max_height",
] as const;
export function datasetItemFilters(params: URLSearchParams): DatasetItemFilters {
  const filters: Record<string, string | number> = {};
  DATASET_ITEM_FILTER_KEYS.forEach((key) => {
    const value = params.get(key === "search" ? "q" : key);
    if (!value) return;
    // Invalid URL conditions must reach validation rather than broaden results.
    filters[key] = key.startsWith("min_") || key.startsWith("max_") ? Number(value) : value;
  });
  return filters as DatasetItemFilters;
}
export function datasetFilterPatch(
  filters: DatasetItemFilters,
): Record<string, string | undefined> {
  return Object.fromEntries(
    DATASET_ITEM_FILTER_KEYS.map((key) => [
      key === "search" ? "q" : key,
      filters[key] == null || filters[key] === "" ? undefined : String(filters[key]),
    ]),
  );
}
export function datasetFilterSignature(filters: DatasetItemFilters): string {
  const values = datasetFilterPatch(filters);
  if (values.extension) {
    values.extension = values.extension.toLowerCase().replace(/^\./, "");
    if (values.extension === "jpeg") values.extension = "jpg";
  }
  if (values.q) values.q = values.q.trim();
  return JSON.stringify(values);
}

export type DatasetTab = "data" | "sources" | "quality" | "projects" | "versions";
export function parseDatasetDetailUrl(params: URLSearchParams) {
  const rawTab = params.get("tab");
  const tab: DatasetTab = ["data", "sources", "quality", "projects", "versions"].includes(
    rawTab ?? "",
  )
    ? (rawTab as DatasetTab)
    : params.has("source") || params.has("scan")
      ? "sources"
      : "data";
  const availability = params.get("availability") as DatasetAvailabilityState | null;
  return {
    tab,
    filters: datasetItemFilters(params),
    savedViewId: params.get("saved_view") || undefined,
    restoreSavedView: !!params.get("saved_view") && params.get("view_query") !== "1",
    query: params.get("q") ?? "",
    sourceId: params.get("source_id") || undefined,
    availability:
      availability && Object.prototype.hasOwnProperty.call(availabilityLabels, availability)
        ? availability
        : undefined,
    page: pageNumber(params.get("page")),
    pageSize: listPageSize(params.get("page_size")),
    view: params.get("view") === "grid" ? ("grid" as const) : ("table" as const),
    itemId: params.get("item") || undefined,
    versionPage: pageNumber(params.get("version_page")),
    versionPageSize: listPageSize(params.get("version_page_size")),
    projectPage: pageNumber(params.get("project_page")),
    projectPageSize: listPageSize(params.get("project_page_size")),
    statsProjectId: params.has("stats_project") ? params.get("stats_project")! : undefined,
  };
}
export function safeDatasetsReturn(value: string | null): string {
  if (!value || /[\\#\r\n]/.test(value) || !/^\/(?:datasets|storage)(?:\?|$)/.test(value))
    return "/datasets";
  return value;
}
export function datasetDetailPath(
  id: string,
  listSearch = "",
  extra?: Record<string, string>,
): string {
  const params = new URLSearchParams(extra);
  params.set("return", safeDatasetsReturn(`/datasets${listSearch}`));
  return `/datasets/${encodeURIComponent(id)}?${params}`;
}
export function legacyDatasetDetailPath(params: URLSearchParams): string | null {
  const id = params.get("dataset");
  if (!id) return null;
  const list = new URLSearchParams(params);
  ["dataset", "source", "scan", "tab"].forEach((key) => list.delete(key));
  const extra: Record<string, string> = {};
  ["source", "scan"].forEach((key) => {
    if (params.get(key)) extra[key] = params.get(key)!;
  });
  if (extra.source || extra.scan) extra.tab = "sources";
  return datasetDetailPath(id, list.size ? `?${list}` : "", extra);
}
