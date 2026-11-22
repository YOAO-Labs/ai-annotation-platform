import { useCallback, useEffect, useRef, useState } from "react";

export type ScenePlaybackRate = 1 | 2 | 4;

export interface ScenePlaybackFrameState {
  taskId: string | null;
  status: "loading" | "ready" | "error";
  error?: string | null;
}

export interface ScenePlaybackTarget {
  taskId: string;
  annotationId: string | null;
  frameIndex: number;
}

interface ScenePlaybackOptions {
  active: boolean;
  onActiveChange: (active: boolean) => void;
  taskId: string | null;
  sceneId: string | null | undefined;
  frameState: ScenePlaybackFrameState;
  rate: ScenePlaybackRate;
  visible?: boolean;
  blocker?: string | null;
  atEnd: boolean;
  resolveNext: (request: {
    taskId: string;
    restart: boolean;
    signal: AbortSignal;
  }) => Promise<ScenePlaybackTarget | null>;
  navigate: (target: ScenePlaybackTarget) => Promise<boolean>;
}

interface PlaybackSession {
  sceneId: string;
  taskId: string;
  previousTaskId: string | null;
  phase: "loading" | "dwell" | "resolving" | "navigating";
  readyAt: number | null;
  deadline: number;
  restart: boolean;
  controller: AbortController;
}

const FRAME_WAIT_TIMEOUT_MS = 15_000;

export function useScenePlayback(options: ScenePlaybackOptions) {
  const { active, taskId, sceneId, frameState, rate, visible = true, blocker, atEnd } = options;
  const latest = useRef(options);
  latest.current = options;
  const session = useRef<PlaybackSession | null>(null);
  const activationConsumed = useRef(false);
  const navigationInFlight = useRef(false);
  const exhaustedTaskId = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const [revision, setRevision] = useState(0);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wake = useCallback(() => setRevision((value) => value + 1), []);
  const stop = useCallback((message: string | null = null) => {
    session.current?.controller.abort();
    session.current = null;
    clearTimeout(timerRef.current);
    setWaiting(false);
    setError(message);
    latest.current.onActiveChange(false);
  }, []);
  const pause = useCallback(() => stop(), [stop]);

  useEffect(() => {
    const onVisibilityChange = () => {
      if (document.hidden && latest.current.active) stop();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      session.current?.controller.abort();
      session.current = null;
      clearTimeout(timerRef.current);
      activationConsumed.current = false;
    };
  }, [stop]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = (delay: number) => {
      timer = setTimeout(wake, Math.max(0, delay));
      timerRef.current = timer;
    };
    if (!active) {
      activationConsumed.current = false;
      session.current?.controller.abort();
      session.current = null;
      setWaiting(false);
      return;
    }
    if (!activationConsumed.current) {
      activationConsumed.current = true;
      if (!taskId || !sceneId || navigationInFlight.current) {
        stop(navigationInFlight.current ? "上一帧仍在切换，请稍后重试" : "请等待 Scene 加载后重试");
        return;
      }
      session.current = {
        sceneId,
        taskId,
        previousTaskId: null,
        phase: "loading",
        readyAt: null,
        deadline: Date.now() + FRAME_WAIT_TIMEOUT_MS,
        restart: atEnd || exhaustedTaskId.current === taskId,
        controller: new AbortController(),
      };
      setError(null);
    }
    const run = session.current;
    if (!run) return;
    if (!visible || document.hidden || blocker) {
      stop(blocker || null);
      return;
    }
    if (
      (sceneId !== undefined && sceneId !== run.sceneId) ||
      (taskId === null
        ? run.previousTaskId === null
        : taskId !== run.taskId && taskId !== run.previousTaskId)
    ) {
      stop();
      return;
    }
    if (taskId === run.taskId) run.previousTaskId = null;
    if (frameState.taskId === taskId && frameState.status === "error") {
      stop(frameState.error || "当前帧加载失败，请重试后播放");
      return;
    }
    const ready =
      taskId === run.taskId &&
      sceneId === run.sceneId &&
      frameState.taskId === run.taskId &&
      frameState.status === "ready";
    const now = Date.now();
    if (run.phase !== "dwell" && now >= run.deadline) {
      stop("等待帧加载超过 15 秒，播放已暂停，请重试");
      return;
    }
    if (run.phase === "dwell" && !ready) {
      run.phase = "loading";
      run.readyAt = null;
      run.deadline = now + FRAME_WAIT_TIMEOUT_MS;
    }
    if (run.phase === "loading" && ready) {
      run.phase = "dwell";
      run.readyAt = now;
    }
    setWaiting(run.phase !== "dwell");
    if (run.phase !== "dwell") {
      schedule(run.deadline - now);
    } else {
      const remaining = 1000 / rate - (now - run.readyAt!);
      if (remaining > 0) {
        schedule(remaining);
      } else {
        run.phase = "resolving";
        run.deadline = now + FRAME_WAIT_TIMEOUT_MS;
        setWaiting(true);
        schedule(FRAME_WAIT_TIMEOUT_MS);
        const isCurrent = () =>
          session.current === run && !run.controller.signal.aborted && latest.current.active;
        void (async () => {
          try {
            const target = await latest.current.resolveNext({
              taskId: run.taskId,
              restart: run.restart,
              signal: run.controller.signal,
            });
            if (!isCurrent()) return;
            run.restart = false;
            if (!target || target.taskId === run.taskId) {
              exhaustedTaskId.current = run.taskId;
              stop();
              return;
            }
            exhaustedTaskId.current = null;
            run.previousTaskId = run.taskId;
            run.taskId = target.taskId;
            run.phase = "navigating";
            run.readyAt = null;
            navigationInFlight.current = true;
            let accepted: boolean;
            try {
              accepted = await latest.current.navigate(target);
            } finally {
              navigationInFlight.current = false;
            }
            if (!isCurrent()) return;
            if (!accepted) {
              stop("切帧未完成，播放已暂停");
              return;
            }
            run.phase = "loading";
            wake();
          } catch (cause) {
            if (isCurrent()) stop(cause instanceof Error ? cause.message : "切帧失败，请重试");
          }
        })();
      }
    }
    return () => clearTimeout(timer);
  }, [
    active,
    taskId,
    sceneId,
    frameState.taskId,
    frameState.status,
    frameState.error,
    rate,
    visible,
    blocker,
    atEnd,
    revision,
    stop,
    wake,
  ]);

  return { waiting, error, pause };
}
