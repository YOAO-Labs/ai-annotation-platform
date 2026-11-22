import {
  PANEL_IDS,
  PANEL_TITLES,
  clampWorkspaceRect,
  sanitizeWorkspaceSnapshot,
  type PanelId,
  type WorkspaceBounds,
  type WorkspaceContext,
  type WorkspaceNode,
  type WorkspaceSnapshot,
} from "./workbenchLayoutSnapshot";
import { defaultDockWidth } from "./workbenchPanelRegistry";

export const WORKSPACE_PRESETS = [
  { id: "standard", title: "标准标注" },
  { id: "focus", title: "专注画布" },
  { id: "review", title: "审核协作" },
  { id: "ai-review", title: "图片 AI 审阅", contexts: ["annotate:image"] },
  { id: "video-tracking", title: "视频追踪", contexts: ["annotate:video"] },
] as const;
export type WorkspacePresetId = (typeof WORKSPACE_PRESETS)[number]["id"];
export type ActiveWorkspacePreset = WorkspacePresetId | "custom";

export function getActiveWorkspacePreset(
  snapshot: WorkspaceSnapshot,
  context: WorkspaceContext,
): ActiveWorkspacePreset {
  if (snapshot.layout.grid.maximizedNode) return "focus";
  if (snapshot.layout.floatingGroups?.length) return "custom";
  const signature = (value: WorkspaceSnapshot) => {
    const visit = (node: WorkspaceNode, horizontal: boolean): unknown => {
      if (node.visible === false) return null;
      if (node.type === "leaf") return node.data.id === "parking" ? null : node.data.views;
      const children = node.data.map((child) => visit(child, !horizontal)).filter(Boolean);
      return children.length === 1 ? children[0] : [horizontal, children];
    };
    return JSON.stringify([
      visit(value.layout.grid.root, value.layout.grid.orientation === "HORIZONTAL"),
      context.endsWith(":3d") && value.visibilityIntent["camera-view"] === "shown"
        ? value.cameraPresentation
        : null,
    ]);
  };
  const current = signature(snapshot);
  return (
    WORKSPACE_PRESETS.find(
      ({ id }) =>
        id !== "focus" &&
        presetSupportsContext(id, context) &&
        signature(createWorkspacePreset(id)) === current,
    )?.id ?? "custom"
  );
}
export type ThreeDWorkspacePresetId = "box-refinement" | "sensor-fusion" | "point-segmentation";
export const DEFAULT_WORKSPACE_BOUNDS: WorkspaceBounds = { width: 1440, height: 800 };
export const PANEL_DEFAULT_POSITION = {
  "task-queue": "left",
  "class-palette": "left",
  inspector: "right",
  discussion: "below",
  "ai-task": "right",
  "video-tracker": "right",
  "tri-view": "right",
  "camera-view": "right",
} as const;

function leaf(
  id: string,
  views: PanelId[],
  size: number,
  hidden = false,
  activeView: PanelId | undefined = views[0],
): Extract<WorkspaceNode, { type: "leaf" }> {
  return {
    type: "leaf",
    data: { id, views, ...(activeView ? { activeView } : {}) },
    size,
    ...(hidden ? { visible: false } : {}),
  };
}
function branch(data: WorkspaceNode[], size: number): WorkspaceNode {
  return { type: "branch", data, size };
}

