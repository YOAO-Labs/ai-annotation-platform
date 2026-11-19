// 共享设置控件:按注册表 control 类型渲染、锁定禁用 + badge、提交回调。
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SettingsFieldControl } from "./SettingsFieldControl";
import type { WorkbenchSettingField } from "../state/workbenchSettingsFields";

const toggleField: WorkbenchSettingField = {
  key: "image.smoothImage",
  section: "imageDisplay",
  category: "image",
  label: "图像平滑",
  control: { type: "toggle", onText: "开", offText: "关" },
  lockable: true,
};

const sliderField: WorkbenchSettingField = {
  key: "image.controlPointsSize",
  section: "drawing",
  category: "image",
  label: "控制点大小",
  description: "顶点拖拽手柄半径",
  control: { type: "slider", min: 2, max: 20, step: 1, format: (v) => `${v}px` },
};

const selectField: WorkbenchSettingField = {
  key: "video.demo",
  section: "playback",
  category: "video",
  label: "演示",
  control: {
    type: "select",
    options: [
      { value: "a", label: "A" },
      { value: "b", label: "B" },
    ],
  },
};

const numericSelectField: WorkbenchSettingField = {
  key: "image.zoomStepFactor",
  section: "imageDisplay",
  category: "image",
  label: "滚轮缩放步长",
  control: {
    type: "select",
    options: [
      { value: 1.05, label: "1.05x" },
      { value: 1.1, label: "1.10x" },
    ],
  },
};

const textField: WorkbenchSettingField = {
  key: "image.cssImageFilter",
  section: "imageDisplay",
  category: "image",
  label: "CSS 图像滤镜",
  control: { type: "text", maxLength: 255, placeholder: "brightness(1.2)" },
};

const multiselectField: WorkbenchSettingField = {
  key: "common.labelContent",
  section: "appearance",
  category: "common",
  label: "标签内容",
  control: {
    type: "multiselect",
    min: 1,
    options: [
      { value: "class", label: "类别名" },
      { value: "id", label: "ID" },
      { value: "score", label: "置信度" },
    ],
  },
};

describe("SettingsFieldControl", () => {
  it("toggle:渲染 switch,切换触发 onCommit(boolean)", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={toggleField} value={true} onCommit={onCommit} />);
    const box = screen.getByRole("switch");
    fireEvent.click(box);
    expect(onCommit).toHaveBeenCalledWith(false);
  });

  it("slider:label 含格式化值和说明 tooltip,松手提交 onCommit(number)", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={sliderField} value={6} onCommit={onCommit} />);
    expect(screen.getByText(/6px/)).toBeTruthy();
    expect(screen.queryByText(/顶点拖拽手柄半径/)).toBeNull();
    expect(screen.getByLabelText("顶点拖拽手柄半径")).toBeTruthy();
    fireEvent.change(screen.getByRole("slider"), { target: { value: "12" } });
    expect(onCommit).not.toHaveBeenCalled();
    // 拖动中(未松手)数字实时跟随,不冻结在旧值 6px。
    expect(screen.getByText(/12px/)).toBeTruthy();
    expect(screen.queryByText(/6px/)).toBeNull();
    fireEvent.pointerUp(screen.getByRole("slider"));
    expect(onCommit).toHaveBeenCalledWith(12);
  });

  it("select:渲染 options,切换触发 onCommit(string)", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={selectField} value="a" onCommit={onCommit} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "b" } });
    expect(onCommit).toHaveBeenCalledWith("b");
  });

  it("select:数字 option 保持 number 类型提交", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={numericSelectField} value={1.05} onCommit={onCommit} />);
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "1.1" } });
    expect(onCommit).toHaveBeenCalledWith(1.1);
  });

  it("text:blur 时 trim 提交,值未变不提交", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={textField} value="" onCommit={onCommit} />);
    const input = screen.getByPlaceholderText("brightness(1.2)");
    fireEvent.blur(input);
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: " invert(1) " } });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledWith("invert(1)");
  });

  it("multiselect:勾选未选项追加、按 options 顺序提交 string[]", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={multiselectField} value={["class"]} onCommit={onCommit} />);
    fireEvent.click(screen.getByRole("button", { name: "置信度" }));
    // 按 options 顺序重建:class 在 score 之前。
    expect(onCommit).toHaveBeenCalledWith(["class", "score"]);
  });

  it("multiselect:取消已选项移除;min 兜底下最后一项禁用不可取消", () => {
    const onCommit = vi.fn();
    const { rerender } = render(
      <SettingsFieldControl
        field={multiselectField}
        value={["class", "score"]}
        onCommit={onCommit}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "置信度" }));
    expect(onCommit).toHaveBeenCalledWith(["class"]);
    // 只剩 class 时,class chip 触底禁用(min:1),点击不提交。
    onCommit.mockClear();
    rerender(
      <SettingsFieldControl field={multiselectField} value={["class"]} onCommit={onCommit} />,
    );
    const classChip = screen.getByRole("button", { name: "类别名" }) as HTMLButtonElement;
    expect(classChip.disabled).toBe(true);
    fireEvent.click(classChip);
    expect(onCommit).not.toHaveBeenCalled();
  });

  it("locked:控件禁用 + 「项目锁定」badge", () => {
    render(<SettingsFieldControl field={toggleField} value={true} locked onCommit={vi.fn()} />);
    expect((screen.getByRole("switch") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("项目锁定")).toBeTruthy();
  });
});

describe("settings layout commit boundaries", () => {
  it("shows a readable description and a separately named numeric input", () => {
    render(
      <SettingsFieldControl layout="settings" field={sliderField} value={6} onCommit={vi.fn()} />,
    );
    expect(screen.getByText("顶点拖拽手柄半径")).toBeVisible();
    expect(screen.getByRole("slider", { name: "控制点大小" })).toHaveAccessibleDescription(
      "顶点拖拽手柄半径",
    );
  });
  it("Enter and subsequent blur only commit once, and composition Enter does not commit", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={textField} value="" onCommit={onCommit} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: " invert(1) " } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledTimes(1);
    expect(onCommit).toHaveBeenCalledWith("invert(1)");
  });
  it("slider pointerup and blur deduplicate, keyboard changes still commit", () => {
    const onCommit = vi.fn();
    render(<SettingsFieldControl field={sliderField} value={6} onCommit={onCommit} />);
    const input = screen.getByRole("slider");
    fireEvent.change(input, { target: { value: "12" } });
    fireEvent.pointerUp(input);
    fireEvent.blur(input);
    expect(onCommit).toHaveBeenCalledTimes(1);
    fireEvent.change(input, { target: { value: "13" } });
    fireEvent.keyUp(input, { key: "ArrowUp" });
    expect(onCommit).toHaveBeenLastCalledWith(13);
  });
});
