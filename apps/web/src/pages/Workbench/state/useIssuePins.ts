import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";
import {
  hasPixelAnchor,
  type AnnotationFeedback,
  type FeedbackVideoContext,
} from "@/api/feedbacks";
import { useInfiniteFeedbacks } from "@/hooks/useFeedbacks";
import type { VideoFrameSeekResult } from "../stage/videoStageControls";
import { useActiveIssueStore } from "./useActiveIssueStore";
import type { Viewport } from "./useViewportTransform";
import { resolvePinViewport } from "./useWorkbenchShellModel.helpers";

export interface IssuePinAnchor {
  x: number;
  y: number;
  frame?: number;
  annotationId?: string;
  annotationLabel?: string;
  maxFrame?: number;
  videoContext?: FeedbackVideoContext;
}

export interface IssueNavigation {
  status: "idle" | "preparing" | "ready" | "cancelled" | "timeout" | "unavailable";
  frameIndex: number | null;
  message?: string;
}

interface IssueOwner {
  projectId: string | undefined;
  taskId: string | undefined;
  isVideoTask: boolean;
}

interface IssueUiState {
  owner: IssueOwner;
  issueCreateOpen: boolean;
  issueAnchorMode: "task" | "pixel";
  issuePinDropArmed: boolean;
  issuePinPrefill: IssuePinAnchor | null;
  issueNavigation: IssueNavigation;
}

interface NavigationRequest {
  owner: IssueOwner;
  frameIndex: number;
  anchor?: IssuePinAnchor;
}

function emptyIssueState(owner: IssueOwner): IssueUiState {
  return {
    owner,
    issueCreateOpen: false,
    issueAnchorMode: "task",
    issuePinDropArmed: false,
    issuePinPrefill: null,
    issueNavigation: { status: "idle", frameIndex: null },
  };
}

function isSourceFrame(frame: unknown): frame is number {
  return typeof frame === "number" && Number.isInteger(frame) && frame >= 0;
}

