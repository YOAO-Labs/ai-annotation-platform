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
  requestTemporaryToolRef: { current: RequestTemporaryTool };
  panelCommandsRef: { current: WorkbenchWorkspaceCommands | null };
  disarmChapterDraft: () => void;
  clearPropagateBrush: () => void;
  trackerJobs: Pick<
    ReturnType<typeof useVideoTrackerJobs>,
    "propagate" | "track" | "candidates" | "jobs"
  >;
  unavailableReason?: string;
  onUnavailable?: (reason: string) => void;
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
  unavailableReason,
  onUnavailable,
}: UseTrackerSeedCollectionParams) {
  const [trackerSeeds, setTrackerSeeds] = useState<TrackerSeedPoint[]>([]);
  const [trackerSeedBoxes, setTrackerSeedBoxes] = useState<TrackerSeedBox[]>([]);
  const [seedMode, setSeedMode] = useState<"point" | "box">("point");
  const seedModeRef = useRef(seedMode);
  seedModeRef.current = seedMode;
  const [seedObj, setSeedObj] = useState(1);
  const [seedAnchorFrame, setSeedAnchorFrame] = useState<number | null>(null);
  const [seedCollecting, setSeedCollecting] = useState(false);
  const seedPrevToolRef = useRef<VideoToolSelection | null>(null);

  useEffect(() => {
    if (!isVideoTask || seedCollecting) return;
    if (videoTool !== "select" && !isVideoToolEnabled(videoTool)) setVideoTool("select");
  }, [isVideoTask, seedCollecting, isVideoToolEnabled, videoTool, setVideoTool]);

  const [propagateDialog, setPropagateDialog] = useState<{
    annotation: TrackerSourceAnnotation | null;
    sources?: TrackerSourceAnnotation[];
    submitting: boolean;
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
  const newSeedTarget = useCallback(() => {
    const hasSeed =
      trackerSeeds.some((s) => s.obj === seedObj) ||
      trackerSeedBoxes.some((b) => b.obj === seedObj);
    if (hasSeed) setSeedObj(seedObj + 1);
  }, [trackerSeeds, trackerSeedBoxes, seedObj]);

  const openPropagateDialog = useCallback(
    (source: TrackerSourceAnnotation | TrackerSourceAnnotation[] | null) => {
      if (unavailableReason) {
        if (propagateDialogRef.current) panelCommandsRef.current?.show("video-tracker");
        else onUnavailable?.(unavailableReason);
        return;
      }
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
    [clearPropagateBrush, disarmChapterDraft, panelCommandsRef, unavailableReason, onUnavailable],
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
      if (unavailableReason) {
        onUnavailable?.(unavailableReason);
        return;
      }
      setPropagateDialog((prev) => (prev ? { ...prev, submitting: true } : prev));
      try {
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
        const batchSources = propagateDialog.sources;
        const job =
          batchSources && batchSources.length >= 2
            ? await trackerJobs.track(taskId, {
                ...withSeeds,
                source_annotation_ids: batchSources.map((sd) => sd.id),
              })
            : propagateDialog.annotation
              ? await trackerJobs.propagate(taskId, propagateDialog.annotation.id, withSeeds)
              : await trackerJobs.track(taskId, withSeeds);
        enterTrackingProgress(job.id);
      } catch (e) {
        setPropagateDialog((prev) => (prev ? { ...prev, submitting: false } : prev));
        throw e;
      }
    },
    [
      propagateDialog,
      taskId,
      trackerJobs,
      trackerSeeds,
      trackerSeedBoxes,
      enterTrackingProgress,
      unavailableReason,
      onUnavailable,
    ],
  );

  const trackingJobId = propagateDialog?.jobId ?? null;
  useEffect(() => {
    if (!trackingJobId) return;
    const candidateReady = Boolean(trackerJobs.candidates[trackingJobId]);
    const job = trackerJobs.jobs[trackingJobId];
    if (candidateReady || !job || job.status === "failed") {
      closePropagateDialog();
    }
  }, [trackingJobId, trackerJobs.candidates, trackerJobs.jobs, closePropagateDialog]);

  const collectPoint = useCallback(
    (pt: [number, number], polarity: 1 | 0, frame: number) => {
      setSeedAnchorFrame((a) => (a === null ? frame : a));
      setTrackerSeeds((prev) => [...prev, { pt, polarity, obj: seedObj, frame }]);
    },
    [seedObj],
  );
  const collectBox = useCallback(
    (bbox: [number, number, number, number], frame: number) => {
      setSeedAnchorFrame((a) => (a === null ? frame : a));
      setTrackerSeedBoxes((prev) => [...prev, { bbox, obj: seedObj, frame }]);
    },
    [seedObj],
  );
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
