/**
 * SkipTaskModal 单测：
 * - 默认选中 image_corrupt → 提交按钮可用 → onConfirm 触发
 * - 选中 other 时 textarea 出现，note 空时禁用提交
 * - 选中 other 后填 note → 提交透传 (other, note)
 */
import { describe, it, expect, vi } from "vitest";
import { render, fireEvent } from "@testing-library/react";
import { SkipTaskModal } from "./SkipTaskModal";

describe("SkipTaskModal", () => {
  it("默认 image_corrupt 可直接提交", () => {
    const onConfirm = vi.fn();
    const { getByTestId } = render(<SkipTaskModal open onClose={() => {}} onConfirm={onConfirm} />);
    const confirm = getByTestId("skip-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("image_corrupt", undefined);
  });

  it("选 other 不填 note 时提交禁用", () => {
    const onConfirm = vi.fn();
    const { getByTestId } = render(<SkipTaskModal open onClose={() => {}} onConfirm={onConfirm} />);
    // 选 other（点击 label 内的 radio input）
    const otherInput = getByTestId("skip-reason-other").querySelector("input")!;
    fireEvent.click(otherInput);
    const confirm = getByTestId("skip-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it("选 other 填 note 后提交透传 (other, note)", () => {
    const onConfirm = vi.fn();
    const { getByTestId } = render(<SkipTaskModal open onClose={() => {}} onConfirm={onConfirm} />);
    const otherInput = getByTestId("skip-reason-other").querySelector("input")!;
    fireEvent.click(otherInput);
    const note = getByTestId("skip-reason-note") as HTMLTextAreaElement;
    fireEvent.change(note, { target: { value: "颜色异常" } });
    const confirm = getByTestId("skip-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("other", "颜色异常");
  });

  it("isSubmitting 时禁用确认按钮", () => {
    const { getByTestId } = render(
      <SkipTaskModal open isSubmitting onClose={() => {}} onConfirm={() => {}} />,
    );
    const confirm = getByTestId("skip-confirm") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    expect(confirm.textContent).toMatch(/提交中/);
  });
});
