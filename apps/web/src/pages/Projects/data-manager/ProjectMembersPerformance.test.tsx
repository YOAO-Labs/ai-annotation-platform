import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  ProjectMemberPerformance,
  ProjectMembersPerformanceResponse,
  ProjectMemberPerformanceDetail,
  ProjectMemberPerformanceEventsResponse,
} from "@/api/projectPerformance";
import { formatPerformanceDate, ProjectMembersPerformance } from "./ProjectMembersPerformance";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  detail: vi.fn(),
  events: vi.fn(),
  exportMembers: vi.fn(),
}));

vi.mock("@/hooks/useProjectPerformance", () => ({
  useProjectMembersPerformance: mocks.list,
  useProjectMemberPerformance: mocks.detail,
  useProjectMemberPerformanceEvents: mocks.events,
}));

vi.mock("@/api/projectPerformance", async () => {
  const actual = await vi.importActual<typeof import("@/api/projectPerformance")>(
    "@/api/projectPerformance",
  );
  return {
    ...actual,
    projectPerformanceApi: { ...actual.projectPerformanceApi, exportMembers: mocks.exportMembers },
  };
});

const metric = (
  value: number | null,
  unit: "tasks" | "objects" | "images" | "decisions" | "minutes" | "percent",
  extra: Record<string, number> = {},
) => ({
  value,
  unit,
  ...extra,
});

function member(overrides: Partial<ProjectMemberPerformance> = {}): ProjectMemberPerformance {
  return {
    user_id: "u1",
    name: "Ada Lovelace",
    email: "ada@example.com",
    project_role: "annotator",
    account_status: "active",
    is_owner: true,
    is_current_member: true,
    member_since: "2026-01-01T00:00:00Z",
    metrics: {
      submitted_tasks: metric(12, "tasks"),
      resubmissions: metric(1, "tasks"),
      approved_task_outcomes: metric(8, "tasks"),
      first_review_pass_rate: metric(66.7, "percent", { numerator: 2, denominator: 3 }),
      recorded_time_minutes: metric(90, "minutes"),
      current_backlog: metric(0, "tasks"),
      review_decisions: metric(4, "decisions"),
      approvals: metric(3, "decisions"),
      rejections: metric(1, "decisions"),
      reviewed_tasks: metric(4, "tasks"),
      recorded_review_minutes: metric(30, "minutes"),
      review_backlog: metric(2, "tasks"),
      contributed_tasks: metric(12, "tasks"),
      annotated_images: metric(9, "images"),
      retained_objects: metric(42, "objects"),
    },
    ...overrides,
  };
}

const listData: ProjectMembersPerformanceResponse = {
  scope: {
    from: "2026-09-08",
    to: "2026-09-15",
    timezone: "Asia/Shanghai",
    as_of: "2026-09-14T12:00:00Z",
  },
  coverage: { state: "partial", source: "task_events", detail: "legacy events" },
  project_totals: {
    submitted_tasks: metric(12, "tasks"),
    approved_task_outcomes: metric(8, "tasks"),
    first_review_pass_rate: metric(66.7, "percent", { numerator: 2, denominator: 3 }),
    recorded_time_minutes: metric(null, "minutes", {}),
    current_backlog: metric(2, "tasks"),
    review_decisions: metric(4, "decisions"),
    approvals: metric(3, "decisions"),
    rejections: metric(1, "decisions"),
    review_backlog: metric(2, "tasks"),
    annotated_images: metric(15, "images"),
    retained_objects: metric(60, "objects"),
  },
  items: [
    member(),
    member({
      user_id: "u2",
      name: "Grace Hopper",
      email: "grace@example.com",
      account_status: "inactive",
      is_owner: false,
      is_current_member: false,
      metrics: {
        submitted_tasks: metric(0, "tasks"),
        resubmissions: metric(0, "tasks"),
        approved_task_outcomes: metric(0, "tasks"),
        first_review_pass_rate: metric(null, "percent"),
        recorded_time_minutes: metric(null, "minutes"),
        current_backlog: metric(0, "tasks"),
        review_decisions: metric(0, "decisions"),
        approvals: metric(0, "decisions"),
        rejections: metric(0, "decisions"),
        reviewed_tasks: metric(0, "tasks"),
        recorded_review_minutes: metric(null, "minutes"),
        review_backlog: metric(0, "tasks"),
        contributed_tasks: metric(0, "tasks"),
        annotated_images: metric(0, "images"),
        retained_objects: metric(0, "objects"),
      },
    }),
  ],
  next_cursor: null,
};

