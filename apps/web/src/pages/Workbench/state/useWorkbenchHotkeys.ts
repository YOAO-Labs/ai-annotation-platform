// v0.6.4 P1：从 WorkbenchShell 拆出的键盘快捷键接线层。
//
// 集中管理：
//  - polygon 专用键（Enter / Esc / Backspace，capture 阶段拦截）
//  - 主 keydown 通过 dispatchKey 路由所有 action
//  - keyup 释放空格 / 方向键时 flush nudge
//  - 方向键 nudge 临时几何 override（state 与 ref 共用）
//
// 不在这里管的：dispatchKey 自身（state/hotkeys.ts，纯函数不动）、
// AnnotationActions handler（state/useWorkbenchAnnotationActions.ts）。

import { useCallback, useEffect, useRef, useState } from "react";
import type { PolygonDraftHandle } from "../stage/tools";
import { isWorkbenchInteractionBlocked } from "./workbenchInteractionGuards";

import {
  dispatchKey,
  ARROW_KEY_SET,
  hotkeyIgnoreToken,
  isMaskContextHotkey,
  isMaskHotkeyBlocked,
  isSamCandidateHotkeyBlocked,
  type HotkeyStage,
} from "./hotkeys";
import { createCommandEventMatcher, type EffectiveCommandState } from "./hotkeyBindings";
import { nextInCategory, nextCategory } from "../stage/frameObjectCycle";
import { aiBoxOnFrame } from "../stage/aiBoxFrames";
import type { UseMaskEditorReturn } from "./useMaskEditor";
import { canEditMask } from "./canEditMask";
import { bboxGeom } from "./transforms";
import type { useWorkbenchState, VideoTool } from "./useWorkbenchState";
import type { useAnnotationHistory } from "./useAnnotationHistory";
import type { AnnotationResponse, Geometry } from "@/types";
import type { AiBox } from "./transforms";
import type { VideoStageControls } from "../stage/videoStageControls";
import { supportsBBoxNudge } from "../stage/shared/geometry/geometryEditPolicy";

type Geom = { x: number; y: number; w: number; h: number };

interface ToastInput {
  msg: string;
  sub?: string;
  kind?: "success" | "warning" | "error" | "";
}

interface ProjectAttributeSchemaLite {
  attribute_schema?: {
    fields?: {
      key: string;
      type: string;
      hotkey?: string | null;
      applies_to?: unknown;
      options?: { value: string; label: string }[] | null;
    }[];
  } | null;
}

interface ClipboardLike {
  hasClipboard: boolean;
  copySelection: () => number;
  paste: () => Promise<string[]>;
  duplicateSelection: () => Promise<string[]>;
}

interface UpdateMutationLike {
  mutate: (vars: { annotationId: string; payload: { geometry: Geometry } }) => void;
}

export interface UseWorkbenchHotkeysArgs {
  s: ReturnType<typeof useWorkbenchState>;
  history: ReturnType<typeof useAnnotationHistory>;
  classes: string[];
  currentProject: ProjectAttributeSchemaLite | null | undefined;
  annotationsRef: { current: AnnotationResponse[] };
  batchChanging: boolean;
  /** SAM and other class pickers that keep their draft outside ordinary editingClass. */
  classPickerActive?: boolean;
  setBatchChanging: React.Dispatch<React.SetStateAction<boolean>>;
  cancelPendingDrawing?: () => void;
  showHotkeys: boolean;

  // navigation / task helpers
  navigateTask: (dir: "next" | "prev") => void;
  smartNext: (mode: "open" | "uncertain") => void;
  setFitTick: React.Dispatch<React.SetStateAction<number>>;
  // v0.14.1 · 跨帧目标延续 (Alt+→ / Alt+←); 未提供则该键无动作。
  onCrossFramePropagate?: (dir: "next" | "prev") => void;

  // class / attribute / annotation actions
  recordRecentClass: (cls: string) => void;
  handleDeleteBox: (id: string) => void;
  handleBatchDelete: () => void;
  /** v0.10.5 M4-β · I15 shape 状态位字段级 PATCH（lock/hidden/z_order）。 */
  handlePatchShapeFlag?: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  handleStartChangeClass: (id: string) => void;
  handleStartBatchChangeClass: () => void;
  handleSubmitTask: () => void;
  handleAcceptPrediction: (b: AiBox) => void;
  handleRejectPrediction?: (b: AiBox) => void;
  handleUpdateAttributes: (id: string, attrs: Record<string, unknown>) => void;
  handleVideoSetSelectedClass?: (className: string) => boolean;

  // ai
  aiBoxes: AiBox[];

  // ui state setters
  setShowHotkeys: React.Dispatch<React.SetStateAction<boolean>>;

  // clipboard
  clipboard: ClipboardLike;

  // toast
  pushToast: (toast: ToastInput) => void;

