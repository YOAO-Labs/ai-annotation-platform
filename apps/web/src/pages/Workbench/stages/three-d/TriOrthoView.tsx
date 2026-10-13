/**
 * v0.13.5 · 单个正交视图的 2D overlay。
 *
 * B-1 只读画框; B-2 拖边/角改 size/center; B-3 方向线柄绕 box-local 轴旋转 (yaw/pitch/roll)。
 * 数学全走 triview.ts 纯函数 (dragHandle / dragRotation), overlay 不写裸数学。
 *
 * 投影: 把框 4 角 (由实时 box 的 u/v 世界轴 + size 张成) 投进**取景参考系** (拖拽期 = 冻结
 * 起始姿态, 否则 = 框自身)。拖边/角时旋转不变 → 屏上轴对齐矩形; 拖方向线时框绕法线轴转、
 * 相机冻结 → 矩形在屏上真实倾斜、方向线扫过、点不动, 便于对齐朝向。
 *
 * 方向线沿 box-local +u 轴 (= 朝向: Top 朝 X、Side 朝 X、Front 朝 Y), 末端一个旋转柄;
 * 拖它按指针绕框心的角度增量 Δθ 旋转。每视图绕其法线轴, 屏幕 CCW→+Δθ 的手性按视图定 (ROT_SIGN)。
 * v0.17.6 · module.css → Tailwind。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import * as THREE from "three";

import { boxAxisWorldDir } from "./geometry/box3d";
import {
  VIEW_AXES,
  frameOrtho,
  FRAME_MARGIN,
  TRI_ZOOM_DEFAULT,
  stepTriZoom,
  dragHandle,
  dragRotation,
  type TriView,
  type Handle,
  type Psr,
} from "./geometry/triview";

/** overlay 需要的选中框信息 (size 决定矩形, color 描边)。 */
export interface TriSelected {
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  color: string;
}

// v0.17.6 · Tailwind class constants (was ThreeDWorkbench.module.css).
// pointer-events 必须互斥下发；只读框也要接收 wheel / keyboard 视图缩放。
const TRI_OVERLAY = "absolute inset-0 z-local-1";
const TRI_OVERLAY_INTERACTIVE = "pointer-events-auto focus-visible:ring-2 focus-visible:ring-ring";
const TRI_OVERLAY_INACTIVE = "pointer-events-none";
const TRI_ANGLE_HUD =
  "absolute left-1/2 top-1/2 z-local-3 min-w-[104px] flex items-baseline justify-center gap-[7px] px-2.5 py-1.5 -translate-x-1/2 -translate-y-1/2 border border-brand rounded-sm bg-black/56 shadow-[0_0_14px_var(--sc-brand)]/20 text-brand font-mono pointer-events-none before:absolute before:left-2 before:right-2 before:h-px before:top-1 before:bg-brand/45 before:content-[''] after:absolute after:left-2 after:right-2 after:h-px after:bottom-[3px] after:bg-brand/45 after:content-['']";
const TRI_ANGLE_LABEL = "text-2xs text-muted-foreground";
const TRI_ANGLE_VALUE = "text-ui font-bold text-brand";

interface TriOrthoViewProps {
  view: TriView;
  /** 实时 (draft) 选中框; null = 无选中。 */
  selected: TriSelected | null;
  /** 拖拽期冻结取景参考 (= 起始姿态, 只用 center/size/rotation); null = 无拖拽。 */
  frozen: Psr | null;
  /** 是否可编辑；false 时不画几何 handle，但仍允许只读缩放视图。 */
  editable: boolean;
  zoom: number;
  onZoomChange: (zoom: number) => void;
  onDragStart: (startPsr: Psr) => void;
  onDragMove: (psr: Psr) => void;
  onDragEnd: (psr: Psr) => void;
}

type Grab = Handle | "rot";

const HANDLE_TOL = 9; // 边/角命中半径 (px)
const ROT_TOL = 10; // 旋转柄命中半径 (px)
const HANDLE_SZ = 6; // handle 方块边长 (px)
const KNOB_EXT = 22; // 旋转柄相对 +u 边外延 (px; 与 resize 边柄拉开距离, 好区分/好点)
const KNOB_R = 4; // 旋转柄圆半径 (px)
// 命中优先级: 旋转柄 > 角 > 边 (角压在两边端点上, 旋转柄在 +u 边外侧)。
const RESIZE_ORDER: Handle[] = ["ne", "nw", "se", "sw", "e", "w", "n", "s"];
// 每视图绕法线轴旋转的屏幕手性 (屏幕 CCW → +Δθ 的符号; 由 (u×v)·normal 定, 与旋转无关):
// top: X×Y=+Z, front: Y×Z=+X → +1; side: X×Z=−Y → −1。
const ROT_SIGN: Record<TriView, 1 | -1> = { top: 1, side: -1, front: 1 };
const CURSOR: Record<Handle, string> = {
  e: "ew-resize",
  w: "ew-resize",
  n: "ns-resize",
  s: "ns-resize",
  ne: "nesw-resize",
  sw: "nesw-resize",
  nw: "nwse-resize",
  se: "nwse-resize",
};
const ANGLE_LABEL: Record<TriView, string> = {
  top: "Yaw",
  side: "Pitch",
  front: "Roll",
};
const DEG = Math.PI / 180;
const WHEEL_ZOOM_THRESHOLD = 80;

