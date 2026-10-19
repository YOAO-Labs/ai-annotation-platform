import { randomId } from "@/utils/id";
import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { Annotation, PolygonGeometry } from "@/types";
import type { PolygonSliceCommitRequest } from "@/api/annotationSlices";
import type { Pt } from "./polygonGeom";
import {
  POLYGON_SLICE_POINT_LIMIT,
  polygonSliceUnavailableReason,
  slicePolygon,
} from "./shared/geometry/polygonSlice";

export type CommitPolygonSlice = (payload: PolygonSliceCommitRequest) => Promise<void>;
interface Session {
  owner: string;
  source: Annotation & { geometry: PolygonGeometry };
  points: Pt[];
  preview?: [PolygonGeometry, PolygonGeometry];
  request?: PolygonSliceCommitRequest;
  busy?: boolean;
  invalid?: boolean;
  error?: string;
}
interface Options {
  owner: string;
  enabled: boolean;
  annotations: Annotation[];
  commit?: CommitPolygonSlice;
}

/** Only the cut and preview live in Stage; the task owner records persistence and history. */
export function usePolygonSlice(options: Options) {
  const [session, setSession] = useState<Session | null>(null);
  const current = useRef(session),
    live = useRef(options);
  const publish = useCallback((next: Session | null) => {
    current.current = next;
    setSession(next);
  }, []);
  const cancel = useCallback(() => {
    if (!current.current?.busy) publish(null);
  }, [publish]);
  useLayoutEffect(() => {
    live.current = options;
    const draft = current.current;
    if (!draft) return;
    if (draft.owner !== options.owner || !options.enabled) {
      publish(null);
      return;
    }
    // Once sent, retain the immutable payload for a possibly committed request's
    // idempotent retry. A source refetch must not replace its original version.
    if (draft.request || draft.invalid) return;
    const source = options.annotations.find((item) => item.id === draft.source.id);
    if (
      !source ||
      polygonSliceUnavailableReason(source, options.annotations) ||
      source.version !== draft.source.version ||
      source.is_hidden ||
      JSON.stringify(source.geometry) !== JSON.stringify(draft.source.geometry)
    ) {
      publish({
        ...draft,
        invalid: true,
        preview: undefined,
        error: "来源已修改、隐藏或删除，请取消后重新开始切割。",
      });
    }
  });
  useLayoutEffect(
    () => () => {
      current.current = null;
    },
    [],
  );

  const begin = useCallback(
    (annotation: Annotation) => {
      if (!live.current.enabled || !live.current.commit || current.current?.busy) return;
      const error = polygonSliceUnavailableReason(annotation, live.current.annotations);
      if (error || annotation.geometry?.type !== "polygon") return;
      publish({
        owner: live.current.owner,
        source: { ...annotation, geometry: structuredClone(annotation.geometry) },
        points: [],
      });
    },
    [publish],
  );
  const addPoint = useCallback(
    (point: Pt) => {
      const draft = current.current;
      if (!draft || draft.preview || draft.request || draft.invalid || draft.busy) return;
      if (point.some((value) => !Number.isFinite(value) || value < 0 || value > 1)) return;
      if (draft.points.length >= POLYGON_SLICE_POINT_LIMIT) {
        publish({ ...draft, error: "切线最多 256 个点；请预览或撤回一个点。" });
        return;
      }
      publish({ ...draft, points: [...draft.points, point], error: undefined });
    },
    [publish],
  );
  const preview = useCallback(() => {
    const draft = current.current;
    if (!draft || draft.request || draft.invalid || draft.busy) return;
    try {
      publish({
        ...draft,
        preview: slicePolygon(draft.source.geometry, draft.points),
        error: undefined,
      });
    } catch (error) {
      publish({ ...draft, error: error instanceof Error ? error.message : "无法切割当前对象" });
    }
  }, [publish]);
  const back = useCallback(() => {
    const draft = current.current;
    if (!draft || draft.request || draft.invalid || draft.busy) return;
    publish({
      ...draft,
      preview: undefined,
      points: draft.preview ? draft.points : draft.points.slice(0, -1),
      error: undefined,
    });
  }, [publish]);
  const confirm = useCallback(async () => {
    const draft = current.current,
      commit = live.current.commit;
    if (!draft?.preview || draft.invalid || draft.busy || !commit || !live.current.enabled) return;
    const request = draft.request ?? {
      annotation_id: draft.source.id,
      expected_version: draft.source.version!,
      idempotency_key: randomId().replace(/-/g, ""),
      cut_path: structuredClone(draft.points),
    };
    const sending = { ...draft, request, busy: true, error: undefined };
    publish(sending);
    try {
      await commit(request);
      if (current.current === sending) publish(null);
    } catch (error) {
      if (current.current === sending)
        publish({
          ...sending,
          busy: false,
          error: error instanceof Error ? error.message : "提交失败，预览已保留",
        });
    }
  }, [publish]);
  const keyDown = useCallback(
    (event: KeyboardEvent) => {
      const draft = current.current;
      if (!draft || event.isComposing || event.repeat) return;
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable || target.closest("input,textarea,select"))
      )
        return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancel();
      } else if (event.key === "Backspace") {
        event.preventDefault();
        event.stopPropagation();
        back();
      } else if (
        event.key === "Enter" &&
        !(target instanceof HTMLElement && target.closest("button"))
      ) {
        event.preventDefault();
        event.stopPropagation();
        if (draft.preview) void confirm();
        else preview();
      }
    },
    [back, cancel, confirm, preview],
  );
  return { session, begin, addPoint, preview, back, confirm, cancel, keyDown };
}
