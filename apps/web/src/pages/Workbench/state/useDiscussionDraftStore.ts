import type {
  AnnotationCommentAnchor,
  CommentAttachment,
  CommentCanvasDrawing,
  CommentMention,
} from "@/api/comments";
import {
  discussionTargetKey,
  type DiscussionOrigin,
  type DiscussionPayload,
  type DiscussionSessionOwner,
  type DiscussionTarget,
} from "./discussionTypes";
import { randomId as createId } from "@/utils/id";

export type DiscussionDraftStatus = "idle" | "dirty" | "submitting" | "error";

export interface DiscussionDraft extends DiscussionPayload {
  target: DiscussionTarget;
  targetKey: string;
  text: string;
  body: string;
  targetLabel: string | null;
  targetAvailable: boolean;
  unavailableReason: string | null;
  revision: number;
  status: DiscussionDraftStatus;
  error: string | null;
  inFlightRequestId: string | null;
  canvasOrigin: DiscussionOrigin | null;
  canvasActive: boolean;
}

export type DiscussionDraftPatch = {
  text?: string;
  body?: string;
  mentions?: CommentMention[];
  attachments?: CommentAttachment[];
  canvas_drawing?: CommentCanvasDrawing | null;
  anchor?: AnnotationCommentAnchor | null;
  targetLabel?: string | null;
  targetAvailable?: boolean;
  unavailableReason?: string | null;
};

export interface DiscussionTargetCapabilities {
  text: true;
  mentions: boolean;
  attachments: boolean;
  canvasDrawing: boolean;
  anchor: boolean;
}

export interface DiscussionSubmissionSnapshot {
  owner: DiscussionSessionOwner;
  target: DiscussionTarget;
  targetKey: string;
  revision: number;
  payload: DiscussionPayload;
  requestId: string;
}

export interface DiscussionDraftStoreSnapshot {
  owner: DiscussionSessionOwner;
  drafts: Readonly<Record<string, DiscussionDraft>>;
  sendTargets: Readonly<Record<string, DiscussionTarget>>;
  disposed: boolean;
  dirty: boolean;
  dirtyText: boolean;
}

export class UnsupportedDiscussionFieldError extends Error {
  readonly field: keyof DiscussionPayload;
  readonly targetKind: DiscussionTarget["kind"];

  constructor(field: keyof DiscussionPayload, target: DiscussionTarget) {
    super(`Field "${field}" is not supported for ${target.kind} discussion targets`);
    this.name = "UnsupportedDiscussionFieldError";
    this.field = field;
    this.targetKind = target.kind;
  }
}

export class DiscussionDraftStoreDisposedError extends Error {
  constructor() {
    super("The discussion draft session has been disposed");
    this.name = "DiscussionDraftStoreDisposedError";
  }
}

type Listener = () => void;
type DraftStoreInput = {
  owner: DiscussionSessionOwner;
  onDispose?: (owner: DiscussionSessionOwner) => void;
  isOwnerCurrent?: (owner: DiscussionSessionOwner) => boolean;
};

type DraftInternal = DiscussionDraft & {
  inFlight: DiscussionSubmissionSnapshot | null;
};

function clone<T>(value: T): T {
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => clone(item)) as T;
  const result: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    result[key] = clone(item);
  }
  return result as T;
}

function sameOwner(a: DiscussionSessionOwner, b: DiscussionSessionOwner): boolean {
  return a.sessionId === b.sessionId && a.userId === b.userId;
}

function targetId(target: DiscussionTarget): string {
  return discussionTargetKey(target);
}

function projectTaskKey(projectId: string, taskId: string): string {
  return JSON.stringify([projectId, taskId]);
}

export function discussionTargetCapabilities(
  target: DiscussionTarget,
): DiscussionTargetCapabilities {
  const annotation = target.kind === "annotation";
  const task = target.kind === "task";
  return {
    text: true,
    mentions: annotation || task,
    attachments: annotation,
    canvasDrawing: annotation || task,
    anchor: annotation,
  };
}

function fieldIsPresent(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (typeof value === "string") return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  return true;
}

