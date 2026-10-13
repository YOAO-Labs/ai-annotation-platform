import type { DockviewApi, DockviewGroupPanel, IDockviewPanel } from "dockview-react";
import {
  WORKBENCH_PANEL_REGISTRY,
  defaultDockWidth,
  panelMinimumWidth,
} from "./workbenchPanelRegistry";
import {
  createWorkspacePreset,
  PANEL_DEFAULT_POSITION,
  type WorkspacePresetId,
  type ThreeDWorkspacePresetId,
} from "./workbenchLayoutPresets";
import {
  PANEL_IDS,
  clampWorkspaceRect,
  sanitizeWorkspaceSnapshot,
  type PanelId,
  type CameraPresentation,
  type PanelReturn,
  type WorkspaceBounds,
  type WorkspaceGroup,
  type WorkspaceNode,
  type WorkspaceRect,
  type WorkspaceSnapshot,
} from "./workbenchLayoutSnapshot";

type SidePanelId = Exclude<PanelId, "canvas">;
type DockPosition = "left" | "right" | "above" | "below";
type Axis = "HORIZONTAL" | "VERTICAL";
export type CanvasPlacement = "left" | "right" | "above" | "below" | "center";
export type WorkspaceSide = "left" | "right";
export type WorkspaceSideState = "empty" | "open" | "collapsed";
type Tree = (
  | { group: WorkspaceGroup; size: number }
  | { axis: Axis; children: Tree[]; size: number }
) & { visible?: boolean };
const opposite = (axis: Axis): Axis => (axis === "HORIZONTAL" ? "VERTICAL" : "HORIZONTAL");

/** Hidden parking takes no space; unary engine wrappers do not change a layout. */
function treeFromNode(node: WorkspaceNode, axis: Axis, includeHidden = false): Tree | null {
  if (!includeHidden && node.visible === false) return null;
  const visibility = node.visible === false ? { visible: false } : {};
  if (node.type === "leaf")
    return node.data.id === "parking"
      ? null
      : { group: node.data, size: node.size ?? 1, ...visibility };
  const children = node.data
    .map((child) => treeFromNode(child, opposite(axis), includeHidden))
    .filter((child): child is Tree => child !== null);
  if (!children.length) return null;
  if (children.length === 1)
    return { ...children[0], size: node.size ?? children[0].size, ...visibility };
  return { axis, children, size: node.size ?? 1, ...visibility };
}
/** Serialized hidden nodes retain their last visible extent on the parent axis. */
function gridSizes(snapshot: WorkspaceSnapshot): Map<string, WorkspaceBounds> {
  const sizes = new Map<string, WorkspaceBounds>();
  function visit(node: WorkspaceNode, axis: Axis, box: WorkspaceBounds) {
    if (node.type === "leaf") {
      if (node.data.id !== "parking") sizes.set(node.data.id, box);
      return;
    }
    const dimension = axis === "HORIZONTAL" ? "width" : "height";
    node.data.forEach((child) =>
      visit(child, opposite(axis), { ...box, [dimension]: child.size ?? box[dimension] }),
    );
  }
  visit(snapshot.layout.grid.root, snapshot.layout.grid.orientation, snapshot.layout.grid);
  return sizes;
}
function sideGroups(snapshot: WorkspaceSnapshot, side: WorkspaceSide): string[] {
  function visit(
    node: WorkspaceNode,
    axis: Axis,
  ): { canvas: boolean; groups: string[]; side: string[] } {
    if (node.type === "leaf")
      return {
        canvas: node.data.id === "canvas",
        groups: node.data.id === "parking" ? [] : [node.data.id],
        side: [],
      };
    const children = node.data.map((child) => visit(child, opposite(axis)));
    const canvasIndex = children.findIndex((child) => child.canvas);
    return {
      canvas: canvasIndex !== -1,
      groups: children.flatMap((child) => child.groups),
      side:
        canvasIndex === -1
          ? []
          : [
              ...children[canvasIndex].side,
              ...(axis === "HORIZONTAL"
                ? children
                    .filter((_child, index) =>
                      side === "left" ? index < canvasIndex : index > canvasIndex,
                    )
                    .flatMap((child) => child.groups)
                : []),
            ],
    };
  }
  return visit(snapshot.layout.grid.root, snapshot.layout.grid.orientation).side;
}
function shape(tree: Tree): unknown {
  return "group" in tree
    ? { id: tree.group.id, views: tree.group.views, activeView: tree.group.activeView }
    : { axis: tree.axis, children: tree.children.map(shape) };
}
function groupsIn(node: WorkspaceNode): WorkspaceGroup[] {
  return node.type === "leaf" ? [node.data] : node.data.flatMap(groupsIn);
}
function findNode(node: WorkspaceNode, id: string): WorkspaceNode | undefined {
  if (node.type === "leaf") return node.data.id === id ? node : undefined;
  return node.data.map((child) => findNode(child, id)).find(Boolean);
}
function takeCanvas(tree: Tree): { canvas: Tree | null; rest: Tree | null } {
  if ("group" in tree)
    return tree.group.id === "canvas" ? { canvas: tree, rest: null } : { canvas: null, rest: tree };
  let canvas: Tree | null = null;
  const children: Tree[] = [];
  for (const child of tree.children) {
    const taken = takeCanvas(child);
    if (taken.canvas) {
      if (canvas) throw new Error("Duplicate canvas tree");
      canvas = taken.canvas;
    }
    if (taken.rest) children.push(taken.rest);
  }
  const rest =
    children.length === 0
      ? null
      : children.length === 1
        ? { ...children[0], size: tree.size }
        : { ...tree, children };
  return { canvas, rest };
}
function normalizeTree(tree: Tree): Tree {
  if ("group" in tree) return tree;
  const children = tree.children.flatMap((child) => {
    const clean = normalizeTree(child);
    if (!("axis" in clean) || clean.axis !== tree.axis || clean.visible === false) return [clean];
    const total = clean.children.reduce((sum, item) => sum + (item.size || 1), 0);
    return clean.children.map((item) => ({
      ...item,
      size: (clean.size * (item.size || 1)) / total,
    }));
  });
  return children.length === 1 ? { ...children[0], size: tree.size } : { ...tree, children };
}
function nodeFromTree(tree: Tree): WorkspaceNode {
  const visibility = tree.visible === false ? { visible: false } : {};
  return "group" in tree
    ? { type: "leaf", data: structuredClone(tree.group), size: tree.size, ...visibility }
    : { type: "branch", data: tree.children.map(nodeFromTree), size: tree.size, ...visibility };
}
export function getCanvasPlacement(snapshot: WorkspaceSnapshot): CanvasPlacement {
  const tree = treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation, true);
  if (!tree || "group" in tree) return "center";
  const first = tree.children[0];
  const last = tree.children[tree.children.length - 1];
  if ("group" in first && first.group.id === "canvas")
    return tree.axis === "HORIZONTAL" ? "left" : "above";
  if ("group" in last && last.group.id === "canvas")
    return tree.axis === "HORIZONTAL" ? "right" : "below";
  return "center";
}
function placeCanvas(
  input: WorkspaceSnapshot,
  placement: Exclude<CanvasPlacement, "center">,
  bounds: WorkspaceBounds,
): WorkspaceSnapshot {
  const snapshot = structuredClone(input);
  const current = treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation, true);
  if (!current) throw new Error("Workspace has no visible tree");
  const { canvas, rest } = takeCanvas(current);
  if (!canvas) throw new Error("Workspace has no canvas");
  if (!rest) return snapshot;
  const horizontal = placement === "left" || placement === "right";
  const extent = horizontal ? bounds.width : bounds.height;
  const canvasSize = Math.max(horizontal ? 480 : 320, extent * 0.65);
  canvas.size = canvasSize;
  rest.size = Math.max(1, extent - canvasSize);
  const root = normalizeTree({
    axis: horizontal ? "HORIZONTAL" : "VERTICAL",
    children: placement === "left" || placement === "above" ? [canvas, rest] : [rest, canvas],
    size: horizontal ? bounds.height : bounds.width,
  });
  const node = nodeFromTree(root);
  const parking = findNode(snapshot.layout.grid.root, "parking");
  if (parking && node.type === "branch") node.data.push(structuredClone(parking));
  snapshot.layout.grid.root = node;
  snapshot.layout.grid.orientation = (
    horizontal ? "HORIZONTAL" : "VERTICAL"
  ) as WorkspaceSnapshot["layout"]["grid"]["orientation"];
  delete snapshot.layout.grid.maximizedNode;
  return sanitizeWorkspaceSnapshot(snapshot, bounds);
}
function insertionSteps(tree: Tree): { id: string; reference: string; direction: DockPosition }[] {
  const steps: { id: string; reference: string; direction: DockPosition }[] = [];
  function peel(current: Tree): Tree {
    if ("group" in current) return current;
    const nested = current.children.findIndex((child) => !("group" in child));
    if (nested !== -1) current.children[nested] = peel(current.children[nested]);
    else {
      const index = current.children.findIndex(
        (child) => "group" in child && child.group.id !== "canvas",
      );
      const referenceIndex = index === 0 ? 1 : index - 1;
      const child = current.children[index],
        reference = current.children[referenceIndex];
      if (!("group" in child) || !("group" in reference)) throw new Error("Invalid replay tree");
      steps.push({
        id: child.group.id,
        reference: reference.group.id,
        direction:
          current.axis === "HORIZONTAL"
            ? index < referenceIndex
              ? "left"
              : "right"
            : index < referenceIndex
              ? "above"
              : "below",
      });
      current.children.splice(index, 1);
    }
    return current.children.length === 1 ? { ...current.children[0], size: current.size } : current;
  }
  let remaining = structuredClone(tree);
  while (!("group" in remaining)) remaining = peel(remaining);
  if (remaining.group.id !== "canvas") throw new Error("Replay requires canvas anchor");
  return steps.reverse();
}

