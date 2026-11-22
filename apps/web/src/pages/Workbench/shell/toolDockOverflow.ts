export type ToolDockGroup = "select" | "draw" | "ai" | "view" | "frame" | "sam" | "track";

export interface ToolDockEntry {
  id: string;
  group: ToolDockGroup;
}

export interface ToolDockMetrics {
  button: number;
  gap: number;
  divider: number;
  sectionLabel: number;
  subsectionLabel: number;
  padding: number;
}

export function toolDockHeight(
  tools: ToolDockEntry[],
  video: boolean,
  metrics: ToolDockMetrics,
  more = false,
): number {
  const { button, gap, divider, sectionLabel, subsectionLabel, padding } = metrics;
  const stack = (heights: number[]) =>
    heights.reduce((sum, height) => sum + height, 0) + Math.max(0, heights.length - 1) * gap;
  const rows: number[] = [];
  if (video) {
    if (tools.some((tool) => tool.group === "select")) rows.push(button);
    const frames = tools.filter((tool) => tool.group === "frame");
    const sam = tools.filter((tool) => tool.group === "sam");
    if (frames.length || sam.length) {
      if (rows.length) rows.push(divider);
      rows.push(
        stack([
          sectionLabel,
          ...frames.map(() => button),
          ...(sam.length ? [stack([subsectionLabel, ...sam.map(() => button)])] : []),
        ]),
      );
    }
    const tracks = tools.filter((tool) => tool.group === "track");
    if (tracks.length) {
      if (rows.length) rows.push(divider);
      rows.push(stack([sectionLabel, ...tracks.map(() => button)]));
    }
  } else {
    tools.forEach((tool, index) => {
      if (index > 0 && tools[index - 1].group !== tool.group) rows.push(divider);
      rows.push(button);
    });
  }
  if (more) rows.push(button);
  return padding + stack(rows);
}

export function splitToolDock(
  tools: ToolDockEntry[],
  activeId: string,
  height: number,
  video: boolean,
  metrics: ToolDockMetrics,
): { visibleIds: string[]; overflowIds: string[]; scroll: boolean } {
  const all = tools.map((tool) => tool.id);
  if (height <= 0 || metrics.button <= 0 || toolDockHeight(tools, video, metrics) <= height)
    return { visibleIds: all, overflowIds: [], scroll: false };

  const selected = new Set(
    tools.filter((tool) => tool.id === "select" || tool.id === activeId).map((tool) => tool.id),
  );
  const visible = () => tools.filter((tool) => selected.has(tool.id));
  if (toolDockHeight(visible(), video, metrics, true) > height) {
    return {
      visibleIds: visible().map((tool) => tool.id),
      overflowIds: all.filter((id) => !selected.has(id)),
      scroll: true,
    };
  }
  for (const tool of tools) {
    if (selected.has(tool.id)) continue;
    selected.add(tool.id);
    if (toolDockHeight(visible(), video, metrics, true) > height) selected.delete(tool.id);
  }
  return {
    visibleIds: all.filter((id) => selected.has(id)),
    overflowIds: all.filter((id) => !selected.has(id)),
    scroll: false,
  };
}
