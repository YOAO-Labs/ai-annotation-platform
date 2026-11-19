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

// Tool union 扩展: 旧 "sam" 拆为 4 个独立 AI 工具 (smart-point / smart-box /
// text-prompt / exemplar), 每个绑定一个 prompt 范式. 状态层仅保留 polarity (smart-point
// 用) 和 aiToolParams (InteractiveToolBar 用); samSubTool 由 tool 派生, 不再独立持有.
export type Tool =
  // 选择工具：点选 / 移动已有标注与预标注；默认工具，ESC 回退到它。
  | "select"
  | "box"
  // 旋转框 (OBB): 先拖轴对齐矩形, 提交后用旋转手柄调角度.
  | "rotated-box"
  | "hand"
  | "polygon"
  // 折线（开放、不闭合）。
  | "polyline"
  | "canvas"
  | "mask"
  | "smart-point"
  | "smart-box"
  | "smart-scribble"
  | "text-prompt"
  | "exemplar"
  // 复用 SAM bbox prompt 返回的 mask, 取紧凑外接矩形落 bbox 标注 (Magic Box).
  | "magic-box"
  // 关键点 (COCO 范式).
  | "keypoint";
// 视频工具栏保留选择与创建工具；平移不再作为 VideoTool 模式。
// 单帧 vs 轨迹拆成独立工具 (对齐 box=单帧 / track=轨迹 先例):
//   polygon / polyline        = 点击落点画单帧 video_polygon / video_polyline;
//     polygon-track / polyline-track = 绘制 polygon/polyline 轨迹关键帧。
// 四者都走点击累加顶点绘制 (与 box/track 拖拽绘制正交)。
// 交互式 SAM 单帧工具 (smart-point / smart-box): 在当前帧点/框提示后端,
//   候选采纳后落单帧 video_polygon。与图片侧同名工具语义一致, 但底图走当前帧 JPEG。
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
// 点云 3D 工作台工具态(双栈隔离,不复用 2D ToolId)。
export type ThreeDTool = "select" | "box" | "point-mask" | "measure";

/**
 * 派生型 SAM 子工具, 仅作 ImageStage / AIInspectorPanel 等老消费者的兼容外观.
 * 取值由 tool 决定; tool 不是 AI 工具时为 null.
 */
export type SamSubTool = "point" | "bbox" | "scribble" | "text" | "exemplar";

/** SAM-point 子工具下的 polarity, "+" / "-" 键切换; 仅 smart-point 时有意义. */
export type SamPolarity = "positive" | "negative";

