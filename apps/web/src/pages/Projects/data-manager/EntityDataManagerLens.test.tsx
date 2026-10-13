import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RefObject } from "react";

import { EntityDataManagerLens } from "./EntityDataManagerLens";

const scrollRef: RefObject<HTMLDivElement | null> = { current: null };

const state = vi.hoisted(() => ({
  schemaError: false,
  schemaRefetch: vi.fn(),
}));

vi.mock("@/hooks/usePermissions", () => ({
  usePermissions: () => ({ role: "employee" }),
}));

vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: { user: { id: string } }) => unknown) =>
    selector({ user: { id: "u1" } }),
}));

vi.mock("@/components/ui/Toast", () => ({
  useToastStore: (selector: (state: { push: () => void }) => unknown) =>
    selector({ push: vi.fn() }),
}));

vi.mock("./EntityDetailSheet", () => ({
  EntityDetailSheet: ({ selected }: { selected: string | null }) => (
    <output data-testid="entity-selected">{selected ?? ""}</output>
  ),
}));

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

vi.mock("@/hooks/useTaskViews", () => {
  const view = {
    id: null,
    key: "all",
    project_id: "p1",
    owner_id: null,
    name: "全部对象",
    visibility: "project",
    entity_scope: "objects",
    filter_json: { field: "annotation.annotation_count", op: "eq", value: 1 },
    sort_json: [{ field: "annotation.updated_at", direction: "desc" }],
    columns_json: ["class_name"],
    builtin: true,
    task_count: 1,
    result_count: 1,
    created_at: null,
    updated_at: null,
    invalid_fields: [],
  };
  const secondView = {
    ...view,
    id: "v2",
    key: null,
    name: "第二对象视图",
    filter_json: { field: "annotation.annotation_count", op: "eq", value: 2 },
    builtin: false,
    owner_id: "u1",
    visibility: "private",
  };
  const keywordView = {
    ...view,
    id: "keyword",
    key: null,
    name: "关键字视图",
    filter_json: { field: "task.keyword", op: "contains", value: "needle" },
    builtin: false,
    owner_id: "u1",
    visibility: "private",
  };
  const schema = {
    entity_scope: "objects",
    available_entity_scopes: ["tasks", "objects"],
    project_kind: { data_type: "image", type_key: "image", scene_mode: false },
    tool_units: [],
    filter_fields: [
      {
        key: "annotation.annotation_count",
        label: "标注数",
        group: "标注",
        value_type: "number",
        operators: ["eq", "between"],
        options: [],
        expensive: false,
        tool_unit_id: null,
        attribute_key: null,
      },
    ],
    columns: [
      {
        key: "class_name",
        label: "类别",
        group: "标注",
        default: true,
        expensive: false,
        sortable: false,
        sort_field: null,
      },
    ],
    default_columns: ["class_name"],
    sort_fields: [{ value: "annotation.updated_at", label: "更新时间" }],
    metrics: [],
    builtin_views: ["all"],
  };
  const page = {
    items: [
      {
        entity_key: "a1",
        annotation_id: "a1",
        task_id: "t1",
        task_display_id: "T-1",
        file_name: "frame.png",
        batch_id: null,
        class_name: "car",
        tool_unit_id: "bbox",
        annotation_type: "rectangle",
        source: "manual",
        imported: false,
        confidence: null,
        track_id: null,
        parent_prediction_id: null,
        parent_annotation_id: null,
        attributes: {},
        attribute_origins: {},
        created_by_id: "u1",
        created_by_name: "User",
        created_at: null,
        updated_at: null,
        unresolved_feedback_count: 0,
        location: {
          project_id: "p1",
          task_id: "t1",
          task_display_id: "T-1",
          batch_id: null,
          dataset_item_id: null,
          data_type: "image",
          focus_kind: "annotation",
          annotation_id: "a1",
          track_id: null,
          scene_id: null,
          scene_name: null,
          scene_frame_index: null,
          video_frame_index: null,
        },
      },
    ],
    total: 1,
    limit: 100,
    next_cursor: null,
    facets: {
      matched_total: 1,
      task_total: 1,
      by_class: { car: 1 },
      by_source: { manual: 1 },
      by_tool_unit: { bbox: 1 },
      by_type: { rectangle: 1 },
      by_quality: {},
    },
  };
  const query = () => ({
    data: { pages: [page] },
    hasNextPage: false,
    isFetchingNextPage: false,
    isLoading: false,
    isError: false,
    isFetching: false,
    fetchNextPage: vi.fn(),
    refetch: vi.fn(),
  });
  return {
    useTaskViews: () => ({ data: { items: [view, secondView, keywordView] }, isLoading: false }),
    useDataManagerSchema: () => ({
      data: schema,
      isLoading: false,
      isError: state.schemaError,
      refetch: state.schemaRefetch,
    }),
    useDataManagerObjects: query,
    useDataManagerTracks: query,
    useCreateTaskView: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useUpdateTaskView: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useDeleteTaskView: () => ({ mutateAsync: vi.fn(), isPending: false }),
    useDataManagerObjectDetail: () => ({ data: undefined, isLoading: false, isError: false }),
    useDataManagerTrackDetail: () => ({ data: undefined, isLoading: false, isError: false }),
  };
});