export function createWorkspacePreset(
  preset: WorkspacePresetId,
  bounds: WorkspaceBounds = DEFAULT_WORKSPACE_BOUNDS,
  _context?: WorkspaceContext,
): WorkspaceSnapshot {
  const { width, height } = bounds;
  const sidebarWidth = defaultDockWidth(width);
  const parked: PanelId[] =
    preset === "review"
      ? ["task-queue", "class-palette", "ai-task", "video-tracker"]
      : preset === "ai-review"
        ? ["video-tracker"]
        : preset === "video-tracking"
          ? ["discussion", "ai-task"]
          : ["ai-task", "video-tracker"];
  parked.push("tri-view", "camera-view");
  const parking = leaf("parking", parked, 0, true);
  const root =
    preset === "ai-review"
      ? branch(
          [
            branch(
              [
                leaf("task-tools", ["task-queue", "class-palette"], sidebarWidth),
                leaf("canvas", ["canvas"], width - sidebarWidth * 2),
                leaf("inspector", ["inspector", "ai-task"], sidebarWidth, false, "ai-task"),
              ],
              height * 0.72,
            ),
            leaf("discussion", ["discussion"], height * 0.28),
            parking,
          ],
          width,
        )
      : preset === "video-tracking"
        ? branch(
            [
              leaf("task-tools", ["task-queue", "class-palette"], sidebarWidth),
              leaf("canvas", ["canvas"], width - sidebarWidth * 2),
              leaf(
                "video-tracker",
                ["video-tracker", "inspector"],
                sidebarWidth,
                false,
                "video-tracker",
              ),
              parking,
            ],
            height,
          )
        : preset === "review"
          ? branch(
              [
                branch(
                  [
                    leaf("canvas", ["canvas"], width - sidebarWidth),
                    leaf("inspector", ["inspector"], sidebarWidth),
                  ],
                  height * 0.7,
                ),
                leaf("discussion", ["discussion"], height * 0.3),
                parking,
              ],
              width,
            )
          : branch(
              [
                branch(
                  [
                    leaf("task-queue", ["task-queue"], height * 0.5),
                    leaf("class-palette", ["class-palette"], height * 0.5),
                  ],
                  sidebarWidth,
                ),
                leaf("canvas", ["canvas"], width - sidebarWidth * 2),
                branch(
                  [
                    leaf("inspector", ["inspector"], height * 0.5),
                    leaf("discussion", ["discussion"], height * 0.5),
                  ],
                  sidebarWidth,
                ),
                parking,
              ],
              height,
            );
  const returns = Object.fromEntries(
    parked
      .filter(
        (id) =>
          id !== "ai-task" && id !== "video-tracker" && id !== "tri-view" && id !== "camera-view",
      )
      .map((id) => [id, { group: id, index: 0 }]),
  );
  return sanitizeWorkspaceSnapshot(
    {
      layout: {
        grid: {
          root,
          width,
          height,
          orientation: preset === "review" || preset === "ai-review" ? "VERTICAL" : "HORIZONTAL",
          ...(preset === "focus" ? { maximizedNode: { location: [1] } } : {}),
        },
        panels: Object.fromEntries(PANEL_IDS.map((id) => [id, { id, title: PANEL_TITLES[id] }])),
        activeGroup:
          preset === "ai-review"
            ? "inspector"
            : preset === "video-tracking"
              ? "video-tracker"
              : "canvas",
      },
      returns,
    },
    bounds,
  );
}

export function presetSupportsContext(id: WorkspacePresetId, context: WorkspaceContext): boolean {
  const preset = WORKSPACE_PRESETS.find((item) => item.id === id);
  return !preset || !("contexts" in preset) || preset.contexts.some((item) => item === context);
}

interface LegacyFloatingPanel {
  detached?: boolean;
  x?: number | null;
  y?: number | null;
  w?: number | null;
  h?: number | null;
}
export interface LegacyWorkbenchLayout {
  layout?: {
    leftOpen?: boolean;
    rightOpen?: boolean;
    discussionCollapsed?: boolean;
    floatingTaskQueue?: LegacyFloatingPanel;
    floatingClassPalette?: LegacyFloatingPanel;
    floatingInspector?: LegacyFloatingPanel;
    floatingDiscussion?: LegacyFloatingPanel;
    triViewFloat?: { collapsed?: boolean };
  };
  common?: { leftWidthPct?: number; rightWidthPct?: number };
  rightSplitTop?: number;
}

