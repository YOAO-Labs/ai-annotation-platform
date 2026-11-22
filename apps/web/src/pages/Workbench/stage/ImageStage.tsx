import { CopyableError } from "@/components/ui/CopyableError";
import type { CommitPolygonSlice } from "./usePolygonSlice";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type SetStateAction,
} from "react";
import {
  Stage,
  Layer,
  Image as KonvaImage,
  Rect,
  Line,
  Circle,
  Group,
  Label,
  Tag,
  Text,
  Shape,
} from "react-konva";
import type Konva from "konva";
import type { Annotation, Geometry, RotatedBboxGeometry, Keypoint, KeypointSchema } from "@/types";
import { ContextMenu } from "@/components/ui/ContextMenu";
import type { PendingDrawing, Tool } from "../state/useWorkbenchState";
import { isWorkbenchInteractionBlocked } from "../state/workbenchInteractionGuards";
import type { AiBox } from "../state/transforms";
import { useElementSize, type Viewport } from "../state/useViewportTransform";
import { applyResize, applyRotatedResize, type ResizeDirection } from "./ResizeHandles";
import { classColorForCanvas, displayClassName, hexToRgb, hexToRgba } from "./colors";
import { SelectionOverlay } from "./SelectionOverlay";
import { TOOL_REGISTRY, type PolygonDraftHandle, type KeypointDraftHandle } from "./tools";
import { CLOSE_DISTANCE } from "./tools/PolygonTool";
import { usePolygonAutoPoints } from "./usePolygonAutoPoints";
import { POLYGON_AUTO_POINT_LIMIT } from "./polygonAutoPoints";
import { usePolygonBoundaryTrace } from "./usePolygonBoundaryTrace";
import { PolygonBoundaryTraceControls } from "./PolygonBoundaryTraceControls";
import { usePolygonSlice } from "./usePolygonSlice";
import { PolygonSliceControls } from "./PolygonSliceControls";
import { pickBoundary } from "./shared/geometry/polygonBoundaryTrace";
import { polygonSliceUnavailableReason } from "./shared/geometry/polygonSlice";
import { CanvasDrawingLayer } from "./CanvasDrawingLayer";
import { MaskOverlayLayer } from "./overlays/MaskOverlayLayer";
import { TiledMaskOverlayLayer } from "./overlays/TiledMaskOverlayLayer";
import { MaskCompareTileLayer } from "./overlays/MaskCompareTileLayer";
import {
  maskCompareCompanionVisible,
  type MaskCompareTileStore,
} from "./shared/maskCompareTileStore";
import type { UseMaskEditorReturn } from "../state/useMaskEditor";
import { MaskBuffer } from "./shared/geometry/maskBuffer";
import { MASK_BRUSH_MIN_PX, MASK_BRUSH_MAX_PX } from "../state/useMaskEditor";
import { canEditMask } from "../state/canEditMask";
import type { CommentCanvasDrawing } from "@/api/comments";
import { Icon } from "@/components/ui/Icon";
import {
  isSelfIntersecting,
  isSelfIntersectingIncremental,
  moveVertex,
  type Pt,
} from "./polygonGeom";
import { buildSnapIndex, type SnapMatch } from "./shared/geometry/snap";
import { BlurhashLayer } from "./BlurhashLayer";
import { useAbortableImage } from "./useAbortableImage";
import {
  KonvaBox,
  KonvaPolygon,
  KonvaRotatedBox,
  KonvaPolyline,
  KonvaKeypoint,
  keypointColorByIndex,
  SIBLING_HIGHLIGHT_COLOR,
} from "./ImageStageShapes";
import {
  imageBoxFromDrag,
  type BboxCreationMode,
  isNormalizedImagePoint,
  normalizeImageCoordinate,
  resolveSnapMatch,
  shouldRenderImageAnnotationShape,
  siblingHighlightChildren,
} from "./ImageStage.helpers";
import {
  canTranslateAnnotationGeometry,
  clamp01,
  translateGeometry,
} from "../state/geometryTranslate";
import { BOX_LABEL_FONT_FAMILY } from "./boxVisual";
import {
  buildLabelText,
  fillAlpha,
  resolveAnnotationVisual,
  shouldShowLabel,
} from "./annotationVisual";
import {
  buildImageContextMenuItems,
  findContextMenuAnnotationId,
  shouldSuppressImageContextMenu,
  type ImageContextMenuClipboardActions,
} from "./imageStageContextMenu";
import { wheelZoomFactor } from "./imageStageSettings";
import { fitAwareScaleRange, zoomAtPoint } from "./shared/viewport/zoom";
import { supportsSingleRingPolygonEdit } from "./shared/geometry/geometryEditPolicy";
import { IssueLayer } from "./image/IssueLayer";
import { ImageStageCommentBadges } from "./ImageStageCommentBadges";
import { useWorkbenchConfig } from "../state/useWorkbenchConfig";
import { useWorkbenchPerf } from "./shared/useWorkbenchPerf";
import { useRafThrottle } from "./shared/useRafThrottle";
import { useCanvasContextMenu } from "./useCanvasContextMenu";
import { pickTopRasterMaskAt, type RasterMaskRenderRecord } from "./shared/rasterMaskRender";
import type { RasterMaskRecordStatus } from "./shared/useRasterMaskRecords";
import { RasterMaskVisual } from "./RasterMaskVisual";
import { KonvaImageTileLayer } from "./KonvaImageTileLayer";
import type { WorkbenchImageSource } from "./imagePyramid";
import { useImageTileScheduler } from "./useImageTileScheduler";
import { useImageStageFit } from "./useImageStageFit";
import type {
  RasterResourceCoordinator,
  RasterResourceReservation,
} from "./shared/rasterResourceCoordinator";
import styles from "./ImageStage.module.css";

type Geom = { x: number; y: number; w: number; h: number };
type Drag =
  | { kind: "draw"; sx: number; sy: number; cx: number; cy: number; fromCenter?: boolean }
  | {
      kind: "samProbe";
      mode: "point" | "bbox" | "exemplar";
      sx: number;
      sy: number;
      cx: number;
      cy: number;
      alt: boolean;
    }
  | { kind: "samScribble"; points: [number, number][]; alt: boolean }
  | { kind: "move"; id: string; start: Geom; sx: number; sy: number; cur: Geom; alt: boolean }
  | {
      kind: "moveRotatedBox";
      id: string;
      start: RotatedBboxGeometry;
      sx: number;
      sy: number;
      cur: RotatedBboxGeometry;
    }
  | {
      kind: "resize";
      id: string;
      start: Geom;
      sx: number;
      sy: number;
      dir: ResizeDirection;
      cur: Geom;
    }
  | {
      kind: "resizeRotatedBox";
      id: string;
      start: RotatedBboxGeometry;
      sx: number;
      sy: number;
      dir: ResizeDirection;
      cur: RotatedBboxGeometry;
    }
  | { kind: "polyVertex"; id: string; vidx: number; start: Pt[]; cur: Pt[] }
  | { kind: "polyMove"; id: string; start: Pt[]; sx: number; sy: number; cur: Pt[] }
  | { kind: "kpNode"; id: string; nidx: number; start: Keypoint[]; cur: Keypoint[] }
  | { kind: "pan"; sx: number; sy: number }
  | { kind: "canvasStroke"; points: number[] }
  | { kind: "maskBrush"; lastX: number; lastY: number }
  | { kind: "maskSlice"; start: [number, number]; end: [number, number] }
  | { kind: "maskLasso"; points: [number, number][] }
  | { kind: "rotateBox"; id: string; cx: number; cy: number; startAngle: number; cur: number };

function translatePolygon(points: Pt[], dx: number, dy: number): Pt[] {
  let minX = Infinity,
    maxX = -Infinity,
    minY = Infinity,
    maxY = -Infinity;
  for (const [x, y] of points) {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }
  const cdx = Math.max(-minX, Math.min(1 - maxX, dx));
  const cdy = Math.max(-minY, Math.min(1 - maxY, dy));
  return points.map(([x, y]) => [x + cdx, y + cdy] as Pt);
}

function rotatedBboxChanged(before: RotatedBboxGeometry, after: RotatedBboxGeometry): boolean {
  return (
    before.cx !== after.cx ||
    before.cy !== after.cy ||
    before.w !== after.w ||
    before.h !== after.h ||
    before.angle !== after.angle
  );
}

function SamRefineButton({
  left,
  top,
  onClick,
}: {
  left: number;
  top: number;
  onClick: React.MouseEventHandler<HTMLButtonElement>;
}) {
  const ref = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.setProperty("--sam-refine-left", `${left}px`);
    el.style.setProperty("--sam-refine-top", `${top - 32}px`);
  }, [left, top]);

  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      data-testid="sam-candidate-refine"
      className={styles.samRefineButton}
      title="精修 SAM 候选 (R)"
    >
      ✎ 精修 <kbd className={styles.samRefineKey}>R</kbd>
    </button>
  );
}

interface ImageStageProps {
  resourceCoordinator?: RasterResourceCoordinator;
  maskCompareStore?: MaskCompareTileStore | null;
  rasterMaskRecords?: readonly RasterMaskRenderRecord<"annotation">[];
  predictionRasterMaskRecords?: readonly RasterMaskRenderRecord<"prediction">[];
  tiledMaskOverviewRecord?: RasterMaskRenderRecord<"annotation"> | null;
  rasterMaskStatusById?: ReadonlyMap<string, RasterMaskRecordStatus>;
  onRetryRasterMask?: (id: string) => void;
  maskReadOnly?: boolean;
  fileUrl: string | null;
  imageSource?: WorkbenchImageSource | null;
  onRetryImagePyramid?: () => Promise<void>;
  mediaKey?: string | null;
  blurhash?: string | null;
  imageWidth?: number | null;
  imageHeight?: number | null;
  tool: Tool;
  bboxCreationMode?: BboxCreationMode;
  activeClass: string;
  selectedId: string | null;
  selectedIds?: string[];
  userBoxes: Annotation[];
  aiBoxes: AiBox[];
  spacePan: boolean;
  vp: Viewport;
  setVp: React.Dispatch<React.SetStateAction<Viewport>>;
  fitTick: number;
  readOnly?: boolean;
  continuousCreation?: boolean;
  fadedAiIds?: Set<string>;
  pendingDrawing?: PendingDrawing;
  nudgeMap?: Map<string, Geom>;
  pendingGeomMap?: Map<string, Geometry>;
  onJoinSelected?: () => void;
  onCropSelected?: (baseId: string) => void;
  onCommitPolygonSlice?: CommitPolygonSlice;
  onSelectBox: (id: string | null, opts?: { shift?: boolean }) => void;
  onAcceptPrediction?: (b: AiBox) => void;
  onRejectPrediction?: (b: AiBox) => void;
  onDeleteUserBox?: (id: string) => void;
  onChangeUserBoxClass?: (id: string) => void;
  onPatchShapeFlag?: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  clipboardActions?: ImageContextMenuClipboardActions | null;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
  onCommitDrawing?: (geo: Geom) => void;
  onCommitRotatedBbox?: (geo: Geom) => void;
  onCommitRotateBbox?: (
    id: string,
    before: RotatedBboxGeometry,
    after: RotatedBboxGeometry,
  ) => void;
  onSamPrompt?: (
    prompt:
      | { kind: "point"; pt: [number, number]; alt: boolean }
      | { kind: "bbox"; bbox: [number, number, number, number] }
      | {
          kind: "scribble";
          points: [number, number][];
          alt: boolean;
          width: number;
        }
      | { kind: "exemplar"; bbox: [number, number, number, number]; alt: boolean },
  ) => void;
  samCandidates?: {
    id: string;
    type: "polygonlabels" | "rectanglelabels";
    points?: [number, number][];
    bbox?: { x: number; y: number; width: number; height: number };
  }[];
  samMaskRecords?: readonly RasterMaskRenderRecord<"interactive">[];
  onSelectSamMaskCandidate?: (candidateId: string) => void;
  samActiveIdx?: number;
  samSessionPoints?: { pt: [number, number]; polarity: 1 | 0 }[];
  samSessionScribbles?: {
    points: [number, number][];
    polarity: 1 | 0;
    width: number;
  }[];
  samSessionExemplars?: { bbox: [number, number, number, number]; polarity: 1 | 0 }[];
  onCommitMove?: (
    id: string,
    before: Geom,
    after: Geom,
    childMoves?: { id: string; before: Geometry; after: Geometry }[],
  ) => void;
  onCommitResize?: (id: string, before: Geom, after: Geom) => void;
  onCommitPolygonGeometry?: (id: string, before: Pt[], after: Pt[]) => void;
  onCursorMove: (pt: { x: number; y: number } | null) => void;
  onStageGeometry?: (g: { imgW: number; imgH: number; vpSize: { w: number; h: number } }) => void;
  overlay?: React.ReactNode;
  polygonDraft?: PolygonDraftHandle;
  keypointDraft?: KeypointDraftHandle;
  keypointSchema?: KeypointSchema | null;
  onCommitKeypointGeometry?: (id: string, before: Keypoint[], after: Keypoint[]) => void;
  samSubTool?: "point" | "bbox" | "scribble" | "text" | "exemplar" | null;
  samPolarity?: "positive" | "negative";
  canvasShapes?: NonNullable<CommentCanvasDrawing["shapes"]>;
  canvasEditable?: boolean;
  canvasStroke?: string;
  onCanvasStrokeCommit?: (points: number[], stroke: string) => void;
  historicalShapes?: NonNullable<CommentCanvasDrawing["shapes"]>;
  maskEditor?: UseMaskEditorReturn;
  onRefineSamCandidate?: (idx: number) => void;
  projectRenderingConfig?: import("@/api/projects").ProjectRenderingConfig | null;
  issuePixelFeedbacks?: import("@/api/feedbacks").AnnotationFeedback[];
  highlightIssueId?: string | null;
  onIssuePinClick?: (id: string) => void;
  issuePinDropArmed?: boolean;
  onIssuePinDrop?: (x: number, y: number) => void;
  annotationCommentCounts?: Record<string, number>;
  onOpenAnnotationComments?: (annotationId: string) => void;
}

