import type { CanvasTool, ToolPointerContext } from "./index";

export const SelectTool: CanvasTool = {
  id: "select",
  hotkey: "V",
  label: "选择",
  icon: "cursor",
  cursor: "default",
  onPointerDown: ({ evt, onClearSelection }: ToolPointerContext) => {
    if (!evt.shiftKey) onClearSelection();
    return null;
  },
};
