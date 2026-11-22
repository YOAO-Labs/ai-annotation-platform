import type { DockviewApi } from "dockview-react";
import type { ClientRectSnapshot } from "../stages/three-d/PointCloudTriViewPass";

type Rect = ClientRectSnapshot;

export function intersectRect(a: Rect, b: Rect): Rect | null {
  const left = Math.max(a.left, b.left);
  const top = Math.max(a.top, b.top);
  const width = Math.min(a.left + a.width, b.left + b.width) - left;
  const height = Math.min(a.top + a.height, b.top + b.height) - top;
  return width > 0 && height > 0 ? { left, top, width, height } : null;
}

export function subtractRects(rect: Rect, occluders: readonly Rect[]): Rect[] {
  return occluders.reduce<Rect[]>(
    (pieces, occluder) =>
      pieces.flatMap((piece) => {
        const overlap = intersectRect(piece, occluder);
        if (!overlap) return [piece];
        return [
          { left: piece.left, top: piece.top, width: piece.width, height: overlap.top - piece.top },
          {
            left: piece.left,
            top: overlap.top + overlap.height,
            width: piece.width,
            height: piece.top + piece.height - overlap.top - overlap.height,
          },
          {
            left: piece.left,
            top: overlap.top,
            width: overlap.left - piece.left,
            height: overlap.height,
          },
          {
            left: overlap.left + overlap.width,
            top: overlap.top,
            width: piece.left + piece.width - overlap.left - overlap.width,
            height: overlap.height,
          },
        ].filter((item) => item.width > 0 && item.height > 0);
      }),
    [rect],
  );
}

export function viewportRegionClipPath(rects: readonly Rect[], origin: Rect): string {
  if (!rects.length) return "inset(50%)";
  return `path("${rects
    .map((rect) => {
      const x = rect.left - origin.left;
      const y = rect.top - origin.top;
      return `M${x} ${y}h${rect.width}v${rect.height}h${-rect.width}Z`;
    })
    .join(" ")}")`;
}

export function createWorkbenchViewportRegions(api: DockviewApi, host: HTMLElement) {
  const masks = new Map<HTMLElement, string>();
  const occlusion = new Map<string, Rect[]>();
  let bounds: Rect = host.getBoundingClientRect();
  const clearMasks = () => {
    masks.forEach((previous, element) => {
      if (previous) element.style.setProperty("--workbench-clip", previous);
      else element.style.removeProperty("--workbench-clip");
      delete element.dataset.workbenchClipped;
    });
    masks.clear();
  };
  const mask = (element: HTMLElement, covers: Rect[]) => {
    const rect = element.getBoundingClientRect();
    if (!covers.some((cover) => intersectRect(rect, cover))) return;
    masks.set(element, element.style.getPropertyValue("--workbench-clip"));
    element.style.setProperty(
      "--workbench-clip",
      viewportRegionClipPath(subtractRects(rect, covers), rect),
    );
    element.dataset.workbenchClipped = "true";
  };
  return {
    update() {
      clearMasks();
      occlusion.clear();
      bounds = host.getBoundingClientRect();
      const groups = api.groups.filter((group) => group.id !== "parking" && group.api.isVisible);
      const floats = groups
        .flatMap((group) => {
          const wrapper = group.element.closest<HTMLElement>(".dv-resize-container");
          if (group.api.location.type !== "floating" || !wrapper) return [];
          return [
            {
              group,
              wrapper,
              rect: wrapper.getBoundingClientRect(),
              z: Number(getComputedStyle(wrapper).zIndex) || 0,
            },
          ];
        })
        .sort(
          (a, b) =>
            a.z - b.z ||
            (a.wrapper.compareDocumentPosition(b.wrapper) & Node.DOCUMENT_POSITION_FOLLOWING
              ? -1
              : 1),
        );
      for (const group of groups) {
        const active = group.activePanel;
        group.element.dataset.workbenchGpuGroup = String(
          active?.id === "canvas" || active?.id === "tri-view",
        );
        const ownIndex = floats.findIndex((entry) => entry.group === group);
        const covers = floats
          .filter((_, index) => ownIndex < 0 || index > ownIndex)
          .map((entry) => entry.rect);
        for (const panel of group.panels) occlusion.set(panel.id, covers);
        const surface = ownIndex < 0 ? group.element : floats[ownIndex].wrapper;
        mask(surface, covers);
        for (const panel of group.panels) {
          const content = host.querySelector<HTMLElement>(`[data-workbench-panel="${panel.id}"]`);
          const overlay = content?.closest<HTMLElement>(".dv-render-overlay");
          if (overlay && !surface.contains(overlay)) mask(overlay, covers);
        }
      }
    },
    getVisibleRegions(element: HTMLElement): Rect[] {
      const panel = element.closest<HTMLElement>("[data-workbench-panel]");
      const native = panel?.dataset.workbenchPanel
        ? api.getPanel(panel.dataset.workbenchPanel)
        : undefined;
      if (
        native &&
        (native.group.id === "parking" ||
          !native.api.isVisible ||
          !native.group.api.isVisible ||
          native.group.activePanel?.id !== native.id)
      )
        return [];
      const rect = intersectRect(element.getBoundingClientRect(), bounds);
      return rect
        ? subtractRects(rect, occlusion.get(panel?.dataset.workbenchPanel ?? "") ?? [])
        : [];
    },
    dispose() {
      clearMasks();
      api.groups.forEach((group) => {
        delete group.element.dataset.workbenchGpuGroup;
      });
    },
  };
}
