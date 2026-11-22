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

export interface TriSelected {
  center: [number, number, number];
  size: [number, number, number];
  rotation: [number, number, number];
  color: string;
}

const TRI_OVERLAY = "absolute inset-0 z-local-1";
const TRI_OVERLAY_INTERACTIVE = "pointer-events-auto focus-visible:ring-2 focus-visible:ring-ring";
const TRI_OVERLAY_INACTIVE = "pointer-events-none";
const TRI_ANGLE_HUD =
  "absolute left-1/2 top-1/2 z-local-3 min-w-[104px] flex items-baseline justify-center gap-[7px] px-2.5 py-1.5 -translate-x-1/2 -translate-y-1/2 border border-brand rounded-sm bg-black/56 shadow-[0_0_14px_var(--sc-brand)]/20 text-brand font-mono pointer-events-none before:absolute before:left-2 before:right-2 before:h-px before:top-1 before:bg-brand/45 before:content-[''] after:absolute after:left-2 after:right-2 after:h-px after:bottom-[3px] after:bg-brand/45 after:content-['']";
const TRI_ANGLE_LABEL = "text-2xs text-muted-foreground";
const TRI_ANGLE_VALUE = "text-ui font-bold text-brand";

interface TriOrthoViewProps {
  view: TriView;
  selected: TriSelected | null;
  frozen: Psr | null;
  editable: boolean;
  zoom: number;
  onZoomChange: (zoom: number) => void;
  onDragStart: (startPsr: Psr) => void;
  onDragMove: (psr: Psr) => void;
  onDragEnd: (psr: Psr) => void;
}

type Grab = Handle | "rot";

const HANDLE_TOL = 9;
const ROT_TOL = 10;
const HANDLE_SZ = 6;
const KNOB_EXT = 22;
const KNOB_R = 4;
const RESIZE_ORDER: Handle[] = ["ne", "nw", "se", "sw", "e", "w", "n", "s"];
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
    return [cssW / 2 + d.dot(refU) * s, cssH / 2 - d.dot(refV) * s];
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
    ctx.fillStyle = selected.color;
    for (const h of RESIZE_ORDER) {
      const [hx, hy] = proj.handlePts[h];
      ctx.fillRect(hx - HANDLE_SZ / 2, hy - HANDLE_SZ / 2, HANDLE_SZ, HANDLE_SZ);
    }
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
        const dV = -(e.clientY - dr.startY) / dr.s0;
        psr = dragHandle(dr.startPsr, view, dr.handle, dU, dV);
      } else {
        const ang = Math.atan2(-(e.clientY - dr.cyC), e.clientX - dr.cxC);
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
      } else if (e.code === "Digit0" && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
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
