import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FilterGroup, FilterSelect, FilterToggle } from "./FilterControls";
import { ActiveFilterChip } from "./ActiveFilterChip";

describe("inline filter controls", () => {
  it("labels the group with one funnel and exposes pressed filter options", async () => {
    const onClick = vi.fn();
    render(
      <FilterGroup label="任务状态" compact>
        <FilterToggle active onClick={onClick}>
          全部
        </FilterToggle>
        <FilterToggle active={false} disabled>
          已完成
        </FilterToggle>
      </FilterGroup>,
    );
    const group = screen.getByRole("group", { name: "任务状态" });
    expect(group.querySelectorAll(".lucide-funnel")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "全部" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "全部" }));
    expect(onClick).toHaveBeenCalledOnce();
    expect(screen.getByRole("button", { name: "已完成" })).toBeDisabled();
  });
  it("can drop the decorative funnel label while keeping the group name", () => {
    render(
      <FilterGroup label="通知类型筛选" hideLabel compact>
        <FilterToggle active={false} onClick={vi.fn()}>
          全部
        </FilterToggle>
      </FilterGroup>,
    );
    const group = screen.getByRole("group", { name: "通知类型筛选" });
    expect(group.querySelectorAll(".lucide-funnel")).toHaveLength(0);
    expect(screen.queryByText("通知类型筛选")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "全部" })).toBeInTheDocument();
  });
  it("keeps native empty/default values and change events", async () => {
    const onChange = vi.fn();
    render(
      <FilterSelect aria-label="角色筛选" defaultValue="" onChange={onChange}>
        <option value="">全部角色</option>
        <option value="viewer">查看者</option>
      </FilterSelect>,
    );
    const select = screen.getByRole("combobox", { name: "角色筛选" });
    expect(select).toHaveValue("");
    await userEvent.selectOptions(select, "viewer");
    expect(onChange).toHaveBeenCalledOnce();
    expect(select).toHaveValue("viewer");
  });
  it("removes a condition without opening its editor or nesting buttons", async () => {
    const edit = vi.fn();
    const remove = vi.fn();
    render(<ActiveFilterChip label="状态" value="进行中" onClick={edit} onRemove={remove} />);
    await userEvent.click(screen.getByRole("button", { name: "移除状态筛选" }));
    expect(remove).toHaveBeenCalledOnce();
    expect(edit).not.toHaveBeenCalled();
    expect(document.querySelector("button button")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "状态：进行中" }));
    expect(edit).toHaveBeenCalledOnce();
  });
  it("describes an invalid condition in text rather than only by color", () => {
    render(<ActiveFilterChip label="置信度" value="未完成" invalid onClick={vi.fn()} />);
    expect(screen.getByRole("button", { name: "置信度：未完成（条件无效）" })).not.toHaveAttribute(
      "aria-invalid",
    );
    expect(screen.getByText("无效")).toBeInTheDocument();
  });
});
