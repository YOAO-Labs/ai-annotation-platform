import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ProjectTaskQueryPayload } from "@/api/taskViews";
import { server } from "@/mocks/server";
import { expectNoUnexpectedApiRequests, resetUnexpectedApiRequests } from "@/test/apiRequestGuard";
import { createTestUser, resetAuthUser, seedAuthUser } from "@/test/auth";
import {
  createDataManagerTask,
  createTaskView,
  installDataManagerApi,
  READ_CAPABILITIES,
  SCHEMA,
  SUMMARY,
} from "@/test/dataManagerApi";
import { renderWithProviders } from "@/test/renderWithProviders";

import { ProjectDataManagerPage } from "./ProjectDataManagerPage";
import { updateDataManagerUrl } from "./data-manager/dataManagerUrlState";
import { datasetDataManagerPath, datasetTaskFilter } from "./data-manager/datasetScope";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.search}</output>;
}

function HistoryProbe() {
  const navigate = useNavigate();
  return (
    <>
      <button type="button" onClick={() => navigate(-1)}>
        back
      </button>
      <button type="button" onClick={() => navigate(1)}>
        forward
      </button>
    </>
  );
}

function ExternalViewProbe() {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      data-testid="external-view"
      onClick={() => navigate("/projects/p1/data-manager?lens=tasks&view=saved:v1")}
    >
      external
    </button>
  );
}

function renderDataManager(initialEntry: string, probes?: ReactNode) {
  return renderWithProviders(
    <Routes>
      <Route path="/projects/:id/data-manager" element={<ProjectDataManagerPage />} />
      <Route path="/unauthorized" element={<div>unauthorized</div>} />
    </Routes>,
    { initialEntries: [initialEntry], children: probes },
  );
}

function currentSearch(): URLSearchParams {
  return new URLSearchParams(screen.getByTestId("location").textContent ?? "");
}

function currentView(): string | null {
  return currentSearch().get("view");
}

function lastTaskQuery(queries: ProjectTaskQueryPayload[]): ProjectTaskQueryPayload | undefined {
  return queries[queries.length - 1];
}

beforeEach(() => {
  resetUnexpectedApiRequests();
  seedAuthUser(createTestUser());
});

afterEach(() => {
  // A target API request without an explicit handler fails the test that made it.
  expectNoUnexpectedApiRequests();
  resetAuthUser();
});

