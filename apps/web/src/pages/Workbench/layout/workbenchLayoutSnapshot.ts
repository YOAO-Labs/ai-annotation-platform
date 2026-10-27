import type { SerializedDockview } from "dockview-react";

export const PANEL_IDS = [
  "canvas",
  "task-queue",
  "class-palette",
  "inspector",
  "discussion",
  "ai-task",
  "video-tracker",
  "tri-view",
  "camera-view",
] as const;
export type PanelId = (typeof PANEL_IDS)[number];
export const TOOL_PANEL_IDS = ["ai-task", "video-tracker", "tri-view", "camera-view"] as const;
export type ToolPanelId = (typeof TOOL_PANEL_IDS)[number];
export type CameraPresentation = "floating" | "docked";
export const PANEL_TITLES: Record<PanelId, string> = {
  canvas: "画布",
  "task-queue": "任务队列",
  "class-palette": "类别面板",
  inspector: "标注详情",
  discussion: "讨论",
  "ai-task": "单图 AI",
  "video-tracker": "视频追踪",
  "tri-view": "三视图精修",
  "camera-view": "相机视图",
};
export const WORKSPACE_CONTEXTS = [
  "annotate:image",
  "annotate:video",
  "annotate:3d",
  "review:image",
  "review:video",
  "review:3d",
] as const;
export type WorkspaceContext = (typeof WORKSPACE_CONTEXTS)[number];
export interface WorkspaceBounds {
  width: number;
  height: number;
}
export interface WorkspaceRect extends WorkspaceBounds {
  left: number;
  top: number;
}
export interface PanelReturn {
  group: string;
  index: number;
  position?: WorkspaceRect;
  dock?: WorkspaceBounds & {
    reference: PanelId;
    anchors: PanelId[];
    direction: "left" | "right" | "above" | "below";
  };
}
export interface WorkspaceGroup {
  id: string;
  views: PanelId[];
  activeView?: PanelId;
  locked?: boolean | "no-drop-target";
  hideHeader?: boolean;
}
export type WorkspaceNode =
  | { type: "leaf"; data: WorkspaceGroup; size?: number; visible?: boolean }
  | { type: "branch"; data: WorkspaceNode[]; size?: number; visible?: boolean };
export type WorkspaceLayout = Omit<SerializedDockview, "grid"> & {
  grid: Omit<SerializedDockview["grid"], "root"> & {
    root: WorkspaceNode;
    // Dockview 8.2 writes this standard grid field but omits it from SerializedDockview.
    maximizedNode?: { location: number[] };
  };
};
export interface WorkspaceSnapshot {
  layout: WorkspaceLayout;
  returns: Partial<Record<PanelId, PanelReturn>>;
  visibilityIntent: Record<ToolPanelId, "shown" | "hidden">;
  cameraPresentation: CameraPresentation;
}
export type WorkspaceEnvelope = { schemaVersion: 1 | 2 | 3 | 4 | 5; snapshot: unknown };
export type WorkspaceReadOnlyReason = "invalid" | "newer-schema";
export const WORKSPACE_SCHEMA_VERSION = 5 as const;

const LIMIT = 64 * 1024;
const ID = /^[a-zA-Z0-9_-]{1,64}$/;
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("Invalid layout object");
  return value as Record<string, unknown>;
}
function finite(value: unknown, min = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < min || value > 1_000_000)
    throw new Error("Invalid layout dimension");
  return value;
}
function groupId(value: unknown): string {
  if (
    typeof value !== "string" ||
    !ID.test(value) ||
    ["compact-overlay", "__proto__", "constructor", "prototype"].includes(value)
  )
    throw new Error("Invalid layout group");
  return value;
}
export function isPanelId(value: unknown): value is PanelId {
  return typeof value === "string" && PANEL_IDS.includes(value as PanelId);
}

export function clampWorkspaceRect(
  rect: WorkspaceRect,
  bounds: WorkspaceBounds,
  compact = false,
): WorkspaceRect {
  const inset = compact ? 16 : 0;
  const maxWidth = Math.max(1, bounds.width - inset * 2);
  const maxHeight = Math.max(1, bounds.height - inset * 2);
  const width = Math.min(maxWidth, compact ? 360 : Math.min(720, Math.max(320, rect.width)));
  const height = Math.min(maxHeight, compact ? 480 : Math.min(900, Math.max(320, rect.height)));
  return {
    left: Math.min(Math.max(inset, rect.left), Math.max(inset, bounds.width - inset - width)),
    top: Math.min(Math.max(inset, rect.top), Math.max(inset, bounds.height - inset - height)),
    width,
    height,
  };
}