export function validateDiscussionPayload(
  target: DiscussionTarget,
  payload: DiscussionPayload,
): void {
  const capabilities = discussionTargetCapabilities(target);
  const unsupported: Array<[keyof DiscussionPayload, unknown, boolean]> = [
    ["mentions", payload.mentions, capabilities.mentions],
    ["attachments", payload.attachments, capabilities.attachments],
    ["canvas_drawing", payload.canvas_drawing, capabilities.canvasDrawing],
    ["anchor", payload.anchor, capabilities.anchor],
  ];
  for (const [field, value, supported] of unsupported) {
    if (!supported && fieldIsPresent(value))
      throw new UnsupportedDiscussionFieldError(field, target);
  }
}

function normalizePayload(payload: DiscussionPayload): DiscussionPayload {
  return {
    body: payload.body,
    mentions: clone(payload.mentions ?? []),
    attachments: clone(payload.attachments ?? []),
    canvas_drawing: clone(payload.canvas_drawing ?? null),
    ...(payload.anchor === undefined ? {} : { anchor: clone(payload.anchor) }),
  };
}

function hasContent(
  draft: Pick<DiscussionDraft, "body" | "mentions" | "attachments" | "canvas_drawing">,
): boolean {
  return Boolean(
    draft.body.trim() ||
    draft.mentions.length > 0 ||
    draft.attachments.length > 0 ||
    (draft.canvas_drawing?.shapes?.length ?? 0) > 0,
  );
}

function hasTextContent(draft: Pick<DiscussionDraft, "body" | "mentions">): boolean {
  return Boolean(draft.body.trim() || draft.mentions.length > 0);
}

function emptyDraft(target: DiscussionTarget): DraftInternal {
  const key = targetId(target);
  return {
    target: clone(target),
    targetKey: key,
    text: "",
    body: "",
    mentions: [],
    attachments: [],
    canvas_drawing: null,
    anchor: null,
    targetLabel: null,
    targetAvailable: true,
    unavailableReason: null,
    revision: 0,
    status: "idle",
    error: null,
    inFlightRequestId: null,
    canvasOrigin: null,
    canvasActive: false,
    inFlight: null,
  };
}

function publicDraft(draft: DraftInternal): DiscussionDraft {
  const { inFlight: _inFlight, ...value } = draft;
  return clone(value);
}

function publicDrafts(drafts: Map<string, DraftInternal>): Record<string, DiscussionDraft> {
  const result: Record<string, DiscussionDraft> = {};
  for (const [key, draft] of drafts) result[key] = publicDraft(draft);
  return result;
}

function normalizePatch(target: DiscussionTarget, patch: DiscussionDraftPatch): DiscussionPayload {
  const payload: DiscussionPayload = {
    body: patch.body ?? patch.text ?? "",
    mentions: patch.mentions ?? [],
    attachments: patch.attachments ?? [],
    canvas_drawing: patch.canvas_drawing ?? null,
    anchor: patch.anchor ?? null,
  };
  validateDiscussionPayload(target, payload);
  return payload;
}

export interface DiscussionDraftStore {
  readonly owner: DiscussionSessionOwner;
  getOwner(): DiscussionSessionOwner;
  subscribe(listener: Listener): () => void;
  getSnapshot(): DiscussionDraftStoreSnapshot;
  getDraft(target: DiscussionTarget): DiscussionDraft | undefined;
  ensureDraft(target: DiscussionTarget, initial?: Partial<DiscussionDraftPatch>): DiscussionDraft;
  patchDraft(target: DiscussionTarget, patch: DiscussionDraftPatch): DiscussionDraft;
  setTargetAvailability(
    target: DiscussionTarget,
    available: boolean,
    reason?: string | null,
  ): DiscussionDraft;
  captureAnchor(
    target: DiscussionTarget,
    anchor: AnnotationCommentAnchor | null | undefined,
    options?: { force?: boolean },
  ): boolean;
  makeOrigin(target: DiscussionTarget, requestId?: string): DiscussionOrigin | null;
  isOwned(ownerOrOrigin: DiscussionSessionOwner | DiscussionOrigin): boolean;
  saveDrawing(
    origin: DiscussionOrigin,
    drawing: CommentCanvasDrawing | null,
    options?: { active?: boolean },
  ): boolean;
  startCanvasSession(origin: DiscussionOrigin, initial?: CommentCanvasDrawing | null): boolean;
  setCanvasSession(
    origin: DiscussionOrigin,
    drawing: CommentCanvasDrawing | null,
    active: boolean,
  ): boolean;
  getDrawing(origin: DiscussionOrigin): CommentCanvasDrawing | null;
  acceptDrawing(origin: DiscussionOrigin, drawing: CommentCanvasDrawing | null): boolean;
  acceptUpload(origin: DiscussionOrigin, attachment: CommentAttachment): boolean;
  disposeOrigin(origin: DiscussionOrigin): boolean;
  beginSubmission(
    target: DiscussionTarget,
    payload?: Partial<DiscussionPayload>,
  ): DiscussionSubmissionSnapshot | null;
  resolveSubmission(snapshot: DiscussionSubmissionSnapshot): boolean;
  rejectSubmission(snapshot: DiscussionSubmissionSnapshot, error: unknown): boolean;
  setSendTarget(projectId: string, taskId: string, target: DiscussionTarget): boolean;
  getSendTarget(projectId: string, taskId: string): DiscussionTarget | undefined;
  followAnnotationSelection(projectId: string, taskId: string, annotationId: string | null): void;
  hasDirtyDrafts(): boolean;
  hasDirtyTextDrafts(): boolean;
  dispose(): void;
}

