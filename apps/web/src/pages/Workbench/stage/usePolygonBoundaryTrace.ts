import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { Annotation, Geometry } from "@/types";
import type { PolygonDraftHandle } from "./tools";
import type { Pt } from "./polygonGeom";
import {
  appendBoundaryArc,
  boundaryPaths,
  pickBoundary,
  traceRing,
  traceUnsupportedReason,
  type BoundaryDirection,
  type BoundaryHit,
  type BoundaryPaths,
} from "./shared/geometry/polygonBoundaryTrace";

interface Source {
  id: string;
  version: number;
  geometry: Geometry;
  ring: Pt[];
}
interface Trace {
  owner: string;
  expected: Pt[];
  source?: Source;
  start?: BoundaryHit;
  paths?: BoundaryPaths;
  direction: BoundaryDirection;
  error?: string;
  invalid?: boolean;
  busy?: boolean;
}
interface Options {
  enabled: boolean;
  owner: string;
  annotations: Annotation[];
  width: number;
  height: number;
  draft?: PolygonDraftHandle;
}
const unchanged = (
  source: Source,
  current: { version?: number; geometry?: Geometry; is_hidden?: boolean } | undefined | null,
) =>
  !!current &&
  !current.is_hidden &&
  current.version === source.version &&
  JSON.stringify(current.geometry) === JSON.stringify(source.geometry);

