import { useCallback, useEffect, useRef, useState } from "react";
import type { AnnotationResponse } from "@/types";
import type {
  VideoDrawingDraft,
  VideoFrameSeekResult,
  VideoStageControls,
} from "../stage/videoStageControls";
import {
  resolveVideoScopeTransition,
  videoToolScopeForTool,
  videoTrackSelectionTool,
  type VideoToolScope,
  type VideoToolSelection,
} from "../stage/videoToolUnits";
import type { PendingDrawing, useWorkbenchState, VideoTool } from "./useWorkbenchState";
import type { VideoSelectionCommand, VideoSelectionCommandOptions } from "./videoSelectionCommand";

type VideoCommandState = Pick<
  ReturnType<typeof useWorkbenchState>,
  | "videoTool"
  | "videoToolScope"
  | "setVideoToolSelection"
  | "videoFrameIndex"
  | "setVideoFrameIndex"
  | "pendingDrawing"
  | "setPendingDrawing"
  | "selectedId"
  | "selectedIds"
  | "replaceSelected"
>;

interface Options {
  enabled: boolean;
  ownerKey: string;
  state: VideoCommandState;
  controlsRef: React.RefObject<VideoStageControls | null>;
  annotationsRef: React.RefObject<AnnotationResponse[]>;
  isToolEnabled: (tool: VideoTool) => boolean;
  toolDisabledReason: (tool: VideoTool) => string | undefined;
  needsMaskGuard: boolean;
  guardMask: () => Promise<boolean>;
  blockedReason?: string;
  explain: (reason: string) => void;
  onUserIntent?: () => void;
}

type Command =
  | { kind: "leave"; isRelevant: () => boolean }
  | { kind: "tool"; tool: VideoTool }
  | { kind: "scope"; scope: VideoToolScope }
  | {
      kind: "frame";
      frameIndex: number;
      isRelevant: () => boolean;
      onAdmitted?: (controls: VideoStageControls | null) => void;
    }
  | {
      kind: "temporary";
      tool: VideoTool;
      onAdmitted: (previous: VideoToolSelection) => void;
      isRelevant: () => boolean;
    }
  | ({
      kind: "selection";
      id: string | null;
      isRelevant?: () => boolean;
    } & VideoSelectionCommandOptions);

function pendingBelongsToDraft(pending: PendingDrawing, draft: VideoDrawingDraft) {
  if (!pending || !("frameIndex" in pending) || pending.frameIndex !== draft.frameIndex)
    return false;
  const completedKind: Partial<Record<VideoTool, NonNullable<PendingDrawing>["kind"]>> = {
    box: "video_bbox",
    track: "video_track_bbox",
    "rotated-box": "video_rotated_bbox",
    polygon: "video_polygon",
    "polygon-track": "video_track_polygon",
    polyline: "video_polyline",
    "polyline-track": "video_track_polyline",
    keypoint: "video_keypoint",
  };
  return completedKind[draft.tool] === pending.kind;
}

