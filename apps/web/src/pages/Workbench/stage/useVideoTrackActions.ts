import { useCallback, useMemo } from "react";
import type { AnnotationResponse, VideoTrackGeometry, VideoTrackMaskGeometry } from "@/types";
import { nearestTrackBbox, upsertKeyframe } from "./videoStageGeometry";
import { addOutsideRange, isFrameOutside, removeOutsideFrame } from "./videoTrackOutside";
import type { VideoManagedTrackAnnotation } from "./videoStageTypes";
import { resolveVideoMaskTrackAtFrame } from "./videoStageGeometry";

export type TrackMarkPatch = {
  outside?: boolean;
  occluded?: boolean;
  source?: "manual" | "prediction";
};

export type VideoTrackActions = ReturnType<typeof useVideoTrackActions>;

interface UseVideoTrackActionsArgs {
  selectedTrack: VideoManagedTrackAnnotation | null;
  frameIndex: number;
  readOnly: boolean;
  hiddenTrackIds: Set<string>;
  lockedTrackIds: Set<string>;
  onUpdate: (
    annotation: AnnotationResponse,
    geometry: VideoTrackGeometry | VideoTrackMaskGeometry,
  ) => void;
  onToggleHiddenTrack?: (trackId: string) => void;
  onToggleLockedTrack?: (trackId: string) => void;
  onPropagateTrack?: (annotation: VideoManagedTrackAnnotation) => void;
}

export function useVideoTrackActions({
  selectedTrack,
  frameIndex,
  readOnly,
  hiddenTrackIds,
  lockedTrackIds,
  onUpdate,
  onToggleHiddenTrack,
  onToggleLockedTrack,
  onPropagateTrack,
}: UseVideoTrackActionsArgs) {
  const selectedTrackId = selectedTrack?.geometry.track_id ?? null;
  const selectedTrackLocked = Boolean(selectedTrackId && lockedTrackIds.has(selectedTrackId));
  const currentFrameOutside = Boolean(
    selectedTrack && isFrameOutside(selectedTrack.geometry, frameIndex),
  );
  const currentFrameOccluded = Boolean(
    selectedTrack &&
    !currentFrameOutside &&
    (selectedTrack.geometry.type === "video_track_mask"
      ? resolveVideoMaskTrackAtFrame(selectedTrack.geometry, frameIndex)?.occluded
      : selectedTrack.geometry.keyframes.find((kf) => kf.frame_index === frameIndex)?.occluded),
  );
  const canEditSelectedTrack = Boolean(
    selectedTrack && !selectedTrack.is_locked && !readOnly && !selectedTrackLocked,
  );

  const markSelectedTrack = useCallback(
    (patch: TrackMarkPatch) => {
      if (
        !selectedTrack ||
        selectedTrack.is_locked ||
        readOnly ||
        lockedTrackIds.has(selectedTrack.geometry.track_id)
      )
        return;
      if (patch.outside) {
        onUpdate(
          selectedTrack,
          addOutsideRange(selectedTrack.geometry, {
            from: frameIndex,
            to: frameIndex,
            source: patch.source === "prediction" ? "prediction" : "manual",
          }),
        );
        return;
      }
      const visibleTrack = removeOutsideFrame(selectedTrack.geometry, frameIndex);
      if (visibleTrack.type === "video_track_mask") {
        const resolved = resolveVideoMaskTrackAtFrame(visibleTrack, frameIndex);
        if (!resolved) return;
        onUpdate(selectedTrack, {
          ...visibleTrack,
          keyframes: [
            ...visibleTrack.keyframes.filter((keyframe) => keyframe.frame_index !== frameIndex),
            {
              frame_index: frameIndex,
              mask: resolved.mask,
              source: patch.source ?? "manual",
              occluded: patch.occluded ?? resolved.occluded ?? false,
              attributes: resolved.attributes,
            },
          ].sort((a, b) => a.frame_index - b.frame_index),
        });
        return;
      }
      const bbox = nearestTrackBbox(visibleTrack, frameIndex);
      const keyframePatch = patch.source
        ? { occluded: patch.occluded, source: patch.source }
        : { occluded: patch.occluded };
      onUpdate(selectedTrack, upsertKeyframe(visibleTrack, frameIndex, bbox, keyframePatch));
    },
    [frameIndex, lockedTrackIds, onUpdate, readOnly, selectedTrack],
  );

  const toggleSelectedTrackOutside = useCallback(() => {
    markSelectedTrack(
      currentFrameOutside
        ? { outside: false, occluded: false }
        : { outside: true, occluded: false },
    );
  }, [currentFrameOutside, markSelectedTrack]);

  const toggleSelectedTrackOccluded = useCallback(() => {
    markSelectedTrack(
      currentFrameOccluded
        ? { outside: false, occluded: false }
        : { outside: false, occluded: true },
    );
  }, [currentFrameOccluded, markSelectedTrack]);

  const toggleSelectedTrackHidden = useCallback(() => {
    if (!selectedTrackId || readOnly || !onToggleHiddenTrack) return;
    onToggleHiddenTrack(selectedTrackId);
  }, [onToggleHiddenTrack, readOnly, selectedTrackId]);

  const toggleSelectedTrackLocked = useCallback(() => {
    if (!selectedTrackId || readOnly || !onToggleLockedTrack) return;
    onToggleLockedTrack(selectedTrackId);
  }, [onToggleLockedTrack, readOnly, selectedTrackId]);

  const propagateSelectedTrack = useCallback(() => {
    if (
      !selectedTrack ||
      selectedTrack.is_locked ||
      readOnly ||
      selectedTrackLocked ||
      !onPropagateTrack
    )
      return;
    onPropagateTrack(selectedTrack);
  }, [onPropagateTrack, readOnly, selectedTrack, selectedTrackLocked]);

  return useMemo(
    () => ({
      selectedTrackHidden: Boolean(selectedTrackId && hiddenTrackIds.has(selectedTrackId)),
      selectedTrackLocked,
      currentFrameOutside,
      currentFrameOccluded,
      canEditSelectedTrack,
      markSelectedTrack,
      toggleSelectedTrackOutside,
      toggleSelectedTrackOccluded,
      toggleSelectedTrackHidden,
      toggleSelectedTrackLocked,
      propagateSelectedTrack,
    }),
    [
      canEditSelectedTrack,
      currentFrameOccluded,
      currentFrameOutside,
      hiddenTrackIds,
      markSelectedTrack,
      propagateSelectedTrack,
      selectedTrackId,
      selectedTrackLocked,
      toggleSelectedTrackHidden,
      toggleSelectedTrackLocked,
      toggleSelectedTrackOccluded,
      toggleSelectedTrackOutside,
    ],
  );
}
