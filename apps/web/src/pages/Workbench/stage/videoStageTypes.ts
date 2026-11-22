import type {
  AnnotationResponse,
  VideoBboxGeometry,
  VideoPolygonGeometry,
  VideoRotatedBboxGeometry,
  VideoKeypointGeometry,
  Keypoint,
  VideoTrackGeometry,
  VideoTrackMaskGeometry,
  VideoTrackKeyframe,
  VideoTrackOutsideRange,
} from "@/types";

export type VideoStageGeom = { x: number; y: number; w: number; h: number };
export type VideoPoint = { x: number; y: number };
export type VideoStageGeometry =
  | VideoBboxGeometry
  | VideoRotatedBboxGeometry
  | VideoKeypointGeometry
  | VideoTrackGeometry;
export type VideoBboxAnnotation = AnnotationResponse & { geometry: VideoBboxGeometry };
export type VideoTrackAnnotation = AnnotationResponse & { geometry: VideoTrackGeometry };
export type VideoManagedTrackAnnotation = AnnotationResponse & {
  geometry: VideoTrackGeometry | VideoTrackMaskGeometry;
};
export type VideoResizeDirection = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";

export type VideoFrameEntry = {
  id: string;
  ann: AnnotationResponse;
  geom: VideoStageGeom;
  className: string;
  source: "manual" | "prediction" | "interpolated" | "legacy";
  occluded?: boolean;
  trackId?: string;
};

export type VideoTrackGhost = VideoFrameEntry & {
  ann: VideoTrackAnnotation;
  source: "manual";
  trackId: string;
  originFrame: number;
};

export type VideoTrackPreview = {
  id: string;
  trackId: string;
  className: string;
  keyframes: VideoTrackKeyframe[];
  outside?: VideoTrackOutsideRange[];
  selected: boolean;
};

export type VideoDragState =
  | { kind: "draw"; start: { x: number; y: number }; current: { x: number; y: number } }
  | {
      kind: "move";
      id: string;
      start: { x: number; y: number };
      origin: VideoStageGeom;
      current: VideoStageGeom;
    }
  | {
      kind: "resize";
      id: string;
      dir: VideoResizeDirection;
      start: { x: number; y: number };
      origin: VideoStageGeom;
      current: VideoStageGeom;
    }
  | {
      kind: "obbMove";
      id: string;
      start: VideoPoint;
      origin: VideoRotatedBboxGeometry;
      current: VideoRotatedBboxGeometry;
      imageSize: { w: number; h: number };
    }
  | {
      kind: "obbResize";
      id: string;
      dir: VideoResizeDirection;
      start: VideoPoint;
      origin: VideoRotatedBboxGeometry;
      current: VideoRotatedBboxGeometry;
      imageSize: { w: number; h: number };
    }
  | {
      kind: "obbRotate";
      id: string;
      origin: VideoRotatedBboxGeometry;
      current: VideoRotatedBboxGeometry;
      startPointerAngle: number;
      imageSize: { w: number; h: number };
    }
  | {
      kind: "keypointNode";
      id: string;
      nodeIdx: number;
      origin: Keypoint[];
      current: Keypoint[];
    }
  | {
      kind: "polyVertex";
      id: string;
      frameIndex: number;
      vidx: number;
      start: { x: number; y: number };
      origin: [number, number][];
      current: [number, number][];
    }
  | {
      kind: "polyMove";
      id: string;
      frameIndex: number;
      start: { x: number; y: number };
      origin: [number, number][];
      current: [number, number][];
      originHoles?: VideoPolygonGeometry["holes"];
      currentHoles?: VideoPolygonGeometry["holes"];
      translationBounds?: { minX: number; minY: number; maxX: number; maxY: number };
    }
  | {
      kind: "samProbe";
      mode: "point" | "bbox" | "exemplar";
      start: { x: number; y: number };
      current: { x: number; y: number };
      alt: boolean;
    }
  | { kind: "pan"; sx: number; sy: number }
  | null;

export type VideoSamPrompt =
  | { mode: "point"; pt: [number, number]; alt: boolean }
  | { mode: "bbox" | "exemplar"; bbox: [number, number, number, number]; alt: boolean };

export type VideoTrackConversionOptions = {
  operation: "copy" | "split";
  scope: "frame" | "track";
  frameIndex?: number;
  frameMode?: "keyframes" | "all_frames";
};

export type VideoTrackCompositionOptions = {
  operation: "aggregate_bboxes" | "split_track" | "merge_tracks" | "join_tracks";
  annotationIds: string[];
  frameIndex?: number;
  deleteSources?: boolean;
  gapMode?: "interpolate" | "outside";
};

export type VideoTrackKeyframePatch = Partial<VideoTrackKeyframe>;
