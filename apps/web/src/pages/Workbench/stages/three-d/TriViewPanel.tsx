import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { PointCloudScene } from "./PointCloudScene";
import type { PointCloudVisibleRegions } from "./PointCloudTriViewPass";
import { TriOrthoView, type TriSelected } from "./TriOrthoView";
import type { TriView, Psr } from "./geometry/triview";

const TRI_PANEL = "relative flex-1 flex min-h-0 flex-col gap-0 bg-transparent p-0";
const TRI_ROW = "relative flex-1 min-h-0 border border-border overflow-hidden";
const TRI_CAPTION =
  "absolute top-1 left-1/2 z-local-2 -translate-x-1/2 px-1.5 py-px rounded-sm bg-card border border-border text-xs text-muted-foreground tabular-nums whitespace-nowrap pointer-events-none";
const TRI_AXIS_GLYPH =
  "absolute left-1.5 bottom-1 z-local-2 w-[42px] h-[42px] pointer-events-none [filter:drop-shadow(0_0_5px_var(--sc-muted))]";
const TRI_AXIS_PATH =
  "[fill:none] [stroke:currentColor] [stroke-width:2] [stroke-linecap:round] [stroke-linejoin:round] [vector-effect:non-scaling-stroke]";
const TRI_AXIS_TEXT = "[fill:currentColor] font-mono text-xs font-bold";
const TRI_AXIS_ORIGIN = "[fill:var(--sc-foreground)] [stroke:var(--sc-card)] [stroke-width:1]";
const AXIS_X = "text-status-danger";
const AXIS_Y = "text-status-positive";
const AXIS_Z = "text-brand";
const TRI_EMPTY =
  "absolute inset-0 z-local-3 flex items-center justify-center text-center p-3 bg-card text-xs text-muted-foreground leading-relaxed";

const VIEWS: TriView[] = ["top", "side", "front"];
const TRI_LABEL: Record<TriView, string> = {
  top: "俯视 Top",
  side: "侧视 Side",
  front: "正视 Front",
};

const TRI_AXES: Record<TriView, { u: "x" | "y" | "z"; v: "x" | "y" | "z" }> = {
  top: { u: "x", v: "y" },
  side: { u: "x", v: "z" },
  front: { u: "y", v: "z" },
};

const AXIS_LABEL: Record<"x" | "y" | "z", string> = {
  x: "X",
  y: "Y",
  z: "Z",
};

interface TriViewPanelProps {
  scene: PointCloudScene | null;
  selected: TriSelected | null;
  editable: boolean;
  layoutKey: string | number;
  getVisibleRegions?: PointCloudVisibleRegions;
  active?: boolean;
  zoomByView: Record<TriView, number>;
  onZoomChange: (view: TriView, zoom: number) => void;
  onEditPsr: (psr: Psr, commit: boolean) => void;
}

function axisClass(axis: "x" | "y" | "z") {
  if (axis === "x") return AXIS_X;
  if (axis === "y") return AXIS_Y;
  return AXIS_Z;
}

function TriAxisGlyph({ view }: { view: TriView }) {
  const axes = TRI_AXES[view];
  return (
    <svg className={TRI_AXIS_GLYPH} viewBox="0 0 52 52" aria-hidden="true">
      <g className={axisClass(axes.u)}>
        <path className={TRI_AXIS_PATH} d="M10 42H38" />
        <path className={TRI_AXIS_PATH} d="M38 42L32 37M38 42L32 47" />
        <text className={TRI_AXIS_TEXT} x="42" y="46">
          {AXIS_LABEL[axes.u]}
        </text>
      </g>
      <g className={axisClass(axes.v)}>
        <path className={TRI_AXIS_PATH} d="M10 42V12" />
        <path className={TRI_AXIS_PATH} d="M10 12L5 18M10 12L15 18" />
        <text className={TRI_AXIS_TEXT} x="4" y="10">
          {AXIS_LABEL[axes.v]}
        </text>
      </g>
      <circle className={TRI_AXIS_ORIGIN} cx="10" cy="42" r="2.8" />
    </svg>
  );
}

