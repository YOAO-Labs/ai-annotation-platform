import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { AnnotationFeedback, ListFeedbacksParams } from "@/api/feedbacks";
import { useInfiniteFeedbacks } from "@/hooks/useFeedbacks";
import { useAuthStore } from "@/stores/authStore";
import { useDiscussionDraftStore } from "./DiscussionDraftProvider";
import type { DiscussionDraftStore } from "./useDiscussionDraftStore";
import type { DiscussionSessionOwner } from "./discussionTypes";

export type IssueSequenceDirection = "previous" | "next";
export type IssueSequenceState = "unknown" | "available" | "unavailable";

export interface UseIssueSequenceOptions {
  projectId: string;
  taskId?: string | null;
  current: AnnotationFeedback | null;
  enabled?: boolean;
}

export interface UseIssueSequenceResult {
  previousState: IssueSequenceState;
  nextState: IssueSequenceState;
  loading: boolean;
  error: Error | null;
  outsideScope: boolean;
  go: (direction: IssueSequenceDirection) => Promise<AnnotationFeedback | null>;
  cancel: () => void;
}

type IssueQuery = ReturnType<typeof useInfiniteFeedbacks>;
type IssueQueryData = NonNullable<IssueQuery["data"]>;
type IssuePage = IssueQueryData["pages"][number];
type IssueObserverResult = Awaited<ReturnType<IssueQuery["refetch"]>>;

interface OrderKey {
  timestampMicros: number;
  id: string;
}

interface OrderedIssue {
  item: AnnotationFeedback;
  order: OrderKey;
}

interface DirectionEvaluation {
  previousState: IssueSequenceState;
  nextState: IssueSequenceState;
  previous: AnnotationFeedback | null;
  next: AnnotationFeedback | null;
}

interface OwnerContext {
  key: string | null;
  store: DiscussionDraftStore | null;
  owner: DiscussionSessionOwner | null;
}

interface Runtime {
  contextKey: string;
  ownerContext: OwnerContext;
  queryEnabled: boolean;
  current: AnnotationFeedback | null;
  currentAnchor: OrderKey | null;
  projectId: string;
  taskId: string | null;
  query: IssueQuery;
}

interface Run {
  generation: number;
  contextKey: string;
  ownerKey: string;
  owner: DiscussionSessionOwner;
  direction: IssueSequenceDirection;
}

interface LocalState {
  contextKey: string;
  ownerKey: string | null;
  loading: boolean;
  error: Error | null;
  overrideData: IssueQueryData | undefined;
  overrideHasNextPage: boolean;
  overrideIsError: boolean;
  overrideEvaluation: DirectionEvaluation | null;
}

interface QuerySnapshot {
  data: IssueQueryData | undefined;
  hasNextPage: boolean;
  isError: boolean;
  error: Error | null;
}

interface CursorResult {
  valid: boolean;
  nextCursor: string | null;
}

const UNAVAILABLE: DirectionEvaluation = {
  previousState: "unavailable",
  nextState: "unavailable",
  previous: null,
  next: null,
};