function shortestAngleDelta(current: number, start: number) {
  let delta = current - start;
  while (delta > Math.PI) delta -= Math.PI * 2;
  while (delta < -Math.PI) delta += Math.PI * 2;
  return delta;
}

function formatSignedDeg(rad: number, whole = false) {
  const deg = (rad * 180) / Math.PI;
  const abs = Math.abs(deg);
  const text = whole ? String(Math.round(abs)) : abs >= 10 ? abs.toFixed(1) : abs.toFixed(2);
  return `${deg >= 0 ? "+" : "-"}${text}°`;
}

/**
 * 把实时框投到取景参考系的屏幕坐标: 4 角 / 框心 / 8 个 resize 柄 / 方向线末端旋转柄。
 * 角由实时 box 的 u/v 世界轴张成 → 旋转时倾斜; 参考系 (ref) 决定相机基与米→px 比例。
 */
function projectBox(
  view: TriView,
  selected: TriSelected,
  ref: Psr,
  cssW: number,
  cssH: number,
  zoom: number,
) {
  const { u, v } = VIEW_AXES[view];
  const { halfW } = frameOrtho(ref.size, view, cssW / cssH, FRAME_MARGIN, zoom);
  const s = cssW / 2 / halfW;
  const refU = boxAxisWorldDir(ref.rotation, u);
  const refV = boxAxisWorldDir(ref.rotation, v);
  const refC = new THREE.Vector3(ref.center[0], ref.center[1], ref.center[2]);
  const boxU = boxAxisWorldDir(selected.rotation, u);
  const boxV = boxAxisWorldDir(selected.rotation, v);
  const boxC = new THREE.Vector3(selected.center[0], selected.center[1], selected.center[2]);
  const hu = selected.size[u] / 2;
  const hv = selected.size[v] / 2;

  const toScreen = (w: THREE.Vector3): [number, number] => {
    const d = w.clone().sub(refC);
    return [cssW / 2 + d.dot(refU) * s, cssH / 2 - d.dot(refV) * s]; // 屏幕 y 朝下 → v 取负
  };
  const at = (su: number, sv: number) =>
    toScreen(
      boxC
        .clone()
        .addScaledVector(boxU, su * hu)
        .addScaledVector(boxV, sv * hv),
    );
  const ne = at(1, 1);
  const nw = at(-1, 1);
  const sw = at(-1, -1);
  const se = at(1, -1);
  const mid = (a: [number, number], b: [number, number]): [number, number] => [
    (a[0] + b[0]) / 2,
    (a[1] + b[1]) / 2,
  ];
  const center = toScreen(boxC);
  const handlePts: Record<Handle, [number, number]> = {
    ne,
    nw,
    sw,
    se,
    e: mid(ne, se),
    w: mid(nw, sw),
    n: mid(ne, nw),
    s: mid(se, sw),
  };
  // 旋转柄: 从 +u 边中点再沿该方向外延 KNOB_EXT 像素。
  const eMid = handlePts.e;
  const dx = eMid[0] - center[0];
  const dy = eMid[1] - center[1];
  const len = Math.hypot(dx, dy) || 1;
  const rotKnob: [number, number] = [
    eMid[0] + (dx / len) * KNOB_EXT,
    eMid[1] + (dy / len) * KNOB_EXT,
  ];
  return { s, center, corners: [ne, nw, sw, se] as [number, number][], handlePts, rotKnob };
}

function hitTest(px: number, py: number, proj: ReturnType<typeof projectBox>): Grab | null {
  const [kx, ky] = proj.rotKnob;
  if (Math.abs(px - kx) <= ROT_TOL && Math.abs(py - ky) <= ROT_TOL) return "rot";
  for (const h of RESIZE_ORDER) {
    const [hx, hy] = proj.handlePts[h];
    if (Math.abs(px - hx) <= HANDLE_TOL && Math.abs(py - hy) <= HANDLE_TOL) return h;
  }
  return null;
}

