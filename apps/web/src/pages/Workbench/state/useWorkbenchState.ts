import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from "react";
import type { Annotation, Keypoint } from "@/types";
import type { CommentCanvasDrawing } from "@/api/comments";
import { randomId } from "@/utils/id";
import type { DiscussionOrigin } from "./discussionTypes";
import type { TextOutputMode } from "./useInteractiveAI";
import { useWorkbenchConfig } from "./useWorkbenchConfig";
import type { ContinuousImageCreation, ManualCreationDraft } from "./manualImageCreation";
import { videoToolScopeForTool, type VideoToolSelection } from "../stage/videoToolUnits";
import type { BboxCreationMode } from "../stage/ImageStage.helpers";

export type { VideoToolScope, VideoToolSelection } from "../stage/videoToolUnits";

export type Tool =
  | "select"
  | "box"
  | "rotated-box"
  | "hand"
  | "polygon"
  | "polyline"
  | "canvas"
  | "mask"
  | "smart-point"
  | "smart-box"
  | "smart-scribble"
  | "text-prompt"
  | "exemplar"
  | "magic-box"
  | "keypoint";
export type VideoTool =
  | "select"
  | "box"
  | "rotated-box"
  | "keypoint"
  | "track"
  | "mask"
  | "mask-track"
  | "polygon"
  | "polyline"
  | "polygon-track"
  | "polyline-track"
  | "smart-point"
  | "smart-box"
  | "exemplar"
  | "magic-box";
export type ThreeDTool = "select" | "box" | "point-mask" | "measure";

export type SamSubTool = "point" | "bbox" | "scribble" | "text" | "exemplar";

export type SamPolarity = "positive" | "negative";

export function toolToSamSubTool(tool: Tool): SamSubTool | null {
  switch (tool) {
    case "smart-point":
      return "point";
    case "smart-box":
      return "bbox";
    case "smart-scribble":
      return "scribble";
    case "magic-box":
      return "bbox";
    case "text-prompt":
      return "text";
    case "exemplar":
      return "exemplar";
    default:
      return null;
  }
}

export type CanvasDraft = {
  active: boolean;
  annotationId: string | null;
  shapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  stroke: string;
  pendingResult: CommentCanvasDrawing | null;
  origin?: DiscussionOrigin | null;
  resultId?: string | null;
  initialDrawing?: CommentCanvasDrawing | null;
};

const DEFAULT_CANVAS_STROKE = "#ef4444";

export type Geom = { x: number; y: number; w: number; h: number };

type PendingDrawingGeometry =
  | { kind?: "bbox" | "rotated_bbox" | "raster_mask"; geom: Geom }
  | { kind: "polygon" | "polyline"; geom: Geom; points: [number, number][] }
  | { kind: "keypoint"; geom: Geom; points: Keypoint[] }
  | {
      kind:
        | "video_bbox"
        | "video_rotated_bbox"
        | "video_track_bbox"
        | "video_polygon"
        | "video_polyline"
        | "video_track_polygon"
        | "video_track_polyline";
      frameIndex: number;
      geom: Geom;
      anchor: { left: number; top: number };
      points?: [number, number][];
    }
  | {
      kind: "video_keypoint";
      frameIndex: number;
      geom: Geom;
      anchor: { left: number; top: number };
      points: Keypoint[];
    }
  | {
      kind: "video_mask";
      frameIndex: number;
      geom: Geom;
      anchor: { left: number; top: number };
    }
  | null;

export type PendingDrawing =
  | (NonNullable<PendingDrawingGeometry> & { creation?: ManualCreationDraft })
  | null;

export type EditingClass = {
  annotationId: string;
  geom: Geom;
  currentClass: string;
  toolUnitId?: string;
  anchor?: { left: number; top: number };
  accept?: { predictionId: string; shapeIndex?: number; toolUnitId?: string };
} | null;

