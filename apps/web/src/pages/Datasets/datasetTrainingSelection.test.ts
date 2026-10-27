import { describe, expect, it } from "vitest";
import {
  datasetTrainingFilterJSON,
  datasetTrainingPath,
  readDatasetTrainingFilter,
} from "./datasetTrainingSelection";

const id = "11111111-2222-4333-8444-555555555555";
describe("dataset filter training handoff", () => {
  it("preserves a complete definition without page, layout or a dynamic view ID", () => {
    const filters = {
      scene_id: id,
      source_id: id,
      search: "camera",
      extension: "png",
      min_width: 100,
      availability: "verified" as const,
    };
    const json = datasetTrainingFilterJSON(filters)!;
    const url = new URL(datasetTrainingPath("project", id, json), "http://localhost");
    expect(readDatasetTrainingFilter(url.searchParams.get("dataset_filter"), id)).toEqual({
      filter: filters,
      error: "",
    });
    expect([...url.searchParams.keys()].sort()).toEqual(["dataset_filter", "dataset_id", "tab"]);
    expect(
      new URL(datasetTrainingPath("project", id), "http://localhost").searchParams.has(
        "dataset_filter",
      ),
    ).toBe(false);
  });
  it.each([
    "",
    "null",
    "[]",
    "bad",
    '{"page":2}',
    '{"min_width":0}',
    '{"scene_id":"dead"}',
    '{"dimension_state":"unknown","max_height":100}',
    '{"min_width":200,"max_width":100}',
  ])("rejects invalid explicit URL %s", (raw) => {
    const value = readDatasetTrainingFilter(raw, id);
    expect(value.filter).toBeUndefined();
    expect(value.error).toContain("未扩大");
  });
  it("requires a dataset and refuses nonfinite numbers before JSON can erase them", () => {
    expect(readDatasetTrainingFilter("{}", "").error).not.toBe("");
    expect(datasetTrainingFilterJSON({ min_width: NaN })).toBeNull();
    expect(datasetTrainingFilterJSON({ min_width: Infinity })).toBeNull();
    expect(readDatasetTrainingFilter(null, id).error).toBe("");
    expect(readDatasetTrainingFilter('{"search":null}', id)).toEqual({ filter: {}, error: "" });
  });
});
