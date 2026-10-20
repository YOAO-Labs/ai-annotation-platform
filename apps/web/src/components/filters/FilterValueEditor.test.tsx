import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { DataManagerFilterField } from "@/api/taskViews";
import { FilterValueEditor } from "./FilterValueEditor";

const numberField: DataManagerFilterField = {
  key: "count",
  label: "数量",
  group: "测试",
  value_type: "number",
  operators: ["eq", "between"],
  options: [],
  expensive: false,
  tool_unit_id: null,
  attribute_key: null,
};

const enumField: DataManagerFilterField = {
  key: "status",
  label: "状态",
  group: "测试",
  value_type: "select",
  operators: ["in"],
  options: [
    { value: "pending", label: "待处理" },
    { value: "review", label: "待审核" },
  ],
  expensive: false,
  tool_unit_id: null,
  attribute_key: null,
};

const orphanEnumField: DataManagerFilterField = {
  ...enumField,
  options: [{ value: "pending", label: "待处理" }],
};

const booleanField: DataManagerFilterField = {
  ...numberField,
  key: "enabled",
  label: "启用",
  value_type: "boolean",
  operators: ["eq"],
};

describe("FilterValueEditor", () => {
  it("keeps an incomplete numeric draft out of the applied callback", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(
      <FilterValueEditor field={numberField} operator="eq" appliedValue={1} onCommit={onCommit} />,
    );
    const input = screen.getByRole("textbox", { name: "条件值" });
    await user.clear(input);
    await user.type(input, "-");
    await user.tab();
    expect(onCommit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("完整数字");
  });

  it("reports draft validity and clears it when the editor unmounts", async () => {
    const user = userEvent.setup();
    const validity = vi.fn();
    const view = render(
      <FilterValueEditor
        field={numberField}
        operator="eq"
        appliedValue={1}
        editorId="filter:count"
        onCommit={vi.fn()}
        onDraftValidityChange={validity}
      />,
    );
    expect(validity).toHaveBeenLastCalledWith(true, "filter:count");
    const input = screen.getByRole("textbox", { name: "条件值" });
    await user.clear(input);
    await user.type(input, "-");
    expect(validity).toHaveBeenLastCalledWith(false, "filter:count");
    await user.clear(input);
    await user.type(input, "2");
    expect(validity).toHaveBeenLastCalledWith(true, "filter:count");
    view.unmount();
    expect(validity).toHaveBeenLastCalledWith(true, "filter:count");
  });

  it("renders enum IN as multi-select and commits selected values", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(
      <FilterValueEditor field={enumField} operator="in" appliedValue={[]} onCommit={onCommit} />,
    );
    await user.click(screen.getByRole("checkbox", { name: "待处理" }));
    expect(onCommit).toHaveBeenCalledWith(["pending"]);
    await user.click(screen.getByRole("checkbox", { name: "待审核" }));
    expect(onCommit).toHaveBeenLastCalledWith(["pending", "review"]);
  });

  it("labels both endpoints for a between draft", () => {
    render(
      <FilterValueEditor
        field={numberField}
        operator="between"
        appliedValue={[1, 5]}
        onCommit={vi.fn()}
      />,
    );
    expect(screen.getByRole("textbox", { name: "起始值" })).toHaveValue("1");
    expect(screen.getByRole("textbox", { name: "结束值" })).toHaveValue("5");
  });

  it("shows an explicit saved null without rewriting it as an empty draft", () => {
    render(
      <FilterValueEditor
        field={numberField}
        operator="eq"
        appliedValue={null}
        onCommit={vi.fn()}
      />,
    );
    expect(screen.getByRole("textbox", { name: "条件值" })).toHaveValue("空值（已保存）");
    expect(screen.getByRole("button", { name: "替换" })).toBeInTheDocument();
  });

  it("keeps empty range slots independent and does not shift the end into the start", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<FilterValueEditor field={numberField} operator="between" onCommit={onCommit} />);
    await user.type(screen.getByRole("textbox", { name: "结束值" }), "5");
    expect(screen.getByRole("textbox", { name: "起始值" })).toHaveValue("");
    expect(screen.getByRole("textbox", { name: "结束值" })).toHaveValue("5");
    await user.clear(screen.getByRole("textbox", { name: "结束值" }));
    expect(screen.getByRole("textbox", { name: "起始值" })).toHaveValue("");
  });

  it("keeps a restored orphan enum value selectable for replacement", () => {
    render(
      <FilterValueEditor
        field={orphanEnumField}
        operator="eq"
        appliedValue="retired"
        onCommit={vi.fn()}
      />,
    );
    expect(screen.getByRole("option", { name: "retired（已保存）" })).toBeInTheDocument();
  });

  it("uses an explicit empty boolean option for a new rule", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<FilterValueEditor field={booleanField} operator="eq" onCommit={onCommit} />);
    const prompt = screen.getByRole("combobox", { name: "条件值" }).querySelector("option");
    expect(prompt).toBeDisabled();
    expect(prompt).toHaveAttribute("hidden");
    expect(screen.queryByRole("option", { name: "请选择" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "条件值" })).toHaveValue("");
    expect(onCommit).not.toHaveBeenCalled();
    await user.selectOptions(screen.getByRole("combobox", { name: "条件值" }), "true");
    expect(onCommit).toHaveBeenCalledWith(true);
  });
});
