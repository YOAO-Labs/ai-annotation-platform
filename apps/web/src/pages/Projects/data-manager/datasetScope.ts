import { updateDataManagerUrl } from "./dataManagerUrlState";

export function datasetTaskFilter(datasetId: string): Record<string, unknown> {
  return { field: "dataset.dataset_id", op: "eq", value: datasetId };
}

export function datasetDataManagerPath(
  projectId: string,
  datasetId: string,
  filter?: Record<string, unknown>,
): string {
  const datasetFilter = datasetTaskFilter(datasetId);
  const params = updateDataManagerUrl("", {
    section: "data",
    lens: "tasks",
    view: "builtin:all",
    query: "",
    filter:
      filter && Object.keys(filter).length > 0
        ? { op: "and", rules: [datasetFilter, filter] }
        : datasetFilter,
    sort: null,
    columns: null,
    selected: null,
    selectedTasks: null,
    layout: "list",
  });
  return `/projects/${encodeURIComponent(projectId)}/data-manager?${params.toString()}`;
}