type SamCandidateShape = {
  id: string;
  type: "polygonlabels" | "rectanglelabels";
  points?: [number, number][];
  bbox?: { x: number; y: number; width: number; height: number };
};

const SAM_CANDIDATE_STROKE = "#a855f7";
const SAM_DASH: [number, number] = [6, 4];
const SAM_DASH_PERIOD = SAM_DASH[0] + SAM_DASH[1];
const SAM_FLOW_SPEED = 22;
const MASK_OPERATION_PREVIEW_COLOR = [245, 158, 11] as const;
const MASK_INSTANCE_PREVIEW_COLOR = [14, 165, 233] as const;

function SamCandidateOverlay({
  candidates,
  activeIdx,
  imgW,
  imgH,
  scale,
  tool,
}: {
  candidates: SamCandidateShape[];
  activeIdx: number;
  imgW: number;
  imgH: number;
  scale: number;
  tool: string;
}) {
  const hasCandidates = candidates.length > 0;
  const [dashOffset, setDashOffset] = useState(0);
  useEffect(() => {
    if (!hasCandidates) return;
    let raf = 0;
    let start: number | null = null;
    const loop = (t: number) => {
      if (start === null) start = t;
      setDashOffset(-(((t - start) / 1000) * SAM_FLOW_SPEED) % SAM_DASH_PERIOD);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [hasCandidates]);

  if (!hasCandidates) return null;

  const dash = [SAM_DASH[0] / scale, SAM_DASH[1] / scale];
  const offset = dashOffset / scale;

  return (
    <>
      {candidates.map((c, idx) => {
        const isActive = idx === activeIdx;
        const strokeWidth = (isActive ? 3.5 : 2.5) / scale;
        const fillRgba = hexToRgba(SAM_CANDIDATE_STROKE, isActive ? 0.18 : 0.08);
        const opacity = isActive ? 1 : 0.7;
        const common = {
          stroke: SAM_CANDIDATE_STROKE,
          strokeWidth,
          dash,
          dashOffset: offset,
          fill: fillRgba,
          opacity,
          listening: false as const,
        };

        if (tool === "magic-box" && c.points && c.points.length >= 3) {
          let minX = Infinity,
            minY = Infinity,
            maxX = -Infinity,
            maxY = -Infinity;
          for (const [x, y] of c.points) {
            if (x < minX) minX = x;
            if (y < minY) minY = y;
            if (x > maxX) maxX = x;
            if (y > maxY) maxY = y;
          }
          if (!isFinite(minX)) return null;
          return (
            <Rect
              key={c.id}
              x={minX * imgW}
              y={minY * imgH}
              width={(maxX - minX) * imgW}
              height={(maxY - minY) * imgH}
              {...common}
            />
          );
        }
        if (c.type === "rectanglelabels" && c.bbox) {
          return (
            <Rect
              key={c.id}
              x={c.bbox.x * imgW}
              y={c.bbox.y * imgH}
              width={c.bbox.width * imgW}
              height={c.bbox.height * imgH}
              {...common}
            />
          );
        }
        if (!c.points || c.points.length < 3) return null;
        const flat: number[] = [];
        for (const [x, y] of c.points) flat.push(x * imgW, y * imgH);
        return <Line key={c.id} points={flat} closed {...common} />;
      })}
    </>
  );
}

export function ImageStage({
  resourceCoordinator,
  maskCompareStore,
  rasterMaskRecords = [],
  predictionRasterMaskRecords = [],
  rasterMaskStatusById = new Map(),
  onRetryRasterMask,
  maskReadOnly = false,
  tiledMaskOverviewRecord,
  fileUrl,
  imageSource,
  onRetryImagePyramid,
  mediaKey,
  blurhash,
  imageWidth,
  imageHeight,
  tool,
  bboxCreationMode = "corner",
  activeClass,
  selectedId,
  selectedIds,
  userBoxes,
  aiBoxes,
  spacePan,
  vp,
  setVp,
  fitTick,
  readOnly = false,
  continuousCreation = false,
  fadedAiIds,
  pendingDrawing,
  nudgeMap,
  pendingGeomMap,
  onJoinSelected,
  onCropSelected,
  onCommitPolygonSlice,
  onSelectBox,
  onAcceptPrediction,
  onRejectPrediction,
  onDeleteUserBox,
  onChangeUserBoxClass,
  onPatchShapeFlag,
  clipboardActions,
  secondaryBarHidden,
  onToggleSecondaryBar,
  onCommitDrawing,
  onCommitRotatedBbox,
  onCommitRotateBbox,
  onSamPrompt,
  samCandidates,
  samMaskRecords = [],
  onSelectSamMaskCandidate,
  samActiveIdx = 0,
  samSessionPoints,
  samSessionScribbles,
  samSessionExemplars,
  onCommitMove,
  onCommitResize,
  onCommitPolygonGeometry,
  onCursorMove,
  onStageGeometry,
  overlay,
  polygonDraft,
  keypointDraft,
  keypointSchema,
  onCommitKeypointGeometry,
  samSubTool,
  samPolarity,
  canvasShapes,
  canvasEditable = false,
  canvasStroke = "#ef4444",
  onCanvasStrokeCommit,
  historicalShapes,
  maskEditor,
  onRefineSamCandidate,
  projectRenderingConfig,
  issuePixelFeedbacks,
  highlightIssueId,
  onIssuePinClick,
  issuePinDropArmed,
  onIssuePinDrop,
  annotationCommentCounts,
  onOpenAnnotationComments,
}: ImageStageProps) {
  const selSetCacheRef = useRef<{ sig: string; set: Set<string> }>({ sig: " ", set: new Set() });
  const selSet = useMemo(() => {
    const ids =
      selectedIds && selectedIds.length > 0 ? selectedIds : selectedId ? [selectedId] : [];
    if (ids.length === 0) {
      if (selSetCacheRef.current.sig !== "") {
        selSetCacheRef.current = { sig: "", set: new Set() };
      }
      return selSetCacheRef.current.set;
    }
    const sorted = [...ids].sort();
    const sig = sorted.join("");
    if (sig === selSetCacheRef.current.sig) return selSetCacheRef.current.set;
    const set = new Set(ids);
    selSetCacheRef.current = { sig, set };
    return set;
  }, [selectedIds, selectedId]);
  const selectActive = tool === "select";
  const canEditSavedGeometry = !readOnly && !continuousCreation;
  const primarySelectedBox =
    selectedId != null && selSet.size === 1
      ? userBoxes.find((b) => b.id === selectedId)
      : undefined;
  const hasEditablePrimarySelection = !!primarySelectedBox && !primarySelectedBox.is_locked;
  const userLayerListening =
    (selectActive || (!continuousCreation && hasEditablePrimarySelection)) && !readOnly;
  const { config: workbenchConfig } = useWorkbenchConfig(projectRenderingConfig);
  useWorkbenchPerf(workbenchConfig.common.longTaskSampleRate);
  const annotationVisual = useMemo(
    () => resolveAnnotationVisual(workbenchConfig.common),
    [workbenchConfig.common],
  );
  const maskEditorColor = useMemo(
    () => hexToRgb(classColorForCanvas(primarySelectedBox?.cls || activeClass || "mask")),
    [activeClass, primarySelectedBox?.cls],
  );
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<Konva.Stage>(null);
  const vpRef = useRef(vp);
  vpRef.current = vp;
  const userBoxesRef = useRef(userBoxes);
  userBoxesRef.current = userBoxes;
  const { ref: setContainerNode, size: vpSize } = useElementSize(containerRef);

  const resolvedImageSource = useMemo<WorkbenchImageSource | null>(() => {
    if (imageSource !== undefined) return imageSource;
    if (!fileUrl) return null;
    return {
      kind: "single",
      identity: mediaKey ?? fileUrl,
      url: fileUrl,
      width: imageWidth ?? undefined,
      height: imageHeight ?? undefined,
      blurhash: blurhash ?? undefined,
    };
  }, [blurhash, fileUrl, imageHeight, imageSource, imageWidth, mediaKey]);
  const imageIdentity = resolvedImageSource?.identity ?? "";
  const [pyramidRetryState, setPyramidRetryState] = useState<"idle" | "pending" | "failed">("idle");
  useEffect(() => setPyramidRetryState("idle"), [imageIdentity]);
  const retryImagePyramid = useCallback(async () => {
    if (!onRetryImagePyramid || pyramidRetryState === "pending") return;
    setPyramidRetryState("pending");
    try {
      await onRetryImagePyramid();
      setPyramidRetryState("idle");
    } catch {
      setPyramidRetryState("failed");
    }
  }, [onRetryImagePyramid, pyramidRetryState]);
  const previewUrl =
    resolvedImageSource?.kind === "single"
      ? resolvedImageSource.url
      : (resolvedImageSource?.overviewUrl ?? resolvedImageSource?.thumbnailUrl ?? null);
  const imageSourceRef = useRef<{ identity: string; url: string | null }>({
    identity: imageIdentity,
    url: previewUrl,
  });
  if (imageSourceRef.current.identity !== imageIdentity) {
    imageSourceRef.current = { identity: imageIdentity, url: previewUrl };
  } else if (!imageSourceRef.current.url && previewUrl) {
    imageSourceRef.current.url = previewUrl;
  }
  const previewSourceUrl = imageSourceRef.current.url;
  const [image, imageStatus] = useAbortableImage(previewSourceUrl ?? "");
  const overviewResourceRef = useRef<RasterResourceReservation | null>(null);
  const [overviewAdmitted, setOverviewAdmitted] = useState(() => !resourceCoordinator);
  const [overviewLifecycleEpoch, setOverviewLifecycleEpoch] = useState(0);
  useEffect(() => {
    overviewResourceRef.current?.release();
    overviewResourceRef.current = null;
    if (!resourceCoordinator) {
      setOverviewAdmitted(true);
      return;
    }
    if (!image?.naturalWidth || !image.naturalHeight) {
      setOverviewAdmitted(false);
      return;
    }
    const reservation = resourceCoordinator.tryReserve({
      owner: "background",
      category: "background-coverage",
      priority: 1,
      bytes: image.naturalWidth * image.naturalHeight * 4,
      reconstructible: true,
      pinned: true,
    });
    if (!reservation || !reservation.commit()) {
      reservation?.release();
      setOverviewAdmitted(false);
      return;
    }
    overviewResourceRef.current = reservation;
    setOverviewAdmitted(true);
    return () => {
      reservation.release();
      if (overviewResourceRef.current === reservation) overviewResourceRef.current = null;
    };
  }, [image, imageIdentity, overviewLifecycleEpoch, resourceCoordinator]);
  useEffect(() => {
    if (!resourceCoordinator || typeof window === "undefined") return;
    const onPageHide = (event: PageTransitionEvent) => {
      if (!event.persisted) return;
      overviewResourceRef.current?.release();
      overviewResourceRef.current = null;
      setOverviewAdmitted(false);
    };
    const onPageShow = (event: PageTransitionEvent) => {
      if (event.persisted) setOverviewLifecycleEpoch((value) => value + 1);
    };
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, [resourceCoordinator]);
  const displayImage = overviewAdmitted ? image : undefined;
  const sourceWidth =
    resolvedImageSource?.kind === "pyramid"
      ? resolvedImageSource.manifest.width
      : resolvedImageSource?.kind === "pyramid-pending" ||
          resolvedImageSource?.kind === "pyramid-failed"
        ? resolvedImageSource.width
        : resolvedImageSource?.width;
  const sourceHeight =
    resolvedImageSource?.kind === "pyramid"
      ? resolvedImageSource.manifest.height
      : resolvedImageSource?.kind === "pyramid-pending" ||
          resolvedImageSource?.kind === "pyramid-failed"
        ? resolvedImageSource.height
        : resolvedImageSource?.height;
  const imgW = sourceWidth || imageWidth || image?.naturalWidth || 900;
  const imgH = sourceHeight || imageHeight || image?.naturalHeight || 600;
  const imageScaleRange = useMemo(
    () => fitAwareScaleRange(vpSize.w, vpSize.h, imgW, imgH),
    [imgH, imgW, vpSize.h, vpSize.w],
  );
  const imageLoaded = !!displayImage?.naturalWidth;
  const dimsReady = !!(
    (sourceWidth && sourceHeight) ||
    (imageWidth && imageHeight) ||
    imageLoaded ||
    imageStatus === "failed"
  );
  const imageTiles = useImageTileScheduler({
    source: resolvedImageSource,
    viewport: vp,
    viewportSize: vpSize,
    resourceCoordinator,
  });
  const maskCursorNodeRef = useRef<Konva.Circle>(null);
  const maskCursorVisibleRef = useRef(false);
  const [maskCursorVisible, setMaskCursorVisible] = useState(false);
  const updateMaskCursor = useCallback(
    (point: { x: number; y: number } | null) => {
      const visible = isNormalizedImagePoint(point);
      const node = maskCursorNodeRef.current;
      if (node) {
        if (visible) node.position({ x: point.x * imgW, y: point.y * imgH });
        node.visible(visible);
        node.getLayer()?.batchDraw();
      }
      if (maskCursorVisibleRef.current !== visible) {
        maskCursorVisibleRef.current = visible;
        setMaskCursorVisible(visible);
      }
    },
    [imgH, imgW],
  );

  const visibleSortedUserBoxes = useMemo(() => {
    const visible = userBoxes.filter((b) => !b.is_hidden);
    return visible
      .map((b, i) => ({ b, i }))
      .sort((a, c) => (a.b.z_order ?? 0) - (c.b.z_order ?? 0) || a.i - c.i)
      .map((entry) => entry.b);
  }, [userBoxes]);
  const visibleUserBoxById = useMemo(
    () => new Map(visibleSortedUserBoxes.map((annotation) => [annotation.id, annotation])),
    [visibleSortedUserBoxes],
  );
  const displayedRasterMaskRecords = useMemo(
    () =>
      maskCompareStore?.display
        ? rasterMaskRecords.filter(
            (record) =>
              visibleUserBoxById.has(record.id) &&
              maskCompareCompanionVisible(maskCompareStore.display, {
                source: "annotation",
                id: record.id,
              }),
          )
        : rasterMaskRecords.filter((record) => visibleUserBoxById.has(record.id)),
    [maskCompareStore, rasterMaskRecords, visibleUserBoxById],
  );
  const displayedSamMaskRecords = maskCompareCompanionVisible(maskCompareStore?.display, {
    source: "ai",
  })
    ? samMaskRecords
    : [];
  const displayedPredictionMaskRecords = useMemo(() => {
    if (!maskCompareCompanionVisible(maskCompareStore?.display, { source: "ai" })) return [];
    const visible = new Set(aiBoxes.map((box) => box.id));
    return predictionRasterMaskRecords.filter((record) => visible.has(record.id));
  }, [aiBoxes, maskCompareStore, predictionRasterMaskRecords]);
  const maskCompareActive = !!maskCompareStore?.display;
  const rasterMaskIssues = useMemo(
    () =>
      [...rasterMaskStatusById.entries()].flatMap(([id, status]) =>
        status.state === "error" || status.state === "deferred" ? [{ id, status }] : [],
      ),
    [rasterMaskStatusById],
  );
  const snapIndex = useMemo(() => {
    const polygonAnnotations = visibleSortedUserBoxes.filter(
      (annotation): annotation is Annotation & { geometry: Geometry } =>
        !!annotation.geometry &&
        (annotation.geometry.type === "polygon" || annotation.geometry.type === "multi_polygon"),
    );
    return buildSnapIndex(polygonAnnotations);
  }, [visibleSortedUserBoxes]);
  const siblingHighlightChildBoxes = useMemo(
    () =>
      siblingHighlightChildren(visibleSortedUserBoxes, selectedId, selSet.size).filter(
        shouldRenderImageAnnotationShape,
      ),
    [visibleSortedUserBoxes, selSet, selectedId],
  );

  const viewportBBox = useMemo(() => {
    if (!imgW || !imgH || vp.scale <= 0 || !vpSize.w || !vpSize.h) return undefined;
    const bufferPx = 8;
    const sxW = vp.scale * imgW;
    const sxH = vp.scale * imgH;
    return {
      minX: Math.max(0, (-vp.tx - bufferPx) / sxW),
      minY: Math.max(0, (-vp.ty - bufferPx) / sxH),
      maxX: Math.min(1, (vpSize.w - vp.tx + bufferPx) / sxW),
      maxY: Math.min(1, (vpSize.h - vp.ty + bufferPx) / sxH),
    };
  }, [imgW, imgH, vp.scale, vp.tx, vp.ty, vpSize.w, vpSize.h]);
  const setMaskViewport = maskEditor?.setViewport;
  const maskEditorBackend = maskEditor?.backend;
  useEffect(() => {
    if (!setMaskViewport || maskEditorBackend !== "tiled" || !viewportBBox || !imgW || !imgH) {
      setMaskViewport?.(null);
      return;
    }
    setMaskViewport({
      x: viewportBBox.minX * imgW,
      y: viewportBBox.minY * imgH,
      width: (viewportBBox.maxX - viewportBBox.minX) * imgW,
      height: (viewportBBox.maxY - viewportBBox.minY) * imgH,
    });
  }, [imgH, imgW, maskEditorBackend, setMaskViewport, viewportBBox]);

  useEffect(() => {
    onStageGeometry?.({ imgW, imgH, vpSize });
  }, [imgW, imgH, vpSize, onStageGeometry]);

  const [drag, setDragState] = useState<Drag | null>(null);
  const contextMenu = useCanvasContextMenu();
  const [contextMenuTargetId, setContextMenuTargetId] = useState<string | null>(null);
  const rightDownRef = useRef<{ x: number; y: number } | null>(null);
  const dragRef = useRef<Drag | null>(null);
  const setDrag = useCallback((update: SetStateAction<Drag | null>) => {
    const next = typeof update === "function" ? update(dragRef.current) : update;
    dragRef.current = next;
    setDragState(next);
  }, []);
  const { schedule, flush, cancel } = useRafThrottle();

  useEffect(() => {
    const cancelDrawing = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || isWorkbenchInteractionBlocked(event)) return;
      if (dragRef.current?.kind !== "draw" && dragRef.current?.kind !== "maskSlice") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      dragRef.current = null;
      setDrag(null);
    };
    window.addEventListener("keydown", cancelDrawing, true);
    return () => window.removeEventListener("keydown", cancelDrawing, true);
  }, [setDrag]);

  useLayoutEffect(() => {
    if (dragRef.current?.kind !== "maskSlice") return;
    dragRef.current = null;
    setDrag(null);
  }, [
    imageIdentity,
    primarySelectedBox?.id,
    primarySelectedBox?.version,
    setDrag,
    maskEditor?.buffer,
    maskEditor?.tool,
    tool,
    readOnly,
  ]);

  const closeContextMenu = useCallback(() => {
    contextMenu.close();
    setContextMenuTargetId(null);
  }, [contextMenu]);

  const toImg = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect || !imgW || !imgH) return null;
      return normalizeImageCoordinate(clientX, clientY, rect, vpRef.current, imgW, imgH);
    },
    [imgW, imgH],
  );

  const findSnapMatch = useCallback(
    (
      pt: { x: number; y: number },
      evt: Pick<MouseEvent | PointerEvent, "altKey">,
      excludeAnnotationId?: string,
    ): SnapMatch | null => {
      if (evt.altKey || !imgW || !imgH) return null;
      const transform = { imgW, imgH, scale: vpRef.current.scale };
      const point: Pt = [pt.x, pt.y];
      const points = excludeAnnotationId
        ? snapIndex.points.filter((candidate) => candidate.annotationId !== excludeAnnotationId)
        : snapIndex.points;
      const segments = excludeAnnotationId
        ? snapIndex.segments.filter((candidate) => candidate.annotationId !== excludeAnnotationId)
        : snapIndex.segments;
      return resolveSnapMatch(
        point,
        { points, segments },
        workbenchConfig.image.snapThresholdPx,
        transform,
      );
    },
    [imgW, imgH, snapIndex, workbenchConfig.image.snapThresholdPx],
  );

  const snapImagePoint = useCallback(
    (
      pt: { x: number; y: number },
      evt: Pick<MouseEvent | PointerEvent, "altKey">,
      excludeAnnotationId?: string,
    ) => {
      const match = findSnapMatch(pt, evt, excludeAnnotationId);
      if (!match) return { point: pt, match: null };
      return { point: { x: match.point[0], y: match.point[1] }, match };
    },
    [findSnapMatch],
  );

  const { fitted, fitNow } = useImageStageFit({
    imageIdentity,
    imageWidth: imgW,
    imageHeight: imgH,
    dimsReady,
    viewportSize: vpSize,
    setViewport: setVp,
    fitTick,
    autoFitOnResize: workbenchConfig.image.autoFitOnResize,
  });
  const imageReady = imageStatus === "loaded" && imageLoaded && fitted;
  const sliceEnabled =
    tool === "select" && imageReady && !readOnly && !pendingDrawing && !maskReadOnly;
  const polygonSlice = usePolygonSlice({
    enabled: sliceEnabled,
    owner: imageIdentity,
    annotations: userBoxes,
    commit: onCommitPolygonSlice,
  });
  const polygonSliceActive = Boolean(polygonSlice.session);
  useLayoutEffect(() => {
    stageRef.current?.getLayers().forEach((layer) => layer.drawHit());
  }, [polygonSliceActive]);
  const boundaryTrace = usePolygonBoundaryTrace({
    enabled: tool === "polygon" && imageReady && !readOnly && !pendingDrawing,
    owner: imageIdentity,
    annotations: userBoxes,
    width: imgW * vp.scale,
    height: imgH * vp.scale,
    draft: polygonDraft,
  });
  const startPolygonAutoPoints = usePolygonAutoPoints({
    enabled:
      tool === "polygon" &&
      imageReady &&
      !readOnly &&
      !pendingDrawing &&
      !spacePan &&
      !boundaryTrace.trace,
    owner: imageIdentity,
    view: `${vp.scale}:${vp.tx}:${vp.ty}`,
    width: imgW * vp.scale,
    height: imgH * vp.scale,
    draft: polygonDraft,
    toImage: toImg,
    snap: snapImagePoint,
  });

  useLayoutEffect(() => {
    if (fitted) stageRef.current?.draw();
  }, [fitted]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const imagePoint = toImg(e.clientX, e.clientY);
      if (
        !maskCompareActive &&
        !(e.ctrlKey || e.metaKey) &&
        tool === "mask" &&
        maskEditor &&
        isNormalizedImagePoint(imagePoint) &&
        Math.abs(e.deltaY) > Math.abs(e.deltaX)
      ) {
        e.preventDefault();
        const delta = e.deltaY < 0 ? 2 : -2;
        const next = Math.max(
          MASK_BRUSH_MIN_PX,
          Math.min(MASK_BRUSH_MAX_PX, maskEditor.radius + delta),
        );
        maskEditor.setRadius(next);
        return;
      }
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const factor = wheelZoomFactor(e.deltaY, workbenchConfig.image.zoomStepFactor);
      const cur = vpRef.current;
      setVp(zoomAtPoint(cur, cx, cy, cur.scale * factor, imageScaleRange));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [
    imageScaleRange,
    maskCompareActive,
    setVp,
    maskEditor,
    toImg,
    tool,
    workbenchConfig.image.zoomStepFactor,
  ]);

  const dragging = !!drag;
  useEffect(() => {
    if (!dragging) cancel();
  }, [cancel, dragging]);
  const dragOwnerRef = useRef({ imageIdentity, readOnly, tool });
  useLayoutEffect(() => {
    const previous = dragOwnerRef.current;
    if (
      previous.imageIdentity === imageIdentity &&
      previous.readOnly === readOnly &&
      previous.tool === tool
    ) {
      return;
    }
    dragOwnerRef.current = { imageIdentity, readOnly, tool };
    cancel();
    if (dragRef.current?.kind === "maskBrush") maskEditor?.endStroke();
    setDrag(null);
    setSnapIndicator(null);
  }, [cancel, imageIdentity, maskEditor, readOnly, setDrag, tool]);
  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: PointerEvent | MouseEvent) => {
      const d = dragRef.current;
      if (!d) return;
      if (d.kind === "pan") {
        const dx = e.movementX;
        const dy = e.movementY;
        schedule(() => setVp((cur) => ({ ...cur, tx: cur.tx + dx, ty: cur.ty + dy })));
        return;
      }
      const pt =
        d.kind === "maskSlice"
          ? toImg(Math.floor(e.clientX), Math.floor(e.clientY))
          : toImg(e.clientX, e.clientY);
      if (!pt) return;
      if (d.kind === "maskBrush" || d.kind === "maskLasso") {
        const inImage = isNormalizedImagePoint(pt);
        if (d.kind === "maskBrush") updateMaskCursor(inImage ? pt : null);
        if (!inImage) {
          if (d.kind === "maskBrush") {
            d.lastX = Number.NaN;
            d.lastY = Number.NaN;
          }
          return;
        }
      }
      if (d.kind === "draw") {
        schedule(() =>
          setDrag((cur) => (cur && cur.kind === "draw" ? { ...cur, cx: pt.x, cy: pt.y } : cur)),
        );
      } else if (d.kind === "samProbe") {
        schedule(() =>
          setDrag((cur) => (cur && cur.kind === "samProbe" ? { ...cur, cx: pt.x, cy: pt.y } : cur)),
        );
      } else if (d.kind === "samScribble") {
        const last = d.points[d.points.length - 1];
        const nextPoint: [number, number] = [
          Math.max(0, Math.min(1, pt.x)),
          Math.max(0, Math.min(1, pt.y)),
        ];
        if (last && Math.hypot(nextPoint[0] - last[0], nextPoint[1] - last[1]) >= 0.002) {
          if (d.points.length >= 8192) return;
          const nextDrag: Drag = { ...d, points: [...d.points, nextPoint] };
          dragRef.current = nextDrag;
          schedule(() => {
            const latest = dragRef.current;
            setDrag(latest?.kind === "samScribble" ? latest : null);
          });
        }
      } else if (d.kind === "move") {
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "move") return cur;
            const dx = pt.x - cur.sx;
            const dy = pt.y - cur.sy;
            return {
              ...cur,
              cur: {
                ...cur.start,
                x: Math.max(0, Math.min(1 - cur.start.w, cur.start.x + dx)),
                y: Math.max(0, Math.min(1 - cur.start.h, cur.start.y + dy)),
              },
            };
          }),
        );
      } else if (d.kind === "resize") {
        const shiftKey = e.shiftKey;
        const altKey = e.altKey;
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "resize") return cur;
            const next = applyResize(
              { ...cur.start, id: "", cls: "", conf: 1, source: "manual" } as Annotation,
              { x: cur.sx, y: cur.sy },
              pt,
              cur.dir,
              { shiftKey, altKey },
            );
            return { ...cur, cur: next };
          }),
        );
      } else if (d.kind === "moveRotatedBox") {
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "moveRotatedBox") return cur;
            return {
              ...cur,
              cur: {
                ...cur.start,
                cx: clamp01(cur.start.cx + pt.x - cur.sx),
                cy: clamp01(cur.start.cy + pt.y - cur.sy),
              },
            };
          }),
        );
      } else if (d.kind === "resizeRotatedBox") {
        const shiftKey = e.shiftKey;
        const altKey = e.altKey;
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "resizeRotatedBox") return cur;
            const next = applyRotatedResize(
              cur.start,
              { x: cur.sx, y: cur.sy },
              pt,
              cur.dir,
              { w: imgW, h: imgH },
              { shiftKey, altKey },
            );
            return { ...cur, cur: next };
          }),
        );
      } else if (d.kind === "polyVertex") {
        const snapped = snapImagePoint(pt, e, d.id);
        setSnapIndicator(snapped.match ? snapped.point : null);
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "polyVertex") return cur;
            return {
              ...cur,
              cur: moveVertex(cur.cur, cur.vidx, [snapped.point.x, snapped.point.y]),
            };
          }),
        );
      } else if (d.kind === "polyMove") {
        setSnapIndicator(null);
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "polyMove") return cur;
            return { ...cur, cur: translatePolygon(cur.start, pt.x - cur.sx, pt.y - cur.sy) };
          }),
        );
      } else if (d.kind === "kpNode") {
        const cx = Math.max(0, Math.min(1, pt.x));
        const cy = Math.max(0, Math.min(1, pt.y));
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "kpNode") return cur;
            const next = cur.cur.map((kp, i) => (i === cur.nidx ? { ...kp, x: cx, y: cy } : kp));
            return { ...cur, cur: next };
          }),
        );
      } else if (d.kind === "canvasStroke") {
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "canvasStroke") return cur;
            return { ...cur, points: [...cur.points, pt.x, pt.y] };
          }),
        );
      } else if (d.kind === "maskBrush") {
        if (!maskEditor || maskCompareActive) return;
        const phase =
          maskEditor.phase ?? (maskEditor.dirty ? "dirty" : maskEditor.active ? "ready" : "idle");
        if (
          !canEditMask({
            taskReadOnly: readOnly,
            annotationLocked: !!primarySelectedBox?.is_locked,
            trackLocked: false,
            segmentLocked: false,
            editorPhase: phase,
          })
        )
          return;
        const px = pt.x * imgW;
        const py = pt.y * imgH;
        if (!Number.isFinite(d.lastX) || !Number.isFinite(d.lastY)) {
          maskEditor.paintAt(px, py);
          d.lastX = px;
          d.lastY = py;
          return;
        }
        const dx = px - d.lastX;
        const dy = py - d.lastY;
        const dist = Math.hypot(dx, dy);
        const step = Math.max(1, maskEditor.radius / 2);
        const n = Math.max(1, Math.floor(dist / step));
        for (let i = 1; i <= n; i++) {
          const t = i / n;
          maskEditor.paintAt(d.lastX + dx * t, d.lastY + dy * t);
        }
        d.lastX = px;
        d.lastY = py;
      } else if (d.kind === "maskSlice") {
        if (maskCompareActive) return;
        d.end = [Math.max(0, Math.min(1, pt.x)), Math.max(0, Math.min(1, pt.y))];
        setDrag({ ...d });
      } else if (d.kind === "maskLasso") {
        if (maskCompareActive) return;
        const px = pt.x * imgW;
        const py = pt.y * imgH;
        const previous = d.points[d.points.length - 1];
        if (!previous || Math.hypot(px - previous[0], py - previous[1]) >= 1) {
          d.points.push([px, py]);
          setDrag({ ...d, points: [...d.points] });
        }
      } else if (d.kind === "rotateBox") {
        schedule(() =>
          setDrag((cur) => {
            if (!cur || cur.kind !== "rotateBox") return cur;
            const dx = (pt.x - cur.cx) * imgW;
            const dy = (pt.y - cur.cy) * imgH;
            let deg = (Math.atan2(dx, -dy) * 180) / Math.PI;
            deg = ((deg % 360) + 360) % 360;
            return { ...cur, cur: deg };
          }),
        );
      }
    };
    const onUp = (event: PointerEvent | MouseEvent) => {
      flush();
      const d = dragRef.current;
      if (d) {
        if (d.kind === "draw") {
          const end = toImg(event.clientX, event.clientY);
          const { x, y, w, h } = imageBoxFromDrag(end ? { ...d, cx: end.x, cy: end.y } : d);
          if (Math.max(w * imgW, h * imgH) >= 3) {
            if (tool === "rotated-box") onCommitRotatedBbox?.({ x, y, w, h });
            else onCommitDrawing?.({ x, y, w, h });
          }
        } else if (d.kind === "rotateBox") {
          const target = userBoxes.find((bx) => bx.id === d.id);
          const g = target?.geometry;
          if (g && g.type === "rotated_bbox" && d.cur !== d.startAngle) {
            onCommitRotateBbox?.(d.id, g, { ...g, angle: d.cur });
          }
        } else if (d.kind === "samProbe") {
          if (d.mode === "point") {
            onSamPrompt?.({ kind: "point", pt: [d.sx, d.sy], alt: d.alt });
          } else {
            const x1 = Math.min(d.sx, d.cx);
            const y1 = Math.min(d.sy, d.cy);
            const x2 = Math.max(d.sx, d.cx);
            const y2 = Math.max(d.sy, d.cy);
            if (x2 - x1 > 0.005 && y2 - y1 > 0.005) {
              if (d.mode === "exemplar") {
                onSamPrompt?.({ kind: "exemplar", bbox: [x1, y1, x2, y2], alt: d.alt });
              } else {
                onSamPrompt?.({ kind: "bbox", bbox: [x1, y1, x2, y2] });
              }
            }
          }
        } else if (d.kind === "samScribble") {
          if (d.points.length >= 2) {
            onSamPrompt?.({
              kind: "scribble",
              points: d.points,
              alt: d.alt,
              width: 0.008,
            });
          }
        } else if (d.kind === "move") {
          if (d.cur.x !== d.start.x || d.cur.y !== d.start.y) {
            let childMoves: { id: string; before: Geometry; after: Geometry }[] | undefined;
            if (d.alt) {
              const dx = d.cur.x - d.start.x;
              const dy = d.cur.y - d.start.y;
              childMoves = userBoxesRef.current
                .filter((c) => c.parent_annotation_id === d.id && c.geometry)
                .filter(canTranslateAnnotationGeometry)
                .map((c) => ({
                  id: c.id,
                  before: c.geometry as Geometry,
                  after: translateGeometry(c, dx, dy).geometry,
                }));
              if (childMoves.length === 0) childMoves = undefined;
            }
            onCommitMove?.(d.id, d.start, d.cur, childMoves);
          }
        } else if (d.kind === "resize") {
          if (
            d.cur.w > 0.005 &&
            d.cur.h > 0.005 &&
            (d.cur.x !== d.start.x ||
              d.cur.y !== d.start.y ||
              d.cur.w !== d.start.w ||
              d.cur.h !== d.start.h)
          ) {
            onCommitResize?.(d.id, d.start, d.cur);
          }
        } else if (d.kind === "moveRotatedBox") {
          if (rotatedBboxChanged(d.start, d.cur)) {
            onCommitRotateBbox?.(d.id, d.start, d.cur);
          }
        } else if (d.kind === "resizeRotatedBox") {
          if (d.cur.w > 0.005 && d.cur.h > 0.005 && rotatedBboxChanged(d.start, d.cur)) {
            onCommitRotateBbox?.(d.id, d.start, d.cur);
          }
        } else if (d.kind === "polyVertex" || d.kind === "polyMove") {
          const before = d.start;
          const after = d.cur;
          const changed =
            before.length !== after.length ||
            before.some((p, i) => p[0] !== after[i][0] || p[1] !== after[i][1]);
          if (changed) onCommitPolygonGeometry?.(d.id, before, after);
        } else if (d.kind === "kpNode") {
          const before = d.start;
          const after = d.cur;
          const moved = before.some((p, i) => p.x !== after[i].x || p.y !== after[i].y);
          if (moved) onCommitKeypointGeometry?.(d.id, before, after);
        } else if (d.kind === "canvasStroke") {
          if (d.points.length >= 4) onCanvasStrokeCommit?.(d.points, canvasStroke);
        } else if (
          d.kind === "maskSlice" &&
          maskEditor &&
          !maskCompareActive &&
          !readOnly &&
          !primarySelectedBox?.is_locked
        ) {
          const end = toImg(Math.floor(event.clientX), Math.floor(event.clientY));
          const finalPoint: [number, number] = end
            ? [Math.max(0, Math.min(1, end.x)), Math.max(0, Math.min(1, end.y))]
            : d.end;
          void maskEditor.runInstanceOperation("slice_mask", {
            type: "slice_mask",
            cutPath: [d.start, finalPoint],
          });
        } else if (
          d.kind === "maskLasso" &&
          d.points.length >= 3 &&
          maskEditor &&
          !maskCompareActive
        ) {
          void maskEditor.runOperation(maskEditor.tool, {
            type: "polygon",
            points: d.points,
            value: maskEditor.tool === "lasso_subtract" ? 0 : 255,
          });
        }
        if (d.kind === "maskBrush") {
          maskEditor?.endStroke();
        }
      }
      dragRef.current = null;
      setDrag(null);
      setSnapIndicator(null);
    };
    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("mousemove", onMove, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("mouseup", onUp, true);
    return () => {
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("mousemove", onMove, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("mouseup", onUp, true);
    };
  }, [
    dragging,
    flush,
    schedule,
    setDrag,
    setVp,
    toImg,
    onCommitDrawing,
    onCommitRotatedBbox,
    onCommitRotateBbox,
    tool,
    userBoxes,
    onCommitMove,
    onCommitResize,
    onCommitPolygonGeometry,
    onCommitKeypointGeometry,
    onCanvasStrokeCommit,
    canvasStroke,
    onSamPrompt,
    maskEditor,
    maskCompareActive,
    imgW,
    imgH,
    snapImagePoint,
    readOnly,
    primarySelectedBox?.is_locked,
    updateMaskCursor,
  ]);

  useEffect(() => {
    if (!maskCompareActive) return;
    const currentDrag = dragRef.current;
    if (currentDrag?.kind === "maskBrush") maskEditor?.endStroke();
    if (
      currentDrag?.kind === "maskBrush" ||
      currentDrag?.kind === "maskLasso" ||
      currentDrag?.kind === "maskSlice"
    ) {
      dragRef.current = null;
      setDrag(null);
    }
    updateMaskCursor(null);
  }, [maskCompareActive, maskEditor, setDrag, updateMaskCursor]);

  const customMaskCursorActive =
    tool === "mask" &&
    !maskCompareActive &&
    !!maskEditor &&
    (maskEditor.tool === "brush" || maskEditor.tool === "erase");

  useEffect(() => {
    if (!customMaskCursorActive) updateMaskCursor(null);
  }, [customMaskCursorActive, updateMaskCursor]);

  const polygonClickPair = useRef<{ previousPlain: Pt | null; allowDoubleClick: boolean }>({
    previousPlain: null,
    allowDoubleClick: false,
  });
  useEffect(() => {
    polygonClickPair.current = { previousPlain: null, allowDoubleClick: false };
  }, [tool, imageIdentity]);
  const handleStageMouseDown = (e: Konva.KonvaEventObject<MouseEvent>) => {
    if (e.target !== (stageRef.current as unknown)) {
      return;
    }
    const pt =
      tool === "mask" && maskEditor?.tool === "slice_mask"
        ? toImg(Math.floor(e.evt.clientX), Math.floor(e.evt.clientY))
        : toImg(e.evt.clientX, e.evt.clientY);
    if (!pt) return;
    if (polygonSlice.session && !spacePan && e.evt.button === 0) {
      containerRef.current?.focus({ preventScroll: true });
      const hit = !e.evt.altKey
        ? pickBoundary(
            polygonSlice.session.source.geometry.points,
            [pt.x, pt.y],
            imgW * vp.scale,
            imgH * vp.scale,
          )
        : null;
      polygonSlice.addPoint(hit?.point ?? [pt.x, pt.y]);
      return;
    }
    if (boundaryTrace.trace && !spacePan && e.evt.button === 0) {
      polygonClickPair.current = { previousPlain: null, allowDoubleClick: false };
      boundaryTrace.pick([pt.x, pt.y]);
      return;
    }
    if (tool === "polygon") {
      const plain = !e.evt.shiftKey && !spacePan && e.evt.button === 0;
      const previous = polygonClickPair.current.previousPlain;
      polygonClickPair.current = {
        previousPlain: plain ? [e.evt.clientX, e.evt.clientY] : null,
        allowDoubleClick:
          plain &&
          !!previous &&
          Math.hypot(previous[0] - e.evt.clientX, previous[1] - e.evt.clientY) <= 4,
      };
    }
    if (tool === "mask" && !spacePan && !isNormalizedImagePoint(pt)) return;
    if ((e.evt.ctrlKey || e.evt.metaKey) && samMaskRecords.length > 0) {
      const candidate = pickTopRasterMaskAt(samMaskRecords, pt);
      if (candidate) {
        onSelectSamMaskCandidate?.(candidate.id);
        return;
      }
    }
    if (tool === "select" && !spacePan) {
      const hit =
        pickTopRasterMaskAt(displayedRasterMaskRecords, pt) ??
        pickTopRasterMaskAt(displayedPredictionMaskRecords, pt);
      if (hit) {
        onSelectBox(hit.id, { shift: e.evt.shiftKey });
        return;
      }
    }
    const effective = spacePan ? TOOL_REGISTRY.hand : TOOL_REGISTRY[tool];
    const init = effective.onPointerDown?.({
      pt,
      evt: e.evt,
      vp,
      activeClass,
      imgW,
      imgH,
      spacePan,
      readOnly: readOnly || (tool === "mask" && (maskReadOnly || maskCompareActive)),
      pendingDrawing: !!pendingDrawing,
      bboxCreationMode,
      onClearSelection: () => onSelectBox(null),
      preserveSelectionForPrompt:
        primarySelectedBox?.geometry?.type === "raster_mask" &&
        (tool === "smart-point" || tool === "smart-box" || tool === "smart-scribble"),
      snapPoint: (candidate, evt) => {
        const snapped = snapImagePoint(candidate, evt);
        setSnapIndicator(snapped.match ? snapped.point : null);
        return snapped.point;
      },
      polygonDraft,
      startPolygonAutoPoints,
      keypointDraft,
      samPolarity,
      maskEditor,
      annotationLocked: !!primarySelectedBox?.is_locked || maskReadOnly,
    });
    if (init) setDrag(init);
  };

  const handleStageDblClick = () => {
    if (tool === "mask" && maskEditor?.tool === "slice_mask") return;
    if (polygonSlice.session) return;
    if (boundaryTrace.trace) return;
    if (tool === "polygon" && !polygonClickPair.current.allowDoubleClick) return;
    if (tool === "polygon" && polygonDraft && polygonDraft.points.length >= 3) {
      polygonDraft.close();
      return;
    }
    if (tool === "polyline" && polygonDraft && polygonDraft.points.length >= 2) {
      polygonDraft.close();
      return;
    }
    fitNow();
  };

  const handleStageMouseMove = (e: Konva.KonvaEventObject<MouseEvent>) => {
    const pt = toImg(e.evt.clientX, e.evt.clientY);
    const inImagePoint = isNormalizedImagePoint(pt) ? pt : null;
    onCursorMove(inImagePoint);
    if (
      (tool === "polygon" || tool === "polyline") &&
      polygonDraft &&
      polygonDraft.points.length > 0
    ) {
      const snapped = pt ? snapImagePoint(pt, e.evt) : { point: null, match: null };
      setPolygonCursor(snapped.point);
      setSnapIndicator(snapped.match ? snapped.point : null);
    } else if (tool === "keypoint" && keypointDraft && keypointDraft.nodeCount > 0) {
      setPolygonCursor(pt);
      setSnapIndicator(null);
    } else if (polygonCursor) {
      setPolygonCursor(null);
      setSnapIndicator(null);
    } else if (snapIndicator) {
      setSnapIndicator(null);
    }
    if (customMaskCursorActive) {
      updateMaskCursor(inImagePoint);
    } else if (maskCursorVisibleRef.current) {
      updateMaskCursor(null);
    }
  };

  const containerCursor =
    drag?.kind === "pan"
      ? "grabbing"
      : tool === "hand" || spacePan
        ? "grab"
        : tool === "select"
          ? "default"
          : tool === "canvas"
            ? "crosshair"
            : tool === "mask"
              ? customMaskCursorActive
                ? maskCursorVisible
                  ? "none"
                  : "default"
                : "crosshair"
              : pendingDrawing
                ? "default"
                : "crosshair";

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    el.style.setProperty("--image-stage-cursor", containerCursor);
    if (workbenchConfig.image.cssImageFilter) {
      el.style.setProperty("--image-stage-filter", workbenchConfig.image.cssImageFilter);
    } else {
      el.style.removeProperty("--image-stage-filter");
    }
  }, [containerCursor, workbenchConfig.image.cssImageFilter]);

  const [polygonCursor, setPolygonCursor] = useState<{ x: number; y: number } | null>(null);
  const [snapIndicator, setSnapIndicator] = useState<{ x: number; y: number } | null>(null);
  const maskOperationPreviewBuffer = useMemo(() => {
    const preview = maskEditor?.operationPreview;
    if (!preview || preview.alpha.length !== imgW * imgH) return null;
    const buffer = new MaskBuffer({ width: imgW, height: imgH });
    buffer.replaceAlpha(preview.alpha);
    return buffer;
  }, [imgH, imgW, maskEditor?.operationPreview]);
  const maskInstancePreviewBuffer = useMemo(() => {
    const preview = maskEditor?.instanceOperationPreview;
    if (!preview || preview.plan.focusAlpha.length !== imgW * imgH) return null;
    const buffer = new MaskBuffer({ width: imgW, height: imgH });
    buffer.replaceAlpha(preview.plan.focusAlpha);
    return buffer;
  }, [imgH, imgW, maskEditor?.instanceOperationPreview]);

  const drawingPreview = drag?.kind === "draw" ? imageBoxFromDrag(drag) : null;

  const samPreview =
    drag?.kind === "samProbe"
      ? {
          x: Math.min(drag.sx, drag.cx),
          y: Math.min(drag.sy, drag.cy),
          w: Math.abs(drag.cx - drag.sx),
          h: Math.abs(drag.cy - drag.sy),
        }
      : null;

  const overrideGeom = (id: string): Geom | null => {
    if (drag && (drag.kind === "move" || drag.kind === "resize") && drag.id === id) return drag.cur;
    if (nudgeMap?.has(id)) return nudgeMap.get(id) ?? null;
    const pg = pendingGeomMap?.get(id);
    if (pg && pg.type === "bbox") return { x: pg.x, y: pg.y, w: pg.w, h: pg.h };
    return null;
  };

  const polyOverridePoints = (id: string): Pt[] | null => {
    if (drag && (drag.kind === "polyVertex" || drag.kind === "polyMove") && drag.id === id)
      return drag.cur;
    const pg = pendingGeomMap?.get(id);
    if (pg && (pg.type === "polygon" || pg.type === "polyline")) return pg.points;
    return null;
  };

  const kpOverridePoints = (id: string): Keypoint[] | null => {
    if (drag && drag.kind === "kpNode" && drag.id === id) return drag.cur;
    const pg = pendingGeomMap?.get(id);
    if (pg && pg.type === "keypoint") return pg.points;
    return null;
  };

  const rotatedOverride = (id: string): RotatedBboxGeometry | null => {
    const pg = pendingGeomMap?.get(id);
    if (pg && pg.type === "rotated_bbox") return pg;
    return null;
  };

  const commentBadgeAnnotations =
    fitted && workbenchConfig.common.showAnnotationComments && annotationCommentCounts
      ? visibleSortedUserBoxes.flatMap((annotation) => {
          const rawCount = annotationCommentCounts[annotation.id];
          if (!Number.isFinite(rawCount) || Math.trunc(rawCount) <= 0) return [];
          let geometry = annotation.geometry;
          const boxOverride = overrideGeom(annotation.id);
          if (boxOverride && (!geometry || geometry.type === "bbox")) {
            geometry = { type: "bbox", ...boxOverride };
          } else if (geometry?.type === "polygon" || geometry?.type === "polyline") {
            const points = polyOverridePoints(annotation.id);
            if (points) geometry = { ...geometry, points };
          } else if (geometry?.type === "keypoint") {
            const points = kpOverridePoints(annotation.id);
            if (points) geometry = { ...geometry, points };
          } else if (geometry?.type === "rotated_bbox") {
            let liveGeometry = rotatedOverride(annotation.id) ?? geometry;
            if (
              drag &&
              (drag.kind === "moveRotatedBox" || drag.kind === "resizeRotatedBox") &&
              drag.id === annotation.id
            ) {
              liveGeometry = drag.cur;
            } else if (drag?.kind === "rotateBox" && drag.id === annotation.id) {
              liveGeometry = { ...liveGeometry, angle: drag.cur };
            }
            geometry = liveGeometry;
          }
          return [{ ...annotation, geometry }];
        })
      : [];

  const handleUserShapeClick = useCallback(
    (id: string, evt?: Konva.KonvaEventObject<MouseEvent>) => {
      const shift = !!evt?.evt?.shiftKey;
      onSelectBox(id, { shift });
    },
    [onSelectBox],
  );

  const selectedBox = useMemo(() => {
    if (!selectedId) return null;
    return (
      (userBoxes as (Annotation | AiBox)[]).concat(aiBoxes).find((b) => b.id === selectedId) ?? null
    );
  }, [selectedId, userBoxes, aiBoxes]);
  const contextMenuTarget = useMemo(
    () => userBoxes.find((annotation) => annotation.id === contextMenuTargetId) ?? null,
    [contextMenuTargetId, userBoxes],
  );
  const contextMenuSelectedAnnotations = useMemo(() => {
    if (!contextMenuTarget) return [];
    if (!selSet.has(contextMenuTarget.id)) return [contextMenuTarget];
    return userBoxes.filter((annotation) => selSet.has(annotation.id));
  }, [contextMenuTarget, selSet, userBoxes]);
  const keypointDraftPending =
    tool === "keypoint" &&
    Boolean(
      keypointDraft &&
      keypointDraft.nodeCount > 0 &&
      keypointDraft.points.length < keypointDraft.nodeCount,
    );
  const contextMenuItems = useMemo(() => {
    if (!contextMenuTarget) return [];
    const zOrders = userBoxes.map((annotation) => annotation.z_order ?? 0);
    const ownZ = contextMenuTarget.z_order ?? 0;
    return buildImageContextMenuItems({
      annotation: contextMenuTarget,
      readOnly,
      minZOrder: zOrders.length > 0 ? Math.min(...zOrders, ownZ) : ownZ,
      maxZOrder: zOrders.length > 0 ? Math.max(...zOrders, ownZ) : ownZ,
      clipboard: clipboardActions ?? null,
      selectedAnnotations: contextMenuSelectedAnnotations,
      onChangeClass: onChangeUserBoxClass,
      onJoinSelected,
      onCropSelected,
      onSlicePolygon: onCommitPolygonSlice
        ? (annotation) => {
            polygonSlice.begin(annotation);
            onSelectBox(null);
            requestAnimationFrame(() => containerRef.current?.focus({ preventScroll: true }));
          }
        : undefined,
      sliceDisabledReason: !sliceEnabled
        ? "请切换到选择工具并结束当前编辑"
        : polygonSliceUnavailableReason(contextMenuTarget, userBoxes),
      onDelete: onDeleteUserBox,
      onPatchFlag: onPatchShapeFlag,
      secondaryBarHidden,
      onToggleSecondaryBar,
    });
  }, [
    clipboardActions,
    contextMenuTarget,
    contextMenuSelectedAnnotations,
    onChangeUserBoxClass,
    onJoinSelected,
    onCropSelected,
    onCommitPolygonSlice,
    onSelectBox,
    polygonSlice,
    sliceEnabled,
    onDeleteUserBox,
    onPatchShapeFlag,
    readOnly,
    userBoxes,
    secondaryBarHidden,
    onToggleSecondaryBar,
  ]);

  const isSelectedAi = selectedBox ? "predictionId" in selectedBox : false;

  const pendingColor = classColorForCanvas(activeClass || "pending");
  const handleContextMenu = useCallback(
    (evt: ReactMouseEvent<HTMLDivElement>) => {
      evt.preventDefault();
      const down = rightDownRef.current;
      rightDownRef.current = null;
      if (
        shouldSuppressImageContextMenu({
          readOnly,
          keypointDraftPending,
          down,
          point: { x: evt.clientX, y: evt.clientY },
        })
      ) {
        closeContextMenu();
        return;
      }
      const stage = stageRef.current;
      if (!stage) {
        closeContextMenu();
        return;
      }
      const rect = stage.container().getBoundingClientRect();
      const hitNode = stage.getIntersection({
        x: evt.clientX - rect.left,
        y: evt.clientY - rect.top,
      });
      const hitId = findContextMenuAnnotationId(hitNode);
      const imagePoint = toImg(evt.clientX, evt.clientY);
      const rasterHit = imagePoint
        ? pickTopRasterMaskAt(displayedRasterMaskRecords, imagePoint)
        : null;
      const targetId = hitId ?? rasterHit?.id ?? null;
      const target = targetId
        ? (userBoxes.find((annotation) => annotation.id === targetId) ?? null)
        : null;
      if (!target) {
        closeContextMenu();
        return;
      }
      setContextMenuTargetId(target.id);
      if (!selSet.has(target.id)) onSelectBox(target.id);
      contextMenu.openAt(evt.clientX, evt.clientY);
    },
    [
      closeContextMenu,
      contextMenu,
      displayedRasterMaskRecords,
      keypointDraftPending,
      onSelectBox,
      readOnly,
      selSet,
      toImg,
      userBoxes,
    ],
  );

  return (
    <div
      ref={setContainerNode}
      data-testid="workbench-stage"
      tabIndex={polygonSlice.session ? 0 : undefined}
      onKeyDownCapture={(event) => polygonSlice.keyDown(event.nativeEvent)}
      data-image-identity={imageIdentity}
      data-image-ready={imageReady ? "true" : "false"}
      data-image-decoded-width={displayImage?.naturalWidth}
      data-image-decoded-height={displayImage?.naturalHeight}
      data-image-logical-width={imageReady ? imgW : undefined}
      data-image-logical-height={imageReady ? imgH : undefined}
      data-image-tile-retrying={
        (imageTiles.snapshot?.retryingVisibleTiles ?? 0) > 0 ? "true" : "false"
      }
      data-media-x={vp.tx}
      data-media-y={vp.ty}
      data-media-width={imgW * vp.scale}
      data-media-height={imgH * vp.scale}
      data-active-class={activeClass}
      data-user-box-count={userBoxes.length}
      data-selected-box-count={selSet.size}
      data-ai-box-count={aiBoxes.length}
      data-sam-candidate-count={samCandidates?.length ?? 0}
      data-pending-drawing={pendingDrawing ? "true" : "false"}
      data-polygon-draft-count={polygonDraft?.points.length ?? 0}
      data-drag-kind={drag?.kind ?? "none"}
      data-drag-changed={
        drag?.kind === "draw" && (drag.cx !== drag.sx || drag.cy !== drag.sy) ? "true" : "false"
      }
      className={styles.root}
      onMouseLeave={() => {
        onCursorMove(null);
        setSnapIndicator(null);
        updateMaskCursor(null);
        containerRef.current?.style.setProperty("--image-stage-cursor", "default");
      }}
      onContextMenu={handleContextMenu}
      onPointerDown={(evt) => {
        if (evt.button !== 2) return;
        rightDownRef.current = { x: evt.clientX, y: evt.clientY };
        closeContextMenu();
        if (drag || readOnly) return;
        if (keypointDraftPending) return;
        evt.preventDefault();
        setDrag({ kind: "pan", sx: evt.clientX, sy: evt.clientY });
      }}
    >
      <PolygonSliceControls controller={polygonSlice} />
      {tool === "polygon" &&
        imageReady &&
        !readOnly &&
        !pendingDrawing &&
        polygonDraft?.boundaryTrace && (
          <PolygonBoundaryTraceControls
            controller={boundaryTrace}
            onBegin={() => {
              onSelectBox(null);
              setSnapIndicator(null);
              boundaryTrace.begin();
            }}
          />
        )}
      {tool === "polygon" && (polygonDraft?.points.length ?? 0) > 0 && (
        <div
          role="status"
          data-testid="polygon-auto-points-status"
          className="pointer-events-none absolute bottom-3 left-3 z-local-overlay rounded border border-border bg-card px-3 py-2 text-2xs text-muted-foreground"
        >
          {(polygonDraft?.points.length ?? 0) >= POLYGON_AUTO_POINT_LIMIT
            ? "已达 20,000 点，自动落点已暂停；Backspace 撤一点，Esc 取消草稿"
            : `${polygonDraft?.points.length} 点 · Shift 拖动自动落点 · Enter 闭合 · Backspace 撤一点`}
        </div>
      )}
      {!imageLoaded && previewSourceUrl && blurhash && <BlurhashLayer hash={blurhash} />}

      {!resolvedImageSource && (
        <div className={styles.emptyState}>
          <Icon name="warning" size={32} />
          <div className={styles.emptyStateText}>图像不可用</div>
        </div>
      )}

      {(resolvedImageSource?.kind === "pyramid-pending" ||
        resolvedImageSource?.kind === "pyramid-failed") && (
        <div className={styles.pyramidStatus} role="status">
          <Icon
            name={resolvedImageSource.kind === "pyramid-pending" ? "clock" : "warning"}
            size={14}
          />
          {resolvedImageSource.width <= 0 || resolvedImageSource.height <= 0
            ? resolvedImageSource.kind === "pyramid-pending"
              ? "正在准备图片，当前显示安全预览"
              : "图片暂不可用，当前显示安全预览"
            : resolvedImageSource.kind === "pyramid-pending"
              ? "高清切片生成中，当前显示预览"
              : "高清切片暂不可用，当前显示安全预览"}
          {resolvedImageSource.kind === "pyramid-failed" && onRetryImagePyramid && (
            <button
              type="button"
              className={styles.pyramidRetryButton}
              disabled={pyramidRetryState === "pending"}
              onClick={() => void retryImagePyramid()}
            >
              {pyramidRetryState === "pending"
                ? "重试中…"
                : pyramidRetryState === "failed"
                  ? "重试失败，再试一次"
                  : "重新生成"}
            </button>
          )}
        </div>
      )}

      {resolvedImageSource?.kind === "pyramid" &&
        (!overviewAdmitted ||
          imageTiles.snapshot?.prefetchPaused ||
          (imageTiles.snapshot?.targetCoverageRatio ?? 1) < 1) && (
          <div className={styles.pyramidStatus} role="status">
            <Icon name="clock" size={14} />
            {(imageTiles.snapshot?.retryingVisibleTiles ?? 0) > 0
              ? "高清切片请求失败，正在自动重试"
              : "正在恢复图像清晰度"}
          </div>
        )}

      <div className={fitted ? styles.konvaHost : `${styles.konvaHost} ${styles.konvaHostHidden}`}>
        <Stage
          ref={stageRef}
          width={vpSize.w || 1}
          height={vpSize.h || 1}
          x={vp.tx}
          y={vp.ty}
          scaleX={vp.scale}
          scaleY={vp.scale}
          onMouseDown={handleStageMouseDown}
          onMouseMove={handleStageMouseMove}
          onDblClick={handleStageDblClick}
        >
          {resolvedImageSource?.kind === "pyramid" ? (
            <KonvaImageTileLayer
              key={imageIdentity}
              overviewImage={displayImage}
              imageWidth={imgW}
              imageHeight={imgH}
              scheduler={imageTiles.scheduler}
              revision={imageTiles.revision}
              imageSmoothingEnabled={workbenchConfig.image.smoothImage}
            />
          ) : (
            <Layer name="bg" listening={false}>
              {displayImage && (
                <KonvaImage
                  image={displayImage}
                  x={0}
                  y={0}
                  width={imgW}
                  height={imgH}
                  listening={false}
                  imageSmoothingEnabled={workbenchConfig.image.smoothImage}
                />
              )}
            </Layer>
          )}

          <Layer name="image-mask" listening={false} imageSmoothingEnabled={false}>
            {displayedPredictionMaskRecords.map((record) => {
              const candidate = aiBoxes.find((box) => box.id === record.id);
              if (!candidate) return null;
              const color = classColorForCanvas(candidate.cls);
              return (
                <RasterMaskVisual
                  key={record.cacheKey}
                  id={record.id}
                  source="prediction"
                  image={record.image}
                  bounds={record.bounds}
                  sourceWidth={imgW}
                  sourceHeight={imgH}
                  scale={vp.scale}
                  color={color}
                  selected={record.selected}
                  opacityScale={fadedAiIds?.has(record.id) ? workbenchConfig.image.fadedOpacity : 1}
                  visual={annotationVisual}
                  labelText={buildLabelText(
                    {
                      className: displayClassName(candidate.cls),
                      confidence: candidate.conf,
                      attributes: candidate.attributes,
                    },
                    annotationVisual.labelContent.single,
                  )}
                  showLabel={shouldShowLabel(record.selected, annotationVisual.labelVisibility)}
                />
              );
            })}
            {displayedRasterMaskRecords.map((record) => {
              const annotation = visibleUserBoxById.get(record.id);
              const color = classColorForCanvas(annotation?.cls ?? "mask");
              return (
                <RasterMaskVisual
                  key={record.cacheKey}
                  id={record.id}
                  image={record.image}
                  bounds={record.bounds}
                  sourceWidth={imgW}
                  sourceHeight={imgH}
                  scale={vp.scale}
                  color={color}
                  selected={record.selected}
                  visual={annotationVisual}
                  labelText={
                    annotation
                      ? buildLabelText(
                          {
                            className: displayClassName(annotation.cls),
                            confidence: annotation.conf,
                            attributes: annotation.attributes,
                          },
                          annotationVisual.labelContent.single,
                        )
                      : null
                  }
                  showLabel={shouldShowLabel(record.selected, annotationVisual.labelVisibility)}
                />
              );
            })}
          </Layer>

          <MaskCompareTileLayer
            store={maskCompareStore}
            viewport={
              viewportBBox
                ? {
                    x: viewportBBox.minX * imgW,
                    y: viewportBBox.minY * imgH,
                    width: (viewportBBox.maxX - viewportBBox.minX) * imgW,
                    height: (viewportBBox.maxY - viewportBBox.minY) * imgH,
                  }
                : null
            }
          />

          <Layer name="ai" listening={selectActive && !polygonSlice.session}>
            {aiBoxes
              .filter((box) => box.geometry?.type !== "raster_mask")
              .map((b) =>
                b.polyline && b.polyline.length >= 2 ? (
                  <KonvaPolyline
                    key={b.id}
                    b={b}
                    isAi
                    selected={selSet.has(b.id)}
                    faded={fadedAiIds?.has(b.id) ?? false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    points={b.polyline as Pt[]}
                    onClick={(evt) => onSelectBox(b.id, { shift: !!evt?.evt?.shiftKey })}
                  />
                ) : b.polygon && b.polygon.length >= 3 ? (
                  <KonvaPolygon
                    key={b.id}
                    b={b}
                    isAi
                    selected={selSet.has(b.id)}
                    faded={fadedAiIds?.has(b.id) ?? false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    onClick={(evt) => onSelectBox(b.id, { shift: !!evt?.evt?.shiftKey })}
                  />
                ) : (
                  <KonvaBox
                    key={b.id}
                    b={b}
                    isAi
                    selected={selSet.has(b.id)}
                    faded={fadedAiIds?.has(b.id) ?? false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    editable={!readOnly}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    onClick={(evt) => onSelectBox(b.id, { shift: !!evt?.evt?.shiftKey })}
                    onMoveStart={null}
                    onResizeStart={null}
                  />
                ),
              )}
          </Layer>

          <Layer
            name="user"
            listening={userLayerListening && !boundaryTrace.trace && !polygonSlice.session}
          >
            {visibleSortedUserBoxes.map((b) => {
              if (!shouldRenderImageAnnotationShape(b)) return null;
              const ov = overrideGeom(b.id);
              const display: Annotation = ov ? { ...b, ...ov } : b;
              const renderKey = b.render_key ?? b.id;
              if (b.geometry?.type === "rotated_bbox") {
                const g = b.geometry;
                const rotPending = rotatedOverride(b.id);
                const liveGeometry =
                  (drag?.kind === "resizeRotatedBox" || drag?.kind === "moveRotatedBox") &&
                  drag.id === b.id
                    ? drag.cur
                    : (rotPending ?? g);
                const liveAngle =
                  drag?.kind === "rotateBox" && drag.id === b.id ? drag.cur : liveGeometry.angle;
                const isPrimarySingleSelect =
                  selectedId === b.id && selSet.size === 1 && canEditSavedGeometry && !b.is_locked;
                return (
                  <KonvaRotatedBox
                    key={renderKey}
                    b={b}
                    annotationId={b.id}
                    geometry={liveGeometry}
                    angle={liveAngle}
                    isAi={false}
                    selected={selSet.has(b.id)}
                    editable={isPrimarySingleSelect}
                    faded={false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    occluded={!!b.occluded}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    onClick={(evt) => handleUserShapeClick(b.id, evt)}
                    onMoveStart={
                      isPrimarySingleSelect
                        ? (e) => {
                            const pt = toImg(e.evt.clientX, e.evt.clientY);
                            if (!pt) return;
                            setDrag({
                              kind: "moveRotatedBox",
                              id: b.id,
                              start: liveGeometry,
                              sx: pt.x,
                              sy: pt.y,
                              cur: liveGeometry,
                            });
                          }
                        : null
                    }
                    onRotateStart={
                      isPrimarySingleSelect
                        ? (e) => {
                            const pt = toImg(e.evt.clientX, e.evt.clientY);
                            if (!pt) return;
                            setDrag({
                              kind: "rotateBox",
                              id: b.id,
                              cx: g.cx,
                              cy: g.cy,
                              startAngle: g.angle,
                              cur: g.angle,
                            });
                          }
                        : null
                    }
                    onResizeStart={
                      isPrimarySingleSelect
                        ? (dir, e) => {
                            const pt = toImg(e.evt.clientX, e.evt.clientY);
                            if (!pt) return;
                            setDrag({
                              kind: "resizeRotatedBox",
                              id: b.id,
                              start: g,
                              sx: pt.x,
                              sy: pt.y,
                              dir,
                              cur: g,
                            });
                          }
                        : null
                    }
                  />
                );
              }
              if (display.polyline && display.polyline.length >= 2) {
                const polyOv = polyOverridePoints(b.id);
                const livePoints = polyOv ?? (display.polyline as Pt[]);
                const isOnlySelected =
                  selectedId === b.id && selSet.size === 1 && canEditSavedGeometry && !b.is_locked;
                return (
                  <KonvaPolyline
                    key={renderKey}
                    b={display}
                    annotationId={b.id}
                    isAi={false}
                    selected={selSet.has(b.id)}
                    faded={false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    points={livePoints}
                    editable={isOnlySelected}
                    occluded={!!b.occluded}
                    onClick={(evt) => handleUserShapeClick(b.id, evt)}
                    onVertexMouseDown={(vidx, e) => {
                      const cur = (polyOverridePoints(b.id) ?? (b.polyline as Pt[])).slice();
                      if (e.evt.shiftKey) {
                        if (cur.length <= 2) return;
                        const next = cur.slice();
                        next.splice(vidx, 1);
                        onCommitPolygonGeometry?.(b.id, cur, next);
                        return;
                      }
                      setDrag({ kind: "polyVertex", id: b.id, vidx, start: cur, cur });
                    }}
                    onEdgeMouseDown={(edgeIdx, e) => {
                      if (!e.evt.altKey) return;
                      const pt = toImg(e.evt.clientX, e.evt.clientY);
                      if (!pt) return;
                      const cur = (polyOverridePoints(b.id) ?? (b.polyline as Pt[])).slice();
                      const next = cur.slice();
                      next.splice(edgeIdx + 1, 0, [pt.x, pt.y]);
                      onCommitPolygonGeometry?.(b.id, cur, next);
                    }}
                    onBodyMouseDown={
                      isOnlySelected
                        ? (e) => {
                            const pt = toImg(e.evt.clientX, e.evt.clientY);
                            if (!pt) return;
                            const cur = (polyOverridePoints(b.id) ?? (b.polyline as Pt[])).slice();
                            setDrag({
                              kind: "polyMove",
                              id: b.id,
                              start: cur,
                              sx: pt.x,
                              sy: pt.y,
                              cur,
                            });
                          }
                        : null
                    }
                  />
                );
              }
              if (b.geometry?.type === "keypoint") {
                const kpOv = kpOverridePoints(b.id);
                const liveKps = kpOv ?? b.keypoints ?? [];
                const isKpEditable =
                  selectedId === b.id && selSet.size === 1 && canEditSavedGeometry && !b.is_locked;
                return (
                  <KonvaKeypoint
                    key={renderKey}
                    b={kpOv ? { ...display, keypoints: liveKps } : display}
                    annotationId={b.id}
                    isAi={false}
                    selected={selSet.has(b.id)}
                    faded={false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    schema={keypointSchema}
                    editable={isKpEditable}
                    onClick={(evt) => handleUserShapeClick(b.id, evt)}
                    onNodeMouseDown={(nidx) => {
                      const cur = (kpOverridePoints(b.id) ?? b.keypoints ?? []).slice();
                      setDrag({ kind: "kpNode", id: b.id, nidx, start: cur, cur });
                    }}
                    onToggleVisibility={(nidx) => {
                      const cur = (b.keypoints ?? []).slice();
                      const p = cur[nidx];
                      if (!p) return;
                      const nextV = (p.v === 2 ? 1 : p.v === 1 ? 0 : 2) as Keypoint["v"];
                      const next = cur.map((kp, i) => (i === nidx ? { ...kp, v: nextV } : kp));
                      onCommitKeypointGeometry?.(b.id, cur, next);
                    }}
                  />
                );
              }
              if (display.polygon && display.polygon.length >= 3) {
                const polyOv = polyOverridePoints(b.id);
                const livePoints = polyOv ?? (display.polygon as Pt[]);
                const geometrySupportsDirectEdit = b.geometry
                  ? supportsSingleRingPolygonEdit(b.geometry)
                  : !(display.holes?.length || display.multiPolygon?.length);
                const isOnlySelected =
                  geometrySupportsDirectEdit &&
                  selectedId === b.id &&
                  selSet.size === 1 &&
                  canEditSavedGeometry &&
                  !b.is_locked;
                const draggingThisVertex =
                  drag?.kind === "polyVertex" && drag.id === b.id ? drag.vidx : -1;
                const intersects =
                  isOnlySelected &&
                  (draggingThisVertex >= 0
                    ? !isSelfIntersectingIncremental(livePoints, draggingThisVertex).ok
                    : !isSelfIntersecting(livePoints).ok);
                return (
                  <KonvaPolygon
                    key={renderKey}
                    b={display}
                    annotationId={b.id}
                    isAi={false}
                    selected={selSet.has(b.id)}
                    faded={false}
                    fadedOpacity={workbenchConfig.image.fadedOpacity}
                    visual={annotationVisual}
                    imgW={imgW}
                    imgH={imgH}
                    scale={vp.scale}
                    points={livePoints}
                    selfIntersect={intersects}
                    viewportBBox={viewportBBox}
                    editable={isOnlySelected}
                    occluded={!!b.occluded}
                    onClick={(evt) => handleUserShapeClick(b.id, evt)}
                    onVertexMouseDown={(vidx, e) => {
                      const cur = (polyOverridePoints(b.id) ?? (b.polygon as Pt[])).slice();
                      if (e.evt.shiftKey) {
                        if (cur.length <= 3) return;
                        const next = cur.slice();
                        next.splice(vidx, 1);
                        onCommitPolygonGeometry?.(b.id, cur, next);
                        return;
                      }
                      setDrag({ kind: "polyVertex", id: b.id, vidx, start: cur, cur });
                    }}
                    onEdgeMouseDown={(edgeIdx, e) => {
                      if (!e.evt.altKey) return;
                      const pt = toImg(e.evt.clientX, e.evt.clientY);
                      if (!pt) return;
                      const cur = (polyOverridePoints(b.id) ?? (b.polygon as Pt[])).slice();
                      const next = cur.slice();
                      next.splice(edgeIdx + 1, 0, [pt.x, pt.y]);
                      onCommitPolygonGeometry?.(b.id, cur, next);
                    }}
                    onBodyMouseDown={
                      isOnlySelected
                        ? (e) => {
                            const pt = toImg(e.evt.clientX, e.evt.clientY);
                            if (!pt) return;
                            const cur = (polyOverridePoints(b.id) ?? (b.polygon as Pt[])).slice();
                            setDrag({
                              kind: "polyMove",
                              id: b.id,
                              start: cur,
                              sx: pt.x,
                              sy: pt.y,
                              cur,
                            });
                          }
                        : null
                    }
                  />
                );
              }
              const isPrimarySingleSelect =
                selectedId === b.id && selSet.size === 1 && canEditSavedGeometry && !b.is_locked;
              return (
                <KonvaBox
                  key={renderKey}
                  b={display}
                  annotationId={b.id}
                  isAi={false}
                  selected={selSet.has(b.id)}
                  faded={false}
                  fadedOpacity={workbenchConfig.image.fadedOpacity}
                  visual={annotationVisual}
                  editable={canEditSavedGeometry && !b.is_locked}
                  occluded={!!b.occluded}
                  imgW={imgW}
                  imgH={imgH}
                  scale={vp.scale}
                  onClick={(evt) => handleUserShapeClick(b.id, evt)}
                  onMoveStart={
                    isPrimarySingleSelect
                      ? (e) => {
                          const pt = toImg(e.evt.clientX, e.evt.clientY);
                          if (!pt) return;
                          setDrag({
                            kind: "move",
                            id: b.id,
                            start: { x: b.x, y: b.y, w: b.w, h: b.h },
                            sx: pt.x,
                            sy: pt.y,
                            cur: { x: b.x, y: b.y, w: b.w, h: b.h },
                            alt: e.evt.altKey,
                          });
                        }
                      : null
                  }
                  onResizeStart={
                    isPrimarySingleSelect
                      ? (dir, e) => {
                          const pt = toImg(e.evt.clientX, e.evt.clientY);
                          if (!pt) return;
                          setDrag({
                            kind: "resize",
                            id: b.id,
                            start: { x: b.x, y: b.y, w: b.w, h: b.h },
                            sx: pt.x,
                            sy: pt.y,
                            dir,
                            cur: { x: b.x, y: b.y, w: b.w, h: b.h },
                          });
                        }
                      : null
                  }
                />
              );
            })}
            {siblingHighlightChildBoxes.map((b) => (
              <Rect
                key={`sibling-${b.id}`}
                x={b.x * imgW - 6 / vp.scale}
                y={b.y * imgH - 6 / vp.scale}
                width={b.w * imgW + 12 / vp.scale}
                height={b.h * imgH + 12 / vp.scale}
                stroke={SIBLING_HIGHLIGHT_COLOR}
                strokeWidth={2 / vp.scale}
                dash={[2 / vp.scale, 2 / vp.scale]}
                cornerRadius={2 / vp.scale}
                fill="transparent"
                listening={false}
              />
            ))}
          </Layer>

          {!maskCompareActive &&
            tool === "mask" &&
            maskEditor?.active &&
            maskEditor.buffer &&
            !maskOperationPreviewBuffer && (
              <MaskOverlayLayer
                buffer={maskEditor.buffer}
                revision={maskEditor.revision}
                imgW={imgW}
                imgH={imgH}
                opacity={annotationVisual.fillOpacitySelected}
                color={maskEditorColor}
                visible={true}
              />
            )}
          {!maskCompareActive &&
            tool === "mask" &&
            maskEditor?.active &&
            maskEditor.backend === "tiled" && (
              <TiledMaskOverlayLayer
                tiles={maskEditor.tiledTiles}
                overview={
                  tiledMaskOverviewRecord
                    ? {
                        image: tiledMaskOverviewRecord.image,
                        x: tiledMaskOverviewRecord.bounds.x * imgW,
                        y: tiledMaskOverviewRecord.bounds.y * imgH,
                        width: tiledMaskOverviewRecord.bounds.w * imgW,
                        height: tiledMaskOverviewRecord.bounds.h * imgH,
                      }
                    : null
                }
                opacity={annotationVisual.fillOpacitySelected}
                color={maskEditorColor}
                visible
              />
            )}
          {!maskCompareActive && tool === "mask" && maskOperationPreviewBuffer && (
            <MaskOverlayLayer
              buffer={maskOperationPreviewBuffer}
              revision={maskEditor?.operationPreview?.id ?? 0}
              imgW={imgW}
              imgH={imgH}
              opacity={annotationVisual.fillOpacitySelected}
              color={MASK_OPERATION_PREVIEW_COLOR}
              visible
            />
          )}
          {!maskCompareActive && tool === "mask" && maskInstancePreviewBuffer && (
            <MaskOverlayLayer
              buffer={maskInstancePreviewBuffer}
              revision={maskEditor?.instanceOperationPreview?.id ?? 0}
              imgW={imgW}
              imgH={imgH}
              opacity={annotationVisual.fillOpacitySelected}
              color={MASK_INSTANCE_PREVIEW_COLOR}
              visible
            />
          )}

          {historicalShapes && historicalShapes.length > 0 && (
            <Layer name="historical-canvas" listening={false} opacity={0.5}>
              {historicalShapes.map((shape, idx) => {
                const flat: number[] = [];
                for (let i = 0; i < shape.points.length; i += 2) {
                  flat.push(shape.points[i] * imgW, shape.points[i + 1] * imgH);
                }
                return (
                  <Line
                    key={`hist-${idx}`}
                    points={flat}
                    stroke={shape.stroke ?? "#ef4444"}
                    strokeWidth={3 / vp.scale}
                    lineCap="round"
                    lineJoin="round"
                    tension={0.3}
                    dash={[6 / vp.scale, 4 / vp.scale]}
                  />
                );
              })}
            </Layer>
          )}

          <CanvasDrawingLayer
            shapes={canvasShapes ?? []}
            draftStroke={drag?.kind === "canvasStroke" ? drag.points : null}
            draftColor={canvasStroke}
            imgW={imgW}
            imgH={imgH}
            scale={vp.scale}
            editable={canvasEditable && tool === "canvas"}
          />

          <Layer name="overlay" listening={false}>
            {boundaryTrace.trace?.start && (
              <>
                {boundaryTrace.trace.paths && (
                  <Line
                    points={boundaryTrace.trace.paths[boundaryTrace.trace.direction].points.flatMap(
                      ([x, y]) => [x * imgW, y * imgH],
                    )}
                    stroke={pendingColor}
                    strokeWidth={3 / vp.scale}
                    dash={[8 / vp.scale, 4 / vp.scale]}
                    lineJoin="round"
                  />
                )}
                <Circle
                  x={boundaryTrace.trace.start.point[0] * imgW}
                  y={boundaryTrace.trace.start.point[1] * imgH}
                  radius={5 / vp.scale}
                  stroke={pendingColor}
                  strokeWidth={2 / vp.scale}
                  fill="white"
                />
              </>
            )}
            {polygonSlice.session && (
              <Group listening={false}>
                {polygonSlice.session.preview?.map((part, index) => (
                  <Line
                    key={`slice-${index}`}
                    closed
                    points={part.points.flatMap(([x, y]) => [x * imgW, y * imgH])}
                    stroke={classColorForCanvas(index === 0 ? "slice-kept" : "slice-new")}
                    fill={hexToRgba(
                      classColorForCanvas(index === 0 ? "slice-kept" : "slice-new"),
                      0.28,
                    )}
                    strokeWidth={2 / vp.scale}
                  />
                ))}
                <Line
                  points={polygonSlice.session.points.flatMap(([x, y]) => [x * imgW, y * imgH])}
                  stroke={pendingColor}
                  strokeWidth={3 / vp.scale}
                  dash={[8 / vp.scale, 4 / vp.scale]}
                />
                {polygonSlice.session.points.map(([x, y], i) => (
                  <Circle
                    key={`slice-point-${i}`}
                    x={x * imgW}
                    y={y * imgH}
                    radius={4 / vp.scale}
                    stroke={pendingColor}
                    strokeWidth={2 / vp.scale}
                    fill="white"
                  />
                ))}
              </Group>
            )}
            {polygonDraft &&
              polygonDraft.points.length > 0 &&
              (() => {
                const isPolyline = polygonDraft.closed === false;
                const ps = polygonDraft.points;
                const flat: number[] = [];
                for (const [px, py] of ps) flat.push(px * imgW, py * imgH);
                if (polygonCursor) flat.push(polygonCursor.x * imgW, polygonCursor.y * imgH);
                const draftColor = classColorForCanvas(
                  activeClass || (isPolyline ? "polyline" : "polygon"),
                );
                const canClose =
                  !isPolyline &&
                  ps.length >= 3 &&
                  polygonCursor &&
                  Math.hypot(polygonCursor.x - ps[0][0], polygonCursor.y - ps[0][1]) <=
                    CLOSE_DISTANCE;
                return (
                  <>
                    <Line
                      points={flat}
                      closed={!isPolyline}
                      stroke={draftColor}
                      strokeWidth={1.5 / vp.scale}
                      dash={[4 / vp.scale, 3 / vp.scale]}
                      lineCap="round"
                      lineJoin="round"
                      fill={
                        isPolyline ? undefined : hexToRgba(draftColor, annotationVisual.fillOpacity)
                      }
                    />
                    {ps.length > 500 ? (
                      <Shape
                        sceneFunc={(ctx) => {
                          ctx.setAttr("fillStyle", "white");
                          ctx.setAttr("strokeStyle", draftColor);
                          ctx.setAttr("lineWidth", 1.5 / vp.scale);
                          ctx.beginPath();
                          for (const [px, py] of ps.slice(1)) {
                            const x = px * imgW;
                            const y = py * imgH;
                            ctx.moveTo(x + 3 / vp.scale, y);
                            ctx.arc(x, y, 3 / vp.scale, 0, Math.PI * 2);
                          }
                          ctx.fill();
                          ctx.stroke();
                          ctx.beginPath();
                          ctx.arc(ps[0][0] * imgW, ps[0][1] * imgH, 4.5 / vp.scale, 0, Math.PI * 2);
                          ctx.setAttr("fillStyle", canClose ? draftColor : "white");
                          ctx.fill();
                          ctx.stroke();
                        }}
                      />
                    ) : (
                      ps.map(([px, py], i) => (
                        <Circle
                          key={i}
                          x={px * imgW}
                          y={py * imgH}
                          radius={(i === 0 ? 4.5 : 3) / vp.scale}
                          fill={i === 0 && canClose ? draftColor : "white"}
                          stroke={draftColor}
                          strokeWidth={1.5 / vp.scale}
                        />
                      ))
                    )}
                  </>
                );
              })()}
            {snapIndicator && (
              <Circle
                x={snapIndicator.x * imgW}
                y={snapIndicator.y * imgH}
                radius={5 / vp.scale}
                fill={hexToRgba(pendingColor, 0.16)}
                stroke={pendingColor}
                strokeWidth={1.5 / vp.scale}
                listening={false}
              />
            )}
            {tool === "keypoint" &&
              keypointDraft &&
              keypointDraft.nodeCount > 0 &&
              (() => {
                const placed = keypointDraft.points;
                const nextIdx = placed.length;
                const done = nextIdx >= keypointDraft.nodeCount;
                const nextNode = keypointSchema?.nodes[nextIdx];
                const nextName = nextNode
                  ? nextNode.sublabel
                    ? `${nextNode.name}·${nextNode.sublabel}`
                    : nextNode.name
                  : `#${nextIdx + 1}`;
                return (
                  <>
                    {placed.map((p, i) => {
                      if (p.v === 0) {
                        return (
                          <Circle
                            key={`d-${i}`}
                            x={p.x * imgW}
                            y={p.y * imgH}
                            radius={2.5 / vp.scale}
                            fill={hexToRgba(keypointColorByIndex(i, keypointSchema), 0.3)}
                          />
                        );
                      }
                      const c = keypointColorByIndex(i, keypointSchema);
                      return (
                        <Circle
                          key={`d-${i}`}
                          x={p.x * imgW}
                          y={p.y * imgH}
                          radius={4 / vp.scale}
                          fill={p.v === 2 ? c : "white"}
                          stroke={c}
                          strokeWidth={1.5 / vp.scale}
                        />
                      );
                    })}
                    {!done && polygonCursor && (
                      <>
                        <Circle
                          x={polygonCursor.x * imgW}
                          y={polygonCursor.y * imgH}
                          radius={5 / vp.scale}
                          fill={hexToRgba(keypointColorByIndex(nextIdx, keypointSchema), 0.25)}
                          stroke={keypointColorByIndex(nextIdx, keypointSchema)}
                          strokeWidth={1.5 / vp.scale}
                          dash={[3 / vp.scale, 2 / vp.scale]}
                        />
                        <Label
                          x={polygonCursor.x * imgW + 8 / vp.scale}
                          y={polygonCursor.y * imgH - 8 / vp.scale}
                        >
                          <Tag
                            fill={keypointColorByIndex(nextIdx, keypointSchema)}
                            cornerRadius={3 / vp.scale}
                          />
                          <Text
                            text={`${nextName} (${nextIdx + 1}/${keypointDraft.nodeCount})`}
                            fill="white"
                            fontSize={10.5 / vp.scale}
                            padding={4 / vp.scale}
                            fontFamily={BOX_LABEL_FONT_FAMILY}
                          />
                        </Label>
                      </>
                    )}
                  </>
                );
              })()}
            {drawingPreview && drawingPreview.w > 0 && (
              <Rect
                x={drawingPreview.x * imgW}
                y={drawingPreview.y * imgH}
                width={drawingPreview.w * imgW}
                height={drawingPreview.h * imgH}
                stroke={pendingColor}
                strokeWidth={1.5 / vp.scale}
                dash={[4 / vp.scale, 3 / vp.scale]}
                fill={hexToRgba(pendingColor, 0.12)}
                listening={false}
              />
            )}
            {samPreview && samPreview.w > 0 && (
              <Rect
                x={samPreview.x * imgW}
                y={samPreview.y * imgH}
                width={samPreview.w * imgW}
                height={samPreview.h * imgH}
                stroke="#a855f7"
                strokeWidth={1.5 / vp.scale}
                dash={[6 / vp.scale, 4 / vp.scale]}
                fill={hexToRgba("#a855f7", 0.08)}
                listening={false}
              />
            )}
            {drag?.kind === "samScribble" && drag.points.length > 1 && (
              <Line
                points={drag.points.flatMap(([x, y]) => [x * imgW, y * imgH])}
                stroke={drag.alt ? "#ef4444" : "#22c55e"}
                strokeWidth={Math.max(2 / vp.scale, 0.008 * Math.min(imgW, imgH))}
                lineCap="round"
                lineJoin="round"
                opacity={0.85}
                listening={false}
              />
            )}
            {!maskCompareActive && drag?.kind === "maskLasso" && drag.points.length > 1 && (
              <Line
                points={drag.points.flatMap(([x, y]) => [x, y])}
                stroke={maskEditor?.tool === "lasso_subtract" ? "#f97316" : "#22c55e"}
                strokeWidth={2 / vp.scale}
                dash={[5 / vp.scale, 3 / vp.scale]}
                lineCap="round"
                lineJoin="round"
                listening={false}
              />
            )}
            {!maskCompareActive &&
              tool === "mask" &&
              (drag?.kind === "maskSlice" ||
                maskEditor?.instanceOperationPreview?.plan.cutPath) && (
                <Line
                  points={(drag?.kind === "maskSlice"
                    ? [drag.start, drag.end]
                    : (maskEditor?.instanceOperationPreview?.plan.cutPath ?? [])
                  ).flatMap(([x, y]) => [x * imgW, y * imgH])}
                  stroke={classColorForCanvas("mask-slice-cut")}
                  strokeWidth={2 / vp.scale}
                  dash={[6 / vp.scale, 4 / vp.scale]}
                  listening={false}
                />
              )}
            {samCandidates && samCandidates.length > 0 && (
              <SamCandidateOverlay
                candidates={samCandidates}
                activeIdx={samActiveIdx}
                imgW={imgW}
                imgH={imgH}
                scale={vp.scale}
                tool={tool}
              />
            )}
            {displayedSamMaskRecords.map((record) => {
              const { bounds } = record;
              return (
                <Group
                  key={record.cacheKey}
                  id={record.id}
                  name="sam-native-mask-candidate"
                  listening={false}
                >
                  <KonvaImage
                    image={record.image}
                    x={bounds.x * imgW}
                    y={bounds.y * imgH}
                    width={bounds.w * imgW}
                    height={bounds.h * imgH}
                    opacity={fillAlpha(record.selected, annotationVisual)}
                    imageSmoothingEnabled={false}
                    listening={false}
                  />
                  {record.selected && (
                    <Rect
                      x={bounds.x * imgW}
                      y={bounds.y * imgH}
                      width={bounds.w * imgW}
                      height={bounds.h * imgH}
                      stroke="#a855f7"
                      strokeWidth={2 / vp.scale}
                      dash={[6 / vp.scale, 4 / vp.scale]}
                      listening={false}
                    />
                  )}
                </Group>
              );
            })}

            {samSubTool === "point" &&
              samSessionPoints &&
              samSessionPoints.length > 0 &&
              samSessionPoints.map((sp, idx) => {
                const cx = sp.pt[0] * imgW;
                const cy = sp.pt[1] * imgH;
                const r = 5 / vp.scale;
                if (sp.polarity === 1) {
                  return (
                    <Circle
                      key={`sam-pt-${idx}`}
                      x={cx}
                      y={cy}
                      radius={r}
                      fill="#22c55e"
                      stroke="#ffffff"
                      strokeWidth={1.5 / vp.scale}
                      listening={false}
                    />
                  );
                }
                const arm = r;
                return (
                  <Group key={`sam-pt-${idx}`} listening={false}>
                    <Line
                      points={[cx - arm, cy - arm, cx + arm, cy + arm]}
                      stroke="#ef4444"
                      strokeWidth={2 / vp.scale}
                      lineCap="round"
                      listening={false}
                    />
                    <Line
                      points={[cx - arm, cy + arm, cx + arm, cy - arm]}
                      stroke="#ef4444"
                      strokeWidth={2 / vp.scale}
                      lineCap="round"
                      listening={false}
                    />
                  </Group>
                );
              })}

            {samSubTool === "scribble" &&
              samSessionScribbles &&
              samSessionScribbles.length > 0 &&
              samSessionScribbles.map((stroke, idx) => (
                <Line
                  key={`sam-scribble-${idx}`}
                  points={stroke.points.flatMap(([x, y]) => [x * imgW, y * imgH])}
                  stroke={stroke.polarity === 1 ? "#22c55e" : "#ef4444"}
                  strokeWidth={Math.max(2 / vp.scale, stroke.width * Math.min(imgW, imgH))}
                  lineCap="round"
                  lineJoin="round"
                  opacity={0.85}
                  listening={false}
                />
              ))}

            {samSubTool === "exemplar" &&
              samSessionExemplars &&
              samSessionExemplars.length > 0 &&
              samSessionExemplars.map((se, idx) => {
                const [x1, y1, x2, y2] = se.bbox;
                const positive = se.polarity === 1;
                return (
                  <Rect
                    key={`sam-ex-${idx}`}
                    x={x1 * imgW}
                    y={y1 * imgH}
                    width={(x2 - x1) * imgW}
                    height={(y2 - y1) * imgH}
                    stroke={positive ? "#22c55e" : "#ef4444"}
                    strokeWidth={2 / vp.scale}
                    dash={positive ? undefined : [6 / vp.scale, 4 / vp.scale]}
                    listening={false}
                  />
                );
              })}

            {pendingDrawing && (
              <>
                {(pendingDrawing.kind === "polygon" || pendingDrawing.kind === "polyline") && (
                  <Line
                    points={pendingDrawing.points.flatMap(([x, y]) => [x * imgW, y * imgH])}
                    closed={pendingDrawing.kind === "polygon"}
                    stroke="oklch(0.65 0.18 75)"
                    strokeWidth={2 / vp.scale}
                    dash={[5 / vp.scale, 3 / vp.scale]}
                    fill={
                      pendingDrawing.kind === "polygon"
                        ? hexToRgba("#f59e0b", annotationVisual.fillOpacity)
                        : undefined
                    }
                    lineCap="round"
                    lineJoin="round"
                    listening={false}
                  />
                )}
                {pendingDrawing.kind === "keypoint" &&
                  pendingDrawing.points
                    .filter((point) => point.v > 0)
                    .map((point, index) => (
                      <Circle
                        key={`pending-keypoint-${index}`}
                        x={point.x * imgW}
                        y={point.y * imgH}
                        radius={4 / vp.scale}
                        fill={hexToRgba("#f59e0b", 0.25)}
                        stroke="oklch(0.65 0.18 75)"
                        strokeWidth={2 / vp.scale}
                        listening={false}
                      />
                    ))}
                {pendingDrawing.kind !== "polygon" &&
                  pendingDrawing.kind !== "polyline" &&
                  pendingDrawing.kind !== "keypoint" &&
                  pendingDrawing.kind !== "raster_mask" && (
                    <Rect
                      x={pendingDrawing.geom.x * imgW}
                      y={pendingDrawing.geom.y * imgH}
                      width={pendingDrawing.geom.w * imgW}
                      height={pendingDrawing.geom.h * imgH}
                      stroke="oklch(0.65 0.18 75)"
                      strokeWidth={2 / vp.scale}
                      dash={[5 / vp.scale, 3 / vp.scale]}
                      fill={hexToRgba("#f59e0b", 0.1)}
                      shadowColor="oklch(0.65 0.18 75)"
                      shadowBlur={6 / vp.scale}
                      shadowOpacity={0.5}
                      listening={false}
                    />
                  )}
                <Label
                  x={pendingDrawing.geom.x * imgW}
                  y={pendingDrawing.geom.y * imgH - 22 / vp.scale}
                  listening={false}
                >
                  <Tag fill="oklch(0.65 0.18 75)" cornerRadius={3 / vp.scale} />
                  <Text
                    text="? 待选类别"
                    fill="white"
                    fontSize={10.5 / vp.scale}
                    padding={4 / vp.scale}
                    fontFamily={BOX_LABEL_FONT_FAMILY}
                  />
                </Label>
              </>
            )}
            {customMaskCursorActive && maskEditor && (
              <Group clipX={0} clipY={0} clipWidth={imgW} clipHeight={imgH} listening={false}>
                <Circle
                  ref={maskCursorNodeRef}
                  x={0}
                  y={0}
                  visible={maskCursorVisible}
                  radius={maskEditor.radius}
                  stroke={maskEditor.mode === "erase" ? "#64748b" : "#dc2626"}
                  strokeWidth={1.5 / vp.scale}
                  dash={[4 / vp.scale, 3 / vp.scale]}
                  listening={false}
                />
              </Group>
            )}
          </Layer>
          {(issuePixelFeedbacks && issuePixelFeedbacks.length > 0) || issuePinDropArmed ? (
            <IssueLayer
              pixelIssues={(issuePixelFeedbacks ?? []).filter(
                (f) => f.kind === "issue" && f.anchor_type === "pixel" && !!f.anchor_position,
              )}
              imgW={imgW}
              imgH={imgH}
              scale={vp.scale}
              highlightId={highlightIssueId ?? null}
              onPinClick={onIssuePinClick}
              armedForDrop={!!issuePinDropArmed}
              onDrop={onIssuePinDrop}
            />
          ) : null}
        </Stage>
        {rasterMaskIssues.length > 0 && (
          <div
            className="pointer-events-auto absolute right-3 top-3 z-popover max-w-80 rounded-md border border-border bg-card p-2 text-xs text-foreground shadow-md"
            role="status"
            aria-label="Mask 加载状态"
          >
            {rasterMaskIssues.map(({ id, status }) => (
              <div key={id} className="flex items-center gap-2">
                <span className="min-w-0 flex-1 truncate">
                  {userBoxes.find((annotation) => annotation.id === id)?.cls ??
                    aiBoxes.find((box) => box.id === id)?.cls ??
                    "Mask"}
                  ：
                  <CopyableError message={status.message} truncate />
                </span>
                {status.retryable && (
                  <button
                    type="button"
                    className="rounded border border-border px-2 py-1 hover:bg-muted"
                    onClick={() => onRetryRasterMask?.(id)}
                    aria-label={`重试 Mask ${id}`}
                  >
                    重试
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {commentBadgeAnnotations.length > 0 && (
        <ImageStageCommentBadges
          annotations={commentBadgeAnnotations}
          rasterMaskRecords={displayedRasterMaskRecords}
          counts={annotationCommentCounts}
          imgW={imgW}
          imgH={imgH}
          vp={vp}
          viewportSize={vpSize}
          selectedIds={selSet}
          onOpenAnnotationComments={onOpenAnnotationComments}
          interactive={
            selectActive &&
            !pendingDrawing &&
            !polygonSlice.session &&
            !boundaryTrace.trace &&
            !issuePinDropArmed &&
            !drag
          }
        />
      )}

      {tool !== "magic-box" &&
        onRefineSamCandidate &&
        samCandidates &&
        samCandidates.length > 0 &&
        (() => {
          const cand = samCandidates[samActiveIdx];
          if (!cand || cand.type !== "polygonlabels" || !cand.points || cand.points.length < 3)
            return null;
          let minX = Infinity,
            minY = Infinity,
            maxX = -Infinity;
          for (const [x, y] of cand.points) {
            if (x < minX) minX = x;
            if (y < minY) minY = y;
            if (x > maxX) maxX = x;
          }
          if (!isFinite(minX)) return null;
          const left = maxX * imgW * vp.scale + vp.tx;
          const top = minY * imgH * vp.scale + vp.ty;
          return (
            <SamRefineButton
              left={left}
              top={top}
              onClick={(e) => {
                e.stopPropagation();
                onRefineSamCandidate(samActiveIdx);
              }}
            />
          );
        })()}

      {selectedBox && !readOnly && !pendingDrawing && selectActive && isSelectedAi && (
        <SelectionOverlay
          box={selectedBox}
          isAi
          imgW={imgW}
          imgH={imgH}
          vp={vp}
          onAccept={onAcceptPrediction ? () => onAcceptPrediction(selectedBox as AiBox) : undefined}
          onReject={() => onRejectPrediction?.(selectedBox as AiBox)}
        />
      )}

      <ContextMenu
        open={contextMenu.open && contextMenuItems.length > 0}
        x={contextMenu.x}
        y={contextMenu.y}
        items={contextMenuItems}
        onClose={closeContextMenu}
      />

      {overlay}
    </div>
  );
}