export interface WorkbenchLayoutExecutor {
  preserveGridSizes(): (resizedGroup?: string) => void;
  reassertReservedGroups(): void;
  syncConstraints(): void;
  capture(): WorkspaceSnapshot;
  restore(snapshot: WorkspaceSnapshot): void;
  recover(snapshot: WorkspaceSnapshot): void;
  setReturns(returns: WorkspaceSnapshot["returns"]): void;
  setPresentation(
    snapshot: Pick<WorkspaceSnapshot, "cameraPresentation" | "visibilityIntent">,
  ): void;
  setCameraPresentation(mode: CameraPresentation): void;
  getCameraPresentation(): CameraPresentation;
  applyThreeDPreset(preset: ThreeDWorkspacePresetId): void;
  show(id: PanelId): void;
  hide(id: PanelId): void;
  dock(id: PanelId, position: "left" | "right" | "below"): void;
  moveCanvas(position: "left" | "right" | "above" | "below"): void;
  toggleCanvasMaximized(): void;
  isCanvasMaximized(): boolean;
  getSides(): Record<WorkspaceSide, WorkspaceSideState>;
  toggleSide(side: WorkspaceSide): void;
  resizeSide(side: WorkspaceSide, percent: number): void;
  tab(id: PanelId, target: PanelId): void;
  float(id: PanelId): void;
  applyPreset(preset: WorkspacePresetId): void;
  enterCompact(): void;
  exitCompact(): boolean;
  resizeCompact(): void;
  isCompact(): boolean;
  isVisible(id: PanelId): boolean;
}

