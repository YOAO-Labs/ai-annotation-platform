import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PresetRow } from "./PresetRow";

afterEach(cleanup);

it("returns to a prompt when the backend/task preset bucket changes without applying another preset", () => {
  const first = { id: "preset-a", name: "后端 A 预设", values: { confidence: 0.5 }, createdAt: 1 };
  const second = { ...first, id: "preset-b", name: "后端 B 预设" };
  const onApply = vi.fn();
  const props = { onApply, onSave: vi.fn(), onRemove: vi.fn() };
  const view = render(<PresetRow presets={[first]} {...props} />);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: first.id } });
  expect(onApply).toHaveBeenCalledWith(first);
  view.rerender(<PresetRow presets={[second]} {...props} />);
  expect(screen.getByRole("combobox")).toHaveValue("");
  expect(screen.getByRole("combobox")).toHaveDisplayValue("选择预设…");
  expect(onApply).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByRole("combobox"), { target: { value: second.id } });
  expect(onApply).toHaveBeenLastCalledWith(second);
});