export function useWorkbenchState() {
  const {
    config: workbenchConfig,
    layout: workbenchLayout,
    loaded: workbenchConfigLoaded,
    update: updateWorkbenchConfig,
    setFields: setWorkbenchFields,
    setLayout: setWorkbenchLayout,
  } = useWorkbenchConfig();
  const [currentTaskId, setCurrentTaskId] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [videoToolSelection, setVideoToolSelectionRaw] = useState<VideoToolSelection>({
    tool: "select",
    scope: "frame",
  });
  const { tool: videoTool, scope: videoToolScope } = videoToolSelection;
  const setVideoToolSelection = useCallback((selection: VideoToolSelection) => {
    const next = {
      tool: selection.tool,
      scope: videoToolScopeForTool(selection.tool) ?? selection.scope,
    };
    setVideoToolSelectionRaw((current) =>
      current.tool === next.tool && current.scope === next.scope ? current : next,
    );
  }, []);
  const setVideoTool = useCallback((nextTool: SetStateAction<VideoTool>) => {
    setVideoToolSelectionRaw((current) => {
      const next = typeof nextTool === "function" ? nextTool(current.tool) : nextTool;
      const scope = videoToolScopeForTool(next) ?? current.scope;
      return current.tool === next && current.scope === scope ? current : { tool: next, scope };
    });
  }, []);
  const [threeDTool, setThreeDTool] = useState<ThreeDTool>("select");
  const [videoFrameIndex, setVideoFrameIndex] = useState(0);
  const [hiddenVideoTrackIds, setHiddenVideoTrackIds] = useState<Set<string>>(() => new Set());
  const [lockedVideoTrackIds, setLockedVideoTrackIds] = useState<Set<string>>(() => new Set());
  const [trackColorOverrides, setTrackColorOverrides] = useState<Record<string, string>>(
    () => ({}),
  );
  const [samPolarity, setSamPolarity] = useState<SamPolarity>("positive");
  const [exemplarOutputMode, setExemplarOutputMode] = useState<TextOutputMode>("mask");
  const [aiToolParams, setAiToolParams] = useState<Record<string, unknown>>({});
  const [aiVariant, setAiVariant] = useState<Record<string, unknown>>({});
  const samSubTool = useMemo(() => toolToSamSubTool(tool), [tool]);
  const [activeClass, setActiveClass] = useState("");
  const [bboxCreationMode, setBboxCreationMode] = useState<BboxCreationMode>("corner");
  const [continuousCreation, setContinuousCreation] = useState<ContinuousImageCreation | null>(
    null,
  );
  const [pendingDrawing, setPendingDrawing] = useState<PendingDrawing>(null);
  const [editingClass, setEditingClass] = useState<EditingClass>(null);
  const [selectedId, setSelectedIdRaw] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [confThreshold, setConfThreshold] = useState(0.5);
  const layoutTouchedRef = useRef({
    leftOpen: false,
    rightOpen: false,
    attrPanelCollapsed: false,
    aiSectionCollapsed: false,
    manualSectionCollapsed: false,
    trackSectionCollapsed: false,
    discussionCollapsed: false,
  });
  const [leftOpen, setLeftOpenRaw] = useState(workbenchLayout.leftOpen);
  const [rightOpen, setRightOpenRaw] = useState(workbenchLayout.rightOpen);
  const [attrPanelCollapsed, setAttrPanelCollapsedRaw] = useState(
    workbenchLayout.attrPanelCollapsed,
  );
  const [aiSectionCollapsed, setAiSectionCollapsedRaw] = useState(
    workbenchLayout.aiSectionCollapsed,
  );
  const [manualSectionCollapsed, setManualSectionCollapsedRaw] = useState(
    workbenchLayout.manualSectionCollapsed,
  );
  const [trackSectionCollapsed, setTrackSectionCollapsedRaw] = useState(
    workbenchLayout.trackSectionCollapsed,
  );
  const [discussionCollapsed, setDiscussionCollapsedRaw] = useState(
    workbenchLayout.discussionCollapsed,
  );

  useEffect(() => {
    if (!workbenchConfigLoaded) return;
    if (!layoutTouchedRef.current.leftOpen) setLeftOpenRaw(workbenchLayout.leftOpen);
    if (!layoutTouchedRef.current.rightOpen) setRightOpenRaw(workbenchLayout.rightOpen);
    if (!layoutTouchedRef.current.attrPanelCollapsed)
      setAttrPanelCollapsedRaw(workbenchLayout.attrPanelCollapsed);
    if (!layoutTouchedRef.current.aiSectionCollapsed)
      setAiSectionCollapsedRaw(workbenchLayout.aiSectionCollapsed);
    if (!layoutTouchedRef.current.manualSectionCollapsed)
      setManualSectionCollapsedRaw(workbenchLayout.manualSectionCollapsed);
    if (!layoutTouchedRef.current.trackSectionCollapsed)
      setTrackSectionCollapsedRaw(workbenchLayout.trackSectionCollapsed);
    if (!layoutTouchedRef.current.discussionCollapsed)
      setDiscussionCollapsedRaw(workbenchLayout.discussionCollapsed);
  }, [
    workbenchLayout.leftOpen,
    workbenchLayout.rightOpen,
    workbenchLayout.attrPanelCollapsed,
    workbenchLayout.aiSectionCollapsed,
    workbenchLayout.manualSectionCollapsed,
    workbenchLayout.trackSectionCollapsed,
    workbenchLayout.discussionCollapsed,
    workbenchConfigLoaded,
  ]);

  const setLeftOpen = useCallback(
    (open: boolean) => {
      layoutTouchedRef.current.leftOpen = true;
      setLeftOpenRaw(open);
      setWorkbenchLayout({ leftOpen: open });
    },
    [setWorkbenchLayout],
  );

  const setRightOpen = useCallback(
    (open: boolean) => {
      layoutTouchedRef.current.rightOpen = true;
      setRightOpenRaw(open);
      setWorkbenchLayout({ rightOpen: open });
    },
    [setWorkbenchLayout],
  );

  const setAttrPanelCollapsed = useCallback(
    (collapsed: boolean) => {
      layoutTouchedRef.current.attrPanelCollapsed = true;
      setAttrPanelCollapsedRaw(collapsed);
      setWorkbenchLayout({ attrPanelCollapsed: collapsed });
    },
    [setWorkbenchLayout],
  );

  const setAiSectionCollapsed = useCallback(
    (collapsed: boolean) => {
      layoutTouchedRef.current.aiSectionCollapsed = true;
      setAiSectionCollapsedRaw(collapsed);
      setWorkbenchLayout({ aiSectionCollapsed: collapsed });
    },
    [setWorkbenchLayout],
  );

  const setManualSectionCollapsed = useCallback(
    (collapsed: boolean) => {
      layoutTouchedRef.current.manualSectionCollapsed = true;
      setManualSectionCollapsedRaw(collapsed);
      setWorkbenchLayout({ manualSectionCollapsed: collapsed });
    },
    [setWorkbenchLayout],
  );

  const setTrackSectionCollapsed = useCallback(
    (collapsed: boolean) => {
      layoutTouchedRef.current.trackSectionCollapsed = true;
      setTrackSectionCollapsedRaw(collapsed);
      setWorkbenchLayout({ trackSectionCollapsed: collapsed });
    },
    [setWorkbenchLayout],
  );

  const setDiscussionCollapsed = useCallback(
    (collapsed: boolean) => {
      layoutTouchedRef.current.discussionCollapsed = true;
      setDiscussionCollapsedRaw(collapsed);
      setWorkbenchLayout({ discussionCollapsed: collapsed });
    },
    [setWorkbenchLayout],
  );

  const toggleHiddenVideoTrack = useCallback((trackId: string) => {
    setHiddenVideoTrackIds((prev) => {
      const next = new Set(prev);
      if (next.has(trackId)) next.delete(trackId);
      else next.add(trackId);
      return next;
    });
  }, []);
  const toggleLockedVideoTrack = useCallback((trackId: string) => {
    setLockedVideoTrackIds((prev) => {
      const next = new Set(prev);
      if (next.has(trackId)) next.delete(trackId);
      else next.add(trackId);
      return next;
    });
  }, []);
  const setVideoTrackColor = useCallback((trackId: string, color: string | null) => {
    setTrackColorOverrides((prev) => {
      if (color === null) {
        if (!(trackId in prev)) return prev;
        const next = { ...prev };
        delete next[trackId];
        return next;
      }
      if (prev[trackId] === color) return prev;
      return { ...prev, [trackId]: color };
    });
  }, []);
  const resetVideoStageUi = useCallback(() => {
    setVideoFrameIndex(0);
    setHiddenVideoTrackIds(new Set());
    setLockedVideoTrackIds(new Set());
    setTrackColorOverrides({});
  }, []);
  const [clipboard, setClipboard] = useState<Annotation[]>([]);
  const [canvasDraft, setCanvasDraft] = useState<CanvasDraft>({
    active: false,
    annotationId: null,
    shapes: [],
    stroke: DEFAULT_CANVAS_STROKE,
    pendingResult: null,
  });

  const beginCanvasDraft = useCallback(
    (
      annotationId: string | null,
      initial?: CommentCanvasDrawing | null,
      origin?: DiscussionOrigin,
    ) => {
      if (
        origin &&
        ((origin.target.kind === "annotation" && origin.target.annotationId !== annotationId) ||
          (origin.target.kind === "task" && annotationId !== null) ||
          origin.target.kind === "issue")
      ) {
        return;
      }
      setCanvasDraft({
        active: true,
        annotationId,
        shapes: initial?.shapes ?? [],
        stroke: DEFAULT_CANVAS_STROKE,
        pendingResult: null,
        origin: origin ?? null,
        resultId: null,
        initialDrawing: initial ?? null,
      });
      setTool("canvas");
    },
    [],
  );

  const appendCanvasShape = useCallback((shape: CanvasDraft["shapes"][number]) => {
    setCanvasDraft((d) => (d.active ? { ...d, shapes: [...d.shapes, shape] } : d));
  }, []);

  const undoCanvasShape = useCallback(() => {
    setCanvasDraft((d) => ({ ...d, shapes: d.shapes.slice(0, -1) }));
  }, []);

  const clearCanvasShapes = useCallback(() => {
    setCanvasDraft((d) => ({ ...d, shapes: [] }));
  }, []);

  const setCanvasStroke = useCallback((stroke: string) => {
    setCanvasDraft((d) => ({ ...d, stroke }));
  }, []);

  const endCanvasDraft = useCallback(() => {
    const resultId = randomId();
    setCanvasDraft((d) =>
      d.active
        ? {
            ...d,
            active: false,
            shapes: [],
            pendingResult: d.shapes.length > 0 ? { shapes: d.shapes } : { shapes: [] },
            resultId,
          }
        : d,
    );
    setTool("box");
  }, []);

  const cancelCanvasDraft = useCallback(() => {
    const resultId = randomId();
    setCanvasDraft((d) =>
      d.origin && d.active
        ? {
            ...d,
            active: false,
            shapes: [],
            pendingResult: d.initialDrawing ?? { shapes: [] },
            resultId,
          }
        : {
            active: false,
            annotationId: null,
            shapes: [],
            stroke: DEFAULT_CANVAS_STROKE,
            pendingResult: null,
            origin: null,
            resultId: null,
          },
    );
    setTool("box");
  }, []);

  const releaseCanvasDraft = useCallback(() => {
    setCanvasDraft({
      active: false,
      annotationId: null,
      shapes: [],
      stroke: DEFAULT_CANVAS_STROKE,
      pendingResult: null,
      origin: null,
      resultId: null,
    });
    setTool((tool) => (tool === "canvas" ? "box" : tool));
  }, []);

  const consumeCanvasResult = useCallback((resultId?: string) => {
    setCanvasDraft((d) => {
      if (d.origin && (!resultId || resultId !== d.resultId)) return d;
      if (resultId && resultId !== d.resultId) return d;
      return { ...d, pendingResult: null, annotationId: null, origin: null, resultId: null };
    });
  }, []);

  const setSelectedId = useCallback((id: string | null) => {
    setSelectedIdRaw(id);
    setSelectedIds(id ? [id] : []);
  }, []);

  const toggleSelected = useCallback((id: string) => {
    setSelectedIds((prev) => {
      if (prev.includes(id)) {
        const next = prev.filter((x) => x !== id);
        setSelectedIdRaw(next[next.length - 1] ?? null);
        return next;
      }
      const next = [...prev, id];
      setSelectedIdRaw(id);
      return next;
    });
  }, []);

  const replaceSelected = useCallback((ids: string[]) => {
    setSelectedIds(ids);
    setSelectedIdRaw(ids[ids.length - 1] ?? null);
  }, []);

  return {
    currentTaskId,
    setCurrentTaskId,
    tool,
    setTool,
    videoTool,
    setVideoTool,
    videoToolScope,
    setVideoToolSelection,
    threeDTool,
    setThreeDTool,
    videoFrameIndex,
    setVideoFrameIndex,
    hiddenVideoTrackIds,
    lockedVideoTrackIds,
    toggleHiddenVideoTrack,
    toggleLockedVideoTrack,
    resetVideoStageUi,
    trackColorOverrides,
    setVideoTrackColor,
    samSubTool,
    samPolarity,
    setSamPolarity,
    exemplarOutputMode,
    setExemplarOutputMode,
    aiToolParams,
    setAiToolParams,
    aiVariant,
    setAiVariant,
    activeClass,
    continuousCreation,
    bboxCreationMode,
    setBboxCreationMode,
    setContinuousCreation,
    setActiveClass,
    pendingDrawing,
    setPendingDrawing,
    editingClass,
    setEditingClass,
    selectedId,
    setSelectedId,
    selectedIds,
    toggleSelected,
    replaceSelected,
    confThreshold,
    setConfThreshold,
    leftOpen,
    setLeftOpen,
    rightOpen,
    setRightOpen,
    attrPanelCollapsed,
    setAttrPanelCollapsed,
    aiSectionCollapsed,
    setAiSectionCollapsed,
    manualSectionCollapsed,
    setManualSectionCollapsed,
    trackSectionCollapsed,
    setTrackSectionCollapsed,
    discussionCollapsed,
    setDiscussionCollapsed,
    workbenchConfig,
    workbenchConfigLoaded,
    updateWorkbenchConfig,
    setWorkbenchFields,
    workbenchLayout,
    setWorkbenchLayout,
    clipboard,
    setClipboard,
    canvasDraft,
    beginCanvasDraft,
    endCanvasDraft,
    cancelCanvasDraft,
    releaseCanvasDraft,
    appendCanvasShape,
    undoCanvasShape,
    clearCanvasShapes,
    setCanvasStroke,
    consumeCanvasResult,
  };
}

export type WorkbenchState = ReturnType<typeof useWorkbenchState>;
