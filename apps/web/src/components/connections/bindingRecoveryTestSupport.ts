import { fireEvent, within } from "@testing-library/react";
import { expect } from "vitest";

/** Exercise the common recovery path while callers supply purpose-specific errors and candidates. */
export async function expectDeletedTargetRecovery(
  dialog: HTMLElement,
  targetId: string,
  refreshCandidates: () => void,
) {
  fireEvent.change(within(dialog).getByRole("combobox", { name: "目标存储位置" }), {
    target: { value: targetId },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "验证并预览" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("目标位置已不存在");
  const select = within(dialog).getByRole("combobox", { name: "目标存储位置" });
  expect(select).toHaveValue("");
  expect(select).toBeEnabled();
  expect(within(dialog).queryByText("存储管理权限已变化，请刷新后重试。")).not.toBeInTheDocument();
  refreshCandidates();
  fireEvent.click(within(dialog).getByRole("button", { name: "刷新位置" }));
  await within(dialog).findByRole("option", { name: "另一位置" });
  fireEvent.change(select, { target: { value: "other" } });
  expect(within(dialog).getByRole("button", { name: "验证并预览" })).toBeEnabled();
}
