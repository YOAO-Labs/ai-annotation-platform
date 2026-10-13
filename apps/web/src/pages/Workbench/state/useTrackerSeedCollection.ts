/**
 * Tracker propagation dialog + PVS seed collection: one domain owner for the
 * "落点选目标" workflow on video tasks.
 *
 * Owns: the collected point/box seeds (per-target obj, per-frame), the active
 * seed target/mode/anchor state, the collecting session (temporary smart-point/
 * smart-box tool with restore), and the propagation dialog lifecycle
 * (source/无源/多源 capture, submitting, in-progress jobId handoff to the
 * tracker review bar).
 *
 * Async ownership (plan §4.4): the dialog records which tracker job it spawned;
 * the close effect fires only on candidate-ready / failed / removed jobs, so a
 * running job survives re-renders but never leaks into a different task's
 * dialog. Panel visibility and chapter-draft arming remain owned by the
 * workspace/chapter domains — this module only invokes the narrow commands
 * passed in. Temporary tool admission stays owned by useVideoToolCommands; the
 * requester arrives via ref because tool commands are created later in the
 * assembly sequence than collection state.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { useVideoTrackerJobs } from "@/hooks/useVideoTrackerJobs";
import type { AnnotationResponse } from "@/types";
import type { VideoTrackGeometry, VideoTrackMaskGeometry } from "@/types";
import type { VideoTool } from "./useWorkbenchState";
import type { VideoToolSelection } from "../stage/videoToolUnits";
import type { WorkbenchWorkspaceCommands } from "../layout/workbenchPanelRegistry";
import { buildSeedPrompts, hasAnySeed } from "./trackerSeedPrompts";

export type TrackerSourceAnnotation = AnnotationResponse & {
  geometry: VideoTrackGeometry | VideoTrackMaskGeometry;
};

export type TrackerSeedPoint = {
  pt: [number, number];
  polarity: 1 | 0;
  obj: number;
  frame: number;
};
export type TrackerSeedBox = { bbox: [number, number, number, number]; obj: number; frame: number };

type RequestTemporaryTool = (
  tool: VideoTool,
  onAdmitted: (previous: VideoToolSelection) => void,
  isRelevant: () => boolean,
) => void;

export interface UseTrackerSeedCollectionParams {
  taskId: string | undefined;
  isVideoTask: boolean;
  videoTool: VideoTool;
  isVideoToolEnabled: (tool: VideoTool) => boolean;
  setVideoTool: (tool: VideoTool) => void;
  setVideoToolSelection: (selection: VideoToolSelection) => void;
  /** 工具命令在装配序列后段创建;经 ref 注入(与既有 requestVideoSeedToolRef 模式一致)。 */
  requestTemporaryToolRef: { current: RequestTemporaryTool };
  panelCommandsRef: { current: WorkbenchWorkspaceCommands | null };
  /** 章节/面板归各自领域;采集开启时只请求其让位。 */
  disarmChapterDraft: () => void;
  /** 时间轴刷选范围归章节域;打开对话框时请求其清空。 */
  clearPropagateBrush: () => void;
  trackerJobs: Pick<
    ReturnType<typeof useVideoTrackerJobs>,
    "propagate" | "track" | "candidates" | "jobs"
  >;
}

