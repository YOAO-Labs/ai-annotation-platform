import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ListPagination, listPageSize } from "./ListPagination";
import { useListPagination } from "@/hooks/useListPagination";

afterEach(cleanup);

function Collection({ count = 65, filter = "" }: { count?: number; filter?: string }) {
  const pagination = useListPagination(
    Array.from({ length: count }, (_, index) => index),
    filter,
  );
  return (
    <>
      <ul aria-label="当前页记录">
        {pagination.items.map((item) => (
          <li key={item}>记录 {item + 1}</li>
        ))}
      </ul>
      <ListPagination
        label="记录"
        page={pagination.page}
        pageSize={pagination.pageSize}
        total={pagination.total}
        onPageChange={pagination.setPage}
        onPageSizeChange={pagination.setPageSize}
      />
    </>
  );
}

describe("shared list pagination", () => {
  it("pages the filtered collection and resets to page one when its size or filter changes", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Collection />);
    const rows = () =>
      within(screen.getByRole("list", { name: "当前页记录" })).getAllByRole("listitem");
    expect(rows()).toHaveLength(20);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(rows()[0]).toHaveTextContent("记录 21");
    await user.selectOptions(screen.getByRole("combobox", { name: "每页记录数" }), "50");
    expect(rows()).toHaveLength(50);
    expect(rows()[0]).toHaveTextContent("记录 1");
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(rows()).toHaveLength(15);
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    rerender(<Collection filter="new" />);
    expect(rows()[0]).toHaveTextContent("记录 1");
  });

  it("clamps a removed last page and keeps empty-list controls visible", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<Collection count={21} />);
    await user.click(screen.getByRole("button", { name: "下一页" }));
    rerender(<Collection count={20} />);
    expect(screen.getByRole("status")).toHaveTextContent("共 20 条 · 第 1 / 1 页");
    rerender(<Collection count={0} />);
    expect(screen.getByRole("status")).toHaveTextContent("共 0 条 · 第 1 / 1 页");
    expect(screen.getByRole("combobox")).toBeEnabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });

  it("supports cursor pages without inventing totals and disables requests while loading", async () => {
    const user = userEvent.setup();
    const next = vi.fn();
    const changeSize = vi.fn();
    const props = {
      label: "游标记录",
      page: 1,
      pageSize: 20,
      count: 20,
      hasNext: true,
      onPageChange: next,
      onPageSizeChange: changeSize,
    };
    const { rerender } = render(<ListPagination {...props} />);
    expect(screen.getByRole("status")).toHaveTextContent("第 2 页 · 本页 20 条");
    expect(screen.getByRole("status")).not.toHaveTextContent("共");
    await user.click(screen.getByRole("button", { name: "上一页" }));
    expect(next).toHaveBeenLastCalledWith(0);
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(next).toHaveBeenLastCalledWith(2);
    rerender(<ListPagination {...props} loading />);
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    rerender(<ListPagination {...props} total={0} loading />);
    expect(screen.getByRole("status")).toHaveTextContent("第 2 页");
    expect(screen.getByRole("status")).not.toHaveTextContent("第 2 / 1 页");
  });

  it("accepts only the shared page sizes from URLs", () => {
    for (const value of [10, "20", 50, "100"]) expect(listPageSize(value)).toBe(Number(value));
    for (const value of [null, "", "-1", "0", "25", "invalid"])
      expect(listPageSize(value)).toBe(20);
  });
});
