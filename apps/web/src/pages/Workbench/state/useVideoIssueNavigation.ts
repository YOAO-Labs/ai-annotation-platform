import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "@/api/client";
import { hasPixelAnchor, type AnnotationFeedback } from "@/api/feedbacks";
import { tasksApi } from "@/api/tasks";
import type { AnnotationResponse, TaskResponse } from "@/types";
import type { VideoFrameSeekResult, VideoStageControls } from "../stage/videoStageControls";
import type { IssueNavigation } from "./useIssuePins";
import { readVideoIssueContext } from "./videoIssueContext";

interface Options {
  projectId?: string;
  taskId?: string;
  requestedTaskId: string | null;
  annotationsReady: boolean;
  annotations: AnnotationResponse[];
  frameCount: number;
  controlsRef: React.RefObject<VideoStageControls | null>;
  selectTask: (taskId: string, signal: AbortSignal) => Promise<boolean>;
  seekFrame: (frame: number, isRelevant: () => boolean) => Promise<VideoFrameSeekResult>;
  selectObject: (id: string | null) => void;
  cacheTargetTask?: (task: TaskResponse) => void;
}

interface Request {
  issue: AnnotationFeedback;
  owner: object;
  controller: AbortController;
  originTaskId?: string;
  enteredTarget: boolean;
  enteredTargetUrl: boolean;
}

const idle: IssueNavigation = { status: "idle", frameIndex: null };

function waitUntil(check: () => boolean, signal: AbortSignal, timeout = 15_000) {
  return new Promise<boolean>((resolve) => {
    let handle = 0;
    const finish = (result: boolean) => {
      cancelAnimationFrame(handle);
      clearTimeout(timer);
      signal.removeEventListener("abort", abort);
      resolve(result);
    };
    const abort = () => finish(false);
    const timer = setTimeout(() => finish(false), timeout);
    const poll = () => {
      if (signal.aborted) return finish(false);
      if (check()) return finish(true);
      handle = requestAnimationFrame(poll);
    };
    signal.addEventListener("abort", abort, { once: true });
    poll();
  });
}

