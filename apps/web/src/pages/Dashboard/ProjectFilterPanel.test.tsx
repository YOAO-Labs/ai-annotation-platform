import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProjectFilterPanel } from "./ProjectFilterPanel";
import { ProjectFilterSummary } from "./ProjectFilterSummary";
import { EMPTY_FILTERS, type DashboardFilters } from "./dashboardUrlState";

const users = vi.hoisted(() => ({
  data: [{ id: "alice", name: "Alice", role: "employee" }],
  isLoading: false,
  isError: false,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useUsers", () => ({ useUsers: () => users }));
vi.mock("@/hooks/useMediaQuery", () => ({ useMediaQuery: () => false }));
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (state: { user: null }) => unknown) => selector({ user: null }),
}));

function Harness({
  initial = EMPTY_FILTERS,
  onApply = vi.fn(),
}: {
  initial?: DashboardFilters;
  onApply?: (next: DashboardFilters) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <ProjectFilterPanel
      open={open}
      onOpenChange={setOpen}
      initial={initial}
      onApply={onApply}
      count={0}
    />
  );
}
beforeEach(() => {
  users.isError = false;
  users.isLoading = false;
});

describe("ProjectFilterPanel", () => {
  it("discards cancelled draft and applies only confirmed conditions", async () => {
    const user = userEvent.setup();
    const apply = vi.fn();
    render(<Harness onApply={apply} />);
    await user.click(screen.getByRole("button", { name: "筛选" }));
    expect(screen.getByRole("dialog", { name: "高级筛选" })).toHaveAttribute(
      "data-filter-panel",
      "popover",
    );
    await user.click(screen.getByRole("button", { name: "已完成" }));
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(apply).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "筛选" }));
    expect(screen.getByRole("button", { name: "已完成" })).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "进行中" }));
    await user.click(screen.getByRole("button", { name: "应用" }));
    expect(apply).toHaveBeenCalledOnce();
    expect(apply).toHaveBeenCalledWith(expect.objectContaining({ status: "in_progress" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("resets only the draft and refreshes it after external navigation", async () => {
    const user = userEvent.setup();
    const apply = vi.fn();
    const initial = { ...EMPTY_FILTERS, status: "completed" as const };
    const { rerender } = render(<Harness initial={initial} onApply={apply} />);
    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.click(screen.getByRole("button", { name: "重置" }));
    await user.keyboard("{Escape}");
    expect(apply).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "筛选" }));
    expect(screen.getByRole("button", { name: "已完成" })).toHaveAttribute("aria-pressed", "true");
    rerender(<Harness initial={{ ...EMPTY_FILTERS, status: "in_progress" }} onApply={apply} />);
    expect(screen.getByRole("button", { name: "进行中" })).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps selected members during option search and rejects reversed dates", async () => {
    const user = userEvent.setup();
    const apply = vi.fn();
    render(<Harness initial={{ ...EMPTY_FILTERS, member_id: "alice" }} onApply={apply} />);
    await user.click(screen.getByRole("button", { name: "筛选" }));
    await user.type(screen.getByRole("textbox", { name: "搜索成员" }), "missing");
    expect(screen.getByText("没有匹配成员")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("创建开始日期"), { target: { value: "2026-02-01" } });
    fireEvent.change(screen.getByLabelText("创建结束日期"), { target: { value: "2026-01-01" } });
    expect(screen.getByRole("button", { name: "应用" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("开始日期不能晚于结束日期");
    fireEvent.change(screen.getByLabelText("创建结束日期"), { target: { value: "2026-03-01" } });
    await user.click(screen.getByRole("button", { name: "应用" }));
    expect(apply).toHaveBeenCalledWith(expect.objectContaining({ member_id: "alice" }));
  });

  it("shows a member fetch error without dropping a saved selection", async () => {
    users.isError = true;
    render(<Harness initial={{ ...EMPTY_FILTERS, member_id: "saved-member" }} />);
    await userEvent.click(screen.getByRole("button", { name: "筛选" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("alert")).toHaveTextContent("成员加载失败");
    expect(within(dialog).queryByText("暂无成员")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "我参与的" })).toBeDisabled();
  });
});

it("labels applied conditions as a group and removes a type without clearing status", async () => {
  const change = vi.fn();
  render(
    <ProjectFilterSummary
      filters={{ ...EMPTY_FILTERS, status: "completed", data_type: ["image"] }}
      onChange={change}
      onEdit={vi.fn()}
    />,
  );
  expect(screen.getByRole("group", { name: "已应用的项目筛选" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "移除数据类型筛选" }));
  expect(change).toHaveBeenCalledWith(
    expect.objectContaining({ status: "completed", data_type: [] }),
  );
});
