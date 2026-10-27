import { describe, expect, it } from "vitest";

import type { DataManagerFilterField } from "@/api/taskViews";

import { isExpressionValid } from "./dataManagerFilterExpression";
import { parseDataManagerUrlWithIssues, updateDataManagerUrl } from "./dataManagerUrlState";
import { datasetDataManagerPath, datasetTaskFilter } from "./datasetScope";

const datasetId = "5c806f2e-1c40-48cb-a809-0a5e013bd276";
const datasetField: DataManagerFilterField = {
  key: "dataset.dataset_id",
  label: "数据集 ID",
  group: "数据集",
  value_type: "text",
  operators: ["eq", "ne", "in"],
  options: [],
  expensive: false,
  tool_unit_id: null,
  attribute_key: null,
};

function parsePath(path: string) {
  const url = new URL(path, "http://localhost");
  return { url, ...parseDataManagerUrlWithIssues(url.search) };
}

describe("dataset Data Manager links", () => {
  it("restores the published dataset equality rule as a valid expression", () => {
    const rule = datasetTaskFilter(datasetId);
    expect(rule).toEqual({ field: "dataset.dataset_id", op: "eq", value: datasetId });
    expect(isExpressionValid(rule, [datasetField])).toBe(true);
  });

  it("opens the task data section with only an explicit dataset filter", () => {
    const { url, state, issues } = parsePath(datasetDataManagerPath("project-1", datasetId));
    expect(url.pathname).toBe("/projects/project-1/data-manager");
    expect([...url.searchParams.keys()].sort()).toEqual(["filter", "lens", "view"]);
    expect(state).toMatchObject({
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: datasetTaskFilter(datasetId),
      selected: null,
    });
    expect(issues).toEqual([]);
  });

  it("ANDs the dataset with the complete optional condition without flattening OR", () => {
    const condition = {
      op: "or",
      rules: [
        { field: "annotation.class_name", op: "eq", value: "car" },
        { field: "annotation.class_name", op: "eq", value: "truck" },
      ],
    };
    const path = datasetDataManagerPath("project-1", datasetId, condition);
    const filter = parsePath(path).state.filter;
    expect(filter).toEqual({ op: "and", rules: [datasetTaskFilter(datasetId), condition] });
    expect(
      isExpressionValid(filter!, [
        datasetField,
        { ...datasetField, key: "annotation.class_name", label: "类别" },
      ]),
    ).toBe(true);
    condition.rules[0].value = "changed";
    expect(parsePath(path).state.filter).toEqual(filter);
  });

  it("treats an empty optional condition as the dataset rule", () => {
    expect(parsePath(datasetDataManagerPath("project-1", datasetId, {})).state.filter).toEqual(
      datasetTaskFilter(datasetId),
    );
  });

  it("keeps ordinary filter clearing semantics without a separate dataset parameter", () => {
    const { url, state } = parsePath(datasetDataManagerPath("project-1", datasetId));
    const cleared = updateDataManagerUrl(url.search, { ...state, filter: {} });
    expect(parseDataManagerUrlWithIssues(cleared).state.filter).toEqual({});
    expect(cleared.has("dataset_id")).toBe(false);
    expect(cleared.toString()).not.toContain(datasetId);
  });
});
