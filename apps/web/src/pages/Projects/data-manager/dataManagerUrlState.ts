import type { DataManagerEntityScope, ProjectTaskView, TaskSortItem } from "@/api/taskViews";
import type { UrlStateCodec } from "@/hooks/useUrlFilterState";

export interface UrlStateIssue {
  key: string;
  message: string;
}

export type DataManagerSection = "overview" | "data" | "members";
export type DataManagerLayout = "list" | "gallery";

export interface DataManagerUrlState {
  /** The project section. Missing on legacy URLs, which intentionally open Data. */
  section?: DataManagerSection;
  lens: DataManagerEntityScope;
  view: string | null;
  query: string;
  filter: Record<string, unknown> | null;
  sort: TaskSortItem[] | null;
  columns: string[] | null;
  selected: string | null;
  /** Explicit task IDs selected for task operations; never more than 200. */
  selectedTasks?: string[] | null;
  layout?: DataManagerLayout;
}

export const DATA_MANAGER_FILTER_KEYS = [
  "section",
  "lens",
  "view",
  "q",
  "filter",
  "sort",
  "columns",
  "selected",
  "selected_tasks",
  "layout",
] as const;

interface VersionedValue<T> {
  v: 1;
  value: T;
}

function decode<T>(params: URLSearchParams, key: string, issues: UrlStateIssue[]) {
  const raw = params.get(key);
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<VersionedValue<T>>;
    if (parsed?.v !== 1 || !("value" in parsed)) throw new Error("unsupported version");
    return parsed.value as T;
  } catch {
    issues.push({ key, message: "无法读取 URL 中的筛选状态，已使用安全默认值" });
    return null;
  }
}

function encode<T>(params: URLSearchParams, key: string, value: T | null) {
  if (value === null) {
    params.delete(key);
    return;
  }
  params.set(key, JSON.stringify({ v: 1, value } satisfies VersionedValue<T>));
}

function isSortItem(value: unknown): value is TaskSortItem {
  return Boolean(
    value &&
    typeof value === "object" &&
    typeof (value as TaskSortItem).field === "string" &&
    ((value as TaskSortItem).direction === "asc" || (value as TaskSortItem).direction === "desc"),
  );
}

/**
 * Normalize restored Data Manager task columns.
 *
 * The legacy `unresolved_feedback_count` column is a server-side alias of the
 * corrected `unresolved_issue_count` count, so an explicitly saved old column
 * list is normalized to the new column at restore time. Saved views in the
 * database are never rewritten.
 */
export function normalizeDataManagerColumns(columns: readonly string[]): string[] {
  const normalized: string[] = [];
  for (const column of columns) {
    const key = column === "unresolved_feedback_count" ? "unresolved_issue_count" : column;
    if (!normalized.includes(key)) normalized.push(key);
  }
  return normalized;
}

export function parseDataManagerUrlWithIssues(search: string | URLSearchParams): {
  state: DataManagerUrlState;
  issues: UrlStateIssue[];
} {
  const params =
    typeof search === "string" ? new URLSearchParams(search) : new URLSearchParams(search);
  const issues: UrlStateIssue[] = [];
  const rawLens = params.get("lens");
  const rawSection = params.get("section");
  const section: DataManagerSection =
    rawSection === "overview" || rawSection === "members" || rawSection === "data"
      ? rawSection
      : "data";
  if (rawSection && rawSection !== section)
    issues.push({ key: "section", message: "未知的项目区域" });
  const lens: DataManagerEntityScope =
    rawLens === "objects" || rawLens === "tracks" || rawLens === "tasks" ? rawLens : "tasks";
  if (rawLens && rawLens !== lens) issues.push({ key: "lens", message: "未知的数据视图" });
  const rawFilter = decode<unknown>(params, "filter", issues);
  const rawSort = decode<unknown>(params, "sort", issues);
  const rawColumns = decode<unknown>(params, "columns", issues);
  const rawSelectedTasks = decode<unknown>(params, "selected_tasks", issues);
  const filter =
    rawFilter && typeof rawFilter === "object" && !Array.isArray(rawFilter)
      ? (rawFilter as Record<string, unknown>)
      : rawFilter === null
        ? null
        : (issues.push({ key: "filter", message: "筛选表达式格式无效" }), null);
  const sort =
    rawSort === null
      ? null
      : Array.isArray(rawSort) && rawSort.every(isSortItem)
        ? rawSort
        : (issues.push({ key: "sort", message: "排序格式无效" }), null);
  const columns =
    rawColumns === null
      ? null
      : Array.isArray(rawColumns) && rawColumns.every((item) => typeof item === "string")
        ? rawColumns
        : (issues.push({ key: "columns", message: "列配置格式无效" }), null);
  const selectedTasks =
    rawSelectedTasks === null
      ? null
      : Array.isArray(rawSelectedTasks) &&
          rawSelectedTasks.every((item) => typeof item === "string")
        ? (rawSelectedTasks.length > 200 &&
            issues.push({ key: "selected_tasks", message: "任务选择超过 200 个，已截取前 200 个" }),
          rawSelectedTasks.slice(0, 200))
        : (issues.push({ key: "selected_tasks", message: "任务选择格式无效" }), null);
  if (
    Array.isArray(rawSelectedTasks) &&
    rawSelectedTasks.some((item) => typeof item !== "string" || !item.trim())
  ) {
    issues.push({ key: "selected_tasks", message: "任务选择包含无效任务 ID" });
  }
  const rawLayout = params.get("layout");
  const layout: DataManagerLayout = rawLayout === "gallery" ? "gallery" : "list";
  const state: DataManagerUrlState = {
    lens,
    view: params.get("view"),
    query: params.get("q")?.trim() ?? "",
    filter,
    sort,
    columns,
    selected: params.get("selected"),
  };
  if (rawSection !== null) state.section = section;
  if (rawSelectedTasks !== null) state.selectedTasks = selectedTasks;
  if (rawLayout === "gallery" || rawLayout === "list") state.layout = layout;
  return {
    state,
    issues,
  };
}