export function createDiscussionDraftStore({
  owner,
  onDispose,
  isOwnerCurrent = () => true,
}: DraftStoreInput): DiscussionDraftStore {
  const sessionOwner = clone(owner);
  const drafts = new Map<string, DraftInternal>();
  const sendTargets = new Map<string, DiscussionTarget>();
  const selectedAnnotations = new Map<string, string | null>();
  const disposedOrigins = new Set<string>();
  const latestCanvasOrigins = new Map<string, string>();
  const listeners = new Set<Listener>();
  let disposed = false;
  let snapshot: DiscussionDraftStoreSnapshot = {
    owner: clone(sessionOwner),
    drafts: {},
    sendTargets: {},
    disposed: false,
    dirty: false,
    dirtyText: false,
  };

  const emit = () => {
    const nextSendTargets: Record<string, DiscussionTarget> = {};
    for (const [key, target] of sendTargets) nextSendTargets[key] = clone(target);
    snapshot = {
      owner: clone(sessionOwner),
      drafts: publicDrafts(drafts),
      sendTargets: nextSendTargets,
      disposed,
      dirty: Array.from(drafts.values()).some(
        (draft) => hasContent(draft) || draft.inFlight !== null,
      ),
      dirtyText: Array.from(drafts.values()).some(
        (draft) => hasTextContent(draft) || draft.inFlight !== null,
      ),
    };
    for (const listener of listeners) listener();
  };

  const assertActive = () => {
    if (disposed) throw new DiscussionDraftStoreDisposedError();
    let current = false;
    try {
      current = isOwnerCurrent(sessionOwner);
    } catch {
      current = false;
    }
    if (!current) throw new DiscussionDraftStoreDisposedError();
  };

  const ownerIsCurrent = () => {
    try {
      return !disposed && isOwnerCurrent(sessionOwner);
    } catch {
      return false;
    }
  };

  const find = (target: DiscussionTarget): DraftInternal | undefined =>
    drafts.get(targetId(target));

  const ensure = (
    target: DiscussionTarget,
    initial?: Partial<DiscussionDraftPatch>,
  ): DraftInternal => {
    assertActive();
    const key = targetId(target);
    const existing = drafts.get(key);
    if (existing) return existing;
    const draft = emptyDraft(target);
    if (initial) {
      const normalized = normalizePatch(target, initial);
      draft.body = normalized.body;
      draft.text = normalized.body;
      draft.mentions = clone(normalized.mentions);
      draft.attachments = clone(normalized.attachments);
      draft.canvas_drawing = clone(normalized.canvas_drawing);
      draft.anchor = clone(normalized.anchor ?? null);
      draft.targetLabel = initial.targetLabel ?? null;
      draft.targetAvailable = initial.targetAvailable ?? true;
      draft.unavailableReason = initial.unavailableReason ?? null;
      if (hasContent(draft)) {
        draft.revision = 1;
        draft.status = "dirty";
      }
    }
    drafts.set(key, draft);
    emit();
    return draft;
  };

  const ownerFor = (origin: DiscussionOrigin): boolean =>
    sameOwner(sessionOwner, origin.owner) &&
    ownerIsCurrent() &&
    !disposedOrigins.has(origin.requestId);

  const targetFor = (origin: DiscussionOrigin): DraftInternal | undefined => {
    if (!ownerFor(origin)) return undefined;
    return find(origin.target) ?? ensure(origin.target);
  };

  const mutateContent = (draft: DraftInternal, payload: DiscussionPayload) => {
    draft.body = payload.body;
    draft.text = payload.body;
    draft.mentions = clone(payload.mentions ?? []);
    draft.attachments = clone(payload.attachments ?? []);
    draft.canvas_drawing = clone(payload.canvas_drawing ?? null);
    draft.anchor = clone(payload.anchor ?? null);
    draft.revision += 1;
    draft.error = null;
    draft.status = hasContent(draft) ? "dirty" : "idle";
  };

  const store: DiscussionDraftStore = {
    owner: clone(sessionOwner),
    getOwner: () => clone(sessionOwner),
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => snapshot,
    getDraft: (target) => {
      if (!ownerIsCurrent()) return undefined;
      return snapshot.drafts[targetId(target)];
    },
    ensureDraft: (target, initial) => publicDraft(ensure(target, initial)),
    patchDraft: (target, patch) => {
      const draft = ensure(target);
      const hasPayloadPatch =
        patch.body !== undefined ||
        patch.text !== undefined ||
        patch.mentions !== undefined ||
        patch.attachments !== undefined ||
        patch.canvas_drawing !== undefined ||
        patch.anchor !== undefined;
      if (hasPayloadPatch) {
        const payload: DiscussionPayload = {
          body: patch.body ?? patch.text ?? draft.body,
          mentions: patch.mentions ?? draft.mentions,
          attachments: patch.attachments ?? draft.attachments,
          canvas_drawing:
            patch.canvas_drawing === undefined ? draft.canvas_drawing : patch.canvas_drawing,
          anchor: patch.anchor === undefined ? draft.anchor : patch.anchor,
        };
        validateDiscussionPayload(target, payload);
        const changed =
          payload.body !== draft.body ||
          JSON.stringify(payload.mentions) !== JSON.stringify(draft.mentions) ||
          JSON.stringify(payload.attachments) !== JSON.stringify(draft.attachments) ||
          JSON.stringify(payload.canvas_drawing) !== JSON.stringify(draft.canvas_drawing) ||
          JSON.stringify(payload.anchor) !== JSON.stringify(draft.anchor);
        if (changed) mutateContent(draft, payload);
      }
      if (patch.targetLabel !== undefined) draft.targetLabel = patch.targetLabel;
      if (patch.targetAvailable !== undefined) draft.targetAvailable = patch.targetAvailable;
      if (patch.unavailableReason !== undefined) draft.unavailableReason = patch.unavailableReason;
      emit();
      return publicDraft(draft);
    },
    setTargetAvailability: (target, available, reason = null) => {
      const draft = ensure(target);
      draft.targetAvailable = available;
      draft.unavailableReason = available ? null : (reason ?? "此讨论目标已不可访问");
      if (!available && draft.status === "submitting") {
        draft.status = hasContent(draft) ? "dirty" : "idle";
      }
      emit();
      return publicDraft(draft);
    },
    captureAnchor: (target, anchor, options) => {
      if (!anchor || disposed) return false;
      const draft = ensure(target);
      if (!options?.force && draft.anchor) return false;
      if (JSON.stringify(draft.anchor) === JSON.stringify(anchor)) return false;
      draft.anchor = clone(anchor);
      draft.revision += 1;
      draft.error = null;
      if (hasContent(draft)) draft.status = "dirty";
      emit();
      return true;
    },
    makeOrigin: (target, requestId = `discussion-${createId()}`) => {
      if (!ownerIsCurrent()) return null;
      ensure(target);
      const origin = {
        owner: clone(sessionOwner),
        target: clone(target),
        requestId,
      };
      latestCanvasOrigins.set(targetId(target), requestId);
      return origin;
    },
    isOwned: (ownerOrOrigin) => {
      if ("target" in ownerOrOrigin) return ownerFor(ownerOrOrigin);
      return ownerIsCurrent() && sameOwner(sessionOwner, ownerOrOrigin);
    },
    saveDrawing: (origin, drawing, options) => {
      const draft = targetFor(origin);
      if (!draft) return false;
      const key = targetId(origin.target);
      const latestOrigin = latestCanvasOrigins.get(key);
      if (latestOrigin && latestOrigin !== origin.requestId) return false;
      if (!latestOrigin) latestCanvasOrigins.set(key, origin.requestId);
      validateDiscussionPayload(origin.target, {
        body: draft.body,
        mentions: draft.mentions,
        attachments: draft.attachments,
        canvas_drawing: drawing,
        anchor: draft.anchor,
      });
      const active = options?.active ?? false;
      if (
        JSON.stringify(draft.canvas_drawing) === JSON.stringify(drawing) &&
        draft.canvasOrigin?.requestId === origin.requestId &&
        draft.canvasActive === active
      ) {
        return true;
      }
      draft.canvas_drawing = clone(drawing);
      draft.canvasOrigin = clone(origin);
      draft.canvasActive = active;
      draft.revision += 1;
      draft.error = null;
      draft.status = hasContent(draft) ? "dirty" : "idle";
      emit();
      return true;
    },
    startCanvasSession: (origin, initial) => {
      if (!ownerFor(origin)) return false;
      latestCanvasOrigins.set(targetId(origin.target), origin.requestId);
      if (initial !== undefined) return store.saveDrawing(origin, initial, { active: true });
      const draft = targetFor(origin);
      if (!draft) return false;
      if (draft.canvasOrigin?.requestId === origin.requestId && draft.canvasActive) return true;
      draft.canvasOrigin = clone(origin);
      draft.canvasActive = true;
      draft.error = null;
      emit();
      return true;
    },
    setCanvasSession: (origin, drawing, active) => store.saveDrawing(origin, drawing, { active }),
    getDrawing: (origin) => {
      const draft = targetFor(origin);
      return draft ? clone(draft.canvas_drawing) : null;
    },
    acceptDrawing: (origin, drawing) => store.saveDrawing(origin, drawing, { active: false }),
    acceptUpload: (origin, attachment) => {
      const draft = targetFor(origin);
      if (!draft) return false;
      try {
        validateDiscussionPayload(origin.target, {
          body: draft.body,
          mentions: draft.mentions,
          attachments: [...draft.attachments, attachment],
          canvas_drawing: draft.canvas_drawing,
          anchor: draft.anchor,
        });
      } catch {
        return false;
      }
      draft.attachments = [...draft.attachments, clone(attachment)];
      draft.revision += 1;
      draft.error = null;
      draft.status = "dirty";
      emit();
      return true;
    },
    disposeOrigin: (origin) => {
      if (!sameOwner(sessionOwner, origin.owner) || !ownerIsCurrent()) return false;
      disposedOrigins.add(origin.requestId);
      const draft = find(origin.target);
      if (!draft) return false;
      if (draft.canvasOrigin?.requestId !== origin.requestId) return true;
      draft.canvas_drawing = null;
      draft.canvasOrigin = null;
      draft.canvasActive = false;
      draft.revision += 1;
      draft.status = hasContent(draft) ? "dirty" : "idle";
      emit();
      return true;
    },
    beginSubmission: (target, payload) => {
      assertActive();
      const draft = ensure(target);
      if (!draft.targetAvailable || draft.inFlight || draft.canvasActive) return null;
      const supplied = payload ?? {};
      const candidate = normalizePayload({
        body: supplied.body ?? draft.body,
        mentions: supplied.mentions ?? draft.mentions,
        attachments: supplied.attachments ?? draft.attachments,
        canvas_drawing:
          supplied.canvas_drawing === undefined ? draft.canvas_drawing : supplied.canvas_drawing,
        anchor: supplied.anchor === undefined ? draft.anchor : supplied.anchor,
      });
      validateDiscussionPayload(target, candidate);
      if (
        !candidate.body.trim() &&
        candidate.attachments.length === 0 &&
        (candidate.canvas_drawing?.shapes?.length ?? 0) === 0
      ) {
        return null;
      }
      if (
        candidate.body !== draft.body ||
        JSON.stringify(candidate.mentions) !== JSON.stringify(draft.mentions) ||
        JSON.stringify(candidate.attachments) !== JSON.stringify(draft.attachments) ||
        JSON.stringify(candidate.canvas_drawing) !== JSON.stringify(draft.canvas_drawing) ||
        JSON.stringify(candidate.anchor) !== JSON.stringify(draft.anchor)
      ) {
        mutateContent(draft, candidate);
      }
      const requestId = `comment-${createId()}`;
      const submission: DiscussionSubmissionSnapshot = {
        owner: clone(sessionOwner),
        target: clone(target),
        targetKey: targetId(target),
        revision: draft.revision,
        payload: normalizePayload(candidate),
        requestId,
      };
      draft.inFlight = clone(submission);
      draft.inFlightRequestId = requestId;
      draft.status = "submitting";
      draft.error = null;
      emit();
      return clone(submission);
    },
    resolveSubmission: (submission) => {
      if (!sameOwner(sessionOwner, submission.owner) || !ownerIsCurrent()) return false;
      const draft = drafts.get(submission.targetKey);
      if (!draft || draft.inFlight?.requestId !== submission.requestId) return false;
      const unchanged = draft.revision === submission.revision;
      draft.inFlight = null;
      draft.inFlightRequestId = null;
      if (unchanged) {
        draft.body = "";
        draft.text = "";
        draft.mentions = [];
        draft.attachments = [];
        draft.canvas_drawing = null;
        draft.canvasOrigin = null;
        draft.canvasActive = false;
        latestCanvasOrigins.delete(submission.targetKey);
        draft.anchor = null;
        draft.error = null;
        draft.revision += 1;
        draft.status = "idle";
      } else {
        draft.status = hasContent(draft) ? "dirty" : "idle";
      }
      emit();
      return unchanged;
    },
    rejectSubmission: (submission, error) => {
      if (!sameOwner(sessionOwner, submission.owner) || !ownerIsCurrent()) return false;
      const draft = drafts.get(submission.targetKey);
      if (!draft || draft.inFlight?.requestId !== submission.requestId) return false;
      const unchanged = draft.revision === submission.revision;
      draft.inFlight = null;
      draft.inFlightRequestId = null;
      if (unchanged) {
        draft.error = error instanceof Error ? error.message : String(error ?? "提交失败");
        draft.status = hasContent(draft) ? "error" : "idle";
      } else {
        draft.error = null;
        draft.status = hasContent(draft) ? "dirty" : "idle";
      }
      emit();
      return unchanged;
    },
    setSendTarget: (projectId, taskId, target) => {
      if (target.projectId !== projectId || target.taskId !== taskId || !ownerIsCurrent())
        return false;
      ensure(target);
      sendTargets.set(projectTaskKey(projectId, taskId), clone(target));
      emit();
      return true;
    },
    getSendTarget: (projectId, taskId) => {
      const target = sendTargets.get(projectTaskKey(projectId, taskId));
      return target ? clone(target) : undefined;
    },
    followAnnotationSelection: (projectId, taskId, annotationId) => {
      if (!ownerIsCurrent()) return;
      const key = projectTaskKey(projectId, taskId);
      const hadSelection = selectedAnnotations.has(key);
      if (selectedAnnotations.get(key) === annotationId) return;
      selectedAnnotations.set(key, annotationId);
      if (!annotationId && !hadSelection) return;
      store.setSendTarget(
        projectId,
        taskId,
        annotationId
          ? { projectId, taskId, kind: "annotation", annotationId }
          : { projectId, taskId, kind: "task" },
      );
    },
    hasDirtyDrafts: () => ownerIsCurrent() && snapshot.dirty,
    hasDirtyTextDrafts: () => ownerIsCurrent() && snapshot.dirtyText,
    dispose: () => {
      if (disposed) return;
      disposed = true;
      drafts.clear();
      sendTargets.clear();
      selectedAnnotations.clear();
      disposedOrigins.clear();
      latestCanvasOrigins.clear();
      snapshot = {
        owner: clone(sessionOwner),
        drafts: {},
        sendTargets: {},
        disposed: true,
        dirty: false,
        dirtyText: false,
      };
      for (const listener of listeners) listener();
      listeners.clear();
      onDispose?.(clone(sessionOwner));
    },
  };

  return store;
}

export function createDiscussionSessionId(): string {
  return `session-${createId()}`;
}

export type DiscussionDraftStoreLike = DiscussionDraftStore;