export function useVideoIssueNavigation(options: Options) {
  const latest = useRef(options);
  latest.current = options;
  const owner = useMemo(() => ({ projectId: options.projectId }), [options.projectId]);
  const ownerRef = useRef(owner);
  ownerRef.current = owner;
  const active = useRef<Request | null>(null);
  const retryTarget = useRef<AnnotationFeedback | null>(null);
  const mounted = useRef(true);
  const [state, setState] = useState<{ owner: object; value: IssueNavigation }>({
    owner,
    value: idle,
  });

  const cancel = useCallback(() => {
    active.current?.controller.abort();
    active.current = null;
    retryTarget.current = null;
    if (mounted.current) setState({ owner: ownerRef.current, value: idle });
  }, []);

  useLayoutEffect(() => {
    cancel();
  }, [owner, cancel]);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      active.current?.controller.abort();
      active.current = null;
      retryTarget.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    const request = active.current;
    if (!request) return;
    const target = request.issue.task_id;
    if (options.taskId === target) request.enteredTarget = true;
    if (options.requestedTaskId === target) request.enteredTargetUrl = true;
    const unexpected = (id: string | null | undefined, entered: boolean) =>
      id && id !== target && (entered || id !== request.originTaskId);
    if (
      unexpected(options.taskId, request.enteredTarget) ||
      unexpected(options.requestedTaskId, request.enteredTargetUrl)
    )
      cancel();
  }, [options.taskId, options.requestedTaskId, cancel]);

  const navigate = useCallback(
    async (input: AnnotationFeedback) => {
      cancel();
      const initial = latest.current;
      if (!hasPixelAnchor(input) || !input.task_id || input.project_id !== initial.projectId)
        return;
      const sourceFrame = input.anchor_position.frame;
      if (typeof sourceFrame !== "number" || !Number.isInteger(sourceFrame) || sourceFrame < 0)
        return;
      const request: Request = {
        issue: structuredClone(input),
        owner: ownerRef.current,
        controller: new AbortController(),
        originTaskId: initial.taskId,
        enteredTarget: initial.taskId === input.task_id,
        enteredTargetUrl: initial.requestedTaskId === input.task_id,
      };
      active.current = request;
      retryTarget.current = request.issue;
      const signal = request.controller.signal;
      const current = () =>
        mounted.current &&
        active.current === request &&
        ownerRef.current === request.owner &&
        !signal.aborted;
      const publish = (value: IssueNavigation) => {
        if (current()) setState({ owner: request.owner, value });
      };
      publish({ status: "preparing", frameIndex: sourceFrame });
      let lease: ReturnType<NonNullable<VideoStageControls["beginIssueRestore"]>> = null;
      let subscribedControls: VideoStageControls | null = null;
      let unsubscribe: (() => void) | undefined;
      const subscribe = (controls: VideoStageControls | null) => {
        if (subscribedControls === controls) return;
        unsubscribe?.();
        subscribedControls = controls;
        unsubscribe = controls?.subscribeIssueNavigationInterrupt?.(cancel);
      };
      try {
        subscribe(initial.controlsRef.current);
        const target = await tasksApi.get(input.task_id, { signal });
        if (!current()) return;
        if (target.project_id !== initial.projectId || target.file_type !== "video") {
          publish({
            status: "unavailable",
            frameIndex: sourceFrame,
            message: "问题所在任务已变化",
          });
          return;
        }
        latest.current.cacheTargetTask?.(target);
        if (
          latest.current.taskId !== input.task_id &&
          !(await latest.current.selectTask(input.task_id, signal))
        ) {
          publish({ status: "cancelled", frameIndex: sourceFrame });
          return;
        }
        if (!current()) return;
        const loaded = await waitUntil(() => {
          if (!current()) return false;
          subscribe(latest.current.controlsRef.current);
          return (
            latest.current.taskId === input.task_id &&
            latest.current.annotationsReady &&
            latest.current.frameCount > 0 &&
            latest.current.controlsRef.current?.captureIssueView?.()?.taskId === input.task_id
          );
        }, signal);
        if (!current()) return;
        if (!loaded) {
          publish({ status: "timeout", frameIndex: sourceFrame });
          return;
        }
        const controls = latest.current.controlsRef.current!;
        if (!(await controls.waitForIssueViewReady?.(signal))) {
          publish({ status: "unavailable", frameIndex: sourceFrame });
          return;
        }
        if (!current() || latest.current.taskId !== input.task_id) return;
        const frame = Math.min(sourceFrame, latest.current.frameCount - 1);
        const ready = await latest.current.seekFrame(frame, current);
        if (!current() || latest.current.taskId !== input.task_id) return;
        if (ready.status !== "ready" || ready.frameIndex !== frame) {
          publish({
            status: ready.status === "ready" ? "unavailable" : ready.status,
            frameIndex: frame,
          });
          return;
        }
        const context = readVideoIssueContext(request.issue);
        if (!context) {
          retryTarget.current = null;
          publish({
            status: "ready",
            frameIndex: frame,
            ...(frame !== sourceFrame ? { message: "媒体边界已变化，已按当前范围定位" } : {}),
          });
          return;
        }
        const object =
          context && input.annotation_id
            ? latest.current.annotations.find(
                (annotation) => annotation.id === input.annotation_id && annotation.is_active,
              )
            : undefined;
        const objectChanged =
          !!context &&
          (!!input.annotation_id || context.annotation_version != null || !!context.track_id) &&
          (!object ||
            (context.annotation_version != null && object.version !== context.annotation_version));
        const selectedId = object && !objectChanged ? object.id : null;
        lease = controls.beginIssueRestore?.(current, signal) ?? null;
        if (!lease) {
          publish({ status: "unavailable", frameIndex: frame });
          return;
        }
        latest.current.selectObject(selectedId);
        const captured = controls.captureIssueView?.();
        const fallbackViewport = {
          center_x: input.anchor_position.x,
          center_y: input.anchor_position.y,
          zoom: captured?.viewport.zoom ?? 1,
        };
        const result = await lease.restore(
          {
            viewport: !objectChanged && context?.viewport ? context.viewport : fallbackViewport,
            timeline_window: context?.timeline_window ?? undefined,
          },
          selectedId,
        );
        if (!current() || latest.current.taskId !== input.task_id) return;
        if (result.status !== "restored") {
          publish({ status: result.status, frameIndex: frame });
          return;
        }
        retryTarget.current = null;
        const mediaChanged =
          frame !== sourceFrame ||
          result.clamped ||
          (context?.frame_range?.to_frame ?? 0) >= latest.current.frameCount;
        publish({
          status: "ready",
          frameIndex: frame,
          message: objectChanged
            ? "对象已变化，已定位原问题的帧和位置"
            : mediaChanged
              ? "媒体或视图边界已变化，已按当前范围定位"
              : context
                ? "已恢复问题现场"
                : undefined,
        });
      } catch (error) {
        publish({
          status: "unavailable",
          frameIndex: sourceFrame,
          message:
            error instanceof ApiError && (error.status === 403 || error.status === 404)
              ? "无法访问问题所在任务"
              : "问题现场暂不可用，可重试定位",
        });
      } finally {
        lease?.release();
        unsubscribe?.();
        if (active.current === request) active.current = null;
      }
    },
    [cancel],
  );

  const retry = useCallback(async () => {
    if (active.current || !retryTarget.current) return;
    await navigate(retryTarget.current);
  }, [navigate]);

  return { navigate, cancel, retry, navigation: state.owner === owner ? state.value : idle };
}
