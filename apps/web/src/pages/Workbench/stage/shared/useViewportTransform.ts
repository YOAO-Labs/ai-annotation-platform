import { useCallback, useEffect, useRef, useState } from "react";
import type { MutableRefObject, Ref } from "react";
import { fitToCanvas } from "./viewport/fit";
import { clampScale, zoomAtPoint } from "./viewport/zoom";

export interface Viewport {
  scale: number;
  tx: number;
  ty: number;
}

export function useViewportTransform(initial: Viewport = { scale: 1, tx: 0, ty: 0 }) {
  const [vp, setVp] = useState<Viewport>(initial);
  const vpRef = useRef(vp);
  vpRef.current = vp;

  const reset = useCallback(() => setVp({ scale: 1, tx: 0, ty: 0 }), []);

  const zoomAt = useCallback((cx: number, cy: number, nextScale: number) => {
    setVp((cur) => zoomAtPoint(cur, cx, cy, nextScale));
  }, []);

  const pan = useCallback((dx: number, dy: number) => {
    setVp((cur) => ({ ...cur, tx: cur.tx + dx, ty: cur.ty + dy }));
  }, []);

  const fit = useCallback(
    (viewportW: number, viewportH: number, contentW: number, contentH: number) => {
      const next = fitToCanvas(viewportW, viewportH, contentW, contentH);
      if (next) setVp(next);
    },
    [],
  );

  const setScale = useCallback((s: number) => {
    setVp((cur) => ({ ...cur, scale: clampScale(s) }));
  }, []);

  return { vp, vpRef, reset, zoomAt, pan, fit, setScale, setVp };
}

function setRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (!ref) return;
  if (typeof ref === "function") {
    ref(value);
  } else {
    (ref as MutableRefObject<T | null>).current = value;
  }
}

export function useElementSize<T extends HTMLElement>(forwardedRef?: Ref<T>) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [el, setEl] = useState<T | null>(null);
  const ref = useCallback(
    (node: T | null) => {
      setEl(node);
      setRef(forwardedRef, node);
    },
    [forwardedRef],
  );
  useEffect(() => {
    if (!el) return;
    if (typeof ResizeObserver === "undefined") {
      const update = () => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.height > 0) setSize({ w: r.width, h: r.height });
      };
      update();
      window.addEventListener("resize", update);
      return () => window.removeEventListener("resize", update);
    }
    const initial = el.getBoundingClientRect();
    if (initial.width > 0 && initial.height > 0) {
      setSize({ w: initial.width, h: initial.height });
    }
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) {
        const r = e.contentRect;
        if (r.width > 0 && r.height > 0) setSize({ w: r.width, h: r.height });
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [el]);
  return { ref, size };
}
