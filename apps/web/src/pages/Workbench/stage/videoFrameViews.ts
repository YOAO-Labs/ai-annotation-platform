import type {
  AnnotationResponse,
  Keypoint,
  VideoPolygonGeometry,
  VideoRotatedBboxGeometry,
} from "@/types";
import type { DiffMode } from "../modes/types";
import { classColor, getTrackColor } from "./colors";
import {
  deriveTrackNumber,
  isVideoBbox,
  isVideoMaskTrack,
  isVideoPolygon,
  isVideoPolygonTrack,
  isVideoPolyline,
  isVideoPolylineTrack,
  isVideoRotatedBbox,
  isVideoKeypoint,
  isVideoTrack,
  nearestPointsTrackKeyframe,
  rotatedBboxCorners,
  resolveTrackAtFrame,
  resolveVideoPolygonTrackAtFrame,
  resolveVideoPolylineTrackAtFrame,
  trackReferenceAtFrame,
} from "./videoStageGeometry";
import { isFrameOutside } from "./videoTrackOutside";
import { gridPrev } from "./videoSamplingGrid";
import {
  buildTrackLabelText,
  shouldShowLabel,
  type AnnotationVisualConfig,
} from "./annotationVisual";
import type { VideoStageGeom } from "./videoStageTypes";
import type { VideoReferenceConfig } from "./videoReferencePredict";

const DEFAULT_REFERENCE_CONFIG: VideoReferenceConfig = { mode: "off", preset: "stable" };

export type VideoEntryView = {
  key: string;
  id: string;
  geom: VideoStageGeom;
  points?: [number, number][];
  holes?: VideoPolygonGeometry["holes"];
  open?: boolean;
  rotatedBbox?: VideoRotatedBboxGeometry;
  keypoints?: Keypoint[];
  color: string;
  selected: boolean;
  dashed: boolean;
  occluded: boolean;
  predicted: boolean;
  labelText: string;
  className: string;
};

export type VideoPreviewPoint = { frame: number; x: number; y: number; occluded: boolean };

export type VideoTrackPreviewView = {
  key: string;
  id: string;
  color: string;
  selected: boolean;
  points: VideoPreviewPoint[];
};

export type VideoGhostView = {
  id: string;
  geom: VideoStageGeom;
  points?: [number, number][];
  open?: boolean;
  color: string;
  labelText: string;
  uncertainty?: { sx: number; sy: number; sw: number; sh: number };
};

export type VideoLabelView = {
  key: string;
  geom: VideoStageGeom;
  color: string;
  text: string;
  opacity?: number;
};

function annotationRenderKey(ann: AnnotationResponse): string {
  return ann.render_key ?? ann.id;
}

export interface VideoFrameViews {
  entries: VideoEntryView[];
  previews: VideoTrackPreviewView[];
  ghost: VideoGhostView | null;
  carryOverGhosts: VideoGhostView[];
  labels: VideoLabelView[];
}

export interface DeriveVideoFrameViewsInput {
  annotations: AnnotationResponse[];
  frameIndex: number;
  selectedId: string | null;
  selectedIds?: readonly string[];
  hiddenTrackIds?: Set<string>;
  lockedTrackIds?: Set<string>;
  reviewDisplayMode?: DiffMode;
  trackColorOverrides?: Record<string, string>;
  visual: AnnotationVisualConfig;
  referenceConfig?: VideoReferenceConfig;
  pendingDraft?: { geom: VideoStageGeom; className: string } | null;
  samplingStep?: number;
}

const EMPTY_SET = new Set<string>();

export function visibleInReviewMode(source: string | null, mode?: DiffMode): boolean {
  if (!mode || mode === "diff") return true;
  if (!source) return false;
  if (mode === "raw") return source === "prediction" || source === "interpolated";
  return source === "manual" || source === "legacy";
}