export function useTrackerSeedCollection({
  taskId,
  isVideoTask,
  videoTool,
  isVideoToolEnabled,
  setVideoTool,
  setVideoToolSelection,
  requestTemporaryToolRef,
  panelCommandsRef,
  disarmChapterDraft,
  clearPropagateBrush,
  trackerJobs,
}: UseTrackerSeedCollectionParams) {
  const [trackerSeeds, setTrackerSeeds] = useState<TrackerSeedPoint[]>([]);
  const [trackerSeedBoxes, setTrackerSeedBoxes] = useState<TrackerSeedBox[]>([]);
  // 落点/画框模式: point → smart-point 落点, box → smart-box 画修正框。
  const [seedMode, setSeedMode] = useState<"point" | "box">("point");
  const seedModeRef = useRef(seedMode);
  seedModeRef.current = seedMode;
  const [seedObj, setSeedObj] = useState(1);
  const [seedAnchorFrame, setSeedAnchorFrame] = useState<number | null>(null);
  const [seedCollecting, setSeedCollecting] = useState(false);
  const seedPrevToolRef = useRef<VideoToolSelection | null>(null);

  // 当前创建工具被 video_modes 过滤掉时, 回到选择工具；平移不再是 fallback 工具。
  // PVS 种子采集态会临时把工具切到 smart-point (画布 samProbe 只看工具值、不看
  // enablement), 此时不受本守卫回收 —— 否则未绑交互工具的项目落不了种子。
  useEffect(() => {
    if (!isVideoTask || seedCollecting) return;
    if (videoTool !== "select" && !isVideoToolEnabled(videoTool)) setVideoTool("select");
  }, [isVideoTask, seedCollecting, isVideoToolEnabled, videoTool, setVideoTool]);

  const [propagateDialog, setPropagateDialog] = useState<{
    // 无源检测: annotation 为 null = 画布级入口发起, 不绑选中轨迹。
    annotation: TrackerSourceAnnotation | null;
    // 多选批量: ≥2 条源轨迹一次延展 (单 job 多源, 后端 annotation_id 存 NULL →
    // 走 job 级审阅)。多源时 annotation 置 null, sources 持全列表; 单源/无源时 sources 省略。
    sources?: TrackerSourceAnnotation[];
    submitting: boolean;
    // 提交成功后置入建成的 tracker job id: 对话框就地转「追踪中…」进行态,
    // 追踪进度读该 job (jobs[jobId]); 结果就绪 (候选) / 失败时由 effect 关闭对话框复位。
    jobId?: string;
  } | null>(null);
  const propagateDialogRef = useRef(propagateDialog);
  propagateDialogRef.current = propagateDialog;

  const startSeedCollecting = useCallback(() => {
    const sourceDialog = propagateDialog;
    if (!sourceDialog) return;
    requestTemporaryToolRef.current(
      seedMode === "box" ? "smart-box" : "smart-point",
      (previous) => {
        seedPrevToolRef.current = previous;
        setSeedCollecting(true);
      },
      () => propagateDialogRef.current === sourceDialog && seedModeRef.current === seedMode,
    );
  }, [propagateDialog, seedMode, requestTemporaryToolRef]);
  // 点/框模式切换: 采集中即时切工具 (smart-point ↔ smart-box), 未采集只记模式。
  const changeSeedMode = useCallback(
    (mode: "point" | "box") => {
      setSeedMode(mode);
      if (seedCollecting) setVideoTool(mode === "box" ? "smart-box" : "smart-point");
    },
    [seedCollecting, setVideoTool],
  );
  const stopSeedCollecting = useCallback(() => {
    if (seedPrevToolRef.current !== null) {
      setVideoToolSelection(seedPrevToolRef.current);
      seedPrevToolRef.current = null;
    }
    setSeedCollecting(false);
  }, [setVideoToolSelection]);
  const toggleSeedCollecting = useCallback(() => {
    if (seedCollecting) stopSeedCollecting();
    else startSeedCollecting();
  }, [seedCollecting, startSeedCollecting, stopSeedCollecting]);
  // 「新目标」: 当前目标已落 ≥1 点或框才递增 (不建空目标), 后续点/框归入下一目标。
  const newSeedTarget = useCallback(() => {
    const hasSeed =
      trackerSeeds.some((s) => s.obj === seedObj) ||
      trackerSeedBoxes.some((b) => b.obj === seedObj);
    if (hasSeed) setSeedObj(seedObj + 1);
  }, [trackerSeeds, trackerSeedBoxes, seedObj]);

  const openPropagateDialog = useCallback(
    (source: TrackerSourceAnnotation | TrackerSourceAnnotation[] | null) => {
      // 多选批量归一化: null=无源, 单条=单源延展, ≥2 条=多选批量 (单 job 多源)。
      const list = Array.isArray(source) ? source : source ? [source] : [];
      disarmChapterDraft();
      clearPropagateBrush();
      setPropagateDialog({
        annotation: list.length === 1 ? list[0] : null,
        sources: list.length >= 2 ? list : undefined,
        submitting: false,
      });
      setTrackerSeeds([]);
      setTrackerSeedBoxes([]);
      setSeedObj(1);
      setSeedMode("point");
      setSeedAnchorFrame(null);
      setSeedCollecting(false);
      seedPrevToolRef.current = null;
      panelCommandsRef.current?.show("video-tracker");
    },
    [clearPropagateBrush, disarmChapterDraft, panelCommandsRef],
  );
  const closePropagateDialog = useCallback(() => {
    setPropagateDialog(null);
    setTrackerSeeds([]);
    setTrackerSeedBoxes([]);
    setSeedObj(1);
    setSeedAnchorFrame(null);
    stopSeedCollecting();
    panelCommandsRef.current?.hide("video-tracker");
  }, [panelCommandsRef, stopSeedCollecting]);
  const togglePropagateDialog = useCallback(() => {
    disarmChapterDraft();
    if (propagateDialog) panelCommandsRef.current?.show("video-tracker");
    else openPropagateDialog(null);
  }, [disarmChapterDraft, openPropagateDialog, panelCommandsRef, propagateDialog]);

  // 提交成功后不立即关闭对话框, 而就地转「追踪中…」进行态 (保留对话框显示进度,
  // 让位审阅条前给即时反馈)。清掉种子采集态 (与关闭同款), 但保留对话框记录并挂上 job id。
  const enterTrackingProgress = useCallback(
    (jobId: string) => {
      setTrackerSeeds([]);
      setTrackerSeedBoxes([]);
      setSeedObj(1);
      setSeedAnchorFrame(null);
      stopSeedCollecting();
      setPropagateDialog((prev) => (prev ? { ...prev, submitting: false, jobId } : prev));
    },
    [stopSeedCollecting],
  );

  const handlePropagateSubmit = useCallback(
    async (payload: Parameters<typeof trackerJobs.propagate>[2]) => {
      if (!propagateDialog || !taskId) return;
      setPropagateDialog((prev) => (prev ? { ...prev, submitting: true } : prev));
      try {
        // 有落点则注入 prompt.seeds (obj → frame 双层分组; points 与 bbox 可同帧并存);
        // 分组/排序/归一化见纯模块 trackerSeedPrompts.ts。
        const seedPromptGroups = buildSeedPrompts(trackerSeeds, trackerSeedBoxes);
        const withSeeds = hasAnySeed(trackerSeeds, trackerSeedBoxes)
          ? {
              ...payload,
              prompt: {
                ...(payload.prompt ?? {}),
                seeds: seedPromptGroups,
              },
            }
          : payload;
        // 多选批量 (≥2 源) → 任务级 track 带 source_annotation_ids, 后端逐源
        // 读当前帧几何构 seeds, 一个 job 各回填各自源 (annotation_id 存 NULL, 走 job 级审阅)。
        const batchSources = propagateDialog.sources;
        const job =
          batchSources && batchSources.length >= 2
            ? await trackerJobs.track(taskId, {
                ...withSeeds,
                source_annotation_ids: batchSources.map((sd) => sd.id),
              })
            : propagateDialog.annotation
              ? await trackerJobs.propagate(taskId, propagateDialog.annotation.id, withSeeds)
              : // 无源检测: 走任务级 track (payload 已含 target_class_name)。
                await trackerJobs.track(taskId, withSeeds);
        // 就地转进行态: 不立即关闭, 挂上 job id 让对话框显示「追踪中…」,
        // 直到结果就绪 (候选) / 失败时由 effect 复位关闭。
        enterTrackingProgress(job.id);
      } catch (e) {
        setPropagateDialog((prev) => (prev ? { ...prev, submitting: false } : prev));
        throw e;
      }
    },
    [propagateDialog, taskId, trackerJobs, trackerSeeds, trackerSeedBoxes, enterTrackingProgress],
  );

  // 进行态收尾: 对话框挂着的 job 出候选 (结果就绪待审) → 关闭对话框, 让位顶部
  // 居中的审阅条 (二者同位, 避免叠); job 失败 / 已被终态清理移除 → 同样收起复位。运行中则保持
  // 「追踪中…」。仅依赖 job id + candidates/jobs 引用, 进度 (windowProgress) 变化不触发关闭。
  const trackingJobId = propagateDialog?.jobId ?? null;
  useEffect(() => {
    if (!trackingJobId) return;
    const candidateReady = Boolean(trackerJobs.candidates[trackingJobId]);
    const job = trackerJobs.jobs[trackingJobId];
    if (candidateReady || !job || job.status === "failed") {
      closePropagateDialog();
    }
  }, [trackingJobId, trackerJobs.candidates, trackerJobs.jobs, closePropagateDialog]);

  /** 画布落点手势入口:采集态下归集一颗点(锚定首个落点帧)。 */
  const collectPoint = useCallback(
    (pt: [number, number], polarity: 1 | 0, frame: number) => {
      setSeedAnchorFrame((a) => (a === null ? frame : a));
      setTrackerSeeds((prev) => [...prev, { pt, polarity, obj: seedObj, frame }]);
    },
    [seedObj],
  );
  /** 画布画框手势入口:采集态下归集一颗框种子。 */
  const collectBox = useCallback(
    (bbox: [number, number, number, number], frame: number) => {
      setSeedAnchorFrame((a) => (a === null ? frame : a));
      setTrackerSeedBoxes((prev) => [...prev, { bbox, obj: seedObj, frame }]);
    },
    [seedObj],
  );
  /** 清空已落点/框并回到首个目标 (对话框工具条「清空」)。 */
  const clearSeeds = useCallback(() => {
    setTrackerSeeds([]);
    setTrackerSeedBoxes([]);
    setSeedObj(1);
    setSeedAnchorFrame(null);
  }, []);

  return {
    seeds: trackerSeeds,
    boxes: trackerSeedBoxes,
    seedMode,
    seedObj,
    seedAnchorFrame,
    seedCollecting,
    dialog: propagateDialog,
    collectPoint,
    collectBox,
    toggleCollecting: toggleSeedCollecting,
    newTarget: newSeedTarget,
    changeMode: changeSeedMode,
    openDialog: openPropagateDialog,
    closeDialog: closePropagateDialog,
    toggleDialog: togglePropagateDialog,
    submit: handlePropagateSubmit,
    trackingJobId,
    clearSeeds,
  };
}
