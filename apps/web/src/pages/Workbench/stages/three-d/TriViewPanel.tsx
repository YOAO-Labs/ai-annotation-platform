/**
 * v0.13.5 · 三正交视图面板 (主 3D 视图右栏)。B-1: 只读渲染基建; B-2: 拖边/角精修。
 *
 * 结构:纵向 3 行 (top/side/front),每行一个 2D overlay (TriOrthoView) + 标题。点云底图由
 * PointCloudScene 的唯一 renderer 直接画进主 canvas 对应区域；本组件只量出 client rect 并下发，
 * 不创建第二块 canvas/context。
 *
 * 拖拽: 任一视图开拖 → frozen 记下起始姿态, 期间相机冻结 (setCameraRef) 让框在屏上真实
 * 长/移; 拖拽中 onEditPsr(psr,false) 走 draft 实时四方同步, 松手 onEditPsr(psr,true) 落 PATCH。
 *
 * 生命周期:挂载时注册三视图布局与状态，卸载时从 PointCloudScene 注销。
 * v0.17.6 · module.css → Tailwind。
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { PointCloudScene } from "./PointCloudScene";
import type { PointCloudVisibleRegions } from "./PointCloudTriViewPass";
import { TriOrthoView, type TriSelected } from "./TriOrthoView";
import type { TriView, Psr } from "./geometry/triview";

// v0.17.6 · Tailwind class constants (was ThreeDWorkbench.module.css).
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
  /** 主点云 Scene；三视图只注册 render pass 状态，不拥有 renderer。 */
  scene: PointCloudScene | null;
  /** 当前选中框 (PSR + 色); null = 无选中。拖拽中由上层下发 draft。 */
  selected: TriSelected | null;
  /** 是否可编辑 (任务非只读 且 框未锁定); false → 只读, overlay 不画 handle、不收事件。 */
  editable: boolean;
  /** 浮窗位置/尺寸变化键；fixed 浮窗移动不会触发 ResizeObserver。 */
  layoutKey: string | number;
  getVisibleRegions?: PointCloudVisibleRegions;
  /** 折叠时注销可见 pass，展开后恢复。 */
  active?: boolean;
  /** 当前对象按视图记忆的缩放倍数。 */
  zoomByView: Record<TriView, number>;
  onZoomChange: (view: TriView, zoom: number) => void;
  /** 拖拽中 (commit=false, draft) / 松手 (commit=true, PATCH) 回写选中框 PSR。 */
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
  // 拖拽期冻结取景参考 (起始姿态); null = 无拖拽。
  const [frozen, setFrozen] = useState<Psr | null>(null);
  const activeRef = useRef(active);
  activeRef.current = active;

  // 量出 panel / 3 行的 client rect，由 PointCloudScene 相对主 canvas 统一换算 viewport。
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
      visibleRegions: activeRef.current ? getVisibleRegions?.(panel) : [],
    });
  }, [getVisibleRegions, scene]);

  // 挂载注册布局；浮窗 resize 与窗口 resize 重测，卸载恢复主 canvas 旧区域。
  useLayoutEffect(() => {
    const panel = panelRef.current;
    if (!panel || !scene) return;
    const ro = new ResizeObserver(layout);
    ro.observe(panel);
    window.addEventListener("resize", layout);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", layout);
      scene.setTriViewLayout(null);
    };
  }, [layout, scene]);

  useLayoutEffect(() => {
    if (!scene) return;
    layout();
    scene.setTriViewActive(active);
  }, [active, layout, layoutKey, scene]);

  // fixed 浮窗拖动只改位置，不触发 ResizeObserver；父层通过 layoutKey 同步重测。
  useLayoutEffect(() => {
    if (!scene) return;
    return () => scene.setTriViewActive(false);
  }, [scene]);

  useEffect(() => {
    scene?.setTriViewZoomByView(zoomByView);
  }, [scene, zoomByView]);

  // 选中框 PSR 变化 (含拖拽 draft): 只更新裁剪面/相机。DOM 布局由上方的
  // ResizeObserver / layoutKey 负责，避免业务对象引用刷新把 0.5px 测量抖动放大成渲染循环。
  useLayoutEffect(() => {
    if (!scene) return;
    scene.setTriViewBox(
      selected
        ? { center: selected.center, size: selected.size, rotation: selected.rotation }
        : null,
    );
  }, [scene, selected]);

  // 只在 Scene owner 变化或卸载时清理；selected 引用刷新但 PSR 未变时不能制造一次 null 闪断。
  useEffect(() => {
    if (!scene) return;
    return () => scene.setTriViewBox(null);
  }, [scene]);

  // 拖拽期冻结相机取景 (裁剪面仍随实时 box)。
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
            {TRI_LABEL[view]} · {Math.round(zoomByView[view] * 100)}% · 当前实测
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