export function useIssuePins(params: {
  projectId: string | undefined;
  taskId: string | undefined;
  stageGeom: { imgW: number; imgH: number; vpSize: { w: number; h: number } };
  setVp: Dispatch<SetStateAction<Viewport>>;
  seekVideoFrameReady: (frame: number, isRelevant: () => boolean) => Promise<VideoFrameSeekResult>;
  pauseVideoPlayback: () => void;
  isVideoTask: boolean;
  captureImageContext?: () => Pick<IssuePinAnchor, "annotationId" | "annotationLabel"> | null;
  selectImageAnnotation?: (annotationId: string, isCurrent: () => boolean) => Promise<boolean>;
  captureVideoContext?: (frame: number) => Partial<IssuePinAnchor> | null;
  navigateVideoIssue?: (issue: AnnotationFeedback) => Promise<void>;
  onCreateIntent?: () => void;
}) {
  const { projectId, taskId, setVp, isVideoTask } = params;
  const owner = useMemo(
    () => ({ projectId, taskId, isVideoTask }),
    [projectId, taskId, isVideoTask],
  );
  const ownerRef = useRef(owner);
  ownerRef.current = owner;
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const mountedRef = useRef(true);
  const activeRequestRef = useRef<NavigationRequest | null>(null);
  const retryRequestRef = useRef<NavigationRequest | null>(null);
  const uiRef = useRef(emptyIssueState(owner));
  const [ui, setUi] = useState(uiRef.current);

  if (uiRef.current.owner !== owner) {
    uiRef.current = emptyIssueState(owner);
    activeRequestRef.current = null;
    retryRequestRef.current = null;
  }
  const currentUi = ui.owner === owner ? ui : uiRef.current;

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeRequestRef.current = null;
      retryRequestRef.current = null;
    };
  }, []);

  const updateUi = useCallback(
    (patch: Partial<Omit<IssueUiState, "owner">>) => {
      if (!mountedRef.current || ownerRef.current !== owner) return;
      uiRef.current = { ...uiRef.current, ...patch };
      setUi(uiRef.current);
    },
    [owner],
  );

  const clearRequest = useCallback(() => {
    activeRequestRef.current = null;
    retryRequestRef.current = null;
  }, []);

  const runNavigation = useCallback(
    async (input: NavigationRequest) => {
      if (!mountedRef.current || ownerRef.current !== input.owner) return;
      const request = { ...input, anchor: input.anchor ? { ...input.anchor } : undefined };
      activeRequestRef.current = request;
      retryRequestRef.current = request;
      const isRelevant = () =>
        mountedRef.current &&
        ownerRef.current === request.owner &&
        activeRequestRef.current === request;
      updateUi({ issueNavigation: { status: "preparing", frameIndex: request.frameIndex } });

      let result: VideoFrameSeekResult;
      try {
        paramsRef.current.pauseVideoPlayback();
        result = await paramsRef.current.seekVideoFrameReady(request.frameIndex, isRelevant);
      } catch {
        result = { status: "unavailable", frameIndex: request.frameIndex, source: null };
      }
      if (!isRelevant()) return;

      const status =
        result.status === "ready" && result.frameIndex !== request.frameIndex
          ? "unavailable"
          : result.status;
      updateUi({
        issueNavigation: { status, frameIndex: request.frameIndex },
        ...(status === "ready" && request.anchor
          ? { issueCreateOpen: true, issuePinPrefill: request.anchor }
          : {}),
      });
      if (status === "ready") retryRequestRef.current = null;
    },
    [updateUi],
  );

  const closeIssueCreate = useCallback(() => {
    if (ownerRef.current !== owner) return;
    clearRequest();
    updateUi(emptyIssueState(owner));
  }, [clearRequest, owner, updateUi]);

  const onToggleIssuePinDrop = useCallback(() => {
    if (!mountedRef.current || ownerRef.current !== owner || !projectId || !taskId) return;
    const armed = !uiRef.current.issuePinDropArmed;
    paramsRef.current.onCreateIntent?.();
    clearRequest();
    if (armed && isVideoTask) paramsRef.current.pauseVideoPlayback();
    updateUi({
      ...emptyIssueState(owner),
      issuePinDropArmed: armed,
      issueAnchorMode: armed ? "pixel" : "task",
    });
  }, [clearRequest, isVideoTask, owner, projectId, taskId, updateUi]);

  const openTaskIssue = useCallback(() => {
    if (!mountedRef.current || ownerRef.current !== owner || !projectId || !taskId) return;
    clearRequest();
    if (isVideoTask) paramsRef.current.pauseVideoPlayback();
    paramsRef.current.onCreateIntent?.();
    updateUi({ ...emptyIssueState(owner), issueCreateOpen: true, issueAnchorMode: "task" });
  }, [clearRequest, isVideoTask, owner, projectId, taskId, updateUi]);

  const onIssuePinDrop = useCallback(
    async (x: number, y: number, frame?: number) => {
      if (
        !mountedRef.current ||
        ownerRef.current !== owner ||
        !uiRef.current.issuePinDropArmed ||
        !Number.isFinite(x) ||
        !Number.isFinite(y) ||
        x < 0 ||
        x > 1 ||
        y < 0 ||
        y > 1
      ) {
        return;
      }
      clearRequest();
      if (!isVideoTask) {
        const object = paramsRef.current.captureImageContext?.();
        updateUi({
          issuePinDropArmed: false,
          issuePinPrefill: object ? { x, y, ...object } : { x, y },
          issueCreateOpen: true,
          issueAnchorMode: "pixel",
        });
        return;
      }
      if (!isSourceFrame(frame)) {
        updateUi({
          issuePinDropArmed: false,
          issueNavigation: { status: "unavailable", frameIndex: null },
        });
        return;
      }
      const anchor = { ...paramsRef.current.captureVideoContext?.(frame), x, y, frame };
      updateUi({ issuePinDropArmed: false, issuePinPrefill: anchor, issueAnchorMode: "pixel" });
      await runNavigation({ owner, frameIndex: frame, anchor });
    },
    [clearRequest, isVideoTask, owner, runNavigation, updateUi],
  );

  const onSeekIssueFrame = useCallback(
    async (frame: number) => {
      if (!mountedRef.current || ownerRef.current !== owner || !isVideoTask) return;
      if (!isSourceFrame(frame)) {
        clearRequest();
        updateUi({ issueNavigation: { status: "unavailable", frameIndex: null } });
        return;
      }
      await runNavigation({ owner, frameIndex: frame });
    },
    [clearRequest, isVideoTask, owner, runNavigation, updateUi],
  );

  const retryIssueNavigation = useCallback(async () => {
    const request = retryRequestRef.current;
    if (
      !request ||
      request.owner !== owner ||
      uiRef.current.issueNavigation.status === "preparing"
    ) {
      return;
    }
    await runNavigation(request);
  }, [owner, runNavigation]);

  const issueListParams = useMemo(
    () => ({
      project_id: projectId ?? "",
      task_id: taskId,
      kind: "issue" as const,
      root_only: true,
      include_counts: true,
      limit: 200,
    }),
    [projectId, taskId],
  );
  const issueQueryEnabled = !!projectId && !!taskId;
  const issuePagesQuery = useInfiniteFeedbacks(issueListParams, issueQueryEnabled);
  const fetchNextIssuePage = issuePagesQuery.fetchNextPage;
  const refetchIssuePages = issuePagesQuery.refetch;
  const pinOwnerKey = `${projectId ?? ""}:${taskId ?? ""}:${isVideoTask ? "video" : "image"}`;
  const pinFetchRef = useRef({ ownerKey: pinOwnerKey, inFlight: false });
  if (pinFetchRef.current.ownerKey !== pinOwnerKey) {
    pinFetchRef.current = { ownerKey: pinOwnerKey, inFlight: false };
  }

  useEffect(() => {
    if (
      !issueQueryEnabled ||
      issuePagesQuery.isError ||
      issuePagesQuery.isLoading ||
      issuePagesQuery.isFetching ||
      issuePagesQuery.isFetchingNextPage ||
      !issuePagesQuery.hasNextPage ||
      pinFetchRef.current.inFlight
    ) {
      return;
    }
    const requestOwner = owner;
    pinFetchRef.current.inFlight = true;
    void Promise.resolve(fetchNextIssuePage()).finally(() => {
      if (ownerRef.current === requestOwner) pinFetchRef.current.inFlight = false;
    });
  }, [
    issueQueryEnabled,
    issuePagesQuery.isError,
    issuePagesQuery.isLoading,
    issuePagesQuery.isFetching,
    issuePagesQuery.isFetchingNextPage,
    issuePagesQuery.hasNextPage,
    issuePagesQuery.data,
    fetchNextIssuePage,
    owner,
  ]);

  const issueItems = issuePagesQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const firstIssuePage = issuePagesQuery.data?.pages[0];
  const aggregatedIssuePage = firstIssuePage
    ? {
        ...firstIssuePage,
        items: issueItems,
        next_cursor:
          issuePagesQuery.data?.pages[issuePagesQuery.data.pages.length - 1]?.next_cursor ?? null,
      }
    : undefined;
  const issuesQuery = { ...issuePagesQuery, data: aggregatedIssuePage };
  const openIssueCount = issuePagesQuery.isError
    ? null
    : (firstIssuePage?.status_counts?.open ?? null);
  const openIssueCountLoading =
    issueQueryEnabled &&
    !firstIssuePage &&
    (issuePagesQuery.isLoading || issuePagesQuery.isFetching);
  const openIssueCountError = issueQueryEnabled && issuePagesQuery.isError;
  const issuePinsLoading =
    issueQueryEnabled &&
    (issuePagesQuery.isLoading ||
      issuePagesQuery.isFetchingNextPage ||
      pinFetchRef.current.inFlight);
  const issuePinsError = issueQueryEnabled && issuePagesQuery.isError;
  const issuePinsComplete =
    issueQueryEnabled &&
    !!issuePagesQuery.data &&
    !issuePagesQuery.isError &&
    !issuePagesQuery.isFetching &&
    !issuePagesQuery.hasNextPage;
  const retryIssuePins = useCallback(async () => {
    if (!issueQueryEnabled) return;
    pinFetchRef.current.inFlight = false;
    await refetchIssuePages();
  }, [issueQueryEnabled, refetchIssuePages]);
  const activeIssueHighlightId = useActiveIssueStore((st) => st.highlightId);
  const highlightIssueFromPin = useActiveIssueStore((st) => st.highlightFromPin);
  const requestIssuesTab = useActiveIssueStore((st) => st.requestIssuesTab);
  const issueFocusTick = useActiveIssueStore((st) => st.focusTick);
  const focusTarget = useActiveIssueStore((st) => st.focusTarget);
  const activeIssueHighlightRef = useRef(activeIssueHighlightId);
  activeIssueHighlightRef.current = activeIssueHighlightId;
  const latestFocusRef = useRef({ owner, tick: issueFocusTick });
  latestFocusRef.current = { owner, tick: issueFocusTick };
  const lastIssueFocusRef = useRef({ owner, tick: issueFocusTick });

  useEffect(() => {
    const previous = lastIssueFocusRef.current;
    lastIssueFocusRef.current = { owner, tick: issueFocusTick };
    if (issueFocusTick === previous.tick) return;
    const target =
      focusTarget?.id === activeIssueHighlightId
        ? focusTarget
        : (issuesQuery.data?.items ?? []).find((i) => i.id === activeIssueHighlightId);
    if (!target?.anchor_position) return;
    if (isVideoTask) {
      if (paramsRef.current.navigateVideoIssue && target.project_id === projectId) {
        clearRequest();
        updateUi(emptyIssueState(owner));
        void paramsRef.current.navigateVideoIssue(target);
        return;
      }
      const frame = target.anchor_position.frame;
      if (typeof frame === "number") void onSeekIssueFrame(frame);
      return;
    }
    if (target.project_id !== projectId || target.task_id !== taskId) return;
    if (!hasPixelAnchor(target)) return;
    const request = { owner, tick: issueFocusTick, id: target.id };
    const isCurrent = () =>
      mountedRef.current &&
      ownerRef.current === request.owner &&
      latestFocusRef.current.owner === request.owner &&
      latestFocusRef.current.tick === request.tick &&
      activeIssueHighlightRef.current === request.id;
    const restorePixelViewport = () => {
      if (!isCurrent()) return;
      const { imgW, imgH, vpSize } = paramsRef.current.stageGeom;
      if (!imgW || !imgH || !vpSize.w || !vpSize.h) return;
      setVp((cur) => resolvePinViewport(cur, target.anchor_position, imgW, imgH, vpSize));
    };
    if (target.annotation_id && paramsRef.current.selectImageAnnotation) {
      void paramsRef.current
        .selectImageAnnotation(target.annotation_id, isCurrent)
        .then((allowed) => {
          if (allowed) restorePixelViewport();
        })
        .catch(() => {});
      return;
    }
    restorePixelViewport();
  }, [
    owner,
    issueFocusTick,
    activeIssueHighlightId,
    issuesQuery.data,
    setVp,
    isVideoTask,
    onSeekIssueFrame,
    focusTarget,
    projectId,
    clearRequest,
    updateUi,
    taskId,
  ]);

  return {
    issueCreateOpen: currentUi.issueCreateOpen,
    issueAnchorMode: currentUi.issueAnchorMode,
    issuePinDropArmed: currentUi.issuePinDropArmed,
    issuePinPrefill: currentUi.issuePinPrefill,
    issueNavigation: currentUi.issueNavigation,
    onToggleIssuePinDrop,
    openTaskIssue,
    onIssuePinDrop,
    closeIssueCreate,
    onSeekIssueFrame,
    retryIssueNavigation,
    issueListParams,
    issuesQuery,
    openIssueCount,
    openIssueCountLoading,
    openIssueCountError,
    retryOpenIssueCount: retryIssuePins,
    issuePixelFeedbacks: issueItems.filter(hasPixelAnchor),
    issuePinsComplete,
    issuePinsLoading,
    issuePinsError,
    retryIssuePins,
    activeIssueHighlightId,
    highlightIssueFromPin,
    requestIssuesTab,
  };
}