describe("ProjectDataManagerPage task data", () => {
  it("loads tasks at the API boundary and queries the resolved builtin view", async () => {
    const captured = installDataManagerApi();

    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all");

    expect(await screen.findByText("T-1")).toBeInTheDocument();
    await waitFor(() => expect(captured.taskQueries).toHaveLength(1));
    expect(captured.taskQueries[0]).toMatchObject({
      filter_json: {},
      sort_json: [{ field: "task.created_at", direction: "asc" }],
      columns_json: ["display_id"],
      limit: 20,
      offset: 0,
    });
  });

  it("filters the first task and summary requests from a dataset and class deep link", async () => {
    const datasetId = "5c806f2e-1c40-48cb-a809-0a5e013bd276";
    const classFilter = { field: "annotation.class_name", op: "eq", value: "car" };
    const expectedFilter = { op: "and", rules: [datasetTaskFilter(datasetId), classFilter] };
    const captured = installDataManagerApi();
    const summaryRequests: unknown[] = [];
    const datasetField = {
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
    server.use(
      http.get("*/api/v1/projects/p1/data-manager/schema", () =>
        HttpResponse.json({
          ...SCHEMA,
          filter_fields: [
            ...SCHEMA.filter_fields,
            datasetField,
            { ...datasetField, key: "annotation.class_name", label: "标注类别", group: "标注" },
          ],
        }),
      ),
      http.post("*/api/v1/projects/p1/data-manager/summary", async ({ request }) => {
        summaryRequests.push(await request.json());
        return HttpResponse.json(SUMMARY);
      }),
    );

    renderDataManager(datasetDataManagerPath("p1", datasetId, classFilter));

    expect(await screen.findByText("T-1")).toBeInTheDocument();
    await waitFor(() => {
      expect(captured.taskQueries).toHaveLength(1);
      expect(summaryRequests).toHaveLength(1);
    });
    expect(captured.taskQueries[0].filter_json).toEqual(expectedFilter);
    expect(summaryRequests[0]).toEqual({ filter_json: expectedFilter });
  });

  it("uses the selected row count at the API boundary and returns to the first page", async () => {
    const captured = installDataManagerApi();
    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all");
    expect(await screen.findByText("T-1")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("每页任务数"), { target: { value: "50" } });
    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)).toMatchObject({ limit: 50, offset: 0 }),
    );
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });

  it("restores the URL filter for a saved view without issuing a premature query", async () => {
    const filter = {
      op: "or" as const,
      rules: [
        { field: "task.status", op: "eq" as const, value: "pending" },
        { field: "task.status", op: "eq" as const, value: "review" },
      ],
    };
    const search = updateDataManagerUrl("layout=compact", {
      lens: "tasks",
      view: "saved:v1",
      query: "",
      filter,
      sort: [{ field: "task.created_at", direction: "asc" }],
      columns: ["display_id"],
      selected: null,
    }).toString();
    const captured = installDataManagerApi();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    await waitFor(() => expect(captured.taskQueries).toHaveLength(1));
    expect(captured.taskQueries[0].filter_json).toEqual(filter);
  });

  it("re-queries with the selected saved view's filter", async () => {
    const captured = installDataManagerApi();
    const user = userEvent.setup();

    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all", <LocationProbe />);
    expect(await screen.findByText("T-1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^第二视图/ }));

    await waitFor(() => expect(currentView()).toBe("saved:v2"));
    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)?.filter_json).toEqual({
        field: "task.status",
        op: "eq",
        value: "pending",
      }),
    );
  });

  it("restores each view filter through browser history", async () => {
    const captured = installDataManagerApi();
    const user = userEvent.setup();

    renderDataManager(
      "/projects/p1/data-manager?lens=tasks&view=saved:v1",
      <>
        <LocationProbe />
        <HistoryProbe />
      </>,
    );

    await waitFor(() => expect(lastTaskQuery(captured.taskQueries)?.filter_json).toEqual({}));

    await user.click(screen.getByRole("button", { name: /^第二视图/ }));
    await waitFor(() => expect(currentView()).toBe("saved:v2"));
    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)?.filter_json).toEqual({
        field: "task.status",
        op: "eq",
        value: "pending",
      }),
    );

    await user.click(screen.getByRole("button", { name: "back" }));
    await waitFor(() => expect(currentView()).toBe("saved:v1"));
    await waitFor(() => expect(lastTaskQuery(captured.taskQueries)?.filter_json).toEqual({}));

    await user.click(screen.getByRole("button", { name: "forward" }));
    await waitFor(() => expect(currentView()).toBe("saved:v2"));
    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)?.filter_json).toEqual({
        field: "task.status",
        op: "eq",
        value: "pending",
      }),
    );
  });

  it("hydrates a selected task outside the first page through the task API", async () => {
    installDataManagerApi({ tasks: [] });
    const search = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: "task-deep",
    }).toString();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    expect(await screen.findByRole("heading", { name: "T-deep" })).toBeInTheDocument();
  });

  it("normalizes a legacy feedback column at the API boundary", async () => {
    const captured = installDataManagerApi({
      tasks: [
        createDataManagerTask({
          id: "task-cols",
          display_id: "T-cols",
          file_name: "cols.png",
          unresolved_issue_count: 1,
          unresolved_feedback_count: 1,
          comment_count: 4,
        }),
      ],
    });
    const search = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: null,
      sort: null,
      columns: ["display_id", "unresolved_feedback_count", "comment_count"],
      selected: null,
    }).toString();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)?.columns_json).toEqual([
        "display_id",
        "unresolved_issue_count",
        "comment_count",
      ]),
    );
    expect(screen.getAllByRole("columnheader", { name: "未解决问题" })).toHaveLength(1);
    expect(screen.queryByRole("columnheader", { name: "反馈" })).not.toBeInTheDocument();
    const row = await screen.findByRole("row", { name: /T-cols/ });
    expect(within(row).getByText("4")).toBeInTheDocument();
  });

  it("renders empty filter state with reset button when active filter returns no tasks", async () => {
    installDataManagerApi({ tasks: [] });
    const filter = { field: "task.status", op: "eq" as const, value: "completed" };
    const search = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter,
      sort: null,
      columns: ["display_id"],
      selected: null,
    }).toString();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    expect(await screen.findByText("未找到匹配任务")).toBeInTheDocument();
    const resetBtn = screen.getByRole("button", { name: "重置所有筛选" });
    expect(resetBtn).toBeInTheDocument();

    fireEvent.click(resetBtn);
    await waitFor(() => {
      expect(screen.queryByText("未找到匹配任务")).toBeNull();
    });
  });

  it("does not open task detail from the gallery selection checkbox", async () => {
    const captured = installDataManagerApi({
      tasks: [
        createDataManagerTask({
          id: "task-gallery",
          display_id: "T-gallery",
          file_name: "gallery.bin",
          file_type: "point_cloud",
        }),
      ],
    });
    const search = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter: null,
      sort: null,
      columns: ["display_id"],
      selected: null,
      layout: "gallery",
    }).toString();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    const checkbox = await screen.findByRole("checkbox", { name: "选择任务 T-gallery" });
    await waitFor(() =>
      expect(lastTaskQuery(captured.taskQueries)?.columns_json).toEqual([
        "display_id",
        "annotation_count",
        "unresolved_issue_count",
        "comment_count",
      ]),
    );
    expect(screen.getByLabelText("任务画廊").parentElement).toHaveClass("min-h-[280px]");

    fireEvent.click(checkbox.parentElement!);
    fireEvent.keyDown(checkbox, { key: " " });
    fireEvent.keyDown(checkbox, { key: "Enter" });

    expect(screen.queryByRole("heading", { name: "T-gallery" })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "点云数据" })).toHaveTextContent("点云");
  });

  it("does not query while a newly added condition has no value", async () => {
    const captured = installDataManagerApi();
    const user = userEvent.setup();

    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all");
    expect(await screen.findByText("T-1")).toBeInTheDocument();
    await waitFor(() => expect(captured.taskQueries).toHaveLength(1));

    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(screen.getByRole("button", { name: /标注员 task\.assignee/ }));

    expect(
      screen.getByText("当前筛选包含未完成或 schema 中不存在的条件，完成编辑后才会查询。"),
    ).toBeInTheDocument();
    expect(captured.taskQueries).toHaveLength(1);
    expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled();
  });

  it("rejects a deeply nested URL filter before querying", async () => {
    const captured = installDataManagerApi();
    let filter: Record<string, unknown> = {
      field: "task.status",
      op: "eq",
      value: "pending",
    };
    for (let index = 0; index < 1000; index += 1) {
      filter = { op: "and", rules: [filter] };
    }
    const search = updateDataManagerUrl("", {
      lens: "tasks",
      view: "builtin:all",
      query: "",
      filter,
      sort: [{ field: "task.created_at", direction: "asc" }],
      columns: ["display_id"],
      selected: null,
    }).toString();

    renderDataManager(`/projects/p1/data-manager?${search}`);

    await waitFor(() => expect(screen.getByText(/筛选条件嵌套超过/)).toBeInTheDocument());
    expect(captured.taskQueries).toHaveLength(0);
    expect(screen.getByRole("button", { name: "刷新" })).toBeDisabled();
  });
});