const UNKNOWN: DirectionEvaluation = {
  previousState: "unknown",
  nextState: "unknown",
  previous: null,
  next: null,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function asError(value: unknown, fallback = "问题序列加载失败"): Error {
  if (value instanceof Error) return value;
  if (typeof value === "string" && value.length > 0) return new Error(value);
  if (isRecord(value) && typeof value.message === "string" && value.message.length > 0) {
    return new Error(value.message);
  }
  return new Error(fallback);
}

function timestampMicros(value: unknown): number | null {
  if (typeof value !== "string") return null;
  const match =
    /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return null;

  const [, date, hourText, minuteText, secondText, fractionText = "", zone] = match;
  const hour = Number(hourText);
  const minute = Number(minuteText);
  const second = Number(secondText);
  if (hour > 23 || minute > 59 || second > 59) return null;

  const baseText = date + "T" + hourText + ":" + minuteText + ":" + secondText;
  const baseMillis = Date.parse(baseText + "Z");
  if (!Number.isFinite(baseMillis)) return null;
  if (new Date(baseMillis).toISOString().slice(0, 19) !== baseText) return null;

  let offsetMinutes = 0;
  if (zone !== "Z") {
    const sign = zone[0] === "-" ? -1 : 1;
    const offsetHour = Number(zone.slice(1, 3));
    const offsetMinute = Number(zone.slice(4, 6));
    if (offsetHour > 23 || offsetMinute > 59) return null;
    offsetMinutes = sign * (offsetHour * 60 + offsetMinute);
  }

  const fractionMicros = Number(fractionText.padEnd(6, "0") || "0");
  const result = (baseMillis - offsetMinutes * 60_000) * 1_000 + fractionMicros;
  return Number.isSafeInteger(result) ? result : null;
}

function issueOrder(item: Pick<AnnotationFeedback, "created_at" | "id">): OrderKey | null {
  if (typeof item.id !== "string" || item.id.length === 0) return null;
  const timestamp = timestampMicros(item.created_at);
  return timestamp === null ? null : { timestampMicros: timestamp, id: item.id };
}

function compareNewestFirst(left: OrderKey, right: OrderKey): number {
  if (left.timestampMicros !== right.timestampMicros) {
    return left.timestampMicros > right.timestampMicros ? -1 : 1;
  }
  if (left.id === right.id) return 0;
  return left.id > right.id ? -1 : 1;
}

function isSequenceFeedback(
  value: unknown,
  projectId: string,
  taskId: string | null,
): value is AnnotationFeedback {
  if (!isRecord(value)) return false;
  if (
    typeof value.id !== "string" ||
    value.id.length === 0 ||
    value.kind !== "issue" ||
    value.status !== "open" ||
    value.is_active !== true ||
    value.thread_parent_id != null ||
    value.project_id !== projectId ||
    (taskId !== null && value.task_id !== taskId)
  ) {
    return false;
  }
  return issueOrder(value as Pick<AnnotationFeedback, "created_at" | "id">) !== null;
}

function isCurrentRoot(
  current: AnnotationFeedback | null,
  projectId: string,
  taskId: string | null,
): current is AnnotationFeedback {
  return (
    !!current &&
    typeof current.id === "string" &&
    current.id.length > 0 &&
    current.kind === "issue" &&
    current.thread_parent_id == null &&
    current.is_active === true &&
    (current.status === "open" || current.status === "resolved" || current.status === "wont_fix") &&
    current.project_id === projectId &&
    (taskId === null || current.task_id === taskId)
  );
}

function collectIssues(
  pages: readonly IssuePage[],
  projectId: string,
  taskId: string | null,
): OrderedIssue[] {
  const seen = new Set<string>();
  const result: OrderedIssue[] = [];
  for (const page of pages) {
    if (!Array.isArray(page.items)) continue;
    for (const value of page.items as unknown[]) {
      if (!isSequenceFeedback(value, projectId, taskId) || seen.has(value.id)) continue;
      const order = issueOrder(value);
      if (!order) continue;
      seen.add(value.id);
      result.push({ item: value, order });
    }
  }
  result.sort((left, right) => compareNewestFirst(left.order, right.order));
  return result;
}

function hasOpenRecord(pages: readonly IssuePage[], id: string): boolean {
  return pages.some(
    (page) =>
      Array.isArray(page.items) &&
      page.items.some((value) => isRecord(value) && value.id === id && value.status === "open"),
  );
}

function readCursor(page: IssuePage | undefined): CursorResult {
  if (!page) return { valid: false, nextCursor: null };
  if (page.next_cursor === null) return { valid: true, nextCursor: null };
  if (typeof page.next_cursor !== "string" || page.next_cursor.length === 0) {
    return { valid: false, nextCursor: null };
  }
  return { valid: true, nextCursor: page.next_cursor };
}

function directionState(item: AnnotationFeedback | null, unknown: boolean): IssueSequenceState {
  return unknown ? "unknown" : item ? "available" : "unavailable";
}

function evaluateSequence(input: {
  current: AnnotationFeedback | null;
  currentAnchor: OrderKey | null;
  projectId: string;
  taskId: string | null;
  pages: readonly IssuePage[];
  hasMore: boolean;
  queryEnabled: boolean;
  queryError: boolean;
  queryHasData: boolean;
}): DirectionEvaluation {
  const { current, currentAnchor, projectId, taskId, pages, hasMore, queryEnabled } = input;
  if (!queryEnabled || !isCurrentRoot(current, projectId, taskId)) return UNAVAILABLE;
  if (input.queryError) return UNKNOWN;
  if (!input.queryHasData) return UNKNOWN;

  const issues = collectIssues(pages, projectId, taskId).filter(
    ({ item }) => current.status === "open" || item.id !== current.id,
  );
  if (current.status === "open") {
    const index = issues.findIndex(({ item }) => item.id === current.id);
    if (index < 0) return hasMore ? UNKNOWN : UNAVAILABLE;
    const previous = index > 0 ? (issues[index - 1]?.item ?? null) : null;
    const next = index + 1 < issues.length ? (issues[index + 1]?.item ?? null) : null;
    return {
      previous,
      next,
      previousState: directionState(previous, false),
      nextState: directionState(next, !next && hasMore),
    };
  }

  if (!currentAnchor) return UNAVAILABLE;
  const insertion = issues.findIndex(({ order }) => compareNewestFirst(order, currentAnchor) > 0);
  const index = insertion < 0 ? issues.length : insertion;
  const previous = index > 0 ? (issues[index - 1]?.item ?? null) : null;
  const next = index < issues.length ? (issues[index]?.item ?? null) : null;
  const previousUnknown = index === issues.length && hasMore;
  return {
    previous,
    next,
    previousState: directionState(previous, previousUnknown),
    nextState: directionState(next, !next && hasMore),
  };
}

function scopeKey(projectId: string, taskId: string | null): string {
  return JSON.stringify([projectId, taskId]);
}

function sameOwner(store: DiscussionDraftStore | null, owner: DiscussionSessionOwner): boolean {
  if (!store) return false;
  try {
    return store.isOwned(owner);
  } catch {
    return false;
  }
}

function toQuerySnapshot(value: IssueQuery | IssueObserverResult): QuerySnapshot {
  const hasNextPage =
    "hasNextPage" in value
      ? value.hasNextPage
      : value.data?.pages[value.data.pages.length - 1]?.next_cursor != null;
  return {
    data: value.data,
    hasNextPage,
    isError: value.isError,
    error: value.error,
  };
}

export function useIssueSequence({
  projectId,
  taskId: rawTaskId,
  current,
  enabled = true,
}: UseIssueSequenceOptions): UseIssueSequenceResult {
  const taskId = rawTaskId ?? null;
  const draftStore = useDiscussionDraftStore();
  const authUserId = useAuthStore((state) => state.user?.id ?? null);
  const storeOwner = draftStore?.owner ?? null;
  const owner: OwnerContext =
    storeOwner && authUserId && storeOwner.userId === authUserId && storeOwner.sessionId.length > 0
      ? {
          key: storeOwner.userId + ":" + storeOwner.sessionId,
          store: draftStore,
          owner: storeOwner,
        }
      : { key: null, store: null, owner: null };

  const outsideScope = Boolean(
    current &&
    (current.project_id !== projectId || (rawTaskId != null && current.task_id !== rawTaskId)),
  );
  const currentId = current?.id ?? null;
  const currentScope = scopeKey(projectId, taskId);
  const anchorRef = useRef<{
    ownerKey: string | null;
    scope: string;
    rootId: string | null;
    value: OrderKey | null;
  }>({ ownerKey: null, scope: currentScope, rootId: null, value: null });
  const previousAnchor = anchorRef.current;
  if (
    previousAnchor.ownerKey !== owner.key ||
    previousAnchor.scope !== currentScope ||
    previousAnchor.rootId !== currentId
  ) {
    anchorRef.current = {
      ownerKey: owner.key,
      scope: currentScope,
      rootId: currentId,
      value: current ? issueOrder(current) : null,
    };
  } else if (!previousAnchor.value && current) {
    const value = issueOrder(current);
    if (value) anchorRef.current.value = value;
  }
  const currentAnchor = anchorRef.current.value;

  const contextKey = JSON.stringify([
    owner.key,
    projectId,
    taskId,
    enabled,
    current?.id ?? null,
    current?.kind ?? null,
    current?.status ?? null,
    current?.is_active ?? null,
    current?.thread_parent_id ?? null,
    outsideScope,
  ]);
  const currentRootValid = isCurrentRoot(current, projectId, taskId);
  const queryEnabled = Boolean(
    enabled && owner.key && projectId && currentRootValid && !outsideScope,
  );
  const listParams = useMemo<ListFeedbacksParams>(
    () => ({
      project_id: projectId,
      ...(rawTaskId == null ? {} : { task_id: rawTaskId }),
      kind: "issue",
      status: "open",
      root_only: true,
      limit: 50,
    }),
    [projectId, rawTaskId],
  );
  const query = useInfiniteFeedbacks(listParams, queryEnabled);
  const queryData = query.data;
  const pages = queryData?.pages ?? [];
  const queryHasData = queryData !== undefined;
  const queryHasMore = query.hasNextPage;
  const queryFailed = query.isError;

  const runtimeRef = useRef<Runtime | null>(null);
  runtimeRef.current = {
    contextKey,
    ownerContext: owner,
    queryEnabled,
    current: current ? { ...current } : null,
    currentAnchor,
    projectId,
    taskId,
    query,
  };

  const mountedRef = useRef(true);
  const generationRef = useRef(0);
  const activeRunRef = useRef<Run | null>(null);
  const previousContextRef = useRef({
    contextKey,
    ownerKey: owner.key,
    queryEnabled,
  });
  const [localState, setLocalState] = useState<LocalState>({
    contextKey,
    ownerKey: owner.key,
    loading: false,
    error: null,
    overrideData: undefined,
    overrideHasNextPage: false,
    overrideIsError: false,
    overrideEvaluation: null,
  });

  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
      activeRunRef.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    const previous = previousContextRef.current;
    const changed =
      previous.contextKey !== contextKey ||
      previous.ownerKey !== owner.key ||
      previous.queryEnabled !== queryEnabled;
    previousContextRef.current = {
      contextKey,
      ownerKey: owner.key,
      queryEnabled,
    };
    if (!changed) return;
    generationRef.current += 1;
    activeRunRef.current = null;
    if (mountedRef.current) {
      setLocalState({
        contextKey,
        ownerKey: owner.key,
        loading: false,
        error: null,
        overrideData: undefined,
        overrideHasNextPage: false,
        overrideIsError: false,
        overrideEvaluation: null,
      });
    }
  }, [contextKey, owner.key, queryEnabled]);

  const cancel = useCallback(() => {
    generationRef.current += 1;
    activeRunRef.current = null;
    if (!mountedRef.current) return;
    const runtime = runtimeRef.current;
    if (!runtime) return;
    setLocalState({
      contextKey: runtime.contextKey,
      ownerKey: runtime.ownerContext.key,
      loading: false,
      error: null,
      overrideData: undefined,
      overrideHasNextPage: false,
      overrideIsError: false,
      overrideEvaluation: null,
    });
  }, []);

  const go = useCallback(async (direction: IssueSequenceDirection) => {
    const initial = runtimeRef.current;
    const owner = initial?.ownerContext.owner;
    if (
      !mountedRef.current ||
      !initial ||
      !initial.queryEnabled ||
      !initial.ownerContext.key ||
      !owner ||
      !initial.current ||
      !sameOwner(initial.ownerContext.store, owner)
    ) {
      return null;
    }

    generationRef.current += 1;
    activeRunRef.current = null;
    const run: Run = {
      generation: generationRef.current,
      contextKey: initial.contextKey,
      ownerKey: initial.ownerContext.key,
      owner,
      direction,
    };
    activeRunRef.current = run;
    setLocalState({
      contextKey: run.contextKey,
      ownerKey: run.ownerKey,
      loading: true,
      error: null,
      overrideData: undefined,
      overrideHasNextPage: false,
      overrideIsError: false,
      overrideEvaluation: null,
    });

    const isCurrentRun = () => {
      const latest = runtimeRef.current;
      return Boolean(
        mountedRef.current &&
        activeRunRef.current === run &&
        generationRef.current === run.generation &&
        latest &&
        latest.contextKey === run.contextKey &&
        latest.ownerContext.key === run.ownerKey &&
        latest.queryEnabled &&
        sameOwner(latest.ownerContext.store, run.owner),
      );
    };

    const querySnapshot = initial.query;
    let stateForRun = toQuerySnapshot(querySnapshot);
    let pagesForRun = stateForRun.data?.pages ?? [];
    let hasDataForRun = stateForRun.data !== undefined;
    let hasMoreForRun = stateForRun.hasNextPage;
    let failedForRun = stateForRun.isError;

    const finish = (
      evaluation: DirectionEvaluation,
      error: Error | null,
      snapshot: QuerySnapshot,
    ) => {
      if (!isCurrentRun()) return;
      setLocalState({
        contextKey: run.contextKey,
        ownerKey: run.ownerKey,
        loading: false,
        error,
        overrideData: snapshot.data,
        overrideHasNextPage: snapshot.hasNextPage,
        overrideIsError: snapshot.isError,
        overrideEvaluation: evaluation,
      });
      activeRunRef.current = null;
    };

    try {
      let refreshedForRun = false;
      const staleOpenCurrent =
        initial.current.status !== "open" && hasOpenRecord(pagesForRun, initial.current.id);
      if (failedForRun || !hasDataForRun || staleOpenCurrent) {
        const refreshed = await querySnapshot.refetch({ cancelRefetch: false });
        if (!isCurrentRun()) return null;
        stateForRun = toQuerySnapshot(refreshed);
        pagesForRun = stateForRun.data?.pages ?? [];
        hasDataForRun = stateForRun.data !== undefined;
        hasMoreForRun = stateForRun.hasNextPage;
        failedForRun = stateForRun.isError;
        refreshedForRun = true;
        if (failedForRun || !hasDataForRun) {
          throw stateForRun.error ?? new Error("问题序列尚未加载");
        }
      }
      if (
        !refreshedForRun &&
        (querySnapshot.isRefetching ||
          (querySnapshot.isFetching && !querySnapshot.isFetchingNextPage) ||
          querySnapshot.isStale)
      ) {
        const refreshed = await querySnapshot.refetch({ cancelRefetch: false });
        if (!isCurrentRun()) return null;
        stateForRun = toQuerySnapshot(refreshed);
        pagesForRun = stateForRun.data?.pages ?? [];
        hasDataForRun = stateForRun.data !== undefined;
        hasMoreForRun = stateForRun.hasNextPage;
        failedForRun = stateForRun.isError;
        if (failedForRun || !hasDataForRun) {
          throw stateForRun.error ?? new Error("问题序列刷新失败");
        }
      }

      const seenCursors = new Set<string>();
      while (isCurrentRun()) {
        const latest = runtimeRef.current;
        if (!latest) return null;
        const evaluation = evaluateSequence({
          current: latest.current,
          currentAnchor: latest.currentAnchor,
          projectId: latest.projectId,
          taskId: latest.taskId,
          pages: pagesForRun,
          hasMore: hasMoreForRun,
          queryEnabled: true,
          queryError: failedForRun,
          queryHasData: hasDataForRun,
        });
        const state = direction === "previous" ? evaluation.previousState : evaluation.nextState;
        const neighbor = direction === "previous" ? evaluation.previous : evaluation.next;
        if (state === "available") {
          finish(evaluation, null, stateForRun);
          return neighbor;
        }
        if (state === "unavailable") {
          finish(evaluation, null, stateForRun);
          return null;
        }

        const cursor = readCursor(pagesForRun[pagesForRun.length - 1]);
        if (!cursor.valid) throw new Error("问题序列游标无效，无法继续分页");
        if (!hasMoreForRun || cursor.nextCursor === null) {
          finish(
            {
              ...evaluation,
              previousState: "unavailable",
              nextState: "unavailable",
            },
            null,
            stateForRun,
          );
          return null;
        }
        if (seenCursors.has(cursor.nextCursor)) {
          throw new Error("问题序列游标重复，无法继续分页");
        }
        seenCursors.add(cursor.nextCursor);

        const fetched = await querySnapshot.fetchNextPage({ cancelRefetch: false });
        if (!isCurrentRun()) return null;
        stateForRun = toQuerySnapshot(fetched);
        if (stateForRun.isError) {
          throw stateForRun.error ?? new Error("问题序列分页失败");
        }
        const nextPages = stateForRun.data?.pages ?? [];
        if (nextPages.length <= pagesForRun.length) {
          throw new Error("问题序列分页没有进展");
        }
        pagesForRun = nextPages;
        hasDataForRun = stateForRun.data !== undefined;
        hasMoreForRun = stateForRun.hasNextPage;
        failedForRun = stateForRun.isError;
      }
      return null;
    } catch (error) {
      if (!isCurrentRun()) return null;
      const latest = runtimeRef.current;
      if (!latest) return null;
      const evaluation = evaluateSequence({
        current: latest.current,
        currentAnchor: latest.currentAnchor,
        projectId: latest.projectId,
        taskId: latest.taskId,
        pages: pagesForRun,
        hasMore: hasMoreForRun,
        queryEnabled: true,
        queryError: failedForRun,
        queryHasData: hasDataForRun,
      });
      finish(evaluation, asError(error), stateForRun);
      return null;
    }
  }, []);

  const baseline = evaluateSequence({
    current: current ? { ...current } : null,
    currentAnchor,
    projectId,
    taskId,
    pages,
    hasMore: queryHasMore,
    queryEnabled,
    queryError: queryFailed,
    queryHasData,
  });
  const overrideMatches =
    localState.contextKey === contextKey &&
    localState.ownerKey === owner.key &&
    localState.overrideData === queryData &&
    localState.overrideHasNextPage === queryHasMore &&
    localState.overrideIsError === queryFailed &&
    localState.overrideEvaluation !== null;
  const displayed = overrideMatches ? localState.overrideEvaluation! : baseline;
  const error =
    localState.contextKey === contextKey && localState.ownerKey === owner.key
      ? (localState.error ?? (queryFailed ? asError(query.error) : null))
      : queryFailed
        ? asError(query.error)
        : null;
  const loading =
    localState.contextKey === contextKey && localState.ownerKey === owner.key
      ? localState.loading
      : false;

  return {
    previousState: displayed.previousState,
    nextState: displayed.nextState,
    loading,
    error,
    outsideScope,
    go,
    cancel,
  };
}
