import { useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ModelClassBindings } from "./ModelClassBindings";

afterEach(cleanup);

const names = Array.from({ length: 80 }, (_, index) => `类别${index}`);

function Editor({ disabled = false, submit = vi.fn() }) {
  const [bindings, setBindings] = useState<Record<number, string>>({ 0: "手动类别" });
  return (
    <form onSubmit={submit}>
      <ModelClassBindings
        nativeClasses={names}
        projectClasses={[...names, "手动类别"]}
        bindings={bindings}
        disabled={disabled}
        onChange={setBindings}
      />
      <button type="submit">提交</button>
      <output aria-label="绑定结果">{JSON.stringify(bindings)}</output>
    </form>
  );
}

it("pages and searches large class lists while preserving original indices and prior choices", async () => {
  const user = userEvent.setup();
  render(<Editor />);
  expect(screen.getAllByRole("combobox")).toHaveLength(10);
  expect(screen.getByText("已绑定 1 / 80")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "下一页类别" }));
  await user.selectOptions(screen.getByLabelText("原生类别 10 · 类别10"), "类别12");
  const search = screen.getByRole("searchbox", { name: "搜索类别绑定" });
  await user.type(search, "类别79");
  await user.selectOptions(screen.getByLabelText("原生类别 79 · 类别79"), "类别78");
  await user.clear(search);
  expect(screen.getByLabelText("原生类别 0 · 类别0")).toHaveValue("手动类别");
  await user.click(screen.getByRole("button", { name: "下一页类别" }));
  expect(screen.getByLabelText("原生类别 10 · 类别10")).toHaveValue("类别12");
  expect(JSON.parse(screen.getByLabelText("绑定结果").textContent!)).toEqual({
    0: "手动类别",
    10: "类别12",
    79: "类别78",
  });
  await user.type(search, "不存在");
  expect(screen.getByText("没有匹配的类别，可调整搜索或取消筛选")).toBeVisible();
});

it("explicitly fills all matching unbound classes across filters without replacing manual choices or submitting", async () => {
  const user = userEvent.setup();
  const submit = vi.fn((event) => event.preventDefault());
  render(<Editor submit={submit} />);
  await user.click(screen.getByRole("checkbox", { name: "仅未绑定（79）" }));
  const search = screen.getByRole("searchbox", { name: "搜索类别绑定" });
  await user.type(search, "类别79{Enter}");
  expect(submit).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "按同名绑定" }));
  expect(submit).not.toHaveBeenCalled();
  expect(screen.getByText("已绑定 80 / 80")).toBeVisible();
  expect(screen.getByText("全部类别已绑定")).toBeVisible();
  expect(JSON.parse(screen.getByLabelText("绑定结果").textContent!)).toEqual(
    Object.fromEntries(names.map((name, index) => [index, index === 0 ? "手动类别" : name])),
  );
});

it("disables all binding mutations while registration is pending", () => {
  render(<Editor disabled />);
  expect(screen.getByRole("button", { name: "按同名绑定" })).toBeDisabled();
  screen.getAllByRole("combobox").forEach((select) => expect(select).toBeDisabled());
  expect(screen.getByRole("button", { name: "清除原生类别 0 · 类别0 的绑定" })).toBeDisabled();
});

it("clears a binding explicitly without submitting or changing other rows", async () => {
  const user = userEvent.setup();
  const submit = vi.fn((event) => event.preventDefault());
  render(<Editor submit={submit} />);
  await user.selectOptions(screen.getByLabelText("原生类别 1 · 类别1"), "类别1");
  await user.click(screen.getByRole("button", { name: "清除原生类别 0 · 类别0 的绑定" }));
  expect(screen.getByLabelText("原生类别 0 · 类别0")).toHaveValue("");
  expect(JSON.parse(screen.getByLabelText("绑定结果").textContent!)).toEqual({ 0: "", 1: "类别1" });
  expect(submit).not.toHaveBeenCalled();
});

it("allows clearing a stored binding whose project class no longer exists", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  render(
    <ModelClassBindings
      nativeClasses={["car"]}
      projectClasses={[]}
      bindings={{ 0: "removed" }}
      disabled={false}
      onChange={onChange}
    />,
  );
  expect(screen.getByRole("combobox")).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "清除原生类别 0 · car 的绑定" }));
  expect(onChange).toHaveBeenCalledWith({ 0: "" });
});