const detailData: ProjectMemberPerformanceDetail = {
  scope: listData.scope,
  coverage: listData.coverage,
  member: listData.items[0],
  trend: [
    { date: "2026-09-08", submitted_tasks: 2, approved_task_outcomes: 1, review_decisions: 0 },
  ],
  reject_reasons: [{ reason_type: "class_error", count: 1, pct: 100 }],
  class_distribution: [{ class_name: "car", count: 8, pct: 100 }],
  source_distribution: [{ source: "manual", count: 8, pct: 100 }],
  geometry_distribution: [{ annotation_type: "rectangle", count: 8, pct: 100 }],
  evidence: [],
  evidence_next_cursor: null,
};

const eventsData: ProjectMemberPerformanceEventsResponse = {
  scope: listData.scope,
  items: [],
  next_cursor: null,
};

function NavigationProbe() {
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <output data-testid="location">
        {location.pathname}
        {location.search}
      </output>
      <button data-testid="history-back" onClick={() => navigate(-1)}>
        Back
      </button>
      <button data-testid="history-forward" onClick={() => navigate(1)}>
        Forward
      </button>
    </>
  );
}

function SortNavigationProbe() {
  const navigate = useNavigate();
  return (
    <button
      data-testid="sort-navigation"
      onClick={() =>
        navigate(
          "/projects/p1/data-manager?section=members&members_sort=submitted_tasks&members_direction=desc&members_selected=u1",
        )
      }
    >
      Sort through URL
    </button>
  );
}

function renderPage(initial = "/projects/p1/data-manager?section=members") {
  return render(
    <MemoryRouter initialEntries={[initial]}>
      <ProjectMembersPerformance projectId="p1" />
      <NavigationProbe />
    </MemoryRouter>,
  );
}