/** The only runtime layout controller. No panel is closed, removed or recreated. */
export function createWorkbenchLayoutExecutor(
  api: DockviewApi,
  getBounds: () => WorkspaceBounds,
): WorkbenchLayoutExecutor {
  let returns: WorkspaceSnapshot["returns"] = {};
  let cameraPresentation: CameraPresentation = "floating";
  let cameraIntent: "shown" | "hidden" = "hidden";
  let compactCameraIntent: "shown" | "hidden" | undefined;
  const requestedDockSizes = new Map<string, WorkspaceBounds>();
  let desktop: WorkspaceSnapshot | null = null;
  let restoreMaximizedSizes: ReturnType<typeof rememberGridSizes> | undefined;
  const pendingShows = new Set<SidePanelId>();
  const constraints = new WeakMap<object, string>();
  let hiddenSizes = new Map<string, WorkspaceBounds>();
  let maximizedSides: Record<WorkspaceSide, WorkspaceSideState> = { left: "empty", right: "empty" };
  const getGroup = (id: string) => api.groups.find((group) => group.id === id);
  const canvas = () => {
    const group = getGroup("canvas");
    if (
      !group ||
      group.panels.length !== 1 ||
      group.panels[0].id !== "canvas" ||
      group.api.location.type !== "grid"
    )
      throw new Error("Canvas anchor is unavailable");
    return group;
  };
  const panel = (id: PanelId) => {
    const value = api.getPanel(id);
    if (!value) throw new Error(`Missing workspace panel: ${id}`);
    return value;
  };
  function syncConstraints() {
    for (const group of api.groups) {
      if (["parking", "compact-overlay"].includes(group.id) || !group.panels.length) continue;
      const specs = group.panels.map((item) => WORKBENCH_PANEL_REGISTRY[item.id as PanelId]);
      const floating = group.api.location.type === "floating";
      const minimumWidth = Math.max(
        floating ? 320 : 0,
        ...specs.map((spec) => panelMinimumWidth(spec.id, getBounds().width)),
      );
      const minimumHeight = Math.max(floating ? 320 : 0, ...specs.map((spec) => spec.minHeight));
      const signature = `${minimumWidth}:${minimumHeight}:${floating}`;
      if (constraints.get(group) === signature) continue;
      constraints.set(group, signature);
      for (const item of group.panels) {
        const id = item.id as PanelId;
        item.api.setConstraints({
          minimumWidth: panelMinimumWidth(id, getBounds().width),
          minimumHeight: WORKBENCH_PANEL_REGISTRY[id].minHeight,
        });
      }
      group.api.setConstraints({
        minimumWidth,
        minimumHeight,
        maximumWidth: floating ? 720 : Number.POSITIVE_INFINITY,
        maximumHeight: floating ? 900 : Number.POSITIVE_INFINITY,
      });
      // Constraint changes resize the nested grid, but not a native floating window's
      // outer bounds. Apply them before capture so replay cannot enlarge the window.
      // Dockview also emits layout before updating its explicit group constraints.
      if (floating) {
        const rect = group.element.closest(".dv-resize-container")?.getBoundingClientRect();
        if (rect) {
          const width = Math.min(720, Math.max(minimumWidth, rect.width));
          const height = Math.min(900, Math.max(minimumHeight, rect.height));
          if (width !== rect.width || height !== rect.height) group.api.setSize({ width, height });
        }
      } else if (group.api.isVisible) {
        group.api.setSize({ width: group.api.width, height: group.api.height });
      }
    }
  }
  function preserveGridSizes() {
    if (desktop) return () => {};
    if (api.hasMaximizedGroup()) {
      const restore = restoreMaximizedSizes;
      return (resizedGroup?: string) => {
        if (api.hasMaximizedGroup()) return;
        restoreMaximizedSizes = undefined;
        restore?.(resizedGroup);
      };
    }
    return rememberGridSizes();
  }
  function rememberGridSizes() {
    const previous = new Map(
      api.groups
        .filter(
          (group) =>
            group.id !== "parking" && group.api.location.type === "grid" && group.api.isVisible,
        )
        .map((group) => [group.id, { width: group.api.width, height: group.api.height }]),
    );
    for (const [id, size] of hiddenSizes) {
      const group = getGroup(id);
      if (group?.api.location.type === "grid" && !group.api.isVisible) previous.set(id, size);
    }
    return (resizedGroup?: string, bounds = getBounds()) => {
      if (api.hasMaximizedGroup()) return;
      if (resizedGroup) previous.delete(resizedGroup);
      for (const [id, size] of requestedDockSizes) previous.set(id, size);
      requestedDockSizes.clear();
      syncConstraints();
      const snapshot = rawSnapshot();
      const tree = treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation);
      if (tree) sizeTree(tree, bounds, previous);
    };
  }
  function maximizeCanvas() {
    if (api.hasMaximizedGroup()) return;
    maximizedSides = getSides(true);
    restoreMaximizedSizes = rememberGridSizes();
    canvas().api.maximize();
  }
  function exitCanvasMaximized() {
    const restore = restoreMaximizedSizes;
    restoreMaximizedSizes = undefined;
    api.exitMaximizedGroup();
    restore?.(undefined, { width: api.width, height: api.height });
  }
  function stable(action: () => void, resizedPanel?: PanelId, keepMaximized = false) {
    const maximized = !desktop && keepMaximized && api.hasMaximizedGroup();
    if (!desktop) exitCanvasMaximized();
    const restoreSizes = preserveGridSizes();
    action();
    restoreSizes(resizedPanel ? panel(resizedPanel).group.id : undefined);
    if (maximized) maximizeCanvas();
  }
  function ensureParking() {
    const group =
      getGroup("parking") ??
      api.addGroup({
        id: "parking",
        direction: "right",
        skipSetActive: true,
        hideHeader: true,
        locked: "no-drop-target",
      });
    group.api.locked = "no-drop-target";
    group.header.hidden = true;
    group.api.setVisible(false);
    return group;
  }
  function ensureGroup(id: string, reference = "canvas", direction: DockPosition = "right") {
    return (
      getGroup(id) ??
      api.addGroup({ id, referenceGroup: reference, direction, skipSetActive: true })
    );
  }
  function move(id: PanelId, group: DockviewGroupPanel, index?: number) {
    if (id === "canvas") throw new Error("Canvas cannot move");
    const item = panel(id);
    if (item.group === group && (index === undefined || group.panels.indexOf(item) === index))
      return;
    item.api.moveTo({ group, position: "center", index, skipSetActive: true });
  }
  function removeEmptyGroups(except: string[] = []) {
    for (const group of [...api.groups])
      if (!group.panels.length && !except.includes(group.id) && group.id !== "canvas")
        api.removeGroup(group);
  }
  function rawSnapshot(bounds: WorkspaceBounds = getBounds()): WorkspaceSnapshot {
    // Dockview's serializer exits/reenters maximization and loses nested split sizes.
    // Serialize the corrected desktop grid, then put the canvas back immediately.
    const maximized = api.hasMaximizedGroup();
    if (maximized) exitCanvasMaximized();
    let layout: WorkspaceSnapshot["layout"];
    try {
      layout = api.toJSON() as unknown as WorkspaceSnapshot["layout"];
    } finally {
      if (maximized) maximizeCanvas();
    }
    if (maximized) layout.grid.maximizedNode = { location: [] };
    // Dockview can leave a zero-sized empty branch after moving the last legacy sibling.
    // Normalize engine output only; imported snapshots still use the strict parser.
    function prune(node: typeof layout.grid.root) {
      if (node.type !== "branch") return;
      node.data.forEach(prune);
      node.data = node.data.filter(
        (child) => !(child.type === "branch" && child.data.length === 0 && child.size === 0),
      );
    }
    prune(layout.grid.root);
    // Hiding stacked groups one by one resizes the remaining siblings. Preserve
    // the pre-collapse extents instead of serializing those intermediate sizes.
    function restoreHiddenExtents(node: WorkspaceNode, axis: Axis): WorkspaceBounds {
      if (node.type === "leaf") {
        const size = hiddenSizes.get(node.data.id);
        return (
          size ?? {
            width: getGroup(node.data.id)?.api.width ?? 0,
            height: getGroup(node.data.id)?.api.height ?? 0,
          }
        );
      }
      const sizes = node.data.map((child) => {
        const size = restoreHiddenExtents(child, opposite(axis));
        if (child.visible === false && !(child.type === "leaf" && child.data.id === "parking"))
          child.size = size[axis === "HORIZONTAL" ? "width" : "height"];
        return size;
      });
      return {
        width:
          axis === "HORIZONTAL"
            ? sizes.reduce((sum, size) => sum + size.width, 0)
            : Math.max(...sizes.map((size) => size.width)),
        height:
          axis === "VERTICAL"
            ? sizes.reduce((sum, size) => sum + size.height, 0)
            : Math.max(...sizes.map((size) => size.height)),
      };
    }
    if (hiddenSizes.size) restoreHiddenExtents(layout.grid.root, layout.grid.orientation);
    if (layout.grid.maximizedNode) {
      const findCanvas = (
        node: typeof layout.grid.root,
        path: number[] = [],
      ): number[] | undefined => {
        if (node.type === "leaf") return node.data.id === "canvas" ? path : undefined;
        return node.data.map((child, index) => findCanvas(child, [...path, index])).find(Boolean);
      };
      const location = findCanvas(layout.grid.root);
      if (location) layout.grid.maximizedNode.location = location;
    }
    if (layout.activeGroup && !getGroup(layout.activeGroup)?.api.isVisible)
      layout.activeGroup = "canvas";
    return sanitizeWorkspaceSnapshot(
      { layout, returns, cameraPresentation, visibilityIntent: { "camera-view": cameraIntent } },
      bounds,
    );
  }
  function remember(id: SidePanelId) {
    const item = panel(id),
      group = item.group;
    if (["parking", "compact-overlay"].includes(group.id)) return;
    const entry: PanelReturn = { group: group.id, index: group.panels.indexOf(item) };
    if (id === "camera-view" && group.api.location.type === "grid") {
      const layout = api.toJSON();
      const locate = (node: WorkspaceNode, axis: Axis): boolean => {
        if (node.type === "leaf") return node.data.id === group.id;
        const index = node.data.findIndex((child) => locate(child, opposite(axis)));
        if (index === -1) return false;
        if (!entry.dock) {
          const neighborIndex = node.data
            .map((child, i) => ({ child, i }))
            .filter(
              ({ child, i }) => i !== index && groupsIn(child).some((g) => g.id !== "parking"),
            )
            .sort((a, b) => Math.abs(a.i - index) - Math.abs(b.i - index))[0]?.i;
          if (neighborIndex !== undefined) {
            const reference = groupsIn(node.data[neighborIndex]).find((g) => g.id !== "parking")
              ?.views[0];
            if (reference)
              entry.dock = {
                reference,
                anchors: groupsIn(node.data[neighborIndex])
                  .filter((g) => g.id !== "parking")
                  .flatMap((g) => g.views),
                direction:
                  axis === "HORIZONTAL"
                    ? index < neighborIndex
                      ? "left"
                      : "right"
                    : index < neighborIndex
                      ? "above"
                      : "below",
                width: group.api.width,
                height: group.api.height,
              };
          }
        }
        return true;
      };
      locate(layout.grid.root as WorkspaceNode, layout.grid.orientation);
    }
    if (group.api.location.type === "floating") {
      const floating = api.toJSON().floatingGroups?.find((f) => f.data?.id === group.id);
      if (floating) {
        const bounds = getBounds(),
          p = floating.position;
        entry.position = clampWorkspaceRect(
          {
            left: "left" in p ? p.left : bounds.width - p.right - p.width,
            top: "top" in p ? p.top : bounds.height - p.bottom - p.height,
            width: p.width,
            height: p.height,
          },
          bounds,
        );
      }
    }
    returns[id] = entry;
  }
  function hide(id: PanelId) {
    if (id === "canvas") return;
    if (id === "camera-view") {
      cameraIntent = "hidden";
      if (desktop) compactCameraIntent = cameraIntent;
    }
    if (desktop) pendingShows.delete(id);
    else remember(id);
    move(id, ensureParking());
    ensureParking();
    canvas().api.setActive();
    removeEmptyGroups(["parking"]);
  }
  function defaultGroup(id: SidePanelId) {
    if (id === "ai-task" && panel("inspector").group.id !== "parking")
      return panel("inspector").group;
    const existing = getGroup(id);
    const group =
      existing && existing.id !== "canvas"
        ? existing
        : ensureGroup(id, "canvas", PANEL_DEFAULT_POSITION[id]);
    const spec = WORKBENCH_PANEL_REGISTRY[id];
    group.api.moveTo({
      group: canvas(),
      position: spec.defaultPosition === "below" ? "bottom" : spec.defaultPosition,
      skipSetActive: true,
    });
    group.api.setVisible(true);
    group.api.setSize({
      width: defaultDockWidth(getBounds().width),
      ...(id === "discussion" ? { height: spec.height } : {}),
    });
    return group;
  }
  function setFloat(item: DockviewGroupPanel | IDockviewPanel, position: WorkspaceRect) {
    if (item.id === "canvas" || item.id === "parking")
      throw new Error("Reserved group cannot float");
    // Float panels directly: a temporary grid group redistributes the docked columns.
    api.addFloatingGroup(item, {
      x: position.left,
      y: position.top,
      width: position.width,
      height: position.height,
      position: { left: position.left, top: position.top },
    });
    const group = "group" in item ? item.group : item;
    if (group.id !== "compact-overlay")
      group.api.setConstraints({
        minimumWidth: Math.min(320, getBounds().width),
        minimumHeight: Math.min(320, getBounds().height),
        maximumWidth: 720,
        maximumHeight: 900,
      });
  }
  function compactShow(id: SidePanelId) {
    for (const other of PANEL_IDS)
      if (other !== "canvas" && other !== id && panel(other).group.id !== "parking")
        move(other, ensureParking());
    const group = ensureGroup("compact-overlay");
    move(id, group);
    const position = clampWorkspaceRect(
      { left: 16, top: 16, width: 360, height: 480 },
      getBounds(),
      true,
    );
    group.api.locked = "no-drop-target";
    group.api.setConstraints({
      minimumWidth: position.width,
      maximumWidth: position.width,
      minimumHeight: position.height,
      maximumHeight: position.height,
    });
    setFloat(group, position);
    group.api.setVisible(true);
    panel(id).api.setActive();
    ensureParking();
  }
  function show(id: PanelId) {
    if (id === "canvas") {
      canvas().api.setActive();
      return;
    }
    if (id === "camera-view") {
      cameraIntent = "shown";
      if (desktop) compactCameraIntent = cameraIntent;
      if (cameraPresentation === "floating") return;
    }
    if (desktop) {
      if (
        !groupsIn(
          nodeFromTree(treeFromNode(desktop.layout.grid.root, desktop.layout.grid.orientation)!),
        ).some((g) => g.views.includes(id)) &&
        !desktop.layout.floatingGroups?.some((group) => group.data?.views.includes(id))
      )
        pendingShows.add(id);
      compactShow(id);
      return;
    }
    exitCanvasMaximized();
    const item = panel(id);
    if (item.group.id === "parking") {
      const saved = returns[id];
      const existing = saved && getGroup(saved.group);
      if (
        existing &&
        !["canvas", "parking", "compact-overlay"].includes(existing.id) &&
        (id !== "camera-view" || existing.api.location.type === "grid")
      ) {
        revealGroup(existing);
        move(id, existing, saved.index);
      } else if (saved?.dock && id === "camera-view") {
        restoreCameraDock(saved);
      } else if (saved?.position) {
        setFloat(item, clampWorkspaceRect(saved.position, getBounds()));
        const previousGroup = saved.group;
        for (const entry of Object.values(returns))
          if (entry?.group === previousGroup) entry.group = item.group.id;
      } else {
        const target = defaultGroup(id);
        revealGroup(target);
        move(id, target);
      }
    }
    revealGroup(item.group);
    item.api.setActive();
    ensureParking();
  }
  function revealGroup(group: DockviewGroupPanel) {
    if (group.api.isVisible) return;
    const snapshot = rawSnapshot();
    for (const side of ["left", "right"] as const) {
      const ids = sideGroups(snapshot, side);
      if (ids.includes(group.id)) ids.forEach((id) => getGroup(id)?.api.setVisible(true));
    }
    group.api.setVisible(true);
  }
  function float(id: PanelId) {
    if (desktop || !WORKBENCH_PANEL_REGISTRY[id].capabilities.float) return;
    exitCanvasMaximized();
    const item = panel(id);
    setFloat(
      item.group.api.location.type === "floating" && item.group.panels.length === 1
        ? item.group
        : item,
      clampWorkspaceRect({ left: 32, top: 32, width: 360, height: 480 }, getBounds()),
    );
    panel(id).api.setActive();
    ensureParking();
  }
  function dock(id: PanelId, position: "left" | "right" | "below") {
    if (desktop || id === "canvas") return;
    exitCanvasMaximized();
    if (id === "camera-view") {
      cameraPresentation = "docked";
      cameraIntent = "shown";
    }
    const group = ensureGroup(`dock-${id}`, "canvas", position);
    move(id, group);
    group.api.moveTo({
      group: canvas(),
      position: position === "below" ? "bottom" : position,
      skipSetActive: true,
    });
    group.api.setVisible(true);
    group.api.setSize(
      position === "below" ? { height: 260 } : { width: defaultDockWidth(getBounds().width) },
    );
    panel(id).api.setActive();
    ensureParking();
  }
  function tab(id: PanelId, target: PanelId) {
    if (desktop || id === "canvas" || target === "canvas") return;
    if (id === "camera-view" && panel(target).group.api.location.type === "floating") return;
    if (id === "camera-view" || target === "camera-view") changeCameraPresentation("docked");
    show(target);
    const group = panel(target).group;
    move(id, group);
    group.api.setVisible(true);
    const minimumWidth = Math.max(
      ...group.panels.map((p) => panelMinimumWidth(p.id as PanelId, getBounds().width)),
    );
    const minimumHeight = Math.max(
      ...group.panels.map((p) => WORKBENCH_PANEL_REGISTRY[p.id as PanelId].minHeight),
    );
    group.api.setConstraints({
      minimumWidth: Math.max(minimumWidth, group.api.location.type === "floating" ? 320 : 0),
      minimumHeight: Math.max(minimumHeight, group.api.location.type === "floating" ? 320 : 0),
    });
    panel(id).api.setActive();
    ensureParking();
  }
  function replay(input: WorkspaceSnapshot) {
    const bounds = getBounds();
    const snapshot = sanitizeWorkspaceSnapshot(input, bounds);
    const tree = treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation, true);
    if (!tree) throw new Error("Empty workspace tree");
    exitCanvasMaximized();
    setPresentation(snapshot);
    // Media-query layout effects can run before Dockview's ResizeObserver.
    // Release temporary replay constraints against the current host, not its old compact size.
    api.layout(bounds.width, bounds.height);
    for (const id of PANEL_IDS) if (id !== "canvas") move(id, ensureParking());
    removeEmptyGroups(["parking"]);
    for (const step of insertionSteps(tree)) ensureGroup(step.id, step.reference, step.direction);
    const docked = groupsIn(snapshot.layout.grid.root);
    for (const group of docked) {
      if (group.id === "canvas" || group.id === "parking") continue;
      const target = getGroup(group.id);
      if (!target) throw new Error("Replay group is missing");
      group.views.forEach((id, index) => move(id, target, index));
      target.api.setVisible(true);
    }
    for (const floating of snapshot.layout.floatingGroups ?? []) {
      if (!floating.data) throw new Error("Invalid floating group");
      const target = ensureGroup(floating.data.id);
      floating.data.views.forEach((id, index) => move(id as PanelId, target, index));
      setFloat(target, floating.position as WorkspaceRect);
    }
    const parking = ensureParking();
    docked
      .find((group) => group.id === "parking")
      ?.views.forEach((id, index) => move(id, parking, index));
    returns = structuredClone(snapshot.returns);
    for (const group of [
      ...docked,
      ...(snapshot.layout.floatingGroups ?? []).flatMap((f) => (f.data ? [f.data] : [])),
    ]) {
      if (group.activeView && group.id !== "parking")
        api.getPanel(group.activeView)?.api.setActive();
    }
    getGroup(snapshot.layout.activeGroup ?? "canvas")?.api.setActive();
    hiddenSizes = gridSizes(snapshot);
    syncConstraints();
    sizeTree(tree, bounds, hiddenSizes);
    const restoreSizes = rememberGridSizes();
    function hideCollapsed(node: WorkspaceNode, visible = true) {
      visible &&= node.visible !== false;
      if (node.type === "branch") node.data.forEach((child) => hideCollapsed(child, visible));
      else if (!visible) getGroup(node.data.id)?.api.setVisible(false);
    }
    hideCollapsed(snapshot.layout.grid.root);
    restoreSizes();
    getGroup(snapshot.layout.activeGroup ?? "canvas")?.api.setActive();
    if (snapshot.layout.grid.maximizedNode) maximizeCanvas();
    const actual = rawSnapshot();
    const actualTree = treeFromNode(actual.layout.grid.root, actual.layout.grid.orientation, true);
    if (!actualTree || JSON.stringify(shape(actualTree)) !== JSON.stringify(shape(tree)))
      throw new Error("Workspace replay changed the group tree");
    const expectedParking = docked.find((g) => g.id === "parking")?.views ?? [];
    if (
      JSON.stringify(getGroup("parking")?.panels.map((p) => p.id) ?? []) !==
      JSON.stringify(expectedParking)
    )
      throw new Error("Workspace replay changed hidden panels");
    const actualFloats = api.toJSON().floatingGroups ?? [];
    const expectedFloats = snapshot.layout.floatingGroups ?? [];
    if (actualFloats.length !== expectedFloats.length)
      throw new Error("Workspace replay changed floating groups");
    for (const expected of expectedFloats) {
      const actualFloat = actualFloats.find((f) => f.data?.id === expected.data?.id);
      if (
        !actualFloat?.data ||
        JSON.stringify(actualFloat.data.views) !== JSON.stringify(expected.data?.views) ||
        actualFloat.data.activeView !== expected.data?.activeView
      )
        throw new Error("Workspace replay changed floating tabs");
      const p = actualFloat.position,
        expectedPosition = expected.position as WorkspaceRect;
      const measured: WorkspaceRect = {
        width: p.width,
        height: p.height,
        left: "left" in p ? p.left : getBounds().width - p.right - p.width,
        top: "top" in p ? p.top : getBounds().height - p.bottom - p.height,
      };
      if (
        (["left", "top", "width", "height"] as const).some(
          (key) => Math.abs(measured[key] - expectedPosition[key]) > 1,
        )
      )
        throw new Error("Workspace floating replay exceeded one pixel tolerance");
    }
  }
  function setPresentation(
    snapshot: Pick<WorkspaceSnapshot, "cameraPresentation" | "visibilityIntent">,
  ) {
    cameraPresentation = snapshot.cameraPresentation;
    cameraIntent = snapshot.visibilityIntent["camera-view"];
  }
  function restoreCameraDock(saved: PanelReturn) {
    const dock = saved.dock!;
    let groupId = saved.group;
    for (let index = 0; getGroup(groupId); index++) groupId = `camera-return-${index}`;
    const snapshot = rawSnapshot();
    const tree = treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation, true)!;
    const members = (node: Tree): PanelId[] =>
      "group" in node ? node.group.views : node.children.flatMap(members);
    const available = new Set(members(tree));
    const anchors = dock.anchors.filter((id) => available.has(id));
    if (!anchors.length) anchors.push("canvas");
    const axis: Axis =
      dock.direction === "left" || dock.direction === "right" ? "HORIZONTAL" : "VERTICAL";
    const insert = (node: Tree): Tree => {
      if ("children" in node) {
        const childIndex = node.children.findIndex((child) =>
          anchors.every((id) => members(child).includes(id)),
        );
        if (childIndex !== -1) {
          node.children[childIndex] = insert(node.children[childIndex]);
          return node;
        }
      }
      const size = axis === "HORIZONTAL" ? dock.width : dock.height;
      const extent = axis === "HORIZONTAL" ? getBounds().width : getBounds().height;
      const camera: Tree = {
        group: { id: groupId, views: ["camera-view"], activeView: "camera-view" },
        size,
      };
      const target = { ...node, size: Math.max(1, extent - size) };
      return {
        axis,
        size: node.size,
        children:
          dock.direction === "left" || dock.direction === "above"
            ? [camera, target]
            : [target, camera],
      };
    };
    const root = normalizeTree(insert(tree));
    const parking = structuredClone(findNode(snapshot.layout.grid.root, "parking")!);
    if (parking.type === "leaf") {
      parking.data.views = parking.data.views.filter((id) => id !== "camera-view");
      if (parking.data.activeView === "camera-view")
        parking.data.activeView = parking.data.views[0];
    }
    const node = nodeFromTree(root);
    if (node.type !== "branch" || !("axis" in root))
      throw new Error("Camera dock requires a branch");
    node.data.push(parking);
    snapshot.layout.grid.root = node;
    snapshot.layout.grid.orientation =
      root.axis as WorkspaceSnapshot["layout"]["grid"]["orientation"];
    delete snapshot.layout.grid.maximizedNode;
    snapshot.layout.activeGroup = groupId;
    replay(snapshot);
    requestedDockSizes.set(groupId, { width: dock.width, height: dock.height });
  }
  function changeCameraPresentation(mode: CameraPresentation) {
    if (desktop) return;
    if (mode === "floating") {
      hide("camera-view");
      cameraPresentation = mode;
      cameraIntent = "shown";
    } else {
      cameraPresentation = mode;
      show("camera-view");
    }
  }
  function sizeTree(tree: Tree, bounds: WorkspaceBounds, previous?: Map<string, WorkspaceBounds>) {
    const sizes = new Map<string, WorkspaceRect>();
    const containsCanvas = (node: Tree): boolean =>
      "group" in node ? node.group.id === "canvas" : node.children.some(containsCanvas);
    const hasPreviousSize = (node: Tree): boolean =>
      "group" in node ? !!previous?.has(node.group.id) : node.children.some(hasPreviousSize);
    const extent = (node: Tree, dimension: "width" | "height"): number => {
      if ("group" in node)
        return (
          previous?.get(node.group.id)?.[dimension] ??
          (previous && dimension === "width"
            ? defaultDockWidth(bounds.width)
            : getGroup(node.group.id)!.api[dimension])
        );
      const alongAxis = (node.axis === "HORIZONTAL") === (dimension === "width");
      // Stacking into an existing column/row inherits its cross-axis size, not
      // the moved group's temporary size after Dockview redistributes the grid.
      const retained = alongAxis ? [] : node.children.filter(hasPreviousSize);
      const values = (retained.length ? retained : node.children).map((child) =>
        extent(child, dimension),
      );
      return alongAxis ? values.reduce((sum, value) => sum + value, 0) : Math.max(...values);
    };
    const limits = (node: Tree, dimension: "Width" | "Height", maximum = false): number => {
      if ("group" in node) {
        const group = getGroup(node.group.id)!;
        return maximum ? group[`maximum${dimension}`] : group[`minimum${dimension}`];
      }
      const values = node.children.map((child) => limits(child, dimension, maximum));
      const alongAxis = (node.axis === "HORIZONTAL") === (dimension === "Width");
      return alongAxis
        ? values.reduce((sum, size) => sum + size, 0)
        : maximum
          ? Math.min(...values)
          : Math.max(...values);
    };
    function measure(node: Tree, box: WorkspaceRect) {
      if ("group" in node) {
        sizes.set(node.group.id, box);
        return;
      }
      const dimension = node.axis === "HORIZONTAL" ? "Width" : "Height";
      const axis = node.axis === "HORIZONTAL" ? "width" : "height";
      const coordinate = node.axis === "HORIZONTAL" ? "left" : "top";
      const minimum = node.children.map((child) => limits(child, dimension));
      const maximum = node.children.map((child) => limits(child, dimension, true));
      let available = Math.max(
        box[axis],
        minimum.reduce((sum, size) => sum + size, 0),
      );
      const canvasIndex = node.children.findIndex(containsCanvas);
      // A newly stacked pair shares its column equally; the retained panel's
      // pre-drop full height must not outweigh the incoming panel's split height.
      const splitColumn =
        previous &&
        axis === "height" &&
        canvasIndex === -1 &&
        node.children.length === 2 &&
        node.children.some((child) => !hasPreviousSize(child));
      const weights = node.children.map((child) =>
        splitColumn ? 1 : previous ? extent(child, axis) : child.size || 1,
      );
      if (previous && canvasIndex !== -1)
        weights[canvasIndex] = Math.max(
          1,
          available -
            weights.reduce((sum, value, index) => sum + (index === canvasIndex ? 0 : value), 0),
        );
      const fixed = new Map<number, number>();
      if (previous && canvasIndex !== -1) {
        const requested = weights.map((value, index) =>
          Math.min(maximum[index], Math.max(minimum[index], value)),
        );
        const sideSpace = requested.reduce(
          (sum, value, index) => sum + (index === canvasIndex ? 0 : value),
          0,
        );
        if (sideSpace + minimum[canvasIndex] <= available) {
          requested.forEach((value, index) => {
            if (index !== canvasIndex) fixed.set(index, value);
          });
          available -= sideSpace;
        }
      }
      // Redistribute only space left after a child hits its registry constraint.
      while (fixed.size < node.children.length) {
        const weight = node.children.reduce(
          (sum, _child, index) => sum + (fixed.has(index) ? 0 : weights[index]),
          0,
        );
        const unclamped = available;
        let changed = false;
        node.children.forEach((child, index) => {
          if (fixed.has(index)) return;
          const requested = (unclamped * weights[index]) / weight;
          if (requested < minimum[index] || requested > maximum[index]) {
            const size = Math.min(maximum[index], Math.max(minimum[index], requested));
            fixed.set(index, size);
            available -= size;
            changed = true;
          }
        });
        if (!changed) {
          node.children.forEach((child, index) => {
            if (!fixed.has(index)) fixed.set(index, (available * weights[index]) / weight);
          });
        }
      }
      let offset = box[coordinate];
      node.children.forEach((child, index) => {
        const size = fixed.get(index)!;
        measure(child, { ...box, [coordinate]: offset, [axis]: size });
        offset += size;
      });
    }
    measure(tree, { ...bounds, left: 0, top: 0 });
    const constraints = [...sizes].map(([id, rect]) => {
      const group = getGroup(id)!;
      return {
        group,
        rect,
        previous: {
          minimumWidth: group.minimumWidth,
          maximumWidth: group.maximumWidth,
          minimumHeight: group.minimumHeight,
          maximumHeight: group.maximumHeight,
        },
      };
    });
    // A leaf setSize only bubbles two levels in Dockview. Fixed leaf constraints
    // aggregate through every ancestor, so deep splits restore without private APIs.
    try {
      for (const { group, rect } of constraints) {
        group.api.setConstraints({
          minimumWidth: rect.width,
          maximumWidth: rect.width,
          minimumHeight: rect.height,
          maximumHeight: rect.height,
        });
        group.api.setSize({ width: rect.width, height: rect.height });
      }
    } finally {
      for (const { group, previous } of constraints) group.api.setConstraints(previous);
      // Constraint events fire before Dockview updates the group's explicit limits.
      // Refresh every split after all temporary fixed sizes have been released.
      for (const { group, rect } of constraints)
        group.api.setSize({ width: rect.width, height: rect.height });
    }
    for (const { group, rect } of constraints) {
      if (
        Math.abs(group.api.width - rect.width) > 1 ||
        Math.abs(group.api.height - rect.height) > 1
      )
        throw new Error("Workspace replay exceeded one pixel tolerance");
      const actual = group.api.boundingBox;
      // jsdom has no client rectangles; the real browser gate also checks position.
      if (
        actual &&
        actual.width > 0 &&
        actual.height > 0 &&
        (Math.abs(actual.left - rect.left) > 1 || Math.abs(actual.top - rect.top) > 1)
      )
        throw new Error("Workspace replay changed group position");
    }
  }

  function getSides(restoringDesktop = false): Record<WorkspaceSide, WorkspaceSideState> {
    if (desktop && !restoringDesktop) return { left: "empty", right: "empty" };
    if (api.hasMaximizedGroup()) return maximizedSides;
    const snapshot = rawSnapshot();
    const visible = new Set(
      groupsIn(
        nodeFromTree(treeFromNode(snapshot.layout.grid.root, snapshot.layout.grid.orientation)!),
      ).map((group) => group.id),
    );
    const state = (side: WorkspaceSide): WorkspaceSideState => {
      const groups = sideGroups(snapshot, side);
      return !groups.length ? "empty" : groups.some((id) => visible.has(id)) ? "open" : "collapsed";
    };
    return { left: state("left"), right: state("right") };
  }

  return {
    preserveGridSizes,
    reassertReservedGroups() {
      const parking = getGroup("parking");
      if (parking && parking.api.location.type === "grid") parking.api.setVisible(false);
    },
    syncConstraints,
    capture() {
      if (desktop) return structuredClone(desktop);
      return rawSnapshot();
    },
    restore(snapshot) {
      if (desktop) throw new Error("Desktop restore is disabled in compact mode");
      replay(snapshot);
    },
    recover(snapshot) {
      desktop = null;
      pendingShows.clear();
      replay(snapshot);
    },
    setReturns(value) {
      returns = structuredClone(value);
      hiddenSizes.clear();
      hiddenSizes = gridSizes(rawSnapshot());
    },
    setPresentation,
    setCameraPresentation: (mode) => stable(() => changeCameraPresentation(mode)),
    getCameraPresentation: () => cameraPresentation,
    applyThreeDPreset(preset) {
      if (desktop) return;
      stable(() => {
        if (preset === "box-refinement") {
          hide("camera-view");
          show("tri-view");
        } else if (preset === "sensor-fusion") {
          hide("tri-view");
          show("camera-view");
        } else {
          hide("tri-view");
          hide("camera-view");
        }
      });
    },
    show: (id) => stable(() => show(id)),
    hide: (id) => stable(() => hide(id), undefined, true),
    dock: (id, position) => stable(() => dock(id, position), id),
    moveCanvas(position) {
      if (desktop) return;
      const next = placeCanvas(rawSnapshot(), position, getBounds());
      replay(next);
    },
    toggleCanvasMaximized() {
      if (desktop) return;
      if (api.hasMaximizedGroup()) exitCanvasMaximized();
      else maximizeCanvas();
    },
    isCanvasMaximized: () => api.hasMaximizedGroup(),
    getSides,
    resizeSide(side, percent) {
      if (desktop || !Number.isFinite(percent) || api.hasMaximizedGroup()) return;
      const width = (getBounds().width * Math.min(35, Math.max(10, percent))) / 100;
      stable(() => {
        for (const id of sideGroups(rawSnapshot(), side)) {
          const group = getGroup(id);
          if (group?.api.isVisible) requestedDockSizes.set(id, { width, height: group.api.height });
        }
      });
    },
    toggleSide(side) {
      if (desktop) return;
      exitCanvasMaximized();
      const groups = sideGroups(rawSnapshot(), side).map((id) => getGroup(id)!);
      const expand = !groups.some((group) => group.api.isVisible);
      const restoreSizes = rememberGridSizes();
      for (const group of groups)
        if (group.api.isVisible)
          hiddenSizes.set(group.id, { width: group.api.width, height: group.api.height });
      for (const group of groups) group.api.setVisible(expand);
      canvas().api.setActive();
      restoreSizes();
    },
    tab: (id, target) => stable(() => tab(id, target)),
    float: (id) => stable(() => float(id)),
    applyPreset(preset) {
      if (desktop) return;
      if (preset === "focus") {
        if (api.hasMaximizedGroup()) exitCanvasMaximized();
        else maximizeCanvas();
      } else {
        // Presets own their complete topology, including the canvas position.
        replay(createWorkspacePreset(preset, getBounds()));
      }
    },
    enterCompact() {
      if (desktop) return;
      // The host may already be narrow while Dockview still contains the desktop tree.
      // Latch floating/return rectangles in that tree's coordinate space before projecting it.
      desktop = rawSnapshot({ width: api.width, height: api.height });
      pendingShows.clear();
      compactCameraIntent = undefined;
      exitCanvasMaximized();
      for (const id of PANEL_IDS) if (id !== "canvas") move(id, ensureParking());
      ensureParking();
      canvas().api.setActive();
    },
    exitCompact() {
      if (!desktop) return false;
      const saved = desktop;
      const cameraChange = compactCameraIntent;
      // Keep the latch if replay throws: the caller enters read-only recovery and never writes the projection.
      replay(saved);
      desktop = null;
      const changed = pendingShows.size > 0 || cameraChange !== undefined;
      if (changed)
        stable(() => {
          for (const id of pendingShows) show(id);
        });
      pendingShows.clear();
      if (cameraChange !== undefined) {
        stable(() => (cameraChange === "shown" ? show("camera-view") : hide("camera-view")));
      }
      compactCameraIntent = undefined;
      return changed;
    },
    resizeCompact() {
      const active = getGroup("compact-overlay")?.activePanel?.id;
      if (desktop && active && active !== "canvas") compactShow(active as SidePanelId);
    },
    isCompact: () => desktop !== null,
    isVisible: (id) =>
      id === "camera-view" && cameraPresentation === "floating"
        ? cameraIntent === "shown"
        : panel(id).group.id !== "parking" && panel(id).group.api.isVisible,
  };
}
