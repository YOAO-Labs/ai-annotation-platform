/**
 * FilterToolbar 单测 · 无效目录 URL 条件的提示与移除。
 *
 * plan §5：非法枚举给可见提示和移除入口，不静默保留非法参数。回归
 * （PR #103 Codex P2）：issue chip 此前没有 onRemove，共享链接里的非法
 * catalog_group / catalog_view 只能显示警告，用户没有任何清除入口，
 * 条件会一直留在 URL 里。
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { FilterToolbar } from "./FilterToolbar";
import type { UrlStateIssue } from "@/hooks/useUrlFilterState";

function makeProps(overrides: Partial<Parameters<typeof FilterToolbar>[0]> = {}) {
  return {
    facets: { tasks: ["detection"], families: [], infras: [], modalities: ["image"] },
    taskFilter: new Set<string>(),
    familyFilter: new Set<string>(),
    infraFilter: new Set<string>(),
    modalityFilter: new Set<string>(),
    onToggleTask: vi.fn(),
    onToggleFamily: vi.fn(),
    onToggleInfra: vi.fn(),
    onToggleModality: vi.fn(),
    hasActiveFilter: false,
    onClear: vi.fn(),
    issues: undefined as UrlStateIssue[] | undefined,
    onDismissIssue: vi.fn(),
    matchedCount: 0,
    totalCount: 0,
    categoryCount: null,
    ...overrides,
  };
}

describe("FilterToolbar · 无效目录条件 chip", () => {
  it("issue chip 带「无效」标记和移除按钮", () => {
    render(
      <FilterToolbar
        {...makeProps({
          issues: [
            { key: "catalog_group", message: "无效的目录分组" },
            { key: "catalog_view", message: "无效的目录显示方式" },
          ],
        })}
      />,
    );
    expect(screen.getByText("无效的目录分组")).toBeInTheDocument();
    expect(screen.getByText("无效的目录显示方式")).toBeInTheDocument();
    // ActiveFilterChip 的移除按钮 aria-label = `移除${label}筛选`。
    expect(screen.getByRole("button", { name: "移除无效的目录分组筛选" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "移除无效的目录显示方式筛选" })).toBeInTheDocument();
  });

  it("点击移除按钮回调对应 issue 的 key（由调用方回落默认枚举）", () => {
    const onDismissIssue = vi.fn();
    render(
      <FilterToolbar
        {...makeProps({
          issues: [
            { key: "catalog_group", message: "无效的目录分组" },
            { key: "catalog_view", message: "无效的目录显示方式" },
          ],
          onDismissIssue,
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "移除无效的目录分组筛选" }));
    expect(onDismissIssue).toHaveBeenCalledWith("catalog_group");
    fireEvent.click(screen.getByRole("button", { name: "移除无效的目录显示方式筛选" }));
    expect(onDismissIssue).toHaveBeenCalledWith("catalog_view");
    expect(onDismissIssue).toHaveBeenCalledTimes(2);
  });

  it("没有 issue 时不渲染 chip 区", () => {
    render(<FilterToolbar {...makeProps()} />);
    expect(screen.queryByText(/无效的目录/)).not.toBeInTheDocument();
  });
});