describe("ProjectMembersPerformance", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-14T12:00:00Z"));
    mocks.list.mockReturnValue({
      data: listData,
      isLoading: false,
      isFetching: false,
      isError: false,
      refetch: vi.fn(),
    });
    mocks.detail.mockReturnValue({ data: detailData, isLoading: false, isError: false });
    mocks.events.mockReturnValue({ data: eventsData, isLoading: false, isError: false });
    mocks.exportMembers.mockResolvedValue({ blob: new Blob(["ok"]), filename: "members.csv" });
  });
  afterEach(() => vi.useRealTimers());

  it("preserves calendar trend dates in negative-offset timezones", () => {
    expect(formatPerformanceDate("2026-09-08", "America/Los_Angeles")).toBe("09/08");
    expect(formatPerformanceDate("2026-09-08T00:00:00Z", "America/Los_Angeles")).toBe("09/07");
  });

  it("restores discrete member scope changes through browser history", () => {
    renderPage();
    fireEvent.change(screen.getByRole("combobox", { name: "工作类型" }), {
      target: { value: "review" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("history-back"));
    expect(screen.queryByRole("heading", { name: "Ada Lovelace" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "工作类型" })).toHaveValue("review");
    fireEvent.click(screen.getByTestId("history-back"));
    expect(screen.getByRole("combobox", { name: "工作类型" })).toHaveValue("annotation");
    fireEvent.click(screen.getByTestId("history-forward"));
    expect(screen.getByRole("combobox", { name: "工作类型" })).toHaveValue("review");
  });

  it("blocks future custom ranges from queries and export", () => {
    renderPage(
      "/projects/p1/data-manager?section=members&members_preset=custom&members_from=2026-09-13&members_to=2026-09-16&members_timezone=Asia%2FShanghai",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("结束日期不能晚于今天");
    expect(screen.getByRole("button", { name: "导出 CSV" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "应用范围" })).toBeDisabled();
    expect(mocks.list.mock.calls[mocks.list.mock.calls.length - 1]?.[2]).toBe(false);
  });

  it("opens evidence in the existing Workbench route with a return link", () => {
    const response = {
      ...eventsData,
      items: [{ id: "event-1", at: "2026-09-08T12:00:00Z", action: "提交任务", task_id: "task-1" }],
    };
    mocks.events.mockImplementation((_projectId: string, memberId: string | null) => ({
      data: memberId ? response : undefined,
      isLoading: false,
      isError: false,
    }));
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    fireEvent.click(screen.getByRole("button", { name: "查看任务" }));
    const location = new URL(screen.getByTestId("location").textContent!, "https://test.invalid");
    expect(location.pathname).toBe("/projects/p1/annotate");
    expect(location.searchParams.get("task")).toBe("task-1");
    expect(location.searchParams.get("returnTo")).toContain("section=members");
    expect(location.searchParams.get("returnTo")).toContain("members_selected=u1");
  });

  it("opens review evidence in the review Workbench route", () => {
    const response = {
      ...eventsData,
      items: [{ id: "event-1", at: "2026-09-08T12:00:00Z", action: "审核通过", task_id: "task-1" }],
    };
    mocks.events.mockImplementation((_projectId: string, memberId: string | null) => ({
      data: memberId ? response : undefined,
      isLoading: false,
      isError: false,
    }));
    renderPage("/projects/p1/data-manager?section=members&members_work_type=review");
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    fireEvent.click(screen.getByRole("button", { name: "查看任务" }));
    const location = new URL(screen.getByTestId("location").textContent!, "https://test.invalid");
    expect(location.pathname).toBe("/projects/p1/review");
    expect(location.searchParams.get("task")).toBe("task-1");
  });

  it("keeps zero activity separate from unavailable history", () => {
    renderPage();
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Grace Hopper")).toBeInTheDocument();
    expect(screen.getAllByText("0 个任务").length).toBeGreaterThan(0);
    expect(screen.getAllByText("不可用").length).toBeGreaterThan(0);
    expect(screen.getByText("历史贡献者")).toBeInTheDocument();
    expect(screen.getByText("停用")).toBeInTheDocument();
  });

  it("uses one exclusive current instant for preset queries", () => {
    renderPage();
    const query = mocks.list.mock.calls[mocks.list.mock.calls.length - 1]?.[1] as {
      from: string;
      to: string;
    };
    expect(query.from).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(query.to).toMatch(/^\d{4}-\d{2}-\d{2}T/);
    expect(query.to).not.toBe(query.from);
  });

  it("blocks data requests and export for an invalid custom URL range", () => {
    renderPage(
      "/projects/p1/data-manager?section=members&members_preset=custom&members_from=2026-02-30&members_to=2026-03-01",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("无法应用成员绩效筛选");
    expect(screen.getByRole("button", { name: "导出 CSV" })).toBeDisabled();
    expect(mocks.list.mock.calls[mocks.list.mock.calls.length - 1]?.[2]).toBe(false);
  });

  it("switches between annotation and review work without changing the member scope", () => {
    renderPage();
    fireEvent.change(screen.getByRole("combobox", { name: "工作类型" }), {
      target: { value: "review" },
    });
    expect(screen.getByRole("columnheader", { name: "审核决策" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "当前待审" })).toBeInTheDocument();
    expect(mocks.list).toHaveBeenCalled();
  });

  it("shows saved content before submission counts in the annotation view", () => {
    renderPage();
    // Project summary exposes the two saved-content totals.
    expect(screen.getAllByText("已标注图片").length).toBeGreaterThan(1);
    expect(screen.getAllByText("保留标注").length).toBeGreaterThan(1);
    expect(screen.getByText("15 张图片")).toBeInTheDocument();
    expect(screen.getByText("60 条标注")).toBeInTheDocument();
    // Member rows place the image/object columns before submission counts.
    const headerCells = screen.getAllByRole("columnheader").map((cell) => cell.textContent);
    expect(headerCells.indexOf("已标注图片")).toBeGreaterThan(0);
    expect(headerCells.indexOf("已标注图片")).toBeLessThan(headerCells.indexOf("提交任务"));
    expect(headerCells.indexOf("保留标注")).toBeLessThan(headerCells.indexOf("提交任务"));
    const adaRow = screen.getByRole("button", { name: /Ada Lovelace/ }).closest("tr");
    expect(adaRow).toHaveTextContent("9");
    expect(adaRow).toHaveTextContent("42");
    // Member detail shows the image count alongside retained content.
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    const retained = screen.getByLabelText("保留内容");
    expect(retained).toHaveTextContent("9 张");
    expect(retained).toHaveTextContent("12 个");
    expect(retained).toHaveTextContent("42 条");
  });

  it("opens the selected member detail sheet with evidence sections", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByRole("heading", { name: "Ada Lovelace" })).toBeInTheDocument();
    expect(screen.getByText("产出趋势")).toBeInTheDocument();
    expect(screen.getByText("退回原因")).toBeInTheDocument();
    expect(screen.getByText("保留内容")).toBeInTheDocument();
    expect(screen.getByText("人工")).toBeInTheDocument();
    expect(screen.getByText("活动依据")).toBeInTheDocument();
  });

  it("shows detail failures and allows retry", () => {
    const refetch = vi.fn();
    mocks.detail.mockReturnValue({ data: undefined, isLoading: false, isError: true, refetch });
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByText("成员详情加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(refetch).toHaveBeenCalled();
  });

  it.each(["account status", "historical roster", "search enter", "search debounce"])(
    "closes selected details and resets evidence when changing %s",
    async (filter) => {
      mocks.events.mockImplementation(
        (_projectId: string, memberId: string | null, query: { cursor: string | null }) => ({
          data: memberId
            ? {
                ...eventsData,
                items: [
                  {
                    id: query.cursor ?? "first",
                    at: "2026-09-09T01:00:00Z",
                    action: "提交任务",
                    detail: query.cursor ?? "first evidence",
                  },
                ],
                next_cursor: query.cursor ? null : "next-events",
              }
            : undefined,
          isLoading: false,
          isFetching: false,
          isError: false,
        }),
      );
      renderPage();
      fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
      fireEvent.click(screen.getByRole("button", { name: "加载更多依据" }));
      expect(screen.getByText("next-events")).toBeInTheDocument();
      if (filter === "account status") {
        fireEvent.change(screen.getByLabelText("账号状态"), { target: { value: "inactive" } });
      } else if (filter === "historical roster") {
        fireEvent.click(screen.getByLabelText("包含历史贡献者"));
      } else {
        const search = screen.getByPlaceholderText("姓名或邮箱");
        fireEvent.change(search, { target: { value: "Grace" } });
        if (filter === "search enter") fireEvent.keyDown(search, { key: "Enter" });
      }
      await waitFor(() =>
        expect(screen.queryByRole("heading", { name: "Ada Lovelace" })).not.toBeInTheDocument(),
      );
      expect(
        new URLSearchParams(screen.getByTestId("location").textContent!.split("?")[1]).get(
          "members_selected",
        ),
      ).toBeNull();
      // 关闭详情的渲染与 setEventsCursor(null) 的重渲染可能不在同一次 act 内
      // 完成;等待最终调用收敛到重置后的参数,避免断言落在中间渲染上。
      await waitFor(() =>
        expect(mocks.events).toHaveBeenLastCalledWith(
          "p1",
          null,
          expect.objectContaining({ cursor: null }),
          false,
        ),
      );
    },
  );

  it("keeps accumulated evidence and shows progress while the next page is loading", () => {
    mocks.events.mockImplementation(
      (_projectId: string, memberId: string | null, query: { cursor: string | null }) => ({
        data:
          memberId && !query.cursor
            ? {
                ...eventsData,
                items: [
                  {
                    id: "first",
                    at: "2026-09-09T01:00:00Z",
                    action: "提交任务",
                    detail: "visible evidence",
                  },
                ],
                next_cursor: "next-events",
              }
            : undefined,
        isLoading: Boolean(query.cursor),
        isFetching: Boolean(query.cursor),
        isError: false,
      }),
    );
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByText("visible evidence")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多依据" }));
    expect(screen.getByText("visible evidence")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加载中…" })).toBeDisabled();
  });

  it("appends paginated evidence instead of widening its scope", () => {
    const first = {
      id: "event-1",
      at: "2026-09-09T01:00:00Z",
      action: "提交任务",
      task_id: null,
      task_display_id: "T-1",
      detail: "first",
    };
    const second = {
      id: "event-2",
      at: "2026-09-10T01:00:00Z",
      action: "审核通过",
      task_id: null,
      task_display_id: "T-2",
      detail: "second",
    };
    mocks.events.mockImplementation(
      (_projectId: string, _userId: string | null, query: { cursor: string | null }) => ({
        data: query.cursor
          ? { ...eventsData, items: [second], next_cursor: null }
          : { ...eventsData, items: [first], next_cursor: "next-events" },
        isLoading: false,
        isFetching: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByText("first")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多依据" }));
    expect(screen.getByText("second")).toBeInTheDocument();
  });

  it("starts evidence pagination over when the member sort changes", async () => {
    const first = {
      id: "event-first",
      at: "2026-09-09T01:00:00Z",
      action: "提交任务",
      task_id: null,
      task_display_id: "T-first",
      detail: "first sort",
    };
    const second = {
      id: "event-second",
      at: "2026-09-10T01:00:00Z",
      action: "审核通过",
      task_id: null,
      task_display_id: "T-second",
      detail: "second sort",
    };
    const sortedFirst = {
      id: "event-sorted-first",
      at: "2026-09-11T01:00:00Z",
      action: "审核通过",
      task_id: null,
      task_display_id: "T-sorted",
      detail: "sorted first",
    };
    mocks.events.mockImplementation(
      (
        _projectId: string,
        _userId: string | null,
        query: { cursor: string | null; sort?: string },
      ) => ({
        data: query.cursor
          ? { ...eventsData, items: [second], next_cursor: null }
          : {
              ...eventsData,
              items: [query.sort === "-submitted_tasks" ? sortedFirst : first],
              next_cursor: "next-events",
            },
        isLoading: false,
        isFetching: false,
        isError: false,
        refetch: vi.fn(),
      }),
    );
    render(
      <MemoryRouter initialEntries={["/projects/p1/data-manager?section=members"]}>
        <ProjectMembersPerformance projectId="p1" />
        <NavigationProbe />
        <SortNavigationProbe />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: /Ada Lovelace/ }));
    expect(screen.getByText("first sort")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多依据" }));
    expect(screen.getByText("second sort")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("sort-navigation"));
    await waitFor(() => expect(screen.getByText("sorted first")).toBeInTheDocument());
    expect(screen.queryByText("first sort")).not.toBeInTheDocument();
    expect(screen.queryByText("second sort")).not.toBeInTheDocument();
  });
});