describe("ProjectDataManagerPage project permissions", () => {
  it("shows the members section only when performance.read is granted", async () => {
    installDataManagerApi({
      capabilities: [...READ_CAPABILITIES, "performance.read"],
    });

    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all");

    expect(await screen.findByText("T-1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /成员绩效/ })).toBeInTheDocument();
  });

  it("drops an unauthorized members section back to data and hides its entry", async () => {
    installDataManagerApi({ capabilities: READ_CAPABILITIES });

    renderDataManager("/projects/p1/data-manager?lens=tasks&section=members", <LocationProbe />);

    await waitFor(() => expect(currentSearch().get("section")).toBeNull());
    expect(screen.queryByRole("button", { name: /成员绩效/ })).not.toBeInTheDocument();
    expect(await screen.findByText("T-1")).toBeInTheDocument();
  });
});

describe("ProjectDataManagerPage failure and asynchronous state", () => {
  it("offers a retry action when the schema request fails", async () => {
    const captured = installDataManagerApi({ schemaStatus: 500 });
    const user = userEvent.setup();

    renderDataManager("/projects/p1/data-manager?lens=tasks");

    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载 Data Manager 筛选字段");
    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(captured.schemaRequests).toBe(2));
  });

  it("does not navigate after a create completes once the selected view changed", async () => {
    let releaseCreate!: () => void;
    const createdViewGate = new Promise<void>((resolve) => {
      releaseCreate = resolve;
    });
    const captured = installDataManagerApi({
      createdViewGate,
      createdView: createTaskView({ id: "v-created", name: "异步视图" }),
    });
    const user = userEvent.setup();

    renderDataManager(
      "/projects/p1/data-manager?lens=tasks",
      <>
        <LocationProbe />
        <ExternalViewProbe />
      </>,
    );
    expect(await screen.findByText("T-1")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "保存视图" }));
    const nameInput = screen.getByRole("textbox");
    await user.clear(nameInput);
    await user.type(nameInput, "异步视图");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(captured.createdViewPayloads).toHaveLength(1));

    fireEvent.click(screen.getByTestId("external-view"));
    await waitFor(() => expect(currentView()).toBe("saved:v1"));

    await act(async () => {
      releaseCreate();
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(currentView()).toBe("saved:v1");
  });

  it("maintains task actions bubble container styling consistently across selection states to prevent layout shift", async () => {
    installDataManagerApi({
      tasks: [
        createDataManagerTask({
          id: "task-bubble-1",
          display_id: "T-bubble-1",
        }),
      ],
    });

    renderDataManager("/projects/p1/data-manager?lens=tasks&view=builtin:all");
    expect(await screen.findByText("T-bubble-1")).toBeInTheDocument();

    const actionsContainer = document.querySelector("[data-dm-task-actions]")!;
    expect(actionsContainer).toBeInTheDocument();
    expect(actionsContainer).toHaveClass("rounded-lg", "border", "p-2.5");
    expect(screen.getByText("可勾选任务进行批量操作")).toBeInTheDocument();

    const checkbox = screen.getByRole("checkbox", { name: "选择任务 T-bubble-1" });
    fireEvent.click(checkbox);

    expect(await screen.findByText(/已选择 1 \/ 200 个任务/)).toBeInTheDocument();
    expect(actionsContainer).toHaveClass("rounded-lg", "border", "p-2.5");
    const clearBtn = screen.getByRole("button", { name: "清除任务选择" });
    expect(clearBtn).not.toHaveClass("invisible");

    fireEvent.click(clearBtn);
    expect(await screen.findByText("可勾选任务进行批量操作")).toBeInTheDocument();
    expect(actionsContainer).toHaveClass("rounded-lg", "border", "p-2.5");
  });
});