/** The same allowlist validates network, cache and locally serialized snapshots. */
export function sanitizeWorkspaceSnapshot(
  value: unknown,
  bounds?: WorkspaceBounds,
  allowCollapsed = true,
): WorkspaceSnapshot {
  const json = JSON.stringify(value, (_key, item: unknown) => {
    if (typeof item === "number" && !Number.isFinite(item))
      throw new Error("Non-finite layout value");
    return item;
  });
  if (!json || new TextEncoder().encode(json).length > LIMIT)
    throw new Error("Layout exceeds 64 KiB");
  const input = record(value);
  const raw = record(input.layout);
  const rawGrid = record(raw.grid);
  const width = finite(rawGrid.width, 1);
  const height = finite(rawGrid.height, 1);
  const effectiveBounds = bounds ?? { width, height };
  const orientation = rawGrid.orientation;
  if (orientation !== "HORIZONTAL" && orientation !== "VERTICAL")
    throw new Error("Invalid grid orientation");
  const ids = new Set<string>();
  const visibleIds = new Set<string>();
  const panels = new Set<PanelId>();
  let nodes = 0;
  let canvasPath: number[] | undefined;
  function parseGroup(
    data: unknown,
    floating: boolean,
    visible: boolean,
    path?: number[],
  ): WorkspaceGroup {
    const item = record(data);
    const id = groupId(item.id);
    if (ids.has(id)) throw new Error("Duplicate layout group");
    ids.add(id);
    if (visible) visibleIds.add(id);
    if (!Array.isArray(item.views) || item.views.some((p) => !isPanelId(p)))
      throw new Error("Unknown panel");
    const views = item.views as PanelId[];
    if (views.length > 9) throw new Error("Too many group panels");
    if (floating && views.includes("camera-view"))
      throw new Error("Camera gallery must remain docked");
    for (const panel of views) {
      if (panels.has(panel)) throw new Error("Duplicate layout panel");
      panels.add(panel);
    }
    if (views.includes("canvas")) {
      if (id !== "canvas" || views.length !== 1 || floating || !visible)
        throw new Error("Canvas must be a unique visible docked group");
      canvasPath = path;
    } else if (id === "canvas") throw new Error("Missing canvas");
    if (id === "parking" && (floating || visible))
      throw new Error("Parking must remain hidden and docked");
    if (!allowCollapsed && id !== "parking" && !visible)
      throw new Error("Collapsed groups require schema 4");
    if (item.activeView !== undefined && !views.includes(item.activeView as PanelId))
      throw new Error("Invalid active tab");
    return {
      id,
      views: [...views],
      ...(views.length ? { activeView: (item.activeView ?? views[0]) as PanelId } : {}),
      ...(id === "canvas" ? { locked: true, hideHeader: true } : {}),
      ...(id === "parking" ? { locked: "no-drop-target" as const, hideHeader: true } : {}),
    };
  }
  function parseNode(value: unknown, path: number[] = [], parentVisible = true): WorkspaceNode {
    if (++nodes > 40 || path.length >= 12) throw new Error("Layout tree exceeds limits");
    const node = record(value);
    if (node.visible !== undefined && typeof node.visible !== "boolean")
      throw new Error("Invalid visibility");
    const visible = parentVisible && node.visible !== false;
    const geometry = {
      ...(node.size === undefined ? {} : { size: finite(node.size) }),
      ...(node.visible === false ? { visible: false } : {}),
    };
    if (node.type === "leaf") {
      const data = parseGroup(node.data, false, visible, path);
      return { type: "leaf", data, ...geometry, ...(data.id === "parking" ? { size: 0 } : {}) };
    }
    if (
      node.type !== "branch" ||
      !Array.isArray(node.data) ||
      node.data.length === 0 ||
      node.data.length > 10
    )
      throw new Error("Invalid layout branch");
    return {
      type: "branch",
      data: node.data.map((n, i) => parseNode(n, [...path, i], visible)),
      ...geometry,
    };
  }
  const root = parseNode(rawGrid.root);
  const parseRect = (value: unknown): WorkspaceRect => {
    const rect = record(value);
    const w = finite(rect.width, 1),
      h = finite(rect.height, 1);
    // Dockview can anchor a float to either side. Normalize to top-left coordinates.
    const left =
      rect.left !== undefined ? finite(rect.left, -1_000_000) : width - finite(rect.right) - w;
    const top =
      rect.top !== undefined ? finite(rect.top, -1_000_000) : height - finite(rect.bottom) - h;
    return clampWorkspaceRect({ left, top, width: w, height: h }, effectiveBounds);
  };
  if (raw.floatingGroups !== undefined && !Array.isArray(raw.floatingGroups))
    throw new Error("Invalid floating groups");
  if (Array.isArray(raw.floatingGroups) && raw.floatingGroups.length > 9)
    throw new Error("Too many floating groups");
  const floatingGroups = (raw.floatingGroups as unknown[] | undefined)?.map((entry) => {
    const group = record(entry);
    if (group.grid !== undefined) throw new Error("Nested floating grids are not supported");
    return { data: parseGroup(group.data, true, true), position: parseRect(group.position) };
  });
  if (!canvasPath || panels.size !== PANEL_IDS.length || PANEL_IDS.some((p) => !panels.has(p)))
    throw new Error("Missing core panel");
  if (ids.size - (ids.has("parking") ? 1 : 0) > 9) throw new Error("Too many layout groups");
  const rawPanels = record(raw.panels);
  if (Object.keys(rawPanels).length > 9) throw new Error("Too many panels");
  const cleanPanels = Object.fromEntries(
    PANEL_IDS.map((id) => {
      if (record(rawPanels[id]).id !== id) throw new Error("Invalid panel identity");
      return [
        id,
        {
          id,
          contentComponent: "workbench-panel",
          title: PANEL_TITLES[id],
          renderer:
            id === "task-queue" || id === "class-palette"
              ? ("onlyWhenVisible" as const)
              : ("always" as const),
        },
      ];
    }),
  );
  const rawIntent = input.visibilityIntent === undefined ? {} : record(input.visibilityIntent);
  if (
    Object.keys(rawIntent).some((id) => !TOOL_PANEL_IDS.includes(id as ToolPanelId)) ||
    Object.values(rawIntent).some((intent) => intent !== "shown" && intent !== "hidden")
  )
    throw new Error("Invalid tool panel visibility intent");
  const visibilityIntent = Object.fromEntries(
    TOOL_PANEL_IDS.map((id) => [
      id,
      (rawIntent[id] ?? (groupsInSnapshot(root, floatingGroups).get(id) ? "shown" : "hidden")) as
        | "shown"
        | "hidden",
    ]),
  ) as WorkspaceSnapshot["visibilityIntent"];
  const cameraPresentation = input.cameraPresentation ?? "floating";
  if (cameraPresentation !== "floating" && cameraPresentation !== "docked")
    throw new Error("Invalid camera presentation");
  if (
    cameraPresentation === "floating" &&
    groupsInSnapshot(root, floatingGroups).has("camera-view")
  )
    throw new Error("Floating cameras require the gallery in parking");
  const returns: WorkspaceSnapshot["returns"] = {};
  if (input.returns !== undefined) {
    for (const [id, value] of Object.entries(record(input.returns))) {
      if (!isPanelId(id) || id === "canvas") throw new Error("Invalid return panel");
      const entry = record(value);
      if (id === "camera-view" && entry.position !== undefined)
        throw new Error("Camera gallery cannot return to a native float");
      const group = groupId(entry.group);
      const index = finite(entry.index);
      if (["canvas", "parking"].includes(group) || !Number.isInteger(index) || index > 8)
        throw new Error("Invalid return position");
      let dock: PanelReturn["dock"];
      if (entry.dock !== undefined) {
        const destination = record(entry.dock);
        if (
          !isPanelId(destination.reference) ||
          destination.reference === id ||
          !["left", "right", "above", "below"].includes(destination.direction as string)
        )
          throw new Error("Invalid dock return");
        if (
          !Array.isArray(destination.anchors) ||
          !destination.anchors.length ||
          destination.anchors.length > 8 ||
          destination.anchors.some((anchor) => !isPanelId(anchor) || anchor === id) ||
          new Set(destination.anchors).size !== destination.anchors.length ||
          !destination.anchors.includes(destination.reference)
        )
          throw new Error("Invalid dock return anchors");
        dock = {
          reference: destination.reference,
          anchors: [...destination.anchors] as PanelId[],
          direction: destination.direction as NonNullable<PanelReturn["dock"]>["direction"],
          width: finite(destination.width, 1),
          height: finite(destination.height, 1),
        };
      }
      returns[id] = {
        group,
        index,
        ...(entry.position === undefined ? {} : { position: parseRect(entry.position) }),
        ...(dock ? { dock } : {}),
      };
    }
  }
  let maximizedNode: { location: number[] } | undefined;
  if (rawGrid.maximizedNode !== undefined) {
    const path = record(rawGrid.maximizedNode).location;
    if (!Array.isArray(path) || JSON.stringify(path) !== JSON.stringify(canvasPath))
      throw new Error("Only canvas may be maximized");
    maximizedNode = { location: [...canvasPath] };
  }
  if (
    raw.activeGroup !== undefined &&
    (typeof raw.activeGroup !== "string" ||
      !visibleIds.has(raw.activeGroup) ||
      raw.activeGroup === "parking")
  )
    throw new Error("Invalid active group");
  return {
    layout: {
      grid: {
        root,
        width,
        height,
        orientation: orientation as WorkspaceLayout["grid"]["orientation"],
        ...(maximizedNode ? { maximizedNode } : {}),
      },
      panels: cleanPanels,
      ...(raw.activeGroup ? { activeGroup: raw.activeGroup as string } : {}),
      ...(floatingGroups?.length ? { floatingGroups } : {}),
    },
    returns,
    visibilityIntent,
    cameraPresentation,
  };
}