describe("EntityDataManagerLens", () => {
  beforeEach(() => {
    state.schemaError = false;
    state.schemaRefetch.mockReset();
  });

  it("keeps an incomplete numeric condition out of the applied URL filter", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects"]}>
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /^标注数/ }));
    const input = screen.getByRole("textbox", { name: "条件值" });
    await user.type(input, "-");
    await user.tab();
    expect(screen.getByText("请输入完整数字")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存视图" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled();
    await user.clear(input);
    await user.type(input, "2");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存视图" })).not.toBeDisabled(),
    );
  });

  it("writes the URL and hydrates the selected entity view", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects"]}>
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
        <LocationProbe />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: /^第二对象视图/ }));
    await waitFor(() => {
      const search = new URL("http://localhost" + screen.getByTestId("location").textContent)
        .searchParams;
      expect(search.get("view")).toBe("saved:v2");
    });
    expect(screen.getAllByText("第二对象视图", { exact: true }).length).toBeGreaterThan(0);
  });

  it("keeps the dirty-discard navigation in the URL", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects"]}>
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
        <LocationProbe />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: "降序" }));
    await user.click(screen.getByRole("button", { name: /^第二对象视图/ }));
    await user.click(screen.getByRole("button", { name: "放弃并切换" }));
    await waitFor(() => {
      const search = new URL("http://localhost" + screen.getByTestId("location").textContent)
        .searchParams;
      expect(search.get("view")).toBe("saved:v2");
    });
  });

  it("clears a selected entity when its project identity changes", async () => {
    const view = render(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects&selected=a1"]}>
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("entity-selected")).toHaveTextContent("a1"));

    view.rerender(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects&selected=a1"]}>
        <EntityDataManagerLens
          projectId="p2"
          projectName="Other Project"
          projectDisplayId="P-2"
          projectOwnerId="u2"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("entity-selected")).toHaveTextContent(""));
  });

  it("offers a retry action when the entity schema request fails", () => {
    state.schemaError = true;
    render(
      <MemoryRouter initialEntries={["/projects/p1?lens=objects"]}>
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(state.schemaRefetch).toHaveBeenCalledOnce();
  });

  it("keeps a saved keyword when a URL sort override is present", async () => {
    render(
      <MemoryRouter
        initialEntries={[
          "/projects/p1?lens=objects&view=saved:keyword&sort=%7B%22v%22%3A1%2C%22value%22%3A%5B%7B%22field%22%3A%22annotation.updated_at%22%2C%22direction%22%3A%22asc%22%7D%5D%7D",
        ]}
      >
        <EntityDataManagerLens
          projectId="p1"
          projectName="Project"
          projectDisplayId="P-1"
          projectOwnerId="u1"
          scope="objects"
          availableScopes={["tasks", "objects"]}
          onScopeChange={vi.fn()}
          scrollContainerRef={scrollRef}
        />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "搜索任务、文件或 Scene" })).toHaveValue("needle"),
    );
  });
});