export function useVideoToolCommands(options: Options) {
  const latest = useRef(options);
  latest.current = options;
  const serial = useRef(0);
  const active = useRef(true);
  const confirmationRef = useRef<((discard: boolean) => void) | null>(null);
  const [confirmationOpen, setConfirmationOpen] = useState(false);

  const settleConfirmation = useCallback((discard: boolean) => {
    const resolve = confirmationRef.current;
    confirmationRef.current = null;
    setConfirmationOpen(false);
    resolve?.(discard);
  }, []);

  useEffect(() => {
    serial.current += 1;
    settleConfirmation(false);
  }, [options.ownerKey, options.enabled, settleConfirmation]);

  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      serial.current += 1;
      confirmationRef.current?.(false);
      confirmationRef.current = null;
    };
  }, []);

  const request = useCallback(
    async (command: Command) => {
      const initial = latest.current;
      const requestId = ++serial.current;
      settleConfirmation(false);
      if (!initial.enabled) return false;
      if (initial.blockedReason) {
        initial.explain(initial.blockedReason);
        return false;
      }
      const current: VideoToolSelection = {
        tool: initial.state.videoTool,
        scope: initial.state.videoToolScope,
      };
      let target: VideoToolSelection & { reason?: string } = current;
      let selectedIds: string[] | undefined;
      let targetFrame: number | undefined;
      if (command.kind === "scope") {
        target = resolveVideoScopeTransition(current, command.scope, initial.isToolEnabled);
      } else if (command.kind === "tool" || command.kind === "temporary") {
        target = {
          tool: command.tool,
          scope: videoToolScopeForTool(command.tool) ?? current.scope,
        };
      } else if (command.kind === "frame") {
        if (!Number.isInteger(command.frameIndex) || command.frameIndex < 0) return false;
        targetFrame = command.frameIndex;
      } else if (command.kind === "selection") {
        targetFrame = command.frameIndex;
        if (targetFrame !== undefined && (!Number.isInteger(targetFrame) || targetFrame < 0))
          return false;
        const annotation = initial.annotationsRef.current?.find((item) => item.id === command.id);
        const toggle = !!command.shift && !!annotation;
        const removing = toggle && initial.state.selectedIds.includes(command.id!);
        selectedIds = !command.id
          ? []
          : toggle
            ? removing
              ? initial.state.selectedIds.filter((id) => id !== command.id)
              : [...initial.state.selectedIds, command.id]
            : [command.id];
        const trackTool =
          command.activateTrackTool !== false &&
          !removing &&
          annotation &&
          videoTrackSelectionTool(annotation.geometry.type);
        if (trackTool) target = { tool: trackTool, scope: "track" };
      }
      const reason =
        command.kind === "temporary" || command.kind === "frame" || command.kind === "leave"
          ? undefined
          : initial.toolDisabledReason(target.tool);
      if (
        command.kind !== "temporary" &&
        command.kind !== "frame" &&
        command.kind !== "leave" &&
        (!initial.isToolEnabled(target.tool) || reason)
      ) {
        target = {
          ...target,
          tool: "select",
          reason: reason ?? "项目未启用此范围的工具，已保留选择工具",
        };
      }
      const changesTool = target.tool !== current.tool || target.scope !== current.scope;
      const changesSelection =
        selectedIds !== undefined &&
        (selectedIds.length !== initial.state.selectedIds.length ||
          selectedIds.some((id, index) => id !== initial.state.selectedIds[index]));
      const changesFrame =
        targetFrame !== undefined && targetFrame !== initial.state.videoFrameIndex;
      if (
        !changesTool &&
        !changesSelection &&
        !changesFrame &&
        command.kind !== "temporary" &&
        command.kind !== "frame" &&
        command.kind !== "leave"
      ) {
        if (command.kind === "selection") command.onAdmitted?.();
        if (target.reason) initial.explain(target.reason);
        return true;
      }
      const isCurrent = () =>
        active.current &&
        serial.current === requestId &&
        latest.current.enabled &&
        latest.current.ownerKey === initial.ownerKey &&
        (command.kind !== "selection" || command.isRelevant?.() !== false) &&
        ((command.kind !== "temporary" && command.kind !== "frame" && command.kind !== "leave") ||
          command.isRelevant());
      if (!isCurrent()) return false;
      const pending = initial.state.pendingDrawing;
      const hasPending = pending?.kind !== "video_mask" && !!pending?.kind?.startsWith("video_");
      const stageDraft = initial.controlsRef.current?.getDrawingDraft?.();
      if (stageDraft?.continuingTrack) {
        initial.explain("正在续画轨迹，请先松手完成本帧后再切换");
        return false;
      }
      if (hasPending || stageDraft) {
        const discard = await new Promise<boolean>((resolve) => {
          confirmationRef.current = resolve;
          setConfirmationOpen(true);
        });
        if (!discard || !isCurrent()) return false;
        if (hasPending && latest.current.state.pendingDrawing !== pending) return false;
      }
      if (initial.needsMaskGuard) {
        try {
          if (!(await initial.guardMask())) return false;
        } catch {
          if (isCurrent()) initial.explain("Mask 保存失败，已保留当前绘制");
          return false;
        }
      }
      if (!isCurrent()) return false;
      const currentPending = latest.current.state.pendingDrawing;
      const migratedPending = !!stageDraft && pendingBelongsToDraft(currentPending, stageDraft);
      if (hasPending && currentPending !== pending) return false;
      if (stageDraft && currentPending && currentPending !== pending && !migratedPending)
        return false;
      const latestReason =
        command.kind === "temporary" || command.kind === "frame" || command.kind === "leave"
          ? undefined
          : latest.current.toolDisabledReason(target.tool);
      if (
        command.kind !== "temporary" &&
        command.kind !== "frame" &&
        command.kind !== "leave" &&
        (!latest.current.isToolEnabled(target.tool) || latestReason)
      ) {
        target = {
          ...target,
          tool: "select",
          reason: latestReason ?? "项目未启用此范围的工具，已保留选择工具",
        };
      }
      if (stageDraft) latest.current.controlsRef.current?.discardDrawingDraft?.();
      if (hasPending || migratedPending) latest.current.state.setPendingDrawing(null);
      if (targetFrame !== undefined) {
        const controls = latest.current.controlsRef.current;
        if (command.kind === "frame" && command.onAdmitted) {
          command.onAdmitted(controls);
        } else if (command.kind === "frame" && controls) {
          controls.pausePlayback({ snapToGrid: false });
          controls.seekToFrame(targetFrame, { recordHistory: true });
        } else {
          latest.current.state.setVideoFrameIndex(targetFrame);
        }
      }
      if (selectedIds !== undefined) latest.current.state.replaceSelected(selectedIds);
      if (command.kind === "temporary") command.onAdmitted(current);
      if (command.kind !== "frame" && command.kind !== "leave")
        latest.current.state.setVideoToolSelection(target);
      if (command.kind === "selection") command.onAdmitted?.();
      if (target.reason) latest.current.explain(target.reason);
      return true;
    },
    [settleConfirmation],
  );

  const requestTool = useCallback(
    (tool: VideoTool) => {
      latest.current.onUserIntent?.();
      void request({ kind: "tool", tool });
    },
    [request],
  );
  const requestScope = useCallback(
    (scope: VideoToolScope) => {
      latest.current.onUserIntent?.();
      void request({ kind: "scope", scope });
    },
    [request],
  );
  const requestSelection = useCallback<VideoSelectionCommand>(
    (id, options) => {
      if (id === null && options?.source === "task-reset") {
        latest.current.state.replaceSelected([]);
        return;
      }
      latest.current.onUserIntent?.();
      void request({ kind: "selection", id, ...options });
    },
    [request],
  );

  const requestSelectionReady = useCallback(
    (id: string, isRelevant: () => boolean) =>
      request({ kind: "selection", id, activateTrackTool: false, isRelevant }),
    [request],
  );

  const requestFrame = useCallback(
    (frameIndex: number, isRelevant: () => boolean) => {
      latest.current.onUserIntent?.();
      void request({ kind: "frame", frameIndex, isRelevant });
    },
    [request],
  );

  const requestTemporaryTool = useCallback(
    (
      tool: VideoTool,
      onAdmitted: (previous: VideoToolSelection) => void,
      isRelevant: () => boolean,
    ) => {
      latest.current.onUserIntent?.();
      void request({ kind: "temporary", tool, onAdmitted, isRelevant });
    },
    [request],
  );

  const requestFrameReady = useCallback(
    async (frameIndex: number, isRelevant: () => boolean): Promise<VideoFrameSeekResult> => {
      let ready: Promise<VideoFrameSeekResult> | undefined;
      const admitted = await request({
        kind: "frame",
        frameIndex,
        isRelevant,
        onAdmitted: (controls) => {
          ready = controls
            ? controls.seekToFrameReady(frameIndex, { recordHistory: true })
            : Promise.resolve({ status: "unavailable", frameIndex, source: null });
        },
      });
      if (!admitted || !ready) return { status: "cancelled", frameIndex, source: null };
      const result = await ready;
      return active.current && isRelevant()
        ? result
        : { status: "cancelled", frameIndex, source: null };
    },
    [request],
  );

  const requestLeave = useCallback(
    (isRelevant: () => boolean) => request({ kind: "leave", isRelevant }),
    [request],
  );

  return {
    requestLeave,
    requestTool,
    requestScope,
    requestSelection,
    requestSelectionReady,
    requestFrame,
    requestFrameReady,
    requestTemporaryTool,
    confirmationOpen,
    settleConfirmation,
  };
}