export function TriViewPanel({
  scene,
  selected,
  editable,
  layoutKey,
  getVisibleRegions,
  active = true,
  zoomByView,
  onZoomChange,
  onEditPsr,
}: TriViewPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const rowRefs = useRef<Record<TriView, HTMLDivElement | null>>({
    top: null,
    side: null,
    front: null,
  });
  const [frozen, setFrozen] = useState<Psr | null>(null);

  const layout = useCallback(() => {
    const panel = panelRef.current;
    if (!panel || !scene) return;
    const pr = panel.getBoundingClientRect();
    const views = [];
    for (const view of VIEWS) {
      const el = rowRefs.current[view];
      if (!el) continue;
      const b = el.getBoundingClientRect();
      views.push({
        view,
        left: b.left + el.clientLeft,
        top: b.top + el.clientTop,
        width: el.clientWidth,
        height: el.clientHeight,
      });
    }
    scene.setTriViewLayout({
      panel: { left: pr.left, top: pr.top, width: pr.width, height: pr.height },
      views,
      visibleRegions: active ? getVisibleRegions?.(panel) : [],
    });
  }, [active, getVisibleRegions, scene]);

  useEffect(() => {
    const panel = panelRef.current;
    if (!panel || !scene) return;
    const ro = new ResizeObserver(layout);
    ro.observe(panel);
    window.addEventListener("resize", layout);
    layout();
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", layout);
      scene.setTriViewLayout(null);
    };
  }, [layout, scene]);

  useLayoutEffect(() => layout(), [layout, layoutKey]);

  useEffect(() => {
    if (!scene) return;
    scene.setTriViewActive(active);
    return () => scene.setTriViewActive(false);
  }, [active, scene]);

  useEffect(() => {
    scene?.setTriViewZoomByView(zoomByView);
  }, [scene, zoomByView]);

  useEffect(() => {
    if (!scene) return;
    scene.setTriViewBox(
      selected
        ? { center: selected.center, size: selected.size, rotation: selected.rotation }
        : null,
    );
  }, [scene, selected]);

  useEffect(() => {
    if (!scene) return;
    return () => scene.setTriViewBox(null);
  }, [scene]);

  useEffect(() => {
    if (!scene) return;
    scene.setTriViewCameraRef(frozen);
    return () => scene.setTriViewCameraRef(null);
  }, [frozen, scene]);

  const handleDragStart = useCallback((startPsr: Psr) => setFrozen(startPsr), []);
  const handleDragMove = useCallback((psr: Psr) => onEditPsr(psr, false), [onEditPsr]);
  const handleDragEnd = useCallback(
    (psr: Psr) => {
      onEditPsr(psr, true);
      setFrozen(null);
    },
    [onEditPsr],
  );

  return (
    <div ref={panelRef} className={TRI_PANEL} data-testid="tri-view-renderer-panel">
      {VIEWS.map((view) => (
        <div
          key={view}
          ref={(el) => {
            rowRefs.current[view] = el;
          }}
          className={TRI_ROW}
        >
          <TriOrthoView
            view={view}
            selected={selected}
            frozen={frozen}
            editable={editable}
            zoom={zoomByView[view]}
            onZoomChange={(zoom) => onZoomChange(view, zoom)}
            onDragStart={handleDragStart}
            onDragMove={handleDragMove}
            onDragEnd={handleDragEnd}
          />
          <TriAxisGlyph view={view} />
          <figcaption className={TRI_CAPTION}>
            {TRI_LABEL[view]} · {Math.round(zoomByView[view] * 100)}%
          </figcaption>
        </div>
      ))}
      {!selected && (
        <div className={TRI_EMPTY}>
          选中一个 3D 框后
          <br />
          在此俯 / 侧 / 正三视图精修
        </div>
      )}
    </div>
  );
}

export default TriViewPanel;
