import { useLayoutEffect, useRef } from "react";
import type { PolygonDraftHandle } from "./tools";
import type { Pt } from "./polygonGeom";
import {
  createPolygonSampler,
  POLYGON_AUTO_POINT_LIMIT,
  samePolygonPoint,
} from "./polygonAutoPoints";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";

interface Options {
  enabled: boolean;
  owner: string;
  view: string;
  width: number;
  height: number;
  draft?: PolygonDraftHandle;
  toImage: (x: number, y: number) => { x: number; y: number } | null;
  snap: (point: { x: number; y: number }, event: MouseEvent) => { point: { x: number; y: number } };
}

export function usePolygonAutoPoints(options: Options) {
  const latest = useRef(options);
  const gesture = useRef<{ valid: () => boolean; cancel: () => void } | null>(null);
  useLayoutEffect(() => {
    latest.current = options;
    if (gesture.current && !gesture.current.valid()) gesture.current.cancel();
  });
  useLayoutEffect(() => () => gesture.current?.cancel(), []);

  return (start: Pt, startEvent: MouseEvent) => {
    gesture.current?.cancel();
    const initial = latest.current;
    const draft = initial.draft;
    const auto = draft?.autoPoints;
    if (!initial.enabled || !auto || start.some((v) => !Number.isFinite(v) || v < 0 || v > 1))
      return;
    let expected = auto.getPoints();
    if (expected.length >= POLYGON_AUTO_POINT_LIMIT) return;
    let pending: Pt[] = [];
    let raf: number | null = null;
    let last = start;
    let lastEvent = startEvent;
    const sampler = createPolygonSampler(start, initial.width, initial.height);
    const valid = () =>
      latest.current.enabled &&
      latest.current.owner === initial.owner &&
      latest.current.view === initial.view &&
      auto.getPoints() === expected;
    const flush = () => {
      if (!valid()) {
        cancel();
        return;
      }
      if (!pending.length) return;
      const batch = pending;
      pending = [];
      if (!auto.append(batch, expected)) {
        cancel();
        return;
      }
      expected = auto.getPoints();
    };
    const emit = (point: Pt) => {
      if (expected.length + pending.length >= POLYGON_AUTO_POINT_LIMIT) return false;
      const snapped = latest.current.snap({ x: point[0], y: point[1] }, lastEvent).point;
      const target: Pt = [Math.max(0, Math.min(1, snapped.x)), Math.max(0, Math.min(1, snapped.y))];
      if (!samePolygonPoint(pending[pending.length - 1] ?? expected[expected.length - 1], target))
        pending.push(target);
      return expected.length + pending.length < POLYGON_AUTO_POINT_LIMIT;
    };
    const schedule = () => {
      if (raf !== null) return;
      raf = requestAnimationFrame(() => {
        raf = null;
        flush();
      });
    };
    const finish = () => {
      if (valid()) {
        emit(last);
        flush();
      }
      cancel();
    };
    const move = (event: MouseEvent) => {
      if (!valid()) {
        cancel();
        return;
      }
      if (!event.shiftKey || !(event.buttons & 1)) {
        finish();
        return;
      }
      const point = latest.current.toImage(event.clientX, event.clientY);
      if (!point) return;
      last = [Math.max(0, Math.min(1, point.x)), Math.max(0, Math.min(1, point.y))];
      lastEvent = event;
      sampler.sample(last, emit);
      schedule();
    };
    const up = (event: MouseEvent) => {
      if (event.button !== 0) return;
      if (valid() && event.shiftKey) {
        const point = latest.current.toImage(event.clientX, event.clientY);
        if (point) {
          last = [Math.max(0, Math.min(1, point.x)), Math.max(0, Math.min(1, point.y))];
          lastEvent = event;
          sampler.sample(last, emit);
        }
      }
      finish();
    };
    const key = (event: KeyboardEvent) => {
      if (!valid()) {
        cancel();
        return;
      }
      if (isWorkbenchInteractionBlocked(event)) {
        cancel();
        return;
      }
      if (event.key === " ") {
        finish();
        return;
      }
      if (event.key === "Escape") cancel();
    };
    const keyUp = (event: KeyboardEvent) => {
      if (event.key === "Shift") finish();
    };
    const moveEvent = typeof PointerEvent === "undefined" ? "mousemove" : "pointermove";
    const upEvent = typeof PointerEvent === "undefined" ? "mouseup" : "pointerup";
    function cancel() {
      if (raf !== null) cancelAnimationFrame(raf);
      pending = [];
      window.removeEventListener(moveEvent, move, true);
      window.removeEventListener(upEvent, up, true);
      window.removeEventListener("keydown", key, true);
      window.removeEventListener("keyup", keyUp, true);
      window.removeEventListener("blur", cancel);
      if (auto?.beforeKey.current === finish) auto.beforeKey.current = null;
      gesture.current = null;
    }
    gesture.current = { valid, cancel };
    auto.beforeKey.current = finish;
    emit(start);
    flush();
    if (!gesture.current) return;
    window.addEventListener(moveEvent, move, true);
    window.addEventListener(upEvent, up, true);
    window.addEventListener("keydown", key, true);
    window.addEventListener("keyup", keyUp, true);
    window.addEventListener("blur", cancel);
  };
}
