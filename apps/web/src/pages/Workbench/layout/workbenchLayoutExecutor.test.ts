import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDockview, type DockviewApi } from "dockview-react";
import { createWorkbenchLayoutExecutor, getCanvasPlacement } from "./workbenchLayoutExecutor";
import {
  createWorkspacePreset,
  getActiveWorkspacePreset,
  migrateLegacyWorkspace,
} from "./workbenchLayoutPresets";
import {
  PANEL_IDS,
  type WorkspaceBounds,
  type WorkspaceNode,
  type WorkspaceSnapshot,
} from "./workbenchLayoutSnapshot";
import { WORKBENCH_PANEL_REGISTRY } from "./workbenchPanelRegistry";

describe("workspace executor with Dockview 8", () => {
  let api: DockviewApi;
  let bounds: WorkspaceBounds;
  let element: HTMLDivElement;
  beforeEach(() => {
    bounds = { width: 1600, height: 900 };
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
    element = document.createElement("div");
    document.body.append(element);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (
      this: HTMLElement,
    ) {
      if (
        this.classList.contains("dv-resize-container") ||
        this.classList.contains("dv-floating-overlay-host")
      )
        return new DOMRect(
          parseFloat(this.style.left || this.style.inset.split(/\s+/)[3]) || 0,
          parseFloat(this.style.top || this.style.inset.split(/\s+/)[0]) || 0,
          parseFloat(this.style.width) || 0,
          parseFloat(this.style.height) || 0,
        );
      if (
        this === element ||
        this.classList.contains("dv-dockview") ||
        this.firstElementChild?.classList.contains("dv-dockview") ||
        this.classList.contains("dv-shell")
      )
        return new DOMRect(0, 0, bounds.width, bounds.height);
      return new DOMRect();
    });
    api = createDockview(element, {
      createComponent: () => ({ element: document.createElement("div"), init: () => undefined }),
      disableAutoResizing: true,
    });
    api.layout(bounds.width, bounds.height);
    api.fromJSON(createWorkspacePreset("standard", bounds).layout);
  });
  it("resizes a visible sidebar from settings while preserving the opposite side", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const rightWidth = api.getPanel("inspector")!.group.api.width;
    controller.resizeSide("left", 22);
    expect(api.getPanel("task-queue")!.group.api.width).toBeCloseTo(352, 0);
    expect(api.getPanel("class-palette")!.group.api.width).toBeCloseTo(352, 0);
    expect(api.getPanel("inspector")!.group.api.width).toBe(rightWidth);
  });

  it.each([1100, 1440, 1600])("applies 15%% sidebars in every preset at %ipx", (width) => {
    bounds.width = width;
    api.layout(bounds.width, bounds.height);
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    for (const preset of ["standard", "review", "ai-review", "video-tracking"] as const) {
      controller.applyPreset(preset);
      const context = preset === "video-tracking" ? "annotate:video" : "annotate:image";
      expect(getActiveWorkspacePreset(controller.capture(), context)).toBe(preset);
      const right = api.getPanel("inspector")!.group.api.width;
      expect(Math.abs(right - width * 0.15), preset).toBeLessThanOrEqual(1);
      if (preset !== "review")
        expect(
          Math.abs(api.getPanel("task-queue")!.group.api.width - width * 0.15),
          preset,
        ).toBeLessThanOrEqual(1);
      if (preset === "review" || preset === "ai-review")
        expect(api.getPanel("discussion")!.api.width).toBe(width);
    }
  });

  afterEach(() => {
    api?.dispose();
    element?.remove();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("switches every camera together and restores gallery topology and manual size without recreating panels", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const panels = PANEL_IDS.map((id) => api.getPanel(id));
    controller.show("camera-view");
    expect(controller.isVisible("camera-view")).toBe(true);
    expect(api.getPanel("camera-view")!.group.id).toBe("parking");
    controller.setCameraPresentation("docked");
    expect(api.getPanel("camera-view")!.api.width).toBe(240);
    api.getPanel("camera-view")!.group.api.setSize({ width: 310 });
    const before = controller.capture();
    controller.setCameraPresentation("floating");
    expect(controller.isVisible("camera-view")).toBe(true);
    expect(api.getPanel("camera-view")!.group.id).toBe("parking");
    controller.setCameraPresentation("docked");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    expect(api.getPanel("camera-view")!.api.width).toBe(310);
    controller.hide("camera-view");
    expect(controller.getCameraPresentation()).toBe("docked");
    expect(controller.isVisible("camera-view")).toBe(false);
    controller.show("camera-view");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    expect(PANEL_IDS.map((id) => api.getPanel(id))).toEqual(panels);
  });

  it("restores a gallery column beside a complete vertically split branch", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.setCameraPresentation("docked");
    controller.moveCanvas("left");
    const before = controller.capture();
    controller.setCameraPresentation("floating");
    controller.setCameraPresentation("docked");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
  });

  it("returns cameras to a new dock if their former tab group has become floating", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.setCameraPresentation("docked");
    controller.tab("camera-view", "inspector");
    controller.setCameraPresentation("floating");
    controller.float("inspector");
    controller.setCameraPresentation("docked");
    expect(api.getPanel("camera-view")!.api.location.type).toBe("grid");
    expect(api.getPanel("inspector")!.api.location.type).toBe("floating");
    expect(controller.capture().cameraPresentation).toBe("docked");
  });

  it("keeps camera modes, hidden intent and 3D presets independent of physical side projection", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.setCameraPresentation("docked");
    controller.tab("camera-view", "inspector");
    controller.toggleSide("right");
    expect(controller.capture().visibilityIntent["camera-view"]).toBe("shown");
    controller.toggleSide("right");
    controller.float("camera-view");
    expect(api.getPanel("camera-view")!.api.location.type).toBe("grid");
    controller.applyThreeDPreset("box-refinement");
    expect(controller.isVisible("tri-view")).toBe(true);
    expect(controller.isVisible("camera-view")).toBe(false);
    controller.applyThreeDPreset("sensor-fusion");
    expect(controller.isVisible("tri-view")).toBe(false);
    expect(controller.isVisible("camera-view")).toBe(true);
    expect(api.getPanel("camera-view")!.group).toBe(api.getPanel("inspector")!.group);
    controller.setCameraPresentation("floating");
    controller.enterCompact();
    controller.hide("camera-view");
    expect(controller.exitCompact()).toBe(true);
    expect(controller.isVisible("camera-view")).toBe(false);
    controller.show("camera-view");
    expect(api.getPanel("camera-view")!.group.id).toBe("parking");
  });

  it("collapses physical sides without losing stacked sizes, even after restore and maximization", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.moveCanvas("left");
    const before = controller.capture();
    const sizes = new Map(
      api.groups.filter((g) => g.id !== "parking").map((g) => [g.id, [g.api.width, g.api.height]]),
    );
    expect(controller.getSides()).toEqual({ left: "empty", right: "open" });
    controller.toggleSide("right");
    expect(controller.getSides()).toEqual({ left: "empty", right: "collapsed" });
    expect(api.getPanel("canvas")!.api.width).toBe(bounds.width);
    const collapsed = controller.capture();
    controller.toggleCanvasMaximized();
    controller.toggleCanvasMaximized();
    controller.enterCompact();
    controller.exitCompact();
    controller.restore(collapsed);
    api.fromJSON(collapsed.layout, { reuseExistingPanels: true });
    controller.setReturns(collapsed.returns);
    controller.toggleSide("right");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    for (const [id, size] of sizes) {
      const group = api.groups.find((g) => g.id === id)!;
      expect([group.api.width, group.api.height]).toEqual(size);
    }
  });

  it("uses the current physical side and preserves the opposite side and floats", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.dock("task-queue", "right");
    controller.dock("inspector", "left");
    controller.float("discussion");
    const before = controller.capture();
    controller.toggleSide("left");
    expect(controller.isVisible("task-queue")).toBe(true);
    expect(controller.isVisible("inspector")).toBe(false);
    expect(controller.isVisible("discussion")).toBe(true);
    controller.show("inspector");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    controller.toggleSide("left");
    controller.toggleSide("right");
    controller.toggleSide("left");
    controller.toggleSide("right");
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    expect(controller.capture().layout.floatingGroups).toEqual(before.layout.floatingGroups);
  });

  it("can move the canvas while a side is collapsed and reopen it on the new side", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.toggleSide("left");
    controller.moveCanvas("left");
    expect(controller.getSides()).toEqual({ left: "empty", right: "open" });
    expect(controller.isVisible("task-queue")).toBe(false);
    controller.toggleSide("right");
    controller.toggleSide("right");
    expect(controller.isVisible("task-queue")).toBe(true);
    expect(controller.isVisible("class-palette")).toBe(true);
  });

  it("keeps physical side controls available after returning to a maximized desktop", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.toggleSide("left");
    const sides = controller.getSides();
    controller.toggleCanvasMaximized();
    controller.enterCompact();
    controller.exitCompact();
    expect(controller.isCanvasMaximized()).toBe(true);
    expect(controller.getSides()).toEqual(sides);
  });

  it("expands a collapsed side when restoring a parked tab into its original group", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.tab("discussion", "task-queue");
    controller.hide("discussion");
    controller.toggleSide("left");
    controller.show("discussion");
    expect(controller.isVisible("class-palette")).toBe(true);
    expect(controller.isVisible("task-queue")).toBe(true);
    expect(controller.isVisible("discussion")).toBe(true);
  });

  it("does not reuse an old column width when a removed group ID is recreated", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    api.getPanel("inspector")!.group.api.setSize({ width: 400 });
    controller.setReturns(controller.capture().returns);
    controller.hide("inspector");
    controller.show("inspector");
    expect(api.getPanel("inspector")!.api.width).toBe(240);
  });

  it("replays presets and compact mode without recreating any panel", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const identities = PANEL_IDS.map((id) => api.getPanel(id));
    const fromJSON = vi.spyOn(api, "fromJSON");
    const removePanel = vi.spyOn(api, "removePanel");
    const desktop = controller.capture();
    controller.applyPreset("review");
    expect(api.getPanel("task-queue")?.group.id).toBe("parking");
    controller.restore(desktop);
    controller.enterCompact();
    expect(controller.capture()).toEqual(desktop);
    controller.show("discussion");
    expect(api.getPanel("discussion")?.group.id).toBe("compact-overlay");
    controller.show("inspector");
    expect(api.getPanel("discussion")?.group.id).toBe("parking");
    controller.applyPreset("review");
    controller.dock("inspector", "below");
    expect(controller.exitCompact()).toBe(false);
    expect(PANEL_IDS.map((id) => api.getPanel(id))).toEqual(identities);
    expect(api.groups.some((group) => group.id === "compact-overlay")).toBe(false);
    expect(fromJSON).not.toHaveBeenCalled();
    expect(removePanel).not.toHaveBeenCalled();
  });

  it("restores hidden tabs to their index and falls back to the deterministic sibling", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const inspector = api.groups.find((group) => group.id === "inspector")!;
    api.getPanel("discussion")!.api.moveTo({ group: inspector, position: "center", index: 0 });
    controller.hide("discussion");
    controller.show("discussion");
    expect(inspector.panels.map((p) => p.id)).toEqual(["discussion", "inspector"]);
    controller.hide("discussion");
    controller.hide("inspector");
    controller.show("discussion");
    expect(api.getPanel("discussion")?.group.id).toBe("discussion");
    expect(api.getPanel("canvas")?.group.panels.map((p) => p.id)).toEqual(["canvas"]);
  });

  it("merges existing panels into tabs through the menu command and guards canvas and compact mode", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const discussion = api.getPanel("discussion"),
      inspector = api.getPanel("inspector");
    controller.hide("inspector");
    controller.hide("discussion");
    controller.tab("discussion", "inspector");
    expect(api.getPanel("discussion")).toBe(discussion);
    expect(api.getPanel("inspector")).toBe(inspector);
    expect(discussion?.group).toBe(inspector?.group);
    expect(discussion?.group.panels.map((p) => p.id)).toEqual(["inspector", "discussion"]);
    expect(discussion?.api.isActive).toBe(true);
    expect(discussion?.group.minimumWidth).toBeGreaterThanOrEqual(220);
    controller.tab("discussion", "canvas");
    controller.tab("canvas", "inspector");
    expect(api.getPanel("canvas")?.group.panels.map((p) => p.id)).toEqual(["canvas"]);
    controller.enterCompact();
    controller.tab("task-queue", "class-palette");
    expect(api.getPanel("task-queue")?.group.id).toBe("parking");
    expect(api.groups.some((group) => group.id === "compact-overlay")).toBe(false);
  });

  it("preserves nested splits, tab order, floats, hidden items and the nine-group limit", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.dock("class-palette", "below");
    const target = api.getPanel("class-palette")!.group;
    api.getPanel("task-queue")!.api.moveTo({ group: target, position: "center", index: 0 });
    controller.float("discussion");
    controller.hide("inspector");
    api.addGroup({ id: "empty-one", referenceGroup: "canvas", direction: "above" });
    api.addGroup({ id: "empty-two", referenceGroup: "canvas", direction: "left" });
    const golden = controller.capture();
    controller.enterCompact();
    controller.show("discussion");
    controller.hide("discussion");
    controller.exitCompact();
    const result = controller.capture();
    expect(result.returns).toEqual(golden.returns);
    expect(result.layout.floatingGroups).toEqual(golden.layout.floatingGroups);
    expect(api.getPanel("task-queue")?.group.panels.map((p) => p.id)).toEqual([
      "task-queue",
      "class-palette",
    ]);
    expect(api.getPanel("inspector")?.group.id).toBe("parking");
    expect(api.groups.some((g) => g.id === "empty-one")).toBe(true);
    expect(api.groups.some((g) => g.id === "empty-two")).toBe(true);
  });

  it.each([
    "task-queue",
    "class-palette",
    "inspector",
    "discussion",
    "ai-task",
    "video-tracker",
  ] as const)(
    "keeps docked widths when floating %s, repeating the command and reopening it",
    (id) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      if (id === "ai-task" || id === "video-tracker") controller.tab(id, "inspector");
      const item = api.getPanel(id)!;
      const widths = api.panels
        .filter((panel) => panel.id !== id && panel.group.id !== "parking")
        .map((panel) => [panel, panel.api.width] as const);
      const expectWidths = () => {
        expect(api.getPanel(id)).toBe(item);
        expect(item.api.location.type).toBe("floating");
        for (const [panel, width] of widths)
          expect(panel.api.width, panel.id).toBeCloseTo(width, 0);
      };
      controller.float(id);
      expectWidths();
      controller.float(id);
      expectWidths();
      controller.hide(id);
      controller.show(id);
      expectWidths();
    },
  );

  it("rejoins hidden floating tabs when their original group no longer exists", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.float("discussion");
    controller.tab("ai-task", "discussion");
    controller.hide("ai-task");
    controller.hide("discussion");
    controller.show("discussion");
    controller.show("ai-task");
    const group = api.getPanel("discussion")!.group;
    expect(group.api.location.type).toBe("floating");
    expect(group.panels.map((panel) => panel.id)).toEqual(["discussion", "ai-task"]);
  });

  it.each(["left", "right", "below"] as const)(
    "does not widen existing sidebars when docking a hidden panel %s",
    (position) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      const sidebars = ["task-queue", "class-palette", "inspector", "discussion"].map(
        (id) => api.getPanel(id)!,
      );
      const widths = sidebars.map((panel) => panel.group.api.width);
      controller.dock("ai-task", position);
      expect(sidebars.map((panel) => panel.group.api.width)).toEqual(widths);
    },
  );

  it.each(["task-queue", "class-palette", "inspector", "discussion", "video-tracker"] as const)(
    "does not widen unrelated sidebars when showing %s",
    (id) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.hide(id);
      const sidebars = ["task-queue", "class-palette", "inspector", "discussion"]
        .filter((other) => other !== id)
        .map((other) => api.getPanel(other)!);
      const widths = sidebars.map((panel) => panel.group.api.width);
      controller.show(id);
      expect(sidebars.map((panel) => panel.group.api.width)).toEqual(widths);
    },
  );

  it("gives the canvas freed space when hiding the last panel in a sidebar", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.hide("class-palette");
    const rightWidth = api.getPanel("inspector")!.group.api.width;
    const canvasWidth = api.getPanel("canvas")!.api.width;
    controller.hide("task-queue");
    expect(api.getPanel("inspector")!.group.api.width).toBe(rightWidth);
    expect(api.getPanel("canvas")!.api.width).toBeGreaterThan(canvasWidth);
  });

  it("preserves sidebar width when hiding panels with the canvas maximized", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.syncConstraints();
    const rightWidth = api.getPanel("inspector")!.group.api.width;
    controller.toggleCanvasMaximized();
    controller.hide("class-palette");
    controller.hide("task-queue");
    expect(controller.isCanvasMaximized()).toBe(true);
    controller.toggleCanvasMaximized();
    expect(api.getPanel("inspector")!.group.api.width).toBe(rightWidth);
  });

  it("does not widen unrelated sidebars when merging tabs across columns", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.hide("class-palette");
    const rightWidth = api.getPanel("inspector")!.group.api.width;
    controller.tab("task-queue", "inspector");
    expect(api.getPanel("inspector")!.group.api.width).toBe(rightWidth);
  });

  it.each([
    ["top", false, 300],
    ["bottom", false, 450],
    ["top", true, 450],
    ["bottom", true, 300],
  ] as const)(
    "keeps target width when stacking %s (whole group: %s, source width: %s)",
    (position, wholeGroup, sourceWidth) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.syncConstraints();
      const item = api.getPanel("task-queue")!;
      const target = api.getPanel("class-palette")!.group;
      target.api.moveTo({ group: api.getPanel("canvas")!.group, position: "left" });
      item.group.api.setSize({ width: sourceWidth });
      target.api.setSize({ width: 400 });
      const width = target.api.width;
      const rightWidth = api.getPanel("inspector")!.group.api.width;
      const canvasWidth = api.getPanel("canvas")!.group.api.width;
      const restore = controller.preserveGridSizes();
      (wholeGroup ? item.group.api : item.api).moveTo({ group: target, position });
      restore(item.group.id);
      expect(item.group).not.toBe(target);
      expect(item.group.api.width).toBe(width);
      expect(target.api.width).toBe(width);
      expect(item.group.api.height).toBe(bounds.height / 2);
      expect(target.api.height).toBe(bounds.height / 2);
      expect(api.getPanel("discussion")!.group.api.width).toBe(rightWidth);
      expect(api.getPanel("canvas")!.group.api.width).toBeGreaterThan(canvasWidth);
    },
  );

  it.each(["left", "right"] as const)(
    "keeps the target row height when moving another row to its %s",
    (position) => {
      const seed = createWorkspacePreset("standard", bounds);
      const root = seed.layout.grid.root;
      if (root.type !== "branch" || root.data[0].type !== "branch") throw new Error("Bad preset");
      root.data = [...root.data[0].data, ...root.data.slice(1)];
      root.data.forEach((node, index) => {
        node.size = [180, 200, 320, 200, 0][index];
      });
      root.size = bounds.width;
      seed.layout.grid.orientation = "VERTICAL" as typeof seed.layout.grid.orientation;
      api.fromJSON(seed.layout);
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.syncConstraints();
      const item = api.getPanel("task-queue")!;
      const target = api.getPanel("class-palette")!.group;
      const height = target.api.height;
      const canvasHeight = api.getPanel("canvas")!.group.api.height;
      const restore = controller.preserveGridSizes();
      item.api.moveTo({ group: target, position });
      restore(item.group.id);
      expect(item.group.api.height).toBe(height);
      expect(target.api.height).toBe(height);
      expect(api.getPanel("canvas")!.group.api.height).toBeGreaterThan(canvasHeight);
    },
  );

  it.each(["left", "right", "above", "below"] as const)(
    "preserves peripheral geometry when the canvas is at the %s edge",
    (placement) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.syncConstraints();
      controller.moveCanvas(placement);
      controller.syncConstraints();
      const panels = ["task-queue", "class-palette", "inspector", "discussion"].map(
        (id) => api.getPanel(id)!,
      );
      const geometry = () => panels.map((panel) => [panel.group.api.width, panel.group.api.height]);
      const before = geometry();
      controller.dock("ai-task", "right");
      expect(geometry()).toEqual(before);
    },
  );

  it("preserves widths across the temporary root split used by native edge drops", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.show("ai-task");
    const panels = ["task-queue", "class-palette", "inspector", "discussion"].map(
      (id) => api.getPanel(id)!,
    );
    const widths = panels.map((panel) => panel.group.api.width);
    const restoreSizes = controller.preserveGridSizes();
    const target = api.addGroup({ id: "edge-drop", direction: "left" });
    api.getPanel("ai-task")!.api.moveTo({ group: target, position: "center" });
    restoreSizes(target.id);
    expect(panels.map((panel) => panel.group.api.width)).toEqual(widths);
    expect(target.api.width).toBe(Math.max(bounds.width * 0.15, target.minimumWidth));
  });

  it.each([1600, 1920])("starts new columns at 15 percent in a %spx workspace", (width) => {
    bounds = { width, height: 900 };
    api.layout(bounds.width, bounds.height);
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.dock("class-palette", "left");
    expect(api.getPanel("class-palette")!.group.api.width).toBe(Math.round(width * 0.15));
    controller.hide("inspector");
    controller.show("inspector");
    expect(api.getPanel("inspector")!.group.api.width).toBe(Math.round(width * 0.15));
  });

  it("keeps focused canvas mounted while maximizing and restoring", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.syncConstraints();
    controller.tab("task-queue", "class-palette");
    controller.show("class-palette");
    controller.moveCanvas("left");
    const geometry = () => api.groups.map((group) => [group.id, group.api.width, group.api.height]);
    const before = geometry();
    const canvas = api.getPanel("canvas");
    controller.applyPreset("focus");
    expect(api.hasMaximizedGroup()).toBe(true);
    controller.applyPreset("focus");
    expect(api.hasMaximizedGroup()).toBe(false);
    controller.applyPreset("focus");
    controller.show("inspector");
    expect(api.hasMaximizedGroup()).toBe(false);
    expect(geometry()).toEqual(before);
    controller.show("task-queue");
    controller.show("task-queue");
    expect(api.getPanel("task-queue")!.group.activePanel?.id).toBe("task-queue");
    expect(controller.isVisible("task-queue")).toBe(true);
    expect(geometry()).toEqual(before);
    expect(api.getPanel("canvas")).toBe(canvas);
  });

  it.each(["toggle", "focus"] as const)(
    "preserves grid sizes through repeated %s, capture and restore",
    (command) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.syncConstraints();
      const before = controller.capture();
      const toggle = () =>
        command === "toggle" ? controller.toggleCanvasMaximized() : controller.applyPreset("focus");
      for (let cycle = 0; cycle < 3; cycle++) {
        toggle();
        for (let save = 0; save < 2; save++) {
          const maximized = controller.capture();
          expect(maximized.layout.grid.maximizedNode).toBeDefined();
          expect(maximized.layout.grid.root).toEqual(before.layout.grid.root);
          expect(controller.isCanvasMaximized()).toBe(true);
        }
        toggle();
        expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
      }
      toggle();
      const saved = controller.capture();
      controller.applyPreset("review");
      controller.restore(saved);
      expect(controller.isCanvasMaximized()).toBe(true);
      toggle();
      expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    },
  );

  it("preserves maximized grid sizes across compact mode before the engine resizes", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.syncConstraints();
    const before = controller.capture();
    controller.toggleCanvasMaximized();
    bounds = { width: 1024, height: 900 };
    controller.enterCompact();
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
    api.layout(bounds.width, bounds.height);
    bounds = { width: 1600, height: 900 };
    controller.exitCompact();
    expect(controller.isCanvasMaximized()).toBe(true);
    controller.toggleCanvasMaximized();
    expect(controller.capture().layout.grid.root).toEqual(before.layout.grid.root);
  });

  it("preserves sidebars when a floating panel is dragged into the maximized grid", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.float("ai-task");
    const panels = ["task-queue", "class-palette", "inspector", "discussion"].map(
      (id) => api.getPanel(id)!,
    );
    const widths = panels.map((panel) => panel.group.api.width);
    controller.toggleCanvasMaximized();
    const item = api.getPanel("ai-task")!;
    const resizeFloat = controller.preserveGridSizes();
    item.group.api.setSize({ width: 500, height: 400 });
    resizeFloat();
    expect(controller.isCanvasMaximized()).toBe(true);
    expect(controller.capture().layout.floatingGroups?.[0].position).toMatchObject({
      width: 500,
      height: 400,
    });
    const restoreSizes = controller.preserveGridSizes();
    item.api.moveTo({ group: api.getPanel("canvas")!.group, position: "left" });
    restoreSizes(item.group.id);
    expect(controller.isCanvasMaximized()).toBe(false);
    expect(panels.map((panel) => panel.group.api.width)).toEqual(widths);
  });

  it("keeps sidebar widths when the workspace grows while maximized", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.syncConstraints();
    const rightWidth = api.getPanel("inspector")!.group.api.width;
    controller.toggleCanvasMaximized();
    bounds = { width: 1920, height: 1080 };
    api.layout(bounds.width, bounds.height);
    expect(controller.capture().layout.grid).toMatchObject(bounds);
    controller.toggleCanvasMaximized();
    expect(api.getPanel("inspector")!.group.api.width).toBe(rightWidth);
    expect(api.getPanel("canvas")!.group.api.width).toBe(1920 - 240 - rightWidth);
  });

  it.each(["preset", "left-maximize", "stack"] as const)(
    "keeps splitters draggable after %s restores panel sizes",
    (action) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      controller.syncConstraints();
      if (action === "preset") controller.applyPreset("standard");
      else if (action === "left-maximize") {
        controller.moveCanvas("left");
        controller.toggleCanvasMaximized();
        controller.toggleCanvasMaximized();
      } else {
        const item = api.getPanel("task-queue")!;
        const restore = controller.preserveGridSizes();
        item.api.moveTo({ group: api.getPanel("inspector")!.group, position: "bottom" });
        restore(item.group.id);
      }
      const visibleGroups = api.groups.filter(
        (group) => group.api.location.type === "grid" && group.api.isVisible,
      );
      expect(element.querySelectorAll(".dv-sash.dv-enabled")).toHaveLength(
        visibleGroups.length - 1,
      );
    },
  );

  it.each(["left", "right", "above", "below"] as const)(
    "restores the standard columns after the canvas was moved %s",
    (position) => {
      const controller = createWorkbenchLayoutExecutor(api, () => bounds);
      const identities = PANEL_IDS.map((id) => api.getPanel(id));
      controller.moveCanvas(position);
      expect(getCanvasPlacement(controller.capture())).toBe(position);
      controller.applyPreset("standard");
      expect(getCanvasPlacement(controller.capture())).toBe("center");
      expect(controller.getSides()).toEqual({ left: "open", right: "open" });
      const root = controller.capture().layout.grid.root;
      if (root.type !== "branch") throw new Error("Expected standard columns");
      const views = (node: WorkspaceNode): string[] =>
        node.type === "leaf" ? node.data.views : node.data.flatMap(views);
      expect(root.data.filter((node) => node.visible !== false).map(views)).toEqual([
        ["task-queue", "class-palette"],
        ["canvas"],
        ["inspector", "discussion"],
      ]);
      expect(PANEL_IDS.map((id) => api.getPanel(id))).toEqual(identities);
    },
  );

  it("moves the existing canvas to every root edge and reapplies preset topology", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const identities = PANEL_IDS.map((id) => api.getPanel(id));
    for (const position of ["left", "right", "above", "below"] as const) {
      controller.moveCanvas(position);
      const snapshot = controller.capture();
      expect(getCanvasPlacement(snapshot)).toBe(position);
      expect(api.getPanel("canvas")?.group.id).toBe("canvas");
      expect(api.getPanel("canvas")?.group.panels.map((panel) => panel.id)).toEqual(["canvas"]);
      expect(PANEL_IDS.map((id) => api.getPanel(id))).toEqual(identities);
    }
    for (const preset of ["review", "ai-review", "video-tracking"] as const) {
      controller.moveCanvas("left");
      controller.applyPreset(preset);
      const context = preset === "video-tracking" ? "annotate:video" : "annotate:image";
      expect(getActiveWorkspacePreset(controller.capture(), context)).toBe(preset);
      expect(PANEL_IDS.map((id) => api.getPanel(id))).toEqual(identities);
    }
    controller.toggleCanvasMaximized();
    expect(controller.isCanvasMaximized()).toBe(true);
    controller.toggleCanvasMaximized();
    expect(controller.isCanvasMaximized()).toBe(false);
  });

  it("clears a failed compact latch only through explicit read-only recovery", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.enterCompact();
    vi.spyOn(api, "addGroup").mockImplementationOnce(() => {
      throw new Error("restore failed");
    });
    expect(() => controller.exitCompact()).toThrow("restore failed");
    expect(controller.isCompact()).toBe(true);
    controller.recover(createWorkspacePreset("standard", bounds));
    expect(controller.isCompact()).toBe(false);
    expect(api.getPanel("canvas")?.group.id).toBe("canvas");
  });

  it("reports a write only for deferred visibility changes after compact replay", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.applyPreset("review");
    const inspectorWidth = api.getPanel("inspector")!.group.api.width;
    controller.enterCompact();
    controller.show("task-queue");
    expect(controller.exitCompact()).toBe(true);
    expect(api.getPanel("inspector")!.group.api.width).toBeCloseTo(inspectorWidth, 0);
    expect(api.getPanel("task-queue")?.group.id).toBe("task-queue");
    controller.enterCompact();
    controller.show("class-palette");
    controller.hide("class-palette");
    expect(controller.exitCompact()).toBe(false);
    expect(api.getPanel("class-palette")?.group.id).toBe("parking");
  });

  it("replays against the new host size before a delayed Dockview ResizeObserver", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.tab("discussion", "inspector");
    controller.float("class-palette");
    const originalCanvas = api.getPanel("canvas");
    bounds = { width: 1024, height: 900 };
    controller.enterCompact();
    api.layout(bounds.width, bounds.height);
    controller.show("task-queue");
    controller.show("discussion");

    bounds = { width: 1920, height: 1080 };
    expect(api.width).toBe(1024);
    expect(() => controller.exitCompact()).not.toThrow();
    expect(api.width).toBe(bounds.width);
    expect(api.height).toBe(bounds.height);
    expect(api.getPanel("canvas")).toBe(originalCanvas);
    expect(api.getPanel("discussion")?.group).toBe(api.getPanel("inspector")?.group);
    expect(api.getPanel("class-palette")?.api.location.type).toBe("floating");
    const restored = controller.capture();
    api.layout(bounds.width, bounds.height);
    const withoutSizes = (snapshot: WorkspaceSnapshot) => {
      const clone = structuredClone(snapshot);
      const sizes: number[] = [];
      const visit = (node: WorkspaceNode) => {
        if (node.size !== undefined) {
          sizes.push(node.size);
          delete node.size;
        }
        if (node.type === "branch") node.data.forEach(visit);
      };
      visit(clone.layout.grid.root);
      return { snapshot: clone, sizes };
    };
    const before = withoutSizes(restored),
      after = withoutSizes(controller.capture());
    expect(after.snapshot).toEqual(before.snapshot);
    expect(after.sizes).toHaveLength(before.sizes.length);
    after.sizes.forEach((size, index) =>
      expect(Math.abs(size - before.sizes[index])).toBeLessThanOrEqual(1),
    );
  });

  it("sizes native floating windows to the same constraints used by saved snapshots", () => {
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    controller.syncConstraints();
    api.addFloatingGroup(api.getPanel("discussion")!, {
      x: 32,
      y: 32,
      width: 300,
      height: 300,
    });
    controller.syncConstraints();
    const before = api.toJSON().floatingGroups;
    expect(before?.[0].position).toMatchObject({ width: 320, height: 320 });
    controller.enterCompact();
    controller.exitCompact();
    expect(api.toJSON().floatingGroups).toEqual(before);
  });

  it("latches right-edge floating geometry before the host becomes compact", () => {
    bounds = { width: 1920, height: 1080 };
    api.layout(bounds.width, bounds.height);
    const seed = migrateLegacyWorkspace(
      { layout: { floatingClassPalette: { detached: true, x: 1500, y: 64, w: 360, h: 480 } } },
      bounds,
    );
    api.fromJSON(seed.layout);
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const before = controller.capture();
    expect(before.layout.floatingGroups?.[0].position).toEqual({
      left: 1500,
      top: 64,
      width: 360,
      height: 480,
    });

    bounds = { width: 1024, height: 1080 };
    expect(api.width).toBe(1920);
    controller.enterCompact();
    expect(controller.capture()).toEqual(before);
    api.layout(bounds.width, bounds.height);
    bounds = { width: 1920, height: 1080 };
    expect(controller.exitCompact()).toBe(false);
    expect(controller.capture().layout.floatingGroups).toEqual(before.layout.floatingGroups);
    controller.applyPreset("focus");
    expect(controller.capture().layout.grid.maximizedNode).toBeDefined();
  });

  it("replays four nested split levels within one pixel and reflows actual constraints", () => {
    const leaf = (id: (typeof PANEL_IDS)[number], size: number): WorkspaceNode => ({
      type: "leaf",
      size,
      data: {
        id,
        views: [id],
        activeView: id,
        ...(id === "canvas" ? { locked: true, hideHeader: true } : {}),
      },
    });
    const seed = createWorkspacePreset("standard", bounds);
    seed.layout.grid.root = {
      type: "branch",
      size: 900,
      data: [
        leaf("task-queue", 240),
        {
          type: "branch",
          size: 1360,
          data: [
            leaf("inspector", 200),
            {
              type: "branch",
              size: 700,
              data: [
                leaf("class-palette", 220),
                {
                  type: "branch",
                  size: 1140,
                  data: [leaf("canvas", 500), leaf("discussion", 200)],
                },
              ],
            },
          ],
        },
        {
          type: "leaf",
          size: 0,
          visible: false,
          data: {
            id: "parking",
            views: ["ai-task", "video-tracker", "tri-view", "camera-view"],
            activeView: "ai-task",
            locked: "no-drop-target",
            hideHeader: true,
          },
        },
      ],
    };
    api.fromJSON(seed.layout);
    for (const item of api.panels) {
      const spec = WORKBENCH_PANEL_REGISTRY[item.id as (typeof PANEL_IDS)[number]];
      item.api.setConstraints({ minimumWidth: spec.minWidth, minimumHeight: spec.minHeight });
    }
    const controller = createWorkbenchLayoutExecutor(api, () => bounds);
    const dimensions = new Map(
      api.groups
        .filter((g) => g.id !== "parking")
        .map((group) => [group.id, { width: group.api.width, height: group.api.height }]),
    );
    controller.enterCompact();
    controller.show("inspector");
    controller.exitCompact();
    for (const [id, expected] of dimensions) {
      const group = api.getGroup(id)!;
      expect(Math.abs(group.api.width - expected.width)).toBeLessThanOrEqual(1);
      expect(Math.abs(group.api.height - expected.height)).toBeLessThanOrEqual(1);
    }
    controller.enterCompact();
    bounds = { width: 1280, height: 800 };
    api.layout(bounds.width, bounds.height);
    expect(() => controller.exitCompact()).not.toThrow();
    expect(api.getPanel("canvas")!.group.api.width).toBeGreaterThanOrEqual(480);
    expect(api.getPanel("canvas")!.group.api.height).toBeGreaterThanOrEqual(320);
  });
});