  // stage geom for nudge calc
  stageGeom: { imgW: number; imgH: number };

  // polygon hookup（来自 AnnotationActions hook）
  polygonDraftPoints: [number, number][];
  polygonDraft?: PolygonDraftHandle;
  setPolygonDraftPoints: React.Dispatch<React.SetStateAction<[number, number][]>>;
  submitPolygon: (points: [number, number][]) => void;
  // v0.10.28 · polyline 复用同一草稿 state，Enter 阈值为 2 顶点。
  submitPolyline: (points: [number, number][]) => void;
  cancelManualDrawing?: () => boolean;

  // nudge 提交所用 mutation
  updateMutation: UpdateMutationLike;

  // 切题（清 nudge）
  taskId: string | undefined;

  disabled?: boolean;
  ignoredKeys?: Set<string>;
  videoMode?: boolean;
  /** Actual stage, so image overrides cannot reach a 3D selection. */
  stage?: HotkeyStage;
  requestVideoTool?: (tool: VideoTool) => void;
  /** v0.10.29 · 视频采样网格生效 (step>1) 时改写 ←/→ 键位；step=1 维持现状。 */
  samplingActive?: boolean;
  videoControlsRef?: React.RefObject<VideoStageControls | null>;
  /** v0.10.2 · 由 useMLCapabilities 透传; S 键循环 AI 工具时用来跳过置灰. */
  isPromptSupported?: (type: string) => boolean;
  /** 项目级「交互式 AI 工具」总开关; false 时 AI 工具 hotkey no-op (与 ToolDock 隐藏一致)。 */
  aiInteractiveEnabled?: boolean;
  /** 与 ToolDock 共用的 Mask 禁用原因；存在时 M 热键不进入 Mask。 */
  maskToolDisabledReason?: string;
  /** v0.10.8 · mask 工具激活时的 B/E/Enter/Esc 上下文键由这组 callback 消费。 */
  maskEditor?: UseMaskEditorReturn;
  onMaskPrimaryAction?: () => void;
  onMaskSecondaryAction?: () => void;
  /** v0.23.5 · WS-C · task 级只读 (review/completed 锁), 供 canEditMask 判定 B/E 是否可用。 */
  maskTaskReadOnly?: boolean;
  /** 分块缓存超预算时停止像素编辑，但保留已有草稿的保存入口。 */
  maskPixelReadOnly?: boolean;
  /** 证据对比期间冻结所有 Mask 编辑快捷键，包括取消草稿。 */
  maskInteractionFrozen?: boolean;
  /** Increment B · 账号级快捷键覆盖解析出的生效命令表（含默认值）。 */
  shortcutsEffective?: Map<string, EffectiveCommandState>;
}

export interface UseWorkbenchHotkeysReturn {
  spacePan: boolean;
  markSpacePanDrag: () => void;
  nudgeMap: Map<string, Geom>;
  flushNudges: () => void;
}

export function isWorkbenchInputFocused(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (
    el instanceof HTMLInputElement &&
    el.type === "range" &&
    el.classList.contains("video-timeline-range")
  ) {
    return false;
  }
  return (
    el.tagName === "INPUT" ||
    el.tagName === "TEXTAREA" ||
    el.tagName === "SELECT" ||
    el.isContentEditable ||
    Boolean(
      el.closest(
        '[data-workbench-layout-control], [data-scene-timeline], [role="tab"], [role="menu"], [role="menuitem"], [role="combobox"], [role="listbox"]',
      ),
    )
  );
}

/** AI 工具 (requiredPrompt) 的 hotkey 目标 id; 项目关闭交互式 AI 时这些 hotkey no-op。 */
const AI_TOOL_HOTKEY_IDS = new Set(["smart-point", "smart-box", "magic-box", "exemplar"]);