function groupsInSnapshot(
  root: WorkspaceNode,
  floatingGroups: Array<{ data: WorkspaceGroup }> | undefined,
): Map<PanelId, string> {
  const locations = new Map<PanelId, string>();
  const visit = (node: WorkspaceNode) => {
    if (node.type === "branch") return node.data.forEach(visit);
    if (node.data.id !== "parking")
      node.data.views.forEach((id) => locations.set(id, node.data.id));
  };
  visit(root);
  floatingGroups?.forEach((group) =>
    group.data.views.forEach((id) => locations.set(id, group.data.id)),
  );
  return locations;
}

function upgradeLegacySnapshot(value: unknown, version: number): unknown {
  const snapshot = structuredClone(record(value));
  const layout = record(snapshot.layout);
  const grid = record(layout.grid);
  const panels = record(layout.panels);
  const expected = PANEL_IDS.filter(
    (id) =>
      id !== "tri-view" &&
      id !== "camera-view" &&
      (version >= 3 || (id !== "ai-task" && id !== "video-tracker")),
  );
  if (Object.keys(panels).length !== expected.length || expected.some((id) => !panels[id]))
    throw new Error("Panel set does not match schema version");
  const toolIds: ToolPanelId[] = version >= 3 ? ["tri-view", "camera-view"] : [...TOOL_PANEL_IDS];
  for (const id of toolIds) panels[id] = { id };
  const parking = {
    type: "leaf",
    data: {
      id: "parking",
      views: toolIds,
      activeView: toolIds[0],
      locked: "no-drop-target",
      hideHeader: true,
    },
    size: 0,
    visible: false,
  };
  const findParking = (node: unknown): Record<string, unknown> | null => {
    const parsed = record(node);
    if (parsed.type === "leaf") {
      const data = record(parsed.data);
      return data.id === "parking" ? data : null;
    }
    if (parsed.type !== "branch" || !Array.isArray(parsed.data)) return null;
    for (const child of parsed.data) {
      const found = findParking(child);
      if (found) return found;
    }
    return null;
  };
  const existingParking = findParking(grid.root);
  if (existingParking) {
    if (!Array.isArray(existingParking.views)) throw new Error("Invalid parking group");
    existingParking.views.push(...toolIds);
    existingParking.activeView ??= toolIds[0];
  } else {
    const root = record(grid.root);
    grid.root =
      root.type === "branch" && Array.isArray(root.data)
        ? { ...root, data: [...root.data, parking] }
        : { type: "branch", data: [root, parking], size: root.size };
  }
  snapshot.visibilityIntent = {
    ...(version >= 3 && snapshot.visibilityIntent !== undefined
      ? record(snapshot.visibilityIntent)
      : {}),
    ...Object.fromEntries(toolIds.map((id) => [id, "hidden"])),
  };
  snapshot.cameraPresentation = "floating";
  return snapshot;
}

