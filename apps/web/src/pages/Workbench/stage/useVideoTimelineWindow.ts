import {
  useCallback,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Dispatch, Ref, SetStateAction } from "react";
import { clampWindow, type TimelineWindow } from "./timelineCoords";
import type { VideoTimelineWindowControls } from "./videoStageControls";

export function useVideoTimelineWindow({
  sourceKey,
  maxFrame,
  minSpan,
  controlsRef,
  onInteraction,
}: {
  sourceKey: string;
  maxFrame: number;
  minSpan: number;
  controlsRef?: Ref<VideoTimelineWindowControls>;
  onInteraction?: () => void;
}) {
  const identity = useRef({ key: sourceKey, maxFrame, epoch: 0 });
  if (identity.current.key !== sourceKey || identity.current.maxFrame !== maxFrame) {
    identity.current = { key: sourceKey, maxFrame, epoch: identity.current.epoch + 1 };
  }
  const epoch = identity.current.epoch;
  const mounted = useRef(false);
  const limits = useRef({ maxFrame, minSpan, onInteraction });
  limits.current = { maxFrame, minSpan, onInteraction };
  const fullWindow = useMemo(() => ({ from: 0, to: maxFrame }), [maxFrame]);
  const [state, setState] = useState({ epoch, window: fullWindow });
  const timelineWindow = state.epoch === epoch ? state.window : fullWindow;
  const timelineWindowRef = useRef(timelineWindow);
  timelineWindowRef.current = timelineWindow;

  const setTimelineWindow: Dispatch<SetStateAction<TimelineWindow>> = useCallback(
    (update) => {
      if (!mounted.current || identity.current.epoch !== epoch) return;
      limits.current.onInteraction?.();
      setState((previous) => {
        if (identity.current.epoch !== epoch) return previous;
        const current = previous.epoch === epoch ? previous.window : fullWindow;
        return { epoch, window: typeof update === "function" ? update(current) : update };
      });
    },
    [epoch, fullWindow],
  );

  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useLayoutEffect(() => {
    setState((previous) => (previous.epoch === epoch ? previous : { epoch, window: fullWindow }));
  }, [epoch, fullWindow]);

  useImperativeHandle(
    controlsRef,
    () => ({
      capture: () =>
        mounted.current && identity.current.epoch === epoch
          ? { ...timelineWindowRef.current }
          : null,
      restore: (saved) => {
        if (
          !mounted.current ||
          identity.current.epoch !== epoch ||
          !Number.isFinite(saved.from) ||
          !Number.isFinite(saved.to) ||
          saved.to < saved.from
        )
          return null;
        const window = clampWindow(saved, limits.current.maxFrame, limits.current.minSpan);
        setState({ epoch, window });
        return { window, clamped: saved.from !== window.from || saved.to !== window.to };
      },
    }),
    [epoch],
  );

  return { timelineWindow, timelineWindowRef, setTimelineWindow };
}