export function usePolygonBoundaryTrace(options: Options) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const liveTrace = useRef(trace);
  const live = useRef(options);
  const request = useRef<AbortController | null>(null);
  const publish = useCallback((next: Trace | null) => {
    liveTrace.current = next;
    setTrace(next);
  }, []);
  const cancel = useCallback(() => {
    request.current?.abort();
    request.current = null;
    publish(null);
  }, [publish]);

  useLayoutEffect(() => {
    live.current = options;
    const current = liveTrace.current;
    if (!current) return;
    if (
      !options.enabled ||
      options.owner !== current.owner ||
      options.draft?.boundaryTrace?.getPoints() !== current.expected
    ) {
      cancel();
      return;
    }
    if (
      current.source &&
      !current.invalid &&
      !unchanged(
        current.source,
        options.annotations.find((item) => item.id === current.source!.id),
      )
    ) {
      request.current?.abort();
      publish({
        ...current,
        paths: undefined,
        busy: false,
        invalid: true,
        error: "来源已修改、隐藏或删除，请取消后重新选择边界。",
      });
    }
  });
  useLayoutEffect(
    () => () => {
      request.current?.abort();
      liveTrace.current = null;
    },
    [],
  );

  const begin = useCallback(() => {
    const current = live.current;
    if (!current.enabled || !current.draft?.boundaryTrace) return;
    current.draft.autoPoints?.beforeKey.current?.();
    cancel();
    publish({
      owner: current.owner,
      expected: current.draft.boundaryTrace.getPoints(),
      direction: "clockwise",
    });
  }, [cancel, publish]);

  const pick = useCallback(
    (point: Pt) => {
      const current = liveTrace.current;
      const { enabled, annotations, width, height } = live.current;
      if (!current || !enabled || current.busy || current.invalid) return;
      if (!current.source) {
        let nearest: { annotation: Annotation; hit: BoundaryHit } | null = null;
        for (const annotation of annotations) {
          if (annotation.is_hidden) continue;
          const geometry = annotation.geometry;
          const polygons =
            geometry?.type === "polygon"
              ? [geometry]
              : geometry?.type === "multi_polygon"
                ? geometry.polygons
                : [];
          for (const polygon of polygons) {
            for (const ring of [polygon.points, ...(polygon.holes ?? [])]) {
              const hit = pickBoundary(traceRing(ring), point, width, height);
              if (hit && (!nearest || hit.distance < nearest.hit.distance))
                nearest = { annotation, hit };
            }
          }
        }
        if (!nearest) {
          publish({ ...current, error: "请在已保存的多边形边界 8 像素内选择起点。" });
          return;
        }
        const annotation = nearest.annotation;
        const reason = traceUnsupportedReason(annotation.geometry);
        if (
          reason ||
          !Number.isInteger(annotation.version) ||
          annotation.version! < 1 ||
          annotation.id.startsWith("tmp")
        ) {
          publish({ ...current, error: reason ?? "来源尚未保存，请保存后再追踪。" });
          return;
        }
        if (annotation.geometry?.type !== "polygon") return;
        const geometry = structuredClone(annotation.geometry);
        const source: Source = {
          id: annotation.id,
          version: annotation.version!,
          geometry,
          ring: traceRing(geometry.points),
        };
        publish({
          ...current,
          source,
          start: pickBoundary(source.ring, point, width, height)!,
          error: undefined,
        });
        return;
      }
      const end = pickBoundary(current.source.ring, point, width, height);
      if (!end) {
        publish({
          ...current,
          paths: undefined,
          error: "终点需位于同一个来源的边界上，请重新选择。",
        });
        return;
      }
      const paths = boundaryPaths(current.source.ring, current.start!, end, width, height);
      if (!paths) {
        publish({ ...current, paths: undefined, error: "起点与终点不能重合，请选择另一处边界。" });
        return;
      }
      publish({
        ...current,
        paths,
        direction:
          paths.clockwise.length <= paths.counterclockwise.length
            ? "clockwise"
            : "counterclockwise",
        error: undefined,
      });
    },
    [publish],
  );

  const choose = useCallback(
    (direction: BoundaryDirection) => {
      const current = liveTrace.current;
      if (current?.paths && !current.busy) publish({ ...current, direction });
    },
    [publish],
  );

  const confirm = useCallback(async () => {
    const current = liveTrace.current;
    const capability = live.current.draft?.boundaryTrace;
    if (
      !current?.paths ||
      !current.source ||
      current.busy ||
      current.invalid ||
      !capability ||
      !live.current.enabled
    )
      return;
    const busy = { ...current, busy: true, error: undefined };
    publish(busy);
    const controller = new AbortController();
    request.current = controller;
    try {
      const source = await capability.readSource(current.source.id, controller.signal);
      if (
        controller.signal.aborted ||
        liveTrace.current !== busy ||
        !live.current.enabled ||
        live.current.owner !== current.owner
      )
        return;
      if (!unchanged(current.source, source)) {
        publish({
          ...busy,
          paths: undefined,
          busy: false,
          invalid: true,
          error: "来源已修改、隐藏或删除，请取消后重新选择边界。",
        });
        return;
      }
      const batch = appendBoundaryArc(current.expected, current.paths[current.direction].points);
      if (!capability.append(batch, current.expected)) {
        publish({
          ...busy,
          paths: undefined,
          busy: false,
          invalid: true,
          error: "草稿或任务状态已改变，请取消后重新追踪。",
        });
        return;
      }
      cancel();
    } catch {
      if (!controller.signal.aborted && liveTrace.current === busy)
        publish({ ...busy, busy: false, error: "无法核验来源，请检查连接后重试；草稿尚未追加。" });
    } finally {
      if (request.current === controller) request.current = null;
    }
  }, [cancel, publish]);

  useLayoutEffect(() => {
    const bridge = options.draft?.beforeInput;
    if (!bridge) return;
    const onKey = (event: KeyboardEvent) => {
      if (!liveTrace.current) return false;
      if (event.key === "Escape") {
        cancel();
        return true;
      }
      if (event.key === "Enter") {
        void confirm();
        return true;
      }
      if (event.key === "Backspace") cancel();
      return false;
    };
    bridge.current = onKey;
    return () => {
      if (bridge.current === onKey) bridge.current = null;
    };
  }, [options.draft?.beforeInput, cancel, confirm]);

  return { trace, begin, cancel, pick, choose, confirm };
}