export function readWorkspaceEnvelope(
  value: unknown,
  bounds?: WorkspaceBounds,
): {
  snapshot: WorkspaceSnapshot | null;
  readOnlyReason: WorkspaceReadOnlyReason | null;
} {
  if (value === undefined || value === null) return { snapshot: null, readOnlyReason: null };
  try {
    const envelope = record(value);
    if (
      typeof envelope.schemaVersion === "number" &&
      envelope.schemaVersion > WORKSPACE_SCHEMA_VERSION
    )
      return { snapshot: null, readOnlyReason: "newer-schema" };
    if (
      envelope.schemaVersion !== 1 &&
      envelope.schemaVersion !== 2 &&
      envelope.schemaVersion !== 3 &&
      envelope.schemaVersion !== 4 &&
      envelope.schemaVersion !== 5
    )
      throw new Error("Unsupported layout schema");
    if (envelope.schemaVersion === 5) {
      const snapshot = record(envelope.snapshot);
      const intent = record(snapshot.visibilityIntent);
      if (
        snapshot.cameraPresentation === undefined ||
        TOOL_PANEL_IDS.some((id) => intent[id] === undefined)
      )
        throw new Error("Schema 5 requires camera presentation and visibility intent");
    }
    return {
      snapshot: sanitizeWorkspaceSnapshot(
        envelope.schemaVersion === 5
          ? envelope.snapshot
          : upgradeLegacySnapshot(envelope.snapshot, envelope.schemaVersion),
        bounds,
        envelope.schemaVersion >= 4,
      ),
      readOnlyReason: null,
    };
  } catch {
    return { snapshot: null, readOnlyReason: "invalid" };
  }
}