/** 由 tool 派生 samSubTool, 给老消费者用 (ImageStage / AIInspectorPanel). */
export function toolToSamSubTool(tool: Tool): SamSubTool | null {
  switch (tool) {
    case "smart-point":
      return "point";
    case "smart-box":
      return "bbox";
    case "smart-scribble":
      return "scribble";
    // Magic Box 也是 bbox prompt; 共用 InteractiveToolBar 的 bbox subtool UI.
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

/** canvas 工具激活时的草稿状态。
 *  CommentInput 点「在题图上绘制」→ beginCanvasDraft；ImageStage 在 canvas tool 下
 *  读取 active + shapes 渲染 + 写入新笔触；用户点 Done → endCanvasDraft 把结果
 *  挂到 pendingResult，CommentInput 监听后回写并清空。 */
export type CanvasDraft = {
  active: boolean;
  /** 关联的评论上下文（可选；用于多 Annotation 上下文时区分）。 */
  annotationId: string | null;
  shapes: NonNullable<CommentCanvasDrawing["shapes"]>;
  stroke: string;
  /** 提交后由 hook 写入；CommentInput effect 消费后清空。 */
  pendingResult: CommentCanvasDrawing | null;
  /** The composer that started this drawing, independent of later selection. */
  origin?: DiscussionOrigin | null;
  /** A completion can only be acknowledged by its matching consumer. */
  resultId?: string | null;
  /** Cancelling live editing restores the attachment that existed at entry. */
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

/** 选中已落库 user 框后，再次"改类别"时的状态。 */
export type EditingClass = {
  annotationId: string;
  geom: Geom;
  currentClass: string;
  /** 已落库标注自身的工具单位；改类/属性不得复用当前激活工具的类别域。 */
  toolUnitId?: string;
  anchor?: { left: number; top: number };
  // 采纳时选类: 非空时该弹窗不是"改已存标注的类", 而是"为采纳某预测选项目标签",
  // commit 时走 accept(override_class_name) 而非 update(class_name). 复用同一 ClassPickerPopover.
  // B-57 · toolUnitId: 预测自身的工具单位 (如 polygon→region), 让 popover 按它取类别集合,
  // 而非当前激活工具 (bbox) 的集合 — 否则采纳多边形时只列出矩形框的类, 选不到正确类别。
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
  // session 级 track 颜色覆盖：trackId -> oklch 颜色字符串（取色器写入）。
  const [trackColorOverrides, setTrackColorOverrides] = useState<Record<string, string>>(
    () => ({}),
  );
  // samSubTool 改为派生 (见 toolToSamSubTool); polarity + aiToolParams 仍是 state.
  const [samPolarity, setSamPolarity] = useState<SamPolarity>("positive");
  // exemplar 子工具输出形态 (box/mask/both); 会话级.
  const [exemplarOutputMode, setExemplarOutputMode] = useState<TextOutputMode>("mask");
  /** InteractiveToolBar 维护的后端参数 (来自 /setup.params schema). 切换工具时重置. */
  const [aiToolParams, setAiToolParams] = useState<Record<string, unknown>>({});
  // 模型变体 (sam_variant / dino_variant) 从 aiToolParams 拆出: 落点 AI 面板, 会话级,
  // 切工具不重置 (设置型). 值在 run 时合进 context (链路见 useInteractiveAI.extraParams).
  const [aiVariant, setAiVariant] = useState<Record<string, unknown>>({});
  const samSubTool = useMemo(() => toolToSamSubTool(tool), [tool]);
  /**
   * activeClass 语义：默认/最近使用类别。仅作为绘制时浮框颜色预览 + popover 的默认选中。
   * 实际类别在画完框 → ClassPickerPopover 中确认。
   */
  const [activeClass, setActiveClass] = useState("");
  const [bboxCreationMode, setBboxCreationMode] = useState<BboxCreationMode>("corner");
  const [continuousCreation, setContinuousCreation] = useState<ContinuousImageCreation | null>(
    null,
  );
  const [pendingDrawing, setPendingDrawing] = useState<PendingDrawing>(null);
  const [editingClass, setEditingClass] = useState<EditingClass>(null);
  /**
   * 多选语义：
   * - selectedId：primary（用于 SelectionOverlay 浮按钮锚点 / 单体快捷键）
   * - selectedIds：包含 primary 在内的全部选中 user 框 id
   * 选 AI 框时永远是单选（selectedIds 只含一个 ai id）
   */
  const [selectedId, setSelectedIdRaw] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  // confThreshold: AI 框前端展示阈值 (b.conf >= confThreshold 才进 aiBoxes).
  // 注意: 这是前端过滤, 不重跑模型; "全部采纳"也只会采纳过滤后还显示的框.
  // 改 DINO 召回阈值要去 项目设置 → AI 配置 → box_threshold / text_threshold.
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
  // 右栏属性区折叠态: 走 workbench.layout 服务端偏好, 与 leftOpen/rightOpen 同套持久。
  const [attrPanelCollapsed, setAttrPanelCollapsedRaw] = useState(
    workbenchLayout.attrPanelCollapsed,
  );
  // AI 待审 / 人工分组折叠 + 讨论区完全收起 (照抄 attrPanelCollapsed 范式)。
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
  /** 同任务内剪贴板（仅本会话内存）。 */
  const [clipboard, setClipboard] = useState<Annotation[]>([]);
  /** canvas 批注草稿。reviewer / annotator 在题图上画红圈时使用。*/
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

  /** 提交：把当前 shapes 打包到 pendingResult，CommentInput 消费后清空。 */
  const endCanvasDraft = useCallback(() => {
    const resultId = randomId();
    setCanvasDraft((d) =>
      d.active
        ? {
            ...d,
            active: false,
            // 打包进 pendingResult 后立即清空 shapes，否则草稿笔触会一直残留在题图上
            // （CanvasDrawingLayer 始终渲染 shapes，与 active 无关），直到下次绘制或刷新才消失。
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

  /** The original draft has already been suspended by the lifecycle owner. */
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

  /** 设置 primary，同时把 selectedIds 收敛到 [id] 或 []。 */
  const setSelectedId = useCallback((id: string | null) => {
    setSelectedIdRaw(id);
    setSelectedIds(id ? [id] : []);
  }, []);

  /** 切换某 id 的选中态（Shift+click 用）。primary 跟随最后一次切入的 id。 */
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

  /** 替换全部选中 id（Ctrl+A 用）。 */
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
    // 派生 samSubTool (read-only) + polarity + AI 工具参数.
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