export function TriOrthoView({
  view,
  selected,
  frozen,
  editable,
  zoom,
  onZoomChange,
  onDragStart,
  onDragMove,
  onDragEnd,
}: TriOrthoViewProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [angleHud, setAngleHud] = useState<{ label: string; value: string } | null>(null);
  // 拖拽态: resize (边/角) 用起始 PSR + 累计位移; rot 用绕框心(屏幕中心)的角度增量。用 ref 避重渲。
  const dragRef = useRef<
    | {
        kind: "resize";
        handle: Handle;
        startPsr: Psr;
        s0: number;
        startX: number;
        startY: number;
        last: Psr;
      }
    | {
        kind: "rot";
        startPsr: Psr;
        cxC: number;
        cyC: number;
        startAng: number;
        sign: 1 | -1;
        last: Psr;
      }
    | null
  >(null);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const parent = canvas?.parentElement;
    if (!canvas || !parent) return;
    const cssW = parent.clientWidth;
    const cssH = parent.clientHeight;
    if (!cssW || !cssH) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width = `${cssW}px`;
    canvas.style.height = `${cssH}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!selected) return;

    const ref = frozen ?? selected;
    const proj = projectBox(view, selected, ref, cssW, cssH, zoom);
    const [a, b, c, d] = proj.corners;
    ctx.strokeStyle = selected.color;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.lineTo(c[0], c[1]);
    ctx.lineTo(d[0], d[1]);
    ctx.closePath();
    ctx.stroke();

    if (!editable) return;
    // resize 柄 (方块)。
    ctx.fillStyle = selected.color;
    for (const h of RESIZE_ORDER) {
      const [hx, hy] = proj.handlePts[h];
      ctx.fillRect(hx - HANDLE_SZ / 2, hy - HANDLE_SZ / 2, HANDLE_SZ, HANDLE_SZ);
    }
    // 方向线 + 旋转柄 (圆): 框心 → +u 边 → 外延柄。
    ctx.beginPath();
    ctx.moveTo(proj.center[0], proj.center[1]);
    ctx.lineTo(proj.rotKnob[0], proj.rotKnob[1]);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(proj.rotKnob[0], proj.rotKnob[1], KNOB_R, 0, Math.PI * 2);
    ctx.fill();
  }, [view, selected, frozen, editable, zoom]);

  useEffect(() => {
    draw();
  }, [draw]);

  useEffect(() => {
    const parent = canvasRef.current?.parentElement;
    if (!parent) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(parent);
    return () => ro.disconnect();
  }, [draw]);

  // 最新 props 放 ref, 让下面的交互 effect 用空依赖跑一次: 拖拽中 onDragMove 改 selected (draft)
  // 不会重跑 effect、不会把 window mousemove/mouseup 监听器拆掉 (否则 mouseup 收不到 → 不提交)。
  const propsRef = useRef({
    view,
    selected,
    editable,
    zoom,
    onZoomChange,
    onDragStart,
    onDragMove,
    onDragEnd,
  });
  propsRef.current = {
    view,
    selected,
    editable,
    zoom,
    onZoomChange,
    onDragStart,
    onDragMove,
    onDragEnd,
  };
  const wheelDeltaRef = useRef({ value: 0, sign: 0 });

  // 拖拽: window 级 move/up。resize 用累计位移喂 dragHandle(起始 PSR); rot 用角度增量喂 dragRotation。
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const onMove = (e: MouseEvent) => {
      const dr = dragRef.current;
      if (!dr) return;
      const { view, onDragMove } = propsRef.current;
      let psr: Psr;
      if (dr.kind === "resize") {
        const dU = (e.clientX - dr.startX) / dr.s0;
        const dV = -(e.clientY - dr.startY) / dr.s0; // 屏幕 y 朝下 → v 取负
        psr = dragHandle(dr.startPsr, view, dr.handle, dU, dV);
      } else {
        const ang = Math.atan2(-(e.clientY - dr.cyC), e.clientX - dr.cxC); // 数学系 (v 朝上)
        let delta = dr.sign * shortestAngleDelta(ang, dr.startAng);
        if (e.shiftKey) delta = Math.round(delta / DEG) * DEG;
        psr = dragRotation(dr.startPsr, view, delta);
        setAngleHud({ label: ANGLE_LABEL[view], value: formatSignedDeg(delta, e.shiftKey) });
      }
      dr.last = psr;
      onDragMove(psr);
    };
    const onUp = () => {
      const dr = dragRef.current;
      if (!dr) return;
      dragRef.current = null;
      setAngleHud(null);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      propsRef.current.onDragEnd(dr.last);
    };

    const onDown = (e: MouseEvent) => {
      const { view, selected, editable, zoom, onDragStart } = propsRef.current;
      if (!selected) return;
      canvas.focus({ preventScroll: true });
      if (!editable) return;
      const rect = canvas.getBoundingClientRect();
      const proj = projectBox(view, selected, selected, rect.width, rect.height, zoom);
      const grab = hitTest(e.clientX - rect.left, e.clientY - rect.top, proj);
      if (!grab) return;
      e.preventDefault();
      const startPsr: Psr = {
        center: selected.center,
        size: selected.size,
        rotation: selected.rotation,
      };
      if (grab === "rot") {
        // 框心在屏幕中心 (旋转不移心), 取其 client 坐标当枢轴。
        const cxC = rect.left + proj.center[0];
        const cyC = rect.top + proj.center[1];
        const startAng = Math.atan2(-(e.clientY - cyC), e.clientX - cxC);
        dragRef.current = {
          kind: "rot",
          startPsr,
          cxC,
          cyC,
          startAng,
          sign: ROT_SIGN[view],
          last: startPsr,
        };
        setAngleHud({ label: ANGLE_LABEL[view], value: "+0.00°" });
      } else {
        dragRef.current = {
          kind: "resize",
          handle: grab,
          startPsr,
          s0: proj.s,
          startX: e.clientX,
          startY: e.clientY,
          last: startPsr,
        };
        setAngleHud(null);
      }
      onDragStart(startPsr);
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    };

    // 悬停光标反馈 (非拖拽时)。
    const onHover = (e: MouseEvent) => {
      const { view, selected, editable, zoom } = propsRef.current;
      if (dragRef.current || !editable || !selected) return;
      const rect = canvas.getBoundingClientRect();
      const proj = projectBox(view, selected, selected, rect.width, rect.height, zoom);
      const grab = hitTest(e.clientX - rect.left, e.clientY - rect.top, proj);
      canvas.style.cursor = grab === "rot" ? "grab" : grab ? CURSOR[grab] : "default";
    };

    const onWheel = (e: WheelEvent) => {
      const { selected, zoom, onZoomChange } = propsRef.current;
      if (!selected || dragRef.current) return;
      e.preventDefault();
      canvas.focus({ preventScroll: true });
      const linePx = 16;
      const pagePx = canvas.parentElement?.clientHeight ?? 240;
      const delta = e.deltaY * (e.deltaMode === 1 ? linePx : e.deltaMode === 2 ? pagePx : 1);
      if (!Number.isFinite(delta) || delta === 0) return;
      const sign = Math.sign(delta);
      if (wheelDeltaRef.current.sign !== sign) {
        wheelDeltaRef.current = { value: 0, sign };
      }
      wheelDeltaRef.current.value += delta;
      if (Math.abs(wheelDeltaRef.current.value) < WHEEL_ZOOM_THRESHOLD) return;
      wheelDeltaRef.current.value -= sign * WHEEL_ZOOM_THRESHOLD;
      onZoomChange(stepTriZoom(zoom, sign < 0 ? 1 : -1));
    };

    const onKeyDown = (e: KeyboardEvent) => {
      const { selected, zoom, onZoomChange } = propsRef.current;
      if (!selected || dragRef.current) return;
      const direction = e.key === "+" || e.key === "=" ? 1 : e.key === "-" ? -1 : null;
      if (direction !== null && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        e.stopImmediatePropagation();
        onZoomChange(stepTriZoom(zoom, direction));
      } else if (
        // v0.24 · 0 让位给第十个类别键；聚焦视图缩放重置改为 Shift+物理 Digit0。
        // 事件在本视图消费，不再下落到类别选择或全局视图重置。
        e.code === "Digit0" &&
        e.shiftKey &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey
      ) {
        e.preventDefault();
        e.stopImmediatePropagation();
        onZoomChange(TRI_ZOOM_DEFAULT);
      }
    };

    canvas.addEventListener("mousedown", onDown);
    canvas.addEventListener("mousemove", onHover);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("keydown", onKeyDown);
    return () => {
      canvas.removeEventListener("mousedown", onDown);
      canvas.removeEventListener("mousemove", onHover);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  return (
    <>
      <canvas
        ref={canvasRef}
        tabIndex={selected ? 0 : -1}
        aria-label={`${view === "top" ? "俯视" : view === "side" ? "侧视" : "正视"}精修视图，缩放 ${Math.round(zoom * 100)}%`}
        className={`${TRI_OVERLAY} ${selected ? TRI_OVERLAY_INTERACTIVE : TRI_OVERLAY_INACTIVE}`}
      />
      {angleHud && (
        <div className={TRI_ANGLE_HUD} aria-hidden="true">
          <span className={TRI_ANGLE_LABEL}>Δ{angleHud.label}</span>
          <span className={TRI_ANGLE_VALUE}>{angleHud.value}</span>
        </div>
      )}
    </>
  );
}

export default TriOrthoView;