export function deriveVideoFrameViews(input: DeriveVideoFrameViewsInput): VideoFrameViews {
  const {
    annotations,
    frameIndex,
    selectedId,
    selectedIds,
    hiddenTrackIds = EMPTY_SET,
    lockedTrackIds = EMPTY_SET,
    reviewDisplayMode,
    trackColorOverrides,
    visual,
    referenceConfig = DEFAULT_REFERENCE_CONFIG,
    pendingDraft,
    samplingStep = 1,
  } = input;

  const selectedSet = new Set(selectedIds ?? (selectedId ? [selectedId] : []));

  const videoTracks = annotations.filter(isVideoTrack);
  const polygonTracks = annotations.filter(isVideoPolygonTrack);
  const polylineTracks = annotations.filter(isVideoPolylineTrack);
  const maskTracks = annotations.filter(isVideoMaskTrack);
  const trackNumbers = deriveTrackNumber([
    ...videoTracks,
    ...polygonTracks,
    ...polylineTracks,
    ...maskTracks,
  ]);
  const trackContent = visual.labelContent.track;

  const entries: VideoEntryView[] = [];
  const currentFrameTrackIds = new Set<string>();
  for (const ann of annotations) {
    if (ann.is_hidden) continue;
    if (isVideoBbox(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      entries.push(
        buildEntryView(
          ann,
          ann.geometry,
          "legacy",
          false,
          undefined,
          selectedSet,
          trackNumbers,
          trackColorOverrides,
          trackContent,
        ),
      );
    } else if (isVideoPolygon(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const entry = buildEntryView(
        ann,
        boundsOfPoints(ann.geometry.points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: ann.geometry.points, holes: ann.geometry.holes });
    } else if (isVideoPolyline(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const entry = buildEntryView(
        ann,
        boundsOfPoints(ann.geometry.points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: ann.geometry.points, open: true });
    } else if (isVideoRotatedBbox(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const corners = rotatedBboxCorners(ann.geometry);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(corners),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: corners, rotatedBbox: ann.geometry });
    } else if (isVideoKeypoint(ann) && ann.geometry.frame_index === frameIndex) {
      if (!visibleInReviewMode("legacy", reviewDisplayMode)) continue;
      const points = ann.geometry.points.map((point) => [point.x, point.y] as [number, number]);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(points),
        "legacy",
        false,
        undefined,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, keypoints: ann.geometry.points });
    } else if (isVideoTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      const resolved = resolveTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      entries.push(
        buildEntryView(
          ann,
          resolved.geom,
          resolved.source,
          Boolean(resolved.occluded),
          ann.geometry.track_id,
          selectedSet,
          trackNumbers,
          trackColorOverrides,
          trackContent,
        ),
      );
    } else if (isVideoPolygonTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      const resolved = resolveVideoPolygonTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(resolved.points),
        resolved.source,
        Boolean(resolved.occluded),
        ann.geometry.track_id,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: resolved.points });
    } else if (isVideoPolylineTrack(ann) && !hiddenTrackIds.has(ann.geometry.track_id)) {
      const resolved = resolveVideoPolylineTrackAtFrame(ann.geometry, frameIndex);
      if (!resolved || !visibleInReviewMode(resolved.source, reviewDisplayMode)) continue;
      currentFrameTrackIds.add(ann.geometry.track_id);
      const entry = buildEntryView(
        ann,
        boundsOfPoints(resolved.points),
        resolved.source,
        Boolean(resolved.occluded),
        ann.geometry.track_id,
        selectedSet,
        trackNumbers,
        trackColorOverrides,
        trackContent,
      );
      entries.push({ ...entry, points: resolved.points, open: true });
    }
  }

  const previews: VideoTrackPreviewView[] = videoTracks
    .filter(
      (ann) =>
        !ann.is_hidden &&
        !hiddenTrackIds.has(ann.geometry.track_id) &&
        currentFrameTrackIds.has(ann.geometry.track_id),
    )
    .map((ann) => {
      const previewTrack = {
        type: "video_track_bbox" as const,
        track_id: ann.geometry.track_id,
        keyframes: ann.geometry.keyframes,
        outside: ann.geometry.outside,
      };
      const points: VideoPreviewPoint[] = [...ann.geometry.keyframes]
        .filter((kf) => !isFrameOutside(previewTrack, kf.frame_index))
        .sort((a, b) => a.frame_index - b.frame_index)
        .map((kf) => ({
          frame: kf.frame_index,
          x: kf.bbox.x + kf.bbox.w / 2,
          y: kf.bbox.y + kf.bbox.h / 2,
          occluded: Boolean(kf.occluded),
        }));
      return {
        key: annotationRenderKey(ann),
        id: ann.id,
        color: getTrackColor(ann.geometry.track_id, ann.class_name, trackColorOverrides),
        selected: selectedSet.has(ann.id),
        points,
      };
    });

  let ghost: VideoGhostView | null = null;
  const selectedTrack = videoTracks.find((ann) => ann.id === selectedId) ?? null;
  if (
    selectedTrack &&
    !selectedTrack.is_hidden &&
    !hiddenTrackIds.has(selectedTrack.geometry.track_id) &&
    !lockedTrackIds.has(selectedTrack.geometry.track_id) &&
    visibleInReviewMode("manual", reviewDisplayMode) &&
    !entries.some((e) => e.id === selectedTrack.id)
  ) {
    const reference = trackReferenceAtFrame(
      selectedTrack.geometry,
      frameIndex,
      referenceConfig.mode,
      referenceConfig.preset,
    );
    if (reference) {
      const num = trackNumbers.get(selectedTrack.id);
      const refLabel = reference.predicted
        ? `${reference.predictedKind === "kalman" ? "卡尔曼" : "预测"} F${reference.originFrame}↗`
        : `参考 F${reference.originFrame}`;
      ghost = {
        id: selectedTrack.id,
        geom: reference.bbox,
        color: getTrackColor(
          selectedTrack.geometry.track_id,
          selectedTrack.class_name,
          trackColorOverrides,
        ),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${selectedTrack.class_name} · ${refLabel}`,
        uncertainty: reference.uncertainty,
      };
    }
  }

  if (!ghost) {
    const selectedPointsTrack =
      polygonTracks.find((ann) => ann.id === selectedId) ??
      polylineTracks.find((ann) => ann.id === selectedId) ??
      null;
    if (
      selectedPointsTrack &&
      !selectedPointsTrack.is_hidden &&
      !hiddenTrackIds.has(selectedPointsTrack.geometry.track_id) &&
      !lockedTrackIds.has(selectedPointsTrack.geometry.track_id) &&
      visibleInReviewMode("manual", reviewDisplayMode) &&
      !entries.some((e) => e.id === selectedPointsTrack.id)
    ) {
      const reference = nearestPointsTrackKeyframe(selectedPointsTrack.geometry, frameIndex);
      if (reference) {
        const num = trackNumbers.get(selectedPointsTrack.id);
        ghost = {
          id: selectedPointsTrack.id,
          geom: boundsOfPoints(reference.points),
          points: reference.points,
          open: isVideoPolylineTrack(selectedPointsTrack),
          color: getTrackColor(
            selectedPointsTrack.geometry.track_id,
            selectedPointsTrack.class_name,
            trackColorOverrides,
          ),
          labelText: `${num !== undefined ? `#${num} · ` : ""}${selectedPointsTrack.class_name} · 参考 F${reference.originFrame}`,
        };
      }
    }
  }

  const carryOverGhosts: VideoGhostView[] = [];
  const prevGridFrame = gridPrev(frameIndex, samplingStep, frameIndex);
  if (prevGridFrame < frameIndex && visibleInReviewMode("manual", reviewDisplayMode)) {
    for (const ann of videoTracks) {
      if (ann.is_hidden || ann.id === selectedId) continue;
      const tid = ann.geometry.track_id;
      if (hiddenTrackIds.has(tid) || lockedTrackIds.has(tid)) continue;
      const keyframes = ann.geometry.keyframes;
      if (!keyframes.some((kf) => kf.frame_index === prevGridFrame)) continue;
      if (keyframes.some((kf) => kf.frame_index === frameIndex)) continue;
      if (entries.some((e) => e.id === ann.id)) continue;
      const reference = trackReferenceAtFrame(
        ann.geometry,
        frameIndex,
        referenceConfig.mode,
        referenceConfig.preset,
      );
      if (!reference) continue;
      const num = trackNumbers.get(ann.id);
      carryOverGhosts.push({
        id: ann.id,
        geom: reference.bbox,
        color: getTrackColor(tid, ann.class_name, trackColorOverrides),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${ann.class_name}`,
      });
    }
    for (const ann of [...polygonTracks, ...polylineTracks]) {
      if (ann.is_hidden || ann.id === selectedId) continue;
      const tid = ann.geometry.track_id;
      if (hiddenTrackIds.has(tid) || lockedTrackIds.has(tid)) continue;
      const keyframes = ann.geometry.keyframes;
      if (!keyframes.some((kf) => kf.frame_index === prevGridFrame)) continue;
      if (keyframes.some((kf) => kf.frame_index === frameIndex)) continue;
      if (entries.some((e) => e.id === ann.id)) continue;
      const reference = nearestPointsTrackKeyframe(ann.geometry, frameIndex);
      if (!reference) continue;
      const num = trackNumbers.get(ann.id);
      carryOverGhosts.push({
        id: ann.id,
        geom: boundsOfPoints(reference.points),
        points: reference.points,
        open: isVideoPolylineTrack(ann),
        color: getTrackColor(tid, ann.class_name, trackColorOverrides),
        labelText: `${num !== undefined ? `#${num} · ` : ""}${ann.class_name}`,
      });
    }
  }

  const visibility = visual.labelVisibility;
  const labels: VideoLabelView[] = [];
  for (const entry of entries) {
    if (shouldShowLabel(entry.selected, visibility)) {
      labels.push({
        key: `entry-${entry.key}`,
        geom: entry.geom,
        color: entry.color,
        text: entry.labelText,
      });
    }
  }
  if (pendingDraft && shouldShowLabel(true, visibility)) {
    labels.push({
      key: "pending-draft",
      geom: pendingDraft.geom,
      color: classColor(pendingDraft.className),
      text: pendingDraft.className,
      opacity: 0.9,
    });
  }
  if (ghost && shouldShowLabel(true, visibility)) {
    labels.push({
      key: `ghost-${ghost.id}`,
      geom: ghost.geom,
      color: ghost.color,
      text: ghost.labelText,
      opacity: 0.86,
    });
  }
  if (shouldShowLabel(true, visibility)) {
    for (const g of carryOverGhosts) {
      labels.push({
        key: `carryover-${g.id}`,
        geom: g.geom,
        color: g.color,
        text: g.labelText,
        opacity: 0.6,
      });
    }
  }

  return { entries, previews, ghost, carryOverGhosts, labels };
}

