import { describe, expect, it } from "vitest";
import {
  splitToolDock,
  toolDockHeight,
  type ToolDockEntry,
  type ToolDockMetrics,
} from "./toolDockOverflow";

const metrics: ToolDockMetrics = {
  button: 38,
  gap: 6,
  divider: 13,
  sectionLabel: 12,
  subsectionLabel: 12,
  padding: 20,
};
const image: ToolDockEntry[] = [
  { id: "select", group: "select" },
  { id: "box", group: "draw" },
  { id: "polygon", group: "draw" },
  { id: "smart-point", group: "ai" },
];

describe("tool dock overflow", () => {
  it("充足空间不预留更多按钮，临界高度仍保留全部工具", () => {
    expect(toolDockHeight(image, false, metrics)).toBe(228);
    expect(splitToolDock(image, "polygon", 228, false, metrics)).toEqual({
      visibleIds: ["select", "box", "polygon", "smart-point"],
      overflowIds: [],
      scroll: false,
    });
  });
  it("发生溢出后预留更多入口，按原序收纳；当前尾部工具始终可见", () => {
    expect(splitToolDock(image, "box", 200, false, metrics)).toEqual({
      visibleIds: ["select", "box"],
      overflowIds: ["polygon", "smart-point"],
      scroll: false,
    });
    expect(splitToolDock(image, "smart-point", 200, false, metrics)).toEqual({
      visibleIds: ["select", "smart-point"],
      overflowIds: ["box", "polygon"],
      scroll: false,
    });
  });
  it("极小容器保留选择/当前工具，启用滚动且不丢其它入口", () => {
    expect(splitToolDock(image, "polygon", 70, false, metrics)).toEqual({
      visibleIds: ["select", "polygon"],
      overflowIds: ["box", "smart-point"],
      scroll: true,
    });
  });
  it("视频按实际分组标题和嵌套 SAM 占高分配", () => {
    const video: ToolDockEntry[] = [
      { id: "select", group: "select" },
      { id: "box", group: "frame" },
      { id: "smart-point", group: "sam" },
      { id: "track", group: "track" },
    ];
    expect(toolDockHeight(video, true, metrics)).toBe(282);
    const result = splitToolDock(video, "track", 200, true, metrics);
    expect(result).toEqual({
      visibleIds: ["select", "track"],
      overflowIds: ["box", "smart-point"],
      scroll: false,
    });
  });
  it("高度恢复后按输入顺序恢复，已过滤工具不会凭当前 ID 被补回", () => {
    const filtered = image.filter((tool) => tool.id !== "smart-point");
    expect(splitToolDock(filtered, "smart-point", 400, false, metrics)).toEqual({
      visibleIds: ["select", "box", "polygon"],
      overflowIds: [],
      scroll: false,
    });
  });
});