export function migrateThreeDWorkspace(
  input: WorkspaceSnapshot,
  legacy: LegacyWorkbenchLayout,
  context: WorkspaceContext,
): WorkspaceSnapshot {
  const snapshot = structuredClone(input);
  if (!context.endsWith(":3d")) return snapshot;
  snapshot.cameraPresentation = "floating";
  snapshot.visibilityIntent["camera-view"] = "shown";
  if (legacy.layout?.triViewFloat?.collapsed === true) return snapshot;
  snapshot.visibilityIntent["tri-view"] = "shown";
  const width = Math.max(220, Math.round(snapshot.layout.grid.width * 0.15));
  const visit = (node: WorkspaceNode, axis: "HORIZONTAL" | "VERTICAL"): WorkspaceNode => {
    if (node.type === "leaf") {
      if (node.data.id === "parking") {
        node.data.views = node.data.views.filter((id) => id !== "tri-view");
        if (node.data.activeView === "tri-view") node.data.activeView = node.data.views[0];
      }
      return node;
    }
    node.data = node.data.map((child) =>
      visit(child, axis === "HORIZONTAL" ? "VERTICAL" : "HORIZONTAL"),
    );
    const canvasIndex = node.data.findIndex(
      (child) => child.type === "leaf" && child.data.id === "canvas",
    );
    if (canvasIndex !== -1) {
      const canvas = node.data[canvasIndex];
      const size = canvas.size ?? snapshot.layout.grid.width;
      if (axis === "HORIZONTAL") {
        canvas.size = Math.max(480, size - width);
        node.data.splice(canvasIndex + 1, 0, leaf("tri-view", ["tri-view"], width));
      } else {
        node.data[canvasIndex] = branch(
          [
            { ...canvas, size: Math.max(480, snapshot.layout.grid.width - width) },
            leaf("tri-view", ["tri-view"], width),
          ],
          size,
        );
      }
    }
    return node;
  };
  snapshot.layout.grid.root = visit(snapshot.layout.grid.root, snapshot.layout.grid.orientation);
  if (snapshot.layout.grid.maximizedNode) {
    const find = (node: WorkspaceNode, path: number[] = []): number[] | undefined =>
      node.type === "leaf"
        ? node.data.id === "canvas"
          ? path
          : undefined
        : node.data.map((child, index) => find(child, [...path, index])).find(Boolean);
    snapshot.layout.grid.maximizedNode.location = find(snapshot.layout.grid.root)!;
  }
  return sanitizeWorkspaceSnapshot(snapshot);
}

export function migrateLegacyWorkspace(
  legacy: LegacyWorkbenchLayout,
  bounds: WorkspaceBounds = DEFAULT_WORKSPACE_BOUNDS,
): WorkspaceSnapshot {
  const snapshot = createWorkspacePreset("standard", bounds);
  const root = snapshot.layout.grid.root;
  if (root.type !== "branch") return snapshot;
  const finiteOr = (value: number | null | undefined, fallback: number) =>
    typeof value === "number" && Number.isFinite(value) ? value : fallback;
  const left = Math.min(35, Math.max(10, finiteOr(legacy.common?.leftWidthPct, 15)));
  const right = Math.min(35, Math.max(10, finiteOr(legacy.common?.rightWidthPct, 15)));
  root.data[0].size = (bounds.width * left) / 100;
  root.data[1].size = (bounds.width * (100 - left - right)) / 100;
  root.data[2].size = (bounds.width * right) / 100;
  if (root.data[2].type === "branch") {
    const top = Math.min(bounds.height - 160, Math.max(160, finiteOr(legacy.rightSplitTop, 360)));
    root.data[2].data[0].size = top;
    root.data[2].data[1].size = bounds.height - top;
  }
  const parking = root.data[3];
  const source = legacy.layout ?? {};
  const states: [Exclude<PanelId, "canvas">, LegacyFloatingPanel | undefined, boolean][] = [
    ["task-queue", source.floatingTaskQueue, source.leftOpen !== false],
    ["class-palette", source.floatingClassPalette, source.leftOpen !== false],
    ["inspector", source.floatingInspector, source.rightOpen !== false],
    [
      "discussion",
      source.floatingDiscussion,
      source.rightOpen !== false && source.discussionCollapsed !== true,
    ],
  ];
  function detach(node: WorkspaceNode, id: PanelId): WorkspaceNode | null {
    if (node.type === "leaf") return node.data.views.includes(id) ? null : node;
    const children = node.data
      .map((n) => detach(n, id))
      .filter((n): n is WorkspaceNode => n !== null);
    return children.length ? { ...node, data: children } : null;
  }
  for (const [id, floating, visible] of states) {
    if (!floating?.detached && visible) continue;
    const next = detach(snapshot.layout.grid.root, id);
    if (next) snapshot.layout.grid.root = next;
    if (floating?.detached) {
      const position = clampWorkspaceRect(
        {
          left: finiteOr(floating.x, 32),
          top: finiteOr(floating.y, 32),
          width: finiteOr(floating.w, 360),
          height: finiteOr(floating.h, 480),
        },
        bounds,
      );
      (snapshot.layout.floatingGroups ??= []).push({
        data: { id, views: [id], activeView: id },
        position,
      });
    } else if (parking.type === "leaf") {
      parking.data.views.push(id);
      parking.data.activeView ??= id;
      snapshot.returns[id] = { group: id, index: 0 };
    }
  }
  return sanitizeWorkspaceSnapshot(snapshot, bounds);
}