export function useWorkbenchHotkeys(args: UseWorkbenchHotkeysArgs): UseWorkbenchHotkeysReturn {
  const {
    s,
    history,
    classes,
    currentProject,
    annotationsRef,
    batchChanging,
    classPickerActive = false,
    setBatchChanging,
    cancelPendingDrawing,
    showHotkeys,
    navigateTask,
    smartNext,
    setFitTick,
    onCrossFramePropagate,
    recordRecentClass,
    handleDeleteBox,
    handleBatchDelete,
    handlePatchShapeFlag,
    handleStartChangeClass,
    handleStartBatchChangeClass,
    handleSubmitTask,
    handleAcceptPrediction,
    handleRejectPrediction,
    handleUpdateAttributes,
    handleVideoSetSelectedClass,
    aiBoxes,
    setShowHotkeys,
    clipboard,
    pushToast,
    stageGeom,
    polygonDraftPoints,
    polygonDraft,
    setPolygonDraftPoints,
    submitPolygon,
    submitPolyline,
    cancelManualDrawing,
    updateMutation,
    taskId,
    disabled = false,
    ignoredKeys,
    videoMode = false,
    stage = videoMode ? "video" : "image",
    requestVideoTool,
    samplingActive = false,
    videoControlsRef,
    isPromptSupported,
    aiInteractiveEnabled,
    maskToolDisabledReason,
    maskEditor,
    onMaskPrimaryAction,
    onMaskSecondaryAction,
    maskTaskReadOnly = false,
    maskPixelReadOnly = false,
    maskInteractionFrozen = false,
    shortcutsEffective,
  } = args;

  const [spacePan, setSpacePan] = useState(false);
  const videoSpaceDownRef = useRef(false);
  const videoSpaceDraggedRef = useRef(false);
  const [nudgeMap, setNudgeMap] = useState<Map<string, Geom>>(new Map());
  const nudgeOrigRef = useRef<Map<string, Geom>>(new Map());
  // Increment B · 运行期绑定冲突每会话每按键只上报一次。
  const reportedConflictsRef = useRef(new Set<string>());

  const reportBindingConflict = useCallback(
    (e: KeyboardEvent) => {
      const token = `${e.ctrlKey || e.metaKey ? "Mod+" : ""}${e.altKey ? "Alt+" : ""}${
        e.shiftKey ? "Shift+" : ""
      }${e.key}`;
      if (reportedConflictsRef.current.has(token)) return;
      reportedConflictsRef.current.add(token);
      pushToast({
        msg: `快捷键 ${token} 存在重复绑定，本次未执行`,
        sub: "可在快捷键面板中检查并修正冲突的组合。",
        kind: "warning",
      });
    },
    [pushToast],
  );

  const markSpacePanDrag = useCallback(() => {
    if (videoSpaceDownRef.current) videoSpaceDraggedRef.current = true;
  }, []);

  // 切题清空 nudge
  useEffect(() => {
    setNudgeMap(new Map());
    nudgeOrigRef.current = new Map();
  }, [taskId]);

  const flushNudges = useCallback(() => {
    if (nudgeMap.size === 0) return;
    const cmds: {
      kind: "update";
      annotationId: string;
      before: { geometry: ReturnType<typeof bboxGeom> };
      after: { geometry: ReturnType<typeof bboxGeom> };
    }[] = [];
    nudgeMap.forEach((after, id) => {
      const before = nudgeOrigRef.current.get(id);
      if (!before) return;
      if (
        before.x === after.x &&
        before.y === after.y &&
        before.w === after.w &&
        before.h === after.h
      )
        return;
      const beforeG = bboxGeom(before);
      const afterG = bboxGeom(after);
      updateMutation.mutate({ annotationId: id, payload: { geometry: afterG } });
      cmds.push({
        kind: "update",
        annotationId: id,
        before: { geometry: beforeG },
        after: { geometry: afterG },
      });
    });
    if (cmds.length > 0) history.pushBatch(cmds);
    setNudgeMap(new Map());
    nudgeOrigRef.current = new Map();
  }, [nudgeMap, updateMutation, history]);

  // Disabled listeners cannot receive the held key's release. Settle it before detaching.
  useEffect(() => {
    if (!disabled) return;
    setSpacePan(false);
    videoSpaceDownRef.current = false;
    videoSpaceDraggedRef.current = false;
    flushNudges();
  }, [disabled, flushNudges]);

  // polygon / polyline 专用键：Enter / Esc / Backspace
  // v0.10.28 · 两者共用 polygonDraftPoints 草稿；polygon Enter 需 ≥3 顶点（闭合），
  //            polyline Enter 需 ≥2 顶点（不闭合），分别走 submitPolygon / submitPolyline。
  useEffect(() => {
    if (disabled) return;
    if (s.tool !== "polygon" && s.tool !== "polyline") return;
    const isPolyline = s.tool === "polyline";
    const minPts = isPolyline ? 2 : 3;
    const onKey = (e: KeyboardEvent) => {
      if (isWorkbenchInteractionBlocked(e)) return;
      const t = e.target;
      if (
        t instanceof HTMLElement &&
        (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)
      )
        return;
      if (polygonDraft?.beforeInput?.current?.(e)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        return;
      }
      if (e.key === "Enter" || e.key === "Backspace")
        polygonDraft?.autoPoints?.beforeKey.current?.();
      const points = polygonDraft?.autoPoints?.getPoints() ?? polygonDraftPoints;
      if (points.length === 0) return;
      if (e.key === "Enter" && points.length >= minPts) {
        e.preventDefault();
        e.stopPropagation();
        if (isPolyline) submitPolyline(points);
        else submitPolygon(points);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setPolygonDraftPoints([]);
        return;
      }
      if (e.key === "Backspace") {
        e.preventDefault();
        e.stopPropagation();
        setPolygonDraftPoints((p) => p.slice(0, -1));
        return;
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [
    disabled,
    s.tool,
    polygonDraft,
    polygonDraftPoints,
    submitPolygon,
    submitPolyline,
    setPolygonDraftPoints,
  ]);

  // v0.10.8 · I11 · Mask 工具专用键（capture 阶段，先于主 dispatchKey 抢键）：
  //   B → brush 模式  · E → erase 模式  · Enter/Esc → 当前阶段主/次动作
  // 仅 tool === "mask" 且 maskEditor 注入时生效；输入聚焦 / pending popover 时让位。
  useEffect(() => {
    if (disabled) return;
    if (s.tool !== "mask") return;
    if (!maskEditor) return;
    const onKey = (e: KeyboardEvent) => {
      if (isMaskHotkeyBlocked(e)) return;
      if (maskInteractionFrozen) return;
      if (s.pendingDrawing || s.editingClass || classPickerActive) return;
      // v0.23.5 · WS-C · B/E 模式切换也经 canEditMask: 锁定对象连切笔刷都不允许,
      // 与 pointer 入口 (MaskTool) 一道关闭锁定绕过。readOnly 来自 task 级 (调用方传入),
      // is_locked 读当前选中 annotation。
      const sel = s.selectedId ? annotationsRef.current.find((a) => a.id === s.selectedId) : null;
      const maskEditable = canEditMask({
        taskReadOnly: !!maskTaskReadOnly || !!maskPixelReadOnly,
        annotationLocked: !!sel?.is_locked,
        trackLocked: false,
        segmentLocked: false,
        editorPhase:
          maskEditor.phase ?? (maskEditor.dirty ? "dirty" : maskEditor.active ? "ready" : "idle"),
      });
      const command = e.ctrlKey || e.metaKey;
      if (command && (e.key.toLowerCase() === "z" || e.key.toLowerCase() === "y")) {
        const isRedo = e.key.toLowerCase() === "y" || e.shiftKey;
        const canHandle = isRedo ? maskEditor.canRedo : maskEditor.canUndo;
        if (!maskEditable || canHandle) {
          e.preventDefault();
          e.stopImmediatePropagation();
          if (!maskEditable) return;
          if (isRedo) maskEditor.redo();
          else maskEditor.undo();
          return;
        }
      }
      if (e.key === "b" || e.key === "B") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (!maskEditable) return;
        maskEditor.setMode("brush");
        return;
      }
      if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (!maskEditable) return;
        maskEditor.setMode("erase");
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        e.stopImmediatePropagation();
        onMaskPrimaryAction?.();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        onMaskSecondaryAction?.();
        return;
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [
    disabled,
    s.tool,
    s.pendingDrawing,
    s.editingClass,
    s.selectedId,
    maskEditor,
    onMaskPrimaryAction,
    onMaskSecondaryAction,
    maskInteractionFrozen,
    classPickerActive,
    maskTaskReadOnly,
    maskPixelReadOnly,
    annotationsRef,
  ]);

  // 主 keydown / keyup
  useEffect(() => {
    if (disabled) return;
    const applyArrowNudge = (dx: number, dy: number) => {
      const userTargets = s.selectedIds
        .map((id) => annotationsRef.current.find((a) => a.id === id))
        .filter(
          (annotation): annotation is AnnotationResponse =>
            !!annotation && supportsBBoxNudge(annotation.geometry),
        );
      if (userTargets.length === 0) return;
      const w = stageGeom.imgW || 1;
      const h = stageGeom.imgH || 1;
      const ndx = dx / w;
      const ndy = dy / h;
      setNudgeMap((prev) => {
        const next = new Map(prev);
        for (const ann of userTargets) {
          const orig = nudgeOrigRef.current.get(ann.id) ?? (ann.geometry as Geom);
          if (!nudgeOrigRef.current.has(ann.id)) nudgeOrigRef.current.set(ann.id, orig);
          const cur = next.get(ann.id) ?? orig;
          next.set(ann.id, {
            x: Math.max(0, Math.min(1 - cur.w, cur.x + ndx)),
            y: Math.max(0, Math.min(1 - cur.h, cur.y + ndy)),
            w: cur.w,
            h: cur.h,
          });
        }
        return next;
      });
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented || isWorkbenchInteractionBlocked(e)) return;
      const activeTool = videoMode ? s.videoTool : s.tool;
      if (
        ["smart-point", "smart-box", "smart-scribble", "text-prompt", "exemplar"].includes(
          activeTool ?? "",
        ) &&
        ["Enter", "Escape", "Tab", "r", "R"].includes(e.key) &&
        isSamCandidateHotkeyBlocked(e)
      )
        return;
      const maskToolActive = videoMode
        ? s.videoTool === "mask" || s.videoTool === "mask-track"
        : s.tool === "mask";
      if (
        maskToolActive &&
        isMaskContextHotkey(e) &&
        (maskInteractionFrozen || isMaskHotkeyBlocked(e))
      )
        return;
      const modifiedToken = hotkeyIgnoreToken(e);
      if (ignoredKeys?.has(e.key) || (modifiedToken && ignoredKeys?.has(modifiedToken))) return;
      const attributeHotkey = (digit: string) => {
        // Increment B · 属性快捷键只在显式聚焦的属性快捷键区域内生效；
        // 仅选中标注不再把画布数字键改成属性编辑。
        const active = document.activeElement;
        if (!(active instanceof Element && active.closest("[data-attribute-shortcut-region]"))) {
          return null;
        }
        const sel = s.selectedId;
        if (!sel) return null;
        const ann = annotationsRef.current.find((a) => a.id === sel);
        if (!ann) return null;
        const fields = currentProject?.attribute_schema?.fields ?? [];
        for (const f of fields) {
          if (f.hotkey !== digit) continue;
          if (f.type !== "boolean" && f.type !== "select") continue;
          const applies = f.applies_to;
          if (Array.isArray(applies) && !applies.includes(ann.class_name)) continue;
          const cur = (ann.attributes ?? {})[f.key];
          if (f.type === "boolean") {
            return { key: f.key, type: "boolean" as const, currentValue: cur };
          }
          const opts = (f.options ?? []).map((o) => o.value);
          return { key: f.key, type: "select" as const, options: opts, currentValue: cur };
        }
        return null;
      };

      // Increment B · 生效绑定匹配器（每事件一次；未挂载偏好时走 dispatchKey 默认表）。
      const matcher = shortcutsEffective
        ? createCommandEventMatcher(e, shortcutsEffective, stage)
        : null;

      const action = dispatchKey(e, {
        isInputFocused: isWorkbenchInputFocused(e.target),
        hasSelection: !!s.selectedId || s.selectedIds.length > 0,
        pendingActive: !!s.pendingDrawing || !!s.editingClass || batchChanging || classPickerActive,
        attributeHotkey,
        videoMode,
        stage,
        matchCommand: matcher?.match,
        commandBindingConflict: matcher?.conflict ?? false,
        onCommandBindingConflict: () => reportBindingConflict(e),
        selectedPrediction:
          aiBoxes.find(
            (box) =>
              box.id === s.selectedId && (!videoMode || aiBoxOnFrame(box, s.videoFrameIndex)),
          ) ?? null,
        samplingActive,
        hasSelectedVideoTrack:
          videoMode &&
          !!s.selectedId &&
          annotationsRef.current.some(
            (ann) =>
              ann.id === s.selectedId &&
              (ann.geometry.type === "video_track_bbox" ||
                ann.geometry.type === "video_track_mask"),
          ),
      });
      if (!action) return;

      switch (action.type) {
        case "undo":
          e.preventDefault();
          history.undo();
          return;
        case "redo":
          e.preventDefault();
          history.redo();
          return;
        case "fitReset":
          e.preventDefault();
          setFitTick((n) => n + 1);
          return;
        case "navigateTask":
          e.preventDefault();
          navigateTask(action.dir);
          return;
        case "crossFramePropagate":
          e.preventDefault();
          // v0.14.1 · 阻断按住 Alt+→ 的 auto-repeat: 否则连发多个 propagate POST,
          // 在目标帧造出共享同一新 track_id 的重复 annotation。
          if (e.repeat) return;
          onCrossFramePropagate?.(action.dir);
          return;

        case "videoTogglePlayback":
          e.preventDefault();
          videoControlsRef?.current?.togglePlayback();
          return;
        case "videoSpaceDown":
          e.preventDefault();
          if (!e.repeat) {
            videoSpaceDownRef.current = true;
            videoSpaceDraggedRef.current = false;
          }
          setSpacePan(true);
          return;
        case "videoJogPlayback":
          e.preventDefault();
          videoControlsRef?.current?.jogPlayback(action.dir);
          return;
        case "videoPausePlayback":
          e.preventDefault();
          videoControlsRef?.current?.pausePlayback();
          return;
        case "videoSeek":
          e.preventDefault();
          videoControlsRef?.current?.seekByFrames(action.delta);
          return;
        case "videoSeekGrid":
          e.preventDefault();
          videoControlsRef?.current?.seekGrid(action.dir);
          return;
        case "videoMicroStep":
          e.preventDefault();
          videoControlsRef?.current?.microStep(action.dir);
          return;
        case "videoSeekKeyframe":
          e.preventDefault();
          videoControlsRef?.current?.seekToKeyframe(action.dir);
          return;
        case "videoToggleBookmark":
          e.preventDefault();
          videoControlsRef?.current?.toggleBookmark();
          return;
        case "videoToggleOutside":
          e.preventDefault();
          videoControlsRef?.current?.toggleSelectedTrackOutside();
          return;
        case "videoToggleOccluded":
          e.preventDefault();
          videoControlsRef?.current?.toggleSelectedTrackOccluded();
          return;
        case "videoToggleHiddenTrack":
          e.preventDefault();
          videoControlsRef?.current?.toggleSelectedTrackHidden();
          return;
        case "videoToggleLockedTrack":
          e.preventDefault();
          videoControlsRef?.current?.toggleSelectedTrackLocked();
          return;
        case "videoPropagateTrack":
          e.preventDefault();
          videoControlsRef?.current?.propagateSelectedTrack();
          return;
        case "videoJumpHistory":
          e.preventDefault();
          videoControlsRef?.current?.jumpHistory(action.dir);
          return;
        case "videoClearLoopRegion":
          e.preventDefault();
          videoControlsRef?.current?.clearLoopRegion();
          return;
        case "videoDeleteSelected":
          e.preventDefault();
          if (!s.selectedId) return;
          if (action.scope === "keyframe") {
            const selected = annotationsRef.current.find((ann) => ann.id === s.selectedId);
            const trackId =
              selected && "track_id" in selected.geometry ? selected.geometry.track_id : null;
            if (selected?.is_locked || (trackId && s.lockedVideoTrackIds.has(trackId))) return;
            // Mask 与 bbox 都委托舞台控制器删除关键帧。Mask 控制器会进一步走
            // 专用 PATCH 合同，保留 segment/task lock、审计与 frame history 语义。
            if (selected?.geometry.type === "video_track_mask") {
              videoControlsRef?.current?.deleteSelectedTrackKeyframe();
              return;
            }
            if (selected?.geometry.type === "video_track_bbox") {
              const deleted = videoControlsRef?.current?.deleteSelectedTrackKeyframe();
              if (deleted) return;
              // Delete 永远是关键帧级；最后一帧也不隐式退化为删整轨。
              return;
            }
          }
          handleDeleteBox(s.selectedId);
          return;
        case "videoCycleInCategory":
          e.preventDefault();
          videoControlsRef?.current?.cycleInCategory(action.dir);
          return;
        case "videoStepCategory":
          e.preventDefault();
          videoControlsRef?.current?.stepCategory(action.dir);
          return;

        case "selectAllUser":
          e.preventDefault();
          if (annotationsRef.current.length > 0) {
            s.replaceSelected(annotationsRef.current.map((a) => a.id));
          }
          return;

        case "copy": {
          e.preventDefault();
          const n = clipboard.copySelection();
          if (n > 0) pushToast({ msg: `已复制 ${n} 个标注`, kind: "success" });
          return;
        }
        case "paste":
          e.preventDefault();
          if (clipboard.hasClipboard) {
            clipboard.paste().then((ids) => {
              if (ids.length > 0)
                pushToast({ msg: `已粘贴 ${ids.length} 个标注`, kind: "success" });
            });
          }
          return;
        case "duplicate":
          e.preventDefault();
          if (s.selectedIds.length > 0) {
            clipboard.duplicateSelection().then((ids) => {
              if (ids.length > 0)
                pushToast({ msg: `已复制 ${ids.length} 个标注`, kind: "success" });
            });
          }
          return;

        case "arrowNudge": {
          const hasUser = s.selectedIds.some((id) =>
            annotationsRef.current.some((a) => a.id === id),
          );
          if (!hasUser) return;
          e.preventDefault();
          applyArrowNudge(action.dx, action.dy);
          return;
        }

        case "spacePanOn":
          e.preventDefault();
          setSpacePan(true);
          return;
        case "showHotkeys":
          e.preventDefault();
          setShowHotkeys(true);
          return;
        case "cancel":
          if (showHotkeys) {
            setShowHotkeys(false);
            return;
          }
          if (batchChanging) {
            setBatchChanging(false);
            return;
          }
          // 分层取消：每按一次 ESC 只做一件事（草稿 → 编辑类别 → 选中）。
          if (s.pendingDrawing) {
            if (cancelPendingDrawing) cancelPendingDrawing();
            else s.setPendingDrawing(null);
            return;
          }
          if (s.editingClass) {
            s.setEditingClass(null);
            return;
          }
          if (!videoMode && cancelManualDrawing?.()) return;
          if (!videoMode && s.continuousCreation) {
            s.setContinuousCreation(null);
            s.setTool("select");
            return;
          }
          if (s.selectedId) {
            s.setSelectedId(null);
            return;
          }
          // 无草稿 / 无选中可取消时回选择工具；视频只退到 select, 不再回 hidden hand。
          if (videoMode) (requestVideoTool ?? s.setVideoTool)("select");
          else s.setTool("select");
          return;

        case "thresholdAdjust":
          e.preventDefault();
          s.setConfThreshold(
            Math.max(0, Math.min(1, +(s.confThreshold + action.delta).toFixed(2))),
          );
          return;

        // v0.10.5 M4-β I15 · 切换选中 shape 状态位（lock/hidden/occluded）。
        case "toggleShapeFlag": {
          if (!handlePatchShapeFlag) return;
          const id = s.selectedId;
          if (!id) return;
          const ann = annotationsRef.current.find((a) => a.id === id);
          if (!ann) return;
          e.preventDefault();
          const cur = !!(ann as unknown as Record<string, unknown>)[action.flag];
          handlePatchShapeFlag(id, action.flag, !cur);
          return;
        }

        // v0.10.5 M4-β I15 · 调整选中 shape 的 z_order。
        case "bumpZOrder": {
          if (!handlePatchShapeFlag) return;
          const id = s.selectedId;
          if (!id) return;
          const ann = annotationsRef.current.find((a) => a.id === id);
          if (!ann) return;
          e.preventDefault();
          const cur = typeof ann.z_order === "number" ? ann.z_order : 0;
          handlePatchShapeFlag(id, "z_order", cur + action.delta);
          return;
        }

        case "cycleUser": {
          const list = annotationsRef.current;
          if (list.length === 0) return;
          e.preventDefault();
          const idxNow = s.selectedId ? list.findIndex((a) => a.id === s.selectedId) : -1;
          let next: number;
          if (action.loop) {
            next = (idxNow + action.dir + list.length) % list.length;
          } else {
            next = Math.max(0, Math.min(list.length - 1, idxNow < 0 ? 0 : idxNow + action.dir));
          }
          s.setSelectedId(list[next].id);
          return;
        }

        // v0.21.11 · 图片「同类流转」(Tab) / 「跨类跳转」(`): 类别 = AI 待审(aiBoxes) + 人工(annotations),
        // 无轨迹。数组序循环(与 cycleUser 一致, 无需坐标)。焦点联动由 ImageWorkbench 的选中 effect 处理。
        case "imageCycleInCategory": {
          e.preventDefault();
          const cats = {
            ai: aiBoxes.map((b) => b.id),
            user: annotationsRef.current.map((a) => a.id),
            track: [] as string[],
          };
          const next = nextInCategory(cats, s.selectedId, action.dir);
          if (next) s.setSelectedId(next);
          return;
        }
        case "imageStepCategory": {
          e.preventDefault();
          const cats = {
            ai: aiBoxes.map((b) => b.id),
            user: annotationsRef.current.map((a) => a.id),
            track: [] as string[],
          };
          const next = nextCategory(cats, s.selectedId, action.dir);
          if (next) s.setSelectedId(next);
          return;
        }

        case "smartNext":
          smartNext(action.mode);
          return;

        case "changeClass": {
          const userIds = s.selectedIds.filter((id) =>
            annotationsRef.current.some((a) => a.id === id),
          );
          if (userIds.length > 1) handleStartBatchChangeClass();
          else if (userIds.length === 1) handleStartChangeClass(userIds[0]);
          return;
        }

        case "setTool": {
          // 工具命令可被改绑到 F1/F5/Home/PageDown 等带浏览器默认行为的键，
          // 命中后必须吞掉事件，避免同时切工具又刷新 / 滚动页面。
          e.preventDefault();
          if (action.tool === "mask" && maskToolDisabledReason) {
            pushToast({ msg: maskToolDisabledReason, kind: "warning" });
            return;
          }
          // 项目关闭交互式 AI 总开关时, AI 工具 hotkey 应 no-op (与 ToolDock 隐藏按钮契约一致)。
          if (
            aiInteractiveEnabled === false &&
            (action.tool === "ai-cycle" || AI_TOOL_HOTKEY_IDS.has(action.tool))
          ) {
            return;
          }
          // v0.10.2 · S / Alt+3 → "ai-cycle": 在 AI 工具中循环 (v0.10.17 含 magic-box),
          // 跳过置灰的; 末位再按退回 box. capabilities 通过 props 传入 isPromptSupported.
          // v0.14.18 · text-prompt 已归批量线 (从工具栏摘除), 不再进循环。
          if (action.tool === "ai-cycle") {
            const cycle: Array<"smart-point" | "smart-box" | "magic-box" | "exemplar"> = [
              "smart-point",
              "smart-box",
              "magic-box",
              "exemplar",
            ];
            // v0.18.17 · smart-box / magic-box 的 prompt key 改名 interactive_box (与后端
            // supported_prompts 对齐); 否则按旧 "bbox" 比对 → 工具被错误置灰.
            const requiredOf = (t: (typeof cycle)[number]) =>
              (
                ({
                  "smart-point": "point",
                  "smart-box": "interactive_box",
                  "magic-box": "interactive_box",
                  exemplar: "exemplar",
                }) as const
              )[t];
            const isEnabled = (t: (typeof cycle)[number]) =>
              isPromptSupported ? isPromptSupported(requiredOf(t)) : true;
            const curIdx = cycle.indexOf(s.tool as (typeof cycle)[number]);
            if (curIdx < 0) {
              const first = cycle.find(isEnabled);
              if (first) s.setTool(first);
              return;
            }
            for (let k = 1; k <= cycle.length; k++) {
              const nextIdx = (curIdx + k) % cycle.length;
              if (nextIdx === 0 && k === cycle.length) {
                s.setTool("box");
                return;
              }
              const next = cycle[nextIdx];
              if (isEnabled(next)) {
                s.setTool(next);
                return;
              }
            }
            s.setTool("box");
            return;
          }
          s.setTool(action.tool);
          return;
        }

        case "setVideoTool":
          // 同 setTool：改绑到 F1/F5/Home 等键时不要让浏览器默认行为同时生效。
          e.preventDefault();
          if (aiInteractiveEnabled === false && AI_TOOL_HOTKEY_IDS.has(action.tool)) {
            return;
          }
          (requestVideoTool ?? s.setVideoTool)(action.tool);
          return;

        case "samPolarity": {
          // v0.10.2 · smart-point 点正负; v0.18.19 · exemplar 框正负 (refine 会话) 同享极性。
          if (s.tool === "smart-point" || s.tool === "smart-scribble" || s.tool === "exemplar") {
            s.setSamPolarity(action.polarity);
          }
          return;
        }

        case "setClassByDigit":
          if (classes[action.idx]) {
            if (videoMode && handleVideoSetSelectedClass?.(classes[action.idx])) return;
            s.setActiveClass(classes[action.idx]);
            recordRecentClass(classes[action.idx]);
          }
          return;

        case "setAttribute": {
          e.preventDefault();
          if (!s.selectedId) return;
          const ann = annotationsRef.current.find((a) => a.id === s.selectedId);
          if (!ann) return;
          const next = { ...(ann.attributes ?? {}), [action.key]: action.value };
          handleUpdateAttributes(ann.id, next);
          return;
        }

        case "deleteSelected": {
          const userIds = s.selectedIds.filter((id) =>
            annotationsRef.current.some((a) => a.id === id),
          );
          if (userIds.length > 1) handleBatchDelete();
          else if (userIds.length === 1) handleDeleteBox(userIds[0]);
          return;
        }

        case "submit":
          handleSubmitTask();
          return;

        case "acceptAi":
        case "rejectAi": {
          e.preventDefault();
          if (e.repeat) return;
          const box = aiBoxes.find(
            (candidate) =>
              candidate.id === s.selectedId &&
              (!videoMode || aiBoxOnFrame(candidate, s.videoFrameIndex)),
          );
          if (!box) return;
          if (action.type === "acceptAi") void handleAcceptPrediction(box);
          else void handleRejectPrediction?.(box);
          return;
        }
      }
    };

    const onKeyUp = (e: KeyboardEvent) => {
      if (e.key === " ") {
        setSpacePan(false);
        if (videoMode && videoSpaceDownRef.current) {
          if (!videoSpaceDraggedRef.current && !isWorkbenchInteractionBlocked(e)) {
            videoControlsRef?.current?.togglePlayback();
          }
          videoSpaceDownRef.current = false;
          videoSpaceDraggedRef.current = false;
        }
      }
      if (ARROW_KEY_SET.has(e.key)) flushNudges();
    };

    window.addEventListener("keydown", onKey);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, [
    disabled,
    ignoredKeys,
    videoMode,
    stage,
    requestVideoTool,
    samplingActive,
    videoControlsRef,
    shortcutsEffective,
    reportBindingConflict,
    s,
    history,
    classes,
    currentProject,
    annotationsRef,
    batchChanging,
    classPickerActive,
    setBatchChanging,
    cancelPendingDrawing,
    cancelManualDrawing,
    showHotkeys,
    navigateTask,
    smartNext,
    setFitTick,
    onCrossFramePropagate,
    recordRecentClass,
    handleDeleteBox,
    handleBatchDelete,
    handlePatchShapeFlag,
    handleStartChangeClass,
    handleStartBatchChangeClass,
    handleSubmitTask,
    handleAcceptPrediction,
    handleRejectPrediction,
    handleUpdateAttributes,
    handleVideoSetSelectedClass,
    isPromptSupported,
    aiInteractiveEnabled,
    maskToolDisabledReason,
    maskInteractionFrozen,
    aiBoxes,
    setShowHotkeys,
    clipboard,
    pushToast,
    stageGeom.imgW,
    stageGeom.imgH,
    flushNudges,
    updateMutation,
  ]);

  return { spacePan, markSpacePanDrag, nudgeMap, flushNudges };
}