function boundsOfPoints(points: [number, number][]): VideoStageGeom {
  if (points.length === 0) return { x: 0, y: 0, w: 0, h: 0 };
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (y < minY) minY = y;
    if (x > maxX) maxX = x;
    if (y > maxY) maxY = y;
  }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

function buildEntryView(
  ann: AnnotationResponse,
  geom: VideoStageGeom,
  source: "manual" | "prediction" | "interpolated" | "legacy",
  occluded: boolean,
  trackId: string | undefined,
  selectedSet: ReadonlySet<string>,
  trackNumbers: ReadonlyMap<string, number>,
  trackColorOverrides: Record<string, string> | undefined,
  trackContent: AnnotationVisualConfig["labelContent"]["track"],
): VideoEntryView {
  const color = trackId
    ? getTrackColor(trackId, ann.class_name, trackColorOverrides)
    : classColor(ann.class_name);
  const trackNumber = trackNumbers.get(ann.id);
  const renderKey = annotationRenderKey(ann);
  const stateSuffix = source === "interpolated" ? "插值" : occluded ? "遮挡" : undefined;
  return {
    key: `${renderKey}-${trackId ?? "legacy"}`,
    id: ann.id,
    geom,
    color,
    selected: selectedSet.has(ann.id),
    dashed: source === "interpolated" || occluded,
    occluded,
    predicted: source === "prediction",
    labelText: buildTrackLabelText(
      {
        className: ann.class_name,
        trackNumber,
        stateSuffix,
        attributes: (ann as { attributes?: Record<string, unknown> | null }).attributes ?? null,
      },
      trackContent,
    ),
    className: ann.class_name,
  };
}