export function hasFilterUrlOverrides(search: string | URLSearchParams) {
  const params = typeof search === "string" ? new URLSearchParams(search) : search;
  return ["q", "filter", "sort", "columns", "selected_tasks", "layout"].some((key) =>
    params.has(key),
  );
}

/**
 * Stable key for a saved/builtin task view. A saved view is identified by its
 * id, a builtin one by its key; the two never collide because of the prefix.
 */
export function dataManagerViewKey(view: Pick<ProjectTaskView, "id" | "key">): string {
  return view.id ? `saved:${view.id}` : `builtin:${view.key}`;
}

/** The task view named by the URL; anything else falls back to the builtin all view. */
export function requestedDataManagerViewKey(
  state: Pick<DataManagerUrlState, "lens" | "view">,
): string {
  return state.lens === "tasks" && state.view ? state.view : "builtin:all";
}

/** Find the view whose key the URL requested, or null when it no longer exists. */
export function findDataManagerView(
  views: readonly ProjectTaskView[],
  requestedKey: string,
): ProjectTaskView | null {
  return views.find((view) => dataManagerViewKey(view) === requestedKey) ?? null;
}

/**
 * The first view a caller should select when the URL names a view that is gone.
 * Null when the project has no views yet, so callers must keep their requested
 * key until the list arrives instead of inventing a builtin that may not exist.
 */
export function resolveDataManagerViewKey(
  requestedKey: string,
  views: readonly ProjectTaskView[],
): string | null {
  if (views.some((view) => dataManagerViewKey(view) === requestedKey)) return requestedKey;
  return views.length ? dataManagerViewKey(views[0]) : null;
}

/**
 * Whether per-URL filter overrides (keyword, filter, sort, columns, selection,
 * layout) outrank a saved view's stored query. The URL only wins for the task
 * lens and the currently selected view; another view's stale overrides must not
 * leak into the freshly selected one.
 */
export function shouldUseDataManagerUrlOverrides(
  state: Pick<DataManagerUrlState, "lens" | "view">,
  search: string | URLSearchParams,
  selectedKey: string,
): boolean {
  return (
    state.lens === "tasks" &&
    (!state.view || state.view === selectedKey) &&
    hasFilterUrlOverrides(search)
  );
}

export const dataManagerUrlCodec: UrlStateCodec<DataManagerUrlState> = {
  parse: (search) => parseDataManagerUrlWithIssues(search),
  encode: (current, state) => updateDataManagerUrl(current, state),
  clear: (current, defaults) => updateDataManagerUrl(current, defaults),
};

export function parseDataManagerUrl(search: string | URLSearchParams): DataManagerUrlState {
  return parseDataManagerUrlWithIssues(search).state;
}

export function resolveDataManagerSort(
  urlSort: TaskSortItem[] | null,
  viewSort: TaskSortItem[],
  allowedFields: string[],
  fallbackField: string,
): TaskSortItem[] {
  const allowed = new Set(allowedFields);
  const isValid = (items: TaskSortItem[] | null) =>
    Boolean(
      items?.length &&
      items.every(
        (item) =>
          allowed.has(item.field) && (item.direction === "asc" || item.direction === "desc"),
      ),
    );
  if (isValid(urlSort)) return urlSort!;
  if (isValid(viewSort)) return viewSort;
  return [{ field: fallbackField, direction: "asc" }];
}

export function updateDataManagerUrl(
  current: string | URLSearchParams,
  state: DataManagerUrlState,
): URLSearchParams {
  const params = new URLSearchParams(typeof current === "string" ? current : current.toString());
  if (state.section) {
    if (state.section === "data") params.delete("section");
    else params.set("section", state.section);
  }
  params.set("lens", state.lens);
  if (state.view) params.set("view", state.view);
  else params.delete("view");
  if (state.query) params.set("q", state.query);
  else params.delete("q");
  encode(params, "filter", state.filter);
  encode(params, "sort", state.sort);
  encode(params, "columns", state.columns);
  if (state.selected) params.set("selected", state.selected);
  else params.delete("selected");
  if (state.selectedTasks !== undefined) {
    encode(params, "selected_tasks", state.selectedTasks?.length ? state.selectedTasks : null);
  }
  if (state.layout) {
    if (state.layout === "list") params.delete("layout");
    else params.set("layout", state.layout);
  }
  return params;
}

export function clearFilterUrlState(
  current: string | URLSearchParams,
  defaults: DataManagerUrlState,
) {
  return updateDataManagerUrl(current, defaults);
}
