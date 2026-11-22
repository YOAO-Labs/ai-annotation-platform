import { useCallback, useEffect, useRef } from "react";

export function useRafThrottle() {
  const rafRef = useRef<number | null>(null);
  const pendingRef = useRef<(() => void) | null>(null);

  const cancel = useCallback(() => {
    if (rafRef.current !== null) {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafRef.current);
      else window.clearTimeout(rafRef.current);
      rafRef.current = null;
    }
    pendingRef.current = null;
  }, []);

  const flush = useCallback(() => {
    const fn = pendingRef.current;
    pendingRef.current = null;
    if (rafRef.current !== null) {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafRef.current);
      else window.clearTimeout(rafRef.current);
      rafRef.current = null;
    }
    if (fn) fn();
  }, []);

  const schedule = useCallback((fn: () => void) => {
    pendingRef.current = fn;
    if (rafRef.current !== null) return;
    const raf =
      typeof requestAnimationFrame === "function"
        ? requestAnimationFrame
        : (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 16);
    rafRef.current = raf(() => {
      rafRef.current = null;
      const next = pendingRef.current;
      pendingRef.current = null;
      if (next) next();
    });
  }, []);

  useEffect(() => cancel, [cancel]);

  return { schedule, flush, cancel };
}
