import { describe, expect, it } from "vitest";

import { createTaskView } from "@/test/dataManagerApi";

import {
  dataManagerViewKey,
  findDataManagerView,
  hasFilterUrlOverrides,
  normalizeDataManagerColumns,
  parseDataManagerUrl,
  parseDataManagerUrlWithIssues,
  requestedDataManagerViewKey,
  resolveDataManagerSort,
  resolveDataManagerViewKey,
  shouldUseDataManagerUrlOverrides,
  updateDataManagerUrl,
} from "./dataManagerUrlState";

describe("Data Manager URL state", () => {
  it("normalizes the legacy feedback column to the unresolved-issue column", () => {
    expect(
      normalizeDataManagerColumns([
        "display_id",
        "unresolved_feedback_count",
        "comment_count",
        "unresolved_issue_count",
      ]),
    ).toEqual(["display_id", "unresolved_issue_count", "comment_count"]);
  });

  it("round-trips all three lens states while preserving unrelated params", () => {
    for (const lens of ["tasks", "objects", "tracks"] as const) {
      const params = updateDataManagerUrl("keep=1", {
        lens,
        view: "builtin:all",
        query: "car / red",
        filter: { op: "and", rules: [] },
        sort: [{ field: "annotation.updated_at", direction: "desc" }],
        columns: ["class_name", "attributes"],
        selected: "实体 / 中文",
      });
      expect(params.get("keep")).toBe("1");
      expect(parseDataManagerUrl(params)).toEqual({
        lens,
        view: "builtin:all",
        query: "car / red",
        filter: { op: "and", rules: [] },
        sort: [{ field: "annotation.updated_at", direction: "desc" }],
        columns: ["class_name", "attributes"],
        selected: "实体 / 中文",
      });
    }
  });

  it("round-trips project sections, gallery layout and capped task selection", () => {
    const taskIds = Array.from({ length: 205 }, (_, index) => `task-${index}`);
    const params = updateDataManagerUrl("keep=1", {
      section: "members",
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: null,
      selectedTasks: taskIds,
      layout: "gallery",
    });
    const state = parseDataManagerUrl(params);
    expect(state.section).toBe("members");
    expect(state.layout).toBe("gallery");
    expect(state.selectedTasks).toHaveLength(200);
    expect(state.selectedTasks?.[199]).toBe("task-199");
    expect(params.get("keep")).toBe("1");
  });

  it("reports malformed task selection entries instead of hiding them", () => {
    const params = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: null,
      selectedTasks: ["task-1", ""],
    });
    expect(parseDataManagerUrlWithIssues(params).issues).toEqual([
      { key: "selected_tasks", message: "任务选择包含无效任务 ID" },
    ]);
  });

  it("falls back safely for invalid lens and malformed JSON", () => {
    const state = parseDataManagerUrl("lens=other&filter=%7Bbad&columns=1");
    expect(state.lens).toBe("tasks");
    expect(state.filter).toBeNull();
    expect(state.columns).toBeNull();
  });

  it("preserves an explicit empty filter envelope and reports malformed state", () => {
    const params = updateDataManagerUrl("layout=compact", {
      lens: "tasks",
      view: "saved:1",
      query: "",
      filter: {},
      sort: null,
      columns: null,
      selected: null,
    });
    expect(params.get("layout")).toBe("compact");
    expect(params.has("q")).toBe(false);
    expect(params.has("filter")).toBe(true);
    expect(hasFilterUrlOverrides("lens=tasks&selected=t1")).toBe(false);
    expect(parseDataManagerUrl(params).filter).toEqual({});
    expect(parseDataManagerUrlWithIssues("filter=%7Bbad").issues).toHaveLength(1);
  });

  it("uses the view sort when a short URL has no sort or carries another lens sort", () => {
    const viewSort = [{ field: "track.track_id", direction: "asc" }] as const;
    expect(
      resolveDataManagerSort(null, [...viewSort], ["track.track_id"], "track.track_id"),
    ).toEqual(viewSort);
    expect(
      resolveDataManagerSort(
        [{ field: "annotation.updated_at", direction: "desc" }],
        [...viewSort],
        ["track.track_id"],
        "track.track_id",
      ),
    ).toEqual(viewSort);
  });

  it("drops a saved sort whose field no longer exists in the schema", () => {
    expect(
      resolveDataManagerSort(
        null,
        [{ field: "removed.sort", direction: "desc" }],
        ["task.created_at", "unresolved_issue_count"],
        "task.created_at",
      ),
    ).toEqual([{ field: "task.created_at", direction: "asc" }]);
  });

  it("keys saved and builtin views by their stable identifier", () => {
    expect(dataManagerViewKey({ id: "v1", key: null })).toBe("saved:v1");
    expect(dataManagerViewKey({ id: null, key: "all" })).toBe("builtin:all");
  });

  it("resolves the requested task view and falls back to the builtin all view", () => {
    expect(requestedDataManagerViewKey({ lens: "tasks", view: "saved:v1" })).toBe("saved:v1");
    expect(requestedDataManagerViewKey({ lens: "tasks", view: null })).toBe("builtin:all");
    expect(requestedDataManagerViewKey({ lens: "objects", view: "saved:v1" })).toBe("builtin:all");
  });

  it("selects the saved view or falls back to the first one when the URL names a missing view", () => {
    const views = [
      createTaskView({ key: "all", builtin: true, name: "全部任务" }),
      createTaskView({ id: "v1", name: "组合视图" }),
    ];
    expect(findDataManagerView(views, "saved:v1")?.id).toBe("v1");
    expect(findDataManagerView(views, "saved:gone")).toBeNull();
    expect(resolveDataManagerViewKey("saved:gone", views)).toBe("builtin:all");
    expect(resolveDataManagerViewKey("saved:v1", views)).toBe("saved:v1");
    expect(resolveDataManagerViewKey("saved:v1", [])).toBeNull();
  });

  it("lets URL overrides outrank a saved view only for its own task lens", () => {
    const overrides =
      "lens=tasks&view=saved:v1&layout=gallery&sort=%7B%22v%22%3A1%2C%22value%22%3A%5B%5D%7D";
    expect(
      shouldUseDataManagerUrlOverrides({ lens: "tasks", view: "saved:v1" }, overrides, "saved:v1"),
    ).toBe(true);
    expect(
      shouldUseDataManagerUrlOverrides({ lens: "tasks", view: "saved:v1" }, overrides, "saved:v2"),
    ).toBe(false);
    expect(
      shouldUseDataManagerUrlOverrides(
        { lens: "objects", view: "saved:v1" },
        overrides,
        "saved:v1",
      ),
    ).toBe(false);
    expect(
      shouldUseDataManagerUrlOverrides(
        { lens: "tasks", view: "saved:v1" },
        "lens=tasks&view=saved:v1",
        "saved:v1",
      ),
    ).toBe(false);
  });
});
