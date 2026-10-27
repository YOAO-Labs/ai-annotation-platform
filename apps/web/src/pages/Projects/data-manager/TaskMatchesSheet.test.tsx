import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

const useMatches = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/useTaskViews", () => ({
  useDataManagerMatches: (...args: unknown[]) => useMatches(...args),
}));
vi.mock("./DataManagerAnnotationPreview", () => ({ default: () => null }));
import { TaskMatchesSheet } from "./TaskMatchesSheet";

const items = Array.from({ length: 45 }, (_, index) => ({
  id: `match-${index}`,
  entity_kind: "annotation",
  class_name: `匹配对象 ${index}`,
  attributes: {},
}));

describe("matching entity pages", () => {
  it("requests each offset and resets paging before querying another task", () => {
    useMatches.mockImplementation((_project, _task, _filter, _open, pagination) => ({
      data: {
        items: items.slice(pagination.offset, pagination.offset + pagination.limit),
        total: 45,
      },
      isFetching: false,
    }));
    const props = {
      projectId: "p1",
      task: { id: "task-a", display_id: "T-A", file_name: "a.jpg" },
      filterJson: {},
      open: true,
      onOpenChange: vi.fn(),
    };
    const view = render(
      <MemoryRouter>
        <TaskMatchesSheet {...props} />
      </MemoryRouter>,
    );
    expect(screen.getByText("匹配对象 0")).toBeInTheDocument();
    expect(screen.queryByText("匹配对象 20")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(useMatches).toHaveBeenLastCalledWith("p1", "task-a", {}, true, {
      limit: 20,
      offset: 20,
    });
    expect(screen.getByText("匹配对象 20")).toBeInTheDocument();
    expect(screen.queryByText("匹配对象 0")).not.toBeInTheDocument();
    view.rerender(
      <MemoryRouter>
        <TaskMatchesSheet {...props} task={{ ...props.task, id: "task-b" }} />
      </MemoryRouter>,
    );
    expect(useMatches).toHaveBeenLastCalledWith("p1", "task-b", {}, true, { limit: 20, offset: 0 });
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
  });
});
